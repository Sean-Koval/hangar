"""HTTP surface: auth on REST + MCP, secrets never echoed."""

import pytest
from fastapi.testclient import TestClient

from hangar.app import create_app
from hangar.models import CredentialKind
from tests.conftest import FakeProvider


@pytest.fixture
def client(settings):
    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.hangar.fleet.providers["docker"] = FakeProvider()
        app.state.hangar.fleet.provider_errors.pop("docker", None)
        c.headers["Authorization"] = "Bearer t0ken"
        yield c


def test_auth_required(client):
    assert client.get("/api/overview", headers={"Authorization": ""}).status_code == 401
    assert client.post("/mcp/", headers={"Authorization": "Bearer wrong"}, json={}).status_code == 401
    assert client.get("/healthz").json() == {"ok": True}


def test_credentials_never_echo_secret(client):
    r = client.post("/api/credentials", json={"name": "k", "harness": "claude-code", "kind": "api_key",
                                              "value": "sk-ant-super-secret"})
    assert r.status_code == 200 and "super-secret" not in r.text
    assert "super-secret" not in client.get("/api/credentials").text
    assert "super-secret" not in client.get("/api/audit").text


def test_sandbox_lifecycle_over_rest(client):
    [sb] = client.post("/api/sandboxes", json={"environment": "plain", "name": "web"}).json()
    assert sb["state"] == "running"
    r = client.post(f"/api/sandboxes/{sb['id']}/exec", json={"command": "echo hi"}).json()
    assert r["stdout"].strip() == "hi"
    assert client.delete("/api/sandboxes/web").json()["state"] == "stopped"
    assert client.get("/api/sandboxes/nope").status_code == 404


def test_environment_edit_and_context(client, settings):
    r = client.put("/api/environments/mine", content="extends: plain\ndescription: mine\n")
    assert r.status_code == 200 and (settings.saved_env_dir / "mine.yaml").exists()  # on the data volume
    ctx = client.get("/api/environments/mine/context")
    assert ctx.headers["X-Hangar-Image"].startswith("hangar-env/mine:")
    assert ctx.content[:100]  # tarball


def test_connect_snippets(client):
    d = client.get("/api/connect").json()
    assert d["mcp_url"].endswith("/mcp")
    assert 'bearer_token_env_var = "HANGAR_TOKEN"' in d["codex_config_toml"]


def test_tickets_are_single_use_and_tokens_not_accepted_in_urls(client):
    job = {"id": "nope"}
    t = client.post("/api/tickets").json()["ticket"]
    assert t.startswith("tkt_")
    # valid ticket for a missing build -> 404 (auth passed); reused ticket -> 401
    assert client.get(f"/api/builds/{job['id']}/stream?ticket={t}", headers={"Authorization": ""}).status_code == 404
    assert client.get(f"/api/builds/{job['id']}/stream?ticket={t}", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/builds/x/stream?token=t0ken", headers={"Authorization": ""}).status_code == 401


def test_mcp_path_without_trailing_slash_is_not_redirected(client):
    r = client.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={}, follow_redirects=False)
    assert r.status_code == 401  # reached the MCP auth layer, no 307


def test_api_docs_not_public(client):
    assert client.get("/openapi.json", headers={"Authorization": ""}).status_code in (401, 404)


def test_jobs_scope_and_ownership(client):
    h = client.app.state.hangar
    svc_raw, _ = h.auth.create("svc", ["jobs"])
    other_raw, _ = h.auth.create("other", ["jobs"])
    svc = {"Authorization": f"Bearer {svc_raw}"}
    base = {"instructions": "write notes", "deliver": "none", "context": [{"name": "a", "content": "b"}]}
    # service tokens only reach repos the operator mapped, in the mapped environments
    assert client.post("/api/jobs", headers=svc, json={**base, "environment": "plain"}).status_code == 400
    h.settings.github_repos = {"acme/app": "plain"}
    repo = {"repo": "https://github.com/acme/app"}
    for bad in [{"environment": "minimal"}, {"provider": "docker"}, {"callback_secret": "x"},
                {"branch": "main"}, {"repo": "https://github.com/acme/other"}]:
        assert client.post("/api/jobs", headers=svc, json={**base, **repo, **bad}).status_code == 400, bad
    r = client.post("/api/jobs", headers=svc, json={**base, **repo})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["requester"] == "svc" and job["context"] == [{"name": "a", "kind": "text", "size": 1, "url": None}]
    # the service token is limited to jobs
    assert client.get("/api/sandboxes", headers=svc).status_code == 403
    assert client.post("/mcp/", headers=svc, json={}).status_code == 401
    # other services can't see or touch it; admin can
    other = {"Authorization": f"Bearer {other_raw}"}
    assert client.get(f"/api/jobs/{job['id']}", headers=other).status_code == 404
    assert client.get("/api/jobs", headers=other).json() == []
    assert client.get(f"/api/jobs/{job['id']}").json()["id"] == job["id"]
    assert client.post("/api/tokens", json={"name": "x", "scopes": ["root"]}).status_code == 400
    assert client.post("/api/tokens", json={"name": "admin", "scopes": ["jobs"]}).status_code == 400
    assert client.post("/api/tokens", json={"name": "github:alice", "scopes": ["jobs"]}).status_code == 400
    # a jobs token can't be traded for an MCP (full access) OAuth grant
    assert h.auth.identify(svc_raw) is None and h.auth.identify(client.headers["Authorization"][7:]) is not None
    # ownership is by token id, not name: a second token called "svc" sees nothing
    twin_raw, _ = h.auth.create("svc", ["jobs"])
    assert client.get("/api/jobs", headers={"Authorization": f"Bearer {twin_raw}"}).json() == []


def test_github_webhook_signature(client):
    import hashlib
    import hmac as _hmac

    h = client.app.state.hangar
    assert client.post("/api/hooks/github", content=b"{}").status_code == 404  # off until configured
    h.broker.put("gh-hook", "webhook", CredentialKind.GENERIC, "whsec")
    h.settings.github_webhook_secret_credential = "gh-hook"
    body = b'{"zen": "hi"}'
    sig = "sha256=" + _hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert client.post("/api/hooks/github", content=body, headers={"X-Hub-Signature-256": "sha256=bad",
                                                                   "X-GitHub-Event": "ping"}).status_code == 401
    r = client.post("/api/hooks/github", content=body, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping",
                                                                "Authorization": ""})
    assert r.status_code == 202 and r.json()["pong"] is True


def test_created_environments_survive_restart(settings, tmp_path):
    from hangar.app import Hangar

    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / "base.yaml").write_text("name: base\nharnesses: [claude-code]\n")
    settings.env_dirs = [shipped]
    h = Hangar(settings)
    h.fleet.clone_environment("base", "base-jobs", {"credentials": ["claude-pro"]})
    assert not (shipped / "base-jobs.yaml").exists()  # the image's envs dir is not written
    assert (settings.data_dir / "envs" / "base-jobs.yaml").exists()
    again = Hangar(settings)  # a restart / redeploy with the same data volume
    assert again.catalog.get("base-jobs").credentials == ["claude-pro"]


def test_session_routes_accept_json_bodies(client):
    # body models must be module-level: FastAPI can't resolve function-local annotations (they become query params)
    assert client.post("/api/sessions/nope/send", json={"message": "hi"}).status_code == 404
    assert client.post("/api/sessions/nope/open", json={"harness": None}).status_code == 404
    assert client.post("/api/sessions", json={"name": "x"}).status_code == 400


def test_doctor_reports_with_fixes(client):
    from hangar.models import CredentialKind

    h = client.app.state.hangar
    h.settings.github_repos = {"acme/app": "plain,ghost"}
    h.settings.github_webhook_secret_credential = "gh-hook"
    checks = {c["name"]: c for c in client.get("/api/doctor").json()}
    assert checks["github token"]["status"] == "fail" and "hangar-site" in checks["github token"]["fix"]
    assert checks["github webhook secret"]["status"] == "fail" and "hangar cred add gh-hook" in \
        checks["github webhook secret"]["fix"]
    assert checks["environment ghost"]["status"] == "fail"
    assert checks["environment plain"]["status"] == "fail" and "credentials" in checks["environment plain"]["detail"]
    assert checks["session checkpoint storage"]["status"] == "ok"
    h.broker.put("gh-hook", "webhook", CredentialKind.GENERIC, "s")
    assert {c["name"]: c for c in client.get("/api/doctor").json()}["github webhook secret"]["status"] == "ok"


def test_session_idle_route(client):
    r = client.post("/api/sessions/nope/idle", json={"idle_minutes": 0})
    assert r.status_code == 404
    assert client.post("/api/sessions/nope/idle", json={"idle_minutes": -1}).status_code == 422


def test_events_routes_and_token_budget_cap(client):
    """Workstream C: event routes, and a jobs token's max budget."""
    from hangar.events import RunEvent

    tok = client.post("/api/tokens", json={"name": "svc", "scopes": ["jobs"],
                                           "max_budget": {"max_cost_usd": 2.5}}).json()
    assert tok["max_budget"]["max_cost_usd"] == 2.5
    assert client.post("/api/tokens", json={"name": "bad", "max_budget": {"max_cost_usd": -1}}).status_code == 422
    h = client.app.state.hangar
    s = h.sessions.for_job(job_id="job-0000beef", environment="plain", harness=None, model=None, repo=None,
                           base_branch=None, branch="hangar/job-0000beef", provider=None, labels={}, extra_dirs=[],
                           idle_minutes=5)
    h.sessions.activity.append(s.id, [RunEvent(kind="progress", text=f"p{i}", turn=1) for i in range(3)])
    r = client.get(f"/api/sessions/{s.name}/events", params={"since": 1}).json()
    assert [e["text"] for e in r["events"]] == ["p1", "p2"] and r["next"] == 3 and "off" not in r["events"][0]
    assert [e["text"] for e in client.get(f"/api/sessions/{s.id}/events?tail=1").json()["events"]] == ["p2"]
    assert client.get("/api/sessions/nope/events").status_code == 404
    assert client.get("/api/jobs/job-nope/events").status_code == 404
    svc = {"Authorization": f"Bearer {tok['token']}"}
    assert client.get(f"/api/sessions/{s.id}/events", headers=svc).status_code == 403  # admin route
    assert client.get("/api/jobs/job-nope/events", headers=svc).status_code == 404
