"""The console's front door against script injection (KAN-174, finding L-7).

The console renders agent-controlled text (job titles, summaries, events, branch names, PR titles, terminal output)
and holds the admin token. Three layers keep one missed escape from leaking it:

* a strict Content-Security-Policy on every page (no inline script, no eval, only the pinned CDN files), with
  nosniff, no referrer and no framing;
* every HTML sink in the console's own scripts audited: each `innerHTML`, `outerHTML`, `insertAdjacentHTML` and
  `airframe.el` is listed below, so a new one, or one moved to another function, fails here until someone has read it;
* the admin token in sessionStorage, so it is gone with the browser and never written to disk.

`python tests/test_ui_csp.py` prints the sinks as the audit table wants them.
"""

import re
from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient

from hangar.app import create_app
from hangar.uiheaders import csp

UI = Path(__file__).resolve().parents[1] / "src" / "hangar" / "ui"
PAGES = ["index.html", "vnc.html"]
SCRIPTS = ["app.js", "fleet.js", "tails.js", "theme.js", "boot.js", "vnc.js"]

SINK = re.compile(r"\.(?:innerHTML|outerHTML)\s*=(?!=)|\.insertAdjacentHTML\(|document\.write(?:ln)?\(|\bairframe\.el\("
                  r"|\bhtml:\s*true\b|\beval\(|\bnew Function\(|\bsetTimeout\(\s*[\"'`]|\bsetInterval\(\s*[\"'`]")
TOP = re.compile(r"^(?:async\s+)?function\s+(\w+)|^(?:const|let)\s+(\w+)\s*=|^(\S.{0,47})")

# Every HTML sink in the console's scripts, by the top-level function (or statement) it sits in, with how often.
# Audited for KAN-174: each one writes markup built from static strings, numbers, and data passed through esc() (or
# Airframe's helpers, which escape every value: status, meta, btn, avatar, icon), idHtml/tails (tail numbers, from a
# strict ID pattern), linkify (escapes first, links http(s) only) or safeUrl (http(s) only, escaped). A new sink goes
# through the same review: escape every interpolated value, or build the node with textContent / DOM APIs.
AUDITED = Counter({
    "app.js: diffClick: if (mine.length) thread.outerHTML =": 1,  # threadHtml: esc on every comment and key
    "app.js: diffClick: row.insertAdjacentHTML(": 1,
    "app.js: restoreThreads: row.insertAdjacentHTML(": 1,
    "app.js: busy: finally { if (btn) { btn.disabled = false; btn.innerHTML =": 1,  # the button's own markup back
    "app.js: showGate: $(\"#gate-error\").innerHTML =": 1,  # errorLine: Airframe status() escapes
    "app.js: refreshMeta: $(\"#providers\").innerHTML =": 1,
    "app.js: refreshMeta: $(\"#more-providers\").innerHTML =": 1,
    "app.js: drawProd: el.innerHTML =": 1,  # --- KAN-216: item() escapes every key, value and title with esc()
    "app.js: syncNewCreds: $(\"#new-creds\").innerHTML =": 1,
    "app.js: $(\"#new-form\").addEventListener(\"submit\", async: btn.innerHTML =": 1,  # its own markup
    "app.js: dialog: f.innerHTML =": 1,  # titles are idHtml or esc'd at the call; bodies are field() and static
    "app.js: openTerminal: tab.innerHTML =": 1,
    "app.js: connectTerm: entry.el.insertAdjacentHTML(": 1,  # the server's error text through esc()
    "app.js: (function initDrawer() {: extra.innerHTML =": 1,  # static
    "app.js: renderJobs: $(\"#job-list\").innerHTML =": 1,
    "app.js: renderJobs: else if (!$(\"#job-detail .af-empty\")) $(\"#job-detail\").innerHTML =": 1,
    "app.js: renderJob: if (!j) { box.innerHTML =": 1,
    "app.js: renderJob: keepFocus(box, () => { box.innerHTML =": 1,  # summaries, events, PR links: esc, safeUrl
    "app.js: renderJob: $(\"#job-composer\")?.prepend(airframe.el(": 1,  # queuedRow: esc
    "app.js: renderSessions: $(\"#session-list\").innerHTML =": 1,
    "app.js: renderSessions: else if (!$(\"#session-detail .af-empty\")) $(\"#session-detail\").innerHTML =": 1,
    "app.js: renderSession: if (!x) { box.innerHTML =": 1,
    "app.js: renderSession: keepFocus(box, () => { box.innerHTML =": 1,
    "app.js: drawActivity: el.innerHTML =": 1,  # agent events: esc, linkify
    "app.js: document.addEventListener(\"click\", (e) => {: more.innerHTML =": 1,  # static
    "app.js: loadEnvs: $(\"#env-list\").innerHTML =": 1,
    "app.js: loadEnvs: else if (!$(\"#env-detail .af-empty\")) $(\"#env-detail\").innerHTML =": 1,
    "app.js: showEnv: box.innerHTML =": 1,
    "app.js: loadCreds: $(\"#cred-table tbody\").innerHTML =": 1,
    "app.js: loadCreds: $(\"#login-buttons\").innerHTML =": 1,
    "app.js: loadSnaps: $(\"#snap-table tbody\").innerHTML =": 1,
    "app.js: loadConnect: $(\"#connect-snippets\").innerHTML =": 1,
    "app.js: loadOAuth: $(\"#oauth-table tbody\").innerHTML =": 1,
    "app.js: loadTokens: $(\"#token-table tbody\").innerHTML =": 1,
    "app.js: $(\"#token-form\").addEventListener(\"submit\", asyn: box.innerHTML =": 1,
    "app.js: drawAudit: $(\"#audit-table tbody\").innerHTML =": 1,
    "app.js: loadUsage: $(\"#usage-tiles\").innerHTML =": 1,
    "app.js: loadUsage: $(`${sel} tbody`).innerHTML =": 1,
    "app.js: loadUsage: $(\"#usage-stops tbody\").innerHTML =": 1,
    "app.js: drawSpend: if (!max) { box.innerHTML =": 1,
    "app.js: drawSpend: box.innerHTML =": 1,  # numbers and dates
    "app.js: drawSpend: tip.innerHTML =": 1,
    "fleet.js: followOpen: ol.innerHTML =": 1,  # timelineHtml: esc
    "fleet.js: drawFleet: $(\"#fleet-title\").innerHTML =": 1,  # counts
    "fleet.js: drawNeeds: $(\"#needs-sum\").innerHTML =": 1,
    "fleet.js: paint: el.innerHTML =": 1,  # the deck, rows, cards, consoles: esc, idHtml, safeUrl
    "fleet.js: patchUnit: for (const n of $$(`[data-ring=\"${CSS.escape(u.id)}\"]`)) n.innerHTML =": 1,
    "fleet.js: patchUnit: for (const n of $$(`[data-capsule=\"${CSS.escape(u.id)}\"]`)) n.innerHTML =": 1,
    "fleet.js: patchUnit: for (const n of $$(`[data-spark=\"${CSS.escape(u.id)}\"]`)) { const w = Number(n.getAttribute"
    "(\"width\")), h = Number(n.getAttribute(\"height\")); n.outerHTML =": 1,
    "fleet.js: patchUnit: for (const n of $$(`[data-stages=\"${CSS.escape(u.id)}\"]`)) n.outerHTML =": 1,
    "fleet.js: setInterval(() => {: for (const n of $$(`[data-stages=\"${CSS.escape(u.id)}\"]`)) { const h = "
    "stagesHtml(u); if (n.outerHTML !== h) n.outerHTML =": 1,
})


def sinks() -> Counter:
    found = Counter()
    for name in SCRIPTS:
        top = ""
        for line in (UI / name).read_text().splitlines():
            m = TOP.match(line)
            if m and not line.startswith(("/*", "//", " ", "}", ")", "]")):
                top = m.group(1) or m.group(2) or m.group(3).strip()
            s = SINK.search(line)
            if s:
                found[f"{name}: {top}: {line[:s.end()].strip()}"] += 1
    return found


def test_every_html_sink_is_audited():
    found = sinks()
    new, gone = found - AUDITED, AUDITED - found
    assert not new, ("an HTML sink nobody has audited (KAN-174): escape every interpolated value with esc() or an "
                     "Airframe helper, or build the node with textContent / DOM APIs, then list it in AUDITED:\n"
                     + "\n".join(new))
    assert not gone, "an audited sink is gone: take it out of AUDITED\n" + "\n".join(gone)


def test_sink_guard_catches_raw_sinks():
    assert all(SINK.search(x) for x in ("el.innerHTML = x", "el.outerHTML=x", "el.insertAdjacentHTML('beforeend', x)",
                                         "document.write(x)", "airframe.el(x)", "confirm({html: true})", "eval(s)",
                                         "setTimeout('alert(1)')"))
    safe = ("if (n.innerHTML === h)", "el.textContent = x", "const label = btn?.innerHTML;")
    assert not any(SINK.search(x) for x in safe)


def test_links_from_data_are_http_only():
    """An href built from data goes through safeUrl (http and https only): an agent's PR URL or a job's context link
    can't be a javascript: URL."""
    bad = []
    for name in ("app.js", "fleet.js"):
        for n, line in enumerate((UI / name).read_text().splitlines(), 1):
            for m in re.finditer(r'href="\$\{(?!safeUrl\()([^}]*)\}', line):
                if not (name == "app.js" and m.group(1) == "u"):  # linkify: escaped, and its pattern is https?:// only
                    bad.append(f"{name}:{n}: {m.group(0)}")
            if re.search(r"javascript:", line, re.I):
                bad.append(f"{name}:{n}: javascript: URL")
    assert not bad, "\n".join(bad)


def test_pages_have_no_inline_script():
    """The policy refuses inline script, so the pages carry none: no <script> without src, no on* attributes, no
    javascript: URLs, and every external script and stylesheet is one the policy names."""
    policy = csp()
    srcs = dict(p.split(" ", 1) for p in policy.split("; "))
    for name in PAGES:
        page = (UI / name).read_text()
        for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.S):
            assert not tag.strip(), f"{name}: an inline script"
        assert not re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", page), f"{name}: a <script> without src"
        assert not re.search(r"\son[a-z]+\s*=", page, re.I), f"{name}: an inline event handler"
        assert "javascript:" not in page.lower()
        assert "<style" not in page, f"{name}: a <style> block (put it in a stylesheet)"
        for src in re.findall(r'<script[^>]*\bsrc="(https://[^"]+)"', page):
            assert any(src == s or (s.endswith("/") and src.startswith(s)) for s in srcs["script-src"].split()), src
        for href in re.findall(r'<link rel="stylesheet" href="(https://[^"]+)"', page):
            assert any(href == s or (s.endswith("/") and href.startswith(s)) for s in srcs["style-src"].split()), href
    vnc = (UI / "vnc.js").read_text()
    for src in re.findall(r'from "(https://[^"]+)"', vnc):
        assert any(s.endswith("/") and src.startswith(s) for s in srcs["script-src"].split()), src


def test_policy_is_strict():
    p = dict(x.split(" ", 1) for x in csp("hangar.example:8080").split("; "))
    assert p["default-src"] == "'none'" and p["object-src"] == "'none'" and p["base-uri"] == "'none'"
    assert p["frame-ancestors"] == "'none'" and p["form-action"] == "'self'"
    assert "'unsafe-inline'" not in p["script-src"] and "'unsafe-eval'" not in p["script-src"]
    assert "https:" not in p["script-src"].split() and "https://cdn.jsdelivr.net" not in p["script-src"].split(), \
        "a CDN is allowed file by file (or one pinned package), never whole"
    assert p["connect-src"].split() == ["'self'", "ws://hangar.example:8080", "wss://hangar.example:8080"]
    # a Host header that isn't a host name adds nothing to the policy
    for host in ("evil.example; script-src *", "a b", "x\r\ny", ""):
        assert dict(x.split(" ", 1) for x in csp(host).split("; "))["connect-src"] == "'self'"


def test_console_pages_send_the_headers(settings):
    c = TestClient(create_app(settings))
    for path in ("/", "/ui/vnc.html", "/ui/index.html"):
        r = c.get(path, headers={"host": "hangar.test"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), path
        policy = r.headers["content-security-policy"]
        script_src = policy.split("script-src", 1)[1].split(";")[0]
        assert script_src.startswith(" 'self' ") and "'unsafe-inline'" not in script_src, path
        assert "frame-ancestors 'none'" in policy and "wss://hangar.test" in policy, path
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-frame-options"] == "DENY", path
    for path in ("/ui/app.js", "/ui/fleet.js", "/ui/theme.js", "/ui/boot.js", "/ui/vnc.js", "/ui/vnc.css",
                 "/ui/airframe/airframe.js", "/ui/app.css"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff", path
        assert "content-security-policy" not in r.headers, path  # a page's policy, not a script's
    # the API is untouched: a bearer token still works, and no page headers ride on JSON
    r = c.get("/api/overview")
    assert r.status_code == 401 and "content-security-policy" not in r.headers


def test_token_lives_in_session_storage():
    app = (UI / "app.js").read_text()
    store = app[app.index("const store = {"):app.index("};", app.index("const store = {"))]
    assert 'sessionStorage.getItem("hangar.token")' in store and 'sessionStorage.setItem("hangar.token", v)' in store
    assert 'localStorage.setItem("hangar.token"' not in app, "the token is never written to localStorage"
    assert 'localStorage.removeItem("hangar.token")' in app, "a token an older console stored is deleted"
    assert "Authorization: `Bearer ${store.token}`" in app, "the console still sends the token as a bearer header"


if __name__ == "__main__":
    for k, v in sorted(sinks().items()):
        print(f"{v}  {k}")
