"""--- trust (#95): best-of-N attempts the nightly power-down cuts off resume from their checkpoints at power-up,
through the restart path, instead of each failing on its own. Failed on build/ops: the job failed `sandbox_gone`."""

from __future__ import annotations

import asyncio

from hangar import power
from hangar.jobs import Caller
from hangar.models import JobState
from tests.test_jobs import DONE, _until
from tests.test_power import off
from tests.test_strategies import _idle, bon, jobs, world  # noqa: F401 - fixtures


async def test_attempts_the_power_down_cuts_off_resume_at_power_up(jobs, world, fleet, settings):  # noqa: F811
    settings.power_providers = ["docker"]
    settings.job_restart_stagger_seconds = 0
    world["mode"].write_text("sleep")
    job = jobs.submit(bon(world), Caller("svc", "svc"))
    while not (job.attempts and all(a.run_id for a in job.attempts)):
        await asyncio.sleep(0.05)
    ses = jobs.sessions
    attempts = [ses.get(a.session_id) for a in job.attempts]
    for s in attempts:  # the drain's checkpoints
        assert await ses.checkpoint_now(s.id, requester="power.sh") is not None
    off(fleet.power)  # 01:00: power.sh tells hangar, then the pool and its pods go
    for s in attempts:
        sb = fleet.get(s.sandbox_id)
        await fleet.providers["docker"].destroy(sb)
        fleet._mark_gone(sb, "disappeared from provider")
    for _ in range(300):
        if jobs.get(job.id).reason == power.WAITING:
            break
        await asyncio.sleep(0.05)
    j = jobs.get(job.id)
    msgs = [e.message for e in j.events]
    assert j.state == JobState.QUEUED and j.reason == power.WAITING, msgs  # on build/ops: failed, sandbox_gone
    assert all(a.state != "done" and a.outcome is None and a.restarts == 1 for a in j.attempts), msgs
    cut = [ses.get(a.session_id).turns[-2] for a in j.attempts]
    assert all(t.outcome == "gone" and t.outcome_reason == "the sandbox pools powered down" for t in cut)
    world["mode"].write_text("ok")
    fleet.power.mark("up", "power.sh")
    fleet.power.release(jobs)
    done = await _until(jobs, job.id, DONE, timeout=60)
    await _idle(jobs, done)
    msgs = [e.message for e in done.events]
    assert done.state == JobState.SUCCEEDED and done.result.pushed, msgs
    assert sorted(a.rank for a in done.attempts) == [1, 2, 3] and done.attempts[2].rank == 1, msgs
    box = fleet.providers["docker"].boxes[done.sandbox_id]  # the winner's
    prompts = sorted((box / "home/agent/.hangar/runs").glob("*.prompt"), key=lambda p: p.stat().st_mtime)
    assert "powered down for the night" in prompts[-1].read_text()
