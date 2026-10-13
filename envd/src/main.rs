//! hangar-envd: the in-sandbox daemon hangar talks to instead of `docker exec` / `kubectl exec`.
//!
//! It speaks E2B envd's protocol (Connect RPC `process.Process` and `filesystem.Filesystem`, plus `/files` and
//! `/health`) over HTTP/1.1 and cleartext HTTP/2 on one port, and requires the access token hangar gave it on every
//! request but `/health`. It runs as the sandbox user and can do nothing that user couldn't: the trust boundary is
//! the control plane, which treats every answer from it as untrusted.

mod auth;
mod config;
mod entry;
mod files;
mod fs;
mod paths;
mod process;
mod stream;
#[cfg(test)]
mod tests;

pub mod proto {
    connectrpc::include_generated!();
}

use std::net::SocketAddr;
use std::os::fd::AsRawFd;
use std::sync::Arc;

use axum::Router;
use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;

use crate::config::{Config, TOKEN_ENV};
use crate::entry::Names;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

pub struct App {
    pub cfg: Arc<Config>,
    pub procs: Arc<process::Procs>,
    pub auth: auth::Auth,
}

fn denied(why: &str) -> Response {
    let body = serde_json::json!({"code": "unauthenticated", "message": why});
    (StatusCode::UNAUTHORIZED, [("content-type", "application/json")], body.to_string()).into_response()
}

/// Deny by default: everything but `/health` needs the token or a signature made with it (auth.rs).
async fn require_auth(State(app): State<Arc<App>>, req: Request, next: Next) -> Response {
    let h = req.headers();
    let token = h.get("x-access-token").map(|v| v.as_bytes());
    let sig = h.get("x-hangar-sig").and_then(|v| v.to_str().ok());
    let pq = req.uri().path_and_query().map(|p| p.as_str()).unwrap_or("/");
    match app.auth.check(token, sig, req.method().as_str(), pq) {
        auth::Check::Ok => next.run(req).await,
        auth::Check::Denied(why) => denied(why),
        auth::Check::Body(p) if req.method() == axum::http::Method::POST && req.uri().path() == "/files" => {
            let mut req = req;
            req.extensions_mut().insert(p); // checked by the upload handler once the body is on disk
            next.run(req).await
        }
        auth::Check::Body(p) => {
            let (parts, body) = req.into_parts();
            let Ok(bytes) = axum::body::to_bytes(body, auth::MAX_SIGNED_BODY).await else {
                return denied("signed request body is too large or was cut off");
            };
            if !app.auth.verify(&p, &bytes) {
                return denied("X-Hangar-Sig does not match the request");
            }
            next.run(Request::from_parts(parts, axum::body::Body::from(bytes))).await
        }
    }
}

/// Hangar extension: who and what this daemon is. Authenticated, so a 200 also proves the token is the right one.
async fn info(State(app): State<Arc<App>>) -> Response {
    let body = serde_json::json!({
        "version": VERSION,
        "pid": std::process::id(),
        "user": app.cfg.user,
        "uid": app.cfg.uid,
        "home": app.cfg.home,
        "workdir": app.cfg.workdir,
        "processes": app.procs.running(),
        "max_processes": app.cfg.max_processes,
    });
    ([("content-type", "application/json")], body.to_string()).into_response()
}

/// E2B's `/envs`: the environment processes start with.
async fn envs() -> Response {
    let m: serde_json::Map<String, serde_json::Value> =
        std::env::vars().map(|(k, v)| (k, serde_json::Value::String(v))).collect();
    ([("content-type", "application/json")], serde_json::Value::Object(m).to_string()).into_response()
}

pub fn router(cfg: Arc<Config>) -> (Router, Arc<process::Procs>) {
    let procs = Arc::new(process::Procs::new(cfg.clone()));
    let names = Arc::new(Names::default());
    let fs = Arc::new(fs::Fs::new(cfg.clone(), names));
    let auth = auth::Auth::new(cfg.token.clone());
    let app = Arc::new(App { cfg, procs: procs.clone(), auth });
    let connect = connectrpc::Router::new().add_service(procs.clone()).add_service(fs);
    let protected = Router::new()
        .route("/files", get(files::download).post(files::upload))
        .route("/info", get(info))
        .route("/envs", get(envs))
        .fallback_service(connectrpc::ConnectRpcService::new(connect))
        .layer(middleware::from_fn_with_state(app.clone(), require_auth))
        .with_state(app);
    let r = Router::new().route("/health", get(|| async { StatusCode::NO_CONTENT })).merge(protected);
    (r, procs)
}

/// Detach from the starting shell: a new session, stdio away from the caller's pipes (or `docker exec` and the
/// k8s exec stream would wait for the daemon to exit). Called before any thread exists.
fn daemonize(log: Option<&str>) -> Result<(), String> {
    use nix::unistd::{ForkResult, fork, setsid};
    // SAFETY: single-threaded here (no runtime yet), so fork is sound.
    match unsafe { fork() }.map_err(|e| format!("fork: {e}"))? {
        ForkResult::Parent { child } => {
            println!("hangar-envd {VERSION} started (pid {child})");
            std::process::exit(0);
        }
        ForkResult::Child => {}
    }
    setsid().map_err(|e| format!("setsid: {e}"))?;
    let null = std::fs::OpenOptions::new().read(true).write(true).open("/dev/null").map_err(|e| e.to_string())?;
    let out = match log {
        Some(p) => std::fs::OpenOptions::new().create(true).append(true).open(p).map_err(|e| format!("{p}: {e}"))?,
        None => null.try_clone().map_err(|e| e.to_string())?,
    };
    for (src, dst) in [(null.as_raw_fd(), 0), (out.as_raw_fd(), 1), (out.as_raw_fd(), 2)] {
        // SAFETY: dup2 onto the standard descriptors; both are valid open fds.
        if unsafe { libc::dup2(src, dst) } < 0 {
            return Err(format!("dup2: {}", std::io::Error::last_os_error()));
        }
    }
    Ok(())
}

fn main() {
    // Take the token out of the environment before anything else: processes we start inherit this environment.
    let token = std::env::var(TOKEN_ENV).ok();
    // SAFETY: no other threads exist yet.
    unsafe { std::env::remove_var(TOKEN_ENV) };
    let mut args: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    if args.iter().any(|a| a == "--version") {
        println!("hangar-envd {VERSION}");
        return;
    }
    let mut log: Option<String> = None;
    if let Some(i) = args.iter().position(|a| a == "--log") {
        args.remove(i);
        if i < args.len() {
            log = args.remove(i).into_string().ok();
        }
    }
    let cfg = match Config::parse(args, token) {
        Ok(c) => Arc::new(c),
        Err(e) => {
            eprintln!("hangar-envd: {e}");
            std::process::exit(2);
        }
    };
    // Bind before detaching, so the starter learns of a taken port from our exit status, not a later probe.
    let addr: SocketAddr = match format!("{}:{}", cfg.listen, cfg.port).parse() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("hangar-envd: bad listen address: {e}");
            std::process::exit(2);
        }
    };
    let listener = match std::net::TcpListener::bind(addr) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("hangar-envd: cannot listen on {addr}: {e}");
            std::process::exit(1);
        }
    };
    if cfg.daemon
        && let Err(e) = daemonize(log.as_deref())
    {
        eprintln!("hangar-envd: {e}");
        std::process::exit(1);
    }
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .max_blocking_threads(64)
        .enable_all()
        .build()
        .expect("tokio runtime");
    rt.block_on(serve(cfg, listener));
}

async fn serve(cfg: Arc<Config>, listener: std::net::TcpListener) {
    listener.set_nonblocking(true).expect("nonblocking listener");
    let listener = tokio::net::TcpListener::from_std(listener).expect("listener");
    let (app, procs) = router(cfg);
    let shutdown = async {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM");
        tokio::select! {
            _ = term.recv() => {}
            _ = tokio::signal::ctrl_c() => {}
        }
    };
    let listener = axum::serve::ListenerExt::tap_io(listener, |tcp| {
        let _ = tcp.set_nodelay(true);
    });
    let served = axum::serve(listener, app).with_graceful_shutdown(shutdown);
    let res = tokio::select! {
        r = served => r,
        // Graceful shutdown waits for open streams; a stream of a process that never ends would hold it forever.
        _ = async {
            let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM");
            term.recv().await;
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        } => Ok(()),
    };
    procs.kill_all();
    if let Err(e) = res {
        eprintln!("hangar-envd: server error: {e}");
        std::process::exit(1);
    }
}
