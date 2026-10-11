"""HTTP surface: auth on REST + MCP, secrets never echoed."""

import pytest
from fastapi.testclient import TestClient

from hangar.app import create_app
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


def test_environment_edit_and_context(client, settings, tmp_path):
    settings.env_dirs.insert(0, tmp_path)
    client.app.state.hangar.catalog.dirs.insert(0, tmp_path)
    r = client.put("/api/environments/mine", content="extends: plain\ndescription: mine\n")
    assert r.status_code == 200 and (tmp_path / "mine.yaml").exists()
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
