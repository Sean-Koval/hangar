"""KAN-290: spend guardrails on by default. The default daily cap per repo (HANGAR_REPO_MAX_USD_PER_DAY), the alerts
at 80% and 100% of a daily cap (spendalert.py), an env-setup job keeping its own budget, and the deploys setting
every cap."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from hangar import jobbudget, metrics, people, spendalert
from hangar.config import Settings
from hangar.jobs import Caller, SubmitJob
from hangar.models import JobBudget, JobState, Person, Session, SessionTurn, Usage, now
from hangar.repopolicy import PolicyUpdate, RepoPolicy, update
from tests.test_interactions import DONE, jobs, sessions, until, world  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/acme/app.git"


def _session(jobs, usd: float, person: str | None = "alice", repo: str = REPO) -> Session:  # noqa: F811
    s = Session(name="s", environment="seamenv", repo=repo, person=person)
    s.turns = [SessionTurn(message="x", run_id=f"r{usd}", started_at=now(), usage=Usage(cost_usd=usd))]
    jobs.sessions._sessions[s.id] = s
    return s


@pytest.fixture
def clean(jobs):  # noqa: F811
    """Settings and sessions as they were, after each test."""
    s = jobs.settings
    before = (s.person_max_usd_per_day, s.repo_max_usd_per_day, list(s.spend_alert_at))
    added = set(jobs.sessions._sessions)
    yield jobs
    s.person_max_usd_per_day, s.repo_max_usd_per_day, s.spend_alert_at = before
    for sid in set(jobs.sessions._sessions) - added:
        del jobs.sessions._sessions[sid]
    for k in jobs.store.kv_keys(spendalert.PREFIX):
        jobs.store.kv_delete(k)


# ====================================================================== the default repo cap
def test_the_repos_own_cap_wins_over_the_default():
    s = Settings(repo_max_usd_per_day=40)
    assert jobbudget.repo_daily_cap(s, None) == 40
    assert jobbudget.repo_daily_cap(s, RepoPolicy(repo="a/b", max_usd_per_day=100)) == 100
    assert jobbudget.repo_daily_cap(Settings(), RepoPolicy(repo="a/b")) is None


def test_the_default_repo_cap_counts_against_a_repo_with_no_policy(jobs, clean):  # noqa: F811
    jobs.settings.repo_max_usd_per_day = 1.0
    _session(jobs, 1.5, person=None)
    [cap] = jobbudget.caps(jobs, REPO)
    assert cap.name == "acme/app's daily cap" and cap.limit_usd == 1.0 and cap.spent_usd == pytest.approx(1.5)
    assert jobbudget.over_daily(jobs, REPO).startswith("acme/app's daily cap is spent: $1.50 of $1.00 today")
    assert jobbudget.over_daily(jobs, "https://github.com/acme/other.git") is None
    jobs.settings.repo_max_usd_per_day = None
    assert jobbudget.caps(jobs, REPO) == []


async def test_a_repo_over_the_default_cap_holds_new_work(jobs, world, clean):  # noqa: F811
    submit = lambda: jobs.submit(SubmitJob(instructions="make fix.txt say good", repo=world["url"],  # noqa: E731
                                           environment="seamenv"), Caller("svc", "svc"))
    first = submit()  # admitted before the cap, so its budget isn't lowered to what is left of it
    jobs.settings.repo_max_usd_per_day = 0.002
    first = await until(jobs, first.id, DONE)
    assert first.state == JobState.SUCCEEDED  # it spent $0.003
    held = submit()
    assert held.state == JobState.AWAITING_APPROVAL and "daily cap is spent" in held.approval.reason
    jobs.cancel(held.id, "t")
    update(jobs, PolicyUpdate(repo=world["url"], max_usd_per_day=100), "admin")  # the repo's own cap wins
    ok = submit()
    assert ok.state != JobState.AWAITING_APPROVAL
    jobs.cancel(ok.id, "t")


# ====================================================================== env-setup keeps its own job budget
def test_an_env_setup_job_keeps_its_own_default_budget():
    s = Settings(job_budget_max_cost_usd=5, job_budget_max_turns=10)
    assert jobbudget.resolve(None, None, s) == JobBudget(max_cost_usd=5, max_turns=10)
    assert jobbudget.resolve(None, None, s, defaults=False) is None  # autoinstall._begin gives it $30
    p = RepoPolicy(repo="a/b", job_budget=JobBudget(max_cost_usd=12))
    assert jobbudget.resolve(None, p, s, defaults=False) == JobBudget(max_cost_usd=12)  # the policy's still wins


async def test_an_env_setup_submission_skips_the_deploys_job_budget(jobs, world, clean):  # noqa: F811
    jobs.settings.job_budget_max_cost_usd = 5
    try:
        plain = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="seamenv", deliver="none"),
                            Caller("svc", "svc"))
        setup = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="seamenv", deliver="none",
                                      labels={"hangar.kind": "env-setup"}), Caller("svc", "svc"))
        assert plain.job_budget == JobBudget(max_cost_usd=5)
        assert setup.job_budget is None  # _begin sets env_setup_max_cost_usd ($30), not the deploy's $5
    finally:
        jobs.settings.job_budget_max_cost_usd = None
        for j in (plain, setup):
            jobs.cancel(j.id, "t")


# ====================================================================== a job budget is a ceiling, and fits the caps
def test_a_job_budget_can_lower_the_ceiling_but_not_raise_it():
    cap = JobBudget(max_cost_usd=5, max_turns=10)
    assert jobbudget.ceiling(JobBudget(max_cost_usd=5000), cap) == JobBudget(max_cost_usd=5, max_turns=10)
    assert jobbudget.ceiling(JobBudget(max_cost_usd=2, max_tokens=900), cap) == JobBudget(
        max_cost_usd=2, max_tokens=900, max_turns=10)
    assert jobbudget.ceiling(JobBudget(max_cost_usd=9), None) == JobBudget(max_cost_usd=9)
    assert jobbudget.ceiling(None, cap) == cap


def test_a_job_budget_fits_what_is_left_of_its_caps():
    assert jobbudget.within(None, 3.0) == JobBudget(max_cost_usd=3.0)
    assert jobbudget.within(JobBudget(max_cost_usd=5, max_turns=4), 2.0) == JobBudget(max_cost_usd=2.0, max_turns=4)
    assert jobbudget.within(JobBudget(max_cost_usd=1), 2.0) == JobBudget(max_cost_usd=1)
    assert jobbudget.within(JobBudget(max_cost_usd=5), None) == JobBudget(max_cost_usd=5)  # nothing caps it
    assert jobbudget.within(JobBudget(max_cost_usd=5), 0.0) == JobBudget(max_cost_usd=5)  # spent: the hold's job


def test_headroom_is_the_least_left_across_the_caps(jobs, clean):  # noqa: F811
    assert jobbudget.headroom(jobs, REPO, "alice") is None
    jobs.settings.person_max_usd_per_day, jobs.settings.repo_max_usd_per_day = 20.0, 40.0
    _session(jobs, 15.0, person="alice")
    assert jobbudget.headroom(jobs, REPO, "alice") == pytest.approx(5.0)  # the person's $5, not the repo's $25
    assert jobbudget.headroom(jobs, REPO, "bob") == pytest.approx(20.0)  # bob's own $20, not the repo's $25 left


@pytest.fixture
def members(jobs):  # noqa: F811
    people.add(jobs.store, "mem", role="member")
    people.add(jobs.store, "boss", role="admin")
    yield
    for pid in ("mem", "boss"):
        people.remove(jobs.store, pid)


async def test_only_an_admin_may_ask_for_more_than_the_deploys_job_budget(jobs, world, clean, members):  # noqa: F811
    jobs.settings.job_budget_max_cost_usd = 5
    made = []
    try:
        def submit(caller: Caller, usd: float):
            made.append(j := jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="seamenv",
                                                   deliver="none", job_budget=JobBudget(max_cost_usd=usd)), caller))
            return j.job_budget.max_cost_usd

        assert submit(Caller("mem", "tok-mem", person="mem"), 5000) == 5  # held to the deploy's $5
        assert submit(Caller("mem", "tok-mem", person="mem"), 2) == 2  # lowering it is fine
        assert submit(Caller("boss", "tok-boss", person="boss"), 50) == 50  # an admin's own ask
        assert submit(Caller("admin", "admin"), 50) == 50  # the server's admin token
        assert not jobs._may_raise_job_budget(Caller("boss", "tok", restricted=True, person="boss"))  # a jobs token
        assert not jobs._may_raise_job_budget(Caller("x", "tok", person="nobody"))  # a person hangar doesn't know
        update(jobs, PolicyUpdate(repo=world["url"], job_budget=JobBudget(max_cost_usd=12)), "admin")
        assert submit(Caller("mem", "tok-mem", person="mem"), 5000) == 12  # the repo policy's is the ceiling
    finally:
        jobs.settings.job_budget_max_cost_usd = None
        update(jobs, PolicyUpdate(repo=world["url"], job_budget=None), "admin")
        for j in made:
            jobs.cancel(j.id, "t")


async def test_a_new_job_can_spend_only_what_is_left_of_its_daily_caps(jobs, world, clean, members):  # noqa: F811
    jobs.settings.job_budget_max_cost_usd = 5
    jobs.settings.person_max_usd_per_day = 20.0
    _session(jobs, 18.0, person="mem")
    made = []
    try:
        made.append(j := jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="seamenv",
                                               deliver="none"), Caller("mem", "tok-mem", person="mem")))
        assert j.job_budget.max_cost_usd == pytest.approx(2.0)  # $18 of $20 spent: $2 left, under the $5 budget
        made.append(j := jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="seamenv",
                                               deliver="none", job_budget=JobBudget(max_cost_usd=500)),
                                     Caller("boss", "tok-boss", person="boss")))
        assert j.job_budget.max_cost_usd == 20  # an admin may raise the job budget, but not past their own $20 cap
        _session(jobs, 3.0, person="mem")  # $21: spent
        made.append(j := jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="seamenv",
                                               deliver="none"), Caller("mem", "tok-mem", person="mem")))
        assert j.state == JobState.AWAITING_APPROVAL and j.job_budget.max_cost_usd == 5  # held; an approver decides
    finally:
        jobs.settings.job_budget_max_cost_usd = None
        for j in made:
            jobs.cancel(j.id, "t")


# ====================================================================== the alerts
def test_spend_today_is_counted_by_repo_and_by_person(jobs, clean):  # noqa: F811
    _session(jobs, 1.0, person="alice")
    _session(jobs, 2.0, person="bob")
    _session(jobs, 0.5, person=None, repo="https://github.com/acme/web.git")
    by_repo, by_person = spendalert.spent_today_by(jobs)
    assert by_repo["acme/app"] == pytest.approx(3.0) and by_repo["acme/web"] == pytest.approx(0.5)
    assert by_person == {"alice": pytest.approx(1.0), "bob": pytest.approx(2.0)}
    assert by_repo["acme/app"] == pytest.approx(jobbudget.repo_spent_today(jobs, "acme/app"))
    assert by_person["alice"] == pytest.approx(jobbudget.person_spent_today(jobs, "alice"))


def test_levels_ignore_shares_outside_zero_to_one(caplog):
    assert spendalert.levels(Settings()) == [80, 100]
    assert spendalert.levels(Settings(spend_alert_at=[1.0, 0.5, 0, 2])) == [50, 100]
    assert "shares are between 0 and 1" in caplog.text  # a typo like 80 is said, not silently dropped
    assert spendalert.levels(Settings(spend_alert_at=[])) == []


async def test_a_cap_alerts_once_at_80_and_once_at_100(jobs, clean, caplog, monkeypatch):  # noqa: F811
    caplog.set_level(logging.INFO, "hangar.slo")
    monkeypatch.setattr(metrics, "M", metrics.Metrics())
    sent: list[tuple[str, str]] = []

    async def fake_dm(svc, user, text, api=None):
        sent.append((user, text))
        return True

    monkeypatch.setattr("hangar.inbox_notify.send_dm", fake_dm)
    monkeypatch.setattr(people, "list_people", lambda store: [
        Person(id="alice", links=["slack:T1/UALICE"]), Person(id="bob", links=["slack:T1/UBOB"]),
        Person(id="root", role="admin", links=["slack:T1/UROOT"]),
        Person(id="quiet", role="admin", links=["slack:T1/UQUIET"], notify="off"),
        Person(id="nolink", role="admin")])
    jobs.settings.person_max_usd_per_day = 10.0
    _session(jobs, 8.5, person="alice")

    [c] = await spendalert.check(jobs)
    assert (c.scope, c.name, c.levels) == ("person", "alice", (80,))
    # every scope's and share's series exists from the first pass, at 0 until it alerts (increase() needs a start)
    assert metrics.M.spend_alerts.labels("repo", "100")._value.get() == 0
    assert {u for u, _ in sent} == {"UALICE", "UROOT"}  # the person and the admins who get DMs, nobody else
    own = next(t for u, t in sent if u == "UALICE")
    admin = next(t for u, t in sent if u == "UROOT")
    assert own.startswith("Spend alert: your daily cap is 80% spent ($8.50 of $10.00 today")
    assert admin.startswith("Spend alert: alice's daily cap is 80% spent")
    assert "At 100% new jobs wait" in own and "HANGAR_PERSON_MAX_USD_PER_DAY" in own
    lines = [r.getMessage() for r in caplog.records if r.name == "hangar.slo"]
    assert lines == ["event=spend_cap scope=person level=80"]  # bounded: no person, no repo
    assert metrics.M.spend_alerts.labels("person", "80")._value.get() == 1

    sent.clear()
    assert await spendalert.check(jobs) == [] and sent == []  # once a share a day
    _session(jobs, 2.0, person="alice")  # $10.50: spent
    [c] = await spendalert.check(jobs)
    assert c.levels == (100,) and "now wait for a trusted person's approval" in sent[0][1]


async def test_a_jump_past_both_shares_sends_one_dm_and_both_lines(jobs, clean, caplog, monkeypatch):  # noqa: F811
    caplog.set_level(logging.INFO, "hangar.slo")
    sent: list[str] = []

    async def fake_dm(svc, user, text, api=None):
        sent.append(text)
        return True

    monkeypatch.setattr("hangar.inbox_notify.send_dm", fake_dm)
    monkeypatch.setattr(people, "list_people", lambda store: [Person(id="root", role="admin",
                                                                     links=["slack:T1/UROOT"])])
    jobs.settings.repo_max_usd_per_day = 1.0
    _session(jobs, 3.0, person=None)
    [c] = await spendalert.check(jobs)
    assert (c.scope, c.name, c.levels) == ("repo", "acme/app", (80, 100))
    assert len(sent) == 1 and sent[0].startswith("Spend alert: acme/app's daily cap is 100% spent")
    assert "hangar repo set acme/app --max-usd-per-day" in sent[0]
    lines = [r.getMessage() for r in caplog.records if r.name == "hangar.slo"]
    assert lines == ["event=spend_cap scope=repo level=80", "event=spend_cap scope=repo level=100"]


async def test_no_caps_no_alerts_and_past_days_are_pruned(jobs, clean):  # noqa: F811
    _session(jobs, 50.0)
    assert await spendalert.check(jobs) == []  # nothing is capped
    jobs.store.kv_set(f"{spendalert.PREFIX}1:person:alice:80", "1")
    jobs.store.kv_set(f"{spendalert.PREFIX}bad", "1")
    await spendalert.check(jobs)
    assert jobs.store.kv_keys(spendalert.PREFIX) == []


async def test_a_cap_raised_mid_day_alerts_again_at_its_new_shares(jobs, clean, monkeypatch):  # noqa: F811
    monkeypatch.setattr(people, "list_people", lambda store: [])
    jobs.settings.person_max_usd_per_day = 10.0
    _session(jobs, 10.0, person="alice")
    [c] = await spendalert.check(jobs)
    assert c.levels == (80, 100)
    jobs.settings.person_max_usd_per_day = 12.0  # an admin raises it: $10 is 83% of the new cap
    [c] = await spendalert.check(jobs)
    assert (c.limit_usd, c.levels) == (12.0, (80,))
    assert await spendalert.check(jobs) == []


async def test_a_failed_dm_still_alerts_by_log_and_metric(jobs, clean, caplog, monkeypatch):  # noqa: F811
    caplog.set_level(logging.INFO, "hangar.slo")

    async def broken(svc, user, text, api=None):
        raise RuntimeError("slack down")

    monkeypatch.setattr("hangar.inbox_notify.send_dm", broken)
    monkeypatch.setattr(people, "list_people", lambda store: [Person(id="alice", links=["slack:T1/UA"])])
    jobs.settings.person_max_usd_per_day = 1.0
    _session(jobs, 1.0)
    assert len(await spendalert.check(jobs)) == 1
    assert "event=spend_cap scope=person level=100" in [r.getMessage() for r in caplog.records]
    assert await spendalert.check(jobs) == []  # marked: the log line isn't repeated every pass


def test_the_ops_loop_runs_it():
    from hangar import opstatus

    spendalert.register()
    assert spendalert.tick in opstatus.MINUTELY


# ====================================================================== the deploys set every cap
CAPS = ("HANGAR_JOB_BUDGET_MAX_COST_USD", "HANGAR_PERSON_MAX_USD_PER_DAY", "HANGAR_REPO_MAX_USD_PER_DAY")


@pytest.mark.parametrize("path", ["deploy/k8s/config.yaml", "deploy/kustomize/base/config.yaml"])
def test_the_deploys_set_a_job_budget_and_daily_caps(path):
    doc = next(d for d in yaml.safe_load_all((ROOT / path).read_text()) if d and d.get("kind") == "ConfigMap")
    s = Settings(**{k.removeprefix("HANGAR_").lower(): v for k, v in doc["data"].items() if k in CAPS})
    assert s.job_budget_max_cost_usd and s.person_max_usd_per_day and s.repo_max_usd_per_day


def test_the_company_overlay_sets_them_too():
    k = yaml.safe_load((ROOT / "deploy/kustomize/example/kustomization.yaml").read_text())
    site = next(g for g in k["configMapGenerator"] if g["name"] == "hangar-site")
    keys = {lit.split("=", 1)[0] for lit in site["literals"]}
    assert set(CAPS) <= keys and "HANGAR_BUDGET_DAY_TIMEZONE" in keys
