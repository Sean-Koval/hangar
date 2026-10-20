"""The deck's Retry (KAN-140 S6): `POST /api/jobs/{id}/retry`
(docs/jobs/working-with-the-agent.md#5-work-with-the-agent).

Two buttons, two meanings:

* **Retry** continues the same conversation with a canned follow-up, `RETRY_MESSAGE`: the agent keeps its context,
  its branch and its sandbox (or checkpoint) and is told to re-run the check that failed and fix it. It is an
  ordinary follow-up (the same rules, budgets and admission), with its turn marked `origin: "retry"`.
* **Start over** (`?fresh=1`) submits a new job with the same task and settings, from a clean sandbox and the base
  branch; the old job stays as it ended. The new job is labelled `retry_of: <old id>`.

Both are audited (`job.retry`, with the mode) and counted (`hangar_job_retries{mode}`), so a retry is told apart from
a person's own follow-up in metrics, the audit log and the job's turns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hangar import autoinstall, experiments, metrics
from hangar.jobs import JobError, SubmitJob

if TYPE_CHECKING:
    from hangar.jobs import Caller, JobService
    from hangar.models import Job

RETRY_MESSAGE = "Re-run the failing check and fix what failed"
ORIGIN = "retry"


def retry(svc: JobService, job_id: str, caller: Caller, owner: str | None) -> Job:
    """Retry: the canned follow-up in the same conversation."""
    job = svc.follow_up(job_id, RETRY_MESSAGE, requester=caller.requester, owner=owner, origin=ORIGIN)
    svc.fleet.audit(caller.requester, "job.retry", job.id, mode="same", turn=len(job.turns))
    metrics.job_retry("same")
    return job


def start_over(svc: JobService, job_id: str, caller: Caller, owner: str | None) -> Job:
    """Start over: a new job with the old one's task and settings."""
    old = svc.get(job_id, owner)
    if old.parent:
        old = svc.get(old.parent, owner)
    if old.state.value not in ("succeeded", "failed", "cancelled"):
        raise JobError(f"job {old.id} is {old.state.value}; cancel it first, or wait for it to finish")
    if experiments.is_experiment(old) or autoinstall.is_setup(old):
        raise JobError(f"{old.id} can't be started over from here; submit it again")
    if old.untrusted_source:
        raise JobError(f"{old.id} came from outside the team ({old.untrusted_source}); start it again where it "
                       "came from, so its approval applies")
    if old.source == "changeset" or old.changeset:
        raise JobError(f"{old.id} is part of a change set; submit the change set again")
    req = SubmitJob(
        instructions=old.instructions, title=old.title, repo=old.repo, base_branch=old.base_branch,
        environment=old.environment, harness=old.harness, model=old.model, provider=old.provider,
        verify=old.verify if old.verify_source == "job" else None, deliver=old.deliver,
        context=[c for c in old.context if c.content is not None or c.url],
        labels={**{k: v for k, v in old.labels.items() if k != "retry_of"}, "retry_of": old.id},
        timeout_minutes=old.timeout_minutes, keep_minutes=old.keep_minutes, budget=old.budget,
        job_budget=old.job_budget, output_schema=old.output_schema, strategy=old.strategy,
        n=old.n if old.strategy == "best_of_n" else None, judge=old.judge, verify_in_loop=old.verify_in_loop,
        max_verify_rounds=old.max_verify_rounds, autofix=old.autofix, queue_class=old.queue_class,
    )
    new = svc.submit(req, caller, source="api")
    svc.fleet.audit(caller.requester, "job.retry", old.id, mode="fresh", new_job=new.id)
    metrics.job_retry("fresh")
    return new


def view(svc: JobService, job: Job, mode: str, of: str) -> dict[str, Any]:
    return {**svc.view(job), "retry": {"mode": mode, "of": of}}
