"""KAN-246: PR state from GitHub's API when its webhooks don't arrive, and the Jira key on MCP jobs.

A PR merged while hangar had no webhook is recorded as merged by one reconcile pass, the way its webhook would have,
and a job held for review capacity starts. A second pass, or the late webhook, changes nothing. `submit_job` with no
`ticket` takes the one Jira key its title or instructions name; two keys give none and an event saying why."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from hangar import fairness, intake, prsync
from hangar.jobs import JobService, handle_github
from hangar.models import CredentialKind, Person
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures
from tests.test_landing import fx, pr_job, settle
from tests.test_review_capacity import merged, new_job, take

CAP = 5


class GitHubPRs:
    """GET /repos/o/r/pulls/{n}: each PR as GitHub has it now (open unless the test merged or closed it)."""

    def __init__(self):
        self.prs: dict[int, dict] = {}
        self.reads: list[int] = []
        self.missing: set[int] = set()

    def pr(self, number: int, state: str = "open", merged_by: str | None = None) -> dict:
        p = {"number": number, "state": state, "merged": merged_by is not None, "draft": False,
             "node_id": f"PR_{number}", "head": {"sha": "6dcb09b5b57875f334f61aebed695e2e4193db5e"},
             "base": {"ref": "main"}, "mergeable": True, "mergeable_state": "clean"}
        if merged_by:
            p |= {"merged_at": "2026-11-01T15:00:00Z", "merged_by": {"login": merged_by},
                  "merge_commit_sha": "e5bd3914e2e596debea16f433f57875b5b90bcd6", "closed_at": "2026-11-01T15:00:00Z"}
        elif state == "closed":
            p["closed_at"] = "2026-11-01T16:00:00Z"
        self.prs[number] = p
        return p

    def __call__(self, r: httpx.Request) -> httpx.Response:
        parts = r.url.path.split("/")
        if r.method == "GET" and r.url.path.startswith("/repos/o/r/pulls/") and len(parts) == 6:
            n = int(parts[-1])
            self.reads.append(n)
            if n in self.missing:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self.prs.get(n) or self.pr(n))
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def gh() -> GitHubPRs:
    return GitHubPRs()


@pytest.fixture
def svc(fleet, settings, broker, gh) -> JobService:
    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_control")
    settings.github_token_credential, settings.github_repos = "gh", {"o/r": "jobenv"}
    settings.person_max_open_agent_prs = CAP
    settings.autofix_coalesce_seconds = 0  # the conflict checks a merge queues run now, not a minute later
    s = JobService(fleet, None, transport=httpx.MockTransport(gh))
    s.store.put(Person(id="alice", name="alice"))
    return s


def open_prs(svc: JobService, n: int, first: int = 425) -> list:
    out = []
    for i in range(n):
        job = pr_job(svc, person="alice", requester="alice")
        job.result.pr_number, job.result.pr_url = first + i, f"https://github.com/o/r/pull/{first + i}"
        svc._save(job)
        out.append(job)
    return out


async def test_a_pr_merged_with_no_webhook_is_recorded_and_a_held_job_starts(svc, gh):
    prs = open_prs(svc, CAP)
    held = new_job(svc, "alice")
    assert await take(svc) is None and held.reason == fairness.AWAITING
    gh.pr(425, "closed", merged_by="sean")  # merged on GitHub; no webhook ever came
    report = await prsync.reconcile(svc)
    await settle(svc)
    assert report["checked"] == CAP and report["corrected"] == 1 and report["prs"] == ["o/r#425 merged"]
    st = prs[0].landing
    assert st.pr_state == "merged" and st.merged_by == "sean" and st.merge_commit_sha.startswith("e5bd39")
    assert any(e.message == "PR #425 merged by @sean" for e in prs[0].events)  # the webhook's own event
    assert any("recorded from GitHub's API (the PR reconcile)" in e.message for e in prs[0].events)
    assert fairness.open_agent_prs(svc, "alice") == CAP - 1
    assert await take(svc) == held.id  # the held job starts
    assert all(p.landing.pr_state == "open" for p in prs[1:])
    # the webhook's other half: a merge moved main, so the other open PRs get a conflict check
    assert all(any(k.startswith("conflict:425:") for k in p.autofix_state.seen) for p in prs[1:])

    # again, and then the real webhook, late: nothing changes
    events = [len(p.events) for p in prs]
    again = await prsync.reconcile(svc)
    assert again["corrected"] == 0 and 425 not in gh.reads[CAP:]  # a recorded PR isn't read again
    late = merged(425)
    res = await handle_github(svc, "pull_request", "d-late", late)
    await settle(svc)
    assert res.get("ignored") == "PR already recorded as merged", res
    assert [len(p.events) for p in prs] == events
    assert fairness.open_agent_prs(svc, "alice") == CAP - 1


async def test_a_closed_pr_is_recorded_and_unreadable_ones_are_left_alone(svc, gh):
    shut, kept, gone = open_prs(svc, 3, first=500)
    gh.pr(500, "closed")
    gh.missing.add(502)
    report = await prsync.reconcile(svc)
    assert (report["corrected"], report["errors"], report["checked"]) == (1, 1, 2)
    assert shut.landing.pr_state == "closed" and shut.landing.closed_at is not None
    assert any("closed without merging" in e.message for e in shut.events)
    assert kept.landing.pr_state == "open" and gone.landing.pr_state == "open"


async def test_a_webhook_first_then_the_pass_changes_nothing(svc, gh):
    [job] = open_prs(svc, 1, first=7)
    p = fx("pull_request.closed.merged")
    await handle_github(svc, "pull_request", "d-merged", p)
    await settle(svc)
    assert job.landing.pr_state == "merged"
    n = len(job.events)
    gh.pr(7, "closed", merged_by="octocat")
    report = await prsync.reconcile(svc)
    assert report["checked"] == 0 and report["corrected"] == 0 and len(job.events) == n


async def test_doctor_says_how_many_the_last_pass_corrected(svc, gh, settings):
    assert prsync.doctor_check(svc)["detail"].startswith("no pass has run yet")
    open_prs(svc, 2)
    await prsync.reconcile(svc)
    ok = prsync.doctor_check(svc)
    assert ok["status"] == "ok" and "checked 2 open PR(s) and corrected 0" in ok["detail"]
    gh.pr(426, "closed", merged_by="sean")
    await prsync.reconcile(svc)
    warn = prsync.doctor_check(svc)
    assert warn["status"] == "warn" and "o/r#426 merged" in warn["detail"]
    assert "webhook is missing or failing" in warn["detail"] and "hangar setup github" in warn["fix"]
    await prsync.reconcile(svc)  # a quiet pass this week still shows the correction
    assert prsync.doctor_check(svc)["status"] == "warn"
    settings.github_pr_reconcile_minutes = 0
    assert "off" in prsync.doctor_check(svc)["detail"]
    settings.github_token_credential = None
    assert prsync.doctor_check(svc) is None


async def test_the_loop_runs_a_pass_at_startup(svc, gh):
    [job] = open_prs(svc, 1)
    gh.pr(425, "closed", merged_by="sean")
    loop = prsync.of(svc)
    loop.start()
    for _ in range(100):
        if job.landing.pr_state == "merged":
            break
        await settle(svc)
        await asyncio.sleep(0.01)
    await loop.stop()
    assert job.landing.pr_state == "merged"


# ---------------------------------------------------------------------- the Jira key on MCP jobs
JIRA = {"url": "https://acme.atlassian.net", "email": "hangar@acme.com", "token_credential": "jira-api",
        "webhook_secret_credential": "jira-hook", "projects": {"KAN": {"repo": "o/r"}, "PAY": {"repo": "o/r"}},
        "trigger": {"status": "Ready for agent"}}


def test_the_one_configured_key_in_the_text_is_the_ticket(settings):
    settings.jira = JIRA
    assert intake.ticket_from_text(settings, None, "Do KAN-123: make apply atomic")[0] == "jira:KAN-123"
    assert intake.ticket_from_text(settings, "KAN-123: apply", "see KAN-123 and UTF-8")[0] == "jira:KAN-123"
    assert intake.ticket_from_text(settings, None, "branch hangar/KAN-35-job-1")[0] == "jira:KAN-35"
    none, note = intake.ticket_from_text(settings, None, "KAN-1 then PAY-2")
    assert none is None and "KAN-1, PAY-2" in note
    assert intake.ticket_from_text(settings, None, "ABC-9 isn't ours, nor xKAN-4") == (None, None)
    settings.jira = {}
    assert intake.ticket_from_text(settings, None, "KAN-123") == (None, None)  # no Jira configured


async def _submit(fleet, svc, args: dict) -> dict:
    from mcp import Client

    from hangar.mcp_server import build_mcp

    async with Client(build_mcp(fleet, svc)) as c:
        res = await c.call_tool("submit_job", args)
        return json.loads(res.content[0].text)


async def test_mcp_submit_job_takes_the_key_its_instructions_name(jobs, world, fleet, settings):  # noqa: F811
    settings.jira = JIRA
    base = {"repo": world["url"], "environment": "jobenv", "deliver": "branch"}
    one = jobs.get((await _submit(fleet, jobs, {**base, "instructions": "Do KAN-123: make apply atomic"}))["id"])
    assert one.branch == f"hangar/KAN-123-{one.id}" and one.title.startswith("KAN-123: ")
    assert any("ticket KAN-123" in e.message for e in one.events)
    two = jobs.get((await _submit(fleet, jobs, {**base, "instructions": "KAN-123 and KAN-124 together"}))["id"])
    assert two.branch == f"hangar/{two.id}" and two.ticket is None
    assert any("several Jira keys (KAN-123, KAN-124)" in e.message for e in two.events)
    given = jobs.get((await _submit(fleet, jobs, {**base, "instructions": "KAN-1", "ticket": "jira:PAY-9"}))["id"])
    assert given.branch == f"hangar/PAY-9-{given.id}"  # an explicit ticket wins
    one = await _until(jobs, one.id, DONE)
    assert "\nJira: KAN-123\n" in jobs._pr_body(one)
    two = await _until(jobs, two.id, DONE)
    assert "Jira:" not in jobs._pr_body(two)
    await _until(jobs, given.id, DONE)
