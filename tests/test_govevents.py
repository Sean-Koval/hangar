"""Tower's events (KAN-77, spec 0015 §5, §12): the signed receiver, suspend and resume on approvals, run.stop.

The live tests run real turns under the real driver with the scripted Claude Code of tests/test_steer.py, against a
fake Tower whose decisions the test sets; events are signed as Tower signs them (the contract test checks that
against a fixture signed by Tower's own code)."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from hangar import driver
from hangar import steer as steering
from hangar.governance import Decision, EventRejected, sign_event, verify_event
from hangar.govevents import SEEN, WAIT, Receiver
from hangar.models import CredentialKind, SessionState
from hangar.sessions import SessionService
from hangar.vault import Vault
from tests.test_steer import KEY, live, new, pending_approval, repo_file, running, settle, svc  # noqa: F401

SECRET = "whsec-hangar-tests"
FIXTURE = Path(__file__).parent / "fixtures" / "governance" / "tower_events.json"


class FakeTower:
    """GET /v1/decisions/{id} and withdraw, answered from `decisions` (a wait until the test decides)."""

    def __init__(self):
        self.decisions: dict[str, Decision] = {}
        self.reads: list[str] = []
        self.withdrawn: list[str] = []

    async def decide(self, req):  # pragma: no cover - the receiver never asks
        raise AssertionError("the receiver only reads decisions")

    async def status(self, approval_id: str, *, kind) -> Decision:
        self.reads.append(approval_id)
        return self.decisions.get(approval_id, Decision(outcome="wait", approval_id=approval_id, status="waiting"))

    async def withdraw(self, approval_id: str, reason: str) -> bool:
        self.withdrawn.append(approval_id)
        return True

    def allow(self, aid: str, receipt: str = "RC-1") -> None:
        self.decisions[aid] = Decision(outcome="allow", approval_id=aid, receipt=receipt, status="approved",
                                       reason=f"Signed by Dana ({aid}).")

    def deny(self, aid: str, reason: str = "not on a Friday") -> None:
        self.decisions[aid] = Decision(outcome="deny", approval_id=aid, status="denied", reason=reason)


def event(kind: str, aid: str | None = None, *, t: int | None = None, secret: str = SECRET, **extra):
    """Headers and body as Tower's Outbox._post sends them (aw/webhooks.py)."""
    p = {"id": f"ev-{uuid.uuid4().hex}", "kind": kind, "at": "2026-10-27T15:02:11.000Z", **extra}
    if aid:
        p["approval"] = {"id": aid, "status": "approved" if kind == "approval.decided" else "waiting",
                         "kind": "hangar.tool.call", "decided_by": "dana@acme.test"}
    body = json.dumps(p, sort_keys=True, separators=(",", ":")).encode()
    return {"content-type": "application/json", "x-aw-event-id": p["id"],
            "x-aw-signature": sign_event(secret, body, int(time.time()) if t is None else t)}, body


def configure(settings, broker) -> None:
    settings.governance = "enforce"
    settings.governance_webhook_secret = "tower-webhook"
    broker.put("tower-webhook", "webhook", CredentialKind.GENERIC, SECRET, exposure="control_plane", team=True)


@pytest.fixture
def tower():
    return FakeTower()


@pytest.fixture
def recv(svc, settings, broker, tower):  # noqa: F811
    configure(settings, broker)
    return Receiver(svc, tower=tower)


def audits(svc, action: str) -> list:  # noqa: F811
    return [e for e in svc.store.audit_tail(200) if e.action == action]


async def waiting_call(svc, live, recv, name: str, aid: str, expect: str = "waiting"):  # noqa: F811
    """A session whose `ask` turn holds `touch tool-ran.txt`, relayed to Tower and waiting on approval `aid`."""
    new(svc, live, name, "TOOL make the file", approvals="ask")
    a = await pending_approval(svc, name)
    s = svc.get(name)
    assert a.args_hash == driver.args_hash({"command": "touch tool-ran.txt"})  # the driver says what it hashed
    w = await recv.watch(s, s.turns[-1], a, Decision(outcome="wait", approval_id=aid, status="waiting"))
    assert w is not None and w.state == expect and w.args_hash == a.args_hash
    return s, w


async def suspended(svc, live, recv, name: str, aid: str):  # noqa: F811
    s, w = await waiting_call(svc, live, recv, name, aid)
    w.since -= recv.settings.governance_suspend_after_seconds + 1
    await recv.tick()
    await recv.drain()
    s = await settle(svc, name)
    assert w.state == "suspended", (w.state, s.reason)
    return s, w


# ====================================================================== the contract
def test_an_event_signed_by_towers_own_code_verifies():
    """tests/fixtures/governance/tower_events.json was signed by Tower's aw.webhooks.sign, headers as Outbox._post
    sends them: hangar's verify_event must accept exactly that, and nothing a byte off."""
    doc = json.loads(FIXTURE.read_text())
    secret, t = doc["secret"], doc["t"]
    for e in doc["events"]:
        body = e["body"].encode()
        ev = verify_event(secret, e["headers"], body, {}, now=t + 5)
        assert ev.id == e["headers"]["x-aw-event-id"] and ev.kind == "approval.decided"
        assert ev.approval["id"] == "A-123" and ev.corr["session"] == "ses-77"
        # header names in any case, as an HTTP stack may hand them over
        assert verify_event(secret, {k.upper(): v for k, v in e["headers"].items()}, body, {}, now=t).id == ev.id
        for bad, why in [((secret, e["headers"], body.replace(b"A-123", b"A-124"), {}), "bad signature"),
                         ((secret + "x", e["headers"], body, {}), "bad signature"),
                         ((secret, e["headers"], body, {ev.id: t}), "replayed")]:
            with pytest.raises(EventRejected, match=why):
                verify_event(*bad, now=t)
        with pytest.raises(EventRejected, match="stale"):
            verify_event(secret, e["headers"], body, {}, now=t + 301)


def test_the_driver_hashes_tool_input_as_governance_does():
    from hangar.governance import args_hash

    for data in ({"command": "git push origin HEAD:main"}, {"file_path": "/a", "content": "é\n"}, {}):
        assert driver.args_hash(data) == args_hash(data)


def test_a_preclear_file_counts_only_signed_with_the_run_key(tmp_path):
    key = b"k" * 64
    calls = [{"tool": "Bash", "args_hash": "ab" * 32, "receipt": "RC-1", "approval": "A-1"}]
    (tmp_path / "preclear").write_bytes(steering.preclear_file(key, calls))
    assert driver.load_preclear(str(tmp_path), key) == calls
    assert not (tmp_path / "preclear").exists()  # read once, then gone
    (tmp_path / "preclear").write_bytes(steering.preclear_file(b"x" * 64, calls))  # the agent's own forgery
    assert driver.load_preclear(str(tmp_path), key) == []
    (tmp_path / "preclear").write_bytes(json.dumps({"calls": calls}).encode())  # unsigned
    assert driver.load_preclear(str(tmp_path), key) == []
    assert driver.load_preclear(str(tmp_path), key) == []  # no file at all


# ====================================================================== the route
@pytest.fixture
def bare(fleet, store, settings, broker, tower):
    """A receiver over a session service that never starts a turn: for the checks that come before any turn."""
    return Receiver(SessionService(fleet, Vault(store, KEY)), tower=tower)


async def test_off_or_unconfigured_answers_404(bare, settings, broker):
    headers, body = event("approval.created", "A-1")
    assert (await bare.receive(headers, body))[0] == 404  # HANGAR_GOVERNANCE=off, the default
    settings.governance = "shadow"
    code, out = await bare.receive(headers, body)
    assert code == 404 and "HANGAR_GOVERNANCE_WEBHOOK_SECRET" in out["error"]
    settings.governance_webhook_secret = "tower-webhook"  # named, but not stored
    assert (await bare.receive(headers, body))[0] == 503
    settings.governance, settings.governance_modes = "off", "tool.call=enforce"  # one kind on is enough
    configure(settings, broker)
    settings.governance = "off"
    assert (await bare.receive(headers, body))[0] == 200


async def test_bad_unsigned_stale_and_replayed_events_are_rejected_and_audited(bare, settings, broker, tower):
    configure(settings, broker)
    headers, body = event("approval.decided", "A-1")
    code, out = await bare.receive(headers, body)
    assert code == 200 and "nothing in hangar waits" in out["ignored"]
    assert bare.store.kv_get(SEEN + headers["x-aw-event-id"]) is not None  # kept in the store: a restart remembers
    again = await Receiver(bare.sessions, tower=tower).receive(headers, body)  # a duplicate delivery
    assert again == (200, {"ignored": "duplicate event"})
    unsigned = {k: v for k, v in headers.items() if k != "x-aw-signature"}
    forged, fbody = event("approval.decided", "A-2", secret="not-the-secret")
    stale, sbody = event("approval.decided", "A-3", t=int(time.time()) - 301)
    tampered = body.replace(b"A-1", b"A-9")
    for h, b, why in [(unsigned, body, "unsigned"), (forged, fbody, "bad signature"), (stale, sbody, "stale"),
                      (headers, tampered, "bad signature")]:
        code, out = await bare.receive(h, b)
        assert (code, out) == (401, {"error": why if why != "stale" else "stale signature"}), why
    h2, b2 = event("approval.decided", "A-4")  # the header and the body name different events
    assert (await bare.receive({**h2, "x-aw-event-id": "ev-other"}, b2))[0] == 400
    reasons = sorted(e.detail["reason"] for e in audits(bare.sessions, "governance.event_rejected"))
    assert reasons == ["bad signature", "bad signature", "event id header and body disagree", "replayed",
                       "stale signature", "unsigned"]
    assert tower.reads == []  # nothing rejected ever reached Tower


async def test_the_route_is_mounted_without_a_token(settings, broker):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as client:
        h = app.state.hangar
        assert client.post("/api/governance/events", content=b"{}").status_code == 404  # off
        h.settings.governance, h.settings.governance_webhook_secret = "shadow", "tower-webhook"
        h.broker.put("tower-webhook", "webhook", CredentialKind.GENERIC, SECRET, exposure="control_plane",
                     team=True)
        assert client.post("/api/governance/events", content=b"{}").status_code == 401
        headers, body = event("approval.created", "A-1")
        r = client.post("/api/governance/events", content=body, headers=headers)
        assert r.status_code == 200 and "ignored" in r.json()


# ====================================================================== suspend and resume
async def test_a_waiting_call_suspends_then_resumes_with_the_call_cleared_once(svc, live, recv, tower):  # noqa: F811
    s, w = await suspended(svc, live, recv, "gov-resume", "A-1")
    assert s.state == SessionState.SUSPENDED and s.sandbox_id is None and s.checkpoints  # the slot is released
    assert "A-1" in (s.reason or "")
    t1 = s.turns[0]
    assert t1.outcome in ("interrupted", "exited")  # stopped at the call (the scripted agent may finish first)
    assert [(a.status, a.by, a.message) for a in t1.approvals] == [
        ("denied", "tower", "Paused for approval A-1; this turn resumes when it's signed.")]
    assert audits(svc, "governance.suspend")

    tower.allow("A-1", "RC-104")
    headers, body = event("approval.decided", "A-1")
    code, out = await recv.receive(headers, body)
    assert code == 200 and out == {"ok": True, "approval": "A-1", "resumed": "gov-resume"}, out
    assert tower.reads == ["A-1"]  # the event was a hint: hangar asked Tower
    assert await recv.receive(headers, body) == (200, {"ignored": "duplicate event"})  # duplicate delivery
    h2, b2 = event("approval.decided", "A-1")  # the same decision again, as another event: settled
    assert (await recv.receive(h2, b2))[1] == {"ignored": "nothing in hangar waits on A-1 (yet)"}
    s = await settle(svc, "gov-resume")
    assert len(s.turns) == 2 and s.turns[1].mode == "resume"  # the conversation, from the checkpoint
    t2 = s.turns[1]
    assert t2.preclear == [{"tool": "Bash", "args_hash": w.args_hash, "receipt": "RC-104", "approval": "A-1"}]
    assert [(a.status, a.by, a.receipt) for a in t2.approvals] == [("allowed", "tower", "RC-104")]
    assert repo_file(svc, s, "tool-ran.txt").exists()
    assert w.state == "resumed" and s.governance.state == "resumed"
    assert svc.store.kv_get(WAIT + "A-1") is None
    assert [e.detail.get("receipt") for e in audits(svc, "governance.resume")] == ["RC-104"]


async def test_decided_before_created_is_read_once_something_waits(svc, live, recv, tower):  # noqa: F811
    tower.allow("A-2")
    for kind in ("approval.decided", "approval.created"):  # out of order, before hangar follows A-2
        code, out = await recv.receive(*event(kind, "A-2"))
        assert code == 200 and "ignored" in out
    assert tower.reads == []
    s, w = await waiting_call(svc, live, recv, "gov-early", "A-2", expect="allowed")  # answered in the running turn
    assert tower.reads == ["A-2"]
    s = await settle(svc, "gov-early")
    assert len(s.turns) == 1 and repo_file(svc, s, "tool-ran.txt").exists()
    assert [(a.status, a.by, a.receipt) for a in s.turns[0].approvals] == [("allowed", "tower", "RC-1")]


async def test_a_deny_leaves_the_session_suspended_with_the_reason(svc, live, recv, tower):  # noqa: F811
    await suspended(svc, live, recv, "gov-deny", "A-3")
    tower.deny("A-3", "force-push needs a maintainer")
    code, out = await recv.receive(*event("approval.decided", "A-3"))
    assert code == 200 and out.get("denied"), out
    s = svc.get("gov-deny")
    assert s.state == SessionState.SUSPENDED and len(s.turns) == 1 and "force-push needs a maintainer" in s.reason


async def test_tower_unreachable_is_a_503_and_the_retry_is_not_a_replay(svc, live, recv, tower):  # noqa: F811
    await suspended(svc, live, recv, "gov-down", "A-4")
    tower.decisions["A-4"] = Decision(outcome="wait", approval_id="A-4", status="unavailable", source="fail_closed",
                                      failure="unreachable", reason="approval service unavailable")
    headers, body = event("approval.decided", "A-4")
    assert (await recv.receive(headers, body))[0] == 503
    tower.allow("A-4")
    code, out = await recv.receive(headers, body)  # Tower's outbox retries the same event
    assert code == 200 and out.get("resumed") == "gov-down"


# ====================================================================== run.stop
async def test_stop_then_approve_stays_stopped(svc, live, recv, tower):  # noqa: F811
    s, w = await suspended(svc, live, recv, "gov-stop", "A-5")
    code, out = await recv.receive(*event("run.stop", corr={"source": "hangar", "session": s.id},
                                          run={"stopped_by": "lee@acme.test", "reason": "wrong repo"}))
    assert code == 200 and out == {"ok": True, "stopped": ["gov-stop"]}
    await recv.drain()
    assert w.state == "stopped" and tower.withdrawn == ["A-5"]
    tower.allow("A-5")
    code, out = await recv.receive(*event("approval.decided", "A-5"))  # signed after the stop
    assert code == 200 and "ignored" in out
    await recv.tick()
    s = svc.get("gov-stop")
    assert len(s.turns) == 1 and s.state == SessionState.SUSPENDED and s.governance_stop.by == "lee@acme.test"
    [stop] = audits(svc, "governance.run_stop")
    assert stop.actor == "tower:lee@acme.test" and stop.detail["reason"] == "wrong repo"
    # a later request in the stopped turn isn't followed either
    turn = s.turns[0]
    again = await recv.watch(s, turn, turn.approvals[0], Decision(outcome="wait", approval_id="A-6"))
    assert again.state == "stopped" and svc.store.kv_get(WAIT + "A-6") is None


async def test_stop_interrupts_a_running_turn(svc, live, recv):  # noqa: F811
    new(svc, live, "gov-run", "SLOW build the thing")
    s = await running(svc, "gov-run")
    code, out = await recv.receive(*event("run.stop", run={"session": "gov-run", "run": s.turns[0].run_id,
                                                           "stopped_by": "lee@acme.test"}))
    assert code == 200 and out["stopped"] == ["gov-run"]
    s = await settle(svc, "gov-run")
    assert s.turns[0].outcome == "interrupted"
    assert [st.requester for st in s.turns[0].steers if st.kind == "interrupt"] == ["tower:lee@acme.test"]
    assert audits(svc, "governance.run_stop")[0].detail["interrupted"] is True
    code, out = await recv.receive(*event("run.stop", run={"session": "nope"}))
    assert code == 200 and out == {"ignored": "hangar has no such run"}
