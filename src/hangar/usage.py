"""Cost and usage, aggregated from agent turns: GET /api/usage and the dashboard's Usage page.

Every agent turn records its tokens and cost (docs/activity.md). The turns that ran live in sessions (a job's
turns are its sessions' turns, best-of-N attempts and the judge included), so this sums session turns, plus the
turns of jobs from before sessions carried them, each turn once. A turn counts on the day it started (UTC).
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from hangar.events import total_usage
from hangar.github import parse_repo

GROUPS = ("day", "model", "environment", "requester", "repo", "harness")
DEFAULT_DAYS = 30
MAX_ROWS = 200
_REL = re.compile(r"^(\d{1,5})([dhm])$")


def parse_since(value: str | None, now: float | None = None) -> float:
    """`7d`, `12h`, `30m`, an ISO 8601 date or datetime, or epoch seconds; default: 30 days ago."""
    now = time.time() if now is None else now
    v = (value or "").strip()
    if not v:
        return now - DEFAULT_DAYS * 86400
    if m := _REL.match(v):
        return now - int(m[1]) * {"d": 86400, "h": 3600, "m": 60}[m[2]]
    try:
        return float(v)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise ValueError(f"since: use 7d, 12h, an ISO date such as 2026-10-01, or epoch seconds (got {v!r})") from None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def _empty(key: str) -> dict[str, Any]:
    return {"key": key, "turns": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "tokens": 0, "cost_usd": 0.0, "unpriced_turns": 0, "budget_stops": 0}


def _add(row: dict[str, Any], u: Any, over_budget: bool) -> None:
    row["turns"] += 1
    row["budget_stops"] += int(over_budget)
    if u is None:
        return
    for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        row[f] += getattr(u, f)
    row["tokens"] += u.tokens
    if u.cost_usd is not None:
        row["cost_usd"] += u.cost_usd
    elif u.tokens or u.cache_read_tokens:
        row["unpriced_turns"] += 1


def _repo(url: str | None) -> str:
    if not url:
        return "(no repo)"
    if name := parse_repo(url):
        return name
    u = urlsplit(url)
    return u._replace(netloc=u.netloc.rpartition("@")[2]).geturl().removesuffix(".git")  # never userinfo


def _turns(sessions: Any, jobs: Any) -> list[dict[str, Any]]:
    """One record per agent turn: when, what it used, and who and what it was for."""
    job_map = dict(jobs._jobs) if jobs is not None else {}
    out: list[dict[str, Any]] = []
    carried: set[str] = set()
    for s in list(sessions._sessions.values()):
        job = job_map.get(s.job_id or "")
        for t in s.turns:
            if not (t.run_id or t.usage or t.repair_usage):
                continue  # never ran, or a fork's copy of its parent's history (the parent counts those)
            if s.job_id:
                carried.add(s.job_id)
            out.append({"ts": t.started_at or t.finished_at or s.created_at, "usage": total_usage([t]),
                        "outcome": t.outcome, "reason": t.outcome_reason, "model": s.model,
                        "harness": t.harness or s.harness, "environment": s.environment, "repo": s.repo,
                        "requester": t.requester or (job.requester if job else s.owner),
                        "session": s.name, "job": s.job_id})
    for job in job_map.values():  # jobs from before sessions carried them: their own turn records
        if job.id in carried:
            continue
        for t in job.turns:
            if not (t.run_id or t.usage):
                continue
            out.append({"ts": t.started_at or t.finished_at or job.created_at, "usage": total_usage([t]),
                        "outcome": t.outcome, "reason": t.outcome_reason, "model": job.model, "harness": job.harness,
                        "environment": job.environment, "repo": job.repo,
                        "requester": t.requester or job.requester, "session": None, "job": job.id})
    return out


def _key(rec: dict[str, Any], group_by: str) -> str:
    if group_by == "day":
        return datetime.fromtimestamp(rec["ts"], UTC).date().isoformat()
    if group_by == "model":
        u = rec["usage"]
        return (u.model if u is not None and u.model else None) or rec["model"] or "(unknown)"
    if group_by == "repo":
        return _repo(rec["repo"])
    return rec[group_by] or "(unknown)"


def aggregate(sessions: Any, jobs: Any, group_by: str = "day", since: float | None = None,
              limit: int = 50) -> dict[str, Any]:
    if group_by not in GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(GROUPS)}")
    since = parse_since(None) if since is None else since
    rows: dict[str, dict[str, Any]] = {}
    total = _empty("total")
    stops: list[dict[str, Any]] = []
    for rec in _turns(sessions, jobs):
        if rec["ts"] < since:
            continue
        key = _key(rec, group_by)
        over = rec["outcome"] == "over_budget"
        _add(rows.setdefault(key, _empty(key)), rec["usage"], over)
        _add(total, rec["usage"], over)
        if over:
            u = rec["usage"]
            stops.append({"ts": rec["ts"], "session": rec["session"], "job": rec["job"], "reason": rec["reason"],
                          "repo": _repo(rec["repo"]), "requester": rec["requester"],
                          "cost_usd": u.cost_usd if u is not None else None})
    out = list(rows.values())
    if group_by == "day":
        out.sort(key=lambda r: r["key"])
    else:
        out.sort(key=lambda r: (r["cost_usd"], r["tokens"]), reverse=True)
    limit = max(1, min(limit, MAX_ROWS))
    for r in [*out, total]:
        r["cost_usd"] = round(r["cost_usd"], 6)
    stops.sort(key=lambda r: r["ts"], reverse=True)
    return {"group_by": group_by, "since": since, "rows": out[-limit:] if group_by == "day" else out[:limit],
            "total": total, "budget_stops": stops[:20]}


def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/usage?group_by=day|model|environment|requester|repo|harness&since=7d&limit=50 (admin)."""
    from fastapi import HTTPException

    @app.get("/api/usage")
    async def usage_route(group_by: str = "day", since: str | None = None, limit: int = 50, _: str = admin):
        try:
            return aggregate(h.sessions, h.jobs, group_by, parse_since(since), limit)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
