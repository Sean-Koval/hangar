//! The accept loop: HTTP/1.1 and cleartext HTTP/2 on one port, with limits on what a peer that never authenticates
//! can hold. Anything that can reach the port can connect (on Docker's default bridge, that includes other sandboxes),
//! so connections are capped in total and per peer address, and a connection that sends no request for `idle` is
//! closed (a silent peer, a stalled header, an idle keep-alive). A peer over its own cap is turned away; at the total
//! cap the daemon stops accepting until a connection closes, so new connections wait in the backlog rather than fail.

use std::collections::HashMap;
use std::future::Future;
use std::net::IpAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use hyper_util::server::conn::auto;
use hyper_util::service::TowerToHyperService;
use tokio::sync::Semaphore;

#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub max_conns: usize,
    pub max_conns_per_peer: usize,
    /// How long a connection may sit without sending a request (or the rest of its headers).
    pub idle: Duration,
}

/// hangar's pool keeps idle connections 30 s, so `idle` must be longer: a connection the daemon closes just as the
/// client reuses it fails a request that may have to be reported rather than retried.
pub const LIMITS: Limits = Limits { max_conns: 1024, max_conns_per_peer: 512, idle: Duration::from_secs(75) };

#[derive(Default)]
struct Peers(Mutex<HashMap<IpAddr, usize>>);

struct PeerSlot(Arc<Peers>, IpAddr);

impl Peers {
    fn enter(self: &Arc<Self>, ip: IpAddr, max: usize) -> Option<PeerSlot> {
        let mut m = crate::lock(&self.0);
        let n = m.entry(ip).or_insert(0);
        if *n >= max {
            return None;
        }
        *n += 1;
        Some(PeerSlot(self.clone(), ip))
    }
}

impl Drop for PeerSlot {
    fn drop(&mut self) {
        let mut m = crate::lock(&(self.0).0);
        if let Some(n) = m.get_mut(&self.1) {
            *n -= 1;
            if *n == 0 {
                m.remove(&self.1);
            }
        }
    }
}

fn builder(limits: Limits) -> auto::Builder<TokioExecutor> {
    let mut b = auto::Builder::new(TokioExecutor::new());
    b.http1().timer(TokioTimer::new()).header_read_timeout(limits.idle);
    b.http2()
        .timer(TokioTimer::new())
        .keep_alive_interval(Some(Duration::from_secs(30)))
        .keep_alive_timeout(Duration::from_secs(20))
        .max_concurrent_streams(256);
    b
}

/// Serves `app` on `listener` until `shutdown` resolves, then stops accepting and gives open connections up to
/// `grace` to finish.
pub async fn serve(
    listener: tokio::net::TcpListener,
    app: axum::Router,
    limits: Limits,
    shutdown: impl Future<Output = ()>,
    grace: Duration,
) {
    let conns = Arc::new(Semaphore::new(limits.max_conns));
    let peers = Arc::new(Peers::default());
    let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
    let accept = async {
        loop {
            let Ok(permit) = conns.clone().acquire_owned().await else { return };
            let (tcp, peer) = match listener.accept().await {
                Ok(c) => c,
                Err(e) => {
                    // Out of descriptors, or a connection reset before it was accepted: keep serving the others.
                    eprintln!("hangar-envd: accept: {e}");
                    tokio::time::sleep(Duration::from_millis(100)).await;
                    continue;
                }
            };
            let Some(slot) = peers.enter(peer.ip(), limits.max_conns_per_peer) else { continue };
            let _ = tcp.set_nodelay(true);
            let app = app.clone();
            let mut stop = stop_rx.clone();
            tokio::spawn(async move {
                let _held = (permit, slot);
                // HTTP version detection waits for the first bytes with no timeout of its own.
                tokio::select! {
                    r = tokio::time::timeout(limits.idle, tcp.readable()) => if !matches!(r, Ok(Ok(()))) { return },
                    _ = stop.wait_for(|s| *s) => return,
                }
                let b = builder(limits);
                let conn = b.serve_connection(TokioIo::new(tcp), TowerToHyperService::new(app));
                tokio::pin!(conn);
                tokio::select! {
                    _ = conn.as_mut() => return,
                    _ = stop.wait_for(|s| *s) => conn.as_mut().graceful_shutdown(),
                }
                let _ = conn.await; // idle connections close now; requests under way get to finish
            });
        }
    };
    tokio::select! {
        _ = accept => {}
        _ = shutdown => {}
    }
    drop(listener);
    let _ = stop_tx.send(true);
    let _ = tokio::time::timeout(grace, conns.acquire_many(limits.max_conns as u32)).await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    async fn start(limits: Limits) -> (std::net::SocketAddr, tokio::sync::oneshot::Sender<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let app = axum::Router::new().route("/health", axum::routing::get(|| async { "ok" }));
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        tokio::spawn(serve(listener, app, limits, async { let _ = rx.await; }, Duration::from_millis(100)));
        (addr, tx)
    }

    /// Whether the server closed the connection within `within` (reading nothing).
    async fn closed(c: &mut tokio::net::TcpStream, within: Duration) -> bool {
        let mut b = [0u8; 64];
        matches!(tokio::time::timeout(within, c.read(&mut b)).await, Ok(Ok(0)) | Ok(Err(_)))
    }

    async fn get(addr: std::net::SocketAddr) -> String {
        let mut c = tokio::net::TcpStream::connect(addr).await.unwrap();
        c.write_all(b"GET /health HTTP/1.1\r\nhost: x\r\nconnection: close\r\n\r\n").await.unwrap();
        let mut s = String::new();
        let _ = tokio::time::timeout(Duration::from_secs(5), c.read_to_string(&mut s)).await;
        s
    }

    #[tokio::test]
    async fn silent_and_stalled_connections_are_closed() {
        let (addr, _stop) = start(Limits { max_conns: 16, max_conns_per_peer: 16, idle: Duration::from_millis(300) }).await;
        let mut silent = tokio::net::TcpStream::connect(addr).await.unwrap();
        let mut stalled = tokio::net::TcpStream::connect(addr).await.unwrap();
        stalled.write_all(b"GET /health HTTP/1.1\r\nhost: x\r\n").await.unwrap(); // never finishes its headers
        assert!(closed(&mut silent, Duration::from_secs(3)).await);
        assert!(closed(&mut stalled, Duration::from_secs(3)).await);
        assert!(get(addr).await.starts_with("HTTP/1.1 200"));
    }

    #[tokio::test]
    async fn one_peer_cannot_take_every_connection() {
        let (addr, _stop) = start(Limits { max_conns: 16, max_conns_per_peer: 2, idle: Duration::from_secs(30) }).await;
        let mut held = Vec::new();
        for _ in 0..2 {
            let mut c = tokio::net::TcpStream::connect(addr).await.unwrap();
            c.write_all(b"GET").await.unwrap();
            held.push(c);
        }
        let mut third = tokio::net::TcpStream::connect(addr).await.unwrap();
        assert!(closed(&mut third, Duration::from_secs(3)).await, "over its cap, a peer is turned away");
        drop(held);
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(get(addr).await.starts_with("HTTP/1.1 200"), "and is served again once under it");
    }

    #[tokio::test]
    async fn shutdown_closes_idle_connections_and_stops_listening() {
        let (addr, stop) = start(Limits { max_conns: 16, max_conns_per_peer: 16, idle: Duration::from_secs(30) }).await;
        let mut kept = tokio::net::TcpStream::connect(addr).await.unwrap();
        kept.write_all(b"GET /health HTTP/1.1\r\nhost: x\r\n\r\n").await.unwrap();
        let mut b = [0u8; 256];
        assert!(kept.read(&mut b).await.unwrap() > 0); // answered; the connection stays open for the next request
        stop.send(()).unwrap();
        assert!(closed(&mut kept, Duration::from_secs(1)).await, "an idle keep-alive connection outlived shutdown");
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(tokio::net::TcpStream::connect(addr).await.is_err());
    }

    #[tokio::test]
    async fn at_the_total_cap_new_connections_wait_for_a_free_slot() {
        let (addr, _stop) = start(Limits { max_conns: 1, max_conns_per_peer: 8, idle: Duration::from_millis(500) }).await;
        let mut first = tokio::net::TcpStream::connect(addr).await.unwrap();
        first.write_all(b"GET").await.unwrap();
        let t = std::time::Instant::now();
        assert!(get(addr).await.starts_with("HTTP/1.1 200"), "served once the stalled one timed out");
        assert!(t.elapsed() >= Duration::from_millis(300));
    }
}
