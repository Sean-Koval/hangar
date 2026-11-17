"""SSO sign-in for the console and the CLI, and deprovisioning by the identity provider (KAN-294).

A fake OpenID Connect provider (discovery, JWKS, authorize, token with PKCE and refresh) and a fake IAP key stand in
for Google, Okta and Entra: no real identity provider or credential is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

from hangar import people, sso
from hangar.app import create_app
from hangar.models import ApiToken

BASE = "https://hangar.example"
ISSUER = "https://idp.example"
CLIENT = "hangar-client"
AUD = "/projects/123/global/backendServices/456"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _int(n: int, size: int | None = None) -> str:
    return _b64(n.to_bytes(size or (n.bit_length() + 7) // 8, "big"))


def jwt_rs(key, claims: dict, kid: str = "k1", alg: str = "RS256") -> str:
    head = _b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


def jwt_es(key, claims: dict, kid: str = "iap1") -> str:
    head = _b64(json.dumps({"alg": "ES256", "kid": kid, "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class FakeIdP:
    """Discovery, JWKS, authorize (by hand: `approve`) and the token endpoint, with PKCE and refresh tokens."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.users: dict[str, dict] = {}  # email -> {sub, groups, verified}
        self.codes: dict[str, dict] = {}
        self.refresh: dict[str, str] = {}  # refresh token -> email
        self.down = False
        self.no_refresh = False
        self.rotate = False  # each refresh spends the token and issues a new one (Okta, Auth0, Keycloak)
        self.jwks_down = False
        self.token_error: str | None = None

    def user(self, email: str, groups=("hangar-users",), verified=True, sub: str | None = None):
        self.users[email] = {"sub": sub or "sub-" + email.split("@")[0], "groups": list(groups), "verified": verified}

    def id_token(self, email: str, nonce: str | None = None, **over) -> str:
        u = self.users[email]
        c = {"iss": ISSUER, "aud": CLIENT, "sub": u["sub"], "email": email, "email_verified": u["verified"],
             "groups": u["groups"], "iat": int(time.time()), "exp": int(time.time()) + 300, **over}
        if nonce:
            c["nonce"] = nonce
        return jwt_rs(self.key, c)

    def approve(self, location: str, email: str) -> tuple[str, str]:
        """The person signs in at the provider: (code, state) for the redirect back."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == f"{BASE}/auth/sso/callback"
        code = "code-" + email
        self.codes[code] = {"email": email, "nonce": q["nonce"], "challenge": q["code_challenge"]}
        return code, q["state"]

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        url = str(req.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
                                             "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            if self.jwks_down:
                raise httpx.ConnectError("down")
            n = self.key.public_key().public_numbers()
            return httpx.Response(200, json={"keys": [{"kty": "RSA", "kid": "k1", "n": _int(n.n), "e": _int(n.e)}]})
        if url == f"{ISSUER}/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            assert form["client_id"] == CLIENT
            if form["grant_type"] == "authorization_code":
                c = self.codes.pop(form["code"], None)
                if c is None:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                ch = _b64(hashlib.sha256(form["code_verifier"].encode()).digest())
                if ch != c["challenge"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                rt = "rt-" + c["email"]
                self.refresh[rt] = c["email"]
                out = {"id_token": self.id_token(c["email"], c["nonce"]), "access_token": "at"}
                if not self.no_refresh:
                    out["refresh_token"] = rt
                return httpx.Response(200, json=out)
            if form["grant_type"] == "refresh_token":
                if self.token_error:
                    return httpx.Response(400, json={"error": self.token_error})
                email = self.refresh.get(form["refresh_token"])
                if email is None or email not in self.users:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                out = {"id_token": self.id_token(email), "access_token": "at"}
                if self.rotate:
                    del self.refresh[form["refresh_token"]]
                    out["refresh_token"] = f"rt-{email}-{len(self.refresh)}-{time.monotonic_ns()}"
                    self.refresh[out["refresh_token"]] = email
                return httpx.Response(200, json=out)
        return httpx.Response(404)


@pytest.fixture
def idp():
    return FakeIdP()


def _settings(settings, **kw):
    settings.public_url = BASE
    settings.admin_token = "break-glass"
    settings.sso_mode = "oidc"
    settings.sso_issuer = ISSUER
    settings.sso_client_id = CLIENT
    settings.sso_recheck_minutes = 60
    settings.sso_max_stale_minutes = 120
    for k, v in kw.items():
        setattr(settings, f"sso_{k}" if not k.startswith("sso_") and hasattr(settings, f"sso_{k}") else k, v)
    return settings


@pytest.fixture
def make(settings, idp):
    clients = []

    def _make(**kw):
        app = create_app(_settings(settings, **kw))
        c = TestClient(app, base_url=BASE, follow_redirects=False)
        c.__enter__()
        clients.append(c)
        app.state.hangar.sso.transport = httpx.MockTransport(idp.handler)
        h = app.state.hangar
        if people.get(h.store, "alice") is None:
            people.add(h.store, "alice", role="member")
            people.link(h.store, "alice", "email", "alice@corp.example")
        idp.user("alice@corp.example")
        return c, h

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def sign_in(c: TestClient, idp: FakeIdP, email: str = "alice@corp.example", query: str = "") -> httpx.Response:
    r = c.get(f"/auth/sso/login{query}")
    assert r.status_code == 302, r.text
    code, state = idp.approve(r.headers["location"], email)
    return c.get(f"/auth/sso/callback?code={code}&state={state}")


def why(h, r: httpx.Response) -> str:
    """A refusal page shows only a reference; the reason is in the audit log under it."""
    assert r.status_code in (403, 503) and "Sign-in" in r.text
    ref = r.text.split("<code>")[1].split("</code>")[0]
    [e] = [e for e in h.store.audit_tail(200) if e.detail.get("ref") == ref]
    return e.detail["why"]


def session_cookie(r: httpx.Response) -> str:
    return next(v for v in r.headers.get_list("set-cookie") if v.startswith(sso.SESSION_COOKIE + "="))


# ====================================================================== off by default
def test_off_by_default(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        h = app.state.hangar
        assert settings.sso_mode == "off" and h.auth.gate is None and not h.sso.on
        assert c.get("/auth/sso/config").json() == {"mode": "off", "login": None}
        assert c.get("/auth/sso/login").status_code == 404
        assert c.get("/api/sso/bindings", headers={"Authorization": "Bearer x"}).status_code in (401, 404)


def test_settings_that_cant_start(settings):
    s = _settings(settings, sso_client_id=None)
    assert any("CLIENT_ID" in p for p in sso.problems(s))
    s = _settings(settings, sso_default_role="member", sso_allowed_domains="")
    assert any("ALLOWED_DOMAINS" in p for p in sso.problems(s))
    s = _settings(settings, sso_mode="iap", sso_iap_audience=None)
    assert any("IAP_AUDIENCE" in p for p in sso.problems(s))
    s = _settings(settings, sso_mode="oidc", sso_iap_audience=None, sso_default_role="none")
    s.public_url = "http://hangar.example"
    assert any("https" in p for p in sso.problems(s))
    with pytest.raises(ValueError, match="https"):
        create_app(s)


# ====================================================================== the console
def test_console_signs_in_with_a_strict_http_only_cookie(make, idp):
    c, h = make()
    r = sign_in(c, idp)
    assert r.status_code == 200 and 'http-equiv="refresh"' in r.text  # a same-site hop, so Strict applies
    cookie = session_cookie(r)
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
        assert flag in cookie
    assert "Domain" not in cookie and "Max-Age=3600" in cookie
    me = c.get("/api/me")
    assert me.status_code == 200 and me.json()["person"] == "alice"
    [tok] = [t for t in h.store.list(ApiToken) if t.name == sso.SESSION_NAME]
    assert tok.person == "alice" and tok.scopes == ["user", "jobs"] and tok.expires_at > time.time() + 3000
    b = sso.binding(h.store, "alice")
    assert b["sub"] == "sub-alice" and b["refresh"] and "rt-alice" not in json.dumps(b)  # sealed at rest


def test_cookie_requests_that_change_things_need_the_console_header(make, idp):
    c, _ = make()
    sign_in(c, idp)
    assert c.post("/api/tokens", json={"name": "x", "scopes": ["user"]}).status_code == 403
    r = c.post("/api/tokens", json={"name": "x", "scopes": ["user"]}, headers={"X-Requested-With": "hangar",
                                                                             "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = c.post("/api/tokens", json={"name": "x", "scopes": ["user"]}, headers={"X-Requested-With": "hangar",
                                                                             "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200, r.text


def test_sign_out_revokes_the_session(make, idp):
    c, h = make()
    sign_in(c, idp)
    assert c.post("/auth/sso/logout").status_code == 403
    r = c.post("/auth/sso/logout", headers={"X-Requested-With": "hangar"})
    assert r.status_code == 200 and "Max-Age=0" in session_cookie(r)
    assert not [t for t in h.store.list(ApiToken) if t.name == sso.SESSION_NAME]


def test_an_expired_session_stops_working(make, idp):
    c, h = make()
    sign_in(c, idp)
    [tok] = [t for t in h.store.list(ApiToken) if t.name == sso.SESSION_NAME]
    tok.expires_at = time.time() - 1
    h.store.put(tok)
    assert c.get("/api/me").status_code == 401


def test_a_sign_in_belongs_to_the_browser_that_started_it(make, idp):
    c, h = make()
    r = c.get("/auth/sso/login")
    code, state = idp.approve(r.headers["location"], "alice@corp.example")
    c.cookies.clear()  # the victim's browser: no pending cookie (login CSRF)
    r = c.get(f"/auth/sso/callback?code={code}&state={state}")
    assert r.status_code == 403 and "another browser" in why(h, r)
    assert r.headers.get("content-security-policy")


def test_an_unreachable_provider_shows_a_reference_not_the_error(make, idp):
    c, h = make()
    idp.down = True
    r = c.get("/auth/sso/login")
    assert r.status_code == 503 and "ConnectError" not in r.text and "idp.example" not in r.text
    assert "discovery document" in why(h, r)
    assert any(e.action == "sso.unavailable" for e in h.store.audit_tail(20))


def test_a_state_works_once(make, idp):
    c, _ = make()
    r = c.get("/auth/sso/login")
    code, state = idp.approve(r.headers["location"], "alice@corp.example")
    assert c.get(f"/auth/sso/callback?code={code}&state={state}").status_code == 200
    assert c.get(f"/auth/sso/callback?code={code}&state={state}").status_code == 403


def test_a_tampered_or_foreign_id_token_is_refused(make, idp, monkeypatch):
    c, h = make()
    real = idp.id_token
    monkeypatch.setattr(idp, "id_token", lambda e, n=None, **o: real(e, n, aud="someone-else"))
    r = sign_in(c, idp)
    assert r.status_code == 403 and "another application" in why(h, r)
    assert "another application" not in r.text and "corp.example" not in r.text  # the page names no reason
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(idp, "id_token", lambda e, n=None, **o: jwt_rs(other, {"iss": ISSUER, "aud": CLIENT}))
    r = sign_in(c, idp)
    assert r.status_code == 403 and "signature" in why(h, r)
    monkeypatch.setattr(idp, "id_token", lambda e, n=None, **o: real(e, "not-the-nonce"))
    assert "this sign-in" in why(h, sign_in(c, idp))
    assert c.get("/api/me").status_code == 401


@pytest.mark.asyncio
async def test_jwt_checks(idp):
    jwks = sso.JWKS(f"{ISSUER}/jwks", lambda: httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)))
    idp.user("a@corp.example")
    ok = idp.id_token("a@corp.example")
    assert (await sso.verify_jwt(ok, jwks, issuer=ISSUER, audience=CLIENT, algs=("RS256",)))["sub"] == "sub-a"
    for bad, why in ((idp.id_token("a@corp.example", exp=int(time.time()) - 3600), "expired"),
                     (idp.id_token("a@corp.example", iss="https://evil.example"), "another issuer"),
                     (ok.rsplit(".", 1)[0] + ".AAAA", "signature"),
                     ("nope", "isn't a JWT")):
        with pytest.raises(sso.Refused, match=why):
            await sso.verify_jwt(bad, jwks, issuer=ISSUER, audience=CLIENT, algs=("RS256",))
    with pytest.raises(sso.Refused, match="algorithm"):  # an RSA token can't pass as another algorithm
        await sso.verify_jwt(ok, jwks, issuer=ISSUER, audience=CLIENT, algs=("ES256",))


# ====================================================================== who gets in
def test_an_unknown_email_gets_nothing_by_default(make, idp):
    c, h = make()
    idp.user("mallory@corp.example")
    r = sign_in(c, idp, "mallory@corp.example")
    assert r.status_code == 403 and "isn't linked" in why(h, r)
    assert people.resolve_external(h.store, "email", "mallory@corp.example") is None


def test_an_unknown_email_may_become_a_member_in_an_allowed_domain(make, idp):
    c, h = make(sso_default_role="member", sso_allowed_domains="corp.example")
    idp.user("bob.smith@corp.example")
    assert sign_in(c, idp, "bob.smith@corp.example").status_code == 200
    p = people.resolve_external(h.store, "email", "bob.smith@corp.example")
    assert p is not None and p.role == "member" and p.id == "bob.smith"
    idp.user("eve@elsewhere.example")
    assert "allowed domain" in why(h, sign_in(c, idp, "eve@elsewhere.example"))


def test_unverified_emails_and_other_groups_are_refused(make, idp):
    c, h = make(sso_required_group="hangar-users")
    idp.user("alice@corp.example", verified=False)
    assert "doesn't vouch for" in why(h, sign_in(c, idp))
    idp.user("alice@corp.example", groups=["sales"])
    assert "isn't in the group" in why(h, sign_in(c, idp))


def test_a_reused_email_with_another_subject_is_refused(make, idp):
    c, h = make()
    assert sign_in(c, idp).status_code == 200
    idp.user("alice@corp.example", sub="someone-new")
    r = sign_in(c, idp)
    assert r.status_code == 403 and "different account" in why(h, r)
    assert c.delete("/api/sso/bindings/alice", headers={"Authorization": "Bearer break-glass"}).status_code == 200
    assert sign_in(c, idp).status_code == 200


# ====================================================================== the CLI
def confirm(c: TestClient, r: httpx.Response) -> httpx.Response:
    """Press the confirmation page's button: a CLI sign-in waits for it."""
    assert r.status_code == 200 and "/auth/sso/cli/confirm" in r.text, r.text
    value = r.text.split('name="c" value="')[1].split('"')[0]
    return c.post("/auth/sso/cli/confirm", content=f"c={value}",
                  headers={"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"})


def _cli_login(c, idp, verifier="v" * 50, port=0):
    challenge = sso.s256(verifier)
    return confirm(c, sign_in(c, idp, query=f"?cli_port={port}&cli_challenge={challenge}"))


def _refresh_url(r: httpx.Response) -> str:
    return r.text.split('content="0;url=')[1].split('"')[0].replace("&amp;", "&")


def test_cli_gets_a_token_through_the_browser(make, idp):
    c, h = make()
    r = _cli_login(c, idp)
    assert r.status_code == 200 and sso.SESSION_COOKIE not in r.headers.get("set-cookie", "")
    code = r.text.split("<code>")[2].split("</code>")[0]
    assert c.post("/auth/sso/cli/token", json={"code": code, "verifier": "w" * 50}).status_code == 400
    r = _cli_login(c, idp)
    code = r.text.split("<code>")[2].split("</code>")[0]
    r = c.post("/auth/sso/cli/token", json={"code": code, "verifier": "v" * 50, "name": "laptop"})
    assert r.status_code == 200 and r.json()["person"] == "alice"
    tok = r.json()["token"]
    me = c.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200 and me.json()["person"] == "alice"
    assert c.post("/auth/sso/cli/token", json={"code": code, "verifier": "v" * 50}).status_code == 400  # once


def test_cli_loopback_redirect_goes_to_localhost_only(make, idp):
    c, _ = make()
    r = _cli_login(c, idp, port=53682)
    assert r.status_code == 200 and _refresh_url(r).startswith("http://127.0.0.1:53682/sso?code=hgc_")
    assert r.headers["referrer-policy"] == "no-referrer"
    assert c.get("/auth/sso/login?cli_port=x&cli_challenge=y").status_code == 400


def test_a_cli_sign_in_waits_for_the_person_to_confirm(make, idp):
    """A crafted CLI link can't hand its author a token: the person signs in, then sees whose command line asks."""
    c, h = make()
    r = sign_in(c, idp, query=f"?cli_port=0&cli_challenge={sso.s256('a' * 50)}")
    assert r.status_code == 200 and "hgc_" not in r.text and "hangar login --sso" in r.text
    value = r.text.split('name="c" value="')[1].split('"')[0]
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    r = c.post("/auth/sso/cli/confirm", content=f"c={value}", headers={**form, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and "hgc_" not in r.text
    c.cookies.delete(sso.CONFIRM_COOKIE)  # another browser: no confirmation cookie
    r = c.post("/auth/sso/cli/confirm", content=f"c={value}", headers=form)
    assert r.status_code == 403 and "hgc_" not in r.text


# ====================================================================== deprovisioning
def _cli_token(c, idp) -> str:
    r = _cli_login(c, idp)
    code = r.text.split("<code>")[2].split("</code>")[0]
    return c.post("/auth/sso/cli/token", json={"code": code, "verifier": "v" * 50}).json()["token"]


def test_removed_at_the_provider_disables_tokens_without_an_admin(make, idp):
    c, h = make()
    tok = _cli_token(c, idp)
    hdr = {"Authorization": f"Bearer {tok}"}
    assert c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"}).json() == {"ok": 1}
    assert c.get("/api/me", headers=hdr).status_code == 200
    del idp.users["alice@corp.example"]  # suspended or deleted in the provider
    assert c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"}).json() == {"disabled": 1}
    assert c.get("/api/me", headers=hdr).status_code == 401
    assert not [t for t in h.store.list(ApiToken) if t.person == "alice"]
    assert people.get(h.store, "alice") is not None  # the person and their records stay
    b = sso.binding(h.store, "alice")
    assert b["disabled_at"] and "refused the refresh" in b["disabled_why"] and "refresh" not in b
    assert any(e.action == "sso.deprovisioned" for e in h.store.audit_tail(50))


def test_removed_from_the_group_disables_tokens(make, idp):
    c, h = make(sso_required_group="hangar-users")
    tok = _cli_token(c, idp)
    idp.user("alice@corp.example", groups=["former-staff"])
    c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"})
    assert c.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_an_unreachable_provider_never_disables_but_staleness_fails_closed(make, idp):
    c, h = make()
    tok = _cli_token(c, idp)
    hdr = {"Authorization": f"Bearer {tok}"}
    idp.down = True
    assert c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"}).json() == {"unreachable": 1}
    assert c.get("/api/me", headers=hdr).status_code == 200
    b = sso.binding(h.store, "alice")
    b["verified_at"] = time.time() - 121 * 60
    h.store.kv_set_json("sso:binding:alice", b)
    assert c.get("/api/me", headers=hdr).status_code == 401  # not confirmed for too long
    assert c.get("/api/me", headers={"Authorization": "Bearer break-glass"}).status_code == 200  # break-glass
    idp.down = False
    c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"})
    assert c.get("/api/me", headers=hdr).status_code == 200  # confirmed again


def test_sso_required_refuses_people_who_never_signed_in(make, idp):
    """On by default whenever a mode is: a token minted before SSO is inside deprovisioning too."""
    c, h = make()
    assert h.settings.sso_required is None and sso.required(h.settings)
    people.add(h.store, "carol", role="member")
    raw, _ = h.auth.create("carol-laptop", ["user"], person="carol")
    assert c.get("/api/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 401
    h.settings.sso_required = False  # the explicit opt-out, for a rollout
    assert c.get("/api/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 200


@pytest.mark.asyncio
async def test_opting_out_of_sso_required_warns_and_service_people_can_be_exempt(make, idp):
    c, h = make(sso_required=False)
    checks = {x["name"]: x for x in await sso.doctor_checks(h)}
    assert checks["sso required"]["status"] == "warn"
    h.settings.sso_required = None
    people.add(h.store, "ci-bot", role="member")
    raw, _ = h.auth.create("ci", ["jobs"], person="ci-bot")
    assert h.auth.principal(raw) is None
    h.settings.sso_exempt_people = "ci-bot"
    assert h.auth.principal(raw) is not None


@pytest.mark.asyncio
async def test_doctor_warns_while_break_glass_is_in_use(make, idp):
    c, h = make()
    checks = {x["name"]: x for x in await sso.doctor_checks(h)}
    assert checks["sso break-glass"]["status"] == "ok" and checks["sso provider"]["status"] == "ok"
    assert c.get("/api/me", headers={"Authorization": "Bearer break-glass"}).status_code == 200
    checks = {x["name"]: x for x in await sso.doctor_checks(h)}
    assert checks["sso break-glass"]["status"] == "warn" and "used" in checks["sso break-glass"]["detail"]
    assert "sso required" not in checks  # on by default whenever a mode is


@pytest.mark.asyncio
async def test_doctor_warns_when_people_have_no_refresh_token(make, idp):
    c, h = make()
    idp.no_refresh = True
    sign_in(c, idp)
    checks = {x["name"]: x for x in await sso.doctor_checks(h)}
    assert checks["sso recheck"]["status"] == "fail" and "offline_access" in checks["sso recheck"]["fix"]


def test_the_session_cookie_is_not_a_bearer_token(make, idp):
    """Copied out of the browser, the cookie's value is refused as a bearer: only the console's checks can use it."""
    c, h = make()
    raw = session_cookie(sign_in(c, idp)).split("=", 1)[1].split(";")[0]
    assert c.get("/api/me").status_code == 200
    c.cookies.clear()
    assert c.get("/api/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_a_rotated_refresh_token_is_kept_even_when_the_check_fails(make, idp):
    c, h = make()
    tok = _cli_token(c, idp)
    idp.rotate = True
    idp.jwks_down = True
    h.sso._jwks = None
    h.sso._meta = None
    admin = {"Authorization": "Bearer break-glass"}
    assert c.post("/api/sso/recheck", headers=admin).json() == {"unreachable": 1}
    idp.jwks_down = False
    assert c.post("/api/sso/recheck", headers=admin).json() == {"ok": 1}  # the new token, not the spent one
    assert c.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_a_misconfigured_client_drops_no_one(make, idp):
    """unauthorized_client is about hangar's registration, not the person; and a pass that would drop most of the
    team revokes nothing, but suspends them (every token stops) until a clean pass, and doctor says why."""
    c, h = make(sso_required_group="hangar-users")
    for name in ("bob", "carol"):
        people.add(h.store, name, role="member")
        people.link(h.store, name, "email", f"{name}@corp.example")
        idp.user(f"{name}@corp.example")
        sign_in(c, idp, f"{name}@corp.example")
    tok = _cli_token(c, idp)
    svc, _ = h.auth.create("ci", ["jobs"], person="alice")
    admin = {"Authorization": "Bearer break-glass"}
    idp.token_error = "unauthorized_client"
    assert c.post("/api/sso/recheck", headers=admin).json() == {"unreachable": 3}
    idp.token_error = None
    for name in ("alice", "bob", "carol"):  # the group claim went missing from the client's tokens
        idp.users[f"{name}@corp.example"]["groups"] = []
    assert c.post("/api/sso/recheck", headers=admin).json() == {"halted": 3}
    assert not any(b.get("disabled_at") for b in sso.bindings(h.store))  # nothing revoked...
    assert all(b.get("suspended_at") for b in sso.bindings(h.store))  # ...but nobody's tokens work meanwhile
    assert c.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    assert h.auth.principal(svc) is None
    assert people.resolve_external(h.store, "email", "alice@corp.example") is None  # nor through another door
    assert c.get("/api/me", headers=admin).status_code == 200  # break-glass still works
    import asyncio

    checks = {x["name"]: x for x in asyncio.run(sso.doctor_checks(h))}
    assert checks["sso recheck"]["status"] == "fail" and "suspended them" in checks["sso recheck"]["detail"]
    assert checks["sso suspended"]["status"] == "warn"
    idp.users["alice@corp.example"]["groups"] = ["hangar-users"]
    idp.users["bob@corp.example"]["groups"] = ["hangar-users"]  # one person really left: they're dropped
    assert c.post("/api/sso/recheck", headers=admin).json() == {"ok": 2, "disabled": 1}
    assert sso.binding(h.store, "carol")["disabled_at"] and h.store.kv_get_json("sso:halted") is None
    assert not sso.binding(h.store, "alice")["suspended_at"]  # a clean check lifts the suspension
    assert c.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_staleness_stops_a_persons_own_tokens_not_their_service_tokens(make, idp):
    c, h = make()
    tok = _cli_token(c, idp)
    svc, _ = h.auth.create("ci", ["jobs"], person="alice")
    b = sso.binding(h.store, "alice")
    b["verified_at"] = time.time() - 121 * 60
    h.store.kv_set_json("sso:binding:alice", b)
    assert c.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    assert h.auth.principal(svc) is not None  # a jobs token rides out a provider outage
    del idp.users["alice@corp.example"]
    c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"})
    assert h.auth.principal(svc) is None  # and is revoked with the rest when the provider drops them


def test_service_tokens_stop_after_the_service_grace(make, idp):
    """A removed person's jobs token can't outlive them: a provider that never answers for them (no refresh token,
    IAP, a halted pass) stops it after HANGAR_SSO_SERVICE_STALE_HOURS."""
    c, h = make()
    idp.no_refresh = True
    sign_in(c, idp)
    svc, _ = h.auth.create("ci", ["jobs"], person="alice")
    b = sso.binding(h.store, "alice")
    b["verified_at"] = time.time() - 23 * 3600
    h.store.kv_set_json("sso:binding:alice", b)
    assert h.auth.principal(svc) is not None
    b["verified_at"] = time.time() - 25 * 3600
    h.store.kv_set_json("sso:binding:alice", b)
    assert h.auth.principal(svc) is None
    s = _settings(h.settings, sso_service_stale_hours=1, sso_max_stale_minutes=120)
    assert any("SERVICE_STALE_HOURS" in p for p in sso.problems(s))


def test_a_first_sign_in_needs_a_vouched_email_for_everyone(make, idp):
    """Entra: an unverified, editable email can't claim a member who never signed in, nor become a new member."""
    c, h = make(sso_require_email_verified=False, sso_allowed_domains="corp.example", sso_default_role="member")
    idp.user("alice@corp.example", verified=None, sub="mallory-oid")  # someone else's account, alice's address
    r = sign_in(c, idp)
    assert r.status_code == 403 and "doesn't vouch" in why(h, r) and sso.binding(h.store, "alice") is None
    idp.user("newbie@corp.example", verified=None)
    assert sign_in(c, idp, "newbie@corp.example").status_code == 403 and people.get(h.store, "newbie") is None
    real = idp.id_token
    idp.user("alice@corp.example", verified=None, sub="alice-oid")
    idp.id_token = lambda e, n=None, **o: real(e, n, xms_edov=True)
    assert sign_in(c, idp).status_code == 200 and sso.binding(h.store, "alice")["sub"] == "alice-oid"
    idp.id_token = real  # bound now: later sign-ins find alice by her subject, vouched or not
    assert sign_in(c, idp).status_code == 200


def test_a_bound_person_is_found_by_subject_and_an_admins_links_still_decide(make, idp):
    c, h = make()
    sign_in(c, idp)
    people.add(h.store, "bob", role="admin")
    people.link(h.store, "bob", "email", "bob@corp.example")
    # alice's provider account now reads bob's address: she never becomes bob, and isn't let in as alice either
    idp.users["bob@corp.example"] = idp.users.pop("alice@corp.example")
    r = sign_in(c, idp, "bob@corp.example")
    assert r.status_code == 403 and "now linked to bob" in why(h, r) and sso.binding(h.store, "bob") is None
    idp.users["alice2@corp.example"] = idp.users.pop("bob@corp.example")  # an address linked to nobody: still alice
    assert sign_in(c, idp, "alice2@corp.example").status_code == 200 and c.get("/api/me").json()["person"] == "alice"
    people.unlink(h.store, "alice", "email", "alice@corp.example")  # an admin unlinks her: sign-in and recheck end
    assert "no email linked" in why(h, sign_in(c, idp, "alice2@corp.example"))
    c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"})
    assert sso.binding(h.store, "alice")["disabled_at"]


def test_a_person_never_confirmed_past_the_service_grace_is_suspended_everywhere(make, idp):
    """IAP, or no refresh token: hangar never learns they left. Past the grace, every front door stops them too."""
    c, h = make()
    people.link(h.store, "alice", "slack", "T1/U1")
    idp.no_refresh = True
    sign_in(c, idp)
    c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"})
    assert people.resolve_external(h.store, "slack", "T1/U1") is not None
    b = sso.binding(h.store, "alice")
    b["verified_at"] = time.time() - 25 * 3600
    h.store.kv_set_json("sso:binding:alice", b)
    c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"})
    assert "suspended" in people.deprovisioned(h.store, "alice")
    assert people.resolve_external(h.store, "slack", "T1/U1") is None
    assert sign_in(c, idp).status_code == 200 and not people.deprovisioned(h.store, "alice")  # a sign-in lifts it


def test_a_check_on_an_old_binding_never_disables_a_new_one(make, idp):
    """An admin unbinds, and the person signs in again with another account, while a pass is checking the old one."""
    c, h = make()
    sign_in(c, idp)
    h.store.kv_delete("sso:binding:alice")
    idp.user("alice@corp.example", sub="alice-new")
    sign_in(c, idp)
    h.sso._drop("alice", "the provider refused the refresh", "sub-alice")
    assert not sso.binding(h.store, "alice")["disabled_at"]


def test_slack_refuses_a_deprovisioned_person_rather_than_treating_them_as_an_outsider(make, idp):
    c, h = make()
    people.link(h.store, "alice", "slack", "T1/U1")
    sign_in(c, idp)
    del idp.users["alice@corp.example"]
    c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"})
    from hangar.slack import SlackFrontDoor

    door = SlackFrontDoor.__new__(SlackFrontDoor)
    door.store, door.settings = h.store, h.settings
    h.settings.slack_channels = {"C1": "https://github.com/o/r"}
    h.settings.slack_users = []
    assert door._refusal("T1", "C1", "U1") == "Your hangar access has ended."


def test_with_sso_off_bindings_left_behind_stop_no_one(make, idp, settings):
    c, h = make()
    sign_in(c, idp)
    del idp.users["alice@corp.example"]
    c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"})
    assert people.deprovisioned(h.store, "alice")
    settings.sso_mode = "off"
    sso.SSO(h)  # a restart with SSO off
    assert not people.deprovisioned(h.store, "alice")


def test_a_google_account_must_be_a_workspace_account_of_an_allowed_domain(make, idp, monkeypatch):
    """A personal Google account registered with a work address has email_verified=true but no hd."""
    from hangar.config import Settings

    s = _settings(Settings(), sso_issuer="https://accounts.google.com", sso_allowed_domains="")
    assert any("Google issuer" in p for p in sso.problems(s))
    c, h = make(sso_allowed_domains="corp.example")
    monkeypatch.setattr(sso, "google", lambda settings: True)
    r = sign_in(c, idp)
    assert r.status_code == 403 and "(hd)" in why(h, r)
    real = idp.id_token
    idp.id_token = lambda e, n=None, **o: real(e, n, hd="corp.example")
    assert sign_in(c, idp).status_code == 200


def test_iap_checks_the_workspace_domain(make):
    iap = FakeIAP()
    c, h = make(sso_mode="iap", sso_iap_audience=AUD, sso_allowed_domains="corp.example")
    h.sso.transport = httpx.MockTransport(iap.jwks)
    assert c.get("/auth/sso/login", headers={sso.IAP_HEADER: iap.assertion("alice@corp.example")}).status_code == 403
    r = c.get("/auth/sso/login", headers={sso.IAP_HEADER: iap.assertion("alice@corp.example", hd="corp.example")})
    assert r.status_code == 200


def test_a_deprovisioned_person_gets_nothing_through_other_front_doors(make, idp):
    """Slack, GitHub and Jira name a person through people.resolve_external: a leaver resolves to nobody there.
    The relay and Tower passports refuse them outright (tests/test_relay.py, tests/test_job_passports.py)."""
    c, h = make()
    people.link(h.store, "alice", "slack", "T1/U1")
    sign_in(c, idp)
    assert people.resolve_external(h.store, "slack", "T1/U1").id == "alice"
    del idp.users["alice@corp.example"]
    c.post("/api/sso/recheck", headers={"Authorization": "Bearer break-glass"})
    assert people.deprovisioned(h.store, "alice")
    assert people.resolve_external(h.store, "slack", "T1/U1") is None
    assert people.resolve_external(h.store, "email", "alice@corp.example") is None
    assert people.resolve_external(h.store, "email", "alice@corp.example", include_deprovisioned=True).id == "alice"


def test_break_glass_is_the_bootstrap_tokens_id_not_its_name(make, idp):
    from hangar.auth import BOOTSTRAP_KV, break_glass_token_id

    c, h = make()
    imposter, _ = h.auth.create("bootstrap-admin")  # named like it, but not the recorded bootstrap token
    h.store.kv_set(BOOTSTRAP_KV, "tok-the-real-one")
    assert break_glass_token_id(h.store) == "tok-the-real-one" and h.auth.principal(imposter) is None
    h.store.kv_delete(BOOTSTRAP_KV)  # a store from before the id was recorded: the oldest such token, pinned once
    assert break_glass_token_id(h.store) is not None and h.auth.principal(imposter) is not None
    later, _ = h.auth.create("bootstrap-admin")
    assert h.auth.principal(later) is None
    h.store.kv_delete(BOOTSTRAP_KV)
    for t in h.store.list(ApiToken):
        if t.name == "bootstrap-admin":
            h.store.delete(ApiToken, t.id)
    assert break_glass_token_id(h.store) is None  # none to pin: recorded as none, so a later one is ordinary too
    again, _ = h.auth.create("bootstrap-admin")
    assert break_glass_token_id(h.store) is None and h.auth.principal(again) is None


def test_a_refusal_is_logged_escaped_and_audited_at_a_bounded_rate(make, idp, caplog):
    c, h = make()
    r = c.get("/auth/sso/callback?error=x%0Afake%20log%20line")
    reason = why(h, r)
    assert "\n" not in reason and "\\n" in reason  # the caller's newline is escaped, not written
    assert not any("\n" in rec.getMessage() for rec in caplog.records if rec.name == "hangar.sso")
    for _ in range(sso.REFUSED_AUDITS_PER_MINUTE + 5):
        c.get("/auth/sso/callback?error=x")
    assert len([e for e in h.store.audit_tail(200) if e.action == "sso.refused"]) == sso.REFUSED_AUDITS_PER_MINUTE


def test_small_checks(settings):
    assert not sso._local_url("http://localhost.evil.com") and sso._local_url("http://localhost:8080/x")
    s = _settings(settings, sso_mode="iap", sso_iap_audience="/projects/123/apps/my-proj")
    assert not any("IAP_AUDIENCE" in p for p in sso.problems(s))
    s = _settings(settings, sso_mode="iap", sso_iap_audience="/projects/123/anything")
    assert any("IAP_AUDIENCE" in p for p in sso.problems(s))
    with pytest.raises(sso.Refused):
        sso._email("alice@corp.e\u212axample")  # the Kelvin sign, which lowercases to k
    assert sso._email("  Alice@Corp.Example ") == "alice@corp.example"


@pytest.mark.asyncio
async def test_jwks_ignores_encryption_keys(idp):
    n = idp.key.public_key().public_numbers()
    keys = [{"kty": "RSA", "kid": "enc", "use": "enc", "n": _int(n.n), "e": _int(n.e)},
            {"kty": "RSA", "kid": "sig", "use": "sig", "n": _int(n.n), "e": _int(n.e)}]
    jwks = sso.JWKS("https://idp.example/jwks", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": keys}))))
    assert await jwks.key("sig") is not None and set(jwks._keys) == {"sig"}


def test_the_cli_loopback_gets_its_state_back(make, idp):
    c, _ = make()
    challenge = sso.s256("v" * 50)
    r = confirm(c, sign_in(c, idp, query=f"?cli_port=53682&cli_challenge={challenge}&cli_state={'s' * 24}"))
    assert f"state={'s' * 24}" in _refresh_url(r)
    assert c.get(f"/auth/sso/login?cli_port=1&cli_challenge={challenge}&cli_state=bad!").status_code == 400


def test_an_admins_first_sign_in_needs_a_vouched_email(make, idp):
    """Entra's email is editable and unverified: it can't make whoever claims an admin's address that admin."""
    c, h = make(sso_require_email_verified=False, sso_allowed_domains="corp.example")
    people.add(h.store, "root", role="admin")
    people.link(h.store, "root", "email", "root@corp.example")
    idp.user("root@corp.example", verified=None)
    r = sign_in(c, idp, "root@corp.example")
    assert r.status_code == 403 and "xms_edov" in why(h, r)
    assert sign_in(c, idp).status_code == 200  # a member is fine (the domain is allowed)
    real = idp.id_token
    idp.id_token = lambda e, n=None, **o: real(e, n, xms_edov=True)
    assert sign_in(c, idp, "root@corp.example").status_code == 200
    idp.id_token = lambda e, n=None, **o: real(e, n, xms_edov=False)
    assert "isn't verified" in why(h, sign_in(c, idp, "root@corp.example"))


def test_iap_cant_check_a_group():
    from hangar.config import Settings

    s = _settings(Settings(), sso_mode="iap", sso_iap_audience=AUD, sso_required_group="hangar-users")
    assert any("IAP's header carries no groups" in p for p in sso.problems(s))


# ====================================================================== IAP
class FakeIAP:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())

    def jwks(self, req: httpx.Request) -> httpx.Response:
        n = self.key.public_key().public_numbers()
        return httpx.Response(200, json={"keys": [{"kty": "EC", "crv": "P-256", "kid": "iap1",
                                                   "x": _int(n.x, 32), "y": _int(n.y, 32)}]})

    def assertion(self, email: str, aud: str = AUD, **over) -> str:
        return jwt_es(self.key, {"iss": sso.IAP_ISSUER, "aud": aud, "sub": "accounts.google.com:" + email,
                                 "email": email, "iat": int(time.time()), "exp": int(time.time()) + 600, **over})


def test_iap_mode(make):
    iap = FakeIAP()
    c, h = make(sso_mode="iap", sso_iap_audience=AUD)
    h.sso.transport = httpx.MockTransport(iap.jwks)
    assert c.get("/auth/sso/login").status_code == 403  # not through IAP
    hdr = {sso.IAP_HEADER: iap.assertion("alice@corp.example")}
    assert c.get("/auth/sso/login", headers={sso.IAP_HEADER: iap.assertion("alice@corp.example", aud="/x")}
                 ).status_code == 403
    r = c.get("/auth/sso/login", headers=hdr)
    assert r.status_code == 200 and "SameSite=Strict" in session_cookie(r)
    assert c.get("/api/me", headers=hdr).json()["person"] == "alice"
    assert c.get("/api/me").status_code == 401  # the cookie alone isn't enough: IAP must vouch each time
    people.add(h.store, "bob", role="member")
    people.link(h.store, "bob", "email", "bob@corp.example")
    assert c.get("/api/me", headers={sso.IAP_HEADER: iap.assertion("bob@corp.example")}).status_code == 401
    # the CLI's bearer routes stay outside IAP: a token works with no assertion
    raw, _ = h.auth.create("alice-cli", ["user"], person="alice")
    c.cookies.clear()
    assert c.get("/api/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 200


def test_hangar_login_sso_end_to_end(make, idp, monkeypatch):
    """`hangar login --sso`: the browser signs in, the loopback listener gets the code, the CLI trades it."""
    import webbrowser

    from typer.testing import CliRunner

    from hangar.cli import _config_path
    from hangar.cli import app as cli

    c, h = make()

    def browser(link: str) -> bool:
        u = urlsplit(link)
        r = c.get(f"{u.path}?{u.query}")
        code, state = idp.approve(r.headers["location"], "alice@corp.example")
        back = _refresh_url(confirm(c, c.get(f"/auth/sso/callback?code={code}&state={state}")))
        assert back.startswith("http://127.0.0.1:")
        with httpx.Client(trust_env=False) as local:
            assert local.get(back).status_code == 200
        return True

    def post(url, **kw):
        return c.post(urlsplit(url).path, json=kw["json"])

    def get(url, **kw):
        return c.get(urlsplit(url).path, headers=kw.get("headers"))

    monkeypatch.setattr(webbrowser, "open", browser)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    res = CliRunner().invoke(cli, ["login", BASE, "--sso", "--name", "my-laptop"])
    assert res.exit_code == 0, res.output
    saved = json.loads(_config_path().read_text())
    tok = next(t for t in h.store.list(ApiToken) if t.name == "my-laptop")
    assert tok.person == "alice" and saved["url"] == BASE and saved["token"].startswith("hgr_")


def test_console_keeps_no_token_in_session_storage_with_sso():
    """With SSO on, the console's session is an HttpOnly cookie; a break-glass token stays in the tab's memory."""
    from pathlib import Path

    ui = Path(__file__).resolve().parents[1] / "src" / "hangar" / "ui"
    app = (ui / "app.js").read_text()
    store = app[app.index("const store = {"):app.index("};", app.index("const store = {"))]
    getter, setter = (line for line in store.splitlines() if "get token()" in line or "set token(" in line)
    for line in (getter, setter):  # the SSO branch returns before sessionStorage is touched
        assert line.index('sso.mode !== "off"') < line.index("sessionStorage")
    assert '"X-Requested-With": "hangar"' in app and "/auth/sso/config" in app and "/auth/sso/logout" in app
    page = (ui / "index.html").read_text()
    assert 'href="/auth/sso/login"' in page and 'id="gate-break-glass"' in page
