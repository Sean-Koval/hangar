"""Jobs a control-plane restart interrupted resume instead of failing (hardening B8, product B1/P2).

A deploy, a crash or the 01:00 power-down stops hangar under jobs that are provisioning or mid-turn. Their sandbox
is gone by the time it starts again, but nothing is lost: provisioning can simply run again, and a job whose
session has a checkpoint has its repo and the agent's conversation in it. So instead of `failed`/`interrupted`
(which left the owner a failed job and a missing PR in the morning), such a job goes back to the queue: a job caught
provisioning starts over, and one caught in or after a turn gets a follow-up turn that resumes its session from the
last checkpoint and tells the agent to carry on. Each job does this at most HANGAR_JOB_RESTART_RESUMES times, so a
job that brings the control plane down can't loop. The jobs re-enter the queue HANGAR_JOB_RESTART_STAGGER_SECONDS
apart: a power-up starts on a cold cluster, and every one of them needs a sandbox.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from hangar import strategies
from hangar.models import Job, JobState, Turn, now

if TYPE_CHECKING:
    from hangar.jobs import JobService

CONTINUE = (
    "hangar's control plane restarted while you were working on this task (a deploy or a scheduled power-down), "
    "and your sandbox was restored from its last checkpoint: the repository and this conversation are as they were "
    "then, so anything after it is missing. Check where the work stands (`git status`, `git log`, the files you were "
    "changing), redo what is missing, and carry on until the task is done."
)


def _left(svc: JobService, job: Job) -> bool:
    return job.restarts < max(0, svc.settings.job_restart_resumes) and not strategies.is_attempt_phase(job)


def start_over(svc: JobService, job: Job) -> bool:
    """A job the restart caught while provisioning, with nothing saved yet (its sandbox already discarded): queue it
    to provision again. False when it has used up its restarts."""
    if not _left(svc, job):
        return False
    job.restarts += 1
    job.session_id = None  # its session never got a checkpoint and was archived with the sandbox
    job.sandbox_id = None
    svc._event(job, "control plane restarted while provisioning; starting over", JobState.QUEUED, reason=None)
    return True


def resume(svc: JobService, job: Job, why: str = "the control plane restarted", message: str = CONTINUE) -> bool:
    """A job whose sandbox is gone after the restart: queue it to resume its session from the last checkpoint. False
    when there is no checkpoint to resume from, or it has used up its restarts. --- ops: `why` and `message` for a
    sandbox the nightly power-down took away while hangar stayed up (power.py)."""
    s = svc._session(job)
    if s is None or not svc.sessions.resumable(s) or not _left(svc, job):
        return False
    job.restarts += 1
    last = job.turns[-1] if job.turns else None
    if last is not None and last.run_id is not None:  # a turn ran: a new one picks up where the checkpoint left it
        if last.finished_at is None:
            last.finished_at = now()
            last.outcome, last.outcome_reason = "gone", why
            for st in s.turns:  # the session's record of that turn ends too, so nothing waits on it
                if st.run_id == last.run_id and st.finished_at is None:
                    st.finished_at, st.outcome, st.outcome_reason = now(), "gone", why
            svc.sessions._save(s)
        job.turns.append(Turn(kind="follow_up", message=message, requester="hangar", reply_to=last.reply_to,
                              trigger="restart"))
    svc._event(job, f"{why} and the sandbox is gone; resuming from its last checkpoint "
                    f"(restart {job.restarts} of {svc.settings.job_restart_resumes})", JobState.QUEUED, reason=None)
    return True


def release(svc: JobService, job_ids: list[str]) -> None:
    """Put the resumed jobs on the queue one at a time, HANGAR_JOB_RESTART_STAGGER_SECONDS apart. Each stays
    `queued` (and cancellable) until its turn comes."""
    if not job_ids:
        return
    stagger = max(0.0, svc.settings.job_restart_stagger_seconds)

    async def run() -> None:
        for i, jid in enumerate(job_ids):
            if i and stagger:
                await asyncio.sleep(stagger)
            svc._queue.put_nowait(jid)

    svc._spawn(run())
