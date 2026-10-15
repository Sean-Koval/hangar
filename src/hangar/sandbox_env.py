#!/usr/bin/env python3
"""hangar-env: an agent inspects its own environment and reports what blocks it (docs/computer-use.md).

Installed into every sandbox as ~/.hangar/bin/hangar-env (stdlib only, Python 3.8+) and reached through
`hangar-agent env ...`. It reads ~/.hangar/envstatus.json, which hangar writes at claim and at the start of each
turn (never a secret value: names, versions, health checks and the egress policy), checks what it can from inside
the sandbox, and files requests for the owner as plain files under $HANGAR_IO/out/requests/, which hangar picks up
while the turn runs. Nothing here is granted automatically: a request waits for a person.

    hangar-env doctor [--host HOST[:PORT]]... [--json]
    hangar-env request secret NAME --why TEXT
    hangar-env request egress HOST[:PORT] --why TEXT
    hangar-env requests
    hangar-env mcp            a read-only stdio MCP server with the same report (rendered into the harness config)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
STATUS = os.path.join(HOME, ".hangar", "envstatus.json")
ENV_FILE = os.path.join(HOME, ".hangar", "env")
SVC = os.path.join(HOME, ".hangar", "bin", "hangar-svc")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}"
                     r"[a-z0-9_])?)*$")
MAX_WHY = 2000
PROBE_TIMEOUT = 5.0
CHECK_TIMEOUT = 10.0
VERSION = 1


class Usage(Exception):
    pass


# ====================================================================== status and helpers
def load_status() -> dict:
    """What hangar wrote about this sandbox; {} when it wrote nothing (a sandbox from before this version)."""
    try:
        with open(STATUS, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def io_dir(status: dict) -> str | None:
    """The turn's exchange directory: $HANGAR_IO, else the one hangar recorded (MCP servers may not inherit env)."""
    return os.environ.get("HANGAR_IO") or status.get("io") or None


def parse_host(text: str) -> tuple[str, int | None]:
    """'Example.com', 'example.com:8443', '[::1]:443' or 'https://example.com/x' -> (host, port)."""
    t = (text or "").strip()
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0].split("@")[-1]
    port: int | None = None
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        if rest.startswith(":"):
            port = _port(rest[1:])
    elif t.count(":") == 1:
        host, p = t.split(":")
        port = _port(p)
    else:
        host = t
    host = host.lower().rstrip(".")
    if not host or not (HOST_RE.match(host) or _is_ip(host)):
        raise Usage(f"'{text}' is not a host name or IP address")
    return host, port


def _port(p: str) -> int:
    if not p.isdigit() or not 0 < int(p) < 65536:
        raise Usage(f"'{p}' is not a port number")
    return int(p)


def _is_ip(host: str) -> bool:
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, host)
            return True
        except (OSError, ValueError):
            continue
    return False


def _env_names() -> set[str]:
    """Variable names this process has, plus those ~/.hangar/env sets (an MCP server may get a trimmed env)."""
    names = set(os.environ)
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", ln)
                if m:
                    names.add(m.group(1))
    except OSError:
        pass
    return names


def _check(status: str, name: str, message: str, **data) -> dict:
    return {"name": name, "status": status, "message": message, **({"data": data} if data else {})}


def _tcp(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> str | None:
    """None when a TCP connection opens, else why it didn't."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except socket.gaierror as e:
        return f"DNS lookup failed ({e.strerror or e})"
    except OSError as e:
        return str(e.strerror or e) or type(e).__name__


# ====================================================================== the checks
def check_build(st: dict) -> dict:
    b = st.get("build")
    if not isinstance(b, dict) or not b:
        return _check("unknown", "build", "hangar recorded no build version for this sandbox")
    version = b.get("version") or "unknown"
    if b.get("fallback"):
        why = b.get("reason") or "the environment's build failed"
        return _check("fail", "build", f"running a FALLBACK image, not this environment's build ({why}). "
                                       "Tools, dependencies or services the environment sets up may be missing.",
                      version=version, fallback=True, reason=why)
    note = "; a rebuild is running because the repo's config moved on" if b.get("stale") else ""
    return _check("warn" if b.get("stale") else "ok", "build", f"environment build {version}{note}",
                  version=version, fallback=False, stale=bool(b.get("stale")))


def _supervised() -> dict[str, dict]:
    """Live state from the service supervisor, when the sandbox has one (`hangar-svc status --json`): its
    `programs` (terminals, the start step, added ones) and `services` (sidecars), by name; `ready` is health."""
    if not os.access(SVC, os.X_OK):
        return {}
    try:
        out = subprocess.run([SVC, "status", "--json"], capture_output=True, text=True, timeout=CHECK_TIMEOUT)
        data = json.loads(out.stdout or "null")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    if not isinstance(data, dict):
        return {}
    items = [*(data.get("services") or []), *(data.get("programs") or [])]  # a program's entry (with its log) wins
    return {str(s.get("name")): s for s in items if isinstance(s, dict) and s.get("name")}


def check_services(st: dict) -> dict:
    declared = [s for s in st.get("services") or [] if isinstance(s, dict) and s.get("name")]
    if not declared:
        return _check("ok", "services", "this environment declares no services or ports")
    live = _supervised()
    bad, good = [], []
    for s in declared:
        name, port = str(s["name"]), s.get("port")
        host = s.get("host") or "127.0.0.1"
        why = None
        if s.get("check"):
            try:
                r = subprocess.run(["sh", "-c", str(s["check"])], capture_output=True, text=True,
                                   timeout=CHECK_TIMEOUT)
                if r.returncode != 0:
                    why = f"check `{s['check']}` exited {r.returncode}: {(r.stderr or r.stdout).strip()[-200:]}"
            except subprocess.TimeoutExpired:
                why = f"check `{s['check']}` took over {CHECK_TIMEOUT:g}s"
        elif isinstance(port, int):
            if (err := _tcp(host, port, 2.0)) is not None:
                why = f"nothing answers on {host}:{port} ({err})"
        sup = live.get(name) or {}
        state = sup.get("state")
        if why is None and sup.get("ready") is False:
            why = f"the supervisor reports it not ready ({state or 'no state'})"
        elif why is None and state in ("failed", "backoff", "exited", "stopped"):
            why = f"the supervisor reports it {state}"
        entry = {"name": name, "kind": s.get("kind") or "service", "port": port, "host": host}
        if state:
            entry["state"] = state
        if why:
            entry["problem"] = why
            if sup.get("log"):
                entry["log"] = sup["log"]
            bad.append(entry)
        else:
            good.append(entry)
    if bad:
        names = ", ".join(e["name"] for e in bad)
        return _check("fail", "services", f"unhealthy: {names}. Run `hangar-agent svc status` and read its logs "
                                          "(or start it yourself); report it with `hangar-agent ask` only if it "
                                          "can't be fixed from here.", unhealthy=bad, healthy=good)
    return _check("ok", "services", f"{len(good)} healthy", healthy=good)


def check_secrets(st: dict) -> dict:
    expected = [s for s in st.get("secrets") or [] if isinstance(s, dict) and s.get("name")]
    have = _env_names()
    missing = [s for s in expected if s["name"] not in have]
    skipped = [w for w in st.get("warnings") or [] if isinstance(w, str) and "needs" in w]
    if missing:
        names = ", ".join(s["name"] for s in missing)
        return _check("fail", "secrets", f"missing: {names}. Ask for one with `hangar-agent env request secret "
                                         "<NAME> --why '...'`; never ask for or print a value.",
                      missing=[{"name": s["name"], "for": s.get("for")} for s in missing], warnings=skipped)
    if skipped:
        return _check("warn", "secrets", "; ".join(skipped), warnings=skipped)
    return _check("ok", "secrets", f"all {len(expected)} expected names are set" if expected
                  else "this environment expects no secrets")


def check_setup(st: dict) -> dict:
    fails = [f for f in st.get("setup_failures") or [] if isinstance(f, dict)][-10:]
    if not fails:
        return _check("ok", "setup", "no setup failures recorded")
    last = fails[-1]
    return _check("fail", "setup", f"{len(fails)} recent setup failure(s); latest in {last.get('step', '?')}: "
                                   f"{str(last.get('message', ''))[:300]}", failures=fails)


def check_computer(st: dict) -> dict | None:
    c = st.get("computer")
    if not c:
        return None
    tools = {t: bool(shutil.which(t)) for t in ("Xvfb", "x11vnc", "ffmpeg")}
    shm = _shm_mb()
    notes = []
    if not all(tools.values()):
        notes.append("missing " + ", ".join(t for t, ok in tools.items() if not ok))
    if shm is not None and shm < 512:
        notes.append(f"/dev/shm is {shm} MB: Chromium runs with --disable-dev-shm-usage")
    if st.get("runtime") == "gvisor":
        notes.append("gVisor: Chromium's own sandbox is off (gVisor is the boundary); expect it 1.5-2x slower")
    status = "fail" if not all(tools.values()) else "ok"
    msg = "computer: " + ("; ".join(notes) if notes else f"display {c.get('display', ':99')} ready to start")
    return _check(status, "computer", msg, tools=tools, shm_mb=shm)


def _shm_mb() -> int | None:
    try:
        s = os.statvfs("/dev/shm")
        return int(s.f_frsize * s.f_blocks / (1 << 20))
    except OSError:
        return None


def _matches(host: str, pattern: str) -> bool:
    p = pattern.lower().strip().rstrip(".")
    if p.startswith("*."):
        return host.endswith(p[1:])
    if p.startswith("."):
        return host == p[1:] or host.endswith(p)
    return host == p.split(":")[0]


def _no_proxy(host: str) -> bool:
    for p in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(","):
        p = p.strip().lower()
        if p == "*" or (p and (host == p.lstrip(".") or host.endswith("." + p.lstrip(".")))):
            return True
    return False


def probe_host(st: dict, target: str) -> dict:
    """Whether this sandbox can reach host[:port] under its egress policy: what the policy says, then a real
    connection (through the egress proxy when there is one), so 'blocked by policy' is told apart from 'down'."""
    host, port = parse_host(target)
    port = port or 443
    eg = st.get("egress") if isinstance(st.get("egress"), dict) else {}
    mode, enforced = eg.get("mode") or "open", bool(eg.get("enforced"))
    rule = next((p for p in eg.get("allow") or [] if isinstance(p, str) and _matches(host, p)), None)
    policy = {"mode": mode, "enforced": enforced, "rule": rule,
              "allowed": None if mode == "open" else (rule is not None and mode != "none")}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or eg.get("proxy")
    out = {"host": host, "port": port, "policy": policy}
    if proxy and not _no_proxy(host):
        out["via"] = "proxy"
        code, err = _connect_via(proxy, host, port)
        if code == 200:
            out.update(reachable=True, verdict="reachable", detail="the egress proxy opened a tunnel")
        elif code in (403, 407):
            out.update(reachable=False, verdict="blocked by policy",
                       detail=f"the egress proxy refused it ({code}). Request it with `hangar-agent env request "
                              f"egress {host}:{port} --why '...'`")
        else:
            out.update(reachable=False, verdict="unreachable", detail=err or f"the egress proxy answered {code}")
        return out
    out["via"] = "direct"
    err = _tcp(host, port)
    if err is None:
        out.update(reachable=True, verdict="reachable", detail="a TCP connection opened")
    elif policy["allowed"] is False and enforced:
        out.update(reachable=False, verdict="blocked by policy",
                   detail=f"{err}; the egress policy ({mode}) has no rule for it. Request it with "
                          f"`hangar-agent env request egress {host}:{port} --why '...'`")
    else:
        out.update(reachable=False, verdict="dns failed" if err.startswith("DNS") else "unreachable", detail=err)
    return out


def _connect_via(proxy: str, host: str, port: int) -> tuple[int | None, str | None]:
    """HTTP CONNECT through the proxy: (status code, None) or (None, why it failed)."""
    p = proxy.split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
    phost, _, pport = p.rpartition(":") if ":" in p else (p, "", "")
    try:
        with socket.create_connection((phost or p, int(pport or 80)), timeout=PROBE_TIMEOUT) as s:
            target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            line = s.makefile("rb").readline(1024).decode("latin-1")
    except (OSError, ValueError) as e:
        return None, f"could not reach the egress proxy {phost}: {e}"
    m = re.match(r"HTTP/\d(?:\.\d)?\s+(\d{3})", line)
    return (int(m.group(1)), None) if m else (None, f"the egress proxy answered {line.strip()[:80]!r}")


def doctor(hosts: list[str] | None = None) -> dict:
    st = load_status()
    checks = [check_build(st), check_services(st), check_secrets(st), check_setup(st)]
    if (c := check_computer(st)) is not None:
        checks.append(c)
    report: dict = {"v": VERSION, "ok": all(c["status"] != "fail" for c in checks), "checks": checks,
                    "environment": st.get("environment"), "sandbox": st.get("sandbox"),
                    "requests": [r for r in st.get("requests") or [] if isinstance(r, dict)]}
    if not st:
        report["checks"].insert(0, _check("warn", "status", f"{STATUS} is missing: hangar has not described this "
                                                             "sandbox (an older sandbox, or not run by hangar)"))
    elif isinstance(st.get("generated_at"), (int, float)):
        report["status_age_seconds"] = int(time.time() - st["generated_at"])
    if hosts:
        report["hosts"] = [probe_host(st, h) for h in hosts]
    return report


def render(report: dict) -> str:
    mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "unknown": "??  "}
    lines = [f"hangar env doctor: {report.get('environment') or '?'} ({report.get('sandbox') or '?'})"]
    for c in report["checks"]:
        lines.append(f"[{mark.get(c['status'], c['status'])}] {c['name']}: {c['message']}")
        for e in (c.get("data") or {}).get("unhealthy") or []:
            lines.append(f"         - {e['name']}: {e['problem']}")
    for h in report.get("hosts") or []:
        pol = h["policy"]
        rule = f", rule {pol['rule']}" if pol.get("rule") else ""
        lines.append(f"[{'ok  ' if h['reachable'] else 'FAIL'}] {h['host']}:{h['port']}: {h['verdict']} via "
                     f"{h['via']} (policy {pol['mode']}{'' if pol['enforced'] else ', not enforced'}{rule}): "
                     f"{h['detail']}")
    lines += request_lines(report.get("requests") or [])
    return "\n".join(lines)


def request_lines(reqs: list) -> list[str]:
    out = []
    for r in reqs:
        what = r.get("name") or (f"{r.get('host')}:{r['port']}" if r.get("port") else r.get("host"))
        out.append(f"[req ] {r.get('id')}: {r.get('kind')} {what} is {r.get('state')}"
                   + (f" ({r['note']})" if r.get("note") else ""))
    return out


# ====================================================================== requests
def file_request(kind: str, target: str, why: str) -> dict:
    """Write a request for the owner where hangar picks it up. Returns what was filed (or found pending)."""
    why = " ".join((why or "").split())
    if not why:
        raise Usage("--why is required: say what you need it for, so the owner can decide")
    if len(why) > MAX_WHY:
        raise Usage(f"--why is longer than {MAX_WHY} characters")
    if kind == "secret":
        if not NAME_RE.match(target or ""):
            raise Usage(f"'{target}' is not an environment variable name")
        rec: dict = {"kind": "secret", "name": target}
    elif kind == "egress":
        host, port = parse_host(target)
        rec = {"kind": "egress", "host": host, "port": port}
    else:
        raise Usage("request what? `secret NAME` or `egress HOST[:PORT]`")
    st = load_status()
    for r in st.get("requests") or []:
        if (isinstance(r, dict) and r.get("state") == "pending" and r.get("kind") == kind
                and all(r.get(k) == v for k, v in rec.items() if k != "kind")):
            return {**r, "duplicate": True}
    waiting = sum(1 for r in st.get("requests") or [] if isinstance(r, dict) and r.get("state") == "pending")
    if waiting >= int(st.get("request_limit") or 5):  # hangar ignores more anyway; say so now
        raise Usage(f"{waiting} requests already wait for the owner. Keep working on what they don't block; ask "
                    "again once one is answered (`hangar-agent env requests`).")
    io = io_dir(st)
    if not io:
        raise Usage("requests reach the owner from a hangar job or session turn, and this isn't one "
                    "(HANGAR_IO is not set)")
    d = os.path.join(io, "out", "requests")
    os.makedirs(d, exist_ok=True)
    for f in sorted(os.listdir(d)):
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(old, dict) and all(old.get(k) == v for k, v in rec.items()):
            return {**old, "state": "pending", "duplicate": True}
    local = f"{int(time.time())}-{secrets.token_hex(4)}"
    rec.update(local=local, why=why, ts=time.time())
    tmp = os.path.join(d, f".{local}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(tmp, os.path.join(d, f"{local}.json"))
    return {**rec, "state": "pending"}


def describe(rec: dict) -> str:
    what = rec.get("name") or (f"{rec.get('host')}:{rec['port']}" if rec.get("port") else rec.get("host"))
    if rec.get("duplicate"):
        return (f"hangar-env: {rec['kind']} {what} is already requested ({rec.get('id') or 'waiting to be picked up'}"
                f", {rec.get('state', 'pending')}). Keep working on what it doesn't block.")
    return (f"hangar-env: requested {rec['kind']} {what}. The owner decides; nothing is granted automatically. Keep "
            "working on everything this doesn't block; `hangar-agent env requests` shows the answer when it comes.")


# ====================================================================== MCP (stdio, read-only resources)
TOOLS = [
    {"name": "env_doctor", "title": "Diagnose this sandbox",
     "description": "Report this sandbox's environment: build version and whether it is a fallback, unhealthy "
                    "services or ports, missing secret names (never values), recent setup failures, pending "
                    "requests, and, for each host given, whether it is reachable under the egress policy.",
     "inputSchema": {"type": "object", "properties": {"hosts": {"type": "array", "items": {"type": "string"},
                                                                "description": "host or host:port to probe"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": True}},
    {"name": "env_request", "title": "Ask the owner for a secret or an egress host",
     "description": "File a request with the owner for a missing secret (by NAME, never a value) or for a host "
                    "the egress policy blocks. Never granted automatically: keep working on everything else.",
     "inputSchema": {"type": "object", "required": ["kind", "target", "why"], "properties": {
         "kind": {"type": "string", "enum": ["secret", "egress"]},
         "target": {"type": "string", "description": "the env var NAME, or HOST[:PORT]"},
         "why": {"type": "string", "description": "what you need it for"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
]
RESOURCES = [
    {"uri": "hangar-env://doctor", "name": "doctor", "mimeType": "application/json",
     "description": "The environment report (env_doctor without hosts)."},
    {"uri": "hangar-env://status", "name": "status", "mimeType": "application/json",
     "description": "What hangar recorded about this sandbox: build, services, expected secret names, egress."},
    {"uri": "hangar-env://requests", "name": "requests", "mimeType": "application/json",
     "description": "Requests filed with the owner and their answers."},
]
TEMPLATES = [{"uriTemplate": "hangar-env://egress/{host}", "name": "egress", "mimeType": "application/json",
              "description": "Whether host[:port] is reachable under the egress policy."}]
# The handshake-era versions. A 2026-07-28 client probes `server/discover` first; this server answers "no such
# method", which is how a client learns to fall back to `initialize`.
HANDSHAKE = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
INSTRUCTIONS = ("Read hangar-env://doctor when something in the environment doesn't work (a service, a secret, the "
                "network) before working around it, and file blockers with env_request. Nothing is granted "
                "automatically, so keep working on everything a request doesn't block.")


def _text(obj) -> str:
    return json.dumps(obj, indent=2)


def _read(uri: str) -> dict:
    if uri == "hangar-env://doctor":
        body = doctor()
    elif uri == "hangar-env://status":
        body = load_status()
    elif uri == "hangar-env://requests":
        body = load_status().get("requests") or []
    elif uri.startswith("hangar-env://egress/"):
        body = probe_host(load_status(), uri[len("hangar-env://egress/"):])
    else:
        raise LookupError(f"unknown resource {uri}")
    return {"contents": [{"uri": uri, "mimeType": "application/json", "text": _text(body)}]}


def _call(name: str, args: dict) -> dict:
    try:
        if name == "env_doctor":
            hosts = args.get("hosts") or []
            if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts) or len(hosts) > 20:
                raise Usage("hosts must be a list of at most 20 strings")
            rep = doctor(hosts)
            return {"content": [{"type": "text", "text": render(rep)}], "structuredContent": rep,
                    "isError": False}
        if name == "env_request":
            rec = file_request(str(args.get("kind") or ""), str(args.get("target") or ""), str(args.get("why") or ""))
            return {"content": [{"type": "text", "text": describe(rec)}], "isError": False}
    except Usage as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    raise LookupError(f"unknown tool {name}")


def _handle(msg: dict) -> dict | None:
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:  # a notification (initialized, cancelled, ...): nothing to answer
        return None
    caps = {"tools": {}, "resources": {}}
    info = {"name": "hangar-env", "version": str(VERSION)}
    try:
        if method == "initialize":
            want = params.get("protocolVersion")
            res: dict = {"protocolVersion": want if want in HANDSHAKE else HANDSHAKE[-1], "capabilities": caps,
                         "serverInfo": info, "instructions": INSTRUCTIONS}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            res = _call(str(params.get("name")), params.get("arguments") or {})
        elif method == "resources/list":
            res = {"resources": RESOURCES}
        elif method == "resources/templates/list":
            res = {"resourceTemplates": TEMPLATES}
        elif method == "resources/read":
            res = _read(str(params.get("uri") or ""))
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    except (LookupError, Usage) as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e)}}
    except Exception as e:  # noqa: BLE001 - one bad request must not end the server
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def serve(inp=None, out=None) -> None:
    """Newline-delimited JSON-RPC on stdin/stdout (MCP's stdio transport)."""
    inp, out = inp or sys.stdin, out or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            batch = msg if isinstance(msg, list) else [msg]
            answers = [a for m in batch if isinstance(m, dict) and (a := _handle(m)) is not None]
            resp = (answers if isinstance(msg, list) else answers[0]) if answers else None
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()


# ====================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hangar-agent env", description="Inspect this sandbox's environment and "
                                 "ask the owner for what blocks you.")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("doctor", help="report what works and what doesn't")
    d.add_argument("--host", action="append", default=[], help="HOST[:PORT] to probe (repeatable)")
    d.add_argument("--json", action="store_true")
    r = sub.add_parser("request", help="ask the owner for a secret or an egress host (never automatic)")
    r.add_argument("kind", choices=["secret", "egress"])
    r.add_argument("target", help="NAME of the env var, or HOST[:PORT]")
    r.add_argument("--why", required=True, help="what you need it for")
    sub.add_parser("requests", help="requests filed and their answers")
    sub.add_parser("mcp", help="serve the report over MCP on stdio")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "doctor":
            rep = doctor(a.host)
            print(json.dumps(rep, indent=2) if a.json else render(rep))
            return 0 if rep["ok"] and all(h["reachable"] for h in rep.get("hosts") or []) else 1
        if a.cmd == "request":
            print(describe(file_request(a.kind, a.target, a.why)))
            return 0
        if a.cmd == "requests":
            reqs = load_status().get("requests") or []
            print("\n".join(request_lines(reqs)) if reqs else "no requests filed")
            return 0
        if a.cmd == "mcp":
            serve()
            return 0
    except Usage as e:
        print(f"hangar-env: {e}", file=sys.stderr)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
