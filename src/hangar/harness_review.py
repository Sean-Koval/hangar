"""The Friday harness review (--- #169; docs/runbooks/harness-review.md): label the first failure of failed and
abandoned jobs, until 20 of them are candidate regression tasks for flightdeck.

* **The queue** (`hangar review queue [--since 7d]`, `GET /api/review/queue`): agent jobs (weekly.agent_jobs: not
  experiment trials, not review jobs) changed in the window that failed, whose PR closed unmerged (#168), or that
  took two or more correction steers (#162), newest first, without a label. `infra_error` jobs (#166) are left out
  unless asked for. Each row links the job, its trace and its session's event timeline, with the first `error` or
  failed tool result as the suggested `seq`.
* **A label** (`hangar review label <job> --seq N --category … --fix … --note … [--task]`,
  `POST /api/jobs/{id}/failure-label`): a `FailureLabel` on the job (one per job; relabelling replaces it and is
  audited `review.relabel`, the first one `review.label`), shown on the job view and as `hangar.failure.*` on the
  trace's `job` span, and exported as a `label` job event through the outbox (`v1/jobs/{date}/{job}/events/{ts}-label
  .json`) for flightdeck's task builder: references (base SHA, ticket ref, PR) and the redacted ticket text.
* **The counter** (`hangar review status`): candidate tasks toward GOAL, labels by category. At GOAL the queue says
  "20 reached: hand over to flightdeck"; labelling still works.

A labelled job counts as a failure for retention (outbox.failure, #189): its runs stay until exported.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from typing import TYPE_CHECKING, Any, get_args

from pydantic import BaseModel, Field

from hangar.models import FailureLabel, Job, JobState, now

if TYPE_CHECKING:
    from hangar.jobs import JobService

GOAL = 20
CORRECTIONS = 2  # correction steers that put a job in the queue
MAX_ROWS = 100
TICKET_CHARS = 4000
CATEGORIES = get_args(FailureLabel.model_fields["category"].annotation)
FIXES = get_args(FailureLabel.model_fields["fix"].annotation)
REACHED = f"{GOAL} reached: hand over to flightdeck"


class ReviewError(ValueError):
    pass


class LabelBody(BaseModel):
    seq: int | None = Field(None, ge=0)
    category: str
    fix: str = "none"
    note: str = Field("", max_length=2000)
    candidate_task: bool = False


# ====================================================================== the queue
def _why(svc: JobService, job: Job) -> list[str]:
    from hangar import weekly

    out = []
    if job.state == JobState.FAILED:
        out.append("failed")
    if weekly.closed_unmerged_at(job) is not None:
        out.append("abandoned")
    if weekly._steers(svc.store, job)[1] >= CORRECTIONS:
        out.append("corrections")
    return out


def first_failure(svc: JobService, job: Job) -> tuple[str | None, int | None, int | None]:
    """(session id, turn, seq) of the first `error` event or failed tool result in the job's main session: a
    suggestion for --seq, never decided for the reviewer."""
    from hangar import traces

    sid = job.session_id
    act = getattr(getattr(svc, "sessions", None), "activity", None)
    if not sid or act is None:
        return sid, None, None
    with contextlib.suppress(Exception):
        for ev in traces.read_events(act, sid, limit=5000):
            if ev.kind == "error" or (ev.kind == "tool_result" and ev.data.get("ok") is False):
                return sid, ev.turn or None, ev.seq or None
    return sid, None, None


def queue(svc: JobService, since: float, include_infra: bool = False, visible: Any = None) -> dict[str, Any]:
    from hangar import weekly

    url = svc.settings.public_url.rstrip("/")
    rows = []
    for j in sorted(weekly.agent_jobs(svc.store, now() - since), key=lambda x: -x.updated_at):
        if j.failure_label is not None or (visible is not None and not visible(j.person)):
            continue
        if weekly.infra(j) and not include_infra:
            continue
        why = _why(svc, j)
        if not why:
            continue
        sid, turn, seq = first_failure(svc, j)
        rows.append({"job": j.id, "title": j.title, "repo": j.repo, "person": j.person, "why": why,
                     "state": j.state.value, "reason": j.reason, "abandon_reason": j.landing.abandon_reason,
                     "outcome_class": j.result.outcome_class, "updated_at": j.updated_at, "pr": j.result.pr_url,
                     "session_id": sid, "suggested_turn": turn, "suggested_seq": seq,
                     "links": {"job": f"{url}/api/jobs/{j.id}", "trace": f"{url}/api/jobs/{j.id}/trace",
                               "timeline": (f"{url}/api/sessions/{sid}/events" + (f"?since={seq - 1}" if seq else ""))
                               if sid else None}})
        if len(rows) >= MAX_ROWS:
            break
    st = status(svc)
    return {"since_seconds": since, "rows": rows, "status": st, **({"note": REACHED} if st["reached"] else {})}


# ====================================================================== labels
def label(svc: JobService, job: Job, body: LabelBody, by: str) -> FailureLabel:
    """Store the job's label (replacing one it had: audited), on the job record, and export it through the outbox."""
    if body.category not in CATEGORIES:
        raise ReviewError(f"category must be one of: {', '.join(CATEGORIES)}")
    if body.fix not in FIXES:
        raise ReviewError(f"fix must be one of: {', '.join(FIXES)}")
    sid, turn, _ = first_failure(svc, job) if body.seq is None else (job.session_id, None, None)
    if body.seq is not None and job.session_id:
        turn = _turn_of(svc, job.session_id, body.seq)
    before = job.failure_label
    fl = FailureLabel(job_id=job.id, session_id=sid or job.session_id, turn=turn, seq=body.seq,
                      category=body.category, fix=body.fix, note=svc.broker.redact(body.note),  # type: ignore[arg-type]
                      candidate_task=body.candidate_task, labelled_by=by)
    job.failure_label = fl
    svc._save(job)
    svc._event(job, f"failure label: {fl.category} (fix: {fl.fix}" + (", candidate task" if fl.candidate_task else "")
               + (f", at seq {fl.seq}" if fl.seq is not None else "") + f") by {by}")
    if before is not None:
        svc.fleet.audit(by, "review.relabel", job.id, before=before.model_dump(mode="json"),
                        after=fl.model_dump(mode="json"))
    else:
        svc.fleet.audit(by, "review.label", job.id, label=fl.model_dump(mode="json"))
    export(svc, job, fl)
    return fl


def _turn_of(svc: JobService, sid: str, seq: int) -> int | None:
    from hangar import traces

    act = getattr(getattr(svc, "sessions", None), "activity", None)
    if act is None:
        return None
    with contextlib.suppress(Exception):
        return next((ev.turn for ev in traces.read_events(act, sid, limit=20_000) if ev.seq == seq), None)
    return None


def export(svc: JobService, job: Job, fl: FailureLabel) -> str | None:
    """The `label` job event (outbox): the label, and what flightdeck's task builder needs: references and the
    ticket text, redacted (it may hold customer data; the builder redacts again)."""
    from hangar import outbox

    data = {**fl.model_dump(mode="json"), "base_sha": job.result.base_sha, "head_sha": job.result.head_sha,
            "repo": job.repo, "ticket_ref": job.source_ref, "title": svc.broker.redact(job.title),
            "ticket": svc.broker.redact(job.instructions or "")[:TICKET_CHARS],
            "ticket_untrusted": bool(job.untrusted_source), "verify": job.verify,
            "pr_state": job.landing.pr_state, "abandon_reason": job.landing.abandon_reason}
    return outbox.of(svc.fleet).job_event(job, "label", fl.at, data, once=f"label:{fl.id}")


def labels(svc: JobService) -> list[Job]:
    from hangar.store import NOT_NULL

    return svc.store.list_where(Job, archived=True, failure_label=NOT_NULL)


def status(svc: JobService) -> dict[str, Any]:
    js = labels(svc)
    tasks = sum(1 for j in js if j.failure_label and j.failure_label.candidate_task)
    return {"labelled": len(js), "candidate_tasks": tasks, "goal": GOAL, "reached": tasks >= GOAL,
            "categories": dict(Counter(j.failure_label.category for j in js if j.failure_label).most_common()),
            "fixes": dict(Counter(j.failure_label.fix for j in js if j.failure_label).most_common())}


def trace_attrs(job: Job) -> dict[str, Any]:
    """`hangar.failure.*` on the trace's `job` span."""
    fl = job.failure_label
    if fl is None:
        return {}
    return {"hangar.failure.category": fl.category, "hangar.failure.fix": fl.fix, "hangar.failure.seq": fl.seq,
            "hangar.failure.turn": fl.turn, "hangar.failure.session_id": fl.session_id,
            "hangar.failure.note": fl.note[:500] or None, "hangar.failure.candidate_task": fl.candidate_task,
            "hangar.failure.labelled_by": fl.labelled_by, "hangar.failure.at": fl.at}


# ====================================================================== REST
def mount(app: Any, h: Any, person_actor: Any) -> None:
    """GET /api/review/queue?since=7d&include_infra=, GET /api/review/status, POST /api/jobs/{id}/failure-label.
    A member sees and labels their own jobs; an admin, everyone's."""
    from fastapi import HTTPException

    from hangar import identity, outbox, people

    @app.get("/api/review/queue")
    async def review_queue(since: str = "7d", include_infra: bool = False, _: str = person_actor):
        try:
            secs = outbox.parse_since(since)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return await asyncio.to_thread(queue, h.jobs, secs, include_infra, people.visible)

    @app.get("/api/review/status")
    async def review_status(_: str = person_actor):
        return await asyncio.to_thread(status, h.jobs)

    @app.post("/api/jobs/{job_id}/failure-label")
    async def failure_label(job_id: str, body: LabelBody, who: str = person_actor):
        job = h.jobs.get(job_id)
        if not people.visible(job.person):
            raise KeyError(f"no job '{job_id}'")
        p = identity.principal()
        by = (p.person if p and p.person else None) or who
        try:
            fl = label(h.jobs, job, body, by)  # on the loop: the job is saved and its event appended here
        except ReviewError as e:
            raise HTTPException(400, str(e)) from None
        st = status(h.jobs)
        return {"label": fl.model_dump(mode="json"), "status": st, **({"note": REACHED} if st["reached"] else {})}
