"""Budgets that bound a job, and a repo's daily spend cap (--- frontdoor, jobbudget.py).

The agent here is test_interactions' one: it reports token usage in Claude Code's stream-json (priced as
claude-sonnet-4-5: 1,000 input tokens are $0.003), and MODE_FILE picks how much it spends.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import hangar.cli as cli
from hangar import jobbudget, verifyloop
from hangar.app import create_app
from hangar.config import Settings
from hangar.jobs import Caller, JobError, SubmitJob
from hangar.models import Budget, Job, JobBudget, JobState, Turn, Usage, now
from hangar.repopolicy import PolicyUpdate, RepoPolicy, update
from hangar.triggers import approve
from tests.conftest import FakeProvider
from tests.test_interactions import DONE, SETTLED, bon, jobs, sessions, until, world  # noqa: F401 - fixtures


def _submit(jobs, world, **kw) -> Job:  # noqa: F811
    return jobs.submit(SubmitJob(**{"instructions": "make fix.txt say good", "repo": world["url"],
                                    "environment": "seamenv", **kw}), Caller("svc", "svc"))


def test_precedence_job_then_repo_then_settings():
    s = Settings(job_budget_max_cost_usd=10, job_budget_max_turns=20)
    p = RepoPolicy(repo="a/b", job_budget=JobBudget(max_cost_usd=5, max_tokens=1000))
    assert jobbudget.resolve(JobBudget(max_cost_usd=2), p, s) == JobBudget(max_cost_usd=2, max_tokens=1000,
                                                                          max_turns=20)
    assert jobbudget.resolve(None, None, Settings()) is None
    assert jobbudget.merge(None, Budget(max_minutes=5), cls=Budget) == Budget(max_minutes=5)
    assert "needs 4 agent turns" in jobbudget.check_submit(JobBudget(max_turns=3), 3, True)
    assert jobbudget.check_submit(JobBudget(max_turns=3), 1, False) is None


async def test_a_job_budget_bounds_follow_ups(jobs, world):  # noqa: F811
    """B4: each follow-up (and auto-fix round, which is one) used to get a fresh per-turn budget, so a job's spend
    had no bound. Now a turn gets only what the job has left, and a spent job starts no more turns."""
    job = _submit(jobs, world, job_budget=JobBudget(max_tokens=500_000))
    job = await until(jobs, job.id, DONE)
    assert job.state == JobState.SUCCEEDED and job.job_budget.max_tokens == 500_000
    world["mode"].write_text("burnall")  # spends 900,000 tokens, then keeps going for 30 s
    jobs.follow_up(job.id, "more", requester="svc")
    job = await until(jobs, job.id, DONE)
    assert job.state == JobState.FAILED and job.reason == "over_budget", [e.message for e in job.events]
    assert "499,000" in job.turns[-1].outcome_reason  # the turn got what the job had left, and was stopped there
    assert any("may use what the job budget has left: 499,000 tokens" in e.message for e in job.events)
    with pytest.raises(JobError, match="job budget tokens are used"):
        jobs.follow_up(job.id, "and more", requester="svc")


async def test_turns_are_capped_and_verify_fix_rounds_respect_the_job_budget(jobs, world):  # noqa: F811
    job = _submit(jobs, world, job_budget=JobBudget(max_turns=1))
    job = await until(jobs, job.id, DONE)
    with pytest.raises(JobError, match="its 1 agent turn"):
        jobs.follow_up(job.id, "more", requester="svc")
    assert verifyloop.fix_turn(jobs, job, job.turns[-1], "exited") is None  # the gate's rounds don't get around it
    job.verify = "false"
    assert verifyloop.fix_turn(jobs, job, job.turns[-1], "exited") is None
    assert any("no verify-fix turn: its 1 agent turn(s) are used" in e.message for e in job.events)


async def test_best_of_n_attempts_and_the_judge_share_the_job_budget(jobs, world):  # noqa: F811
    world["mode"].write_text("budget")  # attempt 1 spends 900,000 tokens and keeps going; the others 1,000
    job = jobs.submit(bon(world, n=2, job_budget=JobBudget(max_tokens=1_000_000)), Caller("svc", "svc"))
    job = await until(jobs, job.id, SETTLED)
    a1, a2 = job.attempts
    assert a1.outcome == "over_budget" and a2.outcome == "exited", [e.message for e in job.events]
    s1 = jobs.sessions.get(a1.session_id)
    assert s1.budget.max_tokens == 500_000  # half each: together they can't cross it
    with pytest.raises(JobError, match="needs 4 agent turns"):
        jobs.submit(bon(world, n=3, judge=True, job_budget=JobBudget(max_turns=3)), Caller("svc", "svc"))


async def test_the_repo_policy_sets_budgets(jobs, world):  # noqa: F811
    update(jobs, PolicyUpdate(repo=world["url"], budget=Budget(max_minutes=30), job_budget=JobBudget(max_turns=5)),
           "admin")
    job = _submit(jobs, world, budget=Budget(max_tokens=10_000), deliver="none")
    assert job.budget == Budget(max_minutes=30, max_tokens=10_000)  # per field: the job's, then the repo's
    assert job.job_budget == JobBudget(max_turns=5)
    jobs.cancel(job.id, "t")


async def test_a_repo_over_its_daily_cap_holds_new_work(jobs, world):  # noqa: F811
    update(jobs, PolicyUpdate(repo=world["url"], max_usd_per_day=0.002), "admin")
    first = await until(jobs, _submit(jobs, world).id, DONE)
    assert first.state == JobState.SUCCEEDED  # admitted under the cap; it spent $0.003
    assert jobbudget.repo_spent_today(jobs, first.repo.removesuffix(".git")) == pytest.approx(0.003)
    held = _submit(jobs, world)
    assert held.state == JobState.AWAITING_APPROVAL
    assert held.approval.reason.endswith("daily cap is spent: $0.0030 of $0.0020 today (UTC)")
    with pytest.raises(JobError, match="daily cap is spent"):
        jobs.follow_up(first.id, "more", requester="svc")  # and auto-fix rounds, which are follow-ups
    approve(jobs, held.id, "admin")  # a trusted person can let it run anyway
    held = await until(jobs, held.id, DONE)
    assert held.state == JobState.SUCCEEDED


def test_spend_counts_from_midnight_utc(jobs):  # noqa: F811
    from hangar.models import Session, SessionTurn

    s = Session(name="s", environment="seamenv", repo="https://github.com/acme/app.git")
    s.turns = [SessionTurn(message="x", run_id="r1", started_at=now(), usage=Usage(cost_usd=1.5)),
               SessionTurn(message="x", run_id="r0", started_at=jobbudget.day_start() - 60, usage=Usage(cost_usd=9)),
               SessionTurn(message="x", started_at=now(), usage=None)]  # a fork's copy: never ran here
    jobs.sessions._sessions[s.id] = s
    assert jobbudget.repo_spent_today(jobs, "acme/app") == pytest.approx(1.5)
    del jobs.sessions._sessions[s.id]


def test_turns_count_once_across_sessions(jobs):  # noqa: F811
    job = Job(title="t", instructions="x", environment="seamenv", branch="hangar/j",
              job_budget=JobBudget(max_turns=3, max_cost_usd=1.0))
    job.turns = [Turn(run_id="a", usage=Usage(input_tokens=10, cost_usd=0.4))]
    assert jobbudget.spent(jobs, job) == (0.4, 10, 1) and jobbudget.exhausted(jobs, job) is None
    clipped = jobbudget.clip(jobs, job, Budget(max_cost_usd=5.0, max_minutes=10))
    assert clipped.max_minutes == 10 and clipped.max_cost_usd == pytest.approx(0.6)
    job.turns.append(Turn(run_id="b", usage=Usage(input_tokens=10, cost_usd=0.7)))
    assert "of its $1.00 job budget is spent" in jobbudget.exhausted(jobs, job)
    assert jobbudget.clip(jobs, job, None).max_cost_usd == pytest.approx(1e-6)  # stops at the first poll


def test_rest_and_cli_budget_fields(settings, monkeypatch):
    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.hangar.fleet.providers["docker"] = FakeProvider()
        c.headers["Authorization"] = "Bearer t0ken"
        r = c.put("/api/repos/policy", json={"repo": "acme/app", "job_budget": {"max_cost_usd": 5, "max_turns": 8},
                                              "max_usd_per_day": 50, "budget": {"max_minutes": 45}})
        assert r.status_code == 200, r.text
        assert r.json()["job_budget"] == {"max_cost_usd": 5.0, "max_tokens": None, "max_turns": 8}
        assert r.json()["spent_today_usd"] == 0
        assert c.put("/api/repos/policy", json={"repo": "acme/app", "max_usd_per_day": 0}).status_code == 422
        assert c.delete("/api/repos/policy", params={"repo": "acme/app", "field": ["job_budget"]}).json()[
            "job_budget"] is None
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw))
        return {"repo": "acme/app", "job_budget": {"max_cost_usd": 5.0}, "max_usd_per_day": 50.0}

    monkeypatch.setattr(cli, "_call", fake)
    r = CliRunner().invoke(cli.app, ["repo", "set", "acme/app", "--job-max-cost", "5", "--max-usd-per-day", "50",
                                     "--max-minutes", "45"])
    assert r.exit_code == 0, r.output
    assert calls[-1][2]["json"] == {"repo": "acme/app", "max_usd_per_day": 50.0, "budget": {"max_minutes": 45},
                                    "job_budget": {"max_cost_usd": 5.0}}
    assert "job budget: cost_usd=5.0" in r.output and "daily cap: $50" in r.output


async def test_a_github_issue_over_the_daily_cap_says_why_on_the_issue(jobs, world):  # noqa: F811
    import asyncio

    from hangar.jobs import handle_github
    from hangar.models import Session, SessionTurn

    update(jobs, PolicyUpdate(repo="o/r", max_usd_per_day=1.0), "admin")
    s = Session(name="spent", environment="seamenv", repo="https://github.com/o/r.git")
    s.turns = [SessionTurn(message="x", run_id="r1", started_at=now(), usage=Usage(cost_usd=1.25))]
    jobs.sessions._sessions[s.id] = s
    issue = {"number": 5, "title": "bug", "body": "...", "user": {"login": "sean"}, "author_association": "OWNER"}
    res = await handle_github(jobs, "issues", "d-1", {"action": "labeled", "label": {"name": "hangar"},
                                                       "repository": {"full_name": "o/r"}, "issue": issue,
                                                       "sender": {"login": "sean"}, })
    assert res["awaiting_approval"] and jobs.get(res["job"]).state == JobState.AWAITING_APPROVAL
    await asyncio.sleep(0.1)
    said = [c for c in jobs.hooks.comments() if "waiting for approval" in c]
    assert said and "o/r's daily cap is spent: $1.25 of $1.00 today (UTC)" in said[0] and "Queued" not in said[0]
    del jobs.sessions._sessions[s.id]


# ====================================================================== review fixes
def test_the_day_starts_at_midnight_where_the_team_is(jobs, monkeypatch):  # noqa: F811
    """A UTC day turns over at 20:00 in New York (19:00 in winter), in the middle of the team's working hours, so
    a 'daily' cap could be spent twice in one working day. HANGAR_BUDGET_DAY_TIMEZONE puts midnight where the
    team is, through daylight-saving changes."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from hangar.models import Session, SessionTurn

    ny = ZoneInfo("America/New_York")
    late = datetime(2026, 11, 1, 23, 30, tzinfo=ny).timestamp()  # the day DST ends: 25 hours long
    assert jobbudget.day_start(late, "America/New_York") == datetime(2026, 11, 1, 4, tzinfo=UTC).timestamp()
    assert jobbudget.day_start(late) == datetime(2026, 11, 2, tzinfo=UTC).timestamp()  # the default stays UTC
    spring = datetime(2026, 3, 8, 12, 0, tzinfo=ny).timestamp()  # the day DST starts: 23 hours long
    assert jobbudget.day_start(spring, "America/New_York") == datetime(2026, 3, 8, 5, tzinfo=UTC).timestamp()

    t = datetime(2026, 10, 14, 21, 0, tzinfo=ny).timestamp()  # 01:00 UTC on the 15th
    monkeypatch.setattr(jobbudget, "now", lambda: t)
    s = Session(name="s", environment="seamenv", repo="https://github.com/acme/app.git")
    s.turns = [SessionTurn(message="x", run_id="r1", started_at=t - 5400, usage=Usage(cost_usd=2.0)),  # 19:30 ET
               SessionTurn(message="x", run_id="r2", started_at=t - 7200, usage=Usage(cost_usd=3.0))]  # 19:00 ET
    jobs.sessions._sessions[s.id] = s
    try:
        assert jobbudget.repo_spent_today(jobs, "acme/app") == pytest.approx(0.0)  # UTC: a new day began at 20:00
        monkeypatch.setattr(jobs.settings, "budget_day_timezone", "America/New_York")
        assert jobbudget.repo_spent_today(jobs, "acme/app") == pytest.approx(5.0)
        jobs.policies.put(RepoPolicy(repo="acme/app", max_usd_per_day=4))
        assert jobbudget.over_daily(jobs, "acme/app").endswith("$5.00 of $4.00 today (America/New_York)")
        monkeypatch.setattr(jobs.settings, "budget_day_timezone", "Mars/Olympus")  # a typo: still a cap, in UTC
        assert jobbudget.over_daily(jobs, "acme/app") is None
    finally:
        del jobs.sessions._sessions[s.id]


async def test_a_spent_job_budget_starts_no_judge(jobs, world):  # noqa: F811
    """The judge got 'what the attempts left', clipped to almost nothing when they overshot their shares, but it
    still started and spent until the first poll stopped it. A spent job runs no judge."""
    world["mode"].write_text("burnall")  # every attempt passes, spends 900,000 tokens and is stopped over its share
    job = jobs.submit(bon(world, judge=True, job_budget=JobBudget(max_tokens=2_000_000)), Caller("svc", "svc"))
    job = await until(jobs, job.id, SETTLED)
    events = "\n".join(e.message for e in job.events)
    assert all(a.outcome == "over_budget" for a in job.attempts), events
    turns = [t for s in jobs.sessions._sessions.values() if s.job_id == job.id for t in s.turns]
    assert not any((t.message or "").startswith("judge") for t in turns), events
    assert "no judge: " in events and "ranking: " in events
