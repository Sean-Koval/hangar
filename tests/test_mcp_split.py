"""#140: two MCP servers. `hangar` (/mcp) lists the work tools, `hangar-sandbox` (/mcp/sandbox) the raw drive tools;
the moved tools still answer on /mcp for one release, with a note; `hangar connect` writes both for Claude Code and
Codex; `hangar doctor` checks both."""

from __future__ import annotations

import json
import tomllib

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import hangar.cli as cli
from hangar import doctor, mcp_live
from hangar.app import create_app
from tests.conftest import FakeProvider
from tests.test_connect import _Keep, home  # noqa: F401 - fixtures

runner = CliRunner()
ACCEPT = "application/json, text/event-stream"


def _rpc(c, path: str, token: str | None, method: str, params: dict | None = None, sid: str | None = None):
    h = {"Accept": ACCEPT, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if sid:
        h |= {"mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"}
    r = c.post(path, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    body = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):
        body = next(ln[5:] for ln in body.splitlines() if ln.startswith("data:"))
    return r, (json.loads(body) if body.strip().startswith("{") else None)


def _session(c, path: str, token: str) -> tuple[str, dict]:
    r, out = _rpc(c, path, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "t", "version": "1"}})
    assert r.status_code == 200, r.text
    sid = r.headers["mcp-session-id"]
    c.post(path, headers={"Accept": ACCEPT, "Content-Type": "application/json", "Authorization": f"Bearer {token}",
                          "mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"},
           json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return sid, out["result"]


@pytest.fixture
def app_client(settings, monkeypatch):
    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost:8787") as c:
        app.state.hangar.fleet.providers["docker"] = FakeProvider()
        c.headers["Authorization"] = "Bearer t0ken"
        monkeypatch.setattr(cli, "_client", lambda: _Keep(c))
        yield c


def test_both_servers_answer_over_http_with_their_own_tools(app_client):
    c = app_client
    mcp_tok = c.post("/api/tokens", json={"name": "laptop", "scopes": ["mcp"]}).json()["token"]
    listed = {}
    for path in ("/mcp", "/mcp/sandbox"):
        sid, init = _session(c, path, mcp_tok)
        listed[path] = init["serverInfo"]["name"], {t["name"] for t in
                                                    _rpc(c, path, mcp_tok, "tools/list", sid=sid)[1]["result"]["tools"]}
    (work_name, work), (drive_name, drive) = listed["/mcp"], listed["/mcp/sandbox"]
    assert (work_name, drive_name) == ("hangar", "hangar-sandbox")
    assert {"submit_job", "new_session", "wait_for_turn", "create_schedule", "fleet_overview"} <= work
    assert {"create_sandbox", "exec", "read_file", "terminal_open", "run_agent", "snapshot_sandbox"} <= drive
    assert not work & drive and all(mcp_live.server_of(n) == "hangar-sandbox" for n in drive)
    # a moved tool still answers on /mcp, and says where it lives now
    sid, _ = _session(c, "/mcp", mcp_tok)
    _, out = _rpc(c, "/mcp", mcp_tok, "tools/call", {"name": "list_sandboxes", "arguments": {}}, sid=sid)
    texts = [b.get("text", "") for b in out["result"]["content"]]
    assert not out["result"].get("isError") and any("moved to the hangar-sandbox server" in t for t in texts)
    # the sandbox server has no work tools, and says where they are
    sid, _ = _session(c, "/mcp/sandbox", mcp_tok)
    _, out = _rpc(c, "/mcp/sandbox", mcp_tok, "tools/call", {"name": "list_jobs", "arguments": {}}, sid=sid)
    assert "list_jobs is on the hangar server" in json.dumps(out)


def test_the_sandbox_server_takes_the_same_tokens_and_has_its_own_discovery(app_client):
    c = app_client
    r = c.post("/mcp/sandbox", headers={"Accept": ACCEPT, "Authorization": ""}, json={})
    assert r.status_code == 401
    assert "/.well-known/oauth-protected-resource/mcp/sandbox" in r.headers["www-authenticate"]
    meta = c.get("/.well-known/oauth-protected-resource/mcp/sandbox").json()
    assert meta["resource"].rstrip("/").endswith("/mcp/sandbox") and meta["authorization_servers"]
    assert c.post("/mcp/sandbox", headers={"Accept": ACCEPT, "Authorization": "Bearer nope"}, json={}).status_code \
        == 401
    jobs_tok = c.post("/api/tokens", json={"name": "svc", "scopes": ["jobs"]}).json()["token"]
    assert _rpc(c, "/mcp/sandbox", jobs_tok, "initialize")[0].status_code == 401  # as on /mcp
    oauth = c.app.state.oauth
    assert oauth._audience_ok("http://localhost:8787/mcp/sandbox") and oauth._audience_ok("http://localhost:8787/mcp")
    assert not oauth._audience_ok("http://localhost:8787/api")


def test_connect_writes_both_servers_for_claude_code_and_codex_only(app_client, home):  # noqa: F811
    r = runner.invoke(cli.app, ["connect", "claude-code"])
    assert r.exit_code == 0, r.output
    servers = json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert servers["hangar"]["url"] == "http://localhost:8787/mcp"
    assert servers["hangar-sandbox"]["url"] == "http://localhost:8787/mcp/sandbox"
    assert servers["hangar"]["headers"] == servers["hangar-sandbox"]["headers"]  # one token, both servers
    token = servers["hangar-sandbox"]["headers"]["Authorization"].removeprefix("Bearer ")
    assert _session(app_client, "/mcp/sandbox", token)[1]["serverInfo"]["name"] == "hangar-sandbox"
    assert runner.invoke(cli.app, ["connect", "codex"]).exit_code == 0
    codex = tomllib.loads((home / ".codex" / "config.toml").read_text())["mcp_servers"]
    assert codex["hangar-sandbox"]["url"].endswith("/mcp/sandbox") and codex["hangar"]["url"].endswith("/mcp")
    r = runner.invoke(cli.app, ["connect", "cursor"])
    cursor = json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert list(cursor) == ["hangar"] and "--with-sandbox" in r.output  # off by default, and says how
    assert runner.invoke(cli.app, ["connect", "cursor", "--with-sandbox"]).exit_code == 0
    assert "hangar-sandbox" in json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]
    (home / ".gemini").mkdir()
    assert runner.invoke(cli.app, ["connect", "gemini", "--no-sandbox"]).exit_code == 0
    assert list(json.loads((home / ".gemini" / "settings.json").read_text())["mcpServers"]) == ["hangar"]
    r = runner.invoke(cli.app, ["connect", "claude-code", "--print"])
    assert '"hangar-sandbox"' in r.output and "/mcp/sandbox" in r.output


async def test_doctor_checks_both_endpoints(app_client):
    h = app_client.app.state.hangar
    checks = {c["name"]: c for c in doctor.mcp_checks(h)}
    assert checks["mcp hangar"]["status"] == "ok" and "/mcp:" in checks["mcp hangar"]["detail"]
    assert checks["mcp hangar-sandbox"]["status"] == "ok" and "/mcp/sandbox:" in checks["mcp hangar-sandbox"]["detail"]
    h.mcp_servers.pop("/mcp/sandbox")
    assert {c["name"]: c["status"] for c in doctor.mcp_checks(h)}["mcp hangar-sandbox"] == "fail"
