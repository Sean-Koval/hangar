"""One control plane shared by a team (docs/team-scale.md): caps per person, and job slots sized from capacity.

A person can hold only so much at once: live sessions, sandboxes started directly, and running jobs. The job queue
gives a free slot to whoever has the fewest jobs running, so one person's batch can't hold everyone else's work
behind it for an hour. --- review capacity (#159): and it holds a person's *new* jobs (reason `awaiting_capacity`)
while too many of their agent PRs already wait on review (`ReviewLoad`, HANGAR_PERSON_MAX_OPEN_AGENT_PRS).
`person_of` is the one place that maps who acted to the person a cap counts: the person identity recorded
(people.py; `owner` for the admin's tokens), else the person in the actor string.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from hangar.models import SESSION_LIVE, Job, JobState, Sandbox, Session

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.jobs import JobService
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


# --- review: the most job slots auto-sizing gives. Job budgets and daily spend caps (jobbudget.py) are opt-in, so by
# default nothing bounds what a job spends across its turns, or a repo or person in a day; and a slot is a job, not a
# sandbox, so 24 best-of-N jobs could hold every one of 120 sandboxes and refuse every session. Set
# HANGAR_JOB_CONCURRENCY (24 on GKE) once budgets are set.
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

    `person` maps a job id to its person ("" for jobs nobody in particular asked for). --- review capacity (#159):
    `admit` (optional) is asked about each job that could run; False leaves it waiting in its place (`hold_new`)."""

    def __init__(self, person: Callable[[str], str], cap: Callable[[], int],
                 admit: Callable[[str], bool] | None = None, rank: Callable[[str], int] | None = None,
                 recheck: float | None = None):
        self._person = person
        self._cap = cap
        self._admit = admit
        self._rank = rank  # --- #181: a lower rank always goes first (interactive 0, eval 1: evalqueue.py)
        self._recheck = recheck  # --- #181: seconds between looks with nothing changed (time-based admission)
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

    def waiting(self) -> list[str]:
        return [job_id for _, job_id in self._waiting]

    def wake(self) -> None:
        """--- review capacity (#159): something that held a waiting job may have changed (a PR merged)."""
        self._changed.set()

    def _pick(self) -> tuple[str, str] | None:
        """(job id, person) of the job to run next, taken off the queue; None while everyone waiting is capped."""
        cap = self._cap()
        best: tuple[int, int, int, int] | None = None  # (rank, their running jobs, arrival, index)
        people: dict[str, str] = {}
        for i, (seq, job_id) in enumerate(self._waiting):
            who = people[job_id] = self._person(job_id)
            if cap > 0 and who and self._running[who] >= cap:
                continue
            key = (self._rank(job_id) if self._rank else 0, self._running[who] if who else 0, seq, i)
            if best is not None and key >= best:
                continue
            if self._admit is not None and not self._admit(job_id):  # --- review capacity (#159)
                continue
            if best is None or key < best:
                best = key
        if best is None:
            return None
        _, job_id = self._waiting.pop(best[3])
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
            if self._recheck:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), self._recheck)
            else:
                await self._changed.wait()

    def task_done(self) -> None:
        who = self._held.pop(asyncio.current_task(), None)
        if who is not None:
            self._running[who] -= 1
            if self._running[who] <= 0:
                del self._running[who]
        self._changed.set()


# ====================================================================== review capacity (#159)
AWAITING = "awaiting_capacity"  # a held new job's reason; its state stays `queued`


def bucket_of(job: Job) -> str:
    """Whose review an agent PR waits on: the job's person (they review their own delegated PR), else the repo's
    team (`team:<owner/repo>`)."""
    from hangar.repopolicy import repo_key

    return job.person or person_of(job.requester) or f"team:{repo_key(job.repo) or '?'}"


def counts_as_open(job: Job) -> bool:
    """An agent PR that waits on a person: open or approved (approved but not landing still needs them), not ended.
    AI review jobs and experiment trials open none."""
    st = job.landing
    if job.source == "review" or "hangar.experiment" in job.labels or not job.result.pr_number:
        return False
    return st.pr_state in ("open", "approved") and not st.ended


class ReviewLoad:
    """Open agent PRs per review bucket, kept up to date from every save of a job (landing events save the job) and
    counted again at startup. A PR counts for its bucket; when the self-approval rule (#160) needs a second reviewer,
    also for the people linked to the repo policy's approvers (`landing.approver_people`)."""

    def __init__(self) -> None:
        self.counts: collections.Counter[str] = collections.Counter()
        self._of: dict[str, frozenset[str]] = {}

    def buckets(self, svc: JobService, job: Job) -> frozenset[str]:
        if not counts_as_open(job):
            return frozenset()
        mine = bucket_of(job)
        out = {mine}
        if job.landing.approver_people and not mine.startswith("team:") and svc.landing.needs_second_reviewer(job):
            out |= {p for p in job.landing.approver_people if p and p != mine}
        return frozenset(out)

    def update(self, svc: JobService, job: Job) -> bool:
        """Record `job`'s PR where it counts now. True when a count went down (a held job may run)."""
        new = self.buckets(svc, job)
        old = self._of.get(job.id, frozenset())
        if new == old:
            return False
        for b in old - new:
            self.counts[b] -= 1
            if self.counts[b] <= 0:
                del self.counts[b]
        for b in new - old:
            self.counts[b] += 1
        if new:
            self._of[job.id] = new
        else:
            self._of.pop(job.id, None)
        return bool(old - new)

    def recount(self, svc: JobService) -> None:
        """At startup: from the jobs in memory (every job with an open PR is: JobService._pinned)."""
        self.counts.clear()
        self._of.clear()
        for job in list(svc._jobs.values()):
            self.update(svc, job)


def open_agent_prs(svc: JobService, person: str) -> int:
    """Agent PRs awaiting `person`'s review (a person id, or `team:<owner/repo>`)."""
    return svc.review_load.counts.get(person, 0)


def pr_cap(svc: JobService, job: Job) -> int:
    """The open-PR cap for the job's bucket: a per-person override, else the repo policy's `max_open_agent_prs`,
    else HANGAR_PERSON_MAX_OPEN_AGENT_PRS; a team bucket gets that times the number of policy approvers. 0 = off."""
    s, policy = svc.settings, svc.policies.get(job.repo)
    b = bucket_of(job)
    over = s.person_max_open_agent_prs_overrides or {}
    if b in over:
        return max(0, int(over[b]))
    cap = policy.max_open_agent_prs if policy and policy.max_open_agent_prs is not None \
        else s.person_max_open_agent_prs
    if b.startswith("team:"):
        return cap * max(1, len(policy.approvers or []) if policy else 1)
    return cap


def exempt(job: Job) -> str | None:
    """Why a waiting job never waits for review capacity, or None: only a job's first turn does."""
    if job.force_admitted_by:
        return f"admitted by {job.force_admitted_by}"
    if job.source == "review":
        return "an AI review"
    if "hangar.experiment" in job.labels:
        return "an experiment trial"
    if len(job.turns) > 1 or any(t.run_id for t in job.turns):
        return "not its first turn"  # follow-ups, auto-fix and verify-fix rounds shrink the queue
    return None


def review_link(svc: JobService) -> str:
    """Where a person's agent PRs wait: GitHub's search for open PRs that request their review."""
    return "https://github.com/pulls?q=is%3Aopen+is%3Apr+review-requested%3A%40me"


def held_line(svc: JobService, job: Job) -> str | None:
    """Why a new job would wait for review capacity now, or None (the same rule as `hold_new`)."""
    if job.state != JobState.QUEUED or exempt(job):
        return None
    cap = pr_cap(svc, job)
    if cap <= 0:
        return None
    b = bucket_of(job)
    n = open_agent_prs(svc, b)
    if n < cap:
        return None
    who = "the repo's approvers have" if b.startswith("team:") else f"{b} has"
    return f"{who} {n} agent PRs awaiting review (the cap is {cap})"


def hold_new(svc: JobService, job_id: str) -> bool:
    """FairJobQueue's `admit`, inverted: True when a waiting job must wait for review capacity. The job keeps its
    place and its `queued` state; the first time, its reason becomes `awaiting_capacity` and an event says why."""
    job = svc._jobs.get(job_id)
    if job is None or job.cancel_requested:
        return False
    why = held_line(svc, job)
    if why is None:
        return False
    if job.reason != AWAITING:
        svc._event(job, f"waiting: {why}; reviewing or closing one starts it ({review_link(svc)})", reason=AWAITING)
    return True


def admitted(svc: JobService, job_id: str) -> None:
    """A worker took the job: a job held for review capacity says it starts."""
    job = svc._jobs.get(job_id)
    if job is not None and job.reason == AWAITING and job.state == JobState.QUEUED:
        svc._event(job, "review capacity is free; starting", reason=None)


def awaiting(svc: JobService, person: str | None = None) -> list[Job]:
    """Jobs held for review capacity (a person's: `person`)."""
    out = [j for j in svc._jobs.values() if j.state == JobState.QUEUED and j.reason == AWAITING]
    return [j for j in out if person is None or bucket_of(j) == person]


def capacity_view(svc: JobService, person: str) -> dict[str, int]:
    """For a digest (#158) or a front door: a person's open agent PRs, their cap, and their jobs held for it."""
    probe = Job(title="", instructions="", environment="", person=person, requester=person)
    live = sum(1 for j in list(svc._jobs.values()) if person in svc.review_load.buckets(svc, j))  # counted afresh
    return {"open_agent_prs": live, "cap": pr_cap(svc, probe),
            "awaiting_capacity": len(awaiting(svc, person))}


def queued_note(svc: JobService, job: Job) -> str:
    """The front doors' "queued" reply: empty, or a sentence saying the job waits for review capacity."""
    why = held_line(svc, job)
    if why is None:
        return ""
    return f" It waits for review capacity: {why}. Review queue: {review_link(svc)}"


def admit(svc: JobService, job_id: str, actor: str) -> Job:
    """An admin lets a held new job past the open-PR cap (`hangar job admit`)."""
    from hangar.jobs import JobError

    job = svc.get(job_id)
    if job.state != JobState.QUEUED:
        raise JobError(f"job {job.id} is {job.state}; only a queued job is admitted")
    job.force_admitted_by = actor
    svc._event(job, f"admitted past the review-capacity cap by {actor}", reason=None if job.reason == AWAITING
               else False)
    svc.fleet.audit(actor, "job.admit", job.id)
    svc._queue.wake()
    return job


def mount(app: Any, h: Any, admin: Any) -> None:
    """POST /api/jobs/{id}/admit (admin): past the review-capacity cap. GET /api/review-capacity (admin): open agent
    PRs per reviewer and the jobs held for them."""
    from fastapi import HTTPException

    from hangar.jobs import JobError, JobNotFound

    svc: JobService = h.jobs

    @app.post("/api/jobs/{job_id}/admit")
    async def job_admit(job_id: str, who: str = admin):
        try:
            return svc.view(admit(svc, job_id, who))
        except JobNotFound as e:
            raise HTTPException(404, str(e).strip("'\"")) from e
        except JobError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/review-capacity")
    async def review_capacity(_: str = admin):
        held = awaiting(svc)
        return {"open_agent_prs": dict(svc.review_load.counts), "awaiting_capacity": len(held),
                "held": [{"job": j.id, "for": bucket_of(j), "title": j.title} for j in held[:200]],
                "default_cap": svc.settings.person_max_open_agent_prs}
