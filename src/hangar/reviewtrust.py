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

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hangar import people

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job

log = logging.getLogger("hangar.autofix")

# --- review (#101): what needs GitHub runs off the webhook request (GitHub waits 10 s for an answer), a few at a
# time, and a verdict holds for a reviewer on a job's head commit for a while, so a flood of comments costs one check.
CHECKS = 4
CHECK_SECONDS = 120.0
VERDICT_SECONDS = 300.0


def is_bot(user: dict[str, Any]) -> bool:
    """A GitHub App or other bot account: `type: Bot`, or a `[bot]` login (what Apps comment and review as)."""
    return user.get("type") == "Bot" or str(user.get("login") or "").lower().endswith("[bot]")


def quick(svc: JobService, job: Job, user: dict[str, Any], association: Any = None) -> str | None:
    """Why this reviewer may start a round, from what hangar knows without asking GitHub; None: ask (why_trusted)."""
    from hangar.triggers import DEFAULT_TRUSTED

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
    if job.person is None and not svc.trust.configured and isinstance(association, str) \
            and association.upper() in DEFAULT_TRUSTED:  # GitHubTrust's rule without HANGAR_GITHUB_TRUSTED
        return "trusted on GitHub"
    policy = svc.policies.get(job.repo)
    if policy and login.lower() in {a.lower() for a in policy.approvers or []}:
        return "approver in the repo policy"
    return None


async def why_trusted(svc: JobService, job: Job, user: dict[str, Any], association: Any) -> str | None:
    """Why this reviewer may start a fix round on `job` ("the job's person", "an admin", ...), or None."""
    login = str(user.get("login") or "")
    if not login or is_bot(user):
        return None
    if why := quick(svc, job, user, association):
        return why
    if job.person is None and await svc.trust.trusted(login, association if isinstance(association, str) else None):
        return "trusted on GitHub"
    # the policy's approvers, or a code owner of all it touches (--- taint, #130: who may ask for a fix round, not who
    # may land, so not the tainted-landing rule)
    return await svc.landing._qualifies(job, login)


def check_later(svc: JobService, job_id: str, user: dict[str, Any], association: Any,
                then: Callable[[str], Any], refused: Callable[[], Any]) -> None:
    """--- review (#101): why_trusted in the background, not tied to the webhook request (#75); then `then(why)`
    or `refused()`. A restart in between loses the review (GitHub has its 2xx); the reviewer comments again."""
    svc._spawn(_check(svc, job_id, user, association, then, refused))


async def _check(svc: JobService, job_id: str, user: dict[str, Any], association: Any,
                 then: Callable[[str], Any], refused: Callable[[], Any]) -> None:
    job = svc._jobs.get(job_id)
    if job is None:
        return
    if not hasattr(svc, "_review_trust"):
        svc._review_trust = (asyncio.Semaphore(CHECKS), {})
    slots, verdicts = svc._review_trust
    key = (job_id, str(user.get("login") or "").lower(), job.result.head_sha)
    hit = verdicts.get(key)
    if hit is not None and hit[0] > time.monotonic():
        why = hit[1]
    else:
        async with slots:
            try:
                why = await asyncio.wait_for(why_trusted(svc, job, user, association), CHECK_SECONDS)
            except Exception as e:  # noqa: BLE001 - GitHub down or slow: not trusted, and not remembered
                log.warning("auto-fix: couldn't check whether %s may review job %s: %s", key[1], job_id,
                            type(e).__name__)
                refused()
                return
        if len(verdicts) > 1000:
            verdicts.clear()
        verdicts[key] = (time.monotonic() + VERDICT_SECONDS, why)
    then(why) if why else refused()
