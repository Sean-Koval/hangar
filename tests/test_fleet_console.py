"""The Fleet page's counts (KAN-162): the headline and the deck count the same agents, in the same words.

fleet.js is a classic browser script; these tests run it in node with the DOM stubbed out and call its model
functions on a fixture shaped like `GET /api/fleet`.
"""

# ruff: noqa: E501 - the embedded JavaScript
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "src" / "hangar" / "ui"
node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const any = new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => "" : k === Symbol.iterator ? [][Symbol.iterator] : any), apply: () => any, construct: () => any });
const ctx = { console, $: () => any, $$: () => [], localStorage: { getItem: () => null, setItem() {} }, airframe: any, document: any, window: any,
  location: { hash: "" }, history: any, addEventListener() {}, setTimeout: () => 0, setInterval: () => 0, requestAnimationFrame() {},
  CSS: { escape: (x) => x }, state: {}, tails: { see() {}, resolve: (x) => x },
  plural: (n, w) => `${n} ${w}${n === 1 ? "" : "s"}` };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + "\n;globalThis.F = { fleet, deckSets, headline, units, needYou };", ctx);
const F = ctx.F, D = JSON.parse(fs.readFileSync(0, "utf8"));
F.fleet.data = D;
const sets = F.deckSets();
console.log(JSON.stringify({ headline: F.headline(F.units()), stack: sets.stack.map((i) => i.id), stopped: sets.stopped.map((i) => [i.kind, i.unit || i.job]),
  one: F.needYou(1), five: F.needYou(5) }));
"""


def _run(data: dict) -> dict:
    r = subprocess.run(["node", "-e", HARNESS, str(UI / "fleet.js")], input=json.dumps(data), capture_output=True,
                       text=True, timeout=60, check=True)
    return json.loads(r.stdout)


def _unit(i: str, role: str, **kw) -> dict:
    return {"id": i, "role": role, "environment": "base", "work": f"work {i}", "created_at": 1, "inbox": [], **kw}


FLEET = {
    "units": [
        _unit("sb-1", "approval", job="job-1", inbox=["i-ask"]),
        _unit("sb-2", "denied", job="job-2", inbox=["i-deny"]),
        _unit("sb-3", "failed", job="job-3"),            # its item names the job, not the unit
        _unit("sb-4", "failed"),                         # the image failed to build: no job, no item
        _unit("sb-5", "running"),
    ],
    "inbox": {"items": [
        {"id": "i-ask", "kind": "approval", "job": "job-1", "since": 5},
        {"id": "i-deny", "kind": "denied", "job": "job-2", "since": 4},
        {"id": "i-fail", "kind": "failed", "job": "job-3", "since": 3},
        {"id": "i-review", "kind": "review", "job": "job-9", "since": 2},
    ]},
}


@node
def test_the_deck_lists_every_stopped_agent_the_headline_counts():
    got = _run(FLEET)
    m = re.search(r"(\d+) stopped", got["headline"])
    assert m and int(m.group(1)) == 3, got["headline"]
    assert len(got["stopped"]) == 3, got["stopped"]
    assert ["failed", "sb-4"] in got["stopped"], "a stopped agent with no inbox item is still listed"
    assert got["stack"] == ["i-ask"], "a stray stopped agent is a row under Stopped, never a card"


@node
def test_with_nothing_to_decide_the_stray_stopped_agent_still_shows():
    data = json.loads(json.dumps(FLEET))
    data["units"][0]["role"] = "running"
    data["inbox"]["items"] = [i for i in data["inbox"]["items"] if i["kind"] != "approval"]
    got = _run(data)
    assert got["stack"] == ["i-deny", "i-fail"] or got["stack"] == ["i-fail", "i-deny"]
    assert got["stopped"] == [["failed", "sb-4"]]


@node
def test_count_lines_say_need_you_in_digits():
    got = _run(FLEET)
    assert got["five"] == "5 need you" and got["one"] == "1 needs you"
    assert "1 needs you" in got["headline"]
    js = (UI / "fleet.js").read_text()
    assert "waiting on you`" not in js and "Nothing is waiting on you" not in js
    assert 'setAttribute("aria-label", needYou(q))' in js, "the nav badge reads the same words"
