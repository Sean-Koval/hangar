"""The `governed` approval policy: hangar's flagged tool calls, egress and secret requests ask Tower (KAN-79).

Spec 0015 §2, §5-§7. Under `approvals: governed` the turn's driver allows at once what hangar's rules don't flag and
holds the rest (`driver.risky`, the taint gate's `taint_sink`) as it holds every call under `ask`. While the turn
runs, `Governed.tick` relays each held call as `governance.decide(kind="tool.call")` with the driver's `args_hash`,
the run's correlation and the taint flag. Egress and secret requests the agent files (`envrequests.py`) are relayed
as `egress.request` and `secret.request`, whatever the session's approval policy: spec 0015 sets those per kind.

What the answer does depends on the kind's mode (`HANGAR_GOVERNANCE`, `HANGAR_GOVERNANCE_MODES`):

* `off`: Tower isn't asked. A held call waits for a person, as under `ask`; a request waits for its owner.
* `shadow`: hangar's own policy decides (a person answers). Tower is asked the same question as a dry run and both
  answers are kept on the approval or request (`governance`), audited `governance.shadow`, and a difference also
  `governance.disagree`. Tower failing changes nothing: the record says hangar fell back to its own policy.
* `enforce`: Tower decides. `allow` (always with a receipt) answers the call or grants the request, `deny` refuses
  with Tower's reason, and `wait` hands a tool call to `govevents.Receiver.watch` (suspend, resume, pre-clear) and
  keeps a request pending until its approval settles (an event, or the poll here). Nothing that goes wrong is ever an
  allow: an unreachable or misconfigured Tower denies the call (`hangar.slo: event=fail_closed`) and keeps a request
  pending. A person answering in hangar is sent to Tower as a signer (§4.4): Tower counts it or says why not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from hangar import steer as steering
from hangar.governance import (
    DETAIL_MAX,
    Action,
    Context,
    Correlation,
    Decision,
    DecisionRequest,
    LocalDecisionPoint,
    Party,
    Signer,
    Subject,
    fail_closed,
    idempotency_key,
    parse_modes,
)
from hangar.govevents import Unavailable
from hangar.models import now

if TYPE_CHECKING:
    from hangar.envrequests import EnvRequest
    from hangar.models import Approval, Session, SessionTurn

log = logging.getLogger("hangar.governed")

REQ = "gov-req:"  # kv: Tower approval id -> the environment request waiting on it
POLL = 60.0  # seconds between status reads of a waiting request, and retries of one Tower couldn't answer
SIGNER_VIA = ("mcp", "rest", "github", "jira", "slack", "system")


def record(mode: str, d: Decision, *, local: Decision | None = None, tower: Decision | None = None) -> dict[str, Any]:
    """What was asked and answered, kept on the approval or request (`governance`) and shown with it."""
    def part(x: Decision) -> dict[str, Any]:
        return {k: v for k, v in {"outcome": x.outcome, "reason": x.reason[:500], "rule": x.rule, "status": x.status,
                                  "approval": x.approval_id, "receipt": x.receipt, "failure": x.failure}.items()
                if v not in (None, "")}

    by = "hangar" if d.source == "local" else ("tower" if d.source == "workspace" else "fail_closed")
    out: dict[str, Any] = {"mode": mode, "decided_by": by, **part(d), "at": now()}
    if local is not None:
        out["hangar"] = part(local)
    if tower is not None:
        out["tower"] = part(tower)
        if tower.source == "fail_closed" and mode == "shadow":
            out["fallback"] = f"Tower couldn't answer ({tower.failure}); hangar's own policy decided"
    return out


class Governed:
    """One per control plane: `sessions.governed`, `env_requests.governed` and `receiver.governed` point here."""

    def __init__(self, sessions: Any, receiver: Any, env_requests: Any = None):
        self.sessions, self.receiver, self.requests = sessions, receiver, env_requests
        self.fleet, self.settings, self.store = sessions.fleet, sessions.settings, sessions.store
        self.local = LocalDecisionPoint()
        self._asked: set[str] = set()  # driver approval ids relayed by this process
        self._polled: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        sessions.governed = self
        receiver.governed = self
        if env_requests is not None:
            env_requests.governed = self

    # ================================================================== asking
    def mode(self, kind: str) -> str:
        """The kind's mode. A setting hangar can't read counts as `enforce`: Tower decides, failing closed."""
        try:
            return parse_modes(self.settings.governance, self.settings.governance_modes or None).get(kind, "off")
        except ValueError as e:
            log.error("governance: %s; treating %s as enforce", e, kind)
            return "enforce"

    async def tower_decide(self, req: DecisionRequest) -> Decision:
        try:
            tower = self.receiver.tower()
        except Unavailable as e:
            return fail_closed(req.kind, "unreachable", str(e), retryable=False)
        try:
            return await tower.decide(req)
        except Exception as e:  # noqa: BLE001 - a client bug is no allow either
            log.exception("governance: deciding %s failed", req.idempotency_key)
            return fail_closed(req.kind, "unreachable", f"{type(e).__name__}: {e}"[:300])

    async def ask(self, req: DecisionRequest, mode: str) -> tuple[Decision, dict[str, Any]]:
        """The decision that counts, and the record of what was asked (spec 0015 §7)."""
        if mode == "enforce":
            d = await self.tower_decide(req)
            return d, record(mode, d, tower=d)
        local = await self.local.decide(req)
        if mode != "shadow":
            return local, record(mode, local, local=local)
        tower = await self.tower_decide(req.model_copy(update={"dry_run": True}))
        rec = record(mode, local, local=local, tower=tower)
        detail = {"kind": req.kind, "key": req.idempotency_key, "hangar": local.outcome, "tower": tower.outcome,
                  "tower_rule": tower.rule, "tower_reason": tower.reason[:300], "failure": tower.failure}
        self.fleet.audit("hangar", "governance.shadow", req.subject.session, **detail)
        if tower.source == "workspace" and tower.outcome != local.outcome:
            self.fleet.audit("hangar", "governance.disagree", req.subject.session, **detail)
            with contextlib.suppress(Exception):
                from hangar import metrics

                metrics.M.governance_disagreements.labels(req.kind).inc()
        return local, rec

    def signer(self, requester: str, verdict: str = "approve") -> Signer:
        """The person answering at a hangar surface, as Tower should know them (§4.4)."""
        from hangar import approvals, identity

        who = approvals.from_principal(identity.current.get(), "rest")
        return Signer(person=who.person, external=None if who.person else requester[:200],
                      via=who.via if who.via in SIGNER_VIA else "system", verdict=verdict)  # type: ignore[arg-type]

    # ================================================================== tool calls
    def tool_request(self, s: Session, turn: SessionTurn, a: Approval, signer: Signer | None = None
                     ) -> DecisionRequest:
        from hangar import taint
        from hangar.repopolicy import repo_key

        n = next((i for i, t in enumerate(s.turns, 1) if t is turn), len(s.turns))
        return DecisionRequest(
            kind="tool.call", idempotency_key=idempotency_key("tool.call", session=s.id, request=a.id),
            requester=Party(person=s.person), signer=signer,
            subject=Subject(job=s.job_id, session=s.id, turn=n, run=turn.run_id, repo=repo_key(s.repo),
                            environment=s.environment, model=s.model),
            action=Action(tool=a.tool or None, args_hash=a.args_hash, risky=a.risky or a.taint,
                          detail=str(self.fleet.broker.redact(a.detail or ""))[:DETAIL_MAX]),
            context=Context(tainted=bool(a.taint) or taint.tainted(s) or taint.tainted(turn)),
            correlation=Correlation(job=s.job_id, session=s.id, turn=n, run=turn.run_id),
            deadline=a.expires_at)

    def tick(self, s: Session, turn: SessionTurn) -> None:
        """While a `governed` turn runs: relay each call the driver holds for hangar. Never raises."""
        if turn.approval_policy != "governed" or not turn.live or turn.finished_at:
            return
        for a in turn.approvals:
            if a.status != "pending" or a.id in self._asked or not (a.risky or a.taint):
                continue
            if a.governance and a.governance.get("outcome"):
                continue  # relayed before a restart, and answered
            self._asked.add(a.id)
            a.governance = {"mode": self.mode("tool.call"), "state": "asking", "at": now()}
            self._spawn(self.relay(s, turn, a))

    async def relay(self, s: Session, turn: SessionTurn, a: Approval) -> Decision | None:
        mode = self.mode("tool.call")
        try:
            d, rec = await self.ask(self.tool_request(s, turn, a), mode)
        except Exception as e:  # noqa: BLE001 - e.g. a request the contract refuses: no allow
            log.exception("session %s: asking about %s failed", s.id, a.id)
            if mode != "enforce":
                a.governance = {"mode": mode, "decided_by": "hangar", "outcome": "wait", "at": now(),
                                "fallback": f"asking Tower failed ({type(e).__name__}); a person answers"}
                self.sessions._save(s)
                return None
            d = fail_closed("tool.call", "contract", f"{type(e).__name__}: {e}"[:300], retryable=False)
            rec = record(mode, d, tower=d)
        a.governance = rec
        if mode != "enforce":  # a person answers, as under `ask`
            self.sessions._save(s)
            if mode == "shadow":
                tw = rec.get("tower", {})
                self._event(s, f"Tower (shadow) would answer {a.tool}: {tw.get('outcome', '?')}"
                               + (f" ({tw['reason']})" if tw.get("reason") else ""), approval=a.id)
            return d
        a.workspace_approval = d.approval_id or a.workspace_approval
        self.sessions._save(s)
        self.fleet.audit("tower" if d.source == "workspace" else "hangar", f"governance.tool.{d.outcome}", s.id,
                         approval=a.id, tower_approval=d.approval_id, receipt=d.receipt, tool=a.tool,
                         failure=d.failure, reason=d.reason[:300])
        if d.outcome == "allow":
            await self._answer(s, a, "allow", "", receipt=d.receipt, approval=d.approval_id)
        elif d.outcome == "deny":
            msg = (f"Denied by Tower ({d.approval_id or d.rule or 'policy'}): {d.reason}" if d.source == "workspace"
                   else f"Denied by hangar: {d.reason}. Tower decides this call and couldn't be asked, so it is "
                        "refused (fail-closed).")
            await self._answer(s, a, "deny", msg)
        else:
            await self.receiver.watch(s, turn, a, d)
        return d

    async def _answer(self, s: Session, a: Approval, decision: str, message: str, **extra: Any) -> None:
        try:
            done = await steering.answer(self.sessions, s, a.id, decision, "tower", message[:2000] or None)
        except (steering.SteerError, KeyError) as e:  # the driver's deadline, or the turn, ended first
            log.info("session %s: could not answer %s: %s", s.id, a.id, e)
            return
        if extra.get("receipt"):
            for x in {id(a): a, id(done): done}.values():
                x.receipt, x.workspace_approval = extra["receipt"], extra.get("approval") or x.workspace_approval
            self.sessions._save(s)

    async def sign_tool(self, s: Session, turn: SessionTurn, a: Approval, requester: str, decision: str) -> None:
        """A person answered a held call in hangar (steer.answer). In `enforce` an allow is a signature Tower judges
        (§4.4): it counts, needs more, or is refused (SteerError). A deny always goes through."""
        if turn.approval_policy != "governed" or not (a.risky or a.taint) or self.mode("tool.call") != "enforce":
            return
        if decision == "deny":
            if a.workspace_approval:
                self._spawn(self._withdraw(a.workspace_approval, f"denied in hangar by {requester}"))
            return
        if decision == "allow_always":
            raise steering.SteerError("allow_always isn't available under approvals=governed: Tower decides each "
                                      "call; answer allow")
        d = await self.tower_decide(self.tool_request(s, turn, a, signer=self.signer(requester)))
        a.governance = record("enforce", d, tower=d)
        if d.outcome != "deny":  # a refused signature leaves the approval the call waits on as it was
            a.workspace_approval = d.approval_id or a.workspace_approval
        self.sessions._save(s)
        if d.outcome == "allow":
            a.receipt = d.receipt
            return
        if d.outcome == "wait":
            raise steering.SteerError(f"Tower took your answer on {d.approval_id}, and the call still waits: "
                                      f"{d.reason}")
        raise steering.SteerError(f"Tower refused it: {d.reason}")

    async def _withdraw(self, approval: str, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.receiver.tower().withdraw(approval, reason)

    # ================================================================== egress and secret requests
    def request_request(self, r: EnvRequest, signer: Signer | None = None) -> DecisionRequest:
        from hangar import taint

        s = self._session(r.session_id)
        kind = f"{r.kind}.request"
        target = r.target
        return DecisionRequest(
            kind=kind,  # type: ignore[arg-type]
            idempotency_key=idempotency_key(kind, session=r.session_id, request=r.id),  # type: ignore[arg-type]
            requester=Party(person=r.owner), signer=signer,
            subject=Subject(job=r.job_id, session=r.session_id, turn=r.turn or None, environment=r.environment),
            action=Action(host=r.host if r.kind == "egress" else None, secret=r.name if r.kind == "secret" else None,
                          scope=r.scope, detail=f"{target}: {r.why}"[:DETAIL_MAX]),
            context=Context(tainted=taint.tainted(s) if s is not None else False),
            correlation=Correlation(job=r.job_id, session=r.session_id, turn=r.turn or None))

    def filed(self, r: EnvRequest) -> None:
        """A request the agent just filed (envrequests._scan). Never raises."""
        if self.mode(f"{r.kind}.request") != "off":
            self._spawn(self.relay_request(r))

    async def relay_request(self, r: EnvRequest) -> Decision | None:
        from hangar.envrequests import EnvRequest

        mode = self.mode(f"{r.kind}.request")
        try:
            d, rec = await self.ask(self.request_request(r), mode)
        except Exception:  # noqa: BLE001 - the request stays pending: never granted on an error
            log.exception("request %s: asking Tower failed", r.id)
            return None
        cur = self.store.get(EnvRequest, r.id)
        if cur is None or cur.state != "pending":
            return d  # a person answered meanwhile
        if mode != "enforce":
            cur.governance = rec
            self.store.put(cur)
            return d
        return await self._settle_request(cur, d)

    async def _settle_request(self, r: EnvRequest, d: Decision) -> Decision:
        """Act on Tower's answer for a pending request (enforce)."""
        r.governance = {**(r.governance or {}), **record("enforce", d, tower=d)}
        if d.outcome == "allow":
            self._unfollow(r)
            await self.requests.apply(r, True, "tower", note=d.reason or None, receipt=d.receipt,
                                      approval=d.approval_id)
        elif d.outcome == "deny" and d.status in ("expired", "withdrawn"):
            self._unfollow(r)  # nobody decided: the request waits; a person's answer asks again (a new approval)
            r.workspace_approval = None
            self.store.put(r)
            self._req_event(r, f"approval {d.approval_id} {d.status} in Tower; the request for {r.target} waits")
        elif d.outcome == "deny":
            self._unfollow(r)
            await self.requests.apply(r, False, "tower", note=f"Denied in Tower: {d.reason}"[:2000],
                                      approval=d.approval_id)
        elif d.source == "fail_closed":  # stays pending; retried by the poll, the owner told once per outage
            told = (r.governance or {}).get("told")
            r.governance["told"] = told or now()
            self.store.put(r)
            if not told:
                self._req_event(r, f"approval service unavailable: the request for {r.target} stays pending "
                                   f"({d.failure})")
        else:
            first = r.workspace_approval != d.approval_id
            r.workspace_approval = d.approval_id
            self.store.put(r)
            if d.approval_id:
                self.store.kv_set(REQ + d.approval_id, r.id, durable=True)
            if first:
                self._req_event(r, f"waiting for approval {d.approval_id} in Tower: {d.reason}"[:500])
        return d

    async def sign_request(self, r: EnvRequest, approve: bool, actor: str) -> Decision | None:
        """A person answered a request in hangar (envrequests.decide). In `enforce` an approval is a signature Tower
        judges; it raises RequestError unless Tower allows. A deny always goes through, and withdraws Tower's."""
        from hangar.envrequests import RequestError

        if self.mode(f"{r.kind}.request") != "enforce":
            return None
        if not approve:
            if r.workspace_approval:
                self._unfollow(r)
                self._spawn(self._withdraw(r.workspace_approval, f"denied in hangar by {actor}"))
            return None
        d = await self.tower_decide(self.request_request(r, signer=self.signer(actor)))
        r.governance = record("enforce", d, tower=d)
        if d.outcome == "allow":
            self._unfollow(r)
            r.receipt, r.workspace_approval = d.receipt, d.approval_id or r.workspace_approval
            return d
        if d.outcome == "wait" and d.approval_id:
            r.workspace_approval = d.approval_id
            self.store.put(r)
            self.store.kv_set(REQ + d.approval_id, r.id, durable=True)
            raise RequestError(f"Tower took your answer on {d.approval_id}, and the request still waits: {d.reason}")
        self.store.put(r)
        raise RequestError(f"Tower refused it: {d.reason}")

    def _unfollow(self, r: EnvRequest) -> None:
        if r.workspace_approval:
            self.store.kv_delete(REQ + r.workspace_approval, durable=True)
            self._polled.pop(r.workspace_approval, None)

    async def hint(self, aid: str) -> dict[str, Any] | None:
        """Tower says approval `aid` changed (an event, or the poll). None: no request waits on it."""
        from hangar.envrequests import EnvRequest

        rid = self.store.kv_get(REQ + aid)
        if rid is None:
            return None
        r = self.store.get(EnvRequest, rid)
        if r is None or r.state != "pending" or r.workspace_approval != aid:
            self.store.kv_delete(REQ + aid, durable=True)
            return {"ignored": f"{aid} is settled"}
        d = await self.receiver.tower().status(aid, kind=f"{r.kind}.request")
        if d.source == "fail_closed":
            raise Unavailable(d.reason)
        if d.outcome == "wait":
            return {"ok": True, "approval": aid, "status": d.status or "waiting"}
        await self._settle_request(r, d)
        return {"ok": True, "approval": aid, "request": r.id, "state": r.state}

    async def tick_requests(self, t: float | None = None) -> None:
        """The receiver's loop: poll requests waiting on Tower, and ask again about ones Tower couldn't answer."""
        from hangar.envrequests import EnvRequest

        t = time.time() if t is None else t
        for key in self.store.kv_keys(REQ):
            aid = key[len(REQ):]
            if t - self._polled.get(aid, 0.0) < POLL:
                continue
            self._polled[aid] = t
            try:
                await self.hint(aid)
            except Unavailable as e:
                log.info("governance: %s: %s", aid, e)
        for r in self.store.list_where(EnvRequest, state="pending"):
            g = r.governance or {}
            if g.get("decided_by") != "fail_closed" or r.workspace_approval or self.mode(f"{r.kind}.request") \
                    != "enforce" or t - self._polled.get(r.id, 0.0) < POLL:
                continue
            self._polled[r.id] = t
            await self.relay_request(r)

    # ================================================================== helpers
    def _session(self, sid: str) -> Session | None:
        try:
            return self.sessions.get(sid)
        except Exception:  # noqa: BLE001 - archived or deleted
            return None

    def _event(self, s: Session, text: str, **data: Any) -> None:
        from hangar.events import RunEvent

        with contextlib.suppress(Exception):
            self.sessions.activity.append(s.id, [RunEvent(kind="progress", turn=len(s.turns), text=text[:500],
                                                          data={"source": "hangar", "action": "governance", **data})])

    def _req_event(self, r: EnvRequest, text: str) -> None:
        from hangar.events import RunEvent

        with contextlib.suppress(Exception):
            self.sessions.activity.append(r.session_id, [RunEvent(kind="progress", turn=r.turn, text=text[:500], data={
                "request": r.id, "request_kind": r.kind, "target": r.target, "state": r.state,
                "approval": r.workspace_approval})])

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for relays in flight (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
