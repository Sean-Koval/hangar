"""OAuth 2.1 flow for MCP clients like ChatGPT: register -> authorize (PKCE) -> consent -> token -> /mcp."""

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from hangar.app import create_app


@pytest.fixture
def client(settings):
    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        yield c


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def register(c):
    r = c.post("/register", json={"client_name": "ChatGPT", "redirect_uris": ["https://chatgpt.com/cb"],
                                   "grant_types": ["authorization_code", "refresh_token"],
                                   "response_types": ["code"], "token_endpoint_auth_method": "none"})
    assert r.status_code == 201, r.text
    return r.json()


def authorize(c, reg, challenge, token="t0ken"):
    r = c.get("/authorize", params={"response_type": "code", "client_id": reg["client_id"],
                                    "redirect_uri": "https://chatgpt.com/cb", "code_challenge": challenge,
                                    "code_challenge_method": "S256", "state": "xyz", "scope": "hangar"},
              follow_redirects=False)
    assert r.status_code == 302
    consent = urlparse(r.headers["location"])
    page = c.get(f"{consent.path}?{consent.query}")
    assert page.headers["x-frame-options"] == "DENY" and "ChatGPT" in page.text
    req = re.search(r'name="req" value="([^"]+)"', page.text).group(1)
    return c.post("/oauth/consent", data={"req": req, "token": token, "action": "approve"}, follow_redirects=False)


def test_discovery_and_challenge(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert 'resource_metadata="http://localhost:8787/.well-known/oauth-protected-resource/mcp"' in \
        r.headers["www-authenticate"]
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == "http://localhost:8787/mcp"
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/register") and "S256" in meta["code_challenge_methods_supported"]


def test_full_flow_refresh_and_revoke(client):
    reg = register(client)
    verifier, challenge = pkce()
    bad = authorize(client, reg, challenge, token="nope")
    assert bad.status_code == 200 and "not accepted" in bad.text
    ok = authorize(client, reg, challenge)
    q = parse_qs(urlparse(ok.headers["location"]).query)
    assert q["state"] == ["xyz"]
    tok = client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                      "redirect_uri": "https://chatgpt.com/cb", "client_id": reg["client_id"],
                                      "code_verifier": verifier}).json()
    assert tok["access_token"].startswith("hoa_") and tok["refresh_token"]
    # code is single use
    again = client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                        "redirect_uri": "https://chatgpt.com/cb", "client_id": reg["client_id"],
                                        "code_verifier": verifier})
    assert again.status_code == 400
    hdrs = {"Authorization": f"Bearer {tok['access_token']}", "accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    assert client.post("/mcp", json=init, headers=hdrs).status_code == 200
    # refresh rotates
    new = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                      "client_id": reg["client_id"]}).json()
    assert new["access_token"] != tok["access_token"]
    assert client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                       "client_id": reg["client_id"]}).status_code == 400
    # dashboard revoke kills access
    admin = {"Authorization": "Bearer t0ken"}
    apps = client.get("/api/oauth/clients", headers=admin).json()
    assert apps[0]["approved_by"] == "admin" and apps[0]["active"]
    client.delete(f"/api/oauth/clients/{reg['client_id']}", headers=admin)
    hdrs["Authorization"] = f"Bearer {new['access_token']}"
    assert client.post("/mcp", json=init, headers=hdrs).status_code == 401


def test_wrong_pkce_verifier_rejected(client):
    reg = register(client)
    _, challenge = pkce()
    ok = authorize(client, reg, challenge)
    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    r = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": "https://chatgpt.com/cb", "client_id": reg["client_id"],
                                    "code_verifier": "x" * 50})
    assert r.status_code == 400


def test_oauth_disabled_for_plain_http_public_host(settings):
    settings.admin_token = "t0ken"
    settings.public_url = "http://hangar.internal:8787"
    with TestClient(create_app(settings)) as c:
        assert c.get("/.well-known/oauth-authorization-server").status_code in (401, 404, 405)
        assert c.get("/api/connect", headers={"Authorization": "Bearer t0ken"}).json()["oauth"] is False
