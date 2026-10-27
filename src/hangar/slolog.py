"""The SLO signals (KAN-144; docs/slos.md): one log line per event, and the same as Prometheus metrics.

Sean's cluster has no Prometheus server, so alerts that page him come from Google Cloud Monitoring's log-based
metrics on hangar's container log. Each signal is one INFO line on the `hangar.slo` logger, in a fixed `key=value`
shape the metrics in deploy/terraform/gke/alerting.tf match and extract from:

    hangar.slo: event=job_start result=ok queued_s=4.2 held=no reason=-
    hangar.slo: event=job_start result=error queued_s=31.0 held=no reason=error
    hangar.slo: event=sandbox_create result=error provider=kubernetes environment=base seconds=12.5 error=FleetError
    hangar.slo: event=fail_closed kind=hangar.tool.call failure=unreachable
    hangar.slo: event=jobs_stuck n=1 oldest_s=4210 states=queued

Installs that scrape /metrics get the same signals as hangar_job_starts_total, hangar_job_queue_seconds,
hangar_sandbox_create_seconds_count{result}, hangar_governance_fail_closed_total and hangar_jobs_stuck
(deploy/k8s/observability/prometheus-rules.yaml). Values are bounded: never a job id, a repo or a person.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from hangar import metrics

log = logging.getLogger("hangar.slo")

# Reasons a failed start is the requester's doing, not hangar's: counted as `rejected`, outside the SLO.
REJECTED = {"bad_branch"}
# Queued jobs held on purpose (power schedule, review capacity, the eval queue) are never stuck.
STUCK_QUEUED_S = 3600.0  # a runnable job queued for an hour
STUCK_PROVISIONING_S = 1200.0  # a sandbox not up in 20 minutes
STUCK_GRACE_S = 1800.0  # past a job's own timeout_minutes, while it runs, verifies or delivers
_WORD = re.compile(r"[^A-Za-z0-9_.:/-]+")


def _v(value: Any) -> str:
    """One bounded token: no spaces, so the line stays `key=value` pairs a regular expression can read."""
    s = _WORD.sub("_", str(value))[:80] if value not in (None, "") else "-"
    return s or "-"


def emit(event: str, **fields: Any) -> str:
    line = " ".join([f"event={_v(event)}", *(f"{k}={_v(v)}" for k, v in fields.items())])
    log.info(line)
    return line


def _state_since(job: Any, state: str) -> float:
    """When `job` entered the run of `state` its events end with (its created_at when the events don't say)."""
    since = None
    for ev in reversed(job.events):
        if ev.state != state:
            break
        since = ev.ts
    return since if since is not None else job.created_at


# ----------------------------------------------------------------------------- job starts (jobs._event)
def job_transition(job: Any, before: str, after: str, at: float | None = None) -> None:
    """A job left `before` for `after`. A start is queued or provisioning becoming running (ok) or failed (error,
    or rejected for the requester's own mistakes). Cancellations and later failures aren't starts. Call it before
    the new state's event is appended, so the events still end with the queued run."""
    if before == after or before not in ("queued", "provisioning") or after not in ("running", "failed"):
        return
    t = at if at is not None else time.time()
    queued_at, held = _queued_at(job)
    queued_s = round(max(0.0, t - queued_at), 1)
    if after == "running":
        result = "ok"
    else:
        result = "rejected" if job.reason in REJECTED else "error"
    emit("job_start", result=result, queued_s=queued_s, held="yes" if held else "no",
         reason=job.reason if after == "failed" else None)
    if metrics.M is not None:
        metrics.M.job_starts.labels(result).inc()
        if result == "ok" and not held:  # the queue SLO is about runnable jobs, not the night or a review cap
            metrics.M.job_queue.observe(queued_s)


def _queued_at(job: Any) -> tuple[float, bool]:
    """When the job was last queued (a follow-up or an approval queues it again), and whether it was held on purpose
    since: the power schedule, review capacity or the eval queue (their events start "waiting:")."""
    seen = held = False
    since = None
    for ev in reversed(job.events):
        if ev.state == "queued":
            seen, since = True, ev.ts
            held = held or ev.message.startswith("waiting:")
        elif seen:
            break
    return (since if since is not None else job.created_at), held


# ----------------------------------------------------------------------------- sandbox creates (observe)
def sandbox_create(provider: Any, environment: Any, seconds: float, err: BaseException | None) -> None:
    """Every sandbox create, ok or not (KAN-136's failures among them). A power-down isn't a failure: the job
    waits for the pools; nor is a cancellation."""
    from hangar.fleet import PoweredDown

    if err is not None and (isinstance(err, PoweredDown) or not isinstance(err, Exception)):
        return
    emit("sandbox_create", result="ok" if err is None else "error", provider=provider, environment=environment,
         seconds=round(seconds, 2), error=type(err).__name__ if err is not None else None)


# ----------------------------------------------------------------------------- governance (fail closed)
def fail_closed(kind: str, failure: str) -> None:
    """hangar answered for Tower because Tower didn't (KAN-78): nothing risky was allowed."""
    emit("fail_closed", kind=kind, failure=failure)
    if metrics.M is not None:
        metrics.M.fail_closed.labels(kind, failure).inc()


# ----------------------------------------------------------------------------- stuck jobs (the ops loop)
def stuck_jobs(jobs: Any, power_off: bool, t: float | None = None) -> list[tuple[float, str]]:
    """(age in its state, state) of each live job that looks stuck. Queued jobs held on purpose aren't, and nor
    is anything queued while the pools are off (the power schedule)."""
    t = t if t is not None else time.time()
    out: list[tuple[float, str]] = []
    for job in list(getattr(jobs, "_jobs", {}).values()):
        state = str(job.state)
        age = t - _state_since(job, state)
        if state == "queued":
            if not power_off and not job.reason and age > STUCK_QUEUED_S:
                out.append((age, state))
        elif state == "provisioning":
            if not power_off and age > STUCK_PROVISIONING_S:
                out.append((age, state))
        elif state in ("running", "verifying", "delivering"):
            if age > 60 * (job.timeout_minutes or 60) + STUCK_GRACE_S:
                out.append((age, state))
    return out


async def stuck_tick(h: Any) -> None:
    """Every minute (opstatus.MINUTELY): how many jobs look stuck, as a gauge and a log line when any do."""
    jobs = getattr(h, "jobs", None)
    if jobs is None:
        return
    power = getattr(getattr(h, "fleet", None), "power", None)
    found = stuck_jobs(jobs, bool(power is not None and power.off))
    if metrics.M is not None:
        metrics.M.jobs_stuck.set(len(found))
    if found:
        emit("jobs_stuck", n=len(found), oldest_s=round(max(a for a, _ in found)),
             states=",".join(sorted({s for _, s in found})))


def register() -> None:
    from hangar import opstatus

    if stuck_tick not in opstatus.MINUTELY:
        opstatus.MINUTELY.append(stuck_tick)
