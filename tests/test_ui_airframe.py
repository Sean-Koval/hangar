"""The web console and the agent screen are built on Airframe, the suite's design system.

Airframe's source of truth is Sean-Koval/agent-factory. scripts/sync-airframe.sh vendors its published theme files,
unmodified, into src/hangar/ui/airframe/ (the console) and docs/stylesheets/ (the docs site). hangar's own UI files
use `var(--af-*)` tokens and `af-` classes only, so a color literal there is a bug: change Airframe instead.
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

# hangar's own files: no color literals. (favicon.svg is the exception: a favicon can't read CSS variables, so it
# carries the Airframe product-hangar and line-strong values by hand.)
OWN = ["app.css", "index.html", "vnc.html", "app.js"]
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


def test_vendored_dir_holds_only_what_the_sync_script_writes():
    pairs = re.search(r'FILES="\n(.*?)\n"', SYNC.read_text(), re.S).group(1).split("\n")
    dests = {line.split()[1] for line in pairs if line.strip()}
    assert {"src/hangar/ui/airframe/airframe.css", "src/hangar/ui/airframe/airframe-components.css",
            "src/hangar/ui/airframe/airframe-icons.svg", "docs/stylesheets/airframe.css",
            "docs/stylesheets/airframe-docs.css"} <= dests
    for d in dests:
        assert (ROOT / d).is_file(), f"{d} is missing: run sh scripts/sync-airframe.sh theme-vN"
    extra = {str(p.relative_to(ROOT)) for p in VENDORED.iterdir()} - dests
    assert not extra, f"hand-made files in the vendored folder (it is replaced on every sync): {sorted(extra)}"


def test_icons_the_console_uses_are_in_the_sprite():
    sprite = set(re.findall(r'<symbol id="af-([a-z0-9-]+)"', (VENDORED / "airframe-icons.svg").read_text()))
    used = set()
    for name in ("index.html", "vnc.html"):
        used |= set(re.findall(r"airframe-icons\.svg#af-([a-z0-9-]+)", (UI / name).read_text()))
    used |= set(re.findall(r'\bic\("([a-z0-9-]+)"', (UI / "app.js").read_text()))
    assert used and not used - sprite, f"not in the Airframe sprite: {sorted(used - sprite)}"


def test_console_serves_and_links_airframe(settings):
    c = TestClient(create_app(settings))
    page = c.get("/").text
    for href in ("/ui/airframe/airframe.css", "/ui/airframe/airframe-components.css", "/ui/app.css"):
        assert f'href="{href}"' in page
    assert page.index("/ui/airframe/airframe.css") < page.index("/ui/airframe/airframe-components.css") < page.index(
        "/ui/app.css")
    for path, kind in (("/ui/airframe/airframe.css", "text/css"), ("/ui/airframe/airframe-components.css", "text/css"),
                       ("/ui/airframe/airframe-icons.svg", "image/svg+xml"), ("/ui/favicon.svg", "image/svg+xml")):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path
    assert "/ui/airframe/airframe.css" in c.get("/ui/vnc.html").text


def test_oauth_consent_page_is_on_airframe_too():
    from hangar.oauth import _PAGE

    page = _code(_PAGE)
    assert not HEX.search(page) and not FUNC.search(page), "the OAuth consent page uses Airframe tokens only"
    assert "/ui/airframe/airframe.css" in _PAGE and "/ui/airframe/airframe-components.css" in _PAGE
