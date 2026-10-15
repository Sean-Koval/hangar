"""--- heavy: hangar's read-only Docker Hub pull-through on the gateway (regmirror.py): the sandbox's credential in the
mirror path, reads only, Docker Hub's token dance and CDN redirect, an Artifact Registry upstream read with hangar's own
Google credentials, and use in the egress audit."""

from __future__ import annotations

import base64

import httpx
import pytest

from hangar import egress, regmirror
from hangar.models import Sandbox, SandboxState

GW = "http://gw.test:8788"
DIGEST = "sha256:" + "a" * 64
MANIFEST = b'{"schemaVersion": 2}'


class Hub:
    """Docker Hub (or Artifact Registry) as a MockTransport, with its bearer-token challenge."""

    def __init__(self, registry: str = "registry-1.docker.io", realm: str = "https://auth.docker.io/token"):
        self.registry, self.realm = registry, realm
        self.calls: list[httpx.Request] = []
        self.blob_to = "https://production.cloudflare.docker.com/registry-v2/blob"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url.startswith(self.realm):
            return httpx.Response(200, json={"token": "pull-tok", "expires_in": 300})
        if request.url.host == self.registry:
            if request.headers.get("authorization") != "Bearer pull-tok":
                return httpx.Response(401, headers={"WWW-Authenticate": f'Bearer realm="{self.realm}",'
                                                                        'service="registry.docker.io"'})
            if "/manifests/" in url:
                return httpx.Response(200, content=MANIFEST, headers={
                    "content-type": "application/vnd.oci.image.index.v1+json", "docker-content-digest": DIGEST})
            if "/blobs/" in url:
                return httpx.Response(307, headers={"location": self.blob_to})
            if url.endswith("/tags/list"):
                return httpx.Response(200, json={"tags": ["3"]})
        if url.startswith("https://production.cloudflare.docker.com/"):
            return httpx.Response(200, content=b"LAYER" * 1000)
        return httpx.Response(404)


@pytest.fixture
def world(fleet, settings, store):
    settings.gateway_url = GW
    settings.heavy_mirror = True
    hub = Hub()
    fleet.gateway.transport = httpx.MockTransport(hub)
    fleet.gateway._client = None

    def sandbox(sid: str, docker: bool = True, state=SandboxState.RUNNING) -> str:
        sb = Sandbox(id=sid, name=sid, environment="plain", provider="kubernetes", state=state, person="ann")
        if docker:
            sb.info["heavy"] = {"docker": True, "vm": True}
        store.put(sb)
        return f"/registry/{sid}/{egress.token(fleet.egress.key, sid)}/v2/"

    class W:
        pass

    w = W()
    w.fleet, w.hub, w.sandbox, w.store, w.settings = fleet, hub, sandbox, store, settings
    w.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=fleet.gateway.asgi_app()), base_url=GW)
    return w


async def test_dockerd_pulls_an_image_through_the_mirror(world):
    w = world
    base = w.sandbox("sb-heavy1")
    async with w.client() as c:
        r = await c.get(base)
        assert r.status_code == 200 and r.headers["docker-distribution-api-version"] == "registry/2.0"
        r = await c.get(base + "library/alpine/manifests/3",
                        headers={"Accept": "application/vnd.oci.image.index.v1+json"})
        assert r.status_code == 200 and r.content == MANIFEST and r.headers["docker-content-digest"] == DIGEST
        r = await c.head(base + "library/alpine/manifests/3")
        assert r.status_code == 200 and r.headers["docker-content-digest"] == DIGEST
        r = await c.get(base + f"library/alpine/blobs/{DIGEST}")
        assert r.status_code == 200 and r.content == b"LAYER" * 1000
        assert (await c.get(base + "library/alpine/tags/list")).json() == {"tags": ["3"]}
    tokens = [x for x in w.hub.calls if str(x.url).startswith(w.hub.realm)]
    assert len(tokens) == 1 and "scope=repository%3Alibrary%2Falpine%3Apull" in str(tokens[0].url)
    assert "authorization" not in tokens[0].headers, "anonymous pull tokens from Docker Hub"
    cdn = [x for x in w.hub.calls if x.url.host == "production.cloudflare.docker.com"]
    assert cdn and "authorization" not in cdn[0].headers, "the pull token stays with the registry"
    manifest = next(x for x in w.hub.calls if "/manifests/" in str(x.url) and x.headers.get("authorization"))
    assert manifest.headers["accept"] == "application/vnd.oci.image.index.v1+json"
    w.fleet.egress.flush()
    allow = [e for e in w.store.audit_tail(50) if e.action == "egress.allow"]
    assert allow and allow[0].detail["host"] == regmirror.HOST and allow[0].detail["bytes_in"] >= 5000


async def test_only_reads_and_only_for_a_running_heavy_sandbox(world):
    w = world
    base = w.sandbox("sb-heavy2")
    plain = w.sandbox("sb-plain2", docker=False)
    gone = w.sandbox("sb-gone2", state=SandboxState.STOPPED)
    async with w.client() as c:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            r = await c.request(method, base + "library/alpine/blobs/uploads/")
            assert r.status_code == 405 and r.headers["allow"] == "GET, HEAD"
        assert (await c.get(plain + "library/alpine/manifests/3")).status_code == 401
        assert (await c.get(gone + "library/alpine/manifests/3")).status_code == 401
        forged = f"/registry/sb-heavy2/{'0' * 64}/v2/library/alpine/manifests/3"
        assert (await c.get(forged)).status_code == 401
        for bad in ("library/Alpine/manifests/3", "library/alpine/blobs/latest", "library/alpine/manifests/a b",
                    "library/%2e%2e/x/manifests/3", "library/alpine/referrers/" + DIGEST, "_catalog"):
            assert (await c.get(base + bad)).status_code == 404, bad
    assert not [x for x in w.hub.calls if x.method != "GET"], "nothing but reads ever goes upstream"
    w.fleet.egress.flush()
    deny = [e for e in w.store.audit_tail(50) if e.action == "egress.deny"]
    assert deny and deny[0].detail["reason"] == "read_only"


async def test_redirects_only_to_the_upstreams_storage(world):
    w = world
    base = w.sandbox("sb-heavy3")
    w.hub.blob_to = "https://attacker.example/steal"
    async with w.client() as c:
        r = await c.get(base + f"library/alpine/blobs/{DIGEST}")
    assert r.status_code == 502
    assert not [x for x in w.hub.calls if x.url.host == "attacker.example"]


async def test_an_artifact_registry_upstream_is_read_with_hangars_google_token(world, monkeypatch):
    w = world
    monkeypatch.setenv("HANGAR_REGISTRY_TOKEN", "google-access-token")
    hub = Hub(registry="us-central1-docker.pkg.dev", realm="https://us-central1-docker.pkg.dev/v2/token")
    w.fleet.gateway.transport = httpx.MockTransport(hub)
    w.fleet.gateway._client = None
    w.settings.heavy_mirror_upstream = "https://us-central1-docker.pkg.dev/acme-proj/dockerhub"
    base = w.sandbox("sb-heavy4")
    async with w.client() as c:
        r = await c.get(base + "library/postgres/manifests/17")
    assert r.status_code == 200
    asked = [str(x.url) for x in hub.calls if x.url.host == "us-central1-docker.pkg.dev" and "/token" not in str(x.url)]
    assert asked[0] == "https://us-central1-docker.pkg.dev/v2/acme-proj/dockerhub/library/postgres/manifests/17"
    token = next(x for x in hub.calls if str(x.url).startswith(hub.realm))
    assert token.headers["authorization"] == "Basic " + base64.b64encode(
        b"oauth2accesstoken:google-access-token").decode()
    assert "scope=repository%3Aacme-proj%2Fdockerhub%2Flibrary%2Fpostgres%3Apull" in str(token.url)


async def test_off_by_default(fleet, settings, store):
    settings.gateway_url = GW
    sb = Sandbox(id="sb-off1", name="x", environment="plain", provider="kubernetes", state=SandboxState.RUNNING)
    sb.info["heavy"] = {"docker": True}
    store.put(sb)
    app = fleet.gateway.asgi_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=GW) as c:
        r = await c.get(f"/registry/sb-off1/{egress.token(fleet.egress.key, 'sb-off1')}/v2/")
    assert r.status_code == 404


def test_paths_and_upstreams():
    assert regmirror.parse("library/alpine/manifests/3.20") == ("library/alpine", "manifests", "3.20")
    assert regmirror.parse(f"acme/app/blobs/{DIGEST}") == ("acme/app", "blobs", DIGEST)
    assert regmirror.parse("a/b/c/tags/list") == ("a/b/c", "tags", "list")
    for bad in ("library/alpine/manifests/", "UPPER/x/manifests/1", "x//y/manifests/1", "x/blobs/sha256:zz"):
        assert regmirror.parse(bad) is None, bad
    from hangar.config import Settings

    assert regmirror.upstream(Settings()) == ("https://registry-1.docker.io", "")
    assert regmirror.upstream(Settings(heavy_mirror_upstream="https://us-docker.pkg.dev/p/r/")) == \
        ("https://us-docker.pkg.dev", "p/r")
    with pytest.raises(ValueError):
        regmirror.upstream(Settings(heavy_mirror_upstream="http://plain.example"))
