//! In-process tests of the whole HTTP surface: the router as a tower service, real processes and files.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tower::ServiceExt;

use crate::auth::{hex, sha256};
use crate::config::Config;

const TOKEN: &str = "test-token-0123456789abcdef";

fn cfg(dir: &std::path::Path, max_processes: usize) -> Arc<Config> {
    Arc::new(Config {
        listen: "127.0.0.1".into(),
        port: 0,
        token: TOKEN.into(),
        user: nix::unistd::User::from_uid(nix::unistd::getuid()).unwrap().unwrap().name,
        uid: nix::unistd::getuid().as_raw(),
        home: dir.to_path_buf(),
        workdir: dir.to_path_buf(),
        max_processes,
        max_watchers: 4,
        daemon: false,
    })
}

fn sign(method: &str, pq: &str, body: &[u8]) -> String {
    sign_as(method, pq, &hex(&sha256(body)))
}

fn sign_as(method: &str, pq: &str, body: &str) -> String {
    let ts = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64;
    crate::auth::tests::sign_body(TOKEN, ts, &format!("{:032x}", rand_u128()), method, pq, body)
}

fn rand_u128() -> u128 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(1);
    let t = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
    t ^ ((N.fetch_add(1, Ordering::Relaxed) as u128) << 64)
}

fn frame(v: &Value) -> Vec<u8> {
    let p = serde_json::to_vec(v).unwrap();
    let mut out = vec![0u8];
    out.extend_from_slice(&(p.len() as u32).to_be_bytes());
    out.extend_from_slice(&p);
    out
}

/// (messages, end-of-stream message) of a Connect server-stream response body.
fn unframe(mut b: &[u8]) -> (Vec<Value>, Value) {
    let mut msgs = Vec::new();
    while b.len() >= 5 {
        let n = u32::from_be_bytes([b[1], b[2], b[3], b[4]]) as usize;
        let v: Value = serde_json::from_slice(&b[5..5 + n]).unwrap_or(Value::Null);
        if b[0] & 2 != 0 {
            return (msgs, v);
        }
        msgs.push(v);
        b = &b[5 + n..];
    }
    panic!("no end-of-stream message")
}

struct Client {
    app: axum::Router,
}

impl Client {
    fn new(cfg: Arc<Config>) -> Self {
        Client { app: crate::router(cfg).0 }
    }

    async fn send(&self, req: Request<Body>) -> (StatusCode, Vec<u8>) {
        let resp = self.app.clone().oneshot(req).await.unwrap();
        let status = resp.status();
        let body = tokio::time::timeout(Duration::from_secs(20), resp.into_body().collect()).await.unwrap().unwrap();
        (status, body.to_bytes().to_vec())
    }

    fn req(method: &str, pq: &str, ctype: &str, body: Vec<u8>) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(pq)
            .header("content-type", ctype)
            .header("x-hangar-sig", sign(method, pq, &body))
            .body(Body::from(body))
            .unwrap()
    }

    async fn unary(&self, method: &str, body: Value) -> (StatusCode, Value) {
        let (s, b) = self
            .send(Self::req("POST", &format!("/{method}"), "application/json", serde_json::to_vec(&body).unwrap()))
            .await;
        (s, serde_json::from_slice(&b).unwrap_or(Value::Null))
    }

    async fn stream(&self, method: &str, body: Value) -> (Vec<Value>, Value) {
        let (s, b) = self.send(Self::req("POST", &format!("/{method}"), "application/connect+json", frame(&body))).await;
        assert_eq!(s, StatusCode::OK, "{}", String::from_utf8_lossy(&b));
        unframe(&b)
    }
}

fn b64(s: &str) -> Vec<u8> {
    crate::paths::base64_decode(s).unwrap()
}

fn b64enc(data: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in data.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            out.push(if i <= c.len() { A[(n >> (18 - 6 * i) & 63) as usize] as char } else { '=' });
        }
    }
    out
}

fn output(msgs: &[Value], kind: &str) -> String {
    let mut out = Vec::new();
    for m in msgs {
        if let Some(d) = m["event"]["data"][kind].as_str() {
            out.extend(b64(d));
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[tokio::test]
async fn deny_by_default() {
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    let get = |uri: &str| Request::builder().uri(uri).body(Body::empty()).unwrap();
    assert_eq!(c.send(get("/health")).await.0, StatusCode::NO_CONTENT);
    assert_eq!(c.send(get("/info")).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(c.send(get("/files?path=x")).await.0, StatusCode::UNAUTHORIZED);
    let r = Request::builder().method("POST").uri("/process.Process/List").header("content-type", "application/json");
    assert_eq!(c.send(r.body(Body::from("{}")).unwrap()).await.0, StatusCode::UNAUTHORIZED);
    let ok = Request::builder().uri("/info").header("x-access-token", TOKEN).body(Body::empty()).unwrap();
    assert_eq!(c.send(ok).await.0, StatusCode::OK);
    let bad = Request::builder().uri("/info").header("x-access-token", "nope").body(Body::empty()).unwrap();
    assert_eq!(c.send(bad).await.0, StatusCode::UNAUTHORIZED);
    // A signature is good once, for its own request.
    let sig = sign("GET", "/info", b"");
    let mk = |s: &str| Request::builder().uri("/info").header("x-hangar-sig", s).body(Body::empty()).unwrap();
    assert_eq!(c.send(mk(&sig)).await.0, StatusCode::OK);
    assert_eq!(c.send(mk(&sig)).await.0, StatusCode::UNAUTHORIZED);
    let other = sign("GET", "/envs", b"");
    assert_eq!(c.send(mk(&other)).await.0, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn runs_commands_and_streams_output() {
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    let (msgs, end) = c
        .stream(
            "process.Process/Start",
            json!({"process": {"cmd": "sh", "args": ["-c", "echo hi; echo err >&2; pwd; echo $FOO; exit 3"],
                               "envs": {"FOO": "bar"}}, "stdin": false}),
        )
        .await;
    assert_eq!(end, json!({}));
    assert!(msgs[0]["event"]["start"]["pid"].as_u64().unwrap() > 0);
    let canon = std::fs::canonicalize(d.path()).unwrap();
    assert_eq!(output(&msgs, "stdout"), format!("hi\n{}\nbar\n", canon.display()));
    assert_eq!(output(&msgs, "stderr"), "err\n");
    let last = &msgs.last().unwrap()["event"]["end"];
    assert_eq!((last["exitCode"].as_i64(), last["exited"].as_bool()), (Some(3), Some(true)));
}

#[tokio::test]
async fn refuses_before_starting() {
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    let (msgs, end) = c
        .stream("process.Process/Start", json!({"process": {"cmd": "true", "cwd": "/nonexistent/dir"}}))
        .await;
    assert!(msgs.is_empty());
    assert_eq!(end["error"]["code"], "invalid_argument");
    let (_, end) = c.stream("process.Process/Start", json!({"process": {"cmd": "/no/such/binary"}})).await;
    assert_eq!(end["error"]["code"], "not_found");
}

#[tokio::test]
async fn caps_concurrent_processes_and_kills_by_signal() {
    let d = tempfile::tempdir().unwrap();
    let c = Arc::new(Client::new(cfg(d.path(), 1)));
    let c2 = c.clone();
    let long = tokio::spawn(async move {
        c2.stream("process.Process/Start", json!({"process": {"cmd": "sleep", "args": ["30"]}, "tag": "long"})).await
    });
    let mut pid = 0;
    for _ in 0..100 {
        let (_, list) = c.unary("process.Process/List", json!({})).await;
        if let Some(p) = list["processes"][0]["pid"].as_u64() {
            pid = p;
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(pid > 0);
    let (_, end) = c.stream("process.Process/Start", json!({"process": {"cmd": "true"}})).await;
    assert_eq!(end["error"]["code"], "resource_exhausted");
    let (s, _) = c.unary("process.Process/SendSignal", json!({"process": {"tag": "long"}, "signal": "SIGNAL_SIGKILL"})).await;
    assert_eq!(s, StatusCode::OK);
    let (msgs, _) = long.await.unwrap();
    let e = &msgs.last().unwrap()["event"]["end"];
    assert_eq!((e["exitCode"].as_i64(), e["status"].as_str()), (Some(-1), Some("signal: killed")));
    // The slot is free again.
    let (msgs, _) = c.stream("process.Process/Start", json!({"process": {"cmd": "true"}})).await;
    assert!(msgs.last().unwrap()["event"]["end"].is_object());
}

#[tokio::test]
async fn huge_output_streams_without_buffering() {
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    let (msgs, _) = c
        .stream("process.Process/Start", json!({"process": {"cmd": "sh", "args": ["-c", "head -c 3000000 /dev/zero"]}}))
        .await;
    let total: usize = msgs.iter().filter_map(|m| m["event"]["data"]["stdout"].as_str()).map(|s| b64(s).len()).sum();
    assert_eq!(total, 3_000_000);
    assert!(msgs.iter().all(|m| m["event"]["data"]["stdout"].as_str().is_none_or(|s| s.len() <= 44 * 1024)));
}

#[tokio::test]
async fn stdin_and_pty() {
    let d = tempfile::tempdir().unwrap();
    let c = Arc::new(Client::new(cfg(d.path(), 4)));
    let c2 = c.clone();
    let t = tokio::spawn(async move {
        c2.stream("process.Process/Start", json!({"process": {"cmd": "sh", "args": ["-c", "read x; echo got $x"]}, "tag": "in"}))
            .await
    });
    let mut sent = false;
    for _ in 0..100 {
        let (s, _) = c
            .unary("process.Process/SendInput", json!({"process": {"tag": "in"}, "input": {"stdin": "YWJjCg=="}}))
            .await;
        if s == StatusCode::OK {
            sent = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(sent);
    assert_eq!(output(&t.await.unwrap().0, "stdout"), "got abc\n");

    let c3 = c.clone();
    let t = tokio::spawn(async move {
        c3.stream("process.Process/Start", json!({"process": {"cmd": "sh"}, "pty": {"size": {"cols": 80, "rows": 24}}, "tag": "tty"}))
            .await
    });
    let mut done = false;
    for _ in 0..100 {
        let (s, _) = c
            .unary("process.Process/Update", json!({"process": {"tag": "tty"}, "pty": {"size": {"cols": 100, "rows": 30}}}))
            .await;
        if s == StatusCode::OK {
            let input = b64enc(b"stty size; exit 7\n");
            let (s, _) = c.unary("process.Process/SendInput", json!({"process": {"tag": "tty"}, "input": {"pty": input}})).await;
            done = s == StatusCode::OK;
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(done);
    let msgs = t.await.unwrap().0;
    assert!(output(&msgs, "pty").contains("30 100"), "{}", output(&msgs, "pty"));
    assert_eq!(msgs.last().unwrap()["event"]["end"]["exitCode"].as_i64(), Some(7));
}

#[tokio::test]
async fn signed_upload_download_and_tampering() {
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    let target: PathBuf = d.path().join("sub/dir/f.bin");
    let pq = format!("/files?path={}&mode=600", target.display());
    let data: Vec<u8> = (0..300_000u32).map(|i| (i % 251) as u8).collect();
    let (s, b) = c.send(Client::req("POST", &pq, "application/octet-stream", data.clone())).await;
    assert_eq!(s, StatusCode::OK, "{}", String::from_utf8_lossy(&b));
    assert_eq!(std::fs::read(&target).unwrap(), data);
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(std::fs::metadata(&target).unwrap().permissions().mode() & 0o777, 0o600);
    // Signed for one body, sent with another: refused, and the file is untouched.
    let mut req = Client::req("POST", &pq, "application/octet-stream", b"original".to_vec());
    *req.body_mut() = Body::from("tampered");
    assert_eq!(c.send(req).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(std::fs::read(&target).unwrap(), data);
    assert_eq!(std::fs::read_dir(target.parent().unwrap()).unwrap().count(), 1, "no temporary file left behind");
    let get = format!("/files?path={}", target.display());
    let (s, b) = c.send(Client::req("GET", &get, "", vec![])).await;
    assert_eq!((s, b.len()), (StatusCode::OK, data.len()));
    assert_eq!(b, data);
    let (s, _) = c.send(Client::req("GET", "/files?path=/nonexistent", "", vec![])).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let dirq = format!("/files?path={}", d.path().display());
    assert_eq!(c.send(Client::req("GET", &dirq, "", vec![])).await.0, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn chunked_uploads_and_proofs() {
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    let target: PathBuf = d.path().join("new/dir/f.bin");
    let pq = format!("/files?path={}", target.display());
    let data: Vec<u8> = (0..3_000_000u32).map(|i| (i % 253) as u8).collect();
    let upload = |body: Vec<u8>, sig: &str| {
        Request::builder()
            .method("POST")
            .uri(&pq)
            .header("content-type", "application/octet-stream")
            .header("x-hangar-sig", sig)
            .body(Body::from(body))
            .unwrap()
    };
    let sig = sign_as("POST", &pq, "chunked");
    let body = crate::auth::tests::chunked(TOKEN, &sig, &data, 1 << 20);
    let resp = c.app.clone().oneshot(upload(body.clone(), &sig)).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let mac = crate::auth::tests::mac_of(&sig);
    let want = crate::auth::Auth::new(TOKEN.into()).proof(&mac);
    assert_eq!(resp.headers()["x-hangar-proof"], want.as_str(), "the daemon proves it holds the token");
    assert_eq!(std::fs::read(&target).unwrap(), data);
    // Cut short, or another request's frames: refused, and the file is left as it was.
    let sig2 = sign_as("POST", &pq, "chunked");
    let short = crate::auth::tests::chunked(TOKEN, &sig2, &data[..1000], 1 << 20);
    assert_eq!(c.send(upload(short[..short.len() - 36].to_vec(), &sig2)).await.0, StatusCode::UNAUTHORIZED);
    let sig3 = sign_as("POST", &pq, "chunked");
    assert_eq!(c.send(upload(body, &sig3)).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(std::fs::read(&target).unwrap(), data);
    assert_eq!(std::fs::read_dir(target.parent().unwrap()).unwrap().count(), 1, "no temporary file left behind");
    // Chunked signatures are for uploads only.
    let list = sign_as("POST", "/process.Process/List", "chunked");
    let r = Request::builder()
        .method("POST")
        .uri("/process.Process/List")
        .header("content-type", "application/json")
        .header("x-hangar-sig", list)
        .body(Body::from("{}"))
        .unwrap();
    assert_eq!(c.send(r).await.0, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn filesystem_rpcs() {
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    let (s, r) = c.unary("filesystem.Filesystem/MakeDir", json!({"path": "a/b"})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(r["entry"]["type"], "FILE_TYPE_DIRECTORY");
    let (s, r) = c.unary("filesystem.Filesystem/MakeDir", json!({"path": "a/b"})).await;
    assert_eq!((s, r["code"].as_str()), (StatusCode::CONFLICT, Some("already_exists")));
    std::fs::write(d.path().join("a/b/f"), "x").unwrap();
    let (_, r) = c.unary("filesystem.Filesystem/ListDir", json!({"path": "a", "depth": 2})).await;
    let names: Vec<&str> = r["entries"].as_array().unwrap().iter().map(|e| e["name"].as_str().unwrap()).collect();
    assert_eq!(names, vec!["b", "f"]);
    let (s, r) = c.unary("filesystem.Filesystem/Move", json!({"source": "a/b/f", "destination": "c/g"})).await;
    assert_eq!((s, r["entry"]["name"].as_str()), (StatusCode::OK, Some("g")));
    let (s, _) = c.unary("filesystem.Filesystem/Remove", json!({"path": "a"})).await;
    assert_eq!(s, StatusCode::OK);
    let (s, r) = c.unary("filesystem.Filesystem/Stat", json!({"path": "a"})).await;
    assert_eq!((s, r["code"].as_str()), (StatusCode::NOT_FOUND, Some("not_found")));
    let (s, _) = c.unary("filesystem.Filesystem/Remove", json!({"path": "a"})).await;
    assert_eq!(s, StatusCode::OK, "removing what isn't there is fine (rm -rf)");
    let (s, r) = c.unary("filesystem.Filesystem/MakeDir", json!({"path": "c/g"})).await;
    assert_eq!((s, r["code"].as_str()), (StatusCode::BAD_REQUEST, Some("invalid_argument")));
}

#[tokio::test]
async fn start_deadline_bounds_the_process_and_connect_sees_recent_exits() {
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    let body = frame(&json!({"process": {"cmd": "sleep", "args": ["30"]}}));
    let mut req = Client::req("POST", "/process.Process/Start", "application/connect+json", body);
    req.headers_mut().insert("connect-timeout-ms", "300".parse().unwrap());
    let t = std::time::Instant::now();
    let (_, b) = c.send(req).await;
    let (_, end) = unframe(&b);
    assert_eq!(end["error"]["code"], "deadline_exceeded");
    assert!(t.elapsed() < Duration::from_secs(5));
    // The process was killed, and its end is still there for a client that reconnects.
    let (msgs, _) = c.stream("process.Process/Start", json!({"process": {"cmd": "sh", "args": ["-c", "cat /proc/self/oom_score_adj; exit 4"]}, "tag": "done"})).await;
    assert_eq!(output(&msgs, "stdout"), "100\n", "children are the OOM killer's first choice");
    let (msgs, end) = c.stream("process.Process/Connect", json!({"process": {"tag": "done"}})).await;
    assert_eq!(end, json!({}));
    assert_eq!(msgs.len(), 2);
    assert_eq!(msgs[1]["event"]["end"]["exitCode"].as_i64(), Some(4));
    assert_eq!(msgs[1]["event"]["end"]["error"], "exit status 4");
    let (_, end) = c.stream("process.Process/Connect", json!({"process": {"pid": 1}})).await;
    assert_eq!(end["error"]["code"], "not_found");
}

#[tokio::test]
async fn stopping_a_process_stops_its_children() {
    // SendSignal goes to the whole process group (pipes) or session (PTY): a shell's background jobs go too.
    let d = tempfile::tempdir().unwrap();
    let c = Arc::new(Client::new(cfg(d.path(), 8)));
    for pty in [false, true] {
        let tag = format!("tree-{pty}");
        let mut start = json!({"process": {"cmd": "sh", "args": ["-c", "sleep 300 & echo child=$!; wait"]}, "tag": tag});
        if pty {
            start["pty"] = json!({"size": {"cols": 80, "rows": 24}});
        }
        let c2 = c.clone();
        let run = tokio::spawn(async move { c2.stream("process.Process/Start", start).await });
        let pidfile = |pid: u64| format!("/proc/{pid}/stat");
        let mut child = 0u64;
        // The child's pid shows up in the output; find it through /proc instead of the stream (still open).
        for _ in 0..200 {
            if let Ok(rd) = std::fs::read_dir("/proc") {
                for e in rd.flatten() {
                    let Ok(pid) = e.file_name().to_string_lossy().parse::<u64>() else { continue };
                    let cmd = std::fs::read(format!("/proc/{pid}/cmdline")).unwrap_or_default();
                    let stat = std::fs::read_to_string(pidfile(pid)).unwrap_or_default();
                    // sleep 300, whose parent is a shell this test started (its tag's process)
                    if cmd == b"sleep\x00300\x00" && !stat.contains(") Z ") {
                        let (_, list) = c.unary("process.Process/List", json!({})).await;
                        let ppid: u64 = stat.rsplit(')').next().unwrap().split_whitespace().nth(1).unwrap().parse().unwrap();
                        // An empty list is left out of the JSON: a stray `sleep 300` can turn up before ours has started.
                        if list["processes"].as_array().into_iter().flatten().any(|p| p["tag"] == tag && p["pid"] == ppid) {
                            child = pid;
                        }
                    }
                }
            }
            if child > 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(child > 0, "no background child found (pty={pty})");
        let (s, _) = c.unary("process.Process/SendSignal", json!({"process": {"tag": tag}, "signal": "SIGNAL_SIGKILL"})).await;
        assert_eq!(s, StatusCode::OK);
        let _ = run.await.unwrap();
        let gone = || std::fs::read_to_string(pidfile(child)).map(|s| s.contains(") Z ")).unwrap_or(true);
        for _ in 0..100 {
            if gone() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(gone(), "the background child outlived its parent's kill (pty={pty})");
    }
}

// ---------------------------------------------------------------------- unauthenticated peers

#[test]
fn release_builds_unwind_panics() {
    // With panic = "abort", one panic in one request's handler kills the daemon and every stream it serves. Tests
    // always unwind, so the profile is checked here.
    let release = include_str!("../Cargo.toml").split("[profile.release]").nth(1).unwrap_or("");
    let release = release.split("\n[").next().unwrap_or("");
    assert!(!release.replace(' ', "").contains("panic=\"abort\""), "[profile.release] aborts on panic");
}

/// A signature with the right shape and a fresh time, not made with the token: what any network peer can send.
fn forged() -> String {
    let ts = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis();
    format!("{ts}.{:032x}.chunked.{}", rand_u128(), "00".repeat(32))
}

#[tokio::test]
async fn a_forged_upload_has_no_side_effects() {
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    let pq = format!("/files?path={}", d.path().join("made/by/a/stranger/f").display());
    let req = Request::builder()
        .method("POST")
        .uri(&pq)
        .header("content-type", "application/octet-stream")
        .header("x-hangar-sig", forged())
        .body(Body::from(vec![b'x'; 1 << 20]))
        .unwrap();
    assert_eq!(c.send(req).await.0, StatusCode::UNAUTHORIZED);
    assert!(!d.path().join("made").exists(), "directories were created for an unauthenticated request");
}

#[tokio::test]
async fn a_forged_request_is_refused_before_its_body() {
    use futures::StreamExt;
    let d = tempfile::tempdir().unwrap();
    let c = Client::new(cfg(d.path(), 4));
    for path in ["/process.Process/List", "/files?path=x"] {
        // A body that never ends (a stalled peer): the refusal must not wait for it, or buffer it.
        let body = futures::stream::once(async { Ok::<_, std::io::Error>(bytes::Bytes::from_static(b"{")) })
            .chain(futures::stream::pending());
        let req = Request::builder()
            .method("POST")
            .uri(path)
            .header("content-type", "application/octet-stream")
            .header("x-hangar-sig", forged())
            .body(Body::from_stream(body))
            .unwrap();
        let resp = tokio::time::timeout(Duration::from_secs(3), c.app.clone().oneshot(req)).await;
        let resp = resp.expect("the daemon waited for an unauthenticated body").unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED, "{path}");
    }
}

/// A refused HTTP/1.1 request leaves the connection in a state the client can trust (#207): a small body is read so
/// the client neither hits a broken pipe writing it nor reuses a connection the daemon silently closed, and a body
/// too large to read is answered with `connection: close`.
#[tokio::test]
async fn a_refused_http1_request_does_not_leave_a_dead_connection() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let d = tempfile::tempdir().unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (app, _) = crate::router(cfg(d.path(), 4));
    tokio::spawn(crate::server::serve(listener, app, crate::server::LIMITS, std::future::pending(), Duration::ZERO));

    async fn head(c: &mut tokio::net::TcpStream) -> String {
        let mut got = Vec::new();
        let mut b = [0u8; 4096];
        while !got.windows(4).any(|w| w == b"\r\n\r\n") {
            let n = tokio::time::timeout(Duration::from_secs(5), c.read(&mut b)).await.unwrap().unwrap();
            assert!(n > 0, "closed before answering: {}", String::from_utf8_lossy(&got));
            got.extend_from_slice(&b[..n]);
        }
        String::from_utf8_lossy(&got).to_lowercase()
    }

    let start = |len: usize| {
        format!("POST /process.Process/Start HTTP/1.1\r\nhost: x\r\nx-access-token: wrong\r\n\
                 content-type: application/connect+json\r\ncontent-length: {len}\r\n\r\n")
    };
    // The body arrives after the headers, as a client writing them separately sends it.
    let mut c = tokio::net::TcpStream::connect(addr).await.unwrap();
    c.write_all(start(4096).as_bytes()).await.unwrap();
    tokio::time::sleep(Duration::from_millis(50)).await;
    c.write_all(&[b'x'; 4096]).await.expect("the body could be written");
    let h = head(&mut c).await;
    assert!(h.starts_with("http/1.1 401") && !h.contains("connection: close"), "{h}");
    c.write_all(b"GET /health HTTP/1.1\r\nhost: x\r\n\r\n").await.unwrap();
    assert!(head(&mut c).await.starts_with("http/1.1 204"), "the connection is usable after the 401");

    // Too large to read for a stranger: refused without reading it, and the client is told not to reuse the connection.
    let mut c = tokio::net::TcpStream::connect(addr).await.unwrap();
    c.write_all(start(10 << 20).as_bytes()).await.unwrap();
    c.write_all(&[b'x'; 128 << 10]).await.unwrap();
    let h = head(&mut c).await;
    assert!(h.starts_with("http/1.1 401") && h.contains("connection: close"), "{h}");
}
