"""Power-down drain: stop starting work, and say when what is in flight has settled (hardening H6, product P2).

`deploy/gke/power.sh down` used to suspend idle sessions and then scale hangar to zero, so SIGTERM cancelled whatever
was half done: creates (a sandbox left `starting` with its lease), suspends (a checkpoint cut off), and queued jobs
started in the last minutes only to be killed. Now it first asks for a drain (POST /api/admin/drain):
- queued jobs stay queued instead of starting (they are queued again when the drain ends, or after the restart);
- GET /api/admin/drain lists what is still in flight: sandboxes being created or destroyed, sessions booting or
  suspending, jobs running. `settled` is true once nothing but running turns is left.
power.sh waits for that, checkpoints the turns still running at its deadline, and only then scales down. Whatever a
shutdown still catches half way is picked up at the next start: the reconciler finishes the sandbox rows, sessions
resume from their checkpoint, and jobs go back to the queue (restarts.py).

A drain ends by itself after `minutes` (a power-down that never happened must not leave hangar refusing work), or
with DELETE /api/admin/drain. Intake stays open: deliveries that arrive meanwhile become queued jobs.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from hangar.models import JobState, SessionState

if TYPE_CHECKING:
    from hangar.jobs import JobService

MAX_MINUTES = 240


class Drain:
    def __init__(self, svc: JobService):
        self.svc = svc
        self.until = 0.0
        self.held: list[str] = []  # job ids that came off the queue while draining
        self._timer: asyncio.Task | None = None

    @property
    def on(self) -> bool:
        return self.until > time.time()

    def hold(self, job_id: str) -> bool:
        """In a job worker: True when the job must wait (it hasn't started, and hangar is draining)."""
        if not self.on:
            return False
        job = self.svc._jobs.get(job_id)
        if job is None or job.state != JobState.QUEUED:
            return False
        if job_id not in self.held:
            self.held.append(job_id)
        return True

    def begin(self, minutes: float) -> None:
        self.until = time.time() + max(1.0, min(float(minutes), MAX_MINUTES)) * 60
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().create_task(self._expire())

    def end(self) -> None:
        self.until = 0.0
        if self._timer is not None and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None
        held, self.held = self.held, []
        for jid in held:
            self.svc._queue.put_nowait(jid)

    def close(self) -> None:
        """At shutdown: the timer goes; held jobs are still queued in the store, so the next start runs them."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def _expire(self) -> None:
        await asyncio.sleep(max(0.0, self.until - time.time()))
        self.end()

    def status(self, h: Any) -> dict[str, Any]:
        flight = h.fleet.reconciler.in_flight()
        sessions = h.sessions.list(limit=500)
        busy = {s.id for s in sessions if h.sessions.busy(s.id)}
        starting = [s.name for s in sessions if s.state == SessionState.STARTING]
        # a suspend runs until its sandbox is destroyed, after the session already says `suspended`
        suspending = [s.name for s in sessions if s.state == SessionState.SUSPENDING
                      or (s.state == SessionState.SUSPENDED and s.id in busy)]
        return {
            "draining": self.on,
            "until": self.until if self.on else None,
            "held_jobs": list(self.held),
            "running_jobs": sorted(self.svc._active),
            "creating": flight["create"],
            "destroying": flight["destroy"],
            "sessions": {
                "starting": starting,
                "suspending": suspending,
                "running": [s.name for s in sessions if s.state == SessionState.RUNNING],
                "with_sandbox": [s.name for s in sessions if s.sandbox_id],
            },
            "settled": not (flight["create"] or flight["destroy"] or starting or suspending),
        }


def mount(app: Any, h: Any, admin: Any) -> None:
    """POST /api/admin/drain (body: {"minutes": 60}), GET it for the status, DELETE it to end the drain."""
    from fastapi import Body

    d: Drain = h.jobs.drain

    @app.post("/api/admin/drain")
    async def drain_begin(body: dict[str, Any] = Body(default_factory=dict), who: str = admin):
        minutes = 60.0
        with contextlib.suppress(TypeError, ValueError):
            minutes = float(body.get("minutes", 60))
        d.begin(minutes)
        h.fleet.audit(who, "admin.drain", None, minutes=minutes)
        return d.status(h)

    @app.get("/api/admin/drain")
    async def drain_status(_: str = admin):
        return d.status(h)

    @app.delete("/api/admin/drain")
    async def drain_end(who: str = admin):
        d.end()
        h.fleet.audit(who, "admin.drain_end", None)
        return d.status(h)
