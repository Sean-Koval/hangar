"""Review-aware admission (#159): past N open agent PRs awaiting a person, their new jobs wait as `awaiting_capacity`
(state `queued`); follow-ups, auto-fix rounds, AI reviews and admitted jobs don't; a merge wakes the queue."""

from __future__ import annotations

import asyncio
import types

import pytest

from hangar import fairness
from hangar.jobs import JobService, handle_github
from hangar.metrics import Metrics
from hangar.models import Job, JobState, Person, Turn
from tests.test_landing import GH, fx, gh, lands, pr_job, settle, svc  # noqa: F401 - fixtures

ALICE_PRS = 5


def open_prs(svc: JobService, person: str, n: int, first: int = 100) -> list[Job]:  # noqa: F811
    """`n` delivered jobs of `person`, each with an open agent PR."""
    out = []
    for i in range(n):
        job = pr_job(svc, person=person, requester=person)
        job.result.pr_number = first + i
        svc._save(job)  # what a landing event does: the review load follows every save
        out.append(job)
    return out


def new_job(svc: JobService, person: str, **kw) -> Job:  # noqa: F811
    job = Job(title=f"new work for {person}", instructions="x", environment="jobenv",
              repo="https://github.com/o/r.git", person=person, requester=person, turns=[Turn()], **kw)
    svc._jobs[job.id] = job
    svc._save(job)
    svc._queue.put_nowait(job.id)
    return job


async def take(svc: JobService, timeout: float = 0.3) -> str | None:  # noqa: F811
    try:
        return await asyncio.wait_for(svc._queue.get(), timeout)
    except TimeoutError:
        return None


def merged(number: int) -> dict:
    p = fx("pull_request.closed.merged")
    p["number"] = p["pull_request"]["number"] = number
    return p


@pytest.fixture
def team(svc, settings):  # noqa: F811
    settings.person_max_open_agent_prs = ALICE_PRS
    for p in ("alice", "bob"):
        svc.store.put(Person(id=p, name=p))
    return svc


async def test_a_person_at_the_cap_waits_and_others_start(team):
    svc = team  # noqa: F811
    prs = open_prs(svc, "alice", ALICE_PRS)
    assert fairness.open_agent_prs(svc, "alice") == ALICE_PRS
    held = new_job(svc, "alice")  # first in line
    bobs = new_job(svc, "bob")
    assert await take(svc) == bobs.id
    assert held.state == JobState.QUEUED and held.reason == fairness.AWAITING
    assert any("alice has 5 agent PRs awaiting review" in e.message for e in held.events)
    assert await take(svc) is None  # still held
    # listings filter by the reason; the metric counts it (no person label)
    page, _ = svc.page(reason=fairness.AWAITING)
    assert [j.id for j, _ in page] == [held.id]
    m = Metrics()
    m.collect_state(types.SimpleNamespace(store=svc.store, jobs=svc))
    assert m.awaiting_capacity._value.get() == 1
    # merging one of alice's PRs starts the held job at once
    await handle_github(svc, "pull_request", "d-merge", merged(prs[0].result.pr_number))
    await settle(svc)
    assert fairness.open_agent_prs(svc, "alice") == ALICE_PRS - 1
    assert await take(svc) == held.id
    fairness.admitted(svc, held.id)
    assert held.reason is None and "review capacity is free" in held.events[-1].message


async def test_follow_ups_autofix_reviews_and_admitted_jobs_still_run(team):
    svc = team  # noqa: F811
    open_prs(svc, "alice", ALICE_PRS)
    follow_up = new_job(svc, "alice")
    follow_up.turns.append(Turn(kind="follow_up", message="fix CI"))  # an auto-fix round is a follow-up turn
    assert await take(svc) == follow_up.id
    review = new_job(svc, "alice", source="review")
    assert await take(svc) == review.id
    trial = new_job(svc, "alice", labels={"hangar.experiment": "job-x"})
    assert await take(svc) == trial.id
    held = new_job(svc, "alice")
    assert await take(svc) is None and held.reason == fairness.AWAITING
    fairness.admit(svc, held.id, "ui:admin")
    assert await take(svc) == held.id and held.force_admitted_by == "ui:admin"


async def test_caps_off_per_repo_and_per_person(team, settings):
    svc = team  # noqa: F811
    open_prs(svc, "alice", 2)
    lands(svc, approvers=["lead"], max_open_agent_prs=2)
    held = new_job(svc, "alice")
    assert await take(svc) is None and held.reason == fairness.AWAITING
    settings.person_max_open_agent_prs_overrides = {"alice": 3}  # an admin raised alice's cap
    svc._queue.wake()
    assert await take(svc) == held.id
    settings.person_max_open_agent_prs_overrides = {}
    lands(svc, approvers=["lead"], max_open_agent_prs=0)  # 0: off for the repo
    off = new_job(svc, "alice")
    assert await take(svc) == off.id


async def test_counts_are_recomputed_after_a_restart_and_held_jobs_keep_their_reason(team, fleet):
    svc = team  # noqa: F811
    open_prs(svc, "alice", ALICE_PRS)
    held = new_job(svc, "alice")
    assert await take(svc) is None and held.reason == fairness.AWAITING
    again = JobService(fleet, None, transport=svc.transport)
    again._jobs.adopt(again._load())
    again.review_load.recount(again)
    assert fairness.open_agent_prs(again, "alice") == ALICE_PRS
    restored = again.get(held.id)
    again._queue.put_nowait(restored.id)
    assert await take(again) is None
    assert restored.reason == fairness.AWAITING
    assert sum("awaiting review" in e.message for e in restored.events) == 1  # said once, not again


async def test_what_counts_and_who_it_waits_on(team):
    svc = team  # noqa: F811
    [job] = open_prs(svc, "alice", 1)
    job.landing.pr_state = "approved"  # approved but not landing still needs her
    svc._save(job)
    assert fairness.open_agent_prs(svc, "alice") == 1
    job.landing.pr_state, job.landing.ended = "merged", True
    svc._save(job)
    assert fairness.open_agent_prs(svc, "alice") == 0
    team_job = pr_job(svc)  # nobody's: the repo's team bucket, N x the policy's approvers
    team_job.result.pr_number = 300
    svc._save(team_job)
    assert fairness.open_agent_prs(svc, "team:o/r") == 1
    lands(svc, approvers=["a", "b"])
    assert fairness.pr_cap(svc, team_job) == 2 * ALICE_PRS
    # a PR that needs a second reviewer (#160) also waits on the people linked to the repo's approvers
    second = pr_job(svc, person="alice", requester="alice")
    second.result.pr_number = 301
    second.landing.risk, second.landing.approver_people = "normal", ["bob"]
    svc._save(second)
    assert fairness.open_agent_prs(svc, "bob") == 1 and fairness.open_agent_prs(svc, "alice") == 1
    assert fairness.capacity_view(svc, "alice") == {"open_agent_prs": 1, "cap": ALICE_PRS, "awaiting_capacity": 0}


async def test_the_front_doors_queued_reply_names_review_capacity(team):
    svc = team  # noqa: F811
    open_prs(svc, "alice", ALICE_PRS)
    job = new_job(svc, "alice")
    note = fairness.queued_note(svc, job)
    assert "waits for review capacity" in note and "review-requested" in note
    assert fairness.queued_note(svc, new_job(svc, "bob")) == ""
