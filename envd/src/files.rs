//! E2B envd's `/files` endpoints: streamed download and upload (raw `application/octet-stream` or multipart), never
//! holding a whole file in memory. An upload lands in a temporary file next to the target and is renamed over it,
//! so a reader never sees half a file and a failed upload leaves the old one in place.
//!
//! Hangar extension: `mode=<octal>` on an upload sets the file's permissions (E2B has no such parameter).

use std::io;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use axum::body::Body;
use axum::extract::{FromRequest, Multipart, Query, Request, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use futures::{Stream, StreamExt};
use serde::Deserialize;
use tokio::io::AsyncWriteExt;

use crate::App;
use crate::paths::{check_user, check_user_header, resolve};

#[derive(Deserialize)]
pub struct FileQuery {
    path: Option<String>,
    username: Option<String>,
    mode: Option<String>,
    signature: Option<String>,
}

pub fn error(code: StatusCode, message: impl Into<String>) -> Response {
    let body = serde_json::json!({"message": message.into(), "code": code.as_u16()});
    (code, [(header::CONTENT_TYPE, "application/json")], body.to_string()).into_response()
}

fn io_status(e: &io::Error) -> StatusCode {
    match e.kind() {
        io::ErrorKind::NotFound => StatusCode::NOT_FOUND,
        io::ErrorKind::PermissionDenied => StatusCode::FORBIDDEN,
        io::ErrorKind::StorageFull => StatusCode::INSUFFICIENT_STORAGE,
        io::ErrorKind::IsADirectory | io::ErrorKind::NotADirectory | io::ErrorKind::InvalidInput => StatusCode::BAD_REQUEST,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    }
}

fn target(app: &App, q: &FileQuery, headers: &HeaderMap, raw: Option<&str>) -> Result<PathBuf, Response> {
    if q.signature.is_some() {
        return Err(error(StatusCode::UNAUTHORIZED, "signed file URLs are not supported; send X-Access-Token"));
    }
    check_user(&app.cfg, q.username.as_deref()).map_err(|e| error(StatusCode::UNAUTHORIZED, e.message.unwrap_or_default()))?;
    check_user_header(&app.cfg, headers).map_err(|e| error(StatusCode::UNAUTHORIZED, e.message.unwrap_or_default()))?;
    let raw = raw.ok_or_else(|| error(StatusCode::BAD_REQUEST, "path is required"))?;
    resolve(&app.cfg, raw).map_err(|e| error(StatusCode::BAD_REQUEST, e.message.unwrap_or_default()))
}

pub async fn download(State(app): State<Arc<App>>, Query(q): Query<FileQuery>, headers: HeaderMap) -> Response {
    let p = match target(&app, &q, &headers, q.path.as_deref()) {
        Ok(p) => p,
        Err(r) => return r,
    };
    // O_NONBLOCK: opening a FIFO someone put at this path must not park a worker thread waiting for a writer.
    let opened = tokio::fs::OpenOptions::new().read(true).custom_flags(libc::O_NONBLOCK).open(&p).await;
    let file = match opened {
        Ok(f) => f,
        Err(e) => return error(io_status(&e), format!("cannot open {}: {e}", p.display())),
    };
    match file.metadata().await {
        Ok(md) if md.is_dir() => return error(StatusCode::BAD_REQUEST, format!("path '{}' is a directory", p.display())),
        Ok(md) if !md.is_file() => return error(StatusCode::BAD_REQUEST, format!("path '{}' is not a regular file", p.display())),
        Ok(_) => {}
        Err(e) => return error(io_status(&e), format!("cannot stat {}: {e}", p.display())),
    }
    let stream = tokio_util::io::ReaderStream::with_capacity(file, 64 * 1024);
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/octet-stream")
        .body(Body::from_stream(stream))
        .unwrap_or_else(|_| error(StatusCode::INTERNAL_SERVER_ERROR, "response"))
}

static TMP_SEQ: AtomicU64 = AtomicU64::new(0);

/// Streams `body` into `dst` through a temporary sibling, then renames it into place.
async fn write_stream<E: std::fmt::Display>(
    dst: &Path,
    mode: Option<u32>,
    mut body: impl Stream<Item = Result<Bytes, E>> + Unpin,
) -> Result<(), Response> {
    let parent = dst.parent().unwrap_or(Path::new("/"));
    let name = dst.file_name().ok_or_else(|| error(StatusCode::BAD_REQUEST, "path names a directory"))?;
    tokio::fs::create_dir_all(parent)
        .await
        .map_err(|e| error(io_status(&e), format!("cannot create {}: {e}", parent.display())))?;
    let existing_mode = tokio::fs::metadata(dst).await.ok().filter(|m| m.is_file()).map(|m| m.mode() & 0o7777);
    let tmp = parent.join(format!(
        ".{}.envd-{}-{}",
        name.to_string_lossy(),
        std::process::id(),
        TMP_SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    let res: Result<(), Response> = async {
        let mut f = tokio::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)
            .await
            .map_err(|e| error(io_status(&e), format!("cannot create {}: {e}", tmp.display())))?;
        while let Some(chunk) = body.next().await {
            let chunk = chunk.map_err(|e| error(StatusCode::BAD_REQUEST, format!("upload interrupted: {e}")))?;
            f.write_all(&chunk).await.map_err(|e| error(io_status(&e), format!("cannot write {}: {e}", dst.display())))?;
        }
        f.flush().await.map_err(|e| error(io_status(&e), format!("cannot write {}: {e}", dst.display())))?;
        let perm = std::fs::Permissions::from_mode(mode.or(existing_mode).unwrap_or(0o644));
        tokio::fs::set_permissions(&tmp, perm)
            .await
            .map_err(|e| error(io_status(&e), format!("cannot chmod {}: {e}", dst.display())))?;
        tokio::fs::rename(&tmp, dst)
            .await
            .map_err(|e| error(io_status(&e), format!("cannot write {}: {e}", dst.display())))
    }
    .await;
    if res.is_err() {
        let _ = tokio::fs::remove_file(&tmp).await;
    }
    res
}

fn written(paths: &[PathBuf]) -> Response {
    let items: Vec<_> = paths
        .iter()
        .map(|p| {
            serde_json::json!({
                "path": p.to_string_lossy(),
                "name": p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
                "type": "file",
            })
        })
        .collect();
    (StatusCode::OK, [(header::CONTENT_TYPE, "application/json")], serde_json::Value::Array(items).to_string()).into_response()
}

pub async fn upload(State(app): State<Arc<App>>, Query(q): Query<FileQuery>, req: Request) -> Response {
    let headers = req.headers().clone();
    if headers.get(header::CONTENT_ENCODING).is_some_and(|v| v != "identity") {
        return error(StatusCode::UNSUPPORTED_MEDIA_TYPE, "compressed uploads are not supported; send the file as is");
    }
    let mode = match q.mode.as_deref().map(|m| u32::from_str_radix(m, 8)) {
        None => None,
        Some(Ok(m)) if m <= 0o7777 => Some(m),
        Some(_) => return error(StatusCode::BAD_REQUEST, "mode must be octal permissions, e.g. 644"),
    };
    let ctype = headers.get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("").to_ascii_lowercase();
    if ctype.starts_with("multipart/form-data") {
        let mut mp = match Multipart::from_request(req, &()).await {
            Ok(m) => m,
            Err(e) => return error(StatusCode::BAD_REQUEST, format!("bad multipart body: {e}")),
        };
        let mut done = Vec::new();
        loop {
            let field = match mp.next_field().await {
                Ok(Some(f)) => f,
                Ok(None) => break,
                Err(e) => return error(StatusCode::BAD_REQUEST, format!("bad multipart body: {e}")),
            };
            // E2B: each part's filename is its path; a lone file may name it in the query instead.
            let raw = field.file_name().map(str::to_string).filter(|s| !s.is_empty()).or_else(|| q.path.clone());
            let dst = match target(&app, &q, &headers, raw.as_deref()) {
                Ok(p) => p,
                Err(r) => return r,
            };
            let chunks = Box::pin(futures::stream::unfold(field, |mut f| async move {
                match f.chunk().await {
                    Ok(Some(c)) => Some((Ok(c), f)),
                    Ok(None) => None,
                    Err(e) => Some((Err(e), f)),
                }
            }));
            if let Err(r) = write_stream(&dst, mode, chunks).await {
                return r;
            }
            done.push(dst);
        }
        if done.is_empty() {
            return error(StatusCode::BAD_REQUEST, "the multipart body has no file");
        }
        return written(&done);
    }
    let dst = match target(&app, &q, &headers, q.path.as_deref()) {
        Ok(p) => p,
        Err(r) => return r,
    };
    let body = req.into_body().into_data_stream();
    match write_stream(&dst, mode, body).await {
        Ok(()) => written(&[dst]),
        Err(r) => r,
    }
}

