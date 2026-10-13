"""The PR auto-fix loop (--- overnight/autofix, spec 0012 item 6): GitHub webhooks about a hangar job's PR become
follow-up turns on the job's session. A scripted agent and a local git remote do the work; a mock GitHub API
(httpx.MockTransport) answers for the PR, its checks, Actions logs and reviews."""

from __future__ import annotations

import asyncio
import json
import subprocess

import httpx
import pytest

from hangar import autofix
from hangar.github import MARKER
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, JobError, JobService, SubmitJob, handle_github
from hangar.models import CredentialKind, Job, JobState
from hangar.setup_api import HOOK_EVENTS, hook_problem
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures

AGENT = r"""#!/usr/bin/env bash
mode=$(cat "$MODE_FILE" 2>/dev/null || echo ok)
case "$mode" in
  ok) echo fixed > fix.txt && git add fix.txt && git commit -qm "fix: add fix.txt" ;;
  cifix) grep -rqs "AssertionError: boom" "$HANGAR_IO/in" && date +%N > ci-fix.txt && git add ci-fix.txt \
           && git commit -qm "fix the failing test" ;;
  review) grep -rqs "rename foo to bar" "$HANGAR_IO/in" && echo bar > bar.txt && git add bar.txt \
           && git commit -qm "rename foo to bar" ;;
  nochange) true ;;
esac
echo "mode $mode" > "$HANGAR_JOB_DIR/RESULT.md"
"""

LOG = ("2026-10-13T10:00:00.1234567Z ##[group]Run pytest -q\n"
       + "2026-10-13T10:00:01.0000000Z \x1b[31mFAILED\x1b[0m tests/test_app.py::test_x\n" * 3
       + "2026-10-13T10:00:02.0000000Z E   AssertionError: boom\n"
       + "2026-10-13T10:00:03.0000000Z ##[error]Process completed with exit code 1.\n")


class GitHubMock:
    """Just enough of api.github.com for one PR (o/r#7). Check runs answer 403 unless listed in `runs`, the way
    they do for a fine-grained token; Actions logs redirect to a signed URL on another host."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.head = lambda: "?"
        self.state = "open"
        self.runs: dict[int, dict] = {}
        self.annotations: dict[int, list] = {}
        self.logs: dict[int, str] = {}
        self.review_comments: dict[int, list] = {}
        self.login = "hangar-bot"

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.calls.append(r)
        p, m = r.url.path, r.method
        if r.url.host == "logs.example":
            assert "authorization" not in r.headers  # the signed URL is fetched without the token
            return httpx.Response(200, text=self.logs[int(p.rsplit("/", 1)[1])])
        if p == "/user":
            return httpx.Response(200, json={"login": self.login})
        if p == "/repos/o/r/pulls/7":
            return httpx.Response(200, json={"number": 7, "state": self.state, "head": {"sha": self.head()}})
        if p == "/repos/o/r/pulls" and m == "GET":
            return httpx.Response(200, json=[{"number": 7, "html_url": "https://github.com/o/r/pull/7"}])
        if p == "/repos/o/r/issues/7/comments" and m == "POST":
            return httpx.Response(201, json={"id": 1})
        if p.startswith("/repos/o/r/check-runs/"):
            rid = int(p.split("/")[5])
            if p.endswith("/annotations") and rid in self.annotations:
                return httpx.Response(200, json=self.annotations[rid])
            if not p.endswith("/annotations") and rid in self.runs:
                return httpx.Response(200, json=self.runs[rid])
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        if p.startswith("/repos/o/r/check-suites/"):
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        if p.startswith("/repos/o/r/actions/jobs/") and p.endswith("/logs"):
            jid = int(p.split("/")[6])
            return httpx.Response(302, headers={"location": f"https://logs.example/blob/{jid}?sig=abc"})
        if p.startswith("/repos/o/r/pulls/7/reviews/") and p.endswith("/comments"):
            return httpx.Response(200, json=self.review_comments.get(int(p.split("/")[7]), []))
        return httpx.Response(404, json={"message": "Not Found"})

    def comments(self) -> list[str]:
        return [json.loads(c.content)["body"].removeprefix(MARKER + "\n") for c in self.calls
                if c.method == "POST" and c.url.path == "/repos/o/r/issues/7/comments"]


@pytest.fixture
def gh(jobs, world, broker, monkeypatch):  # noqa: F811
    agent = world["root"] / "fix-agent.sh"
    agent.write_text(AGENT)
    monkeypatch.setitem(HARNESSES, "scripted", Harness(
        name="scripted", title="Scripted", run=f"MODE_FILE={world['mode']} bash {agent}"))
    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_x")
    s = jobs.settings
    s.github_token_credential, s.github_repos = "gh", {"o/r": "jobenv"}
    s.autofix_coalesce_seconds, s.autofix_cooldown_seconds = 0.3, 0
    mock = GitHubMock()
    jobs.transport = httpx.MockTransport(mock)
    return mock


async def _pr_job(jobs, world, gh, **kw) -> Job:  # noqa: F811
    """A finished job whose PR (o/r#7) hangar opened; the work goes to the local remote."""
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv", **kw),
                      Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE)
    while job.id in jobs._active:
        await asyncio.sleep(0.05)
    assert job.state == JobState.SUCCEEDED and job.result.head_sha
    job.repo, job.result.pr_number, job.result.pr_url = "https://github.com/o/r.git", 7, "https://github.com/o/r/pull/7"
    gh.head = lambda: job.result.head_sha
    return job


def _check_run(job: Job, rid: int, name: str, sha: str | None = None, conclusion: str = "failure", **extra) -> dict:
    run = {"id": rid, "name": name, "head_sha": sha or job.result.head_sha, "status": "completed",
           "conclusion": conclusion, "html_url": f"https://github.com/o/r/runs/{rid}",
           "details_url": f"https://github.com/o/r/actions/runs/99/job/{rid}", "app": {"slug": "github-actions"},
           "output": {"title": f"{name} failed", "summary": f"{name}: 1 failed", "text": None,
                      "annotations_count": 0},
           "pull_requests": [{"number": 7, "head": {"sha": job.result.head_sha}}], **extra}
    return {"action": "completed", "repository": {"full_name": "o/r"}, "check_run": run, "sender": {"login": "ci"}}


def _review(rid: int, login: str, state: str, body: str = "", assoc: str = "MEMBER") -> dict:
    return {"action": "submitted", "repository": {"full_name": "o/r"},
            "pull_request": {"number": 7, "head": {"ref": "x"}},
            "review": {"id": rid, "state": state, "body": body, "user": {"login": login},
                       "author_association": assoc, "commit_id": "abc", "html_url": f"https://github.com/o/r/pull/7#r{rid}"}}


async def _turns(jobs, job, n: int, timeout: float = 20) -> Job:  # noqa: F811
    for _ in range(int(timeout / 0.05)):
        if len(job.turns) >= n and job.state in DONE and job.id not in jobs._active:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"{len(job.turns)} turn(s), {job.state}: {[e.message for e in job.events][-8:]}")


def _attachment(jobs, job, name: str) -> str:  # noqa: F811
    box = jobs.fleet.providers["docker"].boxes[job.sandbox_id]
    return next(box.rglob(name)).read_text()


async def test_ci_failure_becomes_one_follow_up_turn_with_the_logs(jobs, world, gh):  # noqa: F811
    job = await _pr_job(jobs, world, gh)
    head0 = job.result.head_sha
    gh.logs[555] = LOG
    gh.annotations[555] = [{"path": "tests/test_app.py", "start_line": 12, "annotation_level": "failure",
                            "title": "test_x", "message": "AssertionError: boom"}]
    world["mode"].write_text("cifix")
    tests = _check_run(job, 555, "tests")
    tests["check_run"]["output"]["annotations_count"] = 1
    # a burst: three failures and a commit status within the window become one turn
    assert (await handle_github(jobs, "check_run", "d-1", tests))["ok"]
    assert (await handle_github(jobs, "check_run", "d-2", _check_run(job, 556, "lint", app={"slug": "x"},
                                                                      details_url="https://lint.example/1")))["ok"]
    suite = {"action": "completed", "repository": {"full_name": "o/r"},
             "check_suite": {"id": 77, "head_sha": head0, "head_branch": job.branch, "conclusion": "failure",
                             "app": {"name": "Buildkite", "slug": "buildkite"}, "pull_requests": [{"number": 7}]}}
    assert (await handle_github(jobs, "check_suite", "d-3", suite))["ok"]
    status = {"repository": {"full_name": "o/r"}, "id": 4, "sha": head0, "state": "error", "context": "ci/jenkins",
              "description": "build errored", "target_url": "https://jenkins.example/4",
              "branches": [{"name": job.branch}]}
    assert (await handle_github(jobs, "status", "d-4", status))["ok"]
    assert (await handle_github(jobs, "check_run", "d-1b", tests))["ignored"] == "already taken"  # redelivery
    job = await _turns(jobs, job, 2)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert len(job.turns) == 2 and job.turns[1].trigger == "autofix:ci" and job.turns[1].requester == "hangar:autofix"
    assert job.turns[1].reply_to == "github:o/r#7" and any("fix the failing test" in c for c in job.result.commits)
    ci = _attachment(jobs, job, "ci-failures.md")
    assert "AssertionError: boom" in ci and "##[error]Process completed" in ci and "\x1b[" not in ci
    assert "2026-10-13T10" not in ci  # timestamps and colour codes stripped from the log
    assert "tests/test_app.py:12" in ci and "lint: 1 failed" in ci and "build errored" in ci and "Buildkite" in ci
    assert job.result.head_sha != head0  # pushed
    pushed = subprocess.run(["git", "--git-dir", str(world["remote"]), "log", "--oneline", job.branch],
                            capture_output=True, text=True).stdout
    assert "fix the failing test" in pushed
    comments = gh.comments()
    assert comments[0].startswith("**Auto-fix** (round 1 of 3): check `tests` failure; check `lint` failure")
    assert comments[-1].startswith("**Done**")
    st = job.autofix_state
    assert len(st.rounds) == 1 and st.fixed_shas == [head0] and not st.pending
    # the new head goes green: nothing to do; a late failure for the old head doesn't count
    green = _check_run(job, 557, "tests", conclusion="success")
    assert "not a failure" in (await handle_github(jobs, "check_run", "d-5", green))["ignored"]
    late = await handle_github(jobs, "check_run", "d-6", _check_run(job, 558, "tests", sha=head0))
    assert "not the job's own head" in late["ignored"]


async def test_reviews_from_trusted_people_only(jobs, world, gh):  # noqa: F811
    job = await _pr_job(jobs, world, gh)
    world["mode"].write_text("review")
    assert "not trusted" in (await handle_github(jobs, "pull_request_review", "r-1",
                                                 _review(30, "rando", "changes_requested", "do X", "NONE")))["ignored"]
    assert "own review" in (await handle_github(jobs, "pull_request_review", "r-2",
                                                _review(31, "hangar-bot", "changes_requested")))["ignored"]
    assert "own review" in (await handle_github(jobs, "pull_request_review", "r-3",
                                                _review(32, "sean", "commented", f"{MARKER}\nhi")))["ignored"]
    assert "nothing to fix" in (await handle_github(jobs, "pull_request_review", "r-4",
                                                    _review(33, "sean", "approved")))["ignored"]
    assert not job.autofix_state.pending
    gh.review_comments[34] = [
        {"id": 901, "path": "app.py", "line": 3, "body": "Please rename foo to bar here.", "user": {"login": "sean"},
         "diff_hunk": "@@ -1,3 +1,3 @@\n-x = 1\n+foo = 1"},
        {"id": 902, "path": "app.py", "line": 9, "body": "sneaky", "user": {"login": "rando"}}]
    assert (await handle_github(jobs, "pull_request_review", "r-5",
                                _review(34, "sean", "changes_requested", "Close; rename foo to bar.")))["ok"]
    inline = {"action": "created", "repository": {"full_name": "o/r"}, "pull_request": {"number": 7},
              "comment": {"id": 901, "path": "app.py", "line": 3, "body": "Please rename foo to bar here.",
                          "user": {"login": "sean"}, "author_association": "MEMBER", "pull_request_review_id": 34}}
    assert (await handle_github(jobs, "pull_request_review_comment", "r-6", inline))["ok"]
    outsider = {**inline, "comment": {**inline["comment"], "id": 903, "user": {"login": "rando"},
                                      "author_association": "CONTRIBUTOR"}}
    assert "not trusted" in (await handle_github(jobs, "pull_request_review_comment", "r-7", outsider))["ignored"]
    job = await _turns(jobs, job, 2)
    assert job.turns[1].trigger == "autofix:review" and any("rename foo to bar" in c for c in job.result.commits)
    md = _attachment(jobs, job, "review-comments.md")
    assert "`app.py` line 3 (@sean)" in md and "rename foo to bar" in md and "+foo = 1" in md
    assert md.count("Please rename foo to bar here.") == 1 and "sneaky" not in md  # deduped; only the reviewer's
    box = jobs.fleet.providers["docker"].boxes[job.sandbox_id]
    prompt = sorted((box / "home/agent/.hangar/runs").glob("*.prompt"), key=lambda p: p.stat().st_mtime)[-1]
    assert "hangar-agent ask" in prompt.read_text() and "sean requested changes" in prompt.read_text()
    assert gh.comments()[0].startswith("**Auto-fix** (round 1 of 3): sean requested changes (1 inline comment(s))")
    # a "commented" review with no inline comments is conversation, not a request: no round
    world["mode"].write_text("nochange")
    assert (await handle_github(jobs, "pull_request_review", "r-8", _review(35, "sean", "commented", "nice")))["ok"]
    await asyncio.sleep(1)
    assert len(job.turns) == 2 and any("nothing left to fix" in e.message for e in job.events)


async def test_round_limit_cooldown_and_no_self_trigger(jobs, world, gh):  # noqa: F811
    jobs.settings.autofix_max_rounds = 2
    job = await _pr_job(jobs, world, gh)
    head0 = job.result.head_sha
    world["mode"].write_text("nochange")  # the agent looks and pushes nothing
    gh.logs[600] = LOG
    assert (await handle_github(jobs, "check_run", "c-1", _check_run(job, 600, "tests")))["ok"]
    job = await _turns(jobs, job, 2)
    assert job.result.head_sha == head0 and job.autofix_state.fixed_shas == [head0]
    # the same commit fails again (another check, a re-run): no second round for a push it already handled
    assert (await handle_github(jobs, "check_run", "c-2", _check_run(job, 601, "tests")))["ok"]
    await asyncio.sleep(1)
    assert len(job.turns) == 2 and any("nothing left to fix" in e.message for e in job.events)
    # a new push by hangar: one more round is allowed, then the limit stops the loop (said once on the PR)
    world["mode"].write_text("cifix")
    jobs.follow_up(job.id, "keep going", requester="svc")
    job = await _turns(jobs, job, 3)
    world["mode"].write_text("nochange")
    gh.logs[602] = LOG
    assert (await handle_github(jobs, "check_run", "c-3", _check_run(job, 602, "tests")))["ok"]
    job = await _turns(jobs, job, 4)
    assert len(job.autofix_state.rounds) == 2
    for rid in (603, 604):
        await handle_github(jobs, "check_run", f"c-{rid}", _check_run(job, rid, f"t{rid}"))
        await asyncio.sleep(0.8)
    assert len(job.turns) == 4 and job.autofix_state.stopped == "limit"
    assert sum(c.startswith("**Auto-fix stopped**") for c in gh.comments()) == 1
    # the cooldown: a round waits for it
    jobs.settings.autofix_max_rounds, jobs.settings.autofix_cooldown_seconds = 5, 30
    await handle_github(jobs, "check_run", "c-605", _check_run(job, 605, "t605"))
    await asyncio.sleep(0.8)
    st = job.autofix_state
    assert len(job.turns) == 4 and st.pending and abs(st.due_at - (st.rounds[-1].at + 30)) < 1


async def test_someone_elses_push_pauses_the_loop(jobs, world, gh):  # noqa: F811
    job = await _pr_job(jobs, world, gh)
    assert (await handle_github(jobs, "check_run", "h-1", _check_run(job, 700, "tests")))["ok"]
    gh.head = lambda: "f00dfeed" * 5  # a person pushed before the window closed
    await asyncio.sleep(1)
    assert len(job.turns) == 1 and "someone else pushed" in (job.autofix_state.stopped or "")
    paused = [c for c in gh.comments() if c.startswith("**Auto-fix paused**")]
    assert len(paused) == 1 and "f00dfee" in paused[0]


async def test_settings_and_modes(jobs, world, gh, fleet):  # noqa: F811
    with pytest.raises(JobError, match="autofix must be"):
        jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv", autofix="sometimes"),
                    Caller("svc", "svc"))
    assert autofix.check_mode(" Reviews , CI ") == "ci,reviews" and autofix.check_mode("off") == "off"
    job = await _pr_job(jobs, world, gh, autofix="ci")
    review = _review(40, "sean", "changes_requested", "x")
    assert "reviews is off" in (await handle_github(jobs, "pull_request_review", "m-1", review))["ignored"]
    jobs.settings.github_autofix = {"O/R": "off"}  # the repo's setting narrows every job's
    assert "ci is off" in (await handle_github(jobs, "check_run", "m-2", _check_run(job, 800, "t")))["ignored"]
    jobs.settings.github_autofix = {"o/r": "bogus"}
    assert autofix.mode(jobs.settings, job) == set()
    jobs.settings.github_autofix = {}
    job.result.pr_number = None  # a job without a PR (deliver=branch) is nobody's to fix
    assert "not a hangar job" in (await handle_github(jobs, "check_run", "m-3", _check_run(job, 801, "t")))["ignored"]


async def test_pending_triggers_survive_a_restart(jobs, world, gh, fleet):  # noqa: F811
    jobs.settings.autofix_coalesce_seconds = 1.5
    job = await _pr_job(jobs, world, gh)
    world["mode"].write_text("cifix")
    gh.logs[900] = LOG
    assert (await handle_github(jobs, "check_run", "s-1", _check_run(job, 900, "tests")))["ok"]
    await jobs.stop()  # down before the window closed
    again = JobService(fleet, jobs.sessions, transport=jobs.transport)
    await again.start()
    try:
        job = again.get(job.id)
        assert job.autofix_state.pending
        job = await _turns(again, job, 2)
        assert job.turns[1].trigger == "autofix:ci" and len(job.autofix_state.rounds) == 1
    finally:
        await again.stop()


async def test_malformed_payloads_are_ignored(jobs, world, gh):  # noqa: F811
    job = await _pr_job(jobs, world, gh)
    repo = {"full_name": "o/r"}
    weird = [("check_run", {"action": "completed", "repository": repo, "check_run": "x"}),
             ("check_run", {"action": "completed", "repository": repo,
                            "check_run": {"conclusion": "failure", "head_sha": 7, "pull_requests": ["x", None],
                                          "output": "y", "app": 3}}),
             ("status", {"repository": repo, "state": "failure", "sha": ["a"], "branches": "main"}),
             ("pull_request_review", {"action": "submitted", "repository": repo, "review": None,
                                      "pull_request": "7"}),
             ("pull_request_review_comment", {"action": "created", "repository": repo,
                                              "comment": {"user": "sean", "body": 5}, "pull_request": {"number": "7"}})]
    for event, payload in weird:
        assert "ignored" in await handle_github(jobs, event, "w", payload), (event, payload)
    assert not job.autofix_state.pending


def test_the_webhook_subscribes_the_new_events():
    old = {"events": ["issues", "issue_comment", "pull_request_review_comment"], "active": True,
           "config": {"content_type": "json"}}
    problem = hook_problem(old)
    assert problem and all(e in problem for e in ("pull_request_review", "check_run", "check_suite", "status"))
    assert hook_problem({**old, "events": list(HOOK_EVENTS)}) is None
    assert set(autofix.EVENTS) <= set(HOOK_EVENTS)
