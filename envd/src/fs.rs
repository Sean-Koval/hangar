//! `filesystem.Filesystem`: stat, list, mkdir, move, remove, and directory watches (inotify).

use std::collections::{HashMap, VecDeque};
use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use buffa::MessageField;
use connectrpc::{ConnectError, RequestContext, Response, ServiceRequest, ServiceResult, ServiceStream};
use futures::StreamExt;
use inotify::{EventMask, Inotify, WatchDescriptor, WatchMask, Watches};
use tokio::sync::mpsc;

use crate::config::Config;
use crate::entry::{Names, entry_info, io_err};
use crate::paths::{check_user_header, resolve};
use crate::proto::filesystem::*;
use crate::stream::{keepalive_interval, with_keepalive};

/// A recursive listing stops here rather than building an unbounded response.
const MAX_LIST_ENTRIES: usize = 100_000;
/// Inotify watches per watcher (each subdirectory of a recursive watch takes one).
const MAX_WATCHES: usize = 16_384;
/// Events a polled watcher (CreateWatcher) keeps between polls; older ones are dropped first.
const MAX_QUEUED_EVENTS: usize = 10_000;

pub struct Fs {
    cfg: Arc<Config>,
    names: Arc<Names>,
    watchers: std::sync::Mutex<HashMap<String, Polled>>,
    streaming: Arc<std::sync::atomic::AtomicUsize>,
    next_id: AtomicU64,
}

struct Polled {
    queue: Arc<std::sync::Mutex<VecDeque<FilesystemEvent>>>,
    task: tokio::task::JoinHandle<()>,
}

impl Fs {
    pub fn new(cfg: Arc<Config>, names: Arc<Names>) -> Self {
        Fs {
            cfg,
            names,
            watchers: Default::default(),
            streaming: Default::default(),
            next_id: AtomicU64::new(1),
        }
    }

    fn path(&self, ctx: &RequestContext, raw: &str) -> Result<PathBuf, ConnectError> {
        check_user_header(&self.cfg, ctx.headers())?;
        resolve(&self.cfg, raw)
    }

    async fn blocking<T: Send + 'static>(
        f: impl FnOnce() -> Result<T, ConnectError> + Send + 'static,
    ) -> Result<T, ConnectError> {
        tokio::task::spawn_blocking(f)
            .await
            .map_err(|e| ConnectError::internal(format!("worker failed: {e}")))?
    }

    fn watcher_slots_left(&self) -> bool {
        let polled = crate::lock(&self.watchers).len();
        polled + self.streaming.load(Ordering::Relaxed) < self.cfg.max_watchers
    }

    /// A started inotify watch on `dir` and a task forwarding its events to `tx` until `tx` closes.
    fn start_watch(
        &self,
        dir: PathBuf,
        recursive: bool,
        include_entry: bool,
        tx: mpsc::Sender<FilesystemEvent>,
    ) -> Result<tokio::task::JoinHandle<()>, ConnectError> {
        let md = std::fs::metadata(&dir).map_err(|e| io_err("watch", &dir, e))?;
        if !md.is_dir() {
            return Err(ConnectError::invalid_argument(format!("{} is not a directory", dir.display())));
        }
        let ino = Inotify::init().map_err(|e| ConnectError::resource_exhausted(format!("inotify: {e}")))?;
        let mut tree = Tree { root: dir, recursive, wds: HashMap::new(), watches: ino.watches() };
        tree.add_dir(PathBuf::new())?;
        let mut events = ino
            .into_event_stream(vec![0u8; 64 * 1024])
            .map_err(|e| ConnectError::internal(format!("inotify: {e}")))?;
        let names = self.names.clone();
        Ok(tokio::spawn(async move {
            while let Some(ev) = events.next().await {
                let Ok(ev) = ev else { break };
                if ev.mask.contains(EventMask::Q_OVERFLOW) {
                    continue; // the kernel dropped events; there is nothing to forward for them
                }
                let ended = ev.mask.contains(EventMask::IGNORED) && tree.wds.get(&ev.wd).is_some_and(|r| r.as_os_str().is_empty());
                if let Some(out) = tree.translate(&ev.wd, ev.mask, ev.name.as_deref(), include_entry, &names)
                    && tx.send(out).await.is_err()
                {
                    break;
                }
                if ended {
                    break; // the watched directory itself is gone
                }
            }
        }))
    }
}

/// The inotify watches behind one directory watch: one per directory, keyed back to its path relative to the root.
struct Tree {
    root: PathBuf,
    recursive: bool,
    wds: HashMap<WatchDescriptor, PathBuf>,
    watches: Watches,
}

impl Tree {
    fn mask() -> WatchMask {
        WatchMask::CREATE
            | WatchMask::MODIFY
            | WatchMask::DELETE
            | WatchMask::DELETE_SELF
            | WatchMask::MOVED_FROM
            | WatchMask::MOVED_TO
            | WatchMask::MOVE_SELF
            | WatchMask::ATTRIB
            | WatchMask::DONT_FOLLOW
            | WatchMask::ONLYDIR
    }

    fn add_dir(&mut self, rel: PathBuf) -> Result<(), ConnectError> {
        if self.wds.len() >= MAX_WATCHES {
            return Err(ConnectError::resource_exhausted(format!(
                "more than {MAX_WATCHES} directories to watch; watch a smaller tree"
            )));
        }
        let abs = self.root.join(&rel);
        let wd = self.watches.add(&abs, Self::mask()).map_err(|e| io_err("watch", &abs, e))?;
        self.wds.insert(wd, rel.clone());
        if self.recursive
            && let Ok(rd) = std::fs::read_dir(&abs)
        {
            for ent in rd.flatten() {
                if ent.file_type().is_ok_and(|t| t.is_dir()) {
                    // A directory removed meanwhile is fine to skip; running out of watches is not.
                    match self.add_dir(rel.join(ent.file_name())) {
                        Err(e) if e.code == connectrpc::error::ErrorCode::ResourceExhausted => return Err(e),
                        _ => {}
                    }
                }
            }
        }
        Ok(())
    }

    fn translate(
        &mut self,
        wd: &WatchDescriptor,
        mask: EventMask,
        name: Option<&OsStr>,
        include_entry: bool,
        names: &Names,
    ) -> Option<FilesystemEvent> {
        let dir = self.wds.get(wd)?.clone();
        if mask.contains(EventMask::IGNORED) {
            self.wds.remove(wd);
            return None;
        }
        let rel = match name {
            Some(n) => dir.join(n),
            None => dir.clone(),
        };
        let ty = if mask.intersects(EventMask::CREATE | EventMask::MOVED_TO) {
            EventType::EVENT_TYPE_CREATE
        } else if mask.intersects(EventMask::DELETE | EventMask::DELETE_SELF) {
            EventType::EVENT_TYPE_REMOVE
        } else if mask.intersects(EventMask::MOVED_FROM | EventMask::MOVE_SELF) {
            EventType::EVENT_TYPE_RENAME
        } else if mask.contains(EventMask::MODIFY) {
            EventType::EVENT_TYPE_WRITE
        } else if mask.contains(EventMask::ATTRIB) {
            EventType::EVENT_TYPE_CHMOD
        } else {
            return None;
        };
        if name.is_none() && !dir.as_os_str().is_empty() {
            return None; // a subdirectory's own DELETE_SELF/MOVE_SELF: its parent already reported it
        }
        let abs = self.root.join(&rel);
        if self.recursive && mask.contains(EventMask::ISDIR) && ty == EventType::EVENT_TYPE_CREATE {
            let _ = self.add_dir(rel.clone()); // best effort: events in it are missed only if the limit is hit
        }
        let entry = if include_entry && matches!(ty, EventType::EVENT_TYPE_CREATE | EventType::EVENT_TYPE_WRITE | EventType::EVENT_TYPE_CHMOD) {
            entry_info(names, &abs).ok()
        } else {
            None
        };
        Some(FilesystemEvent {
            name: rel.to_string_lossy().into_owned(),
            r#type: ty.into(),
            entry: entry.map(MessageField::some).unwrap_or_default(),
            ..Default::default()
        })
    }
}

/// Entries under `root` down to `depth` levels, in `filepath.WalkDir` order (lexical, parents before children);
/// symlinked directories are listed but not entered.
fn list(names: &Names, root: &Path, depth: u32) -> Result<Vec<EntryInfo>, ConnectError> {
    let md = std::fs::metadata(root).map_err(|e| io_err("list", root, e))?;
    if !md.is_dir() {
        return Err(ConnectError::invalid_argument(format!("{} is not a directory", root.display())));
    }
    let mut out = Vec::new();
    walk(names, root, 1, depth, &mut out, true)?;
    Ok(out)
}

fn walk(names: &Names, dir: &Path, level: u32, depth: u32, out: &mut Vec<EntryInfo>, top: bool) -> Result<(), ConnectError> {
    let rd = match std::fs::read_dir(dir) {
        Ok(rd) => rd,
        Err(e) if top => return Err(io_err("list", dir, e)),
        Err(_) => return Ok(()), // a subdirectory we can't read, or one removed meanwhile
    };
    let mut children: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
    children.sort();
    for p in children {
        let Ok(info) = entry_info(names, &p) else { continue };
        let descend = level < depth && !info.is_symlink && info.r#type.as_known() == Some(FileType::FILE_TYPE_DIRECTORY);
        out.push(info);
        if out.len() > MAX_LIST_ENTRIES {
            return Err(ConnectError::resource_exhausted(format!(
                "more than {MAX_LIST_ENTRIES} entries; list a smaller directory or a lower depth"
            )));
        }
        if descend {
            walk(names, &p, level + 1, depth, out, false)?;
        }
    }
    Ok(())
}

#[allow(refining_impl_trait)]
impl Filesystem for Fs {
    async fn stat(&self, ctx: RequestContext, request: ServiceRequest<'_, StatRequest>) -> ServiceResult<StatResponse> {
        let p = self.path(&ctx, request.path)?;
        let names = self.names.clone();
        let entry = Self::blocking(move || entry_info(&names, &p).map_err(|e| io_err("stat", &p, e))).await?;
        Response::ok(StatResponse { entry: MessageField::some(entry), ..Default::default() })
    }

    async fn make_dir(&self, ctx: RequestContext, request: ServiceRequest<'_, MakeDirRequest>) -> ServiceResult<MakeDirResponse> {
        let p = self.path(&ctx, request.path)?;
        let names = self.names.clone();
        let entry = Self::blocking(move || {
            match std::fs::metadata(&p) {
                Ok(m) if m.is_dir() => return Err(ConnectError::already_exists(format!("{} already exists", p.display()))),
                Ok(_) => return Err(ConnectError::invalid_argument(format!("{} exists and is not a directory", p.display()))),
                Err(_) => {}
            }
            std::fs::create_dir_all(&p).map_err(|e| io_err("mkdir", &p, e))?;
            entry_info(&names, &p).map_err(|e| io_err("stat", &p, e))
        })
        .await?;
        Response::ok(MakeDirResponse { entry: MessageField::some(entry), ..Default::default() })
    }

    async fn r#move(&self, ctx: RequestContext, request: ServiceRequest<'_, MoveRequest>) -> ServiceResult<MoveResponse> {
        let src = self.path(&ctx, request.source)?;
        let dst = self.path(&ctx, request.destination)?;
        let names = self.names.clone();
        let entry = Self::blocking(move || {
            if let Some(parent) = dst.parent() {
                std::fs::create_dir_all(parent).map_err(|e| io_err("mkdir", parent, e))?;
            }
            std::fs::rename(&src, &dst).map_err(|e| io_err("move", &src, e))?;
            entry_info(&names, &dst).map_err(|e| io_err("stat", &dst, e))
        })
        .await?;
        Response::ok(MoveResponse { entry: MessageField::some(entry), ..Default::default() })
    }

    async fn list_dir(&self, ctx: RequestContext, request: ServiceRequest<'_, ListDirRequest>) -> ServiceResult<ListDirResponse> {
        let p = self.path(&ctx, request.path)?;
        let depth = request.depth.max(1);
        let names = self.names.clone();
        let entries = Self::blocking(move || list(&names, &p, depth)).await?;
        Response::ok(ListDirResponse { entries, ..Default::default() })
    }

    async fn remove(&self, ctx: RequestContext, request: ServiceRequest<'_, RemoveRequest>) -> ServiceResult<RemoveResponse> {
        let p = self.path(&ctx, request.path)?;
        Self::blocking(move || {
            // `rm -rf` (Go's os.RemoveAll, as upstream envd): a path that isn't there is already removed.
            let md = match std::fs::symlink_metadata(&p) {
                Ok(md) => md,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
                Err(e) => return Err(io_err("remove", &p, e)),
            };
            if md.is_dir() {
                std::fs::remove_dir_all(&p).map_err(|e| io_err("remove", &p, e))
            } else {
                std::fs::remove_file(&p).map_err(|e| io_err("remove", &p, e))
            }
        })
        .await?;
        Response::ok(RemoveResponse::default())
    }

    async fn watch_dir(
        &self,
        ctx: RequestContext,
        request: ServiceRequest<'_, WatchDirRequest>,
    ) -> ServiceResult<ServiceStream<WatchDirResponse>> {
        let p = self.path(&ctx, request.path)?;
        if !self.watcher_slots_left() {
            return Err(ConnectError::resource_exhausted(format!("at most {} directory watches at once", self.cfg.max_watchers)));
        }
        let (tx, rx) = mpsc::channel::<FilesystemEvent>(256);
        let task = self.start_watch(p, request.recursive, request.include_entry, tx)?;
        let guard = WatchGuard { task, count: self.streaming.clone() };
        guard.count.fetch_add(1, Ordering::Relaxed);
        let start = WatchDirResponse {
            event: Some(watch_dir_response::Event::Start(Box::default())),
            ..Default::default()
        };
        let events = tokio_stream_from(rx).map(move |ev| {
            let _keep = &guard; // the watch lives exactly as long as the response stream
            WatchDirResponse { event: Some(watch_dir_response::Event::Filesystem(Box::new(ev))), ..Default::default() }
        });
        let keep = || WatchDirResponse { event: Some(watch_dir_response::Event::Keepalive(Box::default())), ..Default::default() };
        let s = futures::stream::once(async move { start }).chain(events);
        Ok(Response::stream(with_keepalive(s, keepalive_interval(&ctx), ctx.deadline(), keep)))
    }

    async fn create_watcher(&self, ctx: RequestContext, request: ServiceRequest<'_, CreateWatcherRequest>) -> ServiceResult<CreateWatcherResponse> {
        let p = self.path(&ctx, request.path)?;
        if !self.watcher_slots_left() {
            return Err(ConnectError::resource_exhausted(format!("at most {} directory watches at once", self.cfg.max_watchers)));
        }
        let (tx, mut rx) = mpsc::channel::<FilesystemEvent>(256);
        let inner = self.start_watch(p, request.recursive, request.include_entry, tx)?;
        let queue: Arc<std::sync::Mutex<VecDeque<FilesystemEvent>>> = Default::default();
        let q = queue.clone();
        let task = tokio::spawn(async move {
            while let Some(ev) = rx.recv().await {
                let mut q = crate::lock(&q);
                if q.len() >= MAX_QUEUED_EVENTS {
                    q.pop_front();
                }
                q.push_back(ev);
            }
            inner.abort();
        });
        let id = format!("w{}", self.next_id.fetch_add(1, Ordering::Relaxed));
        crate::lock(&self.watchers).insert(id.clone(), Polled { queue, task });
        Response::ok(CreateWatcherResponse { watcher_id: id, ..Default::default() })
    }

    async fn get_watcher_events(&self, _ctx: RequestContext, request: ServiceRequest<'_, GetWatcherEventsRequest>) -> ServiceResult<GetWatcherEventsResponse> {
        let w = crate::lock(&self.watchers);
        let p = w.get(request.watcher_id).ok_or_else(|| ConnectError::not_found(format!("no watcher {}", request.watcher_id)))?;
        let events: Vec<FilesystemEvent> = crate::lock(&p.queue).drain(..).collect();
        Response::ok(GetWatcherEventsResponse { events, ..Default::default() })
    }

    async fn remove_watcher(&self, _ctx: RequestContext, request: ServiceRequest<'_, RemoveWatcherRequest>) -> ServiceResult<RemoveWatcherResponse> {
        let p = crate::lock(&self.watchers).remove(request.watcher_id);
        match p {
            Some(p) => {
                p.task.abort();
                Response::ok(RemoveWatcherResponse::default())
            }
            None => Err(ConnectError::not_found(format!("no watcher {}", request.watcher_id))),
        }
    }
}

struct WatchGuard {
    task: tokio::task::JoinHandle<()>,
    count: Arc<std::sync::atomic::AtomicUsize>,
}

impl Drop for WatchGuard {
    fn drop(&mut self) {
        self.task.abort();
        self.count.fetch_sub(1, Ordering::Relaxed);
    }
}

fn tokio_stream_from<T: Send + 'static>(mut rx: mpsc::Receiver<T>) -> impl futures::Stream<Item = T> + Send {
    futures::stream::poll_fn(move |cx| rx.poll_recv(cx))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lists_to_depth_without_following_links() {
        let d = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(d.path().join("a/b/c")).unwrap();
        std::fs::write(d.path().join("a/f"), b"x").unwrap();
        std::os::unix::fs::symlink(d.path().join("a"), d.path().join("link")).unwrap();
        let n = Names::default();
        let one: Vec<String> = list(&n, d.path(), 1).unwrap().into_iter().map(|e| e.name).collect();
        assert_eq!(one, vec!["a", "link"]);
        let three: Vec<String> = list(&n, d.path(), 3).unwrap().into_iter().map(|e| e.path).collect();
        let rel: Vec<String> = three.iter().map(|p| p.trim_start_matches(d.path().to_str().unwrap()).to_string()).collect();
        assert_eq!(rel, vec!["/a", "/a/b", "/a/b/c", "/a/f", "/link"]);
        assert!(list(&n, &d.path().join("a/f"), 1).is_err());
    }
}
