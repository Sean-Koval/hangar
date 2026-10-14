"""Intake: what the tracker front doors share (GitHub issues, Slack, Jira, GitHub Projects).

Each front door verifies its own signatures and reads its own payloads (github.py and jobs.handle_github, slack.py,
jira.py, ghprojects.py). What they have in common lives here, so a new tracker only maps its payload:

* **Who.** An outside account (a GitHub user, a Slack user, a Jira account) is a hangar person only when someone
  linked it (`people.resolve_external`). A linked sender's job is theirs: their caps, their credentials, their
  visibility. Anyone else is untrusted: their job waits for a trusted teammate's approval, its text is fenced as
  untrusted for the agent, and it runs on the team's credentials (a job with no person gets none of anyone's own).
* **Once.** A signed delivery is claimed before it is handled and marked done after (`once`, the reconciler's
  commit-after-handle from #84), so a retry of one that failed half way runs again and one that was handled is
  acknowledged and dropped. The delivery's body digest is the key: a replayed copy can't pass as new by changing an
  unsigned header.
* **One job per ticket.** A ticket with an unfinished job doesn't start a second (`open_job`).
* **Submit.** Through `JobService.submit` (repo policy, admission, budgets), with an approval for untrusted senders
  and the refusal turned into an answer the front door can send (`submit`).
* **Write-back.** A job that came from a tracker keeps the ticket in step as it runs: started, a question, delivered,
  failed (`track`, called from `JobService._comment` whenever the job reports, whichever thread the conversation is
  in by then). Each tracker registers how in `TRACKERS`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from hangar import people
from hangar import redeliver as redeliver
from hangar.models import JOB_TERMINAL, Job, JobState

if TYPE_CHECKING:
    from fastapi import Request

    from hangar.jobs import JobService, SubmitJob
    from hangar.store import Store

log = logging.getLogger("hangar.intake")

TRACKER = "intake.tracker"  # job label: the ticket the job keeps in step ("jira:ABC-12", "ghproject:<project>:<item>")
STAGE = "intake.stage"  # job label: the stage last written back, so a repeated report writes nothing twice
STAGES = ("started", "needs_input", "delivered", "failed")

# scheme of the TRACKER label -> update(svc, job, ticket, stage). Raising is fine: `track` records it on the job.
TRACKERS: dict[str, Callable[[JobService, Job, str, str], Awaitable[None]]] = {}


# ====================================================================== who
def person_for(store: Store, source: str, external_id: str | int | None) -> str | None:
    """The id of the hangar person linked to an outside account, or None: unlinked, or the lookup failed (fail
    closed). The ids are the ones that don't change hands (docs/team.md#who-is-trusted): "github", the user's numeric
    id (`sender.id`, never the login); "slack", `TEAM/USER`; "jira", the Atlassian account id (Data Center: the user
    key)."""
    if external_id is None or isinstance(external_id, bool) or not str(external_id).strip():
        return None
    try:
        p = people.resolve_external(store, source, str(external_id).strip())
    except Exception as e:  # noqa: BLE001 - an unlinked sender is untrusted, never an error for the sender
        log.warning("could not resolve %s account %s to a person: %s", source, str(external_id)[:80], e)
        return None
    return p.id if p is not None else None


# ====================================================================== once
async def read_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None past `limit` bytes (stops reading there, before parsing anything)."""
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > limit):
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return body


async def once(svc: JobService, scope: str, digest: str,
               handle: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any] | None:
    """Run `handle` for a verified delivery unless it was handled already (None then). A delivery whose handling
    raised, failed (`error`) or was rate limited is forgotten, so the sender's retry runs it again."""
    replays = svc.admission.replays
    ttl = svc.settings.webhook_replay_hours * 3600
    if not redeliver.claim(replays, digest, ttl, scope=scope):
        return None
    try:
        res = await handle()
    except BaseException:  # a shutdown mid-handler too: nothing happened, let the retry try again
        replays.forget(scope, digest)
        raise
    if res.get("error") or res.get("rate_limited"):
        replays.forget(scope, digest)
    else:
        redeliver.done(replays, digest, ttl, scope=scope)
    return res


# ====================================================================== one job per ticket
def open_job(svc: JobService, ref: str) -> Job | None:
    """The ticket's unfinished job (waiting for approval or an answer included), if any."""
    return next((j for j in svc._jobs.values() if j.source_ref == ref and j.state not in JOB_TERMINAL), None)


def latest_job(svc: JobService, ref: str) -> Job | None:
    jobs = svc.where(source_ref=ref)  # --- durable: finished ones aren't all in memory
    return max(jobs, key=lambda j: j.created_at) if jobs else None


# ====================================================================== submit
def submit(svc: JobService, req: SubmitJob, *, source: str, ref: str, requester: str, person: str | None,
           trusted: bool, who: str, restricted: bool = True) -> dict[str, Any]:
    """Submit a ticket's job. Answers like handle_github: {"ok", "job", "awaiting_approval"?}, {"ignored"},
    {"error"} (configuration: say so on the ticket) or {"rate_limited"} (the sender should retry later)."""
    from hangar.frontdoor import RateLimited, TooManyActive
    from hangar.jobs import Caller, JobError
    from hangar.triggers import new_approval

    if (busy := open_job(svc, ref)) is not None:
        return {"ignored": f"job {busy.id} is already {busy.state} for {ref}", "job": busy.id}
    approval = None if trusted else new_approval(
        svc, requester, f"{who} isn't linked to a hangar person; a linked teammate must approve")
    try:
        job = svc.submit(req, Caller(requester, requester, restricted=restricted, person=person), source=source,
                         source_ref=ref, approval=approval, untrusted_source=None if trusted else requester)
    except RateLimited as e:
        return {"ignored": str(e), "rate_limited": True}
    except TooManyActive as e:
        return {"ignored": str(e)}
    except (JobError, ValueError) as e:  # pydantic's ValidationError is a ValueError
        return {"error": str(e).strip("'\"")[:500]}
    return {"ok": True, "job": job.id, **({"awaiting_approval": True} if job.approval is not None
                                         and job.state == JobState.AWAITING_APPROVAL else {})}


# ====================================================================== write-back
def stage(job: Job, kind: str) -> str | None:
    """Which tracker stage a job report means, or None for reports that move nothing (queued, approvals)."""
    if kind == "final":
        return "delivered" if job.state == JobState.SUCCEEDED else "failed"
    if job.state == JobState.NEEDS_INPUT:
        return "needs_input"
    if job.state == JobState.RUNNING:
        return "started"
    return None


_updates: dict[str, asyncio.Task] = {}  # job id -> its latest tracker update, which runs after the one before


async def track(svc: JobService, job: Job | None, kind: str) -> None:
    """Keep the ticket a job came from in step with the job. The update runs in the background, after the job's
    previous one (a slow or retrying tracker must not hold up the agent, nor land "started" after "delivered"),
    and never raises: what went wrong lands on the job's timeline."""
    if job is None or not (ref := job.labels.get(TRACKER)):
        return
    st = stage(job, kind)
    fn = TRACKERS.get(ref.split(":", 1)[0])
    if st is None or fn is None or job.labels.get(STAGE) == st:
        return
    job.labels[STAGE] = st
    svc._save(job)
    before = _updates.get(job.id)

    async def run() -> None:
        if before is not None:
            await asyncio.wait([before])
        try:
            await fn(svc, job, ref, st)
        except Exception as e:  # noqa: BLE001
            svc._event(job, f"could not update {ref} ({st}): {e}")
        finally:
            if _updates.get(job.id) is task:
                del _updates[job.id]

    task = asyncio.get_running_loop().create_task(run())
    _updates[job.id] = task
    svc._background.add(task)  # cancelled with the job service's other background work
    task.add_done_callback(svc._background.discard)
