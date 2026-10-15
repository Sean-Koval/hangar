"""hangar-svc: the in-sandbox supervisor behind `hangar-agent svc` (--- envbuilds; docs/environments.md).

Installed per sandbox at ~/.hangar/bin/hangar-svc (hangar.supervisor) and run as the sandbox's own user. It is
standalone: the standard library of any python3 the image contract allows (3.8 and later), nothing from hangar.

A small daemon keeps the environment's processes up so the agent doesn't have to: it waits for the services (Postgres,
Redis...) to answer, runs `start` once, then starts the `terminals` (dev servers, watchers) and restarts them with
backoff when they exit. Each program's output goes to ~/.hangar/svc/logs/<name>.log, a file the agent can read and
search. The CLI talks to the daemon over a Unix socket in ~/.hangar/svc (0700).

hangar writes ~/.hangar/svc/manifest.json at claim and runs `hangar-svc up`.

--- heavy: in a sandbox with Docker (the heavy tier), a program can be a Compose stack: `docker compose -p NAME -f FILE
up` in the foreground, so its containers' output is the program's log and a stop (SIGTERM) stops them. Stacks start
with the services, before `start`, and `start` waits for them to be ready: every service of the file with a container
running (and healthy, if it has a healthcheck) or exited 0, unless the stack names a port or a `ready` command.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import selectors
import shlex
import signal
import socket
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DIR = os.environ.get("HANGAR_SVC_DIR") or os.path.join(HOME, ".hangar", "svc")
LOGS = os.path.join(DIR, "logs")
MANIFEST = os.path.join(DIR, "manifest.json")
ADDED = os.path.join(DIR, "added.json")  # programs the agent added with `hangar-agent svc add`
STATE = os.path.join(DIR, "state.json")
SOCK = os.path.join(DIR, "sock")
LOCK = os.path.join(DIR, "lock")
DAEMON_LOG = os.path.join(DIR, "supervisor.log")

LOG_MAX = 10 * 1024 * 1024  # a program's log is rotated to <name>.log.1 past this
BACKOFF_MAX = 60.0
GIVE_UP = 10  # consecutive runs shorter than STABLE seconds, then the program is `failed` until started again
STABLE = 30.0
STOP_GRACE = 10.0
READY_TIMEOUT = 10.0  # a `ready` command's own limit
SERVICE_WAIT = float(os.environ.get("HANGAR_SVC_SERVICE_WAIT") or 180)  # how long `start` waits for services
NAME_OK = "abcdefghijklmnopqrstuvwxyz0123456789-"
DOCKER = os.environ.get("HANGAR_SVC_DOCKER") or "docker"  # --- heavy: the CLI Compose stacks run with (tests set it)

USAGE = """\
usage: hangar-agent svc COMMAND [ARGS]

Your environment's services and long-running processes, kept up for you. Programs come from the repo's
.hangar/environment.yaml (`start`, `terminals`, `services`) and from `svc add`. A program that exits is
restarted with backoff; output is in ~/.hangar/svc/logs/<name>.log (search it with rg or grep).

commands:
  status [NAME...] [--json]       what runs, whether it's ready, restarts, ports and log files
  wait [NAME...] [--timeout S]    block until they are ready (default: everything that starts by itself;
                                  exit 1 if one failed or the time ran out)
  logs NAME [-n LINES] [-f]       the end of a program's log (-f: follow it)
  start NAME... | --all           start programs (also ones declared with autostart: false)
  stop NAME... | --all            stop them (they stay stopped until started)
  restart NAME... | --all         stop, then start
  add NAME [--port P] [--ready CMD] [--cwd DIR] [--once] -- COMMAND...
                                  supervise a command of your own (until the sandbox goes away)
  add NAME --compose FILE [--port P] [--ready CMD]
                                  supervise a Compose stack (`docker compose -p NAME -f FILE up`; heavy tier)
  rm NAME                         stop and forget a program you added
  run-start                       run the environment's `start` command again

examples:
  hangar-agent svc status
  hangar-agent svc wait web --timeout 120 && curl -fsS localhost:3000/health
  hangar-agent svc logs web -n 200 | rg -i error
  hangar-agent svc add worker --ready 'test -f /tmp/worker.ready' -- npm run worker
  hangar-agent svc add deps --compose docker-compose.yml && hangar-agent svc wait deps --timeout 300
"""


# ====================================================================== small helpers
def _now():
    return time.time()


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _port_open(host, port, timeout=0.3):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _ago(ts):
    if not ts:
        return "-"
    s = int(_now() - ts)
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def _valid_name(name):
    return bool(name) and len(name) <= 31 and name[0].isalpha() and all(c in NAME_OK for c in name)


def _manifest():
    m = _read_json(MANIFEST, {})
    return m if isinstance(m, dict) else {}


# ====================================================================== --- heavy: Compose stacks
def compose_spec(spec):
    """A Compose stack's program: `docker compose up` in the foreground, ready by its port, its `ready` command, or
    `hangar-svc compose-ready` (every service up)."""
    name, f = spec["name"], str(spec["compose"])
    out = dict(spec)
    out["command"] = (f"{shlex.quote(DOCKER)} compose -p {shlex.quote(name)} -f {shlex.quote(f)} up --remove-orphans "
                      "--quiet-pull --no-color")
    if not out.get("port") and not out.get("ready"):
        out["ready"] = "{} {} compose-ready {} {}".format(shlex.quote(sys.executable or "python3"),
                                                          shlex.quote(os.path.abspath(__file__)), shlex.quote(name),
                                                          shlex.quote(f))
    return out


def _compose_json(text):
    """`docker compose ps --format json`: a JSON array (older Compose) or one object per line (2.21 and later)."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            v = json.loads(text)
            return [x for x in v if isinstance(x, dict)]
        except ValueError:
            return []
    out = []
    for line in text.splitlines():
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def stack_ready(services, containers):
    """Every service has a container that is running (healthy, when it has a healthcheck) or exited 0."""
    ok = set()
    for c in containers:
        state, health = c.get("State") or "", c.get("Health") or ""
        if (state == "running" and health in ("", "healthy")) or (state == "exited" and c.get("ExitCode") == 0):
            ok.add(c.get("Service"))
    return bool(services) and set(services) <= ok


def compose_ready(name, f):
    base = [DOCKER, "compose", "-p", name, "-f", f]
    try:
        svcs = subprocess.run(base + ["config", "--services"], capture_output=True, text=True, timeout=8)
        ps = subprocess.run(base + ["ps", "--all", "--format", "json"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return 1
    if svcs.returncode != 0 or ps.returncode != 0:
        return 1
    return 0 if stack_ready([s for s in svcs.stdout.split() if s], _compose_json(ps.stdout)) else 1


# ====================================================================== the daemon
class Program:
    """One supervised process: a terminal from the manifest, one the agent added, or the one-shot `start`."""

    def __init__(self, spec, kind):
        self.spec = spec
        self.kind = kind  # terminal | added | start
        self.name = spec["name"]
        self.proc = None
        self.out = None  # the pipe from the process
        self.log = None
        self.state = "stopped"  # stopped | waiting | starting | running | backoff | exited | failed
        self.ready = False
        self.restarts = 0
        self.fails = 0  # consecutive short runs
        self.started_at = None
        self.exited_at = None
        self.last_exit = None
        self.next_start = None
        self.ready_proc = None
        self.ready_started = 0.0
        self.next_check = 0.0
        self.stopping = None  # when SIGTERM was sent
        self.wanted = False  # someone asked for it to run

    @property
    def once(self):
        return self.kind == "start" or self.spec.get("restart") == "never" or self.spec.get("once")

    def logpath(self):
        return os.path.join(LOGS, self.name + ".log")

    def view(self):
        return {"name": self.name, "kind": self.kind, "state": self.state, "ready": self.ready,
                "pid": self.proc.pid if self.proc else None, "restarts": self.restarts,
                "started_at": self.started_at, "exited_at": self.exited_at, "last_exit": self.last_exit,
                "port": self.spec.get("port"), "check": self.spec.get("ready"), "command": self.spec.get("command"),
                "log": self.logpath()}


class Daemon:
    def __init__(self):
        self.sel = selectors.DefaultSelector()
        self.programs = {}  # name -> Program
        self.services = []
        self.repo = None
        self.start_phase = "pending"  # pending | waiting-services | running | done
        self.services_since = None
        self.closing = False
        self.next_home_check = 0.0

    # ------------------------------------------------------------------ setup
    def load(self, first=False):
        m = _manifest()
        self.repo = m.get("repo") or HOME
        self.services = [s for s in m.get("services") or [] if isinstance(s, dict) and _valid_name(s.get("name"))]
        specs = [(t, "terminal") for t in m.get("terminals") or []]
        specs += [(compose_spec({"name": c.get("name"), "compose": c["file"], "port": c.get("port"),  # --- heavy
                                 "ready": c.get("ready"), "env": c.get("env") or {}}), "compose")
                  for c in m.get("compose") or [] if isinstance(c, dict) and c.get("file") and c.get("name")]
        specs += [(compose_spec(t) if isinstance(t, dict) and t.get("compose") else t, "added")
                  for t in _read_json(ADDED, [])]
        if m.get("start"):
            specs.append(({"name": "start", "command": m["start"], "restart": "never"}, "start"))
        seen = set()
        for spec, kind in specs:
            name = spec.get("name") if isinstance(spec, dict) else None
            if not _valid_name(name) or not spec.get("command") or name in seen:
                continue
            seen.add(name)
            if name in self.programs:
                self.programs[name].spec = spec
            else:
                self.programs[name] = Program(spec, kind)
                if first and (kind == "start" or (kind == "terminal" and spec.get("autostart", True))
                              or kind in ("added", "compose")):
                    self.programs[name].wanted = True
        for name in [n for n in self.programs if n not in seen and self.programs[n].proc is None]:
            del self.programs[name]

    def serve(self, lock_fd):
        os.makedirs(LOGS, exist_ok=True)
        try:
            os.unlink(SOCK)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)
        try:
            srv.bind(SOCK)
        finally:
            os.umask(old)
        srv.listen(16)
        srv.setblocking(False)
        self.sel.register(srv, selectors.EVENT_READ, ("accept", None))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "closing", True))
        signal.signal(signal.SIGHUP, lambda *_: self.load())
        self.load(first=True)
        self._log(f"supervisor started (pid {os.getpid()})")
        while not self.closing:
            for key, _ in self.sel.select(timeout=0.5):
                what, prog = key.data
                if what == "accept":
                    self._accept(srv)
                elif what == "out":
                    self._pump(prog)
            self.tick()
        for p in self.programs.values():
            self._stop(p, wait=True)
        os.close(lock_fd)

    def _log(self, msg):
        sys.stderr.write("{} {}\n".format(time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
        sys.stderr.flush()

    # ------------------------------------------------------------------ the loop
    def tick(self):
        now = _now()
        if now >= self.next_home_check:  # its directory gone (a workspace wiped, a test's box removed): stop
            self.next_home_check = now + 5
            if not os.path.isdir(DIR):
                self.closing = True
                return
        start = self.programs.get("start")
        if self.start_phase == "pending":
            self.start_phase = "waiting-services"
            self.services_since = now
        if self.start_phase == "waiting-services":
            stacks = [p for p in self.programs.values() if p.spec.get("compose") and p.wanted]  # --- heavy
            if (all(self._service_ready(s) for s in self.services) and all(p.ready or p.state == "failed"
                                                                            for p in stacks)) \
                    or now - self.services_since > SERVICE_WAIT:
                self.start_phase = "running" if start is not None and start.wanted else "done"
                if self.start_phase == "running":
                    self._spawn(start)
        elif self.start_phase == "running" and (start is None or start.proc is None):
            self.start_phase = "done"
        for p in list(self.programs.values()):
            self._reap(p, now)
            if p.kind != "start" and p.wanted and p.proc is None:
                if self.start_phase != "done" and not p.spec.get("compose"):  # --- heavy: stacks start right away
                    if p.state == "stopped":
                        p.state = "waiting"  # for the services and `start`
                elif p.state in ("stopped", "waiting") or (p.state == "backoff" and now >= (p.next_start or 0)):
                    self._spawn(p)
            self._check_ready(p, now)
            if p.stopping and p.proc is not None and now - p.stopping > STOP_GRACE:
                self._kill(p, signal.SIGKILL)
        self._save()

    def _service_ready(self, s):
        return _port_open(s.get("host") or "127.0.0.1", s.get("port") or 0) if s.get("port") else True

    def _spawn(self, p):
        os.makedirs(LOGS, exist_ok=True)
        cwd = self.repo
        if p.spec.get("cwd"):
            cwd = os.path.join(self.repo, p.spec["cwd"])
        if not os.path.isdir(cwd):
            cwd = self.repo if os.path.isdir(self.repo) else HOME
        env = dict(os.environ)
        for k, v in (p.spec.get("env") or {}).items():
            env[str(k)] = str(v)
        env["HANGAR_SVC"] = p.name
        self._rotate(p)
        p.log = open(p.logpath(), "ab", buffering=0)
        what = f"restart {p.restarts}" if p.restarts else "starting"
        p.log.write(f"\n==> {time.strftime('%Y-%m-%dT%H:%M:%S')} {what}: {p.spec['command']}\n".encode())
        r, w = os.pipe()
        try:
            p.proc = subprocess.Popen(["bash", "-lc", p.spec["command"]], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                      stdout=w, stderr=w, start_new_session=True, close_fds=True)
        except OSError as e:
            os.close(r)
            os.close(w)
            p.log.write((f"could not start: {e}\n").encode())
            p.state, p.last_exit, p.proc = "failed", None, None
            return
        os.close(w)
        os.set_blocking(r, False)
        p.out = r
        self.sel.register(r, selectors.EVENT_READ, ("out", p))
        p.state, p.ready, p.started_at, p.stopping = "starting", False, _now(), None
        p.next_check = 0.0

    def _pump(self, p):
        try:
            data = os.read(p.out, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._close_out(p)
            return
        if p.log:
            try:
                p.log.write(data)
            except OSError:
                pass
            if p.log.tell() > LOG_MAX:
                self._rotate(p, reopen=True)

    def _close_out(self, p):
        if p.out is not None:
            try:
                self.sel.unregister(p.out)
            except (KeyError, ValueError):
                pass
            os.close(p.out)
            p.out = None

    def _rotate(self, p, reopen=False):
        path = p.logpath()
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass
        if reopen and p.log:
            p.log.close()
            p.log = open(path, "ab", buffering=0)

    def _reap(self, p, now):
        if p.proc is None or p.proc.poll() is None:
            return
        while p.out is not None:  # what it wrote last
            before = p.out
            self._pump(p)
            if p.out is before:
                break
        self._close_out(p)
        code = p.proc.returncode
        ran = now - (p.started_at or now)
        p.proc, p.exited_at, p.last_exit, p.ready = None, now, code, False
        if p.log:
            p.log.write(f"==> exited with {code} after {int(ran)}s\n".encode())
            p.log.close()
            p.log = None
        self._kill_ready(p)
        if p.stopping is not None or not p.wanted:
            p.state, p.stopping = "stopped", None
            return
        if p.once or (p.spec.get("restart") == "on-failure" and code == 0):
            p.state = "exited"
            p.wanted = False
            return
        p.fails = 0 if ran >= STABLE else p.fails + 1
        if p.fails >= GIVE_UP:
            p.state = "failed"
            p.wanted = False
            self._log(f"{p.name}: exited {GIVE_UP} times in a row within {int(STABLE)}s; not restarting it")
            return
        p.restarts += 1
        p.state = "backoff"
        p.next_start = now + min(BACKOFF_MAX, 2 ** min(p.fails, 6) * 0.5)

    def _kill_ready(self, p):
        if p.ready_proc is not None:
            if p.ready_proc.poll() is None:
                try:
                    os.killpg(p.ready_proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.ready_proc.wait()
            p.ready_proc = None

    def _check_ready(self, p, now):
        if p.proc is None or p.kind == "start":
            return
        port, cmd = p.spec.get("port"), p.spec.get("ready")
        if not port and not cmd:
            if p.state == "starting":
                p.state, p.ready = "running", True
            return
        if p.ready_proc is not None:  # a `ready` command in flight
            rc = p.ready_proc.poll()
            if rc is None and now - p.ready_started < READY_TIMEOUT:
                return
            self._kill_ready(p)
            self._mark(p, rc == 0, now)
            return
        if now < p.next_check:
            return
        if cmd:
            p.ready_started = now
            p.ready_proc = subprocess.Popen(["bash", "-lc", cmd], cwd=self.repo if os.path.isdir(self.repo) else HOME,
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True)
            return
        self._mark(p, _port_open("127.0.0.1", port), now)

    def _mark(self, p, ok, now):
        p.ready = ok
        if p.state in ("starting", "running"):
            p.state = "running" if ok else "starting"
        p.next_check = now + (15.0 if ok else 1.0)

    def _kill(self, p, sig):
        if p.proc is not None:
            try:
                os.killpg(p.proc.pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                p.proc.send_signal(sig)

    def _stop(self, p, wait=False):
        p.wanted = False
        if p.proc is None:
            if p.state in ("backoff", "waiting", "failed"):
                p.state = "stopped"
            return
        p.stopping = _now()
        self._kill(p, signal.SIGTERM)
        if wait:
            deadline = _now() + STOP_GRACE
            while p.proc.poll() is None and _now() < deadline:
                time.sleep(0.1)
            if p.proc.poll() is None:
                self._kill(p, signal.SIGKILL)
                p.proc.wait()
            self._reap(p, _now())

    def _save(self):
        try:
            _write_json(STATE, {"pid": os.getpid(), "updated_at": _now(), "start": self.start_phase,
                                "programs": [p.view() for p in self.programs.values()]})
        except OSError:
            pass

    # ------------------------------------------------------------------ requests
    def _accept(self, srv):
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2)
            buf = b""
            try:
                while b"\n" not in buf and len(buf) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode() or "{}")
                resp = self.handle(req)
            except (OSError, ValueError) as e:
                resp = {"error": f"bad request: {e}"}
            try:
                conn.sendall(json.dumps(resp).encode() + b"\n")
            except OSError:
                pass

    def _targets(self, req):
        names = req.get("names") or []
        if req.get("all"):
            return [p for p in self.programs.values() if p.kind != "start"], []
        found = [self.programs[n] for n in names if n in self.programs]
        return found, [n for n in names if n not in self.programs]

    def handle(self, req):
        op = req.get("op")
        if op == "status":
            return {"programs": [p.view() for p in self.programs.values()], "start": self.start_phase,
                    "services": [dict(s, ready=self._service_ready(s)) for s in self.services]}
        if op == "reload":
            self.load()
            return {"ok": True}
        if op == "add":
            spec = req.get("spec") or {}
            if spec.get("compose") and _valid_name(spec.get("name")):  # --- heavy
                if not isinstance(spec["compose"], str) or "\n" in spec["compose"] or "\0" in spec["compose"]:
                    return {"error": "--compose takes a file name"}
                spec = compose_spec(spec)
            if not _valid_name(spec.get("name")) or not spec.get("command"):
                return {"error": "NAME must be lowercase letters, digits and '-', starting with a letter"}
            if spec["name"] in self.programs and self.programs[spec["name"]].kind != "added":
                return {"error": "{} is the environment's; pick another name".format(spec["name"])}
            added = [a for a in _read_json(ADDED, []) if a.get("name") != spec["name"]] + [spec]
            _write_json(ADDED, added)
            old = self.programs.get(spec["name"])
            if old is not None:
                self._stop(old, wait=True)
            self.programs[spec["name"]] = p = Program(spec, "added")
            p.wanted = True
            if self.start_phase == "done" or spec.get("compose"):  # --- heavy: a stack doesn't wait for `start`
                self._spawn(p)
            return {"ok": True}
        if op == "rm":
            name = req.get("name")
            p = self.programs.get(name)
            if p is None or p.kind != "added":
                return {"error": f"{name} is not a program you added"}
            self._stop(p, wait=True)
            del self.programs[name]
            _write_json(ADDED, [a for a in _read_json(ADDED, []) if a.get("name") != name])
            return {"ok": True}
        if op == "run-start":
            p = self.programs.get("start")
            if p is None:
                return {"error": "the environment has no `start` command"}
            if p.proc is not None:
                return {"error": "start is still running"}
            p.wanted = True
            self._spawn(p)
            return {"ok": True}
        if op in ("start", "stop", "restart"):
            progs, unknown = self._targets(req)
            if unknown:
                return {"error": "no program named {} (see `hangar-agent svc status`)".format(", ".join(unknown))}
            for p in progs:
                if op in ("stop", "restart"):
                    self._stop(p, wait=op == "restart")
                if op in ("start", "restart"):
                    p.wanted, p.fails = True, 0
                    if p.proc is None:
                        if self.start_phase == "done" or p.kind == "start" or p.spec.get("compose"):  # --- heavy
                            self._spawn(p)
                        else:
                            p.state = "waiting"
            return {"ok": True, "names": [p.name for p in progs]}
        return {"error": "unknown request"}


# ====================================================================== starting the daemon
def _call(req, timeout=15.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode())
    finally:
        s.close()


def _alive():
    try:
        _call({"op": "status"}, timeout=2)
        return True
    except (OSError, ValueError):
        return False


def _no_new_privs():
    """no_new_privs on the daemon, inherited by all it starts. hangar starts it from an exec outside any turn, so the
    flag the agent's harness runs under (docs/steering.md, the uid split) would otherwise not hold for the programs
    the agent hands it (`svc add`): a setuid or file-capability program would gain privileges there. Linux only;
    the daemon doesn't start without it."""
    if not sys.platform.startswith("linux"):
        return
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")


def ensure():
    """The daemon, started (detached, in its own session) if it isn't running. Two at once: the lock picks one."""
    if _alive():
        return True
    os.makedirs(LOGS, exist_ok=True)
    os.chmod(DIR, 0o700)
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            if os.fork():
                os._exit(0)
            fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                os._exit(0 if e.errno in (errno.EAGAIN, errno.EACCES) else 1)
            os.closerange(3, fd)  # nothing of the caller's (an exec's pipes) stays open in the daemon
            os.closerange(fd + 1, 4096)
            null = os.open(os.devnull, os.O_RDWR)
            log = os.open(DAEMON_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.dup2(null, 0)
            os.dup2(log, 1)
            os.dup2(log, 2)
            for fd_ in (null, log):
                os.close(fd_)
            os.chdir(HOME)
            _no_new_privs()
            Daemon().serve(fd)
        except Exception as e:  # noqa: BLE001 - the daemon's own log says why it stopped
            sys.stderr.write(f"supervisor failed: {e!r}\n")
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    deadline = _now() + 5
    while _now() < deadline:
        if _alive():
            return True
        time.sleep(0.05)
    return False


# ====================================================================== the CLI
def _die(msg, code=2):
    sys.stderr.write(f"hangar-agent svc: {msg}\n")
    sys.exit(code)


def _need():
    if not ensure():
        _die(f"the supervisor didn't start; see {DAEMON_LOG}", 1)


def _req(req):
    _need()
    resp = _call(req)
    if resp.get("error"):
        _die(resp["error"], 1)
    return resp


def _status_rows(st, names):
    rows = []
    for p in st.get("programs") or []:
        if names and p["name"] not in names:
            continue
        state = p["state"]
        if p["kind"] == "start" and state == "exited":
            state = "done" if p.get("last_exit") == 0 else "failed (exit {})".format(p.get("last_exit"))
        elif state in ("backoff", "exited", "failed") and p.get("last_exit") is not None:
            state += " (exit {})".format(p["last_exit"])
        ready = "-" if p["kind"] == "start" else ("yes" if p["ready"] else "no")
        rows.append((p["name"], p["kind"], state, ready, str(p.get("pid") or "-"), str(p.get("restarts") or 0),
                     _ago(p.get("started_at")) if p.get("pid") else "-", str(p.get("port") or "-"),
                     p["log"].replace(HOME, "~")))
    for s in st.get("services") or []:
        if names and s["name"] not in names:
            continue
        rows.append((s["name"], "service", "running" if s["ready"] else "not answering", "yes" if s["ready"] else "no",
                     "-", "-", "-", "{}:{}".format(s.get("host") or "127.0.0.1", s.get("port")), "-"))
    return rows


def _print_status(st, names, as_json):
    if as_json:
        out = {"start": st.get("start"), "programs": [], "services": []}
        for p in st.get("programs") or []:
            if not names or p["name"] in names:
                out["programs"].append(p)
        for s in st.get("services") or []:
            if not names or s["name"] in names:
                out["services"].append(s)
        print(json.dumps(out, indent=1))
        return
    rows = [("NAME", "KIND", "STATE", "READY", "PID", "RESTARTS", "UP", "PORT", "LOG")] + _status_rows(st, names)
    if len(rows) == 1:
        print("Nothing is declared: the repo's .hangar/environment.yaml has no start, terminals or services. "
              "Supervise a command of your own with `hangar-agent svc add NAME -- COMMAND`.")
        return
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    for r in rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(len(widths))) + "  " + r[-1])
    if st.get("start") == "waiting-services":
        down = [s["name"] for s in st.get("services") or [] if not s["ready"]]
        print(f"\nwaiting for {', '.join(down) or 'the services'} to answer (up to {int(SERVICE_WAIT)}s), then "
              "`start` and the terminals")


def _pending(st, names):
    """(names not ready yet, names that failed) among `names` (default: what starts by itself)."""
    waiting, failed = [], []
    for p in st.get("programs") or []:
        if names and p["name"] not in names:
            continue
        if not names and p["state"] == "stopped":
            continue
        if p["state"] == "exited":  # a one-shot (start, --once) that ended: done, or failed
            if p.get("last_exit") != 0:
                failed.append(p["name"])
        elif p["state"] in ("failed", "stopped"):
            failed.append(p["name"])
        elif p["kind"] == "start" or not p["ready"]:
            waiting.append(p["name"])
    for s in st.get("services") or []:
        if (not names or s["name"] in names) and not s["ready"]:
            waiting.append(s["name"])
    return waiting, failed


def _opts(args, flags, valued):
    """A tiny argv parser: returns (positional, options)."""
    pos, opts = [], {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a in valued:
            if i + 1 >= len(args):
                _die(f"{a} needs a value")
            opts[a] = args[i + 1]
            i += 2
            continue
        if a in flags:
            opts[a] = True
        elif a.startswith("-") and a != "-":
            _die(f"unknown option {a} (see hangar-agent svc --help)")
        else:
            pos.append(a)
        i += 1
    return pos, opts


def cmd_logs(args):
    pos, opts = _opts(args, {"-f", "--follow"}, {"-n", "--lines"})
    if len(pos) != 1:
        _die("usage: hangar-agent svc logs NAME [-n LINES] [-f]")
    path = os.path.join(LOGS, pos[0] + ".log")
    if not _valid_name(pos[0]) or not os.path.exists(path):
        _die(f"no log for {pos[0]} (see `hangar-agent svc status`)", 1)
    n = opts.get("-n") or opts.get("--lines") or "100"
    if not str(n).isdigit():
        _die("-n takes a number of lines")
    argv = ["tail", "-n", str(n)] + (["-F"] if opts.get("-f") or opts.get("--follow") else []) + [path]
    os.execvp("tail", argv)


def main(argv=None):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `svc status --json | head` ends quietly
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args.pop(0) if args else "help"
    if cmd in ("help", "-h", "--help"):
        print(USAGE, end="")
        return 0
    if "-h" in args or "--help" in args:
        print(USAGE, end="")
        return 0
    if cmd == "up":
        _need()
        return 0
    if cmd == "status":
        pos, opts = _opts(args, {"--json"}, set())
        _print_status(_req({"op": "status"}), pos, opts.get("--json"))
        return 0
    if cmd == "wait":
        pos, opts = _opts(args, set(), {"--timeout", "-t"})
        t = opts.get("--timeout") or opts.get("-t") or "300"
        try:
            deadline = _now() + float(t)
        except ValueError:
            _die("--timeout takes seconds")
        while True:
            st = _req({"op": "status"})
            known = {p["name"] for p in st.get("programs") or []} | {s["name"] for s in st.get("services") or []}
            if unknown := [n for n in pos if n not in known]:
                _die("no program named {}".format(", ".join(unknown)), 1)
            waiting, failed = _pending(st, pos)
            if failed or not waiting or _now() >= deadline:
                _print_status(st, pos, False)
                if failed:
                    print("\nfailed: {} (see `hangar-agent svc logs NAME`)".format(", ".join(failed)))
                elif waiting:
                    print("\nstill not ready after {}s: {}".format(t, ", ".join(waiting)))
                return 1 if failed or waiting else 0
            time.sleep(1)
    if cmd == "logs":
        return cmd_logs(args)
    if cmd in ("start", "stop", "restart"):
        pos, opts = _opts(args, {"--all"}, set())
        if not pos and not opts.get("--all"):
            _die(f"usage: hangar-agent svc {cmd} NAME... | --all")
        resp = _req({"op": cmd, "names": pos, "all": bool(opts.get("--all"))})
        print("{}: {}".format(cmd, ", ".join(resp.get("names") or []) or "nothing to do"))
        return 0
    if cmd == "add":
        pos, opts = _opts(args, {"--once"}, {"--port", "--ready", "--cwd", "--compose"})
        if opts.get("--compose"):  # --- heavy: a Compose stack
            if len(pos) != 1 or opts.get("--cwd") or opts.get("--once"):
                _die("usage: hangar-agent svc add NAME --compose FILE [--port P] [--ready CMD]")
            spec = {"name": pos[0], "compose": opts["--compose"]}
        elif len(pos) < 2:
            _die("usage: hangar-agent svc add NAME [--port P] [--ready CMD] [--cwd DIR] [--once] -- COMMAND...")
        else:
            spec = {"name": pos[0], "command": " ".join(pos[1:]) if len(pos) > 2 else pos[1]}
        if opts.get("--port"):
            if not str(opts["--port"]).isdigit():
                _die("--port takes a number")
            spec["port"] = int(opts["--port"])
        if opts.get("--ready"):
            spec["ready"] = opts["--ready"]
        if opts.get("--cwd"):
            spec["cwd"] = opts["--cwd"]
        if opts.get("--once"):
            spec["once"] = True
        _req({"op": "add", "spec": spec})
        print(f"{pos[0]}: supervised; `hangar-agent svc status {pos[0]}` shows it")
        return 0
    if cmd == "rm":
        if len(args) != 1:
            _die("usage: hangar-agent svc rm NAME")
        _req({"op": "rm", "name": args[0]})
        print(f"{args[0]}: stopped and removed")
        return 0
    if cmd == "run-start":
        _req({"op": "run-start"})
        print("start: running; `hangar-agent svc logs start -f` follows it")
        return 0
    if cmd == "reload":
        _req({"op": "reload"})
        return 0
    if cmd == "compose-ready":  # --- heavy: a stack's default readiness check
        if len(args) != 2:
            _die("usage: hangar-svc compose-ready NAME FILE")
        return compose_ready(args[0], args[1])
    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
