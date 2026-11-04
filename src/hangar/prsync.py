"""PR state from GitHub's API when its webhooks don't arrive (KAN-246).

hangar learns that a job's PR merged or closed from the `pull_request` webhook (landing.py). An install whose webhook
was never registered, or whose deliveries fail, never hears it: the PR stays open in hangar's records, the review cap
(`fairness.ReviewLoad`) counts it, new jobs wait as `awaiting_capacity`, and the weekly numbers are wrong.
`redeliver.py` replays missed deliveries only when a webhook exists.

So a pass reads each PR hangar still has as open or approved (GET /repos/{repo}/pulls/{n}) and, when GitHub says it is
merged or closed, records it through the webhook's own path (`Landing._pull_request`): the landing events, the audit
line, the review load and the end of the job's session are exactly a webhook's. A pass runs at startup and every
HANGAR_GITHUB_PR_RECONCILE_MINUTES. It is idempotent: a PR it recorded is no longer open, so a second pass skips it,
and a late webhook for it is ignored as already recorded. What it corrected is kept for `hangar doctor`, where a
correction means the webhook is missing or failing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import httpx

from hangar.github import GitHub, GitHubError, parse_repo
from hangar.models import now

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job

log = logging.getLogger("hangar.prsync")

KV_LAST = "github:prsync:last"  # the last pass: {"at", "checked", "corrected", "errors", "prs": [...]}
KV_SEEN = "github:prsync:corrected"  # every pass that corrected any: {"total", "last_at", "last_prs"}
MAX_PER_PASS = 200  # PRs read per pass; the rest wait for the next one
START_DELAY = 5.0  # seconds after startup, as redeliver.py
RECENT = 7 * 86400  # doctor warns while a correction is this recent


def pending(svc: JobService) -> list[Job]:
    """Jobs whose PR hangar still has as open or approved (every one is in memory: JobService._pinned)."""
    out = []
    for job in list(svc._jobs.values()):
        st = job.landing
        if job.result.pr_number and parse_repo(job.repo) and st.pr_state in ("open", "approved") and not st.ended:
            out.append(job)
    return sorted(out, key=lambda j: (parse_repo(j.repo) or "", j.result.pr_number or 0))[:MAX_PER_PASS]


async def _apply(svc: JobService, job: Job, pr: dict[str, Any]) -> str | None:
    """Record what GitHub says about the job's PR, as its webhook would have. The new state, or None (unchanged)."""
    if pr.get("state") != "closed":
        return None
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    out = svc.landing._pull_request(job, "closed", pr, head.get("sha") if isinstance(head.get("sha"), str) else None,
                                    {})
    if not out.get("ok"):
        return None
    svc._event(job, f"PR #{job.result.pr_number} {out['pr_state']} on GitHub with no webhook telling hangar: "
                    "recorded from GitHub's API (the PR reconcile)")
    if pr.get("merged"):  # --- autofix/conflicts: the webhook's other half, a merge moved the base for the rest
        from hangar import autofix

        repo = parse_repo(job.repo) or ""
        await autofix.handle(svc, "pull_request", f"prsync:{repo}#{job.result.pr_number}",
                             {"action": "closed", "pull_request": pr, "repository": {"full_name": repo}})
    return str(out["pr_state"])


async def reconcile(svc: JobService) -> dict[str, Any]:
    """One pass. Returns and keeps {"at", "checked", "corrected", "errors", "prs"}."""
    jobs = pending(svc)
    report: dict[str, Any] = {"at": now(), "checked": 0, "corrected": 0, "errors": 0, "prs": []}
    clients: dict[str, GitHub | None] = {}
    try:
        for job in jobs:
            repo = parse_repo(job.repo) or ""
            if repo not in clients:
                clients[repo] = await svc.landing.client(repo)
            gh = clients[repo]
            if gh is None:  # no GitHub credential at all: nothing to read with
                report["errors"] += 1
                continue
            number = int(job.result.pr_number or 0)
            try:
                pr = await gh.pull(repo, number)
            except (GitHubError, httpx.HTTPError) as e:
                report["errors"] += 1
                log.warning("could not read %s#%d for the PR reconcile: %s", repo, number, e)
                continue
            report["checked"] += 1
            if job.landing.pr_state not in ("open", "approved") or job.landing.ended:
                continue  # a webhook got there while we read
            if state := await _apply(svc, job, pr):
                report["corrected"] += 1
                report["prs"].append(f"{repo}#{number} {state}")
    finally:
        for gh in clients.values():
            if gh is not None:
                with contextlib.suppress(Exception):
                    await gh.close()
    svc.store.kv_set_json(KV_LAST, report)
    if report["corrected"]:
        seen = svc.store.kv_get_json(KV_SEEN, {}) or {}
        svc.store.kv_set_json(KV_SEEN, {"total": int(seen.get("total") or 0) + report["corrected"],
                                         "last_at": report["at"], "last_prs": report["prs"][:20]})
        svc.fleet.audit("system", "github.prsync", None, corrected=report["corrected"], prs=report["prs"][:20])
        log.warning("the PR reconcile recorded %d PR(s) no webhook reported: %s", report["corrected"],
                    ", ".join(report["prs"][:20]))
    return report


def doctor_check(svc: JobService) -> dict[str, Any] | None:
    """`hangar doctor`: how many PRs the last pass corrected, and whether any webhook went missing this week. None
    where hangar has no GitHub credential (it opens no PRs to reconcile)."""
    from hangar.doctor import _check

    s = svc.settings
    if not (s.github_token_credential or s.github_app_credential):
        return None
    if not s.github_pr_reconcile_minutes:
        return _check("github PR reconcile", None, "off (HANGAR_GITHUB_PR_RECONCILE_MINUTES=0): a merge no webhook "
                      "reports keeps counting against the review cap",
                      "set HANGAR_GITHUB_PR_RECONCILE_MINUTES (default 15)")
    last = svc.store.kv_get_json(KV_LAST, None)
    if not last:
        return _check("github PR reconcile", None, "no pass has run yet (one runs at startup)")
    seen = svc.store.kv_get_json(KV_SEEN, {}) or {}
    detail = (f"last pass checked {last.get('checked', 0)} open PR(s) and corrected {last.get('corrected', 0)}"
              + (f", {last['errors']} could not be read" if last.get("errors") else ""))
    if seen.get("last_at") and now() - float(seen["last_at"]) < RECENT:
        return _check("github PR reconcile", None,
                      f"{detail}; {seen.get('total', 0)} PR(s) corrected so far, most recently "
                      + ", ".join(seen.get("last_prs") or []) + ": GitHub's webhook is missing or failing",
                      "hangar setup github   (registers the webhook; the control token needs Webhooks read and "
                      "write), then check its deliveries on GitHub")
    return _check("github PR reconcile", True if not last.get("errors") else None, detail,
                  "check the GitHub credential can read the repos' pull requests")


class PRSync:
    """The pass at startup and every HANGAR_GITHUB_PR_RECONCILE_MINUTES."""

    def __init__(self, svc: JobService):
        self.svc = svc
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.svc.settings.github_pr_reconcile_minutes and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(0 if self.svc.transport else START_DELAY)
        while True:
            try:
                await reconcile(self.svc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next pass tries again
                log.exception("the PR reconcile failed")
            await asyncio.sleep(max(60.0, self.svc.settings.github_pr_reconcile_minutes * 60))


def of(svc: JobService) -> PRSync:
    p = svc.__dict__.get("_prsync")
    if p is None:
        p = svc.__dict__["_prsync"] = PRSync(svc)
    return p
