"""Events from Tower: suspend and resume turns on approvals, and honour `run.stop` (KAN-77, spec 0015 §5, §12).

Tower pushes signed events to one route, `POST /api/governance/events` (Tower's `aw/webhooks.py`):

    X-AW-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, t + "." + body)>
    X-AW-Event-Id:  <the body's id>

`governance.verify_event` checks them against the secret in the vault credential HANGAR_GOVERNANCE_WEBHOOK_SECRET
names: the HMAC, `t` within 300 s, and an id not seen in the last 48 hours (past Tower's 24 h of retries). Ids are
kept in the store, so a restart doesn't forget them. A rejected event is audited `governance.event_rejected`.

* `approval.created` and `approval.decided` are hints. hangar re-reads `GET /v1/decisions/{id}` with its own
  credential and acts on that answer, so a reordered event ("decided" before "created") needs nothing special, and
  a hint for an approval nothing waits on yet is kept until something does (`watch`).
* A tool call waiting on Tower (`watch`, called when the driver's request got `wait`) that waits more than
  HANGAR_GOVERNANCE_SUSPEND_AFTER_SECONDS is denied with "Paused for approval A-9", its turn interrupted at that
  call, checkpointed, and its sandbox released (`SessionService._suspend`). When Tower allows it, the session
  resumes from its checkpoint with a turn whose driver allows that one call once (`SessionTurn.preclear`). A deny
  leaves it suspended, with the reason.
* `run.stop` interrupts the run through hangar's interrupt path (`steer.interrupt`), is audited with who stopped
  it, and marks the session (`Session.governance_stop`) so no later approval resumes it.

A job's session waits in place, as `ask` turns did before (spec 0015 §5 step 3): jobs verify and deliver their own
turns. With HANGAR_GOVERNANCE off for every kind, or no webhook secret, the route answers 404.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from collections.abc import Iterator, MutableMapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hangar import fairness
from hangar import steer as steering
from hangar.governance import (
    Decision,
    DecisionPoint,
    EventRejected,
    GovernanceEvent,
    HttpDecisionPoint,
    parse_modes,
    verify_event,
)
from hangar.models import Approval, GovernanceStop, GovernanceWait, Session, SessionState, SessionTurn, now

log = logging.getLogger("hangar.govevents")

SEEN = "gov-event:"  # kv: event id -> when it was accepted
WAIT = "gov-wait:"  # kv: Tower approval id -> the session waiting on it
EARLY = "gov-early:"  # kv: approval id -> when a hint came for it before anything waited on it
SEEN_TTL = 48 * 3600  # spec 0015 §12: past Tower's 24 h retry window
EARLY_TTL = 3600
POLL = 60.0  # seconds between status reads of a suspended wait (the fallback when an event is lost)
TICK = 5.0
FINAL = frozenset({"resumed", "allowed", "denied", "expired", "stopped"})
PAUSED = "Paused for approval {approval}; this turn resumes when it's signed."


class Unavailable(Exception):
    """Tower couldn't answer the status read: the event is forgotten, so Tower's retry is not a replay."""


class SeenEvents(MutableMapping[str, float]):
    """Event ids hangar accepted, in the store's kv (`gov-event:<id>`), for `verify_event`'s replay check."""

    def __init__(self, store: Any, clock: Any = time.time):
        self.store, self.clock = store, clock

    def __getitem__(self, key: str) -> float:
        raw = self.store.kv_get(SEEN + key)
        if raw is None or self.clock() - float(raw) > SEEN_TTL:
            raise KeyError(key)
        return float(raw)

    def __setitem__(self, key: str, value: float) -> None:
        self.store.kv_set(SEEN + key, repr(float(value)), durable=True)

    def __delitem__(self, key: str) -> None:
        self.store.kv_delete(SEEN + key, durable=True)

    def __iter__(self) -> Iterator[str]:
        return (k[len(SEEN):] for k in self.store.kv_keys(SEEN))

    def __len__(self) -> int:
        return len(self.store.kv_keys(SEEN))

    def prune(self) -> int:
        n = 0
        for key in self.store.kv_keys(SEEN):
            raw = self.store.kv_get(key)
            if raw is None or self.clock() - float(raw) > SEEN_TTL:
                self.store.kv_delete(key)
                n += 1
        return n


class VaultCredential:
    """hangar's service key for Tower, read from the vault each time (a rotation applies at once)."""

    def __init__(self, broker: Any, name: str):
        self.broker, self.name = broker, name

    async def token(self, *, fresh: bool = False) -> str:
        return self.broker.secret_text(self.name)


class Receiver:
    def __init__(self, sessions: Any, *, tower: DecisionPoint | None = None, clock: Any = time.time):
        self.sessions = sessions
        self.fleet = sessions.fleet
        self.settings = sessions.settings
        self.store = sessions.store
        self.clock = clock
        self.seen = SeenEvents(self.store, clock)
        self._tower = tower
        self._locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._polled: dict[str, float] = {}
        self._suspending: set[str] = set()  # approvals whose turn is being put away now
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.Task | None = None
        self._pruned = 0.0

    # ================================================================== configuration
    def enabled(self) -> bool:
        """Some kind isn't `off` (HANGAR_GOVERNANCE, HANGAR_GOVERNANCE_MODES)."""
        try:
            modes = parse_modes(self.settings.governance, self.settings.governance_modes or None)
        except ValueError:
            return False
        return any(m != "off" for m in modes.values())

    def ready(self) -> bool:
        """Events can arrive: governance is on and the webhook secret is configured."""
        return self.enabled() and bool(self.settings.governance_webhook_secret)

    def tower(self) -> DecisionPoint:
        if self._tower is None:
            url, cred = self.settings.governance_url, self.settings.governance_credential
            if not url or not cred:
                raise Unavailable("HANGAR_GOVERNANCE_URL and HANGAR_GOVERNANCE_CREDENTIAL must both be set")
            self._tower = HttpDecisionPoint(url, VaultCredential(self.fleet.broker, cred))
        return self._tower

    def audit(self, actor: str, action: str, target: str | None = None, **detail: Any) -> None:
        self.fleet.audit(actor, action, target, **detail)

    # ================================================================== the route
    async def receive(self, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        if not self.enabled():
            return 404, {"error": "governance is off (HANGAR_GOVERNANCE)"}
        name = self.settings.governance_webhook_secret
        if not name:
            return 404, {"error": "governance events are not configured (HANGAR_GOVERNANCE_WEBHOOK_SECRET)"}
        try:  # front-door-auth: say nothing about the vault to an anonymous caller
            secret = self.fleet.broker.secret_text(name)
        except Exception:  # noqa: BLE001
            log.error("governance webhook secret credential %s is not usable", name)
            return 503, {"error": "the governance webhook secret is not stored"}
        h = {k.lower(): v for k, v in headers.items()}
        try:
            ev = verify_event(secret, h, body, self.seen, now=self.clock())
        except EventRejected as e:
            why = str(e)
            self.audit("tower", "governance.event_rejected", None, reason=why,
                       event=(h.get("x-aw-event-id") or "")[:100] or None)
            if why == "replayed":  # a duplicate delivery: acknowledged, and nothing happens
                return 200, {"ignored": "duplicate event"}
            return (400 if why.startswith("malformed") or "disagree" in why else 401), {"error": why}
        try:
            out = await self.apply(ev)
        except Unavailable as e:
            with contextlib.suppress(KeyError):
                del self.seen[ev.id]  # Tower's retry should be acted on, not refused as a replay
            log.warning("governance event %s: %s", ev.id, e)
            return 503, {"error": f"hangar couldn't read the decision from Tower: {e}"}
        self.audit("tower", "governance.event", None, event=ev.id, kind=ev.kind,
                   approval=(ev.approval or {}).get("id"), result=out)
        return 200, out

    async def apply(self, ev: GovernanceEvent) -> dict[str, Any]:
        if ev.kind == "run.stop":
            return await self.stop(ev)
        aid = (ev.approval or {}).get("id")
        if not isinstance(aid, str) or not aid:
            return {"ignored": "the event names no approval"}
        return await self.hint(aid, by=(ev.approval or {}).get("decided_by"))

    # ================================================================== approvals
    async def watch(self, s: Session, turn: SessionTurn, a: Approval, d: Decision) -> GovernanceWait | None:
        """A tool call the driver holds got `wait` from Tower (`governance.decide`, kind tool.call): follow it. The
        caller (the `governed` policy, KAN-79) relays the call; from here hangar suspends and resumes the turn."""
        if d.outcome != "wait" or not d.approval_id:
            return None
        n = next((i for i, t in enumerate(s.turns, 1) if t is turn), len(s.turns))
        w = GovernanceWait(approval=d.approval_id, driver_approval=a.id, tool=a.tool, args_hash=a.args_hash,
                           detail=a.detail, turn=n, run_id=turn.run_id, message=turn.message, since=self.clock())
        a.workspace_approval = d.approval_id
        if self._stopped(s, w):
            w.state, w.reason = "stopped", "the run was stopped in Tower"
            s.governance = w
            self.sessions._save(s)
            self._spawn(self._answer(s, w, "deny", f"Stopped in Tower by {s.governance_stop.by}."))  # type: ignore[union-attr]
            return w
        s.governance = w
        self.sessions._save(s)
        self.store.kv_set(WAIT + w.approval, s.id, durable=True)
        if self.store.kv_get(EARLY + w.approval) is not None:  # decided before hangar asked to follow it
            self.store.kv_delete(EARLY + w.approval)
            with contextlib.suppress(Unavailable):
                await self.hint(w.approval)
        return w

    async def hint(self, aid: str, by: Any = None) -> dict[str, Any]:
        """Something about approval `aid` changed (an event, or the poll): re-read it from Tower and act on that."""
        sid = self.store.kv_get(WAIT + aid)
        if sid is None:
            self.store.kv_set(EARLY + aid, repr(self.clock()))
            return {"ignored": f"nothing in hangar waits on {aid} (yet)"}
        async with self._locks[aid]:
            s, w = self._wait(aid, sid)
            if s is None or w is None:
                return {"ignored": f"{aid} is settled"}
            if self._stopped(s, w):
                return self._refuse(s, w)
            d = await self.tower().status(aid, kind=w.kind)  # type: ignore[arg-type]
            if d.source == "fail_closed":
                raise Unavailable(d.reason)
            if isinstance(by, str) and by:
                w.decided_by = by[:200]
            return await self._settle(s, w, d)

    def _wait(self, aid: str, sid: str) -> tuple[Session | None, GovernanceWait | None]:
        try:
            s = self.sessions.get(sid)
        except KeyError:
            self.store.kv_delete(WAIT + aid)
            return None, None
        w = s.governance
        if w is None or w.approval != aid or w.state in FINAL:
            self.store.kv_delete(WAIT + aid)
            return s, None
        return s, w

    def _stopped(self, s: Session, w: GovernanceWait) -> bool:
        st = s.governance_stop
        return st is not None and w.turn <= st.turn

    def _refuse(self, s: Session, w: GovernanceWait) -> dict[str, Any]:
        st = s.governance_stop
        w.state, w.reason, w.updated_at = "stopped", f"stopped in Tower by {st.by if st else 'someone'}", now()
        self.sessions._save(s)
        self.store.kv_delete(WAIT + w.approval)
        self.audit("tower", "governance.resume_refused", s.id, approval=w.approval, reason=w.reason)
        return {"ignored": f"the run was stopped; {w.approval} doesn't resume it"}

    async def _settle(self, s: Session, w: GovernanceWait, d: Decision) -> dict[str, Any]:
        if d.outcome == "wait":
            return {"ok": True, "approval": w.approval, "status": d.status or "waiting"}
        w.updated_at = now()
        if d.outcome == "allow":
            w.receipt = d.receipt
            if w.state == "waiting":  # the turn still holds the call: answer it there
                if await self._answer(s, w, "allow", ""):
                    w.state = "allowed"
                    self._record_receipt(s, w)
                    self._done(s, w)
                    return {"ok": True, "approval": w.approval, "allowed": "in the running turn"}
                if self._turn_running(s, w):  # the driver denied it meanwhile (its own deadline)
                    w.state, w.reason = "expired", "the turn stopped waiting before it was signed"
                    self._done(s, w)
                    return {"ok": True, "approval": w.approval, "expired": True}
            w.state = "approved"
            self.sessions._save(s)
            return {"ok": True, "approval": w.approval, **(await self._resume(s, w))}
        expired = d.status in ("expired", "withdrawn")
        w.reason = d.reason or ("nobody decided" if expired else "denied in Tower")
        if w.state == "waiting":
            await self._answer(s, w, "deny", f"Denied in Tower ({w.approval}): {w.reason}")
        was = w.state
        w.state = "expired" if expired else "denied"
        if was in ("suspended", "approved") and s.state == SessionState.SUSPENDED:
            self.sessions._set(s, SessionState.SUSPENDED, f"approval {w.approval} {w.state}: {w.reason}")
        self._event(s, f"approval {w.approval} {w.state} in Tower: {w.reason}", action=f"governance.{w.state}")
        self._done(s, w)
        return {"ok": True, "approval": w.approval, w.state: True}

    def _done(self, s: Session, w: GovernanceWait) -> None:
        self.sessions._save(s)
        self.store.kv_delete(WAIT + w.approval, durable=True)
        self._polled.pop(w.approval, None)

    def _turn(self, s: Session, w: GovernanceWait) -> SessionTurn | None:
        return s.turns[w.turn - 1] if 0 < w.turn <= len(s.turns) else None

    def _turn_running(self, s: Session, w: GovernanceWait) -> bool:
        t = self._turn(s, w)
        return t is not None and t is s.turns[-1] and not t.finished_at and s.state == SessionState.RUNNING

    async def _answer(self, s: Session, w: GovernanceWait, decision: str, message: str) -> bool:
        """Answer the driver's request in the running turn. False when it is no longer open there."""
        if not w.driver_approval or not self._turn_running(s, w):
            return False
        try:
            await steering.answer(self.sessions, s, w.driver_approval, decision, "tower", message or None)
        except (steering.SteerError, KeyError) as e:
            log.info("session %s: could not answer %s: %s", s.id, w.driver_approval, e)
            return False
        return True

    def _record_receipt(self, s: Session, w: GovernanceWait) -> None:
        t = self._turn(s, w)
        a = next((x for x in (t.approvals if t else []) if x.id == w.driver_approval), None)
        if a is not None:
            a.receipt, a.workspace_approval = w.receipt, w.approval

    def _event(self, s: Session, text: str, **data: Any) -> None:
        from hangar.events import RunEvent

        with contextlib.suppress(Exception):
            self.sessions.activity.append(s.id, [RunEvent(kind="progress", turn=len(s.turns), text=text,
                                                          data={"source": "hangar", **data})])

    # ================================================================== suspend and resume
    def suspendable(self, s: Session, w: GovernanceWait) -> bool:
        """Spec 0015 §5 step 1: events can reach hangar, the session isn't a job's, and the turn is live and
        still holds the call. Otherwise it waits in place, up to the driver's own deadline (step 3)."""
        t = self._turn(s, w)
        return (self.ready() and not s.job_id and t is not None and t.live and self._turn_running(s, w)
                and any(a.id == w.driver_approval and a.status == "pending" for a in t.approvals))

    async def suspend(self, s: Session, w: GovernanceWait) -> None:
        """Deny the call with "Paused for approval", end the turn at that call, checkpoint, release the sandbox."""
        svc = self.sessions
        w.state, w.updated_at = "suspending", now()
        svc._save(s)
        self._suspending.add(w.approval)
        why = f"waiting for approval {w.approval} in Tower; resumes when it's signed"
        try:
            await self._answer(s, w, "deny", PAUSED.format(approval=w.approval))
            with contextlib.suppress(steering.SteerError):
                await steering.interrupt(svc, s, "tower")
            deadline = time.monotonic() + svc.settings.session_turn_timeout_minutes * 60
            while svc.busy(s.id) and time.monotonic() < deadline:  # the turn ends: its summary and checkpoint
                await asyncio.sleep(max(0.05, min(1.0, svc.settings.session_poll_seconds)))
            if s.sandbox_id and not svc.busy(s.id) and s.state not in (SessionState.ARCHIVED, SessionState.FAILED):
                svc._busy.add(s.id)
                await svc._suspend_task(s.id, why)  # checkpoints, then releases (or parks) the sandbox
            self.audit("tower", "governance.suspend", s.id, approval=w.approval, tool=w.tool,
                       released=s.sandbox_id is None)
            self._event(s, f"paused for approval {w.approval} in Tower: {w.tool}", action="governance.suspend",
                        approval=w.approval)
        except Exception:  # noqa: BLE001 - whatever happened, the wait goes on: the decision still resumes it
            log.exception("session %s: suspending for %s failed", s.id, w.approval)
        finally:
            self._suspending.discard(w.approval)
        async with self._locks[w.approval]:
            if w.state == "suspending":
                w.state = "suspended"
                svc._save(s)
            elif w.state == "approved":  # signed while the turn was being put away
                await self._resume(s, w)
            elif w.state in ("denied", "expired") and s.state == SessionState.SUSPENDED:
                svc._set(s, SessionState.SUSPENDED, f"approval {w.approval} {w.state}: {w.reason}")

    async def _resume(self, s: Session, w: GovernanceWait) -> dict[str, Any]:
        """The approval is signed: start the turn that carries on, from the checkpoint, with the call pre-cleared.
        Exactly once: the state moves to `resumed` before the turn starts, under the approval's lock."""
        svc = self.sessions
        if w.state != "approved":
            return {}
        if self._stopped(s, w):
            return self._refuse(s, w)
        if svc.busy(s.id) or s.state in (SessionState.RUNNING, SessionState.SUSPENDING, SessionState.STARTING):
            return {"resumes": "when the session is free"}
        if s.state == SessionState.ARCHIVED or s.job_id:  # a job drives its own turns: follow up on the job
            w.state, w.reason = "expired", ("the session was archived" if not s.job_id else
                                            "the job's turn ended before it was signed; follow up on the job")
            self._done(s, w)
            return {"expired": True}
        try:
            if not (s.sandbox_id and svc._alive(s.sandbox_id)):
                fairness.check_sessions(svc, s.owner, s)  # resuming takes a sandbox
                self.fleet.power.check(s.provider)  # overnight: resumes at power-up (the tick retries)
        except Exception as e:  # noqa: BLE001
            w.reason = f"resumes when it can: {e}"[:300]
            svc._save(s)
            return {"resumes": "when a sandbox is available"}
        who = f" by {w.decided_by}" if w.decided_by else ""
        message = (f"Approval {w.approval} was signed in Tower{who}. Carry on where you stopped. You were asked:\n\n"
                   f"{w.message[:8000]}\n\nThe call you were paused on ({w.tool}: {w.detail[:500]}) is now allowed "
                   "once: make it again, exactly as before.")
        w.state, w.updated_at = "resumed", now()
        svc._save(s)
        svc.start_turn(s, message, f"tower:{w.decided_by or w.approval}"[:100], preclear=[
            {"tool": w.tool, "args_hash": w.args_hash or "", "receipt": w.receipt or "", "approval": w.approval}])
        self._done(s, w)
        self.audit("tower", "governance.resume", s.id, approval=w.approval, receipt=w.receipt, tool=w.tool)
        return {"resumed": s.name}

    # ================================================================== run.stop
    async def stop(self, ev: GovernanceEvent) -> dict[str, Any]:
        """Tower's per-run stop (KAN-57). It only takes capability away, so it is acted on once verified."""
        run, corr = ev.run or {}, ev.corr or {}
        ref, job, run_id = (run.get("session") or corr.get("session"), run.get("job") or corr.get("job"),
                            run.get("run") or corr.get("run"))
        by = str(run.get("stopped_by") or run.get("by") or "someone in Tower")[:200]
        reason = str(run.get("reason") or "")[:500]
        targets: list[Session] = []
        if isinstance(ref, str) and ref:
            with contextlib.suppress(KeyError):
                targets = [self.sessions.get(ref)]
        if not targets and isinstance(job, str) and job:
            targets = [x for x in self.sessions.of_job(job) if not x.parent_id]
        if not targets:
            self.audit(f"tower:{by}", "governance.run_stop", ref or job, found=False, event=ev.id, run=run_id)
            return {"ignored": "hangar has no such run"}
        stopped = []
        for s in targets:
            n = next((i for i, t in enumerate(s.turns, 1) if run_id and t.run_id == run_id), len(s.turns))
            s.governance_stop = GovernanceStop(by=by, reason=reason, event=ev.id, run_id=run_id, turn=n)
            w = s.governance
            if w is not None and w.state not in FINAL and w.turn <= n:
                async with self._locks[w.approval]:
                    w.state, w.reason, w.updated_at = "stopped", f"stopped in Tower by {by}", now()
                    self._done(s, w)
                self._spawn(self._withdraw(w))
            self.sessions._save(s)
            t = s.turns[-1] if s.turns else None
            interrupted = False
            if t is not None and n == len(s.turns) and t.run_id and not t.finished_at \
                    and s.state == SessionState.RUNNING:
                try:
                    await steering.interrupt(self.sessions, s, f"tower:{by}"[:100])
                    interrupted = True
                except steering.SteerError as e:
                    log.info("session %s: run.stop found the turn over: %s", s.id, e)
            self._event(s, f"run stopped in Tower by {by}" + (f": {reason}" if reason else ""),
                        action="governance.run_stop")
            self.audit(f"tower:{by}", "governance.run_stop", s.id, event=ev.id, run=run_id, turn=n, reason=reason,
                       interrupted=interrupted)
            stopped.append(s.name)
        return {"ok": True, "stopped": stopped}

    async def _withdraw(self, w: GovernanceWait) -> None:
        with contextlib.suppress(Exception):
            await self.tower().withdraw(w.approval, w.reason or "the run was stopped")

    # ================================================================== the loop
    async def tick(self) -> None:
        """Suspend calls that waited long enough, resume approved ones that couldn't start yet, poll suspended ones
        (the fallback for a lost event; on start, every one: spec 0015 §8), and forget old event ids."""
        t = self.clock()
        for key in self.store.kv_keys(WAIT):
            aid = key[len(WAIT):]
            sid = self.store.kv_get(key)
            if sid is None:
                continue
            s, w = self._wait(aid, sid)
            if s is None or w is None:
                continue
            try:
                await self._tick_one(s, w, t)
            except Unavailable as e:
                log.info("governance: %s: %s", aid, e)
            except Exception:  # noqa: BLE001 - one wait must not stop the others
                log.exception("governance: following %s failed", aid)
        if t - self._pruned > 600:
            self._pruned = t
            self.seen.prune()
            for key in self.store.kv_keys(EARLY):
                raw = self.store.kv_get(key)
                if raw is None or t - float(raw) > EARLY_TTL:
                    self.store.kv_delete(key)

    async def _tick_one(self, s: Session, w: GovernanceWait, t: float) -> None:
        if self._stopped(s, w):
            async with self._locks[w.approval]:
                self._refuse(s, w)
            return
        if w.state == "waiting":
            if not self._turn_running(s, w) or not any(a.id == w.driver_approval and a.status == "pending"
                                                       for a in (self._turn(s, w) or s.turns[-1]).approvals):
                w.state, w.reason = "expired", "the turn stopped waiting before it was decided"
                self._done(s, w)
                self._spawn(self._withdraw(w))
            elif t - w.since >= self.settings.governance_suspend_after_seconds and self.suspendable(s, w):
                w.state = "suspending"  # at once, so the next tick doesn't start a second one
                self._spawn(self.suspend(s, w))
            return
        if w.state == "approved":
            async with self._locks[w.approval]:
                await self._resume(s, w)
            return
        if w.state == "suspending" and w.approval not in self._suspending:  # a restart cut it short
            w.state = "suspended"
            self.sessions._save(s)
        if w.state == "suspended" and t - self._polled.get(w.approval, 0.0) >= POLL:
            self._polled[w.approval] = t
            await self.hint(w.approval)

    def start(self) -> None:
        if self._loop is None and self.enabled():
            self._loop = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("governance tick failed")
            await asyncio.sleep(TICK)

    async def stop_loop(self) -> None:
        for task in [*self._tasks, *([self._loop] if self._loop else [])]:
            task.cancel()
        for task in [*self._tasks, *([self._loop] if self._loop else [])]:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()
        self._loop = None
        if isinstance(self._tower, HttpDecisionPoint):
            await self._tower.close()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for suspends and withdrawals in flight (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def mount(app: FastAPI, h: Any) -> None:
    """`POST /api/governance/events`: no token; the HMAC is the authentication (front-door-auth)."""

    @app.post("/api/governance/events")
    async def governance_events(request: Request):
        code, out = await h.governance_events.receive(dict(request.headers), await request.body())
        return JSONResponse(out, status_code=code)
