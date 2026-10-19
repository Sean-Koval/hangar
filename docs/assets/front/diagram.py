"""The docs' diagrams as theme-aware SVGs: hangar's place in the agent factory (docs/assets/front/hangar-suite.svg,
for the README and the docs home) and the explanatory drawings the guide pages inline (docs/assets/diagrams/*.svg).

Drawn with the helpers of the suite's diagram generator (Sean-Koval/agent-factory, scripts/diagrams.py), copied
here so this repo renders on its own: every color is an Airframe token (`var(--af-*, <dark fallback>)`), so an SVG
inlined in a docs page follows the page's theme, and opened on its own (or as the PNGs make.py renders) it shows
the dark or light flavor. Drawings are 800 units wide, so at a docs page's 710 px their 12.5 px body text is
11 px; a docs page includes one with `--8<-- "name.svg"` inside `<figure class="af-figure af-figure--wide">`. Each
guide drawing also has a stacked variant, `name-stacked.svg`, 350 units wide, that phones and narrow tablets show
instead (under 960 px, docs/stylesheets/product.css) so its text stays 11 px or larger at 390 px; the page includes it in a second figure,
`<figure class="af-figure af-figure--stacked">`, with the same caption.

    python3 docs/assets/front/diagram.py      # writes hangar-suite.svg next to this file and docs/assets/diagrams/*.svg

CI (.github/workflows/docs.yml) runs it and fails when a committed SVG differs from what it draws.
"""

# ruff: noqa: E501 - the vendored stylesheet and the drawing's coordinates read best one per line
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "hangar-suite.svg"
DIAGRAMS = HERE.parent / "diagrams"

# ---------------------------------------------------------------- from agent-factory scripts/diagrams.py (unchanged)
STYLE = """
<style>
  .af-d .bg { fill: var(--af-canvas, #000); }
  .af-d .box { fill: var(--af-raised, #141414); }
  .af-d .box-hub { fill: var(--af-raised, #141414); stroke: var(--af-brand-line, #FFC400); stroke-width: 1.5; }
  .af-d .box-plan { fill: none; stroke: var(--af-line-strong, #6B6B6B); stroke-width: 1; stroke-dasharray: 4 4; }
  .af-d .box-ext { fill: var(--af-surface, #0A0A0A); stroke: var(--af-line-strong, #6B6B6B); stroke-width: 1; }
  .af-d .person { fill: var(--af-surface, #0A0A0A); stroke: var(--af-fg-subtle, #8A8A8A); stroke-width: 1; }
  .af-d .bound { fill: none; stroke: var(--af-rule, #3A3A3A); stroke-width: 1; stroke-dasharray: 2 5; }
  .af-d .name { font: 700 15px "Archivo", "Helvetica Neue", Arial, sans-serif; font-stretch: 125%; fill: var(--af-fg, #fff); }
  .af-d .name-s { font: 700 13px "Archivo", "Helvetica Neue", Arial, sans-serif; font-stretch: 125%; fill: var(--af-fg, #fff); }
  .af-d .title { font: 700 20px "Archivo", "Helvetica Neue", Arial, sans-serif; font-stretch: 125%; fill: var(--af-fg, #fff); }
  .af-d .body { font: 400 12.5px "Red Hat Text", "Helvetica Neue", Arial, sans-serif; fill: var(--af-fg-muted, #A6A6A6); }
  .af-d .lbl { font: 500 10.5px "DM Mono", ui-monospace, Menlo, monospace; letter-spacing: .08em; fill: var(--af-fg-subtle, #8A8A8A); }
  .af-d .edge { fill: none; stroke: var(--af-fg-subtle, #8A8A8A); stroke-width: 1.25; }
  .af-d .edge-plan { fill: none; stroke: var(--af-fg-subtle, #8A8A8A); stroke-width: 1.25; stroke-dasharray: 5 4; }
  .af-d .elabel { font: 400 11.5px "Red Hat Text", "Helvetica Neue", Arial, sans-serif; fill: var(--af-fg-subtle, #8A8A8A); }
  .af-d .arrowhead { fill: var(--af-fg-subtle, #8A8A8A); }
  .af-d .dot { fill: var(--af-dot-field, #262626); }
</style>
"""
# hangar's own addition, for the stacked (phone) drawings: an eyebrow one size up, so it stays 11 px at 390
STYLE_STACKED = STYLE.replace("</style>", '  .af-d .lbl-s { font: 500 11.5px "DM Mono", ui-monospace, Menlo, monospace; letter-spacing: .08em; fill: var(--af-fg-subtle, #8A8A8A); }\n</style>')

PRODUCT = {
    "hangar": "var(--af-product-hangar, #EE8F63)",
    "flightdeck": "var(--af-product-flightdeck, #D98CD2)",
    "tower": "var(--af-product-tower, #C5A93B)",
    "aidlc": "var(--af-product-aidlc, #69C27E)",
    "spaceport": "var(--af-product-spaceport, #B199F4)",
}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg(w: int, h: int, label: str, body: list[str], dots: tuple[int, int, int, int] | None = None, pid: str | None = None) -> str:
    pid = pid or label.lower().replace(" ", "-")[:24]
    style = STYLE_STACKED if pid.endswith("-stacked") else STYLE
    field = ""
    if dots:
        x, y, fw, fh = dots
        field = (
            f'<pattern id="{pid}-dots" width="8" height="8" patternUnits="userSpaceOnUse">'
            f'<circle class="dot" cx="4" cy="4" r="1.1"/></pattern>'
            f'<rect x="{x}" y="{y}" width="{fw}" height="{fh}" fill="url(#{pid}-dots)"/>'
        )
    return (
        f'<svg class="af-d" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="{esc(label)}">{style}'
        f'<defs><marker id="{pid}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path class="arrowhead" d="M0 0L10 5L0 10z"/></marker></defs>'
        f'<rect class="bg" width="{w}" height="{h}"/>{field}'
        + "".join(body).replace("ARROW", f"url(#{pid}-arrow)")
        + "</svg>\n"
    )


def box(x, y, w, h, name, lines=(), eyebrow=None, kind="box", product=None, name_cls="name", eyebrow_cls="lbl"):
    out = [f'<rect class="{kind}" x="{x}" y="{y}" width="{w}" height="{h}" rx="3"/>']
    if product:
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="2" fill="{PRODUCT[product]}"/>')
    ty = y + 26
    if eyebrow:
        out.append(f'<text class="{eyebrow_cls}" x="{x + 18}" y="{ty}">{esc(eyebrow)}</text>')
        ty += 22
    out.append(f'<text class="{name_cls}" x="{x + 18}" y="{ty}">{esc(name)}</text>')
    ly = ty + 22
    for line in lines:
        out.append(f'<text class="body" x="{x + 18}" y="{ly}">{esc(line)}</text>')
        ly += 18
    return "".join(out)


def edge(d, plan=False):
    return f'<path class="{"edge-plan" if plan else "edge"}" d="{d}" marker-end="ARROW"/>'


def text(x, y, s, cls="elabel", anchor="start", rotate=None):
    t = f' transform="rotate({rotate} {x} {y})"' if rotate is not None else ""
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}"{t}>{esc(s)}</text>'


# ------------------------------------------------------------------------------------------------- the drawings
W = 800  # every drawing's width, so body text stays 11 px at a docs page's 710 px


def flow(y, xs, w, h, names, lines=(), kinds=(), eyebrows=(), gap_label=None):
    """A row of boxes at the same height with an arrow between each; returns the markup and each box's x."""
    out = []
    for i, x in enumerate(xs):
        out.append(
            box(x, y, w, h, names[i], lines[i] if lines else (), eyebrow=eyebrows[i] if eyebrows else None,
                kind=kinds[i] if kinds else "box", name_cls="name-s")
        )
        if i:
            out.append(edge(f"M{xs[i - 1] + w} {y + h // 2}H{x - 2}"))
    return "".join(out)


def hangar_suite() -> str:
    """Where hangar sits: doors in, hangar runs it, the PR out, people; below, the three sister planes."""
    y, h, w = 92, 118, 175
    xs = (20, 215, 410, 605)
    b = [
        text(20, 44, "Where hangar sits", "title"),
        text(20, 66, "MANY DOORS  ·  ONE RUN PLANE  ·  ONE GATE", "lbl"),
        box(xs[0], y, w, h, "Doors", ["Trackers, chat, automation:", "Jira, GitHub, Slack, MCP,", "webhooks, schedules, A2A"], eyebrow="WORK IN", name_cls="name-s"),
        box(xs[1], y, w, h, "hangar runs it", ["An isolated sandbox per job", "The repo's own check", "Credentials stay outside"], eyebrow="RUN PLANE", kind="box-hub", product="hangar", name_cls="name-s"),
        box(xs[2], y, w, h, "Draft PR", ["One commit, pushed by", "hangar; an approval", "lands it"], eyebrow="THE GATE", kind="box-ext", name_cls="name-s"),
        box(xs[3], y, w, h, "People", ["Review, answer, approve:", "console, Slack, CLI, MCP"], eyebrow="YOUR TEAM", kind="person", name_cls="name-s"),
        edge(f"M{xs[0] + w} {y + h // 2}H{xs[1] - 2}"),
        edge(f"M{xs[1] + w} {y + h // 2}H{xs[2] - 2}"),
        edge(f"M{xs[2] + w} {y + h // 2}H{xs[3] - 2}"),
        # the sister planes, one line each
        box(20, 262, 240, 86, "Define", ["Harness lock, frozen from a good run"], eyebrow="TEAM HARNESS REPO + AI-DLC", product="aidlc", name_cls="name-s"),
        box(280, 262, 240, 86, "Judge", ["Reads every run from the outbox"], eyebrow="FLIGHTDECK", product="flightdeck", name_cls="name-s"),
        box(540, 262, 240, 86, "Govern", ["Decides risky actions (#172, planned)"], eyebrow="TOWER", kind="box-plan", product="tower", name_cls="name-s"),
        edge("M140 260V236H250V212"),
        text(148, 252, "harness lock"),
        edge("M340 212V260"),
        text(348, 246, "outbox"),
        edge("M430 212V236H660V260", plan=True),
        text(545, 230, "decide?", anchor="middle"),
        text(20, 376, "Solid: shipped. Dashed: Tower's decision API is built; hangar's wiring to it is open."),
    ]
    return svg(W, 396, "Where hangar sits", b, dots=(560, 16, 220, 60))


def concepts_map() -> str:
    """The four columns of the concepts page: people and their tools, front doors, the control plane, sandboxes."""
    y, h, w = 20, 146, 176
    xs = (20, 216, 412, 608)
    b = [
        box(xs[0], y, w, h, "People and tools", ["MCP clients, CLI, browser", "Jira, GitHub, Slack", "services, CI, other agents"], eyebrow="WHO", kind="person", name_cls="name-s"),
        box(xs[1], y, w, h, "Front doors", ["MCP, REST, CLI", "GitHub, Jira, Projects, Slack", "webhooks, schedules, A2A"], eyebrow="WORK IN", name_cls="name-s"),
        box(xs[2], y, w, h, "Control plane", ["Jobs and sessions", "Policy, people, budgets", "Credentials: vault, gateway", "Verify, branch, PR, landing"], eyebrow="HANGAR", kind="box-hub", product="hangar", name_cls="name-s"),
        box(xs[3], y, w, h, "Sandboxes", ["Environment + harness", "+ repo checkout, isolated", "Docker, GKE or E2B"], eyebrow="WORK RUNS", kind="box-ext", name_cls="name-s"),
        edge(f"M{xs[0] + w} {y + h // 2}H{xs[1] - 2}"),
        edge(f"M{xs[1] + w} {y + h // 2}H{xs[2] - 2}"),
        edge(f"M{xs[2] + w} {y + h // 2}H{xs[3] - 2}"),
    ]
    return svg(W, 186, "People, front doors, the control plane and sandboxes", b)


def control_plane() -> str:
    """Inside the control plane: two clients above, the control plane, three providers below."""
    b = [
        box(60, 20, 300, 60, "MCP client", ["Codex, Claude Code, Cursor: HTTP + token"], kind="person", name_cls="name-s"),
        box(440, 20, 300, 60, "Browser", ["The console: fleet, approvals, live terminals"], kind="person", name_cls="name-s"),
        box(60, 120, 680, 126, "Control plane", [
            "MCP tools, REST API, websocket PTYs, audit log",
            "Fleet manager: environments (layered specs to images, warm pools), snapshots and forks, TTL reaper",
            "Credential broker: encrypted vault, leases; git gateway",
        ], eyebrow="HANGAR", kind="box-hub", product="hangar", name_cls="name-s"),
        edge("M210 80V118"),
        edge("M590 80V118"),
        box(60, 290, 200, 86, "Docker", ["Laptop or one VM; gVisor, Kata"], eyebrow="PROVIDER · SANDBOXES", kind="box-ext", name_cls="name-s"),
        box(300, 290, 200, 86, "Kubernetes", ["GKE: Agent Sandbox CRDs"], eyebrow="PROVIDER · SANDBOXES", kind="box-ext", name_cls="name-s"),
        box(540, 290, 200, 86, "E2B", ["Firecracker microVMs"], eyebrow="PROVIDER · SANDBOXES", kind="box-ext", name_cls="name-s"),
        edge("M160 246V288"),
        edge("M400 246V288"),
        edge("M640 246V288"),
    ]
    return svg(W, 396, "Inside the control plane", b)


def jobs_flow() -> str:
    """A job: caller to queue to sandbox to agent turn to verify to a branch and a draft PR, and what comes back."""
    y, h, w = 40, 64, 122
    xs = (20, 151, 282, 413, 544, 675)
    names = ("Caller", "Queue", "Sandbox", "Agent turn", "Verify", "Draft PR")
    lines = (["POST /api/jobs"], ["admitted"], ["env + harness"], ["edits, tests"], ["the repo's check"], ["hangar/<job>"])
    kinds = ("person", "box", "box", "box", "box", "box-ext")
    b = [flow(y, xs, w, h, names, lines, kinds)]
    # the signed callback, back to the caller
    b.append(edge(f"M{xs[5] + w // 2} {y + h}V150H{xs[0] + w // 2}V{y + h + 2}"))
    b.append(text(400, 144, "signed callback: job.started, job.finished", anchor="middle"))
    # the engineer, following along
    b.append(text(400, 184, "An engineer follows up (API, MCP, CLI, /hangar on the PR) or attaches to the sandbox's terminal.", anchor="middle"))
    return svg(W, 200, "A job, from the caller to a draft PR", b)


def a2a_flow() -> str:
    """A2A: a client sends a message, hangar runs a job, task updates stream back; the same contextId follows up."""
    y, h = 40, 64
    b = [
        box(20, y, 180, h, "A2A client", ["ADK, AX or any other"], kind="person", name_cls="name-s"),
        box(300, y, 140, h, "/a2a", ["SendMessage in"], kind="box-hub", product="hangar", name_cls="name-s"),
        box(540, y, 240, h, "Job", ["queue, sandbox, agent, verify, PR"], name_cls="name-s"),
        edge(f"M200 {y + h // 2}H298"),
        text(249, y + h // 2 - 8, "SendMessage", anchor="middle"),
        edge(f"M440 {y + h // 2}H538"),
        edge(f"M610 {y + h}V150H110V{y + h + 2}"),
        text(360, 144, "Task, status updates (SSE), push notifications", anchor="middle"),
        edge(f"M110 {y + h}V186H660V{y + h + 2}", plan=True),
        text(385, 180, "SendMessage with the same contextId: a follow-up turn on the same job and branch", anchor="middle"),
    ]
    return svg(W, 206, "A2A: a message in, a task out", b)


def slack_flow() -> str:
    """Slack: /hangar starts a job; the thread carries its progress; a reply in the thread is a follow-up."""
    y, h = 40, 64
    b = [
        box(20, y, 200, h, "/hangar <instructions>", ["in a channel"], kind="person", name_cls="name-s"),
        box(300, y, 200, 82, "hangar posts", ["“@you asked hangar to", "work on owner/repo”"], kind="box-hub", product="hangar", name_cls="name-s"),
        box(580, y, 200, 118, "The thread", ["Queued as job-3fa2c1d0", "Agent started", "Done: PR, verification,", "summary"], kind="box-ext", name_cls="name-s"),
        edge(f"M220 {y + h // 2}H298"),
        edge(f"M500 {y + h // 2}H578"),
        box(300, 190, 200, 64, "Follow-up turn", ["same branch and PR"], name_cls="name-s"),
        edge("M680 160V222H502", plan=True),
        text(590, 216, "a reply in the thread", anchor="middle"),
        edge("M400 254V280H680V282"),
        text(540, 274, "Done, again in the thread", anchor="middle"),
    ]
    return svg(W, 300, "Slack: a command, a thread, a follow-up", b)


def landing_states() -> str:
    """How a PR lands: opened, open, approved, armed, merged, and what sends it back to open."""
    y, h, w = 40, 56, 128
    xs = (20, 184, 348, 512, 676)
    names = ("Opened", "Open", "Approved", "Armed", "Merged")
    lines = (["draft from hangar"], ["review packet"], ["head approved"], ["auto-merge armed"], ["rules met"])
    kinds = ("box", "box", "box", "box-hub", "box-ext")
    b = [
        flow(y, xs, w, h, names, lines, kinds),
        text(266, y - 10, "an approver or code owner", anchor="middle"),
        text(430, y - 10, "checks green, no agent turn", anchor="middle"),
        edge(f"M{xs[3] + w - 12} {y + h}V150H{xs[1] + 12}V{y + h + 2}"),
        text(412, 144, "disarmed, back to open: a push, changes requested, a new agent turn", anchor="middle"),
    ]
    return svg(W, 166, "How a PR lands", b)


# ------------------------------------------------------------------------- the stacked variants, for phones
WS = 350  # a docs page is 343 px wide on a 390 px phone, so the text keeps its size (11 px and up)
BX, BW = 20, 310  # every stacked box's x and width
RW = 270  # the width when an edge returns up the right-hand side
RX, RT = 324, 340  # that edge's x, and its rotated label's x


def stack(y, h, names, lines=(), kinds=(), eyebrows=(), gap=36, labels=(), bw=BW):
    """Boxes one under the other with an arrow between each, and an optional label beside each arrow.
    Returns the markup and the y below the last box."""
    out = []
    for i, name in enumerate(names):
        bh = h[i] if isinstance(h, (list, tuple)) else h
        if i:
            out.append(edge(f"M{BX + bw // 2} {y - gap}V{y - 2}"))
            if labels and labels[i - 1]:
                out.append(text(BX + bw // 2 + 10, y - gap // 2 + 4, labels[i - 1]))
        out.append(box(BX, y, bw, bh, name, lines[i] if lines else (), eyebrow=eyebrows[i] if eyebrows else None,
                       kind=kinds[i] if kinds else "box", name_cls="name-s", eyebrow_cls="lbl-s"))
        y += bh + gap
    return "".join(out), y - gap


def back_edge(y_from, y_to, label, label_y):
    """An edge from a box's right side at y_from, up the right-hand margin, into a box's right side at y_to."""
    return edge(f"M{BX + RW} {y_from}H{RX}V{y_to}H{BX + RW + 2}") + text(RT, label_y, label, anchor="middle", rotate=-90)


def concepts_map_stacked() -> str:
    body, end = stack(20, (110, 110, 128, 110),
                      ("People and tools", "Front doors", "Control plane", "Sandboxes"),
                      (["MCP clients, CLI, browser", "Jira, GitHub, Slack", "services, CI, other agents"],
                       ["MCP, REST, CLI", "GitHub, Jira, Projects, Slack", "webhooks, schedules, A2A"],
                       ["Jobs and sessions", "Policy, people, budgets", "Credentials: vault, gateway", "Verify, branch, PR, landing"],
                       ["Environment + harness", "+ repo checkout, isolated", "Docker, GKE or E2B"]),
                      ("person", "box", "box-hub", "box-ext"), ("WHO", "WORK IN", "HANGAR", "WORK RUNS"))
    return svg(WS, end + 20, "People, front doors, the control plane and sandboxes", [body], pid="concepts-stacked")


def control_plane_stacked() -> str:
    """Two clients side by side, the control plane, three providers one under the other."""
    b = [
        box(20, 20, 150, 82, "MCP client", ["Codex, Claude Code,", "Cursor: HTTP + token"], kind="person", name_cls="name-s"),
        box(180, 20, 150, 82, "Browser", ["The console: fleet,", "approvals, terminals"], kind="person", name_cls="name-s"),
        edge("M95 102V136"), edge("M255 102V136"),
        box(20, 138, 310, 176, "Control plane", [
            "MCP tools, REST API,", "websocket PTYs, audit log",
            "Fleet manager: environments,", "warm pools, snapshots, forks",
            "Credential broker: vault,", "leases; git gateway",
        ], eyebrow="HANGAR", kind="box-hub", product="hangar", name_cls="name-s", eyebrow_cls="lbl-s"),
        edge("M175 314V348"),
        text(185, 336, "sandboxes, on one of"),
        box(20, 350, 310, 60, "Docker", ["Laptop or one VM; gVisor, Kata"], kind="box-ext", name_cls="name-s"),
        box(20, 420, 310, 60, "Kubernetes", ["GKE: Agent Sandbox CRDs"], kind="box-ext", name_cls="name-s"),
        box(20, 490, 310, 60, "E2B", ["Firecracker microVMs"], kind="box-ext", name_cls="name-s"),
    ]
    return svg(WS, 570, "Inside the control plane", b, pid="control-plane-stacked")


def jobs_flow_stacked() -> str:
    names = ("Caller", "Queue", "Sandbox", "Agent turn", "Verify", "Draft PR")
    lines = (["POST /api/jobs"], ["admitted"], ["env + harness"], ["edits, tests"], ["the repo's check"], ["hangar/<job>"])
    kinds = ("person", "box", "box", "box", "box", "box-ext")
    body, end = stack(20, 60, names, lines, kinds, gap=30, bw=RW)
    b = [body,
         back_edge(end - 30, 50, "signed callback: job.started, job.finished", (end + 20) // 2),
         text(BX, end + 32, "An engineer follows up (API, MCP, CLI,"),
         text(BX, end + 50, "/hangar on the PR) or attaches to the terminal.")]
    return svg(WS, end + 68, "A job, from the caller to a draft PR", b, pid="jobs-stacked")


def a2a_flow_stacked() -> str:
    body, end = stack(20, (64, 64, 82),
                      ("A2A client", "/a2a", "Job"),
                      (["ADK, AX or any other"], ["SendMessage in"], ["queue, sandbox, agent,", "verify, PR"]),
                      ("person", "box-hub", "box"), gap=40, labels=("SendMessage", None), bw=RW)
    b = [body,
         back_edge(end - 30, 52, "Task, status updates (SSE), push", (end + 20) // 2),
         edge(f"M{BX + RW // 2} {end}V{end + 30}", plan=True),
         text(BX, end + 52, "SendMessage with the same contextId:"),
         text(BX, end + 68, "a follow-up turn on the same job and branch")]
    return svg(WS, end + 86, "A2A: a message in, a task out", b, pid="a2a-stacked")


def slack_flow_stacked() -> str:
    body, end = stack(20, (64, 82, 118),
                      ("/hangar <instructions>", "hangar posts", "The thread"),
                      (["in a channel"], ["\u201c@you asked hangar to", "work on owner/repo\u201d"],
                       ["Queued as job-3fa2c1d0", "Agent started", "Done: PR, verification,", "summary"]),
                      ("person", "box-hub", "box-ext"), gap=34, bw=RW)
    y2 = end + 44
    b = [body,
         edge(f"M{BX + RW // 2} {end}V{y2 - 2}", plan=True),
         text(BX + RW // 2 + 10, end + 26, "a reply in the thread"),
         box(BX, y2, RW, 64, "Follow-up turn", ["same branch and PR"], name_cls="name-s"),
         back_edge(y2 + 32, end - 20, "Done, again in the thread", (y2 + end) // 2 + 6)]
    return svg(WS, y2 + 84, "Slack: a command, a thread, a follow-up", b, pid="slack-stacked")


def landing_states_stacked() -> str:
    names = ("Opened", "Open", "Approved", "Armed", "Merged")
    lines = (["draft from hangar"], ["review packet"], ["head approved"], ["auto-merge armed"], ["rules met"])
    kinds = ("box", "box", "box", "box-hub", "box-ext")
    body, end = stack(20, 56, names, lines, kinds, gap=34,
                      labels=(None, "an approver or code owner", "checks green, no agent turn", None), bw=RW)
    open_y, armed_y = 20 + 90, 20 + 3 * 90
    b = [body,
         back_edge(armed_y + 40, open_y + 28, "disarmed, back to open", (open_y + armed_y) // 2 + 34),
         text(BX, end + 30, "Disarmed by a push, changes requested"),
         text(BX, end + 46, "or a new agent turn.")]
    return svg(WS, end + 64, "How a PR lands", b, pid="landing-stacked")


def main() -> None:
    OUT.write_text(hangar_suite())
    print("wrote", OUT)
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
