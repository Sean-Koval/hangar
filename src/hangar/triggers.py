"""Triggers (spec 0011, workstream A): who may start jobs, approvals, signed inbound webhooks and schedules.

- Trust: a GitHub trigger starts a job only when a trusted person made it (HANGAR_GITHUB_TRUSTED, or by default
  the repo's owners, members and collaborators). Anyone else's trigger creates a job in `awaiting_approval`,
  which a trusted `/hangar approve` (or REST/MCP/CLI approve) queues.
- Inbound webhooks: systems that can only send webhooks (Sentry, Linear, PagerDuty, internal services) start jobs
  at /api/hooks/inbound/<source>. Each source has its own HMAC secret, a template that maps payload fields into
  the job, and a delivery id for idempotency. Their text is untrusted input to the agent.
- Schedules: a job template submitted on a cron schedule, at most once per slot, across restarts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Literal, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hangar import metrics as metrics  # --- reconciler
from hangar.exchange import UNTRUSTED_RULES
from hangar.github import GitHubError
from hangar.jobs import Caller, JobError, JobService, SubmitJob
from hangar.models import JOB_TERMINAL, Job, JobApproval, JobState, ScheduledJob, now

log = logging.getLogger("hangar.triggers")

DEFAULT_TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$")


class TriggerError(ValueError):
    pass
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


# ====================================================================== trust
class GitHubTrust:
    """Whether a GitHub login may start work without approval.

    With HANGAR_GITHUB_TRUSTED set, only the logins and `@org/team` members it lists are trusted (team membership is
    looked up with the control plane's GitHub token and cached). Unset, the author association decides, as before."""

    def __init__(self, jobs: JobService):
        self.jobs = jobs
        self._teams: dict[tuple[str, str], tuple[float, bool]] = {}  # (org/team, login) -> (expires, member)

    @property
    def configured(self) -> bool:
        return bool(self.jobs.settings.github_trusted)

    async def trusted(self, login: str | None, association: str | None = None) -> bool:
        if not login or not _LOGIN_RE.match(login):
            return False
        entries = [e.strip() for e in self.jobs.settings.github_trusted if e.strip()]
        if not entries:
            return (association or "").upper() in DEFAULT_TRUSTED
        who = login.lower()
        teams = []
        for e in entries:
            if e.startswith("@"):
                teams.append(e[1:].lower())
            elif e.lower() == who:
                return True
        for team in teams if not who.endswith("[bot]") else []:  # apps are never team members
            if await self._member(team, who):
                return True
        return False

    async def labeler_trusted(self, sender: str | None, issue: dict[str, Any]) -> bool:
        """Labels need triage rights, except that an issue form adds its labels for whoever opens the issue. So by
        default the labeler is trusted unless they are the issue's author, whose association then decides."""
        if self.configured:
            return await self.trusted(sender)
        author = (issue.get("user") or {}).get("login")
        if sender and author and sender.lower() == author.lower():
            return await self.trusted(sender, issue.get("author_association"))
        return bool(sender)

    async def _member(self, team: str, login: str) -> bool:
        org, _, slug = team.partition("/")
        if not _SLUG_RE.match(org) or not _SLUG_RE.match(slug):
            log.warning("HANGAR_GITHUB_TRUSTED entry '@%s' is not @org/team", team)
            return False
        key, t = (team, login), time.time()
        hit = self._teams.get(key)
        if hit and hit[0] > t:
            return hit[1]
        gh = self.jobs._github()
        if gh is None:
            log.warning("can't check @%s membership: HANGAR_GITHUB_TOKEN_CREDENTIAL is not set", team)
            return False
        try:
            m = await gh.team_membership(org, slug, login)
        except (GitHubError, httpx.HTTPError) as e:
            log.warning("could not check @%s membership for %s: %s", team, login, e)
            return False  # fail closed, and don't cache the failure
        finally:
            await gh.close()
        ok = bool(m) and m.get("state") == "active"  # a pending invitation doesn't count
        if len(self._teams) > 2000:
            self._teams = {k: v for k, v in self._teams.items() if v[0] > t}
        self._teams[key] = (t + self.jobs.settings.github_team_cache_seconds, ok)
        return ok


def untrusted_note(job: Job) -> str | None:
    """The prompt section that tells the agent which text it must not take orders from."""
    files = [c.name for c in job.context if c.untrusted]
    if not job.untrusted_source and not files:
        return None
    who = job.untrusted_source or "someone outside the trusted list"
    approved = (f" A trusted person ({job.approval.decided_by}) approved running this job, not the text itself."
                if job.approval and job.approval.decision == "approved" else "")
    parts = [f"# Untrusted input\n\nParts of this task come from {who}, which hangar does not trust.{approved}"]
    if files:
        parts.append("Untrusted files: " + ", ".join(f"`{n}`" for n in files) + ".")
    parts.append(UNTRUSTED_RULES)
    return "\n\n".join(parts)


# ====================================================================== approvals
def new_approval(jobs: JobService, requested_by: str, reason: str) -> JobApproval:
    return JobApproval(requested_by=requested_by, reason=reason,
                       expires_at=now() + jobs.settings.job_approval_ttl_hours * 3600)


def approval_command(text: str) -> Literal["approve", "reject"] | None:
    """`/hangar approve` and `/hangar reject` (the text after `/hangar`) are decisions, not new work."""
    word = (text.split() or [""])[0].lower()
    return word if word in ("approve", "reject") else None  # type: ignore[return-value]


def _github_ref(job: Job) -> str | None:
    return job.source_ref if (job.source_ref or "").startswith("github:") else None


def approve(jobs: JobService, job_id: str, approver: str, owner: str | None = None) -> Job:
    """Queue a job that was waiting for a trusted person, and record who let it run."""
    job = jobs.get(job_id, owner)
    if job.state != JobState.AWAITING_APPROVAL:
        raise JobError(f"job {job.id} is {job.state}, not waiting for approval")
    a = job.approval or JobApproval(requested_by=job.requester)
    job.approval = a.model_copy(update={"decided_by": approver, "decided_at": now(), "decision": "approved"})
    jobs._event(job, f"approved by {approver}", JobState.QUEUED, reason=None)
    jobs.fleet.audit(approver, "job.approve", job.id, requested_by=a.requested_by)
    jobs._queue.put_nowait(job.id)
    if ref := _github_ref(job):
        jobs._spawn(jobs._comment(job, ref, f"Approved by {approver}. Hangar job `{job.id}` is queued."))
    return job


def reject(jobs: JobService, job_id: str, approver: str, owner: str | None = None) -> Job:
    job = jobs.get(job_id, owner)
    if job.state != JobState.AWAITING_APPROVAL:
        raise JobError(f"job {job.id} is {job.state}, not waiting for approval")
    a = job.approval or JobApproval(requested_by=job.requester)
    job.approval = a.model_copy(update={"decided_by": approver, "decided_at": now(), "decision": "rejected"})
    _drop(jobs, job, f"rejected by {approver}", "rejected",
          f"Rejected by {approver}. Hangar job `{job.id}` will not run.")
    jobs.fleet.audit(approver, "job.reject", job.id, requested_by=a.requested_by)
    return job


def expire_approvals(jobs: JobService, t: float | None = None) -> list[Job]:
    """Cancel jobs nobody approved in time (HANGAR_JOB_APPROVAL_TTL_HOURS)."""
    t = t if t is not None else now()
    out = []
    for job in [j for j in jobs._jobs.values() if j.state == JobState.AWAITING_APPROVAL]:
        a = job.approval
        if a is None or a.expires_at is None or a.expires_at > t:
            continue
        job.approval = a.model_copy(update={"decided_at": t, "decision": "expired"})
        jobs.fleet.audit("system", "job.approval_expired", job.id, requested_by=a.requested_by)  # front-door-auth
        hours = jobs.settings.job_approval_ttl_hours
        _drop(jobs, job, "nobody approved it in time", "approval_expired",
              f"Nobody approved hangar job `{job.id}` within {hours:g} hours, so it was dropped. "
              "A trusted person can comment `/hangar` to start it again.")
        out.append(job)
    return out


def _drop(jobs: JobService, job: Job, event: str, reason: str, comment: str) -> None:
    jobs._event(job, event, JobState.CANCELLED, reason=reason)
    jobs._spawn(jobs._callback(job, "job.finished"))
    if ref := _github_ref(job):
        jobs._spawn(jobs._comment(job, ref, comment))


def awaiting_comment(jobs: JobService, job: Job, who: str) -> str:
    hours = jobs.settings.job_approval_ttl_hours
    return (f"Hangar job `{job.id}` is waiting for approval: {who} isn't on this repo's trusted list. "
            "A trusted maintainer can comment `/hangar approve` to run it, or `/hangar reject` to drop it. "
            f"It expires in {hours:g} hours.")


async def github_decision(jobs: JobService, ref: str, sender: str, trusted: bool,
                          decision: Literal["approve", "reject"]) -> dict[str, Any]:
    """`/hangar approve|reject` on an issue with a job waiting for approval."""
    if not trusted:
        jobs.admission.refused(f"owner:github:{sender}", f"github:{sender}", f"job.{decision}.refused", ref,
                               why="not trusted")  # --- front-door-auth: every approval decision is audited
        return {"ignored": f"{sender} is not trusted to {decision} jobs"}
    job = next((j for j in jobs.list(state=JobState.AWAITING_APPROVAL.value, limit=500) if j.source_ref == ref), None)
    if job is None:
        return {"ignored": f"no job is waiting for approval on {ref}"}
    try:
        (approve if decision == "approve" else reject)(jobs, job.id, f"github:{sender}")
    except JobError as e:  # decided by someone else in the meantime
        return {"ignored": str(e)}
    return {"ok": True, {"approve": "approved", "reject": "rejected"}[decision]: job.id}


# ====================================================================== inbound webhooks
class SignatureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheme: Literal["hmac-sha256"] = "hmac-sha256"
    header: str = "X-Signature"
    prefix: str = ""  # "sha256=" (GitHub style), "v1=" (PagerDuty); "" for a bare digest (Sentry, Linear)
    encoding: Literal["hex", "base64"] = "hex"
    timestamp_header: str | None = None  # e.g. Sentry-Hook-Timestamp, Linear-Timestamp (seconds or ms)
    max_skew_seconds: int = Field(300, ge=1, le=86400)
    signed_payload: str = "{body}"  # what is signed; "{timestamp}.{body}" for schemes that sign the timestamp

    @field_validator("signed_payload")
    @classmethod
    def _one_body(cls, v: str) -> str:
        if v.count("{body}") != 1 or re.sub(r"\{(body|timestamp)\}", "", v).count("{"):
            raise ValueError("signed_payload may only use {body} (exactly once) and {timestamp}")
        return v


class ContextTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["text", "file", "log", "issue", "diff"] = "text"
    content: str


class JobTemplate(BaseModel):
    """How a payload becomes a job. `{a.b.c}` is replaced by that payload field (`{$}` is the whole payload)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    instructions: str = Field(min_length=1)
    context: list[ContextTemplate] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class PayloadFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    equals: Any


class InboundSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str  # vault credential holding the HMAC key
    signature: SignatureSpec = Field(default_factory=SignatureSpec)
    repo: str
    environment: str | None = None
    template: JobTemplate
    filter: PayloadFilter | list[PayloadFilter] | None = None
    delivery_header: str | None = None  # e.g. X-GitHub-Delivery, Request-ID, Linear-Delivery
    harness: str | None = None
    model: str | None = None
    verify: str | None = None
    deliver: Literal["draft_pr", "pr", "branch", "none"] = "draft_pr"


_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_PATH_RE = re.compile(r"\{(\$|[A-Za-z0-9_][A-Za-z0-9_.-]*)\}")
_DELIVERY_RE = re.compile(r"^[\x21-\x7e]{1,200}$")


def lookup(payload: Any, path: str) -> Any:
    """Follow `a.b.0.c` through dicts (keys) and lists (indexes); None if any step is missing."""
    if path == "$":
        return payload
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def render(template: str, payload: Any) -> str:
    """Substitute `{a.b.c}` paths. Only lookups: no expressions, and substituted text is never re-expanded."""

    def sub(m: re.Match) -> str:
        v = lookup(payload, m.group(1))
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, indent=2, ensure_ascii=False)
        return str(v).lower() if isinstance(v, bool) else str(v)

    return _PATH_RE.sub(sub, template)


def _timestamp(raw: str | None) -> float | None:
    try:
        ts = float((raw or "").strip())
    except ValueError:
        return None
    return ts / 1000 if ts > 1e11 else ts  # milliseconds (Linear) or seconds


def verify_inbound(sig: SignatureSpec, secret: str, headers: Any, body: bytes, t: float) -> str | None:
    """None when the request is authentic and fresh, else why not (for logs, not for the caller)."""
    got = headers.get(sig.header)
    if not got:
        return f"missing {sig.header}"
    ts_raw = ""
    if sig.timestamp_header:
        ts_raw = (headers.get(sig.timestamp_header) or "").strip()
        ts = _timestamp(ts_raw)
        if ts is None:
            return f"missing or bad {sig.timestamp_header}"
        if abs(t - ts) > sig.max_skew_seconds:
            return f"{sig.timestamp_header} is more than {sig.max_skew_seconds}s off"
    head, _, tail = sig.signed_payload.partition("{body}")
    signed = head.replace("{timestamp}", ts_raw).encode() + body + tail.replace("{timestamp}", ts_raw).encode()
    mac = hmac.new(secret.encode(), signed, hashlib.sha256).digest()
    want = mac.hex() if sig.encoding == "hex" else base64.b64encode(mac).decode()
    ok = False
    for cand in got.split(","):  # several signatures during a secret rotation (PagerDuty)
        cand = cand.strip()
        if sig.prefix:
            if not cand.startswith(sig.prefix):
                continue
            cand = cand[len(sig.prefix):]
        if sig.encoding == "hex":
            cand = cand.lower()
        ok |= hmac.compare_digest(want.encode(), cand.encode())  # constant time, and check every candidate
    return None if ok else "signature mismatch"


class InboundResult(NamedTuple):
    status: int
    body: dict[str, Any]


# ====================================================================== cron
class CronError(ValueError):
    pass


_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_DOWS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
_MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
           "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}
_SEARCH_DAYS = 366 * 8 + 2  # Feb 29 on a given weekday can take years to come round


def _cron_field(text: str, lo: int, hi: int, names: dict[str, int] | None) -> set[int]:
    def val(s: str) -> int:
        s = s.lower()
        if names and s in names:
            return names[s]
        if not s.isdigit():
            raise CronError(f"'{s}' is not a number" + (f" or a name ({', '.join(names)})" if names else ""))
        return int(s)

    out: set[int] = set()
    for part in text.split(","):
        rng, slash, step_s = part.partition("/")
        if slash and (not step_s.isdigit() or int(step_s) == 0):
            raise CronError(f"bad step in '{part}'")
        step = int(step_s) if slash else 1
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a_s, _, b_s = rng.partition("-")
            a, b = val(a_s), val(b_s)
        else:
            a = val(rng)
            b = hi if slash else a
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise CronError(f"'{part}' is out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    """A standard 5-field cron expression (minute hour day-of-month month day-of-week), evaluated in a timezone.

    Like Vixie cron, when both day fields are restricted a day matches either. Local times skipped by a DST change
    don't run; times repeated when clocks go back run once (the first time)."""

    def __init__(self, expr: str, timezone: str = "UTC"):
        self.expr = expr.strip()
        text = _MACROS.get(self.expr.lower(), self.expr)
        fields = text.split()
        if len(fields) != 5:
            raise CronError(f"cron needs 5 fields (minute hour day month weekday), got {len(fields)}: '{expr}'")
        try:
            self.tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise CronError(f"unknown timezone '{timezone}' (use an IANA name like Europe/Berlin)") from e
        self.minutes = sorted(_cron_field(fields[0], 0, 59, None))
        self.hours = sorted(_cron_field(fields[1], 0, 23, None))
        self.doms = _cron_field(fields[2], 1, 31, None)
        self.months = _cron_field(fields[3], 1, 12, _MONTHS)
        self.dows = {d % 7 for d in _cron_field(fields[4], 0, 7, _DOWS)}  # 7 is Sunday too
        self._dom_star, self._dow_star = fields[2].startswith("*"), fields[4].startswith("*")

    def _day(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom, dow = d.day in self.doms, (d.weekday() + 1) % 7 in self.dows
        return dom and dow if self._dom_star or self._dow_star else dom or dow

    def _ts(self, d: date, h: int, m: int) -> float | None:
        naive = datetime(d.year, d.month, d.day, h, m)
        ts = naive.replace(tzinfo=self.tz).timestamp()
        if datetime.fromtimestamp(ts, self.tz).replace(tzinfo=None) != naive:
            return None  # skipped by a DST change
        return ts

    def next_after(self, t: float) -> float | None:
        """The first slot strictly after `t` (epoch seconds), or None if the expression never matches."""
        start = datetime.fromtimestamp(t, self.tz).replace(tzinfo=None, second=0, microsecond=0)
        for i in range(_SEARCH_DAYS):
            d = start.date() + timedelta(days=i)
            if not self._day(d):
                continue
            for h in self.hours:
                if i == 0 and h < start.hour:
                    continue
                for m in self.minutes:
                    if i == 0 and h == start.hour and m < start.minute:
                        continue
                    ts = self._ts(d, h, m)
                    if ts is not None and ts > t:
                        return ts
        return None

    def latest_at_or_before(self, t: float) -> float | None:
        """The most recent slot at or before `t`, or None if there is none within the search window."""
        end = datetime.fromtimestamp(t, self.tz).replace(tzinfo=None, second=0, microsecond=0)
        for i in range(_SEARCH_DAYS):
            d = end.date() - timedelta(days=i)
            if not self._day(d):
                continue
            for h in reversed(self.hours):
                if i == 0 and h > end.hour:
                    continue
                for m in reversed(self.minutes):
                    if i == 0 and h == end.hour and m > end.minute:
                        continue
                    ts = self._ts(d, h, m)
                    if ts is not None and ts <= t:
                        return ts
        return None


# ====================================================================== schedules
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class ScheduleSpec(BaseModel):
    """A new schedule: when, and the job (SubmitJob fields) to submit each time."""

    name: str
    cron: str
    timezone: str = "UTC"
    job: dict[str, Any]
    enabled: bool = True
    allow_overlap: bool = False


class ScheduleUpdate(BaseModel):
    cron: str | None = None
    timezone: str | None = None
    job: dict[str, Any] | None = None
    enabled: bool | None = None
    allow_overlap: bool | None = None


def _iso(ts: float | None, tz: str = "UTC") -> str | None:
    if ts is None:
        return None
    with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
        return datetime.fromtimestamp(ts, ZoneInfo(tz)).isoformat(timespec="minutes")
    return datetime.fromtimestamp(ts).isoformat(timespec="minutes")


class Triggers:
    """Inbound webhooks, schedules and approval expiry, wired into the app next to the job service."""

    def __init__(self, jobs: JobService, clock=time.time):
        self.jobs = jobs
        self.settings = jobs.settings
        self.store = jobs.store
        self.clock = clock  # tests drive the scheduler with a fake clock
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:  # the first tick expires approvals that ran out while the control plane was down
            try:
                self.tick()
                metrics.loop_tick("schedules")  # --- reconciler: heartbeat
            except Exception:  # noqa: BLE001 - keep ticking; the next one may succeed
                log.exception("scheduler tick failed")
                metrics.loop_failed("schedules")  # --- reconciler
            await asyncio.sleep(max(0.05, self.settings.schedule_poll_seconds))

    def tick(self) -> None:
        t = self.clock()
        expire_approvals(self.jobs, t)
        for s in self.store.list(ScheduledJob):
            if s.enabled:
                try:
                    self._run_due(s, t)
                except Exception as e:  # noqa: BLE001
                    log.exception("schedule %s failed", s.name)
                    s.last_error = f"error: {e}"[:500]
                    self._save(s)

    # ------------------------------------------------------------------ approvals
    def approve(self, job_id: str, approver: str, owner: str | None = None) -> Job:
        return approve(self.jobs, job_id, approver, owner)

    def reject(self, job_id: str, approver: str, owner: str | None = None) -> Job:
        return reject(self.jobs, job_id, approver, owner)

    # ------------------------------------------------------------------ inbound webhooks
    def inbound_source(self, source: str) -> InboundSource | None:
        raw = self.settings.inbound_webhooks.get(source) if _SOURCE_RE.match(source) else None
        if raw is None:
            return None
        try:
            return InboundSource.model_validate(raw)
        except ValidationError as e:
            log.error("HANGAR_INBOUND_WEBHOOKS['%s'] is invalid: %s", source, e)
            raise TriggerError(f"inbound webhook '{source}' is misconfigured") from e

    def handle_inbound(self, source: str, headers: Any, body: bytes) -> InboundResult:
        """Verify, filter and map one delivery to a job. A redelivery returns the job it already started."""
        try:
            cfg = self.inbound_source(source)
        except TriggerError as e:
            return InboundResult(status=503, body={"error": str(e)})
        if cfg is None:
            return InboundResult(status=404, body={"error": f"no inbound webhook '{source}'"})
        if len(body) > self.settings.inbound_max_body_bytes:
            return InboundResult(status=413, body={"error": "body too large"})
        try:
            secret = self.jobs.broker.secret_text(cfg.secret)
        except Exception:  # noqa: BLE001 - say nothing about the vault to the caller
            log.error("inbound webhook '%s': credential '%s' is not usable", source, cfg.secret)
            return InboundResult(status=503, body={"error": f"inbound webhook '{source}' is misconfigured"})
        if (why := verify_inbound(cfg.signature, secret, headers, body, self.clock())) is not None:
            log.warning("inbound webhook '%s' rejected: %s", source, why)
            return InboundResult(status=401, body={"error": "bad signature"})
        try:
            payload = json.loads(body)
        except ValueError:
            return InboundResult(status=400, body={"error": "body is not JSON"})
        if not isinstance(payload, dict):
            return InboundResult(status=400, body={"error": "body must be a JSON object"})
        filters = cfg.filter if isinstance(cfg.filter, list) else [cfg.filter] if cfg.filter else []
        for f in filters:
            if lookup(payload, f.field) != f.equals:
                return InboundResult(status=200, body={"ignored": f"{f.field} does not match the filter"})
        delivery = (headers.get(cfg.delivery_header) or "").strip() if cfg.delivery_header else ""
        if not _DELIVERY_RE.match(delivery):
            delivery = "sha256:" + hashlib.sha256(body).hexdigest()[:32]  # a retry resends the same body
        who = f"webhook:{source}"
        # --- front-door-auth: a delivery is what its signature covers. The delivery id header (and a timestamp the
        # scheme doesn't sign) can be changed by whoever replays a captured request, so they don't make it new.
        sig = cfg.signature
        signed_ts = (headers.get(sig.timestamp_header) or "").strip() \
            if sig.timestamp_header and "{timestamp}" in sig.signed_payload else ""
        replays, scope = self.jobs.admission.replays, f"inbound:{source}"
        digest = replays.digest(signed_ts.encode(), body)
        if (prev := replays.get(scope, digest)) is not None:
            self.jobs.admission.refused(f"owner:{who}", who, "hook.replay", prev, delivery=delivery[:200])
            return InboundResult(status=202, body={"ok": True, "job": prev, "duplicate": True})
        key = f"inbound:{source}:{delivery}"
        dup = next((j for j in self.jobs._jobs.values() if j.owner == who and j.idempotency_key == key), None)
        if dup is not None:
            return InboundResult(status=202, body={"ok": True, "job": dup.id, "duplicate": True})
        tpl = cfg.template
        instructions = render(tpl.instructions, payload)[:60_000]
        if not instructions.strip():
            return InboundResult(status=422, body={"error": "the template produced empty instructions"})
        title = (" ".join(render(tpl.title, payload).split())[:200] or None) if tpl.title else None
        try:
            req = SubmitJob(
                title=title, instructions=instructions,
                repo=cfg.repo, environment=cfg.environment, harness=cfg.harness, model=cfg.model,
                verify=cfg.verify, deliver=cfg.deliver, idempotency_key=key,
                context=[{"name": c.name, "kind": c.kind, "content": render(c.content, payload), "untrusted": True}
                         for c in tpl.context],
                labels={**{k[:100]: render(v, payload)[:200] for k, v in tpl.labels.items()}, "hangar.source": who})
            job = self.jobs.submit(req, Caller(who, who, restricted=True), source="webhook",
                                   source_ref=f"{who}:{delivery}"[:300], untrusted_source=who)
        except (JobError, ValidationError, KeyError) as e:
            from hangar.frontdoor import RateLimited

            if isinstance(e, RateLimited):  # --- front-door-auth: senders retry a 429 later
                return InboundResult(status=429, body={"error": str(e)})
            if isinstance(e, ValidationError):  # say which field, without echoing payload values
                err = e.errors()[0]
                msg = f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
            else:
                msg = str(e).strip("'\"")[:300]
            log.warning("inbound webhook '%s' could not start a job: %s", source, msg)
            return InboundResult(status=422, body={"error": msg})
        replays.put(scope, digest, job.id, self.settings.webhook_replay_hours * 3600)  # --- front-door-auth
        return InboundResult(status=202, body={"ok": True, "job": job.id})

    # ------------------------------------------------------------------ schedules
    def _save(self, s: ScheduledJob) -> None:
        s.updated_at = now()
        self.store.put(s)

    def get_schedule(self, ref: str) -> ScheduledJob:
        s = self.store.get(ScheduledJob, ref)
        if s is None:
            s = next((x for x in self.store.list(ScheduledJob) if x.name == ref), None)
        if s is None:
            raise KeyError(f"no schedule '{ref}'")
        return s

    def list_schedules(self) -> list[ScheduledJob]:
        return sorted(self.store.list(ScheduledJob), key=lambda s: s.name)

    def _check_template(self, job: dict[str, Any]) -> dict[str, Any]:
        """Validate a job template now, so a bad one fails at create time rather than at 3 a.m."""
        try:
            req = SubmitJob.model_validate(job)
        except ValidationError as e:
            raise TriggerError(f"job template: {e.errors()[0]['loc']}: {e.errors()[0]['msg']}") from e
        if req.idempotency_key:
            raise TriggerError("job template: idempotency_key is set by the schedule (one key per run)")
        if req.branch:
            raise TriggerError("job template: branch must be left out (each run pushes its own hangar/<job>)")
        envs = self.jobs.repo_envs(req.repo)
        env = req.environment or (envs[0] if envs else None)
        if not env:
            raise TriggerError("job template: environment is required (or a repo listed in HANGAR_GITHUB_REPOS)")
        try:
            self.jobs.fleet.catalog.get(env).pick_harness(req.harness)
            self.jobs._check_context(req.context)
            if req.callback_url:
                self.jobs._check_callback(req.callback_url)
        except KeyError as e:
            raise TriggerError(str(e).strip("'\"")) from e
        except (JobError, ValueError) as e:
            raise TriggerError(f"job template: {e}") from e
        return req.model_dump(mode="json", exclude_unset=True)

    def _cron(self, s: ScheduledJob) -> Cron:
        return Cron(s.cron, s.timezone)

    def create_schedule(self, spec: ScheduleSpec, owner: str, requester: str,
                        person: str | None = None) -> ScheduledJob:
        if not _NAME_RE.match(spec.name):
            raise TriggerError(f"schedule name '{spec.name}' must be lowercase letters, digits, '.', '_' or '-'")
        if any(x.name == spec.name for x in self.store.list(ScheduledJob)):
            raise TriggerError(f"a schedule named '{spec.name}' already exists")
        cron = Cron(spec.cron, spec.timezone)
        nxt = cron.next_after(self.clock())
        if nxt is None:
            raise TriggerError(f"'{spec.cron}' never runs")
        s = ScheduledJob(name=spec.name, cron=cron.expr, timezone=spec.timezone,
                         template=self._check_template(spec.job), enabled=spec.enabled,
                         allow_overlap=spec.allow_overlap, owner=owner, next_run_at=nxt,
                         person=person)  # --- identity
        self._save(s)
        self.jobs.fleet.audit(requester, "schedule.create", s.id, name=s.name, cron=s.cron, timezone=s.timezone)
        return s

    def update_schedule(self, ref: str, upd: ScheduleUpdate, requester: str) -> ScheduledJob:
        s = self.get_schedule(ref)
        changes = upd.model_dump(exclude_unset=True, exclude_none=True)
        cron = Cron(changes.get("cron", s.cron), changes.get("timezone", s.timezone))
        if "job" in changes:
            s.template = self._check_template(changes.pop("job"))
        resume = changes.get("enabled") is True and not s.enabled
        for k, v in changes.items():
            setattr(s, k, v.strip() if k == "cron" else v)
        if resume or {"cron", "timezone"} & changes.keys():
            # Start counting from now: re-enabling or re-timing never fires a backlog.
            s.next_run_at = cron.next_after(self.clock())
            if s.next_run_at is None:
                raise TriggerError(f"'{s.cron}' never runs")
        self._save(s)
        self.jobs.fleet.audit(requester, "schedule.update", s.id, **{k: v for k, v in changes.items() if k != "job"})
        return s

    def delete_schedule(self, ref: str, requester: str) -> ScheduledJob:
        s = self.get_schedule(ref)
        self.store.delete(ScheduledJob, s.id)
        self.jobs.fleet.audit(requester, "schedule.delete", s.id, name=s.name)
        return s

    def view_schedule(self, s: ScheduledJob) -> dict[str, Any]:
        d = s.model_dump(mode="json")
        d["next_run"] = _iso(s.next_run_at, s.timezone) if s.enabled else None
        d["last_run"] = _iso(s.last_run_slot, s.timezone)
        return d

    def _previous_running(self, s: ScheduledJob) -> Job | None:
        if not s.last_job_id:
            return None
        try:
            prev = self.jobs.get(s.last_job_id)
        except KeyError:
            return None
        return prev if prev.state not in JOB_TERMINAL else None

    def _submit(self, s: ScheduledJob, key: str) -> Job:
        req = SubmitJob.model_validate({**s.template, "idempotency_key": key})
        return self.jobs.submit(req, Caller(f"schedule:{s.name}", s.owner, person=s.person),  # --- identity
                                source="schedule",
                                source_ref=f"schedule:{s.id}")

    def run_now(self, ref: str, requester: str) -> Job:
        """Submit the schedule's job right away; its regular slots are unaffected."""
        s = self.get_schedule(ref)
        if (prev := self._previous_running(s)) is not None and not s.allow_overlap:
            raise TriggerError(f"job {prev.id} from this schedule is still {prev.state}; wait or cancel it")
        job = self._submit(s, f"schedule:{s.id}:manual:{int(self.clock())}")
        s.last_job_id, s.last_error = job.id, None
        self._save(s)
        self.jobs.fleet.audit(requester, "schedule.run", s.id, job=job.id)
        return job

    def _run_due(self, s: ScheduledJob, t: float) -> None:
        cron = self._cron(s)
        if s.next_run_at is None:
            s.next_run_at = cron.next_after(t)
            self._save(s)
            return
        if s.next_run_at > t:
            return
        # Only the latest missed slot runs: after downtime there is no backlog to catch up on.
        slot = cron.latest_at_or_before(t)
        s.next_run_at = cron.next_after(t)
        if slot is None or (s.last_run_slot is not None and slot <= s.last_run_slot):
            self._save(s)
            return
        s.last_run_slot = slot
        if (prev := self._previous_running(s)) is not None and not s.allow_overlap:
            s.last_error = f"skipped the {_iso(slot, s.timezone)} run: job {prev.id} is still {prev.state}"
            self._save(s)
            log.info("schedule %s: %s", s.name, s.last_error)
            return
        self._save(s)  # the slot is spent before the job exists, so a restart can't run it twice
        try:
            job = self._submit(s, f"schedule:{s.id}:{int(slot)}")
        except (ValueError, KeyError) as e:  # JobError and ValidationError are ValueErrors
            why = str(e).strip("'\"")[:400]
            s.last_error = f"the {_iso(slot, s.timezone)} run could not start: {why}"
            self._save(s)
            log.warning("schedule %s: %s", s.name, s.last_error)
            return
        s.last_job_id, s.last_error = job.id, None
        self._save(s)
