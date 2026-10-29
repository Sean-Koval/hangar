"""The control plane's passport for Tower (KAN-33, #174, spec 0015 §9 phase 2): a token hangar mints itself,
exchanged at Tower's token endpoint (RFC 8693), checked against Tower's JWKS (EdDSA, issuer, audience, expiry, the
act chain) and cached. A fake Tower signs passports the way Tower's aw/workload.py `_passport` builds them."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hangar.cloudcreds import CloudError
from hangar.governance import (
    CONTROL_PLANE_SUB,
    JWT_TYPE,
    TOKEN_EXCHANGE,
    CredentialUnavailable,
    DecisionRequest,
    HttpDecisionPoint,
    PassportCredential,
    PassportRefused,
    PassportVerifier,
    act_chain,
    check_chain,
)
from hangar.govevents import Receiver, passport_credential

ORIGIN = "https://tower.test"
TOKEN_URL = f"{ORIGIN}/oauth/token"


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class Tower:
    """/.well-known/jwks.json, /oauth/token (a passport for whatever agent `agent` says) and /v1/decisions."""

    def __init__(self):
        self.key, self.kid = Ed25519PrivateKey.generate(), "p1"
        self.agent, self.exchanges, self.jwks_reads, self.bearers = "hangar", [], 0, []
        self.status = 200

    def sign(self, claims: dict, key: Ed25519PrivateKey | None = None, kid: str | None = None, alg="EdDSA") -> str:
        head = b64e(json.dumps({"alg": alg, "typ": "JWT", "kid": kid or self.kid}).encode())
        body = b64e(json.dumps(claims, sort_keys=True).encode())
        return f"{head}.{body}.{b64e((key or self.key).sign(f'{head}.{body}'.encode()))}"

    def passport(self, sub: str | None = None, chain: list[str] | None = None, **over) -> dict:
        """Claims as Tower's Passports._passport builds them."""
        chain = chain or [self.agent]
        sub = sub or f"agent:{chain[0]}"
        now = int(time.time())
        claims = {"iss": ORIGIN, "aud": f"{ORIGIN}/mcp", "sub": sub, "agent": chain[-1], "chain": chain,
                  "iat": now, "exp": now + 900, "jti": "j", "fed": {"iss": "https://hangar.test", "sub": "x"}}
        act = None
        for agent in chain if sub != f"agent:{chain[0]}" else chain[1:]:
            act = {"sub": f"agent:{agent}", **({"act": act} if act else {})}
        if act:
            claims["act"] = act
        return {**claims, **over}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            self.jwks_reads += 1
            return httpx.Response(200, json={"keys": [{"kty": "OKP", "crv": "Ed25519", "use": "sig", "alg": "EdDSA",
                                                       "kid": self.kid,
                                                       "x": b64e(self.key.public_key().public_bytes_raw())}]})
        if request.url.path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.exchanges.append(form)
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": self.sign(self.passport()), "issued_token_type": JWT_TYPE,
                                             "token_type": "Bearer", "expires_in": 900})
        if request.url.path == "/v1/decisions":
            self.bearers.append(request.headers["authorization"])
            return httpx.Response(200, json={"v": "hangar.decision/v1", "decision": "wait", "approval": "A-1",
                                             "status": "waiting"})
        return httpx.Response(404)


@pytest.fixture
def tower():
    return Tower()


def client(tower: Tower) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(tower))


def verifier(tower: Tower, **kw) -> PassportVerifier:
    return PassportVerifier(ORIGIN, [f"{ORIGIN}/mcp", ORIGIN], client=client(tower), **kw)


# ====================================================================== minting
async def test_the_control_plane_mints_a_short_token_for_one_audience(fleet, settings):
    settings.oidc_issuer = "https://hangar.test"
    tok, exp = fleet.cloud.mint_service(CONTROL_PLANE_SUB, TOKEN_URL)
    claims = json.loads(b64d(tok.split(".")[1]))
    assert (claims["iss"], claims["sub"], claims["aud"]) == ("https://hangar.test", "hangar:control-plane", TOKEN_URL)
    assert exp - time.time() <= 300 and "person" not in claims and "sandbox" not in claims
    assert json.loads(b64d(tok.split(".")[0]))["kid"] in {k["kid"] for k in fleet.cloud.jwks()["keys"]}
    with pytest.raises(CloudError):
        fleet.cloud.mint_service("hangar:env:prod", TOKEN_URL)  # never an environment's subject
    with pytest.raises(CloudError):
        fleet.cloud.mint_service(CONTROL_PLANE_SUB, TOKEN_URL, lifetime_seconds=3600)


# ====================================================================== the exchange
async def test_exchange_checks_and_caches_the_passport(tower):
    cred = PassportCredential(lambda: "minted.jwt.token", TOKEN_URL, verifier=verifier(tower), client=client(tower))
    first = await cred.token()
    assert await cred.token() == first and len(tower.exchanges) == 1  # cached until 60 s before it expires
    form = tower.exchanges[0]
    assert (form["grant_type"], form["subject_token"], form["subject_token_type"]) == (
        TOKEN_EXCHANGE, "minted.jwt.token", JWT_TYPE)
    await cred.token(fresh=True)  # after a 401
    assert len(tower.exchanges) == 2 and tower.jwks_reads == 1
    await cred.close()


async def test_a_passport_for_another_agent_or_a_refused_exchange_is_no_credential(tower):
    tower.agent = "scout"
    cred = PassportCredential(lambda: "m", TOKEN_URL, verifier=verifier(tower), client=client(tower))
    with pytest.raises(CredentialUnavailable, match="names agent 'scout'"):
        await cred.token()
    tower.agent, tower.status = "hangar", 400
    with pytest.raises(CredentialUnavailable, match="HTTP 400 invalid_grant"):
        await cred.token()

    def broken() -> str:
        raise CloudError("no issuer")

    with pytest.raises(CredentialUnavailable, match="couldn't mint"):
        await PassportCredential(broken, TOKEN_URL, client=client(tower)).token()


async def test_decisions_carry_the_passport_and_fail_closed_without_one(tower):
    cred = PassportCredential(lambda: "m", TOKEN_URL, verifier=verifier(tower), client=client(tower))
    dp = HttpDecisionPoint(ORIGIN, cred, client=client(tower))
    req = DecisionRequest(kind="job.release", idempotency_key="hangar:job:j-1:release")
    d = await dp.decide(req)
    assert d.outcome == "wait" and tower.bearers == [f"Bearer {await cred.token()}"]
    tower.status = 401
    dp2 = HttpDecisionPoint(ORIGIN, PassportCredential(lambda: "m", TOKEN_URL, client=client(tower)),
                            client=client(tower))
    d2 = await dp2.decide(req)
    assert (d2.outcome, d2.source, d2.failure) == ("wait", "fail_closed", "auth")  # never an allow


def test_the_receiver_builds_a_passport_credential(fleet, settings):
    class Sessions:
        pass

    s = Sessions()
    s.fleet, s.settings, s.store = fleet, settings, fleet.store
    settings.governance_url, settings.governance_auth = ORIGIN, "passport"
    dp = Receiver(s).tower()
    assert isinstance(dp, HttpDecisionPoint) and isinstance(dp.credential, PassportCredential)
    assert dp.credential.token_url == TOKEN_URL
    assert passport_credential(fleet, ORIGIN, settings).verifier.jwks_url == f"{ORIGIN}/.well-known/jwks.json"


# ====================================================================== verification
async def test_verification_refuses_what_tower_didnt_sign_for_hangar(tower):
    v = verifier(tower)
    good = tower.passport()
    assert (await v.verify(tower.sign(good)))["agent"] == "hangar"
    cases = {
        "bad signature": tower.sign(good, key=Ed25519PrivateKey.generate()),
        "doesn't publish": tower.sign(good, kid="other"),
        "not EdDSA": tower.sign(good, alg="RS256"),
        "another audience": tower.sign({**good, "aud": "https://elsewhere.test"}),
        "another issuer": tower.sign({**good, "iss": "https://evil.test"}),
        "expired": tower.sign({**good, "exp": int(time.time()) - 120}),
        "act claim doesn't match": tower.sign({**good, "act": {"sub": "agent:ledger"}}),
        "last link": tower.sign({**good, "agent": "ledger"}),
    }
    for why, token in cases.items():
        with pytest.raises(PassportRefused, match=why):
            await v.verify(token)
    with pytest.raises(PassportRefused, match="not a JWT"):
        await v.verify("nope")


def test_act_chains_as_tower_builds_them(tower):
    # Scout's own passport: no act. Scout delegating to Ledger: act names Ledger.
    one = tower.passport(chain=["scout"])
    assert act_chain(one) == [] and check_chain(one) == ["scout"]
    two = tower.passport(chain=["scout", "ledger"])
    assert two["act"] == {"sub": "agent:ledger"} and check_chain(two) == ["scout", "ledger"]
    # a person as subject (Tower#14's requester_claim): every agent acts, the current one outermost
    person = tower.passport(sub="person:dev@acme.test", chain=["hangar", "reviewer"])
    assert person["act"] == {"sub": "agent:reviewer", "act": {"sub": "agent:hangar"}}
    assert act_chain(person) == ["hangar", "reviewer"] and check_chain(person) == ["hangar", "reviewer"]
    with pytest.raises(PassportRefused, match="longer than"):
        check_chain(tower.passport(chain=["a", "b", "c", "d"]))
    with pytest.raises(PassportRefused, match="malformed"):
        act_chain({"act": {"sub": "person:x"}})
