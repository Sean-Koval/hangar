"""#181: the eval queue class. Eval jobs (experiment trials) start only inside the eval window, never ahead of an
interactive job, at most half the job slots at once, and under a per-provider token budget; the power-down drain
cancels running trials as `infra_error/power_down` and they run again in the next window, not at power-up."""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import types
from zoneinfo import ZoneInfo

import pytest

from hangar import evalqueue, experiments
from hangar.jobs import Caller, JobService, SubmitJob
from hangar.metrics import Metrics
from hangar.models import Job, JobState, Turn, Usage
from tests.test_interfaces import jobs, world  # noqa: F401 - fixtures
from tests.test_jobs import _until
from tests.test_landing import gh, svc  # noqa: F401 - fixtures
from tests.test_trial_retries import _first_turn, _spec, slow_first  # noqa: F401 - fixtures

NY = ZoneInfo("America/New_York")


def at(hour: int, minute: int = 0, day: int = 14) -> float:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=NY).timestamp()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": at(14)}
    monkeypatch.setattr(evalqueue, "clock", lambda: now["t"])
    return now


@pytest.fixture
def q(svc, settings, clock):  # noqa: F811
    settings.eval_window, settings.eval_tz = "20:00-00:15", "America/New_York"
    return svc


def queued(svc: JobService, cls: str = "interactive", harness: str = "claude-code", **kw) -> Job:  # noqa: F811
    job = Job(title=f"{cls} work", instructions="x", environment="jobenv", repo="https://github.com/o/r.git",
              requester="alice", person="alice", harness=harness, turns=[Turn()], queue_class=cls, **kw)
    svc._jobs[job.id] = job
    svc._save(job)
    svc._queue.put_nowait(job.id)
    return job


async def take(svc: JobService, timeout: float = 0.2) -> str | None:  # noqa: F811
    try:
        return await asyncio.wait_for(svc._queue.get(), timeout)
    except TimeoutError:
        return None


# ====================================================================== the window
def test_window_crosses_midnight_and_says_when_it_next_opens():
    w = evalqueue.Window("20:00-00:15", "America/New_York")
    assert not w.open(at(14)) and not w.open(at(19, 59))
    assert w.open(at(20)) and w.open(at(23, 59)) and w.open(at(0, 14, day=15))
    assert not w.open(at(0, 15, day=15)) and not w.open(at(1, day=15))
    assert w.next_open(at(14)) == at(20)
    assert w.next_open(at(1, day=15)) == at(20, day=15)
    assert w.next_open(at(21)) == at(21)
    same_day = evalqueue.Window("09:00-17:00", "UTC")
    assert same_day.open(dt.datetime(2026, 1, 1, 12, tzinfo=dt.UTC).timestamp())
    assert not same_day.open(dt.datetime(2026, 1, 1, 17, tzinfo=dt.UTC).timestamp())
    for spec in ("always", "", None, "20:00-20:00"):
        assert evalqueue.Window(spec).always and evalqueue.Window(spec).open(at(3))
    for bad in ("8pm-midnight", "25:00-01:00", "20:00"):
        with pytest.raises(ValueError):
            evalqueue.Window(bad)


def test_window_follows_daylight_saving():
    w = evalqueue.Window("20:00-00:15", "America/New_York")
    # 2026-11-01 is the end of daylight saving time in New York: 20:00 local is 01:00 UTC the next day
    before = dt.datetime(2026, 11, 1, 12, tzinfo=NY).timestamp()
    assert dt.datetime.fromtimestamp(w.next_open(before), dt.UTC).hour == 1


def test_budgets_parse_per_provider_or_for_all(settings):
    settings.eval_tpm_budget = "anthropic=400000, openai=300000"
    assert evalqueue.budgets(settings) == {"anthropic": 400000, "openai": 300000}
    settings.eval_tpm_budget = "250000"
    assert evalqueue.budgets(settings) == {"*": 250000}
    settings.eval_tpm_budget = None
    assert evalqueue.budgets(settings) == {}
    settings.eval_tpm_budget = "anthropic=lots"
    with pytest.raises(ValueError):
        evalqueue.budgets(settings)


# ====================================================================== the queue
async def test_a_trial_submitted_at_14_waits_until_20_and_people_go_first(q, clock):
    trial = queued(q, "eval")
    assert await take(q) is None  # 14:00: outside the window
    assert trial.state == JobState.QUEUED and trial.reason == evalqueue.WAIT_WINDOW
    assert any("eval window (20:00-00:15 America/New_York); next at 20:00" in e.message for e in trial.events)
    mine = queued(q)  # an interactive job starts at once, whatever waits
    assert await take(q) == mine.id
    q._queue.task_done()
    clock["t"] = at(20)
    late = [queued(q, "eval") for _ in range(2)]
    clock["t"] = at(20, 5)
    person = queued(q)  # submitted after the trials, starts before them
    assert await take(q) == person.id
    assert await take(q) == trial.id  # then the trials, oldest first
    q.evalq.admitted(trial.id)
    assert trial.reason is None and trial.events[-1].message == "eval queue: starting"
    assert await take(q) == late[0].id


async def test_the_window_opens_without_an_event(q, clock):
    q._queue._recheck = 0.05  # the queue looks again on its own (RECHECK_SECONDS in service)
    trial = queued(q, "eval")
    waiter = asyncio.ensure_future(q._queue.get())
    await asyncio.sleep(0.1)
    assert not waiter.done()
    clock["t"] = at(20, 1)
    assert await asyncio.wait_for(waiter, 1) == trial.id


async def test_interactive_jobs_never_wait_behind_eval_slots(q, clock, settings):
    clock["t"] = at(21)
    settings.job_concurrency = 4  # half of them for evals
    trials = [queued(q, "eval") for _ in range(3)]
    for t in trials[:2]:
        assert await take(q) == t.id
        q._active.add(t.id)
    assert evalqueue.slots(settings) == 2
    assert await take(q) is None and trials[2].reason == evalqueue.WAIT_SLOTS
    person = queued(q)
    assert await take(q) == person.id
    q._active.discard(trials[0].id)
    q._queue.wake()
    assert await take(q) == trials[2].id


async def test_an_admin_lifts_the_window_for_one_job(q, clock):
    trial, other = queued(q, "eval"), queued(q, "eval")
    assert await take(q) is None
    q.evalq.prioritize(trial.id, "admin")
    assert trial.priority == "now" and trial.reason is None
    assert await take(q) == trial.id
    assert await take(q) is None and other.reason == evalqueue.WAIT_WINDOW
    assert any("priority: now (by admin)" in e.message for e in trial.events)


async def test_admission_stops_at_the_token_budget_and_resumes_as_the_rate_drops(q, clock, settings):
    clock["t"] = at(21)
    settings.eval_tpm_budget, settings.eval_tpm_per_trial = "anthropic=100000", 20000
    m = evalqueue.meter(q.fleet)
    m.note("anthropic", 90_000, clock["t"] - 30, clock["t"] - 30)  # an exact count, from the gateway (#177)
    m.note("openai", 500_000, clock["t"] - 10, clock["t"] - 10)  # another provider's traffic doesn't count
    trial = queued(q, "eval")
    assert await take(q) is None and trial.reason == evalqueue.WAIT_TPM
    assert "anthropic is at 90,000 tokens/min; one more trial (~20,000)" in trial.events[-1].message
    codex = queued(q, "eval", harness="codex")  # openai has no budget set: it starts
    assert await take(q) == codex.id
    clock["t"] += 31  # the count leaves the rolling minute
    q._queue.wake()
    assert await take(q) == trial.id
    st = q.evalq.status()
    assert st["tpm"]["anthropic"] == {"observed": 0, "per_trial": 20000, "budget": 100000}


def test_the_observed_rate_spreads_turns_over_their_run_and_learns_a_trials_rate(clock):
    m = evalqueue.Meter()
    t = clock["t"]
    m.note("anthropic", 120_000, t - 120, t)  # a two-minute turn that just ended: half of it in the last minute
    assert m.rate("anthropic", t) == pytest.approx(60_000)
    assert m.rate("anthropic", t, live=[(30_000, t - 30)]) == pytest.approx(90_000)  # plus a live turn's so far
    assert m.p50("anthropic") is None
    for tokens in (10_000, 30_000, 50_000):
        m.note_trial("anthropic", tokens, 60)
    assert m.p50("anthropic") == 30_000


def test_turn_ends_feed_the_meter_and_eval_turns_the_estimate(svc, clock):  # noqa: F811
    t = clock["t"]
    turn = types.SimpleNamespace(usage=Usage(input_tokens=40_000, output_tokens=20_000), started_at=t - 60,
                                 finished_at=t, harness="claude-code")
    s = types.SimpleNamespace(harness="claude-code")
    evalqueue.turn_ended(svc.fleet, s, turn, Job(title="t", instructions="x", environment="e", queue_class="eval"))
    m = evalqueue.meter(svc.fleet)
    assert m.rate("anthropic", t) == pytest.approx(60_000) and m.p50("anthropic") == pytest.approx(60_000)
    evalqueue.turn_ended(svc.fleet, s, turn, None)  # an interactive turn: the rate, not the estimate
    assert m.rate("anthropic", t) == pytest.approx(120_000) and m.p50("anthropic") == pytest.approx(60_000)
    evalqueue.turn_ended(svc.fleet, s, types.SimpleNamespace(usage=None), None)  # never raises


async def test_metrics_show_the_window_rates_and_held_jobs(q, clock, settings):
    settings.eval_tpm_budget = "anthropic=100000"
    queued(q, "eval")
    assert await take(q) is None
    m = Metrics()
    m.collect_state(types.SimpleNamespace(store=q.store, jobs=q))
    assert m.eval_window._value.get() == 0
    assert m.eval_waiting.labels("eval_window")._value.get() == 1
    assert m.eval_budget.labels("anthropic")._value.get() == 100000


def test_submit_takes_the_class_and_trials_are_eval():
    assert SubmitJob(instructions="x", queue_class="eval").queue_class == "eval"
    assert Job(title="t", instructions="x", environment="e").queue_class == "interactive"


# ====================================================================== the power-down
pytestmark_tmux = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


@pytestmark_tmux
async def test_a_drain_at_01_cancels_running_trials_and_they_rerun_in_the_next_window(
        jobs, world, fleet, slow_first, settings, clock):  # noqa: F811
    settings.eval_window, settings.eval_tz = "20:00-00:15", "America/New_York"
    jobs._queue._recheck = 0.05
    clock["t"] = at(23)
    exp = experiments.submit(jobs, _spec(world), Caller("svc", "svc"))
    child = await _first_turn(jobs, exp.id)
    assert child.queue_class == "eval"
    clock["t"] = at(1, day=15)  # power.sh down
    jobs.drain.begin(60)
    assert jobs.drain.status(types.SimpleNamespace(fleet=fleet, sessions=jobs.sessions))["cancelled_trials"] == \
        [child.id]
    child = await _until(jobs, child.id, {JobState.CANCELLED}, timeout=60)
    assert (child.result.outcome_class, child.result.infra_reason) == ("infra_error", "power_down")
    # the experiment queued a fresh attempt; it waits for tonight's window, through the drain and the power-up
    for _ in range(200):
        st = experiments.load(jobs.get(exp.id))
        if len(st.trials) == 2 and st.trials[1].job:
            break
        await asyncio.sleep(0.05)
    first, retry = st.trials
    assert (first.outcome_class, first.infra_reason, first.excluded) == ("infra_error", "power_down", True)
    jobs.drain.end()
    clock["t"] = at(10, day=15)  # power.sh up
    jobs._queue.wake()
    await asyncio.sleep(0.3)
    held = jobs.get(retry.job)
    assert held.state == JobState.QUEUED and held.reason == evalqueue.WAIT_WINDOW
    clock["t"] = at(20, 1, day=15)
    exp = await _until(jobs, exp.id, {JobState.SUCCEEDED, JobState.FAILED}, timeout=120)
    v = experiments.view(jobs, exp)
    assert [t["attempt"] for t in v["trials"]] == [1, 2]
    assert v["trials"][1]["outcome"] == "succeeded" and not v["trials"][1]["excluded"]
