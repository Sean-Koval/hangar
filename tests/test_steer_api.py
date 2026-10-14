"""REST routes for live turns (workstream steer): wiring, validation and who may use them. The behaviour behind
them is covered end to end in test_steer.py."""

from __future__ import annotations

from hangar.models import Approval, SessionState, SessionTurn
from tests.test_app import client  # noqa: F401 - the fixture


def test_steer_routes_validate_and_need_a_running_turn(client):  # noqa: F811
    h = client.app.state.hangar
    assert client.post("/api/sessions/nope/steer", json={"message": "hi"}).status_code == 404
    assert client.post("/api/sessions/nope/interrupt").status_code == 404
    assert client.get("/api/sessions/nope/approvals").status_code == 404
    s = h.sessions.for_job(job_id="job-0000cafe", environment="plain", harness=None, model=None, repo=None,
                           base_branch=None, branch="hangar/job-0000cafe", provider=None, labels={}, extra_dirs=[],
                           idle_minutes=5)
    r = client.post(f"/api/sessions/{s.name}/steer", json={"message": "hi"})
    assert r.status_code == 400 and "no running turn" in r.json()["error"]
    assert client.post(f"/api/sessions/{s.name}/steer", json={"message": ""}).status_code == 422
    assert client.post(f"/api/sessions/{s.name}/steer", json={"message": "x" * 64_001}).status_code == 422
    assert client.post(f"/api/sessions/{s.name}/approvals/ap-0123456789ab",
                       json={"decision": "sure"}).status_code == 422
    # a turn that is running, with an approval waiting: listed; answering needs the driver, which isn't there
    s.turns.append(SessionTurn(message="m", run_id="run-0123abcd", started_at=1.0, live=True, approval_policy="ask",
                               approvals=[Approval(id="ap-0123456789ab", tool="Bash", detail="make deploy")]))
    s.sandbox_id, s.state = "sb-gone", SessionState.RUNNING
    v = client.get(f"/api/sessions/{s.name}/approvals").json()
    assert v["running"] and v["policy"] == "ask" and [a["id"] for a in v["pending"]] == ["ap-0123456789ab"]
    assert client.get(f"/api/sessions/{s.name}").json()["pending_approvals"][0]["detail"] == "make deploy"
    assert client.post(f"/api/sessions/{s.name}/approvals/ap-ffffffffffff",
                       json={"decision": "allow"}).status_code == 404
    r = client.post(f"/api/sessions/{s.name}/approvals/ap-0123456789ab", json={"decision": "allow"})
    assert r.status_code == 400 and "takes no commands" in r.json()["error"], r.text


def test_job_steer_routes_belong_to_the_jobs_owner(client):  # noqa: F811
    h = client.app.state.hangar
    raw, _ = h.auth.create("svc", ["jobs"])
    svc = {"Authorization": f"Bearer {raw}"}
    assert client.post("/api/sessions/any/steer", headers=svc, json={"message": "hi"}).status_code == 403
    assert client.post("/api/jobs/job-nope/steer", headers=svc, json={"message": "hi"}).status_code == 404
    assert client.get("/api/jobs/job-nope/approvals", headers=svc).status_code == 404
    assert client.post("/api/jobs/job-nope/interrupt").status_code == 404
