"""KAN-140 S6: the deck's Retry (the canned follow-up, same conversation) and Start over (`?fresh=1`, a new job)."""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from hangar import jobretry, metrics
from hangar.jobs import Caller, JobError, SubmitJob
from hangar.models import AuditEvent, JobState
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
SVC = Caller("svc", "svc")


def _audits(store, action: str) -> list[AuditEvent]:
    return [a for a in store.audit_tail(1000) if a.action == action]


async def test_retry_continues_the_same_conversation_with_the_canned_message(jobs, world, fleet):  # noqa: F811
    m = metrics.setup(True)
    world["mode"].write_text("fail")
    job = jobs.submit(SubmitJob(instructions="fix the export", repo=world["url"], environment="jobenv"), SVC)
    job = await _until(jobs, job.id, DONE)
    assert job.state == JobState.FAILED
    with pytest.raises(KeyError):
        jobretry.retry(jobs, job.id, Caller("other", "other"), "other")  # not theirs
    world["mode"].write_text("ok")
    got = jobretry.retry(jobs, job.id, SVC, None)
    assert got.id == job.id and len(got.turns) == 2
    t = got.turns[1]
    assert t.kind == "follow_up" and t.message == jobretry.RETRY_MESSAGE and t.origin == "retry"
    assert got.turns[0].origin is None
    a = _audits(fleet.store, "job.retry")
    assert [(x.target, x.detail["mode"]) for x in a] == [(job.id, "same")]
    assert any(x.detail.get("origin") == "retry" for x in _audits(fleet.store, "job.follow_up"))
    assert m.retries.labels("same")._value.get() == 1
    job = await _until(jobs, job.id, DONE)
    assert job.state == JobState.SUCCEEDED and job.session_id == got.session_id  # the same session
    view = jobretry.view(jobs, job, "same", job.id)
    assert view["retry"] == {"mode": "same", "of": job.id} and view["turns"][1]["origin"] == "retry"
    metrics.setup(False)


async def test_start_over_is_a_new_job_with_the_same_task(jobs, world, fleet):  # noqa: F811
    m = metrics.setup(True)
    world["mode"].write_text("fail")
    job = jobs.submit(SubmitJob(instructions="fix the export", title="Fix the export", repo=world["url"],
                                environment="jobenv", labels={"team": "pay"}, timeout_minutes=30), SVC)
    job = await _until(jobs, job.id, DONE)
    world["mode"].write_text("ok")
    new = jobretry.start_over(jobs, job.id, SVC, None)
    assert new.id != job.id and new.instructions == job.instructions and new.title == job.title
    assert new.repo == job.repo and new.environment == job.environment and new.timeout_minutes == 30
    assert new.labels == {"team": "pay", "retry_of": job.id} and new.session_id != job.session_id
    assert jobs.get(job.id).state == JobState.FAILED  # the old one stays as it ended
    a = _audits(fleet.store, "job.retry")
    assert [(x.target, x.detail["mode"], x.detail["new_job"]) for x in a] == [(job.id, "fresh", new.id)]
    assert m.retries.labels("fresh")._value.get() == 1
    new = await _until(jobs, new.id, DONE)
    assert new.state == JobState.SUCCEEDED and new.branch != job.branch
    # a running job is not started over (cancel it first); one from outside the team neither
    world["mode"].write_text("sleep")
    busy = jobs.submit(SubmitJob(instructions="slow", repo=world["url"], environment="jobenv"), SVC)
    await _until(jobs, busy.id, {JobState.RUNNING})
    with pytest.raises(JobError, match="cancel it first"):
        jobretry.start_over(jobs, busy.id, SVC, None)
    jobs.cancel(busy.id, requester="svc")
    await _until(jobs, busy.id, DONE)
    old = jobs.get(job.id)
    old.untrusted_source = "github:rando"
    with pytest.raises(JobError, match="outside the team"):
        jobretry.start_over(jobs, old.id, SVC, None)
    metrics.setup(False)


def test_the_route(settings):
    from hangar.app import create_app

    settings.admin_token, settings.public_url = "t0ken", "http://localhost:8787"
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        h = {"Authorization": "Bearer t0ken"}
        assert c.post("/api/jobs/job-00000000/retry", headers=h).status_code == 404
        assert c.post("/api/jobs/job-00000000/retry?fresh=1", headers=h).status_code == 404
        assert c.post("/api/jobs/job-00000000/retry").status_code == 401
