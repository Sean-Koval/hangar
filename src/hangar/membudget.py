"""A cap on the bytes the control plane holds at once for one kind of bulk transfer (docs/team-scale.md).

Workspace snapshots, forks and restores move whole archives through hangar's memory (hundreds of MB each, more
when a provider base64-encodes them). Unbounded, `fork_sandbox(count=5)` of a 400 MB workspace needed more than the
2 GiB the control plane gets, and the kernel killed it mid-fork. Each transfer now reserves what it will hold first,
and waits while the budget is spent.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator


class MemoryBudget:
    """`hold(n)` waits until n more bytes fit under the limit. A request bigger than the whole limit waits until
    nothing else holds any, then runs alone: the peak is the larger of the limit and the biggest single request."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.waiting = 0
        self._freed = asyncio.Event()

    @contextlib.asynccontextmanager
    async def hold(self, n: int) -> AsyncIterator[None]:
        n = max(0, int(n))
        self.waiting += 1
        try:
            while not (self.used == 0 or self.used + n <= self.limit):
                self._freed.clear()
                await self._freed.wait()
        finally:
            self.waiting -= 1
        self.used += n  # no await between the check and this: nobody else can take the room meanwhile
        try:
            yield
        finally:
            self.used -= n
            self._freed.set()
