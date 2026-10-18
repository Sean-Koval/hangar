"""The web console and the agent screen are built on Airframe, the suite's design system.

Airframe's source of truth is Sean-Koval/agent-factory. scripts/sync-airframe.sh vendors its published theme files,
unmodified, into src/hangar/ui/airframe/ (the console: the CSS, the icon sprite and airframe.js, the helpers and
behaviours) and docs/stylesheets/ (the docs site). hangar's own UI files use `var(--af-*)` tokens and `af-` classes
only, so a color literal there is a bug: change Airframe instead.
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from hangar.app import create_app

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "hangar" / "ui"
VENDORED = UI / "airframe"
DOCS_CSS = ROOT / "docs" / "stylesheets"
SYNC = ROOT / "scripts" / "sync-airframe.sh"

# hangar's own files: no color literals. (favicon.svg is the exception: a favicon can't read CSS variables, so
# agent-factory generates it from the tokens, and scripts/sync-brand.sh vendors it with the README banner.)
OWN = ["app.css", "index.html", "vnc.html", "app.js", "fleet.js"]
HEX = re.compile(r"(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b")
FUNC = re.compile(r"\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\s*\(", re.IGNORECASE)
HEADER = re.compile(r"\A/\*\s*Airframe (theme-v\d+) (tokens|components)\b.{0,40}?Generated from airframe/", re.S)


COMMENTS = [re.compile(r"/\*.*?\*/", re.S), re.compile(r"<!--.*?-->", re.S), re.compile(r"(?<![:\"'\w])//[^\n]*")]


def _code(text: str) -> str:
    """The file without comments (issue numbers like #157 live there), keeping line numbers."""
    for c in COMMENTS:
        text = c.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return text


def test_own_ui_files_use_tokens_not_color_literals():
    bad = []
    for name in OWN:
        for n, line in enumerate(_code((UI / name).read_text()).splitlines(), 1):
            for m in [*HEX.finditer(line), *FUNC.finditer(line)]:
                bad.append(f"src/hangar/ui/{name}:{n}: {m.group(0)!r} in {line.strip()[:100]}")
    assert not bad, "use var(--af-*) tokens (Airframe), not color literals:\n" + "\n".join(bad)


def test_guard_catches_literals():
    assert HEX.search("color: #FFC400;") and HEX.search("border: 1px solid #fff") and FUNC.search("rgba(0,0,0,.5)")
    assert not HEX.search('href="#fleet"') and not HEX.search("&#39;") and not HEX.search('$("#dialog-ok")')
    assert not HEX.search("#af-agent")
    assert "#157" not in _code("q;  // the inbox's links (#157)") and "#FFC400" in _code('x = "#FFC400"; // a comment')
    assert "#abc" not in _code("/* #abc */ a {}") and "https://x.y/#fff" in _code("u = 'https://x.y/#fff'")


def _defined_tokens() -> set[str]:
    css = (VENDORED / "airframe.css").read_text() + (VENDORED / "airframe-components.css").read_text()
    return set(re.findall(r"(--af-[a-z0-9-]+)\s*:", css))


def test_every_token_used_exists():
    known = _defined_tokens()
    used = {(name, m) for name in OWN for m in re.findall(r"var\((--af-[a-z0-9-]+)", (UI / name).read_text())}
    unknown = sorted(f"{name}: {m}" for name, m in used if m not in known)
    assert not unknown, "not an Airframe token (check airframe/tokens.json in agent-factory):\n" + "\n".join(unknown)


def test_vendored_files_come_unmodified_from_one_tag():
    tags = {}
    for name in ("airframe.css", "airframe-components.css"):
        m = HEADER.match((VENDORED / name).read_text())
        assert m, f"src/hangar/ui/airframe/{name} lacks Airframe's generated header: re-run scripts/sync-airframe.sh"
        tags[name] = m.group(1)
    assert len(set(tags.values())) == 1, f"vendored from different theme tags: {tags}"
    # the console and the docs site carry the same tokens, byte for byte
    assert (VENDORED / "airframe.css").read_bytes() == (DOCS_CSS / "airframe.css").read_bytes()
    assert "Airframe for the docs sites" in (DOCS_CSS / "airframe-docs.css").read_text()
    sprite = (VENDORED / "airframe-icons.svg").read_text()
    assert sprite.startswith('<svg xmlns="http://www.w3.org/2000/svg"') and '<symbol id="af-agent"' in sprite
    # theme-v6: the helpers and behaviours come as one ES module, from the same release
    js = (VENDORED / "airframe.js").read_text()
    assert js.startswith("/*! Airframe helpers.") and "do not edit the copy" in js
    for name in ("status", "icon", "meta", "btn", "avatar", "toast", "confirm"):
        assert f"export function {name}(" in js, f"airframe.js lacks {name}()"
    assert "export const drawer" in js


def test_vendored_dir_holds_only_what_the_sync_script_writes():
    pairs = re.search(r'FILES="\n(.*?)\n"', SYNC.read_text(), re.S).group(1).split("\n")
    dests = {line.split()[1] for line in pairs if line.strip()}
    assert {"src/hangar/ui/airframe/airframe.css", "src/hangar/ui/airframe/airframe-components.css",
            "src/hangar/ui/airframe/airframe-icons.svg", "src/hangar/ui/airframe/airframe.js",
            "docs/stylesheets/airframe.css", "docs/stylesheets/airframe-docs.css"} <= dests
    for d in dests:
        assert (ROOT / d).is_file(), f"{d} is missing: run sh scripts/sync-airframe.sh theme-vN"
    extra = {str(p.relative_to(ROOT)) for p in VENDORED.iterdir()} - dests
    assert not extra, f"hand-made files in the vendored folder (it is replaced on every sync): {sorted(extra)}"


def test_icons_the_console_uses_are_in_the_sprite():
    sprite = set(re.findall(r'<symbol id="af-([a-z0-9-]+)"', (VENDORED / "airframe-icons.svg").read_text()))
    used = set()
    for name in ("index.html", "vnc.html"):
        used |= set(re.findall(r"airframe-icons\.svg#af-([a-z0-9-]+)", (UI / name).read_text()))
    for name in ("app.js", "fleet.js"):
        used |= set(re.findall(r'\bic\("([a-z0-9-]+)"', (UI / name).read_text()))
    assert used and not used - sprite, f"not in the Airframe sprite: {sorted(used - sprite)}"


def test_console_serves_and_links_airframe(settings):
    c = TestClient(create_app(settings))
    page = c.get("/").text
    for href in ("/ui/airframe/airframe.css", "/ui/airframe/airframe-components.css", "/ui/app.css"):
        assert f'href="{href}"' in page
    assert page.index("/ui/airframe/airframe.css") < page.index("/ui/airframe/airframe-components.css") < page.index(
        "/ui/app.css")
    for path, kind in (("/ui/airframe/airframe.css", "text/css"), ("/ui/airframe/airframe-components.css", "text/css"),
                       ("/ui/airframe/airframe-icons.svg", "image/svg+xml"), ("/ui/favicon.svg", "image/svg+xml"),
                       ("/ui/airframe/airframe.js", "text/javascript")):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path
    assert "/ui/airframe/airframe.css" in c.get("/ui/vnc.html").text
    # airframe.js is an ES module, loaded first and handed to the classic scripts as window.airframe; the deferred
    # scripts then run in order: app.js, then the Fleet page (fleet.js), which uses app.js's helpers
    mod = re.search(r'<script type="module">(.*?)</script>', page, re.S)
    assert mod and 'from "/ui/airframe/airframe.js"' in mod.group(1) and "window.airframe = airframe" in mod.group(1)
    assert 'config.icons = "/ui/airframe/airframe-icons.svg"' in mod.group(1), "the sprite is not next to the page"
    assert mod.start() < page.index('src="/ui/app.js"') < page.index('src="/ui/fleet.js"')
    for src in ("/ui/app.js", "/ui/fleet.js"):
        assert re.search(rf'<script defer src="{src}">', page), f"{src} must be deferred to run after the module"
    assert c.get("/ui/fleet.js").status_code == 200


def test_fleet_views_use_the_theme_v3_components():
    """Board, Bays, Columns and Owners are Airframe's FleetView, FleetBays, FleetColumns and FleetOwners: the vendored
    theme has them, and the console builds with them rather than its own copies."""
    css = (VENDORED / "airframe-components.css").read_text()
    js = (UI / "fleet.js").read_text()
    for cls in ("af-fleet__board", "af-stand", "af-bays", "af-bay", "af-statecols", "af-mini", "af-owners", "af-cap",
                "af-track", "af-person"):
        assert f".{cls}" in css, f"{cls} is not in the vendored Airframe: sync theme-v3 or later"
        assert cls in js, f"fleet.js doesn't use {cls}"
    own = (UI / "app.css").read_text()
    for cls in ("af-bay", "af-mini", "af-stand", "af-owners"):  # placement tweaks only, never a redefinition
        assert not re.search(rf"(^|[,}}])\s*\.{cls}\s*\{{", own, re.M), f"app.css redefines .{cls}"
    links = re.findall(r'data-fleet-view="([a-z]+)"', (UI / "index.html").read_text())
    assert links == ["board", "bays", "columns", "owners"]


def test_fleet_header_leads_with_what_needs_a_person():
    """The Fleet header's mark and keyed counts put what needs a person first, then failed, then denied, then the
    quiet states (running, starting, paused): one attention order, shared with each pool's counts and the tiles."""
    js = (UI / "fleet.js").read_text()
    order = re.search(r"const ATTENTION = \[([^\]]*)\]", js)
    assert order, "fleet.js defines ATTENTION, the one attention order"
    assert re.findall(r'"([a-z]+)"', order.group(1)) == ["approval", "failed", "denied", "running", "queued", "off"]
    assert "ATTENTION.find(" in js and "ROLE_RANK = Object.fromEntries(ATTENTION" in js
    # the keyed counts list the roles in that order too, running after the three that carry color
    counts = re.search(r"const countMeta = .*?\n\]\);", js, re.S).group(0)
    keys = [k for k in re.findall(r'\["([a-z ]+)", c\.', counts)]
    assert keys == ["needs you", "failed", "denied", "running", "starting", "paused", "spun down"], keys
    assert "SEVERITY" not in js and "SEVERITY" not in (UI / "app.js").read_text(), "the old denied-first order is gone"


def test_overview_carries_the_person_caps_the_owners_view_shows(settings):
    settings.admin_token = "t0ken"
    settings.person_max_sandboxes, settings.person_max_sessions = 7, 3
    c = TestClient(create_app(settings), headers={"Authorization": "Bearer t0ken"})
    ov = c.get("/api/overview").json()
    assert ov["person_caps"] == {"sandboxes": 7, "sessions": 3} and ov["max_sandboxes"] == settings.max_sandboxes


def test_oauth_consent_page_is_on_airframe_too():
    from hangar.oauth import _PAGE

    page = _code(_PAGE)
    assert not HEX.search(page) and not FUNC.search(page), "the OAuth consent page uses Airframe tokens only"
    assert "/ui/airframe/airframe.css" in _PAGE and "/ui/airframe/airframe-components.css" in _PAGE
