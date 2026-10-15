#!/usr/bin/env python3
"""hangar-computer: the agent's own computer in its sandbox (docs/computer-use.md).

Installed as ~/.hangar/bin/hangar-computer (stdlib only; Playwright comes from the environment's `computer:` layer)
and reached through `hangar-agent screenshot|record|browse|computer`:

    computer start|stop|status    a virtual display (Xvfb), a window manager and a VNC server on 127.0.0.1 only
    screenshot [URL]              a page (headless Chromium) or, without a URL, the whole display
    record start|stop|status      record the display to MP4 (ffmpeg), for a demo
    browse URL                    open a page and report status, title, console errors and failed requests

Screenshots and recordings are handed back as artifacts (screenshots/, demos/) when run inside a hangar turn.
Under gVisor Chromium's own sandbox can't run (gVisor's seccomp and Chromium's seccomp-bpf don't mix), so it runs
without it, with gVisor as the boundary; a small /dev/shm makes it use /tmp instead.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".hangar", "computer")
STATUS = os.path.join(HOME, ".hangar", "envstatus.json")
ROOT = os.environ.get("HANGAR_COMPUTER_ROOT", "/opt/hangar/computer")
VENV_PY = os.path.join(ROOT, "venv", "bin", "python")
DISPLAY = os.environ.get("HANGAR_DISPLAY") or ":99"
X_SOCKET = f"/tmp/.X11-unix/X{DISPLAY.lstrip(':').split('.')[0]}"
VNC_PORT = 5900
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
BRIDGE_READY = "HANGAR-BRIDGE READY"


class Fail(Exception):
    pass


def _status() -> dict:
    try:
        with open(STATUS, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _conf() -> dict:
    c = _status().get("computer") or {}
    return c if isinstance(c, dict) else {}


def _resolution() -> tuple[int, int]:
    m = re.match(r"^(\d{3,4})x(\d{3,4})$", str(_conf().get("resolution") or "1280x800"))
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1280, 800)
    return w - w % 2, h - h % 2  # x264 needs even sizes


def _shm_mb() -> int | None:
    try:
        s = os.statvfs("/dev/shm")
        return int(s.f_frsize * s.f_blocks / (1 << 20))
    except OSError:
        return None


def _pid(name: str) -> int | None:
    """The pid recorded for one of our processes, if it is still alive."""
    try:
        with open(os.path.join(STATE, f"{name}.pid")) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _spawn(name: str, argv: list[str], env: dict | None = None) -> int:
    os.makedirs(STATE, exist_ok=True)
    log = open(os.path.join(STATE, f"{name}.log"), "ab")  # noqa: SIM115 - handed to the child
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                         env={**os.environ, **(env or {})}, close_fds=True)
    with open(os.path.join(STATE, f"{name}.pid"), "w") as f:
        f.write(str(p.pid))
    return p.pid


def _io() -> str | None:
    return os.environ.get("HANGAR_IO") or _status().get("io") or None


def hand_back(path: str, rel: str) -> str | None:
    """Copy a file into the turn's artifacts (as `hangar-agent artifact` does); None outside a hangar turn."""
    io = _io()
    if not io:
        return None
    dest = os.path.join(io, "out", "artifacts", rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not (os.path.exists(dest) and os.path.samefile(path, dest)):
        shutil.copyfile(path, dest)
    return rel


def _name(name: str | None, ext: str) -> str:
    n = name or time.strftime("%Y%m%d-%H%M%S")
    n = n[: -len(ext)] if n.endswith(ext) else n
    if not NAME_RE.match(n):
        raise Fail(f"'{n}' is not a usable name: letters, digits, '.', '_' or '-'")
    return n + ext


# ====================================================================== the display
def display_up() -> bool:
    return _pid("xvfb") is not None and os.path.exists(X_SOCKET)


def start(quiet: bool = False) -> dict:
    """Start what isn't running: Xvfb, a window manager, x11vnc bound to 127.0.0.1. Safe to repeat. Without
    x11vnc the display still works (screenshots, recordings), but nobody can watch."""
    if not shutil.which("Xvfb"):
        raise Fail("Xvfb is not installed: this environment has no computer (set `computer: true` in the "
                   "environment spec and rebuild)")
    w, h = _resolution()
    env = {"DISPLAY": DISPLAY}
    if not display_up():
        # -nolisten tcp: local clients only; no access control beyond that (one user per sandbox)
        _spawn("xvfb", ["Xvfb", DISPLAY, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp", "-dpi", "96"])
        for _ in range(100):
            if os.path.exists(X_SOCKET):
                break
            time.sleep(0.1)
        else:
            raise Fail(f"Xvfb did not start; see {STATE}/xvfb.log")
    if _pid("wm") is None:
        wm = next((w for w in ("openbox", "fluxbox", "matchbox-window-manager") if shutil.which(w)), None)
        if wm:
            _spawn("wm", [wm], env)
    if _pid("vnc") is None and shutil.which("x11vnc"):
        args = ["x11vnc", "-display", DISPLAY, "-rfbport", str(VNC_PORT), "-localhost", "-forever", "-shared",
                "-nopw", "-quiet", "-noxdamage"]
        if _status().get("runtime") == "gvisor":
            args.append("-noshm")  # MIT-SHM between processes is the part of X most likely to trip under gVisor
        _spawn("vnc", args, env)
    st = state()
    if not quiet:
        vnc = f"VNC on 127.0.0.1:{VNC_PORT}" if st["vnc"] else "no VNC server (x11vnc), so nobody can watch"
        print(f"hangar-computer: display {DISPLAY} ({w}x{h}) is up; {vnc}. Run headed apps with DISPLAY={DISPLAY}.")
    return st


def stop() -> None:
    for name in ("recorder", "vnc", "wm", "xvfb"):
        if (pid := _pid(name)) is not None:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGTERM)
    print("hangar-computer: stopped")


def state() -> dict:
    w, h = _resolution()
    return {"display": DISPLAY, "resolution": f"{w}x{h}", "xvfb": _pid("xvfb") is not None,
            "window_manager": _pid("wm") is not None, "vnc": _pid("vnc") is not None, "vnc_port": VNC_PORT,
            "recording": _recording(), "chromium": _chromium() is not None, "shm_mb": _shm_mb(),
            "runtime": _status().get("runtime")}


def _chromium() -> str | None:
    """A Chromium the environment installed for Playwright, if any."""
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(ROOT, "browsers")
    try:
        for d in sorted(os.listdir(base), reverse=True):
            for rel in ("chrome-linux/chrome", "chrome-linux64/chrome",
                        "chrome-headless-shell-linux64/chrome-headless-shell"):
                if d.startswith("chromium") and os.access(os.path.join(base, d, rel), os.X_OK):
                    return os.path.join(base, d, rel)
    except OSError:
        pass
    return None


# ====================================================================== recording
def _recording() -> dict | None:
    try:
        with open(os.path.join(STATE, "recording.json")) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if _pid("recorder") is not None else {**rec, "running": False}


def record_start(name: str | None, max_seconds: int | None) -> None:
    if not shutil.which("ffmpeg"):
        raise Fail("ffmpeg is not installed: this environment has no computer (`computer: true`)")
    if (rec := _recording()) and rec.get("running", True):
        raise Fail(f"already recording {rec['name']}; `hangar-agent record stop` first")
    if not display_up():
        start(quiet=True)
    c = _conf()
    fname = _name(name, ".mp4")
    out_dir = os.path.join(STATE, "recordings")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, fname)
    w, h = _resolution()
    secs = max(1, min(int(max_seconds or c.get("record_max_seconds") or 300), 3600))
    max_mb = max(1, int(c.get("record_max_mb") or 24))
    # Fragmented MP4: the file plays even if the recorder is killed before it finalizes. -fs keeps it under the
    # artifact size limit; -t under the time limit.
    _spawn("recorder", ["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab", "-draw_mouse", "1", "-framerate",
                        "10", "-video_size", f"{w}x{h}", "-i", DISPLAY, "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                        "-t", str(secs), "-fs", str(max_mb * 1024 * 1024), path])
    with open(os.path.join(STATE, "recording.json"), "w") as f:
        json.dump({"name": fname, "path": path, "started": time.time(), "max_seconds": secs}, f)
    print(f"hangar-computer: recording {DISPLAY} to {fname} (at most {secs}s, {max_mb} MB). Do the demo, then "
          "`hangar-agent record stop`.")


def record_stop() -> None:
    rec = _recording()
    if not rec:
        raise Fail("nothing is being recorded")
    if (pid := _pid("recorder")) is not None:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGINT)  # ffmpeg finishes the file on SIGINT
        for _ in range(150):
            if _pid("recorder") is None:
                break
            time.sleep(0.1)
        else:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)
    os.remove(os.path.join(STATE, "recording.json"))
    path = rec["path"]
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise Fail(f"the recording is empty; see {STATE}/recorder.log")
    took = int(time.time() - rec["started"])
    rel = hand_back(path, f"demos/{rec['name']}")
    where = f"handed back as {rel}" if rel else f"saved at {path}"
    print(f"hangar-computer: recorded {took}s ({os.path.getsize(path) // 1024} KB), {where}")


# ====================================================================== the browser (Playwright)
def _need_playwright(argv: list[str]):
    try:
        import playwright.sync_api as api  # noqa: F401
    except ImportError:
        if os.access(VENV_PY, os.X_OK) and os.path.realpath(sys.executable) != os.path.realpath(VENV_PY):
            os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__), *argv])
        raise Fail("Playwright is not installed: this environment has no computer (set `computer: true` in the "
                   "environment spec), or install it in your project and drive Chromium from your own tests") from None
    return api


def _launch(api_mod, p, headed: bool):
    # ECH (GREASE included) is refused by hangar's egress proxy, which must see the server name (--- egress2)
    args = ["--disable-gpu", "--disable-features=EncryptedClientHello"]
    if (_shm_mb() or 0) < 1024:
        args.append("--disable-dev-shm-usage")  # a small /dev/shm (Docker's 64 MB default) crashes renderers
    w, h = _resolution()
    if headed:
        args += [f"--window-size={w},{h}", "--window-position=0,0"]
    try:
        # chromium_sandbox=False: under gVisor Chromium's sandbox can't start; gVisor is the boundary.
        return p.chromium.launch(headless=not headed, chromium_sandbox=False, args=args,
                                 env={**os.environ, "DISPLAY": DISPLAY} if headed else None)
    except api_mod.Error as e:
        raise Fail(f"Chromium did not start: {str(e).strip()[:600]}") from None


def browse(url: str, wait_for: str | None, text: bool, shot: str | None, timeout: float, argv: list[str]) -> int:
    api = _need_playwright(argv)
    headed = display_up()  # on the display when there is one, so a person watching (VNC) or a recording sees it
    report: dict = {"url": url, "headed": headed, "console_errors": [], "page_errors": [], "failed_requests": []}
    with api.sync_playwright() as p:
        browser = _launch(api, p, headed)
        try:
            ctx = browser.new_context(no_viewport=True) if headed else browser.new_context(
                viewport={"width": _resolution()[0], "height": _resolution()[1]})
            page = ctx.new_page()
            page.on("console", lambda m: m.type == "error" and report["console_errors"].append(m.text[:500]))
            page.on("pageerror", lambda e: report["page_errors"].append(str(e)[:500]))
            page.on("requestfailed", lambda r: report["failed_requests"].append(
                f"{r.method} {r.url[:200]}: {r.failure or 'failed'}"))
            try:
                resp = page.goto(url, wait_until="load", timeout=timeout * 1000)
                if wait_for:
                    page.wait_for_selector(wait_for, timeout=timeout * 1000)
            except api.Error as e:
                report["error"] = str(e).strip().splitlines()[0][:500]
                resp = None
            report.update(status=resp.status if resp else None, final_url=page.url, title=page.title())
            if text:
                report["text"] = page.inner_text("body")[:20000] if not report.get("error") else ""
            if shot is not None:
                report["screenshot"] = _save_page_shot(page, shot)
        finally:
            browser.close()
    for k in ("console_errors", "page_errors", "failed_requests"):
        report[k] = report[k][:20]
    print(json.dumps(report, indent=2))
    return 0 if not report.get("error") and (report.get("status") or 0) < 400 else 1


def _save_page_shot(page, name: str, full_page: bool = False) -> str:
    fname = _name(name or None, ".png")
    out = os.path.join(STATE, "screenshots")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, fname)
    page.screenshot(path=path, full_page=full_page)
    return hand_back(path, f"screenshots/{fname}") or path


def screenshot(url: str | None, name: str | None, full_page: bool, wait_for: str | None, timeout: float,
               argv: list[str]) -> None:
    if url:
        api = _need_playwright(argv)
        with api.sync_playwright() as p:
            browser = _launch(api, p, headed=False)
            try:
                w, h = _resolution()
                page = browser.new_context(viewport={"width": w, "height": h}).new_page()
                try:
                    page.goto(url, wait_until="load", timeout=timeout * 1000)
                    if wait_for:
                        page.wait_for_selector(wait_for, timeout=timeout * 1000)
                except api.Error as e:
                    raise Fail(f"could not load {url}: {str(e).strip().splitlines()[0][:400]}") from None
                where = _save_page_shot(page, name or "", full_page)
            finally:
                browser.close()
        print(f"hangar-computer: screenshot of {url}: {where}")
        return
    if not display_up():
        raise Fail("no display is running: pass a URL for a page screenshot, or `hangar-agent computer start`")
    if not shutil.which("ffmpeg"):
        raise Fail("ffmpeg is not installed: this environment has no computer (`computer: true`)")
    fname = _name(name, ".png")
    out = os.path.join(STATE, "screenshots")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, fname)
    w, h = _resolution()
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab", "-video_size", f"{w}x{h}", "-i",
                        DISPLAY, "-frames:v", "1", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise Fail(f"the screenshot failed: {r.stderr.strip()[-400:]}")
    print(f"hangar-computer: screenshot of the display: {hand_back(path, f'screenshots/{fname}') or path}")


# ====================================================================== the VNC bridge
def bridge(port: int) -> int:
    """stdin/stdout <-> 127.0.0.1:port, as base64 lines ("U:<data>" in, "D:<data>" out), for hangar's VNC proxy.
    Text lines survive any terminal transport (a Kubernetes exec stream decodes output as UTF-8), and the two
    prefixes keep an echoing terminal from looping input back. Only loopback ports are reachable."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    except OSError as e:
        print(f"E:{e}", flush=True)
        return 1
    sock.settimeout(None)
    out = sys.stdout.buffer
    out.write((BRIDGE_READY + "\n").encode())
    out.flush()

    def up() -> None:
        """The viewer's side. When it ends (EOF, "X", a bad line), the connection ends both ways."""
        buf = b""
        stdin = sys.stdin.buffer
        try:
            while chunk := os.read(stdin.fileno(), 65536):
                buf += chunk.replace(b"\r", b"")
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    if ln.startswith(b"U:"):
                        sock.sendall(base64.b64decode(ln[2:], validate=True))
                    elif ln == b"X":
                        return
        except (ValueError, OSError):
            return
        finally:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    t = threading.Thread(target=up, daemon=True)
    t.start()
    try:
        while True:
            data = sock.recv(49152)
            if not data:
                break
            out.write(b"D:" + base64.b64encode(data) + b"\n")
            out.flush()
    except OSError:
        pass
    return 0


# ====================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="hangar-agent", description="The agent's own computer: a display, a browser, "
                                 "screenshots and recordings.")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("computer", help="the virtual display and VNC server")
    c.add_argument("action", choices=["start", "stop", "status"])
    c.add_argument("--json", action="store_true")
    c.add_argument("--quiet", action="store_true")
    s = sub.add_parser("screenshot", help="a page (URL) or the whole display; handed back as an artifact")
    s.add_argument("url", nargs="?")
    s.add_argument("--name", help="file name (default: a timestamp)")
    s.add_argument("--full-page", action="store_true")
    s.add_argument("--wait-for", help="CSS selector to wait for before the shot")
    s.add_argument("--timeout", type=float, default=30)
    r = sub.add_parser("record", help="record the display (MP4) for a demo; handed back as an artifact")
    r.add_argument("action", choices=["start", "stop", "status"])
    r.add_argument("--name")
    r.add_argument("--max-seconds", type=int)
    b = sub.add_parser("browse", help="open a page and report status, title, console errors and failed requests")
    b.add_argument("url")
    b.add_argument("--wait-for", help="CSS selector to wait for")
    b.add_argument("--text", action="store_true", help="include the page's visible text")
    b.add_argument("--screenshot", nargs="?", const="", default=None, metavar="NAME", help="also take a screenshot")
    b.add_argument("--timeout", type=float, default=30)
    br = sub.add_parser("bridge")  # hangar's VNC proxy; not for agents
    br.add_argument("port", type=int)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "computer":
            if a.action == "start":
                st = start(quiet=a.quiet or a.json)
            elif a.action == "stop":
                stop()
                return 0
            else:
                st = state()
            if a.json or a.action == "status":
                print(json.dumps(st, indent=2))
            return 0
        if a.cmd == "screenshot":
            screenshot(a.url, a.name, a.full_page, a.wait_for, a.timeout, argv)
            return 0
        if a.cmd == "record":
            if a.action == "start":
                record_start(a.name, a.max_seconds)
            elif a.action == "stop":
                record_stop()
            else:
                print(json.dumps(_recording() or {"recording": False}, indent=2))
            return 0
        if a.cmd == "browse":
            return browse(a.url, a.wait_for, a.text, a.screenshot, a.timeout, argv)
        if a.cmd == "bridge":
            return bridge(a.port)
    except Fail as e:
        print(f"hangar-computer: {e}", file=sys.stderr)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
