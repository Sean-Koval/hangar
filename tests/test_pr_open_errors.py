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
    assert len(body) <= MAX_PR_BODY and "was cut" in body and "RESULT.md" in body
    assert body.startswith("a 41-file change")
    assert body.index("RESULT.md") < body.index("Hangar-Job:")  # the trailers stay last


async def test_the_cut_body_is_still_scanned_for_secrets(svc, world):  # noqa: F811
    job = _job(svc, world)
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    job.result.summary = f"token {token} leaked\n" + "x" * 90_000
    await svc._ensure_pr(job, draft=True)
    body = svc.fake.prs[0]["body"]
    assert token not in body and "was cut" in body and len(body) <= 65_536


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


async def test_the_cut_keeps_hangars_own_lines(svc, world):  # noqa: F811
    job = _job(svc, world)
    job.verify, job.result.verify_exit_code = "pytest -q", 1
    job.result.touched_tests = ["tests/test_x.py"]
    job.result.summary = "long\n" + "x" * 90_000
    await svc._ensure_pr(job, draft=True)
    body = svc.fake.prs[0]["body"]
    assert len(body) <= MAX_PR_BODY and "summary was cut" in body
    assert "Verification `pytest -q`: **failed (exit 1)**" in body  # a long summary can't hide a failed check
    assert "The agent changed the check's own tests" in body
    assert body.index("summary was cut") < body.index("Verification") < body.index("Hangar-Job:")


async def test_a_secret_on_the_cut_is_not_half_kept(svc, world):  # noqa: F811
    job = _job(svc, world)
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    rest_len = len(svc._pr_body(job)) - len("_The agent left no summary._")  # what follows the summary
    for shift in (5, 20, 35):  # the cut falls inside the token
        svc.fake.prs.clear()
        room = MAX_PR_BODY - rest_len - 200  # about where the cut lands; then search around it
        job.result.summary = "y" * (room - shift) + " " + token + " " + "z" * 90_000
        await svc._ensure_pr(job, draft=True)
        body = svc.fake.prs[-1]["body"]
        assert token[:12] not in body and "summary was cut" in body


async def test_a_cut_inside_a_code_block_closes_it(svc, world):  # noqa: F811
    job = _job(svc, world)
    job.result.summary = "log:\n```\n" + "line\n" * 30_000
    await svc._ensure_pr(job, draft=True)
    body = svc.fake.prs[0]["body"]
    head = body[:body.index("summary was cut")]
    assert head.count("```") % 2 == 0


async def test_a_pr_into_another_base_is_not_adopted(svc, world):  # noqa: F811
    svc.fake.refuse = (422, ALREADY)
    svc.fake.listed = [{"number": 9, "html_url": "https://github.com/o/r/pull/9", "base": {"ref": "release"}}]
    job = _job(svc, world)
    job.base_branch = "main"
    await svc._ensure_pr(job, draft=True)
    assert job.result.pr_url is None and "already exists" in job.result.pr_error


async def test_a_failed_lookup_keeps_githubs_reason(svc, world):  # noqa: F811
    svc.fake.refuse = (422, ALREADY)
    job = _job(svc, world)
    job.base_branch = "main"
    calls = {"n": 0}
    real = GitHub.find_pr

    async def flaky(self, repo, branch):
        calls["n"] += 1
        if calls["n"] > 1:
            raise GitHubError("GitHub GET /repos/o/r/pulls: 502 Bad gateway", 502)
        return await real(self, repo, branch)

    GitHub.find_pr = flaky
    try:
        await svc._ensure_pr(job, draft=True)
    finally:
        GitHub.find_pr = real
    assert "already exists" in job.result.pr_error and "502" not in job.result.pr_error


async def test_errors_are_capped():
    def gh(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "m", "errors": [{"message": "w" * 900}] * 50})

    client = GitHub("t", transport=httpx.MockTransport(gh))
    with pytest.raises(GitHubError) as e:
        await client.open_pr("o/r", "b", "main", "t", "body", True)
    await client.close()
    assert len(e.value.errors) == 10 and all(len(x) <= 200 for x in e.value.errors)


async def test_masks_that_outgrow_the_limit_cut_the_summary_not_the_trailers(svc, world, monkeypatch):  # noqa: F811
    """R-hangar on 488: a body that masking pushes past 65,536 is cut in the summary again, so the Jira line and the
    trailers stay. A known short value masked as `[hangar: secret removed]` grows the text."""
    from hangar import secretscan

    real = secretscan.mask

    def mask(text, broker=None):
        out, kinds = real(text, broker)
        return out.replace("SEKRET", "[hangar: secret removed]"), kinds + (["test"] if "SEKRET" in out else [])

    monkeypatch.setattr(secretscan, "mask", mask)
    job = _job(svc, world)
    job.ticket = "jira:KAN-339"
    job.result.summary = "SEKRET " * 2_000 + "x" * 90_000  # 36,000 more characters once masked
    await svc._ensure_pr(job, draft=True)
    body = svc.fake.prs[0]["body"]
    assert len(body) <= 65_536 and "SEKRET" not in body and "summary was cut" in body
    assert "Jira: KAN-339" in body and "Hangar-Job:" in body
    assert body.index("summary was cut") < body.index("Hangar-Job:")


async def test_a_cut_inside_a_tilde_or_long_fence_closes_it(svc, world):  # noqa: F811
    for fence in ("~~~", "````"):
        svc.fake.prs.clear()
        job = _job(svc, world)
        job.result.summary = f"log:\n{fence}python\n```\n" + "line\n" * 30_000  # ``` inside a ```` block is text
        await svc._ensure_pr(job, draft=True)
        body = svc.fake.prs[0]["body"]
        assert f"\n{fence}\n\n_The agent's summary was cut" in body, fence
