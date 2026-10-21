"""The docs' diagrams as theme-aware SVGs: hangar's place in the agent factory (docs/assets/front/hangar-suite.svg and
its stacked twin, for the README and the docs home) and the explanatory drawings the guide pages inline
(docs/assets/diagrams/*.svg).

Drawn in the style of the suite's diagram generator (Sean-Koval/agent-factory, scripts/diagrams.py): tonal cards
with 12 px corners, the product hue as a top edge clipped to those corners, the centre of a drawing marked by a 1 px
line-strong edge (never a yellow or product-colored outline, which would read as a status), and sentence-case Red Hat
Text labels. Every color is an Airframe token (`var(--af-*, <Night fallback>)`), so an SVG inlined in a docs page
follows the page's theme, and opened on its own (or as the PNGs make.py renders) it shows the dark or light flavor.

Sizes follow where each drawing is shown, so no text is under FLOOR px on screen:

- a guide drawing is WIDE units wide and fills the content column, which is 671 px at its narrowest (a 960 px
  window with the navigation and the table of contents beside it);
- its stacked twin, `name-stacked.svg`, is NARROW units wide and replaces it under 960 px
  (docs/stylesheets/product.css), where the column is 343 px at a 390 px phone;
- the suite drawing is SUITE units wide, shown at 800 px or more (the README, the docs home at 960 px and up).

A docs page includes a drawing with `--8<-- "name.svg"` inside `<figure class="af-figure af-figure--wide">` and its
twin in a second figure, `<figure class="af-figure af-figure--stacked">`, with the same caption.

    python3 docs/assets/front/diagram.py      # writes hangar-suite*.svg next to this file and docs/assets/diagrams/*.svg

CI (.github/workflows/docs.yml) runs it and fails when a committed SVG differs from what it draws; this script fails
when a drawing would show text under the floor.
"""

# ruff: noqa: E501 - the stylesheet and the drawing's coordinates read best one per line
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "hangar-suite.svg"
DIAGRAMS = HERE.parent / "diagrams"
# The Night fallbacks come from the vendored Airframe stylesheet (its first value for each token is Night), so a drawing
# opened on its own matches the synced theme; re-run this after scripts/sync-airframe.sh.
_CSS = (HERE.parents[1] / "stylesheets" / "airframe.css").read_text()
NIGHT: dict[str, str] = {}
for _name, _value in re.findall(r"--af-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{3,8})\s*;", _CSS):
    NIGHT.setdefault(_name, _value)


def with_fallbacks(svg_text: str) -> str:
    """Rewrite every var(--af-<token>, <fallback>) so the fallback is that token's Night value."""
    return re.sub(r"var\(--af-([a-z0-9-]+), [^)]+\)", lambda m: f"var(--af-{m.group(1)}, {NIGHT.get(m.group(1), '#888')})", svg_text)

FLOOR = 12  # px on screen, the smallest text anywhere
WIDE, NARROW, SUITE = 680, 350, 800  # drawing widths, in SVG units
SHOWN = {WIDE: 671, NARROW: 343, SUITE: 800}  # the narrowest each is shown at, in CSS px

# The style of agent-factory scripts/diagrams.py (theme-v13), with the body one step up so a 680-unit drawing keeps
# 12 px text at 671 px.
STYLE = """
<style>
  .af-d .bg { fill: var(--af-canvas, #000); }
  .af-d .box { fill: var(--af-raised, #141414); }
  .af-d .box-hub { fill: var(--af-raised, #141414); stroke: var(--af-line-strong, #6B6B6B); stroke-width: 1; }
  .af-d .box-plan { fill: none; stroke: var(--af-line-strong, #6B6B6B); stroke-width: 1; stroke-dasharray: 4 4; }
  .af-d .box-ext { fill: var(--af-surface, #0A0A0A); stroke: var(--af-line-strong, #6B6B6B); stroke-width: 1; }
  .af-d .person { fill: var(--af-surface, #0A0A0A); stroke: var(--af-fg-subtle, #8A8A8A); stroke-width: 1; }
  .af-d .name { font: 700 16px "Archivo", "Helvetica Neue", Arial, sans-serif; font-stretch: 125%; fill: var(--af-fg, #fff); }
  .af-d .name-s { font: 700 14px "Archivo", "Helvetica Neue", Arial, sans-serif; font-stretch: 125%; fill: var(--af-fg, #fff); }
  .af-d .title { font: 700 20px "Archivo", "Helvetica Neue", Arial, sans-serif; font-stretch: 125%; fill: var(--af-fg, #fff); }
  .af-d .body { font: 400 13px "Red Hat Text", "Helvetica Neue", Arial, sans-serif; fill: var(--af-fg-muted, #A6A6A6); }
  .af-d .lbl { font: 500 12.5px "Red Hat Text", "Helvetica Neue", Arial, sans-serif; fill: var(--af-fg-subtle, #8A8A8A); }
  .af-d .edge { fill: none; stroke: var(--af-fg-subtle, #8A8A8A); stroke-width: 1.25; }
  .af-d .edge-plan { fill: none; stroke: var(--af-fg-subtle, #8A8A8A); stroke-width: 1.25; stroke-dasharray: 5 4; }
  .af-d .elabel { font: 400 12.5px "Red Hat Text", "Helvetica Neue", Arial, sans-serif; fill: var(--af-fg-subtle, #8A8A8A); }
  .af-d .arrowhead { fill: var(--af-fg-subtle, #8A8A8A); }
  .af-d .dot { fill: var(--af-dot-field, #262626); }
</style>
"""

PRODUCT = {
    "hangar": "var(--af-product-hangar, #24C1C9)",
    "flightdeck": "var(--af-product-flightdeck, #D98CD2)",
    "tower": "var(--af-product-tower, #67B0F9)",
    "aidlc": "var(--af-product-aidlc, #69C27E)",
    # The team harness repo is not a product: it has no hue of its own.
    "harness": "var(--af-fg-subtle, #8A8A8A)",
}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg(w: int, h: int, label: str, body: list[str], pid: str, dots: tuple[int, int, int, int] | None = None) -> str:
    field = ""
    if dots:
        x, y, fw, fh = dots
        field = (f'<pattern id="{pid}-dots" width="8" height="8" patternUnits="userSpaceOnUse">'
                 f'<circle class="dot" cx="4" cy="4" r="1.1"/></pattern>'
                 f'<rect x="{x}" y="{y}" width="{fw}" height="{fh}" fill="url(#{pid}-dots)"/>')
    out = (f'<svg class="af-d" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" '
           f'aria-label="{esc(label)}">{STYLE}'
           f'<defs><marker id="{pid}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
           f'orient="auto-start-reverse"><path class="arrowhead" d="M0 0L10 5L0 10z"/></marker></defs>'
           f'<rect class="bg" width="{w}" height="{h}"/>{field}'
           + "".join(body).replace("ARROW", f"url(#{pid}-arrow)").replace("CLIP", pid) + "</svg>\n")
    out = with_fallbacks(out)
    check_floor(out, pid, w)
    return out


FONT_SIZE = re.compile(r"font(?:-size)?:[^;}]*?(\d+(?:\.\d+)?)px")


def check_floor(svg_text: str, name: str, w: int) -> None:
    """Every font size, at the narrowest width the drawing is shown at, is at least FLOOR px."""
    scale = SHOWN[w] / w
    small = sorted({float(m) for m in FONT_SIZE.findall(svg_text) if float(m) * scale < FLOOR - 1e-9})
    if small:
        raise SystemExit(f"diagram: {name} shows {small} px type at {scale:.2f}x; the floor is {FLOOR} px")


def product_edge(x, y, w, h, hue):
    """The product hue as the card's top edge, following its 12 px corners (a clipped band, not a square bar)."""
    cid = f"CLIP-pe{x}-{y}-{w}-{h}"
    return (f'<clipPath id="{cid}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/></clipPath>'
            f'<rect x="{x}" y="{y}" width="{w}" height="3" fill="{hue}" clip-path="url(#{cid})"/>')


def box(x, y, w, h, name, lines=(), eyebrow=None, kind="box", product=None, name_cls="name-s"):
    out = [f'<rect class="{kind}" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>']
    if product:
        out.append(product_edge(x, y, w, h, PRODUCT[product]))
    ty = y + 28
    if eyebrow:
        out.append(f'<text class="lbl" x="{x + 16}" y="{ty}">{esc(eyebrow)}</text>')
        ty += 22
    out.append(f'<text class="{name_cls}" x="{x + 16}" y="{ty}">{esc(name)}</text>')
    ly = ty + 22
    for line in lines:
        out.append(f'<text class="body" x="{x + 16}" y="{ly}">{esc(line)}</text>')
        ly += 18
    return "".join(out)


def tall(lines=(), eyebrow=False) -> int:
    """The height a box needs for its name, eyebrow and lines."""
    return 28 + (22 if eyebrow else 0) + 18 * len(lines) + 22


def edge(d, plan=False):
    return f'<path class="{"edge-plan" if plan else "edge"}" d="{d}" marker-end="ARROW"/>'


def text(x, y, s, cls="elabel", anchor="start", rotate=None):
    t = f' transform="rotate({rotate} {x} {y})"' if rotate is not None else ""
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}"{t}>{esc(s)}</text>'


# ------------------------------------------------------------------------------------------ hangar in the suite


def hangar_suite() -> str:
    """Where hangar sits: doors in, hangar runs it, the PR out, people; below, the three sister planes."""
    y, h, w = 104, 124, 172
    xs = (20, 216, 412, 608)
    b = [
        text(20, 44, "Where hangar sits", "title"),
        text(20, 70, "Many doors  ·  one run plane  ·  one gate", "lbl"),
        box(xs[0], y, w, h, "Doors", ["Jira, GitHub, Slack,", "MCP, webhooks,", "schedules, A2A"], eyebrow="Work in"),
        box(xs[1], y, w, h, "hangar runs it", ["A sandbox per job", "The repo's own check", "Credentials outside"], eyebrow="Run", kind="box-hub", product="hangar"),
        box(xs[2], y, w, h, "Draft PR", ["One commit, pushed", "by hangar; an", "approval lands it"], eyebrow="The gate", kind="box-ext"),
        box(xs[3], y, w, h, "People", ["Review, answer and", "approve: console,", "Slack, CLI, MCP"], eyebrow="Your team", kind="person"),
        edge(f"M{xs[0] + w} {y + h // 2}H{xs[1] - 2}"),
        edge(f"M{xs[1] + w} {y + h // 2}H{xs[2] - 2}"),
        edge(f"M{xs[2] + w} {y + h // 2}H{xs[3] - 2}"),
        # the sister planes
        box(20, 300, 240, 106, "Define", ["The harness lock, from a good", "run; ai-dlc adds a plugin"], eyebrow="Team harness repo", product="harness"),
        box(280, 300, 240, 106, "Judge", ["Reads every run from", "the outbox"], eyebrow="flightdeck", product="flightdeck"),
        box(540, 300, 240, 106, "Govern", ["Decides risky actions;", "hangar's wiring is next"], eyebrow="Tower", kind="box-plan", product="tower"),
        edge(f"M140 300V266H{xs[1] + 40}V{y + h + 2}"),
        text(150, 258, "harness lock"),
        edge(f"M{xs[1] + 120} {y + h}V298"),
        text(xs[1] + 130, 284, "outbox"),
        edge(f"M{xs[2] + 60} {y + h}V266H660V298", plan=True),
        text(668, 286, "decide?"),
        text(20, 440, "Solid: shipped. Dashed: Tower's decision API is built; hangar's wiring to it is open."),
    ]
    return svg(SUITE, 460, "Where hangar sits", b, "suite", dots=(560, 16, 220, 64))


def hangar_suite_stacked() -> str:
    x, w = 20, 310
    rows = [
        ("Work in", "Doors", ["Jira, GitHub, Slack, MCP,", "webhooks, schedules, A2A"], "box", None),
        ("Run", "hangar runs it", ["A sandbox per job, the repo's own", "check, credentials outside"], "box-hub", "hangar"),
        ("The gate", "Draft PR", ["One commit, pushed by hangar;", "an approval lands it"], "box-ext", None),
        ("Your team", "People", ["Review, answer and approve: the", "console, Slack, the CLI, MCP"], "person", None),
    ]
    b = [text(20, 40, "Where hangar sits", "title"), text(20, 64, "Many doors · one run plane · one gate", "lbl")]
    y = 88
    for i, (eyebrow, name, lines, kind, product) in enumerate(rows):
        h = tall(lines, True)
        if i:
            b.append(edge(f"M{x + 40} {y - 28}V{y - 2}"))
        b.append(box(x, y, w, h, name, lines, eyebrow=eyebrow, kind=kind, product=product))
        y += h + 28
    b.append(text(20, y + 6, "Around it", "lbl"))
    y += 22
    for eyebrow, name, lines, kind, product in (
        ("Team harness repo", "Define", ["The harness lock, from a good run;", "ai-dlc adds a workflow plugin"], "box", "harness"),
        ("flightdeck", "Judge", ["Reads every run from the outbox"], "box", "flightdeck"),
        ("Tower", "Govern", ["Decides risky actions; hangar's", "wiring to it is next (dashed)"], "box-plan", "tower"),
    ):
        h = tall(lines, True)
        b.append(box(x, y, w, h, name, lines, eyebrow=eyebrow, kind=kind, product=product))
        y += h + 14
    return svg(NARROW, y + 6, "Where hangar sits", b, "suite-stacked")


# ------------------------------------------------------------------------------------------------- guide drawings
M = 16  # a wide drawing's side margin


def concepts_map() -> str:
    """The four parts of the concepts page, as a U: people to the doors, down to the control plane, out to sandboxes."""
    w, gap = 300, WIDE - 2 * M - 2 * 300
    x1, x2 = M, M + w + gap
    t = [
        ("Who", "People and tools", ["MCP clients, the CLI, the browser;", "Jira, GitHub, Slack; services,", "CI and other agents"], "person", None),
        ("Work in", "Front doors", ["MCP, REST and the CLI; GitHub,", "Jira, Projects and Slack;", "webhooks, schedules, A2A"], "box", None),
        ("hangar", "Control plane", ["Jobs and sessions; policy, people", "and budgets; the vault and gateway;", "verify, branch, PR and landing"], "box-hub", "hangar"),
        ("Work runs", "Sandboxes", ["An environment, a harness and the", "repo's checkout, isolated: Docker,", "GKE or E2B"], "box-ext", None),
    ]
    h = tall(t[0][2], True)
    y1, y2 = 16, 16 + h + 44
    b = [
        box(x1, y1, w, h, t[0][1], t[0][2], eyebrow=t[0][0], kind=t[0][3]),
        box(x2, y1, w, h, t[1][1], t[1][2], eyebrow=t[1][0], kind=t[1][3]),
        box(x2, y2, w, h, t[2][1], t[2][2], eyebrow=t[2][0], kind=t[2][3], product=t[2][4]),
        box(x1, y2, w, h, t[3][1], t[3][2], eyebrow=t[3][0], kind=t[3][3]),
        edge(f"M{x1 + w} {y1 + h // 2}H{x2 - 2}"),
        edge(f"M{x2 + w // 2} {y1 + h}V{y2 - 2}"),
        edge(f"M{x2} {y2 + h // 2}H{x1 + w + 2}"),
        text(x2 + w // 2 + 10, y1 + h + 27, "jobs and sessions"),
        text((x1 + w + x2) // 2, y2 + h // 2 - 8, "runs", anchor="middle"),
    ]
    return svg(WIDE, y2 + h + 16, "People, front doors, the control plane and sandboxes", b, "concepts")


def control_plane() -> str:
    """Inside the control plane: two clients above, the control plane, three providers below."""
    cw = (WIDE - 2 * M - 24) // 2
    pw = (WIDE - 2 * M - 2 * 20) // 3
    px = [M + i * (pw + 20) for i in range(3)]
    b = [
        box(M, 16, cw, 74, "MCP client", ["Codex, Claude Code, Cursor: HTTP"], kind="person"),
        box(M + cw + 24, 16, cw, 74, "Browser", ["The console: fleet, approvals, terminals"], kind="person"),
        edge(f"M{M + cw // 2} 90V126"),
        edge(f"M{M + cw + 24 + cw // 2} 90V126"),
        box(M, 128, WIDE - 2 * M, 144, "Control plane", [
            "MCP tools, the REST API, websocket terminals, the audit log",
            "Fleet manager: environments (layered specs to images, warm pools),",
            "snapshots and forks, the TTL reaper",
            "Credential broker: the encrypted vault, leases, the git gateway",
        ], eyebrow="hangar", kind="box-hub", product="hangar"),
        text(px[0] + pw // 2 + 10, 266 + 50, "sandboxes, on one provider"),
        *[edge(f"M{x + pw // 2} 272V{336 - 2}") for x in px],
        box(px[0], 336, pw, 92, "Docker", ["A laptop or one VM;", "gVisor or Kata"], kind="box-ext"),
        box(px[1], 336, pw, 92, "Kubernetes", ["GKE, with the Agent", "Sandbox CRDs"], kind="box-ext"),
        box(px[2], 336, pw, 92, "E2B", ["Firecracker", "microVMs"], kind="box-ext"),
    ]
    return svg(WIDE, 444, "Inside the control plane", b, "control-plane")


def jobs_flow() -> str:
    """A job, in two rows of three: caller, queue, sandbox; agent turn, verify, draft PR; the callback back up."""
    w, gap = 186, 33
    xs = [M + i * (w + gap) for i in range(3)]
    h = 74
    y1, y2 = 44, 44 + h + 52
    row1 = (("Caller", ["POST /api/jobs"], "person"), ("Queue", ["admitted, in its turn"], "box"), ("Sandbox", ["environment, harness"], "box"))
    row2 = (("Agent turn", ["edits and tests"], "box"), ("Verify", ["the repo's own check"], "box"), ("Draft PR", ["branch hangar/<job>"], "box-ext"))
    b = []
    for i, (name, lines, kind) in enumerate(row1):
        b.append(box(xs[i], y1, w, h, name, lines, kind=kind))
        if i:
            b.append(edge(f"M{xs[i - 1] + w} {y1 + h // 2}H{xs[i] - 2}"))
    for i, (name, lines, kind) in enumerate(row2):
        b.append(box(xs[i], y2, w, h, name, lines, kind=kind))
        if i:
            b.append(edge(f"M{xs[i - 1] + w} {y2 + h // 2}H{xs[i] - 2}"))
    # the sandbox's agent turn: down from the end of the first row, back to the start of the second
    b.append(edge(f"M{xs[2] + w // 2} {y1 + h}V{y1 + h + 26}H{xs[0] + w // 2}V{y2 - 2}"))
    # the signed callback: up the right-hand side and over the top, back to the caller
    rx = xs[2] + w + 14
    b.append(edge(f"M{xs[2] + w} {y2 + h // 2}H{rx}V26H{xs[0] + w // 2}V{y1 - 2}"))
    b.append(text(xs[1] + w // 2, 16 + 2, "signed callback: job.started, job.finished", anchor="middle"))
    b.append(text(M, y2 + h + 34, "An engineer follows up (the API, MCP, the CLI, /hangar on the PR) or opens the sandbox's terminal."))
    return svg(WIDE, y2 + h + 52, "A job, from the caller to a draft PR", b, "jobs")


def a2a_flow() -> str:
    """A2A: a client sends a message, hangar runs a job, task updates stream back; the same contextId follows up."""
    w, gap = 180, (WIDE - 2 * M - 3 * 180) // 2
    xs = [M + i * (w + gap) for i in range(3)]
    y, h = 16, 92
    b = [
        box(xs[0], y, w, h, "A2A client", ["ADK, AX or any", "other client"], kind="person"),
        box(xs[1], y, w, h, "/a2a", ["SendMessage in;", "hangar admits it"], kind="box-hub", product="hangar"),
        box(xs[2], y, w, h, "Job", ["queue, sandbox, agent,", "verify, draft PR"]),
        edge(f"M{xs[0] + w} {y + h // 2}H{xs[1] - 2}"),
        edge(f"M{xs[1] + w} {y + h // 2}H{xs[2] - 2}"),
        edge(f"M{xs[2] + w // 2} {y + h}V{y + h + 40}H{xs[0] + w // 2 + 20}V{y + h + 2}"),
        text(xs[1] + w // 2, y + h + 32, "the Task, status updates (SSE), push notifications", anchor="middle"),
        edge(f"M{xs[0] + w // 2 - 20} {y + h}V{y + h + 80}H{xs[2] + w // 2 + 20}V{y + h + 2}", plan=True),
        text(xs[1] + w // 2, y + h + 72, "SendMessage, same contextId: a follow-up turn on the same job", anchor="middle"),
    ]
    return svg(WIDE, y + h + 98, "A2A: a message in, a task out", b, "a2a")


def slack_flow() -> str:
    """Slack: /hangar starts a job; the thread carries its progress; a reply in the thread is a follow-up."""
    w, gap = 196, (WIDE - 2 * M - 3 * 196) // 2
    xs = [M + i * (w + gap) for i in range(3)]
    y, h = 16, 124
    fy, fh = y + h + 64, 74
    tx = xs[2]
    b = [
        box(xs[0], y, w, h, "/hangar", ["<instructions>,", "in a channel"], kind="person"),
        box(xs[1], y, w, h, "hangar posts", ["“@you asked hangar", "to work on", "owner/repo”"], kind="box-hub", product="hangar"),
        box(tx, y, w, h, "The thread", ["Queued as HGR-JOB-W742", "Agent started", "Done: the PR, checks", "and a summary"], kind="box-ext"),
        edge(f"M{xs[0] + w} {y + h // 2}H{xs[1] - 2}"),
        edge(f"M{xs[1] + w} {y + h // 2}H{tx - 2}"),
        box(xs[1], fy, w, fh, "Follow-up turn", ["same branch and PR"]),
        # a reply in the thread starts a follow-up turn; its result goes back into the same thread
        edge(f"M{tx + 40} {y + h}V{fy + 22}H{xs[1] + w + 2}", plan=True),
        text(tx + 30, y + h + 30, "a reply in the thread", anchor="end"),
        edge(f"M{xs[1] + w} {fy + fh - 20}H{tx + 140}V{y + h + 2}"),
        text((xs[1] + w + tx + 140) // 2, fy + fh, "done, in the thread", anchor="middle"),
    ]
    return svg(WIDE, fy + fh + 16, "Slack: a command, a thread, a follow-up", b, "slack")


def landing_states() -> str:
    """How a PR lands: open, approved, armed, merged, and what sends it back to open."""
    w, gap = 136, (WIDE - 2 * M - 4 * 136) // 3
    xs = [M + i * (w + gap) for i in range(4)]
    y, h = 40, 92
    states = (("Open", ["hangar's draft,", "a review packet"], "box"),
              ("Approved", ["the head commit,", "by a person"], "box"),
              ("Armed", ["auto-merge, on", "that commit"], "box-hub"),
              ("Merged", ["the base branch's", "rules met"], "box-ext"))
    b = []
    for i, (name, lines, kind) in enumerate(states):
        b.append(box(xs[i], y, w, h, name, lines, kind=kind))
        if i:
            b.append(edge(f"M{xs[i - 1] + w} {y + h // 2}H{xs[i] - 2}"))
    b.append(text((xs[0] + w + xs[1]) // 2, y - 14, "an approver or code owner", anchor="middle"))
    b.append(text((xs[1] + w + xs[2]) // 2 + 30, y - 14, "checks green, no agent turn", anchor="middle"))
    b.append(edge(f"M{xs[2] + w // 2} {y + h}V{y + h + 34}H{xs[0] + w // 2}V{y + h + 2}"))
    b.append(text((xs[0] + xs[2] + w) // 2, y + h + 56, "disarmed, back to open: a push, changes requested,", anchor="middle"))
    b.append(text((xs[0] + xs[2] + w) // 2, y + h + 74, "a dismissed approval or a new agent turn", anchor="middle"))
    return svg(WIDE, y + h + 92, "How a PR lands", b, "landing")


# ------------------------------------------------------------------------- the stacked variants, for phones
BX, BW = 20, 310  # every stacked box's x and width
RW = 268  # the width when an edge returns up the right-hand side
RX, RT = 306, 330  # that edge's x, and its rotated label's x


def stack(y, rows, gap=34, labels=(), bw=BW):
    """(name, lines, kind, eyebrow, product) boxes one under the other with an arrow between each, and an optional
    label beside each arrow. Returns the markup, each box's top and the y below the last box."""
    out, tops = [], []
    for i, (name, lines, kind, eyebrow, product) in enumerate(rows):
        bh = tall(lines, bool(eyebrow))
        if i:
            out.append(edge(f"M{BX + 40} {y - gap}V{y - 2}"))
            if labels and labels[i - 1]:
                out.append(text(BX + 50, y - gap // 2 + 5, labels[i - 1]))
        out.append(box(BX, y, bw, bh, name, lines, eyebrow=eyebrow, kind=kind, product=product))
        tops.append((y, bh))
        y += bh + gap
    return "".join(out), tops, y - gap


def back_edge(y_from, y_to, label, label_y, plan=False):
    """An edge from a box's right side at y_from, up the right-hand margin, into a box's right side at y_to."""
    return (edge(f"M{BX + RW} {y_from}H{RX}V{y_to}H{BX + RW + 2}", plan=plan)
            + text(RT, label_y, label, anchor="middle", rotate=-90))


def concepts_map_stacked() -> str:
    body, _, end = stack(16, [
        ("People and tools", ["MCP clients, the CLI, the browser;", "Jira, GitHub, Slack; services,", "CI and other agents"], "person", "Who", None),
        ("Front doors", ["MCP, REST and the CLI; GitHub,", "Jira, Projects and Slack;", "webhooks, schedules, A2A"], "box", "Work in", None),
        ("Control plane", ["Jobs and sessions; policy, people", "and budgets; the vault and the", "gateway; verify, PR, landing"], "box-hub", "hangar", "hangar"),
        ("Sandboxes", ["An environment, a harness and the", "repo's checkout, isolated:", "Docker, GKE or E2B"], "box-ext", "Work runs", None),
    ], labels=(None, "jobs and sessions", "runs"))
    return svg(NARROW, end + 16, "People, front doors, the control plane and sandboxes", [body], "concepts-stacked")


def control_plane_stacked() -> str:
    """Two clients side by side, the control plane, three providers one under the other."""
    b = [
        box(20, 16, 150, 92, "MCP client", ["Codex, Claude", "Code, Cursor"], kind="person"),
        box(180, 16, 150, 92, "Browser", ["The console"], kind="person"),
        edge("M95 108V140"), edge("M255 108V140"),
        box(20, 142, 310, 194, "Control plane", [
            "MCP tools, the REST API,", "websocket terminals, the audit log",
            "Fleet manager: environments,", "warm pools, snapshots, forks",
            "Credential broker: the vault,", "leases, the git gateway",
        ], eyebrow="hangar", kind="box-hub", product="hangar"),
        edge("M60 336V374"),
        text(70, 360, "sandboxes, on one of"),
        box(20, 376, 310, 70, "Docker", ["A laptop or one VM; gVisor, Kata"], kind="box-ext"),
        box(20, 456, 310, 70, "Kubernetes", ["GKE, with the Agent Sandbox CRDs"], kind="box-ext"),
        box(20, 536, 310, 70, "E2B", ["Firecracker microVMs"], kind="box-ext"),
    ]
    return svg(NARROW, 622, "Inside the control plane", b, "control-plane-stacked")


def jobs_flow_stacked() -> str:
    rows = [("Caller", ["POST /api/jobs"], "person", None, None), ("Queue", ["admitted, in its turn"], "box", None, None),
            ("Sandbox", ["environment and harness"], "box", None, None), ("Agent turn", ["edits and tests"], "box", None, None),
            ("Verify", ["the repo's own check"], "box", None, None), ("Draft PR", ["branch hangar/<job>"], "box-ext", None, None)]
    body, tops, end = stack(16, rows, gap=28, bw=RW)
    first, last = tops[0], tops[-1]
    b = [body,
         back_edge(last[0] + last[1] // 2, first[0] + first[1] // 2, "signed callback: job.started, job.finished",
                   (first[0] + last[0] + last[1]) // 2),
         text(BX, end + 32, "An engineer follows up (the API, MCP,"),
         text(BX, end + 50, "the CLI, /hangar on the PR) or opens"),
         text(BX, end + 68, "the sandbox's terminal.")]
    return svg(NARROW, end + 86, "A job, from the caller to a draft PR", b, "jobs-stacked")


def a2a_flow_stacked() -> str:
    rows = [("A2A client", ["ADK, AX or any other client"], "person", None, None),
            ("/a2a", ["SendMessage in; hangar admits it"], "box-hub", None, "hangar"),
            ("Job", ["queue, sandbox, agent, verify,", "draft PR"], "box", None, None)]
    body, tops, end = stack(16, rows, gap=40, bw=RW)
    first, last = tops[0], tops[-1]
    b = [body,
         back_edge(last[0] + last[1] // 2, first[0] + first[1] // 2, "the Task and its updates (SSE)",
                   (first[0] + last[0] + last[1]) // 2),
         edge(f"M{BX + 40} {end}V{end + 30}", plan=True),
         text(BX, end + 52, "SendMessage with the same contextId:"),
         text(BX, end + 70, "a follow-up turn on the same job and branch")]
    return svg(NARROW, end + 88, "A2A: a message in, a task out", b, "a2a-stacked")


def slack_flow_stacked() -> str:
    rows = [("/hangar", ["<instructions>, in a channel"], "person", None, None),
            ("hangar posts", ["“@you asked hangar to", "work on owner/repo”"], "box-hub", None, "hangar"),
            ("The thread", ["Queued as HGR-JOB-W742", "Agent started", "Done: the PR, checks and a summary"], "box-ext", None, None)]
    body, tops, end = stack(16, rows, gap=34, bw=RW)
    thread = tops[-1]
    y2 = end + 48
    b = [body,
         edge(f"M{BX + 40} {end}V{y2 - 2}", plan=True),
         text(BX + 50, end + 29, "a reply in the thread"),
         box(BX, y2, RW, 74, "Follow-up turn", ["same branch and PR"]),
         back_edge(y2 + 37, thread[0] + thread[1] // 2, "done, in the thread", (y2 + 37 + thread[0] + thread[1] // 2) // 2)]
    return svg(NARROW, y2 + 90, "Slack: a command, a thread, a follow-up", b, "slack-stacked")


def landing_states_stacked() -> str:
    rows = [("Open", ["a draft from hangar, review packet"], "box", None, None),
            ("Approved", ["the head commit, by a person"], "box", None, None),
            ("Armed", ["auto-merge, on that commit"], "box-hub", None, None),
            ("Merged", ["the base branch's rules met"], "box-ext", None, None)]
    body, tops, end = stack(16, rows, gap=40, labels=("an approver or code owner", "checks green, no agent turn", None), bw=RW)
    op, armed = tops[0], tops[2]
    b = [body,
         back_edge(armed[0] + armed[1] // 2, op[0] + op[1] // 2, "disarmed, back to open", (op[0] + armed[0] + armed[1]) // 2),
         text(BX, end + 32, "A push, changes requested, a dismissed"),
         text(BX, end + 50, "approval or a new agent turn disarms it.")]
    return svg(NARROW, end + 68, "How a PR lands", b, "landing-stacked")


def main() -> None:
    OUT.write_text(hangar_suite())
    print("wrote", OUT)
    stacked = OUT.with_name(OUT.stem + "-stacked.svg")
    stacked.write_text(hangar_suite_stacked())
    print("wrote", stacked)
    DIAGRAMS.mkdir(parents=True, exist_ok=True)
    for name, fn in (
        ("concepts-map", concepts_map),
        ("control-plane", control_plane),
        ("jobs-flow", jobs_flow),
        ("a2a-flow", a2a_flow),
        ("slack-flow", slack_flow),
        ("landing-states", landing_states),
        ("concepts-map-stacked", concepts_map_stacked),
        ("control-plane-stacked", control_plane_stacked),
        ("jobs-flow-stacked", jobs_flow_stacked),
        ("a2a-flow-stacked", a2a_flow_stacked),
        ("slack-flow-stacked", slack_flow_stacked),
        ("landing-states-stacked", landing_states_stacked),
    ):
        (DIAGRAMS / f"{name}.svg").write_text(fn())
        print("wrote", DIAGRAMS / f"{name}.svg")


if __name__ == "__main__":
    main()
