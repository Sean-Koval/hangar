"""Retention of settled sandbox records (docs/team-scale.md).

A record is kept for every sandbox ever started, and a team starts hundreds a day (jobs, best-of-N attempts, forks,
resumed sessions). Stopped and failed ones move to an archive table once idle for HANGAR_SANDBOX_RETENTION_DAYS:
listings and scans stop paying for them, while lookups by id (a snapshot's parent, an old job's sandbox, the audit
trail) still find them. Archived records are deleted after HANGAR_SANDBOX_ARCHIVE_DAYS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from hangar.config import Settings
from hangar.models import Sandbox, SandboxState, now
from hangar.store import Store

log = logging.getLogger("hangar.retention")

SETTLED = (SandboxState.STOPPED, SandboxState.ERROR)
BATCH = 500  # rows per transaction: requests wait on the store's lock for one batch at most (a few ms)
INTERVAL = 3600.0


class Retention:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
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
            before = now() - s.sandbox_retention_days * 86400
            while True:
                n = await asyncio.to_thread(self.store.archive, Sandbox, SETTLED, before, "last_activity", BATCH)
                out["archived"] += n
                if n < BATCH:
                    break
        if s.sandbox_archive_days > 0:
            before = now() - s.sandbox_archive_days * 86400
            out["purged"] = await asyncio.to_thread(self.store.purge_archive, Sandbox, before)
        if out["archived"] or out["purged"]:
            log.info("sandbox records: %d archived, %d deleted from the archive", out["archived"], out["purged"])
        return out

    async def _loop(self) -> None:
        await asyncio.sleep(60)  # not in the way of startup (reconcile, re-attaching sessions and jobs)
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - next hour
                log.exception("sandbox retention pass failed")
            await asyncio.sleep(INTERVAL)
