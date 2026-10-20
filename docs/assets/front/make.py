"""Render the front page's assets: two recordings, the console screenshots, the inbox and the diagram.

    uv run --with playwright==1.56.0 --with asciinema==2.4.0 --with imageio-ffmpeg==0.6.0 \\
        python docs/assets/front/make.py [--out DIR] [--only diagram|ui|console]

It starts the demo hangar (demo.py: the real control plane on the fake provider, a scripted agent and a stand-in
GitHub) on :8787, then drives the console in Chromium with Playwright while a ticket becomes a sandbox run and a
draft PR. The inbox still is a real asciinema recording of `hangar inbox` against the same server, played back with
asciinema-player. The console stills and the review-loop recording show a team's afternoon (fixture.py: four
environments, six people, held tool calls, a question, a failed run, a PR to review) served over the same console
with Playwright's request interception. Frames are captured as screenshots and encoded with ffmpeg; every still is
1440x900 at 2x, Night.

The demo recording opens on the product: the ticket card over the live console for a moment, then the console at
full brightness; while the agent works the frames follow the job's detail panel so its text stays readable in the
960 px GIF. The console stills are cropped to what their caption names (the Needs-you cards, the Fleet board with its
docked panel, the diff with a comment), at 2x, so they read at a README's width.

Writes, next to this file (or into --out):
  hangar-demo.gif / .mp4        a ticket becomes a sandbox run and a draft PR (README: the GIF; docs: the MP4)
  hangar-review.gif / .mp4      the review loop: decide what is held, read the change, comment back to the agent
  ui-jobs.png, ui-sessions.png  the console's Jobs and Sessions views, from the demo run
  ui-fleet-board.png            the Fleet's Board with an agent open in place, from the demo run (docs/console.md)
  ui-needs.png, ui-fleet.png, ui-review.png   Needs you (the deck), the Fleet with its deck, and the Diff, from the fixture
  inbox.png                     `hangar inbox` in a terminal
  hangar-suite.svg, -dark.png, -light.png     the diagram (diagram.py)
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
from fixture import Fixture  # noqa: E402

W, H = 1440, 900  # the recordings' viewport; the demo run's stills are the same frame at 2x
ZOOM = (1040, 650)  # the demo recording's close-up while the agent works: the job's detail panel, same aspect as W x H
NARROW = 820  # the viewport for the Needs-you and Diff crops: one column, so the crop is 820 px wide at 1x
DOCKED = 1280  # the Fleet still: the deck beside its side column and the rows at their full ten columns
VIDEO = (1280, 800)  # the MP4's size; the GIF is 960 wide
MAX_SECONDS = 20  # the demo recording's length
FLEET_VIEWS = ("board",)  # the Board, with an agent open in place (ui-fleet.png, from the fixture, is the List)
MAX_BYTES = 3_000_000  # each asset stays small enough for the repo
DEMO_PACE = "0.6"  # the scripted agent's pauses, so the ticket's run fits a 20 s recording
FONTS = (
    "https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900"
    "&family=DM+Mono:wght@400;500&family=Red+Hat+Text:wght@400..700&display=swap"
)
PLAYER = "https://cdn.jsdelivr.net/npm/asciinema-player@3.8.0/dist/bundle/asciinema-player"

# The overlay the recording draws over the web UI: captions, the ticket and a terminal window.
# Airframe tokens (docs/stylesheets/airframe.css) in the dark flavor.
OVERLAY = r"""
(() => {
  const css = `
  #fp { position: fixed; inset: 0; pointer-events: none; z-index: 9999; font-family: var(--af-font-sans); }
  #fp .shade { position: absolute; inset: 0; background: color-mix(in srgb, var(--af-canvas) 72%, transparent); opacity: 0; transition: opacity .35s; }
  #fp .shade.on { opacity: 1; }
  #fp .cap { position: absolute; left: 50%; bottom: 28px; transform: translateX(-50%); display: flex; gap: 14px;
    align-items: baseline; background: var(--af-raised); color: var(--af-fg); border: 1px solid var(--af-line-strong);
    border-left: 3px solid var(--af-brand); border-radius: var(--af-radius-control); padding: 12px 20px; font-size: 19px;
    max-width: 1200px; white-space: nowrap; box-shadow: var(--af-shadow-overlay); opacity: 0; transition: opacity .25s; }
  #fp .cap.on { opacity: 1; }
  #fp .cap b { font: 500 14px var(--af-font-mono); color: var(--af-brand-text); letter-spacing: .08em; }
  #fp .card { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); background: var(--af-raised);
    border: 1px solid var(--af-line-strong); border-radius: var(--af-radius-card); color: var(--af-fg); opacity: 0; transition: opacity .35s; }
  #fp .card.on { opacity: 1; }
  #fp .eyebrow { font: 500 13px var(--af-font-mono); letter-spacing: .1em; color: var(--af-fg-subtle); }
  #fp .ticket { width: 720px; padding: 26px 30px 24px; border-top: 2px solid var(--af-fg); }
  #fp .ticket h3 { font: 600 26px var(--af-font-display); font-stretch: var(--af-display-stretch); margin: 12px 0 10px; }
  #fp .ticket p { font-size: 17px; color: var(--af-fg-muted); line-height: 1.5; margin: 0 0 16px; }
  #fp .ticket code { font: 15px var(--af-font-mono); color: var(--af-fg); }
  #fp .labels { display: flex; gap: 8px; align-items: center; min-height: 30px; }
  #fp .label { font: 500 13px var(--af-font-mono); padding: 4px 10px; border-radius: var(--af-radius-control); border: 1px solid var(--af-line-strong);
    color: var(--af-fg-muted); }
  #fp .label.hangar { border-color: var(--af-brand); color: var(--af-on-brand); background: var(--af-brand); transform: scale(.6); opacity: 0;
    transition: all .3s; }
  #fp .label.hangar.on { transform: scale(1); opacity: 1; }
  #fp .who { font-size: 14px; color: var(--af-fg-subtle); margin-left: 8px; opacity: 0; transition: opacity .3s .2s; }
  #fp .label.hangar.on + .who { opacity: 1; }
  #fp .fpterm { width: 1180px; padding: 0; overflow: hidden; background: var(--af-terminal-bg); }
  #fp .fpterm .fp-bar { font: 500 13px var(--af-font-mono); color: var(--af-terminal-dim); padding: 10px 16px; border-bottom: 1px solid color-mix(in srgb, var(--af-terminal-fg) 9%, transparent); }
  #fp .fpterm .screen { padding: 12px 14px 16px; }
  #fp .ap-wrapper, #fp .ap-player { background: var(--af-terminal-bg) !important; }
  #fp .ap-control-bar { display: none !important; }`;
  const add = () => {
    if (document.getElementById("fp")) return;
    const s = document.createElement("style"); s.textContent = css; document.head.appendChild(s);
    const f = document.createElement("link"); f.rel = "stylesheet"; f.href = "%FONTS%"; document.head.appendChild(f);
    const p = document.createElement("link"); p.rel = "stylesheet"; p.href = "%PLAYER%.css"; document.head.appendChild(p);
    const root = document.createElement("div"); root.id = "fp";
    root.innerHTML = `<div class="shade"></div>
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
    place(clip) {  // keep the caption inside a close-up of the viewport (null: the whole viewport)
      const c = document.querySelector("#fp .cap");
      if (!clip) { c.style.left = ""; c.style.bottom = ""; c.style.maxWidth = ""; return; }
      c.style.left = (clip.x + clip.width / 2) + "px";
      c.style.bottom = (innerHeight - clip.y - clip.height + 28) + "px";
      c.style.maxWidth = (clip.width - 40) + "px";
    },
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
        folder.mkdir(parents=True, exist_ok=True)
        self.page, self.folder, self.shots, self.on = page, folder, [], False
        self.task: asyncio.Task | None = None
        self.clip: dict | None = None  # a close-up: the part of the viewport each frame shows, same aspect as W x H

    async def _loop(self) -> None:
        while self.on:
            path = self.folder / f"{len(self.shots):05d}.png"
            t = time.monotonic()
            await self.page.screenshot(path=str(path), clip=self.clip)
            self.shots.append((path, t))

    async def zoom(self, selector: str | None) -> None:
        """Follow an element: frames show the ZOOM-sized part of the viewport around it (None: the whole viewport)."""
        if not selector:
            self.clip = None
            await self.page.evaluate("fp.place(null)")
            return
        box = await self.page.locator(selector).first.bounding_box()
        zw, zh = ZOOM
        x = min(max(box["x"] - 24, 0), W - zw) if box else W - zw
        y = min(max(box["y"] - 24, 0), H - zh) if box else 0
        self.clip = {"x": x, "y": y, "width": zw, "height": zh}
        await self.page.evaluate("c => fp.place(c)", self.clip)  # the caption moves into the close-up

    def start(self) -> None:
        self.on = True
        self.task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self.on = False
        if self.task:
            await self.task

    def seconds(self) -> float:
        return self.shots[-1][1] - self.shots[0][1]

    def encode(self, out: Path, name: str = "hangar-demo") -> None:
        """MP4 (H.264) and an optimised GIF from the frames, each shown for as long as it was on screen; close-up
        frames are scaled to the same size as the others."""
        listing = self.folder / "frames.txt"
        lines = []
        for (p, t), (_, nxt) in zip(self.shots, self.shots[1:] + [(None, self.shots[-1][1] + 0.1)], strict=True):
            lines += [f"file '{p}'", f"duration {max(nxt - t, 0.01):.3f}"]
        lines.append(f"file '{self.shots[-1][0]}'")
        listing.write_text("\n".join(lines) + "\n")
        exe = ffmpeg()
        # one lossless pass brings every frame (full viewport or close-up) to the video's size at 15 fps; the MP4
        # and the GIF are made from it, since palettegen cannot follow a frame-size change mid-stream
        uniform = self.folder / "uniform.mkv"
        subprocess.run(
            [exe, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-vf", f"scale={VIDEO[0]}:{VIDEO[1]}:flags=lanczos,fps=15", "-c:v", "ffv1", str(uniform)],
            check=True,
        )
        src = [exe, "-y", "-loglevel", "error", "-i", str(uniform)]
        subprocess.run(
            [*src, "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "30", "-movflags", "+faststart",
             str(out / f"{name}.mp4")],
            check=True,
        )
        subprocess.run(
            [
                *src,
                "-vf",
                "fps=8,scale=960:-1:flags=lanczos,split[a][b];"
                "[a]palettegen=max_colors=64:stats_mode=diff[p];"
                "[b][p]paletteuse=dither=none:diff_mode=rectangle",
                str(out / f"{name}.gif"),
            ],
            check=True,
        )
        print(
            "wrote",
            out / f"{name}.mp4",
            "and",
            out / f"{name}.gif",
            f"({self.seconds():.1f} s, {len(self.shots)} frames)",
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

    # --- 1. the ticket, over the live console (no title card: the banner above the recording already says it)
    await page.evaluate("fp.show('.shade'); fp.show('.ticket')")
    await hold(300)
    frames.start()
    await hold(1200)
    await page.evaluate(
        "document.querySelector('#fp .label.hangar').classList.add('on');"
        "fp.caption('1', 'Someone adds the hangar label to a ticket')"
    )
    job_id = (await asyncio.to_thread(demo.send_ticket))["job"]
    await hold(1500)
    await page.evaluate("fp.show('.ticket', false); fp.show('.shade', false)")

    # --- 2. the job in the web UI, live
    await page.evaluate("fp.caption('2', 'hangar queues a job and starts a sandbox from the repo’s environment')")
    await page.evaluate("loadJobs(true)")
    await hold(1200)
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
                await frames.zoom("#job-detail")  # the close-up: the job's panel, so its text stays readable
            if state in ("verifying", "delivering") and "verify" not in seen:
                seen.add("verify")
                await page.evaluate("fp.caption('4', 'hangar runs the repo’s own check, then pushes the branch')")
            if state in ("succeeded", "failed"):
                break
            await page.evaluate("loadJobs(true)")
            if "run" in seen:  # follow the agent's activity as it arrives
                await page.evaluate("document.querySelector('.activity')?.scrollIntoView({block: 'center'})")
            await hold(600)
    if job["state"] != "succeeded" or not (job.get("result") or {}).get("pr_url"):
        raise RuntimeError(f"the demo job did not deliver a PR: {job['state']} {job.get('reason')}")
    await page.evaluate("loadJobs(true)")
    await hold(400)
    await page.evaluate("window.scrollTo(0, 0); document.querySelector('#main').scrollTo?.(0, 0)")
    pr = job["result"]["pr_number"]
    await page.evaluate(f"fp.caption('5', 'Check passed: draft PR #{pr} is open, with the agent’s summary')")
    await hold(1600)

    # --- 3. what waits on a person: the Needs-you view (the PR to review, a question another job asked)
    await frames.zoom(None)
    await page.evaluate("location.hash = '#needs'")
    await hold(300)
    await page.evaluate("fp.caption('6', 'It waits in Needs you, with a question another job asked')")
    await hold(1800)
    await frames.stop()
    if frames.seconds() > MAX_SECONDS:
        raise RuntimeError(f"the demo recording runs {frames.seconds():.1f} s; it must open on the product and end within {MAX_SECONDS} s")
    frames.encode(out, "hangar-demo")

    # --- the inbox from the CLI, as a still: a real asciinema recording played in the overlay's terminal
    cast = await asyncio.to_thread(record_cast, work / "inbox.cast")
    await page.evaluate("fp.caption('', ''); fp.show('.shade'); fp.show('.fpterm')")
    await page.evaluate("cast => fp.play(cast)", cast)
    await hold(2500)
    await page.locator("#fp .fpterm").screenshot(path=str(out / "inbox.png"))
    print("wrote", out / "inbox.png")

    # --- the stills, without the overlay: 1440x900 at 2x
    shot = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2, color_scheme="dark")
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
    # the fleet both ways; on the Board, the first agent opens in place into its console
    for view in FLEET_VIEWS:
        await tab.goto(f"{demo.URL}/#fleet?view={view}", wait_until="networkidle")
        await tab.wait_for_selector(f'#view-fleet[data-fleet-view="{view}"] #fleet-body .af-agent')
        if view == "board":
            await tab.locator("#fleet-body .af-agent__face").first.click()
            await tab.wait_for_selector("#fleet-body .af-agent.is-open .af-console")
        await tab.wait_for_timeout(800)
        await tab.screenshot(path=str(out / f"ui-fleet-{view}.png"))
        print("wrote", out / f"ui-fleet-{view}.png")
    await browser.close()


async def _console_page(ctx, fx: Fixture, hash_: str):
    """The console on the fixture: the real page, the afternoon's data."""
    page = await ctx.new_page()
    await fx.wire(page)
    await page.goto(f"{demo.URL}/#{hash_}", wait_until="networkidle")
    await page.evaluate("document.fonts.ready")
    await page.wait_for_timeout(700)
    return page


async def _crop(page, path: Path, first: str, last: str, pad: int = 12) -> None:
    """A still of the page from the top of `first` to the bottom of `last`, the viewport's full width, at 2x. The
    sticky top bar stays at the top of the document, so it is not painted into the middle of a crop lower down."""
    await page.add_style_tag(content=".topbar { position: absolute !important; }")
    await page.evaluate("window.scrollTo(0, 0)")
    box = await page.evaluate(
        """([a, b, pad]) => {
            const t = document.querySelector(a).getBoundingClientRect(), u = document.querySelector(b).getBoundingClientRect();
            const y = Math.max(window.scrollY + t.top - pad, 0);
            return {x: 0, y, width: innerWidth, height: window.scrollY + u.bottom + pad - y};
        }""",
        [first, last, pad],
    )
    await page.screenshot(path=str(path), clip=box, full_page=True)
    print("wrote", path, f"({box['width']:.0f}x{box['height']:.0f} at 2x)")


async def _fixture_context(browser, width: int):
    ctx = await browser.new_context(viewport={"width": width, "height": H}, device_scale_factor=2, color_scheme="dark")
    await ctx.add_init_script(f"localStorage.setItem('hangar.token', '{demo.TOKEN}')")
    return ctx


async def console(pw, out: Path) -> None:
    """Needs you (the decision deck), the Fleet with its deck and the first rows, and the Diff with a comment: stills at 2x,
    each cropped to what its caption names, from a viewport narrow enough that the crop reads at a README's width."""
    browser = await pw.chromium.launch()
    fx = Fixture()
    narrow = await _fixture_context(browser, NARROW)
    page = await _console_page(narrow, fx, "needs")
    # the page header and the deck: the held push on top, the next ones peeking, the up-next list
    await _crop(page, out / "ui-needs.png", "#view-needs .af-page-head", "#needs-deck .af-deck")
    await page.close()
    page = await _console_page(narrow, fx, "jobs")
    await page.locator("#job-list li", has_text="Push the refunds").first.click()
    await page.click("[data-diff-show]")
    await page.wait_for_timeout(800)
    await page.hover(".af-diff__line--add >> nth=0")
    await page.click(".af-diff__line--add >> nth=0 >> [data-comment]")
    await page.fill("[data-thread] textarea", "Cache the check; this path runs per request.")
    await page.click("[data-add-comment]")
    await page.wait_for_timeout(400)
    # the change: its files, the diff and the comment on its line, with the sticky bar painted where it sits
    await _crop(page, out / "ui-review.png", "#job-detail h4:has(+ .changes)", "[data-thread]")
    await page.close()
    await narrow.close()
    docked = await _fixture_context(browser, DOCKED)
    page = await _console_page(docked, fx, "fleet")
    # the headline, the deck band and the first agents of the first environment
    await _crop(page, out / "ui-fleet.png", "#view-fleet .af-page-head", "#fleet-body .af-runway .af-agent:nth-child(5)")
    await page.close()
    await docked.close()
    await browser.close()


async def review(pw, out: Path, work: Path) -> None:
    """The review loop in one take: decide a held call in Needs you, open the job, read the change, comment back."""
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1, color_scheme="dark")
    await ctx.add_init_script(f"localStorage.setItem('hangar.token', '{demo.TOKEN}')")
    await ctx.add_init_script(OVERLAY)
    fx = Fixture()
    page = await _console_page(ctx, fx, "needs")
    hold = page.wait_for_timeout
    frames = Frames(page, work)
    frames.start()
    await page.evaluate("fp.caption('1', 'Needs you: one decision at a time, soonest to expire on top')")
    await hold(2200)
    await page.locator('#needs-deck [data-dk="go"]', has_text="Shrink the KV cache").click()
    await hold(700)
    top = page.locator('#needs-deck .af-decision[data-k="0"]')
    await top.locator('[data-dk="allow_always"]').hover()
    await hold(500)
    await top.locator('[data-dk="allow_always"]').click()
    await page.evaluate("fp.caption('2', 'Allow the edit for this turn: the agent carries on, the next card rises')")
    await hold(4200)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.locator('#needs-deck .af-decision', has_text="git push origin main").first.locator("[data-go]").click()
    await hold(900)
    await page.evaluate("fp.caption('3', 'The held push waits on its job, with the change the agent made so far')")
    await hold(1500)
    await page.click("[data-diff-show]")
    await hold(900)
    await page.evaluate("document.querySelector('.changes').scrollIntoView({block: 'start'}); window.scrollBy(0, -96)")
    await page.evaluate("fp.caption('4', 'Read the change as a greyscale diff, file by file')")
    await hold(1700)
    await page.hover(".af-diff__line--add >> nth=0")
    await hold(400)
    await page.click(".af-diff__line--add >> nth=0 >> [data-comment]")
    await hold(400)
    await page.evaluate("fp.caption('5', 'A comment on a line goes back to the agent with your next message')")
    await page.type("[data-thread] textarea", "Cache the check; this path runs per request.", delay=28)
    await hold(300)
    await page.click("[data-add-comment]")
    await hold(1400)
    await page.click("[data-send-comments]")
    await page.evaluate("fp.caption('6', 'Sent: the agent reads it at its next step and the turn continues')")
    await hold(2200)
    await frames.stop()
    frames.encode(out, "hangar-review")
    await browser.close()


def check(out: Path, only: str | None) -> None:
    names = {
        "diagram": ["hangar-suite.svg", "hangar-suite-dark.png", "hangar-suite-light.png"],
        "ui": ["hangar-demo.gif", "hangar-demo.mp4", "ui-jobs.png", "ui-sessions.png", "inbox.png",
               *(f"ui-fleet-{v}.png" for v in sorted(FLEET_VIEWS))],
        "console": ["ui-needs.png", "ui-fleet.png", "ui-review.png", "hangar-review.gif", "hangar-review.mp4"],
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
        if only in (None, "ui", "console"):
            server = subprocess.Popen(
                [sys.executable, str(HERE / "demo.py")],
                cwd=ROOT,
                env={**os.environ, "HANGAR_DEMO_PORT": str(demo.PORT), "DEMO_PACE": DEMO_PACE},
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
                    if only in (None, "ui"):
                        await ui(pw, out, Path(work) / "demo")
                    if only in (None, "console"):
                        await console(pw, out)
                        await review(pw, out, Path(work) / "review")
            finally:
                server.terminate()
                server.wait(timeout=30)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=HERE, help="where to write the assets (default: next to this file)")
    ap.add_argument("--only", choices=["diagram", "ui", "console"],
                    help="render only the diagram, only the demo run's recording and stills, or only the fixture's")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    asyncio.run(main_async(a.out, a.only))
    check(a.out, a.only)


if __name__ == "__main__":
    main()
