"""Who may release a job that waits for a trusted person, and the one place that decision is made (#121).

Every approval path asks here: MCP `approve_job` / `reject_job`, REST `/api/jobs/{id}/approve|reject` (and so
`hangar job approve`), Jira's and GitHub's `/hangar approve|reject`. Before #121 the MCP and REST twins disagreed
and nothing checked approver != requester.

* **Which tokens** (MCP, REST): a person's own login, that is a token holding `admin` or `user` (an OAuth grant acts
  as the login that approved it). `mcp`-only tokens (what `hangar connect` writes into client configs) and `jobs`
  service tokens can't release work, so a leaked client config or service key can't either.
* **Who may decide** (`can_decide`): a person with role `admin` (`rule="admin"`), or a linked person whose GitHub
  login is in the job repo's policy `approvers` (`"repo_approver"`). On a team job (no person: an outsider's
  trigger, an inbound webhook) a front door's own trust rule also counts (`"front_door"`): at GitHub a linked
  account or one the repo trusts (author association, `HANGAR_GITHUB_TRUSTED`), at Jira a linked account
  (docs/team.md#who-is-trusted). A person's own held work (a spent daily cap) is never a front door's to release.
  In-process callers (hangar's own code, tests: no principal) decide as `"system"`, as they may everywhere else.
* **Never your own** (approvals; a rejection is always fine): refused when the approver is the job's person, or one
  of the approver's identities (MCP actor, linked accounts, the commenting account) is the job's requester or
  `approval.requested_by`. `HANGAR_SELF_APPROVAL=allow_solo` lets a self-approval through while exactly one person
  exists (`rule="solo"`, audited `self_approved: true`); the default is `deny`.
* **Through a relay** (--- relay, `auth.resolve_delegation`): a bot's request on behalf of a linked person decides as
  that person, only when the relay token holds `relay_approve`, and counts as a front door (`"front_door"` on team
  jobs, like Slack's: the bot vouches for the account). An account linked to nobody decides nothing. Never their
  own, and `allow_solo` doesn't apply: a bot can't release the work it was asked for by the person it names.
  Unlinked accounts' jobs are held for approval (`relay_hold`) when `HANGAR_RELAY_UNLINKED=approve`.
* **Visibility.** A job a principal may not see answers as missing, as `people.Guard` does for every other tool: a
  member sees their own jobs and, here, jobs in repos whose approvers they are.

Entry points, for callers and for what builds on this (#130's taint gates, part B):

* `decide(jobs, job_id, approver, decision)` (async) is how a front door approves or rejects a job: it looks up the
  approver's GitHub logins when the repo policy's `approvers` could matter, then `decide_now`. `decide_now` (sync)
  finds the job, checks visibility and state, asks `can_decide`, and either audits the refusal
  (`job.<decision>.refused`) and raises `ApprovalRefused`, or records the decision (`JobApproval.decided_person`,
  `self_approved`, `rule`; the same fields on the `job.approve` / `job.reject` audit entries).
* `can_decide(ctx, approver, subject, kind)` is the rule itself, with no side effects: it reads people and repo
  policies from `ctx.store` and the mode from `ctx.settings` (`ctx`: a JobService, a Fleet, anything with both).
  `subject` is whose work it is (`Subject.of_job`, or built by hand for an egress request or a landing). `kind` is
  `approve` or `reject` for jobs; `egress` and `land` are reserved for #130 and get the same self-approval rule as
  `approve`. `risk` is accepted and reserved for part B (#160: self-approval of low-risk packets). Resolve logins
  first (`with_logins`) when `approvers` should count.
* `Approver` is who is deciding: `from_principal` (MCP, REST), `from_github`, `from_jira`, `in_process`.

Part B (#172) swaps the decision source behind `can_decide` for Tower's policy decision point,
without touching callers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import httpx

import hangar.identity as identity
from hangar import people
from hangar.repopolicy import RepoPolicies

if TYPE_CHECKING:
    from hangar.auth import Principal
    from hangar.jobs import JobService
    from hangar.models import Job, JobApproval

log = logging.getLogger("hangar.approvals")

Kind = Literal["approve", "reject", "egress", "land"]
Rule = Literal["admin", "repo_approver", "front_door", "solo", "system"]
SELF_APPROVAL_MODES = ("deny", "allow_solo")
# --- relay: set to True by the submit path once it attaches relay_hold(...) to a held relay principal's job. Until
# then auth.resolve_delegation refuses unlinked accounts under either HANGAR_RELAY_UNLINKED mode (fail closed: an
# unheld team job from an unverified account must never run).
HOLD_SUBMIT = False
_VERB = {"approve": "approve", "reject": "reject", "egress": "approve egress for", "land": "land"}


class ApprovalRefused(identity.Forbidden):
    """The approver may not make this decision. REST answers 403 with the text; MCP reports the same text."""


class Approver(NamedTuple):
    """Who is deciding.

    `actor`: what `decided_by` and the audit log show. `person`: the hangar person (None: an account linked to
    nobody, or an in-process caller). `via`: the surface; principals (`mcp`, `rest`) are subject to visibility and
    the token rule. `identities`: the strings the job's requester may be recorded as (`github:<login>`,
    `jira:<account>`, an MCP actor); the person's linked accounts are added by `can_decide`. `github_logins`
    (lowercase): matched against the repo policy's `approvers`; None until `with_logins` looks them up.
    `front_door`: a front door's own trust rule admitted them (team jobs only). `token`: why a principal's token
    can't decide, or None when it can."""

    actor: str
    person: str | None = None
    via: Literal["mcp", "rest", "github", "jira", "system"] = "system"
    identities: frozenset[str] = frozenset()
    github_logins: frozenset[str] | None = None
    front_door: bool = False
    token: str | None = None
    relay: bool = False  # --- relay: decided through a relay bot (never self-approved, not even allow_solo)


class Subject(NamedTuple):
    """Whose work a decision is about: its person (None for a team job), who asked for it, its repo."""

    person: str | None
    requesters: frozenset[str]
    repo: str | None = None
    what: str = "this job"

    @classmethod
    def of_job(cls, job: Job) -> Subject:
        asked = {job.requester} | ({job.approval.requested_by} if job.approval else set())
        return cls(job.person, frozenset(a for a in asked if a and a != "system"), job.repo, f"job {job.id}")


class Decision(NamedTuple):
    ok: bool
    why: str = ""
    rule: Rule | None = None
    self_approved: bool = False


# ---------------------------------------------------------------------------- building an approver
def from_principal(p: Principal | None, via: Literal["mcp", "rest"] = "mcp") -> Approver:
    """The MCP or REST caller (`identity.current`). Without one (an in-process caller): `in_process`."""
    if p is None:
        return in_process("system")
    name = identity.actor_name(p)
    if p.via is not None:  # --- relay
        why = None
        if p.person is None:
            why = "an account linked to no hangar person decides nothing"
        elif not p.may_approve:
            why = f"approving or rejecting jobs through a relay needs a relay_approve token; {p.via} holds relay only"
        ids = frozenset({name} | ({p.on_behalf_of} if p.on_behalf_of else set()))
        return Approver(name, p.person, via, ids, front_door=p.person is not None, token=why, relay=True)
    token = None if (p.admin or "user" in p.scopes) else (
        f"approving or rejecting jobs needs a person's login token (scope admin or user); this one has "
        f"{sorted(p.scopes)}")
    return Approver(name, p.person, via, frozenset({name}), token=token)


def in_process(actor: str) -> Approver:
    """Hangar's own code and tests deciding with no principal set. A network caller always has one; if one is set,
    it decides instead."""
    p = identity.current.get()
    return from_principal(p) if p is not None else Approver(actor, None, "system", frozenset({actor}))


def from_github(login: str, person: str | None, trusted: bool) -> Approver:
    """A `/hangar approve|reject` comment: the sender's login, the person their user id is linked to, and whether
    the GitHub door trusts them (linked, author association, `HANGAR_GITHUB_TRUSTED`)."""
    key = f"github:{login}"
    return Approver(key, person, "github", frozenset({key}), frozenset({login.lower()}), trusted)


def from_jira(account: str, person: str) -> Approver:
    """A linked Jira account's `/hangar approve|reject` comment (an unlinked account never decides)."""
    return Approver(f"jira:{person}", person, "jira", frozenset({f"jira:{account}"}), front_door=True)


async def with_logins(jobs: JobService, approver: Approver, repo: str | None) -> Approver:
    """`approver` with its person's linked GitHub accounts' logins, looked up by user id with the control plane's
    token, when they could matter: the repo's policy names approvers and the person isn't an admin. A lookup that
    fails counts as no login (fail closed)."""
    if approver.github_logins is not None:  # a GitHub comment: the commenting account is the one that counts
        return approver
    person = people.get(jobs.store, approver.person) if approver.person else None
    policy = jobs.policies.get(repo) if repo else None
    ids = [k.split(":", 1)[1] for k in (person.links if person else []) if k.startswith("github:")]
    known: set[str] = set()
    if person is None or person.role == "admin" or not (policy and policy.approvers) or not ids:
        return approver._replace(github_logins=frozenset(known))
    from hangar.github import GitHubError

    gh = jobs._github()
    if gh is None:
        log.warning("can't look up %s's GitHub login for the repo policy approvers: no GitHub token", person.id)
        return approver._replace(github_logins=frozenset(known))
    try:
        for uid in ids:
            try:
                user = await gh._req("GET", f"/user/{uid}")
            except (GitHubError, httpx.HTTPError) as e:
                log.warning("could not look up GitHub user %s for %s: %s", uid, person.id, e)
                continue
            if isinstance(user, dict) and isinstance(user.get("login"), str):
                known.add(user["login"].lower())
    finally:
        await gh.close()
    return approver._replace(github_logins=frozenset(known))


# ---------------------------------------------------------------------------- the rule
def is_repo_approver(store: Any, approver: Approver, repo: str | None) -> bool:
    """A linked person whose GitHub login is in `repo`'s policy `approvers`."""
    if not (approver.person and approver.github_logins and repo):
        return False
    policy = RepoPolicies(store).get(repo)
    return bool(policy and approver.github_logins & {a.lower() for a in policy.approvers or []})


def can_decide(ctx: Any, approver: Approver, subject: Subject, kind: Kind = "approve", *,
               risk: str | None = None) -> Decision:
    """Whether `approver` may make a `kind` decision on `subject`, by which rule, and if not why: the text every
    surface shows. No side effects."""
    del risk  # reserved for part B (#160)
    store = ctx.store
    verb = f"{approver.actor} may not {_VERB[kind]} {subject.what}"
    if approver.token:
        return Decision(False, f"{verb}: {approver.token}")
    person = people.get(store, approver.person) if approver.person else None
    if approver.person and person is None:
        return Decision(False, f"{verb}: person '{approver.person}' no longer exists")
    ids = approver.identities | set(person.links if person else [])
    own = kind != "reject" and ((person is not None and subject.person == person.id) or bool(ids & subject.requesters))
    rule: Rule | None = None
    if approver.via == "system" and approver.person is None:
        rule = "system"
    elif person is not None and person.role == "admin":
        rule = "admin"
    elif is_repo_approver(store, approver, subject.repo):
        rule = "repo_approver"
    elif approver.front_door and subject.person is None:
        rule = "front_door"
    if rule is None:
        who = f"one of {people_repo(subject.repo)}'s approvers (repo policy)" if subject.repo else \
            "a repo policy approver"
        return Decision(False, f"{verb}: only an admin or {who} may" + (", and nobody approves their own work"
                                                                         if own else ""), None, own)
    if not own:
        return Decision(True, "", rule)
    if approver.relay:  # --- relay
        return Decision(False, f"{verb}: it is {approver.actor}'s own work, and nobody approves their own, not "
                               "through a relay either", None, True)
    if getattr(ctx.settings, "self_approval", "deny") == "allow_solo" and len(people.list_people(store)) == 1:
        return Decision(True, "", "solo", True)
    return Decision(False, f"{verb}: it is {approver.actor}'s own work, and nobody approves their own. Ask another "
                           "admin or a repo policy approver (HANGAR_SELF_APPROVAL=allow_solo allows it while only "
                           "one person exists)", None, True)


def people_repo(repo: str | None) -> str:
    """`owner/name` for a repo URL, for messages."""
    from hangar.repopolicy import repo_key

    return repo_key(repo) or str(repo)


# ---------------------------------------------------------------------------- deciding on a job
async def decide(jobs: JobService, job_id: str, approver: Approver,
                 decision: Literal["approve", "reject"], reason: str | None = None) -> Job:
    """Approve or reject a job waiting for approval: `with_logins`, then `decide_now`. `reason` is the decider's
    words: kept on the decision and told to the requester."""
    job = jobs.get(job_id)
    return decide_now(jobs, job.id, await with_logins(jobs, approver, job.repo), decision, reason)


def decide_now(jobs: JobService, job_id: str, approver: Approver, decision: Literal["approve", "reject"],
               reason: str | None = None) -> Job:
    """Raises JobNotFound for a job the principal may not see, JobError when it isn't waiting, ApprovalRefused
    (audited as `job.<decision>.refused`) when the rule says no."""
    from hangar.jobs import JobError, JobNotFound
    from hangar.models import JobState

    job = jobs.get(job_id)
    if approver.via in ("mcp", "rest") and not _sees(jobs, approver, job):
        raise JobNotFound(f"no job '{job_id}'")
    if job.state != JobState.AWAITING_APPROVAL:
        raise JobError(f"job {job.id} is {job.state}, not waiting for approval")
    d = can_decide(jobs, approver, Subject.of_job(job), decision)
    if not d.ok:
        jobs.admission.refused(f"owner:{approver.actor}", approver.actor, f"job.{decision}.refused", job.id,
                               why=d.why, decided_person=approver.person, self_approved=d.self_approved)
        raise ApprovalRefused(d.why)
    _record(jobs, job, approver, decision, d, (reason or "").strip() or None)
    return job


def _sees(jobs: JobService, approver: Approver, job: Job) -> bool:
    """people.visible for the principal, plus jobs in repos it approves for."""
    p = identity.current.get()
    if p is None or p.role == "admin" or (job.person is not None and job.person == p.person):
        return True
    return is_repo_approver(jobs.store, approver, job.repo)


def _record(jobs: JobService, job: Job, approver: Approver, decision: str, d: Decision,
            reason: str | None = None) -> None:
    from hangar.models import JobApproval, JobState, now
    from hangar.triggers import _drop, _github_ref

    who = approver.actor
    a = job.approval or JobApproval(requested_by=job.requester)
    job.approval = a.model_copy(update={
        "decided_by": who, "decided_at": now(), "decision": "approved" if decision == "approve" else "rejected",
        "decided_person": approver.person, "self_approved": d.self_approved, "rule": d.rule,
        "decision_reason": reason})
    detail = {"requested_by": a.requested_by, "decided_person": approver.person, "self_approved": d.self_approved,
              "rule": d.rule}
    if reason:
        detail["reason"] = reason[:300]
    said = f": {reason}" if reason else ""
    if decision == "approve":
        jobs._event(job, f"approved by {who}" + (" (self-approved: HANGAR_SELF_APPROVAL=allow_solo)"
                                                  if d.self_approved else "") + said, JobState.QUEUED, reason=None)
        jobs.fleet.audit(who, "job.approve", job.id, **detail)
        jobs._queue.put_nowait(job.id)
        if ref := _github_ref(job):
            jobs._spawn(jobs._comment(job, ref, f"Approved by {who}{said}. Hangar job `{job.id}` is queued."))
    else:
        _drop(jobs, job, f"rejected by {who}{said}", "rejected",
              f"Rejected by {who}{said}. Hangar job `{job.id}` will not run.")
        jobs.fleet.audit(who, "job.reject", job.id, **detail)


# ---------------------------------------------------------------------------- --- relay: held jobs
def relay_hold(jobs: Any, p: Principal | None) -> JobApproval | None:
    """The approval a held relay principal's job must wait on (`JobService.submit(..., approval=)`), or None for
    anyone else. Requested by the relayed account (`symphony:<id>`), so linking it later to a person doesn't let them
    approve it."""
    if p is None or not p.held:
        return None
    from hangar.triggers import new_approval

    return new_approval(jobs, p.on_behalf_of or p.name, f"asked through {p.via} by {p.on_behalf_of}, an account "
                                                        "linked to no hangar person")


# ---------------------------------------------------------------------------- hangar doctor
def doctor_checks(h: Any) -> list[dict[str, Any]]:
    """Warn when self-approval is denied and only one person exists: nobody can approve that person's held jobs."""
    from hangar.doctor import _check

    if getattr(h.settings, "self_approval", "deny") != "deny" or len(people.list_people(h.store)) != 1:
        return []
    return [_check("self-approval", None,
                   "HANGAR_SELF_APPROVAL=deny and only one person exists, so nobody can approve that person's own "
                   "held jobs (a spent daily cap holds their new ones)",
                   "add a second person (hangar person add), or: hangar config set HANGAR_SELF_APPROVAL allow_solo")]
