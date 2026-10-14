"""One control plane shared by a team (docs/team-scale.md): caps per person, and job slots sized from capacity.

A person can hold only so much at once: live sessions, sandboxes started directly, and running jobs. The job queue
gives a free slot to whoever has the fewest jobs running, so one person's batch can't hold everyone else's work
behind it for an hour. `person_of` is the one place that maps who acted to the person a cap counts: the person
identity recorded (people.py; `owner` for the admin's tokens), else the person in the actor string.
"""

from __future__ import annotations

import asyncio
import collections
import itertools
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from hangar.models import SESSION_LIVE, Sandbox, Session

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.sessions import SessionService

# Actors that are hangar itself acting (sessions' and jobs' own sandboxes, the reaper, schedules): not a person.
# What they start is capped where a person asked for it (the session, the job).
_INTERNAL = {"system", "reaper", "session", "jobs", "mcp"}
_INTERNAL_PREFIXES = ("session:", "job:", "hangar:", "schedule:", "trigger:")


class CapReached(ValueError):
    """A person is at one of their caps. A ValueError, so every front door already answers it as a 4xx."""


def person_of(actor: str | None, person: str | None = None) -> str | None:
    """The person a cap counts for an actor, or None for hangar's own actors. `person` is identity's person on the
    record or the caller's principal (identity.person()): when it is known, every token and connector of that
    person, the owner's included, shares one set of caps. Without it (records from before people, in-process
    callers) the actor string decides: the same token reached over MCP ("mcp:sean", "sean") or REST ("ui:sean") is
    one person, and so is an OAuth connector a person approved ("oauth:<client>(sean)"): its grant carries the
    approver's name."""
    if not actor:
        return None
    a = actor
    for prefix in ("mcp:", "ui:", "e2b:"):
        if a.startswith(prefix):
            a = a[len(prefix):]
            break
    # --- integration: hangar's own actors stay exempt even on a record that carries its person (a session's or a
    # job's sandbox is counted where the person asked for the session or job)
    if a in _INTERNAL or a.startswith(_INTERNAL_PREFIXES):
        return None
    if person:
        return person
    # --- identity: a teammate's actors are `@<person>:<token name>` (identity.actor_name), so all their tokens and
    # connectors share one person's caps
    if a.startswith("@") and ":" in a:
        return a[1:].split(":", 1)[0] or None
    if a.startswith("oauth:") and a.endswith(")") and "(" in a:
        subject = a[a.index("(") + 1:-1]
        if subject and subject != "?":
            a = subject
    if not a or a in _INTERNAL or a.startswith(_INTERNAL_PREFIXES):
        return None
    return a


def check_sandboxes(settings: Settings, active: Iterable[Sandbox], actor: str, person: str | None = None) -> None:
    """Refuse a create past HANGAR_PERSON_MAX_SANDBOXES live sandboxes the same person started directly."""
    cap = settings.person_max_sandboxes
    who = person_of(actor, person)
    if cap <= 0 or who is None:
        return
    held = sum(1 for s in active if person_of(s.created_by, s.person) == who)
    if held >= cap:
        raise CapReached(f"{who} already has {held} sandboxes running (the limit per person is {cap}); destroy one "
                         "first, or ask the operator to raise HANGAR_PERSON_MAX_SANDBOXES")


def check_sessions(svc: SessionService, owner: str, starting: Session | None = None, adding: int = 1,
                   person: str | None = None) -> None:
    """Refuse `adding` new or resumed sessions (`starting`, when it is one) past HANGAR_PERSON_MAX_SESSIONS live ones:
    each holds a sandbox. Sessions with a boot or turn already on its way count too, so two sends at once can't both
    slip under the cap. `person` defaults to `starting`'s."""
    cap = svc.settings.person_max_sessions
    who = person_of(owner, person or (starting.person if starting else None))
    if cap <= 0 or who is None:
        return
    live = sum(1 for s in svc._sessions.values()
               if (s.state in SESSION_LIVE or s.id in svc._busy) and (starting is None or s.id != starting.id)
               and person_of(s.owner, s.person) == who)
    if live + adding > cap:
        raise CapReached(f"{who} already has {live} sessions running (the limit per person is {cap}); suspend one "
                         "first (suspend_session), or ask the operator to raise HANGAR_PERSON_MAX_SESSIONS")


# --- review: the most job slots auto-sizing gives. Nothing bounds what a job spends across its turns, or a repo in a
# day, until job budgets (#85) land; and a slot is a job, not a sandbox, so 24 best-of-N jobs could hold every one
# of 120 sandboxes and refuse every session. Set HANGAR_JOB_CONCURRENCY to go higher.
AUTO_JOB_SLOTS_MAX = 8


def job_slots(settings: Settings) -> int:
    """Jobs provisioning or running at once: HANGAR_JOB_CONCURRENCY, else sized from the fleet. On Docker (one
    machine) 4; elsewhere a fifth of max_sandboxes between 4 and AUTO_JOB_SLOTS_MAX, which leaves the rest for
    sessions, forks and best-of-N attempts."""
    if settings.job_concurrency > 0:
        return settings.job_concurrency
    if settings.default_provider == "docker":
        return 4
    return max(4, min(AUTO_JOB_SLOTS_MAX, settings.max_sandboxes // 5))


def job_cap(settings: Settings) -> int:
    """Jobs one person may run at once: HANGAR_PERSON_MAX_RUNNING_JOBS, else half the slots, and at least 4 (what
    anyone could run before the cap; a solo install on Docker keeps all four of its slots)."""
    if settings.person_max_running_jobs > 0:
        return settings.person_max_running_jobs
    return max(4, job_slots(settings) // 2)


class FairJobQueue:
    """The job queue, fair between people. A free worker takes the waiting job of whoever has the fewest jobs
    running (the oldest job among equals), skipping people at their cap. A drop-in for the asyncio.Queue the job
    workers used: put_nowait, get, task_done (which frees the slot of the job this worker task took), qsize, empty.

    `person` maps a job id to its person ("" for jobs nobody in particular asked for)."""

    def __init__(self, person: Callable[[str], str], cap: Callable[[], int]):
        self._person = person
        self._cap = cap
        self._seq = itertools.count()
        self._waiting: list[tuple[int, str]] = []
        self._running: collections.Counter[str] = collections.Counter()
        self._held: dict[asyncio.Task | None, str] = {}
        self._changed = asyncio.Event()

    def put_nowait(self, job_id: str) -> None:
        self._waiting.append((next(self._seq), job_id))
        self._changed.set()

    def qsize(self) -> int:
        return len(self._waiting)

    def empty(self) -> bool:
        return not self._waiting

    def running(self) -> dict[str, int]:
        return dict(self._running)

    def _pick(self) -> tuple[str, str] | None:
        """(job id, person) of the job to run next, taken off the queue; None while everyone waiting is capped."""
        cap = self._cap()
        best: tuple[int, int, int] | None = None  # (their running jobs, arrival, index)
        people: dict[str, str] = {}
        for i, (seq, job_id) in enumerate(self._waiting):
            who = people[job_id] = self._person(job_id)
            if cap > 0 and who and self._running[who] >= cap:
                continue
            key = (self._running[who] if who else 0, seq, i)
            if best is None or key < best:
                best = key
        if best is None:
            return None
        _, job_id = self._waiting.pop(best[2])
        return job_id, people[job_id]

    async def get(self) -> str:
        while True:
            picked = self._pick()
            if picked is not None:
                job_id, who = picked
                self._running[who] += 1
                self._held[asyncio.current_task()] = who
                return job_id
            self._changed.clear()
            await self._changed.wait()

    def task_done(self) -> None:
        who = self._held.pop(asyncio.current_task(), None)
        if who is not None:
            self._running[who] -= 1
            if self._running[who] <= 0:
                del self._running[who]
        self._changed.set()
