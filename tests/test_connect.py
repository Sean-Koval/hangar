"""`hangar connect <client>`: each client's file is written in its own format, merged, never clobbered."""

import json
import os
import stat
import tomllib

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import hangar.cli as cli
from hangar import mcp_clients
from hangar.app import create_app
from hangar.mcp_clients import ClientConfigError
from tests.conftest import FakeProvider

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    monkeypatch.setenv("APPDATA", str(h / "AppData"))
    for v in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME"):
        monkeypatch.delenv(v, raising=False)
    return h


def test_config_paths(home, monkeypatch):
    assert mcp_clients.config_path("claude-code") == home / ".claude.json"
    assert mcp_clients.config_path("codex") == home / ".codex" / "config.toml"
    assert mcp_clients.config_path("cursor") == home / ".cursor" / "mcp.json"
    assert mcp_clients.config_path("gemini") == home / ".gemini" / "settings.json"
    monkeypatch.setattr("sys.platform", "linux")
    assert mcp_clients.config_path("vscode") == home / ".config" / "Code" / "User" / "mcp.json"
    with pytest.raises(ClientConfigError, match="macOS and Windows"):
        mcp_clients.config_path("claude-desktop")
    monkeypatch.setattr("sys.platform", "darwin")
    assert mcp_clients.config_path("vscode") == home / "Library/Application Support/Code/User/mcp.json"
    assert mcp_clients.config_path("claude-desktop") == \
        home / "Library/Application Support/Claude/claude_desktop_config.json"
    monkeypatch.setattr("sys.platform", "win32")
    assert mcp_clients.config_path("claude-desktop") == home / "AppData" / "Claude" / "claude_desktop_config.json"
    monkeypatch.setenv("CODEX_HOME", str(home / "cx"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "cc"))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home / "gm"))
    assert mcp_clients.config_path("codex") == home / "cx" / "config.toml"
    assert mcp_clients.config_path("claude-code") == home / "cc" / ".claude.json"
    assert mcp_clients.config_path("gemini") == home / "gm" / ".gemini" / "settings.json"


URL = "https://hangar.test/mcp"


@pytest.mark.parametrize("client,box,expected", [
    ("claude-code", "mcpServers", {"type": "http", "url": URL, "headers": {"Authorization": "Bearer hgr_1"}}),
    ("cursor", "mcpServers", {"url": URL, "headers": {"Authorization": "Bearer hgr_1"}}),
    ("vscode", "servers", {"type": "http", "url": URL, "headers": {"Authorization": "Bearer hgr_1"}}),
    ("gemini", "mcpServers", {"url": URL, "type": "http", "headers": {"Authorization": "Bearer hgr_1"}}),
])
def test_json_clients_merge_without_clobbering(tmp_path, client, box, expected):
    path = tmp_path / "conf.json"
    other = {"command": "npx", "args": ["-y", "other-server"]}
    path.write_text(json.dumps({"theme": "dark", box: {"other": other}, "projects": {"/x": {"a": 1}}}))
    path.chmod(0o640)
    e = mcp_clients.entry(client, URL, "hgr_1")
    assert e == expected
    assert mcp_clients.write(client, path, "hangar", e) == "added"
    data = json.loads(path.read_text())
    assert data[box] == {"other": other, "hangar": expected}
    assert data["theme"] == "dark" and data["projects"] == {"/x": {"a": 1}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640  # an existing file keeps its mode
    assert mcp_clients.write(client, path, "hangar", e) == "unchanged"
    e2 = mcp_clients.entry(client, URL, "hgr_2")
    assert mcp_clients.write(client, path, "hangar", e2) == "updated"
    assert json.loads(path.read_text())[box]["other"] == other
    assert mcp_clients.current(client, path, "hangar") == e2


def test_new_file_is_private_and_symlinks_are_followed(tmp_path):
    real = tmp_path / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    mcp_clients.write("cursor", link, "hangar", mcp_clients.entry("cursor", URL, "t"))
    assert link.is_symlink() and "hangar" in json.loads(real.read_text())["mcpServers"]
    fresh = tmp_path / "new" / "mcp.json"
    mcp_clients.write("cursor", fresh, "hangar", mcp_clients.entry("cursor", URL, "t"))
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_jsonc_and_bad_shapes_are_left_alone(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text('{\n  // my servers\n  "servers": {}\n}\n')
    with pytest.raises(ClientConfigError, match="plain JSON"):
        mcp_clients.write("vscode", p, "hangar", mcp_clients.entry("vscode", URL, "t"))
    assert "// my servers" in p.read_text()
    p.write_text('{"mcpServers": []}')
    with pytest.raises(ClientConfigError, match="isn't an object"):
        mcp_clients.write("cursor", p, "hangar", mcp_clients.entry("cursor", URL, "t"))
    with pytest.raises(ClientConfigError, match="server name"):
        mcp_clients.write("cursor", tmp_path / "x.json", "bad name", {})


CODEX = '''# my codex config
model = "gpt-5.5"

[mcp_servers.other]
command = "foo"  # keep me

[mcp_servers.hangar]
url = "http://old/mcp"
bearer_token_env_var = "HANGAR_TOKEN"

[mcp_servers.hangar.env]
X = "1"

[profiles.fast]
model = "gpt-5.5-mini"
'''


def test_codex_toml_replaces_only_its_table(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CODEX)
    e = mcp_clients.entry("codex", URL, "hgr_1")
    assert e == {"url": URL, "http_headers": {"Authorization": "Bearer hgr_1"}, "startup_timeout_sec": 20,
                 "tool_timeout_sec": 900}
    assert mcp_clients.write("codex", p, "hangar", e) == "updated"
    text = p.read_text()
    data = tomllib.loads(text)
    assert data["mcp_servers"]["hangar"] == e
    assert data["mcp_servers"]["other"] == {"command": "foo"}
    assert data["profiles"] == {"fast": {"model": "gpt-5.5-mini"}}
    assert "# my codex config" in text and "# keep me" in text and "http://old" not in text
    assert mcp_clients.write("codex", p, "hangar", e) == "unchanged"
    # OAuth: no header; codex signs in with `codex mcp login`
    assert "http_headers" not in mcp_clients.entry("codex", URL, None)


def test_codex_new_file_and_unsafe_forms(tmp_path):
    p = tmp_path / "codex" / "config.toml"
    mcp_clients.write("codex", p, "hangar", mcp_clients.entry("codex", URL, "t"))
    assert tomllib.loads(p.read_text())["mcp_servers"]["hangar"]["url"] == URL
    inline = tmp_path / "inline.toml"
    inline.write_text('mcp_servers = { hangar = { url = "http://old" } }\n')
    with pytest.raises(ClientConfigError, match="couldn't edit it safely"):
        mcp_clients.write("codex", inline, "hangar", mcp_clients.entry("codex", URL, "t"))
    assert inline.read_text() == 'mcp_servers = { hangar = { url = "http://old" } }\n'


def test_claude_desktop_bridge(tmp_path):
    hdr = tmp_path / "h.txt"
    e = mcp_clients.entry("claude-desktop", URL, "hgr_1", hdr)
    assert e == {"command": "npx", "args": ["-y", mcp_clients.MCP_REMOTE, URL, "--header-file", str(hdr)]}
    assert "hgr_1" not in json.dumps(e)  # the token stays out of the process list
    plain = mcp_clients.entry("claude-desktop", "http://10.0.0.5:8787/mcp", "t", hdr)
    assert plain["args"][-1] == "--allow-http"
    assert mcp_clients.entry("claude-desktop", "http://localhost:8787/mcp", "t", hdr)["args"][-1] == str(hdr)
    with pytest.raises(ClientConfigError, match="custom connector"):
        mcp_clients.entry("claude-desktop", URL, None)


# ---------------------------------------------------------------------------- the CLI against a real app
class _Keep:
    """cli._client() returns a context manager; keep the TestClient open across calls."""

    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        return False


@pytest.fixture
def client(settings, monkeypatch):
    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.hangar.fleet.providers["docker"] = FakeProvider()
        c.headers["Authorization"] = "Bearer t0ken"
        monkeypatch.setattr(cli, "_client", lambda: _Keep(c))
        yield c


def _mcp_ok(client, token) -> int:
    return client.post("/mcp", headers={"Authorization": f"Bearer {token}", "Accept": "application/json, "
                                        "text/event-stream"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                           "protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}).status_code


def test_connect_writes_config_with_an_mcp_only_token(client, home):
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"other": {"type": "http", "url": "http://o"}},
                                                   "numStartups": 3}))
    r = runner.invoke(cli.app, ["connect", "claude-code"])
    assert r.exit_code == 0, r.output
    data = json.loads((home / ".claude.json").read_text())
    assert data["numStartups"] == 3 and data["mcpServers"]["other"] == {"type": "http", "url": "http://o"}
    entry = data["mcpServers"]["hangar"]
    assert entry["type"] == "http" and entry["url"] == "http://localhost:8787/mcp"
    token = entry["headers"]["Authorization"].removeprefix("Bearer ")
    assert token not in r.output
    # the token works for MCP and nothing else
    assert _mcp_ok(client, token) == 200
    assert client.get("/api/tokens", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    assert client.get("/api/credentials", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    [tok] = [t for t in client.get("/api/tokens").json() if t["name"].startswith("mcp-claude-code-")]
    assert tok["scopes"] == ["mcp"]
    # reconnecting rotates: one token per client and machine, the old one revoked
    assert runner.invoke(cli.app, ["connect", "claude-code"]).exit_code == 0
    new = json.loads((home / ".claude.json").read_text())["mcpServers"]["hangar"]["headers"]["Authorization"]
    assert new != entry["headers"]["Authorization"] and _mcp_ok(client, token) == 401
    assert len([t for t in client.get("/api/tokens").json() if t["name"] == tok["name"]]) == 1


def test_connect_each_client(client, home, monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    for name in mcp_clients.CLIENTS:
        r = runner.invoke(cli.app, ["connect", name])
        assert r.exit_code == 0, (name, r.output)
        assert mcp_clients.current(name, mcp_clients.config_path(name), "hangar"), name
    hdr = cli._config_path().parent / "mcp-headers" / "claude-desktop-hangar.txt"
    assert stat.S_IMODE(hdr.stat().st_mode) == 0o600 and "Authorization: Bearer hgr_" in hdr.read_text()
    r = runner.invoke(cli.app, ["connect", "--list"])
    assert r.exit_code == 0 and r.output.count("(token)") == len(mcp_clients.CLIENTS)


def test_connect_print_writes_nothing_and_oauth(client, home):
    r = runner.invoke(cli.app, ["connect", "codex", "--print"])
    assert r.exit_code == 0 and "[mcp_servers.hangar]" in r.output and "http_headers" in r.output
    assert not (home / ".codex").exists()
    r = runner.invoke(cli.app, ["connect", "codex", "--auth", "oauth"])
    assert r.exit_code == 0, r.output
    assert "codex mcp login hangar" in r.output
    entry = tomllib.loads((home / ".codex" / "config.toml").read_text())["mcp_servers"]["hangar"]
    assert "http_headers" not in entry and entry["url"].endswith("/mcp")
    r = runner.invoke(cli.app, ["connect", "vscode", "--auth", "oauth"])
    assert r.exit_code == 2 and "use --auth token" in r.output
    r = runner.invoke(cli.app, ["connect", "nope"])
    assert r.exit_code == 2 and "choose from" in r.output


def test_connect_falls_back_to_printing(client, home):
    p = home / ".cursor" / "mcp.json"
    p.parent.mkdir()
    p.write_text("{ // comment\n}")
    r = runner.invoke(cli.app, ["connect", "cursor"])
    assert r.exit_code == 1 and "by hand" in r.output and '"mcpServers"' in r.output
    assert p.read_text() == "{ // comment\n}"
    assert os.path.exists(p)
