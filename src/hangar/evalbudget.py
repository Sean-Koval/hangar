"""The team's monthly eval budget, with per-author sub-caps (#182; docs/repo-policy.md#the-eval-budget).

Evals run on API keys with dollar caps. Budgets already bound each trial turn and trial job, each experiment, and each
repo's and person's day (jobbudget.py); this bounds eval spend per calendar month (UTC) across experiments:

* the team: `HANGAR_EVAL_BUDGET_USD_MONTH` (1500; 0 turns the eval budget off);
* each author (the experiment's person, else its requester): `HANGAR_EVAL_AUTHOR_CAP_USD_MONTH` (400), or their own
  (`hangar person set alice --eval-cap 600`, an admin's call).

**Spent** is what eval jobs (`queue_class: eval`, #181: experiment trials and the trials API's) created this month
cost, every session that carried them included (deleted ones too, as `JobService.usage` counts). Cached trial results
(#180) are no new job and cost nothing.

**The gate.** At `POST /api/trials` and when an experiment is submitted, the projected cost is the number of trials
times each task's p50 cost (from the cached trials of the same key, else the trial job budget's `max_cost_usd`); a
request whose projection exceeds the team's or the author's remaining budget is refused, with the numbers. A task with
neither history nor a dollar budget projects 0, which the message notes. Trials already running finish whatever they
push spend to (no trial is killed half-way: the per-trial job budget is the hard stop), but no new trial starts once
either budget is spent (experiments._stop_starting).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from hangar.store import Ge

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job


def clock() -> float:
    """Now, epoch seconds (tests replace it)."""
    return time.time()


def month_start(t: float | None = None) -> float:
    d = datetime.fromtimestamp(clock() if t is None else t, UTC)
    return datetime(d.year, d.month, 1, tzinfo=UTC).timestamp()


def month_name(t: float | None = None) -> str:
    return datetime.fromtimestamp(clock() if t is None else t, UTC).strftime("%Y-%m")


def author_of(job: Job) -> str:
    """Whose eval spend a job counts as: its person, else who asked for it."""
    from hangar.experiments import AUTHOR_LABEL
    from hangar.fairness import person_of

    return job.labels.get(AUTHOR_LABEL) or job.person or person_of(job.requester) or job.requester or "unknown"


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def team_budget(settings: Any) -> float:
    return max(0.0, float(settings.eval_budget_usd_month or 0))


def author_cap(svc: JobService, author: str) -> float:
    from hangar import people

    p = people.get(svc.store, author)
    if p is not None and p.eval_cap_usd_month is not None:
        return max(0.0, float(p.eval_cap_usd_month))
    return max(0.0, float(svc.settings.eval_author_cap_usd_month or 0))


SPEND_TTL = 15.0  # seconds a computed month's spend is reused (the experiment loop asks every poll)


def spending(svc: JobService, t: float | None = None, fresh: bool = False) -> dict[str, float]:
    """This month's eval spend by author (everyone together is the sum)."""
    since = month_start(t)
    memo = svc.__dict__.get("_eval_spend")
    if not fresh and memo is not None and memo[0] == since and time.monotonic() - memo[1] < SPEND_TTL:
        return dict(memo[2])
    out = _spending(svc, since)
    svc.__dict__["_eval_spend"] = (since, time.monotonic(), out)
    return dict(out)


def _spending(svc: JobService, since: float) -> dict[str, float]:
    from hangar import jobbudget

    end = month_start(datetime.fromtimestamp(since, UTC).replace(day=28).timestamp() + 5 * 86400)
    out: dict[str, float] = {}
    for job in svc.touched_since(since, created_at=Ge(since)):
        if job.queue_class != "eval" or job.created_at >= end:
            continue
        cost = jobbudget.spent(svc, job).cost_usd
        if cost:
            a = author_of(job)
            out[a] = out.get(a, 0.0) + cost
    return out


def status(svc: JobService, author: str | None = None) -> dict[str, Any]:
    """`GET /api/eval/budget`: the month, the team's budget, spent and remaining, and per author."""
    by = spending(svc, fresh=True)
    team = team_budget(svc.settings)
    spent = sum(by.values())
    authors = sorted(set(by) | ({author} if author else set()))
    return {"month": month_name(), "on": team > 0, "team": {"budget_usd": team, "spent_usd": round(spent, 4),
                                                             "remaining_usd": round(max(0.0, team - spent), 4)},
            "authors": {a: {"cap_usd": author_cap(svc, a), "spent_usd": round(by.get(a, 0.0), 4),
                            "remaining_usd": round(max(0.0, author_cap(svc, a) - by.get(a, 0.0)), 4)}
                        for a in authors}}


def spent_reason(svc: JobService, author: str) -> str | None:
    """Why no new trial may start for `author`: the team's or their own monthly eval budget is spent."""
    if getattr(svc, "settings", None) is None or not hasattr(svc, "touched_since"):
        return None  # a service without jobs to count (tests' stand-ins)
    team = team_budget(svc.settings)
    if team <= 0:
        return None
    by = spending(svc)
    total = sum(by.values())
    if total >= team:
        return f"the team's monthly eval budget is spent ({_usd(total)} of {_usd(team)}, {month_name()})"
    cap = author_cap(svc, author)
    if cap > 0 and by.get(author, 0.0) >= cap:
        return f"{author}'s monthly eval cap is spent ({_usd(by.get(author, 0.0))} of {_usd(cap)}, {month_name()})"
    return None


def p50(xs: list[float]) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def task_cost(svc: JobService, parts: dict[str, Any] | None, budget_usd: float | None) -> tuple[float, str]:
    """One trial's projected cost and where it came from: the p50 of the cached trials of the key, else the trial
    job budget's dollars, else 0."""
    from hangar import jobbudget, trialcache

    costs = []
    for x in trialcache.lookup(svc.store, parts) if parts and getattr(svc, "store", None) is not None else []:
        job = svc._jobs.get(x.get("job") or "")
        if job is not None:
            costs.append(jobbudget.spent(svc, job).cost_usd)
    if (m := p50(costs)) is not None:
        return m, f"p50 of {len(costs)} cached trial(s)"
    if budget_usd:
        return float(budget_usd), "the trial job budget"
    return 0.0, "no history and no dollar budget"


def gate(svc: JobService, author: str, projected: float, basis: str) -> None:
    """Refuse `projected` dollars of new trials past the team's or the author's remaining budget (JobError)."""
    from hangar.jobs import JobError

    team = team_budget(svc.settings)
    if team <= 0:
        return
    by = spending(svc, fresh=True)
    total = sum(by.values())
    if projected > max(0.0, team - total) + 1e-9:
        raise JobError(f"the trials would cost about {_usd(projected)} ({basis}), more than the team's monthly eval "
                       f"budget has left: {_usd(total)} of {_usd(team)} spent, {_usd(max(0.0, team - total))} "
                       f"remaining ({month_name()}). Ask for fewer trials, or an admin raises "
                       "HANGAR_EVAL_BUDGET_USD_MONTH")
    cap = author_cap(svc, author)
    mine = by.get(author, 0.0)
    if cap > 0 and projected > max(0.0, cap - mine) + 1e-9:
        raise JobError(f"the trials would cost about {_usd(projected)} ({basis}), more than {author}'s monthly eval "
                       f"cap has left: {_usd(mine)} of {_usd(cap)} spent, {_usd(max(0.0, cap - mine))} remaining "
                       f"({month_name()}). Ask for fewer trials, or an admin sets "
                       f"`hangar person set {author} --eval-cap N`")


def experiment_gate(svc: JobService, spec: Any, caller: Any) -> None:
    """At an experiment's submit: tasks x variants x trials, each at its task's p50 (or the trial job budget)."""
    from hangar import pinsets, trialcache

    if team_budget(svc.settings) <= 0:
        return
    budget_usd = spec.trial_job_budget.max_cost_usd if spec.trial_job_budget else None
    total, bases = 0.0, set()
    for t in spec.tasks:
        parts = None
        for vname in spec.variants:
            pid = (spec.pin_sets or {}).get(vname)
            if pid and (td := (spec.task_digests or {}).get(t.id)):
                try:
                    parts = trialcache.parts_for(pinsets.get(svc.fleet, pid), t.id, td)
                except KeyError:
                    parts = None
            cost, basis = task_cost(svc, parts, budget_usd)
            total += cost * spec.trials
            bases.add(basis)
    if total <= 0:
        return  # nothing to project: no history and no dollar budget (the per-trial budget, if any, still binds)
    gate(svc, author_from(caller), total, ", ".join(sorted(bases)))


def author_from(caller: Any) -> str:
    from hangar.fairness import person_of

    if caller is None:
        return "unknown"
    return person_of(caller.requester, caller.person) or caller.requester or "unknown"


def mount(app: Any, h: Any, U: Any, admin: Any) -> None:
    """GET /api/eval/budget (anyone with a login), PUT /api/people/{id}/eval-cap (admin)."""
    from fastapi import Body, HTTPException

    from hangar import identity, people

    @app.get("/api/eval/budget")
    async def eval_budget(_: str = U):
        return status(h.jobs, identity.person())

    @app.put("/api/people/{person_id}/eval-cap")
    async def eval_cap(person_id: str, body: dict[str, Any] = Body(...), who: str = admin):
        p = people.get(h.store, person_id)
        if p is None:
            raise HTTPException(404, f"no person '{person_id}'")
        v = body.get("usd")
        if v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError) as e:
                raise HTTPException(422, "usd must be a number of dollars, or null for the default") from e
            if v < 0:
                raise HTTPException(422, "usd can't be negative")
        p.eval_cap_usd_month = v
        h.store.put(p)
        h.fleet.audit(who, "person.eval_cap", person_id, usd=v)
        return {"id": p.id, "eval_cap_usd_month": p.eval_cap_usd_month}
