"""`hangar up` / `hangar down` with a real server process (no Docker needed: providers are off)."""

import json
import os
import tomllib

import httpx
import pytest
from typer.testing import CliRunner

import hangar.cli as cli
from hangar import setup_cli
from tests.conftest import ENVS
from tests.conftest import free_port as _free_port

runner = CliRunner()


@pytest.fixture
def local(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("HANGAR_DATA_DIR", str(data))
    monkeypatch.setenv("HANGAR_ENABLED_PROVIDERS", "[]")  # the server process: no Docker, no warm pools
    monkeypatch.setenv("HANGAR_ENV_DIRS", json.dumps([str(ENVS)]))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    for v in ("HANGAR_URL", "HANGAR_TOKEN", "HANGAR_ADMIN_TOKEN", "HANGAR_PUBLIC_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(setup_cli, "_docker_ok", lambda: None)
    yield data
    runner.invoke(cli.app, ["down"])
    pid_file = data / "server.pid"
    if pid_file.exists():  # down failed: don't leak a server
        os.kill(int(pid_file.read_text()), 9)


def test_up_is_idempotent_and_connects(local):
    port = _free_port()
    r = runner.invoke(cli.app, ["up", "--port", str(port), "--no-build", "--connect", "codex"])
    assert r.exit_code == 0, r.output
    pid = (local / "server.pid").read_text().strip()
    url = f"http://127.0.0.1:{port}"
    saved = json.loads(cli._config_path().read_text())
    assert saved["url"] == url and saved["token"].startswith("hgr_")
    assert (local / "bootstrap-token").exists()
    # the CLI now works without HANGAR_URL/HANGAR_TOKEN, and the codex entry's token reaches MCP
    entry = tomllib.loads((local.parent / "codex" / "config.toml").read_text())["mcp_servers"]["hangar"]
    assert entry["url"] == f"http://localhost:{port}/mcp"
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    r2 = httpx.post(f"{url}/mcp", json=init,
                    headers={**entry["http_headers"], "Accept": "application/json, text/event-stream"})
    assert r2.status_code == 200
    # again: same server, same login, nothing duplicated
    r = runner.invoke(cli.app, ["up", "--port", str(port), "--no-build"])
    assert r.exit_code == 0, r.output
    assert "already running" in r.output and (local / "server.pid").read_text().strip() == pid
    assert json.loads(cli._config_path().read_text()) == saved
    r = runner.invoke(cli.app, ["env", "list"])
    assert r.exit_code == 0 and "plain" in r.output
    r = runner.invoke(cli.app, ["down"])
    assert r.exit_code == 0 and f"stopped hangar (pid {pid})" in r.output
    assert not (local / "server.pid").exists()
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{url}/healthz", timeout=1)
    assert "isn't running" in runner.invoke(cli.app, ["down"]).output


def test_up_without_docker_says_what_to_do(local, monkeypatch):
    monkeypatch.setattr(setup_cli, "_docker_ok", lambda: "connection refused")
    r = runner.invoke(cli.app, ["up", "--no-build"])
    assert r.exit_code == 1 and "Start Docker Desktop" in r.output
