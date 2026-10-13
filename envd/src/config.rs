//! Daemon settings, from the command line and the environment.

use std::ffi::OsString;
use std::path::PathBuf;

pub const DEFAULT_PORT: u16 = 49983; // E2B envd's port, so E2B tooling finds it where it expects
pub const TOKEN_ENV: &str = "HANGAR_ENVD_TOKEN";

#[derive(Debug, Clone)]
pub struct Config {
    pub listen: String,
    pub port: u16,
    /// Required on every request but /health. Never empty: the daemon refuses to start without one.
    pub token: String,
    /// The account the daemon runs as. It only ever runs processes and touches files as this user.
    pub user: String,
    pub uid: u32,
    pub home: PathBuf,
    /// Working directory for processes that don't name one.
    pub workdir: PathBuf,
    pub max_processes: usize,
    pub max_watchers: usize,
    pub daemon: bool,
}

pub const USAGE: &str = "usage: hangar-envd [--listen ADDR] [--port N] [--workdir DIR] [--max-processes N] \
[--max-watchers N] [--daemon] [--version]\n\
The access token is read from $HANGAR_ENVD_TOKEN (then removed from the environment) or --token-file.";

impl Config {
    /// Parses `args` (without argv[0]). `token_env` is the token from the environment, already taken out of it.
    pub fn parse(args: Vec<OsString>, token_env: Option<String>) -> Result<Config, String> {
        let mut listen = "0.0.0.0".to_string();
        let mut port = DEFAULT_PORT;
        let mut workdir: Option<PathBuf> = None;
        let mut max_processes = 512usize;
        let mut max_watchers = 64usize;
        let mut daemon = false;
        let mut token_file: Option<PathBuf> = None;
        let mut it = args.into_iter();
        while let Some(a) = it.next() {
            let a = a.into_string().map_err(|_| "arguments must be UTF-8".to_string())?;
            let mut val = |name: &str| -> Result<String, String> {
                it.next()
                    .and_then(|v| v.into_string().ok())
                    .ok_or_else(|| format!("{name} needs a value"))
            };
            match a.as_str() {
                "--listen" => listen = val("--listen")?,
                "--port" => port = val("--port")?.parse().map_err(|_| "--port must be a number".to_string())?,
                "--workdir" => workdir = Some(PathBuf::from(val("--workdir")?)),
                "--max-processes" => {
                    max_processes = val("--max-processes")?
                        .parse()
                        .map_err(|_| "--max-processes must be a number".to_string())?
                }
                "--max-watchers" => {
                    max_watchers = val("--max-watchers")?
                        .parse()
                        .map_err(|_| "--max-watchers must be a number".to_string())?
                }
                "--token-file" => token_file = Some(PathBuf::from(val("--token-file")?)),
                "--daemon" => daemon = true,
                "-h" | "--help" => return Err(USAGE.to_string()),
                other => return Err(format!("unknown argument {other}\n{USAGE}")),
            }
        }
        let token = match token_file {
            Some(p) => {
                let t = std::fs::read_to_string(&p).map_err(|e| format!("cannot read {}: {e}", p.display()))?;
                let _ = std::fs::remove_file(&p); // one use: the token shouldn't sit on disk
                t.trim().to_string()
            }
            None => token_env.unwrap_or_default(),
        };
        if token.len() < 16 {
            return Err(format!("an access token of at least 16 characters is required (${TOKEN_ENV} or --token-file)"));
        }
        let uid = nix::unistd::getuid();
        let pw = nix::unistd::User::from_uid(uid).ok().flatten();
        let user = pw.as_ref().map(|u| u.name.clone()).unwrap_or_else(|| uid.to_string());
        let home = pw
            .as_ref()
            .map(|u| u.dir.clone())
            .filter(|d| d.is_absolute())
            .or_else(|| std::env::var_os("HOME").map(PathBuf::from))
            .unwrap_or_else(|| PathBuf::from("/"));
        let workdir = match workdir {
            Some(w) => w,
            None => std::env::current_dir().unwrap_or_else(|_| home.clone()),
        };
        Ok(Config {
            listen,
            port,
            token,
            user,
            uid: uid.as_raw(),
            home,
            workdir,
            max_processes,
            max_watchers,
            daemon,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(a: &[&str]) -> Vec<OsString> {
        a.iter().map(OsString::from).collect()
    }

    #[test]
    fn refuses_to_start_without_a_token() {
        let err = Config::parse(args(&[]), None).unwrap_err();
        assert!(err.contains("access token"), "{err}");
        assert!(Config::parse(args(&[]), Some("short".into())).is_err());
    }

    #[test]
    fn parses_flags() {
        let c = Config::parse(
            args(&["--port", "1234", "--listen", "127.0.0.1", "--max-processes", "3", "--workdir", "/tmp"]),
            Some("0123456789abcdef".into()),
        )
        .unwrap();
        assert_eq!((c.port, c.listen.as_str(), c.max_processes), (1234, "127.0.0.1", 3));
        assert_eq!(c.workdir, PathBuf::from("/tmp"));
        assert!(Config::parse(args(&["--bogus"]), Some("0123456789abcdef".into())).is_err());
    }

    #[test]
    fn token_file_is_read_once() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("tok");
        std::fs::write(&p, "fedcba9876543210fedcba\n").unwrap();
        let c = Config::parse(args(&["--token-file", p.to_str().unwrap()]), None).unwrap();
        assert_eq!(c.token, "fedcba9876543210fedcba");
        assert!(!p.exists());
    }
}
