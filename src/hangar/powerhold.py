"""Holds on the power schedule, and the deploy waiting for a power-up (--- infra-state; docs/gke.md, "Infra states").

A hold pauses the scheduled power-ups and power-downs until it ends: the pools stay as they are. `deploy/gke/power.sh
hold` and `off` keep it in the cluster's resource labels, where the scheduled Cloud Run jobs read it with one gcloud
call even while hangar is off. `hangar power hold` and `release` (POST /api/admin/power/hold, /release) record it
here instead: hangar can't change the cluster's labels, and gets no permission to (container.clusters.update would let
it change anything about the cluster). Each scheduled power run copies the newer of the two to the other side before
it decides (power.sh `sync_hold`), and power.sh tells hangar of its own holds, so `hangar power`, `hangar status` and
`hangar doctor` show the hold whichever side set it.

The record: {"until": epoch seconds, or None once released, "set_at": when it changed, "by": who}. The newer
`set_at` wins. A hold ends by itself at `until`; the schedule then resumes at its next time, nothing powers up early.

`deploy_pending`: a power-up found main's head ahead of the commit hangar runs, because deploy.yml skipped the merges
while the infra was off, held or down. Shown until the next deploy is recorded, or a power-up finds nothing to do.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from hangar.store import Store

HOLD_KEY = "power:hold"
PENDING_KEY = "power:deploy_pending"
MAX_DAYS = 90
SKEW = 300.0  # seconds a set_at from power.sh may run ahead of hangar's clock (it's clamped to now)
SHA = re.compile(r"[0-9a-f]{7,40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def record(store: Store) -> dict[str, Any] | None:
    got = store.kv_get_json(HOLD_KEY, None)
    return got if isinstance(got, dict) else None


def active(store: Store, now: float) -> dict[str, Any] | None:
    """The hold in force now, or None."""
    rec = record(store)
    return rec if rec and rec.get("until") is not None and float(rec["until"]) > now else None


def fmt(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(float(ts), tz).strftime("%a %d %b %H:%M %Z")


def _number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\d{9,11}(\.\d+)?", v.strip()):
        return float(v)
    return None


def parse_until(body: dict[str, Any], tz: ZoneInfo, now: float) -> float:
    """When a hold ends: {"days": N}, or {"until": epoch seconds | "2026-10-22" (that day's 00:00) |
    "2026-10-22T09:00" (both in the power schedule's time zone unless an offset is given)}."""
    days, until = body.get("days"), body.get("until")
    if days is not None and until is not None:
        raise ValueError("days or until, not both")
    if days is not None:
        try:
            n = None if isinstance(days, bool) else float(days)
        except (TypeError, ValueError):
            n = None
        if n is None or not 0 < n <= MAX_DAYS:  # NaN fails this too
            raise ValueError(f"days: a number of days, more than 0 and at most {MAX_DAYS}")
        t = now + n * 86400
    elif until is not None:
        t = _number(until)
        if t is None:
            if not isinstance(until, str):
                raise ValueError("until: epoch seconds, or a date like 2026-10-22 or 2026-10-22T09:00")
            try:
                d = datetime.fromisoformat(until.strip())
            except ValueError as e:
                raise ValueError(f"until: '{until[:40]}' isn't a date (2026-10-22, or 2026-10-22T09:00)") from e
            t = (d if d.tzinfo else d.replace(tzinfo=tz)).timestamp()
    else:
        raise ValueError("a hold needs days (a number) or until (a date)")
    if t <= now:
        raise ValueError(f"{fmt(t, tz)} has passed")
    if t > now + MAX_DAYS * 86400 + SKEW:
        raise ValueError(f"a hold lasts at most {MAX_DAYS} days")
    return t


def _set_at(body: dict[str, Any] | None, now: float) -> float:
    """power.sh copies a hold with the time it was set on the cluster; anything else is set now. Never later than
    now, so no record can claim to be newer than every change still to come."""
    t = _number((body or {}).get("set_at"))
    return min(t, now) if t is not None and t > 0 else now


def set_hold(store: Store, body: dict[str, Any], by: str, tz: ZoneInfo, now: float) -> dict[str, Any]:
    rec = {"until": parse_until(body, tz, now), "set_at": _set_at(body, now), "by": by}
    store.kv_set_json(HOLD_KEY, rec)
    return rec


def end_hold(store: Store, body: dict[str, Any] | None, by: str, now: float) -> dict[str, Any]:
    rec = {"until": None, "set_at": _set_at(body, now), "by": by}
    store.kv_set_json(HOLD_KEY, rec)
    return rec


def pending(store: Store) -> dict[str, Any] | None:
    got = store.kv_get_json(PENDING_KEY, None)
    return got if isinstance(got, dict) and got.get("head") else None


def set_pending(store: Store, body: dict[str, Any], now: float) -> dict[str, Any] | None:
    """power.sh up: main's head is ahead of what hangar runs ({"head": null} clears it)."""
    if not body.get("head"):
        store.kv_set_json(PENDING_KEY, None)
        return None
    head, deployed, repo = str(body.get("head")), str(body.get("deployed") or ""), str(body.get("repo") or "")
    if not SHA.fullmatch(head) or (deployed and not SHA.fullmatch(deployed)) or not REPO.fullmatch(repo):
        raise ValueError("head and deployed: commit shas; repo: OWNER/REPO")
    try:
        ahead = max(0, int(body.get("ahead") or 0))
    except (TypeError, ValueError) as e:
        raise ValueError("ahead: a number of commits") from e
    rec = {"repo": repo, "head": head, "deployed": deployed or None, "ahead": ahead,
           "started": bool(body.get("started")), "at": now,
           "command": f"gh workflow run deploy.yml --repo {repo} --ref main"}
    store.kv_set_json(PENDING_KEY, rec)
    return rec


def clear_pending(store: Store) -> None:
    if store.kv_get_json(PENDING_KEY, None) is not None:
        store.kv_set_json(PENDING_KEY, None)


def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/admin/power/hold (power.sh), POST /api/admin/power/hold and /release (`hangar power hold`/`release`,
    and power.sh copying its own), POST /api/admin/deploys/pending (power.sh up). Admins only; no MCP tools."""
    from fastapi import Body, HTTPException

    p = h.fleet.power

    @app.get("/api/admin/power/hold")
    async def hold_get(_: str = admin):
        return {"record": record(h.store), "hold": p.hold_view()}

    @app.post("/api/admin/power/hold")
    async def hold_set(body: dict[str, Any] = Body(...), who: str = admin):
        try:
            rec = set_hold(h.store, body, who, p._tz(p.state()), p.clock())
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        h.fleet.audit(who, "power.hold", None, until=rec["until"])
        return {"hold": p.hold_view(), "message": (
            f"Held until {p.hold_view()['when']}: the scheduled power-ups and power-downs skip until then (the next "
            "scheduled run reads this from hangar; `bash deploy/gke/power.sh hold` sets it on the cluster at once).")}

    @app.post("/api/admin/power/release")
    async def hold_release(body: dict[str, Any] | None = Body(None), who: str = admin):
        end_hold(h.store, body, who, p.clock())
        h.fleet.audit(who, "power.release", None)
        return {"hold": None, "message": "No hold: the power schedule runs from its next time."}

    @app.post("/api/admin/deploys/pending")
    async def deploy_pending(body: dict[str, Any] = Body(...), who: str = admin):
        try:
            rec = set_pending(h.store, body, p.clock())
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if rec:
            h.fleet.audit(who, "deploy.pending", None, head=rec["head"], started=rec["started"])
        return {"deploy_pending": rec}
