"""The team release's acceptance checks (Waves 1–3), behind scripts/acceptance/gke.sh: the agent's computer, egress
allowlists, secret scanning, repo environment builds, services, landing, Jira intake, power, a scale smoke test and
the image prepull. docs/acceptance.md is the runbook.

They use hangar's REST and MCP API only (no kubectl), so they run from a laptop, from deploy.yml or from anywhere a
token reaches hangar. They are safe on a production install:

* **Dedicated test environments.** Each check that needs an environment of its own saves one named
  `<prefix>-<check>` (`accept-computer`, ...; --test-env-prefix). They are kept between runs, so their images are
  built once; the checks save them again only when what they need changed.
* **Dedicated repos.** Checks that open PRs or push use --test-repo; the build check commits to --fixture-repo's
  default branch and puts the file back afterwards. Nothing touches another repo.
* **Test people.** The scale and intake checks add people named `accept-<run>-...` and remove them.
* **Cleanup, even on failure.** Everything a check makes is recorded in the run's state file before it is made, and
  `cleanup` (cleanup_first, cleanup_last here) removes it: jobs are cancelled, PRs closed and their branches deleted,
  people removed, repo policy and fixture files put back, pending requests denied, and pools powered back on.

Secrets are read from the environment only, never from flags (a command line is visible to every process):
HANGAR_ACCEPT_GITHUB_TOKEN (writes to the test and fixture repos; else `gh auth token`), HANGAR_ACCEPT_APPROVER_TOKEN
(a second GitHub account that approves the landing check's PR) and HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import string
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import hangar_accept as ha
import httpx
from hangar_accept import LABEL, Api, Fail, Skip, State, pct, say

ROOT = Path(__file__).resolve().parents[2]
AGENT = '"$HOME/.hangar/bin/hangar-agent"'
RESOLUTION = (1280, 800)
TERMINAL_JOB = ("succeeded", "failed", "cancelled")
REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")
POWER_POLL = 10.0  # seconds between looks at the power state while the pools go down or up
POLICY_FIELDS = ("land", "approvers")  # what the landing check sets on the test repo's policy, and puts back
GITHUB_TRANSPORT: httpx.BaseTransport | None = None  # tests put a stand-in GitHub here


# ---------------------------------------------------------------------------- shared helpers
def note(state: State, check: str, **values: Any) -> None:
    """Numbers for the JSON report: merged into the check's `data` as it goes, so a failure keeps what was measured."""
    d = dict(state.get(f"data:{check}") or {})
    for k, v in values.items():
        if k == "timings":
            d["timings"] = {**(d.get("timings") or {}), **{n: round(float(s), 2) for n, s in v.items()}}
        else:
            d[k] = v
    state.set(f"data:{check}", d)


def setting(api: Api, name: str) -> Any:
    """A setting's value as hangar uses it (admin), or None when it can't be read."""
    r = api.http.get(f"/api/settings/{name}")
    return r.json().get("value") if r.status_code == 200 else None


TEST_ENV_DESCRIPTION = "hangar acceptance checks (scripts/acceptance, docs/acceptance.md); kept between runs"


def ensure_env(api: Api, opts: argparse.Namespace, suffix: str, spec: dict[str, Any]) -> str:
    """The dedicated test environment `<prefix>-<suffix>`, saved only when it doesn't hold `spec` already (saving the
    same spec again would be harmless, but each save is an audit entry and may queue an image check). An environment
    of that name that isn't the suite's (its description says whose it is) is never saved over."""
    name = f"{opts.test_env_prefix}-{suffix}"
    want = {"description": TEST_ENV_DESCRIPTION, **spec}
    r = api.http.get(f"/api/environments/{name}")
    if r.status_code in (401, 403):
        raise Fail(f"GET /api/environments/{name}: HTTP {r.status_code}: the checks need an admin token")
    raw = (r.json().get("raw") or {}) if r.status_code == 200 else {}
    if r.status_code == 200 and not str(raw.get("description") or "").startswith(TEST_ENV_DESCRIPTION[:24]):
        raise Fail(f"an environment named {name} exists and isn't one of the suite's ({raw.get('description')!r}): "
                   "pick another --test-env-prefix")
    if r.status_code == 200 and all(raw.get(k) == v for k, v in want.items()):
        return name
    api.call("PUT", f"/api/environments/{name}", json=want)
    say(f"environment {name}: {'updated' if r.status_code == 200 else 'created'} (a test environment, kept)")
    return name


def new_sandbox(api: Api, state: State, opts: argparse.Namespace, env: str, suffix: str, ttl: int = 60,
                **extra: Any) -> tuple[dict[str, Any], float]:
    """A running sandbox named accept-<run>-<suffix>, recorded for cleanup, and the seconds it took."""
    t0 = time.monotonic()
    sb = api.call("POST", "/api/sandboxes", json={"environment": env, "name": f"accept-{state.run}-{suffix}",
                                                  "ttl_minutes": ttl, "labels": {LABEL: state.run}, **extra})[0]
    if "id" not in sb:
        raise Fail(f"a sandbox of {env}: {sb.get('error') or sb}")
    state.add("sandboxes", sb["id"])
    if sb["state"] != "running":
        ha._running(api, sb["id"], opts.boot_timeout)
    return api.call("GET", f"/api/sandboxes/{sb['id']}"), time.monotonic() - t0


def probe(api: Api, sb: str, code: str, *args: Any, timeout: int = 120, cwd: str | None = None) -> dict[str, Any]:
    """Run a Python snippet in the sandbox (python3 is in hangar's image contract) and return the JSON it printed on
    its `HANGAR-ACCEPT ` line."""
    cmd = "python3 -c " + shlex.quote(code) + "".join(" " + shlex.quote(str(a)) for a in args)
    r = api.exec(sb, cmd, cwd=cwd, timeout=timeout)
    line = next((ln for ln in reversed(r["stdout"].splitlines()) if ln.startswith("HANGAR-ACCEPT ")), None)
    if line is None:
        raise Fail(f"the probe in {sb} printed nothing usable (exit {r['exit_code']}): "
                   f"{(r['stderr'] or r['stdout'])[-300:].strip()}")
    return json.loads(line.split(" ", 1)[1])


def wait_job(api: Api, job_id: str, timeout: float, done: Callable[[dict[str, Any]], bool] | None = None
             ) -> dict[str, Any]:
    """Poll a job until `done` (default: a terminal state or a question) and return it."""
    done = done or (lambda j: j["state"] in TERMINAL_JOB or j["state"] == "needs_input")
    deadline, last = time.monotonic() + timeout, None
    while True:
        j = api.call("GET", f"/api/jobs/{job_id}")
        if j["state"] != last:
            say(f"job {job_id}: {j['state']}" + (f" ({j['reason']})" if j.get("reason") else ""))
            last = j["state"]
        if done(j):
            return j
        if time.monotonic() > deadline:
            tail = "; ".join(e.get("message", "") for e in (j.get("events") or [])[-3:])
            raise Fail(f"job {job_id} still {j['state']} after {timeout:.0f}s: {tail[:300]}")
        time.sleep(ha.POLL_SECONDS)


def paid(opts: argparse.Namespace) -> dict[str, Any]:
    """A paid job's budget: one turn, and no more than --max-usd (hangar stops it there)."""
    return {"max_turns": 1, "max_cost_usd": opts.max_usd}


def events_text(job: dict[str, Any]) -> str:
    return "\n".join(str(e.get("message") or "") for e in job.get("events") or [])


# ---------------------------------------------------------------------------- GitHub (test and fixture repos)
def github_token() -> str | None:
    """A token that writes to the test and fixture repos: HANGAR_ACCEPT_GITHUB_TOKEN, else the gh CLI's login."""
    if t := (os.environ.get("HANGAR_ACCEPT_GITHUB_TOKEN") or "").strip():
        return t
    if shutil.which("gh"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
    return None


class GitHubApi:
    """The few GitHub REST calls the landing check makes, with one token (or none, for a public repo's reads)."""

    def __init__(self, token: str | None):
        base = os.environ.get("HANGAR_ACCEPT_GITHUB_API") or "https://api.github.com"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url=base, headers=headers, timeout=30, transport=GITHUB_TRANSPORT)

    def req(self, method: str, path: str, ok: tuple[int, ...] = (200, 201), **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code not in ok:
            try:
                msg = r.json().get("message")
            except (ValueError, AttributeError):
                msg = r.text[:200]
            raise Fail(f"GitHub {method} {path}: HTTP {r.status_code}: {msg}")
        return r.json() if r.content else None

    def close(self) -> None:
        self.http.close()


class FixtureRepo:
    """Commits to a repo's default branch with git (one shallow clone per commit). The token goes in through git's
    environment (GIT_CONFIG_*), never a command line or a URL."""

    def __init__(self, url: str, token: str | None):
        self.url, self.token = url, token

    def _env(self, home: str) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "HOME": home,
               "GIT_AUTHOR_NAME": "hangar acceptance", "GIT_AUTHOR_EMAIL": "acceptance@hangar.invalid",
               "GIT_COMMITTER_NAME": "hangar acceptance", "GIT_COMMITTER_EMAIL": "acceptance@hangar.invalid"}
        if self.token:
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                       GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
        return env

    def _git(self, env: dict[str, str], *args: str, cwd: str | None = None) -> str:
        r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()
            if self.token:
                msg = msg.replace(self.token, "[token]")
            raise Fail(f"git {args[0]} {self.url}: {msg[-300:]}")
        return r.stdout

    def commit(self, files: dict[str, str | None], message: str, expect: dict[str, str | None] | None = None
               ) -> tuple[str, dict[str, str | None]]:
        """Write (or, for None, delete) `files` on the default branch in one commit. Returns the new commit and what
        the files held before (None: absent), which is what restoring them takes. With `expect`, a file is changed
        only while it still holds what `expect` says (what this run wrote): someone else's change since stays."""
        with tempfile.TemporaryDirectory(prefix="hangar-accept-git-") as d:
            env, work = self._env(d), os.path.join(d, "r")
            self._git(env, "clone", "-q", "--depth", "1", self.url, work)
            branch = self._git(env, "rev-parse", "--abbrev-ref", "HEAD", cwd=work).strip()
            old: dict[str, str | None] = {}
            for rel, content in files.items():
                p = Path(work, rel)
                old[rel] = p.read_text() if p.is_file() else None
                if expect is not None and rel in expect and old[rel] != expect[rel]:
                    say(f"{rel} changed since this run wrote it: left as it is")
                    continue
                if content is None:
                    p.unlink(missing_ok=True)
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content)
            self._git(env, "add", "-A", cwd=work)  # a fresh clone: only these files changed
            if not self._git(env, "status", "--porcelain", cwd=work).strip():
                return self._git(env, "rev-parse", "HEAD", cwd=work).strip(), old
            self._git(env, "commit", "-q", "-m", message, cwd=work)
            self._git(env, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=work)
            return self._git(env, "rev-parse", "HEAD", cwd=work).strip(), old


# ---------------------------------------------------------------------------- computer
PNG_INFO = r"""
import json, struct, sys, zlib
import os
d = open(os.path.expanduser(sys.argv[1]), "rb").read()
out = {"bytes": len(d), "png": d[:8] == b"\x89PNG\r\n\x1a\n"}
if out["png"]:
    i, idat, ok = 8, b"", True
    while i + 12 <= len(d):
        n, kind = struct.unpack(">I4s", d[i:i + 8])
        if i + 12 + n > len(d):  # cut short
            ok = False
            break
        body = d[i + 8:i + 8 + n]
        if zlib.crc32(kind + body) & 0xffffffff != struct.unpack(">I", d[i + 8 + n:i + 12 + n])[0]:
            ok = False
        if kind == b"IHDR":
            out["width"], out["height"] = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            out["iend"] = True
            break
        i += 12 + n
    out["crc_ok"] = ok
    try:
        raw = zlib.decompress(idat)
        out["distinct"] = len(set(raw[::max(1, len(raw) // 200000)]))
    except zlib.error:
        out["distinct"] = 0
print("HANGAR-ACCEPT " + json.dumps(out))
"""

MP4_INFO = r"""
import json, os, subprocess, sys
p = os.path.expanduser(sys.argv[1])
out = {"bytes": os.path.getsize(p) if os.path.exists(p) else 0}
with open(p, "rb") as f:
    out["ftyp"] = f.read(12)[4:8] == b"ftyp"
try:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                        "stream=width,height,nb_read_packets,codec_name", "-of", "json", p],
                       capture_output=True, text=True, timeout=60)
    s = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    out.update(width=s.get("width"), height=s.get("height"), frames=int(s.get("nb_read_packets") or 0),
               codec=s.get("codec_name"))
except (OSError, ValueError, subprocess.SubprocessError) as e:
    out["ffprobe_error"] = str(e)[:200]
print("HANGAR-ACCEPT " + json.dumps(out))
"""

PAGE = ("data:text/html,<title>hangar acceptance</title><body style='background:%231d4ed8;margin:0'><h1 "
        "style='color:%23fde047;font:bold 96px sans-serif;padding:40px'>hangar acceptance</h1></body>")


def png_problems(info: dict[str, Any], want: tuple[int, int], what: str, content: bool) -> list[str]:
    """What is wrong with a screenshot: not a whole PNG, the wrong size, or (when it should show something) blank."""
    if not info.get("png") or not info.get("iend") or not info.get("crc_ok"):
        return [f"{what} isn't a complete PNG ({info.get('bytes', 0)} bytes)"]
    out = []
    if (info.get("width"), info.get("height")) != want:
        out.append(f"{what} is {info.get('width')}x{info.get('height')}, not {want[0]}x{want[1]}")
    if content and info.get("distinct", 0) <= 10:
        out.append(f"{what} is blank ({info.get('distinct')} distinct byte values)")
    return out


class Rfb:
    """Just enough of an RFB (VNC) client to check hangar's screen relay: the handshake, a pointer and a key event,
    and a 1x1 framebuffer update to show the stream is still alive."""

    def __init__(self, ws: Any, timeout: float = 30.0):
        self.ws, self.timeout, self.buf, self.bpp = ws, timeout, b"", 4

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            msg = self.ws.recv(timeout=self.timeout)
            self.buf += msg if isinstance(msg, bytes) else msg.encode("latin-1")
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def handshake(self) -> tuple[int, int, str]:
        server = self.read(12)
        if not server.startswith(b"RFB "):
            raise Fail(f"the screen didn't speak RFB: {server!r}")
        if server < b"RFB 003.007":
            self.ws.send(b"RFB 003.003\n")
            sec = struct.unpack(">I", self.read(4))[0]
            if sec != 1:
                raise Fail(f"the VNC server wants security type {sec}, not None")
        else:
            reply = b"RFB 003.008\n" if server >= b"RFB 003.008" else b"RFB 003.007\n"
            self.ws.send(reply)
            n = self.read(1)[0]
            if n == 0:
                raise Fail(f"the VNC server refused: {self.read(struct.unpack('>I', self.read(4))[0])!r}")
            types = self.read(n)
            if 1 not in types:
                raise Fail(f"the VNC server offers security types {list(types)}, not None")
            self.ws.send(bytes([1]))
            if reply == b"RFB 003.008\n" and struct.unpack(">I", self.read(4))[0] != 0:
                raise Fail("the VNC server failed the security handshake")
        self.ws.send(bytes([1]))  # ClientInit: shared
        w, h = struct.unpack(">HH", self.read(4))
        fmt = self.read(16)
        self.bpp = max(1, fmt[0] // 8)
        name = self.read(struct.unpack(">I", self.read(4))[0]).decode(errors="replace")
        return w, h, name

    def pointer(self, x: int, y: int, buttons: int = 0) -> None:
        self.ws.send(struct.pack(">BBHH", 5, buttons, x, y))

    def key(self, keysym: int) -> None:
        self.ws.send(struct.pack(">BBxxI", 4, 1, keysym) + struct.pack(">BBxxI", 4, 0, keysym))

    def alive(self) -> None:
        """Ask for one pixel and read the answer: the relay still carries the stream, and the server has handled
        everything sent before the request."""
        self.ws.send(struct.pack(">BBHHHH", 3, 0, 0, 0, 1, 1))
        while True:
            kind = self.read(1)[0]
            if kind == 0:  # FramebufferUpdate
                self.read(1)
                for _ in range(struct.unpack(">H", self.read(2))[0]):
                    _, _, w, h, enc = struct.unpack(">HHHHi", self.read(12))
                    if enc != 0:
                        raise Fail(f"unexpected encoding {enc} in the screen's answer")
                    self.read(w * h * self.bpp)
                return
            if kind == 2:  # Bell
                continue
            if kind == 3:  # ServerCutText
                self.read(3)
                self.read(struct.unpack(">I", self.read(4))[0])
                continue
            raise Fail(f"unexpected message {kind} from the VNC server")


def _mouse(api: Api, sb: str) -> tuple[int, int]:
    out = api.must(sb, "DISPLAY=:99 xdotool getmouselocation --shell")
    vals = dict(ln.split("=", 1) for ln in out.split() if "=" in ln)
    return int(vals.get("X", -1)), int(vals.get("Y", -1))


def _screen(api: Api, sb: str, control: bool) -> tuple[Any, Rfb, tuple[int, int, str]]:
    from websockets.sync.client import connect

    t = api.call("POST", "/api/screen/tickets", json={"ref": sb, "control": control})
    url = api.url.replace("http", "ws", 1) + f"/api/screen?ticket={t['ticket']}"
    ws = connect(url, subprotocols=["binary"], open_timeout=30, max_size=None)  # type: ignore[list-item]
    rfb = Rfb(ws)
    try:
        return ws, rfb, rfb.handshake()
    except BaseException:
        ws.close()
        raise


def check_computer(api: Api, state: State, opts: argparse.Namespace) -> str:
    """A sandbox with `computer:` (under gVisor on GKE): headless Chromium takes a page screenshot (a whole PNG of
    the display's size, not blank), the display itself is captured, 3 s of video are recorded, and the screen opens
    through a one-time ticket, where a view-only viewer's pointer and keys are dropped and a control viewer's aren't."""
    w, h = RESOLUTION
    env = ensure_env(api, opts, "computer", {"extends": opts.env,
                                             "computer": {"resolution": f"{w}x{h}", "record_max_seconds": 60}})
    sb, boot = new_sandbox(api, state, opts, env, "computer")
    sid = sb["id"]
    note(state, "computer", timings={"create": boot})
    st = json.loads(api.must(sid, "cat ~/.hangar/envstatus.json"))
    runtime = st.get("runtime")  # what hangar configured; gVisor's own kernel log says what it runs under
    kernel = api.exec(sid, "dmesg 2>/dev/null | head -n 3; uname -r")["stdout"]
    if "gvisor" in kernel.lower():
        runtime = "gvisor"
    say(f"{sid} on {sb['provider']}, runtime {runtime} ({' '.join(kernel.split())[:80]}); created in {boot:.0f}s")
    if sb["provider"] == "kubernetes" and runtime != "gvisor":
        raise Fail(f"the sandbox runs under {runtime}, not gVisor: the computer must work under GKE Sandbox")
    problems: list[str] = []
    t = time.monotonic()
    api.must(sid, f"{AGENT} screenshot {shlex.quote(PAGE)} --name accept-page --timeout 90", timeout=240)
    chromium = time.monotonic() - t
    page = probe(api, sid, PNG_INFO, "~/.hangar/computer/screenshots/accept-page.png")
    problems += png_problems(page, (w, h), "Chromium's page screenshot", content=True)
    t = time.monotonic()
    api.must(sid, f"{AGENT} computer start --quiet; {AGENT} screenshot --name accept-display", timeout=120)
    display = time.monotonic() - t
    disp = probe(api, sid, PNG_INFO, "~/.hangar/computer/screenshots/accept-display.png")
    problems += png_problems(disp, (w, h), "the display screenshot", content=False)
    say(f"Chromium page screenshot {page.get('width')}x{page.get('height')}, {page.get('bytes')} B in "
        f"{chromium:.1f}s; display {disp.get('width')}x{disp.get('height')} in {display:.1f}s")
    t = time.monotonic()
    api.must(sid, f"{AGENT} record start --name accept-demo --max-seconds 30")
    with contextlib.suppress(Fail):  # something on the screen while it records: a headed browser on the display
        api.exec(sid, f"{AGENT} browse {shlex.quote(PAGE)} --timeout 30", timeout=120)
    time.sleep(max(0.0, 3.0 - (time.monotonic() - t)))
    api.must(sid, f"{AGENT} record stop", timeout=60)
    recorded = time.monotonic() - t
    mp4 = probe(api, sid, MP4_INFO, "~/.hangar/computer/recordings/accept-demo.mp4")
    say(f"recording: {mp4.get('bytes')} B, {mp4.get('codec')} {mp4.get('width')}x{mp4.get('height')}, "
        f"{mp4.get('frames')} frames over {recorded:.1f}s")
    if not mp4.get("ftyp") or not mp4.get("bytes"):
        problems.append(f"the recording isn't an MP4 ({mp4.get('bytes')} bytes)")
    elif (mp4.get("width"), mp4.get("height")) != (w, h):
        problems.append(f"the recording is {mp4.get('width')}x{mp4.get('height')}, not {w}x{h}")
    elif (mp4.get("frames") or 0) < 10:
        problems.append(f"the recording has {mp4.get('frames')} frames for {recorded:.0f}s (10 fps expected)")
    # The screen: a view-only viewer's input must never reach the sandbox; a control viewer's must.
    target = (w // 2 + 13, h // 2 + 17)
    api.must(sid, "DISPLAY=:99 xdotool mousemove 7 9")
    t = time.monotonic()
    ws, rfb, (sw, sh, name) = _screen(api, sid, control=False)
    opened = time.monotonic() - t
    try:
        if (sw, sh) != (w, h):
            problems.append(f"the screen is {sw}x{sh}, not {w}x{h}")
        rfb.pointer(*target, buttons=1)
        rfb.pointer(*target)
        rfb.key(0x61)
        rfb.alive()  # the server answers in order: anything sent before this reached it, or never will
    finally:
        ws.close()
    time.sleep(0.5)
    after_view = _mouse(api, sid)
    say(f"view-only screen ({name!r}, {sw}x{sh}) opened in {opened:.1f}s; pointer sent to {target}: the sandbox's "
        f"pointer is at {after_view}")
    if after_view != (7, 9):
        problems.append(f"a view-only viewer moved the pointer to {after_view}: its input reached the sandbox")
    ws, rfb, _ = _screen(api, sid, control=True)
    try:
        rfb.pointer(*target)
        rfb.alive()
    finally:
        ws.close()
    time.sleep(0.5)
    after_control = _mouse(api, sid)
    say(f"control screen: pointer sent to {target}, the sandbox's pointer is at {after_control}")
    if after_control != target:
        problems.append(f"a control viewer's pointer didn't arrive (at {after_control}, sent {target}), so the "
                        "view-only result above proves nothing")
    note(state, "computer", runtime=runtime, provider=sb["provider"],
         timings={"chromium_screenshot": chromium, "display_screenshot": display, "record_3s": recorded,
                  "screen_open": opened})
    if problems:
        raise Fail("; ".join(problems))
    return (f"{runtime}: Chromium screenshot {chromium:.1f}s, display {display:.1f}s, {mp4.get('frames')} frames in "
            f"{recorded:.0f}s of video, screen ticket {opened:.1f}s; view-only drops input, control doesn't")


# ---------------------------------------------------------------------------- egress
EGRESS_PROBE = r"""
import base64, json, os, socket, sys, urllib.error, urllib.parse, urllib.request
allowed, denied, direct, dns_name = sys.argv[1:5]
out = {}
proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
out["proxy"] = urllib.parse.urlsplit(proxy).hostname if proxy else None
def connect(target):
    u = urllib.parse.urlsplit(proxy)
    s = socket.create_connection((u.hostname, u.port or 3128), timeout=20)
    try:
        req = "CONNECT %s HTTP/1.1\r\nHost: %s\r\n" % (target, target)
        if u.username is not None:
            cred = "%s:%s" % (urllib.parse.unquote(u.username), urllib.parse.unquote(u.password or ""))
            req += "Proxy-Authorization: Basic %s\r\n" % base64.b64encode(cred.encode()).decode()
        s.sendall((req + "\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 16384:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        parts = data.split(b"\r\n", 1)[0].split()
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else repr(data[:80])
    finally:
        s.close()
def https(host):
    try:
        return urllib.request.urlopen("https://%s/" % host, timeout=30).status
    except urllib.error.HTTPError as e:
        return e.code
def direct_conn():
    host, _, port = direct.rpartition(":")
    socket.create_connection((host, int(port)), timeout=8).close()
    return "connected"
def attempt(key, fn):
    try:
        out[key] = fn()
    except Exception as e:
        out[key] = "%s: %s" % (type(e).__name__, str(e)[:200])
def port(h):
    return h if ":" in h else h + ":443"
if proxy:  # with no resolver, hangar's own names come from /etc/hosts (hostAliases on GKE, #97)
    attempt("proxy_resolves", lambda: sorted({a[4][0] for a in socket.getaddrinfo(out["proxy"], None)})[:3])
    attempt("allowed_connect", lambda: connect(port(allowed)))
    attempt("denied_connect", lambda: connect(port(denied)))
    attempt("allowed_https", lambda: https(allowed.split(":")[0]))
attempt("direct", direct_conn)
attempt("dns", lambda: sorted({a[4][0] for a in socket.getaddrinfo(dns_name, 443)})[:3])
try:
    out["resolvers"] = [ln.split()[1] for ln in open("/etc/resolv.conf") if ln.startswith("nameserver")]
except OSError:
    out["resolvers"] = None
print("HANGAR-ACCEPT " + json.dumps(out))
"""


def egress_problems(view: dict[str, Any], out: dict[str, Any], opts: argparse.Namespace) -> list[str]:
    """What the probe's results say is wrong with an allowlist sandbox's network."""
    problems = []
    if view.get("mode") != "allowlist" or not view.get("enforced"):
        problems.append(f"hangar reports mode {view.get('mode')}, enforced {view.get('enforced')}, for an allowlist "
                        "environment" + (f" ({view['warning']})" if view.get("warning") else ""))
    if not out.get("proxy"):
        problems.append("the sandbox has no HTTPS_PROXY: it isn't on the proxied network")
    elif not isinstance(out.get("proxy_resolves"), list):
        problems.append(f"the proxy's name {out['proxy']} doesn't resolve in the sandbox ({out.get('proxy_resolves')})"
                        ": with no resolver it must be in /etc/hosts (the pod's hostAliases, #97)")
    else:
        if out.get("allowed_connect") != 200:
            problems.append(f"the proxy answered {out.get('allowed_connect')!r} for the allowed {opts.egress_allow}")
        if not isinstance(out.get("allowed_https"), int):
            problems.append(f"https://{opts.egress_allow}/ through the proxy: {out.get('allowed_https')}")
        if out.get("denied_connect") != 403:
            problems.append(f"the proxy answered {out.get('denied_connect')!r} for {opts.egress_deny}, not 403")
    if out.get("direct") == "connected":
        problems.append(f"a direct connection to {opts.egress_direct} went through, around the proxy")
    if isinstance(out.get("dns"), list):
        problems.append(f"the sandbox resolved {opts.dns_probe} ({', '.join(out['dns'])}) through "
                        f"{', '.join(out.get('resolvers') or ['?'])}: it has a resolver, which can carry data out "
                        "in the names it looks up")
    return problems


def check_egress(api: Api, state: State, opts: argparse.Namespace) -> str:
    """An `allowlist` sandbox: the allowed host answers through the proxy, another gets 403, a direct connection
    fails, and a DNS lookup fails (no resolver, #97). With --with-model, an agent's `env request egress` shows up as
    pending for its owner (and is denied again)."""
    env = ensure_env(api, opts, "egress", {"extends": opts.session_env, "egress": {
        "mode": "allowlist", "presets": [], "allow": [opts.egress_allow]}})
    sb, boot = new_sandbox(api, state, opts, env, "egress")
    view = api.call("GET", f"/api/egress/sandboxes/{sb['id']}")
    t = time.monotonic()
    out = probe(api, sb["id"], EGRESS_PROBE, opts.egress_allow, opts.egress_deny, opts.egress_direct, opts.dns_probe,
                timeout=180)
    probes = time.monotonic() - t
    say(f"{sb['id']}: mode {view.get('mode')}, enforced {view.get('enforced')}, proxy {out.get('proxy')}")
    for k in ("allowed_connect", "allowed_https", "denied_connect", "direct", "dns"):
        say(f"  {k}: {out.get(k)}")
    note(state, "egress", timings={"create": boot, "probes": probes}, results={k: out.get(k) for k in (
        "allowed_connect", "allowed_https", "denied_connect", "direct", "dns")})
    problems = egress_problems(view, out, opts)
    if problems:
        raise Fail("; ".join(problems))
    request = "env request: not checked (it needs an agent turn: --with-model)"
    if opts.with_model:
        request = _egress_request(api, state, opts, env)
    return (f"{opts.egress_allow} through the proxy, {opts.egress_deny} 403, no direct route, no DNS; {request}")


def _egress_request(api: Api, state: State, opts: argparse.Namespace, env: str) -> str:
    """A real agent turn files `hangar-agent env request egress <denied host>`; it must reach the owner as pending."""
    name = f"accept-{state.run}-egress"
    ha._new_session(api, state, name, argparse.Namespace(**{**vars(opts), "session_env": env, "repo": None}))
    why = f"hangar acceptance check {state.run}"
    cmd = f"hangar-agent env request egress {opts.egress_deny} --why '{why}'"
    t = time.monotonic()
    ha._turn(api, name, f"This is an automated hangar acceptance check. Use your shell tool to run exactly this "
                        f"command, then reply DONE: {cmd}", opts.turn_timeout)
    deadline = time.monotonic() + 60
    while True:
        reqs = api.call("GET", "/api/env-requests", params={"session": name, "state": "pending"})
        mine = [r for r in reqs if r.get("kind") == "egress" and r.get("host") == opts.egress_deny]
        if mine:
            break
        if time.monotonic() > deadline:
            raise Fail(f"the agent's env request for {opts.egress_deny} never showed up as pending "
                       f"(GET /api/env-requests?session={name}: {reqs[:3]})")
        time.sleep(3)
    state.add("env_requests", mine[0]["id"])
    note(state, "egress", timings={"request_turn": time.monotonic() - t})
    api.call("POST", f"/api/env-requests/{mine[0]['id']}", json={"decision": "deny", "note": "acceptance check"})
    return f"the agent's env request {mine[0]['id']} was pending for its owner (denied again)"


# ---------------------------------------------------------------------------- services
PG_PROBE = r"""
import json, socket, sys, time
host, port, wait = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
t0, out, last = time.time(), {"host": host}, None
try:
    out["address"] = socket.getaddrinfo(host, port)[0][4][0]
except OSError as e:
    out["resolve_error"] = "%s: %s" % (type(e).__name__, e)
    print("HANGAR-ACCEPT " + json.dumps(out))
    sys.exit(0)
while time.time() - t0 < wait:
    try:
        s = socket.create_connection((host, port), timeout=5)
        s.sendall(b"\x00\x00\x00\x08\x04\xd2\x16\x2f")  # Postgres's SSLRequest: it answers N or S
        r = s.recv(1)
        s.close()
        if r in (b"N", b"S"):
            out["answer"], out["seconds"] = r.decode(), round(time.time() - t0, 1)
            break
        last = "answered %r" % r
    except OSError as e:
        last = "%s: %s" % (type(e).__name__, e)
    time.sleep(2)
out["last_error"] = last
print("HANGAR-ACCEPT " + json.dumps(out))
"""


def check_services(api: Api, state: State, opts: argparse.Namespace) -> str:
    """An environment with a Postgres service: the sandbox reaches it at its name and it speaks Postgres."""
    env = ensure_env(api, opts, "services", {"extends": opts.env, "services": {"postgres": {
        "image": opts.service_image, "port": 5432,
        "env": {"POSTGRES_PASSWORD": "acceptance", "PGDATA": "/var/lib/postgresql/data/pgdata"},
        "volumes": ["/var/lib/postgresql/data"]}}})
    sb, boot = new_sandbox(api, state, opts, env, "services")
    declared = [s for s in (sb.get("info") or {}).get("services") or [] if s.get("kind") == "service"]
    failures = (sb.get("info") or {}).get("setup_failures") or []
    out = probe(api, sb["id"], PG_PROBE, "postgres", 5432, opts.service_wait, timeout=int(opts.service_wait) + 60)
    say(f"{sb['id']} in {boot:.0f}s; services {[s.get('name') for s in declared] or 'none recorded'}; postgres at "
        f"{out.get('address') or out.get('resolve_error')}: {out.get('answer') or out.get('last_error')}")
    note(state, "services", timings={"create": boot, "postgres_answer": out.get("seconds") or 0})
    if "resolve_error" in out:
        raise Fail(f"`postgres` doesn't resolve in the sandbox ({out['resolve_error']})" +
                   (f"; setup failures: {'; '.join(map(str, failures))[:300]}" if failures else ""))
    if "answer" not in out:
        raise Fail(f"postgres:5432 didn't answer as Postgres within {opts.service_wait:.0f}s ({out.get('last_error')})"
                   + (f"; setup failures: {'; '.join(map(str, failures))[:300]}" if failures else ""))
    return f"postgres:5432 at {out['address']} answered after {out['seconds']}s (sandbox {boot:.0f}s)"


# ---------------------------------------------------------------------------- envbuild
FIXTURE_FILE = ".hangar/environment.yaml"
FIXTURE_IGNORE = ".gitignore"


def fixture_config(run: str, broken: bool = False) -> str:
    """The fixture repo's environment: an install that leaves a marker (ignored by git, so the checkout keeps it), a
    dev server the supervisor keeps up, and its preview port. The broken one's install fails."""
    install = (f"echo 'install for {run} fails on purpose' >&2; exit 3" if broken else
               f"mkdir -p .accept && echo {run} > .accept/installed")
    return (f"# hangar acceptance fixture (scripts/acceptance; run {run})\n"
            f"install: {json.dumps(install)}\n"
            "terminals:\n"
            "  - name: web\n"
            "    command: python3 -m http.server 8000 --bind 0.0.0.0\n"
            "    port: 8000\n"
            "ports:\n"
            "  - {port: 8000, name: web}\n")


def _build(api: Api, repo: str, env: str, timeout: float) -> dict[str, Any]:
    b = api.call("POST", "/api/envbuilds", json={"repo": repo, "environment": env})
    deadline, last = time.monotonic() + timeout, None
    while True:
        b = api.call("GET", f"/api/envbuilds/{b['id']}")
        if (b["state"], b.get("stage")) != last:
            say(f"build {b['id']} ({b.get('version')}): {b['state']}" + (f" ({b['stage']})" if b.get("stage") else ""))
            last = (b["state"], b.get("stage"))
        if b["state"] in ("succeeded", "failed", "interrupted"):
            return b
        if time.monotonic() > deadline:
            raise Fail(f"build {b['id']} still {b['state']} ({b.get('stage')}) after {timeout:.0f}s")
        time.sleep(ha.POLL_SECONDS)


def _restore_fixture(state: State) -> None:
    fx = state.get("fixture")
    if not fx or fx.get("restored"):
        return
    FixtureRepo(fx["url"], github_token()).commit(fx["old"], "hangar acceptance: put the fixture back",
                                                  expect=fx.get("wrote"))
    state.set("fixture", {**fx, "restored": True})


def _preview(api: Api, sid: str) -> str:
    """Open the sandbox's preview link once (a cookie for that origin), load the page with it, then try the same
    link again, which must be refused."""
    links = api.call("GET", f"/api/sandboxes/{sid}/previews")["previews"]
    link = next((x for x in links if x["port"] == 8000), None)
    if link is None:
        raise Fail(f"no preview link for port 8000 ({links})")
    u = httpx.URL(link["url"])
    host = u.host
    client = httpx.Client(timeout=30, follow_redirects=False)
    try:
        target, headers = u, {}
        if host.endswith(".localhost"):  # a laptop's HANGAR_PREVIEW_DOMAIN: hangar itself, with the preview's Host
            base = httpx.URL(api.url)
            target = u.copy_with(scheme=base.scheme, host=base.host, port=base.port)
            headers = {"Host": f"{host}:{u.port}" if u.port else host}
        first = client.get(target, headers=headers)
        if first.status_code != 303 or not first.headers.get("set-cookie"):
            raise Fail(f"opening the preview link: HTTP {first.status_code} {first.text[:200]}")
        cookie = first.headers["set-cookie"].split(";", 1)[0]
        page = client.get(target.copy_with(path="/", query=None), headers={**headers, "Cookie": cookie})
        if page.status_code != 200:
            raise Fail(f"the preview page with its cookie: HTTP {page.status_code} {page.text[:200]}")
        again = client.get(target, headers=headers)
        if again.status_code != 401:
            raise Fail(f"the same preview link opened a second time: HTTP {again.status_code}, not 401")
    finally:
        client.close()
    return f"preview of :8000 at {host} opened once, then refused"


def check_envbuild(api: Api, state: State, opts: argparse.Namespace) -> str:
    """A fixture repo's .hangar/environment.yaml builds; a broken install then fails without replacing the active
    build, and a new sandbox for the repo starts from the good one, with the supervisor's dev server up and (with
    HANGAR_PREVIEW_DOMAIN) a preview link that works once."""
    if not opts.fixture_repo:
        raise Skip("needs --fixture-repo OWNER/REPO (or HANGAR_ACCEPT_FIXTURE_REPO): a repo whose default branch the "
                   "check may commit to (it puts the file back afterwards); docs/acceptance.md")
    token = github_token()
    url = opts.fixture_push_url or f"https://github.com/{opts.fixture_repo}.git"
    if not token and url.startswith("https://github.com/"):
        raise Skip("needs a token that pushes to the fixture repo: HANGAR_ACCEPT_GITHUB_TOKEN, or `gh auth login`")
    repo_url = f"https://github.com/{opts.fixture_repo}.git"
    env = ensure_env(api, opts, "envbuild", {"extends": opts.env})
    fx = FixtureRepo(url, token)
    ignore = "# hangar acceptance fixture\n.accept/\n"
    files = {FIXTURE_FILE: fixture_config(state.run), FIXTURE_IGNORE: ignore}
    state.set("fixture", {"url": url, "old": None, "restored": False})
    good_sha, old = fx.commit(files, f"hangar acceptance {state.run}: the fixture's environment")
    state.set("fixture", {"url": url, "old": old, "wrote": files, "restored": False})
    say(f"{opts.fixture_repo}: committed the fixture config ({good_sha[:9]})")
    t = time.monotonic()
    good = _build(api, repo_url, env, opts.build_timeout)
    good_s = time.monotonic() - t
    if good["state"] != "succeeded":
        raise Fail(f"the fixture's build {good['id']} {good['state']}: {good.get('error')}; log: "
                   f"{(good.get('log') or '')[-300:]}")
    broken = fixture_config(state.run, broken=True)
    state.set("fixture", {**state.get("fixture"), "wrote": {**files, FIXTURE_FILE: broken}})
    bad_sha, _ = fx.commit({FIXTURE_FILE: broken}, f"hangar acceptance {state.run}: a broken install")
    say(f"{opts.fixture_repo}: committed a broken install ({bad_sha[:9]})")
    t = time.monotonic()
    bad = _build(api, repo_url, env, opts.build_timeout)
    bad_s = time.monotonic() - t
    hist = api.call("GET", "/api/envbuilds", params={"repo": opts.fixture_repo, "environment": env})
    say(f"broken build {bad['id']}: {bad['state']} ({(bad.get('error') or '')[:120]}); active {hist.get('active')}")
    note(state, "envbuild", timings={"good_build": good_s, "broken_build": bad_s}, good=good["id"], broken=bad["id"])
    if bad["state"] != "failed":
        raise Fail(f"the build with a failing install {bad['id']} ended {bad['state']}, not failed")
    if hist.get("active") != good["id"]:
        raise Fail(f"a failed build replaced the active one: active is {hist.get('active')}, not {good['id']}")
    sb, boot = new_sandbox(api, state, opts, env, "envbuild", repo=repo_url)
    info = sb.get("env_build") or (sb.get("info") or {}).get("env_build") or {}
    say(f"{sb['id']} in {boot:.0f}s: env_build {info.get('version')} ({info.get('id')}), fallback "
        f"{info.get('fallback')}, stale {info.get('stale')}: {info.get('reason')}")
    if info.get("id") != good["id"] or info.get("fallback"):
        raise Fail(f"the new sandbox started from {info.get('id')} (fallback {info.get('fallback')}: "
                   f"{info.get('reason')}), not the active build {good['id']}")
    repo_path = (sb.get("info") or {}).get("repo_path")
    marker = api.exec(sb["id"], "cat .accept/installed", cwd=repo_path)["stdout"].strip()
    if marker != state.run:
        raise Fail(f"the checkout doesn't hold what the good install made (.accept/installed: {marker!r})")
    t = time.monotonic()
    waited = api.exec(sb["id"], f"{AGENT} svc wait web --timeout 120", timeout=180)
    if waited["exit_code"] != 0:
        status = api.exec(sb["id"], f"{AGENT} svc status", timeout=60)["stdout"]
        raise Fail(f"the supervisor's dev server isn't ready: {(waited['stdout'] + waited['stderr'])[-200:]}; "
                   f"{status[-300:]}")
    served = api.exec(sb["id"], "python3 -c \"import urllib.request; print(urllib.request.urlopen("
                                "'http://127.0.0.1:8000/', timeout=10).status)\"")["stdout"].strip()
    if served != "200":
        raise Fail(f"the dev server on :8000 answered {served!r}")
    svc_s = time.monotonic() - t
    note(state, "envbuild", timings={"sandbox": boot, "svc_ready": svc_s})
    preview = "preview: skipped (HANGAR_PREVIEW_DOMAIN isn't set)"
    if setting(api, "HANGAR_PREVIEW_DOMAIN"):
        preview = _preview(api, sb["id"])
    _restore_fixture(state)
    return (f"build {good.get('version')} in {good_s:.0f}s; the broken install failed ({bad_s:.0f}s) and "
            f"{good.get('version')} stayed active; a new sandbox started from it, svc's dev server up; {preview}")


# ---------------------------------------------------------------------------- secrets
TOKEN_ALPHABET = string.ascii_letters + string.digits


def synthetic_token() -> tuple[str, str]:
    """A GitHub-token-shaped string, as two pieces that only join in the sandbox. Neither this file nor anything sent
    to hangar holds the whole token (GitHub's push protection refuses commits that carry a token literal)."""
    while True:
        body = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(36))
        if not re.search(r"(?i)example|x{8,}|0{16,}|1234567890abcdef|your", body):
            return "TOKEN=g" + "h", "p_" + body


NO_CHANGES = "This is an automated hangar acceptance check. Don't change any files. Reply with exactly: DONE"


def check_secrets(api: Api, state: State, opts: argparse.Namespace) -> str:
    """A job whose change carries a synthetic GitHub token isn't pushed: it fails as `secret_found`, its events name
    the file and line (never the value), and the branch isn't on GitHub."""
    if not opts.with_model:
        raise Skip("needs an agent turn (a few cents): run with --with-model")
    if not opts.test_repo:
        raise Skip("needs --test-repo OWNER/REPO (or HANGAR_ACCEPT_TEST_REPO), in HANGAR_GITHUB_REPOS")
    head, tail = synthetic_token()
    fname = f"accept-secret-{state.run}.txt"
    plant = (f"f={fname}; [ -f $f ] || {{ printf '%s%s\\n' {shlex.quote(head)} {shlex.quote(tail)} > $f && "
             "git add $f && git -c user.name='hangar acceptance' -c user.email=acceptance@hangar.invalid commit -qm "
             f"'acceptance: a synthetic token'; }}; true")
    job = api.call("POST", "/api/jobs", json={
        "title": f"hangar acceptance {state.run}: secret scan", "repo": f"https://github.com/{opts.test_repo}.git",
        "instructions": NO_CHANGES,
        "verify": plant, "verify_in_loop": False, "deliver": "branch", "autofix": "off",
        "job_budget": paid(opts), "labels": {LABEL: state.run}, "timeout_minutes": 30})
    state.add("jobs", job["id"])
    state.add("branches", {"repo": opts.test_repo, "branch": job["branch"]})
    t = time.monotonic()
    job = wait_job(api, job["id"], opts.turn_timeout)
    secs = time.monotonic() - t
    text = events_text(job)
    note(state, "secrets", timings={"job": secs})
    if (head + tail) in json.dumps(job):
        raise Fail("the job's record carries the token's value")
    where = f"{fname}:1"
    if job["state"] != "failed" or job.get("reason") != "secret_found":
        raise Fail(f"the job ended {job['state']} ({job.get('reason')}), not failed (secret_found); pushed: "
                   f"{(job.get('result') or {}).get('pushed')}; {text[-300:]}")
    if where not in text or "GitHub token" not in text:
        raise Fail(f"the job's events don't name {where} and the kind of secret: {text[-400:]}")
    if (job.get("result") or {}).get("pushed"):
        raise Fail(f"the job pushed {job['branch']} although the scan blocked it")
    gh = GitHubApi(github_token())
    try:
        r = gh.http.get(f"/repos/{opts.test_repo}/git/ref/heads/{job['branch']}")
        on_github = r.status_code == 200
    finally:
        gh.close()
    if on_github:
        raise Fail(f"{job['branch']} is on GitHub although hangar blocked the push")
    return f"blocked in {secs:.0f}s: secret_found, events name {where} (GitHub token), nothing pushed"


# ---------------------------------------------------------------------------- landing
def check_landing(api: Api, state: State, opts: argparse.Namespace) -> str:
    """On --test-repo with the GitHub App: a job opens its PR as the App. With HANGAR_ACCEPT_APPROVER_TOKEN, the
    approver's approval makes hangar arm auto-merge for that commit, and a push to the PR disarms it."""
    if not opts.test_repo:
        raise Skip("needs --test-repo OWNER/REPO (or HANGAR_ACCEPT_TEST_REPO) with the GitHub App installed")
    if not setting(api, "HANGAR_GITHUB_APP_CREDENTIAL"):
        raise Skip("the GitHub App isn't configured (HANGAR_GITHUB_APP_CREDENTIAL; docs/landing.md)")
    if not opts.with_model:
        raise Skip("needs an agent turn (a few cents): run with --with-model")
    approver_token = (os.environ.get("HANGAR_ACCEPT_APPROVER_TOKEN") or "").strip() or None
    approver = None
    if approver_token:
        a = GitHubApi(approver_token)
        try:
            approver = a.req("GET", "/user")["login"]
        finally:
            a.close()
        old = api.http.get("/api/repos/policy", params={"repo": opts.test_repo})
        prev = {k: old.json().get(k) for k in POLICY_FIELDS} if old.status_code == 200 else None
        mine = {"land": "auto_after_approval", "approvers": [approver]}
        state.set("policy_restore", {"repo": opts.test_repo, "old": prev, "set": mine, "restored": False})
        api.call("PUT", "/api/repos/policy", json={"repo": opts.test_repo, **mine})
        say(f"{opts.test_repo}: land auto_after_approval, approver @{approver} (put back afterwards)")
    fname = f"accept-landing-{state.run}.txt"
    commit = (f"f={fname}; [ -f $f ] || {{ echo {state.run} > $f && git add $f && git -c user.name='hangar "
              "acceptance' -c user.email=acceptance@hangar.invalid commit -qm 'hangar acceptance: landing'; }; true")
    job = api.call("POST", "/api/jobs", json={
        "title": f"hangar acceptance {state.run}: landing", "repo": f"https://github.com/{opts.test_repo}.git",
        "instructions": NO_CHANGES,
        "verify": commit, "verify_in_loop": False, "deliver": "pr", "autofix": "off",
        "job_budget": paid(opts), "labels": {LABEL: state.run}, "timeout_minutes": 30})
    state.add("jobs", job["id"])
    state.add("branches", {"repo": opts.test_repo, "branch": job["branch"]})
    t = time.monotonic()
    job = wait_job(api, job["id"], opts.turn_timeout)
    job_s = time.monotonic() - t
    number = (job.get("result") or {}).get("pr_number")
    if not number:
        raise Fail(f"the job opened no PR ({job['state']}, {job.get('reason')}): {events_text(job)[-300:]}")
    state.add("prs", {"repo": opts.test_repo, "number": number, "branch": job["branch"]})
    gh = GitHubApi(github_token() or approver_token)
    try:
        pr = gh.req("GET", f"/repos/{opts.test_repo}/pulls/{number}")
        author = pr.get("user") or {}
        say(f"PR #{number} by {author.get('login')} ({author.get('type')}), head {pr['head']['sha'][:9]}")
        note(state, "landing", timings={"job_to_pr": job_s}, pr=number)
        if author.get("type") != "Bot" or not str(author.get("login", "")).endswith("[bot]"):
            raise Fail(f"PR #{number} was opened by {author.get('login')} ({author.get('type')}), not the App: "
                       f"{events_text(job)[-200:]}")
        if not approver_token:
            return (f"PR #{number} opened as {author['login']} in {job_s:.0f}s; approval not simulated "
                    "(HANGAR_ACCEPT_APPROVER_TOKEN isn't set)")
        return _approve_and_push(api, state, opts, gh, approver_token, approver or "", job, pr, job_s)
    finally:
        gh.close()


def _landing(api: Api, job_id: str, want: Callable[[dict[str, Any]], bool], timeout: float, what: str
             ) -> dict[str, Any]:
    deadline, last = time.monotonic() + timeout, None
    while True:
        j = api.call("GET", f"/api/jobs/{job_id}")
        st = j.get("landing") or {}
        now = (st.get("pr_state"), st.get("armed_sha"), st.get("note"))
        if now != last:
            say(f"landing: {st.get('pr_state')}, armed {(st.get('armed_sha') or '-')[:9]}"
                + (f" ({st['note']})" if st.get("note") else ""))
            last = now
        if want(st):
            return st
        if time.monotonic() > deadline:
            raise Fail(f"{what} within {timeout:.0f}s: {st.get('pr_state')}, {st.get('note') or 'no note'}; "
                       f"{events_text(j)[-200:]}")
        time.sleep(5)


def _approve_and_push(api: Api, state: State, opts: argparse.Namespace, gh: GitHubApi, approver_token: str,
                      approver: str, job: dict[str, Any], pr: dict[str, Any], job_s: float) -> str:
    number, head = pr["number"], pr["head"]["sha"]
    a = GitHubApi(approver_token)
    try:
        t = time.monotonic()
        a.req("POST", f"/repos/{opts.test_repo}/pulls/{number}/reviews",
              json={"event": "APPROVE", "commit_id": head, "body": "hangar acceptance check"})
        say(f"@{approver} approved {head[:9]}")
        st = _landing(api, job["id"], lambda s: s.get("pr_state") == "merged" or s.get("armed_sha") == head,
                      opts.landing_timeout, "the approval didn't arm auto-merge")
        armed_s = time.monotonic() - t
        note(state, "landing", timings={"approval_to_armed": armed_s})
        if st.get("pr_state") == "merged":
            return (f"PR #{number} opened as the App ({job_s:.0f}s); approved and merged at once ({armed_s:.0f}s): "
                    "nothing kept it open, so the disarm on push wasn't checked (docs/acceptance.md: require a code "
                    "owner's review on the test repo)")
        auto = (gh.req("GET", f"/repos/{opts.test_repo}/pulls/{number}").get("auto_merge") or {})
        if not auto:
            raise Fail(f"hangar recorded auto-merge armed for {head[:9]}, but GitHub's PR has none")
        t = time.monotonic()
        path = f"accept-landing-{state.run}-push.txt"
        writer = gh if gh.http.headers.get("Authorization") else a
        writer.req("PUT", f"/repos/{opts.test_repo}/contents/{path}", json={
            "message": "hangar acceptance: a push after the approval", "branch": pr["head"]["ref"],
            "content": base64.b64encode(f"{state.run}\n".encode()).decode()})
        _landing(api, job["id"], lambda s: not s.get("armed_sha") and not s.get("approved_sha"),
                 opts.landing_timeout, "a push after the approval didn't disarm auto-merge")
        disarm_s = time.monotonic() - t
        if gh.req("GET", f"/repos/{opts.test_repo}/pulls/{number}").get("auto_merge"):
            raise Fail("hangar recorded the disarm, but GitHub's PR still has auto-merge on")
        note(state, "landing", timings={"push_to_disarmed": disarm_s})
        return (f"PR #{number} opened as the App ({job_s:.0f}s); @{approver}'s approval armed auto-merge in "
                f"{armed_s:.0f}s; a push disarmed it in {disarm_s:.0f}s")
    finally:
        a.close()


# ---------------------------------------------------------------------------- intake
def jira_payload(run: str, project: str, trigger: dict[str, Any], account: str) -> dict[str, Any]:
    """A Jira Cloud `jira:issue_created` delivery for a synthetic issue that matches the trigger."""
    n = 900_000_000 + secrets.randbelow(99_999_999)
    fields: dict[str, Any] = {"project": {"key": project}, "summary": f"hangar acceptance {run}",
                              "description": "A synthetic issue from hangar's acceptance checks. Nothing to do.",
                              "labels": [trigger["label"]] if trigger.get("label") else [],
                              "status": {"name": trigger.get("status") or "To Do"},
                              "reporter": {"accountId": account, "displayName": "hangar acceptance"}}
    return {"webhookEvent": "jira:issue_created", "timestamp": int(time.time() * 1000),
            "user": {"accountId": account, "displayName": "hangar acceptance"},
            "issue": {"key": f"{project}-{n}", "fields": fields}}


def _repo_of(value: str) -> str:
    """`owner/repo`, lower case, of owner/repo, a GitHub https URL or git@github.com:owner/repo(.git)."""
    v = value.strip().lower().rstrip("/").removesuffix(".git")
    return v.split("github.com/")[-1].split("github.com:")[-1]


def jira_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def check_intake(api: Api, state: State, opts: argparse.Namespace) -> str:
    """A signed synthetic Jira webhook from an account linked to a test person creates a job for that person (then
    cancelled); a bad signature is refused and a replay is dropped."""
    cfg = setting(api, "HANGAR_JIRA")
    if not cfg:
        raise Skip("Jira intake isn't configured (HANGAR_JIRA; docs/intake.md)")
    secret = (os.environ.get("HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET") or "").strip()
    if not secret:
        raise Skip("needs the Jira webhook's secret in HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET to sign a delivery")
    mapped = cfg.get("projects") or {}
    projects = sorted(mapped)
    # The job is cancelled at once, but it may start on its repo first (a sandbox, perhaps a turn): only a project
    # mapped to the test repo, never one of the team's real repos, unless --jira-project names one.
    repos = {k: str((mapped[k] or {}).get("repo") or "") for k in projects}
    on_test = [k for k in projects if opts.test_repo and _repo_of(repos[k]) == opts.test_repo.lower()]
    if not opts.jira_project and not on_test:
        listed = ", ".join(f"{k}: {repos[k]}" for k in projects) or "none"
        raise Skip(f"needs a Jira project mapped to the test repo ({opts.test_repo or 'no --test-repo'}) in "
                   f"HANGAR_JIRA (mapped: {listed}), or --jira-project KEY: the synthetic issue's job may start on "
                   "its repo before it's cancelled")
    project = opts.jira_project or on_test[0]
    if project not in projects:
        raise Fail(f"Jira project {project} isn't mapped in HANGAR_JIRA (mapped: {', '.join(projects) or 'none'})")
    person, account = f"accept-{state.run}-jira", f"accept-{state.run}"
    state.add("people", person)
    api.call("POST", "/api/people", json={"id": person, "name": "hangar acceptance (Jira)", "role": "member",
                                          "token": False})
    api.call("POST", f"/api/people/{person}/links", json={"source": "jira", "id": account})
    body = json.dumps(jira_payload(state.run, project, cfg.get("trigger") or {}, account)).encode()
    hdr = {"Content-Type": "application/json", "X-Atlassian-Webhook-Identifier": f"accept-{state.run}"}
    bad = api.http.post("/api/hooks/jira", content=body, headers={**hdr, "X-Hub-Signature": "sha256=" + "0" * 64})
    if bad.status_code != 401:
        raise Fail(f"a delivery with a wrong signature got HTTP {bad.status_code}, not 401")
    t = time.monotonic()
    r = api.http.post("/api/hooks/jira", content=body, headers={**hdr, "X-Hub-Signature": jira_signature(secret, body)})
    took = time.monotonic() - t
    res = r.json() if "json" in r.headers.get("content-type", "") else {"error": r.text[:200]}
    if r.status_code != 200 or not res.get("job"):
        raise Fail(f"the signed delivery: HTTP {r.status_code} {res}")
    state.add("jobs", res["job"])
    job = api.call("GET", f"/api/jobs/{res['job']}")
    say(f"job {job['id']} ({job['state']}) for {job.get('person')} in {took * 1000:.0f} ms: {job.get('title')}")
    with contextlib.suppress(Fail):
        api.call("POST", f"/api/jobs/{job['id']}/cancel")
    again = api.http.post("/api/hooks/jira", content=body,
                          headers={**hdr, "X-Hub-Signature": jira_signature(secret, body)})
    replay = again.json() if "json" in again.headers.get("content-type", "") else {}
    note(state, "intake", timings={"webhook_to_job": took})
    if job.get("person") != person or job.get("source") != "jira":
        raise Fail(f"the job is {job.get('source')}'s for {job.get('person')}, not jira's for {person}")
    if job["state"] == "awaiting_approval":
        raise Fail("the linked account's job waits for approval: the link wasn't trusted")
    if replay.get("job") or "duplicate" not in str(replay.get("ignored", "")):
        raise Fail(f"a replayed delivery wasn't dropped: {replay}")
    return f"{project}: a job for {person} in {took * 1000:.0f} ms (cancelled); bad signature 401; replay dropped"


# ---------------------------------------------------------------------------- scale smoke
def split_sessions(total: int, people: int, cap: int) -> list[int]:
    """Sessions per person: the first as many as their cap allows (so the cap can be tested), the rest shared."""
    first = min(cap, total - (people - 1)) if cap > 0 else total // people + total % people
    rest = total - first
    others = [rest // (people - 1) + (1 if i < rest % (people - 1) else 0) for i in range(people - 1)]
    return [first, *others]


def job_intervals_ok(jobs: list[dict[str, Any]], cap: int) -> list[str]:
    """Fairness from finished jobs: no person ever ran more than `cap` at once, and the others' jobs didn't wait
    behind the first person's batch."""
    def started(j: dict[str, Any]) -> float | None:
        return next((e["ts"] for e in j.get("events") or [] if e.get("state") in ("provisioning", "running")), None)

    def ended(j: dict[str, Any]) -> float:
        return next((e["ts"] for e in reversed(j.get("events") or []) if e.get("state") in TERMINAL_JOB),
                    j.get("updated_at") or 0)

    problems = []
    by: dict[str, list[tuple[float, float]]] = {}
    for j in jobs:
        s = started(j)
        if s is not None:
            by.setdefault(j.get("person") or "?", []).append((s, ended(j)))
    for who, spans in by.items():
        for s, _ in spans:
            running = sum(1 for a, b in spans if a <= s < b)
            if running > cap:
                problems.append(f"{who} had {running} jobs running at once (the cap is {cap})")
                break
    people = list(dict.fromkeys(j.get("person") for j in jobs))
    first = by.get(people[0] or "?", [])
    if first:
        last_start = max(s for s, _ in first)
        for who in people[1:]:
            mine = by.get(who or "?", [])
            if mine and min(s for s, _ in mine) > last_start:
                problems.append(f"{who}'s job started after all of {people[0]}'s: it waited behind their batch")
    return problems


def check_scale_smoke(api: Api, state: State, opts: argparse.Namespace) -> str:
    """--scale-sessions sessions at once across --scale-people test people: the per-person cap refuses one past it,
    a member can't see another's session, p95 create, and nothing is left once they're gone. With --with-model, a
    batch of short jobs shows the per-person job cap and the fair queue."""
    n, k = opts.scale_sessions, opts.scale_people
    if k < 2 or n < k:
        raise Fail("--scale-people must be at least 2 and --scale-sessions at least that many")
    # On a production install the team's own creates come first: start only with room to spare under the fleet's cap
    limit = int(setting(api, "HANGAR_MAX_SANDBOXES") or 0)
    if limit:
        live, spare = len(api.call("GET", "/api/sandboxes")), max(10, limit // 10)
        if live + n + 1 + spare > limit:
            raise Skip(f"no room: {live} of HANGAR_MAX_SANDBOXES={limit} sandboxes are live, and {n + 1} more would "
                       f"leave the team fewer than {spare}; run it in a quieter hour, or with fewer --scale-sessions")
    cap = int(setting(api, "HANGAR_PERSON_MAX_SESSIONS") or 0)
    plan = split_sessions(n, k, cap)
    env = opts.scale_env or opts.session_env
    people = [f"accept-{state.run}-p{i + 1}" for i in range(k)]
    tokens: dict[str, str] = {}
    for p in people:  # members, each with a token for MCP (how the team starts sessions) and for jobs
        state.add("people", p)
        api.call("POST", "/api/people", json={"id": p, "name": f"hangar acceptance {p}", "role": "member",
                                              "token": False})
        tok, _ = api.scopes_token(state, p.removeprefix(f"accept-{state.run}-"), ["mcp", "jobs"], person=p)
        tokens[p] = tok
    names = [(p, f"accept-{state.run}-s{i:02d}") for i, p in enumerate(p for p, m in zip(people, plan, strict=True)
                                                                       for _ in range(m))]
    for _, name in names:
        state.add("sessions", name)
    say(f"{n} sessions on {env} for {k} people ({'/'.join(map(str, plan))}) over MCP; the cap per person is "
        f"{cap or 'off'}")
    t = time.monotonic()
    booted = asyncio.run(_boot_all(api.url, [(tokens[p], name) for p, name in names], env, opts.boot_timeout))
    wall = time.monotonic() - t
    errors = [str(b)[:200] for b in booted if isinstance(b, BaseException)]
    times = [b[0] for b in booted if not isinstance(b, BaseException)]
    sandboxes = [b[1] for b in booted if not isinstance(b, BaseException)]
    if errors:
        raise Fail(f"{len(errors)} of {n} sessions didn't start: {'; '.join(errors[:3])}")
    p95 = pct(times, 95)
    say(f"{n} sessions up in {wall:.0f}s: new_session to idle p50 {pct(times, 50):.1f}s, p95 {p95:.1f}s, max "
        f"{max(times):.1f}s")
    notes = []
    p1 = people[0]
    if cap and plan[0] == cap:
        extra = f"accept-{state.run}-over"
        state.add("sessions", extra)
        err, text, _ = asyncio.run(_mcp_call(api.url, tokens[p1], "new_session",
                                             {"name": extra, "environment": env, "idle_minutes": 0}))
        if not err or "limit per person" not in text:
            raise Fail(f"{p1}'s session {cap + 1}, past the cap of {cap}, wasn't refused: {text[:200]}")
        notes.append(f"{p1}'s session {cap + 1} refused at the cap")
    else:
        notes.append(f"the cap ({cap or 'off'}) needs more than {n} sessions to reach: not checked")
    other = next(name for p, name in names if p == people[1])
    err, text, _ = asyncio.run(_mcp_call(api.url, tokens[p1], "get_session", {"session": other}))
    if not err:
        raise Fail(f"{p1} could read {people[1]}'s session {other} over MCP")
    fair = ""
    if opts.with_model:
        fair = "; " + _fair_jobs(api, state, opts, tokens, people, env)
    t = time.monotonic()
    for _, name in names:
        with contextlib.suppress(Fail):
            api.call("POST", f"/api/sessions/{name}/suspend")
    for _, name in names:
        api.settle(name, opts.boot_timeout, want_sandbox=False)
        api.call("DELETE", f"/api/sessions/{name}")
    leaks: list[str] = []
    deadline = time.monotonic() + 180
    while True:
        live = []
        for sid in sandboxes:
            r = api.http.get(f"/api/sandboxes/{sid}")
            if r.status_code == 200 and r.json().get("state") not in ("stopped", "error"):
                live.append(sid)
        left = [name for _, name in names if api.http.get(f"/api/sessions/{name}").status_code != 404]
        if not live and not left:
            break
        if time.monotonic() > deadline:
            leaks = [f"{len(live)} sandboxes ({', '.join(live[:5])})", f"{len(left)} sessions"]
            break
        time.sleep(3)
    teardown = time.monotonic() - t
    note(state, "scale-smoke", timings={"wall": wall, "create_p50": pct(times, 50), "create_p95": p95,
                                        "create_max": max(times), "teardown": teardown}, sessions=n, people=k)
    if leaks:
        raise Fail("left behind after the sessions were deleted: " + ", ".join(leaks))
    if p95 > opts.scale_p95:
        raise Fail(f"create p95 {p95:.0f}s is over --scale-p95 {opts.scale_p95:.0f}s")
    return (f"{n} sessions/{k} people: create p50 {pct(times, 50):.0f}s, p95 {p95:.0f}s; {'; '.join(notes)}; "
            f"members isolated; nothing leaked ({teardown:.0f}s to tear down){fair}")


def _sc(res: Any) -> Any:
    sc = getattr(res, "structured_content", None)
    return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc


def _mcp_client(url: str, token: str) -> tuple[Any, Any]:
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    return http, Client(streamable_http_client(f"{url}/mcp", http_client=http))


async def _mcp_call(url: str, token: str, tool: str, args: dict[str, Any]) -> tuple[bool, str, Any]:
    """(is it an error, its text, its structured result) for one MCP tool call made with `token`."""
    http, client = _mcp_client(url, token)
    async with http, client as c:
        res = await c.call_tool(tool, args)
    return bool(res.is_error), " ".join(getattr(b, "text", "") for b in res.content), _sc(res)


async def _mcp_boot(url: str, token: str, name: str, env: str, timeout: float) -> tuple[float, str]:
    """A person's new_session over MCP, then get_session until it is idle with its sandbox: (seconds, sandbox)."""
    http, client = _mcp_client(url, token)
    async with http, client as c:
        t0 = time.monotonic()
        res = await c.call_tool("new_session", {"name": name, "environment": env, "idle_minutes": 0})
        if res.is_error:
            raise Fail(f"new_session {name}: {' '.join(getattr(b, 'text', '') for b in res.content)[:200]}")
        while True:
            res = await c.call_tool("get_session", {"session": name})
            s = _sc(res) or {}
            if s.get("state") == "idle" and s.get("sandbox_id") and not s.get("busy"):
                return time.monotonic() - t0, s["sandbox_id"]
            if s.get("state") in ("failed", "suspended", "archived"):
                raise Fail(f"session {name} ended {s.get('state')}: {s.get('reason')}")
            if time.monotonic() - t0 > timeout:
                raise Fail(f"session {name} still {s.get('state')} after {timeout:.0f}s")
            await asyncio.sleep(ha.POLL_SECONDS)


async def _boot_all(url: str, plan: list[tuple[str, str]], env: str, timeout: float) -> list[Any]:
    return await asyncio.gather(*(_mcp_boot(url, tok, name, env, timeout) for tok, name in plan),
                                return_exceptions=True)


def _fair_jobs(api: Api, state: State, opts: argparse.Namespace, tokens: dict[str, str], people: list[str],
               env: str) -> str:
    """A batch of short jobs: the first person submits their running-jobs cap plus two, then each other person one.
    Nobody runs more than the cap at once, and the others' jobs don't wait for the whole batch."""
    cap = int(setting(api, "HANGAR_PERSON_MAX_RUNNING_JOBS") or 0) or opts.job_cap
    body = {"instructions": "This is an automated hangar acceptance check. Reply with exactly: DONE",
            "environment": env, "deliver": "none", "job_budget": paid(opts), "labels": {LABEL: state.run},
            "timeout_minutes": 20}
    clients = {p: Api(api.url, tokens[p]) for p in people}
    ids = []
    try:
        for i in range(cap + 2):
            j = clients[people[0]].call("POST", "/api/jobs", json={**body, "title": f"hangar acceptance fair {i}"})
            state.add("jobs", j["id"])
            ids.append(j["id"])
        for p in people[1:]:
            j = clients[p].call("POST", "/api/jobs", json={**body, "title": f"hangar acceptance fair {p}"})
            state.add("jobs", j["id"])
            ids.append(j["id"])
    finally:
        for c in clients.values():
            c.http.close()
    jobs = [wait_job(api, i, opts.turn_timeout) for i in ids]
    problems = job_intervals_ok(jobs, cap)
    if problems:
        raise Fail("; ".join(problems))
    return f"{len(ids)} jobs: nobody ran more than {cap} at once, and nobody waited behind another's batch"


# ---------------------------------------------------------------------------- power, prepull
LIVE_SESSION = ("starting", "idle", "running")  # an idle session holds a sandbox too: powering down ends it
ACTIVE_JOB = ("provisioning", "running", "verifying", "delivering", "needs_input")


def _every(api: Api, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every row of a listing, page by page (X-Next-Cursor), not just its first page."""
    rows: list[dict[str, Any]] = []
    cursor = None
    for _ in range(200):
        r = api.http.get(path, params={**params, "limit": 500, **({"cursor": cursor} if cursor else {})})
        if r.status_code != 200:
            raise Fail(f"GET {path}: HTTP {r.status_code}")
        rows += r.json()
        if not (cursor := r.headers.get("X-Next-Cursor")):
            return rows
    raise Fail(f"GET {path}: more than 200 pages")


def _others_busy(api: Api, state: State) -> list[str]:
    """Other people's work the pools going down would interrupt: live sessions, active jobs and any other sandbox
    (MCP, REST, E2B). This run's own (its label, its test people, what its state recorded) doesn't count."""
    run = state.run
    own_sbs, own_sessions = set(state.get("sandboxes", [])), set(state.get("sessions", []))

    def ours(person: Any, labels: Any) -> bool:
        return str(person or "").startswith(f"accept-{run}-") or (labels or {}).get(LABEL) == run

    sessions = [s["name"] for s in _every(api, "/api/sessions", {})
                if s.get("state") in LIVE_SESSION and s["name"] not in own_sessions
                and not s["name"].startswith(f"accept-{run}") and not ours(s.get("person"), s.get("labels"))]
    jobs = [j["id"] for st in ACTIVE_JOB for j in _every(api, "/api/jobs", {"state": st})
            if not ours(j.get("person"), j.get("labels"))]
    other = [sb["id"] for sb in api.call("GET", "/api/sandboxes")
             if sb["id"] not in own_sbs and not ours(sb.get("person"), sb.get("labels"))
             and not {"hangar.session", "hangar.job"} & set(sb.get("labels") or {})]  # counted above
    return ([f"{len(sessions)} live session(s) ({', '.join(sessions[:3])})"] * bool(sessions)
            + [f"{len(jobs)} active job(s)"] * bool(jobs) + [f"{len(other)} other sandbox(es)"] * bool(other))


def _run(cmd: str, timeout: float) -> None:
    say(f"$ {cmd}")
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=timeout)
    for ln in (r.stdout + r.stderr).strip().splitlines()[-15:]:
        say(f"  {ln}")
    if r.returncode != 0:
        raise Fail(f"`{cmd}` exited {r.returncode}")


def _power(api: Api) -> dict[str, Any]:
    return api.call("GET", "/api/admin/power")["power"]


def _wait_power(api: Api, want: str, timeout: float, after: float = 0.0) -> dict[str, Any]:
    deadline, last = time.monotonic() + timeout, None
    while True:
        p = _power(api)
        if p["state"] != last:
            say(f"sandbox pools: {p['state']}" + (f" until {p['when']}" if p.get("when") else ""))
            last = p["state"]
        if p["state"] == want and (want != "up" or ((p.get("last_up") or {}).get("at") or 0) >= after):
            return p
        if time.monotonic() > deadline:
            raise Fail(f"the sandbox pools are {p['state']}, not {want}, after {timeout:.0f}s")
        time.sleep(POWER_POLL)


def power_up(api: Api, state: State, opts: argparse.Namespace, since: float) -> dict[str, Any]:
    """Bring the pools back: --power-up-cmd, else hangar's power-up job (HANGAR_POWER_UP_JOB), else power.sh up.
    A command that couldn't tell hangar (no kubectl) is followed by telling it here."""
    cmd = opts.power_up_cmd
    if not cmd and setting(api, "HANGAR_POWER_UP_JOB"):
        api.call("POST", "/api/admin/power/up")
    else:
        _run(cmd or f"bash {ROOT}/deploy/gke/power.sh up", opts.power_timeout)
        if _power(api)["state"] != "up":
            api.call("POST", "/api/admin/power/state", json={"state": "up"})
    p = _wait_power(api, "up", opts.power_timeout, after=since)
    state.set("power_cycle", {**(state.get("power_cycle") or {}), "up": True})
    return p


def _create_time(api: Api, state: State, opts: argparse.Namespace, env: str, suffix: str) -> dict[str, Any]:
    sb, secs = new_sandbox(api, state, opts, env, suffix, ttl=30)
    info = sb.get("info") or {}
    stages = [str(s[1]) for s in info.get("stages") or []]
    pulled = any("pulling the environment image" in s for s in stages)
    added = any("adding a node" in s for s in stages)
    with contextlib.suppress(Fail):
        api.call("DELETE", f"/api/sandboxes/{sb['id']}")
    return {"environment": env, "seconds": round(secs, 1), "warm_pool": bool(info.get("warm")), "pulled": pulled,
            "new_node": added}


def check_power(api: Api, state: State, opts: argparse.Namespace) -> str:
    """The power state hangar reports (ha.check_power). With --power-cycle: the pools go down (a create is then
    refused at once) and come back up, timed, with a create before and after (warm against cold)."""
    summary = ha.check_power(api, state, opts)
    if not opts.power_cycle:
        return summary
    if state.get("data:power", {}).get("state") != "up":
        raise Fail(f"--power-cycle needs the pools on to start with: {summary}")
    def quiet() -> None:
        if (busy := _others_busy(api, state)) and not opts.power_cycle_anyway:
            raise Skip(f"--power-cycle would interrupt {', '.join(busy)}: run it in a quiet window, or add "
                       "--power-cycle-anyway (power.sh checkpoints sessions first)")

    quiet()
    before = _create_time(api, state, opts, opts.env, "power-before")
    say(f"before: a create on {opts.env} took {before['seconds']}s")
    quiet()  # again: someone may have started work during that create
    t0 = time.time()
    state.set("power_cycle", {"down": t0, "up": False})
    _run(opts.power_down_cmd, opts.power_timeout)
    if _power(api)["state"] == "up":  # power.sh without kubectl doesn't tell hangar
        api.call("POST", "/api/admin/power/state", json={"state": "down"})
    _wait_power(api, "down", 120)
    down_s = time.time() - t0
    refused = ha.check_power(api, state, opts)
    t1 = time.time()
    p = power_up(api, state, opts, since=t1)
    up_s = time.time() - t1
    pre = (p.get("last_up") or {}).get("prepull") or {}
    after = _create_time(api, state, opts, opts.env, "power-after")
    say(f"after power-up: a create on {opts.env} took {after['seconds']}s (image pulled: {after['pulled']}, "
        f"new node: {after['new_node']})")
    note(state, "power", cycle={"before": before, "after": after, "prepull": pre},
         timings={"down": down_s, "up": up_s, "create_before": before["seconds"], "create_after": after["seconds"]})
    return (f"cycled: down in {down_s:.0f}s ({refused}); up in {up_s:.0f}s, prepull {pre.get('images', '?')} images "
            f"in {pre.get('seconds', '?')}s; create {before['seconds']}s before, {after['seconds']}s after")


def image_repo(ref: str) -> str:
    """`registry/name` of `registry/name:tag`, `registry/name@sha256:...` or both (a registry's port kept)."""
    ref = ref.split("@", 1)[0]
    head, _, last = ref.rpartition("/")
    return f"{head}/{last.split(':', 1)[0]}" if head else last.split(":", 1)[0]


def check_prepull(api: Api, state: State, opts: argparse.Namespace) -> str:
    """After the pools came up, the first create of each prepulled environment (--prepull-envs) needs no image pull:
    its image is on the floor node. Each is timed."""
    r = api.http.get("/api/admin/power/prepull")
    if r.status_code == 404:
        raise Skip("this hangar has no prepull (it predates the power schedule)")
    if r.status_code == 409:
        raise Skip(f"no prepull here: {r.json().get('detail') or r.text[:200]}")
    if r.status_code != 200:
        raise Fail(f"GET /api/admin/power/prepull: HTTP {r.status_code}")
    images = r.json().get("images") or []
    p = _power(api)
    last = p.get("last_up") or {}
    say(f"{len(images)} images to prepull; last power-up {time.strftime('%H:%M', time.localtime(last['at']))} "
        f"prepulled {(last.get('prepull') or {}).get('images', '?')}" if last.get("at") else
        f"{len(images)} images to prepull; no power-up recorded")
    envs = {e["name"]: e for e in api.call("GET", "/api/environments")}
    names = [x for x in dict.fromkeys((opts.prepull_envs or f"{opts.env},{opts.session_env},{opts.warm_env}")
                                      .split(",")) if x]
    problems, rows = [], []
    for name in names:
        ref = ((envs.get(name) or {}).get("images") or {}).get("kubernetes")
        if not ref:
            problems.append(f"{name} has no kubernetes image recorded")
            continue
        if not any(image_repo(i) == image_repo(ref) for i in images):
            problems.append(f"{name}'s image {image_repo(ref)} isn't in the prepull list")
            continue
        row = _create_time(api, state, opts, name, f"prepull-{len(rows)}")
        rows.append(row)
        path = "warm pool" if row["warm_pool"] else "new node" if row["new_node"] else (
            "image pulled" if row["pulled"] else "cached image")
        say(f"{name}: first create {row['seconds']}s ({path})")
        if row["pulled"] and not row["new_node"] and not row["warm_pool"]:
            problems.append(f"{name}'s first create pulled its image ({row['seconds']}s): the prepull missed it")
    note(state, "prepull", creates=rows, timings={r["environment"]: r["seconds"] for r in rows})
    if problems:
        raise Fail("; ".join(problems))
    return ", ".join(f"{r['environment']} {r['seconds']}s" + (" (new node)" if r["new_node"] else "")
                     for r in rows) + " with no image pull"


# ---------------------------------------------------------------------------- arguments and cleanup
def add_args(p: argparse.ArgumentParser) -> None:
    env = os.environ.get
    p.add_argument("--test-env-prefix", default="accept", help="prefix of the test environments the checks keep")
    p.add_argument("--egress-allow", default="example.com", help="the host the egress check's allowlist allows")
    p.add_argument("--egress-deny", default="www.iana.org", help="a host it doesn't")
    p.add_argument("--egress-direct", default="1.1.1.1:443", help="an address a direct connection must not reach")
    p.add_argument("--dns-probe", default="github.com", help="a public name a proxied sandbox must not resolve")
    p.add_argument("--service-image", default="mirror.gcr.io/library/postgres:17")
    p.add_argument("--service-wait", type=float, default=180.0)
    p.add_argument("--test-repo", default=env("HANGAR_ACCEPT_TEST_REPO"), help="OWNER/REPO for landing and secrets")
    p.add_argument("--fixture-repo", default=env("HANGAR_ACCEPT_FIXTURE_REPO"), help="OWNER/REPO for envbuild")
    p.add_argument("--fixture-push-url", default=env("HANGAR_ACCEPT_FIXTURE_PUSH_URL"))
    p.add_argument("--build-timeout", type=float, default=2400.0)
    p.add_argument("--landing-timeout", type=float, default=900.0)
    p.add_argument("--jira-project", default=env("HANGAR_ACCEPT_JIRA_PROJECT"))
    p.add_argument("--scale-sessions", type=int, default=20)
    p.add_argument("--scale-people", type=int, default=3)
    p.add_argument("--scale-env", default=None, help="default: --session-env")
    p.add_argument("--scale-p95", type=float, default=300.0, help="the slowest p95 create the scale check passes")
    p.add_argument("--max-usd", type=float, default=1.0,
                   help="what one paid job or turn may spend (its budget; hangar stops it there)")
    p.add_argument("--job-cap", type=int, default=4, help="the per-person job cap when hangar's setting is auto")
    p.add_argument("--power-cycle", action="store_true")
    p.add_argument("--power-cycle-anyway", action="store_true")
    p.add_argument("--power-down-cmd",
                   default=env("HANGAR_ACCEPT_POWER_DOWN") or f"bash {ROOT}/deploy/gke/power.sh down")
    p.add_argument("--power-up-cmd", default=env("HANGAR_ACCEPT_POWER_UP"))
    p.add_argument("--power-timeout", type=float, default=2400.0)
    p.add_argument("--prepull-envs", default=None, help="comma list; default: --env, --session-env, --warm-env")


def check_args(p: argparse.ArgumentParser, opts: argparse.Namespace) -> None:
    for flag in ("test_repo", "fixture_repo"):
        v = getattr(opts, flag)
        if v and not REPO_RE.fullmatch(v):
            p.error(f"--{flag.replace('_', '-')} is OWNER/REPO")


def _spend(api: Api, state: State) -> None:
    """What the run's agent turns cost, from its jobs' and sessions' usage, added up before cleanup removes the
    sessions; the report prints it. A job or session that used tokens without a known price is counted apart."""
    usd, unpriced, seen = 0.0, 0, {"jobs": 0, "sessions": 0}
    for kind in seen:
        for ref in state.get(kind, []):
            r = api.http.get(f"/api/{kind}/{ref}")
            if r.status_code != 200:
                continue
            seen[kind] += 1
            u = r.json().get("usage") or {}
            if u.get("cost_usd") is not None:
                usd += float(u["cost_usd"])
            elif u.get("input_tokens") or u.get("output_tokens"):
                unpriced += 1
    state.set("spend", {"usd": round(usd, 4), **seen, "unpriced": unpriced})


def cleanup_first(api: Api, state: State, attempt: Callable[[str, Callable[[], Any]], None],
                  opts: argparse.Namespace) -> None:
    """Before sessions and sandboxes go: the pools back on, jobs cancelled, PRs closed, requests answered."""
    pc = state.get("power_cycle")
    if pc and not pc.get("up"):
        attempt("powered-down sandbox pools (powering them on)",
                lambda: power_up(api, state, opts, since=float(pc.get("down") or 0)))
    for jid in state.get("jobs", []):
        def cancel(jid: str = jid) -> None:
            r = api.http.get(f"/api/jobs/{jid}")
            if r.status_code == 200 and r.json()["state"] not in TERMINAL_JOB:
                try:
                    api.call("POST", f"/api/jobs/{jid}/cancel")
                except Fail:  # it may have ended in between
                    if api.call("GET", f"/api/jobs/{jid}")["state"] not in TERMINAL_JOB:
                        raise
        attempt(f"job {jid}", cancel)
    try:
        _spend(api, state)
    except Exception as e:  # noqa: BLE001 - the report says it's unknown; cleanup goes on
        say(f"could not add up the run's spend: {e}")
    if state.get("prs"):
        gh = GitHubApi(github_token() or os.environ.get("HANGAR_ACCEPT_APPROVER_TOKEN"))
        for pr in state.get("prs", []):
            def close(pr: dict[str, Any] = pr) -> None:
                gh.req("PATCH", f"/repos/{pr['repo']}/pulls/{pr['number']}", json={"state": "closed"})
                gh.req("DELETE", f"/repos/{pr['repo']}/git/refs/heads/{pr['branch']}", ok=(204, 404, 422))
            attempt(f"PR {pr['repo']}#{pr['number']}", close)
        gh.close()
    for rid in state.get("env_requests", []):
        def deny(rid: str = rid) -> None:
            r = api.http.get(f"/api/env-requests/{rid}")
            if r.status_code == 200 and r.json().get("state") == "pending":
                api.call("POST", f"/api/env-requests/{rid}", json={"decision": "deny", "note": "acceptance cleanup"})
        attempt(f"request {rid}", deny)


def _restore_policy(api: Api, state: State, pol: dict[str, Any]) -> None:
    """Put back the test repo's landing fields, but only those that still hold what the landing check set: a change
    someone (or another run) made since stays. With no policy before, those fields are cleared, not the policy."""
    r = api.http.get("/api/repos/policy", params={"repo": pol["repo"]})
    if r.status_code == 404:
        fields: list[str] = []
    else:
        if r.status_code != 200:
            raise Fail(f"GET /api/repos/policy: HTTP {r.status_code}")
        now = r.json()
        mine = pol.get("set") or {k: now.get(k) for k in POLICY_FIELDS}  # a state file from before "set"
        fields = [k for k in POLICY_FIELDS if k in mine and now.get(k) == mine[k]]
        if changed := [k for k in POLICY_FIELDS if k in mine and k not in fields]:
            say(f"{pol['repo']}: {', '.join(changed)} changed since the landing check set them: left as they are")
    if fields and pol["old"] is None:
        api.call("DELETE", "/api/repos/policy", params={"repo": pol["repo"], "field": fields})
    elif fields:
        api.call("PUT", "/api/repos/policy", json={"repo": pol["repo"], **{k: pol["old"].get(k) for k in fields}})
    state.set("policy_restore", {**pol, "restored": True})


# ---------------------------------------------------------------------------- --- integ3: Wave 3's operator views
STATUS_SECTIONS = ("power", "loops", "harnesses", "builds", "failures", "egress", "requests", "slo", "models")


def check_status(api: Api, state: State, opts: argparse.Namespace) -> str:
    """`hangar status` (#104) answers an admin with its sections, and its `models` line (#105) says what jobs run
    on by default and whether a budget caps it. Credentials are left out: reading them may check logins."""
    r = api.http.get("/api/status", params={"section": ",".join(STATUS_SECTIONS)})
    if r.status_code == 404:
        raise Skip("this hangar has no operator console (GET /api/status, #104)")
    if r.status_code != 200:
        raise Fail(f"GET /api/status: HTTP {r.status_code}: {r.text[:200]}")
    v = r.json()
    missing = [s for s in ("power", "loops", "harnesses", "builds", "failures", "egress_denies", "requests", "slo")
               if s not in v]
    if missing:
        raise Fail(f"`hangar status` lacks {', '.join(missing)}")
    late = sorted(n for n, x in (v.get("loops") or {}).items() if not x.get("ok"))
    if late:
        say(f"loops overdue: {', '.join(late)} (hangar power status)")
    m = v.get("models") or {}
    if m.get("uncapped"):
        say(f"jobs default to {m.get('job_default')} with no job budget or daily cap (docs/upgrading.md step 8)")
    state.set("data:status", {"loops_overdue": late, "failures_24h": (v.get("failures") or {}).get("total"),
                              "models": m})
    return (f"power {v['power'].get('state')}, {len(v['loops'])} loops ({len(late)} overdue), "
            f"{(v.get('failures') or {}).get('total', 0)} failure(s) in 24h, {v['requests'].get('pending', 0)} "
            "request(s) waiting" + (f", jobs on {m.get('job_default')} "
                                    f"{'with NO budget or daily cap' if m.get('uncapped') else 'capped'}" if m else ""))


def check_clouddoctor(api: Api, state: State, opts: argparse.Namespace) -> str:
    """A Cloud Doctor dry run (#106): what a run would do with the records since the last one, by its own rules,
    starting no job, filing no request and writing no finding. A GET, so a hangar without it changes nothing."""
    r = api.http.get("/api/admin/doctor/plan")
    if r.status_code in (404, 405):
        raise Skip("this hangar has no Cloud Doctor dry run (GET /api/admin/doctor/plan, #106)")
    if r.status_code != 200:
        raise Fail(f"GET /api/admin/doctor/plan: HTTP {r.status_code}: {r.text[:200]}")
    p = r.json()
    if p.get("dry_run") is not True or not isinstance(p.get("items"), list):
        raise Fail(f"the doctor's dry run answered something else: {str(p)[:200]}")
    acts: dict[str, int] = {}
    for i in p["items"]:
        acts[i["action"]] = acts.get(i["action"], 0) + 1
        if i.get("blocked"):
            say(f"{i.get('cause')} in {i.get('repo') or i.get('environment')}: {i['blocked']}")
    state.set("data:clouddoctor", {"records": p["records"], "groups": p["groups"], "actions": acts})
    what = ", ".join(f"{n} {a}" for a, n in sorted(acts.items())) or "nothing to do"
    return f"dry run: {p['records']} new record(s) in {p['groups']} kind(s): {what}; schedule {p.get('schedule')}"


def cleanup_last(api: Api, state: State, attempt: Callable[[str, Callable[[], Any]], None],
                 opts: argparse.Namespace) -> None:
    """After sessions and sandboxes: the test people, the landing policy and the fixture repo's file."""
    for person in state.get("people", []):
        def remove(person: str = person) -> None:
            r = api.http.delete(f"/api/people/{person}")
            if r.status_code not in (200, 404):
                raise Fail(f"DELETE /api/people/{person}: HTTP {r.status_code}: {r.text[:200]}")
        attempt(f"person {person}", remove)
    pol = state.get("policy_restore")
    if pol and not pol.get("restored"):
        attempt(f"landing policy on {pol['repo']} (put back)", lambda: _restore_policy(api, state, pol))
    fx = state.get("fixture")
    if fx and not fx.get("restored") and fx.get("old") is not None:
        attempt(f"fixture config in {fx['url']} (put back)", lambda: _restore_fixture(state))


CHECKS: dict[str, Callable[[Api, State, argparse.Namespace], str]] = {
    "computer": check_computer, "egress": check_egress, "services": check_services, "envbuild": check_envbuild,
    "secrets": check_secrets, "landing": check_landing, "intake": check_intake, "scale-smoke": check_scale_smoke,
    "power": check_power, "prepull": check_prepull,
    "status": check_status, "clouddoctor": check_clouddoctor,  # --- integ3
}
