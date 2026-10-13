//! Request authentication. Deny by default; two ways in:
//!
//! - `X-Access-Token: <token>`, compared in constant time: E2B's scheme, for E2B clients.
//! - `X-Hangar-Sig: <unix_ms>.<nonce>.<hex hmac>`: hangar's. The HMAC (key: the token) covers the time, a nonce,
//!   the method, the path with its query and the SHA-256 of the body, so a request captured on its way (say, by
//!   whatever now answers at a stale address) is good for nothing else: not for another request, not twice, not
//!   after five minutes. The token itself never crosses the network.
//!
//! Upload bodies are streamed to disk, so for `POST /files` the signature is checked by the upload handler after
//! the last byte and before the file replaces anything ([`PendingSig`]).

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use hmac::{Hmac, KeyInit, Mac};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

/// How far a signature's time may be from ours, either way.
pub const WINDOW: Duration = Duration::from_secs(300);
/// Largest body checked in memory (Connect messages are capped at 4 MiB; this leaves room for framing).
pub const MAX_SIGNED_BODY: usize = 8 << 20;
const MAX_NONCES: usize = 1 << 20;

pub struct Auth {
    token: String,
    nonces: Mutex<HashMap<String, Instant>>,
}

/// A signature whose body hash the upload handler must finish.
#[derive(Clone)]
pub struct PendingSig {
    pub prefix: String,
    pub mac: Vec<u8>,
}

impl PendingSig {
    pub fn verify(&self, token: &str, body_sha256: &[u8]) -> bool {
        verify_mac(token, &self.prefix, body_sha256, &self.mac)
    }
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn unhex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 {
        return None;
    }
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(s.get(i..i + 2)?, 16).ok()).collect()
}

fn verify_mac(token: &str, prefix: &str, body_sha256: &[u8], mac: &[u8]) -> bool {
    let Ok(mut m) = Hmac::<Sha256>::new_from_slice(token.as_bytes()) else { return false };
    m.update(prefix.as_bytes());
    m.update(hex(body_sha256).as_bytes());
    m.verify_slice(mac).is_ok()
}

pub fn sha256(data: &[u8]) -> Vec<u8> {
    Sha256::digest(data).to_vec()
}

pub enum Check {
    /// Authenticated (the token, or a signature over an empty or already checked body).
    Ok,
    /// The signature is well-formed and fresh; the caller still has to hash the body and call `verify`.
    Body(PendingSig),
    Denied(&'static str),
}

impl Auth {
    pub fn new(token: String) -> Self {
        Auth { token, nonces: Mutex::new(HashMap::new()) }
    }

    pub fn token(&self) -> &str {
        &self.token
    }

    pub fn check(&self, access_token: Option<&[u8]>, sig: Option<&str>, method: &str, path_and_query: &str) -> Check {
        if let Some(t) = access_token {
            return if bool::from(t.ct_eq(self.token.as_bytes())) { Check::Ok } else { Check::Denied("wrong X-Access-Token") };
        }
        let Some(sig) = sig else { return Check::Denied("missing X-Access-Token or X-Hangar-Sig") };
        let mut parts = sig.trim().splitn(3, '.');
        let (Some(ts), Some(nonce), Some(mac)) = (parts.next(), parts.next(), parts.next()) else {
            return Check::Denied("malformed X-Hangar-Sig");
        };
        let (Ok(ts_ms), Some(mac)) = (ts.parse::<u64>(), unhex(mac)) else { return Check::Denied("malformed X-Hangar-Sig") };
        if nonce.len() < 16 || nonce.len() > 64 || !nonce.bytes().all(|b| b.is_ascii_alphanumeric()) {
            return Check::Denied("malformed X-Hangar-Sig");
        }
        let now_ms = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0);
        if now_ms.abs_diff(ts_ms) > WINDOW.as_millis() as u64 {
            return Check::Denied("X-Hangar-Sig is outside the time window (check both clocks)");
        }
        let prefix = format!("{ts}\n{nonce}\n{method}\n{path_and_query}\n");
        // Spent before the body is checked (for an upload, that's after its last byte). Burning a nonce takes
        // having seen it, and costs only the one request it belonged to.
        if !self.spend(nonce) {
            return Check::Denied("X-Hangar-Sig was already used");
        }
        Check::Body(PendingSig { prefix, mac })
    }

    pub fn verify(&self, p: &PendingSig, body: &[u8]) -> bool {
        p.verify(&self.token, &sha256(body))
    }

    fn spend(&self, nonce: &str) -> bool {
        let mut n = self.nonces.lock().unwrap();
        let now = Instant::now();
        if n.len() >= MAX_NONCES / 2 {
            n.retain(|_, t| now.duration_since(*t) < 2 * WINDOW);
        }
        if n.len() >= MAX_NONCES {
            return false; // a flood of signed requests: refuse rather than forget nonces still in their window
        }
        n.insert(nonce.to_string(), now).is_none()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TOKEN: &str = "0123456789abcdef0123456789abcdef";

    fn sign(token: &str, ts: u64, nonce: &str, method: &str, pq: &str, body: &[u8]) -> String {
        let mut m = Hmac::<Sha256>::new_from_slice(token.as_bytes()).unwrap();
        m.update(format!("{ts}\n{nonce}\n{method}\n{pq}\n").as_bytes());
        m.update(hex(&sha256(body)).as_bytes());
        format!("{ts}.{nonce}.{}", hex(&m.finalize().into_bytes()))
    }

    fn now() -> u64 {
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
    }

    fn passes(a: &Auth, sig: &str, method: &str, pq: &str, body: &[u8]) -> bool {
        match a.check(None, Some(sig), method, pq) {
            Check::Body(p) => a.verify(&p, body),
            _ => false,
        }
    }

    #[test]
    fn token_is_compared_exactly() {
        let a = Auth::new(TOKEN.into());
        assert!(matches!(a.check(Some(TOKEN.as_bytes()), None, "GET", "/info"), Check::Ok));
        assert!(matches!(a.check(Some(b"0123456789abcdef0123456789abcdeX"), None, "GET", "/info"), Check::Denied(_)));
        assert!(matches!(a.check(Some(b""), None, "GET", "/info"), Check::Denied(_)));
        assert!(matches!(a.check(None, None, "GET", "/info"), Check::Denied(_)));
    }

    #[test]
    fn signatures_bind_the_request_and_are_single_use() {
        let a = Auth::new(TOKEN.into());
        let s = sign(TOKEN, now(), "aaaaaaaaaaaaaaaa1", "POST", "/process.Process/Start", b"body");
        assert!(passes(&a, &s, "POST", "/process.Process/Start", b"body"));
        assert!(!passes(&a, &s, "POST", "/process.Process/Start", b"body"), "replayed");
        let s = sign(TOKEN, now(), "aaaaaaaaaaaaaaaa2", "POST", "/process.Process/Start", b"body");
        assert!(!passes(&a, &s, "POST", "/process.Process/Start", b"other body"));
        let s = sign(TOKEN, now(), "aaaaaaaaaaaaaaaa3", "POST", "/process.Process/Start", b"body");
        assert!(!passes(&a, &s, "POST", "/filesystem.Filesystem/Remove", b"body"));
        let s = sign(TOKEN, now(), "aaaaaaaaaaaaaaaa4", "GET", "/files?path=a", b"");
        assert!(!passes(&a, &s, "GET", "/files?path=b", b""));
        let s = sign("another sandbox's token 0123456", now(), "aaaaaaaaaaaaaaaa5", "GET", "/info", b"");
        assert!(!passes(&a, &s, "GET", "/info", b""));
        let old = sign(TOKEN, now() - 301_000, "aaaaaaaaaaaaaaaa6", "GET", "/info", b"");
        assert!(!passes(&a, &old, "GET", "/info", b""));
        assert!(matches!(a.check(None, Some("1.2"), "GET", "/info"), Check::Denied(_)));
        assert!(matches!(a.check(None, Some("x.y.z"), "GET", "/info"), Check::Denied(_)));
    }
}
