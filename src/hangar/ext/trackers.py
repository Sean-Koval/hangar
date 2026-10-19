"""Trackers: keep the ticket a job came from in step with the job (intake.track).

A job carries its ticket in the `intake.tracker` label ("jira:ABC-12", "ghproject:<project>:<item>"); the scheme
before the first ":" picks the tracker. `stage` is one of intake.STAGES. Raising is fine: intake.track records it
on the job's timeline.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job

Tracker = Callable[["JobService", "Job", str, str], Awaitable[None]]  # (svc, job, ticket, stage)
