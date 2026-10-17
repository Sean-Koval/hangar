"""Landing (--- landing): agent PRs from a GitHub App, PR state from webhooks, approval lands the PR (the control
plane arms auto-merge for the approved commit only), the review packet, the end of a merged PR's session, and
schedules that update their open PR. GitHub is a mock fed recorded payloads (tests/fixtures/github)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hangar import landing
from hangar.github import MARKER
from hangar.jobs import Caller, JobService, SubmitJob, handle_github
from hangar.models import Artifact, CredentialKind, Job, JobState, ScheduledJob, Turn
from hangar.repopolicy import PolicyError, PolicyUpdate, RepoPolicy
from hangar.repopolicy import update as policy_update
from hangar.setup_api import HOOK_EVENTS
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures

FIX = Path(__file__).parent / "fixtures" / "github"
HEAD = "6dcb09b5b57875f334f61aebed695e2e4193db5e"  # the PR head in the recorded payloads
NEW = "9c3f2a1be04d6e5f7a8b9c0d1e2f3a4b5c6d7e8f"  # what the recorded synchronize pushed
NODE = "PR_kwDOMh3e8M59dZ65"


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def sub(payload: dict, old: str, new: str) -> dict:
    """A recorded payload about another commit."""
    return json.loads(json.dumps(payload).replace(old, new))


class GH:
    """api.github.com for o/r#7: the PR, its auto-merge (GraphQL), merges, comments, branch rules, CODEOWNERS and
    App installation tokens. `head` is the PR's head as GitHub has it."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.head = lambda: HEAD
        self.state, self.merged_sha = "open", None
        self.mergeable_state = "blocked"
        self.armed: str | None = None
        self.clean = False  # GitHub refuses to arm a PR that is already mergeable
        self.codeowners: str | None = None
        self.files = ["src/app.py"]
        self.comments: dict[int, str] = {}
        self.comment_ids = 900
        self.protection: int | dict = fx("branch_protection")["protection"]
        self.allow_auto_merge = True
        self.minted: list[dict] = []
        self.graphql: list[dict] = []
        self.reviews: list[dict] = []  # GitHub's own record of the PR's reviews, oldest first
        self.on_arm = None  # called while GitHub arms auto-merge: what happens on GitHub meanwhile
        self.disarm_fails = 0  # how many disable calls GitHub refuses

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.calls.append(r)
        p, m = r.url.path, r.method
        body = json.loads(r.content) if r.content else {}
        if p == "/repos/o/r/installation":
            return httpx.Response(200, json={"id": 42})
        if p == "/app/installations/42/access_tokens":
            self.minted.append(body)
            exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            return httpx.Response(201, json={"token": f"ghs_app{len(self.minted)}", "expires_at": exp})
        if p == "/graphql":
            self.graphql.append(body)
            q, v, g = body["query"], body["variables"], fx("graphql")
            if "enablePullRequestAutoMerge" in q:
                if self.clean:
                    return httpx.Response(200, json=g["enable_auto_merge_clean"])
                if v["sha"] != self.head():
                    return httpx.Response(200, json=g["enable_auto_merge_head_moved"])
                self.armed = v["sha"]
                if self.on_arm is not None:
                    self.on_arm()
                return httpx.Response(200, json=g["enable_auto_merge"])
            if self.disarm_fails:
                self.disarm_fails -= 1
                return httpx.Response(502, json={"message": "Server Error"})
            self.armed = None
            return httpx.Response(200, json=g["disable_auto_merge"])
        if p == "/repos/o/r/pulls/7" and m == "GET":
            return httpx.Response(200, json={
                "number": 7, "state": self.state, "merged": self.merged_sha is not None, "node_id": NODE,
                "draft": False, "head": {"sha": self.head(), "ref": "hangar/job-0001"}, "base": {"ref": "main"},
                "mergeable_state": self.mergeable_state,
                "auto_merge": {"merge_method": "squash"} if self.armed else None})
        if p == "/repos/o/r/pulls/7/merge" and m == "PUT":
            if body.get("sha") != self.head():
                return httpx.Response(409, json={"message": "Head branch was modified. Review and try the merge "
                                                            "again."})
            self.merged_sha = body["sha"]
            return httpx.Response(200, json={"sha": "a1f3c9e", "merged": True,
                                             "message": "Pull Request successfully merged"})
        if p == "/repos/o/r/pulls/7/files":
            per, page = int(r.url.params.get("per_page", 30)), int(r.url.params.get("page", 1))
            return httpx.Response(200, json=[  # a (new, old) pair is a rename
                {"filename": f, "status": "modified"} if isinstance(f, str) else
                {"filename": f[0], "previous_filename": f[1], "status": "renamed"}
                for f in self.files[(page - 1) * per:page * per]])
        if p == "/repos/o/r/pulls/7/reviews":
            per, page = int(r.url.params.get("per_page", 30)), int(r.url.params.get("page", 1))
            return httpx.Response(200, json=self.reviews[(page - 1) * per:page * per])
        if p == "/repos/o/r/pulls":  # none open for the branch yet; opening one makes #7
            pr = {"number": 7, "html_url": "https://github.com/o/r/pull/7", "node_id": NODE, "state": "open",
                  "draft": body.get("draft", False)}
            return httpx.Response(201, json=pr) if m == "POST" else httpx.Response(200, json=[])
        if p == "/repos/o/r/issues/7/comments" and m == "POST":
            self.comment_ids += 1
            cid = self.comment_ids
            self.comments[cid] = body["body"]
            return httpx.Response(201, json={"id": cid})
        if p.startswith("/repos/o/r/issues/comments/") and m == "PATCH":
            cid = int(p.rsplit("/", 1)[1])
            if cid not in self.comments:
                return httpx.Response(404, json={"message": "Not Found"})
            self.comments[cid] = body["body"]
            return httpx.Response(200, json={"id": cid})
        if p == "/repos/o/r/branches/main/protection":
            if isinstance(self.protection, int):
                return httpx.Response(self.protection, json={"message": "Resource not accessible by integration"})
            return httpx.Response(200, json=self.protection)
        if p == "/repos/o/r/branches/main":
            return httpx.Response(200, json=fx("branch_protection")["branch"])
        if p == "/repos/o/r/rules/branches/main":
            return httpx.Response(200, json=fx("branch_protection")["rules"])
        if p == "/repos/o/r/contents/.github/CODEOWNERS" and self.codeowners is not None:
            return httpx.Response(200, json={"encoding": "base64", "path": ".github/CODEOWNERS",
                                             "content": base64.b64encode(self.codeowners.encode()).decode()})
        if p == "/repos/o/r":
            return httpx.Response(200, json={"full_name": "o/r", "default_branch": "main",
                                             "allow_auto_merge": self.allow_auto_merge})
        return httpx.Response(404, json={"message": "Not Found"})

    def enables(self) -> list[str]:
        return [g["variables"]["sha"] for g in self.graphql if "enablePullRequestAutoMerge" in g["query"]]

    def disables(self) -> int:
        return sum("disablePullRequestAutoMerge" in g["query"] for g in self.graphql)


async def settle(svc: JobService) -> None:
    """Wait for the background work webhooks started."""
    for _ in range(200):
        pending = [t for t in svc._background if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
def gh() -> GH:
    return GH()


@pytest.fixture
def svc(fleet, settings, broker, gh) -> JobService:
    """A job service with no sandboxes: what landing does on GitHub, driven by webhooks."""
    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_control")
    settings.github_token_credential, settings.github_repos = "gh", {"o/r": "jobenv"}
    return JobService(fleet, None, transport=httpx.MockTransport(gh))


def pr_job(svc: JobService, person: str | None = None, **kw) -> Job:
    job = Job(title="Fix the unicode bug in CSV export", instructions="x", environment="jobenv",
              repo="https://github.com/o/r.git", branch="hangar/job-0001", base_branch="main",
              state=JobState.SUCCEEDED, person=person, harness="claude-code", model="claude-opus-4-7", **kw)
    job.result.pr_number, job.result.pr_url, job.result.head_sha = 7, "https://github.com/o/r/pull/7", HEAD
    job.landing.pr_state, job.landing.head_sha, job.landing.node_id = "open", HEAD, NODE
    job.landing.push_mode = "gateway"  # its sandbox pushed through the git gateway, holding no GitHub token
    svc._jobs[job.id] = job
    svc.store.put(job)
    return job


def lands(svc: JobService, **kw) -> None:
    svc.policies.put(RepoPolicy(repo="o/r", land="auto_after_approval", **kw))


async def green(svc: JobService, sha: str = HEAD) -> None:
    for name in ("test", "lint"):  # what the recorded branch protection and ruleset require
        run = sub(fx("check_run.completed"), HEAD, sha)
        run["check_run"]["name"] = name
        await handle_github(svc, "check_run", f"d-{name}", run)  # autofix answers for it: not a failure
    await settle(svc)


def approval(sha: str = HEAD, login: str = "alice", gh: GH | None = None, state: str = "approved") -> dict:
    """A review's webhook; with `gh`, GitHub records the review too (hangar checks it there before arming)."""
    p = sub(fx("pull_request_review.submitted.approved"), HEAD, sha)
    p["review"]["user"]["login"] = p["sender"]["login"] = login
    p["review"]["state"] = state
    if gh is not None:
        p["review"]["id"] += len(gh.reviews)
        gh.reviews.append({"id": p["review"]["id"], "user": {"login": login}, "state": state.upper(),
                           "commit_id": sha})
    return p


def dismiss(gh: GH, login: str = "alice") -> None:
    """GitHub dismisses the reviewer's reviews (a stale approval on a push, or someone dismissing it)."""
    for r in gh.reviews:
        if r["user"]["login"] == login:
            r["state"] = "DISMISSED"


# ====================================================================== identity
@pytest.fixture(scope="module")
def app_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


async def test_agent_prs_come_from_the_app_with_the_least_permissions(svc, gh, broker, settings, app_pem):
    broker.put("hangar-app", "git", CredentialKind.GITHUB_APP, json.dumps({"app_id": "99", "private_key": app_pem}))
    settings.github_app_credential = "hangar-app"
    job = pr_job(svc)
    job.result.pr_number = None
    await svc._ensure_pr(job, draft=False)
    opened = [c for c in gh.calls if c.url.path == "/repos/o/r/pulls" and c.method == "POST"]
    assert opened and opened[0].headers["authorization"] == "Bearer ghs_app1"  # the App, not the control token
    assert gh.minted == [{"permissions": {"contents": "write", "pull_requests": "write"}, "repositories": ["r"]}]
    assert job.result.pr_number == 7 and job.landing.node_id == NODE and job.landing.pr_state == "open"
    # comments on the job's PR are the App's; on an issue, the control token's (the App can't write issues)
    await svc._comment(job, "github:o/r#7", "hello")
    await svc._comment(job, "github:o/r#12", "hello")
    sent = [c for c in gh.calls if c.method == "POST" and c.url.path.endswith("/comments")]
    assert [c.headers["authorization"] for c in sent] == ["Bearer ghs_app1", "Bearer ghp_control"]
    from hangar.identity import exposure

    assert exposure(broker.get("hangar-app"), settings) == "control_plane"  # never in a sandbox


def test_the_webhook_and_the_app_subscribe_to_landing_events():
    assert set(landing.EVENTS) <= set(HOOK_EVENTS) and "pull_request" in HOOK_EVENTS


# ====================================================================== state
async def test_pr_state_checks_and_time_from_green_to_merged(svc, gh):
    job = pr_job(svc)
    st = job.landing
    assert (await svc.landing.handle("check_suite", fx("check_suite.completed")))["value"] == "success"
    await green(svc)
    assert st.checks == {"suite:github-actions": "success", "test": "success", "lint": "success"}
    assert st.ci_green_sha == HEAD and st.ci_green_at
    st.ci_green_at = landing._ts("2026-10-14T09:55:41Z")  # as if the checks went green 10 minutes before
    merged = await handle_github(svc, "pull_request", "d-2", fx("pull_request.closed.merged"))
    assert merged["pr_state"] == "merged"
    assert st.merged_at == landing._ts("2026-10-14T10:05:41Z") and st.merged_by == "hangar-agents[bot]"
    assert st.green_to_merge_seconds == 600
    assert any(a.action == "landing.merged" for a in svc.store.audit_tail(20))
    # a check on another commit (a fork, or an old head) changes nothing
    other = sub(fx("check_run.completed"), HEAD, NEW)
    assert "not the PR's head" in (await svc.landing.handle("check_run", other))["ignored"]
    closed = pr_job(svc)
    closed.result.pr_number = 8
    p = fx("pull_request.closed.merged")
    p["number"] = p["pull_request"]["number"] = 8
    p["pull_request"]["merged"] = False
    assert (await handle_github(svc, "pull_request", "d-4", p))["pr_state"] == "closed"


# ====================================================================== approval lands the PR
async def test_an_approvers_approval_and_green_checks_arm_auto_merge_for_that_commit(svc, gh):
    lands(svc, approvers=["alice"], merge_method="squash")
    job = pr_job(svc)
    res = await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    assert res["landing"]["approval"] == "alice" and "nothing to fix" in res["ignored"]  # what each reader did
    await settle(svc)
    assert not gh.enables() and "waiting for required checks: lint, test" in job.landing.note
    await green(svc)
    assert gh.enables() == [HEAD] and gh.armed == HEAD and job.landing.armed_sha == HEAD
    arm = next(g for g in gh.graphql if "enable" in g["query"])
    assert {k: v for k, v in arm["variables"].items() if k != "body"} == {"id": NODE, "sha": HEAD, "method": "SQUASH"}
    assert arm["variables"]["body"].endswith(f"Hangar-Job: {job.id}")  # --- #137: the merged commit's trailers
    assert job.landing.pr_state == "approved" and job.landing.approved_by == "alice"
    assert any(a.action == "landing.arm" for a in svc.store.audit_tail(20))


async def test_who_may_approve(svc, gh):
    lands(svc, approvers=["alice"])
    job = pr_job(svc)
    await green(svc)
    # bob is neither an approver nor a code owner
    await handle_github(svc, "pull_request_review", "d-1", approval(login="bob"))
    await settle(svc)
    assert not gh.enables() and job.landing.pr_state == "open"
    assert any("isn't in the repo policy's approvers" in e.message for e in job.events)
    # an approval of an older commit doesn't count
    stale = approval(login="alice")
    stale["review"]["commit_id"] = "e5bd3914e2e596debea16f433f57875b5b90bcd6"
    assert "older commit" in (await svc.landing.handle("pull_request_review", stale))["ignored"]
    # a code owner of everything the PR touches does (CODEOWNERS is cached per base for a few minutes)
    gh.codeowners = "# owners\n*       @lead\nsrc/    @carol @o/maintainers\ndocs/*  @dave\n"
    svc.landing._cache.clear()
    await handle_github(svc, "pull_request_review", "d-3", approval(login="Carol", gh=gh))
    await settle(svc)
    assert gh.enables() == [HEAD] and job.landing.approved_by == "Carol", [e.message for e in job.events]


async def test_a_mergeable_pr_is_merged_with_the_approved_sha(svc, gh):
    lands(svc, approvers=["alice"], merge_method="rebase")
    job = pr_job(svc)
    gh.clean, gh.mergeable_state = True, "clean"  # no checks recorded, but GitHub says the required ones passed
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    merge = next(c for c in gh.calls if c.url.path == "/repos/o/r/pulls/7/merge")
    assert json.loads(merge.content) == {"sha": HEAD, "merge_method": "rebase"} and gh.merged_sha == HEAD
    assert any(a.action == "landing.merge" for a in svc.store.audit_tail(20))
    assert job.landing.armed_sha is None


async def test_a_job_whose_sandbox_held_a_github_token_isnt_landed(svc, gh):
    lands(svc, approvers=["alice"])
    job = pr_job(svc)
    job.landing.push_mode = "direct"  # the approval could be the agent's own, made with that token
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert not gh.enables() and "credential_mode: direct" in job.landing.note


async def test_manual_land_never_arms(svc, gh):
    svc.policies.put(RepoPolicy(repo="o/r", approvers=["alice"]))  # land unset: manual
    job = pr_job(svc)
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval())
    await settle(svc)
    assert not gh.enables() and job.landing.pr_state == "approved" and "land: manual" in job.landing.note


async def test_a_push_after_approval_disarms_and_needs_a_new_review(svc, gh):
    """The webhook path: someone (or something) pushes to the approved PR."""
    lands(svc, approvers=["alice"])
    job = pr_job(svc)
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert gh.armed == HEAD
    gh.head = lambda: NEW
    await handle_github(svc, "pull_request", "d-2", fx("pull_request.synchronize"))
    await settle(svc)
    st = job.landing
    assert gh.armed is None and gh.disables() == 1 and st.armed_sha is None
    assert st.pr_state == "open" and st.approved_sha is None and st.checks == {}
    # the new head goes green: still nothing lands without a new approval
    await green(svc, NEW)
    assert gh.enables() == [HEAD] and gh.merged_sha is None
    # GitHub dismissing the stale approval is fine too
    dismiss(gh)
    assert (await handle_github(svc, "pull_request_review", "d-3", fx("pull_request_review.dismissed")))["landing"]
    await handle_github(svc, "pull_request_review", "d-4", approval(NEW, gh=gh))
    await settle(svc)
    assert gh.enables() == [HEAD, NEW] and st.armed_sha == NEW


async def test_changes_requested_or_a_dismissal_hold_it(svc, gh):
    lands(svc, approvers=["alice"])
    job = pr_job(svc)
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert gh.armed == HEAD
    dismissed = fx("pull_request_review.dismissed")
    dismissed["pull_request"]["head"]["sha"] = HEAD
    dismiss(gh)
    await handle_github(svc, "pull_request_review", "d-2", dismissed)
    await settle(svc)
    assert gh.armed is None and job.landing.pr_state == "open"
    await handle_github(svc, "pull_request_review", "d-3", approval(gh=gh))
    await settle(svc)
    assert gh.armed == HEAD
    nope = approval(login="bob", gh=gh, state="changes_requested")
    await handle_github(svc, "pull_request_review", "d-4", nope)
    await settle(svc)
    assert gh.armed is None and job.landing.approved_sha is None


async def test_a_replayed_or_late_approval_that_github_no_longer_has_doesnt_arm(svc, gh):
    """Review webhooks can be replayed (only issue events are replay-guarded), redelivered out of order after a
    restart (redeliver.py), or dismissed while hangar is still checking who approved: GitHub's record decides."""
    lands(svc, approvers=["alice", "bob"])
    job = pr_job(svc)
    await green(svc)
    approved = approval(gh=gh)
    await handle_github(svc, "pull_request_review", "d-1", approved)
    await settle(svc)
    assert gh.enables() == [HEAD]
    dismissed = fx("pull_request_review.dismissed")
    dismissed["pull_request"]["head"]["sha"] = HEAD
    dismiss(gh)
    await handle_github(svc, "pull_request_review", "d-2", dismissed)
    await settle(svc)
    assert gh.armed is None
    await handle_github(svc, "pull_request_review", "d-1-replayed", approved)  # the same signed body again
    await settle(svc)
    assert gh.enables() == [HEAD] and gh.armed is None and job.landing.pr_state == "open", \
        [e.message for e in job.events]
    assert any("no longer stands on GitHub" in e.message for e in job.events)
    # alice approves again, but bob's changes-requested review (its webhook lost) still stands on GitHub
    approval(login="bob", gh=gh, state="changes_requested")
    await handle_github(svc, "pull_request_review", "d-3", approval(gh=gh))
    await settle(svc)
    assert gh.enables() == [HEAD] and gh.armed is None and "changes requested by @bob" in job.landing.note


async def test_a_push_or_a_follow_up_while_arming_disarms(svc, gh):
    """The synchronize webhook (or a follow-up) lands while GitHub is still arming: hangar's record of the arm comes
    after `pushed` looked for one, so the arm itself must look again."""
    lands(svc, approvers=["alice"])
    job = pr_job(svc)
    await green(svc)

    def pushed_meanwhile():
        gh.head = lambda: NEW
        svc.landing.pushed(job, NEW, "a push to the branch")

    gh.on_arm = pushed_meanwhile
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert gh.enables() == [HEAD] and gh.armed is None and job.landing.armed_sha is None, \
        [e.message for e in job.events]
    # a follow-up queued while arming: the agent will push, so it doesn't stay armed either
    def queued_meanwhile():
        job.state = JobState.QUEUED

    gh.on_arm = queued_meanwhile
    await green(svc, NEW)
    await handle_github(svc, "pull_request_review", "d-2", approval(NEW, gh=gh))
    await settle(svc)
    assert gh.enables() == [HEAD, NEW] and gh.armed is None and job.landing.armed_sha is None, \
        [e.message for e in job.events]


async def test_a_failed_disarm_is_tried_again(svc, gh):
    lands(svc, approvers=["alice"])
    job = pr_job(svc)
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert gh.armed == HEAD
    gh.head, gh.disarm_fails = (lambda: NEW), 1
    await handle_github(svc, "pull_request", "d-2", fx("pull_request.synchronize"))
    await settle(svc)
    assert gh.armed == HEAD and job.landing.armed_sha == HEAD  # GitHub refused: still armed
    svc.landing.idle(job)  # after the next turn, or a restart (restore)
    await settle(svc)
    assert gh.armed is None and job.landing.armed_sha is None


async def test_a_code_owner_approves_only_what_they_own(svc, gh):
    lands(svc, approvers=["alice"])
    job = pr_job(svc)
    await green(svc)
    gh.codeowners = "docs/*  @dave\nsrc/  @carol\ninfra/ @ops\n"
    # dave owns the docs, not src/ (nor the unowned script): his approval doesn't land the rest
    gh.files = ["docs/a.md", "src/app.py", "scripts/x.sh"]
    await handle_github(svc, "pull_request_review", "d-1", approval(login="dave", gh=gh))
    await settle(svc)
    assert not gh.enables() and job.landing.pr_state == "open"
    gh.files = ["docs/a.md", "scripts/x.sh"]  # an unowned file needs a policy approver too
    svc.landing._cache.clear()
    await handle_github(svc, "pull_request_review", "d-2", approval(login="dave", gh=gh))
    await settle(svc)
    assert not gh.enables()
    # more files than hangar reads: the ones it didn't see may be someone else's
    gh.files = [f"src/m{i}.py" for i in range(300)] + ["infra/prod.tf"]
    await handle_github(svc, "pull_request_review", "d-3", approval(login="carol", gh=gh))
    await settle(svc)
    assert not gh.enables() and job.landing.pr_state == "open"
    gh.files = [("docs/prod.tf", "infra/prod.tf")]  # moving ops' file into dave's docs touches infra/ too
    await handle_github(svc, "pull_request_review", "d-3b", approval(login="dave", gh=gh))
    await settle(svc)
    assert not gh.enables()
    gh.files = ["docs/a.md"]
    await handle_github(svc, "pull_request_review", "d-4", approval(login="dave", gh=gh))
    await settle(svc)
    assert gh.enables() == [HEAD]


async def test_landing_needs_to_know_the_sandbox_held_no_github_token(svc, gh):
    lands(svc, approvers=["alice"])
    job = pr_job(svc)
    job.landing.push_mode = None  # its delivery couldn't tell (deny by default)
    await green(svc)
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    assert not gh.enables() and "git gateway" in job.landing.note


# ====================================================================== the agent can't push past an approval
@pytest.fixture
def flow(jobs, world, broker, settings, gh):  # noqa: F811
    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_control")
    settings.github_token_credential, settings.github_repos = "gh", {"o/r": "jobenv"}
    jobs.transport = httpx.MockTransport(gh)
    lands(jobs, approvers=["alice"])
    return jobs


async def _landed_job(jobs, world, gh) -> Job:  # noqa: F811
    """A finished job whose PR is o/r#7, approved by alice, green, and armed by hangar."""
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE)
    while job.id in jobs._active:
        await asyncio.sleep(0.05)
    assert job.state == JobState.SUCCEEDED and job.result.head_sha
    job.repo, job.result.pr_number, job.result.pr_url = "https://github.com/o/r.git", 7, "https://github.com/o/r/pull/7"
    job.landing.pr_state, job.landing.node_id = "open", NODE
    job.landing.push_mode = "gateway"  # as a gateway sandbox's delivery records it
    gh.head = lambda: job.result.head_sha
    head = job.result.head_sha
    await green(jobs, head)
    await handle_github(jobs, "pull_request_review", "d-a", approval(head, gh=gh))
    await settle(jobs)
    assert gh.armed == head and job.landing.armed_sha == head, [e.message for e in job.events][-6:]
    # nothing the agent left running can push once it's armed: the sandbox went first, its work checkpointed
    s = jobs._session(job)
    assert job.sandbox_id is None and s.sandbox_id is None and s.checkpoints
    return job


async def test_an_agent_turn_after_approval_never_merges_unreviewed_code(flow, world, gh):  # noqa: F811
    jobs = flow  # noqa: F811
    job = await _landed_job(jobs, world, gh)
    approved = job.result.head_sha
    world["mode"].write_text("followup")
    jobs.follow_up(job.id, "also do Y", requester="sean")
    job = await _until(jobs, job.id, DONE)
    while job.id in jobs._active:
        await asyncio.sleep(0.05)
    await settle(jobs)
    st = job.landing
    assert job.result.head_sha != approved  # the agent pushed a new commit to the PR
    # disarmed before the agent ran, and the approval doesn't carry over to the new commit
    disarm = next(i for i, e in enumerate(job.events) if e.message.startswith("auto-merge disarmed"))
    started = max(i for i, e in enumerate(job.events) if e.message.startswith("agent turn 2 starting"))
    assert disarm < started
    assert gh.armed is None and st.armed_sha is None and st.approved_sha is None and st.pr_state == "open"
    await green(jobs, job.result.head_sha)
    assert gh.enables() == [approved] and gh.merged_sha is None
    assert any("needs a new review" in e.message for e in job.events)
    # the delivery to the PR left its review packet there
    assert len(gh.comments) == 1 and next(iter(gh.comments.values())).startswith(MARKER + "\n" + landing.PACKET)


async def test_a_merged_pr_ends_the_session(flow, world, gh):  # noqa: F811
    jobs = flow  # noqa: F811
    job = await _landed_job(jobs, world, gh)
    s = jobs._session(job)
    ckpts = len(s.checkpoints)
    merged = sub(fx("pull_request.closed.merged"), HEAD, job.result.head_sha)
    assert (await handle_github(jobs, "pull_request", "d-m", merged))["pr_state"] == "merged"
    await settle(jobs)
    assert job.landing.ended and job.sandbox_id is None and s.sandbox_id is None
    assert len(s.checkpoints) >= ckpts and s.checkpoints  # the final checkpoint holds the work
    assert any("final checkpoint" in e.message for e in job.events)


async def test_a_schedule_updates_its_open_pr(flow, world, gh):  # noqa: F811
    from hangar.triggers import Triggers

    jobs = flow  # noqa: F811
    job = await _landed_job(jobs, world, gh)
    s = ScheduledJob(name="weekly-deps", cron="0 9 * * 1",
                     template={"instructions": "also do Y", "repo": world["url"], "environment": "jobenv"},
                     last_job_id=job.id)
    jobs.store.put(s)
    world["mode"].write_text("followup")
    ran = Triggers(jobs).run_now("weekly-deps", "sean")
    assert ran.id == job.id and ran.turns[-1].requester == "schedule:weekly-deps"
    assert "Your PR #7 from the last run is still open" in ran.turns[-1].message
    await _until(jobs, job.id, DONE)
    # once it merged, the next run is a new job
    job.landing.pr_state = "merged"
    while job.id in jobs._active:
        await asyncio.sleep(0.05)
    assert Triggers(jobs).run_now("weekly-deps", "sean").id != job.id


# ====================================================================== the packet
async def test_the_review_packet(svc, gh, settings):
    settings.public_url = "https://hangar.example"
    lands(svc, approvers=["alice"], risk_paths=["infra/**"])
    job = pr_job(svc, verify="uv run pytest -q")
    job.result.verify_exit_code = 0
    job.result.diffstat = " 2 files changed, 3 insertions(+)"
    job.landing.files = [".github/workflows/ci.yml", "src/app.py"]
    job.turns = [Turn(kind="initial", run_id="r1", harness_version="2.1.90")]
    job.result.artifacts = [Artifact(path="demo/checkout.mp4", turn=1, size=10, sha256="0", media_type="video/mp4"),
                            # the agent names its files: a name can't forge a link in the packet
                            Artifact(path="demo/x](https://evil.example)", turn=1, size=1, sha256="0",
                                     media_type="image/gif")]
    await svc.landing.post_packet(job)
    (cid, body), = gh.comments.items()
    assert job.landing.packet_comment_id == cid
    for want in ("`uv run pytest -q` passed", "**Risk:** high: touches `.github/workflows/ci.yml`",
                 "**Files:** 2 (2 files changed, 3 insertions(+))",
                 "claude-code 2.1.90 · model `claude-opus-4-7` (list price $5/$25 per MTok in/out)",
                 "no usage recorded", f"[`demo/checkout.mp4`](https://hangar.example/api/jobs/{job.id}/artifacts/1/"
                 "demo/checkout.mp4)", "an approval from @alice or a code owner",
                 "[`demo/x](https://evil.example)`](https://hangar.example"):
        assert want in body, (want, body)
    await svc.landing.post_packet(job)
    assert list(gh.comments) == [cid]  # edited, not posted again
    del gh.comments[cid]  # deleted on GitHub: a new one
    await svc.landing.post_packet(job)
    assert len(gh.comments) == 1 and job.landing.packet_comment_id != cid


def test_paths_risk_and_codeowners():
    assert landing.risk(["docs/a.md", "tests/test_x.py"], None)[0] == "low"
    assert landing.risk(["src/a.py"], ["infra/**"]) == ("normal", [])
    # --- integ3 (review #102): the repo's environment runs in every build and sandbox
    assert landing.risk([".hangar/environment.yaml", "AGENTS.md"], None) == ("high", [".hangar/environment.yaml"])
    assert landing.risk(["infra/net/main.tf", "src/a.py"], ["infra/**"]) == ("high", ["infra/net/main.tf"])
    owners = "*  @lead\n/build/logs/ @ops\ndocs/*  @dave\n*.js @js\napps/ @apps\n/scripts/ \n"
    assert landing.owners_of(owners, "src/a.py") == ["@lead"]
    assert landing.owners_of(owners, "build/logs/x.log") == ["@ops"]
    assert landing.owners_of(owners, "docs/getting-started.md") == ["@dave"]
    assert landing.owners_of(owners, "docs/build-app/troubleshooting.md") == ["@lead"]  # docs/* is not recursive
    assert landing.owners_of(owners, "web/src/x.js") == ["@js"]
    assert landing.owners_of(owners, "deep/apps/main.py") == ["@apps"]
    assert landing.owners_of(owners, "scripts/run.sh") == []  # a line with no owners unowns it


def test_policy_fields(svc):
    p = policy_update(svc, PolicyUpdate(repo="O/R", land="auto_after_approval", approvers=["@alice", "bob"],
                                        merge_method="merge", risk_paths=["infra/**"]), "ui:admin")
    assert (p.land, p.approvers, p.merge_method) == ("auto_after_approval", ["alice", "bob"], "merge")
    with pytest.raises(PolicyError):
        policy_update(svc, PolicyUpdate(repo="o/r", approvers=["not a login"]), "ui:admin")
    with pytest.raises(ValueError):
        PolicyUpdate(repo="o/r", land="whenever")


# ====================================================================== hangar doctor
async def test_doctor_reads_the_branch_protection(svc, gh, settings):
    from types import SimpleNamespace

    h = SimpleNamespace(jobs=svc, settings=settings)
    assert await landing.doctor_checks(h) == []  # nothing lands on approval yet
    lands(svc, approvers=["alice"])
    out = {c["name"]: c for c in await landing.doctor_checks(h)}
    assert out["landing o/r"]["status"] == "ok", out
    assert out["landing identity"]["status"] == "warn" and out["landing: agent tokens"]["status"] == "warn"
    # classic protection without stale-approval dismissal, and no way to read it besides
    gh.protection = {"required_pull_request_reviews": {"required_approving_review_count": 1,
                                                       "dismiss_stale_reviews": False,
                                                       "require_last_push_approval": False}}
    original = fx("branch_protection")["rules"]
    fx_rules = [r for r in original if r["type"] != "pull_request"]
    gh_call = gh.__call__

    def no_ruleset(r):
        if r.url.path == "/repos/o/r/rules/branches/main":
            return httpx.Response(200, json=fx_rules)
        return gh_call(r)

    svc.transport = httpx.MockTransport(no_ruleset)
    gh.allow_auto_merge = False
    c = {c["name"]: c for c in await landing.doctor_checks(h)}["landing o/r"]
    # --- trust: arming refuses without stale-approval dismissal, so it fails rather than warns (test_trust_landing)
    assert c["status"] == "fail" and "dismiss stale approvals" in c["detail"] and "most recent reviewable push" in \
        c["detail"] and "Allow auto-merge" in c["detail"] and "settings/branches" in c["fix"]


async def test_the_packet_names_the_model_that_ran_and_its_price(svc, gh, settings, monkeypatch):
    """--- review #105: a job's model is a policy (jobs default to `balanced`) resolved on its session, so the packet
    shows the session's concrete id and what it costs, not the request's `model` (None: "the harness default")."""
    from types import SimpleNamespace

    job = pr_job(svc)
    job.model = None
    monkeypatch.setattr(svc, "_session", lambda j: SimpleNamespace(
        name="s-1", model="claude-fable-5-1", model_policy="frontier"))
    await svc.landing.post_packet(job)
    (_, body), = gh.comments.items()
    assert "model `claude-fable-5-1` (`frontier`; list price $10/$50 per MTok in/out)" in body, body
    assert "no job budget" in body
