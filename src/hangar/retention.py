"""Retention of settled sandbox records, and of finished jobs and sessions (docs/team-scale.md).

A record is kept for every sandbox ever started, and a team starts hundreds a day (jobs, best-of-N attempts, forks,
resumed sessions). Stopped and failed ones move to an archive table once idle for HANGAR_SANDBOX_RETENTION_DAYS:
listings and scans stop paying for them, while lookups by id (a snapshot's parent, an old job's sandbox, the audit
trail) still find them. Archived records are deleted after HANGAR_SANDBOX_ARCHIVE_DAYS.

--- durable: finished jobs and settled sessions move to their archive tables after HANGAR_JOB_RETENTION_DAYS and
HANGAR_SESSION_RETENTION_DAYS, except what is still referenced (store._KEPT, store._REFERENCED_BY). They are never
deleted: lookups by id and name, listings and the usage report read the archive too. The same loop lets go of the
jobs and sessions nothing needs in memory any more (resident.py), every few minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from hangar.config import Settings
from hangar.models import JOB_TERMINAL, Job, Sandbox, SandboxState, Session, SessionState, now
from hangar.store import Store

log = logging.getLogger("hangar.retention")

SETTLED = (SandboxState.STOPPED, SandboxState.ERROR)
SESSION_SETTLED = (SessionState.SUSPENDED, SessionState.ARCHIVED, SessionState.FAILED)  # --- durable
# Rows per transaction, and a pause between two: requests wait on the store's lock for one batch at most. The first
# pass over an install with months of history moves tens of thousands of rows; at 500 a batch, under load, a batch
# held the lock for up to 0.9 s (scripts/bench/team_load.py), so it goes in small steps in the background instead.
BATCH = 100
PAUSE = 0.05
INTERVAL = 3600.0
EVICT_INTERVAL = 300.0  # --- durable: how often memory lets go of what nothing needs


class Retention:
    def __init__(self, store: Store, settings: Settings, jobs: Any = None, sessions: Any = None):
        self.store = store
        self.settings = settings
        self.jobs = jobs  # --- durable: JobService and SessionService, whose memory `evict` trims
        self.sessions = sessions
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    async def sweep(self) -> dict[str, int]:
        """One pass, in worker threads and in batches, so the event loop keeps serving meanwhile."""
        out = {"archived": 0, "purged": 0}
        s = self.settings
        if s.sandbox_retention_days > 0:
            out["archived"] = await self._archive(Sandbox, SETTLED, s.sandbox_retention_days, "last_activity")
        if s.sandbox_archive_days > 0:
            before = now() - s.sandbox_archive_days * 86400
            out["purged"] = await asyncio.to_thread(self.store.purge_archive, Sandbox, before)
        if out["archived"] or out["purged"]:
            log.info("sandbox records: %d archived, %d deleted from the archive", out["archived"], out["purged"])
        # --- durable: jobs first, so the sessions they pointed at can follow on the next pass
        out["jobs_archived"] = await self._archive(Job, tuple(JOB_TERMINAL), s.job_retention_days)
        out["sessions_archived"] = await self._archive(Session, SESSION_SETTLED, s.session_retention_days)
        if out["jobs_archived"] or out["sessions_archived"]:
            log.info("archived %d jobs and %d sessions", out["jobs_archived"], out["sessions_archived"])
        return out

    async def _archive(self, cls: type, states: tuple, days: float, field: str = "updated_at") -> int:
        if days <= 0:
            return 0
        before, moved = now() - days * 86400, 0
        while True:
            n = await asyncio.to_thread(self.store.archive, cls, states, before, field, BATCH)
            moved += n
            if n < BATCH:
                return moved
            await asyncio.sleep(PAUSE)

    def evict(self) -> dict[str, int]:
        """--- durable: on the loop, which owns the job and session maps."""
        return {"jobs": self.jobs.evict() if self.jobs is not None else 0,
                "sessions": self.sessions.evict() if self.sessions is not None else 0}

    async def _loop(self) -> None:
        await asyncio.sleep(60)  # not in the way of startup (reconcile, re-attaching sessions and jobs)
        last_sweep = 0.0
        while True:
            try:
                if time.monotonic() - last_sweep >= INTERVAL or not last_sweep:
                    last_sweep = time.monotonic()
                    await self.sweep()
                gone = self.evict()
                if any(gone.values()):
                    log.debug("let go of %d jobs and %d sessions in memory", gone["jobs"], gone["sessions"])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - next time round
                log.exception("retention pass failed")
            await asyncio.sleep(EVICT_INTERVAL)
