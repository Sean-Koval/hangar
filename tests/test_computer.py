"""The agent's own computer (docs/computer-use.md): the image layer, the in-sandbox script, the screen proxy with its
view-only filter and owner scoping, and media links for PR bodies. No test needs a real browser: the ones that drive
Xvfb, ffmpeg or Chromium skip cleanly where those are missing."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hangar import computer
from hangar.computer import Bridge, RfbFilter, Screens, ScreenTicket
from hangar.envspec import EnvSpec, render
from hangar.models import Artifact, Session, SessionTurn
from tests.conftest import FakeProvider, scratch_root

SCRIPT = Path(computer.__file__).with_name("sandbox_computer.py")


# ====================================================================== the image layer
def test_computer_is_opt_in_and_renders_a_pinned_browser_layer():
    plain = render(EnvSpec(name="x"))
    assert "playwright" not in plain.dockerfile + "".join(v.decode() for v in plain.files.values())
    assert EnvSpec(name="x", computer=False).computer is None
    spec = EnvSpec(name="x", computer=True)
    assert spec.computer == computer.ComputerSpec()
    ctx = render(spec)
    steps = "\n".join(v.decode() for k, v in ctx.files.items() if k.startswith("steps/"))
    for pkg in ("xvfb", "x11vnc", "ffmpeg", "openbox"):
        assert pkg in steps
    assert f"playwright=={computer.PLAYWRIGHT_VERSION}" in steps and "install --with-deps chromium" in steps
    assert "ENV PLAYWRIGHT_BROWSERS_PATH=/opt/hangar/computer/browsers" in ctx.dockerfile
    assert "@playwright/mcp" not in steps and "node@" not in steps  # the browser MCP (and Node) only on request
    assert ctx.digest != plain.digest

    mcp = render(EnvSpec(name="x", computer={"browser_mcp": True, "resolution": "1600x900"}))
    steps = "\n".join(v.decode() for k, v in mcp.files.items() if k.startswith("steps/"))
    assert f"@playwright/mcp@{computer.PLAYWRIGHT_MCP_VERSION}" in steps and "node@22" in steps
    with pytest.raises(ValueError, match="resolution"):
        EnvSpec(name="x", computer={"resolution": "huge"})


def test_builtin_mcp_servers_and_session_env():
    from hangar.config import Settings
    from hangar.envdoctor import builtin_mcp

    settings = Settings()
    plain = builtin_mcp(EnvSpec(name="x"), settings, "/home/agent")
    assert list(plain) == ["hangar-env"] and plain["hangar-env"].args == ["mcp"]
    full = builtin_mcp(EnvSpec(name="x", computer={"browser_mcp": True}), settings, "/home/agent")
    assert full["browser"].command == "playwright-mcp" and "--no-sandbox" in full["browser"].args
    assert full["browser"].env["DISPLAY"] == ":99"
    # an environment's own server of that name wins; the doctor's server can be turned off
    mine = EnvSpec(name="x", mcp={"hangar-env": {"command": "mine"}})
    assert "hangar-env" not in builtin_mcp(mine, settings, "/home/agent")
    settings.env_doctor_mcp = False
    assert builtin_mcp(EnvSpec(name="x"), settings, "/home/agent") == {}
    assert computer.session_env(EnvSpec(name="x", computer=True)) == {"DISPLAY": ":99"}
    assert computer.session_env(EnvSpec(name="x", computer={"autostart": False})) == {}


# ====================================================================== the view-only filter
def _client_hello() -> bytes:
    return b"RFB 003.008\n" + bytes([1]) + bytes([1])  # version, security None, ClientInit (shared)


KEY = struct.pack(">BBxxI", 4, 1, 0x61)
POINTER = struct.pack(">BBHH", 5, 1, 10, 20)
FB_REQUEST = struct.pack(">BBHHHH", 3, 1, 0, 0, 1280, 800)
ENCODINGS = struct.pack(">BxH", 2, 2) + struct.pack(">ii", 0, -223)
CUT = struct.pack(">BxxxI", 6, 5) + b"hello"
PIXEL_FORMAT = bytes([0]) + bytes(19)


def test_view_only_drops_input_and_passes_what_asks_for_pixels():
    f = RfbFilter()
    resize = struct.pack(">BxHHBx", 251, 1280, 800, 1) + bytes(16)  # SetDesktopSize, one screen
    stream = _client_hello() + PIXEL_FORMAT + ENCODINGS + KEY + POINTER + CUT + resize + FB_REQUEST
    out = f.feed(stream)
    assert out == _client_hello() + PIXEL_FORMAT + ENCODINGS + FB_REQUEST
    # any split of the stream gives the same answer (messages arrive in pieces over a websocket)
    for cut in range(1, len(stream)):
        g = RfbFilter()
        assert g.feed(stream[:cut]) + g.feed(stream[cut:]) == out, cut


def test_view_only_ends_the_stream_on_what_it_cannot_parse():
    f = RfbFilter()
    f.feed(_client_hello())
    with pytest.raises(RfbFilter.Closed, match="unknown"):
        f.feed(bytes([77, 0, 0, 0]))
    g = RfbFilter()
    with pytest.raises(RfbFilter.Closed, match="security"):
        g.feed(b"RFB 003.008\n" + bytes([19]))
    h = RfbFilter()
    h.feed(_client_hello())
    with pytest.raises(RfbFilter.Closed, match="oversized"):  # a clipboard "message" that never ends
        h.feed(struct.pack(">BxxxI", 6, 1 << 30) + b"x" * (RfbFilter.MAX_PENDING + 1))
    # extended clipboard (negative length) and QEMU key events are input too
    k = RfbFilter()
    k.feed(_client_hello())
    assert k.feed(struct.pack(">Bxxxi", 6, -4) + b"abcd" + bytes([255, 0]) + bytes(10) + FB_REQUEST) == FB_REQUEST


# ====================================================================== the bridge, both ends
class FakeTerm:
    def __init__(self, chunks: list[bytes]):
        self.chunks, self.written = list(chunks), b""

    async def read(self) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    async def write(self, data: bytes) -> None:
        self.written += data

    async def close(self) -> None:
        pass


async def test_bridge_framing_ignores_noise_and_round_trips():
    payload = os.urandom(70_000)
    enc = b"D:" + base64.b64encode(payload) + b"\r\n"
    term = FakeTerm([b"stty: noise\r\nHANGAR-BRIDGE ", b"READY\r\n" + enc[:1000], enc[1000:], b""])
    b = Bridge(term)
    got = await b.ready()
    while (more := await b.read()) is not None:
        got += more
    assert got == payload
    await b.write(payload)
    lines = term.written.split(b"\n")[:-1]
    assert len(lines) == 3 and b"".join(base64.b64decode(ln[2:]) for ln in lines) == payload
    with pytest.raises(ConnectionError, match="Xvfb is not installed"):
        await Bridge(FakeTerm([b"hangar-computer: Xvfb is not installed: this environment has no computer\n"])).ready()


def _echo_server() -> tuple[socket.socket, list[bytes]]:
    srv = socket.create_server(("127.0.0.1", 0))
    got: list[bytes] = []

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.sendall(b"RFB 003.008\n")
            while data := conn.recv(65536):
                got.append(data)
                conn.sendall(data)
    threading.Thread(target=serve, daemon=True).start()
    return srv, got


def test_the_sandbox_side_of_the_bridge_is_binary_safe():
    srv, got = _echo_server()
    p = subprocess.Popen([sys.executable, str(SCRIPT), "bridge", str(srv.getsockname()[1])],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline() == b"HANGAR-BRIDGE READY\n"
        assert base64.b64decode(p.stdout.readline()[2:]) == b"RFB 003.008\n"
        blob = bytes(range(256)) * 4
        p.stdin.write(b"noise\nD:" + base64.b64encode(blob) + b"\n")
        p.stdin.flush()
        back = b""
        while len(back) < len(blob):
            back += base64.b64decode(p.stdout.readline().strip()[2:])
        assert back == blob and b"".join(got) == blob
        p.stdin.write(b"X\n")
        p.stdin.flush()
        assert p.wait(10) == 0
    finally:
        p.kill()
        srv.close()


# ====================================================================== tickets and media links
def test_screen_tickets_are_single_use_and_expire():
    s = Screens()
    t = ScreenTicket(sandbox_id="sb-1", actor="ui:x", person="alice", control=False, expires=time.time() + 60)
    raw = s.issue(t)
    assert s.redeem(raw) == t and s.redeem(raw) is None and s.redeem("") is None
    old = s.issue(t.model_copy(update={"expires": time.time() - 1}))
    assert s.redeem(old) is None
    assert raw not in json.dumps(list(s._tickets))  # only a hash of the ticket is kept


def test_media_links_are_signed_scoped_and_expire(settings):
    tok = computer.media_token(settings, "ses-1", 2, "screenshots/a.png", time.time() + 60)
    assert computer.read_media_token(settings, tok) == ("ses-1", 2, "screenshots/a.png")
    body, _, mac = tok.partition(".")
    forged = base64.urlsafe_b64encode(json.dumps(["ses-1", 2, "secret.txt", 9e12]).encode()).decode().rstrip("=")
    for bad in (f"{forged}.{mac}", f"{body}.{'0' * 32}", "", "x.y", body):
        with pytest.raises(KeyError):
            computer.read_media_token(settings, bad)
    with pytest.raises(KeyError, match="expired"):
        computer.read_media_token(settings, computer.media_token(settings, "s", 1, "a.png", time.time() - 1))


def test_pr_body_lists_screenshots_and_demos(settings):
    s = Session(name="job-x", environment="e", branch="b")
    t = SessionTurn(message="m")
    t.artifacts = [Artifact(path="screenshots/after.png", turn=1, size=2048, sha256="0", media_type="image/png"),
                   Artifact(path="demos/flow.mp4", turn=1, size=4096, sha256="1", media_type="video/mp4"),
                   Artifact(path="report.md", turn=1, size=10, sha256="2", media_type="text/markdown")]
    s.turns = [t]

    class Jobs:
        def __init__(self):
            self.settings = settings

        def _session(self, job):
            return s

    class Job:
        id = "job-12345678"

    settings.public_url = "https://hangar.example"
    lines = "\n".join(computer.pr_lines(Jobs(), Job()))
    assert "![after.png](https://hangar.example/api/media/" in lines and "[demos/flow.mp4](" in lines
    assert "report.md" not in lines
    settings.pr_media_links = "off"
    lines = "\n".join(computer.pr_lines(Jobs(), Job()))
    assert "/api/media/" not in lines and "`demos/flow.mp4`" in lines and "hangar session artifacts job-x" in lines


# ====================================================================== the screen over hangar, end to end
class VncProvider(FakeProvider):
    """A fake sandbox whose terminal runs the real bridge script against a fake VNC server."""

    vnc_port = 0
    commands: list[list[str]] = []

    async def open_terminal(self, sandbox, command, cols=120, rows=32):
        VncProvider.commands.append(command)
        p = await asyncio.create_subprocess_exec(sys.executable, str(SCRIPT), "bridge", str(self.vnc_port),
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        class Term:
            async def read(self):
                return await p.stdout.read(65536)

            async def write(self, data):
                p.stdin.write(data)
                await p.stdin.drain()

            async def close(self):
                if p.returncode is None:
                    p.kill()
                    await p.wait()
        return Term()


@pytest.fixture
def app(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        c.app.state.hangar.fleet.providers["docker"] = VncProvider()
        c.app.state.hangar.fleet.provider_errors.pop("docker", None)
        c.headers["Authorization"] = "Bearer t0ken"
        yield c


def _vnc_server() -> tuple[socket.socket, list[bytes]]:
    srv = socket.create_server(("127.0.0.1", 0))
    got: list[bytes] = []

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"RFB 003.008\n")
                while data := conn.recv(65536):
                    got.append(data)
    threading.Thread(target=serve, daemon=True).start()
    return srv, got


def _until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_watching_and_taking_over_a_screen_through_hangar(app):
    c = app
    spec = "extends: plain\ncomputer:\n  autostart: false\n"
    assert c.put("/api/environments/screen", content=spec).status_code == 200
    [sb] = c.post("/api/sandboxes", json={"environment": "screen", "name": "web"}).json()
    srv, got = _vnc_server()
    VncProvider.vnc_port = srv.getsockname()[1]
    try:
        t = c.post("/api/screen/tickets", json={"ref": sb["id"]}).json()
        assert t["url"].startswith("http://localhost:8787/ui/vnc.html#ticket=") and not t["control"]
        sent = _client_hello() + ENCODINGS + KEY + POINTER + FB_REQUEST
        with c.websocket_connect(f"/api/screen?ticket={t['ticket']}", subprotocols=["binary"]) as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"
            ws.send_bytes(sent)
            _until(lambda: b"".join(got) == _client_hello() + ENCODINGS + FB_REQUEST)
        # the ticket was used up
        with pytest.raises(WebSocketDisconnect), c.websocket_connect(f"/api/screen?ticket={t['ticket']}") as ws:
            ws.receive_bytes()
        got.clear()
        ctl = c.post("/api/screen/tickets", json={"ref": "web", "control": True}).json()
        with c.websocket_connect(f"/api/screen?ticket={ctl['ticket']}") as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"
            ws.send_bytes(sent)
            _until(lambda: b"".join(got) == sent)  # control: everything reaches the screen
        assert "bridge" in " ".join(VncProvider.commands[-1]) and "stty raw -echo" in VncProvider.commands[-1][-1]
        actions = [e["action"] for e in c.get("/api/audit").json()]
        assert {"screen.ticket", "screen.view", "screen.control"} <= set(actions)
    finally:
        srv.close()


def test_screens_are_owner_scoped_and_need_a_computer(app):
    c = app
    [plain] = c.post("/api/sandboxes", json={"environment": "plain", "name": "nocomp"}).json()
    r = c.post("/api/screen/tickets", json={"ref": plain["id"]})
    assert r.status_code == 400 and "no computer" in r.json()["error"]
    c.put("/api/environments/screen2", content="extends: plain\ncomputer:\n  autostart: false\n")
    [sb] = c.post("/api/sandboxes", json={"environment": "screen2", "name": "mine"}).json()
    login = c.post("/api/people", json={"id": "alice"}).json()["login"]
    alice = {"Authorization": f"Bearer {login['token']}"}
    r = c.post("/api/screen/tickets", json={"ref": sb["id"]}, headers=alice)
    assert r.status_code == 404 and "no session, job or sandbox" in r.json()["error"]  # not hers: not found
    assert c.post("/api/screen/tickets", json={"ref": "nope"}).status_code == 404
    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/api/screen?ticket=forged") as ws:
        ws.receive_bytes()


def test_media_route_serves_only_what_a_valid_link_names(app):
    c = app
    assert c.get("/api/media/forged.0000", headers={"Authorization": ""}).status_code == 404


# ====================================================================== the in-sandbox script, with real tools
def _run(home: Path, *args: str, env: dict | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=timeout,
                          env={"PATH": os.environ["PATH"], "HOME": str(home), **(env or {})})


def test_the_script_explains_what_is_missing(tmp_path):
    env = {"PATH": "/nonexistent", "HANGAR_DISPLAY": ":197"}
    r = subprocess.run([sys.executable, str(SCRIPT), "computer", "start"], capture_output=True, text=True,
                       env={"HOME": str(tmp_path), **env})
    assert r.returncode == 2 and "Xvfb is not installed" in r.stderr and "computer: true" in r.stderr
    r = subprocess.run([sys.executable, str(SCRIPT), "screenshot"], capture_output=True, text=True,
                       env={"HOME": str(tmp_path), **env})
    assert r.returncode == 2 and "no display is running" in r.stderr
    r = subprocess.run([sys.executable, str(SCRIPT), "record", "stop"], capture_output=True, text=True,
                       env={"HOME": str(tmp_path), **env})
    assert r.returncode == 2 and "nothing is being recorded" in r.stderr


@pytest.mark.skipif(not shutil.which("Xvfb") or not shutil.which("ffmpeg"), reason="needs Xvfb and ffmpeg")
def test_display_screenshot_and_recording_become_artifacts():
    home = Path(tempfile.mkdtemp(prefix="comp-", dir=scratch_root()))
    io = home / "io"
    (home / ".hangar").mkdir()
    (home / ".hangar/envstatus.json").write_text(json.dumps({"computer": {"resolution": "320x240",
                                                                           "record_max_mb": 5}}))
    env = {"HANGAR_DISPLAY": f":{150 + os.getpid() % 40}", "HANGAR_IO": str(io)}
    try:
        st = json.loads(_run(home, "computer", "start", "--json", env=env).stdout)
        assert st["xvfb"] and st["resolution"] == "320x240"
        assert _run(home, "computer", "start", "--quiet", env=env).returncode == 0  # idempotent
        r = _run(home, "screenshot", "--name", "desk", env=env)
        assert r.returncode == 0, r.stderr
        assert (io / "out/artifacts/screenshots/desk.png").read_bytes()[:4] == b"\x89PNG"
        assert _run(home, "record", "start", "--name", "demo", env=env).returncode == 0
        assert "already recording" in _run(home, "record", "start", env=env).stderr
        time.sleep(2)
        r = _run(home, "record", "stop", env=env)
        assert r.returncode == 0 and "handed back as demos/demo.mp4" in r.stdout, r.stderr
        assert (io / "out/artifacts/demos/demo.mp4").stat().st_size > 0
    finally:
        _run(home, "computer", "stop", env=env)
        shutil.rmtree(home, ignore_errors=True)


def _has_chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path.home() / ".cache/ms-playwright")
    return any(p.name.startswith("chromium") for p in Path(base).glob("*")) if Path(base).exists() else False


@pytest.mark.skipif(not _has_chromium(), reason="needs Playwright's Chromium")
def test_browse_reports_console_errors_and_takes_a_screenshot(tmp_path):
    page = tmp_path / "p.html"
    page.write_text("<title>Hi</title><main>ok</main><script>console.error('boom')</script>")
    r = _run(tmp_path, "browse", page.as_uri(), "--text", "--screenshot", "page",
             env={"HANGAR_IO": str(tmp_path / "io"), "HANGAR_DISPLAY": ":196",
                  **({"PLAYWRIGHT_BROWSERS_PATH": os.environ["PLAYWRIGHT_BROWSERS_PATH"]}
                     if "PLAYWRIGHT_BROWSERS_PATH" in os.environ else {})})
    out = json.loads(r.stdout)
    assert out["title"] == "Hi" and "boom" in out["console_errors"][0] and "ok" in out["text"]
    assert (tmp_path / "io/out/artifacts/screenshots/page.png").exists()
