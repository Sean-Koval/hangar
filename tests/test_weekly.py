"""Weekly adoption metrics and the standup line (#168): every metric of docs/metrics.md on a fixture store with known
answers, the landing facts they need (ready, first human review, abandon reasons, reverts), and the standup line."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from hangar import weekly
from hangar.config import Settings
from hangar.jobs import JobService, handle_github
from hangar.models import CredentialKind, Job, JobState, Session, SessionTurn, Steer, Turn, Usage
from hangar.store import Store
from tests.test_landing import approval, fx, gh, pr_job, svc  # noqa: F401 - fixtures

ET = ZoneInfo("America/New_York")
WEEK = "2026-W42"  # Monday 2026-10-12 to Monday 2026-10-19, New York time


def at(day: int, hour: float = 9.0, month: int = 10) -> float:
    return datetime(2026, month, day, int(hour), int((hour % 1) * 60), tzinfo=ET).timestamp()


# ====================================================================== the fixture store
def _job(store: Store, n: int, person: str | None, opened: float, **landing) -> Job:
    j = Job(title=f"j{n}", instructions="x", environment="py", repo="https://github.com/acme/app.git",
            state=JobState.SUCCEEDED, person=person, created_at=opened)
    j.result.pr_number, j.result.pr_url = n, f"https://github.com/acme/app/pull/{n}"
    j.landing.opened_at = opened
    j.landing.pr_state = "open"
    j.turns = [Turn(run_id=f"run-{n:08d}", started_at=opened)]  # it ran: a counted job
    for k, v in landing.items():
        setattr(j.landing, k, v)
    store.put(j)
    return j


@pytest.fixture
def fixture(tmp_path):
    """Three merged agent PRs (ana), two abandoned and one still open (bo), a job that failed without a PR, an
    infra_error job, one cancelled before it ran, an old one, and the revert cohort two weeks back."""
    store = Store(tmp_path / "db")
    settings = Settings(data_dir=tmp_path / "data", github_repos={"acme/app": "py"})
    costs: dict[str, float] = {}
    h = 3600
    j1 = _job(store, 1, "ana", at(13), ready_at=at(13), first_review_at=at(13) + 2 * h, pr_state="merged",
              merged_at=at(14), green_to_merge_seconds=1800)
    j1.turns = [Turn(run_id="run-1", started_at=at(13)), Turn(kind="follow_up", message="also docs",
                                                              run_id="run-2")]
    store.put(j1)
    j2 = _job(store, 2, "ana", at(13), ready_at=at(13), first_review_at=at(13) + 4 * h, pr_state="merged",
              merged_at=at(15), green_to_merge_seconds=3600, reverted_at=at(15) + 3 * 86400, revert_sha="r" * 40)
    j2.turns = [Turn(run_id="run-3", outcome="needs_input", started_at=at(13))]
    store.put(j2)
    _job(store, 3, "ana", at(14), ready_at=at(14), first_review_at=at(14) + 10 * h, pr_state="merged",
         merged_at=at(16), green_to_merge_seconds=7200, fix_forward_pr=40, fix_forward_at=at(17))
    _job(store, 4, "bo", at(12), ready_at=at(12), pr_state="closed", closed_at=at(13), abandon_reason="flaky_env")
    _job(store, 5, "bo", at(12), pr_state="closed", closed_at=at(14))
    _job(store, 6, "bo", at(15), ready_at=at(15))
    infra = _job(store, 7, "ana", at(13), pr_state="closed", closed_at=at(14))
    infra.result.outcome_class = "infra_error"
    store.put(infra)
    _job(store, 8, "ana", at(1, month=9), pr_state="merged", merged_at=at(2, month=9))  # long before
    _job(store, 9, "ana", at(29, month=9), pr_state="merged", merged_at=at(30, month=9), reverted_at=at(2))
    _job(store, 10, "bo", at(29, month=9), pr_state="merged", merged_at=at(1))  # the cohort: W40, not reverted
    nopr = Job(title="j11", instructions="x", environment="py", repo="https://github.com/acme/app.git",
               state=JobState.FAILED, person="bo", turns=[Turn(run_id="run-11", started_at=at(15))])
    never = Job(title="j12", instructions="x", environment="py", repo="https://github.com/acme/app.git",
                state=JobState.CANCELLED, person="bo")
    trial = Job(title="t", instructions="x", environment="py", source="experiment", state=JobState.FAILED,
                turns=[Turn(run_id="run-t")])
    for j in (nopr, never, trial):
        j.updated_at = at(15)
        store.put(j)
    s = Session(name="j1", environment="py", job_id=j1.id,
                turns=[SessionTurn(message="go", steers=[Steer(text="use utf-8"), Steer(text="and tests")])])
    store.put(s)
    for j, usd in ((j1, 2.0), (j2, 1.0)):
        costs[j.id] = usd
    costs[store.list_where(Job, **{"result.pr_number": 3})[0].id] = 3.0
    store.kv_set_json(weekly.KV_MERGED + "acme/app", {"at": at(18), "prs": [
        {"number": n, "merged_at": t} for n, t in ((1, at(14)), (2, at(15)), (3, at(16)), (90, at(13)),
                                                   (91, at(17)), (92, at(5)))]})
    return SimpleNamespace(store=store, settings=settings,
                           usage=lambda j: Usage(cost_usd=costs[j.id]) if j.id in costs else None)


def _metrics(fx_svc, **kw) -> dict:
    return weekly.weekly(fx_svc, WEEK, **kw)["metrics"]


def test_yield_is_merged_over_counted_terminal_agent_jobs(fixture):
    y = _metrics(fixture)["yield"]
    assert (y["merged"], y["terminal"], y["value"]) == (3, 6, 0.5)
    assert _metrics(fixture)["excluded_infra_error"] == 1


def test_agent_share_divides_by_every_merged_pr_on_the_mapped_repos(fixture):
    a = _metrics(fixture)["agent_share"]
    assert (a["agent_merged"], a["all_merged"], a["value"]) == (3, 5, 0.6)
    fixture.store.kv_set(weekly.KV_MERGED + "acme/app", "null")
    assert _metrics(fixture)["agent_share"]["value"] is None


def test_review_wait_is_first_human_review_minus_ready(fixture):
    r = _metrics(fixture)["review_wait"]
    assert (r["p50_hours"], r["p90_hours"], r["n"]) == (4.0, 10.0, 3)


def test_green_to_merge_p50_and_p90(fixture):
    assert _metrics(fixture)["green_to_merge"] == {"p50_hours": 1.0, "p90_hours": 2.0, "n": 3}


def test_steer_rate_counts_follow_ups_steers_and_questions_per_merged_pr(fixture):
    s = _metrics(fixture)["steer_rate"]
    assert (s["follow_ups"], s["steers"], s["needs_input"], s["merged"]) == (1, 2, 1, 3)
    assert s["value"] == round(4 / 3, 4) and s["corrections"] == 0 and s["unclassified"] == 3


def test_abandon_rate_by_reason_with_the_unlabelled_shown(fixture):
    ab = _metrics(fixture)["abandon_rate"]
    assert (ab["abandoned"], ab["terminal"], ab["value"]) == (2, 6, round(2 / 6, 4))
    assert ab["by_reason"] == {"flaky_env": 1, "unlabelled": 1} and ab["unlabelled"] == 1


def test_revert_rate_is_the_cohort_merged_two_weeks_earlier(fixture):
    rv = _metrics(fixture)["revert_rate"]
    assert (rv["reverted"], rv["cohort"], rv["value"]) == (1, 2, 0.5)


def test_cost_per_merged_pr(fixture):
    c = _metrics(fixture)["cost_per_merged_pr"]
    assert (c["usd"], c["total_usd"], c["merged"]) == (2.0, 6.0, 3)


def test_active_delegators_have_three_merged_prs(fixture):
    d = _metrics(fixture)["active_delegators"]
    assert d["value"] == 1 and d["people"] == ["ana"] and d["of"] == 2


def test_per_person_and_per_repo(fixture):
    bo = _metrics(fixture, person="bo")
    assert bo["abandon_rate"]["abandoned"] == 2 and bo["yield"]["value"] == 0.0 and bo["yield"]["terminal"] == 3
    assert bo["agent_share"]["value"] is None and "people" not in bo["active_delegators"]
    assert _metrics(fixture, repo="acme/other")["yield"]["terminal"] == 0
    assert _metrics(fixture, repo="Acme/App")["yield"]["terminal"] == 6


def test_weeks_and_percentiles():
    lo, hi, label = weekly.week_bounds("2026-W42", ET)
    assert label == WEEK and lo == at(12, 0) and hi == at(19, 0)
    utc = weekly.week_bounds("2026-W42", weekly.UTC_ZONE)  # flightdeck's weeks, which weekly() uses
    assert utc[0] == datetime(2026, 10, 12, tzinfo=weekly.UTC_ZONE).timestamp()
    assert weekly.week_bounds(None, ET, at(14))[2] == WEEK
    with pytest.raises(weekly.WeeklyError):
        weekly.week_bounds("2026-43", ET)
    assert weekly.pct([1, 2, 3, 4], 50) == 2 and weekly.pct([1, 2, 3, 4], 90) == 4 and weekly.pct([], 50) is None


# ====================================================================== what landing records
async def test_first_review_ignores_bots_including_the_apps_ai_review(svc, gh):  # noqa: F811
    job = pr_job(svc)
    bot = approval(state="commented")
    bot["review"]["user"] = {"login": "hangar-agents[bot]", "type": "Bot", "id": 9}
    await handle_github(svc, "pull_request_review", "d-1", bot)
    assert job.landing.first_review_at is None
    await handle_github(svc, "pull_request_review", "d-2", approval(state="changes_requested", login="carol"))
    assert job.landing.first_review_by == "carol" and job.landing.first_review_at
    first = job.landing.first_review_at
    await handle_github(svc, "pull_request_review", "d-3", approval(state="commented", login="dave"))
    assert job.landing.first_review_at == first and job.landing.first_review_by == "carol"


async def test_ready_at_and_merge_commit_sha(svc, gh):  # noqa: F811
    job = pr_job(svc)
    svc.landing.opened(job, {"draft": True, "created_at": "2026-10-14T09:00:00Z"})
    assert job.landing.opened_at and job.landing.ready_at is None
    p = fx("pull_request.synchronize")
    p["action"] = "ready_for_review"
    p["pull_request"]["draft"] = False
    await handle_github(svc, "pull_request", "d-1", p)
    assert job.landing.ready_at
    merged = fx("pull_request.closed.merged")
    merged["pull_request"]["merge_commit_sha"] = "m" * 40
    await handle_github(svc, "pull_request", "d-2", merged)
    assert job.landing.merge_commit_sha == "m" * 40


def _comment(body: str, login: str = "alice") -> dict:
    return {"action": "created", "issue": {"number": 7, "pull_request": {}},
            "comment": {"body": body, "user": {"login": login, "id": 77}, "author_association": "OWNER"},
            "repository": {"full_name": "o/r"}, "sender": {"login": login}}


async def test_hangar_abandon_sets_the_reason_and_refuses_an_unknown_one(svc, gh):  # noqa: F811
    job = pr_job(svc)
    got = await handle_github(svc, "issue_comment", "d-1", _comment("/hangar abandon flaky_env"))
    assert got == {"ok": True, "abandon_reason": "flaky_env"} and job.landing.abandon_by == "github:alice"
    assert any(a.action == "landing.abandon_reason" for a in svc.store.audit_tail(10))
    bad = await handle_github(svc, "issue_comment", "d-2", _comment("/hangar abandon meh"))
    assert "isn't an abandon reason" in bad["ignored"] and "wrong_approach" in bad["ignored"]
    assert job.landing.abandon_reason == "flaky_env"
    said = [json.loads(c.content)["body"] for c in gh.calls if c.method == "POST" and c.url.path.endswith(
        "/comments")]
    assert any("superseded" in b for b in said)  # the list, on the PR
    assert not job.turns  # not a follow-up


async def test_an_agent_abandon_label_sets_the_reason(svc, gh):  # noqa: F811
    job = pr_job(svc)
    p = fx("pull_request.synchronize")
    p["action"], p["label"] = "labeled", {"name": "agent-abandon:too_big"}
    await handle_github(svc, "pull_request", "d-1", p)
    assert job.landing.abandon_reason == "too_big"
    p["label"] = {"name": "agent-abandon:nope"}
    await handle_github(svc, "pull_request", "d-2", p)
    assert job.landing.abandon_reason == "too_big" and "isn't an abandon reason" in job.events[-1].message


def test_closed_prs_without_a_reason_are_in_the_inbox(svc):  # noqa: F811
    from hangar import inbox

    job = pr_job(svc)
    job.landing.pr_state, job.landing.closed_at = "closed", weekly.now()
    svc.store.put(job)
    items = weekly.inbox_source(svc, None)
    assert [i["kind"] for i in items] == ["abandon"] and "/hangar abandon" in items[0]["action"]
    job.landing.abandon_reason = "superseded"
    svc.store.put(job)
    assert weekly.inbox_source(svc, None) == [] and inbox  # registered by mount()


# ====================================================================== the nightly pass
async def test_the_revert_pass_finds_a_revert_of_an_agent_prs_squash_sha(fleet, settings, broker):
    t = weekly.now()
    iso = datetime.fromtimestamp(t - 3600).astimezone().isoformat()
    squash = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    calls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(r.url.path)
        if r.url.path == "/repos/o/r/pulls":
            return httpx.Response(200, json=[
                {"number": 7, "merged_at": iso, "updated_at": iso, "merge_commit_sha": squash,
                 "user": {"type": "Bot"}, "body": "fix"},
                {"number": 12, "merged_at": iso, "updated_at": iso, "user": {"type": "User"},
                 "body": "Follow-up.\n\nFixes-forward: #9\n"},
                {"number": 13, "merged_at": None, "updated_at": iso, "user": {"type": "User"}}])
        if r.url.path == "/repos/o/r/commits":
            return httpx.Response(200, json=[{"sha": "f" * 40, "commit": {
                "message": f'Revert "Fix the unicode bug (#7)"\n\nThis reverts commit {squash[:12]}.',
                "committer": {"date": iso}}}])
        return httpx.Response(404, json={})

    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_control")
    settings.github_token_credential, settings.github_repos = "gh", {"o/r": "jobenv"}
    svc = JobService(fleet, None, transport=httpx.MockTransport(handler))  # noqa: F811
    job = pr_job(svc)
    job.landing.pr_state, job.landing.merged_at, job.landing.merge_commit_sha = "merged", t - 7200, squash
    svc.store.put(job)
    other = pr_job(svc)
    other.result.pr_number = 9
    other.landing.pr_state, other.landing.merged_at = "merged", t - 86400
    svc.store.put(other)
    got = await weekly.nightly(svc)
    assert got == {"repos": 1, "merged": 2, "reverted": 1, "fixed_forward": 1}
    assert job.landing.reverted_at and job.landing.revert_sha == "f" * 40
    assert other.landing.fix_forward_pr == 12 and other.landing.fix_forward_at
    cache = svc.store.kv_get_json(weekly.KV_MERGED + "o/r")
    assert sorted(p["number"] for p in cache["prs"]) == [7, 12]
    assert (await weekly.nightly(svc))["reverted"] == 0  # once
    assert all(not c.startswith("/repos/o/r/pulls/") for c in calls)  # read-only listings


# ====================================================================== the standup line
async def test_the_standup_line_posts_once_per_weekday_at_1018_et(fleet, settings, broker):
    posts: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/chat.postMessage"):
            posts.append(dict(httpx.QueryParams(r.content.decode())))
            return httpx.Response(200, json={"ok": True, "ts": "1.0"})
        return httpx.Response(404, json={})

    broker.put("slack-bot", "webhook", CredentialKind.GENERIC, "xoxb-test")
    settings.slack_bot_token_credential = "slack-bot"
    svc = JobService(fleet, None, transport=httpx.MockTransport(handler))  # noqa: F811
    job = pr_job(svc)
    job.landing.pr_state, job.landing.merged_at = "merged", at(13, 15)
    job.landing.ready_at, job.landing.first_review_at = at(13, 9), at(13, 11)
    svc.store.put(job)
    assert await weekly.post_standup(svc, at(14, 10.5)) is None  # no channel: nothing
    settings.standup_channel = "C0STANDUP"
    assert await weekly.post_standup(svc, at(14, 10.2)) is None  # 10:12, before 10:18
    line = await weekly.post_standup(svc, at(14, 10.3))  # Wednesday 10:18
    assert line.startswith("Yesterday: 1 agent PRs merged, 0 abandoned, review wait p50 2.0 h")
    assert "top failure: none" in line
    assert await weekly.post_standup(svc, at(14, 11)) is None  # once a day
    assert len(posts) == 1 and posts[0]["channel"] == "C0STANDUP" and posts[0]["text"] == line
    assert await weekly.post_standup(svc, at(17, 10.5)) is None  # Saturday
    monday = await weekly.post_standup(svc, at(19, 10.5))
    assert monday.startswith(f"Last week ({WEEK}): 1 agent PRs merged") and "active delegators" in monday
    assert len(posts) == 2


def test_standup_due_respects_the_window():
    s = Settings(standup_channel="C1")
    assert weekly.standup_due(s, at(14, 10.3), False)
    assert not weekly.standup_due(s, at(14, 10.3), True)
    assert not weekly.standup_due(s, at(14, 13.5), False)  # hangar was down all morning: skipped
    assert not weekly.standup_due(Settings(), at(14, 10.3), False)


def test_ready_and_the_first_human_review_are_outbox_job_events():
    """flightdeck reserved them: `pr.ready`, and the first human review on `review.first` (#168)."""
    from hangar import outbox

    j = Job(title="t", instructions="x", environment="py")
    j.result.pr_url, j.result.pr_number = "https://github.com/o/r/pull/7", 7
    j.landing.ready_at, j.landing.approved_at, j.landing.approved_by = at(13), at(14), "alice"
    j.landing.first_review_at, j.landing.first_review_by = at(13, 11), "carol"
    facts = {k: (ts, data) for k, ts, data, _ in outbox.Outbox.lifecycle(SimpleNamespace(), j)}
    assert facts["pr.ready"][0] == at(13) and "pr.ready" in outbox.EVENT_KINDS
    assert facts["review.first"][1]["first_review_by"] == "carol"
    assert facts["review.first"][1]["first_review_at"] == at(13, 11)
