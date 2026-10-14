"""Git gateway units: pkt-line parsing, push policy, grants, the proxy against a stub upstream, GitHub App tokens."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hangar.broker import CredentialError
from hangar.gateway import (
    FLUSH,
    AppTokens,
    Commands,
    GatewayError,
    ProtocolError,
    app_jwt,
    check_push,
    mint_installation_token,
    parse_app_credential,
    parse_commands,
    parse_remote,
    parse_report,
    pkt,
    ref_allowed,
    rejection,
    valid_ref,
)
from hangar.models import CredentialKind, Sandbox, SandboxGrant, SandboxState, now

OLD = "a" * 40
NEW = "b" * 40
ZERO = "0" * 40
PAT = "ghp_stubUPSTREAMsecret0987654321"


def cmd_body(*refs: tuple[str, str, str], caps: str = "report-status side-band-64k", pack: bytes = b"PACKxxxx"):
    out = b""
    for i, (old, new, ref) in enumerate(refs):
        line = f"{old} {new} {ref}".encode()
        out += pkt(line + (b"\0" + caps.encode() if i == 0 else b"") + b"\n")
    return out + FLUSH + pack


# ============================================================================ parsing
def test_parse_commands():
    body = cmd_body((OLD, NEW, "refs/heads/x"), (ZERO, NEW, "refs/heads/y"))
    cmds, end = parse_commands(body)
    assert [c.ref for c in cmds.commands] == ["refs/heads/x", "refs/heads/y"]
    assert cmds.capabilities == ["report-status", "side-band-64k"]
    assert body[end:] == b"PACKxxxx"
    for cut in range(end):  # every prefix of the command list asks for more bytes
        assert parse_commands(body[:cut]) is None
    # a bare flush is git's probe before a chunked upload
    assert parse_commands(b"0000") == (Commands([], []), 4)
    # shallow lines come first
    cmds, _ = parse_commands(pkt(f"shallow {OLD}".encode()) + cmd_body((OLD, NEW, "refs/heads/x")))
    assert len(cmds.commands) == 1
    sha256 = "c" * 64
    assert parse_commands(cmd_body((sha256, "d" * 64, "refs/heads/x")))[0].commands[0].old == sha256


@pytest.mark.parametrize("body,why", [
    (b"zzzz", "malformed pkt-line"),
    (b"0001", "special pkt-line"),
    (pkt(b"nonsense\n") + FLUSH, "malformed ref update"),
    (pkt(f"{OLD} {NEW}".encode()) + FLUSH, "malformed ref update"),
    (pkt(f"{OLD} {'b' * 64} refs/heads/x".encode()) + FLUSH, "malformed ref update"),
    (pkt(b"push-cert\0report-status\n") + FLUSH, "signed pushes"),
    (cmd_body((OLD, NEW, "refs/heads/x"))[:-12] + pkt(f"shallow {OLD}".encode()) + FLUSH, "shallow"),
    (pkt(f"{OLD} {NEW} refs/heads/x\0report-status\n".encode())
     + pkt(f"{OLD} {NEW} refs/heads/y\0quiet\n".encode()) + FLUSH, "malformed ref update"),
    (cmd_body(*[(OLD, NEW, f"refs/heads/b{i}") for i in range(9)]), "at most 8"),
    (pkt(f"shallow {OLD}".encode()) * 1400, "too long"),
    (pkt(f"{OLD} {NEW} refs/heads/\xff".encode("latin-1")) + FLUSH, "non-ASCII"),
])
def test_parse_commands_rejects(body, why):
    with pytest.raises(ProtocolError, match=why):
        parse_commands(body)


def test_refs_and_remotes():
    assert valid_ref("refs/heads/hangar/job-1")
    for bad in ("refs/heads/a..b", "refs/heads/a//b", "refs/heads/x.lock", "refs/heads/.x", "heads/x",
                "refs/heads/x/", "refs/heads/a b", "refs/heads/a@{1}", "refs/heads/a~1", "refs/heads/a:b"):
        assert not valid_ref(bad), bad
    pats = ["refs/heads/job", "refs/heads/hangar/**"]
    assert ref_allowed(pats, "refs/heads/job") and ref_allowed(pats, "refs/heads/hangar/x/y")
    assert not ref_allowed(pats, "refs/heads/hangar/") and not ref_allowed(pats, "refs/heads/hangarx")
    assert not ref_allowed(pats, "refs/heads/job2") and not ref_allowed(pats, "refs/heads/main")
    hosts = ["github.com"]
    assert parse_remote("https://github.com/Acme/Widgets.git", hosts) == ("github.com", "acme/widgets")
    assert parse_remote("git@github.com:acme/widgets.git", hosts) == ("github.com", "acme/widgets")
    assert parse_remote("ssh://git@github.com/acme/widgets", hosts) == ("github.com", "acme/widgets")
    assert parse_remote("https://gitlab.com/acme/widgets.git", hosts) is None
    assert parse_remote("https://github.com/acme/../x", hosts) is None


# ============================================================================ policy
def _grant(**kw) -> SandboxGrant:
    return SandboxGrant(sandbox_id="sb-1", token_hash="x", repos=["acme/widgets"], credential="pat",
                        push_refs=kw.pop("push_refs", ["refs/heads/hangar/job-1", "refs/heads/hangar/**"]), **kw)


@pytest.mark.parametrize("old,new,ref,caps,expect", [
    (ZERO, NEW, "refs/heads/hangar/job-1", "", None),
    (OLD, NEW, "refs/heads/hangar/job-1", "", None),
    (ZERO, NEW, "refs/heads/hangar/other", "", None),
    (OLD, NEW, "refs/heads/hangar/other", "", "create new branches there, not update existing ones"),
    (OLD, NEW, "refs/heads/main", "", "may push only to hangar/job-1 or branches under hangar/"),
    (OLD, NEW, "refs/pull/1/head", "", "may push only"),
    (OLD, ZERO, "refs/heads/hangar/job-1", "", "deleting refs is not allowed"),
    (ZERO, NEW, "refs/tags/v1", "", "pushing tags is not allowed"),
    (OLD, NEW, "refs/heads/hangar/job-1", "push-options", "push options"),
    (OLD, NEW, "refs/heads/hangar/a..b", "", "invalid ref"),
    (OLD, NEW, "refs/heads/hangar/theirs", "", "belongs to another sandbox"),
])
def test_check_push(old, new, ref, caps, expect):
    from hangar.gateway import Command

    bad = check_push(_grant(), Commands([Command(old, new, ref)], caps.split()), False, {"refs/heads/hangar/theirs"})
    if expect is None:
        assert bad == {}
    else:
        assert expect in bad[ref] and bad[ref].startswith("hangar: ")


def test_tags_when_allowed():
    from hangar.gateway import Command

    g = _grant()
    assert check_push(g, Commands([Command(ZERO, NEW, "refs/tags/v1")], []), True, set()) == {}
    assert "not moved" in check_push(g, Commands([Command(OLD, NEW, "refs/tags/v1")], []), True, set())["refs/tags/v1"]


def test_rejection_is_a_valid_report():
    from hangar.gateway import Command

    cmds = Commands([Command(OLD, NEW, "refs/heads/main"), Command(OLD, NEW, "refs/heads/hangar/job-1")], [])
    bad = {"refs/heads/main": "hangar: nope"}
    plain = rejection(cmds, bad)
    assert plain == (pkt(b"unpack ok\n") + pkt(b"ng refs/heads/main hangar: nope\n")
                     + pkt(b"ng refs/heads/hangar/job-1 hangar: not pushed, another ref in this push was rejected\n")
                     + FLUSH)
    side = rejection(cmds._replace(capabilities=["side-band-64k"]), bad)
    assert side.endswith(FLUSH) and b"\x02hangar: nope" in side
    assert parse_report(side, sideband=True)[:2] == ["unpack ok", "ng refs/heads/main hangar: nope"]
    assert parse_report(plain, sideband=False)[0] == "unpack ok"


# ============================================================================ the proxy (stub upstream)
@pytest.fixture
def gwfleet(fleet, settings, broker):
    settings.gateway_url = "http://gw.test:8788"
    broker.put("pat", "git", CredentialKind.GIT_TOKEN, PAT)
    return fleet


def _running(store, sb_id="sb-1", state=SandboxState.RUNNING, expires=None):
    sb = Sandbox(id=sb_id, name=sb_id, environment="e", provider="fake", state=state, expires_at=expires)
    store.put(sb)
    return sb


def _client(fleet):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=fleet.gateway.asgi_app()), base_url="http://gw.test")


def _basic(token: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()}


async def _stream(body: bytes):
    """A streamed body, as a network transport gives (in-memory bodies are pre-read and can't be relayed raw)."""
    yield body


class Stub:
    def __init__(self, status=200, body=b"001e# service=git-upload-pack\n0000", ctype=None):
        self.requests: list[tuple[httpx.Request, bytes]] = []
        self.status, self.body = status, body
        self.ctype = ctype or "application/x-git-upload-pack-advertisement"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request, await request.aread()))
        return httpx.Response(self.status, content=_stream(self.body), headers={"content-type": self.ctype})


def _stub(fleet, **kw) -> Stub:
    stub = Stub(**kw)
    fleet.gateway.transport = httpx.MockTransport(stub)
    fleet.gateway._client = None
    return stub


INFO = "/git/github.com/acme/widgets.git/info/refs?service=git-upload-pack"


async def test_auth_and_repo_checks(gwfleet, store):
    stub = _stub(gwfleet)
    sb = _running(store)
    token = gwfleet.gateway.mint(sb, "github.com", ["acme/widgets"], ["refs/heads/hangar/job-1"], "pat")
    async with _client(gwfleet) as c:
        r = await c.get(INFO)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert (await c.get(INFO, headers=_basic("hgr_" + token[4:]))).status_code == 401  # hangar API tokens: never
        assert (await c.get(INFO, headers=_basic("hgs_nope"))).status_code == 401
        r = await c.get(INFO, headers=_basic(token))
        assert r.status_code == 200 and r.content.startswith(b"001e# service=")
        assert (await c.get(INFO, headers={"Authorization": f"Bearer {token}"})).status_code == 200
        # The upstream got the PAT, never the grant; nothing echoes either back.
        req, _ = stub.requests[0]
        assert str(req.url) == "https://github.com/acme/widgets.git/info/refs?service=git-upload-pack"
        assert req.headers["authorization"] == _basic(PAT)["Authorization"]
        assert token not in str(req.headers) and PAT not in r.text
        # other repos, other hosts, traversal, dumb protocol
        for path in ("/git/github.com/acme/other.git/info/refs?service=git-upload-pack",
                     "/git/gitlab.com/acme/widgets.git/info/refs?service=git-upload-pack",
                     "/git/github.com/acme/../widgets.git/info/refs?service=git-upload-pack",
                     "/git/github.com/acme/%2e%2e/info/refs?service=git-upload-pack",
                     "/git/github.com//acme/widgets.git/info/refs?service=git-upload-pack",
                     "/git/github.com/acme/widgets.git/HEAD",
                     "/git/github.com/acme/widgets.git/objects/info/packs"):
            assert (await c.get(path, headers=_basic(token))).status_code == 404, path
        r = await c.get("/git/github.com/acme/widgets.git/info/refs", headers=_basic(token))
        assert r.status_code == 403
        assert (await c.get("/git/github.com/ACME/Widgets/info/refs?service=git-upload-pack",
                            headers=_basic(token))).status_code == 200
    assert len(stub.requests) == 3
    assert any(e.action == "gw.git.deny" and e.detail["reason"] == "repo not granted" for e in store.audit_tail(50))


async def test_grant_lifecycle(gwfleet, store):
    _stub(gwfleet)
    gw = gwfleet.gateway
    sb = _running(store, expires=now() + 60)
    token = gw.mint(sb, "github.com", ["acme/widgets"], [], "pat")
    assert gw.verify(token) is not None
    assert store.list(SandboxGrant)[0].token_hash != token  # only the hash is stored
    sb.expires_at = now() + 3600
    gw.extend(sb)
    assert store.list(SandboxGrant)[0].expires_at == sb.expires_at
    sb.state = SandboxState.STOPPED  # the sandbox died without a revoke: still refused
    store.put(sb)
    assert gw.verify(token) is None
    sb.state = SandboxState.RUNNING
    store.put(sb)
    g = store.list(SandboxGrant)[0]
    g.expires_at = now() - 1
    store.put(g)
    assert gw.verify(token) is None and store.list(SandboxGrant) == []  # expired grants are dropped
    token = gw.mint(sb, "github.com", ["acme/widgets"], [], "pat")
    gw.revoke(sb.id)
    assert gw.verify(token) is None


async def test_receive_pack_policy_and_limits(gwfleet, store, settings):
    stub = _stub(gwfleet, body=pkt(b"\x01" + pkt(b"unpack ok\n") + pkt(b"ok refs/heads/hangar/job-1\n") + FLUSH)
                 + FLUSH, ctype="application/x-git-receive-pack-result")
    sb = _running(store)
    token = gwfleet.gateway.mint(sb, "github.com", ["acme/widgets"], ["refs/heads/hangar/job-1"], "pat")
    url = "/git/github.com/acme/widgets.git/git-receive-pack"
    hdrs = {**_basic(token), "Content-Type": "application/x-git-receive-pack-request"}
    async with _client(gwfleet) as c:
        body = cmd_body((OLD, NEW, "refs/heads/hangar/job-1"), pack=b"PACK" + b"\0" * 5000)
        r = await c.post(url, content=body, headers=hdrs)
        assert r.status_code == 200
        req, sent = stub.requests[-1]
        assert sent == body  # forwarded byte for byte
        assert req.headers["authorization"] == _basic(PAT)["Authorization"]
        push = next(e for e in store.audit_tail(10) if e.action == "gw.git.push")
        assert push.detail["result"] == "ok" and push.detail["refs"][0]["ref"] == "refs/heads/hangar/job-1"

        # git's probe (a bare flush before a chunked upload) goes through, and only the flush does
        r = await c.post(url, content=b"0000" + b"junk", headers=hdrs)
        assert r.status_code == 200 and stub.requests[-1][1] == b"0000"

        n = len(stub.requests)
        r = await c.post(url, content=cmd_body((OLD, NEW, "refs/heads/main")), headers=hdrs)
        assert r.status_code == 200 and r.headers["content-type"] == "application/x-git-receive-pack-result"
        assert "ng refs/heads/main hangar: this sandbox may push only to hangar/job-1" in parse_report(r.content, True)
        r = await c.post(url, content=b"0032" + b"x" * 10, headers=hdrs)  # truncated
        assert r.status_code == 400
        r = await c.post(url, content=body, headers={**hdrs, "Content-Encoding": "gzip"})
        assert r.status_code == 415
        settings.gateway_max_push_mb = 1
        r = await c.post(url, content=cmd_body((OLD, NEW, "refs/heads/hangar/job-1"), pack=b"P" * (2 << 20)),
                         headers=hdrs)
        assert r.status_code == 413 and "limit" in r.text
        assert len(stub.requests) == n  # nothing above reached the upstream

        async def chunked():  # no Content-Length: the cap is enforced while streaming
            yield cmd_body((OLD, NEW, "refs/heads/hangar/job-1"), pack=b"")
            for _ in range(40):
                yield b"P" * 65536

        r = await c.post(url, content=chunked(), headers=hdrs)
        assert r.status_code == 502
    deny = [e for e in store.audit_tail(50) if e.action == "gw.git.deny"]
    assert any("refs/heads/main" in json.dumps(e.detail) for e in deny)
    assert PAT not in json.dumps([e.model_dump() for e in store.audit_tail(50)])


async def test_upstream_refusal_is_not_a_401(gwfleet, store):
    _stub(gwfleet, status=401, body=b"bad credentials")
    sb = _running(store)
    token = gwfleet.gateway.mint(sb, "github.com", ["acme/widgets"], [], "pat")
    async with _client(gwfleet) as c:
        r = await c.get(INFO, headers=_basic(token))
    assert r.status_code == 502 and "refused" in r.text  # a 401 would make git erase its credential and prompt


# ============================================================================ GitHub App
@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    return key, pem


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_app_jwt(rsa_key):
    key, pem = rsa_key
    t = 1_700_000_000
    jwt = app_jwt("12345", pem, at=t)
    h, c, s = jwt.split(".")
    assert json.loads(_unb64(h)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_unb64(c)) == {"iat": t - 60, "exp": t + 540, "iss": "12345"}
    key.public_key().verify(_unb64(s), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_app_credential_validation(rsa_key):
    _, pem = rsa_key
    assert parse_app_credential(json.dumps({"app_id": 7, "private_key": pem})).app_id == "7"
    with pytest.raises(GatewayError) as e:
        parse_app_credential(json.dumps({"app_id": 7, "private_key": "-----BEGIN nope-----secretbits"}))
    assert "secretbits" not in str(e.value)
    with pytest.raises(GatewayError):
        parse_app_credential("not json")


class FakeGitHub:
    def __init__(self, key, ttl=3600):
        self.key, self.ttl, self.calls = key, ttl, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        h, c, s = request.headers["authorization"].removeprefix("Bearer ").split(".")
        self.key.public_key().verify(_unb64(s), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
        assert request.headers["accept"] == "application/vnd.github+json"
        if request.url.path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 42})
        if request.url.path == "/app/installations/42/access_tokens" and request.method == "POST":
            exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + self.ttl))
            return httpx.Response(201, json={"token": f"ghs_minted{len(self.calls)}", "expires_at": exp})
        return httpx.Response(404, json={"message": "Not Found"})


async def test_installation_token_and_cache(rsa_key):
    key, pem = rsa_key
    gh = FakeGitHub(key)
    client = httpx.AsyncClient(transport=httpx.MockTransport(gh))
    value = json.dumps({"app_id": "99", "private_key": pem})
    tokens = AppTokens("https://api.github.test", lambda: client)
    assert await tokens.token(value, "acme/widgets") == "ghs_minted2"  # lookup, then mint
    body = json.loads(gh.calls[1].content)
    # --- landing: a push needs contents only; the App's PR identity asks for pull_requests on its own token
    assert body == {"permissions": {"contents": "write"}, "repositories": ["widgets"]}
    assert await tokens.token(value, "acme/widgets") == "ghs_minted2"  # cached
    assert len(gh.calls) == 2
    gh.ttl = 60  # a token close to expiry is replaced on next use
    tokens._cache.clear()
    await tokens.token(value, "acme/widgets")
    n = len(gh.calls)
    await tokens.token(value, "acme/widgets")
    assert len(gh.calls) == n + 2
    with_id = json.dumps({"app_id": "99", "installation_id": 42, "private_key": pem})
    tok, _ = await mint_installation_token(client, "https://api.github.test", parse_app_credential(with_id),
                                           "acme/widgets", {"contents": "write"})
    assert tok.startswith("ghs_") and gh.calls[-1].url.path == "/app/installations/42/access_tokens"
    with pytest.raises(GatewayError, match="not installed on acme/nope"):
        await mint_installation_token(client, "https://api.github.test", parse_app_credential(value), "acme/nope", {})
    await client.aclose()


async def test_gateway_uses_app_token_upstream(gwfleet, store, broker, rsa_key):
    key, pem = rsa_key
    broker.put("app", "git", CredentialKind.GITHUB_APP, json.dumps({"app_id": "99", "private_key": pem}))
    gh = FakeGitHub(key)
    seen = []

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return gh(request)
        seen.append(request.headers["authorization"])
        return httpx.Response(200, content=_stream(b"001e# service=git-upload-pack\n0000"))

    gwfleet.gateway.transport = httpx.MockTransport(route)
    gwfleet.gateway._client = None
    sb = _running(store)
    token = gwfleet.gateway.mint(sb, "github.com", ["acme/widgets"], [], "app")
    async with _client(gwfleet) as c:
        assert (await c.get(INFO, headers=_basic(token))).status_code == 200
        assert (await c.get(INFO, headers=_basic(token))).status_code == 200
    assert seen == [_basic("ghs_minted2")["Authorization"]] * 2  # minted once, cached


def test_broker_keeps_app_keys_out_of_sandboxes(broker, rsa_key):
    _, pem = rsa_key
    with pytest.raises(CredentialError, match="JSON"):
        broker.put("app", "git", CredentialKind.GITHUB_APP, "nope")
    with pytest.raises(CredentialError, match="can't be mapped"):
        broker.put("app", "git", CredentialKind.GITHUB_APP, json.dumps({"app_id": 1, "private_key": pem}),
                   env_var="X")
    c = broker.put("app", "git", CredentialKind.GITHUB_APP, json.dumps({"app_id": 1, "private_key": pem}))
    assert c.env_var is None and c.file_path is None
    body = pem.splitlines()[1]
    escaped = json.dumps(pem)[1:-1]
    assert body not in broker.redact(f"log {escaped} end") and "***" in broker.redact(f"log {escaped} end")
    assert body not in broker.redact(f"raw {pem} end")


async def test_inject_skips_app_credentials(broker, rsa_key):
    from tests.conftest import FakeProvider

    _, pem = rsa_key
    prov = FakeProvider()
    sb = Sandbox(name="t", environment="e", provider="fake")
    await prov.create(sb, "img")
    broker.put("app", "git", CredentialKind.GITHUB_APP, json.dumps({"app_id": 1, "private_key": pem}))
    await broker.inject(sb, prov, broker.acquire(["app"], sb.id))
    assert b"PRIVATE KEY" not in (await prov.read_file(sb, "/home/agent/.hangar/env") or b"")


# ============================================================================ doctor
async def test_doctor_checks(gwfleet, broker, settings, rsa_key, tmp_path):
    key, pem = rsa_key
    broker.put("app", "git", CredentialKind.GITHUB_APP,
               json.dumps({"app_id": "99", "installation_id": 42, "private_key": pem}))
    gh = FakeGitHub(key)

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gw.test":
            return httpx.Response(200, json={"ok": True, "service": "hangar-gateway"})
        return gh(request)

    checks = {c["name"]: c for c in await gwfleet.gateway.doctor(httpx.MockTransport(route))}
    assert checks["git gateway"]["status"] == "ok"
    assert checks["github app app"]["status"] == "ok"
    assert json.loads(gh.calls[-1].content) == {"permissions": {"metadata": "read"}}
    settings.gateway_url = None
    gwfleet.catalog.add({"name": "needs-gw", "credential_mode": "gateway"}, save_dir=tmp_path)
    checks = {c["name"]: c for c in await gwfleet.gateway.doctor(httpx.MockTransport(route))}
    assert checks["git gateway"]["status"] == "fail" and "needs-gw" in checks["git gateway"]["detail"]


# ============================================================================ wiring into the server
def test_gateway_listener_and_shared_port(settings):
    import socket

    from fastapi.testclient import TestClient

    from hangar.app import create_app

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    settings.admin_token = "t0ken"
    settings.gateway_url = f"http://127.0.0.1:{port}"
    settings.gateway_port = port
    with TestClient(create_app(settings)) as c:
        assert c.get(INFO).status_code == 404  # grant routes are not on the main port
        deadline = time.time() + 10
        while True:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/healthz")
                break
            except httpx.ConnectError:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)
        assert r.json() == {"ok": True, "service": "hangar-gateway"}
        assert httpx.get(f"http://127.0.0.1:{port}/api/overview",
                         headers={"Authorization": "Bearer t0ken"}).status_code == 404  # no admin routes
        assert httpx.get(f"http://127.0.0.1:{port}{INFO}").status_code == 401
    settings.gateway_port = 0
    with TestClient(create_app(settings)) as c:
        assert c.get(INFO).status_code == 401
