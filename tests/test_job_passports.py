"""Tower passports on hangar's job API (KAN-33, hangar#174's consumer side; spec 0015 §9).

A caller holding a passport Tower issued is authenticated on /api/jobs* as the person (`sub: person:<email>`, linked
by email) or listed agent in it, after the signature, issuer, audience, expiry and act chain check out against
Tower's JWKS. Keys are generated here and the JWKS is served by a mock transport: no network."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from hangar import people
from hangar.auth import PassportAuth, PassportRejected, looks_like_passport, parse_agents
from hangar.governance import PassportVerifier
from hangar.jobs import Caller, SubmitJob
from tests.test_passports import ORIGIN, Tower, b64d, b64e

HANGAR = "http://localhost:8787"
AUDS = [f"{ORIGIN}/mcp", ORIGIN]  # Tower mints passports for its own audiences only (aw/workload.py)
ADMIN_H = {"Authorization": "Bearer t0ken"}


def passport(tower: Tower, sub: str = "person:alice@example.com", chain: list[str] | None = None, **over) -> str:
    return tower.sign(tower.passport(sub=sub, chain=chain or ["claude-code"], **{"aud": f"{ORIGIN}/mcp", **over}))


@pytest.fixture
def tower():
    return Tower()


@pytest.fixture
def world(settings, tower):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.public_url = HANGAR
    settings.governance_url = ORIGIN
    settings.job_passports = "on"
    settings.job_passport_agents = "ci-bot=bob"
    app = create_app(settings)
    with TestClient(app, base_url=HANGAR) as client:
        h = app.state.hangar
        h.jobs._queue = asyncio.Queue()
        for pid in ("alice", "bob"):
            people.add(h.store, pid, role="member")
        people.link(h.store, "alice", "email", "Alice@Example.com")
        pa = h.passports()
        pa.verifier._client = httpx.AsyncClient(transport=httpx.MockTransport(tower))
        yield {"client": client, "h": h}


def bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# ====================================================================== accepted
def test_a_person_passport_is_that_person_with_the_jobs_scope_only(world, tower):
    c, h = world["client"], world["h"]
    tok = passport(tower)
    mine = h.jobs.submit(SubmitJob(instructions="mine", environment="plain", deliver="none"),
                         Caller("passport:claude-code", "passport:alice", person="alice"), source="api")
    other = h.jobs.submit(SubmitJob(instructions="theirs", environment="plain", deliver="none"),
                          Caller("svc", "tok-other"), source="api")
    r = c.get("/api/jobs", headers=bearer(tok))
    assert r.status_code == 200, r.text
    ids = {j["id"] for j in r.json()}
    assert mine.id in ids and other.id not in ids  # a restricted caller: the jobs it submitted, nothing else
    assert c.get(f"/api/jobs/{other.id}", headers=bearer(tok)).status_code == 404
    # authenticated, and held to a service token's rules (no repo mapped for it here)
    r = c.post("/api/jobs", headers=bearer(tok), json={"instructions": "fix it", "environment": "plain"})
    assert r.status_code == 400 and "HANGAR_GITHUB_REPOS" in r.text
    assert tower.jwks_reads == 1  # the key is cached


async def test_the_principal_names_the_person_and_the_chain(store, tower):
    people.add(store, "alice", role="admin")
    people.link(store, "alice", "email", "alice@example.com")
    pa = PassportAuth(store, PassportVerifier(ORIGIN, AUDS, client=httpx.AsyncClient(
        transport=httpx.MockTransport(tower))))
    p = await pa.principal(passport(tower, chain=["claude-code", "hangar"]))
    assert (p.person, p.role, p.id, p.scopes) == ("alice", "admin", "passport:alice", ["jobs"])
    assert p.name == "passport:claude-code>hangar" and not p.admin  # an admin's passport is still jobs-only


def test_a_listed_agent_acts_as_its_person(world, tower):
    tok = passport(tower, sub="agent:ci-bot", chain=["ci-bot"])
    assert world["client"].get("/api/jobs", headers=bearer(tok)).status_code == 200


def test_hangar_tokens_work_as_before(world):
    c, h = world["client"], world["h"]
    assert c.get("/api/jobs", headers=ADMIN_H).status_code == 200
    svc, _ = h.auth.create("svc", ["jobs"])
    assert c.get("/api/jobs", headers=bearer(svc)).status_code == 200
    assert c.get("/api/jobs", headers=bearer("hgr_nope")).status_code == 401


# ====================================================================== refused
def _tampered(tower: Tower) -> str:
    head, body, sig = passport(tower).split(".")
    claims = b64d(body).replace(b"alice@example.com", b"bob@example.com!")
    return f"{head}.{b64e(claims)}.{sig}"


CASES = {
    "tampered": (_tampered, "bad signature"),
    "expired": (lambda t: passport(t, exp=int(time.time()) - 3600), "expired"),
    "wrong audience": (lambda t: passport(t, aud=HANGAR), "another audience"),
    "another tower's audience": (lambda t: passport(t, aud="https://other-tower.test/mcp"), "another audience"),
    "unknown issuer": (lambda t: passport(t, iss="https://evil.test"), "another issuer"),
    "unknown key": (lambda t: t.sign(t.passport(sub="person:alice@example.com", aud=f"{ORIGIN}/mcp"),
                                     key=Ed25519PrivateKey.generate(), kid="x9"), "key Tower doesn't publish"),
    "not EdDSA": (lambda t: t.sign(t.passport(sub="person:alice@example.com", aud=ORIGIN), alg="HS256"), "EdDSA"),
    "bad act chain": (lambda t: passport(t, act={"sub": "agent:someone-else"}), "act claim"),
    "unlinked email": (lambda t: passport(t, sub="person:mallory@example.com"), "isn't linked"),
    "unlisted agent": (lambda t: passport(t, sub="agent:rogue", chain=["rogue"]), "HANGAR_JOB_PASSPORT_AGENTS"),
    "odd subject": (lambda t: passport(t, sub="hangar:control-plane"), "person:<email> or agent:<id>"),
}


@pytest.mark.parametrize("case", list(CASES))
def test_refusals(world, tower, case):
    make, why = CASES[case]
    r = world["client"].get("/api/jobs", headers=bearer(make(tower)))
    assert r.status_code == 401 and why in r.json()["detail"], r.text
    assert "invalid_token" in r.headers["www-authenticate"]


async def test_refusals_are_audited_without_the_token(store, tower):
    seen = []
    pa = PassportAuth(store, PassportVerifier(ORIGIN, AUDS, client=httpx.AsyncClient(
        transport=httpx.MockTransport(tower))), audit=lambda *a, **kw: seen.append((a, kw)))
    tok = passport(tower, sub="person:nobody@example.com")
    with pytest.raises(PassportRejected):
        await pa.principal(tok)
    [(args, kw)] = seen
    assert args == ("tower", "passport.refused", "person:nobody@example.com") and tok not in kw["why"]


def test_a_passport_never_approves(world, tower):
    h = world["h"]
    job = h.jobs.submit(SubmitJob(instructions="x", environment="plain", deliver="none"),
                        Caller("svc", "tok-other"), source="api")
    r = world["client"].post(f"/api/jobs/{job.id}/approve", headers=bearer(passport(tower)))
    assert r.status_code == 403 and "can't approve" in r.text


def test_passports_are_off_by_default(settings, tower):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.governance_url = ORIGIN
    app = create_app(settings)
    with TestClient(app, base_url=HANGAR) as client:
        assert app.state.hangar.passports() is None
        r = client.get("/api/jobs", headers=bearer(passport(tower)))
        assert r.status_code == 401 and r.json()["detail"] == "unauthorized" and tower.jwks_reads == 0


def test_on_without_an_issuer_accepts_nothing(settings, tower):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.job_passports = "on"
    app = create_app(settings)
    with TestClient(app, base_url=HANGAR) as client:
        with pytest.raises(ValueError, match="Tower's https URL"):
            app.state.hangar.passports()
        assert client.get("/api/jobs", headers=bearer(passport(tower))).status_code == 401
        assert client.get("/api/jobs", headers=ADMIN_H).status_code == 200


def test_helpers():
    assert looks_like_passport("a.b.c") and not looks_like_passport("hgr_abc") and not looks_like_passport(None)
    assert not looks_like_passport("a.b") and not looks_like_passport("hgr_a.b.c")
    assert parse_agents(" ci-bot = bob , x=y") == {"ci-bot": "bob", "x": "y"} and parse_agents("") == {}
    with pytest.raises(ValueError):
        parse_agents("lonely")


def test_towers_own_audiences_by_default_or_the_override(world, tower, settings):
    c, h = world["client"], world["h"]
    assert h.passports().verifier.audiences == AUDS
    assert c.get("/api/jobs", headers=bearer(passport(tower, aud=ORIGIN))).status_code == 200
    assert c.get("/api/jobs", headers=bearer(passport(tower, aud=[f"{ORIGIN}/mcp", "x"]))).status_code == 200
    settings.job_passport_audience = f"{ORIGIN}/hooks, {ORIGIN}/jobs/"
    h._passports = None
    assert h.passports().verifier.audiences == [f"{ORIGIN}/hooks", f"{ORIGIN}/jobs"]
