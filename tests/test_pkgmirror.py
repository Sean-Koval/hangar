"""Read-only package mirrors on the gateway (--- egress2): who may read which ecosystem, index pages rewritten to the
mirror, the disk cache, nothing but GET and HEAD, no publish path, Go's no-fetch rule, Artifact Registry upstreams,
and the read-only GitHub API through the gateway."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from hangar import egress, ghapi
from hangar.models import AuditEvent, CredentialKind, Sandbox, SandboxState

GW = "http://gw.test:8788"
PAT = "ghp_stubUPSTREAMsecret0987654321"


class Upstream:
    """The registries and GitHub, as a MockTransport: answers by URL, records what it was asked."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.routes: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return httpx.Response(resp.status_code, headers=resp.headers, content=resp.content)
        return httpx.Response(404, text="not here")


@pytest.fixture
def world(fleet, settings, store, broker, tmp_path):
    settings.gateway_url = GW
    up = Upstream()
    fleet.gateway.transport = httpx.MockTransport(up)
    fleet.gateway._client = None
    for name, eg in (("locked", {"mode": "allowlist"}), ("npm-only", {"mode": "allowlist", "presets": ["npm"]}),
                     ("shut", {"mode": "none"})):
        fleet.catalog.add({"name": name, "harnesses": ["git"], "egress": eg}, save_dir=tmp_path)

    def sandbox(env: str, sid: str, state=SandboxState.RUNNING) -> tuple[Sandbox, dict[str, str]]:
        sb = Sandbox(id=sid, name=sid, environment=env, provider="docker", state=state, person="ann")
        store.put(sb)
        cred = base64.b64encode(f"{sid}:{egress.token(fleet.egress.key, sid)}".encode()).decode()
        return sb, {"Authorization": f"Basic {cred}"}

    class W:
        pass

    w = W()
    w.fleet, w.up, w.sandbox, w.store, w.settings, w.broker = fleet, up, sandbox, store, settings, broker
    w.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=fleet.gateway.asgi_app()), base_url=GW)
    return w


SIMPLE = (b'<html><body><a href="https://files.pythonhosted.org/packages/aa/bb/demo-1.0-py3-none-any.whl#sha256=00">'
          b'demo-1.0-py3-none-any.whl</a></body></html>')


async def test_pip_reads_the_index_and_files_through_the_mirror_and_files_are_cached(world):
    w = world
    w.up.routes["https://pypi.org/simple/demo/"] = httpx.Response(200, content=SIMPLE,
                                                                   headers={"content-type": "text/html"})
    w.up.routes["https://files.pythonhosted.org/packages/aa/bb/"] = httpx.Response(200, content=b"WHEEL")
    sb, auth = w.sandbox("locked", "sb-pip1")
    async with w.client() as c:
        r = await c.get("/pkg/pypi/simple/demo/", headers={**auth, "Cookie": "a=b"})
        assert r.status_code == 200
        assert f"{GW}/pkg/pypi/files/packages/aa/bb/demo-1.0-py3-none-any.whl#sha256=00".encode() in r.content
        assert b"files.pythonhosted.org" not in r.content
        for _ in range(2):
            r = await c.get("/pkg/pypi/files/packages/aa/bb/demo-1.0-py3-none-any.whl", headers=auth)
            assert r.status_code == 200 and r.content == b"WHEEL"
    files = [x for x in w.up.calls if x.url.host == "files.pythonhosted.org"]
    assert len(files) == 1  # the second came from the cache
    for req in w.up.calls:  # nothing of the client's reaches the registry
        assert "authorization" not in req.headers and "cookie" not in req.headers
        assert req.headers["user-agent"] == "hangar-pkgmirror"
    w.fleet.egress.flush()
    allow = [e for e in w.store.audit_tail(50) if e.action == "egress.allow"]
    assert allow and allow[0].detail["host"] == "pypi.mirror.hangar" and allow[0].detail["connections"] == 3


async def test_nothing_but_reads_and_no_way_to_publish(world):
    w = world
    sb, auth = w.sandbox("locked", "sb-npm1")
    async with w.client() as c:
        for method in ("PUT", "POST", "DELETE", "PATCH"):
            r = await c.request(method, "/pkg/npm/left-pad", headers=auth, content=b"{}")
            assert r.status_code == 405 and "read-only" in r.text
        assert (await c.get("/pkg/npm/../../etc/passwd", headers=auth)).status_code in (400, 404)
        assert (await c.get("/pkg/npm/a/%2e%2e/b", headers=auth)).status_code == 400
        assert (await c.get("/pkg/rubygems/x", headers=auth)).status_code == 404
    assert not w.up.calls
    deny = [e for e in w.store.audit_tail(50) if e.action == "egress.deny"]
    assert deny and deny[0].detail["reason"] == "read_only" and deny[0].detail["person"] == "ann"


async def test_only_sandboxes_whose_policy_has_the_preset(world):
    w = world
    _, npm_only = w.sandbox("npm-only", "sb-npmonly")
    _, shut = w.sandbox("shut", "sb-shut1")
    _, stopped = w.sandbox("locked", "sb-stopped", SandboxState.STOPPED)
    w.up.routes["https://registry.npmjs.org/"] = httpx.Response(200, json={})
    async with w.client() as c:
        assert (await c.get("/pkg/pypi/simple/x/", headers=npm_only)).status_code == 403
        assert (await c.get("/pkg/npm/x", headers=npm_only)).status_code == 200
        assert (await c.get("/pkg/npm/x", headers=shut)).status_code == 403
        assert (await c.get("/pkg/npm/x", headers=stopped)).status_code == 401
        assert (await c.get("/pkg/npm/x")).status_code == 401
        bad = {"Authorization": "Basic " + base64.b64encode(b"sb-npmonly:" + b"0" * 64).decode()}
        assert (await c.get("/pkg/npm/x", headers=bad)).status_code == 401


async def test_a_sandbox_from_before_a_key_rotation_still_reads_the_mirror_until_the_key_is_retired(world):
    """--- integ3 (#107 x #109): the mirror checks a sandbox's credential against every vault key not yet retired, as
    the egress proxy does, so a proxied sandbox started before `rotate-key` keeps its package installs."""
    from cryptography.fernet import Fernet

    from hangar import keys

    w = world
    ring = keys.ring(w.settings)
    old = ring.primary
    _, auth = w.sandbox("npm-only", "sb-rot1")  # its credential is derived from the old key
    ring.set_keys([Fernet.generate_key(), old])  # a new key put first: the old one still reads
    w.up.routes["https://registry.npmjs.org/"] = httpx.Response(200, json={})
    async with w.client() as c:
        assert (await c.get("/pkg/npm/x", headers=auth)).status_code == 200
        ring.retire(keys.key_id(old))
        assert (await c.get("/pkg/npm/x", headers=auth)).status_code == 401


async def test_npm_metadata_from_artifact_registry_is_rewritten_and_read_with_hangars_google_token(world, monkeypatch):
    w = world
    monkeypatch.setenv("HANGAR_REGISTRY_TOKEN", "ya29.test-access-token")
    w.settings.package_mirrors = {"npm": "https://us-npm.pkg.dev/proj/npm-remote", "pip": "x", "go": "http://x"}
    base = "https://us-npm.pkg.dev/proj/npm-remote"
    doc = {"name": "demo", "versions": {"1.0.0": {"dist": {"tarball": f"{base}/demo/-/demo-1.0.0.tgz"}}}}
    w.up.routes[f"{base}/demo"] = httpx.Response(200, json=doc, headers={"content-type": "application/json"})
    _, auth = w.sandbox("locked", "sb-ar01")
    async with w.client() as c:
        r = await c.get("/pkg/npm/demo", headers={**auth, "Accept": "application/vnd.npm.install-v1+json"})
    assert r.status_code == 200
    assert r.json()["versions"]["1.0.0"]["dist"]["tarball"] == f"{GW}/pkg/npm/demo/-/demo-1.0.0.tgz"
    req = w.up.calls[-1]
    assert req.headers["authorization"] == "Bearer ya29.test-access-token"
    assert req.headers["accept"] == "application/vnd.npm.install-v1+json"


async def test_go_reads_only_what_the_module_proxy_has_cached(world):
    w = world
    w.up.routes["https://proxy.golang.org/golang.org/x/mod/@v/v0.20.0.info"] = httpx.Response(
        200, json={"Version": "v0.20.0"})
    w.up.routes["https://sum.golang.org/lookup/golang.org/x/mod@v0.20.0"] = httpx.Response(200, text="123\nmod h1:x")
    _, auth = w.sandbox("locked", "sb-go01")
    async with w.client() as c:
        assert (await c.get("/pkg/go/sumdb/sum.golang.org/supported", headers=auth)).status_code == 200
        r = await c.get("/pkg/go/golang.org/x/mod/@v/v0.20.0.info", headers=auth)
        assert r.status_code == 200 and r.json()["Version"] == "v0.20.0"
        r = await c.get("/pkg/go/sumdb/sum.golang.org/lookup/golang.org/x/mod@v0.20.0", headers=auth)
        assert r.status_code == 200 and r.text.startswith("123")
        # a module the proxy hasn't seen: no lookup reaches the checksum database (it would fetch the origin)
        r = await c.get("/pkg/go/sumdb/sum.golang.org/lookup/attacker.example/s3cr3t@v1.0.0", headers=auth)
        assert r.status_code == 404
    proxy_calls = [x for x in w.up.calls if x.url.host == "proxy.golang.org"]
    assert proxy_calls and all(x.headers["disable-module-fetch"] == "true" for x in proxy_calls)
    assert not any("attacker.example" in str(x.url) for x in w.up.calls if x.url.host == "sum.golang.org")
    assert any(e.action == "egress.deny" and e.detail["reason"] == "not_cached" for e in w.store.audit_tail(20))


async def test_redirects_stay_on_the_registrys_own_hosts(world):
    w = world
    w.up.routes["https://pypi.org/simple/Demo/"] = httpx.Response(301, headers={"location": "/simple/demo/"})
    w.up.routes["https://pypi.org/simple/demo/"] = httpx.Response(200, content=b"ok",
                                                                   headers={"content-type": "text/html"})
    w.up.routes["https://pypi.org/simple/evil/"] = httpx.Response(302, headers={"location": "https://evil.example/"})
    _, auth = w.sandbox("locked", "sb-red1")
    async with w.client() as c:
        assert (await c.get("/pkg/pypi/simple/Demo/", headers=auth)).content == b"ok"
        assert (await c.get("/pkg/pypi/simple/evil/", headers=auth)).status_code == 502
    assert not any(x.url.host == "evil.example" for x in w.up.calls)


async def test_the_cache_keeps_to_its_size(world):
    w = world
    w.settings.package_mirror_cache_mb = 1
    m = w.fleet.gateway.mirror
    for i in range(12):
        p = m._cached("npm", f"x/-/x-{i}.tgz")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * 100_000)
    m._evict(1024 * 1024)
    left = sum(f.stat().st_size for f in m.cache_dir.rglob("*") if f.is_file())
    assert left <= 0.9 * 1024 * 1024 and m._cache_size == left


async def test_metadata_held_for_rewriting_is_bounded_across_sandboxes(world, monkeypatch):
    """--- review (#107): an index page or packument is held whole to rewrite its URLs, up to 64 MB each, and a
    sandbox may have 64 requests in flight: a few sandboxes asking for big packuments at once would hold gigabytes
    on the control plane. Past the shared budget the mirror answers 503 with Retry-After, which pip and npm retry."""
    from hangar import pkgmirror

    w = world
    monkeypatch.setattr(pkgmirror, "METADATA_HELD", 1000)
    w.up.routes["https://registry.npmjs.org/big"] = httpx.Response(200, content=b'{"x": "' + b"y" * 2000 + b'"}',
                                                                    headers={"content-type": "application/json"})
    w.up.routes["https://registry.npmjs.org/small"] = httpx.Response(200, json={"name": "small"},
                                                                      headers={"content-type": "application/json"})
    _, auth = w.sandbox("locked", "sb-big1")
    async with w.client() as c:
        r = await c.get("/pkg/npm/big", headers=auth)
        assert r.status_code == 503 and r.headers["retry-after"]
        assert (await c.get("/pkg/npm/small", headers=auth)).status_code == 200  # what it held is given back
    assert w.fleet.gateway.mirror._held == 0


# ============================================================================ the read-only GitHub API
@pytest.fixture
def gh(world, broker):
    broker.put("pat", "git", CredentialKind.GIT_TOKEN, PAT)
    sb = Sandbox(id="sb-gh01", name="g", environment="locked", provider="docker", state=SandboxState.RUNNING)
    world.store.put(sb)
    world.grant = world.fleet.gateway.mint(sb, "github.com", ["acme/widgets"], ["refs/heads/hangar/x"], "pat")
    world.gh_auth = {"Authorization": "Basic " + base64.b64encode(f"x-access-token:{world.grant}".encode()).decode()}
    return world


async def test_the_github_api_is_readable_for_the_grants_repo_only(gh):
    w = gh
    w.up.routes["https://api.github.com/repos/acme/widgets/pulls"] = httpx.Response(
        200, json=[{"number": 1}], headers={"link": '<https://api.github.com/repos/acme/widgets/pulls?page=2>; '
                                                    'rel="next"', "set-cookie": "x=y"})
    async with w.client() as c:
        r = await c.get("/gh/api/repos/acme/widgets/pulls", headers=w.gh_auth)
        assert r.status_code == 200 and r.json() == [{"number": 1}] and "set-cookie" not in r.headers
        assert r.headers["link"] == f'<{GW}/gh/api/repos/acme/widgets/pulls?page=2>; rel="next"'
        assert w.up.calls[-1].headers["authorization"] == f"Bearer {PAT}"
        assert (await c.get("/gh/api/repos/acme/other/pulls", headers=w.gh_auth)).status_code == 404
        assert (await c.get("/gh/api/user", headers=w.gh_auth)).status_code == 404
        for escaped in ("/gh/api/repos/acme/widgets/../other/pulls", "/gh/api/repos/acme/widgets/%2e%2e/other/pulls",
                        "/gh/api/repos/acme/widgets/x%2f..%2f..%2fother/pulls", "/gh/api/repos/acme/widgets//x"):
            assert (await c.get(escaped, headers=w.gh_auth)).status_code == 404, escaped
        r = await c.post("/gh/api/repos/acme/widgets/issues", headers=w.gh_auth, json={"title": "x"})
        assert r.status_code == 405
        assert (await c.get("/gh/api/repos/acme/widgets/pulls")).status_code == 401
    assert [x.url.path for x in w.up.calls] == ["/repos/acme/widgets/pulls"]
    assert PAT not in json.dumps([e.model_dump() for e in w.store.audit_tail(50) if isinstance(e, AuditEvent)])


async def test_github_redirects_are_followed_to_its_content_hosts_without_the_token(gh):
    w = gh
    logs = "https://productionresultssa1.blob.core.windows.net/actions-results/x?sig=abc"
    w.up.routes["https://api.github.com/repos/acme/widgets/actions/jobs/1/logs"] = httpx.Response(
        302, headers={"location": logs})
    w.up.routes["https://productionresultssa1.blob.core.windows.net/"] = httpx.Response(200, text="log lines")
    w.up.routes["https://api.github.com/repos/acme/widgets/actions/jobs/2/logs"] = httpx.Response(
        302, headers={"location": "https://evil.example/x"})
    async with w.client() as c:
        r = await c.get("/gh/api/repos/acme/widgets/actions/jobs/1/logs", headers=w.gh_auth)
        assert r.status_code == 200 and r.text == "log lines"
        assert "authorization" not in w.up.calls[-1].headers
        assert (await c.get("/gh/api/repos/acme/widgets/actions/jobs/2/logs", headers=w.gh_auth)).status_code == 502


def test_a_gateway_sandbox_learns_where_the_api_is_and_netrc_keeps_other_lines():
    from hangar.config import Settings

    s = Settings(gateway_url="http://hangar-gateway.hangar.svc:8788")
    sb = Sandbox(name="x", environment="e", provider="docker", info={"git": {"mode": "gateway", "repo": "a/b"}})
    assert ghapi.sandbox_env(s, sb) == {"HANGAR_GITHUB_API": "http://hangar-gateway.hangar.svc:8788/gh/api"}
    assert ghapi.sandbox_env(s, sb.model_copy(update={"info": {"git": {"mode": "direct"}}})) == {}
    cur = "machine example.com login a password b\nmachine hangar-gateway.hangar.svc login x password old\n"
    out = ghapi.netrc(cur, "hangar-gateway.hangar.svc", "hgs_new")
    assert out.splitlines() == ["machine example.com login a password b",
                                "machine hangar-gateway.hangar.svc login x-access-token password hgs_new"]
