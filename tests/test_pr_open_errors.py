"""KAN-339: opening a job's PR. GitHub's errors[] reach the job, an over-long body is cut, an existing PR is adopted."""

from __future__ import annotations

import json

import httpx
import pytest

from hangar.github import GitHub, GitHubError
from hangar.jobs import MAX_PR_BODY, Caller, JobService, SubmitJob
from tests.test_jobs import Hooks, _gh_settings, world  # noqa: F401 - world is a fixture

ALREADY = {"message": "Validation Failed", "errors": [
    {"resource": "PullRequest", "code": "custom", "message": "A pull request already exists for o:hangar/x."}]}
TOO_LONG = {"message": "Validation Failed", "errors": [
    {"resource": "PullRequest", "code": "custom", "message": "body is too long (maximum is 65536 characters)"}]}


class GitHubFake(Hooks):
    """test_jobs' fake GitHub, with a POST /pulls that can refuse and a GET /pulls that can list an open PR."""

    def __init__(self):
        super().__init__()
        self.refuse: tuple[int, dict | str] | None = None
        self.listed: list[dict] = []
        self.gets = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.url.host == "api.github.com" and p.endswith("/pulls"):
            self.calls.append(request)
            if request.method == "GET":
                self.gets += 1
                return httpx.Response(200, json=self.listed if self.gets > 1 else [])
            body = json.loads(request.content)
            if len(body["body"]) > 65_536:
                return httpx.Response(422, json=TOO_LONG)
            if self.refuse:
                code, why = self.refuse
                return httpx.Response(code, json=why) if isinstance(why, dict) else httpx.Response(code, text=why)
            pr = {"number": 7, "html_url": "https://github.com/o/r/pull/7", **body}
            self.prs.append(pr)
            return httpx.Response(201, json=pr)
        return super().__call__(request)


@pytest.fixture
def svc(fleet, broker, world):  # noqa: F811
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    fake = GitHubFake()
    s = JobService(fleet, None, transport=httpx.MockTransport(fake))  # not started: we run delivery's PR step
    s.fake = fake
    _gh_settings(s, broker)
    return s


def _job(svc, world):  # noqa: F811
    job = svc.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("svc", "svc"))
    job.repo = "https://github.com/o/r.git"
    return job


async def test_errors_reach_the_job_event_and_result(svc, world):  # noqa: F811
    svc.fake.refuse = (422, {"message": "Validation Failed", "errors": [{"field": "head", "code": "invalid"}]})
    job = _job(svc, world)
    await svc._ensure_pr(job, draft=True)
    ev = [e.message for e in job.events if "opening the PR failed" in e.message]
    assert ev and "422 Validation Failed (code=invalid field=head)" in ev[-1]
    assert job.result.pr_url is None and "code=invalid field=head" in job.result.pr_error
    assert "ghp_x" not in ev[-1] and "ghp_x" not in job.result.pr_error  # never the token


async def test_an_over_long_body_is_cut_and_the_pr_opens(svc, world):  # noqa: F811
    job = _job(svc, world)
    job.result.summary = "a 41-file change\n" + "x" * 90_000
    await svc._ensure_pr(job, draft=True)
    assert job.result.pr_number == 7 and job.result.pr_error is None
    body = svc.fake.prs[0]["body"]
    assert len(body) <= MAX_PR_BODY and "cut at 60,000 characters" in body and "RESULT.md" in body
    assert body.startswith("a 41-file change")
    assert body.index("RESULT.md") < body.index("Hangar-Job:")  # the trailers stay last


async def test_the_cut_body_is_still_scanned_for_secrets(svc, world):  # noqa: F811
    job = _job(svc, world)
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    job.result.summary = f"token {token} leaked\n" + "x" * 90_000
    await svc._ensure_pr(job, draft=True)
    body = svc.fake.prs[0]["body"]
    assert token not in body and "cut at 60,000" in body and len(body) <= 65_536


async def test_an_existing_pr_is_adopted(svc, world):  # noqa: F811
    svc.fake.refuse = (422, ALREADY)
    svc.fake.listed = [{"number": 123, "html_url": "https://github.com/o/r/pull/123", "draft": True}]
    job = _job(svc, world)
    await svc._ensure_pr(job, draft=True)
    assert job.result.pr_number == 123 and job.result.pr_url.endswith("/pull/123") and job.result.pr_error is None
    assert any("adopted PR #123" in e.message for e in job.events)
    assert not any("opening the PR failed" in e.message for e in job.events)


async def test_already_exists_with_nothing_to_adopt_still_fails(svc, world):  # noqa: F811
    svc.fake.refuse = (422, ALREADY)
    job = _job(svc, world)
    await svc._ensure_pr(job, draft=True)
    assert job.result.pr_url is None and "already exists" in job.result.pr_error


async def test_a_non_json_error_is_still_sensible():
    def gh(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>\n  <h1>Bad gateway</h1>\n</html>")

    client = GitHub("ghp_secret", transport=httpx.MockTransport(gh))
    with pytest.raises(GitHubError) as e:
        await client.open_pr("o/r", "b", "main", "t", "body", True)
    await client.close()
    assert str(e.value) == "GitHub POST /repos/o/r/pulls: 502 <html> <h1>Bad gateway</h1> </html>"
    assert e.value.status == 502 and e.value.errors == []


async def test_errors_keep_room_under_the_cap():
    def gh(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "m" * 900, "errors": [{"code": "custom", "message": "why " * 50},
                                                                          "a plain string"]})

    client = GitHub("t", transport=httpx.MockTransport(gh))
    with pytest.raises(GitHubError) as e:
        await client.open_pr("o/r", "b", "main", "t", "body", True)
    await client.close()
    msg = str(e.value)
    assert len(msg) <= 500 and "(code=custom why why" in msg and "a plain string)" in msg
