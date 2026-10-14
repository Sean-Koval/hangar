"""Who may start an auto-fix review round on a job's PR (--- trust: #91's open Medium; docs/team.md#who-is-trusted).

A review round runs in the job's session, with its person's own login and on their budget, so a review counts
only from someone that person or the repo's policy trusts with it:
* the job's person, through their linked GitHub account, or an admin's linked account;
* a login in the repo policy's `approvers`, or a code owner of every path the PR touches (`landing.qualifies`, the
  rule that lets an approval land the PR);
* on a team job (no person), also anyone the GitHub front door trusts: a linked teammate, or the author
  association or HANGAR_GITHUB_TRUSTED, as for issues.
Bots never do, hangar's own App (`<slug>[bot]`) included: their reviews are another program's output.

Whoever it is, their text reaches the agent fenced as untrusted data and labelled with its author
(autofix._review_context): what to do with it is hangar's instruction, never the reviewer's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hangar import people

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job


def is_bot(user: dict[str, Any]) -> bool:
    """A GitHub App or other bot account: `type: Bot`, or a `[bot]` login (what Apps comment and review as)."""
    return user.get("type") == "Bot" or str(user.get("login") or "").lower().endswith("[bot]")


async def why_trusted(svc: JobService, job: Job, user: dict[str, Any], association: Any) -> str | None:
    """Why this reviewer may start a fix round on `job` ("the job's person", "an admin", ...), or None."""
    login = str(user.get("login") or "")
    if not login or is_bot(user):
        return None
    uid = user.get("id")
    linked = people.resolve_external(svc.store, "github", uid) \
        if isinstance(uid, int) and not isinstance(uid, bool) else None  # by id: a renamed login can be re-registered
    if linked is not None:
        if job.person and linked.id == job.person:
            return "the job's person"
        if linked.role == "admin":
            return "an admin"
        if job.person is None:
            return f"linked to {linked.id}"
    if job.person is None and await svc.trust.trusted(login, association if isinstance(association, str) else None):
        return "trusted on GitHub"
    return await svc.landing.qualifies(job, login)  # the policy's approvers, or a code owner of all it touches
