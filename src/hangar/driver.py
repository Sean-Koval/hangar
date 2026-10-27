"""Hangar's turn driver: one agent turn with the harness in its structured, steerable mode (workstream steer).

Hangar copies this file into the sandbox and the turn's script runs it under tmux, in place of the harness's
one-shot command, so attaching and resuming work as before. Standard library only, Python 3.8+.

* It starts the harness (Claude Code `-p --input-format stream-json`, or `codex app-server`), hands it the prompt
  and writes its stream to stdout, the turn's run log, in the format hangar's parsers already read: Claude Code's
  own `stream-json`, and Codex app-server notifications translated into `codex exec --json` lines. Its own events
  are `{"type": "hangar.*"}` lines in the same log (approval requests and answers, steers, interrupts).
* Hangar steers, interrupts and answers approvals through files. It moves each command into `<ctl>/in/` signed
  with the run's key (HMAC-SHA256; the driver reads the key file and deletes it before the harness starts) and
  reads the outcome from `<ctl>/acks`. Unsigned, replayed or malformed commands are refused. When the turn is
  over, `in/` is renamed away first and `<ctl>/done` written, so a command that comes too late fails to land
  instead of being lost; one that landed before is still carried out (a late steer continues the turn).
* Tool approvals follow `--approvals`: `auto` runs without prompts (today's behaviour), `ask` waits for hangar's
  answer and denies after `--approval-timeout` seconds, `deny-risky` answers at once from the rules in `risky()`.
* Pre-cleared calls (KAN-77, spec 0015 §5): a turn that resumes after Tower signed the call it was suspended on
  starts with `--preclear`: `<ctl>/preclear`, signed with the run's key, read and deleted at start, lists
  `(tool, args_hash, receipt)`. The first request whose tool and input hash match is allowed once, by "tower",
  with the receipt on its answer; anything else asks as usual.
* Taint (#130): once the turn has read untrusted content (`--tainted` when it starts that way, hangar's signed
  `taint` command, or a web tool call the driver sees in the stream), pushes, PR opening, fetches to a host and the
  environment's MCP write tools are risky too (`taint_risky()`): denied under `deny-risky`, flagged to the person
  under `ask`. Taint can only be set: no command clears it.
* With `--as` (the uid split, hangar/isolation.py) the driver runs as hangar's own user and starts the harness as the
  agent's, with no capabilities and no_new_privs: the agent can't read the key, change the driver's files, or signal
  or trace it. Approval requests and answers also go to `<ctl>/approvals`, which only hangar's user can write: hangar
  reads a split turn's approvals from there, not from the log the harness writes into (--- harden). `--base-env`
  replaces the agent's ~/.hangar/env with hangar's own copy (hangar/harden.py).
* With `--relay` (the driver sidecar on Kubernetes, KAN-83; a prototype) the driver runs in a container of its own and
  the harness in the agent's, started by relay.py, which connects back on the pod's loopback with the run's token
  (`Relay`, `RemoteProc`; docs/security/driver-sidecar.md). The verify gate doesn't run there yet.

* Trees (#165): at the turn's start and end, and after each edit or shell tool result, the driver writes the working
  tree's git tree id as a `hangar.tree` line (only when it changed since the last one): `git add -A` and
  `git write-tree` into a scratch index and object store in its private `<ctl>/tree/`, under a git directory of its
  own (the agent's `.git/config`, hooks, filters and fsmonitor never run; its objects are read as alternates). A hash
  over TREE_SLOW seconds stops hashing for the turn (`skipped: "slow"`).

Exit status: the harness's own, 1 when its last turn failed, 130 after an interrupt that brought no new message.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import os
import pwd
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

POLL = 0.1  # seconds between looks at the inbox while the harness is quiet
MAX_COMMAND = 256 * 1024
MAX_TEXT = 64_000
DETAIL = 2000  # how much of a tool's input an approval request shows
OPS = ("steer", "interrupt", "approve", "taint")  # --- taint (#130): set only; nothing clears it
ENDED = "the turn has ended"  # a command's answer when the harness is gone: hangar takes it as not delivered
DECISIONS = ("allow", "deny", "allow_always")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TREE_SLOW = 2.0  # --- #165: seconds; one hash slower than this stops hashing for the turn
TREE_TIMEOUT = 30.0  # a git that hangs is killed
TREE_TOOLS = {"Bash", "BashOutput", "Write", "Edit", "MultiEdit", "NotebookEdit"}  # Claude Code's edit and shell tools
_SHA = re.compile(r"^[0-9a-f]{40,64}$")


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign(key: bytes, cmd: dict) -> str:
    """The MAC of a command: HMAC-SHA256 over its canonical JSON without the `mac` field (hangar signs the same)."""
    return hmac.new(key, canonical({k: v for k, v in cmd.items() if k != "mac"}), hashlib.sha256).hexdigest()


def args_hash(data: object) -> str:
    """--- KAN-77: lowercase hex SHA-256 of the tool's input as canonical JSON (spec 0015 §10; governance.args_hash)."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def load_preclear(ctl: str, key: bytes) -> list:
    """--- KAN-77: `<ctl>/preclear`, deleted once read. Only a file signed with the run's key counts (the agent can
    write into ~/.hangar/runs without the uid split); anything else clears nothing."""
    path = os.path.join(ctl, "preclear")
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_COMMAND + 1)
    except OSError:
        return []
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    try:
        doc = json.loads(raw)
    except ValueError:
        return []
    mac = doc.get("mac") if isinstance(doc, dict) else None
    if not key or not isinstance(mac, str) or not hmac.compare_digest(mac, sign(key, doc)):
        sys.stderr.write("hangar: the pre-cleared calls are not signed by hangar; ignoring them\n")
        return []
    return [c for c in doc.get("calls") or [] if isinstance(c, dict) and isinstance(c.get("tool"), str)
            and isinstance(c.get("args_hash"), str)]


def clip(s: object, n: int) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def shown(s: object) -> str:
    """What a person approving a tool call is shown: all of it, or both ends of a long one with the cut marked,
    so padding can't push the dangerous part out of sight."""
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    if len(s) <= DETAIL:
        return s
    head, tail = DETAIL * 3 // 5, DETAIL // 4
    return f"{s[:head]} […{len(s) - head - tail} characters not shown…] {s[-tail:]}"


# ====================================================================== the run log
class Out:
    """stdout is the run log (the turn's script tees it): harness lines as they come, and hangar.* events."""

    def line(self, raw: str) -> None:
        sys.stdout.write(raw.rstrip("\r\n") + "\n")
        sys.stdout.flush()

    def event(self, kind: str, **data: object) -> str:
        line = json.dumps({"type": "hangar." + kind, "ts": round(time.time(), 3), **data}, ensure_ascii=False)
        self.line(line)
        return line


# ====================================================================== trees (--- #165)
class Trees:
    """The working tree's git tree id at points of the turn, as `hangar.tree` lines in the run log.

    It never touches the agent's repository: a git directory of its own (`<ctl>/tree/git`, made here, with no
    config but what it sets), the checkout as its work tree, a scratch index seeded once from the repo's (so `add -A`
    rehashes only what changed), new objects in its own store, the repo's objects read as alternates. So the agent's
    `.git/config` (filters, fsmonitor, hooks) runs nothing as hangar's user under the uid split, and nothing is
    written into its `.git`. `.gitignore` files in the work tree are honoured. Ids match `git write-tree` of the same
    content in the repo itself (barring content-changing config there: autocrlf, filters)."""

    def __init__(self, out: Out, cwd: str, private_dir: str | None):
        self.out, self.cwd, self.dir = out, cwd, private_dir
        self.repo: str | None = None
        self.objects: str | None = None
        self.last: str | None = None
        self.off: str | None = None  # why hashing stopped: "slow", "error", "no repo"
        self.ready = False

    def _git(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run(["git", "-c", "safe.directory=*", "-c", "core.fsmonitor=false",
                               "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                              timeout=TREE_TIMEOUT, env={**base, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
                                                         "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                                         **(env or {})})

    def _setup(self) -> bool:
        self.ready = True
        if not self.dir or shutil.which("git") is None:
            self.off = "no git"
            return False
        try:
            top = self._git("-C", self.cwd, "rev-parse", "--show-toplevel", "--git-common-dir")
            lines = top.stdout.splitlines()
            if top.returncode != 0 or len(lines) < 2:
                self.off = "no repo"
                return False
            self.repo = lines[0]
            common = lines[1] if os.path.isabs(lines[1]) else os.path.join(self.cwd, lines[1])
            self.objects = os.path.join(os.path.realpath(common), "objects")
            git = os.path.join(self.dir, "git")
            os.makedirs(os.path.join(git, "objects"), mode=0o700, exist_ok=True)
            if not os.path.exists(os.path.join(git, "HEAD")):
                r = self._git("init", "-q", env={"GIT_DIR": git})
                if r.returncode != 0:
                    self.off = "error"
                    return False
            index = os.path.join(self.dir, "index")
            idx = os.path.join(os.path.realpath(common), "index")
            if os.path.isfile(idx) and not os.path.exists(index):
                with contextlib.suppress(OSError):
                    shutil.copyfile(idx, index)
        except (OSError, subprocess.SubprocessError):
            self.off = "error"
            return False
        return True

    def hash(self) -> tuple:
        """(tree id or None, milliseconds)."""
        if not self.ready and not self._setup():
            return None, 0
        if self.off:
            return None, 0
        assert self.dir and self.repo and self.objects
        env = {"GIT_DIR": os.path.join(self.dir, "git"), "GIT_WORK_TREE": self.repo,
               "GIT_INDEX_FILE": os.path.join(self.dir, "index"), "GIT_OBJECT_DIRECTORY":
               os.path.join(self.dir, "git", "objects"), "GIT_ALTERNATE_OBJECT_DIRECTORIES": self.objects}
        t0 = time.monotonic()
        try:
            add = self._git("-C", self.repo, "add", "-A", env=env)
            tree = self._git("-C", self.repo, "write-tree", env=env) if add.returncode == 0 else add
        except (OSError, subprocess.SubprocessError):
            return None, int((time.monotonic() - t0) * 1000)
        ms = int((time.monotonic() - t0) * 1000)
        sha = tree.stdout.strip()
        return (sha if tree.returncode == 0 and _SHA.match(sha) else None), ms

    def point(self, phase: str | None = None, tool_use_id: object = None) -> None:
        """A tree after a tool result (`tool_use_id`; only when it changed), or at the turn's `start` or `end`."""
        if self.off:
            return
        sha, ms = self.hash()
        if self.off:
            if phase == "start" and self.off != "no repo":
                self.out.event("tree", phase=phase, skipped=self.off)
            return
        if sha is None:
            return
        if ms > TREE_SLOW * 1000:
            self.off = "slow"
            self.out.event("tree", **({"phase": phase} if phase else {"tool_use_id": tool_use_id}), skipped="slow",
                           ms=ms)
            return
        if phase is None and sha == self.last:
            return
        self.last = sha
        self.out.event("tree", **({"phase": phase} if phase else {"tool_use_id": tool_use_id}), tree=sha, ms=ms)

    def close(self) -> None:
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)


# ====================================================================== the agent's user (--- isolation)
# With the uid split (hangar/isolation.py) the driver runs as hangar's own user and starts the harness as the agent's.
# The driver holds CAP_SETUID, CAP_SETGID and CAP_KILL in its ambient set for that; setpriv drops all of them for the
# harness (a uid change between two non-root users keeps capabilities otherwise), and sets no_new_privs, so neither the
# harness nor anything it starts gains privileges through a setuid program (sudo, su, one planted in the image).
# The harness's environment is built as the agent, in the order a single-user run has: its ~/.hangar/env, then the
# run's own exports. --- harden: with hangar's copy of that env on a pipe (HANGAR_ENV_FD), it comes from there instead,
# read with builtins only, before the agent's PATH applies: nothing the agent planted runs before the harness does.
LAUNCH = ('if [ -n "${HANGAR_ENV_FD:-}" ]; then __hangar_env=; IFS= read -r -d "" -u "$HANGAR_ENV_FD" __hangar_env; '
          'unset HANGAR_ENV_FD; fi; PATH="${HANGAR_AGENT_PATH:-$PATH}"; set -a; '
          'if [ -n "${__hangar_env+x}" ]; then eval "$__hangar_env"; '
          'elif [ -f "$HOME/.hangar/env" ]; then . "$HOME/.hangar/env"; fi; . "$1"; '
          'set +a; unset HANGAR_AGENT_PATH __hangar_env; cd -- "$2" || exit 1; shift 2; exec "$@"')


def agent_launch(user: str, home: str, run_env: str, cwd: str, argv: list) -> list:
    """`argv` in `user`'s environment (home `home`), from `cwd`: what runs once the process is the agent's."""
    return ["env", f"HOME={home}", f"USER={user}", f"LOGNAME={user}", "bash", "-c", LAUNCH, "hangar-agent", run_env,
            cwd, *argv]


def as_agent(user: str, home: str, run_env: str, cwd: str, argv: list) -> list:
    """`argv` run as `user` (home `home`), with no capabilities, from `cwd`, in that user's environment. Also used,
    joined, for the one-shot command in the turn's script."""
    return ["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", "--inh-caps=-all", "--ambient-caps=-all",
            "--no-new-privs", "--", *agent_launch(user, home, run_env, cwd, argv)]


BASE_ENV: bytes | None = None  # --- harden: --base-env, read once at start and deleted (like the key)


@contextlib.contextmanager
def agent_env():
    """Keyword arguments for starting a process as the agent: with --base-env, hangar's copy of its environment on a
    pipe the process inherits (HANGAR_ENV_FD, read by LAUNCH); {} without, and LAUNCH reads ~/.hangar/env."""
    if BASE_ENV is None:
        yield {}
        return
    r, w = os.pipe()

    def feed(data: bytes) -> None:
        try:
            while data:
                data = data[os.write(w, data):]
        except OSError:
            pass  # the process went away without reading it
        finally:
            os.close(w)

    threading.Thread(target=feed, args=(BASE_ENV,), daemon=True).start()
    try:
        yield {"pass_fds": (r,), "env": dict(os.environ, HANGAR_ENV_FD=str(r))}
    finally:
        os.close(r)


# ====================================================================== the control channel
PR_SET_DUMPABLE = 4


def private() -> str | None:
    """Close this process to the agent, which runs as the same uid without the uid split: no ptrace and no
    /proc/<pid>/mem or environ (Docker leaves same-uid ptrace to the kernel's Yama setting, which is often 0 or
    absent), so the run key can't be read out of memory. The harness gets the normal setting back when it execs.
    Returns why it failed."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            return os.strerror(ctypes.get_errno())
    except (ImportError, OSError, AttributeError) as e:
        return str(e) or type(e).__name__
    return None


class Control:
    """Commands from hangar: `<ctl>/in/*.json`, each signed; outcomes appended to `<ctl>/acks` as JSON lines."""

    def __init__(self, ctl: str):
        self.dir = ctl
        self.inbox = os.path.join(ctl, "in")
        self.acks = os.path.join(ctl, "acks")
        self.seen: set = set()
        self.closings = 0
        path = os.path.join(ctl, "key")
        try:
            with open(path, "rb") as f:
                self.key = f.read().strip()
            os.unlink(path)  # in memory only from here on
        except OSError:
            self.key = b""  # no key: every command is refused (deny by default)
        os.makedirs(self.inbox, mode=0o700, exist_ok=True)

    def commands(self) -> list:
        return self._drain(self.inbox)

    def _drain(self, d: str) -> list:
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        except OSError:
            return []
        out = []
        for name in names:
            path = os.path.join(d, name)
            try:
                with open(path, "rb") as f:
                    raw = f.read(MAX_COMMAND + 1)
            except OSError:
                continue
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            cmd, err = self._check(raw)
            if err:
                ref = cmd.get("id") if isinstance(cmd, dict) and isinstance(cmd.get("id"), str) else name[:-5]
                self.ack(ref if _ID.match(ref) else "invalid", False, error=err)
                continue
            out.append(cmd)
        return out

    def _check(self, raw: bytes):
        if len(raw) > MAX_COMMAND:
            return {}, "command too large"
        try:
            cmd = json.loads(raw)
        except ValueError:
            return {}, "not JSON"
        if not isinstance(cmd, dict) or not isinstance(cmd.get("id"), str) or not _ID.match(cmd["id"]):
            return {}, "malformed command"
        mac = cmd.get("mac")
        if not self.key or not isinstance(mac, str) or not hmac.compare_digest(mac, sign(self.key, cmd)):
            return cmd, "bad signature"
        if cmd["id"] in self.seen:
            return {}, "replayed command"
        self.seen.add(cmd["id"])
        if cmd.get("op") not in OPS or not isinstance(cmd.get("args"), dict):
            return cmd, "unknown command"
        return cmd, None

    def ack(self, cid: str, ok: bool, **info: object) -> None:
        """Signed like the commands: the agent can write to `acks` too, and must not answer for the driver."""
        rec: dict = {"id": cid, "ok": ok, "ts": round(time.time(), 3), **info}
        if self.key:
            rec["mac"] = sign(self.key, rec)
        line = json.dumps(rec, ensure_ascii=False)
        with open(self.acks, "a") as f:
            f.write(line + "\n")

    def close(self) -> list:
        """Stop taking commands. Returns the ones that landed before; they still count."""
        self.closings += 1
        dest = f"{self.inbox}.closed.{self.closings}"
        try:
            os.rename(self.inbox, dest)
        except OSError:
            return []
        try:
            return self._drain(dest)
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def record(self, line: str) -> None:
        """--- harden: an approval request or answer, also into `<ctl>/approvals`. Under the uid split only hangar's
        user can write there, so it is what hangar takes a split turn's approvals from; the run log is the harness's
        output too, and the agent can write into that (its stdout and stderr are the agent's)."""
        try:
            with open(os.path.join(self.dir, "approvals"), "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def reopen(self) -> None:
        os.makedirs(self.inbox, mode=0o700, exist_ok=True)

    def finish(self) -> None:
        """For good: a command written from now on fails at once ("the turn has ended")."""
        with open(os.path.join(self.dir, "done"), "w") as f:
            f.write(f"{os.getpid()}\n")


# ====================================================================== policy
# deny-risky: what is refused without asking anyone. A guard rail against an agent's mistakes, not a sandbox.
_SECRETS = re.compile(r"\.ssh/|\.git-credentials|\.hangar/|\.config/hangar|/proc/[^/\s]+/(environ|mem)\b")
_RISKY_SHELL = [
    (re.compile(r"\bgit\s+push\b[^|;&]*(\s-f\b|--force|--delete|\s:\S|\s\+\S)"),
     "force-push or remote branch deletion"),
    (re.compile(r"(^|[;&|('\"`]\s*|\s)(sudo|su|doas)\s"), "privilege escalation"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"), "piping a download into a shell"),
    (re.compile(r"\b(mkfs(\.\w+)?|shutdown|reboot|halt|poweroff)\b|\bdd\b[^|;&]*\bof=/dev/"), "system-level change"),
    (_SECRETS, "credentials or hangar's own files"),
]
_FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"}
_PATH_KEYS = ("file_path", "notebook_path", "path")  # where tools name the file they read or write
_SEGMENT = re.compile(r"&&|\|\||[;&|\n()`]")


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _io_free(command: str) -> str:
    """The command with the turn's own $HANGAR_IO (where it writes RESULT.md and reads attachments, under
    ~/.hangar) taken out, so the rule for hangar's private files leaves it alone. A path climbing out stays."""
    io = os.environ.get("HANGAR_IO") or ""
    if not io.startswith("/"):
        return command
    home = os.path.expanduser("~")
    forms = [r"\$\{?HANGAR_IO\}?", re.escape(io)]
    if _inside(io, home) and io != home:
        forms += [re.escape("~" + io[len(home):]), r"\$\{?HOME\}?" + re.escape(io[len(home):])]
    return re.sub(rf"(?:{'|'.join(forms)})(?!/\.\.)(?=/|\s|$|[\"'])", "HANGAR_IO", command)


def _deletes_outside(command: str, root: str, depth: int = 0) -> bool:
    """A recursive `rm` of the working tree itself or of anything outside it (`bash -c '...'` looked into). It
    resolves paths against the turn's directory, so `cd elsewhere && rm -r x` slips by: a guard rail."""
    for seg in _SEGMENT.split(command):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        for i, w in enumerate(words):
            if depth < 3 and i and re.fullmatch(r"-[a-z]*c", words[i - 1]) and " " in w:
                if _deletes_outside(w, root, depth + 1):
                    return True
            if os.path.basename(w) != "rm":
                continue
            args = words[i + 1:]
            flags = [a for a in args if a.startswith("-") and a != "-"]
            if not any(a == "--recursive" or (not a.startswith("--") and re.search("[rR]", a)) for a in flags):
                continue
            for target in (a for a in args if not a.startswith("-")):
                full = os.path.realpath(os.path.join(root, os.path.expandvars(os.path.expanduser(target))))
                if full == root or not _inside(full, root):
                    return True
    return False


def risky(tool: str, data: dict, cwd: str) -> str | None:
    """Why a tool call is too risky to allow without a person, or None."""
    root = os.path.realpath(cwd)
    io = os.environ.get("HANGAR_IO") or ""
    io = os.path.realpath(io) if io.startswith("/") else ""
    command = data.get("command")
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)
    if isinstance(command, str):
        for pattern, why in _RISKY_SHELL:
            if pattern.search(_io_free(command) if pattern is _SECRETS else command):
                return why
        if _deletes_outside(command, root):
            return "recursive delete outside the working tree"
    paths = data.get("paths") if isinstance(data.get("paths"), list) else [data.get(k) for k in _PATH_KEYS]
    if tool == "Glob":
        paths = [*paths, data.get("pattern")]
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        full = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
        if io and _inside(full, io):
            continue  # the turn's own $HANGAR_IO
        if _SECRETS.search(full + "/"):
            return f"credentials or hangar's own files ({path})"
        if tool in _FILE_TOOLS and not _inside(full, root):
            return f"writes outside the working tree ({path})"
    return None


# --- #292: hangar's own hand-back commands are not the agent's tool use. Under approvals=ask, a turn's closing
# `hangar-agent done "..."` waited on a person like any other command. These subcommands only write files under the
# turn's $HANGAR_IO (exchange.HELPER); `artifact`, `result @FILE`, `env`, `svc` and the computer commands read or
# run other things and still ask.
_OWN_SUBCOMMANDS = ("progress", "ask", "done", "result")
_SHELLS = ("bash", "sh", "zsh", "dash")


def _one_simple_command(command: str) -> bool:
    """The command is one simple command: no `;`, `&&`, `|`, `&`, redirection, subshell, newline, backquote or
    `$(...)` / `${...}` outside single quotes, and every quote closed. Plain words, quoted strings, `$VAR`."""
    quote, i, n = "", 0, len(command)
    while i < n:
        c = command[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            if i + 1 < n and command[i + 1] == "\n":
                return False  # a line continuation: keep it simple
            i += 1  # the next character is taken literally, inside double quotes or out
        elif c == "`" or (c == "$" and command[i + 1:i + 2] in ("(", "{")):
            return False  # command or parameter substitution, which runs (or assigns) inside double quotes too
        elif quote == '"':
            quote = "" if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c in ";&|<>()\n\r":
            return False
        i += 1
    return quote == "" and not command.endswith("\\")


def own_command(tool: str, data: dict, depth: int = 0) -> bool:
    """A shell call that is exactly one of hangar's own hand-back commands (`hangar-agent done "..."`,
    `progress`, `ask`, `result '<json>'`), which a person need not approve: it only writes the turn's files under
    $HANGAR_IO. Anything chained, redirected or substituted (`hangar-agent done; rm -rf ~`) is not."""
    if tool not in ("Bash", "shell"):
        return False
    argv = data.get("argv")
    if depth == 0 and isinstance(argv, list) and len(argv) == 3 and all(isinstance(a, str) for a in argv) \
            and os.path.basename(argv[0]) in _SHELLS and re.fullmatch(r"-l?c", argv[1]):
        return own_command(tool, {"command": argv[2]}, depth + 1)  # codex: ["bash", "-lc", "<script>"]
    command = data.get("command")
    if not isinstance(command, str) or not _one_simple_command(command.strip()):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 2 or words[0] != "hangar-agent" or words[1] not in _OWN_SUBCOMMANDS:
        return False
    return not (words[1] == "result" and len(words) > 2 and words[2].startswith("@"))  # @FILE reads a file


# --- taint (#130): what needs a person once the turn has read untrusted content (the plan's sinks): a push, a PR, a
# fetch to a host (data out, or a payload in), and MCP tools the environment marks as writes
_TAINT_WEB = {"WebFetch", "WebSearch"}  # tools whose use taints the turn, seen in the harness's own stream
_LOCAL_HOST = re.compile(r"^(localhost|127(\.\d+){3}|0\.0\.0\.0|\[?::1\]?)$", re.I)


def _words(seg: str) -> list:
    try:
        words = shlex.split(seg)
    except ValueError:  # a quote opened in another segment (`bash -lc 'a && git push'`): words without quotes
        words = [w.strip("'\"") for w in seg.split()]
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]
    while words and os.path.basename(words[0]) in ("sudo", "env", "timeout", "time", "nice", "command", "exec"):
        words = words[1:]
        while words and (words[0].startswith("-") or re.fullmatch(r"\d+[smh]?|[A-Za-z_]\w*=.*", words[0])):
            words = words[1:]
    return words


def _host_arg(words: list) -> str | None:
    """A host a curl, wget or nc command names (not the sandbox itself)."""
    for w in words[1:]:
        if w.startswith("-"):
            continue
        m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^/@\s]*@)?(\[[^\]]+\]|[^/:?#\s]+)", w, re.I)
        host = m.group(1) if m else (w.split("/", 1)[0].split(":", 1)[0] if re.match(
            r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?(/\S*)?$", w) else None)
        if host and not _LOCAL_HOST.match(host):
            return host.lower()
    return None


def taint_sink(tool: str, data: dict, write_tools: list | tuple = (), depth: int = 0) -> str | None:
    """What a tool call sends out that needs a person after an untrusted read, or None: `git push`, `gh pr create`,
    `curl`/`wget`/`nc` to a host, an MCP write tool (`write_tools`: fnmatch patterns of tool names)."""
    import fnmatch

    if any(fnmatch.fnmatchcase(tool, p) for p in write_tools or ()):
        return f"the MCP write tool {tool}"
    command = data.get("command")
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)
    if not isinstance(command, str):
        return None
    for seg in _SEGMENT.split(command):
        words = _words(seg.strip())
        if not words:
            continue
        head = os.path.basename(words[0])
        flag = next((i for i, w in enumerate(words[1:3], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", w)), None)
        if depth < 3 and head in ("bash", "sh", "zsh", "dash") and flag is not None and len(words) > flag + 1:
            if (why := taint_sink(tool, {"command": words[flag + 1]}, write_tools, depth + 1)) is not None:
                return why  # `bash -lc '...'` (how Codex runs every command) looked into
            continue
        args = [w for w in words[1:] if not w.startswith("-")]
        if head == "git":
            sub = next((w for i, w in enumerate(words[1:], 1) if not w.startswith("-")
                        and words[i - 1] not in ("-c", "-C")), None)
            if sub == "push":
                return "git push"
        if head == "gh" and args[:2] == ["pr", "create"]:
            return "gh pr create"
        if head in ("curl", "wget", "nc", "ncat", "netcat") and (host := _host_arg(words)):
            return f"{head} to {host}"
    return None


# ====================================================================== approvals and commands
class Approval:
    def __init__(self, aid: str, key: object, tool: str, answer, expires: float):
        self.id, self.key, self.tool, self.answer, self.expires = aid, key, tool, answer, expires


class Driver:
    """Approvals, and hangar's commands routed to the harness adapter."""

    def __init__(self, args: argparse.Namespace, out: Out, ctl: Control):
        self.args, self.out, self.ctl = args, out, ctl
        self.pending: dict = {}
        self.harness: Harness | None = None
        self.relay: Relay | None = None  # --- KAN-83: the driver sidecar
        self.unanswered: set = set()  # commands taken whose outcome isn't known yet
        self.tainted = bool(getattr(args, "tainted", False))  # --- taint (#130): only ever set
        self.write_tools = list(getattr(args, "write_tool", None) or [])
        self.preclear: list = []  # --- KAN-77: calls Tower signed, each allowed once
        ctl_dir = getattr(args, "ctl", None)
        self.trees = Trees(out, self.cwd(), os.path.join(ctl_dir, "tree") if ctl_dir else None)  # --- #165

    def taint(self, ref: str) -> None:
        """--- taint (#130): the turn has read untrusted content. Never undone."""
        if not self.tainted:
            self.tainted = True
            self.out.event("taint", ref=clip(ref, 200))

    def cwd(self) -> str:
        """The turn's working directory: --cwd under the uid split, where the driver doesn't run in it."""
        return getattr(self.args, "cwd", None) or os.getcwd()

    def ended(self) -> None:
        """The harness is gone: what it never took is answered so (hangar then starts the next turn with it)."""
        for cid in sorted(self.unanswered):
            self.ctl.ack(cid, False, error=ENDED)
        self.unanswered.clear()

    # --- approvals
    def request(self, key: object, tool: str, data: dict, answer) -> None:
        """The harness asks to use a tool; `answer(decision, message)` replies to it (decision in DECISIONS)."""
        aid = "ap-" + uuid.uuid4().hex[:12]
        policy = self.args.approvals
        what = data.get("command") if isinstance(data.get("command"), str) else data
        expires = time.time() + self.args.approval_timeout
        # --- taint (#130): after an untrusted read, a push, a PR, a fetch to a host and MCP writes need a person
        sink = taint_sink(tool, data, self.write_tools) if self.tainted and policy != "auto" else None
        gate = {"taint": f"{sink}, after reading untrusted content"} if sink else {}
        digest = args_hash(data)  # --- KAN-77: what Tower's receipt and the pre-clear match on
        self._approval("approval_request", id=aid, tool=tool, detail=shown(what), policy=policy,
                       expires_at=round(expires), args_hash=digest, **gate)
        a = Approval(aid, key, tool, answer, expires)
        if policy != "auto" and own_command(tool, data):  # --- #292: hangar's own hand-back, not the agent's
            return self._answer(a, "allow", "", "policy")
        if policy != "auto":  # --- KAN-77: the call Tower signed while the session was suspended, once
            hit = next((c for c in self.preclear if c["tool"] == tool and c["args_hash"] == digest), None)
            if hit is not None:
                self.preclear.remove(hit)
                return self._answer(a, "allow", "", "tower", receipt=str(hit.get("receipt") or ""),
                                    approval=str(hit.get("approval") or ""))
        if policy == "ask":
            self.pending[aid] = a
            return
        why = risky(tool, data, self.cwd()) if policy == "deny-risky" else None
        if why is None and sink:
            why = f"{sink} after reading untrusted content (a person must allow it: approvals=ask)"
        self._answer(a, "deny" if why else "allow", ("Denied by hangar's deny-risky policy: " + why) if why else "",
                     "policy")

    def decide(self, aid: object, decision: str, message: str, by: str) -> str | None:
        a = self.pending.pop(aid, None) if isinstance(aid, str) else None
        if a is None:
            return f"no pending approval {aid}"
        self._answer(a, decision, message, by)
        return None

    def withdrawn(self, key: object) -> None:
        """The harness took a request back (an interrupt cancels its prompts)."""
        for a in [a for a in self.pending.values() if a.key == key]:
            del self.pending[a.id]
            self._approval("approval_resolved", id=a.id, decision="cancelled", by="harness")

    def expire(self) -> None:
        t = time.time()
        for a in [a for a in self.pending.values() if a.expires <= t]:
            self.decide(a.id, "deny", f"No answer within {self.args.approval_timeout} seconds; denied by hangar.",
                        "timeout")

    def drop_all(self) -> None:
        for aid in list(self.pending):
            del self.pending[aid]
            self._approval("approval_resolved", id=aid, decision="cancelled", by="harness")

    def _answer(self, a: Approval, decision: str, message: str, by: str, **extra: str) -> None:
        a.answer(decision, message or ("Denied by hangar." if decision == "deny" else ""))
        self._approval("approval_resolved", id=a.id, decision=decision, by=by, message=clip(message or "", 500),
                       **{k: clip(v, 200) for k, v in extra.items() if v})

    def _approval(self, kind: str, **data: object) -> None:
        self.ctl.record(self.out.event(kind, **data))  # --- harden: the log, and hangar's own copy

    # --- commands
    def handle(self, cmd: dict) -> None:
        cid, op, args = cmd["id"], cmd["op"], cmd["args"]
        h = self.harness
        assert h is not None
        self.unanswered.add(cid)

        def done(err: str | None = None) -> None:
            if cid not in self.unanswered:
                return
            self.unanswered.discard(cid)
            if err:
                self.ctl.ack(cid, False, error=err)
            else:
                self.ctl.ack(cid, True)

        if op == "taint":  # --- taint (#130): signed like every command, so the agent can't set or clear it
            self.taint(str(args.get("ref") or "hangar"))
            return done()
        if op == "approve":
            decision = args.get("decision")
            message = args.get("message") or ""
            if decision not in DECISIONS or not isinstance(message, str):
                return done("decision must be one of " + ", ".join(DECISIONS))
            by = clip(args.get("by") or "hangar", 100)
            return done(self.decide(args.get("request"), decision, message[:MAX_TEXT], by))
        text = args.get("text")
        if op == "steer" and not (isinstance(text, str) and text.strip()):
            return done("the message is empty")
        if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT):
            return done(f"the message must be a string of at most {MAX_TEXT} characters")
        if op == "steer":
            def steered(err: str | None = None) -> None:
                if err is None:
                    self.out.event("steer", id=cid, text=clip(text, 500))
                done(err)

            return h.steer(text, steered)

        def interrupted(err: str | None = None) -> None:
            if err is None:
                self.out.event("interrupt", id=cid, text=clip(text or "", 500))
            done(err)

        h.interrupt(text or None, interrupted)


# ====================================================================== the driver sidecar (--- KAN-83)
# On Kubernetes the agent's container can't hold a second user, so the driver runs in a container of its own
# (docs/security/driver-sidecar.md): its own uid, its own PID namespace, its control files on a volume the agent's
# container doesn't mount. It can't start the harness there itself. Hangar starts relay.py in the agent's container,
# as the agent, with a one-run token on its stdin; the relay connects back on the pod's loopback, twice (`io`, the
# harness's stdin and stdout; `ctl`, its stderr, exit status and signals), and starts what the driver sends it.
RELAY_HELLO = "hangar-relay"
RELAY_WAIT = 60.0  # seconds the driver waits for the relay to connect
RELAY_LOST = 1  # the harness's exit status when its relay went away without reporting one


class RelayError(RuntimeError):
    pass


class _SockWriter:
    """The harness's stdin, as Harness uses a pipe's: closing it is end of input (a half-close)."""

    def __init__(self, sock: socket.socket):
        self.sock = sock

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_WR)


class _SockReader:
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def fileno(self) -> int:
        return self.sock.fileno()


class RemoteProc:
    """The harness behind its relay, with the parts of Popen that Harness uses."""

    def __init__(self, io: socket.socket, ctl: socket.socket):
        self.io, self.ctl = io, ctl
        self.stdin, self.stdout = _SockWriter(io), _SockReader(io)
        self.returncode: int | None = None
        self.buf = b""

    def _pump(self, timeout: float) -> None:
        """Read what the relay said within `timeout`, then whatever else is already there."""
        while self.returncode is None:
            ready, _, _ = select.select([self.ctl], [], [], timeout)
            if not ready:
                return
            timeout = 0
            try:
                data = self.ctl.recv(1 << 16)
            except OSError:
                data = b""
            if not data:
                sys.stderr.write("hangar: the harness's relay went away without its exit status\n")
                self.returncode = RELAY_LOST
                return
            self.buf += data
            while b"\n" in self.buf and self.returncode is None:
                line, self.buf = self.buf.split(b"\n", 1)
                word, _, rest = line.partition(b" ")
                if word == b"err":
                    with contextlib.suppress(ValueError, OSError):
                        sys.stderr.buffer.write(base64.b64decode(rest))
                        sys.stderr.flush()
                elif word == b"exit":
                    try:
                        self.returncode = int(rest)
                    except ValueError:
                        self.returncode = RELAY_LOST

    def poll(self) -> int | None:
        self._pump(0)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        end = None if timeout is None else time.time() + timeout
        while self.returncode is None:
            left = 1.0 if end is None else end - time.time()
            if left <= 0:
                raise subprocess.TimeoutExpired("harness", timeout or 0)
            self._pump(min(left, 1.0))
        return self.returncode

    def _say(self, word: str) -> None:
        with contextlib.suppress(OSError):
            self.ctl.sendall(word.encode() + b"\n")

    def terminate(self) -> None:
        self._say("term")

    def kill(self) -> None:
        self._say("kill")


class Relay:
    """The driver's end: a listener on 127.0.0.1, its port in a file the relay reads. The first `io` and `ctl`
    connections that say the run's token are the harness's; then the listener closes. Anything else that connects
    (the agent can reach the pod's loopback) is dropped: without the token it can't pass as the harness."""

    def __init__(self, portfile: str, token: bytes):
        self.portfile, self.token = portfile, token
        self.lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(8)
        tmp = f"{portfile}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write(f"{self.lsock.getsockname()[1]}\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, portfile)

    def _hello(self, conn: socket.socket) -> str | None:
        conn.settimeout(5)
        line = b""
        try:
            while b"\n" not in line and len(line) < 256:
                chunk = conn.recv(256 - len(line))
                if not chunk:
                    return None
                line += chunk
        except OSError:
            return None
        words = line.split(b"\n", 1)[0].decode(errors="replace").split()
        if len(words) != 3 or words[0] != RELAY_HELLO or words[1] not in ("io", "ctl"):
            return None
        if not hmac.compare_digest(words[2].encode(), self.token):
            return None
        conn.settimeout(None)
        return words[1]

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.lsock.close()
        with contextlib.suppress(OSError):
            os.unlink(self.portfile)

    def start(self, argv: list, base_env: bytes | None, wait: float = RELAY_WAIT) -> RemoteProc:
        """Wait for the relay, hand it what to start, and return the harness."""
        conns: dict = {}
        deadline = time.time() + wait
        try:
            while len(conns) < 2:
                left = deadline - time.time()
                if left <= 0:
                    raise RelayError(f"the harness's relay didn't connect within {int(wait)} s")
                ready, _, _ = select.select([self.lsock], [], [], left)
                if not ready:
                    continue
                conn, _ = self.lsock.accept()
                role = self._hello(conn)
                if role is None or role in conns:
                    conn.close()
                    continue
                conns[role] = conn
            spec = {"argv": argv, "base_env": base64.b64encode(base_env).decode() if base_env is not None else None}
            conns["ctl"].sendall(json.dumps(spec).encode() + b"\n")
        except BaseException:
            for c in conns.values():
                c.close()
            raise
        finally:
            self.close()
        return RemoteProc(conns["io"], conns["ctl"])


# ====================================================================== harnesses
class Harness:
    """A harness process speaking JSON lines on stdin/stdout. Adapters implement the protocol."""

    def __init__(self, drv: Driver, argv: list):
        self.drv, self.out = drv, drv.out
        self.proc: subprocess.Popen | RemoteProc
        if drv.relay is not None:  # --- KAN-83: the driver sidecar: the harness starts in the agent's container
            a = drv.args
            argv = agent_launch(a.agent, a.agent_home or f"/home/{a.agent}", a.run_env, drv.cwd(), argv)
            self.proc = drv.relay.start(argv, BASE_ENV)
        else:
            if user := getattr(drv.args, "as_user", None):  # --- isolation
                argv = as_agent(user, pwd.getpwnam(user).pw_dir, drv.args.run_env, drv.cwd(), argv)
            with agent_env() as kw:  # --- harden
                self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0, **kw)
        self.buf = b""
        self.stop_after_interrupt = False  # an interrupt without a new message: exit 130 once the turn stops

    def send(self, obj: dict) -> bool:
        """False when the harness has exited (the read loop then sees EOF)."""
        if self.proc.poll() is not None:
            return False
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def read(self, timeout: float) -> bool:
        """Read and handle what the harness wrote within `timeout`. False once its stdout is closed."""
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return True
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            if self.buf:
                self._line(self.buf)
                self.buf = b""
            return False
        self.buf += chunk
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                return True
            raw, self.buf = self.buf[:nl], self.buf[nl + 1:]
            self._line(raw)

    def _line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace")
        try:
            d = json.loads(text)
        except ValueError:
            d = None
        if not isinstance(d, dict):
            self.out.line(text)  # not protocol: keep it in the log as it was
            return
        if str(d.get("type") or "").startswith("hangar."):  # --- harden: only the driver speaks for hangar
            self.out.line("[harness] " + text)  # kept, but no longer a line hangar's parser reads as an event
            return
        self.on(d, text)

    def finish(self, timeout: float = 30) -> int:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        return self.proc.returncode

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()

    # --- the protocol
    def start(self, prompt: str) -> None:
        raise NotImplementedError

    def on(self, d: dict, raw: str) -> None:
        raise NotImplementedError

    def steer(self, text: str, done) -> None:
        raise NotImplementedError

    def interrupt(self, text: str | None, done) -> None:
        raise NotImplementedError

    def settled(self) -> bool:
        """The work asked for is over and nothing hangar sent is outstanding."""
        raise NotImplementedError

    def status(self, code: int) -> int:
        """The driver's exit status, given the harness's."""
        raise NotImplementedError


class Claude(Harness):
    """Claude Code, `claude -p --input-format stream-json --output-format stream-json --verbose`.

    Verified with 2.1.296: a user message written while a turn runs is folded into it, and `command_lifecycle`
    frames follow each message (queued, started, completed or cancelled). `--permission-prompt-tool stdio` turns
    permission prompts into `can_use_tool` control requests. An `interrupt` control request stops the turn and
    cancels its open prompts (`control_cancel_request`); the process then takes the next message in the same
    conversation. A result's `total_cost_usd` and `modelUsage` count the whole process, its `usage` only itself."""

    LIFECYCLE_DONE = ("completed", "cancelled", "failed")

    def __init__(self, drv: Driver, argv: list):
        perms = (["--dangerously-skip-permissions"] if drv.args.approvals == "auto"
                 else ["--permission-prompt-tool", "stdio", "--permission-mode", "default"])
        super().__init__(drv, argv + perms)
        self.sent: dict = {}  # uuid of each message written -> its lifecycle state
        self.lifecycle = False  # this CLI reports command_lifecycle
        self.results = 0  # results since the last message written
        self.last: dict = {}  # the last result
        self.interrupts: dict = {}  # our interrupt request id -> (follow-up text, done)
        self.n = 0
        self.tools: dict = {}  # --- #165: tool_use id -> tool name, for the tree after its result

    def start(self, prompt: str) -> None:
        self.user(prompt)

    def user(self, text: str) -> bool:
        u = str(uuid.uuid4())
        self.sent[u] = "sent"
        self.results = 0
        return self.send({"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
                          "uuid": u})

    def on(self, d: dict, raw: str) -> None:
        t = d.get("type")
        if t == "result":
            d = self._cumulative(d)
            raw = json.dumps(d, ensure_ascii=False)
        self.out.line(raw)
        if t == "command_lifecycle":
            self.lifecycle = True
            if d.get("command_uuid") in self.sent:
                self.sent[d["command_uuid"]] = d.get("state")
        elif t == "result":
            self.results += 1
            self.last = d
        elif t == "assistant":  # --- taint (#130): a web tool's use taints the turn, whatever hangar sends
            for c in (d.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in _TAINT_WEB:
                    self.drv.taint(f"web:{c.get('name')}")
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id"):  # --- #165
                    self.__dict__.setdefault("tools", {})[str(c["id"])] = str(c.get("name") or "")
        elif t == "user":  # --- #165: after an edit or shell tool's result, the tree it left
            content = (d.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tid = str(c.get("tool_use_id") or "")
                    trees = getattr(self.drv, "trees", None)
                    if self.__dict__.get("tools", {}).pop(tid, None) in TREE_TOOLS and trees is not None:
                        trees.point(tool_use_id=tid)
        elif t == "control_request":
            self._control(d.get("request_id"), d.get("request") or {})
        elif t == "control_cancel_request":
            self.drv.withdrawn(d.get("request_id"))
        elif t == "control_response":
            r = d.get("response") or {}
            if r.get("request_id") in self.interrupts:
                text, done = self.interrupts.pop(r["request_id"])
                if r.get("subtype") != "success":
                    self.stop_after_interrupt = False
                    return done(clip(r.get("error") or "the agent refused the interrupt", 300))
                if text:
                    self.user(text)
                done(None)

    @staticmethod
    def _cumulative(d: dict) -> dict:
        """Give a result the process's token totals, as its cost already is: hangar's parser takes the last
        result as the turn's total, and one turn may span several results (a steer after the agent finished)."""
        mu = d.get("modelUsage")
        if not isinstance(mu, dict) or not mu:
            return d
        models = [m for m in mu.values() if isinstance(m, dict)]

        def total(key: str) -> int:
            return sum(int(m.get(key) or 0) for m in models)

        usage = dict(d.get("usage") or {})
        usage.update(input_tokens=total("inputTokens"), output_tokens=total("outputTokens"),
                     cache_read_input_tokens=total("cacheReadInputTokens"),
                     cache_creation_input_tokens=total("cacheCreationInputTokens"))
        return dict(d, usage=usage)

    def _control(self, rid: object, req: dict) -> None:
        if req.get("subtype") != "can_use_tool":
            return self.send({"type": "control_response", "response": {
                "subtype": "error", "request_id": rid, "error": f"hangar does not handle {req.get('subtype')}"}})
        data = req.get("input") if isinstance(req.get("input"), dict) else {}
        suggestions = [s for s in req.get("permission_suggestions") or [] if isinstance(s, dict)]

        def answer(decision: str, message: str) -> None:
            if decision == "deny":
                resp: dict = {"behavior": "deny", "message": message}
            else:
                resp = {"behavior": "allow", "updatedInput": data}
                if decision == "allow_always" and suggestions:
                    # this process only (the turn): never a settings file; the next turn asks again
                    resp["updatedPermissions"] = [dict(s, destination="session") for s in suggestions]
            self.send({"type": "control_response",
                       "response": {"subtype": "success", "request_id": rid, "response": resp}})

        self.drv.request(rid, str(req.get("tool_name") or "?"), data, answer)

    def _idle(self) -> bool:
        if not self.results:
            return False
        if self.lifecycle:
            return all(s in self.LIFECYCLE_DONE for s in self.sent.values())
        return not self.last.get("queued_turn_count")

    def steer(self, text: str, done) -> None:
        if not self.user(text):
            return done(ENDED)
        self.stop_after_interrupt = False  # a message after an interrupt: carry on with it
        done(None)

    def interrupt(self, text: str | None, done) -> None:
        if self.proc.poll() is not None:
            return done(ENDED)
        if self._idle() and not self.interrupts:  # nothing is running
            if text and not self.user(text):
                return done(ENDED)
            return done(None)
        self.n += 1
        rid = f"hangar-interrupt-{self.n}"
        self.interrupts[rid] = (text, done)
        if not text:
            self.stop_after_interrupt = True
            self.results = 0  # wait for the result of the stopped turn
        self.send({"type": "control_request", "request_id": rid, "request": {"subtype": "interrupt"}})

    def settled(self) -> bool:
        if self.interrupts:
            return False
        if self.stop_after_interrupt:
            return self.results > 0
        return self._idle()

    def status(self, code: int) -> int:
        if self.stop_after_interrupt:
            return 130
        if code:
            return code
        return 1 if self.last.get("is_error") or (self.last.get("subtype") or "success") != "success" else 0


def _snake(s: object) -> object:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", s).lower() if isinstance(s, str) else s


def exec_item(item: object) -> dict | None:
    """An app-server thread item as `codex exec --json` writes it, or None for items exec leaves out."""
    if not isinstance(item, dict):
        return None
    t, out = item.get("type"), {"id": item.get("id")}
    if t == "agentMessage":
        out.update(type="agent_message", text=item.get("text") or "")
    elif t == "reasoning":
        out.update(type="reasoning", text=" ".join(str(x) for x in item.get("summary") or []))
    elif t == "commandExecution":
        out.update(type="command_execution", command=item.get("command") or "",
                   aggregated_output=item.get("aggregatedOutput") or "", exit_code=item.get("exitCode"),
                   status=_snake(item.get("status")))
    elif t == "fileChange":
        changes = []
        for c in item.get("changes") or []:
            if isinstance(c, dict):
                kind = c.get("kind")
                changes.append({"path": c.get("path"), "kind": kind.get("type") if isinstance(kind, dict) else kind})
        out.update(type="file_change", changes=changes, status=_snake(item.get("status")))
    elif t == "mcpToolCall":
        out.update(type="mcp_tool_call", server=item.get("server"), tool=item.get("tool"),
                   arguments=item.get("arguments"), result=item.get("result"), error=item.get("error"),
                   status=_snake(item.get("status")))
    elif t == "webSearch":
        out.update(type="web_search", query=item.get("query") or "")
    else:
        return None
    return out


class Codex(Harness):
    """Codex `app-server`: JSON-RPC over stdio, without the `jsonrpc` field.

    Verified with 0.162.1: `initialize`, the `initialized` notification, then thread/start (or thread/resume,
    thread/fork) and turn/start. turn/steer adds input to the active turn (-32600 "no active turn to steer" when
    there is none). turn/interrupt answers {} and the turn completes with status `interrupted`, its open
    approvals resolved (`serverRequest/resolved`). With approvalPolicy `untrusted`, commands and file changes come
    as approval requests answered with a decision; a declined one lets the turn go on."""

    APPROVALS = {"item/commandExecution/requestApproval": "shell", "item/fileChange/requestApproval": "apply_patch",
                 "execCommandApproval": "shell", "applyPatchApproval": "apply_patch"}
    DECISION = {"allow": "accept", "allow_always": "acceptForSession", "deny": "decline"}
    LEGACY = {"allow": "approved", "allow_always": "approved_for_session", "deny": "denied"}

    def __init__(self, drv: Driver, argv: list):
        super().__init__(drv, argv)
        self.ids = 0
        self.calls: dict = {}  # request id -> callback(result, error)
        self.prompt = ""
        self.thread: str | None = None
        self.turn: str | None = None
        self.starting = False  # turn/start sent, the turn not known yet
        self.later: list = []  # steers that came while no turn could take them: (text, done)
        self.queue: list = []  # the message an interrupt brought, for the next turn
        self.completed = 0
        self.last_status: str | None = None
        self.failed: str | None = None
        self.base: dict | None = None  # thread token totals before the current turn
        self.total: dict | None = None
        self.files: dict = {}  # fileChange item id -> paths (approval requests name only the item)
        self.held_interrupt: tuple | None = None  # an interrupt that came before the turn started

    def call(self, method: str, params: dict, then=None) -> None:
        self.ids += 1
        self.calls[self.ids] = then
        if not self.send({"id": self.ids, "method": method, "params": params}):
            self.calls.pop(self.ids, None)  # it exited: nothing will answer; Driver.ended() answers for it

    def emit(self, obj: dict) -> None:
        self.out.line(json.dumps(obj, ensure_ascii=False))

    @staticmethod
    def _err(error: object) -> str:
        return clip(error.get("message") if isinstance(error, dict) else error, 300)

    # --- start
    def start(self, prompt: str) -> None:
        self.prompt = prompt
        self.call("initialize", {"clientInfo": {"name": "hangar", "version": "1"}}, self._initialized)

    def _initialized(self, result: object, error: object) -> None:
        if error:
            return self._fail("initialize failed: " + self._err(error))
        self.send({"method": "initialized"})
        params: dict = {"cwd": self.drv.cwd(), "sandbox": "danger-full-access",
                        "approvalPolicy": "never" if self.drv.args.approvals == "auto" else "untrusted"}
        if self.drv.args.approvals != "auto":  # --- harden: to hangar, whatever the agent's config.toml says
            params["approvalsReviewer"] = "user"
        if os.environ.get("HANGAR_MODEL"):
            params["model"] = os.environ["HANGAR_MODEL"]
        fork, resume = os.environ.get("HANGAR_SESSION_FORK"), os.environ.get("HANGAR_SESSION_RESUME")
        if fork:
            self.call("thread/fork", dict(params, threadId=fork), self._opened)
        elif resume:
            self.call("thread/resume", dict(params, threadId=resume), self._opened)
        else:
            self.call("thread/start", params, self._opened)

    def _opened(self, result: object, error: object) -> None:
        thread = (result or {}).get("thread") if isinstance(result, dict) else None
        if error or not isinstance(thread, dict) or not thread.get("id"):
            return self._fail("could not open the conversation: " + self._err(error or "no thread in the answer"))
        self.thread = thread["id"]
        self.emit({"type": "thread.started", "thread_id": self.thread})
        self._start_turn(self.prompt, None)

    def _fail(self, message: str) -> None:
        self.emit({"type": "turn.failed", "error": {"message": message}})
        self.failed = message
        waiting = self.later + ([self.held_interrupt] if self.held_interrupt else [])
        self.later, self.held_interrupt = [], None
        for _, done in waiting:
            done("the agent could not start: " + message)

    def _start_turn(self, text: str, done) -> None:
        self.starting = True

        def started(result: object, error: object) -> None:
            self.starting = False
            if error:
                return done(self._err(error)) if done else self._fail("turn/start failed: " + self._err(error))
            turn = (result or {}).get("turn") if isinstance(result, dict) else None
            if isinstance(turn, dict) and turn.get("id") and turn.get("status") in (None, "inProgress"):
                self.turn = turn["id"]
            if done:
                done(None)
            self._flush()

        self.call("turn/start", {"threadId": self.thread, "input": [{"type": "text", "text": text}]}, started)

    def _flush(self) -> None:
        """Hand waiting steers, then a waiting interrupt, to the turn that now runs (or start one for them)."""
        waiting, self.later = self.later, []
        for text, done in waiting:
            self.steer(text, done)
        if self.held_interrupt and not self.starting:
            (text, done), self.held_interrupt = self.held_interrupt, None
            self.interrupt(text, done)

    # --- protocol
    def on(self, d: dict, raw: str) -> None:
        if "method" in d and "id" in d:
            return self._request(d["id"], d["method"], d.get("params") or {})
        if "id" in d:
            then = self.calls.pop(d["id"], None)
            if then:
                then(d.get("result"), d.get("error"))
            return
        m, p = d.get("method"), d.get("params") or {}
        if m == "turn/started":
            self.turn = (p.get("turn") or {}).get("id") or self.turn
            self.starting = False
            self.emit({"type": "turn.started"})
            self._flush()
        elif m in ("item/started", "item/completed"):
            item = p.get("item") or {}
            if item.get("type") == "fileChange":
                self.files[item.get("id")] = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
            if item.get("type") == "webSearch":  # --- taint (#130)
                self.drv.taint("web:web_search")
            it = exec_item(item)
            if it:
                self.emit({"type": "item.started" if m == "item/started" else "item.completed", "item": it})
            trees = getattr(self.drv, "trees", None)
            if m == "item/completed" and item.get("type") in ("commandExecution", "fileChange") and trees:  # #165
                trees.point(tool_use_id=item.get("id"))
        elif m == "thread/tokenUsage/updated":
            u = p.get("tokenUsage") or {}
            total, last = u.get("total"), u.get("last") or {}
            if isinstance(total, dict):
                if self.base is None:
                    self.base = {k: (v or 0) - (last.get(k) or 0) for k, v in total.items() if isinstance(v, int)}
                self.total = total
        elif m == "turn/completed":
            self._completed(p.get("turn") or {})
        elif m == "error":
            e = p.get("error") or {}
            self.emit({"type": "error", "message": e.get("message") if isinstance(e, dict) else str(e)})
        elif m == "serverRequest/resolved":
            self.drv.withdrawn(p.get("requestId"))

    def _usage(self) -> dict:
        total, base = self.total or {}, self.base or {}

        def delta(key: str) -> int:
            return max(0, int(total.get(key) or 0) - int(base.get(key) or 0))

        out = {"input_tokens": delta("inputTokens"), "cached_input_tokens": delta("cachedInputTokens"),
               "cache_write_input_tokens": delta("cacheWriteInputTokens"), "output_tokens": delta("outputTokens"),
               "reasoning_output_tokens": delta("reasoningOutputTokens")}
        if self.total is not None:
            self.base = dict(self.total)
        return out

    def _completed(self, turn: dict) -> None:
        status = turn.get("status")
        if status == "failed":
            err = turn.get("error") or {}
            self.emit({"type": "turn.failed", "error": {"message": err.get("message") or "the turn failed"}})
        self.emit({"type": "turn.completed", "usage": self._usage()})
        self.turn, self.last_status = None, status
        self.completed += 1
        if self.queue:
            self.stop_after_interrupt = False
            self._start_turn(self.queue.pop(0), None)
        elif self.later:
            text, done = self.later.pop(0)
            self._start_turn(text, done)

    def _request(self, rid: object, method: str, p: dict) -> None:
        tool = self.APPROVALS.get(method)
        if tool is None:
            return self.send({"id": rid, "error": {"code": -32601, "message": f"hangar does not answer {method}"}})
        if tool == "shell":
            command = p.get("command")
            data = {"command": " ".join(map(str, command)) if isinstance(command, list) else command}
            if isinstance(command, list):
                data["argv"] = [str(c) for c in command]  # --- #292: the script, its quoting intact
        else:
            paths = self.files.get(p.get("itemId")) or list((p.get("fileChanges") or {}).keys())
            data = {"paths": paths + ([p["grantRoot"]] if p.get("grantRoot") else []), "reason": p.get("reason")}
        table = self.LEGACY if method in ("execCommandApproval", "applyPatchApproval") else self.DECISION

        def answer(decision: str, message: str) -> None:
            self.send({"id": rid, "result": {"decision": table[decision]}})

        self.drv.request(rid, tool, data, answer)

    # --- hangar's commands
    def steer(self, text: str, done) -> None:
        if self.failed:
            return done("the agent could not start: " + self.failed)
        self.stop_after_interrupt = False  # a message after an interrupt: carry on with it
        if self.thread is None or self.starting:
            self.later.append((text, done))
        elif self.turn is None:
            self._start_turn(text, done)
        else:
            def steered(result: object, error: object) -> None:
                if error and self.turn is None:  # it ended meanwhile: the message starts the next turn
                    return self.steer(text, done)
                if error:  # the turn is ending (-32600 can come before turn/completed): the next one takes it
                    return self.later.append((text, done))
                done(None)

            self.call("turn/steer", {"threadId": self.thread, "expectedTurnId": self.turn,
                                     "input": [{"type": "text", "text": text}]}, steered)

    def interrupt(self, text: str | None, done) -> None:
        if self.turn is None and not self.failed and (self.thread is None or self.starting):
            self.held_interrupt = (text, done)  # for the turn about to start
            return
        if self.turn is None:
            return self.steer(text, done) if text else done(None)
        if text:
            self.queue.append(text)
        else:
            self.stop_after_interrupt = True
        self.call("turn/interrupt", {"threadId": self.thread, "turnId": self.turn},
                  lambda result, error: done(self._err(error) if error else None))

    def settled(self) -> bool:
        if self.failed:
            return not self.calls
        return (self.completed > 0 and self.turn is None and not self.starting and not self.queue
                and not self.later and not self.held_interrupt and not self.calls)

    def status(self, code: int) -> int:
        if self.failed or self.last_status == "failed":
            return code or 1
        if self.stop_after_interrupt and self.last_status == "interrupted":
            return 130
        return code


# ====================================================================== the verify gate (--- overnight/autofix)
class Gate:
    """The job's check, run when the agent settles; a failure goes back into the same conversation as a message,
    up to `rounds` times and only before `deadline` (verifyloop.py writes $HANGAR_VERIFY_GATE). Each check is
    recorded in ~/.hangar/runs/<run>.verify ("fail <exit>" or "pass") and as a hangar.verify event."""

    TAIL = 6000

    def __init__(self, spec: dict, out: Out, args: argparse.Namespace | None = None):
        self.command, self.cwd = spec["command"], spec["cwd"]
        self.rounds, self.deadline, self.timeout = spec["rounds"], spec["deadline"], spec["timeout"]
        self.out, self.failed = out, 0
        # --- testresults: shell that goes before the check (its reporter variables), with @D@ for the directory its
        # reports go to: one per check, next to the rounds file (~/.hangar/runs/<run>.tests/<n>/)
        self.prefix = spec.get("prefix") if isinstance(spec.get("prefix"), str) else ""
        self.checks = 0
        # integrate(#65 x #66): under the uid split the check is the agent's code (the repo's tests), so it runs as
        # the agent, in the harness's environment, never as hangar's user, whose capabilities could make it root.
        # The rounds file is the agent's too, where hangar and the Stop hook read and write it.
        self.agent: tuple[str, str, str] | None = None
        home = "~"
        if args is not None and (user := getattr(args, "as_user", None)):
            home = pwd.getpwnam(user).pw_dir
            self.agent = (user, home, args.run_env)
        self.state = os.path.expanduser(f"{home}/.hangar/runs/{os.environ.get('HANGAR_RUN_ID') or 'run'}.verify")

    def _argv(self, argv: list) -> list:
        if self.agent is None:
            return argv
        user, home, run_env = self.agent
        return as_agent(user, home, run_env, self.cwd, argv)

    def _record(self, line: str) -> None:
        try:
            if self.agent:
                with agent_env() as kw:  # --- harden
                    subprocess.run(self._argv(["sh", "-c", 'printf "%s\\n" "$1" >> "$0"', self.state, line]),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=30, **kw)
            else:
                with open(self.state, "a") as f:
                    f.write(line + "\n")
        except (OSError, subprocess.SubprocessError):
            pass

    @classmethod
    def load(cls, out: Out, args: argparse.Namespace | None = None) -> Gate | None:
        try:
            spec = json.loads(os.environ.get("HANGAR_VERIFY_GATE") or "null")
        except ValueError:
            return None
        if not (isinstance(spec, dict) and isinstance(spec.get("command"), str) and spec["command"].strip()
                and isinstance(spec.get("cwd"), str) and all(isinstance(spec.get(k), int) and spec[k] >= 0
                                                             for k in ("rounds", "deadline", "timeout"))):
            return None
        return cls(spec, out, args)

    def due(self) -> bool:
        return self.failed < self.rounds and time.time() < self.deadline

    def check(self) -> str | None:
        """Run the check; None when it passes (or can't run: hangar's own check after the turn decides), else
        the message that sends the agent back to work."""
        command = self.command
        if self.prefix:  # --- testresults
            reports = f"{self.state.removesuffix('.verify')}.tests/{self.checks}"
            command = self.prefix.replace("@D@", shlex.quote(reports)) + self.command
        try:
            with agent_env() as kw:  # --- harden
                p = subprocess.run(self._argv(["bash", "-c", command]), cwd=None if self.agent else self.cwd,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=max(1, self.timeout), **kw)
            code, output = p.returncode, p.stdout or b""
        except subprocess.TimeoutExpired as e:
            code, output = 124, (e.stdout or b"") + f"\n(timed out after {self.timeout} s)".encode()
        except OSError:
            return None
        self._record("pass" if code == 0 else f"fail {code}")
        self.checks += 1  # --- testresults: one per line of the rounds file, as the Stop hook counts them
        if code == 0:
            self.out.event("verify", round=self.failed, exit_code=0, passed=True)
            return None
        self.failed += 1
        self.out.event("verify", round=self.failed, of=self.rounds, exit_code=code, passed=False)
        tail = output.decode("utf-8", errors="replace").strip()[-self.TAIL:] or "(no output)"
        return (f"hangar: the job's check `{self.command}` failed (exit {code}), round {self.failed} of "
                f"{self.rounds}. Fix the cause and run the check again before you finish. Don't change the check "
                "or the tests it runs to make it pass; if the failure is outside this task, say so in RESULT.md. "
                f"The check's output ends with:\n\n{tail}")


# ====================================================================== main
def main(argv: list | None = None) -> int:
    global BASE_ENV  # --- harden
    ap = argparse.ArgumentParser(description="Run one agent turn for hangar (see the module docstring).")
    ap.add_argument("--harness", choices=("claude", "codex"), required=True)
    ap.add_argument("--ctl", required=True, help="the run's control directory")
    ap.add_argument("--approvals", choices=("auto", "ask", "deny-risky"), default="auto")
    ap.add_argument("--approval-timeout", type=int, default=600)
    # --- isolation: the uid split
    ap.add_argument("--as", dest="as_user", help="start the harness as this user (the driver runs as hangar's own)")
    ap.add_argument("--run-env", help="with --as: the run's exports, sourced for the harness after ~/.hangar/env")
    ap.add_argument("--cwd", help="the turn's working directory (default: the current one)")
    ap.add_argument("--base-env", help="with --as: hangar's copy of the agent's ~/.hangar/env, used in its place; "
                                       "read and deleted at start (--- harden)")
    ap.add_argument("--tainted", action="store_true", help="the turn starts having read untrusted content (#130)")
    ap.add_argument("--preclear", action="store_true",
                    help="calls Tower signed are in <ctl>/preclear, signed with the run's key (KAN-77)")
    ap.add_argument("--write-tool", action="append", default=[],
                    help="an MCP tool (fnmatch pattern) that writes: needs a person once tainted (#130)")
    # --- KAN-83: the driver sidecar
    ap.add_argument("--relay", help="the harness starts in the agent's container through relay.py, which connects to "
                                    "the port this file is given; its token is <ctl>/relay-token, read and deleted "
                                    "at start")
    ap.add_argument("--agent", help="with --relay: the agent's user")
    ap.add_argument("--agent-home", help="with --relay: the agent's home (default /home/<agent>)")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="-- the harness command")
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("the harness command is missing")
    if args.as_user and not args.run_env:  # --- isolation
        ap.error("--as needs --run-env")
    if args.relay and (args.as_user or not args.run_env or not args.agent):  # --- KAN-83
        ap.error("--relay needs --agent and --run-env, and not --as (the harness starts in another container)")
    if args.base_env and not (args.as_user or args.relay):  # --- harden
        ap.error("--base-env needs --as or --relay")
    token_file = os.path.join(args.ctl, "relay-token")
    why = private()  # before the key is read
    if why and args.approvals != "auto":  # deny by default: the agent could answer its own approvals
        sys.stderr.write(f"hangar: approvals={args.approvals} needs the turn driver to keep its key from the agent, "
                         f"and it could not (prctl PR_SET_DUMPABLE: {why})\n")
        for path in (os.path.join(args.ctl, "key"), token_file, args.base_env):
            try:
                os.unlink(path or "")
            except OSError:
                pass
        return 1
    if args.base_env:  # --- harden: in memory only from here on, like the key
        try:
            with open(args.base_env, "rb") as f:
                BASE_ENV = f.read()
            os.unlink(args.base_env)
        except OSError as e:
            sys.stderr.write(f"hangar: could not read the harness's environment ({args.base_env}: {e.strerror})\n")
            return 1
    with open(os.environ["HANGAR_PROMPT_FILE"], encoding="utf-8", errors="replace") as f:
        prompt = f.read()
    out, ctl = Out(), Control(args.ctl)
    drv = Driver(args, out, ctl)
    if args.preclear:  # --- KAN-77: with the key in hand, before the harness can ask anything
        drv.preclear = load_preclear(args.ctl, ctl.key)
    if args.relay:  # --- KAN-83: like the key, the relay's token is in memory only from here on
        try:
            with open(token_file, "rb") as f:
                token = f.read().strip()
            os.unlink(token_file)
        except OSError:
            token = b""
        if not token:
            sys.stderr.write("hangar: --relay needs the relay's token in <ctl>/relay-token\n")
            ctl.finish()
            return 1
        drv.relay = Relay(args.relay, token)
    try:
        h: Harness = (Claude if args.harness == "claude" else Codex)(drv, command)
    except RelayError as e:
        sys.stderr.write(f"hangar: {e}\n")
        ctl.finish()
        return 1
    drv.harness = h

    def stop(signum: int, _frame: object) -> None:
        h.kill()
        drv.ended()
        ctl.finish()
        os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop)
    gate = Gate.load(out, args)  # --- overnight/autofix
    if gate is not None and args.relay:  # --- KAN-83: the check is the agent's code; it would run as the driver's uid
        sys.stderr.write("hangar: the verify gate doesn't run under the driver sidecar yet; hangar's verify still "
                         "runs after the turn\n")
        gate = None
    drv.trees.point("start")  # --- #165: the harness is up but has no prompt yet: nothing has changed the tree
    h.start(prompt)
    alive = True
    while alive:
        alive = h.read(POLL)
        if not alive:  # the harness went away: nothing would read what came in now
            break
        for cmd in ctl.commands():
            drv.handle(cmd)
        drv.expire()
        if alive and h.settled():
            # --- overnight/autofix: a failing check continues the conversation (never after a bare interrupt)
            if gate is not None and not h.stop_after_interrupt and gate.due():
                fix = gate.check()  # commands that come meanwhile wait in the inbox for the next pass
                if fix is not None:
                    h.steer(fix, lambda err=None: None)
                    continue
            late = ctl.close()
            if not late:
                break
            ctl.reopen()
            for cmd in late:  # landed before the close: still carried out (a steer continues the turn)
                drv.handle(cmd)
    if not alive:  # the harness went away on its own
        for cmd in ctl.close():
            ctl.ack(cmd["id"], False, error=ENDED)
        drv.ended()
    drv.drop_all()
    ctl.finish()
    code = h.finish()
    drv.trees.point("end")  # --- #165
    drv.trees.close()
    return h.status(code)


if __name__ == "__main__":
    sys.exit(main())
