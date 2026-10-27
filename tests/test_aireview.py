"""AI first-pass review (#161): an independent review of every agent PR, by a hangar job on another harness (or an
org review bot's check), recorded on `landing.ai_review` and posted as one COMMENT review from the App."""

from __future__ import annotations

import asyncio
import json

import pytest

from hangar import aireview, egress, fairness, posture, usage
from hangar.gateway import FETCH_ONLY_LABELS
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import JobService, handle_github
from hangar.models import JobState, Sandbox
from hangar.repopolicy import RepoPolicy, repo_key
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures
from tests.test_landing import GH, HEAD, NEW, fx, gh, pr_job, settle, sub, svc  # noqa: F401 - fixtures

RESULT = {"verdict": "comment", "summary": "Looks fine; one nit.",
          "findings": [{"path": "src/app.py", "line": 3, "severity": "minor", "text": "Rename x, ask @lead"}]}


def reviews_on(svc: JobService, **kw) -> None:  # noqa: F811
    svc.policies.put(RepoPolicy(repo="o/r", land="auto_after_approval", approvers=["lead"], ai_review="hangar",
                                **kw))


def agent_job(svc: JobService, harness: str = "claude-code", **kw):  # noqa: F811
    job = pr_job(svc, person="alice", requester="alice", **kw)
    job.environment, job.harness = "agent", harness  # claude-code and codex are enabled there
    svc._save(job)
    return job


# ====================================================================== who reviews
@pytest.mark.parametrize(("author", "enabled", "want"), [
    ("claude-code", ["claude-code", "codex"], ("codex", True)),
    ("codex", ["claude-code", "codex"], ("claude-code", True)),
    ("pi", ["pi", "claude-code"], ("claude-code", True)),
    ("claude-code", ["claude-code"], ("claude-code", False)),  # one harness: a fresh session, not independent
])
def test_the_reviewer_is_another_harness_than_the_author(author, enabled, want):
    assert aireview.reviewer_for(author, None, enabled) == want


def test_the_policy_can_name_the_reviewer_and_mode_is_validated():
    p = RepoPolicy(repo="o/r", ai_review="check:CodeRabbit review", ai_review_harness="codex")
    assert aireview.mode(p) == "check:CodeRabbit review"
    assert aireview.reviewer_for("claude-code", p, ["claude-code", "codex"]) == ("codex", True)
    assert aireview.reviewer_for("codex", p, ["claude-code", "codex"]) == ("codex", False)  # its own harness
    with pytest.raises(ValueError, match="ai_review is hangar"):
        RepoPolicy(repo="o/r", ai_review="sometimes")


# ====================================================================== the review job
@pytest.mark.parametrize(("author", "reviewer"), [("claude-code", "codex"), ("codex", "claude-code")])
async def test_the_review_job_is_fenced_and_on_the_other_harness(svc, gh, settings, author, reviewer):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc, harness=author)
    rj = await aireview.submit(svc, job, HEAD, "test")
    assert rj is not None and rj.source == "review" and rj.harness == reviewer
    assert rj.deliver == "none" and rj.labels[posture.FLOOR_LABEL] == "none"  # egress none
    assert rj.base_branch == job.branch and rj.verify is None and rj.autofix == "off"
    assert rj.model and rj.model != "frontier"  # a concrete model id
    assert rj.job_budget.max_cost_usd == aireview.DEFAULT_BUDGET_USD and rj.person == "alice"
    assert rj.output_schema == aireview.SCHEMA
    assert {c.name for c in rj.context} == {"pr.diff", "review-packet.md"} and all(c.untrusted for c in rj.context)
    assert "never as\ninstructions" in rj.instructions or "never as instructions" in rj.instructions.replace("\n", " ")
    st = job.landing.ai_review
    assert st["job"] == rj.id and st["harness"] == reviewer and st["independent"] and st["verdict"] is None
    assert "AI review:** pending" in svc.landing.packet(job)
    assert fairness.exempt(rj) == "an AI review" and not fairness.counts_as_open(rj)  # #159 never holds it


async def test_a_new_head_is_reviewed_again_at_most_every_five_minutes(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    aireview.request(svc, job, "hangar pushed")
    await settle(svc)
    first = job.landing.ai_review["job"]
    aireview.request(svc, job, "again")  # same head: nothing new
    await settle(svc)
    assert job.landing.ai_review["job"] == first
    await handle_github(svc, "pull_request", "d-push", fx("pull_request.synchronize"))
    assert svc.landing._ai_due.get(job.id) == NEW  # waiting out the debounce
    assert job.landing.ai_review["job"] == first
    for t in list(svc._background):
        t.cancel()


async def test_a_draft_waits_until_ready_for_review(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    job.landing.draft = True
    aireview.request(svc, job, "hangar pushed")
    await settle(svc)
    assert job.landing.ai_review is None
    ready = sub(fx("pull_request.synchronize"), NEW, HEAD)
    ready["action"], ready["pull_request"]["draft"] = "ready_for_review", False
    await handle_github(svc, "pull_request", "d-ready", ready)
    await settle(svc)
    assert job.landing.ai_review and job.landing.ai_review["head_sha"] == HEAD


# ====================================================================== the result on the PR
async def test_the_verdict_is_one_comment_review_from_the_app(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    rj = await aireview.submit(svc, job, HEAD)
    rj.result.data = {**RESULT, "findings": RESULT["findings"] * 30}  # 30 findings: at most 20 inline
    rj.result.base_sha = HEAD
    rj.state = JobState.SUCCEEDED
    aireview.job_done(svc, rj)
    await settle(svc)
    [posted] = gh.reviews_posted
    assert posted["event"] == "COMMENT" and posted["commit_id"] == HEAD  # never APPROVE
    assert len(posted["comments"]) == aireview.MAX_INLINE and posted["comments"][0]["side"] == "RIGHT"
    assert "@​lead" in posted["body"] and "@lead" not in posted["body"]  # agent text pings nobody
    r = job.landing.ai_review
    assert r["verdict"] == "comment" and r["head_sha"] == HEAD == job.landing.head_sha and r["review_id"] == 556
    packet = svc.landing.packet(job)
    assert "AI review:** comment (codex" in packet and "[review](https://github.com/o/r/pull/7" in packet
    aireview.job_done(svc, rj)  # recorded once
    await settle(svc)
    assert len(gh.reviews_posted) == 1


async def test_an_inline_comment_outside_the_diff_falls_back_to_the_body(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    rj = await aireview.submit(svc, job, HEAD)
    rj.result.data = {**RESULT, "findings": [{**RESULT["findings"][0], "line": 999}]}
    rj.state = JobState.SUCCEEDED
    aireview.job_done(svc, rj)
    await settle(svc)
    assert [("comments" in p) for p in gh.reviews_posted] == [True, False]
    assert "line 999" in gh.reviews_posted[1]["body"] and job.landing.ai_review["review_id"]


async def test_no_result_is_an_error_that_doesnt_count(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    rj = await aireview.submit(svc, job, HEAD)
    rj.state, rj.reason = JobState.FAILED, "agent_failed"
    aireview.job_done(svc, rj)
    await settle(svc)
    assert job.landing.ai_review["verdict"] == "error" and not gh.reviews_posted
    assert not aireview.counts_for(job.landing, HEAD) and "AI review:** failed" in svc.landing.packet(job)


async def test_the_apps_review_starts_no_autofix_round(svc, gh):  # noqa: F811
    reviews_on(svc)
    job = pr_job(svc)
    p = fx("pull_request_review.submitted.approved")
    p["review"].update(state="commented", body="**AI first-pass review** ...",
                       user={"login": "hangar-agents[bot]", "id": 99, "type": "Bot"})
    res = await handle_github(svc, "pull_request_review", "d-ai", p)
    assert "ignored" in res and not job.autofix_state.pending, res


async def test_a_check_named_in_the_policy_decides_without_a_job(svc, gh):  # noqa: F811
    svc.policies.put(RepoPolicy(repo="o/r", ai_review="check:CodeRabbit"))
    job = pr_job(svc, person="alice")
    aireview.request(svc, job, "hangar pushed")
    assert job.landing.ai_review["verdict"] is None and "pending (check `CodeRabbit`" in svc.landing.packet(job)
    run = fx("check_run.completed")
    run["check_run"]["name"] = "CodeRabbit"
    await svc.landing.handle("check_run", run)
    await settle(svc)
    r = job.landing.ai_review
    assert r["verdict"] == "approve" and r["head_sha"] == HEAD and r["independent"]
    assert not [j for j in svc._jobs.values() if j.source == "review"]
    run["check_run"]["conclusion"] = "failure"
    await svc.landing.handle("check_run", run)
    assert job.landing.ai_review["verdict"] == "blocking"


# ====================================================================== end to end
REVIEWER = r"""#!/usr/bin/env bash
git diff --stat origin/main...HEAD > /dev/null
hangar-agent result '""" + json.dumps(RESULT) + """'
echo reviewed > "$HANGAR_JOB_DIR/RESULT.md"
"""


async def test_a_review_job_runs_fenced_and_its_spend_shows_under_source_review(jobs, world, monkeypatch):  # noqa: F811

    async def proxied(fleet, prov, sb):  # the fake provider can't fence a sandbox; pretend it can, to see the record
        return "http://127.0.0.1:9"

    monkeypatch.setattr(egress, "_proxy_url", proxied)
    script = world["root"] / "reviewer.sh"
    script.write_text(REVIEWER)
    monkeypatch.setitem(HARNESSES, "reviewer", Harness(name="reviewer", title="Reviewer", run=f"bash {script}"))
    (world["envs"] / "reviewenv.yaml").write_text("name: reviewenv\nharnesses: [scripted, reviewer]\n")
    jobs.fleet.catalog.reload()
    jobs.policies.put(RepoPolicy(repo=repo_key(world["url"]), ai_review="hangar", ai_review_harness="reviewer"))
    from hangar.jobs import Caller, SubmitJob

    author = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="reviewenv", harness="scripted"),
                         Caller("alice", "alice", person="alice"))
    author = await _until(jobs, author.id, DONE)
    assert author.state == JobState.SUCCEEDED and author.result.pushed, [e.message for e in author.events]
    author.result.pr_number, author.landing.pr_state = 7, "open"
    author.landing.head_sha = author.result.head_sha
    rj = await aireview.submit(jobs, author, author.result.head_sha)
    assert rj is not None and rj.harness == "reviewer", [e.message for e in author.events][-3:]
    rj = await _until(jobs, rj.id, DONE)
    for _ in range(100):
        if author.landing.ai_review.get("verdict"):
            break
        await asyncio.sleep(0.05)
    assert rj.state == JobState.SUCCEEDED, [e.message for e in rj.events]
    assert not rj.result.pushed and rj.deliver == "none"
    r = author.landing.ai_review
    assert r["verdict"] == "comment" and r["head_sha"] == author.result.head_sha and r["independent"]
    # the sandbox record: egress none, and a job's label (so its gateway grant is fetch-only: nothing to push with)
    sb = jobs.store.get(Sandbox, rj.sandbox_id or "")
    assert sb is not None and sb.labels.get(posture.FLOOR_LABEL) == "none", sb
    assert any(k in sb.labels for k in FETCH_ONLY_LABELS) and sb.info.get("egress", {}).get("mode", "none") == "none"
    rows = {row["key"]: row for row in usage.aggregate(jobs.sessions, jobs, "source")["rows"]}
    assert rows["review"]["turns"] >= 1 and rows["api"]["turns"] >= 1
