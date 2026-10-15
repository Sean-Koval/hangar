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
import contextlib
from typing import TYPE_CHECKING

from hangar import strategies
from hangar.models import Attempt, Job, JobState, Session, SessionState, SessionTurn, Turn, now

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


def start_over(svc: JobService, job: Job, why: str = "control plane restarted while provisioning") -> bool:
    """A job the restart caught while provisioning, with nothing saved yet (its sandbox already discarded): queue it
    to provision again, base check included. False when it has used up its restarts."""
    if not _left(svc, job):
        return False
    job.restarts += 1
    s = svc._session(job)
    if s is not None and s.state != SessionState.ARCHIVED and not any(t.run_id for t in job.turns):
        # --- trust: a checkpoint taken while provisioning (the drain's) holds only the base: drop it, so provisioning
        # starts the session afresh (for_job reuses it) and runs the base check again
        svc.sessions.release_restart_hold(s)
        with contextlib.suppress(Exception):
            svc.sessions.archive(s.id, requester=f"job:{job.id}")
    job.session_id = None  # its session never got a checkpoint and was archived with the sandbox
    job.sandbox_id = None
    job.result.baseline = None  # --- trust: checked again on the new sandbox
    svc._event(job, f"{why}; starting over", JobState.QUEUED, reason=None)
    return True


async def lost_while_provisioning(svc: JobService, job: Job) -> bool:
    """--- trust: the job's sandbox went away before its first turn, typically under a long base check (#82's
    baseline): provision again from the start, like a restart that caught it there, instead of starting the turn
    on a session with no sandbox and no baseline. With the pools powered down it waits for them (the worker's
    PoweredDown path). Always False: this provisioning didn't finish."""
    from hangar.fleet import PoweredDown

    if (why := svc.fleet.power.refusal(job.provider)) is not None:
        job.result.baseline = None
        raise PoweredDown(why)
    await svc._discard_sandbox(job)
    if start_over(svc, job, "the sandbox went away while provisioning"):
        release(svc, [job.id])
    else:
        svc._event(job, "the sandbox went away while provisioning", JobState.FAILED, reason="sandbox_gone")
        await svc._report(job)
    return False


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


def resume_attempt(svc: JobService, job: Job, a: Attempt, s: Session, why: str, message: str) -> bool:
    """--- trust: a best-of-N attempt whose sandbox went away mid-turn (#95: the nightly power-down) resumes its own
    session from its last checkpoint, the way `resume` does a job's: its cut-off turn ends `gone`, a turn that tells
    the agent to carry on is queued on the session, and the attempt waits (`waiting`) to run again. At most
    HANGAR_JOB_RESTART_RESUMES times per attempt; False when it can't (no checkpoint, or no resumes left), and the
    attempt ends as before."""
    if not s.checkpoints or a.restarts >= max(0, svc.settings.job_restart_resumes):
        return False
    a.restarts += 1
    for st in s.turns:
        if st.run_id == a.run_id:
            st.finished_at = st.finished_at or now()
            st.outcome, st.outcome_reason = "gone", why
    s.turns.append(SessionTurn(message=message, requester="hangar"))
    svc.sessions._save(s)
    a.run_id = a.outcome = a.exit_code = a.finished_at = a.error = None
    a.state = "waiting"
    svc._event(job, f"attempt {a.index}: {why} and its sandbox is gone; it resumes from its last checkpoint "
                    f"(restart {a.restarts} of {svc.settings.job_restart_resumes})")
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
