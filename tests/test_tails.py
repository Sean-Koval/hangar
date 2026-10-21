"""Tail numbers (KAN-114): hangar shows Airframe's short IDs (HGR-7Q2K, HGR-JOB-4F7A) derived from the real ones.

The console's formatter is src/hangar/ui/tails.js; src/hangar/tails.py is the same function for what the server writes
for people. Display only: the API, URLs and data attributes keep the real ID.
"""

# ruff: noqa: E501 - the embedded JavaScript and the guard's patterns
import json
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from hangar import tails

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "hangar" / "ui"
# Airframe's form (guide/brand.md, "Tail-number IDs"): product prefix, optional kind, a short code; uppercase,
# hyphenated; the code in Crockford base32 (no I, L, O, U)
TAIL = re.compile(r"^HGR-(?:(?:JOB|SES|APR|SNAP|REQ|RUN|BLD|TOK)-)?[0-9A-HJKMNP-TV-Z]{4,10}$")
node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def _ids(n: int, prefix: str = "sb", nbytes: int = 4) -> list[str]:
    out: set[str] = set()
    while len(out) < n:
        out.add(f"{prefix}-{secrets.token_hex(nbytes)}")
    return sorted(out)


def _js(script: str, data) -> object:
    """Run `script` in node with tails.js loaded as `T` and `data` as `D`; returns what it prints as JSON."""
    prog = (f"const T = require({json.dumps(str(UI / 'tails.js'))});"
            f"const D = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n{script}")
    r = subprocess.run(["node", "-e", prog], input=json.dumps(data), capture_output=True, text=True, timeout=60, check=True)
    return json.loads(r.stdout)


def test_python_tails_follow_airframes_form_and_are_deterministic():
    assert tails.tail("sb-8e7f50c8") == "HGR-8AS5" and tails.tail("job-e0123d68") == "HGR-JOB-VDTR"
    assert tails.tail("apr-8e7f50c8") == "HGR-APR-P9TK" and tails.tail("ap-0123456789ab") == "HGR-APR-XN3D"
    assert tails.tail("snap-1234abcd") == "HGR-SNAP-T0SC" and tails.tail("ses-1a2b3c4d") == "HGR-SES-S12P"
    for raw in _ids(200) + _ids(50, "job") + _ids(50, "ap", 6):
        assert TAIL.match(tails.tail(raw)), raw
        assert tails.tail(raw) == tails.tail(raw)
    # names and anything else are not IDs: they stay as typed
    for name in ("base-codex", "tax-rules", "sb-x", "", None, "hangar/job-e0123d68"):
        assert tails.tail(name) == (name or "")
    assert tails.label("job-e0123d68") == "HGR-JOB-VDTR (`job-e0123d68`)"


@node
def test_console_and_server_compute_the_same_tail():
    ids = ["sb-8e7f50c8", "job-e0123d68", "apr-8e7f50c8", "ses-1a2b3c4d", "snap-1234abcd", "rq-00ff00ff00ff",
           "base-codex"] + _ids(300) + _ids(100, "job")
    got = _js("console.log(JSON.stringify(D.map((id) => T.code(id))))", ids)
    assert got == [tails.code(i, tails.MAX) for i in ids]
    # a page that has seen just these IDs, all distinct at four characters, shows the server's form
    four = [i for i in ids if tails.is_raw(i)][:20]
    assert len({tails.code(i) for i in four}) == len(four)
    assert _js("console.log(JSON.stringify(D.map((id) => T.tail(id))))", four) == [tails.tail(i) for i in four]


@node
def test_no_two_ids_read_the_same_over_ten_thousand():
    """Four characters are Airframe's length; two IDs the page has seen that share a code both grow until they differ,
    so no two live objects look the same, and a 200-sandbox fleet almost always stays at four."""
    ids = _ids(10_000)
    r = _js("""T.see(D); const shown = D.map(T.tail), codes = D.map(T.short);
      console.log(JSON.stringify({unique: new Set(shown).size, longest: Math.max(...codes.map((c) => c.length)),
        again: D.every((id, i) => T.tail(id) === shown[i]), form: shown.every((t) => /^HGR-[0-9A-HJKMNP-TV-Z]{4,10}$/.test(t))}))""", ids)
    assert r == {"unique": 10_000, "longest": r["longest"], "again": True, "form": True} and r["longest"] <= 7
    fleet = _ids(200)
    r = _js("T.see(D); console.log(JSON.stringify(new Set(D.map(T.tail)).size))", fleet)
    assert r == 200


@node
def test_a_tail_resolves_back_to_its_id():
    ids = ["sb-8e7f50c8", "sb-11111111", "job-e0123d68", "job-22222222"]
    got = _js("""T.see(D); console.log(JSON.stringify([T.resolve("HGR-8AS5", D), T.resolve("8as5", D),
      T.resolve("hgr-job-vdtr", D), T.resolve("job-e0123d68", D), T.resolve("HGR-JOB-8AS5", D), T.resolve("tax-rules", D)]))""", ids)
    assert got == ["sb-8e7f50c8", "sb-8e7f50c8", "job-e0123d68", "job-e0123d68", "HGR-JOB-8AS5", "tax-rules"]


@node
def test_markup_keeps_the_real_id_in_title_and_data_only():
    got = _js("""console.log(JSON.stringify([T.html("sb-8e7f50c8"), T.html("sb-8e7f50c8", {codeOnly: true, cls: "af-agent__tail"}),
      T.inText("queued as job-e0123d68 on hangar/job-e0123d68 by job:job-e0123d68"), T.inPlain("Cancelling job-e0123d68")]))""", [])
    assert got[0] == '<span class="af-id" title="sb-8e7f50c8" data-raw-id="sb-8e7f50c8">HGR-8AS5</span>'
    assert got[1] == '<span class="af-agent__tail" title="sb-8e7f50c8" data-raw-id="sb-8e7f50c8">8AS5</span>'
    # a branch keeps its name as typed; an ID in the words reads as its tail
    assert got[2].count("HGR-JOB-VDTR") == 2 and "hangar/job-e0123d68" in got[2]
    assert got[3] == "Cancelling HGR-JOB-VDTR"


# an interpolation of an ID straight into the page: `${esc(j.id)}`, `${u.tail}`, `${x.job_id}` ...
INTERP = re.compile(r"\$\{(?:esc\()?([\w?.]*?(?:\.id|_id|\.tail|\.job|\.sandbox|\.session|\.approval|\.run_id))\)?\}")


def test_the_console_shows_no_raw_id_outside_attributes_and_urls():
    page = (UI / "index.html").read_text()
    assert page.index('src="/ui/tails.js"') < page.index('src="/ui/app.js"') < page.index('src="/ui/fleet.js"')
    for name in ("app.js", "fleet.js"):
        js = (UI / name).read_text()
        assert "u.tail" not in js and "u?.tail" not in js, f"{name}: the server's 4-hex tail is retired; use tailOf(u.id)"
        bad = []
        for m in INTERP.finditer(js):
            before = js[max(0, m.start() - 200):m.start()]
            line = before.rsplit("\n", 1)[-1]
            in_attr = re.search(r'[\w-]+="[^"<>]*$', before) is not None  # title="…", data-id="…", href="…"
            in_url = re.search(r"(/api/|#jobs\?job=|\?ticket=)[^`\s]*$|/approvals/$|`(?:jobs|sessions)/$", line) is not None
            in_key = re.search(r"(?:key|draft|DRAFT|CSS\.escape)\([^)]*$|`\$\{where\}-r-$|=\s*`?$|\"data-[\w-]+\":\s*`[^`]*$",
                               line) is not None  # a lookup key, a data attribute given as an object
            if not (in_attr or in_url or in_key):
                bad.append(f"{name}:{js.count(chr(10), 0, m.start()) + 1}: {m.group(0)}")
        assert not bad, "raw IDs shown as text (use tailOf/idHtml):\n" + "\n".join(bad)
