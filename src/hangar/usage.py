"""Cost and usage, aggregated from agent turns: GET /api/usage and the dashboard's Usage page.

Every agent turn records its tokens and cost (docs/activity.md). The turns that ran live in sessions (a job's
turns are its sessions' turns, best-of-N attempts and the judge included), so this sums session turns, plus the
turns of jobs from before sessions carried them, each turn once. A turn counts on the day it started (UTC).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from hangar.events import total_usage
from hangar.github import parse_repo

GROUPS = ("day", "model", "environment", "requester", "repo", "harness",
          "session", "job", "cache_ttl",  # --- overnight/cache: per session and job, and by the TTL turns asked for
          "source")  # --- AI review (#161): what started the job (api, jira, review, ...); "session" for none
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
            "cache_write_tokens": 0, "tokens": 0, "cost_usd": 0.0, "unpriced_turns": 0, "budget_stops": 0,
            # --- overnight/cache: 1-hour writes (part of cache_write_tokens), the share of prompt tokens read from
            # the cache, and what caching saved against no cache (turns with an unknown price add nothing)
            "cache_write_1h_tokens": 0, "cache_hit_ratio": None, "saved_usd": 0.0}


def _add(row: dict[str, Any], u: Any, over_budget: bool, turns: int = 1) -> None:
    row["turns"] += turns
    row["budget_stops"] += int(over_budget)
    if u is None:
        return
    for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cache_write_1h_tokens"):
        row[f] += getattr(u, f)
    row["saved_usd"] += u.saved_usd or 0.0  # --- overnight/cache
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


def _secs(t: Any) -> float:
    """--- KAN-300: a turn's wall time, the sandbox time it used (a kept-alive sandbox's idle time isn't counted)."""
    a, b = getattr(t, "started_at", None), getattr(t, "finished_at", None)
    return max(0.0, b - a) if a and b else 0.0


def _turns(sessions: Any, jobs: Any, since: float = 0.0) -> list[dict[str, Any]]:
    """One record per agent turn: when, what it used, and who and what it was for. --- durable: of the sessions and
    jobs saved since `since` (a turn's session and job are saved as it runs), read from the store's indexes."""
    job_map = {j.id: j for j in jobs.touched_since(since)} if jobs is not None else {}
    out: list[dict[str, Any]] = []
    carried: set[str] = set()
    for s in sessions.touched_since(since):
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
                        "session": s.name, "job": s.job_id, "cache_ttl": t.cache_ttl,
                        "source": job.source if job else ("job" if s.job_id else "session"),
                        "person": (job.person if job else None) or s.person, "seconds": _secs(t)})
    for job in job_map.values():
        # integrate (#57): a session deleted since keeps its total on the job (Job.deleted_session_usage), not its
        # turns, so it counts on the day the job was created, as no turns
        gone = [u for sid, u in job.deleted_session_usage.items() if not sessions.exists(sid)]
        out += [{"ts": job.created_at, "usage": u, "outcome": None, "reason": None, "model": job.model,
                 "harness": job.harness, "environment": job.environment, "repo": job.repo,
                 "requester": job.requester, "session": None, "job": job.id, "turns": 0, "cache_ttl": None,
                 "source": job.source, "person": job.person, "seconds": 0.0}
                for u in gone]
        if job.id in carried or gone:  # else: a job from before sessions carried it, with its own turn records
            continue
        for t in job.turns:
            if not (t.run_id or t.usage):
                continue
            out.append({"ts": t.started_at or t.finished_at or job.created_at, "usage": total_usage([t]),
                        "outcome": t.outcome, "reason": t.outcome_reason, "model": job.model, "harness": job.harness,
                        "environment": job.environment, "repo": job.repo,
                        "requester": t.requester or job.requester, "session": None, "job": job.id,
                        "cache_ttl": None, "source": job.source, "person": job.person, "seconds": _secs(t)})
    return out


def _key(rec: dict[str, Any], group_by: str) -> str:
    if group_by == "day":
        return datetime.fromtimestamp(rec["ts"], UTC).date().isoformat()
    if group_by == "model":
        u = rec["usage"]
        return (u.model if u is not None and u.model else None) or rec["model"] or "(unknown)"
    if group_by == "repo":
        return _repo(rec["repo"])
    if group_by == "cache_ttl":  # --- overnight/cache: turns from before, or harnesses with no TTL setting
        return rec["cache_ttl"] or "(not set)"
    return rec[group_by] or "(unknown)"


def aggregate(sessions: Any, jobs: Any, group_by: str = "day", since: float | None = None,
              limit: int = 50) -> dict[str, Any]:
    if group_by not in GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(GROUPS)}")
    since = parse_since(None) if since is None else since
    rows: dict[str, dict[str, Any]] = {}
    total = _empty("total")
    stops: list[dict[str, Any]] = []
    for rec in _turns(sessions, jobs, since):
        if rec["ts"] < since:
            continue
        key = _key(rec, group_by)
        over = rec["outcome"] == "over_budget"
        _add(rows.setdefault(key, _empty(key)), rec["usage"], over, rec.get("turns", 1))
        _add(total, rec["usage"], over, rec.get("turns", 1))
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
        r["saved_usd"] = round(r["saved_usd"], 6)  # --- overnight/cache
        prompt = r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"]
        r["cache_hit_ratio"] = round(r["cache_read_tokens"] / prompt, 4) if prompt else None
    stops.sort(key=lambda r: r["ts"], reverse=True)
    return {"group_by": group_by, "since": since, "rows": out[-limit:] if group_by == "day" else out[:limit],
            "total": total, "budget_stops": stops[:20]}


# --- KAN-300: the chargeback export
EXPORT_BY = ("person", "repo")
EXPORT_COLUMNS = ("month", "key", "turns", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                  "tokens", "cost_usd", "unpriced_turns", "sandbox_hours", "merged_prs")


def month_bounds(month: str) -> tuple[float, float]:
    """`YYYY-MM` as [first second, first second of the next month), UTC."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", month or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError("month is YYYY-MM, e.g. 2026-11")
    y, mo = int(m.group(1)), int(m.group(2))
    lo = datetime(y, mo, 1, tzinfo=UTC)
    hi = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=UTC)
    return lo.timestamp(), hi.timestamp()


def _export_key(by: str, person: str | None, requester: str | None, repo: str | None) -> str:
    if by == "repo":
        return _repo(repo)
    return person or requester or "(unknown)"


def export(sessions: Any, jobs: Any, month: str, by: str = "person") -> str:
    """One CSV row per person (or repo) for a calendar month (UTC): tokens, cost, sandbox hours (the turns' wall
    time) and the agent PRs merged that month, for whoever pays (docs/metrics.md#chargeback-export)."""
    import csv
    import io

    if by not in EXPORT_BY:
        raise ValueError(f"by must be one of {', '.join(EXPORT_BY)}")
    lo, hi = month_bounds(month)
    rows: dict[str, dict[str, Any]] = {}

    def row(key: str) -> dict[str, Any]:
        return rows.setdefault(key, {**_empty(key), "seconds": 0.0, "merged_prs": 0})

    for rec in _turns(sessions, jobs, lo):
        if not lo <= rec["ts"] < hi:
            continue
        r = row(_export_key(by, rec.get("person"), rec["requester"], rec["repo"]))
        _add(r, rec["usage"], rec["outcome"] == "over_budget", rec.get("turns", 1))
        r["seconds"] += rec.get("seconds") or 0.0
    if jobs is not None:
        for j in jobs.touched_since(lo):
            if j.source in ("experiment", "review") or not j.result.pr_number:
                continue
            if lo <= (j.landing.merged_at or 0) < hi:
                row(_export_key(by, j.person, j.requester, j.repo))["merged_prs"] += 1
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(EXPORT_COLUMNS)
    for r in sorted(rows.values(), key=lambda r: (-r["cost_usd"], r["key"])):
        w.writerow([month, r["key"], r["turns"], r["input_tokens"], r["output_tokens"], r["cache_read_tokens"],
                    r["cache_write_tokens"], r["tokens"], f"{r['cost_usd']:.6f}", r["unpriced_turns"],
                    f"{r['seconds'] / 3600:.4f}", r["merged_prs"]])
    return out.getvalue()


def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/usage?group_by=day|model|...|cache_ttl|source&since=7d&limit=50
    (admin)."""
    from fastapi import HTTPException

    @app.get("/api/usage")
    async def usage_route(group_by: str = "day", since: str | None = None, limit: int = 50, _: str = admin):
        try:
            t0 = parse_since(since)
            out = aggregate(h.sessions, h.jobs, group_by, t0, limit)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        from hangar import weekly  # --- KAN-300: what the window's spend bought, next to what it cost

        out["prs"] = await asyncio.to_thread(weekly.window, h.jobs, t0, time.time())
        return out

    @app.get("/api/usage/export")  # --- KAN-300: GET /api/usage/export?month=YYYY-MM&by=person|repo (admin), a CSV
    async def usage_export(month: str, by: str = "person", _: str = admin):
        from fastapi.responses import PlainTextResponse

        try:
            body = await asyncio.to_thread(export, h.sessions, h.jobs, month, by)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return PlainTextResponse(body, media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="usage-{month}-{by}.csv"'})
