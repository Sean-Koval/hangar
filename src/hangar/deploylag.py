"""How far production is behind main (KAN-216; docs/runbooks/deploy.md): `hangar doctor`, the console and an alert.

Production once ran a commit from 10-17 while main moved 383 commits on: deploy.yml coalesces merges behind the one
run waiting for the `production` approval, and nobody saw that run go stale. This makes the lag visible.

* What runs: the newest deploy record (power.py, POST /api/admin/deploys from deploy.yml) gives the commit and the
  server version; a rolled-back deploy runs the image it replaced, whose tag is its commit.
* How far behind: GitHub's compare of that commit with main, through hangar's own GitHub token
  (HANGAR_GITHUB_TOKEN_CREDENTIAL), at most every REFRESH seconds and cached in the store. Offline, or without a token
  or a repo, the lag is "unknown" and nothing fails.
* Days behind count from the oldest commit main has and production lacks: a merge commit's committer date (when the
  pull request merged), or any commit's where main has no merges (squash and rebase merges).
* The alert: every minute (opstatus.MINUTELY) that production is more than HANGAR_DEPLOY_LAG_ALERT_WORKING_DAYS working
  days behind while the infra is up (the pools on, no hold), one `hangar.slo: event=deploy_lag` line, which the
  `deploy-lag` policy in deploy/terraform/gke/alerting.tf counts, and hangar_deploy_lag_over for Prometheus. Never
  while the pools are powered down for the night: deploy.yml skips merges then by design.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from hangar import metrics, slolog
from hangar.store import Store

log = logging.getLogger(__name__)

KEY = "ops:deploy_lag"
REFRESH = 900.0  # seconds between looks at GitHub (a new deploy record looks at once)
TIMEOUT = 15.0  # seconds a look may take before the lag is unknown
_SHA = re.compile(r"[0-9a-f]{7,40}")
_IMAGE_SHA = re.compile(r":([0-9a-f]{40})(?:@|$)")
_RUN_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


# ---------------------------------------------------------------------- what runs
def _image_sha(image: Any) -> str | None:
    m = _IMAGE_SHA.search(image) if isinstance(image, str) else None
    return m.group(1) if m else None


def running(store: Store) -> dict[str, Any] | None:
    """The commit production runs, from the deploy records (newest first): {"sha", "version", "deployed_at"}."""
    from hangar.power import deploys

    recs = deploys(store)
    for i, rec in enumerate(recs):
        if rec.get("result") == "rolled_back":
            sha = _image_sha(rec.get("previous_image"))
            if not sha:
                continue  # an older record says what it went back to
            older = next((r for r in recs[i + 1:] if (r.get("sha") or _image_sha(r.get("image"))) == sha), {})
            return {"sha": sha, "version": older.get("version"), "deployed_at": older.get("at"),
                    "rolled_back_from": rec.get("sha")}
        sha = rec.get("sha") or _image_sha(rec.get("image"))
        if isinstance(sha, str) and _SHA.fullmatch(sha):
            return {"sha": sha, "version": rec.get("version"), "deployed_at": rec.get("at")}
    return None


def repo(store: Store, settings: Any) -> str | None:
    """The repo deploy.yml deploys from: HANGAR_DEPLOY_REPO, else the newest deploy record's run URL, else the repo
    power.sh named when a power-up found main ahead."""
    from hangar import powerhold
    from hangar.power import deploys

    if settings.deploy_repo and _REPO.fullmatch(settings.deploy_repo.strip()):
        return settings.deploy_repo.strip()
    for rec in deploys(store):
        if m := _RUN_REPO.match(str(rec.get("run_url") or "")):
            return m.group(1)
    pend = powerhold.pending(store)
    return pend.get("repo") if pend else None


# ---------------------------------------------------------------------- days
def _iso(s: Any) -> float | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def oldest_undeployed(commits: list[dict[str, Any]]) -> float | None:
    """When the oldest of `commits` (the ones main has and production lacks) reached main."""
    def when(c: dict[str, Any]) -> float | None:
        return _iso(((c.get("commit") or {}).get("committer") or {}).get("date"))

    merges = [c for c in commits if isinstance(c, dict) and len(c.get("parents") or []) > 1]
    times = [t for c in (merges or commits) if isinstance(c, dict) and (t := when(c)) is not None]
    return min(times) if times else None


def working_days(since: float, now: float, tz: ZoneInfo) -> int:
    """Monday-to-Friday dates after `since`'s and up to `now`'s, in `tz`: merged Monday, it is 2 on Wednesday."""
    d, end = datetime.fromtimestamp(since, tz).date(), datetime.fromtimestamp(now, tz).date()
    n = 0
    for _ in range(3700):  # ten years; past that the number doesn't matter
        if d >= end:
            break
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


# ---------------------------------------------------------------------- the look at GitHub
def cached(store: Store) -> dict[str, Any]:
    got = store.kv_get_json(KEY, None)
    return got if isinstance(got, dict) else {}


async def _look(gh: Any, full: str, sha: str, known_tag: tuple[str | None] | None) -> dict[str, Any]:
    main = await gh.branch_sha(full, "main")
    cmp = await gh.compare_first_page(full, sha, main)
    status = str(cmp.get("status") or "")
    behind = int(cmp.get("ahead_by") or 0) if status in ("ahead", "diverged") else 0
    out: dict[str, Any] = {"main_sha": main, "behind_commits": behind, "compare": status or None,
                           "oldest_undeployed_at": oldest_undeployed(cmp.get("commits") or []) if behind else None}
    if known_tag is not None:
        out["tag"] = known_tag[0]
    else:
        out["tag"] = next((t.get("name") for t in await gh.tags(full)
                           if (t.get("commit") or {}).get("sha") == sha), None)
    return out


async def refresh(h: Any, force: bool = False, clock=None) -> dict[str, Any]:
    """Look again when the cache is older than REFRESH or production runs another commit. Never raises."""
    power = getattr(getattr(h, "fleet", None), "power", None)
    clock = clock or getattr(power, "clock", None) or time.time
    store, t = h.store, clock()
    run = running(store)
    old = cached(store)
    if run is None:
        rec: dict[str, Any] = {"checked_at": t, "error": "no deploy recorded yet (deploy.yml records each one)"}
        store.kv_set_json(KEY, rec)
        return rec
    if (not force and old.get("sha") == run["sha"] and not old.get("error")
            and t - float(old.get("checked_at") or 0) < REFRESH):
        return old
    if (not force and old.get("sha") == run["sha"] and old.get("error")
            and t - float(old.get("checked_at") or 0) < REFRESH / 3):
        return old  # a failed look is tried again sooner, but not every minute
    rec = {**run, "checked_at": t}
    full = repo(store, h.settings)
    jobs = getattr(h, "jobs", None)
    gh = jobs._github() if jobs is not None and hasattr(jobs, "_github") else None
    if not full:
        rec["error"] = "no repo to compare with (set HANGAR_DEPLOY_REPO=OWNER/REPO)"
    elif gh is None:
        rec["error"] = "no GitHub token (HANGAR_GITHUB_TOKEN_CREDENTIAL)"
    else:
        rec["repo"] = full
        same = old.get("sha") == run["sha"] and "tag" in old and not old.get("error")
        try:
            rec |= await asyncio.wait_for(_look(gh, full, run["sha"], (old.get("tag"),) if same else None), TIMEOUT)
        except Exception as e:  # noqa: BLE001 - offline, a 404, a timeout: the lag is unknown, nothing fails
            rec["error"] = f"couldn't ask GitHub: {type(e).__name__}: {str(e)[:200]}"
            if same:
                rec["tag"] = old.get("tag")
        finally:
            with contextlib.suppress(Exception):
                await gh.close()
    store.kv_set_json(KEY, rec)
    return rec


# ---------------------------------------------------------------------- the view
def view(store: Store, settings: Any, power: Any = None, clock=time.time) -> dict[str, Any] | None:
    """For the overview, the console and doctor, from the cache (no GitHub call). None before any deploy."""
    rec = cached(store)
    run = running(store)
    if run is None:
        return None
    t = clock()
    if rec.get("sha") != run["sha"]:  # a deploy since the last look: what runs is known, the lag not yet
        rec = {**run, "error": "not checked yet"}
    tz = power._tz(power.state()) if power is not None else ZoneInfo("UTC")
    threshold = float(settings.deploy_lag_alert_working_days)
    behind = rec.get("behind_commits") if not rec.get("error") else None
    oldest = rec.get("oldest_undeployed_at") if behind else None
    days = int((t - float(oldest)) // 86400) if oldest is not None else (0 if behind == 0 else None)
    wdays = working_days(float(oldest), t, tz) if oldest is not None else (0 if behind == 0 else None)
    infra_up = power is None or (not power.off and power.hold_view() is None)
    over = bool(behind and wdays is not None and wdays > threshold)
    return {"sha": run["sha"], "short": run["sha"][:7], "tag": rec.get("tag"), "version": run.get("version"),
            "deployed_at": run.get("deployed_at"), "repo": rec.get("repo"), "main_sha": rec.get("main_sha"),
            "behind_commits": behind, "days_behind": days, "working_days_behind": wdays,
            "state": "unknown" if behind is None else ("behind" if behind else "current"),
            "reason": rec.get("error"), "checked_at": rec.get("checked_at"),
            "threshold_working_days": threshold, "over": over, "alerting": over and infra_up,
            "infra_up": infra_up}


def describe(v: dict[str, Any], tz: ZoneInfo | None = None) -> str:
    """One line for people: `e0a79c8 (v0.1.0), deployed 17 Oct: 383 commits and 12 days behind main`."""
    what = v["short"] + (f" ({v['tag']})" if v.get("tag") else "")
    if v.get("deployed_at"):
        what += ", deployed " + datetime.fromtimestamp(float(v["deployed_at"]), tz or UTC).strftime("%d %b")
    if v["state"] == "unknown":
        return f"{what}: commits behind main unknown ({v.get('reason') or 'not checked'})"
    if v["state"] == "current":
        return f"{what}: main's head, nothing to deploy"
    n, d = v["behind_commits"], v["days_behind"]
    return (f"{what}: {n} commit{'s' if n != 1 else ''} and {d} day{'s' if d != 1 else ''} behind main "
            f"({v['working_days_behind']} working)")


# ---------------------------------------------------------------------- doctor
async def doctor_checks(h: Any) -> list[dict[str, Any]]:
    from hangar.doctor import _check

    power = h.fleet.power
    try:
        await refresh(h)
        v = view(h.store, h.settings, power, power.clock)
    except Exception as e:  # noqa: BLE001 - a check, never a failure of doctor
        return [_check("production lag", None, f"could not check: {type(e).__name__}: {str(e)[:200]}")]
    if v is None:
        return []
    line = describe(v, power._tz(power.state()))
    if v["state"] == "unknown":
        return [_check("production lag", None, line,
                       "hangar needs a GitHub token that can read the repo (hangar setup github) and the repo "
                       "(HANGAR_DEPLOY_REPO, or a deploy record with its run URL)")]
    if v["over"]:
        return [_check("production lag", None, line + f": over {v['threshold_working_days']:g} working days",
                       "reject the stale deploy run and approve the newest (docs/runbooks/deploy.md)")]
    return [_check("production lag", True, line)]


# ---------------------------------------------------------------------- the alert (the ops loop, every minute)
async def tick(h: Any) -> None:
    fleet = getattr(h, "fleet", None)
    if fleet is None or getattr(h, "store", None) is None:
        return
    await refresh(h)
    v = view(h.store, h.settings, fleet.power, fleet.power.clock)
    if metrics.M is not None:
        known = v is not None and v["behind_commits"] is not None
        metrics.M.deploy_lag_commits.set(v["behind_commits"] if known else -1)
        metrics.M.deploy_lag_working_days.set(v["working_days_behind"] if known else -1)
        metrics.M.deploy_lag_over.set(1 if v is not None and v["alerting"] else 0)
    if v is not None and v["alerting"]:
        slolog.emit("deploy_lag", commits=v["behind_commits"], working_days=v["working_days_behind"],
                    days=v["days_behind"])


def register() -> None:
    from hangar import opstatus

    if tick not in opstatus.MINUTELY:
        opstatus.MINUTELY.append(tick)
