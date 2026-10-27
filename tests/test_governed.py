"""The `governed` approval policy (KAN-79, spec 0015): flagged tool calls, egress and secret requests ask Tower.

Tool calls run real turns under the real driver with the scripted Claude Code of tests/test_steer.py (FORCEPUSH is
a call hangar's rules flag; TOOL and READFILE are calls they don't), against a fake Tower whose answers the test
sets. Requests are rows the agent would have filed, relayed as envrequests._scan relays them."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx
import pytest

from hangar import driver
from hangar import steer as steering
from hangar.envrequests import EnvRequest, EnvRequests, RequestError
from hangar.governance import Decision, DecisionRequest, HttpDecisionPoint, StaticCredential
from hangar.governed import REQ, Governed
from hangar.govevents import Receiver
from hangar.harnesses import HARNESSES
from hangar.models import SessionState, SessionTurn
from tests.test_govevents import FakeTower as EventsTower
from tests.test_govevents import audits, configure, event
from tests.test_steer import live, new, pending_approval, repo_file, settle, svc  # noqa: F401

PUSH = {"command": "git push --force origin HEAD"}


class FakeTower(EventsTower):
    """Tower's decision API: `answer(req)` decides each request; status reads come from `decisions`."""

    def __init__(self, answer: Callable[[DecisionRequest], Decision] | None = None):
        super().__init__()
        self.asked: list[DecisionRequest] = []
        self.answer = answer or (lambda req: Decision(outcome="wait", approval_id="A-1", status="waiting"))

    async def decide(self, req: DecisionRequest) -> Decision:
        self.asked.append(req)
        return self.answer(req)


def allow(receipt: str = "RC-9") -> Callable[[DecisionRequest], Decision]:
    return lambda req: Decision(outcome="allow", approval_id="A-9", receipt=receipt, status="allowed",
                                rule="push-ok", reason="Pushes to this repo are fine.")


def deny(reason: str) -> Callable[[DecisionRequest], Decision]:
    return lambda req: Decision(outcome="deny", approval_id="A-2", status="blocked", rule="no-force-push",
                                reason=reason)


@pytest.fixture
def tower():
    return FakeTower()


@pytest.fixture
def gov(svc, settings, broker, tower):  # noqa: F811
    configure(settings, broker)  # HANGAR_GOVERNANCE=enforce and Tower's webhook secret
    return Governed(svc, Receiver(svc, tower=tower), EnvRequests(svc.fleet, svc, None))


def unreachable() -> HttpDecisionPoint:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return HttpDecisionPoint("https://tower.test", StaticCredential("k"),
                             client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))


async def until(cond: Callable[[], object], timeout: float = 20.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def asked(svc, name: str):  # noqa: F811
    """The session's first held call, once hangar has relayed it and recorded the answer."""
    a = await pending_approval(svc, name)
    await until(lambda: (a.governance or {}).get("outcome"))
    return svc.get(name), a


# ====================================================================== tool calls, enforce
async def test_a_force_push_is_denied_with_towers_reason(svc, live, gov, tower):  # noqa: F811
    tower.answer = deny("force-push needs a maintainer")
    new(svc, live, "gv-deny", "FORCEPUSH clean up history", approvals="governed")
    s = await settle(svc, "gv-deny")
    t = s.turns[0]
    assert t.approval_policy == "governed" and t.outcome == "exited"  # the turn carries on without the call
    assert not repo_file(svc, s, "tool-ran.txt").exists()
    [a] = t.approvals
    assert (a.status, a.by) == ("denied", "tower")
    assert a.message == "Denied by Tower (A-2): force-push needs a maintainer"
    assert a.governance["decided_by"] == "tower" and a.governance["rule"] == "no-force-push"
    [req] = tower.asked
    assert req.kind == "tool.call" and req.idempotency_key == f"hangar:session:{s.id}:tool:{a.id}"
    assert req.action.tool == "Bash" and req.action.args_hash == driver.args_hash(PUSH)
    assert req.action.risky == "force-push or remote branch deletion" and req.context.tainted is False
    assert (req.correlation.session, req.correlation.turn, req.correlation.run) == (s.id, 1, t.run_id)
    assert req.subject.environment == "liveenv" and not req.dry_run


@pytest.mark.parametrize("message", ["READFILE look at the readme", "TOOL make the file"])
async def test_calls_hangars_rules_dont_flag_never_reach_tower(svc, live, gov, tower, message):  # noqa: F811
    new(svc, live, "gv-plain", message, approvals="governed")
    s = await settle(svc, "gv-plain")
    assert [(a.status, a.by, a.risky) for a in s.turns[0].approvals] == [("allowed", "policy", None)]
    assert repo_file(svc, s, "tool-ran.txt").exists() and tower.asked == []


async def test_an_allow_answers_the_call_with_its_receipt(svc, live, gov, tower):  # noqa: F811
    tower.answer = allow("RC-104")
    new(svc, live, "gv-allow", "FORCEPUSH clean up history", approvals="governed")
    s = await settle(svc, "gv-allow")
    [a] = s.turns[0].approvals
    assert (a.status, a.by, a.receipt, a.workspace_approval) == ("allowed", "tower", "RC-104", "A-9")
    assert repo_file(svc, s, "tool-ran.txt").exists()
    assert [e.detail["receipt"] for e in audits(svc, "governance.tool.allow")] == ["RC-104"]


async def test_wait_suspends_then_resumes_through_the_receiver(svc, live, gov, tower):  # noqa: F811
    tower.answer = lambda req: Decision(outcome="wait", approval_id="A-7", status="waiting",
                                        reason="Waiting for Dana to sign A-7.")
    new(svc, live, "gv-wait", "FORCEPUSH clean up history", approvals="governed")
    s, a = await asked(svc, "gv-wait")
    w = s.governance
    assert w is not None and (w.approval, w.state, w.args_hash) == ("A-7", "waiting", driver.args_hash(PUSH))
    assert a.workspace_approval == "A-7" and a.status == "pending"
    w.since -= gov.settings.governance_suspend_after_seconds + 1  # past suspend_after: the slot is released
    await gov.receiver.tick()
    await gov.receiver.drain()
    s = await settle(svc, "gv-wait")
    assert w.state == "suspended" and s.state == SessionState.SUSPENDED and s.sandbox_id is None

    tower.allow("A-7", "RC-7")
    code, out = await gov.receiver.receive(*event("approval.decided", "A-7"))
    assert code == 200 and out.get("resumed") == "gv-wait", out
    s = await settle(svc, "gv-wait")
    t2 = s.turns[1]
    assert [(x.status, x.by, x.receipt) for x in t2.approvals] == [("allowed", "tower", "RC-7")]  # pre-cleared
    assert repo_file(svc, s, "tool-ran.txt").exists() and len(tower.asked) == 1  # not asked again


async def test_a_person_allowing_in_hangar_is_a_signature_tower_judges(svc, live, gov, tower):  # noqa: F811
    tower.answer = lambda req: (deny("you may not sign your own work")(req) if req.signer is not None else
                                Decision(outcome="wait", approval_id="A-8", status="waiting"))
    new(svc, live, "gv-sign", "FORCEPUSH clean up history", approvals="governed")
    s, a = await asked(svc, "gv-sign")
    with pytest.raises(steering.SteerError, match="Tower refused it: you may not sign your own work"):
        await steering.answer(svc, s, a.id, "allow", "sean")
    with pytest.raises(steering.SteerError, match="allow_always"):
        await steering.answer(svc, s, a.id, "allow_always", "sean")
    assert tower.asked[-1].signer.verdict == "approve" and tower.asked[-1].signer.external == "sean"
    await steering.answer(svc, s, a.id, "deny", "sean", "not today")  # a no always goes through
    await gov.drain()
    s = await settle(svc, "gv-sign")
    assert (s.turns[0].approvals[0].status, s.turns[0].approvals[0].by) == ("denied", "sean")
    assert "A-8" in tower.withdrawn and not repo_file(svc, s, "tool-ran.txt").exists()


# ====================================================================== shadow, off, and Tower down
async def test_shadow_records_both_answers_and_a_person_decides(svc, live, gov, tower, settings):  # noqa: F811
    settings.governance = "shadow"
    tower.answer = lambda req: Decision(outcome="deny", status="blocked", rule="no-force-push",
                                        reason="force-push needs a maintainer")
    new(svc, live, "gv-shadow", "FORCEPUSH clean up history", approvals="governed")
    s, a = await asked(svc, "gv-shadow")
    g = a.governance
    assert (g["mode"], g["decided_by"], g["outcome"]) == ("shadow", "hangar", "wait")
    assert g["hangar"]["outcome"] == "wait" and g["tower"] == {
        "outcome": "deny", "reason": "force-push needs a maintainer", "rule": "no-force-push", "status": "blocked"}
    assert tower.asked[0].dry_run and a.status == "pending"  # Tower changed nothing
    [d] = audits(svc, "governance.disagree")
    assert (d.detail["hangar"], d.detail["tower"]) == ("wait", "deny")
    await steering.answer(svc, s, a.id, "allow", "ana")  # hangar's own policy: a person answers
    s = await settle(svc, "gv-shadow")
    assert s.turns[0].approvals[0].by == "ana" and repo_file(svc, s, "tool-ran.txt").exists()
    assert len(tower.asked) == 1  # a person's answer isn't a signature in shadow


async def test_off_asks_a_person_and_not_tower(svc, live, gov, tower, settings):  # noqa: F811
    settings.governance = "off"
    new(svc, live, "gv-off", "FORCEPUSH clean up history", approvals="governed")
    s, a = await asked(svc, "gv-off")
    assert a.governance["mode"] == "off" and a.status == "pending" and tower.asked == []
    await steering.answer(svc, s, a.id, "deny", "ana")
    await settle(svc, "gv-off")


async def test_tower_unreachable_in_enforce_denies_with_the_slo_line(svc, live, gov, caplog):  # noqa: F811
    gov.receiver._tower = unreachable()
    caplog.set_level(logging.INFO, logger="hangar.slo")
    new(svc, live, "gv-down", "FORCEPUSH clean up history", approvals="governed")
    s = await settle(svc, "gv-down")
    [a] = s.turns[0].approvals
    assert (a.status, a.by) == ("denied", "tower") and "refused (fail-closed)" in a.message
    assert a.governance["decided_by"] == "fail_closed" and a.governance["failure"] == "unreachable"
    assert s.turns[0].outcome == "exited" and not repo_file(svc, s, "tool-ran.txt").exists()
    assert "event=fail_closed kind=tool.call failure=unreachable" in caplog.text


async def test_tower_unreachable_in_shadow_falls_back_to_hangars_policy(svc, live, gov, settings):  # noqa: F811
    settings.governance = "shadow"
    gov.receiver._tower = unreachable()
    new(svc, live, "gv-fall", "FORCEPUSH clean up history", approvals="governed")
    s, a = await asked(svc, "gv-fall")
    g = a.governance
    assert g["decided_by"] == "hangar" and g["tower"]["failure"] == "unreachable" and "fallback" in g
    assert a.status == "pending"  # still a person's to answer
    await steering.answer(svc, s, a.id, "allow", "ana")
    s = await settle(svc, "gv-fall")
    assert repo_file(svc, s, "tool-ran.txt").exists()


async def test_tower_not_configured_in_enforce_fails_closed(svc, live, settings, broker):  # noqa: F811
    configure(settings, broker)
    Governed(svc, Receiver(svc), None)  # no HANGAR_GOVERNANCE_URL or credential
    new(svc, live, "gv-nourl", "FORCEPUSH clean up history", approvals="governed")
    s = await settle(svc, "gv-nourl")
    [a] = s.turns[0].approvals
    assert a.status == "denied" and "HANGAR_GOVERNANCE_URL" in a.message


# ====================================================================== egress and secret requests
def filed(svc, kind: str = "egress", **kw) -> EnvRequest:  # noqa: F811
    fields = {"host": "pypi.org", "port": 443} if kind == "egress" else {"name": "STRIPE_KEY"}
    r = EnvRequest(kind=kind, environment="liveenv", why="uv sync needs the index", requester="session:ses-gv",
                   session_id="ses-gv", session="gv", turn=1, **{**fields, **kw})
    svc.store.put(r)
    return r


async def test_an_egress_request_in_enforce_goes_through_tower(svc, gov, tower):  # noqa: F811
    tower.answer = allow("RC-E1")
    r = filed(svc)
    await gov.relay_request(r)
    r = svc.store.get(EnvRequest, r.id)
    assert (r.state, r.decided_by, r.receipt) == ("approved", "tower", "RC-E1")
    assert "now allowed" in (r.message or "")  # egressapi.on_decided applied it
    [req] = tower.asked
    assert req.kind == "egress.request" and req.idempotency_key == f"hangar:session:ses-gv:egress:{r.id}"
    assert (req.action.host, req.action.scope) == ("pypi.org", "session")


async def test_an_egress_request_waits_then_settles_on_towers_event(svc, gov, tower):  # noqa: F811
    tower.answer = lambda req: Decision(outcome="wait", approval_id="A-E2", status="waiting", reason="ask Lee")
    r = filed(svc)
    await gov.relay_request(r)
    r = svc.store.get(EnvRequest, r.id)
    assert (r.state, r.workspace_approval) == ("pending", "A-E2") and svc.store.kv_get(REQ + "A-E2") == r.id
    tower.deny("A-E2", "not this index")
    code, out = await gov.receiver.receive(*event("approval.decided", "A-E2"))
    assert code == 200 and out["state"] == "denied", out
    r = svc.store.get(EnvRequest, r.id)
    assert (r.state, r.decided_by, r.note) == ("denied", "tower", "Denied in Tower: not this index")
    assert svc.store.kv_get(REQ + "A-E2") is None


async def test_a_secret_request_tower_cant_answer_stays_pending(svc, gov, tower):  # noqa: F811
    gov.receiver._tower = unreachable()
    r = filed(svc, "secret")
    await gov.relay_request(r)
    r = svc.store.get(EnvRequest, r.id)
    assert r.state == "pending" and r.governance["decided_by"] == "fail_closed" and r.governance["told"]
    gov.receiver._tower = tower  # back: the poll asks again
    tower.answer = allow("RC-S1")
    await gov.tick_requests()
    r = svc.store.get(EnvRequest, r.id)
    assert (r.state, r.receipt, tower.asked[0].kind, tower.asked[0].action.secret) == (
        "approved", "RC-S1", "secret.request", "STRIPE_KEY")


async def test_a_person_approving_a_request_in_enforce_signs_it(svc, gov, tower):  # noqa: F811
    tower.answer = lambda req: (Decision(outcome="wait", approval_id="A-S2", status="waiting", reason="one more")
                                if req.signer else Decision(outcome="wait", approval_id="A-S2", status="waiting"))
    r = filed(svc, "secret")
    reqs = gov.requests
    with pytest.raises(RequestError, match="still waits: one more"):
        await reqs.decide(r.id, True, "sean")
    assert svc.store.get(EnvRequest, r.id).state == "pending"
    tower.answer = allow("RC-S2")
    r = await reqs.decide(r.id, True, "sean")
    assert (r.state, r.decided_by, r.receipt) == ("approved", "sean", "RC-S2")


async def test_shadow_requests_record_both_and_wait_for_a_person(svc, gov, tower, settings):  # noqa: F811
    settings.governance = "shadow"
    tower.answer = lambda req: Decision(outcome="allow", status="allowed", rule="pypi-ok")
    r = filed(svc)
    await gov.relay_request(r)
    r = svc.store.get(EnvRequest, r.id)
    assert r.state == "pending" and r.governance["hangar"]["outcome"] == "wait"
    assert r.governance["tower"]["outcome"] == "allow" and tower.asked[0].dry_run


# ====================================================================== GKE
async def test_required_uid_split_refuses_governed_like_ask(svc, live, settings, monkeypatch):  # noqa: F811
    """Tower deciding doesn't stop an agent that shares the driver's user from forging the driver's answer, so
    HANGAR_UID_SPLIT=required refuses a governed turn where there is no split (GKE until KAN-186), as it does `ask`."""
    new(svc, live, "gv-gke", "hello")
    s = await settle(svc, "gv-gke")
    sb = svc.fleet.get(s.sandbox_id)
    settings.uid_split = "required"
    s.approvals = "governed"
    with pytest.raises(steering.SteerError, match="approvals=governed needs hangar's driver to run as a user of"):
        await steering.prepare(svc, s, SessionTurn(message="x"), sb, HARNESSES["live-claude"])
