"""Prompt caching (overnight/cache, spec 0012 item 1): the TTL each harness is asked for, one model per session,
best-of-N/fork siblings waiting for the first to cache the shared prefix, and what caching saved.

The real-CLI tests at the end run Claude Code and pi offline against the scripted Messages API in
fixtures/live/fake_models.py and check the cache_control TTL they actually send. They are skipped unless
HANGAR_TEST_CLAUDE / HANGAR_TEST_PI name the CLIs (verified with claude 2.1.296 and pi 1.2.0).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from hangar import cache, metrics, usage
from hangar.envspec import EnvSpec
from hangar.events import DEFAULT_PRICES, RunEvent, add_usage, cost_of, parse_log, price_for, saved_of
from hangar.harnesses import HARNESSES, Harness
from hangar.models import Session, SessionTurn, Usage
from hangar.sessions import NewSession, SessionError, SessionService
from hangar.vault import Vault
from tests.conftest import scratch_root
from tests.fixtures.live import fake_models

TTL_1H = {"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}
TTL_5M = {"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"}


# ====================================================================== policy -> env, per harness
class _Catalog:
    def __init__(self, spec: EnvSpec):
        self.spec = spec

    def get(self, name: str) -> EnvSpec:
        if name != self.spec.name:
            raise KeyError(name)
        return self.spec


def _svc(settings, **spec) -> SimpleNamespace:
    env = EnvSpec(name="env", harnesses=["claude-code", "codex", "pi", "antigravity"], **spec)
    out = SimpleNamespace(settings=settings, events=[], audits=[], saved=0)
    out.fleet = SimpleNamespace(catalog=_Catalog(env), audit=lambda *a, **k: out.audits.append((a, k)))
    out.activity = SimpleNamespace(append=lambda sid, evs: out.events.extend(evs))
    out._save = lambda s: setattr(out, "saved", out.saved + 1)
    return out


def _session(harness: str, **kw) -> Session:
    return Session(name="s", environment="env", harness=harness, **kw)


@pytest.mark.parametrize(("harness", "policy", "idle", "approvals", "ttl", "env"), [
    ("claude-code", "auto", 30, None, "1h", TTL_1H),  # a session that waits 30 min for its next message
    ("claude-code", "auto", 0, None, "1h", TTL_1H),  # never suspends
    ("claude-code", "auto", 5, None, "5m", TTL_5M),  # never waits longer than the 5-minute cache
    ("claude-code", "auto", 5, "ask", "1h", TTL_1H),  # people answer its tool approvals
    ("claude-code", "5m", 30, None, "5m", TTL_5M),
    ("claude-code", "1h", 2, None, "1h", TTL_1H),
    ("claude-code", "off", 30, None, "off", {"DISABLE_PROMPT_CACHING": "1"}),
    ("pi", "auto", 30, None, "1h", {"PI_CACHE_RETENTION": "long"}),
    ("pi", "5m", 30, None, "5m", {"PI_CACHE_RETENTION": "short"}),
    ("pi", "off", 30, None, None, {}),  # pi has no switch for it: its default applies
    ("codex", "auto", 30, None, None, {}),  # OpenAI caches automatically; codex has no setting
    ("codex", "1h", 30, None, None, {}),
    ("antigravity", "auto", 30, None, None, {}),
])
def test_policy_env_per_harness(settings, harness, policy, idle, approvals, ttl, env):
    svc = _svc(settings, approvals=approvals)
    env_out, got, why = cache.turn_env(svc, _session(harness, cache=policy, idle_minutes=idle), HARNESSES[harness])
    assert (got, env_out) == (ttl, env), why
    assert why


def test_policy_precedence_and_models(settings):
    h = HARNESSES["claude-code"]
    settings.cache = "5m"
    svc = _svc(settings)
    assert cache.choose(svc, _session("claude-code"), h)[0] == "5m"  # HANGAR_CACHE
    svc = _svc(settings, cache="off")
    assert cache.choose(svc, _session("claude-code"), h)[0] == "off"  # the environment's
    assert cache.choose(svc, _session("claude-code", cache="1h"), h)[0] == "1h"  # the session's wins
    settings.cache = "auto"
    svc = _svc(settings)
    for model, ttl in (("claude-opus-5-5", "1h"), ("opus", "1h"), ("anthropic/claude-sonnet-5-5[1m]", "1h"),
                       ("us.anthropic.claude-haiku-5-5-v1:0", "1h"), (None, "1h"), ("my-gateway-model", "5m")):
        assert cache.choose(svc, _session("claude-code", model=model), h)[0] == ttl, model
    # an agent that asks people questions waits on them, whatever the idle time
    assert cache.choose(svc, _session("claude-code", idle_minutes=3, needs_input="which one?"), h)[0] == "1h"
    asked = _session("claude-code", idle_minutes=3, turns=[SessionTurn(message="m", outcome="needs_input")])
    assert cache.choose(svc, asked, h)[0] == "1h"
    assert cache.choose(svc, _session("claude-code", idle_minutes=3), h)[0] == "5m"
    # every harness that asks for a TTL does so with values the CLI accepts (Claude Code ignores anything else)
    assert {v for e in h.cache_env.values() for k, v in e.items() if k.endswith("_TTL")} == {"5m", "1h"}


def test_pin_and_switch_model(settings):
    svc = _svc(settings, model={"claude-code": "claude-opus-5-5"})
    s = _session("claude-code")
    cache.pin_model(svc, s)
    assert s.model == "claude-opus-5-5"
    assert cache.switch_model(svc, s, None, "sean") is None and cache.switch_model(svc, s, " claude-opus-5-5 ", "x") \
        is None and not svc.events
    w = cache.switch_model(svc, s, "claude-sonnet-5-5", "sean")
    assert s.model == "claude-sonnet-5-5" and "uncached" in w and "claude-opus-5-5" in w
    (ev,) = svc.events
    assert ev.kind == "progress" and ev.data["warning"] == "model_switch" and ev.data["to"] == "claude-sonnet-5-5"
    assert svc.audits[0][0][1] == "session.model" and svc.saved == 1
    with pytest.raises(ValueError):
        cache.switch_model(svc, s, "a\nb", "sean")
    s.turns.append(SessionTurn(message="hi"))
    assert "uncached" in cache.note_switch(svc, s, "claude-haiku-5-5", "sean") and s.turns[-1].warnings


# ====================================================================== siblings
async def test_stagger_leader_goes_first_and_releases_the_rest():
    st = cache.Stagger()
    st.group(["a", "b", "c"])
    assert await st.wait("b", 5) == 0.0  # the first to start leads
    waits = asyncio.gather(st.wait("a", 5), st.wait("c", 5))
    await asyncio.sleep(0.2)
    st.responded("a")  # only the leader's response counts
    await asyncio.sleep(0.1)
    assert not waits.done()
    st.responded("b")
    a, c = await waits
    assert 0.25 < a < 2 and 0.25 < c < 2
    assert await st.wait("d", 5) == 0.0 and not st._of  # strangers and finished groups never wait


async def test_stagger_waits_at_most_seconds_after_the_leader_started():
    st = cache.Stagger()
    st.group(["a", "b"])
    await st.wait("a", 0.3)
    t = time.monotonic()
    assert 0.2 < await st.wait("b", 0.3) < 0.6 and time.monotonic() - t < 0.6
    st.group(["x", "y"])
    await st.wait("x", 0.2)
    await asyncio.sleep(0.3)
    assert await st.wait("y", 0.2) == 0.0  # it started late enough: no wait
    st.group(["p", "q"])
    assert await st.wait("p", 0) == 0.0 and await st.wait("q", 0) == 0.0  # HANGAR_CACHE_STAGGER_SECONDS=0
    st.group(["solo"])
    assert await st.wait("solo", 5) == 0.0


def test_stagger_forgets_siblings_that_never_started(monkeypatch):
    st = cache.Stagger()
    st.group(["a", "b"])
    monkeypatch.setattr(cache, "STALE_SECONDS", -1)
    st.group(["c", "d"])
    assert set(st._of) == {"c", "d"}


async def test_watched_hook_releases_through_the_shared_stagger():
    cache.STAGGER.group(["l1", "f1"])
    await cache.STAGGER.wait("l1", 5)
    w = asyncio.ensure_future(cache.STAGGER.wait("f1", 5))
    await asyncio.sleep(0.1)
    cache.watched("l1", [RunEvent(kind="progress", text="agent started")])
    await asyncio.sleep(0.1)
    assert not w.done()
    cache.watched("l1", [RunEvent(kind="usage", text="1k tokens")])
    assert 0.1 < await w < 2


# ====================================================================== usage maths against the price table
def test_price_table_matches_the_spec_numbers():
    opus = price_for("claude-opus-5-5", DEFAULT_PRICES)
    # spec 0012 §6.5: a 150k-token conversation on Opus 5.5, read from cache vs written again at the 5m price
    assert cost_of(Usage(cache_read_tokens=150_000), opus) == pytest.approx(0.03)
    assert cost_of(Usage(cache_write_tokens=150_000), opus) == pytest.approx(0.75)
    # a 1-hour write costs 2x input ($8/MTok), $0.30 more per 100k tokens than a 5-minute one
    assert cost_of(Usage(cache_write_tokens=100_000, cache_write_1h_tokens=100_000), opus) == pytest.approx(0.80)
    assert cost_of(Usage(cache_write_tokens=100_000), opus) == pytest.approx(0.50)
    # best-of-N, 4 attempts on a 100k prefix: all write ($2.00), or one writes and three read ($0.56)
    assert 4 * cost_of(Usage(cache_write_tokens=100_000), opus) == pytest.approx(2.00)
    assert cost_of(Usage(cache_write_tokens=100_000, cache_read_tokens=300_000), opus) == pytest.approx(0.56)


def test_claude_usage_prices_1h_writes_and_savings():
    u = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 1000,
         "cache_creation": {"ephemeral_5m_input_tokens": 400, "ephemeral_1h_input_tokens": 600}}
    lines = [{"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": "x"},
             {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [
                 {"type": "text", "text": "hi"}], "usage": u}}]
    evs, got = parse_log("\n".join(map(json.dumps, lines)).encode() + b"\n", "claude-code")
    # $4 in, $20 out, $0.20 read, $5 5m write, $8 1h write per MTok
    assert got.cache_write_1h_tokens == 600 and got.cost_usd == pytest.approx((40 + 100 + 1800 + 2000 + 4800) / 1e6)
    # without a cache: 10,010 prompt tokens at $4 = $0.04004 + output; saved = reads minus the write premiums
    assert got.saved_usd == pytest.approx((9000 * 3.8 - 400 * 1 - 600 * 4) / 1e6)
    assert got.cache_hit_ratio == pytest.approx(9000 / 10010, abs=1e-4)
    ev = next(e for e in evs if e.kind == "usage")
    assert ev.data["cache_write_1h_tokens"] == 600 and ev.data["saved_usd"] == pytest.approx(got.saved_usd)
    # the result line's totals (Claude Code prices 1h writes at 2x too: $0.0069 for this usage) replace the sum
    lines.append({"type": "result", "subtype": "success", "result": "hi", "total_cost_usd": 0.00874, "usage": u,
                  "modelUsage": {"claude-opus-5-5": {}}})
    _, total = parse_log("\n".join(map(json.dumps, lines)).encode(), "claude-code")
    assert total.cost_source == "harness" and total.cache_write_1h_tokens == 600
    assert total.saved_usd == pytest.approx(got.saved_usd)


def test_codex_and_pi_savings_and_sums():
    codex = {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20,
                                                   "cache_write_input_tokens": 70, "output_tokens": 10}}
    _, u = parse_log(json.dumps(codex).encode() + b"\n", "codex", model="gpt-6.1-sol")
    # gpt-6.1-sol: $2 in, $0.10 read, $2.50 write (1.25x), $10 out
    assert (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens) == (10, 20, 70)
    assert u.saved_usd == pytest.approx((20 * 1.9 - 70 * 0.5) / 1e6) and u.cache_write_1h_tokens == 0
    pi = {"type": "message_end", "message": {"role": "assistant", "model": "claude-sonnet-5-5", "content": [],
                                            "usage": {"input": 5, "output": 2, "cacheRead": 1000, "cacheWrite": 300,
                                                      "cacheWrite1h": 300, "cost": {"total": 0.0012}}}}
    _, p = parse_log(json.dumps(pi).encode() + b"\n", "pi")
    assert p.cache_write_1h_tokens == 300 and p.cost_source == "harness"
    assert p.saved_usd == pytest.approx((1000 * 1.9 - 300 * 2.0) / 1e6)  # sonnet 5.5: $2 in, $0.10 read, $4 1h
    both = add_usage(u, p)
    assert both.saved_usd == pytest.approx(u.saved_usd + p.saved_usd) and both.cache_write_1h_tokens == 300
    # a model with no price: what caching saved is unknown, and stays unknown in any sum that includes it
    _, unknown = parse_log(json.dumps(codex).encode() + b"\n", "codex", model="no-such-model")
    assert unknown.saved_usd is None and add_usage(u, unknown).saved_usd is None
    assert add_usage(u, Usage(input_tokens=5)).saved_usd == pytest.approx(u.saved_usd)  # no cache tokens: no change


def test_usage_report_by_session_and_ttl():
    def turn(ttl, **u):
        return SessionTurn(message="m", run_id="run-00000001", started_at=time.time(), cache_ttl=ttl, usage=Usage(
            cost_usd=0.01, **u))

    a = Session(name="a", environment="e", harness="claude-code", turns=[
        turn("1h", input_tokens=10, cache_read_tokens=900, cache_write_tokens=90, cache_write_1h_tokens=90,
             saved_usd=0.5),
        turn("1h", input_tokens=10, cache_read_tokens=990, saved_usd=0.25)])
    b = Session(name="b", environment="e", harness="codex", turns=[turn(None, input_tokens=100, saved_usd=0.0)])
    sessions = SimpleNamespace(_sessions={a.id: a, b.id: b})
    r = usage.aggregate(sessions, None, "session")
    rows = {x["key"]: x for x in r["rows"]}
    assert rows["a"]["cache_hit_ratio"] == pytest.approx(1890 / 2000) and rows["a"]["saved_usd"] == 0.75
    assert rows["a"]["cache_write_1h_tokens"] == 90 and rows["b"]["cache_hit_ratio"] == 0.0
    assert r["total"]["saved_usd"] == 0.75 and r["total"]["cache_hit_ratio"] == pytest.approx(1890 / 2100)
    by_ttl = {x["key"]: x["turns"] for x in usage.aggregate(sessions, None, "cache_ttl")["rows"]}
    assert by_ttl == {"1h": 2, "(not set)": 1}


def test_metrics_count_uncached_cost_and_1h_writes(monkeypatch):
    monkeypatch.setattr(metrics, "M", metrics.Metrics())
    u = Usage(input_tokens=10, cache_read_tokens=900, cache_write_tokens=90, cache_write_1h_tokens=90, cost_usd=0.01,
              saved_usd=0.02, model="claude-opus-5-5")
    metrics.turn_finished(harness="claude-code", environment="e", model=None, requester="r", outcome="exited",
                          seconds=1.0, usage=u, cache_ttl="1h")
    get = metrics.M.registry.get_sample_value
    assert get("hangar_cost_uncached_usd_total", {"model": "claude-opus-5-5", "harness": "claude-code",
                                                  "environment": "e"}) == pytest.approx(0.03)
    assert get("hangar_cache_write_1h_tokens_total", {"model": "claude-opus-5-5", "harness": "claude-code"}) == 90
    assert get("hangar_cache_ttl_turns_total", {"ttl": "1h", "harness": "claude-code"}) == 1


def test_rest_and_cli(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from hangar import cli
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = "Bearer t0ken"
        r = c.get("/api/usage", params={"group_by": "session"})
        assert r.status_code == 200 and r.json()["total"]["cache_hit_ratio"] is None
        assert c.get("/api/usage", params={"group_by": "nope"}).status_code == 400
        assert c.post("/api/sessions", json={"name": "x", "environment": "plain", "cache": "2h"}).status_code == 422

    def fake_call(method, path, **kw):
        assert (method, path, kw["params"]["group_by"]) == ("GET", "/api/usage", "session")
        row = {"key": "csv-fix", "turns": 3, "tokens": 12000, "cache_read_tokens": 1_200_000,
               "cache_write_tokens": 90000, "cache_write_1h_tokens": 90000, "cost_usd": 1.25, "unpriced_turns": 0,
               "cache_hit_ratio": 0.9234, "saved_usd": 4.5}
        return {"rows": [row], "total": {**row, "key": "total"}}

    monkeypatch.setattr(cli, "_call", fake_call)
    out = CliRunner().invoke(cli.app, ["usage", "--by", "session", "--cache"]).output
    assert "HIT" in out and "csv-fix" in out and "92%" in out and "1.2M" in out and "$4.50" in out, out
    plain = CliRunner().invoke(cli.app, ["usage", "--by", "session"]).output
    assert "HIT" not in plain and "$1.25" in plain


# ====================================================================== real turns on the fake provider
pytestmark_tmux = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

# Records when it started and the env it got, waits $DELAY_FILE's seconds, then answers like Claude Code's
# stream-json: one assistant message with a cache_creation split, and a result.
AGENT = r"""#!/usr/bin/env bash
date +%s.%N > "$HOME/start.$HANGAR_RUN_ID"
env | grep -E '^(CLAUDE_CODE_PROMPT_CACHE_TTL|DISABLE_PROMPT_CACHING|HANGAR_MODEL)=' | sort > "$HOME/env.$HANGAR_RUN_ID"
sleep "$(cat "$DELAY_FILE" 2>/dev/null || echo 0)"
u='{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":9000,"cache_creation_input_tokens":1000,'
u+='"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":1000}}'
m="{\"id\":\"m-$HANGAR_RUN_ID\",\"model\":\"claude-opus-5-5\",\"content\":[{\"type\":\"text\",\"text\":\"hi\"}]"
echo "{\"type\":\"assistant\",\"message\":$m,\"usage\":$u}}"
echo "{\"type\":\"result\",\"subtype\":\"success\",\"result\":\"done\",\"usage\":$u}"
"""


@pytest.fixture
def world(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="cache-", dir=scratch_root()))
    remote, seed = root / "remote.git", root / "seed"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    subprocess.run([*git, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    (root / "agent.sh").write_text(AGENT)
    delay = root / "delay"
    monkeypatch.setitem(HARNESSES, "cachey", Harness(
        name="cachey", title="Cache probe", run=f"DELAY_FILE={delay} bash {root / 'agent.sh'}",
        cache_env=HARNESSES["claude-code"].cache_env, session_dirs=[".fake"], resume="preset", fork=True))
    envs = root / "envs"
    envs.mkdir()
    (envs / "cacheenv.yaml").write_text("name: cacheenv\nharnesses: [cachey]\nmodel: {cachey: claude-opus-5-5}\n")
    yield {"root": root, "url": f"file://{remote}", "envs": envs, "delay": delay}
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def svc(fleet, world, settings, store):
    settings.session_poll_seconds = 0.1
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    s = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    await s.start()
    yield s
    await s.stop()
    for b in fleet.providers["docker"].root.iterdir():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{b}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


async def settle(svc: SessionService, ref: str, timeout: float = 30.0) -> Session:
    for _ in range(int(timeout / 0.05)):
        s = svc.get(ref)
        if s.state.value in ("idle", "suspended", "failed") and not svc.busy(s.id):
            return s
        await asyncio.sleep(0.05)
    raise AssertionError(f"{ref} stuck in {svc.get(ref).state}: {svc.get(ref).reason}")


def _home(fleet, s: Session) -> Path:
    return fleet.providers["docker"].boxes[s.sandbox_id] / "home/agent"


def _env(fleet, s: Session, turn: SessionTurn) -> dict[str, str]:
    text = (_home(fleet, s) / f"env.{turn.run_id}").read_text()
    return dict(ln.split("=", 1) for ln in text.splitlines())


@pytestmark_tmux
async def test_turns_get_the_ttl_env_and_one_model(svc, world, fleet):
    s = svc.new(NewSession(name="long", environment="cacheenv", repo=world["url"], message="start"),
                owner="admin", requester="sean")
    assert s.model == "claude-opus-5-5"  # pinned at creation, before any turn
    s = await settle(svc, "long")
    t = s.turns[0]
    assert t.exit_code == 0 and t.cache_ttl == "1h"  # auto: it idles 30 min between messages
    assert _env(fleet, s, t) == {**TTL_1H, "HANGAR_MODEL": "claude-opus-5-5"}
    assert t.usage.cache_write_1h_tokens == 1000 and t.usage.cost_usd == pytest.approx(
        (40 + 100 + 1800 + 8000) / 1e6) and t.usage.saved_usd == pytest.approx((9000 * 3.8 - 1000 * 4) / 1e6)
    view = svc.view(s)
    assert view["usage"]["cache_hit_ratio"] == pytest.approx(9000 / 10010, abs=1e-4) and view["usage"]["saved_usd"]

    # another model mid-session: allowed, warned, and the next turn runs it
    svc.send("long", "go on", requester="sean", model="claude-sonnet-5-5")
    s = await settle(svc, "long")
    t2 = s.turns[1]
    assert s.model == "claude-sonnet-5-5" and _env(fleet, s, t2)["HANGAR_MODEL"] == "claude-sonnet-5-5"
    assert any("uncached" in w for w in t2.warnings)
    assert any(e["data"].get("warning") == "model_switch" for e in svc.events("long"))
    svc.send("long", "and again", requester="sean", model="claude-sonnet-5-5")  # the same model: no warning
    s = await settle(svc, "long")
    assert not s.turns[2].warnings
    with pytest.raises(SessionError):
        svc.send("long", "x", requester="sean", model="bad\nmodel")
    assert len(svc.get("long").turns) == 3 and svc.get("long").model == "claude-sonnet-5-5"

    s = svc.new(NewSession(name="quick", environment="cacheenv", repo=world["url"], message="start", idle_minutes=3),
                owner="admin", requester="sean")
    s = await settle(svc, "quick")
    assert s.turns[0].cache_ttl == "5m" and _env(fleet, s, s.turns[0])["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "5m"
    s = svc.new(NewSession(name="nocache", environment="cacheenv", repo=world["url"], message="start", cache="off"),
                owner="admin", requester="sean")
    s = await settle(svc, "nocache")
    env = _env(fleet, s, s.turns[0])
    assert s.turns[0].cache_ttl == "off" and env["DISABLE_PROMPT_CACHING"] == "1"
    assert "CLAUDE_CODE_PROMPT_CACHE_TTL" not in env


@pytestmark_tmux
@pytest.mark.parametrize("policy", ["auto", "off"])
async def test_forks_with_a_message_wait_for_the_first_to_answer(svc, world, fleet, settings, policy):
    from hangar.strategies import fork_session

    settings.cache_stagger_seconds = 20
    svc.new(NewSession(name="p", environment="cacheenv", repo=world["url"], message="start", cache=policy),
            owner="admin", requester="sean")
    await settle(svc, "p")
    world["delay"].write_text("1.5")  # each child takes 1.5 s to its first model response
    kids = await fork_session(svc, "p", 2, "sean", message="try it")
    kids = [await settle(svc, k.name) for k in kids]
    turns = [k.turns[-1] for k in kids]
    assert all(t.exit_code == 0 for t in turns)
    starts = [float((_home(fleet, k) / f"start.{t.run_id}").read_text()) for k, t in zip(kids, turns, strict=True)]
    waited = [t.cache_wait_seconds for t in turns]
    if policy == "off":
        assert waited == [None, None]  # nothing cached, nothing to wait for
        return
    lead = waited.index(None)
    other = 1 - lead
    assert waited[other] >= 1.0, waited
    # the second started after the first had answered (1.5 s), well before the 20 s cap
    assert 1.3 < starts[other] - starts[lead] < 10, starts


# ====================================================================== the real CLIs, offline
CLAUDE, PI = os.environ.get("HANGAR_TEST_CLAUDE"), os.environ.get("HANGAR_TEST_PI")
USAGE = {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 1000,
         "cache_creation": {"ephemeral_5m_input_tokens": 400, "ephemeral_1h_input_tokens": 600}}


def _home_dir(tmp: Path) -> Path:
    home = tmp / f"h-{uuid.uuid4().hex[:6]}"
    (home / "work").mkdir(parents=True)
    return home


@pytest.mark.skipif(not CLAUDE, reason="set HANGAR_TEST_CLAUDE to the real claude CLI")
@pytest.mark.parametrize("ttl", [None, "5m", "1h", "off"])
def test_real_claude_sends_the_ttl_hangar_asks_for(tmp_path, ttl):
    """The env hangar sets for each TTL (Harness.cache_env), and the cache_control the real CLI then sends."""
    srv, port = fake_models.serve(usage=USAGE)
    try:
        home = _home_dir(tmp_path)
        (home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "CLAUDE"))}
        env.update(HOME=str(home), ANTHROPIC_API_KEY="sk-ant-fake", ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1",
                   **HARNESSES["claude-code"].cache_env.get(ttl or "", {}))
        r = subprocess.run([CLAUDE, "-p", "hello", "--output-format", "stream-json", "--verbose", "--model",
                            "claude-opus-5-5"], env=env, cwd=home / "work", capture_output=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        assert r.returncode == 0, r.stderr.decode()[-2000:]
        seen = [x for x in srv.RequestHandlerClass.seen if x["tools"]]  # the main conversation's requests
        assert seen
        ttls = {t for x in seen for t in x["ttls"]}
        if ttl == "off":
            assert ttls == set()
        else:
            assert ttls == {ttl or "5m"}, seen  # an API key gets 5 minutes unless asked
            assert ("extended-cache-ttl-" in seen[0]["beta"]) == (ttl == "1h")
        _, u = parse_log(r.stdout, "claude-code")
        assert u.cache_write_1h_tokens == 600 and u.cost_source == "harness"
        # Claude Code's own price for this usage agrees with hangar's table (1h writes at 2x input)
        assert u.cost_usd == pytest.approx(cost_of(u, DEFAULT_PRICES["claude-opus-5-5"]))
        assert u.saved_usd == pytest.approx(saved_of(u, DEFAULT_PRICES["claude-opus-5-5"]))
    finally:
        srv.shutdown()


@pytest.mark.skipif(not PI, reason="set HANGAR_TEST_PI to the real pi CLI")
@pytest.mark.parametrize("ttl", ["5m", "1h"])
def test_real_pi_sends_the_ttl_hangar_asks_for(tmp_path, ttl):
    srv, port = fake_models.serve(usage=USAGE)
    try:
        home = _home_dir(tmp_path)
        (home / ".pi/agent").mkdir(parents=True)
        (home / ".pi/agent/models.json").write_text(json.dumps({"providers": {"fake": {
            "baseUrl": f"http://127.0.0.1:{port}", "api": "anthropic-messages", "apiKey": "sk-ant-fake",
            "models": [{"id": "claude-sonnet-5-5"}]}}}))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "PI_"))}
        env.update(HOME=str(home), PI_SKIP_VERSION_CHECK="1", PI_TELEMETRY="0", **HARNESSES["pi"].cache_env[ttl])
        r = subprocess.run([PI, "-p", "--mode", "json", "--model", "fake/claude-sonnet-5-5", "hello"], env=env,
                           cwd=home / "work", capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        assert r.returncode == 0, r.stderr.decode()[-2000:]
        seen = srv.RequestHandlerClass.seen
        assert seen and {t for x in seen for t in x["ttls"]} == {ttl}, seen
        _, u = parse_log(r.stdout, "pi")
        assert u.cache_write_1h_tokens == 600
    finally:
        srv.shutdown()
