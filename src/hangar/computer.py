"""Every agent gets its own computer: a display, a browser, screenshots, recorded demos, and a screen people can
watch or take over (docs/computer-use.md).

* **The image layer.** `computer:` in an environment spec adds Xvfb, a window manager, x11vnc, ffmpeg and Chromium
  through Playwright (pinned) to the image. It is opt-in: most environments don't need ~400 MB of browser.
* **In the sandbox.** `hangar-agent screenshot|record|browse|computer` run `sandbox_computer.py`, installed per
  sandbox like the helper (no rebuild to update it). Their files become artifacts through the context exchange.
* **The screen.** x11vnc listens on 127.0.0.1 only. A person's VNC client reaches it through hangar: a one-time
  ticket, owner-scoped like every record (people.visible), then a websocket that hangar bridges into the sandbox
  over the provider's terminal stream (`hangar-computer bridge`, base64 lines, so Kubernetes' UTF-8 exec stream
  carries it). No port is ever exposed. Watching is view-only unless the ticket asked for control: hangar drops
  the viewer's keyboard and mouse messages itself, rather than trusting the browser.
* **Out.** Screenshots and recordings show up as session events and in the job's PR body, linked with signed,
  expiring URLs (`HANGAR_PR_MEDIA_LINKS`).

Integration point (envbuilds): Xvfb, the window manager and x11vnc are started by `hangar-computer computer start`
(at claim and each turn, idempotently). When the environment's service supervisor lands, it can own those three
processes as supervised terminals instead; nothing else here changes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.envspec import EnvSpec
    from hangar.harnesses import Harness
    from hangar.models import Job, Sandbox, Session, SessionTurn

log = logging.getLogger("hangar.computer")

ROOT = "/opt/hangar/computer"
BROWSERS = f"{ROOT}/browsers"
DISPLAY = ":99"
VNC_PORT = 5900
# Verified on PyPI 2026-10-14: playwright 1.63.0 (2026-09-15) is the newest release older than two weeks (1.64.0
# landed yesterday). @playwright/mcp 0.0.83 (npm, 2026-09-28) for the optional browser MCP.
PLAYWRIGHT_VERSION = "1.63.0"
PLAYWRIGHT_MCP_VERSION = "0.0.83"
APT = ["xvfb", "x11vnc", "openbox", "ffmpeg", "xdotool", "fonts-liberation", "fonts-noto-color-emoji",
       "fonts-dejavu-core"]
SCRIPT = Path(__file__).with_name("sandbox_computer.py")
SKILL = Path(__file__).with_name("skills") / "verify-ui" / "SKILL.md"
SKILL_NAME = "hangar-verify-ui"
MEDIA_PREFIXES = ("screenshots/", "demos/")


class ComputerSpec(BaseModel):
    """`computer: true`, or a map with these fields."""

    resolution: str = "1280x800"  # the display; also the browser's window and the recording's frame
    autostart: bool = True  # start the display and VNC at claim and each turn (else `hangar-agent computer start`)
    browser_mcp: bool = False  # Playwright's MCP server for every harness (adds Node to the image)
    record_max_seconds: int = Field(300, ge=10, le=3600)

    @field_validator("resolution")
    @classmethod
    def _res(cls, v: str) -> str:
        m = re.fullmatch(r"(\d{3,4})x(\d{3,4})", v)
        if not m or not (320 <= int(m.group(1)) <= 3840 and 240 <= int(m.group(2)) <= 2160):
            raise ValueError(f"computer.resolution '{v}': use WIDTHxHEIGHT between 320x240 and 3840x2160")
        return v


def coerce(v: Any) -> Any:
    """`computer: true` -> defaults, `false`/null -> off, a map -> as given."""
    if v is True:
        return ComputerSpec()
    if v is False:
        return None
    return v


# ====================================================================== the image
def root_script(c: ComputerSpec, user: str) -> str:
    """Packages and the browser, as root. Debian/Ubuntu bases (apt); Playwright's `--with-deps` adds Chromium's
    libraries. The browsers directory belongs to the sandbox user so the optional browser MCP can add its own
    Chromium build next to it."""
    return (
        "apt-get update\n"
        f"apt-get install -y --no-install-recommends {' '.join(APT)}\n"
        f"python3 -m venv {ROOT}/venv\n"
        f"{ROOT}/venv/bin/pip install --no-cache-dir playwright=={PLAYWRIGHT_VERSION}\n"
        f"PLAYWRIGHT_BROWSERS_PATH={BROWSERS} {ROOT}/venv/bin/playwright install --with-deps chromium\n"
        f"chown -R {user}:{user} {BROWSERS} && chmod -R a+rX {ROOT}\n"
        f"{ECH_POLICY}\n"
        "rm -rf /var/lib/apt/lists/*"
    )


# --- egress2: hangar's egress proxy refuses ClientHellos with ECH (GREASE included, it looks the same), and Chromium
# sends GREASE ECH by default. A managed policy turns ECH off for every Chromium build in the image (Chromium's,
# Chrome's and Chrome for Testing's policy directories); sandbox_computer.py also passes the feature switch.
ECH_POLICY = ("for d in /etc/chromium /etc/opt/chrome /etc/opt/chrome_for_testing; do "
              "mkdir -p $d/policies/managed && "
              "echo '{\"EncryptedClientHelloEnabled\": false}' > $d/policies/managed/hangar-egress.json; done")


def user_script(c: ComputerSpec) -> str | None:
    """The optional browser MCP and the Chromium build its own Playwright wants, as the sandbox user."""
    if not c.browser_mcp:
        return None
    return (
        f"npm install -g @playwright/mcp@{PLAYWRIGHT_MCP_VERSION}\n"
        "mise reshim || true\n"
        "cli=$(find \"$(npm root -g)/@playwright/mcp\" -path '*/playwright-core/cli.js' | head -n 1)\n"
        f"PLAYWRIGHT_BROWSERS_PATH={BROWSERS} node \"$cli\" install chromium"
    )


IMAGE_ENV = f"ENV PLAYWRIGHT_BROWSERS_PATH={BROWSERS} HANGAR_COMPUTER=1"


# ====================================================================== in the sandbox
def session_env(spec: EnvSpec) -> dict[str, str]:
    """What every shell in the sandbox gets: the display, when one is started for it."""
    return {"DISPLAY": DISPLAY} if spec.computer and spec.computer.autostart else {}


def status(spec: EnvSpec, settings: Settings) -> dict[str, Any] | None:
    """The `computer` part of envstatus.json (read by the sandbox scripts)."""
    c = spec.computer
    if c is None:
        return None
    return {"enabled": True, "display": DISPLAY, "resolution": c.resolution, "vnc_port": VNC_PORT,
            "record_max_seconds": c.record_max_seconds, "browser_mcp": c.browser_mcp,
            "record_max_mb": max(1, settings.exchange_max_artifact_mb - 1)}


def builtin_mcp(spec: EnvSpec, home: str) -> dict[str, Any]:
    """Playwright's MCP server, headed on the sandbox's display (so VNC and recordings see what it does)."""
    from hangar.harnesses import McpServer

    c = spec.computer
    if c is None or not c.browser_mcp:
        return {}
    return {"browser": McpServer(command="playwright-mcp", args=[
        "--browser", "chromium", "--no-sandbox", "--isolated", "--viewport-size", c.resolution.replace("x", ","),
        "--output-dir", f"{home}/.hangar/computer/mcp"],
        env={"DISPLAY": DISPLAY, "PLAYWRIGHT_BROWSERS_PATH": BROWSERS})}


async def install(prov: Any, sb: Sandbox, spec: EnvSpec, h: Harness | None, home: str) -> None:
    """The computer script for every sandbox (it explains itself where there is no computer), and the verify-UI
    skill for the harness when there is one."""
    await prov.write_file(sb, f"{home}/.hangar/bin/hangar-computer", SCRIPT.read_bytes(), mode=0o755)
    if spec.computer is not None and h is not None and h.skills_dir:
        await prov.write_file(sb, f"{home}/{h.skills_dir}/{SKILL_NAME}/SKILL.md", SKILL.read_bytes(), mode=0o644)


def start_cmd(home: str) -> str:
    """Start the display in the background: the exec returns at once, the processes outlive it."""
    return f"nohup {home}/.hangar/bin/hangar-computer computer start --quiet >/dev/null 2>&1 </dev/null &"


# ====================================================================== media out: events and the PR body
def is_media(path: str, media_type: str | None) -> bool:
    return path.startswith(MEDIA_PREFIXES) or (media_type or "").startswith(("image/", "video/"))


def note_media(svc: Any, s: Session, turn: SessionTurn) -> None:
    """A session event for each screenshot or recording the turn handed back (never raises)."""
    from hangar.events import RunEvent
    from hangar.exchange import artifact_uri

    try:
        n = next(i for i, t in enumerate(s.turns, 1) if t is turn)
        evs = [RunEvent(kind="progress", turn=n, run_id=turn.run_id or "",
                        text=f"{'recording' if a.media_type.startswith('video/') else 'screenshot'} handed back: "
                             f"{a.path}", data={"artifact": artifact_uri(s, a), "media_type": a.media_type,
                                                "size": a.size})
               for a in turn.artifacts if is_media(a.path, a.media_type)]
        if evs:
            svc.activity.append(s.id, evs)
    except Exception:  # noqa: BLE001 - events are best effort; the artifacts are kept either way
        log.warning("session %s: could not record media events", s.id, exc_info=True)


def _media_key(settings: Settings, key: bytes | None = None) -> bytes:
    from hangar import keys  # --- durable: the newest vault key signs; any not yet retired checks (a rotation)

    return hmac.new(key or keys.key_for(settings), b"hangar-media", hashlib.sha256).digest()


def media_token(settings: Settings, session_id: str, turn: int, path: str, expires: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps([session_id, turn, path, int(expires)],
                                               separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_media_key(settings), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{mac}"


def read_media_token(settings: Settings, token: str) -> tuple[str, int, str]:
    """(session id, turn, path) of a valid, unexpired link. Raises KeyError for anything else (one answer)."""
    body, _, mac = (token or "").partition(".")
    from hangar import keys

    if not body or not any(hmac.compare_digest(mac, hmac.new(_media_key(settings, k), body.encode(),
                                                             hashlib.sha256).hexdigest()[:32])
                           for k in keys.verify_keys(settings)):
        raise KeyError("no such link")
    try:
        sid, turn, path, exp = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError) as e:
        raise KeyError("no such link") from e
    if time.time() > exp:
        raise KeyError("this link has expired; ask for the artifact through hangar")
    return str(sid), int(turn), str(path)


def pr_lines(jobs: Any, job: Job) -> list[str]:
    """Screenshots and demos the job's agent handed back, for the PR body. By default, their names and how to fetch
    them through hangar, signed in and owner-scoped: a PR on a private repo is read by more people than may see the
    job, and a link that is its own credential would hand them its screens (code, data, an app's admin pages).
    HANGAR_PR_MEDIA_LINKS=signed embeds them through expiring links instead."""
    settings = jobs.settings
    s = jobs._session(job)
    if s is None:
        return []
    media = [(n, a) for n, t in enumerate(s.turns, 1) for a in t.artifacts if is_media(a.path, a.media_type)]
    if not media:
        return []
    media = media[-12:]
    lines = ["", "**Screenshots and demos** (handed back by the agent)", ""]
    if settings.pr_media_links != "signed":
        lines += [f"- `{a.path}` (turn {n}, {a.size // 1024} KB)" for n, a in media]
        lines.append(f"\nSigned in to hangar (the job's owner or an admin): `hangar session artifacts {s.name} --get "
                     f"<path>`, MCP `get_artifact`, or `GET /api/jobs/{job.id}/artifacts/<turn>/<path>`.")
        return lines
    exp = time.time() + settings.pr_media_link_days * 86400
    base = settings.public_url.rstrip("/")
    for n, a in media:
        url = f"{base}/api/media/{media_token(settings, s.id, n, a.path, exp)}"
        name = a.path.rsplit("/", 1)[-1]
        lines.append(f"![{name}]({url})" if a.media_type.startswith("image/") else f"- [{a.path}]({url}) (video)")
    lines.append(f"\n_Links expire in {settings.pr_media_link_days} days._")
    return lines


# ====================================================================== the screen: tickets and the proxy
class ScreenTicket(BaseModel):
    sandbox_id: str
    actor: str
    person: str | None
    control: bool
    expires: float
    session: str | None = None


class Screens:
    """One-time tickets and live viewers. In memory: the control plane is one process, and a ticket lives 60 s."""

    TTL = 60.0
    MAX_VIEWERS = 4  # per sandbox

    def __init__(self) -> None:
        self._tickets: dict[str, ScreenTicket] = {}
        self.viewers: dict[str, int] = {}

    def issue(self, t: ScreenTicket) -> str:
        now = time.time()
        for k in [k for k, v in self._tickets.items() if v.expires < now]:
            del self._tickets[k]
        raw = secrets.token_urlsafe(24)
        self._tickets[hashlib.sha256(raw.encode()).hexdigest()] = t
        return raw

    def redeem(self, raw: str) -> ScreenTicket | None:
        t = self._tickets.pop(hashlib.sha256((raw or "").encode()).hexdigest(), None)
        return t if t is not None and t.expires >= time.time() else None


def bridge_cmd(home: str) -> list[str]:
    """Raw mode first, so nothing hangar writes is echoed or cooked; the display is started if it isn't up."""
    tool = f"{home}/.hangar/bin/hangar-computer"
    return ["sh", "-c", f"stty raw -echo 2>/dev/null; {tool} computer start --quiet >/dev/null 2>&1; "
                        f"exec {tool} bridge {VNC_PORT}"]


READY = b"HANGAR-BRIDGE READY"
UP_CHUNK = 3000  # bytes per line to the sandbox: 4 KB of base64, under a terminal's canonical line limit


class RfbFilter:
    """Client-to-server RFB with the viewer's input removed (view-only watching).

    Parses the client's side of the protocol (RFC 6143 plus the extensions noVNC sends) message by message and
    passes only what asks for pixels: version, security choice, ClientInit, SetPixelFormat, SetEncodings,
    FramebufferUpdateRequest, EnableContinuousUpdates and Fence. Keys, pointer, clipboard, desktop resizing and
    anything unknown are dropped; an unknown message type ends the stream (it can't be skipped safely)."""

    MAX_PENDING = 1 << 20

    class Closed(Exception):
        pass

    def __init__(self) -> None:
        self.buf = b""
        self.phase = "version"
        self.v33 = False

    def feed(self, data: bytes) -> bytes:
        self.buf += data
        if len(self.buf) > self.MAX_PENDING:
            raise self.Closed("viewer sent an oversized message")
        out = bytearray()
        while True:
            n, keep = self._next()
            if n == 0:
                break
            if keep:
                out += self.buf[:n]
            self.buf = self.buf[n:]
        return bytes(out)

    def _next(self) -> tuple[int, bool]:
        b = self.buf
        if self.phase == "version":
            if len(b) < 12:
                return 0, True
            self.v33 = b[:12] == b"RFB 003.003\n"
            self.phase = "init" if self.v33 else "security"
            return 12, True
        if self.phase == "security":
            if not b:
                return 0, True
            if b[0] not in (1, 2):  # None, or VNC auth (16-byte response follows)
                raise self.Closed(f"unsupported security type {b[0]}")
            if b[0] == 2:
                self.phase = "auth"
            else:
                self.phase = "init"
            return 1, True
        if self.phase == "auth":
            if len(b) < 16:
                return 0, True
            self.phase = "init"
            return 16, True
        if self.phase == "init":
            if not b:
                return 0, True
            self.phase = "messages"
            return 1, True
        if not b:
            return 0, True
        t = b[0]
        fixed = {0: (20, True), 3: (10, True), 4: (8, False), 5: (6, False), 150: (10, True), 250: (4, False)}
        if t in fixed:
            size, keep = fixed[t]
            return (size, keep) if len(b) >= size else (0, True)
        if t == 2:  # SetEncodings: 4 + 4 * count
            if len(b) < 4:
                return 0, True
            size = 4 + 4 * struct.unpack(">H", b[2:4])[0]
            return (size, True) if len(b) >= size else (0, True)
        if t == 6:  # ClientCutText: 8 + length (negative: extended clipboard, its absolute value)
            if len(b) < 8:
                return 0, True
            size = 8 + abs(struct.unpack(">i", b[4:8])[0])
            return (size, False) if len(b) >= size else (0, True)
        if t == 248:  # Fence: 9 + payload length
            if len(b) < 9:
                return 0, True
            size = 9 + b[8]
            return (size, True) if len(b) >= size else (0, True)
        if t == 251:  # SetDesktopSize: 8 + 16 * screens
            if len(b) < 8:
                return 0, True
            size = 8 + 16 * b[6]
            return (size, False) if len(b) >= size else (0, True)
        if t == 255:  # QEMU client messages: extended key events (12 bytes)
            if len(b) < 2:
                return 0, True
            if b[1] == 0:
                return (12, False) if len(b) >= 12 else (0, True)
        raise self.Closed(f"unknown viewer message type {t}")


class Bridge:
    """The provider's terminal stream carrying base64 lines to and from `hangar-computer bridge`: "D:" lines from
    the sandbox, "U:" lines to it, so a terminal that echoes (no `stty`) can't feed the viewer its own input."""

    def __init__(self, term: Any):
        self.term = term
        self.pending = b""

    async def ready(self, timeout: float = 30.0) -> bytes:
        """Wait for the bridge to connect to the VNC server. Returns any data that came with it."""
        deadline = time.monotonic() + timeout
        while READY not in self.pending:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("the screen did not come up in the sandbox (is `computer:` set?)")
            chunk = await asyncio.wait_for(self.term.read(), left)
            if not chunk:
                tail = self.pending.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
                raise ConnectionError(f"the screen bridge ended: {tail[0][:200]}")
            self.pending += chunk
            if len(self.pending) > 1 << 16 and READY not in self.pending:
                raise ConnectionError("the screen bridge printed something unexpected")
        self.pending = self.pending.split(READY, 1)[1]
        return self._frames()

    def _frames(self) -> bytes:
        *lines, self.pending = self.pending.replace(b"\r", b"").split(b"\n")
        out = bytearray()
        for ln in lines:
            if ln.startswith(b"D:"):
                with contextlib.suppress(ValueError):
                    out += base64.b64decode(ln[2:], validate=True)
        return bytes(out)

    async def read(self) -> bytes | None:
        """The next server bytes (possibly b""), or None when the stream ended."""
        chunk = await self.term.read()
        if not chunk:
            return None
        self.pending += chunk
        if len(self.pending) > 8 << 20:
            raise ConnectionError("the screen bridge sent an oversized line")
        return self._frames()

    async def write(self, data: bytes) -> None:
        for i in range(0, len(data), UP_CHUNK):
            await self.term.write(b"U:" + base64.b64encode(data[i:i + UP_CHUNK]) + b"\n")
