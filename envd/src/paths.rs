//! Path resolution shared by the RPC services and the file endpoints.
//!
//! Paths are resolved the way E2B envd does: `~` and relative paths are relative to the user's home, and the
//! result is cleaned lexically (Go's `filepath.Clean`), so `a/../b` means `b` regardless of symlinks. The daemon
//! runs as the sandbox user and enforces nothing beyond what the kernel lets that user do: there is no jail to
//! escape, only the requirement that every path means exactly one, predictable thing.

use std::path::{Component, Path, PathBuf};

use connectrpc::ConnectError;
use http::HeaderMap;

use crate::config::Config;

/// The absolute, cleaned path `raw` names for this daemon's user.
pub fn resolve(cfg: &Config, raw: &str) -> Result<PathBuf, ConnectError> {
    if raw.contains('\0') {
        return Err(ConnectError::invalid_argument("path contains a NUL byte"));
    }
    if raw.len() > 4096 {
        return Err(ConnectError::invalid_argument("path is longer than 4096 bytes"));
    }
    let joined = if raw.is_empty() || raw == "~" {
        cfg.home.clone()
    } else if let Some(rest) = raw.strip_prefix("~/") {
        cfg.home.join(rest)
    } else if raw.starts_with('/') {
        PathBuf::from(raw)
    } else {
        cfg.home.join(raw)
    };
    Ok(clean(&joined))
}

/// Lexical cleanup of an absolute path: drops `.`, resolves `..` against the previous component, never above `/`.
pub fn clean(p: &Path) -> PathBuf {
    let mut out = PathBuf::from("/");
    for c in p.components() {
        match c {
            Component::Normal(s) => out.push(s),
            Component::ParentDir => {
                out.pop();
            }
            Component::RootDir | Component::CurDir | Component::Prefix(_) => {}
        }
    }
    out
}

/// The user an E2B client asked for (`Authorization: Basic base64(user:)`), checked against the daemon's own: it
/// runs unprivileged, so it can act only as itself. No header means the daemon's user.
pub fn check_user_header(cfg: &Config, headers: &HeaderMap) -> Result<(), ConnectError> {
    let Some(v) = headers.get(http::header::AUTHORIZATION) else { return Ok(()) };
    let Some(b64) = v.to_str().ok().and_then(|s| s.strip_prefix("Basic ")) else { return Ok(()) };
    let user = decode_basic_user(b64.trim()).ok_or_else(|| ConnectError::invalid_argument("malformed Authorization header"))?;
    check_user(cfg, Some(&user))
}

pub fn check_user(cfg: &Config, user: Option<&str>) -> Result<(), ConnectError> {
    match user {
        None | Some("") => Ok(()),
        Some(u) if u == cfg.user || u == cfg.uid.to_string() => Ok(()),
        Some(u) => Err(ConnectError::permission_denied(format!(
            "this daemon runs as {} and cannot act as {u}",
            cfg.user
        ))),
    }
}

fn decode_basic_user(b64: &str) -> Option<String> {
    let raw = base64_decode(b64)?;
    let s = String::from_utf8(raw).ok()?;
    Some(s.split(':').next().unwrap_or_default().to_string())
}

/// Standard base64 (with or without padding); small enough not to need a crate.
pub fn base64_decode(s: &str) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(s.len() * 3 / 4);
    let (mut acc, mut bits) = (0u32, 0u32);
    for b in s.bytes() {
        let v = match b {
            b'A'..=b'Z' => b - b'A',
            b'a'..=b'z' => b - b'a' + 26,
            b'0'..=b'9' => b - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            b'=' => break,
            _ => return None,
        } as u32;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> Config {
        Config {
            listen: "127.0.0.1".into(),
            port: 0,
            token: "x".repeat(32),
            user: "agent".into(),
            uid: 1000,
            home: PathBuf::from("/home/agent"),
            workdir: PathBuf::from("/workspace"),
            max_processes: 4,
            max_watchers: 4,
            daemon: false,
        }
    }

    #[test]
    fn resolves_like_envd() {
        let c = cfg();
        assert_eq!(resolve(&c, "a.txt").unwrap(), PathBuf::from("/home/agent/a.txt"));
        assert_eq!(resolve(&c, "~/x/../y").unwrap(), PathBuf::from("/home/agent/y"));
        assert_eq!(resolve(&c, "~").unwrap(), PathBuf::from("/home/agent"));
        assert_eq!(resolve(&c, "/a/./b//c/").unwrap(), PathBuf::from("/a/b/c"));
        assert_eq!(resolve(&c, "/../../etc/passwd").unwrap(), PathBuf::from("/etc/passwd"));
        assert_eq!(resolve(&c, "../../../etc").unwrap(), PathBuf::from("/etc"));
        assert!(resolve(&c, "a\0b").is_err());
    }

    #[test]
    fn only_its_own_user() {
        let c = cfg();
        assert!(check_user(&c, None).is_ok());
        assert!(check_user(&c, Some("agent")).is_ok());
        assert!(check_user(&c, Some("1000")).is_ok());
        assert!(check_user(&c, Some("root")).is_err());
        let mut h = HeaderMap::new();
        h.insert(http::header::AUTHORIZATION, "Basic cm9vdDo=".parse().unwrap()); // root:
        assert!(check_user_header(&c, &h).is_err());
        h.insert(http::header::AUTHORIZATION, "Basic YWdlbnQ6".parse().unwrap()); // agent:
        assert!(check_user_header(&c, &h).is_ok());
    }

    #[test]
    fn base64() {
        assert_eq!(base64_decode("aGVsbG8=").unwrap(), b"hello");
        assert_eq!(base64_decode("aGVsbG8").unwrap(), b"hello");
        assert!(base64_decode("a*").is_none());
    }
}
