"""Job release and landing ask Tower (KAN-33, hangar#172, spec 0015): shadow records both answers, enforce lets Tower
decide and fails closed, and hangar's own rule always runs first.

Jobs are held as an outsider's `/hangar` on GitHub holds them (tests/test_triggers.py), and decided with
`/hangar approve|reject` comments; Tower is a fake decision point whose answers each test sets."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable

import httpx
import pytest

import tests.test_jobs as tj
from hangar import approvals, people
from hangar.governance import Decision, DecisionRequest, HttpDecisionPoint, StaticCredential
from hangar.govrelease import LAND, REL, ApprovalPending, Releases
from hangar.jobs import JobService, SubmitJob, handle_github
from hangar.models import JobState
from hangar.triggers import new_approval
from tests.test_triggers import GH, _comment, _issue

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

world = tj.world


class FakeTower:
    """Tower's decision API: `answer(req)` decides each request; status reads come from `decisions`."""

    def __init__(self):
        self.asked: list[DecisionRequest] = []
        self.decisions: dict[str, Decision] = {}
        self.withdrawn: list[tuple[str, str]] = []
        self.answer: Callable[[DecisionRequest], Decision] = lambda req: Decision(
            outcome="wait", approval_id="A-1", status="waiting", reason="Waiting for Dana or Lee to sign A-1")

    async def decide(self, req: DecisionRequest) -> Decision:
        self.asked.append(req)
        return self.answer(req)

    async def status(self, approval_id: str, *, kind) -> Decision:
        return self.decisions.get(approval_id, Decision(outcome="wait", approval_id=approval_id, status="waiting"))

    async def withdraw(self, approval_id: str, reason: str) -> bool:
        self.withdrawn.append((approval_id, reason))
        return True


class Receiver:
    """govevents.Receiver as Releases uses it: Tower, and the `releases` back-reference."""

    def __init__(self, tower):
        self._tower = tower
        self.releases = None

    def tower(self):
        return self._tower


def unreachable() -> HttpDecisionPoint:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return HttpDecisionPoint("https://tower.test", StaticCredential("k"),
                             client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))


@pytest.fixture
def tower():
    return FakeTower()


@pytest.fixture
def intake(fleet, world, broker):
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    gh = GH()
    svc = JobService(fleet, None, transport=httpx.MockTransport(gh))
    svc.hooks = gh
    tj._gh_settings(svc, broker)
    return svc


@pytest.fixture
def rel(intake, tower):
    return Releases(intake, Receiver(tower))


def mode(svc: JobService, m: str, kind: str = "job.release") -> None:
    svc.settings.governance = "off"
    svc.settings.governance_modes = f"{kind}={m}"
    svc.settings.governance_url = "https://tower.test"


async def held(svc: JobService, n: int) -> str:
    res = await handle_github(svc, "issue_comment", f"d-{n}", _comment(_issue(n), "/hangar", "rando", "NONE"))
    return res["job"]


async def comment(svc: JobService, n: int, text: str, login: str = "sean", assoc: str = "OWNER") -> dict:
    return await handle_github(svc, "issue_comment", f"d-{n}-{text}-{login}", _comment(_issue(n), text, login, assoc))


def audits(svc, action: str) -> list:
    return [e for e in svc.store.audit_tail(200) if e.action == action]


# ====================================================================== shadow
async def test_shadow_release_records_both_answers_and_never_changes_the_outcome(intake, rel, tower):
    mode(intake, "shadow")
    job_id = await held(intake, 51)
    assert await comment(intake, 51, "/hangar approve") == {"ok": True, "approved": job_id}
    await rel.drain()
    job = intake.get(job_id)
    assert job.state == JobState.QUEUED and job.approval.decided_by == "github:sean"  # hangar decided
    [req] = tower.asked
    assert req.dry_run and req.kind == "job.release" and req.idempotency_key == f"hangar:job:{job_id}:release"
    assert (req.signer.via, req.signer.verdict, req.signer.external) == ("github", "approve", "github:sean")
    assert req.requester.external == "github:rando" and req.context.untrusted_source == "github:rando"
    g = job.approval.governance
    assert (g["mode"], g["decided_by"], g["hangar"]["outcome"], g["tower"]["outcome"]) == ("shadow", "hangar",
                                                                                         "allow", "wait")
    [d] = audits(intake, "governance.disagree")
    assert d.target == job_id and (d.detail["hangar"], d.detail["tower"]) == ("allow", "wait")
    assert job.approval.receipt is None and not intake.store.kv_keys(REL)  # nothing opened in Tower


async def test_shadow_release_with_tower_down_still_releases(intake, tower, world):
    mode(intake, "shadow")
    rel = Releases(intake, Receiver(unreachable()))
    job_id = await held(intake, 52)
    await comment(intake, 52, "/hangar approve")
    await rel.drain()
    job = intake.get(job_id)
    assert job.state == JobState.QUEUED
    g = job.approval.governance
    assert g["tower"]["failure"] == "unreachable" and "hangar's own policy decided" in g["fallback"]
    assert audits(intake, "governance.disagree") == []


# ====================================================================== enforce: a person approves in hangar
async def test_enforce_release_needs_towers_allow_and_keeps_the_receipt(intake, rel, tower):
    mode(intake, "enforce")
    tower.answer = lambda req: Decision(outcome="allow", approval_id="A-123", receipt="RC-104", status="approved",
                                        rule="release-untrusted-job", reason="Signed by Sean (A-123).")
    job_id = await held(intake, 53)
    assert await comment(intake, 53, "/hangar approve") == {"ok": True, "approved": job_id}
    job = intake.get(job_id)
    assert job.state == JobState.QUEUED
    assert (job.approval.receipt, job.approval.workspace_approval_id) == ("RC-104", "A-123")
    assert job.approval.governance["decided_by"] == "tower"
    [req] = tower.asked
    assert not req.dry_run and req.signer.external == "github:sean"
    await asyncio.sleep(0.05)
    assert any("Tower A-123, receipt RC-104" in c for c in intake.hooks.comments())
    [a] = audits(intake, "governance.release.allow")
    assert a.detail["receipt"] == "RC-104"


async def test_enforce_release_deny_rejects_with_towers_reason_on_the_issue(intake, rel, tower):
    mode(intake, "enforce")
    tower.answer = lambda req: Decision(outcome="deny", status="blocked", rule="no-outsiders",
                                        reason="outside contributors can't run jobs on o/r")
    job_id = await held(intake, 54)
    await comment(intake, 54, "/hangar approve")
    job = intake.get(job_id)
    assert job.state == JobState.CANCELLED and job.reason == "rejected"
    assert job.approval.decided_by == "tower" and job.approval.rule == "tower"
    await asyncio.sleep(0.05)
    assert any("Rejected by tower: outside contributors can't run jobs on o/r" in c for c in intake.hooks.comments())


async def test_enforce_wait_keeps_the_job_waiting_until_tower_settles_it(intake, rel, tower):
    mode(intake, "enforce")
    job_id = await held(intake, 55)
    res = await comment(intake, 55, "/hangar approve")
    assert res["waiting"] == job_id and "A-1" in res["reason"]
    job = intake.get(job_id)
    assert job.state == JobState.AWAITING_APPROVAL and job.approval.workspace_approval_id == "A-1"
    assert intake.store.kv_get(REL + "A-1") == job_id
    assert await rel.hint("A-1") == {"ok": True, "approval": "A-1", "status": "waiting"}
    tower.decisions["A-1"] = Decision(outcome="allow", approval_id="A-1", receipt="RC-7", status="approved",
                                      reason="Signed by Dana (A-1).")
    out = await rel.hint("A-1", "dana@acme.test")
    assert out["state"] == "queued" and job.state == JobState.QUEUED
    assert (job.approval.decided_by, job.approval.receipt) == ("tower:dana@acme.test", "RC-7")
    assert intake.store.kv_get(REL + "A-1") is None


async def test_a_refused_signature_leaves_the_job_waiting(intake, rel, tower):
    """Tower denies the signature (the signer isn't a maintainer) while its approval still waits."""
    mode(intake, "enforce")
    tower.answer = lambda req: Decision(outcome="deny", approval_id="A-3", status="denied",
                                        reason="Sean can't sign A-3: not a maintainer")
    job_id = await held(intake, 56)
    res = await comment(intake, 56, "/hangar approve")
    assert "Tower refused github:sean's signature" in res["ignored"]
    job = intake.get(job_id)
    assert job.state == JobState.AWAITING_APPROVAL and job.approval.workspace_approval_id == "A-3"
    assert [e.detail["why"] for e in audits(intake, "job.approve.refused")] == [res["ignored"]]


async def test_tower_down_fails_closed_and_says_so_once(intake, world):
    mode(intake, "enforce")
    rel = Releases(intake, Receiver(unreachable()))
    job_id = await held(intake, 57)
    for login in ("sean", "lee"):
        res = await comment(intake, 57, "/hangar approve", login)
        assert res["waiting"] == job_id and "approval service unavailable" in res["reason"]
    job = intake.get(job_id)
    assert job.state == JobState.AWAITING_APPROVAL and job.approval.receipt is None
    assert job.approval.governance["decided_by"] == "fail_closed"
    await asyncio.sleep(0.05)
    said = [c for c in intake.hooks.comments() if "approval service unavailable" in c]
    assert len(said) == 1 and "Nothing was allowed" in said[0]
    with pytest.raises(ApprovalPending):  # REST and MCP get the same answer
        await approvals.decide(intake, job_id, approvals.in_process("ops"), "approve")
    await rel.stop()


async def test_self_approval_is_refused_before_tower_is_asked(intake, rel, tower):
    mode(intake, "enforce")
    people.add(intake.store, "dana")
    people.link(intake.store, "dana", "github", "4242")
    job = intake.submit(SubmitJob(instructions="x", environment="jobenv", deliver="none"),
                        tj.Caller("github:dana", "github:dana", person="dana"), source="github",
                        source_ref="github:o/r#58", approval=new_approval(intake, "github:dana", "budget: cap spent"))
    body = _comment(_issue(58), "/hangar approve", "dana", "MEMBER")
    body["comment"]["user"]["id"] = 4242
    res = await handle_github(intake, "issue_comment", "d-58", body)
    assert "nobody approves their own" in res["ignored"] and job.state == JobState.AWAITING_APPROVAL
    assert tower.asked == []


async def test_a_rejection_in_hangar_goes_through_and_withdraws_towers_approval(intake, rel, tower):
    mode(intake, "enforce")
    job_id = await held(intake, 59)
    await rel.tick()  # the job entered awaiting_approval: Tower's approval opens (no signer)
    [opened] = tower.asked
    assert opened.signer is None and intake.get(job_id).approval.workspace_approval_id == "A-1"
    assert await comment(intake, 59, "/hangar reject") == {"ok": True, "rejected": job_id}
    await rel.drain()
    assert intake.get(job_id).state == JobState.CANCELLED and len(tower.asked) == 1
    assert tower.withdrawn == [("A-1", "rejected in hangar by github:sean")]
    assert intake.store.kv_get(REL + "A-1") is None


async def test_the_poll_settles_a_deny_in_tower_and_withdraws_what_nobody_needs(intake, rel, tower):
    mode(intake, "enforce")
    a, b = await held(intake, 60), await held(intake, 61)
    tower.answer = lambda req: Decision(outcome="wait", approval_id=f"A-{req.subject.job}", status="waiting")
    await rel.tick(1000.0)
    tower.decisions[f"A-{a}"] = Decision(outcome="deny", approval_id=f"A-{a}", status="denied",
                                         reason="Dana denied it.")
    intake.cancel(b, requester="sean")
    await rel.tick(1000.0 + 61)
    await rel.drain()
    assert intake.get(a).state == JobState.CANCELLED and intake.get(a).approval.decided_by == "tower"
    assert [w[0] for w in tower.withdrawn] == [f"A-{b}"]
    assert intake.store.kv_keys(REL) == []


async def test_off_never_asks_tower(intake, rel, tower):
    mode(intake, "off")
    job_id = await held(intake, 62)
    await rel.tick()
    assert await comment(intake, 62, "/hangar approve") == {"ok": True, "approved": job_id}
    assert tower.asked == [] and intake.get(job_id).approval.governance is None


# ====================================================================== landing
def approved(svc: JobService, sha: str = "a" * 40):
    job = svc.submit(SubmitJob(instructions="Fix the flaky retry test\nmore", environment="jobenv", deliver="none",
                               repo="https://github.com/o/r"), tj.Caller("sean", "sean"), source="api")
    job.result.pr_number, job.result.base_sha = 7, "b" * 40
    st = job.landing
    st.pr_state, st.approved_by, st.approved_sha, st.head_sha = "approved", "lee", sha, sha
    st.files, st.risk = ["tests/test_client.py"], "low"
    st.checks_sha, st.checks = sha, {"ci": "success"}
    svc.landing._login_ids["lee"] = 31337
    return job


async def test_landing_in_enforce_arms_only_on_towers_allow(intake, rel, tower):
    mode(intake, "enforce", "landing.approve")
    job = approved(intake)
    note = await rel.landing(job)
    assert note == "waiting for approval A-1 in Tower: Waiting for Dana or Lee to sign A-1"
    [req] = tower.asked
    assert req.kind == "landing.approve" and req.idempotency_key == f"hangar:job:{job.id}:landing:{'a' * 40}"
    assert (req.subject.pr, req.subject.head_sha, req.subject.repo) == (7, "a" * 40, "o/r")
    assert req.signer.external == "github:31337" and req.context.risk == "low"
    assert [c.name for c in req.context.checks] == ["ci"]
    assert intake.store.kv_get(LAND + "A-1") == job.id and job.landing.receipt is None
    tower.decisions["A-1"] = Decision(outcome="allow", approval_id="A-1", receipt="RC-55", status="approved")
    out = await rel.hint("A-1")
    assert out["landing"] == "allowed" and (job.landing.receipt, job.landing.receipt_sha) == ("RC-55", "a" * 40)
    assert await rel.landing(job) is None and len(tower.asked) == 1  # the receipt covers this head
    assert "receipt `RC-55`" in intake.landing.packet(job)


async def test_landing_deny_and_tower_down_never_arm(intake, rel, tower, world):
    mode(intake, "enforce", "landing.approve")
    tower.answer = lambda req: Decision(outcome="deny", status="blocked", rule="no-friday", reason="not on Fridays")
    job = approved(intake)
    assert await rel.landing(job) == "Tower refused it (no-friday): not on Fridays"
    down = Releases(intake, Receiver(unreachable()))
    job2 = approved(intake, "c" * 40)
    note = await down.landing(job2)
    assert note.startswith("approval service unavailable (unreachable)") and job2.landing.receipt is None


async def test_a_new_head_withdraws_the_old_landing_approval(intake, rel, tower):
    mode(intake, "enforce", "landing.approve")
    job = approved(intake)
    await rel.landing(job)
    st = job.landing
    st.approved_sha = st.head_sha = "d" * 40  # pushed, and approved again
    tower.answer = lambda req: Decision(outcome="wait", approval_id="A-2", status="waiting", reason="sign A-2")
    assert await rel.landing(job) == "waiting for approval A-2 in Tower: sign A-2"
    await rel.drain()
    assert tower.withdrawn == [("A-1", "a new head replaced the one it was asked about")]
    assert intake.store.kv_keys(LAND) == [LAND + "A-2"]


async def test_landing_in_shadow_records_both_and_lands_as_before(intake, rel, tower):
    mode(intake, "shadow", "landing.approve")
    job = approved(intake)
    assert await rel.landing(job) is None
    assert await rel.landing(job) is None and len(tower.asked) == 1  # once per approved head
    [req] = tower.asked
    assert req.dry_run
    g = job.landing.governance
    assert (g["hangar"]["outcome"], g["tower"]["outcome"], g["sha"]) == ("allow", "wait", "a" * 40)
    assert [e.target for e in audits(intake, "governance.disagree")] == [job.id]
