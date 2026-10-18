"""Render the front page's assets: the recording, the web UI screenshots, the inbox and the diagram.

    uv run --with playwright==1.56.0 --with asciinema==2.4.0 --with imageio-ffmpeg==0.6.0 \\
        python docs/assets/front/make.py [--out DIR] [--only diagram|ui]

It starts the demo hangar (demo.py: the real control plane on the fake provider, a scripted agent and a stand-in
GitHub) on :8787, then drives the web UI in Chromium with Playwright while a ticket becomes a sandbox run and a
draft PR. The terminal part is a real asciinema recording of `hangar inbox` against the same server, played back
with asciinema-player. Frames are captured as screenshots and encoded with ffmpeg.

Writes, next to this file (or into --out):
  hangar-demo.gif / hangar-demo.mp4      the recording (README uses the GIF, the docs site the MP4)
  ui-jobs.png, ui-sessions.png           the web UI's jobs and sessions views
  ui-fleet-{board,bays,columns,owners}.png  the Fleet page's four views (docs/console.md)
  inbox.png                              `hangar inbox` in a terminal
  hangar-suite.svg, -dark.png, -light.png  the diagram (diagram.py)
CI (.github/workflows/front-page.yml) runs this, checks the files and their sizes, and keeps them as an artifact.
"""

# ruff: noqa: E501 - the overlay's embedded CSS and JavaScript
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

import demo  # noqa: E402
import diagram  # noqa: E402

W, H = 1280, 720  # the recording and screenshot viewport
FLEET_VIEWS = ("bays", "columns", "owners", "board")  # the Board last: it is shot with a held sandbox selected
MAX_BYTES = 3_000_000  # each asset stays small enough for the repo
FONTS = (
    "https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900"
    "&family=DM+Mono:wght@400;500&family=Red+Hat+Text:wght@400..700&display=swap"
)
PLAYER = "https://cdn.jsdelivr.net/npm/asciinema-player@3.8.0/dist/bundle/asciinema-player"

# The overlay the recording draws over the web UI: captions, the opening card, the ticket and a terminal window.
# Airframe tokens (docs/stylesheets/airframe.css) in the dark flavor.
OVERLAY = r"""
(() => {
  const css = `
  #fp { position: fixed; inset: 0; pointer-events: none; z-index: 9999; font-family: "Red Hat Text", sans-serif; }
  #fp .shade { position: absolute; inset: 0; background: rgba(0,0,0,.72); opacity: 0; transition: opacity .35s; }
  #fp .shade.on { opacity: 1; }
  #fp .cap { position: absolute; left: 50%; bottom: 26px; transform: translateX(-50%); display: flex; gap: 14px;
    align-items: baseline; background: #000; color: #fff; border: 1px solid #3A3A3A; border-left: 3px solid #FFC400;
    border-radius: 2px; padding: 12px 20px; font-size: 19px; max-width: 1100px; white-space: nowrap;
    box-shadow: 0 8px 30px rgba(0,0,0,.5); opacity: 0; transition: opacity .25s; }
  #fp .cap.on { opacity: 1; }
  #fp .cap b { font: 500 14px "DM Mono", monospace; color: #FFC400; letter-spacing: .08em; }
  #fp .card { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); background: #0A0A0A;
    border: 1px solid #3A3A3A; border-radius: 2px; color: #fff; opacity: 0; transition: opacity .35s; }
  #fp .card.on { opacity: 1; }
  #fp .eyebrow { font: 500 13px "DM Mono", monospace; letter-spacing: .1em; color: #8A8A8A; }
  #fp .title { font: 700 44px "Archivo", sans-serif; font-stretch: 125%; margin: 10px 0 14px; }
  #fp .lede { font-size: 22px; color: #A6A6A6; line-height: 1.45; }
  #fp .ticket { width: 700px; padding: 26px 30px 24px; border-top: 2px solid #EE8F63; }
  #fp .ticket h3 { font: 700 26px "Archivo", sans-serif; font-stretch: 112%; margin: 12px 0 10px; }
  #fp .ticket p { font-size: 17px; color: #A6A6A6; line-height: 1.5; margin: 0 0 16px; }
  #fp .ticket code { font: 15px "DM Mono", monospace; color: #fff; }
  #fp .labels { display: flex; gap: 8px; align-items: center; min-height: 30px; }
  #fp .label { font: 500 13px "DM Mono", monospace; padding: 4px 10px; border-radius: 2px; border: 1px solid #6B6B6B;
    color: #C7C7C7; }
  #fp .label.hangar { border-color: #FFC400; color: #111213; background: #FFC400; transform: scale(.6); opacity: 0;
    transition: all .3s; }
  #fp .label.hangar.on { transform: scale(1); opacity: 1; }
  #fp .who { font-size: 14px; color: #8A8A8A; margin-left: 8px; opacity: 0; transition: opacity .3s .2s; }
  #fp .label.hangar.on + .who { opacity: 1; }
  #fp .fpterm { width: 1180px; padding: 0; overflow: hidden; }
  #fp .fpterm .fp-bar { background: #0A0A0A; font: 500 13px "DM Mono", monospace; color: #8A8A8A; padding: 10px 16px; border-bottom: 1px solid #1F1F1F; }
  #fp .fpterm .screen { padding: 12px 14px 16px; background: #000; }
  #fp .ap-wrapper, #fp .ap-player { background: #000 !important; }
  #fp .ap-control-bar { display: none !important; }`;
  const add = () => {
    if (document.getElementById("fp")) return;
    const s = document.createElement("style"); s.textContent = css; document.head.appendChild(s);
    const f = document.createElement("link"); f.rel = "stylesheet"; f.href = "%FONTS%"; document.head.appendChild(f);
    const p = document.createElement("link"); p.rel = "stylesheet"; p.href = "%PLAYER%.css"; document.head.appendChild(p);
    const root = document.createElement("div"); root.id = "fp";
    root.innerHTML = `<div class="shade"></div>
      <div class="card intro" style="width:900px;padding:44px 50px">
        <div class="eyebrow">AGENT FACTORY · RUN PLANE</div><div class="title">hangar</div>
        <div class="lede">Hand a ticket to a coding agent in an isolated cloud sandbox,<br>
        and get back a verified draft pull request.</div></div>
      <div class="card ticket">
        <div class="eyebrow">ACME/CHECKOUT-API · ISSUE #42 · OPENED BY MARIA</div>
        <h3>CSV export drops non-ASCII characters</h3>
        <p>Exporting orders to CSV loses accented names: <code>Zoë Ångström</code> comes out as
          <code>Zo ngstrm</code>. Expected: the CSV keeps UTF-8 text as is.</p>
        <div class="labels"><span class="label">bug</span><span class="label hangar">hangar</span>
          <span class="who">sean added the hangar label</span></div></div>
      <div class="card fpterm"><div class="fp-bar">~/checkout-api</div><div class="screen" id="fp-screen"></div></div>
      <div class="cap"><b></b><span></span></div>`;
    document.body.appendChild(root);
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", add); else add();
  window.fp = {
    show(sel, on = true) { document.querySelector("#fp " + sel).classList.toggle("on", on); },
    caption(step, text) {
      const c = document.querySelector("#fp .cap");
      if (!text) { c.classList.remove("on"); return; }
      c.querySelector("b").textContent = step; c.querySelector("span").textContent = text; c.classList.add("on");
    },
    play(cast) {
      return new Promise((resolve) => {
        const s = document.createElement("script"); s.src = "%PLAYER%.min.js";
        s.onload = () => {
          const p = AsciinemaPlayer.create({ data: cast }, document.getElementById("fp-screen"),
            { autoPlay: true, fit: false, terminalFontSize: "14px", controls: false, idleTimeLimit: 1,
              terminalFontFamily: "'DM Mono', ui-monospace, monospace",
              theme: "asciinema" });
          p.addEventListener("ended", () => resolve());
          setTimeout(resolve, 15000);
        };
        document.head.appendChild(s);
      });
    },
  };
})();
""".replace("%FONTS%", FONTS).replace("%PLAYER%", PLAYER)

TYPE = r"""
type() { printf '\033[1;33m$\033[0m '; s="$1"; for ((i=0;i<${#s};i++)); do printf '%s' "${s:i:1}"; sleep 0.045; done
         sleep 0.4; echo; }
type "hangar inbox"
"$HANGAR_PY" -c 'import sys; sys.argv[0] = "hangar"; from hangar.cli import app; app()' inbox
sleep 1
"""


def ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return shutil.which("ffmpeg") or sys.exit("needs ffmpeg (pip install imageio-ffmpeg, or put it on PATH)")


def render_diagram(out: Path) -> None:
    """hangar-suite.svg, and PNGs in both Airframe flavors for the README."""
    diagram.OUT = out / "hangar-suite.svg"
    diagram.main()


async def diagram_pngs(pw, out: Path) -> None:
    css = (ROOT / "docs" / "stylesheets" / "airframe.css").read_text()
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(device_scale_factor=2, viewport={"width": 1200, "height": 700})
    tab = await ctx.new_page()
    src = (out / "hangar-suite.svg").read_text()
    for theme in ("dark", "light"):
        await tab.set_content(
            f'<!doctype html><html data-theme="{theme}"><head><meta charset="utf-8">'
            f'<link rel="stylesheet" href="{FONTS}"><style>{css}'
            "html,body{margin:0;background:var(--af-canvas)} svg{display:block;width:1200px}"
            f"</style></head><body>{src}</body></html>",
            wait_until="networkidle",
        )
        await tab.evaluate("document.fonts.ready")
        await tab.locator("svg").first.screenshot(path=str(out / f"hangar-suite-{theme}.png"))
        print("wrote", out / f"hangar-suite-{theme}.png")
    await browser.close()


class Frames:
    """Screenshots taken back to back while the scenes play, with the time each was taken."""

    def __init__(self, page, folder: Path):
        self.page, self.folder, self.shots, self.on = page, folder, [], False
        self.task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while self.on:
            path = self.folder / f"{len(self.shots):05d}.png"
            t = time.monotonic()
            await self.page.screenshot(path=str(path))
            self.shots.append((path, t))

    def start(self) -> None:
        self.on = True
        self.task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self.on = False
        if self.task:
            await self.task

    def encode(self, out: Path) -> None:
        """MP4 (H.264) and an optimised GIF from the frames, each shown for as long as it was on screen."""
        listing = self.folder / "frames.txt"
        lines = []
        for (p, t), (_, nxt) in zip(self.shots, self.shots[1:] + [(None, self.shots[-1][1] + 0.1)], strict=True):
            lines += [f"file '{p}'", f"duration {max(nxt - t, 0.01):.3f}"]
        lines.append(f"file '{self.shots[-1][0]}'")
        listing.write_text("\n".join(lines) + "\n")
        exe = ffmpeg()
        src = [exe, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing)]
        subprocess.run(
            [
                *src,
                "-vf",
                "fps=15,format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                "30",
                "-movflags",
                "+faststart",
                str(out / "hangar-demo.mp4"),
            ],
            check=True,
        )
        subprocess.run(
            [
                *src,
                "-vf",
                "fps=8,scale=960:-1:flags=lanczos,split[a][b];"
                "[a]palettegen=max_colors=64:stats_mode=diff[p];"
                "[b][p]paletteuse=dither=none:diff_mode=rectangle",
                str(out / "hangar-demo.gif"),
            ],
            check=True,
        )
        print(
            "wrote",
            out / "hangar-demo.mp4",
            "and",
            out / "hangar-demo.gif",
            f"({self.shots[-1][1] - self.shots[0][1]:.1f} s, {len(self.shots)} frames)",
        )


def api():
    import httpx

    return httpx.Client(base_url=demo.URL, headers={"authorization": f"Bearer {demo.TOKEN}"}, timeout=30)


def wait_settled(timeout: float = 90) -> None:
    """The seeded work has finished: no job or session still starting or running."""
    deadline = time.time() + timeout
    with api() as c:
        while time.time() < deadline:
            jobs, sessions = c.get("/api/jobs").json(), c.get("/api/sessions").json()
            if (
                len(jobs) >= 2
                and sessions
                and all(j["state"] in ("succeeded", "failed", "cancelled", "needs_input") for j in jobs)
                and all(s["state"] in ("idle", "suspended") for s in sessions)
            ):
                return
            time.sleep(0.5)
    raise TimeoutError("the demo's seeded jobs did not settle")


def record_cast(path: Path) -> str:
    """A real asciinema recording of `hangar inbox` against the demo server."""
    script = path.with_suffix(".sh")
    script.write_text(TYPE)
    env = {
        **os.environ,
        "HANGAR_URL": demo.URL,
        "HANGAR_TOKEN": demo.TOKEN,
        "HANGAR_PY": sys.executable,
        "COLUMNS": "136",
        "TERM": "xterm-256color",
    }
    asciinema = [sys.executable, "-m", "asciinema"]
    subprocess.run(
        [*asciinema, "rec", "-q", "--overwrite", "--cols", "136", "--rows", "7", "-c", f"bash {script}", str(path)],
        env=env,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return path.read_text()


async def ui(pw, out: Path, work: Path) -> None:
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1, color_scheme="dark")
    await ctx.add_init_script(f"localStorage.setItem('hangar.token', '{demo.TOKEN}')")
    await ctx.add_init_script(OVERLAY)
    page = await ctx.new_page()
    await page.goto(f"{demo.URL}/#jobs", wait_until="networkidle")
    await page.evaluate("document.fonts.ready")
    await page.wait_for_selector("#job-list li")
    frames = Frames(page, work)
    hold = page.wait_for_timeout

    # --- 1. the opening card and the ticket
    await page.evaluate("fp.show('.shade'); fp.show('.intro')")
    await hold(400)
    frames.start()
    await hold(2600)
    await page.evaluate("fp.show('.intro', false)")
    await hold(400)
    await page.evaluate("fp.show('.ticket')")
    await hold(1600)
    await page.evaluate(
        "document.querySelector('#fp .label.hangar').classList.add('on');"
        "fp.caption('1', 'Someone adds the hangar label to a ticket')"
    )
    job_id = (await asyncio.to_thread(demo.send_ticket))["job"]
    await hold(2200)
    await page.evaluate("fp.show('.ticket', false); fp.show('.shade', false)")

    # --- 2. the job in the web UI, live
    await page.evaluate("fp.caption('2', 'hangar queues a job and starts a sandbox from the repo’s environment')")
    await page.evaluate("loadJobs(true)")
    await hold(1600)
    await page.locator("#job-list li", has_text="#42").first.click()
    seen, deadline = set(), time.time() + 90
    with api() as c:
        while time.time() < deadline:
            job = c.get(f"/api/jobs/{job_id}").json()
            state = job["state"]
            if state == "running" and "run" not in seen:
                seen.add("run")
                await page.evaluate(
                    "fp.caption('3', 'The agent works in its own sandbox: reads, tests, edits, commits')"
                )
            if state in ("verifying", "delivering") and "verify" not in seen:
                seen.add("verify")
                await page.evaluate("fp.caption('4', 'hangar runs the repo’s own check, then pushes the branch')")
            if state in ("succeeded", "failed"):
                break
            await page.evaluate("loadJobs(true)")
            if "run" in seen:  # follow the agent's activity as it arrives
                await page.evaluate("document.querySelector('.activity')?.scrollIntoView({block: 'center'})")
            await hold(700)
    if job["state"] != "succeeded" or not (job.get("result") or {}).get("pr_url"):
        raise RuntimeError(f"the demo job did not deliver a PR: {job['state']} {job.get('reason')}")
    await page.evaluate("loadJobs(true)")
    await hold(500)
    await page.evaluate("window.scrollTo(0, 0); document.querySelector('#main').scrollTo?.(0, 0)")
    pr = job["result"]["pr_number"]
    await page.evaluate(f"fp.caption('5', 'Check passed: draft PR #{pr} is open, with the agent’s summary')")
    cast = await asyncio.to_thread(record_cast, work / "inbox.cast")
    await hold(1500)

    # --- 3. the inbox in a terminal
    await page.evaluate("fp.show('.shade'); fp.show('.fpterm')")
    await page.evaluate("fp.caption('6', 'It waits in your inbox, with a question another job asked')")
    await page.evaluate("cast => fp.play(cast)", cast)
    await hold(2500)
    await frames.stop()
    await page.locator("#fp .fpterm").screenshot(path=str(out / "inbox.png"))
    print("wrote", out / "inbox.png")
    frames.encode(out)

    # --- the screenshots, without the overlay
    await page.evaluate("document.getElementById('fp').remove()")
    shot = await browser.new_context(viewport={"width": W, "height": 800}, device_scale_factor=2, color_scheme="dark")
    await shot.add_init_script(f"localStorage.setItem('hangar.token', '{demo.TOKEN}')")
    tab = await shot.new_page()
    await tab.goto(f"{demo.URL}/#jobs", wait_until="networkidle")
    await tab.evaluate("document.fonts.ready")
    await tab.locator("#job-list li", has_text="#42").first.click()
    await tab.wait_for_selector("#job-detail .kv")
    await tab.wait_for_timeout(800)
    await tab.screenshot(path=str(out / "ui-jobs.png"))
    await tab.goto(f"{demo.URL}/#sessions", wait_until="networkidle")
    await tab.locator("#session-list li").first.click()
    await tab.wait_for_timeout(1200)
    await tab.screenshot(path=str(out / "ui-sessions.png"))
    print("wrote", out / "ui-jobs.png", "and", out / "ui-sessions.png")
    # the fleet four ways; on the Board, the sandbox waiting on a person is selected, so its detail panel shows
    for view in FLEET_VIEWS:
        await tab.goto(f"{demo.URL}/#fleet?view={view}", wait_until="networkidle")
        await tab.wait_for_selector(f'#view-fleet[data-fleet-view="{view}"] #bays [data-tile]')
        if view == "board":
            await tab.locator("#bays [data-tile].af-stand--approval").first.click()
            await tab.wait_for_selector("#fleet-detail .fd")
        await tab.wait_for_timeout(800)
        await tab.screenshot(path=str(out / f"ui-fleet-{view}.png"))
        print("wrote", out / f"ui-fleet-{view}.png")
    await browser.close()


def check(out: Path, only: str | None) -> None:
    names = {
        "diagram": ["hangar-suite.svg", "hangar-suite-dark.png", "hangar-suite-light.png"],
        "ui": ["hangar-demo.gif", "hangar-demo.mp4", "ui-jobs.png", "ui-sessions.png", "inbox.png",
               *(f"ui-fleet-{v}.png" for v in sorted(FLEET_VIEWS))],
    }
    want = [n for k, v in names.items() if only in (None, k) for n in v]
    bad = []
    for n in want:
        p = out / n
        size = p.stat().st_size if p.exists() else 0
        print(f"  {n:28} {size / 1e6:6.2f} MB")
        if not 1000 < size <= MAX_BYTES:
            bad.append(f"{n}: {size} bytes")
    if bad:
        sys.exit("assets missing or over the size limit: " + "; ".join(bad))


async def main_async(out: Path, only: str | None) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        if only in (None, "diagram"):
            render_diagram(out)
            await diagram_pngs(pw, out)
        if only in (None, "ui"):
            server = subprocess.Popen(
                [sys.executable, str(HERE / "demo.py")],
                cwd=ROOT,
                env={**os.environ, "HANGAR_DEMO_PORT": str(demo.PORT)},
            )
            try:
                with api() as c:
                    for _ in range(150):
                        try:
                            if c.get("/api/jobs").status_code == 200:
                                break
                        except Exception:  # noqa: BLE001 - not listening yet
                            pass
                        await asyncio.sleep(0.2)
                await asyncio.to_thread(wait_settled)
                with tempfile.TemporaryDirectory(prefix="hangar-frames-") as work:
                    await ui(pw, out, Path(work))
            finally:
                server.terminate()
                server.wait(timeout=30)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=HERE, help="where to write the assets (default: next to this file)")
    ap.add_argument("--only", choices=["diagram", "ui"], help="render only the diagram, or only the UI assets")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    asyncio.run(main_async(a.out, a.only))
    check(a.out, a.only)


if __name__ == "__main__":
    main()
