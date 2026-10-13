"""Workstream C end to end on the fake provider: live collection from a running turn, restart safety, budgets.

Uses the jobs test world (a local git remote and the `jobenv` environment) with scripted agents that print the
harnesses' real stream formats.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from pathlib import Path

import httpx
import pytest

from hangar.events import parse_log
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, JobService, SubmitJob
from hangar.models import Budget, JobState
from hangar.sessions import NewSession, SessionService
from tests import test_jobs
from tests.test_jobs import DONE, Hooks, _gh_settings, _until

# the jobs tests' world: a local git remote, the `jobenv` environment and a started JobService
world, sessions, jobs = test_jobs.world, test_jobs.sessions, test_jobs.jobs

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

STREAMS = Path(__file__).parent / "fixtures" / "streams"


def claude_step(i: int, tokens: int = 100_000) -> str:
    """A Claude Code stream-json assistant line: $0.315 at Sonnet 4.5 list prices for 100k in + 1k out."""
    return json.dumps({"type": "assistant", "message": {
        "id": f"msg_{i}", "model": "claude-sonnet-4-5", "role": "assistant",
        "content": [{"type": "text", "text": f"step {i}"}], "usage": {"input_tokens": tokens, "output_tokens": 1000}}})


def agent(root: Path, monkeypatch, body: str, name: str = "scripted") -> None:
    script = root / f"{name}-agent.sh"
    script.write_text("#!/usr/bin/env bash\n" + body)
    monkeypatch.setitem(HARNESSES, name, Harness(name=name, title=name, run=f"bash {script}"))


async def _settle(svc: SessionService, ref: str, timeout: float = 20.0):
    for _ in range(int(timeout / 0.05)):
        s = svc.get(ref)
        if s.state.value in ("idle", "suspended", "failed") and not svc.busy(s.id):
            return s
        await asyncio.sleep(0.05)
    raise AssertionError(f"{ref} stuck in {svc.get(ref).state}: {svc.get(ref).reason}")


async def _wait_for(pred, timeout: float = 15.0):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting")


def _home(fleet, s) -> Path:
    return fleet.providers["docker"].boxes[s.sandbox_id] / "home/agent"


async def test_job_over_cost_budget_is_stopped_and_delivered_as_draft(jobs, world, broker, monkeypatch):
    """Acceptance (0011 C): a job with max_cost_usd is stopped when the reported cost crosses it, and says why."""
    steps = "\n".join(f"echo '{claude_step(i)}'; sleep 0.2" for i in range(1, 41))
    agent(world["root"], monkeypatch, f"""
echo '{{"type":"system","subtype":"init","model":"claude-sonnet-4-5","session_id":"s-1"}}'
echo partial > partial.txt && git add partial.txt && git commit -qm "partial work"
{steps}
touch "$HOME/finished"
echo '{{"type":"result","subtype":"success","is_error":false,"result":"all done","total_cost_usd":12.6}}'
""")
    _gh_settings(jobs, broker)
    drafts: list[bool] = []

    async def ensure_pr(job, draft):
        drafts.append(draft)

    monkeypatch.setattr(jobs, "_ensure_pr", ensure_pr)
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv", deliver="pr",
                                budget=Budget(max_cost_usd=1.0)),
                      Caller("github:sean", "github:sean"), source="github", source_ref="github:o/r#12")
    assert job.budget == Budget(max_cost_usd=1.0)
    job = await _until(jobs, job.id, DONE, timeout=30)
    assert job.state == JobState.FAILED and job.reason == "over_budget", [e.message for e in job.events]
    turn = job.turns[0]
    assert turn.outcome == "over_budget" and "the budget is $1.00" in turn.outcome_reason
    assert 1.0 < turn.usage.cost_usd < 3.0 and turn.usage.cost_source == "prices"  # stopped soon after crossing
    s = jobs.sessions.get(job.session_id)
    assert not (_home(jobs.fleet, s) / "finished").exists()  # the agent was killed, not left spending
    # what it had done is delivered, as a draft
    assert job.result.pushed and any("partial work" in c for c in job.result.commits) and drafts == [True]
    assert any("stopped over budget" in e.message for e in job.events)
    comment = jobs.hooks.comments()[-1]
    assert "(over_budget)" in comment and "Stopped over budget (spent $" in comment and "Usage:" in comment
    # usage and the event stream are visible through the job
    v = jobs.view(job)
    assert v["usage"]["cost_usd"] == pytest.approx(turn.usage.cost_usd) and v["budget"]["max_cost_usd"] == 1.0
    evs = jobs.events(job.id)
    assert [e["text"] for e in evs if e["kind"] == "message"][:2] == ["step 1", "step 2"]
    assert any(e["kind"] == "error" and "over budget" in e["text"] for e in evs)
    assert [e["seq"] for e in evs] == sorted(e["seq"] for e in evs) and all(e["turn"] == 1 for e in evs)
    assert jobs.events(job.id, since=evs[-2]["seq"]) == evs[-1:]
    assert jobs.events(job.id, tail=1) == evs[-1:]
    with pytest.raises(KeyError):
        jobs.events(job.id, owner="someone-else")


async def test_token_budget_caps_restricted_callers(fleet, world, settings):
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    svc = JobService(fleet, None, transport=httpx.MockTransport(Hooks()))  # not started: intake only
    settings.github_repos = {world["url"]: "jobenv"}
    settings.budget_max_minutes = 45
    cap = Budget(max_cost_usd=0.5)
    job = svc.submit(SubmitJob(instructions="x", repo=world["url"], budget=Budget(max_cost_usd=5.0)),
                     Caller("svc", "tok-1", restricted=True, max_budget=cap))
    assert job.budget == Budget(max_cost_usd=0.5, max_minutes=45)
    admin = svc.submit(SubmitJob(instructions="x", repo=world["url"], budget=Budget(max_cost_usd=5.0)),
                       Caller("admin", "admin", max_budget=cap))  # only jobs-scoped tokens are capped
    assert admin.budget == Budget(max_cost_usd=5.0, max_minutes=45)


async def test_events_resume_after_a_restart(jobs, world, fleet, monkeypatch):
    """The control plane restarts mid-turn, after recording events it hadn't saved its cursor for: the new one
    continues from the stored offset with nothing missed, nothing recorded twice and usage counted once."""
    line = claude_step
    agent(world["root"], monkeypatch, f"""
echo '{line(1)}'; echo '{line(2)}'; echo '{line(3)}'
while [ ! -f "$HOME/go1" ]; do sleep 0.05; done
echo '{line(4)}'; echo '{line(5)}'
while [ ! -f "$HOME/go2" ]; do sleep 0.05; done
printf '%s' '{line(6)}'   # the last line has no newline: only the end-of-turn parse sees it
""")
    svc = jobs.sessions
    svc.new(NewSession(name="restart", environment="jobenv", repo=world["url"], message="go"), owner="admin",
            requester="sean")

    def messages(service: SessionService) -> list[str]:
        return [e["text"] for e in service.events("restart") if e["kind"] == "message"]

    await _wait_for(lambda: messages(svc) == ["step 1", "step 2", "step 3"])
    s = svc.get("restart")
    turn = s.turns[-1]
    assert turn.usage.cost_usd == pytest.approx(3 * 0.315) and s.state.value == "running"
    stale = copy.deepcopy(turn.event_cursor)
    assert stale and stale["offset"] > 0
    (_home(fleet, s) / "go1").touch()
    await _wait_for(lambda: len(messages(svc)) == 5)
    await svc.stop()  # restart: the turn keeps running in the sandbox
    turn.event_cursor = stale  # as if we died after recording 4 and 5 but before saving the cursor past them
    svc._save(s)

    again = SessionService(fleet, svc.vault)
    await again.start()
    try:
        r = again.get("restart")
        await _wait_for(lambda: again.busy(r.id))  # re-attached to the running turn
        (_home(fleet, r) / "go2").touch()
        r = await _settle(again, "restart")
        t = r.turns[-1]
        assert messages(again) == [f"step {i}" for i in range(1, 7)]
        assert t.usage.cost_usd == pytest.approx(6 * 0.315) and t.usage.input_tokens == 600_000
        assert t.outcome == "exited" and t.event_cursor is None
        seqs = [e["seq"] for e in again.events("restart")]
        assert seqs == list(range(1, len(seqs) + 1))
        assert again.view(r)["usage"]["cost_usd"] == pytest.approx(6 * 0.315)
        assert "event_cursor" not in again.view(r)["turns"][0]
    finally:
        await again.stop()


@pytest.mark.parametrize("fixture", ["claude-code", "codex", "pi", "antigravity"])
async def test_live_collection_matches_the_recorded_stream(jobs, world, fleet, monkeypatch, fixture):
    """A real harness stream, written to the run log in small pieces while the turn runs, gives the same events
    as parsing the whole log."""
    src = STREAMS / f"{fixture}.jsonl"
    writer = world["root"] / "trickle.py"
    writer.write_text("import sys, time\ndata = open(sys.argv[1], 'rb').read()\n"
                      "for i in range(0, len(data), 97):\n"
                      "    sys.stdout.buffer.write(data[i:i + 97]); sys.stdout.buffer.flush(); time.sleep(0.002)\n")
    agent(world["root"], monkeypatch, f"python3 {writer} {src}\n")
    svc = jobs.sessions
    svc.new(NewSession(name=f"live-{fixture}", environment="jobenv", repo=world["url"], message="go"),
            owner="admin", requester="sean")
    s = await _settle(svc, f"live-{fixture}")
    want, usage = parse_log(src.read_bytes())
    got = svc.events(s.name)
    assert [(e["kind"], e["text"]) for e in got] == [(e.kind, e.text) for e in want]
    assert s.turns[-1].usage.tokens == usage.tokens and s.turns[-1].usage.cache_read_tokens == usage.cache_read_tokens


async def test_session_max_minutes_budget(jobs, world, fleet, monkeypatch, settings):
    agent(world["root"], monkeypatch, f"echo '{claude_step(1, 10)}'; sleep 30; touch \"$HOME/finished\"\n")
    svc = jobs.sessions
    svc.new(NewSession(name="slow", environment="jobenv", repo=world["url"], message="go",
                       budget=Budget(max_minutes=1)), owner="admin", requester="sean")
    await _wait_for(lambda: svc.get("slow").turns and svc.get("slow").turns[-1].run_id)
    s = svc.get("slow")
    assert s.budget == Budget(max_minutes=1)
    s.turns[-1].started_at -= 61  # a minute has passed
    s = await _settle(svc, "slow")
    t = s.turns[-1]
    assert t.outcome == "over_budget" and "budget is 1 min" in t.outcome_reason
    assert not (_home(fleet, s) / "finished").exists()
    assert "over_budget" in (s.reason or "")


async def test_mcp_budget_and_get_events(jobs, world, fleet, monkeypatch):
    from mcp import Client

    from hangar.mcp_server import build_mcp

    agent(world["root"], monkeypatch, f"echo '{claude_step(1)}'; sleep 30\n")
    async with Client(build_mcp(fleet, jobs, jobs.sessions)) as c:
        res = await c.call_tool("submit_job", {"instructions": "x", "repo": world["url"], "environment": "jobenv",
                                               "deliver": "none", "budget": {"max_tokens": 50_000}})
        jid = json.loads(res.content[0].text)["id"]
        job = await _until(jobs, jid, DONE)
        assert job.reason == "over_budget" and "tokens; the budget is 50,000" in job.turns[0].outcome_reason
        got = json.loads((await c.call_tool("get_events", {"job": jid})).content[0].text)
        assert [e["kind"] for e in got["events"]][-1] == "error" and got["next"] == got["events"][-1]["seq"]
        again = json.loads((await c.call_tool("get_events", {"job": jid, "since": got["next"]})).content[0].text)
        assert again == {"events": [], "next": got["next"]}
        by_session = json.loads((await c.call_tool("get_events", {"session": job.session_id})).content[0].text)
        assert by_session == got
        bad = await c.call_tool("get_events", {})
        assert bad.is_error and "exactly one" in bad.content[0].text
