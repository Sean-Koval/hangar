"""KAN-320: hangar's traces carry the ticket and PR facts the work-system specs judge (flightdeck), so they need
neither GitHub nor Jira: the job span's `hangar.ticket.key` and `hangar.jira.ready_at`, and the `pr.open` event's
`hangar.pr.title`, `hangar.pr.head_ref` and `hangar.pr.tracker_line`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from hangar import jira, outbox, traces
from hangar.models import Job
from tests.test_intake import AUTO_TOKEN, door, fixture, post  # noqa: F401 - door is a fixture


def _job(**kw) -> Job:
    job = Job(title=kw.pop("title", "KAN-320: carry ticket facts"), instructions="do it",
              repo="https://github.com/acme/api.git", environment="tenv", branch="hangar/KAN-320-job-1", **kw)
    job.result.pr_url, job.result.pr_number = "https://github.com/acme/api/pull/7", 7
    return job


def _jobs(policy=None) -> SimpleNamespace:
    return SimpleNamespace(sessions=None, usage=lambda job: None, broker=SimpleNamespace(redact=lambda v: v),
                           store=None, policies=SimpleNamespace(get=lambda repo: policy))


def _export(monkeypatch, job: Job, policy=None) -> tuple[dict, dict]:
    monkeypatch.setattr(traces.testresults, "add_to_export", lambda *a, **k: None)
    out = traces.job_export(_jobs(policy), job, sessions=[], usage=None)
    [span] = out["resourceSpans"][0]["scopeSpans"][0]["spans"]
    attrs = {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}
    [pr] = [e for e in span["events"] if e["name"] == "pr.open"]
    return attrs, {a["key"]: next(iter(a["value"].values())) for a in pr["attributes"]}


def test_a_jira_jobs_trace_names_its_ticket_ready_time_and_pr(monkeypatch):
    job = _job(labels={"jira.issue": "KAN-320", jira.READY_LABEL: "2026-11-09T05:00:00Z"})
    attrs, pr = _export(monkeypatch, job)
    assert attrs["hangar.ticket.key"] == "KAN-320"
    assert attrs["hangar.jira.ready_at"] == "2026-11-09T05:00:00Z"
    assert pr["hangar.pr.title"] == "KAN-320: carry ticket facts"
    assert pr["hangar.pr.head_ref"] == "hangar/KAN-320-job-1"
    assert pr["hangar.pr.tracker_line"] is True
    assert pr["hangar.pr.number"] == "7"  # the old attributes are still there (OTLP ints are strings)


def test_a_job_without_a_ticket_has_no_key_and_no_tracker_line(monkeypatch):
    attrs, pr = _export(monkeypatch, _job(title="tidy the README"))
    assert "hangar.ticket.key" not in attrs and "hangar.jira.ready_at" not in attrs
    assert pr["hangar.pr.tracker_line"] is False and pr["hangar.pr.title"] == "tidy the README"


def test_the_pr_title_follows_the_repo_policys_template(monkeypatch):
    policy = SimpleNamespace(title_template="[{ticket}] {title}")
    job = _job(ticket="jira:PAY-412", title="retry uploads")
    _, pr = _export(monkeypatch, job, policy)
    assert "PAY-412" in pr["hangar.pr.title"] and "retry uploads" in pr["hangar.pr.title"]
    assert pr["hangar.pr.tracker_line"] is True


def test_the_outbox_pr_open_event_carries_the_same_facts():
    job = _job(labels={"jira.issue": "KAN-320"})
    box = SimpleNamespace(jobs=_jobs())
    [(kind, _, data, _)] = [x for x in outbox.Outbox.lifecycle(box, job) if x[0] == "pr.open"]
    assert data == {"draft": job.landing.draft, "title": "KAN-320: carry ticket facts",
                    "head_ref": "hangar/KAN-320-job-1", "tracker_line": True, "ticket": "KAN-320"}


def test_ready_label_is_utc_iso_and_empty_when_unknown():
    assert jira.READY_LABEL == traces.JIRA_READY_AT
    assert jira.ready_label(1760440000.0) == {jira.READY_LABEL: "2025-10-14T11:06:40Z"}
    assert jira.ready_label(None) == {} and jira.ready_label(float("nan")) == {} and jira.ready_label(0) == {}


async def test_the_jira_door_labels_a_webhook_job_with_its_ready_time(door):  # noqa: F811
    payload = fixture("jira_issue_updated.json")
    r = await post(door["client"], payload)
    assert r.status_code == 200 and r.json().get("ok"), r.json()
    job = door["svc"].get(r.json()["job"])
    want = datetime.fromtimestamp(payload["timestamp"] / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert job.labels[jira.READY_LABEL] == want


async def test_the_jira_door_takes_an_automation_jobs_ready_time_from_the_history(door):  # noqa: F811
    ts = fixture("jira_issue_updated.json")["timestamp"] / 1000
    now = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.0+0000")
    body = json.dumps({"webhookEvent": "hangar:automation", "timestamp": now, "issue": {"key": "ABC-12"}}).encode()
    r = await post(door["client"], {}, raw=body, headers={"X-Hangar-Token": AUTO_TOKEN,
                                                          "Content-Type": "application/json"})
    assert r.status_code == 200 and r.json().get("ok"), r.json()
    job = door["svc"].get(r.json()["job"])
    assert job.labels[jira.READY_LABEL] == "2025-10-14T11:06:00Z"  # FakeAPIs' history: moved to Ready for agent
