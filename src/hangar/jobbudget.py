"""Budgets that bound a job, and a repo's spend per day (--- frontdoor).

A `budget` caps one agent turn, and every turn used to get a fresh one: follow-ups, verify-fix turns, PR auto-fix
rounds, each best-of-N attempt and the judge. `budget.max_cost_usd=2` with best of 4, a judge and 3 auto-fix rounds
could spend $16 before anyone followed up. So:

* **A job budget** (`job_budget`: `max_cost_usd`, `max_tokens`, `max_turns`) caps all of a job's agent turns
  together. Each turn's own budget is cut to what the job has left, so the turn that crosses it is stopped like
  any turn over budget (`over_budget`, delivered as a draft); and a job that has used it up starts no more turns:
  follow-ups and auto-fix rounds are refused, verify-fix rounds aren't started. Best-of-N attempts run in
  parallel, so each gets an equal share, the judge included, and together they can't cross it. A sequential
  best-of-N (KAN-231) gives attempt 1 its `escalate_at` of the budget (or the same equal share); the attempts that
  start after it split what it left, the judge again included.
* **A daily cap per repo** (`max_usd_per_day` in the repo's policy) is checked when work is admitted: a new job
  over it waits in `awaiting_approval` (a trusted person can let it run anyway), and a follow-up or auto-fix round
  is refused. Turns already running, and a job's own verify-fix turns, finish under the job budget. A day starts
  at midnight in HANGAR_BUDGET_DAY_TIMEZONE (UTC unless set; UTC midnight is mid-afternoon or evening in the
  Americas, so a team there sets its own zone and a cap can't be spent twice in one working day).
* **A daily cap per person** (HANGAR_PERSON_MAX_USD_PER_DAY) works the same way for the jobs a person submits,
  counting everything their sessions and jobs spent that day (identity's person on each record).

Precedence for each field: the job, then the repo's policy, then HANGAR_JOB_BUDGET_*. Costs are what the harness
reports, or priced from the token counts (events.py); a turn with neither counts its tokens only, and on a
subscription login the dollars are an estimate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hangar.events import total_usage
from hangar.models import Budget, Job, JobBudget, Session, SessionTurn, now
from hangar.repopolicy import RepoPolicy, repo_key
from hangar.store import Ge  # --- durable

if TYPE_CHECKING:
    from hangar.jobs import JobService

log = logging.getLogger("hangar.jobbudget")

def merge(*layers: Any, cls: type = JobBudget) -> Any:
    """Per field, the first layer that sets it (job, repo, then settings)."""
    vals: dict[str, Any] = {}
    for f in cls.model_fields:
        vals[f] = next((getattr(x, f) for x in layers if x is not None and getattr(x, f, None) is not None), None)
    return cls(**vals) if any(v is not None for v in vals.values()) else None


def resolve(req: JobBudget | None, policy: RepoPolicy | None, settings: Any) -> JobBudget | None:
    defaults = JobBudget(max_cost_usd=settings.job_budget_max_cost_usd, max_tokens=settings.job_budget_max_tokens,
                         max_turns=settings.job_budget_max_turns)
    return merge(req, policy.job_budget if policy else None, defaults)


# ====================================================================== what a job has used
class Spent(NamedTuple):
    cost_usd: float
    tokens: int
    turns: int


def _job_sessions(svc: JobService, job: Job) -> list[Session]:
    return svc.sessions.of_job(job.id) if svc.sessions is not None else []  # --- durable: in the store too


def spent(svc: JobService, job: Job, leave_out: SessionTurn | None = None) -> Spent:
    """Every agent turn of the job's sessions (attempts and the judge included), and those deleted since. A fork's
    copy of its parent's turns never ran here, so it counts once, in the parent."""
    u = svc.usage(job)
    cost, tokens = (u.cost_usd or 0.0, u.tokens) if u else (0.0, 0)
    if leave_out is not None and (cur := total_usage([leave_out])) is not None:
        cost, tokens = cost - (cur.cost_usd or 0.0), tokens - cur.tokens
    runs = {t.run_id for s in _job_sessions(svc, job) for t in s.turns if t.run_id}
    runs |= {t.run_id for t in job.turns if t.run_id}  # jobs from before sessions carried them
    runs.discard(leave_out.run_id if leave_out is not None else None)
    return Spent(max(cost, 0.0), max(tokens, 0), len(runs))


def exhausted(svc: JobService, job: Job) -> str | None:
    """Why the job may not start another turn, or None."""
    b = job.job_budget
    if b is None:
        return None
    s = spent(svc, job)
    if b.max_turns and s.turns >= b.max_turns:
        return f"its {b.max_turns} agent turn(s) are used"
    if b.max_cost_usd and s.cost_usd >= b.max_cost_usd - min(0.005, b.max_cost_usd / 100):
        return f"{_usd(s.cost_usd)} of its {_usd(b.max_cost_usd)} job budget is spent"
    if b.max_tokens and s.tokens >= b.max_tokens:
        return f"{s.tokens:,} of its {b.max_tokens:,} job budget tokens are used"
    return None


def clip(svc: JobService, job: Job, budget: Budget | None, current: SessionTurn | None = None) -> Budget | None:
    """The turn's budget, cut to what the job has left (leaving out the turn itself, which may be running)."""
    b = job.job_budget
    if b is None or not (b.max_cost_usd or b.max_tokens):
        return budget
    s = spent(svc, job, leave_out=current)
    vals = (budget or Budget()).model_dump()
    if b.max_cost_usd:
        left = max(b.max_cost_usd - s.cost_usd, 1e-6)  # a budget is > 0; this one stops the turn at once
        vals["max_cost_usd"] = left if vals["max_cost_usd"] is None else min(vals["max_cost_usd"], left)
    if b.max_tokens:
        left_t = max(b.max_tokens - s.tokens, 1)
        vals["max_tokens"] = left_t if vals["max_tokens"] is None else min(vals["max_tokens"], left_t)
    out = Budget(**vals)
    if out != budget:
        left = [f"{_usd(out.max_cost_usd)}" if out.max_cost_usd != (budget.max_cost_usd if budget else None) else "",
                f"{out.max_tokens:,} tokens" if out.max_tokens != (budget.max_tokens if budget else None) else ""]
        svc._event(job, f"this turn may use what the job budget has left: {' and '.join(x for x in left if x)}")
    return out


def attempt_share(job: Job) -> Budget | None:
    """Best-of-N runs its attempts at once: each gets an equal share of the job budget (the judge one more), so
    together they can't cross it. The attempts are the job's first turns, so nothing was spent before them."""
    b = job.job_budget
    if b is None or not (b.max_cost_usd or b.max_tokens):
        return job.budget
    parts = job.n + (1 if job.judge else 0)
    vals = (job.budget or Budget()).model_dump()
    if b.max_cost_usd:
        share = b.max_cost_usd / parts
        vals["max_cost_usd"] = share if vals["max_cost_usd"] is None else min(vals["max_cost_usd"], share)
    if b.max_tokens:
        share_t = max(b.max_tokens // parts, 1)
        vals["max_tokens"] = share_t if vals["max_tokens"] is None else min(vals["max_tokens"], share_t)
    return Budget(**vals)


def first_share(job: Job) -> Budget | None:
    """--- KAN-231: attempt 1 of a sequential best-of-N runs alone: `escalate_at` of the job budget, or the equal
    share a parallel attempt gets. Either way the others still have their shares after it."""
    seq, b = job.sequential, job.job_budget
    if seq is None or seq.escalate_at is None or b is None or not (b.max_cost_usd or b.max_tokens):
        return attempt_share(job)
    vals = (job.budget or Budget()).model_dump()
    if b.max_cost_usd:
        share = b.max_cost_usd * seq.escalate_at
        vals["max_cost_usd"] = share if vals["max_cost_usd"] is None else min(vals["max_cost_usd"], share)
    if b.max_tokens:
        share_t = max(int(b.max_tokens * seq.escalate_at), 1)
        vals["max_tokens"] = share_t if vals["max_tokens"] is None else min(vals["max_tokens"], share_t)
    return Budget(**vals)


def used_fraction(svc: JobService, job: Job) -> float | None:
    """The share of the job budget spent so far (dollars or tokens, whichever is further along); None: no budget."""
    b = job.job_budget
    if b is None or not (b.max_cost_usd or b.max_tokens):
        return None
    s = spent(svc, job)
    return max(s.cost_usd / b.max_cost_usd if b.max_cost_usd else 0.0,
               s.tokens / b.max_tokens if b.max_tokens else 0.0)


def rest_share(svc: JobService, job: Job, attempts: int) -> Budget | None:
    """--- KAN-231: what attempt 1 of a sequential best-of-N left of the job budget, split equally among the
    `attempts` that start now (the judge one share more), so together they can't cross it."""
    b = job.job_budget
    if b is None or not (b.max_cost_usd or b.max_tokens):
        return job.budget
    s = spent(svc, job)
    parts = attempts + (1 if job.judge else 0)
    vals = (job.budget or Budget()).model_dump()
    if b.max_cost_usd:
        share = max(b.max_cost_usd - s.cost_usd, 1e-6) / parts
        vals["max_cost_usd"] = share if vals["max_cost_usd"] is None else min(vals["max_cost_usd"], share)
    if b.max_tokens:
        share_t = max((b.max_tokens - s.tokens) // parts, 1)
        vals["max_tokens"] = share_t if vals["max_tokens"] is None else min(vals["max_tokens"], share_t)
    return Budget(**vals)


def check_submit(job_budget: JobBudget | None, attempts: int, judge: bool) -> str | None:
    if job_budget and job_budget.max_turns and attempts > 1 and attempts + judge > job_budget.max_turns:
        return (f"best of {attempts}{' with a judge' if judge else ''} needs {attempts + judge} agent turns; the job "
                f"budget allows {job_budget.max_turns}")
    return None


# ====================================================================== daily caps
class Cap(NamedTuple):
    name: str  # "acme/app's daily cap"
    limit_usd: float
    spent_usd: float


def zone(settings: Any) -> str:
    """The zone daily caps count days in: HANGAR_BUDGET_DAY_TIMEZONE, or UTC when it isn't a zone (a typo must
    not lift the cap)."""
    name = getattr(settings, "budget_day_timezone", None) or "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("HANGAR_BUDGET_DAY_TIMEZONE=%r is not an IANA time zone; counting days in UTC", name)
        return "UTC"
    return name


def day_start(t: float | None = None, tz: str = "UTC") -> float:
    """Midnight of the day `t` falls in, in `tz`. Local midnight, so days are 23 or 25 hours when DST changes."""
    z = ZoneInfo(tz)
    d = datetime.fromtimestamp(now() if t is None else t, z)
    return datetime(d.year, d.month, d.day, tzinfo=z).timestamp()


def _spent_today(svc: JobService, session: Callable[[Session], bool], job_of: Callable[[Job], bool],
                 **fields: Any) -> float:
    """What the matching sessions' agent turns cost since midnight (zone()), plus the sessions deleted since of the
    matching jobs, on the day their job was created. --- durable: today's sessions and jobs (and `fields`, which
    narrow both), from the store's indexes."""
    since, total = day_start(tz=zone(svc.settings)), 0.0
    sessions = svc.sessions
    for s in sessions.touched_since(since, **fields) if sessions is not None else []:
        if not session(s):
            continue
        for t in s.turns:
            if (t.started_at or 0) >= since and (t.run_id or t.usage or t.repair_usage):
                u = total_usage([t])
                total += (u.cost_usd or 0.0) if u else 0.0
    for job in svc.touched_since(since, created_at=Ge(since), **fields):
        if job.deleted_session_usage and job_of(job):
            total += sum(u.cost_usd or 0.0 for sid, u in job.deleted_session_usage.items()
                         if sessions is None or not sessions.exists(sid))
    return total


def repo_spent_today(svc: JobService, key: str) -> float:
    """What the repo's agent turns cost today: every session on the repo (jobs, attempts, judges and interactive
    sessions), plus the sessions deleted since."""
    return _spent_today(svc, lambda s: repo_key(s.repo) == key, lambda j: repo_key(j.repo) == key)


def person_spent_today(svc: JobService, person: str) -> float:
    """--- integration: what a person's agent turns cost today: their interactive sessions and every session of
    their jobs (attempts and judges included), by the person identity records on each (people.py)."""
    return _spent_today(svc, lambda s: s.person == person, lambda j: j.person == person, person=person)


def caps(svc: JobService, repo: str | None, person: str | None = None) -> list[Cap]:
    """Every daily cap a piece of work counts against: its repo's, and the person's it runs for (identity's
    person; --- teamcreds: a GitHub or Slack sender linked to a person counts as them, while webhooks, schedules
    without a person and senders linked to nobody have none).
    Submissions, follow-ups and auto-fix rounds check every cap this returns."""
    out: list[Cap] = []
    policy = svc.policies.get(repo)
    if policy is not None and policy.max_usd_per_day:
        out.append(Cap(f"{policy.repo}'s daily cap", policy.max_usd_per_day, repo_spent_today(svc, policy.repo)))
    if person and (limit := svc.settings.person_max_usd_per_day):  # --- integration
        out.append(Cap(f"{person}'s daily cap", limit, person_spent_today(svc, person)))
    return out


def _usd(v: float) -> str:
    return f"${v:,.2f}" if v >= 0.1 else f"${v:.4f}"


def over_daily(svc: JobService, repo: str | None, person: str | None = None) -> str | None:
    for c in caps(svc, repo, person):
        if c.spent_usd >= c.limit_usd:
            return f"{c.name} is spent: {_usd(c.spent_usd)} of {_usd(c.limit_usd)} today ({zone(svc.settings)})"
    return None


def held_message(job: Job) -> str:
    why = job.approval.reason if job.approval else "a daily spend cap is reached"
    return (f"Hangar job `{job.id}` is waiting for approval: {why}. A trusted person can let it run anyway "
            "(`/hangar approve` on GitHub, `hangar job approve`); otherwise it is dropped when the approval expires.")
