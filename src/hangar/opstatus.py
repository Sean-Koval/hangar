"""The operator console (--- clouddoctor): `hangar status`, `GET /api/status` and the `hangar://status` resource.

One admin-only view of what an operator otherwise needs kubectl for (observability review P3, merged into one
console in round 2), as a resource rather than a tool:

* power: the sandbox pools' state, and the last deploy (power.py);
* loops: each background loop's last pass and failures (metrics.loop_tick);
* harnesses: the locked version of each agent CLI and the version each provider's pack installed;
* builds in progress: environment images, harness packs, repo builds;
* failures in the last 24 hours by cause, transient or not (flightrec.py), and egress denies by host;
* requests agents filed that wait for an answer;
* SLOs: create p50/p95 warm, cold and on a build path, turn start, resume hot and cold (slo.py);
* credentials: health, what expires soon, and pools (credhealth.py);
* the nightly pass, and the last warnings hangar logged: its own loggers only (never `uvicorn.access`, whose
  lines carry request paths), each line through `broker.redact` before it is shown.

The ops loop (`Ops`) also runs here: every minute it saves the SLO samples and beats the `ops` heartbeat, every hour
it deletes expired flight records, and once a night (HANGAR_DOCTOR_SCHEDULE) it runs the nightly pass: the
credential checks, and whatever else registers in NIGHTLY (the Cloud Doctor).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from hangar import credhealth, flightrec, metrics, slo
from hangar.models import now

log = logging.getLogger("hangar.opstatus")

RING = 200  # warnings kept
LOG_SHOWN = 50
TICK = 60.0
PRUNE_EVERY = 3600.0
NIGHTLY_KEY = "ops:nightly"
SLOT_KEY = "ops:nightly:slot"
SECTIONS = ("power", "loops", "harnesses", "builds", "failures", "egress", "requests", "slo", "credentials",
            "nightly", "log", "models")  # --- integ3: + models (review #105)
# More of the nightly pass: fn(h, by) -> a JSON-able summary. The credential checks are the first.
NIGHTLY: list[tuple[str, Callable[[Any, str], Awaitable[Any]]]] = []
# More of every minute's pass: fn(h) (the inbox digests, #158). Runs while the sandbox pools are powered down too.
MINUTELY: list[Callable[[Any], Awaitable[Any]]] = []
# More status sections: name -> fn(fleet, sessions, jobs) -> JSON-able (the Cloud Doctor's).
EXTRA: dict[str, Callable[[Any, Any, Any], Any]] = {}


# ====================================================================== the log ring
class LogRing(logging.Handler):
    """The last RING warnings and errors of hangar's own loggers, as plain records (redacted when read)."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: collections.deque[dict[str, Any]] = collections.deque(maxlen=RING)

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("hangar") or record.name.startswith("uvicorn"):
            return  # attached to "hangar", so only its children reach here; uvicorn.access never does
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not break logging
            msg = str(record.msg)
        line: dict[str, Any] = {"at": record.created, "level": record.levelname, "logger": record.name,
                                "message": msg[:1000]}
        if record.exc_info and record.exc_info[1] is not None:
            line["error"] = f"{type(record.exc_info[1]).__name__}: {str(record.exc_info[1])[:300]}"
        self.lines.append(line)


RING_HANDLER = LogRing()


def install_ring() -> None:
    lg = logging.getLogger("hangar")
    if RING_HANDLER not in lg.handlers:
        lg.addHandler(RING_HANDLER)


def recent_log(redact: Callable[[Any], Any], n: int = LOG_SHOWN) -> list[dict[str, Any]]:
    """Newest first: stored values out (`redact`), and token formats too (--- review: a token hangar never stored,
    like the bootstrap admin token it logs on first boot or an App installation token in a clone error)."""
    from hangar import secretscan

    def mask(v: Any) -> Any:
        return secretscan.mask(v)[0] if isinstance(v, str) else v

    return [redact({k: mask(v) for k, v in x.items()}) for x in list(RING_HANDLER.lines)[-n:]][::-1]


# ====================================================================== sections
def _harnesses(fleet: Any) -> dict[str, Any]:
    from hangar.harnesses import HARNESSES

    out = {}
    for name, h in HARNESSES.items():
        if not h.agent:
            continue
        prov = {p: fleet.harness_version(p, name) for p in fleet.providers}
        out[name] = {"locked": h.version, "providers": prov, "deprecated": h.deprecated,
                     "behind": sorted(p for p, v in prov.items() if v and v != h.version)}
    return out


def _builds(fleet: Any) -> dict[str, Any]:
    from hangar import envbuilds

    t = now()
    images = [{**{k: v for k, v in j.summary().items() if k != "log_tail"}, "seconds": round(t - j.started_at)}
              for j in list(fleet.builds.values()) if j.state == "running"]
    packs = sorted(k for k, task in list(getattr(fleet, "_pack_builds", {}).items()) if not task.done())
    repos = []
    eb = envbuilds.of(fleet)
    for key, task in list(eb._tasks.items()):
        if task.done():
            continue
        with contextlib.suppress(Exception):
            head = envbuilds.Head.model_validate_json(fleet.store.kv_get(key) or "{}")
            b = eb.get(head.ids[0])
            repos.append({"id": b.id, "version": b.version, "repo": b.repo, "environment": b.environment,
                          "provider": b.provider, "state": b.state, "stage": b.stage,
                          "seconds": round(t - (b.started_at or b.created_at))})
    return {"images": images, "packs": packs, "repos": repos}


def _failures(fleet: Any, t: float) -> dict[str, Any]:
    recs = flightrec.of(fleet).list(t - 86400, limit=2000)
    return {"hours": 24, "total": len(recs), "transient": sum(r.transient for r in recs),
            "by_cause": flightrec.by_cause(recs), "recent": [flightrec.brief(r) for r in recs[:10]]}


def _egress(fleet: Any, t: float) -> dict[str, Any]:
    rows = fleet.store.audit_since(t - 86400, ("egress.deny",), limit=5000)
    hosts: dict[str, dict[str, Any]] = {}
    for e in rows:
        d = e.detail
        h = hosts.setdefault(str(d.get("host")), {"host": d.get("host"), "count": 0, "environments": [],
                                                    "sandboxes": set(), "last_at": e.ts})
        h["count"] += 1 + int(d.get("suppressed_before") or 0)
        h["sandboxes"].add(e.target)
        if (env := d.get("environment")) and env not in h["environments"]:
            h["environments"].append(env)
    top = sorted(hosts.values(), key=lambda h: -h["count"])[:20]
    return {"hours": 24, "total": sum(h["count"] for h in hosts.values()),
            "by_host": [{**h, "sandboxes": len(h["sandboxes"])} for h in top]}


def _requests(fleet: Any, t: float) -> dict[str, Any]:
    from hangar.envrequests import EnvRequest

    rows = sorted(fleet.store.list_where(EnvRequest, state="pending"), key=lambda r: r.created_at)
    return {"pending": len(rows), "oldest_seconds": round(t - rows[0].created_at) if rows else None,
            "items": [{"id": r.id, "kind": r.kind, "target": r.target, "environment": r.environment,
                       "session": r.session, "job": r.job_id, "owner": r.owner, "age_seconds": round(t - r.created_at)}
                      for r in rows[:20]]}


async def status(fleet: Any, sessions: Any = None, jobs: Any = None, sections: set[str] | None = None,
                 check_credentials: bool = False) -> dict[str, Any]:
    """The console. `sections` limits it (SECTIONS, and EXTRA's); `check_credentials` checks every credential now
    instead of only those not checked in the last hour."""
    t = now()

    def want(name: str) -> bool:
        return sections is None or name in sections

    out: dict[str, Any] = {"at": t, "version": flightrec._version()}
    if want("power") or want("loops"):
        ov = fleet.power.overview()
        if want("power"):
            out["power"], out["last_deploy"] = ov["power"], ov["last_deploy"]
        if want("loops"):
            out["loops"] = ov["loops"]
    if want("harnesses"):
        out["harnesses"] = _harnesses(fleet)
    if want("builds"):
        out["builds"] = _builds(fleet)
    if want("failures"):
        out["failures"] = await asyncio.to_thread(_failures, fleet, t)
    if want("egress"):
        out["egress_denies"] = await asyncio.to_thread(_egress, fleet, t)
    if want("requests"):
        out["requests"] = _requests(fleet, t)
    if want("slo"):
        out["slo"] = slo.SAMPLES.summary(t)
    if want("credentials"):
        await credhealth.check_all(fleet, "status", max_age=0 if check_credentials else credhealth.STATUS_MAX_AGE,
                                   budget=credhealth.STATUS_BUDGET)
        out["credentials"] = credhealth.summary(fleet.store)
    if want("nightly"):
        out["nightly"] = fleet.store.kv_get_json(NIGHTLY_KEY, None)
    if want("models"):  # --- integ3 (review #105): what jobs run on by default, and whether anything caps it
        from hangar import modelpolicy

        out["models"] = modelpolicy.spend_guard(fleet)
    for name, fn in EXTRA.items():
        if want(name):
            try:
                out[name] = fn(fleet, sessions, jobs)
            except Exception as e:  # noqa: BLE001 - one section failing leaves the rest
                out[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
    if want("log"):
        out["log"] = recent_log(fleet.broker.redact)
    return out


def overview_fields(fleet: Any) -> dict[str, Any]:
    """For fleet_overview and GET /api/overview: the day's failures by cause, and credential health in counts."""
    try:
        recs = flightrec.of(fleet).list(now() - 86400, limit=2000)
        cred = credhealth.summary(fleet.store)
    except Exception:  # noqa: BLE001 - the overview must answer whatever happens here
        log.warning("overview fields failed", exc_info=True)
        return {}
    return {"failures_24h": {c: v["count"] for c, v in flightrec.by_cause(recs).items()},
            "credential_health": {**cred["counts"], "attention": len(cred["attention"])}}


# ====================================================================== the ops loop
class Ops:
    """Every TICK: the SLO samples are saved and the `ops` heartbeat beats; every PRUNE_EVERY: expired flight records
    go; once a night (HANGAR_DOCTOR_SCHEDULE): the nightly pass."""

    def __init__(self, h: Any):
        self.h = h
        self._task: asyncio.Task | None = None
        self._nightly: asyncio.Task | None = None
        self._pruned = 0.0

    def start(self) -> None:
        install_ring()
        slo.SAMPLES.load(self.h.store)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for t in (self._task, self._nightly):
            if t is not None:
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t
        with contextlib.suppress(Exception):
            slo.SAMPLES.save(self.h.store)
        await flightrec.of(self.h.fleet).settle(5)

    def _cron(self) -> Any:
        from hangar.triggers import Cron

        expr = (self.h.settings.doctor_schedule or "").strip()
        if expr.lower() in ("", "off", "none"):
            return None
        tz = self.h.settings.doctor_time_zone or self.h.fleet.power.state().get("time_zone") or "UTC"
        return Cron(expr, tz)

    def due(self, t: float) -> float | None:
        """The nightly slot to run now, if one passed since the last run. The first look only remembers the latest
        slot: a deploy never runs it on the spot."""
        cron = self._cron()
        if cron is None:
            return None
        slot = cron.latest_at_or_before(t)
        last = self.h.store.kv_get_json(SLOT_KEY, None)
        if slot is None or (last is not None and slot <= float(last)):
            return None
        self.h.store.kv_set_json(SLOT_KEY, slot)
        return slot if last is not None else None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
                metrics.loop_tick("ops")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - next minute
                metrics.loop_failed("ops")
                log.exception("ops loop pass failed")
            await asyncio.sleep(TICK)

    async def tick(self) -> None:
        h = self.h
        slo.SAMPLES.save(h.store)
        t = time.monotonic()
        if t - self._pruned >= PRUNE_EVERY:
            self._pruned = t
            if n := await flightrec.of(h.fleet).prune(h.sessions):
                log.info("deleted %d flight record(s) older than %d days", n, flightrec.RETENTION_DAYS)
        if self.due(now()) is not None and (self._nightly is None or self._nightly.done()):
            self._nightly = asyncio.create_task(self.nightly("schedule"))
        for fn in MINUTELY:
            try:
                await fn(h)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one failing doesn't stop the others
                log.exception("ops minute step %s failed", getattr(fn, "__name__", fn))

    async def nightly(self, by: str) -> dict[str, Any]:
        """The credential checks, then each NIGHTLY step, one after another; one failing doesn't stop the rest."""
        started = now()
        steps: dict[str, Any] = {}
        for name, fn in [("credentials", _check_credentials), *NIGHTLY]:
            try:
                steps[name] = await fn(self.h, by)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("nightly step %s failed", name)
                steps[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
        rec = {"at": started, "finished_at": now(), "by": by, "steps": steps}
        self.h.store.kv_set_json(NIGHTLY_KEY, rec)
        return rec


async def _check_credentials(h: Any, by: str) -> dict[str, Any]:
    res = await credhealth.check_all(h.fleet, f"nightly:{by}")
    counts: dict[str, int] = {}
    for r in res:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"checked": len(res), **counts}


# ====================================================================== front doors
def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/status (admins): the console, or `section=a,b` of it."""

    @app.get("/api/status")
    async def status_route(section: str | None = None, check_credentials: bool = False, _: str = admin):
        want = {s.strip() for s in section.split(",") if s.strip()} if section else None
        return await status(h.fleet, h.sessions, h.jobs, want, check_credentials)


def register(mcp: Any, fleet: Any, sessions: Any, jobs: Any) -> None:
    """`hangar://status`: admins only (a member's token gets refused, as for an admin tool)."""
    from hangar import identity

    @mcp.resource("hangar://status", name="status", mime_type="application/json",
                  description="Admins: power, loop heartbeats, harness versions, builds running, the day's "
                              "failures by cause, egress denies, waiting requests, SLOs, credential health and "
                              "recent warnings.")
    async def status_resource() -> str:
        from mcp.server.mcpserver.exceptions import ResourceError

        try:
            identity.require("admin", "hangar://status")
        except identity.Forbidden as e:  # said to the client as it is, not as "error reading resource"
            raise ResourceError(str(e)) from None
        return json.dumps(await status(fleet, sessions, jobs), indent=1, default=str)
