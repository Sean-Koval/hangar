"""Short-lived cloud credentials (--- egress): hangar as an OIDC issuer, a token per sandbox that verifies against its
JWKS, the GCP credential configuration google-auth itself reads and exchanges (against a fake STS), AWS's web
identity variables, renewal, and the discovery endpoints."""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from types import SimpleNamespace

import pytest

from hangar import cloudcreds, egress
from hangar.cloudcreds import CloudSpec
from hangar.config import Settings
from hangar.envspec import EnvSpec
from hangar.models import Sandbox, SandboxState

PROVIDER = "projects/123456789/locations/global/workloadIdentityPools/hangar-pool/providers/hangar-oidc"


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(token: str, jwks: dict) -> dict:
    """Check an RS256 JWT against a JWKS the way a relying party does; return its claims."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    head, body, sig = token.split(".")
    h = json.loads(_b64(head))
    assert h["alg"] == "RS256"
    k = next(k for k in jwks["keys"] if k["kid"] == h["kid"])
    pub = rsa.RSAPublicNumbers(int.from_bytes(_b64(k["e"]), "big"), int.from_bytes(_b64(k["n"]), "big")).public_key()
    pub.verify(_b64(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return json.loads(_b64(body))


def test_the_spec_is_checked():
    CloudSpec(gcp={"provider": "//iam.googleapis.com/" + PROVIDER,
                   "service_account": "ci-bot@proj-1.iam.gserviceaccount.com"},
              aws={"role_arn": "arn:aws:iam::123456789012:role/hangar-agents"})
    for bad in ({"gcp": {"provider": "projects/my-project/locations/global/workloadIdentityPools/p/providers/x"}},
                {"gcp": {"provider": PROVIDER, "service_account": "someone@gmail.com"}},
                {"aws": {"role_arn": "arn:aws:iam::12:role/x"}}, {"lifetime_minutes": 2}, {"azure": {}}):
        with pytest.raises(ValueError):
            CloudSpec(**bad)
    assert CloudSpec(gcp={"provider": PROVIDER}).gcp.token_audience == f"https://iam.googleapis.com/{PROVIDER}"
    from hangar import identity

    assert "cloud" in identity.ADMIN_FIELDS


@pytest.fixture
def cloud_env(fleet):
    fleet.settings.public_url = "https://hangar.example.com"
    spec = EnvSpec(name="plain", harnesses=["claude-code"], cloud={
        "gcp": {"provider": PROVIDER}, "aws": {"role_arn": "arn:aws:iam::123456789012:role/hangar-agents",
                                              "region": "us-east-1"}, "lifetime_minutes": 30})
    sb = Sandbox(name="s", environment="plain", provider="docker", person="ann", repo="https://github.com/Acme/App.git",
                 labels={"hangar.session": "ses-1", "hangar.job": "job-1"}, state=SandboxState.RUNNING)
    return spec, sb


def test_tokens_verify_against_the_published_keys_and_carry_the_sandbox(fleet, cloud_env):
    spec, sb = cloud_env
    iss = fleet.cloud
    tok, exp = iss.mint(sb, spec.cloud.gcp.token_audience, 30, now_=time.time())
    c = verify(tok, iss.jwks())
    assert c["iss"] == "https://hangar.example.com" and c["sub"] == "hangar:env:plain"
    assert c["aud"] == f"https://iam.googleapis.com/{PROVIDER}" and c["exp"] == int(exp)
    assert c["exp"] - c["iat"] <= 31 * 60 and c["iat"] <= time.time()
    assert (c["environment"], c["repo"], c["person"], c["sandbox"], c["session"], c["job"]) == \
        ("plain", "acme/app", "ann", sb.id, "ses-1", "job-1")
    assert all("d" not in k and "p" not in k for k in iss.jwks()["keys"])  # public parts only
    assert iss.discovery()["jwks_uri"] == "https://hangar.example.com/oidc/jwks.json"
    old = c
    kid = iss.rotate()
    new = verify(iss.mint(sb, "x", 5)[0], iss.jwks())
    assert json.loads(_b64(iss.mint(sb, "x", 5)[0].split(".")[0]))["kid"] == kid
    assert verify(tok, iss.jwks()) == old and new["aud"] == "x"  # the old token still verifies after rotation
    fleet.cloud._keys = None  # the keys survive a restart (vault)
    assert [k["kid"] for k in fleet.cloud.jwks()["keys"]][0] == kid


async def test_a_sandbox_gets_files_and_variables_and_google_auth_exchanges_the_token(fleet, cloud_env):
    """The configuration hangar writes is the one google-auth reads: build credentials from it and let them refresh
    against a fake STS, which must receive hangar's token for the provider's audience."""
    google_ia = pytest.importorskip("google.auth.identity_pool")
    spec, sb = cloud_env
    prov = fleet.providers["docker"]
    await prov.create(sb, "img")
    env = await fleet.cloud.attach(prov, sb, spec)
    home = prov.boxes[sb.id] / "home/agent/.hangar/cloud"
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == env["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] == \
        "/home/agent/.hangar/cloud/gcp.json"
    assert env["AWS_ROLE_ARN"].endswith("role/hangar-agents") and env["AWS_ROLE_SESSION_NAME"] == f"hangar-{sb.id}"
    assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/home/agent/.hangar/cloud/aws-token"
    for f in ("gcp.json", "gcp-token", "aws-token"):
        assert oct((home / f).stat().st_mode & 0o777) == "0o600"
    assert verify((home / "aws-token").read_text(), fleet.cloud.jwks())["aud"] == "sts.amazonaws.com"
    assert sb.info["cloud"]["gcp"] == PROVIDER
    assert [e.action for e in fleet.store.audit_tail(10)].count("cloud.token") == 1
    cfg = json.loads((home / "gcp.json").read_text())
    assert cfg["type"] == "external_account" and cfg["audience"] == f"//iam.googleapis.com/{PROVIDER}"
    cfg["credential_source"]["file"] = str(home / "gcp-token")  # the fake sandbox's path for the same file
    seen = []

    def request(url, method="GET", body=None, headers=None, **kw):
        form = dict(urllib.parse.parse_qsl(body.decode() if isinstance(body, bytes) else body or ""))
        seen.append((url, form))
        data = json.dumps({"access_token": "ya29.fake", "issued_token_type":
                           "urn:ietf:params:oauth:token-type:access_token", "token_type": "Bearer",
                           "expires_in": 3600}).encode()
        return SimpleNamespace(status=200, headers={}, data=data)

    creds = google_ia.Credentials.from_info(cfg, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(request)
    assert creds.token == "ya29.fake"
    url, form = seen[0]
    assert url == "https://sts.googleapis.com/v1/token"
    assert form["audience"] == f"//iam.googleapis.com/{PROVIDER}"
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:jwt"
    assert verify(form["subject_token"], fleet.cloud.jwks())["aud"] == f"https://iam.googleapis.com/{PROVIDER}"


async def test_tokens_are_renewed_past_half_their_life_and_not_before(fleet, cloud_env, monkeypatch):
    spec, sb = cloud_env
    prov = fleet.providers["docker"]
    await prov.create(sb, "img")
    monkeypatch.setattr(fleet.catalog, "get", lambda name: spec)
    await fleet.cloud.attach(prov, sb, spec)
    fleet.store.put(sb)
    tok = (prov.boxes[sb.id] / "home/agent/.hangar/cloud/gcp-token").read_text()
    assert await fleet.cloud.refresh() == 0
    sb.info["cloud"]["expires_at"] = time.time() + 60
    fleet.store.put(sb)
    assert await fleet.cloud.refresh() == 1
    assert (prov.boxes[sb.id] / "home/agent/.hangar/cloud/gcp-token").read_text() != tok
    assert fleet.store.get(Sandbox, sb.id).info["cloud"]["expires_at"] > time.time() + 25 * 60
    sb.state = SandboxState.STOPPED  # a stopped sandbox gets nothing more
    sb.info["cloud"]["expires_at"] = 0
    fleet.store.put(sb)
    assert await fleet.cloud.refresh() == 0


async def test_without_cloud_nothing_happens_and_without_https_it_fails_closed(fleet, cloud_env):
    spec, sb = cloud_env
    assert await fleet.cloud.attach(None, sb, EnvSpec(name="e")) == {}
    fleet.settings.public_url = "http://localhost:8787"
    with pytest.raises(cloudcreds.CloudError, match="HANGAR_OIDC_ISSUER"):
        await fleet.cloud.attach(fleet.providers["docker"], sb, spec)


def test_a_proxied_sandbox_may_reach_its_clouds_token_exchange(cloud_env):
    spec, _ = cloud_env
    locked = spec.model_copy(update={"egress": egress.EgressSpec(mode="none")})
    pol = egress.resolve(Settings(), locked, harness="claude-code")
    for host in ("sts.googleapis.com", "iamcredentials.googleapis.com", "sts.amazonaws.com",
                 "sts.us-east-1.amazonaws.com"):
        assert pol.allows(host, 443).source == "cloud", host
    assert pol.allows("s3.amazonaws.com", 443) is None


def test_discovery_and_jwks_are_public_and_rotation_is_admin_only(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app
    from tests.conftest import FakeProvider

    settings.admin_token = "t0ken"
    settings.public_url = "https://hangar.example.com"
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.hangar.fleet.providers["docker"] = FakeProvider()
        d = c.get("/.well-known/openid-configuration").json()
        assert d["issuer"] == "https://hangar.example.com" and d["id_token_signing_alg_values_supported"] == ["RS256"]
        keys = c.get("/oidc/jwks.json").json()["keys"]
        assert len(keys) == 1 and set(keys[0]) == {"kty", "use", "alg", "kid", "n", "e"}
        assert c.post("/api/cloud/rotate").status_code in (401, 403)
        r = c.post("/api/cloud/rotate", headers={"Authorization": "Bearer t0ken"})
        assert r.status_code == 200 and len(c.get("/oidc/jwks.json").json()["keys"]) == 2
        r = c.post("/api/cloud/rotate", params={"drop_old": "true"}, headers={"Authorization": "Bearer t0ken"})
        assert r.status_code == 200 and len(c.get("/oidc/jwks.json").json()["keys"]) == 1


# ============================================================================ review
def test_a_members_environment_cant_take_the_subject_of_another(fleet, tmp_path):
    """`cloud:` needs an admin, but a member may create any unused environment name and `extends` an environment
    that has `cloud:`. With `sub` = the sandbox's environment name, a member's `deploy` (extending `ci`) got the
    grants meant for an admin's `deploy` (set up before it, or after it was removed). The subject names the
    environment that sets `cloud:` itself, which only an admin can write."""
    fleet.catalog.save_dir = tmp_path
    fleet.settings.public_url = "https://hangar.example.com"
    fleet.catalog.add({"name": "ci", "harnesses": ["claude-code"], "cloud": {"gcp": {"provider": PROVIDER}}})
    fleet.catalog.add({"name": "deploy", "extends": "ci", "cloud": {}})  # `{}` passes the admin check
    fleet.catalog.add({"name": "own", "extends": "ci",
                       "cloud": {"gcp": {"provider": PROVIDER}, "lifetime_minutes": 10}})
    sb = Sandbox(name="s", environment="deploy", provider="docker", person="bob", state=SandboxState.RUNNING)
    c = verify(fleet.cloud.mint(sb, "x", 5, env=fleet.cloud.subject_env("deploy"))[0], fleet.cloud.jwks())
    assert c["sub"] == "hangar:env:ci" and c["environment"] == "ci" and c["sandbox_environment"] == "deploy"
    assert fleet.cloud.subject_env("own") == "own"  # an environment that sets cloud: itself is its own subject


def test_rotation_retires_old_keys_once_no_token_they_signed_can_be_valid(fleet):
    """Old keys stay published only as long as a token they signed can live (lifetime_minutes is at most 720), and
    `drop_old` (a leaked key) unpublishes them at once; before, a stolen key kept verifying through three more
    rotations."""
    iss = fleet.cloud
    first = iss.keys()[0]["kid"]
    second = iss.rotate()
    assert {k["kid"] for k in iss.jwks()["keys"]} == {first, second}
    for k in iss._keys:
        if k["kid"] == first:
            k["retired"] = time.time() - cloudcreds.MAX_TOKEN_SECONDS - 1
    assert [k["kid"] for k in iss.jwks()["keys"]] == [second]
    third = iss.rotate(drop_old=True)
    assert [k["kid"] for k in iss.jwks()["keys"]] == [third]
    fleet.cloud._keys = None
    assert [k["kid"] for k in fleet.cloud.jwks()["keys"]] == [third]  # what the vault holds too


def test_the_public_jwks_doesnt_parse_private_keys_on_every_request(fleet, monkeypatch):
    """/oidc/jwks.json needs no token, and loading an RSA private key costs milliseconds of the event loop: an
    anonymous loop on it must not do that per request."""
    import cryptography.hazmat.primitives.serialization as ser

    fleet.cloud.jwks()
    calls = []
    real = ser.load_pem_private_key
    monkeypatch.setattr(ser, "load_pem_private_key", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(20):
        fleet.cloud.jwks()
    assert calls == []
