"""`hangar connect antigravity` (I-G6): the Antigravity CLI (`agy`) reads `~/.gemini/config/mcp_config.json`, where a
remote server is `mcpServers.<name>` with `serverUrl` (it ignores `url` and `httpUrl`) and `headers`. It doesn't
expand environment variables, so the token is written as is and the file must stay private (0600)."""

import json
import stat

from typer.testing import CliRunner

import hangar.cli as cli
from hangar import mcp_clients
from tests import test_connect as tc

runner = CliRunner()
client, home = tc.client, tc.home
URL = "https://hangar.test/mcp"


def test_antigravity_is_a_client_with_its_own_file(home):
    c = mcp_clients.get("antigravity")
    assert (c.title, c.binary, c.oauth) == ("Antigravity", "agy", True)
    assert mcp_clients.config_path("antigravity") == home / ".gemini" / "config" / "mcp_config.json"
    # not Gemini CLI's settings.json, whose `url` entries agy ignores
    assert mcp_clients.config_path("antigravity") != mcp_clients.config_path("gemini")


def test_entry_is_server_url_and_headers():
    assert mcp_clients.entry("antigravity", URL, "hgr_1") == {"serverUrl": URL,
                                                              "headers": {"Authorization": "Bearer hgr_1"}}
    e = mcp_clients.entry("antigravity", URL, "hgr_1")
    assert "url" not in e and "httpUrl" not in e and "type" not in e
    # OAuth: no headers, so Antigravity discovers hangar's OAuth server and registers itself
    assert mcp_clients.entry("antigravity", URL, None) == {"serverUrl": URL}
    assert json.loads(mcp_clients.render("antigravity", "hangar", e)) == {"mcpServers": {"hangar": e}}


def test_write_merges_and_keeps_the_file_private(tmp_path):
    path = tmp_path / ".gemini" / "config" / "mcp_config.json"
    e = mcp_clients.entry("antigravity", URL, "hgr_1")
    assert mcp_clients.write("antigravity", path, "hangar", e) == "added"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # a new file holding a token
    other = {"command": "npx", "args": ["-y", "other"], "env": {"K": "v"}}
    data = json.loads(path.read_text())
    data["mcpServers"]["other"] = other
    path.write_text(json.dumps(data))
    path.chmod(0o644)  # as if the client rewrote it world-readable
    e2 = mcp_clients.entry("antigravity", URL, "hgr_2")
    assert mcp_clients.write("antigravity", path, "hangar", e2) == "updated"
    assert json.loads(path.read_text())["mcpServers"] == {"hangar": e2, "other": other}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert mcp_clients.current("antigravity", path, "hangar") == e2


def test_connect_antigravity_writes_a_working_mcp_only_token(client, home):
    r = runner.invoke(cli.app, ["connect", "antigravity"])
    assert r.exit_code == 0, r.output
    path = home / ".gemini" / "config" / "mcp_config.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    entry = json.loads(path.read_text())["mcpServers"]["hangar"]
    assert set(entry) == {"serverUrl", "headers"} and entry["serverUrl"].endswith("/mcp")
    token = entry["headers"]["Authorization"].removeprefix("Bearer ")
    assert token.startswith("hgr_") and token not in r.output
    assert tc._mcp_ok(client, token) == 200
    [tok] = [t for t in client.get("/api/tokens").json() if t["name"].startswith("mcp-antigravity-")]
    assert tok["scopes"] == ["mcp"]
    assert "/mcp lists the server" in r.output
    # no sandbox server by default (Antigravity isn't one of the clients that drive sandboxes themselves)
    assert "hangar-sandbox" not in json.loads(path.read_text())["mcpServers"]


def test_connect_antigravity_with_oauth_writes_no_token(client, home):
    r = runner.invoke(cli.app, ["connect", "antigravity", "--auth", "oauth"])
    assert r.exit_code == 0, r.output
    entry = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())["mcpServers"]["hangar"]
    assert entry == {"serverUrl": entry["serverUrl"]} and entry["serverUrl"].endswith("/mcp")
    assert "antigravity.google" in r.output  # its redirect isn't allowed by default: say so
