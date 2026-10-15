"""The "needs you" inbox (--- landing): everything waiting on one person, in one list.

What waits on a human used to be spread over GitHub comments, Slack threads and MCP elicitations. The inbox gathers
it from the records hangar already keeps, per person (#88: your own work; admins also get the work nobody in
particular owns, such as jobs from GitHub issues and Slack):

* `review`: your agent's PR is open and waits for your review (or, approved with `land: manual`, for your merge);
* `question`: an agent asked something (a job in `needs_input`, a session's open question);
* `approval`: a job waits for approval (an untrusted trigger, a spend cap), a turn waits on a tool approval, and
  whatever other modules add through `SOURCES` (egress and secret requests);
* `failed`: a job failed in the last week.

It is read, not acted on: each item says what to do and with which command. MCP clients read `hangar://inbox` (and
can subscribe: it changes when a job or session does), the CLI runs `hangar inbox`, REST serves `GET /api/inbox`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hangar import identity, steer
from hangar.models import JobState, now
from hangar.store import NOT_NULL, Ge  # --- durable

if TYPE_CHECKING:
    from hangar.auth import Principal
    from hangar.jobs import JobService

URI = "hangar://inbox"
FAILED_DAYS = 7
MAX_ITEMS = 200

def yours(person: str | None, p: Principal | None) -> bool:
    """A record owned by `person` needs the caller: it is theirs, or nobody's and they are an admin. In-process
    callers (None) get everything."""
    if p is None:
        return True
    return person == p.person if person is not None else p.role == "admin"


def _item(kind: str, reason: str, action: str, since: float, **ref: Any) -> dict[str, Any]:
    return {"kind": kind, "reason": reason, "action": action, "since": since,
            **{k: v for k, v in ref.items() if v is not None}}


def env_requests(svc: JobService, p: Principal | None) -> list[dict[str, Any]]:
    """--- integration (#94 + #96): a secret or a host an agent asked for (envrequests.py) waits on its owner, or on
    an admin when it has none. Never granted by itself, so it sits here until someone answers it."""
    from hangar.envrequests import EnvRequest

    out = []
    for r in svc.store.list_where(EnvRequest, state="pending"):
        if not yours(r.owner, p):
            continue
        what = f"the secret {r.name}" if r.kind == "secret" else f"network access to {r.target}"
        out.append(_item("approval", f"the agent asks for {what}: {r.why}"[:500],
                         f"hangar requests approve {r.id} (or deny {r.id} --note '<why>')", r.created_at,
                         request=r.id, session=r.session, job=r.job_id))
    return out


# More kinds of request a person answers: fn(svc, principal or None) -> items. A source decides what the caller may
# see; an item is {"kind", "reason", "action", "since", ...}. Egress and secret requests are the first.
SOURCES: list[Callable[[JobService, Principal | None], list[dict[str, Any]]]] = [env_requests]


def items(svc: JobService, p: Principal | None = None) -> list[dict[str, Any]]:
    """What waits on the caller (`p`, or identity's current principal), newest first."""
    p = p if p is not None else identity.principal()
    out: list[dict[str, Any]] = []
    # --- durable: unfinished jobs and PRs on their way are in memory; recent failures are asked of the store
    failed = svc.where(state=JobState.FAILED, updated_at=Ge(now() - FAILED_DAYS * 86400))
    jobs = [j for j in {x.id: x for x in [*svc._jobs.values(), *failed]}.values() if yours(j.person, p)]
    job_sessions: set[str] = set()
    for j in jobs:
        st, r = j.landing, j.result
        ref = {"job": j.id, "title": j.title, "repo": j.repo, "pr": r.pr_url}
        if j.session_id:
            job_sessions.add(j.session_id)
        pr_open = bool(r.pr_number) and st.pr_state == "open"
        if j.state == JobState.NEEDS_INPUT and r.question:
            out.append(_item("question", f"the agent asks: {r.question[:500]}",
                             f"hangar job follow-up {j.id} '<your answer>' (or reply /hangar <answer> where it "
                             "asked)", j.updated_at, **ref))
        elif j.state == JobState.AWAITING_APPROVAL:
            why = (j.approval.reason if j.approval else "") or "it waits for a trusted person"
            out.append(_item("approval", f"job waits for approval: {why}", f"hangar job approve {j.id} (or reject)",
                             j.updated_at, **ref))
        elif j.state == JobState.FAILED and j.updated_at > now() - FAILED_DAYS * 86400 and not pr_open and \
                st.pr_state not in ("merged", "closed"):
            out.append(_item("failed", f"job failed ({j.reason or 'error'})",
                             f"hangar job get {j.id}; then follow up or submit it again", j.updated_at, **ref))
        if pr_open and j.id not in svc._active and j.state in (JobState.SUCCEEDED, JobState.FAILED):
            draft = " (a draft: hangar wasn't sure; read the packet)" if st.draft else ""
            if j.state == JobState.FAILED:
                draft += f" (the job ended {j.reason or 'with an error'})"
            out.append(_item("review", f"PR #{r.pr_number} waits for your review{draft}" +
                             (f": {st.note}" if st.note else ""), f"review and approve {r.pr_url}",
                             st.approved_at or j.updated_at, risk=st.risk, **ref))
        elif r.pr_number and st.pr_state == "approved" and not st.armed_sha and st.note:
            out.append(_item("review", f"PR #{r.pr_number} is approved but not landing: {st.note}",
                             f"merge it, or fix what holds it: {r.pr_url}", st.approved_at or j.updated_at, **ref))
    # --- durable: a live turn's approvals are in memory; an open question can be anywhere
    sessions = [*svc.sessions._sessions.values(), *svc.sessions.where(needs_input=NOT_NULL)] \
        if svc.sessions is not None else []
    for s in {x.id: x for x in sessions}.values():
        owner = s.person
        if s.job_id:
            job = svc._jobs.get(s.job_id)
            owner = job.person if job is not None else s.person
        if not yours(owner, p):
            continue
        for a in steer.pending(s):
            out.append(_item("approval", f"the agent wants to run {a.tool}: {a.detail[:300]}",
                             f"hangar session approvals {s.name} --allow {a.id} | --deny {a.id}", a.requested_at,
                             session=s.name, job=s.job_id, expires_at=a.expires_at))
        if s.needs_input and not s.job_id and s.id not in job_sessions:
            out.append(_item("question", f"the agent asks: {s.needs_input[:500]}",
                             f"hangar session send {s.name} '<your answer>'", s.last_active_at, session=s.name))
    for source in SOURCES:
        out += source(svc, p)
    out.sort(key=lambda i: i.get("since") or 0, reverse=True)
    return out[:MAX_ITEMS]


def summary(xs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for x in xs:
        counts[x["kind"]] = counts.get(x["kind"], 0) + 1
    return counts


def view(svc: JobService) -> dict[str, Any]:
    xs = items(svc)
    p = identity.principal()
    return {"person": p.person if p else None, "counts": summary(xs), "items": xs}


# ====================================================================== front doors
def mount(app: Any, h: Any, person_actor: Any) -> None:
    """GET /api/inbox: an admin's token, or a person's own `user` token."""

    @app.get("/api/inbox")
    async def inbox_route(_: str = person_actor):
        return view(h.jobs)


def register(mcp: Any, jobs: JobService | None) -> None:
    """`hangar://inbox`: a resource, not a tool, so it costs nothing in the tool list and clients can subscribe."""
    if jobs is None:
        return

    @mcp.resource(URI, name="inbox", mime_type="application/json",
                  description="What needs you: PRs waiting for your review, agents' questions, approvals and failed "
                              "jobs, newest first. Subscribe to it to hear when it changes.")
    async def inbox_resource() -> str:
        return json.dumps(view(jobs), indent=1, default=str)
