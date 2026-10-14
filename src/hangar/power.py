"""The sandbox node pools power down at night; hangar stays up (owner's decision 1, docs/gke.md section 6).

`deploy/gke/power.sh` tells hangar what it is doing, through POST /api/admin/power/state:
- `down` (01:00 ET, before the pools go): creates on the powered providers answer at once, saying when sandboxes
  are back, instead of waiting for a node that won't come. Jobs from every front door (GitHub, Slack, Jira, GitHub
  Projects, schedules, MCP) are accepted and wait in the queue with that status. A turn the power-down cuts off
  resumes from its checkpoint afterwards, like one a restart cut off (restarts.py).
- `starting` (09:50): the pools are coming back; the same answers, with "about N minutes".
- `up` (once the floor node is ready and the images are pulled onto it): held jobs go back to the queue a few at a
  time, and each environment's warm pool is sized for the morning from last week's creates in that hour.
A `down` or `starting` that should have ended an hour ago counts as up: a power job that failed to report must not
leave hangar refusing work. `hangar doctor` says so.

`hangar power up` (POST /api/admin/power/up) runs the power-up Cloud Run job early. Deploys (deploy.yml) record
their result here (POST /api/admin/deploys), and doctor and the overview show it with the loops' heartbeats.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hangar import metrics
from hangar.config import Settings
from hangar.models import JobState, Sandbox
from hangar.store import Store

if TYPE_CHECKING:
    from hangar.fleet import Fleet
    from hangar.jobs import JobService
    from hangar.models import Job

log = logging.getLogger(__name__)

STATE_KEY = "power:state"
DEPLOYS_KEY = "ops:deploys"
STATES = ("up", "starting", "down")
GRACE = 3600.0  # a down or starting state this long past its ready time counts as up
WAITING = "powered_down"  # a held job's reason
RECHECK = 60.0  # seconds between a held job's own looks at the power state
LOOKBACK_DAYS = 7
MAX_DEPLOYS = 20
DEPLOY_RESULTS = ("passed", "failed", "rolled_back")
PREPULL = "hangar-prepull"
RUN_API = "https://run.googleapis.com/v2"
METADATA = "http://metadata.google.internal/computeMetadata/v1"
# Background loops and how often each passes (metrics.loop_tick); one silent for 3 intervals and a minute is stuck.
LOOPS: dict[str, str | float] = {"reaper": "reaper_interval_seconds", "reconciler": "reconcile_interval_seconds",
                                 "sessions": 60.0, "schedules": "schedule_poll_seconds"}

POWERED_DOWN = (
    "The sandbox you were working in was shut down when hangar's sandbox nodes powered down for the night, and it "
    "was restored from its last checkpoint: the repository and this conversation are as they were then, so anything "
    "after it is missing. Check where the work stands (`git status`, `git log`, the files you were changing), redo "
    "what is missing, and carry on until the task is done."
)


class Power:
    """The power state of the sandbox pools, the jobs waiting for them, and the warm pool sizes for the day."""

    def __init__(self, store: Store, settings: Settings, clock=time.time):
        self.store = store
        self.settings = settings
        self.clock = clock
        self.started = clock()
        self.held: list[str] = []  # queued jobs a worker set aside while the pools are off
        st = store.kv_get_json(STATE_KEY, None) or {}
        self._warm: dict[str, int] = dict(st.get("warm") or {}) if st.get("state") == "up" else {}

    # ------------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        st = dict(self.store.kv_get_json(STATE_KEY, None) or {"state": "up"})
        ready = st.get("ready_at")
        # with no ready time (a `down` from a laptop that never set the schedule), a day
        limit = float(ready) + GRACE if ready is not None else float(st.get("since") or 0) + 86400
        if st.get("state") != "up" and self.clock() > limit:
            st["stale"] = True
        return st

    @property
    def off(self) -> bool:
        """True while sandboxes on the powered providers can't start (down or starting, and not stale)."""
        st = self.state()
        return st.get("state") != "up" and not st.get("stale")

    def affects(self, provider: str | None) -> bool:
        return (provider or self.settings.default_provider) in self.settings.power_providers

    def refusal(self, provider: str | None) -> str | None:
        return self.message() if self.off and self.affects(provider) else None

    def check(self, provider: str | None) -> None:
        """Refuse a create on a powered-down provider, at once and with when it's back."""
        if (why := self.refusal(provider)) is not None:
            from hangar.fleet import PoweredDown

            raise PoweredDown(why)

    def _tz(self, st: dict[str, Any]) -> ZoneInfo:
        try:
            return ZoneInfo(st.get("time_zone") or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def when(self, st: dict[str, Any] | None = None) -> str | None:
        """'10:00 EDT (in 8 h 12 min)', or None when nobody said when."""
        st = st or self.state()
        if st.get("ready_at") is None:
            return None
        ready = float(st["ready_at"])
        at = datetime.fromtimestamp(ready, self._tz(st)).strftime("%H:%M %Z")
        left = max(0, int(ready - self.clock()) // 60)
        span = f"{left // 60} h {left % 60} min" if left >= 60 else f"{left} min"
        return f"{at} (in {span})" if left else f"{at} (any minute now)"

    def message(self) -> str:
        st = self.state()
        when = self.when(st)
        if st.get("state") == "starting":
            return ("Sandboxes are powering on now" + (f" and are ready at about {when}" if when else "") +
                    ". Try again then; jobs submitted now are queued and start once they're on.")
        return ("Sandboxes are powered down for the night" + (f" and power on at {when}" if when else "") +
                ". Jobs submitted now are queued and start then; an admin can power them on early with "
                "`hangar power up`.")

    def status_line(self) -> str:
        """The status a held job shows."""
        when = self.when()
        if self.state().get("state") == "starting":
            return "sandboxes are powering on" + (f"; ready at about {when}" if when else "")
        return "sandboxes power on at " + when if when else "the sandbox pools are powered down"

    def mark(self, state: str, by: str, *, up_cron: str | None = None, time_zone: str | None = None,
             warm_minutes: float | None = None, ready_at: float | None = None,
             prepull: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record what power.sh (or `hangar power up`) is doing. Returns the new state."""
        if state not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        prev = self.state()
        t = self.clock()
        tz = time_zone or prev.get("time_zone") or "UTC"
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown time zone '{tz}' (an IANA name like America/New_York)") from e
        cron = up_cron or prev.get("up_cron")
        warm_s = 60 * max(0.0, float(warm_minutes if warm_minutes is not None else prev.get("warm_minutes", 10)))
        if ready_at is None and state == "down" and cron:
            from hangar.triggers import Cron, CronError

            try:
                nxt = Cron(cron, tz).next_after(t)
            except CronError as e:
                raise ValueError(str(e)) from e
            ready_at = nxt + warm_s if nxt is not None else None
        elif ready_at is None and state == "starting":
            ready_at = t + (warm_s or 600)
        st: dict[str, Any] = {"state": state, "since": t, "by": by, "time_zone": tz, "up_cron": cron,
                              "warm_minutes": warm_s / 60, "ready_at": ready_at if state != "up" else None,
                              "last_down": prev.get("last_down"), "last_up": prev.get("last_up"),
                              "warm": self._warm if state == "up" else {}}
        if state == "down":
            st["last_down"] = t
        if state == "up":  # ready_at: when people were told to expect sandboxes (the warm pools' hour)
            st["last_up"] = {"at": t, "by": by, "prepull": _prepull_summary(prepull),
                             "ready_at": prev.get("ready_at") if prev.get("state") != "up" else None}
        self.store.kv_set_json(STATE_KEY, st)
        return self.state()

    # ------------------------------------------------------------------ jobs
    def hold(self, svc: JobService, job_id: str) -> bool:
        """In a job worker: True when a queued job must wait for the pools (it keeps its place in the store)."""
        job = svc._jobs.get(job_id)
        if job is None or job.state != JobState.QUEUED:
            return False
        if not self.off or not self.affects(job.provider) or job.cancel_requested:
            if job.reason == WAITING:  # held before a restart; it runs now
                job.reason = None
            return False
        self._hold(svc, job_id)
        if job.reason != WAITING:
            svc._event(job, f"waiting: {self.status_line()}", reason=WAITING)
        return True

    def _hold(self, svc: JobService, job_id: str) -> None:
        """Set a job aside until release(). It also looks again every RECHECK seconds, so a power-up that is never
        reported (the state goes stale) or a cancel doesn't leave it waiting."""
        if job_id in self.held:
            return
        self.held.append(job_id)
        with contextlib.suppress(RuntimeError):  # no loop: tests and tools that only read the state
            asyncio.get_running_loop().call_later(RECHECK, self._recheck, svc, job_id)

    def _recheck(self, svc: JobService, job_id: str) -> None:
        if job_id not in self.held:
            return  # released
        job = svc._jobs.get(job_id)
        if self.off and job is not None and job.state == JobState.QUEUED and not job.cancel_requested:
            asyncio.get_running_loop().call_later(RECHECK, self._recheck, svc, job_id)
            return
        self.held.remove(job_id)
        if job is not None and job.state == JobState.QUEUED:
            svc._queue.put_nowait(job_id)  # the worker runs it, or cancels it

    def release(self, svc: JobService) -> list[str]:
        """The pools are up: the held jobs go back to the queue, HANGAR_JOB_RESTART_STAGGER_SECONDS apart."""
        from hangar import restarts

        held, self.held = self.held, []
        ids = []
        for jid in held:
            job = svc._jobs.get(jid)
            if job is None or job.state != JobState.QUEUED:
                continue
            if job.reason == WAITING:
                svc._event(job, "sandboxes are on; starting", reason=None)
            ids.append(jid)
        restarts.release(svc, ids)
        return ids

    async def requeue(self, svc: JobService, job: Job) -> None:
        """A job whose create was refused because the pools went down between the worker's check and the create:
        it waits like the others. One that never got a sandbox provisions again from the start."""
        s = svc._session(job)
        fresh = all(t.run_id is None for t in job.turns) and (s is None or not s.checkpoints)
        if job.state == JobState.PROVISIONING and fresh:
            await svc._discard_sandbox(job)
            job.session_id = None
        svc._event(job, f"waiting: {self.status_line()}", JobState.QUEUED, reason=WAITING)
        self._hold(svc, job.id)
        if not self.off:  # it came up meanwhile
            self.release(svc)

    def resume_cut_off(self, svc: JobService, job: Job) -> bool:
        """A turn whose sandbox went away while the pools are off: resume it from its checkpoint after power-up
        (at most HANGAR_JOB_RESTART_RESUMES times), instead of failing the job."""
        from hangar import restarts

        if not self.off or not self.affects(job.provider):
            return False
        why = "the sandbox pools powered down"
        if not restarts.resume(svc, job, why=why, message=POWERED_DOWN):
            return False
        cut = [t for t in job.turns if t.run_id and t.outcome in ("gone", "error")]
        if cut and not cut[-1].outcome_reason:
            cut[-1].outcome_reason = why
        svc._event(job, f"waiting: {self.status_line()}", reason=WAITING)
        self._hold(svc, job.id)
        return True

    # ------------------------------------------------------------------ warm pools
    def warm_for(self, env: str) -> int:
        return self._warm.get(env, 0)

    def warm_plan(self, sandboxes: list[Sandbox], envs: list[str], ready_at: float, tz: ZoneInfo) -> dict[str, int]:
        """Warm pool sizes for the day: HANGAR_POWER_WARM_POOLS `auto` takes, for each environment, the most of its
        sandboxes created in the hour after `ready_at` on any of the last 7 days (forks excluded, at most
        HANGAR_POWER_WARM_MAX); `env=N,env2=M` sets them; `off` none."""
        mode = (self.settings.power_warm_pools or "off").strip()
        known = set(envs)
        if mode.lower() in ("off", "0", "none", ""):
            return {}
        if mode.lower() != "auto":
            out: dict[str, int] = {}
            for part in filter(None, (p.strip() for p in mode.split(","))):
                name, _, n = part.partition("=")
                if name.strip() in known and n.strip().isdigit() and int(n) > 0:
                    out[name.strip()] = min(int(n), 50)
            return out
        start = datetime.fromtimestamp(ready_at, tz)
        windows = [((start - timedelta(days=d)).timestamp(), (start - timedelta(days=d, hours=-1)).timestamp())
                   for d in range(1, LOOKBACK_DAYS + 1)]
        counts: dict[str, list[int]] = {}
        for sb in sandboxes:
            if sb.parent or sb.environment not in known or not self.affects(sb.provider):
                continue
            for i, (a, b) in enumerate(windows):
                if a <= sb.created_at < b:
                    counts.setdefault(sb.environment, [0] * LOOKBACK_DAYS)[i] += 1
        cap = max(0, self.settings.power_warm_max)
        return {e: min(max(c), cap) for e, c in counts.items() if min(max(c), cap) > 0}

    async def size_warm_pools(self, fleet: Fleet) -> dict[str, int]:
        """At power-up: size each environment's warm pool for the morning and sync it, so the first creates claim a
        pod that is already up. Environments sized yesterday go back to their own setting."""
        st = self.state()
        ready = (st.get("last_up") or {}).get("ready_at") or self.clock()
        sandboxes = await asyncio.to_thread(self.store.list, Sandbox)
        sizes = self.warm_plan(sandboxes, fleet.catalog.names(), float(ready), self._tz(st))
        before, self._warm = self._warm, sizes
        cur = self.store.kv_get_json(STATE_KEY, None) or {}
        if cur.get("state") == "up":
            cur["warm"] = sizes
            self.store.kv_set_json(STATE_KEY, cur)
        await self._sync(fleet, set(sizes) | set(before))
        return sizes

    async def clear_warm_pools(self, fleet: Fleet) -> None:
        """At power-down: the day's sizes end; those pools go back to each environment's own setting."""
        before, self._warm = self._warm, {}
        await self._sync(fleet, set(before))

    async def _sync(self, fleet: Fleet, envs: set[str]) -> None:
        for name in sorted(envs):
            try:
                spec = fleet.catalog.get(name)
            except Exception:  # noqa: BLE001 - an environment deleted since
                continue
            for pname, prov in fleet.providers.items():
                image = self.store.kv_get(f"image:{pname}:{name}")
                if not image or not self.affects(pname) or not prov.capabilities.warm_pools:
                    continue
                try:
                    await fleet._resolve_packs(spec, pname, "power", lazy=True)  # never waits on a build
                    await prov.sync_warm_pool(fleet._warm(spec), image)
                except Exception:  # noqa: BLE001 - the others still get theirs
                    log.exception("warm pool %s on %s: sync failed", name, pname)

    # ------------------------------------------------------------------ early power-up
    async def request_up(self, by: str, transport: Any = None) -> dict[str, Any]:
        """Run the power-up Cloud Run job now (HANGAR_POWER_UP_JOB), as hangar's own service account."""
        import httpx

        job = (self.settings.power_up_job or "").strip()
        if not self.off:
            return {**self.state(), "message": "Sandboxes are already on."}
        if self.state().get("state") == "starting":
            return {**self.state(), "message": self.message()}
        if not re.fullmatch(r"projects/[^/]+/locations/[^/]+/jobs/[^/]+", job):
            raise PowerError("no power-up job is configured: set HANGAR_POWER_UP_JOB to the job's name (terraform "
                             "output power_up_job, `projects/P/locations/R/jobs/hangar-power-up`), or run "
                             "`bash deploy/gke/power.sh up` where kubectl reaches the cluster")
        async with httpx.AsyncClient(transport=transport, timeout=20) as c:
            try:
                r = await c.get(f"{METADATA}/instance/service-accounts/default/token",
                                headers={"Metadata-Flavor": "Google"})
                token = r.json()["access_token"] if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError, KeyError):
                token = None
            if not token:
                raise PowerError("can't get a Google Cloud token from the metadata server: `hangar power up` works "
                                 "where hangar runs on GKE with Workload Identity")
            try:
                r = await c.post(f"{RUN_API}/{job}:run", json={}, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise PowerError(f"couldn't reach Cloud Run: {e}") from e
        if r.status_code >= 400:
            detail = ""
            with contextlib.suppress(ValueError, KeyError, TypeError, AttributeError):
                detail = r.json()["error"]["message"]
            hint = (" (hangar's service account needs roles/run.invoker on the job: terraform apply in "
                    "deploy/terraform/gke grants it)") if r.status_code == 403 else ""
            raise PowerError(f"Cloud Run refused to run {job}: HTTP {r.status_code} {detail[:300]}{hint}")
        execution = None
        with contextlib.suppress(ValueError, KeyError, TypeError, AttributeError):
            execution = r.json().get("metadata", {}).get("name") or r.json().get("name")
        st = self.mark("starting", by)
        return {**st, "execution": execution, "message": self.message()}

    # ------------------------------------------------------------------ operator view
    def overview(self) -> dict[str, Any]:
        """For fleet_overview, GET /api/overview and `hangar power`: power, the last deploy, the loops."""
        st = self.state()
        return {"power": {"state": "up" if st.get("stale") else st.get("state", "up"), "stale": bool(st.get("stale")),
                          "since": st.get("since"), "ready_at": st.get("ready_at"), "when": self.when(st),
                          "jobs_waiting": len(self.held), "warm_pools": dict(self._warm),
                          "last_up": st.get("last_up"), "last_down": st.get("last_down")},
                "last_deploy": (deploys(self.store) or [None])[0],
                "loops": loops(self.settings, self.started, self.clock())}


class PowerError(RuntimeError):
    pass


def _prepull_summary(p: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(p, dict):
        return None
    out: dict[str, Any] = {}
    for k in ("images", "seconds", "nodes"):
        with contextlib.suppress(TypeError, ValueError):
            if p.get(k) is not None:
                out[k] = round(float(p[k]), 1) if k == "seconds" else int(p[k])
    if isinstance(p.get("ready"), bool):
        out["ready"] = p["ready"]
    return out


# ---------------------------------------------------------------------- the prepull DaemonSet
def prepull_manifest(settings: Settings, refs: list[str], namespace: str = "hangar") -> dict[str, Any]:
    """A DaemonSet on the sandbox nodes that mounts every image as an image volume, so each node pulls them all
    into the runtime's image store as it joins, and the running pod keeps them from image GC. Nothing in the
    images runs: the one container is a pause image. Same runtime class, selector and tolerations as sandboxes."""
    pod: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "terminationGracePeriodSeconds": 1,
        "securityContext": {"runAsUser": 65535, "runAsGroup": 65535, "runAsNonRoot": True},
        "containers": [{
            "name": "hold",
            "image": settings.power_prepull_image,
            "securityContext": {"capabilities": {"drop": ["ALL"]}, "allowPrivilegeEscalation": False,
                                "privileged": False, "readOnlyRootFilesystem": True},
            "resources": {"requests": {"cpu": "1m", "memory": "8Mi"}, "limits": {"memory": "32Mi"}},
            "volumeMounts": [{"name": f"i{n}", "mountPath": f"/prepull/{n}", "readOnly": True}
                             for n in range(len(refs))],
        }],
        "volumes": [{"name": f"i{n}", "image": {"reference": ref, "pullPolicy": "IfNotPresent"}}
                    for n, ref in enumerate(refs)],
    }
    rc = settings.k8s_runtime_class
    if rc:
        pod["runtimeClassName"] = rc
        if rc == "gvisor" and settings.k8s_gke_node_selector:
            pod["nodeSelector"] = {"sandbox.gke.io/runtime": "gvisor"}
            pod["tolerations"] = [{"key": "sandbox.gke.io/runtime", "operator": "Equal", "value": "gvisor",
                                   "effect": "NoSchedule"}]
        elif rc.startswith("kata") and settings.k8s_gke_node_selector:
            pod["nodeSelector"] = {"hangar.dev/runtime": "kata"}
            pod["tolerations"] = [{"key": "hangar.dev/runtime", "operator": "Equal", "value": "kata",
                                   "effect": "NoSchedule"}]
    labels = {"app": PREPULL, "app.kubernetes.io/managed-by": "hangar"}
    return {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": PREPULL, "namespace": namespace, "labels": labels},
        "spec": {
            "selector": {"matchLabels": {"app": PREPULL}},
            # every node pulls the new list at once; nothing waits on these pods
            "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "100%"}},
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    }


async def prepull_images(fleet: Fleet) -> dict[str, Any]:
    """What to pull onto the sandbox nodes: each image and pack `hangar images plan` lists that the registry has (by
    digest), and environments' own prebuilt `image:`s. Images the registry lacks are listed, not pulled."""
    from hangar import images

    s = fleet.settings
    if "kubernetes" not in fleet.providers:
        raise PowerError("prepull is for the kubernetes provider, which this hangar doesn't run")
    items = images.plan(s, fleet.catalog, "kubernetes")
    await images.check(items, "kubernetes")
    refs = [i.ref for i in items if i.status == "present" and i.ref]
    for name in fleet.catalog.names():
        with contextlib.suppress(Exception):
            if (img := fleet.catalog.get(name).image) and img not in refs:
                refs.append(img)
    return {"images": refs, "missing": [i.tag or i.name for i in items if i.status not in ("present",)]}


# ---------------------------------------------------------------------- deploys and loops
def record_deploy(store: Store, body: dict[str, Any], by: str, clock=time.time) -> dict[str, Any]:
    """A deploy.yml run's outcome, newest first, the last MAX_DEPLOYS kept."""
    result = str(body.get("result", ""))
    if result not in DEPLOY_RESULTS:
        raise ValueError(f"result must be one of {', '.join(DEPLOY_RESULTS)}")
    rec: dict[str, Any] = {"at": clock(), "by": by, "result": result}
    for k, limit in (("image", 300), ("sha", 64), ("version", 120), ("previous_image", 300), ("run_url", 300),
                     ("summary", 1000)):
        if isinstance(body.get(k), str) and body[k].strip():
            rec[k] = body[k].strip()[:limit]
    if isinstance(body.get("checks"), dict):
        rec["checks"] = {str(k)[:40]: str(v)[:10] for k, v in list(body["checks"].items())[:40]}
    if rec.get("run_url") and not rec["run_url"].startswith("https://"):
        rec.pop("run_url")
    store.kv_set_json(DEPLOYS_KEY, [rec, *deploys(store)][:MAX_DEPLOYS])
    return rec


def deploys(store: Store) -> list[dict[str, Any]]:
    got = store.kv_get_json(DEPLOYS_KEY, None)
    return got if isinstance(got, list) else []


def loops(settings: Settings, started: float, t: float) -> dict[str, dict[str, Any]]:
    """Each background loop's last pass (metrics.loop_tick), and whether it's overdue."""
    out = {}
    for name, every in LOOPS.items():
        interval = float(getattr(settings, every) if isinstance(every, str) else every)
        last = metrics.TICKS.get(name)
        limit = 3 * interval + 60
        age = t - (last if last is not None else started)
        out[name] = {"last": last, "every_seconds": interval, "ok": age <= limit,
                     "failures": metrics.FAILS.get(name, 0)}
    return out


def _ago(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s} s" if s < 120 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h"


def doctor_checks(h: Any) -> list[dict[str, Any]]:
    from hangar.doctor import _check

    p: Power = h.fleet.power
    t = p.clock()
    st = p.state()
    out = []
    if st.get("stale"):
        out.append(_check("sandbox power", None,
                          f"recorded {st['state']} since {_ago(t - float(st.get('since') or t))} ago, but the pools "
                          "should have been on an hour ago: power.sh never said `up`. Hangar treats them as on.",
                          "check the power-up job's last run: gcloud run jobs executions list --job hangar-power-up "
                          "--region <region>; then `hangar power up`"))
    elif st.get("state") != "up":
        out.append(_check("sandbox power", True, f"{p.message()} {len(p.held)} job(s) waiting."))
    else:
        last = st.get("last_up") or {}
        pre = last.get("prepull") or {}
        bits = [f"on since {_ago(t - float(last['at']))} ago" if last.get("at") else "on"]
        if pre.get("images") is not None:
            bits.append(f"{pre['images']} images pulled onto {pre.get('nodes', '?')} node(s) in {pre.get('seconds')} s"
                        + ("" if pre.get("ready", True) else " (not finished when it reported)"))
        if p._warm:
            bits.append("warm pools " + ", ".join(f"{k}={v}" for k, v in sorted(p._warm.items())))
        out.append(_check("sandbox power", True, "; ".join(bits)))
    if last_deploy := (deploys(h.store) or [None])[0]:
        what = f"{last_deploy.get('version') or last_deploy.get('image', '?')}"
        when = f"{_ago(t - float(last_deploy['at']))} ago"
        url = f" ({last_deploy['run_url']})" if last_deploy.get("run_url") else ""
        if last_deploy["result"] == "passed":
            out.append(_check("last deploy", True, f"{what}, {when}: acceptance passed{url}"))
        else:
            out.append(_check("last deploy", None, f"{what}, {when}: {last_deploy['result'].replace('_', ' ')}"
                              f"{url}" + (f": {last_deploy['summary']}" if last_deploy.get("summary") else ""),
                              "read the run's summary; the previous image is running again"
                              if last_deploy["result"] == "rolled_back" else "read the run's summary"))
    lp = loops(h.settings, p.started, t)
    late = [(f"{n} last ran {_ago(t - v['last'])} ago" if v["last"] else f"{n} hasn't run since start")
            + f" (every {_ago(v['every_seconds'])})" for n, v in lp.items() if not v["ok"]]
    failing = [f"{n} ({v['failures']} failed passes)" for n, v in lp.items() if v["failures"]]
    if late:
        out.append(_check("background loops", None, "; ".join(late),
                          "see hangar's log for the loop's errors (`kubectl -n hangar logs deploy/hangar`); a "
                          "restart starts them again"))
    else:
        out.append(_check("background loops", True, "all passing on time" +
                          (f"; failures since start: {', '.join(failing)}" if failing else "")))
    return out


# ---------------------------------------------------------------------- REST
def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/admin/power; POST /api/admin/power/state (power.sh); POST /api/admin/power/up (`hangar power up`);
    GET /api/admin/power/prepull (power.sh); POST and GET /api/admin/deploys (deploy.yml)."""
    from fastapi import Body, HTTPException, Request
    from fastapi.responses import JSONResponse

    from hangar.fleet import PoweredDown

    p: Power = h.fleet.power

    @app.exception_handler(PoweredDown)
    async def _powered_down(_: Request, exc: PoweredDown):
        st = p.state()
        retry = max(60, int(float(st.get("ready_at") or 0) - p.clock())) if st.get("ready_at") else 600
        return JSONResponse({"error": str(exc), "power": p.overview()["power"]}, status_code=503,
                            headers={"Retry-After": str(retry)})

    @app.get("/api/admin/power")
    async def power_status(_: str = admin):
        return p.overview()

    @app.post("/api/admin/power/state")
    async def power_state(body: dict[str, Any] = Body(...), who: str = admin):
        state = body.get("state")
        try:
            warm = float(body["warm_minutes"]) if body.get("warm_minutes") is not None else None
            st = p.mark(str(state), who, up_cron=body.get("up_cron") or None, time_zone=body.get("time_zone") or None,
                        warm_minutes=warm, prepull=body.get("prepull"))
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        h.fleet.audit(who, f"power.{state}", None, ready_at=st.get("ready_at"))
        out: dict[str, Any] = {"state": st, "message": p.message() if p.off else "sandboxes are on"}
        if state == "up":
            out["released_jobs"] = p.release(h.jobs)
            out["warm_pools"] = await p.size_warm_pools(h.fleet)
        elif state == "down":
            await p.clear_warm_pools(h.fleet)
        return out

    @app.post("/api/admin/power/up")
    async def power_up(who: str = admin):
        try:
            out = await p.request_up(who, h.jobs.transport)
        except PowerError as e:
            raise HTTPException(409, str(e)) from e
        h.fleet.audit(who, "power.up_requested", None, execution=out.get("execution"))
        return out

    @app.get("/api/admin/power/prepull")
    async def power_prepull(namespace: str = "hangar", _: str = admin):
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", namespace):
            raise HTTPException(400, "namespace: a Kubernetes namespace name")
        try:
            found = await prepull_images(h.fleet)
        except (PowerError, ValueError) as e:
            raise HTTPException(409, str(e)) from e
        return {**found, "manifest": prepull_manifest(h.settings, found["images"], namespace)}

    @app.post("/api/admin/deploys")
    async def deploy_record(body: dict[str, Any] = Body(...), who: str = admin):
        try:
            rec = record_deploy(h.store, body, who)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        h.fleet.audit(who, "deploy.recorded", None, result=rec["result"], image=rec.get("image"))
        return rec

    @app.get("/api/admin/deploys")
    async def deploy_list(_: str = admin):
        return deploys(h.store)
