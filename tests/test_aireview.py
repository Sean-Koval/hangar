"""AI first-pass review (#161): an independent review of every agent PR, by a hangar job on another harness (or an
org review bot's check), recorded on `landing.ai_review` and posted as one COMMENT review from the App. KAN-230: a
cheaper second pass keeps only the findings it confirms, ranked by severity, with at most 5 inline comments."""

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
          "findings": [{"path": "src/app.py", "line": 3, "severity": "minor", "text": "Rename x, ask @lead",
                        "evidence": "print('hi')"}]}
QUOTE = "+print('hi')"  # a line of the fake PR diff (tests.test_landing.GH)


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
    assert rj.job_budget.max_cost_usd == round(aireview.DEFAULT_BUDGET_USD * aireview.REVIEW_SHARE, 2)
    assert rj.person == "alice"
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
async def review(svc, job, result, confirm=None):  # noqa: F811
    """The first pass answers `result`; the confirmation pass (when one starts) answers `confirm`. Returns it."""
    rj = await aireview.submit(svc, job, HEAD)
    rj.result.data, rj.result.base_sha, rj.state = result, HEAD, JobState.SUCCEEDED
    aireview.job_done(svc, rj)
    await settle(svc)
    cid = (job.landing.ai_review or {}).get("confirm_job")
    if cid is None:
        return None
    cj = svc._jobs[cid]
    if confirm is None:
        cj.state, cj.reason = JobState.FAILED, "agent_failed"
    else:
        cj.result.data, cj.state = confirm, JobState.SUCCEEDED
    aireview.job_done(svc, cj)
    await settle(svc)
    return cj


def yes(i, kind="quote", evidence=QUOTE):
    return {"id": i, "confirmed": True, "kind": kind, "evidence": evidence}


async def test_the_verdict_is_one_comment_review_from_the_app(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    # 30 findings, all confirmed: most severe first, at most 5 inline, the rest counted in the packet
    sev = ["minor", "info", "critical", "major", "minor", "info"] * 5
    found = [{**RESULT["findings"][0], "line": i % 8 + 1, "severity": s} for i, s in enumerate(sev)]  # near 3
    await review(svc, job, {**RESULT, "findings": found}, {"findings": [yes(i) for i in range(30)]})
    [posted] = gh.reviews_posted
    assert posted["event"] == "COMMENT" and posted["commit_id"] == HEAD  # never APPROVE
    assert len(posted["comments"]) == aireview.DEFAULT_INLINE_MAX and posted["comments"][0]["side"] == "RIGHT"
    assert [c["body"].split("]")[0] for c in posted["comments"]] == ["[critical"] * 5
    assert "Evidence (quote)" in posted["comments"][0]["body"]
    assert "@​lead" in posted["body"] and "@lead" not in posted["body"]  # agent text pings nobody
    r = job.landing.ai_review
    assert [f["severity"] for f in r["findings"]][:15] == ["critical"] * 5 + ["major"] * 5 + ["minor"] * 5
    assert r["verdict"] == "comment" and r["head_sha"] == HEAD == job.landing.head_sha and r["review_id"] == 556
    packet = svc.landing.packet(job)
    assert "AI review:** comment (codex" in packet and "[review](https://github.com/o/r/pull/7" in packet
    assert "30 confirmed finding(s) (5 inline, 25 in the review body only)" in packet
    aireview.job_done(svc, svc._jobs[r["job"]])  # recorded once
    aireview.job_done(svc, svc._jobs[r["confirm_job"]])
    await settle(svc)
    assert len(gh.reviews_posted) == 1


async def test_the_check_runs_cheaper_inside_the_same_budget(svc, gh, settings):  # noqa: F811
    from hangar import modelpolicy

    reviews_on(svc)
    job = agent_job(svc)
    rj = await aireview.submit(svc, job, HEAD)
    rj.result.data, rj.result.base_sha, rj.state = RESULT, HEAD, JobState.SUCCEEDED
    aireview.job_done(svc, rj)
    await settle(svc)
    r = job.landing.ai_review
    assert r["verdict"] is None and "pending (checking 1 finding(s) in job" in svc.landing.packet(job)
    cj = svc._jobs[r["confirm_job"]]
    assert cj.source == "review" and cj.labels[aireview.PHASE_LABEL] == "confirm" and cj.harness == rj.harness
    assert cj.model == modelpolicy.of(svc.fleet).concrete(rj.harness, "balanced") != rj.model  # the cheaper tier
    assert cj.deliver == "none" and cj.labels[posture.FLOOR_LABEL] == "none" and cj.autofix == "off"
    assert cj.output_schema == aireview.CONFIRM_SCHEMA and cj.person == "alice"
    assert rj.job_budget.max_cost_usd + cj.job_budget.max_cost_usd <= aireview.DEFAULT_BUDGET_USD
    assert {c.name for c in cj.context} == {"pr.diff", "findings.json"} and all(c.untrusted for c in cj.context)
    claims = json.loads(next(c.content for c in cj.context if c.name == "findings.json"))
    assert claims[0]["id"] == 0 and claims[0]["evidence"] == "print('hi')"
    assert fairness.exempt(cj) == "an AI review" and not fairness.counts_as_open(cj)


async def test_only_findings_with_evidence_are_kept_and_the_packet_counts_the_dropped(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    found = [{**RESULT["findings"][0], "line": i + 1, "text": f"finding {i}"} for i in range(5)]
    await review(svc, job, {**RESULT, "findings": found}, {"findings": [
        yes(0),  # a line the diff has
        yes(1, "command", "uv run pytest tests/test_app.py -k rename\nFAILED test_rename"),
        yes(2, evidence="+import os  # not in the diff"),  # a quote the diff doesn't have
        {"id": 3, "confirmed": False, "kind": "command", "evidence": "couldn't reproduce"},
        yes(9),  # no such finding
    ]})
    r = job.landing.ai_review
    assert [f["text"] for f in r["findings"]] == ["finding 0", "finding 1"] and r["unconfirmed"] == 3
    assert [f["evidence_kind"] for f in r["findings"]] == ["quote", "command"]
    [posted] = gh.reviews_posted
    assert len(posted["comments"]) == 2 and "3 more finding(s) dropped" in posted["body"]
    assert "evidence (command, unverified: hangar didn't run it)" in posted["body"]  # the checker's word
    assert [f["verified"] for f in r["findings"]] == [True, False]
    assert "2 confirmed finding(s) (2 inline, 0 in the review body only), 3 unconfirmed finding(s) dropped" \
        in svc.landing.packet(job)


@pytest.mark.parametrize(("severity", "verdict"), [("minor", "comment"), ("major", "blocking"),
                                                   ("critical", "blocking")])
async def test_blocking_needs_a_confirmed_high_severity_finding(svc, gh, settings, severity, verdict):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    found = [{**RESULT["findings"][0], "severity": severity}, {**RESULT["findings"][0], "severity": "critical"}]
    no = {"id": 1, "confirmed": False, "kind": "command", "evidence": "couldn't reproduce"}
    await review(svc, job, {**RESULT, "verdict": "blocking", "findings": found}, {"findings": [yes(0), no]})
    r = job.landing.ai_review
    assert r["verdict"] == verdict and r["unconfirmed"] == 1  # the critical one wasn't confirmed
    assert aireview.counts_for(job.landing, HEAD) == (verdict != "blocking")


CRITICAL = {**RESULT, "verdict": "blocking", "findings": [{**RESULT["findings"][0], "severity": "critical"}]}


def still_blocking(job, note: str) -> None:
    r = job.landing.ai_review
    assert r["verdict"] == "blocking" and r["findings"] == [] and note in (r.get("confirm_note") or "")
    assert not aireview.counts_for(job.landing, HEAD)  # no self-approval on it


@pytest.mark.parametrize(("state", "reason"), [(JobState.FAILED, "agent_failed"), (JobState.FAILED, "timeout"),
                                               (JobState.CANCELLED, "cancelled")])
async def test_a_check_that_didnt_complete_never_lifts_a_block(svc, gh, settings, state, reason):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    rj = await aireview.submit(svc, job, HEAD)
    rj.result.data, rj.result.base_sha, rj.state = CRITICAL, HEAD, JobState.SUCCEEDED
    aireview.job_done(svc, rj)
    await settle(svc)
    cj = svc._jobs[job.landing.ai_review["confirm_job"]]
    cj.state, cj.reason = state, reason
    cj.result.data = {"findings": [{"id": 0, "confirmed": False, "kind": "quote", "evidence": "x"}]}  # partial
    aireview.job_done(svc, cj)
    await settle(svc)
    still_blocking(job, "the check produced no result")
    r = job.landing.ai_review
    assert r["unconfirmed"] == 1 and r["unchecked"]
    [posted] = gh.reviews_posted
    assert "comments" not in posted and "the check didn't complete" in posted["body"]
    assert "1 unchecked finding(s) dropped" in svc.landing.packet(job)


async def test_a_check_with_no_answer_never_lifts_a_block(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    await review(svc, job, CRITICAL, {"findings": []})  # it ran, but said nothing about the critical finding
    still_blocking(job, "")
    job2 = agent_job(svc)
    await review(svc, job2, CRITICAL, {"findings": [{"id": 0, "kind": "quote", "evidence": "x"}]})  # no `confirmed`
    still_blocking(job2, "")


async def test_a_check_of_another_head_never_lifts_a_block(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    rj = await aireview.submit(svc, job, HEAD)
    rj.result.data, rj.result.base_sha, rj.state = CRITICAL, HEAD, JobState.SUCCEEDED
    aireview.job_done(svc, rj)
    await settle(svc)
    cj = svc._jobs[job.landing.ai_review["confirm_job"]]
    assert cj.labels[aireview.HEAD_LABEL] == HEAD  # what pin_head checks out
    cj.result.data, cj.result.base_sha, cj.state = {"findings": [{"id": 0, "confirmed": False, "kind": "quote",
                                                                  "evidence": "x"}]}, NEW, JobState.SUCCEEDED
    aireview.job_done(svc, cj)
    await settle(svc)
    still_blocking(job, "not the reviewed head")


async def test_a_skipped_check_or_no_findings_never_lifts_a_block(svc, gh, settings):  # noqa: F811
    reviews_on(svc, ai_review_budget_usd=0.06)  # the first pass may spend 0.04: the check gets 0.02, too little
    job = agent_job(svc)
    assert await review(svc, job, {**RESULT, "verdict": "blocking", "findings": []}) is None  # zero findings
    assert job.landing.ai_review["verdict"] == "blocking" and len(gh.reviews_posted) == 1
    assert not aireview.counts_for(job.landing, HEAD)
    job2 = agent_job(svc)
    assert await review(svc, job2, CRITICAL) is None  # skipped: no budget left for it
    still_blocking(job2, "no budget left")
    job3 = agent_job(svc)
    assert await review(svc, job3, RESULT) is None  # a comment stays a comment
    r = job3.landing.ai_review
    assert r["verdict"] == "comment" and r["unconfirmed"] == 1 and "no budget left" in r["confirm_note"]


async def test_a_check_that_couldnt_start_never_lifts_a_block(svc, gh, settings, monkeypatch):  # noqa: F811
    from hangar.jobs import JobError

    reviews_on(svc)
    job = agent_job(svc)
    rj = await aireview.submit(svc, job, HEAD)
    rj.result.data, rj.result.base_sha, rj.state = CRITICAL, HEAD, JobState.SUCCEEDED

    def refuse(*a, **kw):
        raise JobError("queue full")

    monkeypatch.setattr(svc, "_submit", refuse)
    aireview.job_done(svc, rj)
    await settle(svc)
    still_blocking(job, "the check couldn't start")


# ====================================================================== after a restart
async def first_pass(svc, job, result):  # noqa: F811
    rj = await aireview.submit(svc, job, HEAD)
    rj.result.data, rj.result.base_sha, rj.state = result, HEAD, JobState.SUCCEEDED
    return rj


async def test_restore_sends_unchecked_candidates_to_the_check(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    rj = await first_pass(svc, job, CRITICAL)
    r = job.landing.ai_review
    r.update(candidates=aireview._findings(CRITICAL), proposed_verdict="blocking")  # down before _confirm ran
    aireview.restore(svc)
    await settle(svc)
    r = job.landing.ai_review
    assert r["verdict"] is None and r["confirm_job"] and r["job"] == rj.id
    cj = svc._jobs[r["confirm_job"]]
    cj.result.data, cj.result.base_sha, cj.state = {"findings": [yes(0)]}, HEAD, JobState.SUCCEEDED
    aireview.restore(svc)  # the check ended while hangar was down
    await settle(svc)
    r = job.landing.ai_review
    assert r["verdict"] == "blocking" and len(r["findings"]) == 1 and len(gh.reviews_posted) == 1


def gone(svc, job_id: str) -> None:  # noqa: F811
    from hangar.models import Job

    svc._jobs.pop(job_id)
    svc.store.delete(Job, job_id)


async def test_a_missing_check_job_record_times_out_to_an_error(svc, gh, settings, monkeypatch):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    rj = await first_pass(svc, job, CRITICAL)
    aireview.job_done(svc, rj)
    await settle(svc)
    cid = job.landing.ai_review["confirm_job"]
    gone(svc, cid)  # its record is gone: nothing will ever answer
    monkeypatch.setattr(aireview, "MISSING_SECONDS", 0.05)
    aireview.restore(svc)
    assert job.landing.ai_review["verdict"] is None  # not at once: it waits MISSING_SECONDS from the check's start
    for _ in range(100):
        if job.landing.ai_review.get("verdict"):
            break
        await asyncio.sleep(0.01)
    for t in list(svc._background):  # the new review it asks for waits out the debounce
        t.cancel()
    r = job.landing.ai_review
    assert r["verdict"] == "error" and cid in r["error"] and not aireview.counts_for(job.landing, HEAD)
    assert "candidates" not in r and "AI review:** failed" in svc.landing.packet(job)
    assert not gh.reviews_posted


async def test_a_long_missing_review_job_record_is_an_error_at_once(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    rj = await first_pass(svc, job, CRITICAL)
    gone(svc, rj.id)
    job.landing.ai_review["requested_at"] -= aireview.MISSING_SECONDS + 1
    aireview.restore(svc)
    r = job.landing.ai_review
    assert r["verdict"] == "error" and svc.landing._ai_due.get(job.id) == HEAD  # and a new review is asked for
    assert any(f"the review job {rj.id} is gone" in e.message for e in job.events)
    for t in list(svc._background):
        t.cancel()


class _Sh:
    def __init__(self, code=0):
        self.code, self.commands = code, []

    async def __call__(self, job, command, cwd=None, timeout=120):
        self.commands.append((command, cwd))
        return type("R", (), {"exit_code": self.code, "stdout": "", "stderr": "no such commit"})()


async def test_the_check_checks_out_the_reviewed_head_not_the_tip():
    from types import SimpleNamespace

    from hangar.jobs import JobError
    from hangar.models import Job

    sb = SimpleNamespace(info={"repo_path": "/w/r"})
    sh = _Sh()
    svc_ = SimpleNamespace(fleet=SimpleNamespace(get=lambda _id: sb), _sh=sh)
    cj = Job(title="c", instructions="x", environment="e", source="review", sandbox_id="sb",
             labels={aireview.PHASE_LABEL: "confirm", aireview.HEAD_LABEL: HEAD})
    s = SimpleNamespace(base_sha=NEW, branch="hangar/job-c")  # the tip moved on after the first pass
    await aireview.pin_head(svc_, cj, s)
    [(cmd, cwd)] = sh.commands
    assert cwd == "/w/r" and f"git checkout -q -B hangar/job-c {HEAD}" in cmd
    assert s.base_sha == cj.result.base_sha == HEAD
    await aireview.pin_head(svc_, cj, s)  # already there: nothing to do
    first = Job(title="r", instructions="x", environment="e", source="review", sandbox_id="sb",
                labels={aireview.HEAD_LABEL: HEAD})
    await aireview.pin_head(svc_, first, SimpleNamespace(base_sha=NEW, branch="b"))  # the first pass: untouched
    assert len(sh.commands) == 1
    svc_._sh = _Sh(code=1)
    with pytest.raises(JobError, match="reviewed head"):
        await aireview.pin_head(svc_, cj, SimpleNamespace(base_sha=NEW, branch="b"))


async def test_a_tiny_budget_runs_no_review_and_doesnt_crash(svc, gh, settings):  # noqa: F811
    reviews_on(svc, ai_review_budget_usd=0.005)  # 70% rounds to $0.00
    job = agent_job(svc)
    assert await aireview.submit(svc, job, HEAD) is None
    assert job.landing.ai_review is None and "too small for a review" in job.events[-1].message


async def test_the_policy_sets_the_inline_cap(svc, gh, settings):  # noqa: F811
    reviews_on(svc, ai_review_inline_max=2)
    job = agent_job(svc)
    found = [{**RESULT["findings"][0], "line": i + 1} for i in range(4)]
    await review(svc, job, {**RESULT, "findings": found}, {"findings": [yes(i) for i in range(4)]})
    assert len(gh.reviews_posted[0]["comments"]) == 2
    assert "4 confirmed finding(s) (2 inline, 2 in the review body only)" in svc.landing.packet(job)
    with pytest.raises(ValueError):
        RepoPolicy(repo="o/r", ai_review_inline_max=21)


def test_a_quote_must_be_an_added_line_of_the_findings_file_near_its_line():
    diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n-old = 1\n+new = 2\n+more = 3\n ctx()\n"
            "@@ -40 +41,2 @@\n tail()\n+far = 4\n"
            "diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -0,0 +1 @@\n+other = 5\n"
            "diff --git a/z b/z\n--- a/z\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone = 6\n")
    q = aireview._quoted
    assert q("new = 2", diff, "x", 1) and q("+new = 2", diff, "x", 1) and q("new = 2\n+more = 3", diff, "x", 2)
    assert q("far = 4", diff, "x", 42) and q("far = 4", diff, "./x", None)  # no line: anywhere in its file
    assert not q("far = 4", diff, "x", 1)  # 41 lines away from the finding
    assert not q("other = 5", diff, "x", 1) and q("other = 5", diff, "y", 1)  # another file's line
    assert not q("old = 1", diff, "x", 1) and not q("-old = 1", diff, "x", 1)  # a removed line
    assert not q("ctx()", diff, "x", 3) and not q("gone = 6", diff, "z", 1)  # context, a deleted file
    assert not q("new = 3", diff, "x", 1) and not q("new", diff, "x", 1)
    assert not q("  ", diff, "x", 1) and not q("+++ b/x", diff, "x", 1)


async def test_an_inline_comment_outside_the_diff_falls_back_to_the_body(svc, gh, settings):  # noqa: F811
    reviews_on(svc)
    job = agent_job(svc)
    await review(svc, job, {**RESULT, "findings": [{**RESULT["findings"][0], "line": 999}]},
                 {"findings": [yes(0, "command", "pytest -k app\nFAILED")]})
    assert [("comments" in p) for p in gh.reviews_posted] == [True, False]
    assert "line 999" in gh.reviews_posted[1]["body"] and job.landing.ai_review["review_id"]
    assert "(0 inline, 1 in the review body only)" in svc.landing.packet(job)


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
BOTH = {**RESULT, "findings": [{**RESULT["findings"][0], **yes(0, "command", "git diff --stat")}]}
REVIEWER = r"""#!/usr/bin/env bash
git diff --stat origin/main...HEAD > /dev/null
hangar-agent result '""" + json.dumps(BOTH) + """'
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
    for _ in range(400):
        if author.landing.ai_review.get("verdict"):
            break
        await asyncio.sleep(0.05)
    assert rj.state == JobState.SUCCEEDED, [e.message for e in rj.events]
    assert not rj.result.pushed and rj.deliver == "none"
    r = author.landing.ai_review
    assert r["verdict"] == "comment" and r["head_sha"] == author.result.head_sha and r["independent"]
    assert len(r["findings"]) == 1 and r["findings"][0]["evidence_kind"] == "command"  # the check confirmed it
    cj = jobs.store.get(type(rj), r["confirm_job"])
    assert cj is not None and cj.state == JobState.SUCCEEDED and cj.labels[aireview.PHASE_LABEL] == "confirm"
    assert cj.result.base_sha == r["head_sha"]  # it read the head the first pass reviewed
    # the sandbox record: egress none, and a job's label (so its gateway grant is fetch-only: nothing to push with)
    sb = jobs.store.get(Sandbox, rj.sandbox_id or "")
    assert sb is not None and sb.labels.get(posture.FLOOR_LABEL) == "none", sb
    assert any(k in sb.labels for k in FETCH_ONLY_LABELS) and sb.info.get("egress", {}).get("mode", "none") == "none"
    rows = {row["key"]: row for row in usage.aggregate(jobs.sessions, jobs, "source")["rows"]}
    assert rows["review"]["turns"] >= 1 and rows["api"]["turns"] >= 1
