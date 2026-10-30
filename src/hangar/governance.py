"""The governance decision point: how hangar asks Tower whether an action may happen (spec 0015).

Hangar is the Run plane and enforces; the workspace is the Govern plane and decides. Five kinds of decision cross
that line: releasing a job that waits for a trusted person, a tool call a live turn asks about, arming a landing,
an egress request and a secret request. This module is the contract's client side, kept small on purpose:

* `DecisionRequest` / `Decision`: what hangar asks and what it gets back, with `to_wire` / `from_wire` for the
  `hangar.decision/v1` JSON the workspace speaks (`POST /v1/decisions`, `GET /v1/decisions/{id}`).
* `DecisionPoint`: the protocol every source of decisions implements (`decide`, `status`, `withdraw`).
* `LocalDecisionPoint`: today's hangar rules (approvals.can_decide), for `off` and break-glass.
* `HttpDecisionPoint`: the workspace over HTTP with fixed timeouts, a circuit breaker and the fail-closed table.
  Nothing that goes wrong on the way is ever an allow.
* `GovernanceRouter`: picks the source per kind and mode (`off`, `shadow`, `enforce`) and honours break-glass.
* `verify_event`: the signed approval events the workspace pushes (hints only: hangar re-reads the decision).

* `PassportCredential` / `PassportVerifier` (--- KAN-33, #174, spec 0015 §9 phase 2): the control plane's own
  passport, by RFC 8693 token exchange of a token hangar mints itself, checked before hangar uses it.

govrelease.py (job release, landing), governed.py (tool calls, egress and secret requests) and govevents.py (events)
use it. With every kind `off` (the default) Tower is never asked.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, get_args, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("hangar.governance")

SCHEMA = "hangar.decision/v1"
WIRE_PREFIX = "hangar."  # wire kind = "hangar." + kind ("hangar.job.release")
Kind = Literal["job.release", "tool.call", "landing.approve", "egress.request", "secret.request"]
KINDS: tuple[str, ...] = get_args(Kind)
Outcome = Literal["allow", "deny", "wait"]
Mode = Literal["off", "shadow", "enforce"]
MODES: tuple[str, ...] = get_args(Mode)
Failure = Literal["unreachable", "timeout", "server_error", "rate_limited", "auth", "contract", "malformed",
                  "circuit_open", "unknown_approval"]
# Workspace approval states, plus the two hangar adds for answers that didn't come from the workspace's policy.
Status = Literal["allowed", "approved", "blocked", "waiting", "denied", "expired", "withdrawn", "unavailable",
                 "local"]

CONNECT_TIMEOUT = 2.0  # seconds; spec 0015 §5
DECIDE_TIMEOUT = 5.0
DETAIL_MAX = 2000  # driver.DETAIL: what a person approving a tool call is shown
FILES_MAX = 300
CHECKS_MAX = 50  # gaw#18 caps attest.checks at 50
EVENT_TOLERANCE = 300  # seconds: a signed event older or newer than this is stale (gaw#17)
BREAK_GLASS_MAX = 24 * 3600

# What a kind falls back to when the workspace can't answer: never allow (spec 0015 §6). `wait` keeps the thing
# pending (the job stays awaiting_approval, the landing isn't armed, the request stays open); a tool call is denied,
# and its caller may ask again while the driver's own deadline allows (`retryable`).
FAIL_CLOSED: dict[str, Outcome] = {"job.release": "wait", "landing.approve": "wait", "egress.request": "wait",
                                   "secret.request": "wait", "tool.call": "deny"}


# ====================================================================== the request
class Party(BaseModel):
    """A person (or an outsider) as the workspace should know them. `person`: hangar's person id; `email`: a verified
    address from the person's linked identity, which the workspace maps to its user; `external`: a front door's
    stable id (`github:<numeric id>`, `jira:<account id>`, `webhook:<source>`), never a renameable login."""

    model_config = ConfigDict(extra="forbid")
    person: str | None = None
    email: str | None = None
    external: str | None = None


class Signer(Party):
    """A person who acted at one of hangar's own surfaces (`/hangar approve`, REST, a PR review). The workspace counts
    it as a signature only if its rules let that person sign (group, not the requester, how many signatures)."""

    via: Literal["mcp", "rest", "github", "jira", "slack", "system"]
    verdict: Literal["approve", "reject"] = "approve"


class Subject(BaseModel):
    """What the decision is about. `job`, `session`, `turn` and `run` are hangar's ids; the digests and the concrete
    model id come from the turn's manifest (spec 0014) and end up in the receipt's attestation (gaw#18)."""

    model_config = ConfigDict(extra="forbid")
    job: str | None = None
    session: str | None = None
    turn: int | None = None
    run: str | None = None
    repo: str | None = None  # owner/name (repopolicy.repo_key)
    environment: str | None = None
    pr: int | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    files: list[str] = Field(default_factory=list, max_length=FILES_MAX)
    author: Party | None = None  # landing: whose work the PR is (the App authors it; this is the job's person)
    harness_digest: str | None = None
    environment_digest: str | None = None
    model: str | None = None  # a concrete id; aliases are refused by the workspace (gaw#18)


class Action(BaseModel):
    """The action itself. `args_hash` is the lowercase hex SHA-256 of the harness's tool input as canonical JSON
    (`args_hash()`), the value receipts and trace spans carry; `detail` is what a person is shown, already redacted
    and clipped at both ends (driver.shown)."""

    model_config = ConfigDict(extra="forbid")
    tool: str | None = None
    args_hash: str | None = None
    detail: str = Field("", max_length=DETAIL_MAX + 100)
    risky: str | None = None  # why hangar's rules flag it (driver.risky / taint_sink)
    host: str | None = None  # egress.request
    secret: str | None = None  # secret.request: the variable's name, never its value
    scope: Literal["session", "environment"] | None = None


class AiReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    sha: str  # the head it reviewed; a review of an older head doesn't count
    harness: str | None = None
    model: str | None = None
    verdict: Literal["ok", "comments", "blocking"] | None = None


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=200)
    conclusion: str
    sha: str
    trusted: bool = False


class Context(BaseModel):
    model_config = ConfigDict(extra="forbid")
    untrusted_source: str | None = None  # Job.untrusted_source: who outside hangar wrote the task
    tainted: bool = False  # taint.tainted: the turn or job read untrusted content
    risk: Literal["low", "normal", "high"] | None = None  # landing.risk
    packet: Literal["docs", "tests", "code"] | None = None
    ai_review: AiReview | None = None
    checks: list[Check] = Field(default_factory=list, max_length=CHECKS_MAX)


class Correlation(BaseModel):
    """The correlation key (spec 0015 §9): hangar's ids and the W3C trace context of the span that asked."""

    model_config = ConfigDict(extra="forbid")
    traceparent: str | None = None
    job: str | None = None
    session: str | None = None
    turn: int | None = None
    run: str | None = None
    tool_call_id: str | None = None  # the harness's own id for the call (Claude Code tool_use_id)


class DecisionRequest(BaseModel):
    """One question for the decision point."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    kind: Kind
    idempotency_key: str = Field(min_length=1, max_length=300)
    requester: Party = Field(default_factory=Party)  # whose work it is (empty for a team job)
    signer: Signer | None = None
    subject: Subject = Field(default_factory=Subject)
    action: Action = Field(default_factory=Action)
    context: Context = Field(default_factory=Context)
    correlation: Correlation = Field(default_factory=Correlation)
    deadline: float | None = None  # epoch seconds; the workspace's expires_at is never later
    dry_run: bool = False  # shadow: evaluate only; no approval, no cards, no receipt
    # For LocalDecisionPoint only, never sent: the signer as approvals.Approver, and whose work it is as
    # approvals.Subject (Subject.of_job), which today's rules compare by hangar's own requester strings.
    approver: Any = Field(default=None, exclude=True)
    local_subject: Any = Field(default=None, exclude=True)

    def to_wire(self) -> dict[str, Any]:
        """The `hangar.decision/v1` JSON body. None and empty values are left out."""
        body = self.model_dump(mode="json", exclude_none=True, exclude={"approver", "local_subject", "deadline"})
        body = {"v": SCHEMA, **body, "kind": WIRE_PREFIX + self.kind}
        if self.deadline is not None:
            body["deadline"] = rfc3339(self.deadline)
        return _prune(body)

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> DecisionRequest:
        """Parse a wire body (the workspace's side of the contract, and the fake workspace in the tests)."""
        if body.get("v") != SCHEMA:
            raise ValueError(f"expected v={SCHEMA}")
        data = {k: v for k, v in body.items() if k != "v"}
        kind = str(data.get("kind") or "")
        if not kind.startswith(WIRE_PREFIX) or kind[len(WIRE_PREFIX):] not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        data["kind"] = kind[len(WIRE_PREFIX):]
        if data.get("deadline") is not None:
            data["deadline"] = parse_time(data["deadline"])
        return cls.model_validate(data)


# ====================================================================== the answer
class Decision(BaseModel):
    """What the decision point answered. `source`: who decided: the workspace, hangar's local rules, or nobody
    (`fail_closed`: the workspace couldn't answer and this is the kind's fallback, `failure` says why).
    `retryable`: asking again later may get a real answer."""

    outcome: Outcome
    reason: str = ""
    approval_id: str | None = None
    receipt: str | None = None
    expires_at: float | None = None
    status: Status | None = None
    rule: str | None = None
    source: Literal["workspace", "local", "fail_closed"] = "workspace"
    failure: Failure | None = None
    retryable: bool = False

    @property
    def settled(self) -> bool:
        return self.outcome != "wait"

    @classmethod
    def from_wire(cls, body: Any, *, require_receipt: bool = True) -> Decision:
        """Parse a 200 answer. Raises ValueError for anything that isn't a well-formed decision: an allow without a
        receipt (unless `require_receipt` is off: dry runs), a wait without an approval, an unknown decision."""
        if not isinstance(body, dict):
            raise ValueError("not a JSON object")
        outcome = body.get("decision")
        if outcome not in ("allow", "deny", "wait"):
            raise ValueError(f"unknown decision {outcome!r}")
        approval = _opt_str(body.get("approval"))
        receipt = _opt_str(body.get("receipt"))
        if outcome == "allow" and require_receipt and not receipt:
            raise ValueError("an allow without a receipt")
        if outcome == "wait" and not approval and require_receipt:
            raise ValueError("a wait without an approval to follow")
        status = body.get("status")
        if status is not None and status not in get_args(Status):
            raise ValueError(f"unknown status {status!r}")
        expires = body.get("expires_at")
        return cls(outcome=outcome, reason=str(body.get("reason") or "")[:2000], approval_id=approval,
                   receipt=receipt, expires_at=parse_time(expires) if expires is not None else None,
                   status=status, rule=_opt_str(body.get("rule")))

    def to_wire(self) -> dict[str, Any]:
        """The workspace's side: what a 200 answer looks like (used by the fake workspace in tests)."""
        return _prune({"v": SCHEMA, "decision": self.outcome, "approval": self.approval_id, "receipt": self.receipt,
                       "expires_at": rfc3339(self.expires_at) if self.expires_at is not None else None,
                       "status": self.status, "rule": self.rule, "reason": self.reason,
                       "poll": f"/v1/decisions/{self.approval_id}" if self.approval_id else None})


def fail_closed(kind: str, failure: Failure, reason: str, *, approval_id: str | None = None,
                retryable: bool = True) -> Decision:
    """The kind's fallback when the workspace didn't answer. Never an allow."""
    outcome = FAIL_CLOSED.get(kind, "deny")
    assert outcome != "allow"
    from hangar import slolog  # --- KAN-144: the "Tower down, failing closed" alert counts these

    slolog.fail_closed(kind, failure)
    return Decision(outcome=outcome, reason=f"approval service unavailable: {reason}", approval_id=approval_id,
                    status="unavailable", source="fail_closed", failure=failure, retryable=retryable)


# ====================================================================== the protocol
@runtime_checkable
class DecisionPoint(Protocol):
    async def decide(self, req: DecisionRequest) -> Decision:
        """Ask. Same `idempotency_key`, same approval while it waits."""
        ...

    async def status(self, approval_id: str, *, kind: Kind) -> Decision:
        """Where an approval stands (`kind` picks the fallback if the workspace can't say)."""
        ...

    async def withdraw(self, approval_id: str, reason: str) -> bool:
        """Close an approval nobody needs any more (hangar expired the job, a new head replaced the PR's)."""
        ...


# ====================================================================== local: today's hangar rules
_LOCAL_KIND = {"job.release": "approve", "landing.approve": "land", "egress.request": "egress"}


class LocalDecisionPoint:
    """Hangar's own approval rules, as before the workspace: the decision source for `off`, for kinds still in
    `shadow` (it answers; the workspace is only compared) and during break-glass.

    Without a signer every kind waits for a person, as today (the job waits in `awaiting_approval`, the tool call in
    the turn's pending approvals). With one, job release, landing and egress ask approvals.can_decide (admin, repo
    policy approver, front door; never your own work); a tool call or secret request is allowed for whoever answers,
    because the surfaces that take those answers (steer.answer, envrequests.decide) already checked that they may see
    the work. A rejection is always allowed through as a deny. Local decisions carry no receipt."""

    def __init__(self, ctx: Any = None, rules: dict[str, Callable[[DecisionRequest], Decision]] | None = None):
        self.ctx = ctx
        self.rules = dict(rules or {})

    async def decide(self, req: DecisionRequest) -> Decision:
        if rule := self.rules.get(req.kind):
            return rule(req)
        s = req.signer
        if s is None:
            return Decision(outcome="wait", reason="waiting for a person (hangar's own approvals)", status="waiting",
                            source="local")
        if s.verdict == "reject":
            return Decision(outcome="deny", reason=f"rejected by {s.person or s.external}", status="denied",
                            source="local", rule="reject")
        kind = _LOCAL_KIND.get(req.kind)
        if kind is None or self.ctx is None or req.approver is None:
            return Decision(outcome="allow", reason="answered by a person who may see the work", status="local",
                            source="local", rule="session_access")
        from hangar import approvals

        subject = req.local_subject or approvals.Subject(
            req.subject.author.person if req.subject.author else req.requester.person,
            frozenset(x for x in (req.requester.external,) if x), req.subject.repo,
            f"job {req.subject.job}" if req.subject.job else "this request")
        d = approvals.can_decide(self.ctx, req.approver, subject, kind)  # type: ignore[arg-type]
        if not d.ok:
            return Decision(outcome="deny", reason=d.why, status="denied", source="local")
        return Decision(outcome="allow", reason=f"allowed by hangar's rule {d.rule}", status="local",
                        source="local", rule=d.rule)

    async def status(self, approval_id: str, *, kind: Kind) -> Decision:
        return Decision(outcome="wait", reason="hangar's own approvals have no remote state", approval_id=approval_id,
                        status="waiting", source="local")

    async def withdraw(self, approval_id: str, reason: str) -> bool:
        return True


# ====================================================================== the workspace over HTTP
class Credential(Protocol):
    async def token(self, *, fresh: bool = False) -> str:
        """A bearer token for the workspace: the service key (phase 1), or a control-plane passport
        (`PassportCredential`, phase 2). Raises CredentialUnavailable when there is none to give."""
        ...


class CredentialUnavailable(Exception):
    """hangar couldn't get a credential for Tower (the passport exchange failed): the call fails closed (`auth`)."""


class StaticCredential:
    def __init__(self, key: str):
        self.key = key

    async def token(self, *, fresh: bool = False) -> str:
        return self.key


class HttpDecisionPoint:
    """The workspace's decision API. Every request has a 2 s connect and 5 s total budget; an answer that isn't a
    well-formed decision, any 5xx (whatever its body says), a timeout, a refused credential or an open circuit
    becomes the kind's fail-closed decision (`FAIL_CLOSED`). A 401 is retried once with a fresh token.

    The circuit opens after `breaker_failures` unavailable answers within `breaker_window` seconds and stays open for
    `breaker_cooldown` seconds: while it is open nothing is sent and every call fails closed at once, so an outage
    doesn't hold up 60 sessions behind 5 s timeouts."""

    def __init__(self, url: str, credential: Credential, *, client: httpx.AsyncClient | None = None,
                 connect_timeout: float = CONNECT_TIMEOUT, timeout: float = DECIDE_TIMEOUT,
                 breaker_failures: int = 5, breaker_window: float = 30.0, breaker_cooldown: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.url = url.rstrip("/")
        self.credential = credential
        self.timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self.budget = timeout + connect_timeout
        self._client = client
        self._own = client is None
        self.breaker_failures, self.breaker_window, self.breaker_cooldown = (breaker_failures, breaker_window,
                                                                             breaker_cooldown)
        self._failures: list[float] = []
        self._open_until = 0.0
        self._clock = clock

    async def close(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    # --- the circuit
    def _circuit_open(self) -> bool:
        return self._clock() < self._open_until

    def _failed(self) -> None:
        t = self._clock()
        self._failures = [f for f in self._failures if t - f < self.breaker_window] + [t]
        if len(self._failures) >= self.breaker_failures:
            self._open_until = t + self.breaker_cooldown
            self._failures.clear()
            log.warning("governance: circuit open for %.0f s after repeated failures", self.breaker_cooldown)

    def _ok(self) -> None:
        self._failures.clear()

    # --- the calls
    async def decide(self, req: DecisionRequest) -> Decision:
        headers = {"idempotency-key": req.idempotency_key}
        if req.correlation.traceparent:
            headers["traceparent"] = req.correlation.traceparent
        return await self._call("POST", "/v1/decisions", req.kind, json=req.to_wire(), headers=headers,
                                require_receipt=not req.dry_run)

    async def status(self, approval_id: str, *, kind: Kind) -> Decision:
        d = await self._call("GET", f"/v1/decisions/{approval_id}", kind, approval_id=approval_id)
        if d.source == "workspace" and d.approval_id and d.approval_id != approval_id:  # KAN-174: not the one asked
            return fail_closed(kind, "contract", f"asked about {approval_id}, answered about {d.approval_id}"[:300],
                               approval_id=approval_id, retryable=False)
        return d if d.approval_id or d.source != "workspace" else d.model_copy(update={"approval_id": approval_id})

    async def withdraw(self, approval_id: str, reason: str) -> bool:
        d = await self._call("POST", f"/v1/decisions/{approval_id}/withdraw", "job.release",
                             json={"v": SCHEMA, "reason": reason[:500]}, approval_id=approval_id,
                             require_receipt=False)
        return d.source == "workspace"

    async def _call(self, method: str, path: str, kind: str, *, json: Any = None, headers: dict[str, str] | None = None,
                    approval_id: str | None = None, require_receipt: bool = True) -> Decision:
        if self._circuit_open():
            return fail_closed(kind, "circuit_open", "the workspace failed repeatedly; not asking for a while",
                               approval_id=approval_id)
        fresh = False
        for _ in range(2):
            try:
                token = await self.credential.token(fresh=fresh)
            except CredentialUnavailable as e:  # --- KAN-33: no passport, no request
                self._failed()
                return fail_closed(kind, "auth", f"hangar has no credential for Tower: {e}"[:300],
                                   approval_id=approval_id)
            try:
                r = await asyncio.wait_for(
                    self._http().request(method, self.url + path, json=json, timeout=self.timeout,
                                         headers={**(headers or {}), "authorization": f"Bearer {token}"}),
                    self.budget)
            except (TimeoutError, httpx.TimeoutException) as e:
                self._failed()
                return fail_closed(kind, "timeout", f"no answer within {self.budget:g} s ({type(e).__name__})",
                                   approval_id=approval_id)
            except httpx.HTTPError as e:
                self._failed()
                return fail_closed(kind, "unreachable", f"{type(e).__name__}: {e}"[:300], approval_id=approval_id)
            if r.status_code == 401 and not fresh:
                fresh = True
                continue
            return self._answer(r, kind, approval_id, require_receipt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _answer(self, r: httpx.Response, kind: str, approval_id: str | None, require_receipt: bool) -> Decision:
        code = r.status_code
        if code >= 500:  # a 503 {"decision": "deny"} is the workspace failing, not its policy saying no
            self._failed()
            return fail_closed(kind, "server_error", f"HTTP {code}", approval_id=approval_id)
        if code == 429:
            self._failed()
            return fail_closed(kind, "rate_limited", "HTTP 429", approval_id=approval_id)
        if code in (401, 403):
            return fail_closed(kind, "auth", f"HTTP {code}: hangar's credential was refused", approval_id=approval_id,
                               retryable=False)
        if code == 404 and approval_id:
            return fail_closed(kind, "unknown_approval", f"the workspace doesn't know {approval_id}",
                               approval_id=approval_id)
        if code in (409, 410) and approval_id:  # withdraw of something already closed: it is closed
            self._ok()
            return Decision(outcome="deny", reason=_body_reason(r), approval_id=approval_id, status="withdrawn")
        if code != 200:
            return fail_closed(kind, "contract", f"HTTP {code}: {_body_reason(r)}"[:300], approval_id=approval_id,
                               retryable=False)
        try:
            body = r.json()
            if not require_receipt and isinstance(body, dict) and "decision" not in body:  # withdraw: {"ok": true}
                self._ok()
                return Decision(outcome="deny", reason="withdrawn", approval_id=approval_id, status="withdrawn")
            d = Decision.from_wire(body, require_receipt=require_receipt)
        except (ValueError, ValidationError) as e:
            self._failed()
            return fail_closed(kind, "malformed", f"an answer hangar can't read: {e}"[:300], approval_id=approval_id)
        self._ok()
        return d


# ====================================================================== passports (--- KAN-33, #174)
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # RFC 8693
JWT_TYPE = "urn:ietf:params:oauth:token-type:jwt"
CONTROL_PLANE_SUB = "hangar:control-plane"  # spec 0015 §9: the subject Tower trusts for agent `hangar`
PASSPORT_LEEWAY = 60  # seconds of clock skew, as Tower allows; also how early a cached passport is renewed
MAX_CHAIN = 3  # Tower's MAX_CHAIN: at most three agents behind one call
JWKS_REFETCH = 60.0  # an unknown key id refetches Tower's JWKS at most once a minute


class PassportRefused(Exception):
    """A passport hangar won't use or accept: why, safe to log (never the token)."""


def _b64d(part: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def act_chain(claims: dict[str, Any]) -> list[str]:
    """The agents an RFC 8693 `act` claim names, earliest first (the current actor is the outermost `act`, earlier
    ones nested inside it, §4.1). Ids without the `agent:` prefix. Raises PassportRefused for a malformed or too
    deep claim."""
    out: list[str] = []
    act = claims.get("act")
    while act is not None:
        if not isinstance(act, dict) or not isinstance(act.get("sub"), str) or not act["sub"].startswith("agent:"):
            raise PassportRefused("a malformed act claim")
        out.append(act["sub"][len("agent:"):])
        if len(out) > MAX_CHAIN:
            raise PassportRefused(f"an act chain longer than {MAX_CHAIN}")
        act = act.get("act")
    return out[::-1]


def check_chain(claims: dict[str, Any]) -> list[str]:
    """The passport's `chain` (earliest agent first), checked against `agent` and `act` the way Tower builds them
    (aw/workload.py `_passport`): `agent` is the last link; `act` names every link, or all but the first when the
    subject is that first agent itself. Raises PassportRefused."""
    chain = claims.get("chain")
    if not isinstance(chain, list) or not chain or not all(isinstance(a, str) and a for a in chain):
        raise PassportRefused("no chain of agents")
    if len(chain) > MAX_CHAIN:
        raise PassportRefused(f"a chain of {len(chain)} agents is longer than the {MAX_CHAIN} allowed")
    if len(set(chain)) != len(chain):
        raise PassportRefused("an agent appears twice in the chain")
    if claims.get("agent") != chain[-1]:
        raise PassportRefused("the passport's agent isn't the last link of its chain")
    want = chain[1:] if claims.get("sub") == f"agent:{chain[0]}" else chain
    if act_chain(claims) != want:
        raise PassportRefused("the act claim doesn't match the chain")
    return list(chain)


class PassportVerifier:
    """Checks a passport Tower signed (EdDSA, its `/.well-known/jwks.json`): the key, the signature, issuer,
    audience, expiry and not-before with `PASSPORT_LEEWAY`, and the chain of agents (`check_chain`). An unknown key
    id refetches the JWKS at most once a minute."""

    def __init__(self, issuer: str, audience: str | list[str], *, jwks_url: str | None = None,
                 client: httpx.AsyncClient | None = None, clock: Callable[[], float] = time.time):
        self.issuer = issuer.rstrip("/")
        self.audiences = [audience] if isinstance(audience, str) else list(audience)
        self.jwks_url = jwks_url or f"{self.issuer}/.well-known/jwks.json"
        self._client, self._own = client, client is None
        self.clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched = float("-inf")

    async def close(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _refresh(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if self.clock() - self._fetched < JWKS_REFETCH:
            return
        self._fetched = self.clock()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(DECIDE_TIMEOUT, connect=CONNECT_TIMEOUT))
        try:
            r = await self._client.get(self.jwks_url)
            keys = r.json().get("keys") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            raise PassportRefused(f"Tower's JWKS couldn't be read ({type(e).__name__})") from None
        if not isinstance(keys, list):
            raise PassportRefused(f"Tower's JWKS answered HTTP {r.status_code}")
        found: dict[str, Any] = {}
        for k in keys:
            if isinstance(k, dict) and k.get("kty") == "OKP" and k.get("crv") == "Ed25519" and k.get("kid"):
                try:
                    found[str(k["kid"])] = Ed25519PublicKey.from_public_bytes(_b64d(str(k.get("x", ""))))
                except ValueError:
                    continue
        self._keys = found

    async def verify(self, token: str) -> dict[str, Any]:
        """The passport's claims, or PassportRefused."""
        from cryptography.exceptions import InvalidSignature

        try:
            head_b, body_b, sig_b = token.split(".")
            header, claims = json.loads(_b64d(head_b)), json.loads(_b64d(body_b))
            sig = _b64d(sig_b)
        except (ValueError, json.JSONDecodeError):
            raise PassportRefused("not a JWT") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise PassportRefused("not a JWT")
        if header.get("alg") != "EdDSA":
            raise PassportRefused(f"algorithm {header.get('alg')!r}, not EdDSA")
        kid = str(header.get("kid") or "")
        if kid not in self._keys:
            await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise PassportRefused("signed with a key Tower doesn't publish")
        try:
            key.verify(sig, f"{head_b}.{body_b}".encode())
        except InvalidSignature:
            raise PassportRefused("bad signature") from None
        if str(claims.get("iss") or "").rstrip("/") != self.issuer:
            raise PassportRefused("from another issuer")
        aud = claims.get("aud")
        if not any(a in self.audiences for a in (aud if isinstance(aud, list) else [aud])):
            raise PassportRefused("for another audience")
        now_ = self.clock()
        exp, nbf = claims.get("exp"), claims.get("nbf", claims.get("iat"))
        if not isinstance(exp, int | float) or exp + PASSPORT_LEEWAY < now_:
            raise PassportRefused("expired")
        if isinstance(nbf, int | float) and nbf - PASSPORT_LEEWAY > now_:
            raise PassportRefused("not valid yet")
        check_chain(claims)
        return claims


class PassportCredential:
    """The control plane's passport for Tower (spec 0015 §9 phase 2): hangar mints itself a short-lived token
    (`mint`: `sub: hangar:control-plane`, `aud` = Tower's token endpoint), exchanges it at `token_url` (RFC 8693)
    for a 15-minute passport, checks it (`verifier`; it must name `agent`), and caches it until `PASSPORT_LEEWAY`
    seconds before it expires. `fresh=True` (after a 401) exchanges again. Any failure is CredentialUnavailable,
    which the decision point turns into its kind's fail-closed answer."""

    def __init__(self, mint: Callable[[], str], token_url: str, *, agent: str = "hangar",
                 verifier: PassportVerifier | None = None, client: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.time):
        self.mint, self.token_url, self.agent, self.verifier = mint, token_url, agent, verifier
        self._client, self._own = client, client is None
        self.clock = clock
        self._passport: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self.verifier is not None:
            await self.verifier.close()

    async def token(self, *, fresh: bool = False) -> str:
        async with self._lock:
            if not fresh and self._passport and self.clock() < self._expires - PASSPORT_LEEWAY:
                return self._passport
            self._passport, self._expires = await self._exchange()
            return self._passport

    async def _exchange(self) -> tuple[str, float]:
        try:
            subject = self.mint()
        except Exception as e:  # noqa: BLE001 - no signing key, no issuer URL
            raise CredentialUnavailable(f"hangar couldn't mint its own token ({type(e).__name__}: {e})"[:300]) from None
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(DECIDE_TIMEOUT, connect=CONNECT_TIMEOUT))
        try:
            r = await self._client.post(self.token_url, data={
                "grant_type": TOKEN_EXCHANGE, "subject_token": subject, "subject_token_type": JWT_TYPE,
                "requested_token_type": JWT_TYPE})
        except httpx.HTTPError as e:
            raise CredentialUnavailable(f"the token exchange failed ({type(e).__name__})") from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
            why = str(body.get("error") or "") if isinstance(body, dict) else ""
            raise CredentialUnavailable(f"Tower refused the token exchange: HTTP {r.status_code}"
                                        + (f" {why}"[:100] if why else ""))
        passport = body["access_token"]
        exp = self.clock() + float(body.get("expires_in") or 0)
        if self.verifier is not None:
            try:
                claims = await self.verifier.verify(passport)
            except PassportRefused as e:
                raise CredentialUnavailable(f"Tower's passport didn't check out: {e}") from None
            if claims.get("agent") != self.agent:
                raise CredentialUnavailable(f"Tower's passport names agent {claims.get('agent')!r}, not {self.agent!r}")
            exp = float(claims["exp"])
        if exp <= self.clock():
            raise CredentialUnavailable("Tower's passport has no lifetime")
        log.info("governance: exchanged a passport for agent %s, valid %.0f s", self.agent, exp - self.clock())
        return passport, exp


# ====================================================================== waiting
async def wait_for(dp: DecisionPoint, first: Decision, kind: Kind, deadline: float, *,
                   sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, clock: Callable[[], float] = time.time,
                   start: float = 2.0, cap: float = 60.0, woken: asyncio.Event | None = None) -> Decision:
    """Follow a `wait` until it settles or `deadline` (epoch seconds) passes, polling `status` with exponential
    backoff (`start`, doubling, at most `cap`, with jitter). `woken`: set by the event receiver when the workspace
    says the approval changed, so a webhook cuts the wait short; polling is only the fallback. Past the deadline the
    answer is the kind's fail-closed decision (`deny` for a tool call, `wait` for the rest: still pending)."""
    d, delay = first, start
    while d.outcome == "wait" and d.approval_id:
        left = deadline - clock()
        if left <= 0:
            return fail_closed(kind, "timeout", f"{d.approval_id} wasn't decided before hangar's deadline",
                               approval_id=d.approval_id, retryable=False)
        pause = min(delay * (0.8 + 0.4 * random.random()), left)
        if woken is not None:
            woken.clear()
            try:
                await asyncio.wait_for(woken.wait(), pause)
            except TimeoutError:
                pass
        else:
            await sleep(pause)
        delay = min(delay * 2, cap)
        nxt = await dp.status(d.approval_id, kind=kind)
        if nxt.source == "fail_closed" and nxt.failure != "unknown_approval":
            continue  # keep the approval we know about; the workspace being down isn't a decision
        d = nxt
    return d


# ====================================================================== modes and break-glass
class BreakGlass(BaseModel):
    """`hangar governance local --until`: hangar's local rules decide while the workspace is down. Two different
    admins (one starts it, another confirms), at most 24 hours, audited; a decision taken under it is replayed to
    the workspace as a record once it is back (spec 0015 §7)."""

    started_by: str
    confirmed_by: str | None = None
    reason: str = Field(min_length=1, max_length=500)
    started_at: float
    until: float

    def model_post_init(self, _: Any) -> None:
        if self.until <= self.started_at or self.until - self.started_at > BREAK_GLASS_MAX:
            raise ValueError("break-glass lasts more than 0 and at most 24 hours")
        if self.confirmed_by is not None and self.confirmed_by == self.started_by:
            raise ValueError("break-glass needs a second admin to confirm it")

    def active(self, t: float | None = None) -> bool:
        t = time.time() if t is None else t
        return self.confirmed_by is not None and self.started_at <= t < self.until


def parse_modes(default: str, overrides: str | dict[str, str] | None = None) -> dict[str, Mode]:
    """`HANGAR_GOVERNANCE` (the default mode) and `HANGAR_GOVERNANCE_MODES` (`{"tool.call": "shadow"}` or
    `tool.call=shadow,landing.approve=enforce`) as a mode per kind."""
    if default not in MODES:
        raise ValueError(f"HANGAR_GOVERNANCE must be one of {', '.join(MODES)}")
    out: dict[str, Mode] = dict.fromkeys(KINDS, default)  # type: ignore[arg-type]
    if isinstance(overrides, str):
        text = overrides.strip()
        overrides = json.loads(text) if text.startswith("{") else dict(
            p.split("=", 1) for p in (x.strip() for x in text.split(",")) if p)
    for k, m in (overrides or {}).items():
        if k not in KINDS or m not in MODES:
            raise ValueError(f"bad governance mode {k}={m}")
        out[k] = m  # type: ignore[assignment]
    return out


OnDisagree = Callable[[DecisionRequest, Decision, Decision], Any]


class GovernanceRouter:
    """The one object approval paths call. Per kind:

    * `off`: the local rules decide. The workspace isn't asked.
    * `shadow`: the local rules decide; the workspace is asked the same question as a dry run in the background
      (sampled by `shadow_sample`, at most `shadow_inflight` at a time) and a different outcome calls `on_disagree`
      (audited `governance.disagree`). Shadow never changes an outcome and never waits for the workspace.
    * `enforce`: the workspace decides, failing closed. Under an active break-glass the local rules decide and
      `on_break_glass` records it for replay."""

    def __init__(self, local: DecisionPoint, remote: DecisionPoint | None, modes: dict[str, Mode], *,
                 on_disagree: OnDisagree | None = None, on_break_glass: Callable[[DecisionRequest, Decision], Any]
                 | None = None, shadow_sample: dict[str, float] | None = None, shadow_inflight: int = 8):
        if remote is None and any(m != "off" for m in modes.values()):
            raise ValueError("shadow or enforce needs the workspace's URL (HANGAR_GOVERNANCE_URL)")
        self.local, self.remote, self.modes = local, remote, modes
        self.on_disagree, self.on_break_glass = on_disagree, on_break_glass
        self.shadow_sample = shadow_sample or {}
        self._slots = asyncio.Semaphore(shadow_inflight)
        self._tasks: set[asyncio.Task[Any]] = set()
        self.break_glass: BreakGlass | None = None

    def mode(self, kind: str) -> Mode:
        return self.modes.get(kind, "off")

    async def decide(self, req: DecisionRequest) -> Decision:
        mode = self.mode(req.kind)
        if mode == "enforce" and self.remote is not None:
            if self.break_glass is not None and self.break_glass.active():
                d = await self.local.decide(req)
                if self.on_break_glass is not None:
                    self.on_break_glass(req, d)
                return d
            return await self.remote.decide(req)
        d = await self.local.decide(req)
        if mode == "shadow" and self.remote is not None and random.random() < self.shadow_sample.get(req.kind, 1.0):
            self._spawn(self._shadow(req, d))
        return d

    async def status(self, approval_id: str, *, kind: Kind) -> Decision:
        dp = self.remote if self.mode(kind) == "enforce" and self.remote is not None else self.local
        return await dp.status(approval_id, kind=kind)

    async def withdraw(self, approval_id: str, reason: str, *, kind: Kind) -> bool:
        dp = self.remote if self.mode(kind) == "enforce" and self.remote is not None else self.local
        return await dp.withdraw(approval_id, reason)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for shadow comparisons in flight (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _shadow(self, req: DecisionRequest, local: Decision) -> None:
        if self._slots.locked():
            return  # shed load rather than queue: shadow is a sample
        async with self._slots:
            assert self.remote is not None
            remote = await self.remote.decide(req.model_copy(update={"dry_run": True}))
        if remote.source == "fail_closed" or self.on_disagree is None:
            return
        if remote.outcome != local.outcome:
            try:
                self.on_disagree(req, local, remote)
            except Exception:  # an audit failure mustn't break the local decision
                log.exception("governance: recording a disagreement failed")


# ====================================================================== keys and hashes
def args_hash(args: Any) -> str:
    """The arguments hash receipts and trace spans share: lowercase hex SHA-256 of canonical JSON, exactly as the
    workspace computes it (`aw.store.args_hash`)."""
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def idempotency_key(kind: Kind, *, job: str | None = None, session: str | None = None, head_sha: str | None = None,
                    request: str | None = None) -> str:
    """Spec 0015 §4.3. One approval per thing a person decides on:

    * `hangar:job:<job>:release`
    * `hangar:job:<job>:landing:<head sha>` (a new head is a new question)
    * `hangar:session:<session>:tool:<driver approval id>`
    * `hangar:session:<session>:egress:<request id>` and `...:secret:<request id>` (envrequests)"""
    if kind == "job.release" and job:
        return f"hangar:job:{job}:release"
    if kind == "landing.approve" and job and head_sha:
        return f"hangar:job:{job}:landing:{head_sha}"
    if kind in ("tool.call", "egress.request", "secret.request") and session and request:
        return f"hangar:session:{session}:{kind.split('.')[0]}:{request}"
    raise ValueError(f"not enough to key a {kind} decision")


# ====================================================================== events from the workspace
class EventRejected(Exception):
    """A pushed event hangar won't act on: bad or missing signature, stale, replayed, malformed."""


class GovernanceEvent(BaseModel):
    """`approval.created`, `approval.decided` (gaw#17) and `run.stop` (gaw#22). An approval event is a hint: the
    receiver re-reads the decision with `status` and acts on that, so a forged or reordered event can't approve
    anything. A `run.stop` only takes capability away, and is acted on once its signature checks out."""

    model_config = ConfigDict(extra="allow")
    id: str
    kind: Literal["approval.created", "approval.decided", "run.stop"]
    at: str
    approval: dict[str, Any] | None = None
    corr: dict[str, Any] | None = None
    run: dict[str, Any] | None = None


def sign_event(secret: str, body: bytes, t: int) -> str:
    """The `X-AW-Signature` header value for `body` at unix time `t` (gaw#17): `t=<t>,v1=<hex HMAC-SHA256>`."""
    mac = hmac.new(secret.encode(), str(t).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_event(secret: str, headers: dict[str, str], body: bytes, seen: MutableMapping[str, float], *,
                 now: float | None = None, tolerance: int = EVENT_TOLERANCE) -> GovernanceEvent:
    """Check a pushed event: the HMAC over `t + "." + body` with the subscription's secret, `t` within `tolerance`
    seconds, the event id not seen before (`seen`: event id -> when, kept for at least the workspace's 24 h retry
    window). Raises EventRejected. Header names are matched case-insensitively."""
    if not secret:
        raise EventRejected("no webhook secret configured")
    h = {k.lower(): v for k, v in headers.items()}
    parts = dict(p.split("=", 1) for p in h.get("x-aw-signature", "").split(",") if "=" in p)
    try:
        t = int(parts.get("t", ""))
    except ValueError:
        raise EventRejected("unsigned") from None
    now = time.time() if now is None else now
    if abs(now - t) > tolerance:
        raise EventRejected("stale signature")
    want = sign_event(secret, body, t).split("v1=", 1)[1]
    if not hmac.compare_digest(want, parts.get("v1", "")):
        raise EventRejected("bad signature")
    try:
        ev = GovernanceEvent.model_validate_json(body)
    except ValidationError as e:
        raise EventRejected(f"malformed event: {e.error_count()} errors") from None
    if h.get("x-aw-event-id") not in (None, ev.id):
        raise EventRejected("event id header and body disagree")
    if ev.id in seen:
        raise EventRejected("replayed")
    seen[ev.id] = now
    return ev


# ====================================================================== helpers
def rfc3339(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_time(v: Any) -> float:
    """RFC 3339 (`Z` or an offset, as the workspace's `iso()` writes) or epoch seconds."""
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str) or not v:
        raise ValueError(f"not a time: {v!r}")
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"a time without a zone: {v!r}")
    return dt.timestamp()


def _opt_str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _body_reason(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    return str(body.get("reason") or body.get("error") or body)[:200] if isinstance(body, dict) else str(body)[:200]


def _prune(v: Any) -> Any:
    """Drop None, empty strings, empty lists/dicts and False flags (except meaningful booleans kept by callers)."""
    if isinstance(v, dict):
        out = {k: _prune(x) for k, x in v.items()}
        return {k: x for k, x in out.items() if x not in (None, "", [], {}) and not (x is False and k != "trusted")}
    if isinstance(v, list):
        return [_prune(x) for x in v]
    return v
