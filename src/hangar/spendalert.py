"""Spend alerts (KAN-290): a person's or a repo's daily cap 80% and 100% spent, said before it stops work.

A daily cap (jobbudget.py) holds new jobs once it is spent, and nothing said it was getting close. So every few minutes
the ops loop (opstatus.MINUTELY) adds up today's spend by person and by repo, the way the caps count it, and for each
share in HANGAR_SPEND_ALERT_AT (80% and 100% by default) a cap reaches for the first time today:

* **A Slack DM** through the inbox's sender (inbox_notify.send_dm): a person's cap to that person, and every cap to the
  admins, each who has a linked Slack account and doesn't have `--dm off`. It says what is spent, what happens at
  100%, and who can raise the cap.
* **A log line** `hangar.slo: event=spend_cap scope=person level=80` (slolog.py), which Cloud Monitoring's
  `hangar_spend_cap` metric counts (deploy/terraform/gke/alerting.tf, the spend-cap-80 and spend-cap-100 alerts),
  and **a counter** hangar_spend_cap_alerts_total{scope,level} for installs that scrape /metrics
  (deploy/k8s/observability/prometheus-rules.yaml). Both are bounded: never a person or a repo.

Each (day, cap, amount, share) alerts once: `spendalert:<day>:<scope>:<name>:<usd>:<pct>` in kv, pruned once the day
is over, so a cap raised mid-day alerts again. A cap that jumps past both shares in one pass sends one DM, at the
higher share, and a line for each. The counter's series start at 0 (prime(), each pass), so Prometheus's increase()
sees the first alert after a restart.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from hangar import jobbudget, metrics, people, slolog
from hangar.events import total_usage
from hangar.repopolicy import repo_key
from hangar.store import Ge

if TYPE_CHECKING:
    from hangar.jobs import JobService

log = logging.getLogger("hangar.spendalert")

PREFIX = "spendalert:"
EVERY = 300.0  # seconds between passes: spend moves in turns, not seconds


class Crossing(NamedTuple):
    scope: str  # "person" or "repo"
    name: str  # the person id or the repo key
    limit_usd: float
    spent_usd: float
    levels: tuple[int, ...]  # the shares newly reached, in percent, ascending


def spent_today_by(svc: JobService, since: float | None = None) -> tuple[dict[str, float], dict[str, float]]:
    """Today's spend by repo key and by person, counted as jobbudget._spent_today counts one of them: agent turns of
    the sessions touched since midnight (zone()), plus deleted sessions of the jobs created today. One pass."""
    since = jobbudget.day_start(tz=jobbudget.zone(svc.settings)) if since is None else since
    by_repo: dict[str, float] = {}
    by_person: dict[str, float] = {}

    def add(repo: str | None, person: str | None, usd: float) -> None:
        if usd <= 0:
            return
        if (key := repo_key(repo)) is not None:
            by_repo[key] = by_repo.get(key, 0.0) + usd
        if person:
            by_person[person] = by_person.get(person, 0.0) + usd

    sessions = svc.sessions
    for s in sessions.touched_since(since) if sessions is not None else []:
        usd = 0.0
        for t in s.turns:
            if (t.started_at or 0) >= since and (t.run_id or t.usage or t.repair_usage):
                u = total_usage([t])
                usd += (u.cost_usd or 0.0) if u else 0.0
        add(s.repo, s.person, usd)
    for job in svc.touched_since(since, created_at=Ge(since)):
        if job.deleted_session_usage:
            add(job.repo, job.person, sum(u.cost_usd or 0.0 for sid, u in job.deleted_session_usage.items()
                                          if sessions is None or not sessions.exists(sid)))
    return by_repo, by_person


def levels(settings: Any) -> list[int]:
    """HANGAR_SPEND_ALERT_AT as whole percents, ascending; shares outside (0, 1] are ignored."""
    raw = getattr(settings, "spend_alert_at", None) or []
    return sorted({round(float(x) * 100) for x in raw if 0 < float(x) <= 1})


def _key(day: int, scope: str, name: str, limit_usd: float, pct: int) -> str:
    """The cap's amount is part of the key, so a cap raised mid-day alerts again at its new 80% and 100%."""
    return f"{PREFIX}{day}:{scope}:{name}:{limit_usd:g}:{pct}"


def crossings(svc: JobService, t: float | None = None) -> list[Crossing]:
    """Caps that reached a share today they haven't alerted on yet (nothing is marked here)."""
    pcts = levels(svc.settings)
    if not pcts:
        return []
    tz = jobbudget.zone(svc.settings)
    day = int(jobbudget.day_start(t, tz))
    by_repo, by_person = spent_today_by(svc, since=float(day))
    candidates: list[tuple[str, str, float, float]] = []
    if limit := svc.settings.person_max_usd_per_day:
        candidates += [("person", p, limit, usd) for p, usd in sorted(by_person.items())]
    for repo, usd in sorted(by_repo.items()):
        if limit_r := jobbudget.repo_daily_cap(svc.settings, svc.policies.get(repo)):
            candidates.append(("repo", repo, limit_r, usd))
    out: list[Crossing] = []
    for scope, name, limit_usd, usd in candidates:
        new = tuple(p for p in pcts
                    if usd >= limit_usd * p / 100 and svc.store.kv_get(_key(day, scope, name, limit_usd, p)) is None)
        if new:
            out.append(Crossing(scope, name, limit_usd, usd, new))
    return out


def message(c: Crossing, tz: str, own: bool = False) -> str:
    """The DM. `own`: to the person whose cap it is ("your daily cap"); else the cap is named."""
    from hangar.slack import escape

    pct = c.levels[-1]
    whose = "your" if own else f"{escape(c.name)}'s"
    head = (f"Spend alert: {whose} daily cap is {pct}% spent ({jobbudget._usd(c.spent_usd)} of "
            f"{jobbudget._usd(c.limit_usd)} today; days start at midnight {tz}).")
    what = ("New jobs now wait for a trusted person's approval, and follow-ups and auto-fix rounds are refused, "
            "until the day ends." if pct >= 100 else
            "At 100% new jobs wait for a trusted person's approval, and follow-ups and auto-fix rounds are refused.")
    setting = ("HANGAR_PERSON_MAX_USD_PER_DAY" if c.scope == "person" else
               f"the repo's max_usd_per_day (`hangar repo set {escape(c.name)} --max-usd-per-day <usd>`), or "
               "HANGAR_REPO_MAX_USD_PER_DAY for every repo without one")
    return f"{head}\n{what}\nTo raise it, ask a hangar admin to change {setting} (docs/team-scale.md)."


async def _notify(svc: JobService, c: Crossing, tz: str) -> int:
    """DM the cap's person (a person cap) and the admins; returns how many DMs went."""
    from hangar.inbox_notify import send_dm, slack_user

    sent = 0
    for p in people.list_people(svc.store):
        own = c.scope == "person" and p.id == c.name
        if p.notify == "off" or (user := slack_user(p)) is None or not (own or p.role == "admin"):
            continue
        if await send_dm(svc, user, message(c, tz, own)):
            sent += 1
    return sent


async def check(svc: JobService, t: float | None = None) -> list[Crossing]:
    """One pass: alert each new crossing, mark it, prune past days. Returns what alerted."""
    t = time.time() if t is None else t
    tz = jobbudget.zone(svc.settings)
    day = int(jobbudget.day_start(t, tz))
    prime(svc.settings)
    found = await asyncio.to_thread(crossings, svc, t)  # a scan of today's sessions and jobs: off the event loop
    for c in found:
        for pct in c.levels:
            slolog.emit("spend_cap", scope=c.scope, level=pct)
            if metrics.M is not None:
                metrics.M.spend_alerts.labels(c.scope, str(pct)).inc()
            svc.store.kv_set(_key(day, c.scope, c.name, c.limit_usd, pct), f"{t:.0f}")
        try:
            await _notify(svc, c, tz)
        except Exception:  # noqa: BLE001 - the log line and the metric already carry the alert
            log.exception("spend alert DM failed for %s cap %s", c.scope, c.name)
    for k in svc.store.kv_keys(PREFIX):
        try:
            if int(k[len(PREFIX):].split(":", 1)[0]) < day:
                svc.store.kv_delete(k)
        except ValueError:
            svc.store.kv_delete(k)
    return found


async def tick(h: Any) -> None:
    """The ops loop's minute: a pass every EVERY seconds."""
    svc = getattr(h, "jobs", None)
    if svc is None or getattr(svc, "store", None) is None:
        return
    last = svc.__dict__.get("_spendalert_last", 0.0)
    if time.time() - last < EVERY:
        return
    svc.__dict__["_spendalert_last"] = time.time()
    await check(svc)


def prime(settings: Any) -> None:
    """Create each scope's and share's counter series at 0: increase() can't see a series that first appears at 1."""
    if metrics.M is not None:
        for scope in ("person", "repo"):
            for pct in levels(settings):
                metrics.M.spend_alerts.labels(scope, str(pct))


def register() -> None:
    from hangar import opstatus

    if tick not in opstatus.MINUTELY:
        opstatus.MINUTELY.append(tick)
