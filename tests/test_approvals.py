"""Who may release a held job, on every surface (#121): approvals.can_decide, MCP and REST give the same answer.

Before #121, any token of an admin-role person (an `mcp`-only `hangar connect` token, every legacy token) approved
any job over MCP, while REST wanted an admin token; a member approved their own spend-cap hold; nothing checked
approver != requester at the GitHub or Jira doors.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from hangar import approvals, people
from hangar.auth import Principal
from hangar.jobs import Caller, SubmitJob
from hangar.models import CredentialKind, JobState
from hangar.repopolicy import RepoPolicy
from hangar.triggers import new_approval
from tests.conftest import FakeProvider
from tests.test_identity import _call, _mcp_session

ADMIN_H = {"Authorization": "Bearer t0ken"}
REPO = "https://github.com/o/r"


def _github(request: httpx.Request) -> httpx.Response:
    """GitHub's users-by-id: carol's linked account (id 77) is `carol-gh`."""
    if request.url.host == "api.github.com" and request.url.path == "/user/77":
        return httpx.Response(200, json={"id": 77, "login": "Carol-GH", "type": "User"})
    if request.url.host == "api.github.com":
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.Response(200, json={"ok": True})


@pytest.fixture
def world(settings):
    """A control plane with: the owner (admin token `t0ken`), admin `ada`, members `alice` and `bob`, member `carol`
    who is one of o/r's policy approvers (by her linked GitHub account). Each person has a login token, and an
    `mcp`-only token (what `hangar connect` writes)."""
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost:8787") as client:
        h = app.state.hangar
        h.fleet.providers["docker"] = FakeProvider()
        h.jobs._queue = asyncio.Queue()  # keep approved jobs from running in this HTTP-only test
        h.jobs.transport = httpx.MockTransport(_github)
        h.broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_x")
        h.settings.github_token_credential = "gh"
        tokens: dict[str, dict[str, str]] = {"owner": {"login": "t0ken"}}
        for pid, role in (("ada", "admin"), ("alice", "member"), ("bob", "member"), ("carol", "member")):
            r = client.post("/api/people", json={"id": pid, "role": role}, headers=ADMIN_H).json()
            login = r["login"]["token"]
            mcp = client.post("/api/tokens", json={"name": f"cc-{pid}", "scopes": ["mcp"]},
                              headers={"Authorization": f"Bearer {login}"}).json()["token"]
            tokens[pid] = {"login": login, "mcp": mcp}
        tokens["owner"]["mcp"] = client.post("/api/tokens", json={"name": "cc", "scopes": ["mcp"]},
                                             headers=ADMIN_H).json()["token"]
        people.link(h.store, "carol", "github", "77")
        h.jobs.policies.put(RepoPolicy(repo="o/r", approvers=["carol-gh"]))
        yield {"client": client, "h": h, "tokens": tokens}


def _held(h, person: str | None, requester: str = "github:rando", reason: str = "untrusted") -> str:
    """A job waiting for approval: an outsider's (person None) or a person's own (e.g. a spent daily cap)."""
    return h.jobs.submit(SubmitJob(instructions="x", environment="plain", deliver="none", repo=REPO),
                         Caller(requester, requester, person=person), source="github",
                         approval=new_approval(h.jobs, requester, reason)).id


def _rest(w, token: str, job: str, verb: str = "approve") -> tuple[str, str]:
    r = w["client"].post(f"/api/jobs/{job}/{verb}", headers={"Authorization": f"Bearer {token}"})
    return _outcome(r.status_code, r.json().get("error") or r.json().get("state") or "")


def _mcp(w, token: str, job: str, verb: str = "approve") -> tuple[str, str]:
    res = _call(w["client"], _mcp_session(w["client"], token), f"{verb}_job", {"job": job})
    text = res["content"][0]["text"]
    if res.get("isError"):
        text = text.removeprefix(f"Error executing tool {verb}_job: ")  # the MCP SDK's frame around the message
        return ("not_found" if text.startswith("no job") else "error" if "not waiting" in text else "forbidden"), text
    return "ok", json.loads(text)["state"]


def _outcome(status: int, text: str) -> tuple[str, str]:
    return {200: "ok", 403: "forbidden", 404: "not_found", 400: "error"}.get(status, str(status)), text


# ====================================================================== parity: refusals, same text on both surfaces
@pytest.mark.parametrize("case", ["member_own_spend_cap", "member_someone_elses", "admin_own", "mcp_only_token"])
def test_each_refusal_is_the_same_over_mcp_and_rest(world, case):
    """Acceptance (#121): each of these is refused on both surfaces with the same message, audited, and the job
    keeps waiting."""
    h, t = world["h"], world["tokens"]
    who, token, job_person, requester = {
        # a member approving their own job held by a daily spend cap
        "member_own_spend_cap": ("alice", t["alice"]["login"], "alice", "@alice:login-alice"),
        # a member approving someone else's job
        "member_someone_elses": ("alice", t["alice"]["login"], "bob", "@bob:login-bob"),
        # an admin approving a job whose person is themselves
        "admin_own": ("ada", t["ada"]["login"], "ada", "github:ada-gh"),
        # an admin's mcp-only token (hangar connect), and the owner's legacy one, can't release anything
        "mcp_only_token": ("owner", t["owner"]["mcp"], None, "github:rando"),
    }[case]
    a, b = _held(h, job_person, requester), _held(h, job_person, requester)
    if case == "member_someone_elses":  # a job a member may not see answers as missing (Guard), the same way
        rest, mcp = _rest(world, token, a), _mcp(world, token, b)
        assert rest[0] == mcp[0] == "not_found" and rest[1].replace(a, "J") == mcp[1].replace(b, "J")
        assert h.jobs.get(a).state == h.jobs.get(b).state == JobState.AWAITING_APPROVAL
        # and one they could see (a repo they don't approve for) is refused by the rule, in so many words
        d = approvals.can_decide(h.jobs, approvals.from_principal(h.auth.principal(token)),
                                 approvals.Subject("bob", frozenset({"@bob:login-bob"}), REPO, "job x"))
        assert not d.ok and "only an admin or one of o/r's approvers (repo policy) may" in d.why
        return
    rest, mcp = _rest(world, token, a), _mcp(world, token, b)
    assert rest[0] == mcp[0] == "forbidden", (rest, mcp)
    assert rest[1].replace(a, "J") == mcp[1].replace(b, "J"), (rest, mcp)
    assert h.jobs.get(a).state == h.jobs.get(b).state == JobState.AWAITING_APPROVAL
    refused = [e for e in h.store.audit_tail(50) if e.action == "job.approve.refused" and e.target in (a, b)]
    assert len(refused) == 2 and refused[0].detail["why"] == rest[1].replace(a, refused[0].target or "")
    if case in ("member_own_spend_cap", "admin_own"):
        assert "nobody approves their own" in rest[1] and refused[0].detail["self_approved"] is True
    if case == "mcp_only_token":
        assert "login token" in rest[1]


def test_admins_and_repo_approvers_approve_someone_elses_job_on_both_surfaces(world):
    """Acceptance: an admin-role person, or a repo `approvers` person, approves someone else's job, over MCP and
    REST; the job records who decided and by which rule, and so does the audit entry."""
    h, t = world["h"], world["tokens"]
    for who, rule in (("ada", "admin"), ("owner", "admin"), ("carol", "repo_approver")):
        for surface in (_rest, _mcp):
            job = _held(h, "bob", "@bob:login-bob", "budget: daily cap is spent")
            assert surface(world, t[who]["login"], job) == ("ok", "queued"), (who, surface)
            ap = h.jobs.get(job).approval
            assert (ap.decision, ap.decided_person, ap.rule, ap.self_approved) == ("approved", who, rule, False)
            [audit] = [e for e in h.store.audit_tail(50) if e.action == "job.approve" and e.target == job]
            assert audit.detail | {"requested_by": None} == {"requested_by": None, "decided_person": who,
                                                              "self_approved": False, "rule": rule}
    # carol approves only o/r's: another repo's held job is invisible to her, and REST and MCP agree
    other = h.jobs.submit(SubmitJob(instructions="x", environment="plain", deliver="none",
                                    repo="https://github.com/o/elsewhere"), Caller("b", "b", person="bob"),
                          source="github", approval=new_approval(h.jobs, "b", "budget")).id
    assert _rest(world, t["carol"]["login"], other)[0] == _mcp(world, t["carol"]["login"], other)[0] == "not_found"
    # anyone eligible may reject, their own job included (a rejection releases nothing)
    own = _held(h, "ada", "@ada:login-ada")
    assert _mcp(world, t["ada"]["login"], own, "reject") == ("ok", "cancelled")
    assert h.jobs.get(own).approval.rule == "admin"


def test_service_tokens_and_unknown_jobs(world):
    h = world["h"]
    job = _held(h, None)
    svc, _ = h.auth.create("svc", ["jobs"])
    assert _rest(world, svc, job)[0] == "forbidden"
    assert world["client"].post(f"/api/jobs/{job}/approve").status_code == 401
    assert _rest(world, "t0ken", "nope")[0] == _mcp(world, "t0ken", "nope")[0] == "not_found"
    assert _rest(world, "t0ken", job) == ("ok", "queued")
    assert _rest(world, "t0ken", job)[0] == _mcp(world, "t0ken", job)[0] == "error"  # not waiting any more


# ====================================================================== HANGAR_SELF_APPROVAL
def test_allow_solo_only_while_one_person_exists(settings, fleet):
    """Acceptance: `allow_solo` lets the only person approve their own job, audited `self_approved: true`; with a
    second person it is refused again. `deny` (the default) refuses it, and doctor warns."""
    from hangar.jobs import JobService

    jobs = JobService(fleet, None)
    owner = approvals.from_principal(Principal("laptop", "tok-1", ["admin"], person="owner", role="admin"))
    held = lambda: jobs.submit(SubmitJob(instructions="x", environment="plain", deliver="none"),  # noqa: E731
                               Caller("laptop", "tok-1", person="owner"), source="mcp",
                               approval=new_approval(jobs, "laptop", "budget: daily cap is spent"))
    j = held()
    with pytest.raises(approvals.ApprovalRefused, match="nobody approves their own"):
        approvals.decide_now(jobs, j.id, owner, "approve")
    h = type("H", (), {"settings": settings, "store": jobs.store})()
    [warn] = approvals.doctor_checks(h)
    assert warn["status"] == "warn" and "allow_solo" in warn["fix"]
    settings.self_approval = "allow_solo"
    assert approvals.doctor_checks(h) == []
    approvals.decide_now(jobs, j.id, owner, "approve")
    assert (j.approval.self_approved, j.approval.rule, j.state) == (True, "solo", JobState.QUEUED)
    [audit] = [e for e in jobs.store.audit_tail(20) if e.action == "job.approve"]
    assert audit.detail["self_approved"] is True and audit.detail["rule"] == "solo"
    assert any("self-approved" in e.message for e in j.events)
    people.add(jobs.store, "ada", role="admin")  # a second person: ask them
    with pytest.raises(approvals.ApprovalRefused, match="nobody approves their own"):
        approvals.decide_now(jobs, held().id, owner, "approve")


# ====================================================================== the rule itself (what #130 builds on)
def test_can_decide_rules(settings, store):
    ctx = type("Ctx", (), {"settings": settings, "store": store})()
    people.add(store, "alice")
    people.add(store, "ada", role="admin")
    team = approvals.Subject(None, frozenset({"github:rando"}), REPO)
    alices = approvals.Subject("alice", frozenset({"@alice:laptop"}), REPO)
    ada = approvals.Approver("@ada:x", "ada", "mcp", frozenset({"@ada:x"}))
    alice = approvals.Approver("@alice:laptop", "alice", "mcp", frozenset({"@alice:laptop"}))
    assert approvals.can_decide(ctx, ada, team) == approvals.Decision(True, "", "admin")
    assert approvals.can_decide(ctx, ada, alices).ok  # someone else's
    assert not approvals.can_decide(ctx, alice, team).ok  # not an approver
    assert not approvals.can_decide(ctx, alice, alices).ok
    # a front door's own trust counts on team jobs only, never for a person's own held work
    door = approvals.from_github("rando2", None, trusted=True)
    assert approvals.can_decide(ctx, door, team).rule == "front_door"
    assert not approvals.can_decide(ctx, door, alices).ok
    assert not approvals.can_decide(ctx, approvals.from_github("rando", None, True), team).ok  # the requester
    # the requester via a linked account: ada's linked GitHub account opened it
    people.link(store, "ada", "github", "5")
    assert not approvals.can_decide(ctx, ada, approvals.Subject(None, frozenset({"github:5"}), REPO)).ok
    # every kind but reject gets the self-approval rule (#130: egress, land)
    for kind in ("egress", "land"):
        assert not approvals.can_decide(ctx, ada, approvals.Subject("ada", frozenset(), REPO), kind).ok, kind
    assert approvals.can_decide(ctx, ada, approvals.Subject("ada", frozenset(), REPO), "reject").ok
    assert approvals.can_decide(ctx, ada, team, "land", risk="low").ok  # risk: accepted, reserved (#160)
    # in-process callers (hangar's own code) decide as the system
    assert approvals.can_decide(ctx, approvals.in_process("svc"), alices).rule == "system"
    # a removed person's account decides nothing
    gone = approvals.Approver("jira:zed", "zed", "jira", front_door=True)
    assert "no longer exists" in approvals.can_decide(ctx, gone, team).why
