"""Scopes on capabilities and the credential exposure class (identity; hardening B4, H3, O3).

The B4 tests fail on main: there an MCP client maps the GitHub webhook secret into a sandbox through an
environment's `secrets:` and reads it back, encoded so that redaction can't see it.
"""

from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from hangar.auth import Principal
from hangar.broker import CredentialError
from hangar.mcp_server import build_mcp
from hangar.models import CredentialKind
from tests.test_mcp_modern import sc

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

SECRET = "whsec-0123456789abcdef"
MCP_ONLY = Principal("laptop", "tok-1", ["mcp"])
ADMIN = Principal("ops", "tok-2", ["admin"])


@pytest.fixture
def webhook_secret(fleet, broker):
    broker.put("github-webhook-secret", "webhook", CredentialKind.GENERIC, SECRET)
    fleet.settings.github_webhook_secret_credential = "github-webhook-secret"
    return "github-webhook-secret"


def as_principal(p: Principal | None):
    import hangar.identity as identity  # not at the top: the B4 tests run, and fail, on main too

    return identity.current.set(p)


def reset(tok) -> None:
    import hangar.identity as identity

    identity.current.reset(tok)


@pytest.fixture(autouse=True)
def _saved_envs_in_tmp(request, tmp_path):
    """Environments the tests save go to a temporary directory, not the fixtures."""
    if "fleet" in request.fixturenames:
        request.getfixturevalue("fleet").catalog.save_dir = tmp_path / "saved-envs"


def err(res) -> str:
    assert res.is_error, res.content
    return res.content[0].text


# ====================================================================== B4: control-plane secrets over MCP
async def test_b4_webhook_secret_never_reaches_a_sandbox_through_secrets(fleet, webhook_secret):
    """hardening B4's repro, as the admin over MCP: on main the reversed secret came back from `exec`."""
    async with Client(build_mcp(fleet)) as c:
        res = await c.call_tool("create_environment", {"spec": {"name": "leak", "extends": "plain",
                                                                "secrets": {"X": webhook_secret}}})
        assert "control-plane only" in err(res)
        # Saved some other way (a file in an environment directory): the sandbox still never gets it.
        fleet.catalog._raw["leak"] = ({"name": "leak", "extends": "plain", "secrets": {"X": webhook_secret}}, None)
        res = await c.call_tool("create_sandbox", {"environment": "leak"})
        assert "control-plane only" in err(res)
        for sb in fleet.list(include_stopped=True):  # nothing ran, nothing was written
            assert sb.environment != "leak" or sb.state.value == "error"
            ran = await c.call_tool("exec", {"sandbox": sb.id, "command": 'printf %s "$X" | rev'})
            assert SECRET[::-1] not in json.dumps(ran.structured_content or {})


async def test_b4_mcp_token_may_not_set_secrets_or_image(fleet, broker):
    broker.put("openrouter", "pi", CredentialKind.API_KEY, "sk-or-0123456789abcdef")
    tok = as_principal(MCP_ONLY)
    try:
        async with Client(build_mcp(fleet)) as c:
            for extra in ({"secrets": {"K": "openrouter"}}, {"image": "evil.example/x:1"}, {"base": "evil/x"},
                          {"runtime_class": ""}, {"credential_mode": "direct"}):
                res = await c.call_tool("create_environment", {"spec": {"name": "e1", "extends": "plain", **extra}})
                assert "needs an admin token" in err(res)
            res = await c.call_tool("clone_environment", {"source": "plain", "name": "e2",
                                                          "overrides": {"secrets": {"K": "openrouter"}}})
            assert "needs an admin token" in err(res)
            # what a member may still do: environments without those fields, and the gateway mode
            ok = sc(await c.call_tool("create_environment", {"spec": {"name": "e3", "extends": "plain",
                                                                      "credential_mode": "gateway"}}))
            assert ok["name"] == "e3"
    finally:
        reset(tok)
    tok = as_principal(ADMIN)  # an admin token may map a sandbox credential
    try:
        async with Client(build_mcp(fleet)) as c:
            sc(await c.call_tool("create_environment", {"spec": {"name": "e4", "extends": "plain",
                                                                 "secrets": {"K": "openrouter"}}}))
    finally:
        reset(tok)


async def test_attached_control_plane_credentials_are_refused(fleet, broker):
    with pytest.raises(CredentialError, match="control-plane only"):
        broker.put("slack-bot", "webhook", CredentialKind.GENERIC, "xoxb-0123456789", env_var="SLACK_TOKEN")
    # The control plane's own GitHub token, attached to a direct-mode sandbox
    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_0123456789abcdef")
    fleet.settings.github_token_credential = "gh"
    with pytest.raises(Exception, match="control-plane only.*hangar cred exposure gh sandbox"):
        await fleet.create("plain", credentials=["gh"])
    # An admin may share that one on purpose
    broker.set_exposure("gh", "sandbox")
    sb = await fleet.create("plain", credentials=["gh"])
    assert "ghp_0123456789abcdef" in (await fleet.read_file(sb.id, "/home/agent/.hangar/env")).decode()
    # ...but never a signing key
    broker.put("hook", "webhook", CredentialKind.GENERIC, "s3cret-signing-key")
    with pytest.raises(CredentialError, match="stays control-plane"):
        broker.set_exposure("hook", "sandbox")
    fleet.settings.slack_bot_token_credential = "bot"
    broker.put("bot", "git", CredentialKind.GIT_TOKEN, "xoxb-0123456789abcdef")
    with pytest.raises(CredentialError, match="stays control-plane"):
        broker.set_exposure("bot", "sandbox")


def test_exposure_defaults(settings, broker):
    import hangar.identity as identity
    from hangar.models import Credential

    settings.inbound_webhooks = {"sentry": {"secret": "sentry-hmac"}}
    settings.gateway_credential = "gw-pat"
    broker.settings = settings
    cases = {("app", "git", CredentialKind.GITHUB_APP): "control_plane",
             ("sentry-hmac", "custom", CredentialKind.GENERIC): "control_plane",
             ("gw-pat", "git", CredentialKind.GIT_TOKEN): "control_plane",
             ("claude", "claude-code", CredentialKind.OAUTH_TOKEN): "sandbox"}
    for (name, harness, kind), want in cases.items():
        assert identity.exposure(Credential(name=name, harness=harness, kind=kind), settings) == want, name


# ====================================================================== scopes
async def test_scoped_refuses_narrower_tokens():
    import hangar.identity as identity

    async def drain() -> str:
        return "drained"

    guarded = identity.scoped(drain, "admin")
    assert await guarded() == "drained"  # in-process caller
    for p, ok in ((ADMIN, True), (MCP_ONLY, False), (Principal("svc", "tok-3", ["jobs", "admin"]), True)):
        tok = as_principal(p)
        try:
            if ok:
                assert await guarded() == "drained"
            else:
                with pytest.raises(identity.Forbidden, match="'admin' scope"):
                    await guarded()
        finally:
            reset(tok)


def _mcp_session(client, token: str) -> dict[str, str]:
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
         "Authorization": f"Bearer {token}"}
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code == 200, r.text
    h |= {"mcp-session-id": r.headers["mcp-session-id"], "mcp-protocol-version": "2025-06-18"}
    client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return h


def _call(client, h, tool: str, args: dict) -> dict:
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                             "params": {"name": tool, "arguments": args}})
    assert r.status_code == 200, r.text
    body = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):
        body = next(ln[5:] for ln in body.splitlines() if ln.startswith("data:"))
    return json.loads(body)["result"]


def test_the_mcp_endpoint_sets_the_tokens_principal(settings):
    """Over HTTP: an `mcp` token (hangar connect) is refused `secrets:`; the admin token is refused only the
    control-plane credential; REST answers a token outside its scope with 403."""
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings)) as client:
        hg = client.app.state.hangar
        hg.broker.put("github-webhook-secret", "webhook", CredentialKind.GENERIC, SECRET)
        hg.settings.github_webhook_secret_credential = "github-webhook-secret"
        hg.broker.put("openrouter", "pi", CredentialKind.API_KEY, "sk-or-0123456789abcdef")
        admin = {"Authorization": "Bearer t0ken"}
        mcp_tok = client.post("/api/tokens", json={"name": "laptop", "scopes": ["mcp"]}, headers=admin).json()
        h = _mcp_session(client, mcp_tok["token"])
        res = _call(client, h, "create_environment", {"spec": {"name": "leak", "extends": "plain",
                                                               "secrets": {"X": "openrouter"}}})
        assert res["isError"] and "needs an admin token" in res["content"][0]["text"]
        h = _mcp_session(client, "t0ken")
        res = _call(client, h, "create_environment", {"spec": {"name": "leak", "extends": "plain",
                                                               "secrets": {"X": "github-webhook-secret"}}})
        assert res["isError"] and "control-plane only" in res["content"][0]["text"]
        res = _call(client, h, "create_environment", {"spec": {"name": "ok", "extends": "plain",
                                                               "secrets": {"X": "openrouter"}}})
        assert not res.get("isError"), res
        # REST: the exposure listing and the switch
        creds = {c["name"]: c for c in client.get("/api/credentials", headers=admin).json()}
        assert creds["github-webhook-secret"]["exposure"] == "control_plane"
        assert creds["openrouter"]["exposure"] == "sandbox"
        r = client.put("/api/credentials/github-webhook-secret/exposure", json={"exposure": "sandbox"},
                       headers=admin)
        assert r.status_code == 400 and "stays control-plane" in r.json()["error"]
        bearer = {"Authorization": f"Bearer {mcp_tok['token']}"}
        assert client.put("/api/credentials/openrouter/exposure", json={"exposure": "control_plane"},
                          headers=bearer).status_code == 403


def test_oauth_grants_act_as_the_token_that_approved_them(settings):
    from tests import test_oauth

    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    from hangar.app import create_app

    with TestClient(create_app(settings), base_url="http://localhost:8787") as client:
        _, tok = test_oauth._grant(client)
        oauth = client.app.state.oauth
        import asyncio

        p = asyncio.run(oauth.principal(tok["access_token"]))
        assert p is not None and p.admin and p.id == "admin" and p.name.startswith("oauth:")
        assert asyncio.run(oauth.principal("hoa_nope")) is None
