"""The governance decision point's client side (spec 0015, #171): wire contract, fail-closed table, modes, events.

The workspace is faked with httpx.MockTransport; every row of the fail-closed table is checked for every kind.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import httpx
import pytest

from hangar import approvals, governance, people
from hangar.governance import (
    Decision,
    DecisionRequest,
    EventRejected,
    GovernanceRouter,
    HttpDecisionPoint,
    LocalDecisionPoint,
    StaticCredential,
)

SPEC = Path(__file__).resolve().parents[1] / "docs" / "specs" / "0015-governance-decision-point.md"
URL = "https://workspace.test"
ALLOW = {"v": governance.SCHEMA, "decision": "allow", "receipt": "RC-1", "approval": "A-1", "status": "approved",
         "reason": "Signed by Dana (A-1)."}
WAIT = {"v": governance.SCHEMA, "decision": "wait", "approval": "A-1", "status": "waiting",
        "expires_at": "2026-10-17T14:05:00Z", "reason": "Waiting for Dana."}


def req(kind: str = "job.release", **kw) -> DecisionRequest:
    base = {"kind": kind, "idempotency_key": f"hangar:test:{kind}", "subject": {"job": "j-1", "repo": "acme/api"},
            "correlation": {"job": "j-1", "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}}
    return DecisionRequest.model_validate({**base, **kw})


def point(handler, **kw) -> HttpDecisionPoint:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpDecisionPoint(URL, StaticCredential("k"), client=client, **kw)


def answer(status: int, body=None, text: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)
    return handler


def raises(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


# ====================================================================== the fail-closed table
FAILURES = {
    "unreachable": raises(httpx.ConnectError("refused")),
    "connect timeout": raises(httpx.ConnectTimeout("slow")),
    "read timeout": raises(httpx.ReadTimeout("slow")),
    "500": answer(500, {"error": "boom"}),
    "502 html": answer(502, text="<html>bad gateway</html>"),
    "503 deny body": answer(503, {"decision": "deny"}),  # the workspace failing, not its policy
    "429": answer(429, {"error": "slow down"}),
    "401": answer(401, {"error": "unknown key"}),
    "403": answer(403, {"error": "no"}),
    "400": answer(400, {"error": "unknown kind"}),
    "422": answer(422, {"error": "?"}),
    "not json": answer(200, text="ok"),
    "a list": answer(200, ["allow"]),
    "unknown decision": answer(200, {"decision": "yes", "receipt": "RC-1"}),
    "allow without receipt": answer(200, {"decision": "allow", "reason": "fine"}),
    "wait without approval": answer(200, {"decision": "wait"}),
    "bad time": answer(200, {**ALLOW, "expires_at": "tomorrow"}),
    "unknown status": answer(200, {**ALLOW, "status": "maybe"}),
}


@pytest.mark.parametrize("kind", governance.KINDS)
@pytest.mark.parametrize("failure", list(FAILURES))
async def test_every_failure_fails_closed_for_every_kind(kind, failure):
    d = await point(FAILURES[failure]).decide(req(kind))
    assert d.outcome != "allow"
    assert d.outcome == governance.FAIL_CLOSED[kind]
    assert d.source == "fail_closed" and d.status == "unavailable" and d.failure
    assert d.reason.startswith("approval service unavailable")
    assert d.outcome == ("deny" if kind == "tool.call" else "wait")


async def test_failure_kinds_and_retryability():
    expect = {"unreachable": ("unreachable", True), "connect timeout": ("timeout", True),
              "503 deny body": ("server_error", True), "429": ("rate_limited", True), "401": ("auth", False),
              "403": ("auth", False), "400": ("contract", False), "allow without receipt": ("malformed", True)}
    for name, (failure, retry) in expect.items():
        d = await point(FAILURES[name]).decide(req())
        assert (d.failure, d.retryable) == (failure, retry), name


async def test_total_budget_is_enforced(monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json=ALLOW)

    dp = point(slow, timeout=0.05, connect_timeout=0.05)
    t = time.monotonic()
    d = await dp.decide(req("tool.call"))
    assert time.monotonic() - t < 2 and d.outcome == "deny" and d.failure == "timeout"


async def test_default_timeouts_are_the_spec_values():
    dp = HttpDecisionPoint(URL, StaticCredential("k"))
    assert (dp.timeout.connect, dp.timeout.read) == (2.0, 5.0)
    await dp.close()


async def test_status_failures_fail_closed_with_the_approval_kept():
    d = await point(FAILURES["503 deny body"]).status("A-7", kind="landing.approve")
    assert (d.outcome, d.approval_id, d.source) == ("wait", "A-7", "fail_closed")
    d = await point(answer(404, {"error": "no such"})).status("A-7", kind="tool.call")
    assert (d.outcome, d.failure) == ("deny", "unknown_approval")


# ====================================================================== the happy paths and the wire
async def test_decide_sends_the_contract_and_reads_the_answer():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={**ALLOW, "expires_at": "2026-10-17T14:05:00+00:00", "rule": "r-1"})

    r = req("landing.approve", deadline=1_800_000_000.0, signer={"person": "p-2", "via": "github",
                                                                 "external": "github:42"},
            context={"risk": "low",
                     "checks": [{"name": "ci", "conclusion": "success", "sha": "abc", "trusted": False}]})
    d = await point(handler).decide(r)
    assert (d.outcome, d.receipt, d.approval_id, d.rule, d.source) == ("allow", "RC-1", "A-1", "r-1", "workspace")
    assert d.expires_at == governance.parse_time("2026-10-17T14:05:00Z")
    (sent,) = seen
    assert sent.method == "POST" and str(sent.url) == f"{URL}/v1/decisions"
    assert sent.headers["authorization"] == "Bearer k"
    assert sent.headers["idempotency-key"] == r.idempotency_key
    assert sent.headers["traceparent"] == r.correlation.traceparent
    body = json.loads(sent.content)
    assert body["v"] == "hangar.decision/v1" and body["kind"] == "hangar.landing.approve"
    assert body["deadline"] == "2027-01-15T08:00:00.000Z"
    assert body["signer"] == {"person": "p-2", "external": "github:42", "via": "github", "verdict": "approve"}
    assert body["context"]["checks"][0]["trusted"] is False  # a meaningful false survives pruning
    assert "approver" not in body and "local_subject" not in body and "dry_run" not in body
    assert DecisionRequest.from_wire(body) == r.model_copy(update={"deadline": 1_800_000_000.0})


async def test_a_401_is_retried_once_with_a_fresh_token():
    class Cred:
        calls: list[bool] = []

        async def token(self, *, fresh: bool = False) -> str:
            self.calls.append(fresh)
            return "new" if fresh else "old"

    def handler(request):
        ok = request.headers["authorization"] == "Bearer new"
        return httpx.Response(200, json=ALLOW) if ok else httpx.Response(401, json={})

    cred = Cred()
    dp = HttpDecisionPoint(URL, cred, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await dp.decide(req())).outcome == "allow"
    assert cred.calls == [False, True]


async def test_dry_run_allows_without_receipt_and_wait_without_approval():
    d = await point(answer(200, {"decision": "allow", "rule": "r"})).decide(req(dry_run=True))
    assert d.outcome == "allow" and d.receipt is None
    d = await point(answer(200, {"decision": "wait"})).decide(req(dry_run=True))
    assert d.outcome == "wait"


async def test_withdraw():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    assert await point(handler).withdraw("A-3", "the job expired") is True
    assert seen == [("POST", "/v1/decisions/A-3/withdraw", {"v": governance.SCHEMA, "reason": "the job expired"})]
    assert await point(answer(409, {"error": "already denied"})).withdraw("A-3", "x") is True
    assert await point(FAILURES["unreachable"]).withdraw("A-3", "x") is False


async def test_circuit_breaker_opens_and_recovers():
    calls = []
    healthy = False

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=ALLOW) if healthy else httpx.Response(503, json={"decision": "deny"})

    t = [0.0]
    dp = point(handler, breaker_failures=3, breaker_window=30, breaker_cooldown=10, clock=lambda: t[0])
    for _ in range(3):
        assert (await dp.decide(req())).failure == "server_error"
    d = await dp.decide(req())
    assert d.failure == "circuit_open" and d.outcome == "wait" and len(calls) == 3  # no network call
    healthy, t[0] = True, 11.0
    assert (await dp.decide(req())).outcome == "allow"


# ====================================================================== waiting
async def test_wait_for_polls_until_settled():
    answers = iter([WAIT, FAILURES, WAIT, ALLOW])  # a failure in the middle is not a decision

    def handler(request):
        a = next(answers)
        if a is FAILURES:
            return httpx.Response(503, json={"decision": "deny"})
        return httpx.Response(200, json=a)

    dp = point(handler)
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)

    first = Decision.from_wire(WAIT)
    d = await governance.wait_for(dp, first, "job.release", time.time() + 3600, sleep=sleep)
    assert d.outcome == "allow" and d.receipt == "RC-1"
    assert len(slept) == 4 and slept[1] > slept[0]  # backoff


async def test_wait_for_fails_closed_at_the_deadline():
    dp = point(answer(200, WAIT))
    now = [1000.0]

    async def sleep(s):
        now[0] += s

    first = Decision.from_wire(WAIT)
    d = await governance.wait_for(dp, first, "tool.call", 1030.0, sleep=sleep, clock=lambda: now[0])
    assert (d.outcome, d.failure, d.approval_id, d.retryable) == ("deny", "timeout", "A-1", False)
    d = await governance.wait_for(dp, first, "job.release", 1030.0, sleep=sleep, clock=lambda: now[0])
    assert d.outcome == "wait"  # a job stays pending; expire_approvals drops it later


async def test_an_event_wakes_the_waiter_before_the_poll():
    state = {"a": WAIT}
    dp = point(lambda request: httpx.Response(200, json=state["a"]))
    woken = asyncio.Event()

    async def sign_later():
        await asyncio.sleep(0.05)
        state["a"] = ALLOW
        woken.set()

    task = asyncio.create_task(sign_later())
    t = time.monotonic()
    d = await governance.wait_for(dp, Decision.from_wire(WAIT), "job.release", time.time() + 60, start=30,
                                  woken=woken)
    await task
    assert d.outcome == "allow" and time.monotonic() - t < 5


# ====================================================================== local rules
async def test_local_waits_without_a_signer_and_asks_can_decide_with_one(settings, store):
    ctx = type("Ctx", (), {"settings": settings, "store": store})()
    people.add(store, "alice")
    people.add(store, "ada", role="admin")
    local = LocalDecisionPoint(ctx)
    assert (await local.decide(req())).outcome == "wait"
    ada = approvals.Approver("@ada:x", "ada", "mcp", frozenset({"@ada:x"}))
    alice = approvals.Approver("@alice:x", "alice", "mcp", frozenset({"@alice:x"}))
    alices = approvals.Subject("alice", frozenset({"@alice:x"}), "acme/api", "job j-1")
    signed = {"signer": {"person": "ada", "via": "mcp"}}
    d = await local.decide(req(**signed, approver=ada, local_subject=alices))
    assert (d.outcome, d.rule, d.source, d.receipt) == ("allow", "admin", "local", None)
    d = await local.decide(req(signer={"person": "alice", "via": "mcp"}, approver=alice, local_subject=alices))
    assert d.outcome == "deny" and "own work" in d.reason
    d = await local.decide(req(signer={"person": "alice", "via": "mcp", "verdict": "reject"}))
    assert d.outcome == "deny" and d.rule == "reject"
    d = await local.decide(req("tool.call", signer={"person": "alice", "via": "mcp"}))
    assert d.outcome == "allow" and d.rule == "session_access"


# ====================================================================== modes
class Fixed:
    def __init__(self, d: Decision):
        self.d, self.asked = d, []

    async def decide(self, r):
        self.asked.append(r)
        return self.d

    async def status(self, approval_id, *, kind):
        return self.d

    async def withdraw(self, approval_id, reason):
        return True


async def test_modes():
    local = Fixed(Decision(outcome="allow", source="local", status="local"))
    remote = Fixed(Decision(outcome="wait", approval_id="A-1", status="waiting"))
    disagreements = []
    modes = governance.parse_modes("shadow", "landing.approve=enforce,tool.call=off")
    router = GovernanceRouter(local, remote, modes, on_disagree=lambda r, a, b: disagreements.append((r.kind, a, b)))

    d = await router.decide(req("job.release"))  # shadow: local answers, the workspace is compared
    await router.drain()
    assert d.outcome == "allow" and d.source == "local"
    assert remote.asked[-1].dry_run is True
    assert disagreements and disagreements[0][0] == "job.release"

    assert (await router.decide(req("landing.approve"))).outcome == "wait"  # enforce: the workspace
    assert remote.asked[-1].dry_run is False

    n = len(remote.asked)
    await router.decide(req("tool.call"))  # off: nobody remote is asked
    await router.drain()
    assert len(remote.asked) == n


async def test_shadow_never_waits_for_or_fails_on_the_workspace():
    local = Fixed(Decision(outcome="deny", source="local", status="denied"))
    router = GovernanceRouter(local, point(FAILURES["unreachable"]), governance.parse_modes("shadow"),
                              on_disagree=lambda *a: pytest.fail("a failure isn't a disagreement"))
    assert (await router.decide(req())).outcome == "deny"
    await router.drain()


async def test_break_glass():
    t = time.time()
    with pytest.raises(ValueError):
        governance.BreakGlass(started_by="ada", confirmed_by="ada", reason="outage", started_at=t, until=t + 60)
    with pytest.raises(ValueError):
        governance.BreakGlass(started_by="ada", reason="outage", started_at=t, until=t + 25 * 3600)
    bg = governance.BreakGlass(started_by="ada", reason="outage", started_at=t, until=t + 3600)
    assert not bg.active()  # not until a second admin confirms
    bg.confirmed_by = "lee"
    assert bg.active() and not bg.active(t + 3601)

    local = Fixed(Decision(outcome="allow", source="local"))
    remote = Fixed(Decision(outcome="deny", status="denied"))
    replay = []
    router = GovernanceRouter(local, remote, governance.parse_modes("enforce"),
                              on_break_glass=lambda r, d: replay.append(r.kind))
    assert (await router.decide(req())).outcome == "deny"
    router.break_glass = bg
    assert (await router.decide(req())).outcome == "allow" and replay == ["job.release"]


def test_parse_modes_and_router_needs_a_url():
    assert governance.parse_modes("off", '{"tool.call": "shadow"}')["tool.call"] == "shadow"
    for bad in [("on", None), ("off", "tool.call=loud"), ("off", "job=enforce")]:
        with pytest.raises(ValueError):
            governance.parse_modes(*bad)
    with pytest.raises(ValueError):
        GovernanceRouter(LocalDecisionPoint(), None, governance.parse_modes("enforce"))
    GovernanceRouter(LocalDecisionPoint(), None, governance.parse_modes("off"))


# ====================================================================== keys and hashes
def _workspace_args_hash(args: dict) -> str:
    """Copied from Tower (Sean-Koval/tower) server/aw/store.py (sha256 + args_hash): the two must agree."""
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@pytest.mark.parametrize("args", [{"command": "git push origin HEAD:main"}, {"b": [1, {"z": None, "a": "é"}], "a": 1.5},
                                  {"when": time.gmtime(0)}, {}])
def test_args_hash_matches_the_workspace(args):
    assert governance.args_hash(args) == _workspace_args_hash(args)
    assert re.fullmatch(r"[0-9a-f]{64}", governance.args_hash(args))


def test_idempotency_keys():
    k = governance.idempotency_key
    assert k("job.release", job="j-1") == "hangar:job:j-1:release"
    assert k("landing.approve", job="j-1", head_sha="abc") == "hangar:job:j-1:landing:abc"
    assert k("tool.call", session="s-1", request="ap-1") == "hangar:session:s-1:tool:ap-1"
    assert k("secret.request", session="s-1", request="rq-1") == "hangar:session:s-1:secret:rq-1"
    with pytest.raises(ValueError):
        k("landing.approve", job="j-1")


# ====================================================================== events
SECRET = "whsec-test"


def signed(body: dict, t: int, secret: str = SECRET) -> tuple[dict, bytes]:
    raw = json.dumps(body).encode()
    return {"X-AW-Signature": governance.sign_event(secret, raw, t), "X-AW-Event-Id": body["id"]}, raw


def test_events_verify_and_reject():
    ev = {"id": "ev-1", "kind": "approval.decided", "at": "2026-10-16T15:02:11Z",
          "approval": {"id": "A-1", "status": "approved"}}
    now = 1_760_000_000
    seen: dict[str, float] = {}
    headers, raw = signed(ev, now)
    got = governance.verify_event(SECRET, headers, raw, seen, now=now + 10)
    assert got.kind == "approval.decided" and got.approval["id"] == "A-1"
    with pytest.raises(EventRejected, match="replayed"):
        governance.verify_event(SECRET, headers, raw, seen, now=now + 10)
    cases = [
        (signed({**ev, "id": "ev-2"}, now - 301), "stale"),
        (signed({**ev, "id": "ev-3"}, now, secret="other"), "bad signature"),
        (({"X-AW-Signature": "t=x"}, raw), "unsigned"),
        (({}, raw), "unsigned"),
        (signed({"id": "ev-4", "kind": "approval.vanished", "at": "x"}, now), "malformed"),
    ]
    for (h, body), why in cases:
        with pytest.raises(EventRejected, match=why):
            governance.verify_event(SECRET, h, body, seen, now=now)
    h, body = signed({**ev, "id": "ev-5"}, now)
    with pytest.raises(EventRejected, match="bad signature"):
        governance.verify_event(SECRET, h, body.replace(b"approved", b"denied!!"), seen, now=now)
    with pytest.raises(EventRejected, match="no webhook secret"):
        governance.verify_event("", h, body, seen, now=now)


# ====================================================================== the spec's examples are the contract
def _examples(tag: str) -> list[dict]:
    text = SPEC.read_text()
    blocks = re.findall(rf"<!-- contract: {tag} -->\s*```json\n(.*?)```", text, re.S)
    assert blocks, f"no {tag} examples in {SPEC.name}"
    return [json.loads(b) for b in blocks]


def test_spec_request_examples_parse_and_round_trip():
    for body in _examples("request"):
        r = DecisionRequest.from_wire(body)
        assert r.to_wire() == {**body, "deadline": governance.rfc3339(governance.parse_time(body["deadline"]))}
        if r.kind == "tool.call":
            assert r.action.args_hash == governance.args_hash({"command": r.action.detail})


def test_spec_response_and_event_examples_parse():
    for body in _examples("response"):
        d = Decision.from_wire(body)
        assert d.outcome in ("allow", "wait") and d.approval_id == "A-123"
        assert Decision.from_wire(d.to_wire()) == d
    (ev,) = _examples("event")
    headers, raw = signed(ev, 1_760_000_000)
    assert governance.verify_event(SECRET, headers, raw, {}, now=1_760_000_000).approval["receipt"] == "RC-104"


def test_spec_kinds_and_fail_closed_table_match_the_code():
    text = SPEC.read_text()
    for kind in governance.KINDS:
        assert f"`{kind}`" in text and f"`hangar.{kind}`" in text
    assert set(governance.FAIL_CLOSED) == set(governance.KINDS)
    assert "allow" not in governance.FAIL_CLOSED.values()
