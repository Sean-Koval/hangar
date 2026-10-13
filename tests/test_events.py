"""Activity events, usage and budgets (spec 0011 workstream C): parsers, prices, budgets and the event log.

The stream fixtures in tests/fixtures/streams/ are real output of the four harness CLIs, captured against a scripted
local model by the scripts in tests/fixtures/streams/capture/ (see capture.sh).
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from hangar.events import (
    DEFAULT_PRICES,
    KINDS,
    MAX_LINE,
    Activity,
    EventLog,
    Parser,
    RunEvent,
    add_usage,
    over_budget,
    parse_log,
    price_for,
    resolve_budget,
    total_usage,
    usage_line,
)
from hangar.models import Budget, Usage

STREAMS = Path(__file__).parent / "fixtures" / "streams"
HARNESSES = ["claude-code", "codex", "pi", "antigravity"]
# codex and agy don't name their model in the stream: hangar prices them with the model the turn was started with
MODELS = {"codex": "gpt-5.3-codex", "antigravity": "gemini-3.1-pro-preview"}


def fixture(name: str) -> bytes:
    return (STREAMS / f"{name}.jsonl").read_bytes()


def parsed(name: str, harness: str):
    return parse_log(fixture(name), harness, model=MODELS.get(harness))


@pytest.mark.parametrize("harness", HARNESSES)
def test_every_harness_yields_the_same_kinds(harness):
    evs, usage = parsed(harness, harness)
    kinds = {e.kind for e in evs}
    # Each capture writes a file, runs a shell command and answers: the same activity, whatever the harness.
    assert {"progress", "message", "tool_call", "tool_result", "command", "file_change", "usage"} <= kinds
    assert kinds <= set(KINDS)
    assert any(e.kind == "file_change" and e.text.endswith(("hello.txt", "notes.md")) for e in evs)
    assert any(e.kind == "command" and "hello.txt" in e.text for e in evs)
    assert any(e.kind == "tool_result" and "hello from the capture" in e.text and e.data["ok"] for e in evs)
    assert [e for e in evs if e.kind == "message"][-1].text.endswith("Done.")
    assert usage.tokens > 0 and usage.cost_usd and usage.cost_usd > 0
    assert all(e.off is not None for e in evs)
    assert not any(e.kind == "error" for e in evs)


def test_claude_usage_live_estimate_then_harness_total():
    evs, usage = parsed("claude-code", "claude-code")
    live = [e for e in evs if e.kind == "usage" and not e.data["final"]]
    # one usage event per model call, although Claude repeats a message's usage on each of its content blocks
    assert len(live) == 3 and all(e.data["cost_source"] == "prices" for e in live)
    final = [e for e in evs if e.kind == "usage" and e.data["final"]]
    assert len(final) == 1
    # the result event's own totals win: 4500 in, 120 out, 4000 cache reads, 500 cache writes, $0.018375
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (
        4500, 120, 4000, 500)
    assert usage.cost_usd == pytest.approx(0.018375) and usage.cost_source == "harness"
    # ...and our price table agrees with Claude Code's own arithmetic for the same tokens
    assert price_for("claude-sonnet-4-5", DEFAULT_PRICES)
    from hangar.events import cost_of

    assert cost_of(usage, DEFAULT_PRICES["claude-sonnet-4-5"]) == pytest.approx(0.018375)


def test_codex_usage_is_priced_from_the_table():
    _, usage = parsed("codex", "codex")
    # OpenAI counts cached tokens inside input_tokens: 10200 input of which 6144 cached
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (10200 - 6144, 6144, 180)
    p = DEFAULT_PRICES["gpt-5.3-codex"]
    want = (4056 * p["input"] + 6144 * p["cache_read"] + 180 * p["output"]) / 1e6
    assert usage.cost_usd == pytest.approx(want) and usage.cost_source == "prices"
    # without a known model the tokens still count, but the cost is unknown (not zero)
    _, unpriced = parse_log(fixture("codex"), "codex")
    assert unpriced.tokens == usage.tokens and unpriced.cost_usd is None


def test_pi_and_agy_usage():
    _, pi = parsed("pi", "pi")
    assert pi.cost_source == "harness" and pi.cost_usd == pytest.approx(0.018375)  # pi's own catalog price
    _, agy = parsed("antigravity", "antigravity")
    assert (agy.input_tokens, agy.output_tokens, agy.cache_read_tokens) == (8608, 240, 8192)
    assert agy.cost_source == "prices"


@pytest.mark.parametrize("name,harness,needle", [
    ("claude-code-error", "claude-code", "Prompt is too long"),
    ("codex-error", "codex", "context_length_exceeded"),
    ("antigravity-error", "antigravity", "API error"),
])
def test_error_streams(name, harness, needle):
    evs, _ = parsed(name, harness)
    errors = [e for e in evs if e.kind == "error"]
    assert errors and any(needle in e.text for e in errors)


@pytest.mark.parametrize("harness", HARNESSES)
def test_incremental_feeding_matches_one_pass(harness):
    """Bytes arrive in arbitrary pieces (partial lines, split UTF-8); the result must not depend on the split."""
    data = fixture(harness)
    whole, usage = parsed(harness, harness)
    rnd = random.Random(7)
    p = Parser(None, MODELS.get(harness))  # format detected from the stream itself
    got, i = [], 0
    while i < len(data):
        n = rnd.randint(1, 300)
        got += p.feed(data[i:i + n])
        i += n
    got += p.flush()
    assert [(e.kind, e.text, e.off) for e in got] == [(e.kind, e.text, e.off) for e in whole]
    assert p.view() == usage


@pytest.mark.parametrize("harness", HARNESSES)
def test_resume_from_saved_state(harness):
    """A restart mid-stream: a new parser loads the saved state and continues from its offset."""
    data = fixture(harness)
    whole, usage = parsed(harness, harness)
    first = Parser(None, MODELS.get(harness))
    cut = len(data) // 2 + 17  # mid-line
    evs = first.feed(data[:cut])
    state = json.loads(json.dumps(first.state()))  # it is stored as JSON on the turn
    assert state["offset"] <= cut and data[state["offset"] - 1:state["offset"]] == b"\n"
    second = Parser()
    second.load(state)
    evs += second.feed(data[state["offset"]:]) + second.flush()
    assert [(e.kind, e.off) for e in evs] == [(e.kind, e.off) for e in whole]
    assert second.view() == usage


def test_malformed_and_oversized_lines_are_skipped():
    p = Parser("claude")
    good = b'{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
    junk = b"plain stderr line\n{not json\n[1,2]\n" + b'{"type":"assistant","message":{"content":"nope"}}\n'
    huge = b'{"type":"assistant","message":{"content":[{"type":"text","text":"' + b"x" * (MAX_LINE + 10)
    evs = p.feed(junk + huge[:MAX_LINE // 2])
    evs += p.feed(huge[MAX_LINE // 2:])  # carry would exceed the cap: dropped
    evs += p.feed(b'"}]}}\n' + good)
    assert [e.text for e in evs] == ["hi"] and p.dropped == 1
    assert p.flush() == []


def test_unknown_stream_produces_nothing():
    evs, usage = parse_log(b'{"hello":"world"}\nnot json\n', None)
    assert evs == [] and usage.tokens == 0


def test_redaction_on_append(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path, events_max_mb=1, model_prices={})
    a = Activity(settings, lambda x: json.loads(json.dumps(x).replace("sk-secret", "***")))
    a.append("ses-1", [RunEvent(kind="command", text="curl -H 'x: sk-secret'", data={"cmd": "sk-secret"})])
    ev = a.read("ses-1")[0]
    assert ev.text == "curl -H 'x: ***'" and ev.data == {"cmd": "***"}


def test_prices():
    t = DEFAULT_PRICES
    assert price_for("claude-sonnet-4-5", t) is t["claude-sonnet-4-5"]
    assert price_for("claude-sonnet-4-5-20250929", t) is t["claude-sonnet-4-5"]  # dated snapshot
    assert price_for("anthropic/claude-opus-4-1", t) is t["claude-opus-4-1"]  # provider prefix
    assert price_for("claude-opus-4-20250514", t) is t["claude-opus-4"]
    assert price_for("claude-sonnet-4-5@20250929", t) is t["claude-sonnet-4-5"]  # vertex style
    assert price_for("claude-opus-4-7[1m]", t) is t["claude-opus-4-7"]
    assert price_for("google-vertex/gemini-3.1-pro-preview", t) is t["gemini-3.1-pro-preview"]
    assert price_for("claude-opus-4-9", t) is None  # not a date suffix: unknown, not Opus 4
    assert price_for(None, t) is None and price_for("my-local-model", t) is None
    # HANGAR_MODEL_PRICES extends and overrides the table
    a = Activity(SimpleNamespace(data_dir=Path("/nonexistent"), events_max_mb=1,
                                 model_prices={"My-Local-Model": {"input": 1, "output": 2},
                                               "claude-sonnet-4-5": {"input": 0, "output": 0}}), lambda x: x)
    assert price_for("my-local-model", a.prices) == {"input": 1, "output": 2}
    assert price_for("claude-sonnet-4-5", a.prices)["input"] == 0


def test_usage_sums_and_lines():
    a = Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=5000, cost_usd=0.1, cost_source="prices")
    b = Usage(input_tokens=500, output_tokens=100, cost_usd=0.2, cost_source="harness")
    s = add_usage(a, b)
    assert s.tokens == 1800 and s.cache_read_tokens == 5000 and s.cost_usd == pytest.approx(0.3)
    assert s.cost_source == "mixed"
    unknown = add_usage(a, Usage(input_tokens=10))
    assert unknown.cost_usd is None  # some tokens had no price: the total isn't known
    assert total_usage([SimpleNamespace(usage=a), SimpleNamespace(usage=None), SimpleNamespace(usage=b)]) == s
    assert total_usage([]) is None
    assert usage_line(s) == "1.8k tokens (+5.0k cached), $0.30"
    assert usage_line(unknown).endswith("cost unknown")


def test_budgets():
    settings = SimpleNamespace(budget_max_minutes=None, budget_max_tokens=None, budget_max_cost_usd=None)
    assert resolve_budget(None, settings) is None
    settings.budget_max_cost_usd = 5.0
    assert resolve_budget(None, settings) == Budget(max_cost_usd=5.0)
    assert resolve_budget(Budget(max_cost_usd=1, max_tokens=10), settings) == Budget(max_cost_usd=1, max_tokens=10)
    # a jobs token's cap limits what its callers ask for, and applies when they ask for nothing
    cap = Budget(max_cost_usd=2.0, max_minutes=30)
    assert resolve_budget(Budget(max_cost_usd=50), settings, cap) == Budget(max_cost_usd=2.0, max_minutes=30)
    assert resolve_budget(Budget(max_minutes=10), settings, cap) == Budget(max_minutes=10, max_cost_usd=2.0)
    with pytest.raises(ValueError):
        Budget(max_cost_usd=0)

    u = Usage(input_tokens=900, output_tokens=200, cache_read_tokens=10**6, cost_usd=0.6)
    import time

    assert over_budget(None, u, time.time()) is None
    assert "tokens" in over_budget(Budget(max_tokens=1000), u, time.time())  # cache reads don't count
    assert over_budget(Budget(max_tokens=2000), u, time.time()) is None
    assert "$0.60" in over_budget(Budget(max_cost_usd=0.5), u, time.time())
    assert over_budget(Budget(max_cost_usd=0.5), u.model_copy(update={"cost_usd": None}), time.time()) is None
    assert "min" in over_budget(Budget(max_minutes=1), None, time.time() - 120)


def test_event_log_cursor_tail_rotation_and_torn_lines(tmp_path):
    log = EventLog(tmp_path, max_bytes=64 * 1024)
    log.append("ses-a", [RunEvent(kind="message", text=f"m{i}", run_id="run-1", off=i) for i in range(5)])
    assert [e.seq for e in log.read("ses-a")] == [1, 2, 3, 4, 5]
    assert [e.text for e in log.read("ses-a", since=3)] == ["m3", "m4"]
    assert [e.text for e in log.read("ses-a", limit=2)] == ["m0", "m1"]
    assert [e.text for e in log.read("ses-a", tail=2)] == ["m3", "m4"]
    assert log.max_off("ses-a", "run-1") == 4 and log.max_off("ses-a", "run-2") == -1
    # a new process continues the sequence; a write torn by a crash doesn't swallow the next event
    with open(tmp_path / "ses-a.jsonl", "ab") as f:
        f.write(b'{"seq":6,"ts":1,"tu')
    again = EventLog(tmp_path, max_bytes=64 * 1024)
    again.append("ses-a", [RunEvent(kind="progress", text="after")])
    assert [e.text for e in again.read("ses-a", since=5)] == ["after"]
    # past the cap the log rotates once; reads span both files, seq keeps counting
    for _ in range(40):
        again.append("ses-a", [RunEvent(kind="message", text="x" * 2000)])
    assert (tmp_path / "ses-a.1.jsonl").exists()
    seqs = [e.seq for e in again.read("ses-a", limit=1000)]
    assert seqs == sorted(seqs) and seqs[-1] == again.last_seq("ses-a")
    again.drop("ses-a")
    assert again.read("ses-a") == [] and not list(tmp_path.glob("ses-a*"))
