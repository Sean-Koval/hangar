"""Events out (docs/plugins.md#events): `hangar.event/v1` to every notifier, and the built-in signed webhook.

* **Derived, not called.** Like inbox_notify, the dispatcher listens to store writes. A job (or its session)
  written to the store is read as a set of facts: queued, awaiting approval, started, needs input, a tool
  approval pending, delivered, PR opened, approved, checks done, landed, closed, failed, finished. Each fact has a
  stable id ("job_abc:pr.opened:12"); a notifier gets it once, kept in kv (`events:sent:<notifier>:<id>`, a week),
  so a restart doesn't repeat what was said and a burst of writes is one event. jobs.py stays as it is.
* **Other producers** (change sets) call `of(svc).emit(event)` with an event of their own.
* **Retries.** A notifier that answers False (or raises) gets the event again on a later pass, up to
  MAX_ATTEMPTS; then the job's timeline says it was dropped.
* **Since.** A notifier hears facts from when it was first enabled (`events:since:<notifier>`), not the
  store's history.
* **The webhook** (`HANGAR_PLUGINS.notifiers.webhook`) POSTs the event as JSON, signed for replay safety:
  `X-Hangar-Timestamp: <unix seconds>`, `X-Hangar-Delivery: <event id>`, and `X-Hangar-Signature: sha256=<hex>`,
  the HMAC-SHA256 of `<timestamp>.<body>` with the secret in `secret_credential`. A receiver checks the signature
  (`verify`), refuses a timestamp more than five minutes off, and drops an event id it has already seen. The URL
  follows the job callback rules (https, public, or a host in HANGAR_JOB_CALLBACK_HOSTS), checked again on every
  connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import hmac
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field, field_validator

from hangar.ext.notify import REPLY_TO_LABEL, SCHEMA, Action, Event, Notifier
from hangar.models import JOB_TERMINAL, Job, JobState, Person, Session

if TYPE_CHECKING:
    from hangar.jobs import JobService

log = logging.getLogger("hangar.events_out")

SENT_PREFIX = "events:sent:"
SINCE_PREFIX = "events:since:"
SENT_TTL = 7 * 86400
COALESCE = 1.0  # seconds a burst of writes to one job is gathered for
RECHECK = 30.0  # seconds between retries of what a notifier refused
MAX_ATTEMPTS = 10
TOLERANCE = 300  # seconds a webhook timestamp may be off
_RUNNING = {JobState.PROVISIONING, JobState.RUNNING, JobState.VERIFYING, JobState.DELIVERING}


# ====================================================================== facts
def _reply_to(job: Job) -> str | None:
    if v := (job.labels or {}).get(REPLY_TO_LABEL):
        return v
    for t in reversed(job.turns):
        if t.reply_to:
            return t.reply_to
    return job.source_ref


def _base(svc: JobService, job: Job) -> dict[str, Any]:
    from hangar import inbox
    from hangar.intake import TRACKER

    links = {"job": inbox.job_url(svc, job.id)}
    if job.result.pr_url and job.result.pr_url.startswith("https://"):
        links["pr"] = job.result.pr_url
    with contextlib.suppress(Exception):
        if url := inbox.ticket_url(svc, job):
            links["ticket"] = url
    data: dict[str, Any] = {"state": str(job.state), "title": job.title}
    if t := (job.labels or {}).get(TRACKER):
        data["ticket"] = t
    return {"job": job.id, "session": job.session_id, "person": job.person, "repo": job.repo,
            "reply_to": _reply_to(job), "links": {k: v for k, v in links.items() if v}, "data": data}


def _ev(svc: JobService, job: Job, kind: str, key: str | int | None, at: float | None, summary: str,
        actions: list[Action] | None = None, **data: Any) -> Event:
    b = _base(svc, job)
    b["data"].update({k: v for k, v in data.items() if v is not None})
    return Event(id=f"{job.id}:{kind}" + (f":{key}" if key not in (None, "") else ""), type=kind,
                 at=at or job.updated_at, summary=svc.broker.redact(summary)[:500], actions=actions or [], **b)


def facts(svc: JobService, job: Job) -> list[Event]:
    """What is true of a job now, as events. Each id names the fact, so the same fact is the same event."""
    out: list[Event] = []
    st = job.state
    turn_no = len(job.turns) or 1
    last = job.turns[-1] if job.turns else None
    if st == JobState.AWAITING_APPROVAL:
        why = job.approval.reason if job.approval else ""
        out.append(_ev(svc, job, "job.awaiting_approval", None, job.created_at,
                       f"{job.title}: waiting for approval" + (f" ({why})" if why else ""),
                       [Action(tool="approve_job", args={"job": job.id}, label="Approve"),
                        Action(tool="reject_job", args={"job": job.id}, label="Reject")]))
    admitted = st in (JobState.QUEUED, JobState.NEEDS_INPUT, *_RUNNING) or \
        (st in JOB_TERMINAL and bool(job.turns and job.turns[0].started_at))
    if admitted:
        out.append(_ev(svc, job, "job.queued", None, job.created_at, f"{job.title}: queued"))
    if last is not None and last.started_at and (st in _RUNNING or last.finished_at):
        out.append(_ev(svc, job, "job.started", turn_no, last.started_at,
                       f"{job.title}: the agent started" + (f" (turn {turn_no})" if turn_no > 1 else ""), turn=turn_no))
    if st == JobState.NEEDS_INPUT:
        q = job.result.question or (last.question if last else None) or "the agent has a question"
        out.append(_ev(svc, job, "job.needs_input", turn_no, last.finished_at if last else None,
                       f"{job.title}: {q}",
                       [Action(tool="follow_up_job", args={"job": job.id, "message": ""}, label="Answer")],
                       question=q, turn=turn_no))
    r = job.result
    if r.pushed and r.head_sha:
        out.append(_ev(svc, job, "job.delivered", r.head_sha[:12], last.finished_at if last else None,
                       f"{job.title}: delivered {r.head_sha[:12]}",
                       head_sha=r.head_sha))
    if r.pr_url:
        out.append(_ev(svc, job, "pr.opened", r.pr_number, job.landing.opened_at,
                       f"{job.title}: PR #{r.pr_number} opened" if r.pr_number else f"{job.title}: PR opened",
                       pr_number=r.pr_number, draft=job.landing.draft))
    ld = job.landing
    if ld.approved_sha:
        out.append(_ev(svc, job, "pr.approved", ld.approved_sha[:12], ld.approved_at,
                       f"{job.title}: PR #{r.pr_number} approved" + (f" by {ld.approved_by}" if ld.approved_by else ""),
                       pr_number=r.pr_number, approved_by=ld.approved_by))
    if ld.checks_sha and ld.checks and "pending" not in ld.checks.values():
        bad = sorted(k for k, v in ld.checks.items() if v not in ("success", "neutral", "skipped"))
        verdict = "failed" if bad else "passed"
        out.append(_ev(svc, job, "pr.checks", f"{ld.checks_sha[:12]}:{verdict}", ld.ci_green_at,
                       f"{job.title}: checks {verdict}" + (f" ({', '.join(bad[:5])})" if bad else ""),
                       pr_number=r.pr_number, checks=verdict, failed=bad or None, sha=ld.checks_sha))
    if ld.pr_state == "merged":
        out.append(_ev(svc, job, "pr.landed", r.pr_number, ld.merged_at, f"{job.title}: PR #{r.pr_number} landed",
                       pr_number=r.pr_number, merged_by=ld.merged_by, merge_sha=ld.merge_commit_sha))
    elif ld.pr_state == "closed":
        out.append(_ev(svc, job, "pr.closed", r.pr_number, ld.closed_at,
                       f"{job.title}: PR #{r.pr_number} closed without merging", pr_number=r.pr_number))
    if st in JOB_TERMINAL:
        why = f": {job.reason}" if job.reason else ""
        ended = (last.finished_at if last else None) or job.updated_at
        if st == JobState.FAILED:
            out.append(_ev(svc, job, "job.failed", turn_no, ended, f"{job.title}: failed{why}", turn=turn_no))
        out.append(_ev(svc, job, "job.finished", turn_no, ended, f"{job.title}: {st}{why}", turn=turn_no,
                       outcome=str(st)))
    return out


def session_facts(svc: JobService, s: Session) -> list[Event]:
    """A live turn's pending tool approvals (steer.pending), for the session's job when it has one."""
    from hangar import steer

    pending = steer.pending(s)
    if not pending:
        return []
    job = svc._jobs.get(s.job_id) if s.job_id else None
    out = []
    for a in pending:
        actions = [Action(tool="answer_approval", args={"ref": s.name, "approval": a.id, "decision": d}, label=d)
                   for d in ("allow", "deny")]
        what = f"wants to use {a.tool}" + (f": {a.detail[:200]}" if a.detail else "")
        if job is not None:
            ev = _ev(svc, job, "turn.tool_approval", a.id, a.requested_at, f"{job.title}: the agent {what}", actions,
                     tool=a.tool, approval=a.id, expires_at=a.expires_at)
        else:
            ev = Event(id=f"{s.name}:turn.tool_approval:{a.id}", type="turn.tool_approval", at=a.requested_at,
                       session=s.name, person=s.person, repo=s.repo, summary=svc.broker.redact(f"{s.name}: {what}"),
                       actions=actions, data={"tool": a.tool, "approval": a.id, "expires_at": a.expires_at})
        out.append(ev)
    return out


# ====================================================================== the dispatcher
class Dispatcher:
    """Turns store writes into events and hands each notifier what it wants, once."""

    def __init__(self, svc: JobService):
        self.svc = svc
        self.store = svc.store
        self.notifiers: list[Notifier] = []
        self.coalesce = COALESCE
        self.recheck = RECHECK
        self._dirty: dict[tuple[str, str], Job | Session] = {}  # ("job", id) | ("session", name): as last written
        self._queued: list[Event] = []  # emitted by other producers
        self._retry: dict[tuple[str, str], tuple[Event, int]] = {}  # (notifier, event id) -> (event, attempts)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._pruned_at = 0.0

    def start(self, notifiers: list[Notifier]) -> None:
        self.notifiers = list(notifiers)
        if not self.notifiers or (self._task is not None and not self._task.done()):
            return
        t = time.time()
        for n in self.notifiers:
            if self.store.kv_get(SINCE_PREFIX + n.name) is None:
                self.store.kv_set(SINCE_PREFIX + n.name, repr(t))
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        if self.on_put not in self.store.on_put:
            self.store.on_put.append(self.on_put)
        self._task = self._loop.create_task(self._run())

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self.store.on_put.remove(self.on_put)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    # --- producers (any thread)
    def on_put(self, obj: Any) -> None:
        if isinstance(obj, Job):
            self._mark(("job", obj.id), obj)
        elif isinstance(obj, Session):
            self._mark(("session", obj.name), obj)

    def emit(self, event: Event) -> None:
        """Send an event that isn't derived from a job (a change set's state). Its id must be stable."""
        with self._lock:
            self._queued.append(event)
        self._poke()

    def _mark(self, key: tuple[str, str], obj: Job | Session) -> None:
        with self._lock:
            self._dirty[key] = obj
        self._poke()

    def _poke(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            wake.set()
        else:
            loop.call_soon_threadsafe(wake.set)

    # --- the loop
    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            woken = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.recheck)
                woken = True
            if woken and self.coalesce:
                await asyncio.sleep(self.coalesce)
            self._wake.clear()
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - next pass
                log.exception("events-out pass failed")

    def collect(self) -> list[Event]:
        """The events for everything written since the last pass."""
        with self._lock:
            dirty, self._dirty = self._dirty, {}
            queued, self._queued = self._queued, []
        out = list(queued)
        for (kind, key), obj in sorted(dirty.items(), key=lambda kv: kv[0]):
            try:
                if isinstance(obj, Job):
                    out += facts(self.svc, obj)
                elif isinstance(obj, Session):
                    out += session_facts(self.svc, obj)
            except Exception:  # noqa: BLE001 - one record's facts never stop the others
                log.exception("could not read events from %s %s", kind, key)
        return out

    async def flush(self) -> dict[str, int]:
        """One pass: new events and due retries to each notifier. Returns {notifier: events sent}."""
        t = time.time()
        sent: dict[str, int] = {}
        retry, self._retry = self._retry, {}
        work: list[tuple[Notifier, Event, int]] = []
        by_name = {n.name: n for n in self.notifiers}
        for (name, _), (ev, attempts) in retry.items():
            if (n := by_name.get(name)) is not None:
                work.append((n, ev, attempts))
        for ev in self.collect():
            for n in self.notifiers:
                if (n.name, ev.id) not in retry:
                    work.append((n, ev, 0))
        people: dict[str | None, Person | None] = {}
        for n, ev, attempts in work:
            # Whole seconds: a PR is dated by GitHub's created_at, so one opened in the second a notifier started
            # would read as older than it and be dropped for good (KAN-201).
            if self._sent(n.name, ev.id, t) or ev.at < int(self._since(n.name)):
                continue
            if ev.person not in people:
                people[ev.person] = self.store.get(Person, ev.person) if ev.person else None
            person = people[ev.person]
            try:
                if not n.wants(person, ev):
                    continue
                ok = bool(await n.send(ev, person))
            except Exception as e:  # noqa: BLE001 - a notifier never breaks the others
                log.warning("notifier %s could not send %s: %s", n.name, ev.id, e)
                ok = False
            if ok:
                self.store.kv_set(f"{SENT_PREFIX}{n.name}:{ev.id}", f"{t + SENT_TTL:.0f}")
                sent[n.name] = sent.get(n.name, 0) + 1
            elif attempts + 1 < MAX_ATTEMPTS:
                self._retry[(n.name, ev.id)] = (ev, attempts + 1)
            else:
                self._dropped(n.name, ev)
        if sent and t - self._pruned_at > 3600:
            self._pruned_at = t
            self._prune(t)
        return sent

    def _since(self, name: str) -> float:
        try:
            return float(self.store.kv_get(SINCE_PREFIX + name) or 0)
        except ValueError:
            return 0.0

    def _sent(self, name: str, ev_id: str, t: float) -> bool:
        v = self.store.kv_get(f"{SENT_PREFIX}{name}:{ev_id}")
        try:
            return v is not None and float(v) > t
        except ValueError:
            return False

    def _prune(self, t: float) -> None:
        for k in self.store.kv_keys(SENT_PREFIX):
            with contextlib.suppress(ValueError, TypeError):
                if float(self.store.kv_get(k) or 0) <= t:
                    self.store.kv_delete(k)

    def _dropped(self, name: str, ev: Event) -> None:
        msg = f"event {ev.type} not delivered to notifier {name} after {MAX_ATTEMPTS} attempts"
        job = self.svc._jobs.get(ev.job) if ev.job else None
        if job is not None:
            self.svc._event(job, msg)
        else:
            log.warning("%s (%s)", msg, ev.id)


def of(svc: JobService) -> Dispatcher:
    """The job service's dispatcher (made on first use)."""
    d = svc.__dict__.get("_events_out")
    if d is None:
        d = svc.__dict__["_events_out"] = Dispatcher(svc)
    return d


# ====================================================================== the signed webhook
def sign(key: str, body: bytes, timestamp: int) -> str:
    return "sha256=" + hmac.new(key.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(key: str, body: bytes, headers: Any, *, now: float | None = None, tolerance: int = TOLERANCE) -> bool:
    """For receivers (and tests): the signature matches and the timestamp is fresh. Deduplicating on
    X-Hangar-Delivery inside the window is the receiver's part of replay safety."""
    ts = str(headers.get("x-hangar-timestamp") or "")
    sig = str(headers.get("x-hangar-signature") or "")
    if not ts.isdigit() or not sig:
        return False
    if abs((now if now is not None else time.time()) - int(ts)) > tolerance:
        return False
    return hmac.compare_digest(sign(key, body, int(ts)), sig)


class WebhookSettings(BaseModel):
    model_config = {"extra": "forbid"}

    url: str = Field(min_length=8, max_length=2000)
    secret_credential: str = Field(min_length=1, max_length=200)  # a vault credential name, never the secret
    events: list[str] = Field(default_factory=lambda: ["*"], max_length=100)  # glob patterns: "pr.*", "job.failed"
    timeout_seconds: float = Field(10.0, gt=0, le=60)

    @field_validator("events")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p or len(p) > 100 or any(c.isspace() for c in p):
                raise ValueError(f"'{p}' isn't an event pattern like pr.* or job.failed")
        return v


class WebhookNotifier:
    name = "webhook"

    def __init__(self, svc: JobService, cfg: WebhookSettings):
        self.svc = svc
        self.cfg = cfg

    def wants(self, person: Person | None, event: Event) -> bool:
        return any(fnmatch.fnmatchcase(event.type, p) for p in self.cfg.events)

    async def send(self, event: Event, person: Person | None) -> bool:
        from hangar.frontdoor import outbound_transport

        try:
            key = self.svc.broker.secret_text(self.cfg.secret_credential)
        except Exception:  # noqa: BLE001 - never sent unsigned; the doctor says why
            log.warning("webhook notifier: credential '%s' is not usable", self.cfg.secret_credential)
            return False
        body = event.body()
        ts = int(time.time())
        headers = {"Content-Type": "application/json", "User-Agent": "hangar", "X-Hangar-Event": event.type,
                   "X-Hangar-Delivery": event.id, "X-Hangar-Timestamp": str(ts),
                   "X-Hangar-Signature": sign(key, body, ts), "X-Hangar-Schema": SCHEMA}
        transport = self.svc.transport or outbound_transport(self.svc)
        async with httpx.AsyncClient(transport=transport, timeout=self.cfg.timeout_seconds,
                                     follow_redirects=False) as c:
            try:
                r = await c.post(self.cfg.url, content=body, headers=headers)
            except httpx.HTTPError as e:
                log.info("webhook notifier: %s for %s", type(e).__name__, event.id)
                return False
        return r.status_code < 300

    def doctor(self) -> list[dict[str, Any]]:
        from hangar.doctor import _check

        try:
            self.svc.broker.secret_text(self.cfg.secret_credential)
        except Exception:  # noqa: BLE001
            return [_check("events webhook", False, f"credential '{self.cfg.secret_credential}' is not stored",
                           f"hangar cred add {self.cfg.secret_credential} --harness webhook --kind generic "
                           "--file <key file>")]
        return [_check("events webhook", True, f"signed events ({', '.join(self.cfg.events)}) to {self.cfg.url}")]


class _WebhookPlugin:
    """Built in: loads when HANGAR_PLUGINS.notifiers.webhook is set."""

    name = "webhook"
    api = "hangar.plugin/v1"
    Settings = WebhookSettings

    def register(self, host: Any, settings: WebhookSettings) -> None:
        from hangar.jobs import JobError
        from hangar.pluginhost import PluginConfigError

        try:
            host.jobs._check_callback(settings.url)  # the job callback rules (SSRF guard)
        except JobError as e:
            raise PluginConfigError(f"HANGAR_PLUGINS.notifiers.webhook.url: {e}") from None
        host.add_notifier(WebhookNotifier(host.jobs, settings))


PLUGIN = _WebhookPlugin()
