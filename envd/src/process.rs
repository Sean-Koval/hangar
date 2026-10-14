//! `process.Process`: start commands (piped or on a PTY), stream their output, reattach, send input and signals,
//! resize terminals.
//!
//! Output is never buffered for later: each process has one pump task that reads its stdout/stderr (or PTY) and
//! hands every chunk to the clients streaming it right now, waiting for each (so a slow client slows the process,
//! like a terminal, instead of growing memory). With nobody attached, output is read and dropped, as in E2B.

use std::collections::HashMap;
use std::io;
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::process::ExitStatusExt;
use std::process::{ExitStatus, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use buffa::MessageField;
use bytes::Bytes;
use connectrpc::{ConnectError, InboundStream, RequestContext, Response, ServiceRequest, ServiceResult, ServiceStream};
use futures::StreamExt;
use nix::sys::signal::{Signal as Sig, kill};
use nix::unistd::Pid;
use tokio::io::unix::AsyncFd;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::mpsc;

use crate::config::Config;
use crate::paths::{check_user_header, resolve};
use crate::proto::process::process_event::{DataEvent, EndEvent, Event, StartEvent};
use crate::proto::process::*;
use crate::stream::{keepalive_interval, with_keepalive};

const CHUNK: usize = 32 * 1024;
/// Chunks queued per attached client before the process waits for it.
const SUB_QUEUE: usize = 16;
const MAX_SUBSCRIBERS: usize = 8;
/// How long the process waits for one client to take a chunk before giving up on that client.
const STALLED_CLIENT: Duration = Duration::from_secs(60);
/// After the process exits, how long to keep reading output its leftover children still write to the same pipes.
const DRAIN_AFTER_EXIT: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, Debug, PartialEq)]
enum Kind {
    Stdout,
    Stderr,
    Pty,
}

#[derive(Clone)]
enum Ev {
    Data(Kind, Bytes),
    End(EndEvent),
}

impl Ev {
    fn to_proto(&self) -> ProcessEvent {
        use crate::proto::process::process_event::data_event::Output;
        let event = match self {
            Ev::Data(k, b) => {
                let v = b.to_vec();
                let output = match k {
                    Kind::Stdout => Output::Stdout(v),
                    Kind::Stderr => Output::Stderr(v),
                    Kind::Pty => Output::Pty(v),
                };
                Event::Data(Box::new(DataEvent { output: Some(output), ..Default::default() }))
            }
            Ev::End(e) => Event::End(Box::new(e.clone())),
        };
        ProcessEvent { event: Some(event), ..Default::default() }
    }
}

fn start_event(pid: u32) -> ProcessEvent {
    ProcessEvent { event: Some(Event::Start(Box::new(StartEvent { pid, ..Default::default() }))), ..Default::default() }
}

fn keepalive_event() -> ProcessEvent {
    ProcessEvent { event: Some(Event::Keepalive(Box::default())), ..Default::default() }
}

/// The controller side of a pseudo-terminal, non-blocking, readable and writable from different tasks.
struct Pty {
    fd: AsyncFd<OwnedFd>,
}

impl Pty {
    async fn read(&self, buf: &mut [u8]) -> io::Result<usize> {
        loop {
            let mut g = self.fd.readable().await?;
            match g.try_io(|fd| nix::unistd::read(fd.get_ref(), buf).map_err(io::Error::from)) {
                Ok(Err(e)) if e.raw_os_error() == Some(libc::EIO) => return Ok(0), // every terminal side closed
                Ok(r) => return r,
                Err(_would_block) => continue,
            }
        }
    }

    async fn write_all(&self, mut data: &[u8]) -> io::Result<()> {
        while !data.is_empty() {
            let mut g = self.fd.writable().await?;
            match g.try_io(|fd| nix::unistd::write(fd.get_ref(), data).map_err(io::Error::from)) {
                Ok(Ok(n)) => data = &data[n..],
                Ok(Err(e)) => return Err(e),
                Err(_would_block) => continue,
            }
        }
        Ok(())
    }

    fn resize(&self, cols: u16, rows: u16) -> io::Result<()> {
        let ws = libc::winsize { ws_row: rows, ws_col: cols, ws_xpixel: 0, ws_ypixel: 0 };
        // SAFETY: TIOCSWINSZ reads one winsize from the pointer, which outlives the call.
        let r = unsafe { libc::ioctl(self.fd.get_ref().as_raw_fd(), libc::TIOCSWINSZ as _, &ws) };
        if r < 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
    }
}

enum Input {
    Pipe(tokio::sync::Mutex<Option<ChildStdin>>),
    Pty(Arc<Pty>),
}

struct Subs {
    list: Vec<mpsc::Sender<Ev>>,
    finished: bool,
}

struct Proc {
    pid: u32,
    tag: Option<String>,
    config: ProcessConfig,
    input: Input,
    subs: std::sync::Mutex<Subs>,
}

impl Proc {
    fn subscribe(&self) -> Result<mpsc::Receiver<Ev>, ConnectError> {
        let mut s = crate::lock(&self.subs);
        if s.finished {
            return Err(ConnectError::not_found(format!("process {} has exited", self.pid)));
        }
        s.list.retain(|t| !t.is_closed());
        if s.list.len() >= MAX_SUBSCRIBERS {
            return Err(ConnectError::resource_exhausted(format!(
                "process {} already has {MAX_SUBSCRIBERS} clients attached",
                self.pid
            )));
        }
        let (tx, rx) = mpsc::channel(SUB_QUEUE);
        s.list.push(tx);
        Ok(rx)
    }

    /// Hands `ev` to every attached client, waiting for each: output is never queued beyond SUB_QUEUE chunks per
    /// client. A client that takes nothing for STALLED_CLIENT (a dead connection nobody closed) is let go, so it
    /// can't hold the process up for good; its stream ends without an exit event.
    async fn fanout(&self, ev: Ev) {
        let subs: Vec<mpsc::Sender<Ev>> = crate::lock(&self.subs).list.clone();
        let mut gone = Vec::new();
        for s in &subs {
            match tokio::time::timeout(STALLED_CLIENT, s.send(ev.clone())).await {
                Ok(Ok(())) => {}
                _ => gone.push(s.clone()),
            }
        }
        if !gone.is_empty() {
            crate::lock(&self.subs).list.retain(|t| !t.is_closed() && !gone.iter().any(|g| g.same_channel(t)));
        }
    }

    /// Process-group wide, so a shell's children go too; the group is the process's own (it leads a new one).
    fn signal(&self, sig: Sig) -> Result<(), ConnectError> {
        let pid = self.pid as i32;
        kill(Pid::from_raw(-pid), sig)
            .or_else(|_| kill(Pid::from_raw(pid), sig))
            .map_err(|e| ConnectError::internal(format!("signal {sig} to {pid}: {e}")))
    }
}

/// How long an exited process's end event stays available to `Connect` (upstream envd keeps 30 s).
const EXITED_RETENTION: Duration = Duration::from_secs(30);

type Exited = Arc<std::sync::Mutex<HashMap<u32, (std::time::Instant, Option<String>, EndEvent)>>>;

pub struct Procs {
    cfg: Arc<Config>,
    table: Arc<std::sync::Mutex<HashMap<u32, Arc<Proc>>>>,
    exited: Exited,
    live: Arc<AtomicUsize>,
}

impl Procs {
    pub fn new(cfg: Arc<Config>) -> Self {
        Procs { cfg, table: Default::default(), exited: Default::default(), live: Default::default() }
    }

    /// The end event of a process that exited within EXITED_RETENTION.
    fn recently_exited(&self, sel: Option<&ProcessSelector>) -> Option<(u32, EndEvent)> {
        use crate::proto::process::process_selector::Selector;
        let mut ex = crate::lock(&self.exited);
        ex.retain(|_, (t, _, _)| t.elapsed() < EXITED_RETENTION);
        match sel.and_then(|s| s.selector.as_ref())? {
            Selector::Pid(pid) => ex.get(pid).map(|(_, _, e)| (*pid, e.clone())),
            Selector::Tag(tag) => ex
                .iter()
                .find(|(_, (_, t, _))| t.as_deref() == Some(tag.as_str()))
                .map(|(pid, (_, _, e))| (*pid, e.clone())),
        }
    }

    pub fn running(&self) -> usize {
        self.live.load(Ordering::Relaxed)
    }

    /// SIGKILL every process this daemon started (its shutdown).
    pub fn kill_all(&self) {
        for p in crate::lock(&self.table).values() {
            let _ = p.signal(Sig::SIGKILL);
        }
    }

    fn find(&self, sel: Option<&ProcessSelector>) -> Result<Arc<Proc>, ConnectError> {
        use crate::proto::process::process_selector::Selector;
        let t = crate::lock(&self.table);
        match sel.and_then(|s| s.selector.as_ref()) {
            Some(Selector::Pid(pid)) => t.get(pid).cloned().ok_or_else(|| ConnectError::not_found(format!("no running process {pid}"))),
            Some(Selector::Tag(tag)) => t
                .values()
                .find(|p| p.tag.as_deref() == Some(tag.as_str()))
                .cloned()
                .ok_or_else(|| ConnectError::not_found(format!("no running process tagged {tag}"))),
            None => Err(ConnectError::invalid_argument("a process selector (pid or tag) is required")),
        }
    }

    fn spawn(&self, req: &StartRequest) -> Result<(Child, Input, Option<Arc<Pty>>), ConnectError> {
        let pc = req.process.as_option().ok_or_else(|| ConnectError::invalid_argument("process is required"))?;
        if pc.cmd.is_empty() {
            return Err(ConnectError::invalid_argument("process.cmd is required"));
        }
        let cwd = resolve(&self.cfg, pc.cwd.as_deref().unwrap_or(self.cfg.workdir.to_str().unwrap_or("/")))?;
        if !cwd.is_dir() {
            return Err(ConnectError::invalid_argument(format!("cwd '{}' does not exist", cwd.display())));
        }
        let mut cmd = Command::new(&pc.cmd);
        cmd.args(&pc.args).current_dir(&cwd).envs(&pc.envs);
        let err = |e: io::Error| match e.kind() {
            io::ErrorKind::NotFound => ConnectError::not_found(format!("{}: command not found", pc.cmd)),
            io::ErrorKind::PermissionDenied => ConnectError::permission_denied(format!("{}: {e}", pc.cmd)),
            _ => ConnectError::internal(format!("start {}: {e}", pc.cmd)),
        };
        match req.pty.as_option() {
            Some(pty) => {
                let size = pty.size.as_option();
                let ws = nix::pty::Winsize {
                    ws_row: size.map(|s| s.rows).unwrap_or(24).clamp(1, 1000) as u16,
                    ws_col: size.map(|s| s.cols).unwrap_or(80).clamp(1, 1000) as u16,
                    ws_xpixel: 0,
                    ws_ypixel: 0,
                };
                let p = nix::pty::openpty(Some(&ws), None).map_err(|e| ConnectError::resource_exhausted(format!("openpty: {e}")))?;
                for fd in [&p.master, &p.slave] {
                    set_cloexec(fd).map_err(err)?;
                }
                nix::fcntl::fcntl(&p.master, nix::fcntl::FcntlArg::F_SETFL(nix::fcntl::OFlag::O_NONBLOCK))
                    .map_err(|e| err(e.into()))?;
                let slave = p.slave;
                cmd.stdin(Stdio::from(slave.try_clone().map_err(err)?))
                    .stdout(Stdio::from(slave.try_clone().map_err(err)?))
                    .stderr(Stdio::from(slave));
                if !pc.envs.contains_key("TERM") {
                    cmd.env("TERM", "xterm-256color");
                }
                // SAFETY: only async-signal-safe calls between fork and exec: a new session, with the terminal on
                // stdin as its controlling terminal.
                unsafe {
                    cmd.pre_exec(|| {
                        if libc::setsid() < 0 || libc::ioctl(0, libc::TIOCSCTTY as _, 0) < 0 {
                            return Err(io::Error::last_os_error());
                        }
                        prefer_for_oom_kill();
                        Ok(())
                    });
                }
                let child = cmd.spawn().map_err(err)?;
                drop(cmd); // closes our copies of the terminal side, so EOF arrives when the process's copies close
                let pty = Arc::new(Pty { fd: AsyncFd::new(p.master).map_err(err)? });
                Ok((child, Input::Pty(pty.clone()), Some(pty)))
            }
            None => {
                let stdin = req.stdin.unwrap_or(true); // E2B: absent means on, for old clients
                cmd.process_group(0)
                    .stdin(if stdin { Stdio::piped() } else { Stdio::null() })
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped());
                // SAFETY: open/write/close only, all async-signal-safe.
                unsafe {
                    cmd.pre_exec(|| {
                        prefer_for_oom_kill();
                        Ok(())
                    });
                }
                let mut child = cmd.spawn().map_err(err)?;
                let input = Input::Pipe(tokio::sync::Mutex::new(child.stdin.take()));
                Ok((child, input, None))
            }
        }
    }

    fn launch(&self, req: StartRequest) -> Result<(Arc<Proc>, mpsc::Receiver<Ev>), ConnectError> {
        if self.live.fetch_add(1, Ordering::SeqCst) >= self.cfg.max_processes {
            self.live.fetch_sub(1, Ordering::SeqCst);
            return Err(ConnectError::resource_exhausted(format!(
                "{} processes are already running (the limit)",
                self.cfg.max_processes
            )));
        }
        let slot = Slot(self.live.clone());
        let (mut child, input, pty) = self.spawn(&req)?;
        let pid = child.id().ok_or_else(|| ConnectError::internal("process exited before it had a pid"))?;
        let proc_ = Arc::new(Proc {
            pid,
            tag: req.tag.clone(),
            config: req.process.as_option().cloned().unwrap_or_default(),
            input,
            subs: std::sync::Mutex::new(Subs { list: Vec::new(), finished: false }),
        });
        let rx = proc_.subscribe()?; // before the pump starts: the first byte of output must reach the starter
        crate::lock(&self.table).insert(pid, proc_.clone());
        let p = proc_.clone();
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();
        let registry = self.table.clone();
        let exited = self.exited.clone();
        tokio::spawn(async move {
            let status = match pty {
                Some(pty) => pump_pty(&p, &mut child, pty).await,
                None => pump_pipes(&p, &mut child, stdout, stderr).await,
            };
            let end = end_event(status);
            let subs = {
                let mut s = crate::lock(&p.subs);
                s.finished = true;
                std::mem::take(&mut s.list)
            };
            crate::lock(&exited).insert(p.pid, (std::time::Instant::now(), p.tag.clone(), end.clone()));
            crate::lock(&registry).remove(&p.pid);
            drop(slot);
            for s in subs {
                let _ = tokio::time::timeout(Duration::from_secs(10), s.send(Ev::End(end.clone()))).await;
            }
        });
        Ok((proc_, rx))
    }
}

/// One counted process slot, released when the process has ended.
struct Slot(Arc<AtomicUsize>);
impl Drop for Slot {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

/// Raise this (forked, not yet exec'd) process's OOM score, so that under memory pressure the kernel kills the
/// workload before the daemon. Raising needs no privilege. Best effort; only async-signal-safe calls.
fn prefer_for_oom_kill() {
    // SAFETY: a NUL-terminated path and a buffer that outlive the calls.
    unsafe {
        let fd = libc::open(c"/proc/self/oom_score_adj".as_ptr(), libc::O_WRONLY | libc::O_CLOEXEC);
        if fd >= 0 {
            libc::write(fd, b"100".as_ptr().cast(), 3);
            libc::close(fd);
        }
    }
}

fn set_cloexec(fd: &OwnedFd) -> io::Result<()> {
    nix::fcntl::fcntl(fd, nix::fcntl::FcntlArg::F_SETFD(nix::fcntl::FdFlag::FD_CLOEXEC))
        .map(|_| ())
        .map_err(io::Error::from)
}

async fn read_some<R: AsyncRead + Unpin>(r: &mut Option<R>, buf: &mut [u8]) -> io::Result<usize> {
    match r {
        Some(r) => r.read(buf).await,
        None => std::future::pending().await,
    }
}

async fn pump_pipes(
    p: &Proc,
    child: &mut Child,
    mut stdout: Option<tokio::process::ChildStdout>,
    mut stderr: Option<tokio::process::ChildStderr>,
) -> io::Result<ExitStatus> {
    let (mut ob, mut eb) = (vec![0u8; CHUNK], vec![0u8; CHUNK]);
    let mut status: Option<io::Result<ExitStatus>> = None;
    let mut drain_until: Option<tokio::time::Instant> = None;
    loop {
        if stdout.is_none() && stderr.is_none() && status.is_some() {
            break;
        }
        tokio::select! {
            r = read_some(&mut stdout, &mut ob), if stdout.is_some() => match r {
                Ok(n) if n > 0 => p.fanout(Ev::Data(Kind::Stdout, Bytes::copy_from_slice(&ob[..n]))).await,
                _ => stdout = None,
            },
            r = read_some(&mut stderr, &mut eb), if stderr.is_some() => match r {
                Ok(n) if n > 0 => p.fanout(Ev::Data(Kind::Stderr, Bytes::copy_from_slice(&eb[..n]))).await,
                _ => stderr = None,
            },
            s = child.wait(), if status.is_none() => {
                status = Some(s);
                drain_until = Some(tokio::time::Instant::now() + DRAIN_AFTER_EXIT);
            }
            _ = tokio::time::sleep_until(drain_until.unwrap_or_else(tokio::time::Instant::now)), if drain_until.is_some() => break,
        }
    }
    status.unwrap_or_else(|| Err(io::Error::other("no exit status")))
}

async fn pump_pty(p: &Proc, child: &mut Child, pty: Arc<Pty>) -> io::Result<ExitStatus> {
    let mut buf = vec![0u8; CHUNK];
    let mut open = true;
    let mut status: Option<io::Result<ExitStatus>> = None;
    let mut drain_until: Option<tokio::time::Instant> = None;
    loop {
        if !open && status.is_some() {
            break;
        }
        tokio::select! {
            r = pty.read(&mut buf), if open => match r {
                Ok(n) if n > 0 => p.fanout(Ev::Data(Kind::Pty, Bytes::copy_from_slice(&buf[..n]))).await,
                _ => open = false,
            },
            s = child.wait(), if status.is_none() => {
                status = Some(s);
                drain_until = Some(tokio::time::Instant::now() + DRAIN_AFTER_EXIT);
            }
            _ = tokio::time::sleep_until(drain_until.unwrap_or_else(tokio::time::Instant::now)), if drain_until.is_some() => break,
        }
    }
    status.unwrap_or_else(|| Err(io::Error::other("no exit status")))
}

fn signal_name(sig: i32) -> String {
    match sig {
        libc::SIGKILL => "killed".into(),
        libc::SIGTERM => "terminated".into(),
        libc::SIGINT => "interrupt".into(),
        libc::SIGHUP => "hangup".into(),
        libc::SIGSEGV => "segmentation fault".into(),
        libc::SIGABRT => "aborted".into(),
        libc::SIGPIPE => "broken pipe".into(),
        n => format!("signal {n}"),
    }
}

/// Go's `ProcessState` conventions, which E2B clients expect: -1 and `signal: killed` for a signal.
fn end_event(status: io::Result<ExitStatus>) -> EndEvent {
    match status {
        Ok(s) => match (s.code(), s.signal()) {
            (Some(c), _) => EndEvent {
                exit_code: c,
                exited: true,
                status: format!("exit status {c}"),
                error: (c != 0).then(|| format!("exit status {c}")),
                ..Default::default()
            },
            (None, Some(sig)) => EndEvent {
                exit_code: -1,
                exited: false,
                status: format!("signal: {}", signal_name(sig)),
                error: Some(format!("signal: {}", signal_name(sig))),
                ..Default::default()
            },
            _ => EndEvent { exit_code: -1, status: "unknown".into(), ..Default::default() },
        },
        Err(e) => EndEvent { exit_code: -1, status: "unknown".into(), error: Some(e.to_string()), ..Default::default() },
    }
}

fn event_stream<T: Send + 'static>(
    ctx: &RequestContext,
    pid: u32,
    rx: mpsc::Receiver<Ev>,
    wrap: fn(ProcessEvent) -> T,
) -> ServiceStream<T> {
    let mut rx = rx;
    let events = futures::stream::poll_fn(move |cx| rx.poll_recv(cx)).map(move |ev| wrap(ev.to_proto()));
    let s = futures::stream::once(async move { wrap(start_event(pid)) }).chain(events);
    Box::pin(with_keepalive(s, keepalive_interval(ctx), ctx.deadline(), move || wrap(keepalive_event())))
}

fn as_start(e: ProcessEvent) -> StartResponse {
    StartResponse { event: MessageField::some(e), ..Default::default() }
}

fn as_connect(e: ProcessEvent) -> ConnectResponse {
    ConnectResponse { event: MessageField::some(e), ..Default::default() }
}

async fn write_input(p: &Proc, input: Option<&ProcessInput>) -> Result<(), ConnectError> {
    use crate::proto::process::process_input::Input as In;
    let data: &[u8] = match input.and_then(|i| i.input.as_ref()) {
        Some(In::Stdin(d)) => d,
        Some(In::Pty(d)) => {
            if matches!(p.input, Input::Pipe(_)) {
                return Err(ConnectError::invalid_argument(format!("process {} has no terminal; send stdin", p.pid)));
            }
            d
        }
        None => return Err(ConnectError::invalid_argument("input is required")),
    };
    let werr = |e: io::Error| ConnectError::failed_precondition(format!("write to process {}: {e}", p.pid));
    match &p.input {
        Input::Pty(pty) => pty.write_all(data).await.map_err(werr),
        Input::Pipe(m) => {
            let mut g = m.lock().await;
            let w = g
                .as_mut()
                .ok_or_else(|| ConnectError::failed_precondition(format!("stdin of process {} is closed or was not requested", p.pid)))?;
            w.write_all(data).await.map_err(werr)?;
            w.flush().await.map_err(werr)
        }
    }
}

#[allow(refining_impl_trait)]
impl Process for Procs {
    async fn list(&self, ctx: RequestContext, _request: ServiceRequest<'_, ListRequest>) -> ServiceResult<ListResponse> {
        check_user_header(&self.cfg, ctx.headers())?;
        let mut processes: Vec<ProcessInfo> = self
            .table
            .lock()
            .unwrap()
            .values()
            .map(|p| ProcessInfo { config: MessageField::some(p.config.clone()), pid: p.pid, tag: p.tag.clone(), ..Default::default() })
            .collect();
        processes.sort_by_key(|p| p.pid);
        Response::ok(ListResponse { processes, ..Default::default() })
    }

    async fn connect(&self, ctx: RequestContext, request: ServiceRequest<'_, ConnectRequest>) -> ServiceResult<ServiceStream<ConnectResponse>> {
        check_user_header(&self.cfg, ctx.headers())?;
        let req = request.to_owned_message();
        let found = self.find(req.process.as_option()).and_then(|p| p.subscribe().map(|rx| (p.pid, rx)));
        match found {
            Ok((pid, rx)) => Ok(Response::new(event_stream(&ctx, pid, rx, as_connect))),
            Err(e) => {
                let Some((pid, end)) = self.recently_exited(req.process.as_option()) else { return Err(e) };
                let (tx, rx) = mpsc::channel(1);
                let _ = tx.try_send(Ev::End(end));
                Ok(Response::new(event_stream(&ctx, pid, rx, as_connect)))
            }
        }
    }

    async fn start(&self, ctx: RequestContext, request: ServiceRequest<'_, StartRequest>) -> ServiceResult<ServiceStream<StartResponse>> {
        check_user_header(&self.cfg, ctx.headers())?;
        let req = request.to_owned_message();
        let (p, rx) = self.launch(req)?;
        if let Some(deadline) = ctx.deadline() {
            // Upstream envd's semantics: Connect-Timeout-Ms on Start bounds the process, not just the stream.
            let p = p.clone();
            tokio::spawn(async move {
                tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)).await;
                if !crate::lock(&p.subs).finished {
                    let _ = p.signal(Sig::SIGKILL);
                }
            });
        }
        Ok(Response::new(event_stream(&ctx, p.pid, rx, as_start)))
    }

    async fn update(&self, ctx: RequestContext, request: ServiceRequest<'_, UpdateRequest>) -> ServiceResult<UpdateResponse> {
        check_user_header(&self.cfg, ctx.headers())?;
        let req = request.to_owned_message();
        let p = self.find(req.process.as_option())?;
        let Input::Pty(pty) = &p.input else {
            return Err(ConnectError::failed_precondition(format!("process {} has no terminal to resize", p.pid)));
        };
        if let Some(size) = req.pty.as_option().and_then(|t| t.size.as_option()) {
            pty.resize(size.cols.clamp(1, 1000) as u16, size.rows.clamp(1, 1000) as u16)
                .map_err(|e| ConnectError::internal(format!("resize: {e}")))?;
        }
        Response::ok(UpdateResponse::default())
    }

    async fn stream_input(&self, ctx: RequestContext, mut requests: InboundStream<StreamInputRequest>) -> ServiceResult<StreamInputResponse> {
        use crate::proto::process::stream_input_request::Event as In;
        check_user_header(&self.cfg, ctx.headers())?;
        let mut target: Option<Arc<Proc>> = None;
        while let Some(msg) = requests.next().await {
            let msg = msg?.to_owned_message();
            match msg.event {
                Some(In::Start(s)) => target = Some(self.find(s.process.as_option())?),
                Some(In::Data(d)) => {
                    let p = target.as_ref().ok_or_else(|| ConnectError::invalid_argument("the first message must select the process"))?;
                    write_input(p, d.input.as_option()).await?;
                }
                Some(In::Keepalive(_)) | None => {}
            }
        }
        Response::ok(StreamInputResponse::default())
    }

    async fn send_input(&self, ctx: RequestContext, request: ServiceRequest<'_, SendInputRequest>) -> ServiceResult<SendInputResponse> {
        check_user_header(&self.cfg, ctx.headers())?;
        let req = request.to_owned_message();
        let p = self.find(req.process.as_option())?;
        write_input(&p, req.input.as_option()).await?;
        Response::ok(SendInputResponse::default())
    }

    async fn send_signal(&self, ctx: RequestContext, request: ServiceRequest<'_, SendSignalRequest>) -> ServiceResult<SendSignalResponse> {
        check_user_header(&self.cfg, ctx.headers())?;
        let req = request.to_owned_message();
        let p = self.find(req.process.as_option())?;
        let sig = match req.signal.as_known() {
            Some(Signal::SIGNAL_SIGKILL) => Sig::SIGKILL,
            Some(Signal::SIGNAL_SIGTERM) => Sig::SIGTERM,
            _ => return Err(ConnectError::invalid_argument("signal must be SIGTERM or SIGKILL")),
        };
        p.signal(sig)?;
        Response::ok(SendSignalResponse::default())
    }

    async fn close_stdin(&self, ctx: RequestContext, request: ServiceRequest<'_, CloseStdinRequest>) -> ServiceResult<CloseStdinResponse> {
        check_user_header(&self.cfg, ctx.headers())?;
        let req = request.to_owned_message();
        let p = self.find(req.process.as_option())?;
        match &p.input {
            Input::Pipe(m) => drop(m.lock().await.take()),
            Input::Pty(_) => {
                return Err(ConnectError::failed_precondition(format!(
                    "process {} runs on a terminal; send Ctrl-D (0x04) instead",
                    p.pid
                )));
            }
        }
        Response::ok(CloseStdinResponse::default())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn end_events_follow_go_conventions() {
        let ok = end_event(Ok(ExitStatus::from_raw(3 << 8)));
        assert_eq!((ok.exit_code, ok.exited, ok.status.as_str()), (3, true, "exit status 3"));
        let killed = end_event(Ok(ExitStatus::from_raw(libc::SIGKILL)));
        assert_eq!((killed.exit_code, killed.exited, killed.status.as_str()), (-1, false, "signal: killed"));
    }

    #[test]
    fn a_panic_elsewhere_does_not_break_the_process_table() {
        // A panic while a lock is held poisons it; every later request would then panic on it too.
        let cfg = Arc::new(crate::config::Config::parse(vec![], Some("0123456789abcdef".into())).unwrap());
        let procs = Arc::new(Procs::new(cfg));
        let p = procs.clone();
        let _ = std::thread::spawn(move || {
            let _g = p.table.lock();
            panic!("a bug somewhere");
        })
        .join();
        assert!(std::panic::catch_unwind(|| procs.kill_all()).is_ok());
        assert!(std::panic::catch_unwind(|| procs.find(None).is_err()).is_ok());
    }
}
