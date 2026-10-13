"""overnight/sweep, security caps: what the wave 1 reviews of #48 and #50 left open.

Uncapped reads of sandbox-written files, a body cap that ignored the token's scope, replay entries that a power loss
could roll back, and Cursor's OAuth sign-in. Each test failed on origin/overnight/perf before the fix."""

from __future__ import annotations

import ast
import json
import os
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from typer.testing import CliRunner

import hangar.cli as cli
from hangar.auth import TokenAuth
from hangar.broker import AUTH_FILE_LIMIT, ENV_FILE_LIMIT
from hangar.config import Settings
from hangar.fleet import CONFIG_READ_LIMIT, FleetError
from hangar.frontdoor import ReplayGuard
from hangar.models import CredentialKind, Sandbox
from hangar.oauth import HangarOAuthProvider
from tests.conftest import FakeProvider
from tests.test_connect import client, home  # noqa: F401 - fixtures

SRC = Path(__file__).resolve().parents[1] / "src" / "hangar"
CURSOR = ["cursor://anysphere.cursor-mcp/oauth/callback", "https://www.cursor.com/agents/mcp/oauth/callback",
          "http://localhost:8787/callback"]  # what Cursor registers in one request (forum.cursor.com, 2026-07-15)


# ====================================================================== 1. reads of sandbox-controlled files
def test_every_read_of_a_sandbox_file_passes_a_cap():
    """The sandbox decides how big its files are: every provider/fleet read_file call in hangar passes `limit`."""
    missing = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read_file" \
                    and not any(k.arg in ("limit", None) for k in node.keywords):
                missing.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not missing, f"read_file without a limit: {missing}"


async def _box() -> tuple[FakeProvider, Sandbox]:
    prov = FakeProvider()
    sb = Sandbox(name="t", environment="e", provider="fake")
    await prov.create(sb, "img")
    return prov, sb


async def test_an_oversized_auth_file_is_not_written_back(broker):
    prov, sb = await _box()
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, '{"refresh":"r1"}')
    creds = broker.acquire(["codex"], sb.id)
    await broker.inject(sb, prov, creds)
    await prov.write_file(sb, "/home/agent/.codex/auth.json", b"x" * (AUTH_FILE_LIMIT + 10))
    await broker.release(sb, prov)
    assert broker.vault.get_text("cred:codex") == '{"refresh":"r1"}'  # the vault copy is kept
    assert broker.get("codex").leased_to is None  # and the lease is still returned


async def test_a_runaway_env_file_is_started_over(broker):
    prov, sb = await _box()
    await prov.write_file(sb, "/home/agent/.hangar/env", b"# filler\n" * (ENV_FILE_LIMIT // 9 + 10))
    broker.put("key", "claude-code", CredentialKind.API_KEY, "sk-ant-api-1")
    await broker.inject(sb, prov, broker.acquire(["key"], sb.id))
    env = await prov.read_file(sb, "/home/agent/.hangar/env")
    assert env == b"export ANTHROPIC_API_KEY=sk-ant-api-1\n"


async def test_a_runaway_config_file_is_left_alone(fleet):
    sb = await fleet.create("plain")
    prov = fleet.providers["docker"]
    big = json.dumps({"projects": "x" * CONFIG_READ_LIMIT}).encode()
    await prov.write_file(sb, "/home/agent/.claude.json", big)
    await fleet._merge_json(prov, sb, "/home/agent/.claude.json", {"hasCompletedOnboarding": True})
    assert await prov.read_file(sb, "/home/agent/.claude.json") == big
    assert any(".claude.json is over 8 MB" in w for w in sb.info["warnings"])
    await prov.write_file(sb, "/home/agent/.claude.json", b'{"a": 1}')  # an ordinary one is still merged
    await fleet._merge_json(prov, sb, "/home/agent/.claude.json", {"b": 2})
    assert json.loads(await prov.read_file(sb, "/home/agent/.claude.json")) == {"a": 1, "b": 2}


async def test_a_workspace_snapshot_over_the_cap_is_refused(fleet, settings):
    settings.snapshot_max_mb = 1
    sb = await fleet.create("plain")
    await fleet.write_file(sb.id, "blob.bin", os.urandom(2 * 1024 * 1024))  # doesn't compress
    with pytest.raises(FleetError, match="HANGAR_SNAPSHOT_MAX_MB"):
        await fleet.snapshot(sb.id, kind="workspace")
    assert not list(settings.snapshot_dir.iterdir())


async def test_a_login_capture_file_is_capped(fleet, broker):
    for size, ok in ((AUTH_FILE_LIMIT + 1, False), (20, True)):
        sb = await fleet.create("plain")
        await fleet.write_file(sb.id, "/home/agent/.codex/auth.json", b"{" + b" " * (size - 2) + b"}")
        fleet.logins["login-1"] = {"id": "login-1", "harness": "codex", "credential": "codex", "sandbox": sb.id,
                                   "session": "login", "flow": {}}
        if ok:
            await fleet.complete_login("login-1")
            assert broker.vault.get("cred:codex") == b"{" + b" " * 18 + b"}"
        else:
            with pytest.raises(FleetError, match="over 1024 KB"):
                await fleet.complete_login("login-1")
            assert broker.vault.get("cred:codex") is None


async def test_mcp_read_file_answers_with_the_first_ten_megabytes(fleet, monkeypatch):
    from mcp import Client

    from hangar import mcp_server

    monkeypatch.setattr(mcp_server, "READ_FILE_LIMIT", 1000)
    sb = await fleet.create("plain")
    await fleet.write_file(sb.id, "big.txt", b"a" * 5000)
    await fleet.write_file(sb.id, "small.txt", b"hello")
    jobs = type("J", (), {"sessions": None})()
    async with Client(mcp_server.build_mcp(fleet, jobs, None)) as c:
        big = json.loads((await c.call_tool("read_file", {"sandbox": sb.id, "path": "big.txt"})).content[0].text)
        small = json.loads((await c.call_tool("read_file", {"sandbox": sb.id, "path": "small.txt"})).content[0].text)
    assert big["content"] == "a" * 1000 and big["truncated"] is True and "exec" in big["note"]
    assert small == {"path": "small.txt", "exists": True, "content": "hello"}


# ====================================================================== 2. body caps by token scope
def _app(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    return create_app(settings)


def test_body_cap_follows_the_tokens_scope(settings):
    """A `jobs` or `mcp` token used to get the 100 MB cap on every route, admin-only ones included, and a 403 only
    after its body was read. Now it gets the anonymous 64 KB cap on routes its scope doesn't reach."""
    app = _app(settings)
    body = json.dumps({"environment": "plain", "instructions": "x" * 200_000}).encode()

    def post(path: str, token: str, chunked: bool = False) -> int:
        hdr = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        content = (body[i:i + 16_384] for i in range(0, len(body), 16_384)) if chunked else body
        return tc.post(path, content=content, headers=hdr).status_code

    with TestClient(app) as tc:
        h = app.state.hangar
        jobs_tok, _ = h.auth.create("svc", scopes=["jobs"])
        mcp_tok, _ = h.auth.create("ide", scopes=["mcp"])
        assert post("/api/sandboxes", jobs_tok) == 413
        assert post("/api/sandboxes", jobs_tok, chunked=True) == 413  # no Content-Length: counted as it arrives
        assert post("/api/credentials", mcp_tok) == 413
        assert post("/api/jobs", mcp_tok) == 413
        assert post("/a2a", mcp_tok) == 413
        assert post("/api/jobs", jobs_tok) not in (413, 401, 403)  # its own route: the usual limit
        assert post("/mcp", mcp_tok) != 413
        assert post("/api/sandboxes", "t0ken") != 413  # admin: everything
        small = tc.post("/api/sandboxes", json={"environment": "x"}, headers={"Authorization": f"Bearer {jobs_tok}"})
        assert small.status_code == 403  # a small body still gets the route's own answer


def test_github_webhook_buffers_at_most_two_megabytes(settings):
    app = _app(settings)
    with TestClient(app) as tc:
        h = app.state.hangar
        h.settings.github_webhook_secret_credential = "gh"
        h.broker.put("gh", "webhook", CredentialKind.GENERIC, "ghsec")
        sig = {"X-Hub-Signature-256": "sha256=00", "X-GitHub-Event": "issues"}
        assert tc.post("/api/hooks/github", content=b"x" * (3 * 1024 * 1024), headers=sig).status_code == 413
        assert tc.post("/api/hooks/github", content=b"x" * (1024 * 1024), headers=sig).status_code == 401


# ====================================================================== 3. replay entries are synced
def _synced(store, fn) -> list[bool]:
    statements: list[str] = []
    store._db.set_trace_callback(statements.append)
    try:
        fn()
    finally:
        store._db.set_trace_callback(None)
    out, mode = [], "NORMAL"
    for s in statements:
        if s.startswith("PRAGMA synchronous="):
            mode = s.split("=")[1]
        elif s.startswith(("INSERT", "DELETE")):
            out.append(mode == "FULL")
    return out


def test_replay_entries_commit_with_a_wal_sync(store):
    """synchronous=NORMAL can roll back the last commits after a power loss: a webhook delivery or Slack event
    hangar already acted on would then run again when the sender retries it."""
    from types import SimpleNamespace

    from hangar.slack import SlackFrontDoor

    guard = ReplayGuard(store)
    guard._last_prune = time.time()  # pruning (expired entries only) may stay on NORMAL
    assert _synced(store, lambda: guard.put("github", "d1", "job-1", 60)) == [True]
    assert _synced(store, lambda: guard.forget("github", "d1")) == [True]
    door = SimpleNamespace(store=store, _last_prune=time.time())
    assert _synced(store, lambda: SlackFrontDoor._first(door, "Ev0123")) == [True]
    assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 1


# ====================================================================== 8. Cursor's OAuth sign-in
def _client(*uris: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id=uuid.uuid4().hex, client_id_issued_at=int(time.time()),
                                      redirect_uris=[AnyUrl(u) for u in uris])


async def test_cursor_registers_with_the_default_redirect_list(store):
    p = HangarOAuthProvider(store, TokenAuth(store, "adm"), "https://hangar.example",
                            Settings().oauth_redirect_hosts)
    c = _client(*CURSOR)
    await p.register_client(c)
    assert [str(u) for u in c.redirect_uris] == [CURSOR[0], CURSOR[2]]  # not Cursor's web callback
    assert [str(u) for u in (await p.get_client(c.client_id)).redirect_uris] == [CURSOR[0], CURSOR[2]]


async def test_custom_schemes_need_an_exact_entry(store):
    auth = TokenAuth(store, "adm")
    p = HangarOAuthProvider(store, auth, "https://hangar.example", ["claude.ai", "localhost"])
    for bad in ("evil://claude.ai/cb", "cursor://anysphere.cursor-mcp/oauth/callback"):
        with pytest.raises(RegistrationError) as e:
            await p.register_client(_client(bad))
        assert e.value.error == "invalid_redirect_uri"
    assert 'add "cursor://anysphere.cursor-mcp" to HANGAR_OAUTH_REDIRECT_HOSTS' in e.value.error_description
    await p.register_client(_client("https://sub.claude.ai/cb"))  # bare hosts still cover https subdomains
    ok = HangarOAuthProvider(store, auth, "https://hangar.example", ["cursor://anysphere.cursor-mcp/"])
    await ok.register_client(_client(CURSOR[0]))
    with pytest.raises(RegistrationError):
        await ok.register_client(_client("https://anysphere.cursor-mcp/cb"))  # the scheme is part of the entry


def test_cursor_dynamic_registration_end_to_end(settings):
    settings.admin_token, settings.public_url = "t0ken", "http://localhost:8787"
    from hangar.app import create_app

    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        r = c.post("/register", json={"client_name": "Cursor", "redirect_uris": CURSOR,
                                      "grant_types": ["authorization_code", "refresh_token"],
                                      "response_types": ["code"], "token_endpoint_auth_method": "none"})
        assert r.status_code == 201, r.text
        assert r.json()["redirect_uris"] == [CURSOR[0], CURSOR[2]]
        authz = c.get("/authorize", params={"response_type": "code", "client_id": r.json()["client_id"],
                                            "redirect_uri": CURSOR[0], "code_challenge": "x" * 43,
                                            "code_challenge_method": "S256", "state": "s"}, follow_redirects=False)
        assert authz.status_code == 302 and "/oauth/consent" in authz.headers["location"]


def test_connect_cursor_says_how_to_allow_its_redirect(client, home):  # noqa: F811
    runner = CliRunner()
    r = runner.invoke(cli.app, ["connect", "cursor", "--auth", "oauth"])
    assert r.exit_code == 0 and "HANGAR_OAUTH_REDIRECT_HOSTS" not in r.output  # the default list allows it
    client.put("/api/settings/HANGAR_OAUTH_REDIRECT_HOSTS", json={"value": '["claude.ai"]'})
    r = runner.invoke(cli.app, ["connect", "cursor", "--auth", "oauth"])
    assert r.exit_code == 0, r.output
    assert """hangar config set HANGAR_OAUTH_REDIRECT_HOSTS '["claude.ai", "cursor://anysphere.cursor-mcp"]'""" \
        in r.output
