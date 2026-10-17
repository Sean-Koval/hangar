"""The team's monthly eval budget with per-author sub-caps (#182): eval jobs' spend per calendar month (UTC) and
author; experiments and trials projected over what is left are refused with the numbers; running trials finish but no
new one starts once a budget is spent; the month's rollover resets it; cached trials (#180) cost nothing."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from hangar import evalbudget, experiments, jobbudget, pinsets
from hangar.jobs import Caller, JobError
from hangar.models import Job, JobBudget, JobState, Person
from tests import test_harness_thaw as th
from tests.test_harness_lock import DONE
from tests.test_jobs import _until
from tests.test_pinsets import _pinned, _task

skillenv, world, jobs = th.skillenv, th.world, th.jobs
needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


def at(month: int, day: int = 15) -> float:
    return datetime(2026, month, day, 12, tzinfo=UTC).timestamp()


def _spec(world, trials: int = 4, usd: float | None = 3.0, parallel: int | None = None):
    return experiments.ExperimentSpec.model_validate({
        "name": "budgeted", "environment": "lockenv", "trials": trials, "keep_minutes": 0,
        **({"parallel": parallel} if parallel else {}),
        "tasks": [{"id": "t", "instructions": "work", "repo": world["url"]}], "variants": {"a": {}},
        **({"trial_job_budget": {"max_cost_usd": usd}} if usd else {})})


def _costed(monkeypatch, costs: dict[str, float]):
    real = jobbudget.spent
    monkeypatch.setattr(jobbudget, "spent", lambda svc, job, leave_out=None: jobbudget.Spent(
        costs[job.id], 0, 1) if job.id in costs else real(svc, job, leave_out))


def _eval_job(svc, person: str, t: float, cost: float, costs: dict[str, float]) -> Job:
    job = Job(title="trial", instructions="x", environment="lockenv", queue_class="eval", person=person,
              requester=person, state=JobState.SUCCEEDED, created_at=t, updated_at=t)
    svc._jobs[job.id] = job
    svc.store.put(job)
    costs[job.id] = cost
    return job


# ====================================================================== accounting
async def test_spend_is_per_month_and_author_and_rolls_over(jobs, monkeypatch, settings):  # noqa: F811
    costs: dict[str, float] = {}
    _costed(monkeypatch, costs)
    clock = {"t": at(10)}
    monkeypatch.setattr(evalbudget, "clock", lambda: clock["t"])
    _eval_job(jobs, "alice", at(10, 2), 120.0, costs)
    _eval_job(jobs, "bob", at(10, 3), 30.0, costs)
    _eval_job(jobs, "alice", at(9, 28), 999.0, costs)  # last month
    interactive = Job(title="j", instructions="x", environment="lockenv", person="alice", created_at=at(10, 4),
                      updated_at=at(10, 4))
    jobs._jobs[interactive.id] = interactive
    jobs.store.put(interactive)
    costs[interactive.id] = 500.0  # not an eval job: never counted
    st = evalbudget.status(jobs, "carol")
    assert st["month"] == "2026-10" and st["team"] == {"budget_usd": 1500.0, "spent_usd": 150.0,
                                                       "remaining_usd": 1350.0}
    assert st["authors"]["alice"] == {"cap_usd": 400.0, "spent_usd": 120.0, "remaining_usd": 280.0}
    assert st["authors"]["carol"]["spent_usd"] == 0.0
    jobs.store.put(Person(id="alice", eval_cap_usd_month=600))
    assert evalbudget.status(jobs)["authors"]["alice"]["cap_usd"] == 600.0
    clock["t"] = at(11, 1)  # a new month: nothing spent yet
    assert evalbudget.status(jobs)["team"]["spent_usd"] == 0.0
    settings.eval_budget_usd_month = 0
    assert not evalbudget.status(jobs)["on"] and evalbudget.spent_reason(jobs, "alice") is None


# ====================================================================== the gate
@needs_tools
async def test_an_experiment_over_the_authors_remaining_cap_is_refused(jobs, world, monkeypatch, settings):
    costs: dict[str, float] = {}
    _costed(monkeypatch, costs)
    _eval_job(jobs, "svc", evalbudget.clock(), 395.0, costs)
    with pytest.raises(JobError) as e:
        experiments.submit(jobs, _spec(world, trials=4, usd=3.0), Caller("svc", "svc"))
    msg = str(e.value)
    assert "would cost about $12.00 (the trial job budget)" in msg and "svc's monthly eval cap" in msg
    assert "$395.00 of $400.00 spent, $5.00 remaining" in msg
    settings.eval_author_cap_usd_month, settings.eval_budget_usd_month = 1000, 400
    with pytest.raises(JobError, match=r"team's monthly eval budget has left: \$395.00 of \$400.00 spent"):
        experiments.submit(jobs, _spec(world, trials=4, usd=3.0), Caller("svc", "svc"))
    experiments.submit(jobs, _spec(world, trials=1, usd=3.0), Caller("svc", "svc"))  # $3 fits in $5


@needs_tools
async def test_running_trials_finish_but_no_new_trial_starts_past_the_cap(jobs, world, monkeypatch):
    exp = experiments.submit(jobs, _spec(world, trials=3, usd=None, parallel=1), Caller("svc", "svc"))
    for _ in range(400):  # the first trial is running
        st = experiments.load(jobs.get(exp.id))
        if st.trials and st.trials[0].state == "running":
            break
        await __import__("asyncio").sleep(0.05)
    real = evalbudget.spending
    monkeypatch.setattr(evalbudget, "spending", lambda svc, t=None, fresh=False: {"svc": 401.0} if not fresh
                        else real(svc, t, fresh))  # it went over meanwhile
    exp = await _until(jobs, exp.id, DONE, timeout=120)
    st = experiments.load(exp)
    assert st.trials[0].outcome == "succeeded"  # the running one finished
    assert [t.state for t in st.trials[1:]] == ["skipped", "skipped"]
    assert "svc's monthly eval cap is spent ($401.00 of $400.00" in st.trials[1].reason


@needs_tools
async def test_cached_trials_cost_nothing_against_the_budget(jobs, world, monkeypatch, settings):
    p, _ = await _pinned(jobs, world)
    req = pinsets.TrialsRequest(pin_set=p.id, task=_task(world), n=2, budget=JobBudget(max_cost_usd=2),
                                reuse={"min_trials": 2})
    first = pinsets.start_trials(jobs, req, Caller("svc", "svc"))
    await _until(jobs, first["experiment"], DONE, timeout=180)
    _costed(monkeypatch, {x["job"]: 1.5 for x in __import__("hangar.trialcache").trialcache.lookup(
        jobs.store, first["cache_parts"])})  # what those trials cost
    settings.eval_budget_usd_month = 1.0  # less than one more trial
    again = pinsets.start_trials(jobs, req, Caller("svc", "svc"))
    assert again["experiment"] is None and len(again["cached"]) == 2  # served from the cache, no gate
    with pytest.raises(JobError, match=r"would cost about \$1.50 \(p50 of 2 cached trial\(s\)\)"):
        pinsets.start_trials(jobs, req.model_copy(update={"n": 3}), Caller("svc", "svc"))  # one new trial won't fit


def test_task_cost_uses_cached_trials_p50_then_the_budget(monkeypatch):
    from hangar import trialcache

    svc = SimpleNamespace(store=object(), _jobs={f"j{i}": SimpleNamespace(id=f"j{i}") for i in range(3)})
    monkeypatch.setattr(trialcache, "lookup", lambda store, parts: [{"job": "j0"}, {"job": "j1"}, {"job": "j2"}])
    monkeypatch.setattr(jobbudget, "spent", lambda s, job, leave_out=None: jobbudget.Spent(
        {"j0": 1.0, "j1": 9.0, "j2": 2.0}[job.id], 0, 1))
    assert evalbudget.task_cost(svc, {"k": 1}, 5.0) == (2.0, "p50 of 3 cached trial(s)")
    monkeypatch.setattr(trialcache, "lookup", lambda store, parts: [])
    assert evalbudget.task_cost(svc, {"k": 1}, 5.0) == (5.0, "the trial job budget")
    assert evalbudget.task_cost(svc, None, None) == (0.0, "no history and no dollar budget")


def test_rest_and_cli(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import hangar.cli as cli
    from hangar.app import create_app
    from tests.test_connect import _Keep

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings), headers={"Authorization": "Bearer t0ken"}) as c:
        member = c.post("/api/people", json={"id": "mo", "role": "member"}).json()["login"]["token"]
        b = c.get("/api/eval/budget").json()
        assert b["on"] and b["team"]["budget_usd"] == 1500.0
        assert c.get("/api/eval/budget", headers={"Authorization": f"Bearer {member}"}).status_code == 200
        assert c.put("/api/people/mo/eval-cap", json={"usd": 600}).json() == {"id": "mo", "eval_cap_usd_month": 600.0}
        assert c.put("/api/people/mo/eval-cap", json={"usd": 9},
                     headers={"Authorization": f"Bearer {member}"}).status_code == 403  # an admin's call
        assert c.put("/api/people/nobody/eval-cap", json={"usd": 1}).status_code == 404
        assert c.put("/api/people/mo/eval-cap", json={"usd": -1}).status_code == 422
        monkeypatch.setattr(cli, "_client", lambda: _Keep(c))
        r = CliRunner().invoke(cli.app, ["eval", "budget"])
        assert r.exit_code == 0 and "team $0.00 of $1,500.00 spent" in r.output
        r = CliRunner().invoke(cli.app, ["person", "set", "mo", "--eval-cap", ""])
        assert r.exit_code == 0 and "eval cap the default" in r.output
