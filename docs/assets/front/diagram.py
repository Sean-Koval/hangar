"""Hangar's place in the agent factory, as a theme-aware SVG (docs/assets/front/hangar-suite.svg).

Drawn with the helpers of the suite's diagram generator (Sean-Koval/agent-factory, scripts/diagrams.py), copied
here so this repo renders on its own: every color is an Airframe token (`var(--af-*, <dark fallback>)`), so the SVG
inlined in a docs page follows the page's theme, and opened on its own (or as the PNGs make.py renders) it shows
the dark or light flavor.

    python3 docs/assets/front/diagram.py      # writes hangar-suite.svg next to this file
"""

# ruff: noqa: E501 - the vendored stylesheet and the drawing's coordinates read best one per line
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent / "hangar-suite.svg"

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

PRODUCT = {
    "hangar": "var(--af-product-hangar, #EE8F63)",
    "flightdeck": "var(--af-product-flightdeck, #D98CD2)",
    "tower": "var(--af-product-tower, #C5A93B)",
    "aidlc": "var(--af-product-aidlc, #69C27E)",
    "spaceport": "var(--af-product-spaceport, #B199F4)",
}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg(w: int, h: int, label: str, body: list[str], dots: tuple[int, int, int, int] | None = None) -> str:
    pid = label.lower().replace(" ", "-")[:24]
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
        f'aria-label="{esc(label)}">{STYLE}'
        f'<defs><marker id="{pid}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path class="arrowhead" d="M0 0L10 5L0 10z"/></marker></defs>'
        f'<rect class="bg" width="{w}" height="{h}"/>{field}'
        + "".join(body).replace("ARROW", f"url(#{pid}-arrow)")
        + "</svg>\n"
    )


def box(x, y, w, h, name, lines=(), eyebrow=None, kind="box", product=None, name_cls="name"):
    out = [f'<rect class="{kind}" x="{x}" y="{y}" width="{w}" height="{h}" rx="3"/>']
    if product:
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="2" fill="{PRODUCT[product]}"/>')
    ty = y + 26
    if eyebrow:
        out.append(f'<text class="lbl" x="{x + 18}" y="{ty}">{esc(eyebrow)}</text>')
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


# ------------------------------------------------------------------------------------------------- the drawing
def hangar_suite() -> str:
    D = (40, 250)  # doors column x, width
    H = (390, 400)  # hangar x, width
    G = (930, 230)  # the gate column x, width
    b = [
        text(40, 48, "Where hangar sits", "title"),
        text(40, 72, "MANY DOORS  ·  ONE RUN PLANE  ·  ONE GATE", "lbl"),
        # doors
        box(
            D[0],
            110,
            D[1],
            76,
            "Trackers",
            ["Jira “Ready for agent”, GitHub", "labels and Projects columns"],
            name_cls="name-s",
        ),
        box(
            D[0],
            202,
            D[1],
            76,
            "Chat and laptops",
            ["Slack @hangar; Claude Code, Codex", "or Cursor over MCP"],
            name_cls="name-s",
        ),
        box(D[0], 294, D[1], 76, "Automation", ["Schedules, webhooks, CI,", "A2A and the REST API"], name_cls="name-s"),
        # hangar
        box(
            H[0],
            110,
            H[1],
            250,
            "Run the work",
            [
                "Admission: repo policy, trust, budgets, fairness",
                "Jobs (one-shot) and sessions (resumable)",
                "Isolated sandboxes from prepared environments:",
                "  Docker, gVisor or Kata VM, GKE, E2B",
                "Claude Code, Codex, pi or Antigravity inside",
                "Verify with the repo's own check",
                "Push through the git gateway; credentials stay out",
                "The inbox: reviews, questions, approvals",
            ],
            eyebrow="HANGAR  ·  RUN PLANE",
            kind="box-hub",
            product="hangar",
        ),
        # the gate
        box(
            G[0],
            110,
            G[1],
            120,
            "One gate: the PR",
            ["Draft PR with a review packet;", "an approval lands it", "(landing, merge stays human)"],
            eyebrow="GITHUB",
            kind="box-ext",
            name_cls="name-s",
        ),
        box(
            G[0],
            250,
            G[1],
            120,
            "People",
            ["Review PRs, answer questions,", "steer a running agent", "(web UI, Slack, CLI, MCP)"],
            eyebrow="YOUR TEAM",
            kind="person",
            name_cls="name-s",
        ),
        # sister tools
        box(
            40,
            440,
            350,
            112,
            "Define",
            ["Native files and harness.lock, frozen from", "a good run; ai-dlc's optional workflow plugin"],
            eyebrow="TEAM HARNESS REPO  +  AI-DLC",
            product="aidlc",
            name_cls="name-s",
        ),
        box(
            425,
            440,
            350,
            112,
            "Judge",
            ["Reads runs from the outbox: manifests,", "traces (hangar.trace/v1), grades"],
            eyebrow="FLIGHTDECK",
            product="flightdeck",
            name_cls="name-s",
        ),
        box(
            810,
            440,
            350,
            112,
            "Govern",
            ["Rules and approvals for risky actions;", "hangar will ask it once #172 lands"],
            eyebrow="TOWER  ·  DECISION API BUILT",
            kind="box-plan",
            product="tower",
            name_cls="name-s",
        ),
        # doors -> hangar
        edge(f"M{D[0] + D[1]} 148H{H[0] - 30}V200H{H[0] - 2}"),
        edge(f"M{D[0] + D[1]} 240H{H[0] - 2}"),
        edge(f"M{D[0] + D[1]} 332H{H[0] - 30}V280H{H[0] - 2}"),
        text(D[0] + D[1] + 8, 232, "tickets, tasks"),
        # hangar -> gate, people
        edge(f"M{H[0] + H[1]} 170H{G[0] - 2}"),
        text(H[0] + H[1] + 12, 162, "branch + draft PR"),
        edge(f"M{H[0] + H[1]} 290H{G[0] - 2}"),
        text(H[0] + H[1] + 12, 282, "inbox, comments"),
        edge(f"M{G[0]} 340H{H[0] + H[1] + 2}"),
        text(H[0] + H[1] + 12, 332, "replies, approvals"),
        # sister tools
        edge("M215 438V400H500V362"),
        text(223, 418, "harness lock"),
        edge("M590 360V438"),
        text(598, 410, "outbox"),
        edge("M720 360V400H985V438", plan=True),
        text(870, 392, "decide?", anchor="middle"),
    ]
    return svg(1200, 590, "Where hangar sits", b, dots=(900, 20, 300, 70))


def main() -> None:
    OUT.write_text(hangar_suite())
    print("wrote", OUT)


if __name__ == "__main__":
    main()
