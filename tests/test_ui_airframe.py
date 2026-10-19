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
                "af-track", "af-who"):
        assert f".{cls}" in css, f"{cls} is not in the vendored Airframe: sync theme-v6 or later"
        assert cls in js, f"fleet.js doesn't use {cls}"
    # the avatar triad (theme-v6): owners are round people, harnesses hexagonal agents, through airframe.js's avatar()
    for kind in ("person", "agent"):
        assert f".af-avatar--{kind}" in css and f'avatar("{kind}"' in js, f"fleet.js doesn't draw {kind} avatars"
    assert "af-person__av" not in js, "FleetOwners' own initials are the avatar triad now"
    own = (UI / "app.css").read_text()
    for cls in ("af-bay", "af-mini", "af-stand", "af-owners"):  # placement tweaks only, never a redefinition
        assert not re.search(rf"(^|[,}}])\s*\.{cls}\s*\{{", own, re.M), f"app.css redefines .{cls}"
    links = re.findall(r'data-fleet-view="([a-z]+)"', (UI / "index.html").read_text())
    assert links == ["board", "bays", "columns", "owners"]


def test_console_is_assembled_from_airframe_parts():
    """theme-v6: the console's page headers, empty states, toasts, confirms, the form dialog and both drawers are
    Airframe's parts (patterns CSS and airframe.js), not hangar's own copies; the copies' CSS is gone."""
    html, css = (UI / "index.html").read_text(), (UI / "app.css").read_text()
    app, fleet = (UI / "app.js").read_text(), (UI / "fleet.js").read_text()
    js = app + fleet
    # page header: one af-page-head per view, title in the display face, lede, summary, actions
    views = re.findall(r'<section id="view-[a-z-]+" class="view"', html)
    assert len(views) == html.count('<header class="af-page-head')
    assert "view-head" not in html and ".view-head" not in css
    assert 'id="fleet-sub" class="af-page-head__sum"' in html
    # toasts and confirms come from airframe.js; window.confirm() and the own toast are gone
    assert "airframe.toast(" in app and "airframe.confirm(" in app
    assert not re.search(r"(?<![.\w])confirm\(", js), "use confirmAction() (airframe.confirm), never window.confirm()"
    assert 'id="toast"' not in html and ".toast" not in css
    # the form dialog is an af-dialog with the Airframe footer order (refuse, then accept); fields are af-field
    assert '<dialog id="dialog" class="af-dialog"' in html
    assert "af-dialog__foot" in app and "af-field__label" in app
    assert re.search(r"\bdialog\s*\{", css) is None, "app.css no longer styles <dialog>: af-dialog does"
    # drawers: the terminal is the bottom drawer (always Night), the Fleet detail the right one (airframe.drawer)
    assert 'id="drawer" class="af-drawer af-drawer--bottom' in html and 'data-theme="dark"' in html
    assert 'airframe.drawer.open("#drawer", { scrim: false })' in app
    assert "af-drawer--right" in fleet and "af-drawer__body" in fleet
    assert "airframe.drawer.open(el, { scrim: false })" in fleet
    assert "--drawer-pad: calc(var(--af-drawer-inset)" in css, "the page and the detail stop above the terminal drawer"
    # empty states are af-empty (no-data, after-action, compact); the five own shapes are gone
    assert "af-empty" in js and 'kind: "after-action"' in app
    for dead in ("empty-floor", "fleet-empty__envs", "fd-empty", "empty-row", 'class="empty"', 'class="error"'):
        assert dead not in js and dead not in html, f"{dead} is still used"
    for dead in (".empty", ".empty-floor", ".fd-empty", ".fd-decide", ".fd-head", ".error", ".drawer", ".drawer-grip"):
        assert not re.search(rf"(^|[,}}])\s*{re.escape(dead)}(?![\w-])", css, re.M), f"app.css defines {dead}"
    # the markup helpers are airframe.js's, not copies
    assert "const { status, btn, avatar } = airframe;" in app
    assert "airframe.meta(" in app and "airframe.icon(" in app


def test_the_reply_is_a_composer_and_a_question_is_a_question_card():
    """theme-v6: the follow-up and message forms are Airframe's Composer (a queued row with a take-back while the
    agent is busy, a disabled state that says why), and an agent's question is Airframe's Question card answered in
    place, instead of a kv line and "Answer the agent" elsewhere."""
    app, css = (UI / "app.js").read_text(), (UI / "app.css").read_text()
    for part in ("af-composer", "af-composer__queued", "af-composer__qmsg", "af-composer__box", "af-composer__input",
                 "af-composer__row", "af-composer__send", "af-composer__why", "af-question", "af-question__q",
                 "af-question__free", "af-question__act", "af-chip"):
        assert part in app, f"app.js lacks {part}"
    assert "function composerHtml(" in app and "function questionHtml(" in app
    # every disabled composer says why; the busy one queues through the server's steer and takes back with withdraw
    assert 'disabled: true, why:' in app and '"data-take-back"' in app
    assert '/steers/${id}/withdraw' in app and "/api/jobs/${j.id}/steer" in app and "/api/jobs/${j.id}/follow-up" in app
    assert "/api/jobs/${j.id}/approvals" in app, "a running job's turn info says whether it can be steered"
    # the old forms and their CSS are gone
    for dead in ("job-followup", "ses-send", "ses-msg", 'class="followup"'):
        assert dead not in app, dead
    assert ".followup" not in css
    # a question is distinct from an approval but takes the same role and word
    assert 'status("approval", "Needs input", { lit: true })' in app


def test_the_change_is_a_greyscale_diff_with_comments_for_the_agent():
    """theme-v6: a job's or session's change (GET .../patch) renders as Airframe's Diff (file index with the stat,
    hunks, greyscale lines, a Comment affordance per line); a comment opens a thread under its line and is queued;
    the queued comments go to the agent with the next message from the Composer, or alone, each with its file:line
    and the line itself."""
    app, css = (UI / "app.js").read_text(), (UI / "app.css").read_text()
    for part in ("af-diff", "af-diff__file", "af-diff__path", "af-diff__stat", "af-diff__add", "af-diff__del",
                 "af-diff__tools", "af-diff__hunk", "af-diff__hh", "af-diff__line", "af-diff__n", "af-diff__s",
                 "af-diff__code", "af-diff__cmt", "af-diff__more", "af-diff__thread", "af-diff__note", "af-comment",
                 "af-comment__who", "af-comment__name", "af-comment__time", "af-comment__text"):
        assert part in app, f"app.js lacks {part}"
    assert "function parsePatch(" in app and "function diffFileHtml(" in app and "function threadHtml(" in app
    assert "af-diff__line af-diff__line--${l.kind}" in app
    assert '"\\u2212"' in app, "a real minus sign in the sign column and the stat"
    assert "/api/${kind}/${id}/patch" in app
    assert 'changesHtml("jobs", j.id' in app and 'changesHtml("sessions", x.id' in app
    # comments are queued per job or session and ride with the next message, with the file:line and the line itself
    assert "const review = new Map()" in app and "function reviewText(" in app
    assert "Review comments on the change (" in app and "${c.key}\\n> ${c.code}\\n${c.text}" in app
    assert "Sent to the agent with your next message" in app and "review comment${queued === 1" in app
    assert '"data-send-comments"' in app and "review.delete(rkey)" in app
    # a half-written comment survives the 4 s repaint; no color of hangar's own in the diff
    assert "function restoreThreads(" in app and "restoreThreads(box, threads, rkey)" in app
    assert "af-diff__line--add" not in css and "af-diff__line--del" not in css, "the diff's tones are Airframe's"


def test_held_actions_are_approval_cards_decided_in_place():
    """theme-v6: a held tool call, a job waiting to start and an environment request are Airframe's ApprovalCard v2,
    on the job and session details, in the Fleet detail and in the Needs-you view; Deny asks for a reason and refuses
    without one (it goes back to the agent), Approve is the primary, "Allow for this turn" the second accept with a
    hint; decided ones are verdict rows. The Needs-you view reads GET /api/inbox and leads the nav."""
    html, app, fleet = (UI / "index.html").read_text(), (UI / "app.js").read_text(), (UI / "fleet.js").read_text()
    for part in ("af-approval", "af-approval__subject", "af-approval__cmd", "af-approval__act", "af-approval__btns",
                 "af-approval__reason", "af-approval__hint", "af-field__error", "af-verdict", "af-verdict__ok",
                 "af-verdict__why"):
        assert part in app, f"app.js lacks {part}"
    assert "function approvalHtml(" in app and "function denyReasonHtml(" in app and "function verdictHtml(" in app
    # the decision order and the reason rule
    btns = re.search(r'<div class="af-approval__btns">\$\{inspect\}(.*?)</div>', app, re.S).group(1)
    order = [btns.index(f'"data-decide": "{d}"') for d in ("deny", "allow_always", "allow")]
    assert order == sorted(order), "refuse, the second accept, then the primary accept"
    assert "(required)</span>" in app and "Reason for ${a.kind" in app
    assert "if (!reason) {" in app and "aria-invalid" in app, "deny without a reason is refused in place"
    # the three routes a card answers through, with the reason where each takes it
    assert "reason ? { reason } : {}" in app and '"reject" : "approve"' in app
    assert "{ decision, message: reason || null }" in app and "/api/env-requests/${a.id}" in app
    # where the cards are: job detail, session detail, Fleet detail (delegated clicks), Needs you
    assert app.count("approvalClick(e, ") >= 3
    assert "approvalClick(e, (id) => fleet.cards.get(id), loadFleet)" in fleet
    assert "jobApproval(j, false)" in app and "toolApprovals(x.pending_approvals" in app
    assert "function detailCards(u)" in fleet
    assert 'api("GET", "/api/inbox")' in app and 'id="view-needs"' in html
    assert 'href="#needs" data-view="needs"' in html
    assert html.index('href="#needs"') < html.index('href="#jobs"'), "what needs a person leads the nav"
    assert 'cls: "af-empty--clear"' in app and 'title: "Nothing is waiting on you."' in app, "the clear state"
    # a half-written reason or answer survives the 4 s repaint
    assert "function openReasons(" in app or "const openReasons" in app
    assert "restoreReasons(box, reasons, findCard)" in app and "restoreReasons(el, reasons" in fleet


def test_narrow_screens_get_a_bottom_tab_bar():
    """Below 760px the nine tabs become a bottom tab bar (Jobs, Sessions, Fleet, More) and the six other sections sit in
    the More sheet, so every section is reachable on a phone (Tower's pattern)."""
    html, css, app = (UI / "index.html").read_text(), (UI / "app.css").read_text(), (UI / "app.js").read_text()
    top = re.findall(r'<a class="af-nav__tab" href="#([a-z]+)"', html)
    bar = re.findall(r'<a class="tab" href="#([a-z]+)"', html)
    more = re.findall(r'<a class="af-menu__item" href="#([a-z]+)"', html)
    assert len(top) == 10 and bar == ["needs", "jobs", "sessions", "fleet"] and bar + more == top, (bar, more)
    assert 'class="tab" id="tab-more" data-more aria-expanded="false" aria-controls="more-sheet"' in html
    assert '<div id="more-sheet" class="more-sheet" role="dialog" aria-label="More sections" hidden>' in html
    # the counts show in both bars; the current section is marked in all three places
    for name in ("needs", "jobs", "sessions", "fleet"):
        assert html.count(f'data-count="{name}"') == 2, name
    assert "navCount(" in app and "function markCurrent()" in app and 'moreSheet(' in app
    # CSS: hidden wide, a fixed 4-column bar below 760px; what sits at the bottom stops above it
    assert ".tabbar, .more-sheet { display: none; }" in css
    narrow = css[css.index("@media (max-width: 760px)"):]
    for rule in (".topbar .af-nav__tabs { display: none; }", "grid-template-columns: repeat(5, 1fr)",
                 "--tabbar-h: calc(57px", ".terminal-drawer { bottom: var(--tabbar-h); }",
                 ".af-toasts { bottom: calc(var(--af-space-16) + var(--drawer-pad)); }"):
        assert rule in narrow, rule
    assert "--drawer-pad: calc(var(--af-drawer-inset) + var(--tabbar-h))" in css


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


def test_page_forms_are_airframe_fields_and_the_screen_has_an_empty_state():
    """Every field on the page forms (new job, session and sandbox, the credential form, the environment spec) is
    Airframe's af-field with its control in an af-input well; hangar styles only the two native controls Airframe has
    no part for (select, file). The agent screen says what is not there with an Airframe empty state."""
    html, css, app = (UI / "index.html").read_text(), (UI / "app.css").read_text(), (UI / "app.js").read_text()
    own = r"\b(?:af-field|af-field__label|check|chip)\b"
    bare = [m.group(0)[:80] for m in re.finditer(rf"<label(?![^>]*\bclass=\"[^\"]*{own})[^>]*>", html)]
    assert not bare, "a bare <label>: wrap the control in af-field / af-input:\n" + "\n".join(bare)
    for field in re.findall(r'<label class="af-field[^"]*">(.*?)</label>', html, re.S):
        assert field.startswith('<span class="af-field__label">'), field[:80]
        assert re.search(r'<span class="af-input|<select|<input type="file"', field), field[:80]
    sans = 'af-input af-input--area af-input--sans"><textarea name="instructions"'
    assert sans in html, "sentences people write are sans"
    assert 'field("Spec (YAML)", inputWell(' in app
    bare_css = re.search(r"^(?:input, select, textarea|label|textarea|input)\s*\{", css, re.M)
    assert not bare_css, "app.css styles bare controls; Airframe's af-input does"
    assert not re.search(r"[a-z]\(s\)[ ,.`<]", app), "write the plural (plural(n, word)), never '(s)'"
    vnc = (UI / "vnc.html").read_text()
    assert 'class="af-empty"' in vnc and 'show("no-data", "No screen to show."' in vnc and 'show("error"' in vnc


def test_phones_land_under_the_header_with_a_compact_fleet_and_activity_cards():
    """KAN-111: a tapped job or session scrolls in under the sticky header, not behind it; at 480px the Fleet header
    is three short rows (the views as a select, one line of counts); the Activity log's detail is keyed pairs, a run
    of identical rows collapses to one with a count, and on a phone each row is a card."""
    html, css = (UI / "index.html").read_text(), (UI / "app.css").read_text()
    app, fleet = (UI / "app.js").read_text(), (UI / "fleet.js").read_text()
    assert ".job-detail, .env-detail { scroll-margin-top: calc(var(--af-header-h) + var(--af-space-16)); }" in css
    assert '<select id="fleet-view-select" class="fleet-view-select" aria-label="Fleet view">' in html
    select = html.split('id="fleet-view-select"')[1].split("</select>")[0]
    assert re.findall(r'<option value="([a-z]+)">', select) == ["board", "bays", "columns", "owners"]
    assert '$("#fleet-view-select").value = fleet.view;' in fleet
    assert 'location.hash = `#fleet?view=${e.target.value}`' in fleet
    assert '<span class="fleet-brief af-muted">' in fleet
    phone = css[css.index("@media (max-width: 480px)"):]
    for rule in ('grid-template-areas: "title new" "sum sum" "views stopped"',
                 "#view-fleet .fleet-view-select { display: block;",
                 "#view-fleet .fleet-views, #view-fleet #fleet-sub > .af-meta, #view-fleet #fleet-sub > .af-cap "
                 "{ display: none; }",
                 "#audit-table thead { display: none; }", '"action when" "who who" "target target" "detail detail"'):
        assert rule in phone, rule
    assert "function auditRows(" in app and "function auditDetail(" in app and 'class="af-meta__key"' in app
    assert "× ${a.n} in ${auditSpan(a.ts - a.first)}" in app
    assert 'td class="af-table__mono clip"' not in app and ".table td.clip" not in css, "the detail is keyed pairs"


def test_theme_switch_lands_on_needs_you_and_one_primary_per_view():
    """KAN-110: Night is the default and a person can switch to Day (hangar.theme, applied before first paint); the
    console opens on Needs you while something waits; Jobs and Sessions carry one yellow primary at a time."""
    html, css, app = (UI / "index.html").read_text(), (UI / "app.css").read_text(), (UI / "app.js").read_text()
    assert html.startswith('<!doctype html>\n<html lang="en" data-theme="dark">'), "Night is the default, not the OS"
    head = html[:html.index("<link")]
    assert 'localStorage.getItem("hangar.theme")' in head, "the stored theme is applied before the stylesheets"
    assert (UI / "vnc.html").read_text().count('localStorage.getItem("hangar.theme")') == 1, "the agent screen follows"
    assert 'id="theme-switch" class="af-btn af-btn--ghost af-btn--sm af-btn--icon theme-switch"' in html
    assert 'aria-label="Switch to Day"' in html and '"Switch to Night"' in app
    assert 'localStorage.setItem("hangar.theme"' in app and "function applyTheme(" in app
    # the landing: Needs you when its count is above zero, the Fleet otherwise
    assert 'history.replaceState(null, "", waiting ? "#needs" : "#fleet")' in app
    # one primary: the page header's New job / New session are secondary; the panel's empty state carries the primary
    assert 'class="af-btn af-btn--secondary" id="btn-new-job"' in html
    assert 'class="af-btn af-btn--secondary" id="btn-new-session"' in html
    assert 'btn("New job", { kind: "primary"' in app and 'btn("New session", { kind: "primary"' in app
    assert ".view:has(> .new-form:not([hidden])) .job-detail .af-empty__act { display: none; }" in css
