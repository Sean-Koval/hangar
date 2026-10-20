"""The "needs you" inbox (--- landing): everything waiting on one person, in one list, delivered to them.

What waits on a human used to be spread over GitHub comments, Slack threads and MCP elicitations. The inbox gathers
it from the records hangar already keeps, per person (#88: your own work; admins also get the work nobody in
particular owns, such as jobs from GitHub issues and Slack):

* `review`: your agent's PR is open and waits for your review (or, approved with `land: manual`, for your merge);
* `question`: an agent asked something (a job in `needs_input`, a session's open question);
* `approval`: a job waits for approval (an untrusted trigger, a spend cap), a turn waits on a tool approval, and
  whatever other modules add through `SOURCES` (egress and secret requests);
* `failed`: a job failed in the last week;
* `denied`: policy denied a step of a turn that is still running (KAN-140): the agent carries on, and the person
  decides whether to steer it.

A `failed` or `denied` item (or a `review`) can be acknowledged per person, `POST /api/inbox/{id}/ack` (`DELETE` takes
it back): it leaves that person's list for as long as the failed window, `FAILED_DAYS`. An `approval` or a `question`
can't be: it waits for a decision, and only the decision clears it.

Each item says what to do and with which command, carries a stable `id` (the same wait is the same item however
often the inbox is read) and `links` to the native object the action happens on: the PR, the ticket (Jira or a
GitHub issue), the Slack thread, the job on the dashboard (#157).

Delivery (inbox_notify.py): a `question` or `approval` reaches its person in a Slack DM within a minute, once per
item; everything else waits for their twice-daily digest (#158). `review` items are never sent one by one: GitHub's
review queue stays the place for them. MCP clients read `hangar://inbox` (and can subscribe: it changes when a job
or session does), the CLI runs `hangar inbox`, REST serves `GET /api/inbox`.
"""

from __future__ import annotations

import hashlib
import json
import logging
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
ACKABLE = ("failed", "denied", "review", "alert", "abandon")  # what a person may put away; a decision is not
ACKS_PREFIX = "inbox:acks:"  # kv, per person: {item id: until}

def yours(person: str | None, p: Principal | None) -> bool:
    """A record owned by `person` needs the caller: it is theirs, or nobody's and they are an admin. In-process
    callers (None) get everything."""
    if p is None:
        return True
    return person == p.person if person is not None else p.role == "admin"


def item_id(kind: str, ref: dict[str, Any], reason: str = "") -> str:
    """#157: a stable digest of what an item waits on: its kind, the job, session or request (or other record) it is
    about, and the approval it is or the question it asks. Reading the inbox again gives the same id; a new question
    on the same job, or another approval in the same turn, a new one."""
    what = next((f"{k}={ref[k]}" for k in ("request", "job", "session", "issue", "credential", "pr")
                 if ref.get(k) is not None), "")
    extra = str(ref.get("approval") or "")
    if not extra and kind in ("question", "alert"):
        extra = hashlib.sha256(reason.encode()).hexdigest()[:16]
    if not what and not extra:  # an item only its text tells apart
        extra = hashlib.sha256(reason.encode()).hexdigest()[:16]
    return "inb-" + hashlib.sha256(f"{kind}|{what}|{extra}".encode()).hexdigest()[:16]


def _item(kind: str, reason: str, action: str, since: float, **ref: Any) -> dict[str, Any]:
    ref = {k: v for k, v in ref.items() if v is not None}
    return {"kind": kind, "reason": reason, "action": action, "since": since, **ref,
            "id": item_id(kind, ref, reason)}


# ====================================================================== native links (#157)
LINKS = ("pr", "ticket", "thread", "job")
PERMALINK_PREFIX = "slack:permalink:"  # kv: chat.getPermalink's answer per thread (inbox_notify fills it)


def thread_of(job: Any) -> str | None:
    """The Slack thread a job talks in (`slack:<channel>:<ts>`): where it started, or where its last turn reports."""
    refs = [job.source_ref, *(t.reply_to for t in reversed(job.turns))]
    return next((r for r in refs if isinstance(r, str) and r.startswith("slack:")), None)


def thread_url(store: Any, ref: str | None) -> str | None:
    """A Slack thread's permalink: Slack's own (chat.getPermalink, cached by the notifier), else the archive URL Slack
    redirects to the workspace's."""
    from hangar.slack import parse_ref

    where = parse_ref(ref or "")
    if where is None:
        return None
    if cached := store.kv_get(PERMALINK_PREFIX + ref):
        return cached
    return f"https://slack.com/archives/{where[0]}/p{where[1].replace('.', '')}"


def ticket_url(svc: JobService, job: Any) -> str | None:
    """The ticket a job came from: a Jira issue (`intake.tracker` `jira:ABC-12` -> `<jira url>/browse/ABC-12`) or a
    GitHub issue (a `github:owner/repo#N` source, or an issue a Projects item carries)."""
    import re

    from hangar.github import parse_repo
    from hangar.intake import TRACKER

    labels = job.labels or {}
    tracker = labels.get(TRACKER) or ""
    key = labels.get("jira.issue") or (tracker.split(":", 1)[1] if tracker.startswith("jira:") else "")
    if key and re.match(r"^[A-Z][A-Z0-9_]{0,49}-[1-9][0-9]{0,9}$", key):
        base = str((svc.settings.jira or {}).get("url") or "").rstrip("/")
        return f"{base}/browse/{key}" if base.startswith("https://") else None
    m = re.match(r"^github:([\w.-]+/[\w.-]+)#([1-9][0-9]{0,9})$", job.source_ref or "")
    if m:
        return f"https://github.com/{m.group(1)}/issues/{m.group(2)}"
    number, repo = labels.get("github.issue"), parse_repo(job.repo) if job.repo else None
    if number and str(number).isdigit() and repo:
        return f"https://github.com/{repo}/issues/{number}"
    return None


def job_url(svc: JobService, job_id: str | None) -> str | None:
    """The job on the dashboard."""
    return f"{svc.settings.public_url.rstrip('/')}/#jobs?job={job_id}" if job_id else None


def links(svc: JobService, item: dict[str, Any]) -> dict[str, str | None]:
    """`links` of an item: {pr, ticket, thread, job}, each the native URL or None."""
    job = svc._jobs.get(item["job"]) if item.get("job") else None
    if job is None and item.get("job"):
        try:
            job = svc.get(item["job"])
        except Exception:  # noqa: BLE001 - gone: the item keeps its other links
            job = None
    pr = item.get("pr") or (job.result.pr_url if job is not None else None)
    return {"pr": pr if isinstance(pr, str) and pr.startswith("https://") else None,
            "ticket": ticket_url(svc, job) if job is not None else None,
            "thread": thread_url(svc.store, thread_of(job)) if job is not None else None,
            "job": job_url(svc, item.get("job"))}


def native(item: dict[str, Any]) -> str | None:
    """Where an item is answered: the conversation it was asked in, the ticket, the PR, else the job."""
    ls = item.get("links") or {}
    return next((ls[k] for k in ("thread", "ticket", "pr", "job") if ls.get(k)), None)


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
    jobs = [j for j in {x.id: x for x in [*svc._jobs.values(), *failed]}.values() if yours(j.person, p)
            and j.source != "shadow"]  # --- #183: a shadow never reaches anyone's inbox
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
                             j.updated_at, expires_at=j.approval.expires_at if j.approval else None, **ref))
        elif j.state == JobState.FAILED and j.updated_at > now() - FAILED_DAYS * 86400 and not pr_open and \
                st.pr_state not in ("merged", "closed") and j.source != "review":  # --- #161: the packet says so
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
    # --- self-approval (#160): a PR its requester's own approval doesn't land waits on the repo's approvers (the
    # people linked to them; admins when none is linked)
    for j in svc._jobs.values():
        st, r = j.landing, j.result
        if not (st.needs_second and r.pr_number and st.pr_state == "open" and not st.ended):
            continue
        if p is not None and not (p.person in st.approver_people if st.approver_people else p.role == "admin"):
            continue
        if p is not None and p.person == j.person:
            continue
        out.append(_item("review", f"PR #{r.pr_number} needs a second reviewer: {j.person}'s own approval doesn't "
                                   f"land it" + (f" ({st.note})" if st.note else ""), f"review and approve {r.pr_url}",
                         j.updated_at, risk=st.risk, job=j.id, title=j.title, repo=j.repo, pr=r.pr_url))
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
                             session=s.name, job=s.job_id, expires_at=a.expires_at, approval=a.id))
        if (d := denial_of(s)) is not None:  # KAN-140: policy stopped a step; the agent went on without it
            out.append(_item("denied", f"policy denied {d.tool or 'a tool call'}: {d.detail[:300]}"
                             + (f" ({d.message})" if d.message else ""),
                             f"hangar session send {s.name} '<what to do instead>' (or hangar job follow-up "
                             f"{s.job_id})" if s.job_id else f"hangar session send {s.name} '<what to do instead>'",
                             d.decided_at or d.requested_at, session=s.name, job=s.job_id, approval=d.id,
                             tool=d.tool))
        if s.needs_input and not s.job_id and s.id not in job_sessions:
            out.append(_item("question", f"the agent asks: {s.needs_input[:500]}",
                             f"hangar session send {s.name} '<your answer>'", s.last_active_at, session=s.name))
    for source in SOURCES:
        out += source(svc, p)
    out.sort(key=lambda i: i.get("since") or 0, reverse=True)
    for x in out:  # #157: a source's items too
        x.setdefault("id", item_id(x["kind"], x, str(x.get("reason") or "")))
    if p is not None and (acked := acks(svc.store, person_key(p))):  # KAN-140: what this person put away
        out = [x for x in out if x["id"] not in acked]
    out = out[:MAX_ITEMS]
    for x in out:
        x["links"] = links(svc, x)
    return out


def denial_of(s: Any) -> Any:
    """The latest policy denial in a session's running turn, or None: the agent is still going, so the person
    may want to steer it; a finished turn's denials are history."""
    t = s.turns[-1] if s.turns else None
    if t is None or t.finished_at or not t.live:
        return None
    return next((a for a in reversed(t.approvals) if a.status == "denied" and a.by == "policy"), None)


# ====================================================================== acknowledgements (KAN-140)
def person_key(p: Principal) -> str:
    """Whose acks: the person, else the token (a service token without one)."""
    return p.person or f"token:{p.id}"


def acks(store: Any, who: str) -> dict[str, float]:
    """The item ids `who` put away, each with when the ack lapses (the failed window), expired ones dropped."""
    raw = store.kv_get_json(ACKS_PREFIX + who, {}) or {}
    t = now()
    return {k: float(v) for k, v in raw.items() if isinstance(v, int | float) and v > t}


def ack(svc: JobService, p: Principal, item_id_: str, on: bool = True) -> dict[str, Any]:
    """Put an item away for this person (`on`), or bring it back. The item must be in their inbox now, and of a
    kind that can be put away: a decision (approval, question) can't."""
    who = person_key(p)
    current = acks(svc.store, who)
    if not on:
        current.pop(item_id_, None)
        svc.store.kv_set_json(ACKS_PREFIX + who, current)
        _changed(svc)
        return {"id": item_id_, "acked": False}
    if item_id_ in current:
        return {"id": item_id_, "acked": True, "until": current[item_id_]}
    item = next((x for x in items(svc, p) if x["id"] == item_id_), None)
    if item is None:
        raise KeyError(f"no inbox item '{item_id_}'")
    if item["kind"] not in ACKABLE:
        raise ValueError(f"a {item['kind']} waits for your decision and can't be acknowledged; decide it")
    current[item_id_] = now() + FAILED_DAYS * 86400
    svc.store.kv_set_json(ACKS_PREFIX + who, current)
    svc.fleet.audit(f"ui:{p.name}", "inbox.ack", item_id_, kind=item["kind"], job=item.get("job"),
                    session=item.get("session"))
    _changed(svc)
    return {"id": item_id_, "acked": True, "until": current[item_id_]}


def _changed(svc: Any) -> None:
    """An ack changed someone's inbox: tell whoever listens on the service (`inbox_listeners`, set by the fleet
    stream, fleet_stream.Changes)."""
    for fn in list(getattr(svc, "inbox_listeners", ())):
        try:
            fn()
        except Exception:  # noqa: BLE001 - a listener never fails an ack
            logging.getLogger("hangar.inbox").exception("inbox listener failed")


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

    # KAN-140: put a failed or denied item away (per person, for the failed window); a decision can't be
    @app.post("/api/inbox/{item_id}/ack")
    async def inbox_ack(item_id: str, _: str = person_actor):
        p = identity.principal()
        assert p is not None
        return ack(h.jobs, p, item_id, on=True)

    @app.delete("/api/inbox/{item_id}/ack")
    async def inbox_unack(item_id: str, _: str = person_actor):
        p = identity.principal()
        assert p is not None
        return ack(h.jobs, p, item_id, on=False)


def register(mcp: Any, jobs: JobService | None) -> None:
    """`hangar://inbox`: a resource, not a tool, so it costs nothing in the tool list and clients can subscribe."""
    if jobs is None:
        return

    @mcp.resource(URI, name="inbox", mime_type="application/json",
                  description="What needs you: PRs waiting for your review, agents' questions, approvals and failed "
                              "jobs, newest first. Subscribe to it to hear when it changes.")
    async def inbox_resource() -> str:
        return json.dumps(view(jobs), indent=1, default=str)
