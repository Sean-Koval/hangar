//! Request authentication. Deny by default; two ways in:
//!
//! - `X-Access-Token: <token>`, compared in constant time: E2B's scheme, for E2B clients.
//! - `X-Hangar-Sig: <unix_ms>.<nonce>.<body>.<hex hmac>`: hangar's. The HMAC (key: the token) covers the time, a
//!   nonce, the method, the path with its query and `<body>`: the body's SHA-256 in hex, or `chunked` for an upload
//!   whose body carries its own chained MACs ([`ChunkReader`]). A request captured on its way (say, by whatever now
//!   answers at a stale address) is good for nothing else: not for another request or body, not twice, not after
//!   five minutes. The token itself never crosses the network.
//!
//! The signature is checked before the body is read, so a peer without the token gets nothing done, nothing buffered
//! and no nonce spent. The body is then checked against what was signed: in memory for RPCs (they are small), as it
//! streams to disk for uploads, before the file replaces anything. Responses to signed requests carry
//! `X-Hangar-Proof`, an HMAC over the request's, so hangar can tell its sandbox's daemon from whatever else answers.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use bytes::{Buf, Bytes, BytesMut};
use hmac::{Hmac, KeyInit, Mac};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

/// How far a signature's time may be from ours, either way.
pub const WINDOW: Duration = Duration::from_secs(300);
/// Largest body checked in memory (Connect messages are capped at 4 MiB; this leaves room for framing).
pub const MAX_SIGNED_BODY: usize = 8 << 20;
/// Largest chunk of a chunked upload (what one upload holds in memory).
pub const MAX_CHUNK: usize = 4 << 20;
const MAX_NONCES: usize = 1 << 20;
/// A nonce must be remembered while its signature could still be accepted: its time may be up to WINDOW ahead.
const NONCE_TTL: Duration = Duration::from_secs(2 * 300 + 1);

pub struct Auth {
    token: String,
    nonces: Mutex<Nonces>,
}

#[derive(Default)]
struct Nonces {
    seen: HashMap<String, Instant>,
    pruned: Option<Instant>,
}

/// What a valid signature says about the body.
#[derive(Clone, Debug, PartialEq)]
pub enum SignedBody {
    Sha256([u8; 32]),
    Chunked,
}

/// A request whose signature checked out (and whose nonce is now spent); the body is still to be checked.
#[derive(Clone)]
pub struct Signed {
    pub mac: Vec<u8>,
    pub body: SignedBody,
}

pub enum Check {
    /// The X-Access-Token.
    Token,
    Signed(Signed),
    Denied(&'static str),
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn unhex(s: &str) -> Option<Vec<u8>> {
    if !s.len().is_multiple_of(2) {
        return None;
    }
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(s.get(i..i + 2)?, 16).ok()).collect()
}

pub fn sha256(data: &[u8]) -> Vec<u8> {
    Sha256::digest(data).to_vec()
}

impl Auth {
    pub fn new(token: String) -> Self {
        Auth { token, nonces: Mutex::default() }
    }

    fn hmac(&self) -> Hmac<Sha256> {
        // Any key length is valid for HMAC.
        Hmac::<Sha256>::new_from_slice(self.token.as_bytes()).expect("HMAC takes any key")
    }

    pub fn check(&self, access_token: Option<&[u8]>, sig: Option<&str>, method: &str, path_and_query: &str) -> Check {
        if let Some(t) = access_token {
            return if bool::from(t.ct_eq(self.token.as_bytes())) { Check::Token } else { Check::Denied("wrong X-Access-Token") };
        }
        let Some(sig) = sig else { return Check::Denied("missing X-Access-Token or X-Hangar-Sig") };
        let parts: Vec<&str> = sig.trim().split('.').collect();
        let [ts, nonce, body, mac] = parts[..] else { return Check::Denied("malformed X-Hangar-Sig") };
        let (Ok(ts_ms), Some(mac)) = (ts.parse::<u64>(), unhex(mac)) else { return Check::Denied("malformed X-Hangar-Sig") };
        if nonce.len() < 16 || nonce.len() > 64 || !nonce.bytes().all(|b| b.is_ascii_alphanumeric()) {
            return Check::Denied("malformed X-Hangar-Sig");
        }
        let body_sig = if body == "chunked" {
            SignedBody::Chunked
        } else {
            match unhex(body).and_then(|b| <[u8; 32]>::try_from(b).ok()) {
                Some(h) => SignedBody::Sha256(h),
                None => return Check::Denied("malformed X-Hangar-Sig"),
            }
        };
        let now_ms = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0);
        if now_ms.abs_diff(ts_ms) > WINDOW.as_millis() as u64 {
            return Check::Denied("X-Hangar-Sig is outside the time window (check both clocks)");
        }
        let mut m = self.hmac();
        m.update(format!("{ts}\n{nonce}\n{method}\n{path_and_query}\n{body}").as_bytes());
        if m.verify_slice(&mac).is_err() {
            return Check::Denied("X-Hangar-Sig does not match the request");
        }
        // Spent only once the MAC is good: burning a nonce takes the token.
        if !self.spend(nonce) {
            return Check::Denied("X-Hangar-Sig was already used");
        }
        Check::Signed(Signed { mac, body: body_sig })
    }

    /// `X-Hangar-Proof` for the response to a signed request: only a holder of the token can make it.
    pub fn proof(&self, request_mac: &[u8]) -> String {
        let mut m = self.hmac();
        m.update(b"proof\n");
        m.update(request_mac);
        hex(&m.finalize().into_bytes())
    }

    /// Checks a chunked upload's frames as they arrive; see [`ChunkReader`].
    pub fn chunk_reader(&self, s: &Signed) -> ChunkReader {
        ChunkReader { mac: self.hmac(), prev: s.mac.clone(), buf: BytesMut::new(), done: false }
    }

    fn spend(&self, nonce: &str) -> bool {
        let mut n = crate::lock(&self.nonces);
        let now = Instant::now();
        if n.seen.len() >= MAX_NONCES / 2 || n.pruned.is_none_or(|t| now.duration_since(t) > Duration::from_secs(30)) {
            n.seen.retain(|_, t| now.duration_since(*t) < NONCE_TTL);
            n.pruned = Some(now);
        }
        if n.seen.len() >= MAX_NONCES {
            return false; // a flood of signed requests: refuse rather than forget nonces still in their window
        }
        n.seen.insert(nonce.to_string(), now).is_none()
    }

    #[cfg(test)]
    fn spent(&self) -> usize {
        crate::lock(&self.nonces).seen.len()
    }
}

/// The body of an upload signed `chunked`: frames of `u32 length (big-endian) | data | HMAC`, each HMAC over
/// `"chunk\n" | the previous HMAC (the request's for the first) | length | data`, ended by a frame of length 0.
/// Data is released only after its frame's HMAC checks out, so nothing unauthenticated reaches the disk, and the
/// chain makes frames impossible to reorder, drop, splice from another request or cut short unnoticed.
pub struct ChunkReader {
    mac: Hmac<Sha256>,
    prev: Vec<u8>,
    buf: BytesMut,
    done: bool,
}

impl ChunkReader {
    /// Adds received bytes; returns the data of every complete frame they finish, in order.
    pub fn push(&mut self, data: &[u8]) -> Result<Vec<Bytes>, &'static str> {
        if self.done {
            return if data.is_empty() { Ok(vec![]) } else { Err("data after the upload's last chunk") };
        }
        self.buf.extend_from_slice(data);
        let mut out = Vec::new();
        while self.buf.len() >= 4 {
            let n = u32::from_be_bytes([self.buf[0], self.buf[1], self.buf[2], self.buf[3]]) as usize;
            if n > MAX_CHUNK {
                return Err("an upload chunk is over the limit");
            }
            if self.buf.len() < 4 + n + 32 {
                break;
            }
            let mut m = self.mac.clone();
            m.update(b"chunk\n");
            m.update(&self.prev);
            m.update(&self.buf[..4 + n]);
            let want = m.finalize().into_bytes();
            if !bool::from(want.as_slice().ct_eq(&self.buf[4 + n..4 + n + 32])) {
                return Err("an upload chunk does not match its signature");
            }
            self.prev = want.to_vec();
            self.buf.advance(4);
            let chunk = self.buf.split_to(n).freeze();
            self.buf.advance(32);
            if n == 0 {
                self.done = true;
                if !self.buf.is_empty() {
                    return Err("data after the upload's last chunk");
                }
                break;
            }
            out.push(chunk);
        }
        Ok(out)
    }

    /// The body ended: fine only after the last frame.
    pub fn finish(&self) -> Result<(), &'static str> {
        if self.done { Ok(()) } else { Err("the upload ended before its last chunk") }
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    const TOKEN: &str = "0123456789abcdef0123456789abcdef";

    pub fn sign_body(token: &str, ts: u64, nonce: &str, method: &str, pq: &str, body: &str) -> String {
        let mut m = Hmac::<Sha256>::new_from_slice(token.as_bytes()).unwrap();
        m.update(format!("{ts}\n{nonce}\n{method}\n{pq}\n{body}").as_bytes());
        format!("{ts}.{nonce}.{body}.{}", hex(&m.finalize().into_bytes()))
    }

    fn sign(token: &str, ts: u64, nonce: &str, method: &str, pq: &str, body: &[u8]) -> String {
        sign_body(token, ts, nonce, method, pq, &hex(&sha256(body)))
    }

    pub fn mac_of(sig: &str) -> Vec<u8> {
        unhex(sig.rsplit('.').next().unwrap()).unwrap()
    }

    /// The frames of a chunked upload body, as hangar sends them.
    pub fn chunked(token: &str, sig: &str, data: &[u8], size: usize) -> Vec<u8> {
        let mut prev = unhex(sig.rsplit('.').next().unwrap()).unwrap();
        let mut out = Vec::new();
        for part in data.chunks(size).chain(std::iter::once(&[][..])) {
            let mut m = Hmac::<Sha256>::new_from_slice(token.as_bytes()).unwrap();
            let len = (part.len() as u32).to_be_bytes();
            m.update(b"chunk\n");
            m.update(&prev);
            m.update(&len);
            m.update(part);
            prev = m.finalize().into_bytes().to_vec();
            out.extend_from_slice(&len);
            out.extend_from_slice(part);
            out.extend_from_slice(&prev);
        }
        out
    }

    fn now() -> u64 {
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
    }

    fn passes(a: &Auth, sig: &str, method: &str, pq: &str, body: &[u8]) -> bool {
        match a.check(None, Some(sig), method, pq) {
            Check::Signed(Signed { body: SignedBody::Sha256(h), .. }) => h.as_slice() == sha256(body),
            _ => false,
        }
    }

    #[test]
    fn token_is_compared_exactly() {
        let a = Auth::new(TOKEN.into());
        assert!(matches!(a.check(Some(TOKEN.as_bytes()), None, "GET", "/info"), Check::Token));
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
        let ahead = sign(TOKEN, now() + 301_000, "aaaaaaaaaaaaaaaa7", "GET", "/info", b"");
        assert!(!passes(&a, &ahead, "GET", "/info", b""));
        let skewed = sign(TOKEN, now() + 200_000, "aaaaaaaaaaaaaaaa8", "GET", "/info", b"");
        assert!(passes(&a, &skewed, "GET", "/info", b""), "clocks a few minutes apart still work");
        // The body part can't be swapped either.
        let s = sign_body(TOKEN, now(), "aaaaaaaaaaaaaaaa9", "POST", "/files?path=a", "chunked");
        let other = s.replacen(".chunked.", &format!(".{}.", hex(&sha256(b"x"))), 1);
        assert!(matches!(a.check(None, Some(&other), "POST", "/files?path=a"), Check::Denied(_)));
        assert!(matches!(a.check(None, Some(&s), "POST", "/files?path=a"), Check::Signed(Signed { body: SignedBody::Chunked, .. })));
        assert!(matches!(a.check(None, Some("1.2"), "GET", "/info"), Check::Denied(_)));
        assert!(matches!(a.check(None, Some("x.y.z.w"), "GET", "/info"), Check::Denied(_)));
        let three_part = format!("{}.aaaaaaaaaaaaaaaa10.{}", now(), "00".repeat(32));
        assert!(matches!(a.check(None, Some(&three_part), "GET", "/info"), Check::Denied(_)));
    }

    #[test]
    fn forged_signatures_spend_nothing() {
        // Whoever reaches the port can send well-formed signatures with fresh times. Without the token they must
        // not be able to burn nonces or fill the nonce table (which locks hangar out once it is full).
        let a = Auth::new(TOKEN.into());
        for i in 0..1000 {
            let forged = format!("{}.forgednonce{i:08}.{}.{}", now(), hex(&sha256(b"")), "00".repeat(32));
            assert!(matches!(a.check(None, Some(&forged), "GET", "/info"), Check::Denied(_)));
        }
        assert_eq!(a.spent(), 0, "unauthenticated requests took nonce table entries");
    }

    #[test]
    fn proofs_belong_to_the_token_and_the_request() {
        let a = Auth::new(TOKEN.into());
        let b = Auth::new("another sandbox's token 0123456".into());
        assert_eq!(a.proof(b"mac one"), a.proof(b"mac one"));
        assert_ne!(a.proof(b"mac one"), a.proof(b"mac two"));
        assert_ne!(a.proof(b"mac one"), b.proof(b"mac one"));
    }

    #[test]
    fn chunked_bodies_are_checked_frame_by_frame() {
        let a = Auth::new(TOKEN.into());
        let data: Vec<u8> = (0..100_000u32).map(|i| i as u8).collect();
        let fresh = |n: &str| {
            let sig = sign_body(TOKEN, now(), n, "POST", "/files?path=f", "chunked");
            let Check::Signed(s) = a.check(None, Some(&sig), "POST", "/files?path=f") else { panic!("refused") };
            (sig, s)
        };
        let read = |s: &Signed, body: &[u8], step: usize| -> Result<Vec<u8>, &'static str> {
            let mut r = a.chunk_reader(s);
            let mut out = Vec::new();
            for piece in body.chunks(step) {
                for c in r.push(piece)? {
                    out.extend_from_slice(&c);
                }
            }
            r.finish().map(|_| out)
        };
        let (sig, s) = fresh("bbbbbbbbbbbbbbbb1");
        let body = chunked(TOKEN, &sig, &data, 30_000);
        for step in [1, 7, 4096, body.len()] {
            assert_eq!(read(&s, &body, step).unwrap(), data, "received {step} bytes at a time");
        }
        // Cut short, extended, altered, or framed for another request: refused.
        assert!(read(&s, &body[..body.len() - 36], 4096).is_err());
        assert!(read(&s, &[&body[..], b"x"].concat(), 4096).is_err());
        let mut bad = body.clone();
        bad[10] ^= 1;
        assert!(read(&s, &bad, 4096).is_err());
        let (_, other) = fresh("bbbbbbbbbbbbbbbb2");
        assert!(read(&other, &body, 4096).is_err());
        let huge = ((MAX_CHUNK + 1) as u32).to_be_bytes();
        assert!(read(&s, &huge, 4).is_err());
    }
}
