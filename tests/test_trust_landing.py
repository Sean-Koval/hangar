"""--- trust (#96 finding 7): arming auto-merge needs the base branch to dismiss stale approvals on a push, unless the
repo policy opts out. On build/ops hangar armed regardless; a write-access push that hangar's webhook missed could
then merge unreviewed."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from hangar import landing
from hangar.jobs import handle_github
from hangar.repopolicy import PolicyUpdate
from hangar.repopolicy import update as policy_update
from tests.test_landing import GH, HEAD, approval, fx, gh, green, lands, pr_job, settle, svc  # noqa: F401


def _no_stale_dismissal(svc, gh: GH) -> None:  # noqa: F811
    """Classic protection that doesn't dismiss stale approvals, and a ruleset without a pull_request rule."""
    gh.protection = {"required_pull_request_reviews": {"required_approving_review_count": 1,
                                                       "dismiss_stale_reviews": False,
                                                       "require_last_push_approval": True}}
    rules = [r for r in fx("branch_protection")["rules"] if r["type"] != "pull_request"]
    real = gh.__call__

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/repos/o/r/rules/branches/main":
            return httpx.Response(200, json=rules)
        return real(r)

    svc.transport = httpx.MockTransport(handler)


async def test_arming_needs_stale_approvals_dismissed(svc, gh):  # noqa: F811
    lands(svc, approvers=["alice"])
    job = pr_job(svc)
    _no_stale_dismissal(svc, gh)
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert not gh.enables() and gh.armed is None, [e.message for e in job.events]
    assert "dismiss stale approvals" in job.landing.note and "require-stale-dismissal" in job.landing.note
    # the protection can't be read at all: deny by default
    gh.protection = 403
    svc.landing._cache.clear()
    svc.transport = httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "Forbidden"})
                                        if r.url.path.startswith(("/repos/o/r/rules/", "/repos/o/r/branches/main/p"))
                                        else gh(r))
    await svc.landing.evaluate(job)
    assert not gh.enables() and "couldn't read" in job.landing.note
    # the repo policy opts out explicitly: it lands
    policy_update(svc, PolicyUpdate(repo="o/r", require_stale_dismissal=False), "admin")
    _no_stale_dismissal(svc, gh)
    await svc.landing.evaluate(job)
    assert gh.enables() == [HEAD] and job.landing.armed_sha == HEAD, [e.message for e in job.events]


async def test_doctor_fails_landing_without_stale_dismissal_and_names_the_fix(svc, gh, settings):  # noqa: F811
    h = SimpleNamespace(jobs=svc, settings=settings)
    lands(svc, approvers=["alice"])
    _no_stale_dismissal(svc, gh)
    c = {c["name"]: c for c in await landing.doctor_checks(h)}["landing o/r"]
    assert c["status"] == "fail" and "won't arm auto-merge" in c["detail"], c
    assert "dismiss stale approvals" in c["fix"] and "--no-require-stale-dismissal" in c["fix"]
    policy_update(svc, PolicyUpdate(repo="o/r", require_stale_dismissal=False), "admin")
    c = {c["name"]: c for c in await landing.doctor_checks(h)}["landing o/r"]
    assert c["status"] == "warn" and "opted out" in c["detail"], c
