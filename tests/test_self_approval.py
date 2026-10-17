"""The self-approval rule (#160): a job's own person may be its PR's only approver only for a low-risk PR with an
independent AI review of the approved head (`self_approval: low_risk_with_ai_review`, the default); never for a
tainted one (the taint gate, #130, comes first and isn't loosened); `any` is the old behaviour."""

from __future__ import annotations

from hangar import aireview, inbox
from hangar.auth import Principal
from hangar.jobs import handle_github
from tests.test_landing import GH, HEAD, NEW, approval, fx, gh, green, lands, pr_job, settle, sub, svc  # noqa: F401
from tests.test_taint_gates import TAINT, link_github

ALICE, BOB = 1294, 4242  # alice's id is the recorded review's user id


def review(job, sha: str = HEAD, verdict: str = "approve", independent: bool = True) -> None:
    job.landing.ai_review = {"source": "hangar", "job": "job-review", "harness": "codex", "model": "gpt-6-astra",
                             "head_sha": sha, "verdict": verdict, "independent": independent, "findings": []}


def bob_approves(gh: GH, sha: str = HEAD) -> dict:  # noqa: F811
    p = approval(sha=sha, login="bob", gh=gh)
    p["review"]["user"]["id"] = BOB
    return p


def setup(svc, **policy):  # noqa: F811
    lands(svc, approvers=["alice", "bob"], **policy)
    link_github(svc, "alice", ALICE)
    link_github(svc, "bob", BOB)


def principal(person: str) -> Principal:
    return Principal(id=f"tok-{person}", name=person, scopes=["user"], person=person, role="member")


async def test_the_requesters_approval_of_a_normal_pr_needs_a_second_reviewer(svc, gh):  # noqa: F811
    setup(svc)
    job = pr_job(svc, person="alice")
    job.landing.risk = "normal"
    gh.users = {"alice": ALICE, "bob": BOB}
    await svc.landing.resolve_approvers(job, svc.policies.get(job.repo))
    assert job.landing.approver_people == ["bob"]
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert not gh.enables() and job.landing.pr_state == "open"
    assert "self-approval doesn't land this PR (risk normal): it needs a second reviewer" in job.landing.note
    assert any("self-approval doesn't land this PR (risk normal)" in e.message for e in job.events)
    assert [a.detail["decision"] for a in svc.store.audit_tail(30) if a.action == "landing.self_approval"] == \
        ["refused"]
    # the repo's other approver gets a review item; alice doesn't get one for her own PR
    [item] = [i for i in inbox.items(svc, principal("bob")) if "own approval doesn't land" in i["reason"]]
    assert item["kind"] == "review" and item["job"] == job.id
    assert not [i for i in inbox.items(svc, principal("alice")) if "own approval doesn't land" in i["reason"]]
    await handle_github(svc, "pull_request_review", "d-2", bob_approves(gh))
    await settle(svc)
    assert gh.enables() == [HEAD] and job.landing.approved_by == "bob" and not job.landing.needs_second


async def test_a_low_risk_pr_lands_on_its_requester_with_an_ai_review_of_that_head(svc, gh):  # noqa: F811
    setup(svc)
    job = pr_job(svc, person="alice")
    job.landing.risk = "low"
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))  # no AI review yet
    await settle(svc)
    assert not gh.enables() and "no independent AI review of 6dcb09b yet" in job.landing.note
    review(job)  # the review comes in: the approval it held is looked at again
    await aireview._settled(svc, job.id)
    await settle(svc)
    assert gh.enables() == [HEAD] and job.landing.approved_by == "alice"


async def test_a_blocking_or_dependent_ai_review_doesnt_count(svc, gh):  # noqa: F811
    setup(svc)
    job = pr_job(svc, person="alice")
    job.landing.risk = "low"
    await green(svc)
    for verdict, independent, why in (("blocking", True, "is blocking"), ("approve", False, "author's own harness")):
        review(job, verdict=verdict, independent=independent)
        await handle_github(svc, "pull_request_review", f"d-{verdict}", approval(gh=gh))
        await settle(svc)
        assert not gh.enables() and why in job.landing.note


async def test_an_ai_review_of_an_older_head_stops_counting_after_a_push(svc, gh):  # noqa: F811
    setup(svc)
    job = pr_job(svc, person="alice")
    job.landing.risk = "low"
    review(job, sha=HEAD)
    await handle_github(svc, "pull_request", "d-push", fx("pull_request.synchronize"))  # the head is now NEW
    assert job.landing.head_sha == NEW
    gh.head = lambda: NEW
    await green(svc, NEW)
    await handle_github(svc, "pull_request_review", "d-1", approval(sha=NEW, gh=gh))
    await settle(svc)
    assert not gh.enables() and "no independent AI review of 9c3f2a1" in job.landing.note


async def test_self_approval_any_restores_the_old_behaviour_and_never_refuses_always(svc, gh):  # noqa: F811
    setup(svc, self_approval="any")
    job = pr_job(svc, person="alice")
    job.landing.risk = "normal"
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert gh.enables() == [HEAD] and job.landing.approved_by == "alice"
    assert svc.landing._self_approval(job, "alice", HEAD) is None
    setup(svc, self_approval="never")
    job.landing.risk = "low"
    review(job)
    assert "self_approval is never" in svc.landing._self_approval(job, "alice", HEAD)


async def test_it_never_loosens_the_taint_gate(svc, gh):  # noqa: F811
    """A tainted low-risk PR with an independent AI review still needs someone other than its person (#130)."""
    setup(svc, self_approval="any")
    job = pr_job(svc, person="alice", taint=dict(TAINT))
    job.landing.risk = "low"
    review(job)
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert not gh.enables() and any("read untrusted content" in e.message for e in job.events)
    assert svc.landing.needs_second_reviewer(job)
    assert "untrusted content" in svc.landing.landing_rule(job, svc.policies.get(job.repo))


async def test_team_jobs_are_unchanged_and_the_packet_states_the_rule(svc, gh):  # noqa: F811
    setup(svc)
    team = pr_job(svc)  # nobody's job: alice is just an approver
    team.landing.risk = "normal"
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert gh.enables() == [HEAD]
    mine = pr_job(svc, person="alice")
    mine.landing.risk = "low"
    assert "once an independent AI review of this head isn't blocking" in svc.landing.packet(mine)
    review(mine)
    assert "may approve it alone" in svc.landing.packet(mine)
    assert "AI review:** approve (codex · gpt-6-astra) on 6dcb09b" in svc.landing.packet(mine)
    mine.landing.risk = "normal"
    assert "a second reviewer: risk normal" in svc.landing.packet(mine)
