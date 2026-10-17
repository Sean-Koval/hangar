"""The Friday harness review (#169): the queue of failed and abandoned jobs, failure labels (one per job, relabels
audited) exported through the outbox, the count toward 20, labels on the job view and the trace."""

from __future__ import annotations

import gzip
import json

import pytest
from fastapi.testclient import TestClient

from hangar import harness_review, outbox, traces
from hangar.app import create_app
from hangar.harness_review import LabelBody
from hangar.models import Job, JobState, Turn, now
from hangar.outbox import OutboxItem
from tests.conftest import FakeProvider
from tests.test_landing import gh, pr_job, svc  # noqa: F401 - fixtures

DAY = 86400


def _job(svc, title: str, state: JobState, age: float = DAY, **kw) -> Job:  # noqa: F811
    j = Job(title=title, instructions=f"fix {title}: customer token ghp_" + "a" * 36, environment="jobenv",
            repo="https://github.com/o/r.git", state=state, person="ana", turns=[Turn(run_id="run-1")], **kw)
    j.updated_at = now() - age
    svc.store.put(j)
    j.updated_at = now() - age  # put() may stamp it
    svc.store.put(j)
    return j


@pytest.fixture
def jobs(svc):  # noqa: F811
    failed = _job(svc, "failed", JobState.FAILED, reason="verify_failed")
    abandoned = pr_job(svc)
    abandoned.landing.pr_state, abandoned.landing.closed_at = "closed", now() - DAY
    abandoned.landing.abandon_reason = "too_big"
    svc.store.put(abandoned)
    infra = _job(svc, "infra", JobState.FAILED)
    infra.result.outcome_class = "infra_error"
    svc.store.put(infra)
    old = _job(svc, "old", JobState.FAILED, age=30 * DAY)
    fine = _job(svc, "fine", JobState.SUCCEEDED)
    trial = _job(svc, "trial", JobState.FAILED, source="experiment")
    return {"failed": failed, "abandoned": abandoned, "infra": infra, "old": old, "fine": fine, "trial": trial}


def test_the_queue_lists_last_weeks_failed_and_abandoned_jobs_with_links(svc, jobs):  # noqa: F811
    q = harness_review.queue(svc, 7 * DAY)
    rows = {r["job"]: r for r in q["rows"]}
    assert set(rows) == {jobs["failed"].id, jobs["abandoned"].id}
    assert rows[jobs["failed"].id]["why"] == ["failed"] and rows[jobs["abandoned"].id]["why"] == ["abandoned"]
    assert rows[jobs["abandoned"].id]["abandon_reason"] == "too_big"
    links = rows[jobs["failed"].id]["links"]
    assert links["job"].endswith(f"/api/jobs/{jobs['failed'].id}") and links["trace"].endswith("/trace")
    assert [r["job"] for r in q["rows"]][0] in rows  # newest first
    with_infra = {r["job"] for r in harness_review.queue(svc, 7 * DAY, include_infra=True)["rows"]}
    assert jobs["infra"].id in with_infra
    # labelled jobs drop out
    harness_review.label(svc, jobs["failed"], LabelBody(seq=12, category="env", fix="env"), "ana")
    assert {r["job"] for r in harness_review.queue(svc, 7 * DAY)["rows"]} == {jobs["abandoned"].id}


def test_a_label_is_stored_exported_and_a_relabel_audited(svc, jobs, settings):  # noqa: F811
    settings.outbox_url = str(settings.data_dir / "bucket")
    outbox.of(svc.fleet).sink = outbox.make_sink(settings.outbox_url)
    j = jobs["failed"]
    fl = harness_review.label(svc, j, LabelBody(seq=12, category="env", fix="env", note="no libpq",
                                                candidate_task=True), "ana")
    assert j.failure_label == fl and fl.seq == 12 and fl.labelled_by == "ana" and fl.candidate_task
    assert svc.store.get(Job, j.id).failure_label.category == "env"
    items = [i for i in svc.store.list_where(OutboxItem, job_id=j.id) if i.key.endswith("-label.json")]
    assert len(items) == 1 and "/jobs/" in items[0].key and f"/{j.id}/events/" in items[0].key
    ev = json.loads(gzip.decompress(outbox.of(svc.fleet)._blob(items[0].key).read_bytes()))
    assert ev["kind"] == "label" and ev["schema"] == "hangar.job_event/v1"
    assert ev["data"]["category"] == "env" and ev["data"]["seq"] == 12 and ev["data"]["candidate_task"] is True
    assert ev["data"]["ticket"].startswith("fix failed") and "ghp_" + "a" * 36 not in json.dumps(ev)  # redacted
    assert any(a.action == "review.label" for a in svc.store.audit_tail(20))
    again = harness_review.label(svc, j, LabelBody(seq=14, category="tooling"), "bo")
    assert j.failure_label == again and again.category == "tooling"
    relabel = [a for a in svc.store.audit_tail(20) if a.action == "review.relabel"]
    assert relabel and relabel[0].detail["before"]["category"] == "env"
    assert len([i for i in svc.store.list_where(OutboxItem, job_id=j.id) if i.key.endswith("-label.json")]) == 2
    with pytest.raises(harness_review.ReviewError):
        harness_review.label(svc, j, LabelBody(category="vibes"), "ana")
    with pytest.raises(harness_review.ReviewError):
        harness_review.label(svc, j, LabelBody(category="env", fix="pray"), "ana")


def test_status_counts_toward_20_with_the_category_histogram(svc):  # noqa: F811
    for i in range(20):
        j = _job(svc, f"j{i}", JobState.FAILED)
        harness_review.label(svc, j, LabelBody(category="env" if i % 2 else "model", candidate_task=i < 19), "ana")
    st = harness_review.status(svc)
    assert (st["labelled"], st["candidate_tasks"], st["reached"]) == (20, 19, False)
    assert st["categories"] == {"env": 10, "model": 10}
    j = _job(svc, "last", JobState.FAILED)
    harness_review.label(svc, j, LabelBody(category="flaky", candidate_task=True), "ana")
    assert harness_review.status(svc)["reached"]
    assert harness_review.queue(svc, 7 * DAY)["note"] == "20 reached: hand over to flightdeck"


def test_labels_show_on_the_trace_and_keep_artifacts_until_exported(svc, jobs):  # noqa: F811
    j = jobs["fine"]  # a succeeded job a reviewer still labels (2+ corrections)
    assert not outbox.failure(j)
    harness_review.label(svc, j, LabelBody(seq=3, category="instructions", fix="agents_md"), "ana")
    assert outbox.failure(j)  # retention (#189): its runs stay until exported
    out = traces.job_export(svc, j, sessions=[])
    root = next(sp for rs in out["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]
                if sp["name"] == "invoke_workflow hangar.job")
    attrs = {a["key"]: next(iter(a["value"].values())) for a in root["attributes"]}
    assert attrs["hangar.failure.category"] == "instructions" and attrs["hangar.failure.fix"] == "agents_md"
    assert attrs["hangar.failure.seq"] in (3, "3") and attrs["hangar.failure.labelled_by"] == "ana"


def test_rest_queue_label_and_job_view(settings):
    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        h = app.state.hangar
        h.fleet.providers["docker"] = FakeProvider()
        c.headers["Authorization"] = "Bearer t0ken"
        j = Job(title="t", instructions="x", environment="agent", repo="o/r", state=JobState.FAILED,
                turns=[Turn(run_id="run-1")])
        h.store.put(j)
        q = c.get("/api/review/queue", params={"since": "7d"}).json()
        assert [r["job"] for r in q["rows"]] == [j.id]
        bad = c.post(f"/api/jobs/{j.id}/failure-label", json={"category": "vibes"})
        assert bad.status_code == 400 and "category must be one of" in bad.text
        got = c.post(f"/api/jobs/{j.id}/failure-label", json={"seq": 7, "category": "env", "fix": "env",
                                                                "candidate_task": True}).json()
        assert got["label"]["seq"] == 7 and got["status"]["candidate_tasks"] == 1
        assert c.get(f"/api/jobs/{j.id}").json()["failure_label"]["category"] == "env"
        assert c.get("/api/review/queue").json()["rows"] == []
        assert c.get("/api/review/status").json()["categories"] == {"env": 1}
        assert c.get("/api/review/queue", params={"since": "soon"}).status_code == 400
