"""Doors: where work comes in (an HTTP route a chat, tracker or bot posts to), and how its threads hear back."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from fastapi import FastAPI

    from hangar.jobs import JobService
    from hangar.models import Job

# (service, job, ref, body, kind): post `body` to the thread `ref` ("slack:C1:1712345678.000100"); kind is "final"
# for the outcome report and "note" for everything else. The door's `name` is the ref's scheme (jobs.NOTIFIERS).
Reply = Callable[["JobService", "Job | None", str, str, str], Awaitable[None]]


@runtime_checkable
class Door(Protocol):
    name: str  # also the scheme of the refs its jobs reply to, when it has `reply`

    def mount(self, app: FastAPI) -> None:
        """Add the door's routes. Called once, after hangar's own API routes."""
        ...

    def doctor(self) -> list[dict[str, Any]] | Awaitable[list[dict[str, Any]]]:
        """Checks `hangar doctor` lists (doctor._check's shape); may be async."""
        ...

    # Optional attribute: `reply: Reply | None`, read with getattr.
