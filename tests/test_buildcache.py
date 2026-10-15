"""--- caches: dependency and compile caches (buildcache.py, cachestore.py, docs/caches.md).

The store: encrypted content-addressed chunks, integrity checks, restore-keys lookups, LRU caps, retention, the sweep,
build secrets kept out. The protocols on the gateway: sccache (WebDAV), Bazel's HTTP cache and the Turborepo API, with
the poisoning rules: a repo's own builds write its shared layer, everyone else reads it and writes a private layer of
their own, no repo reads another's. And the sandbox's side, end to end on the fake provider: `hangar-cache` restores in
the background (the agent is told the caches are warming), saves, and the repo build's hooks."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest

from hangar import buildcache, cachestore, envbuilds, gateway, opstatus, supervisor
from hangar.cachestore import CacheError, CacheStore, Index
from hangar.checkpoints import LocalStore
from hangar.models import CredentialKind, Sandbox, SandboxState, Session

from .conftest import free_port

GW = "http://gw.test:8788"
REPO_URL = "https://github.com/acme/app.git"
T = cachestore.shared("plain")  # the shared layer of builds in the `plain` environment
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]


async def _bytes(it) -> bytes:
    out = bytearray()
    async for p in it:
        out += p
    return bytes(out)


async def _stream(*pieces: bytes):
    for p in pieces:
        yield p


# ====================================================================== the store
@pytest.fixture
def cs(settings, broker, tmp_path, monkeypatch):
    monkeypatch.setattr(cachestore, "CHUNK", 1024)
    st = CacheStore(LocalStore(tmp_path / "blobs"), Index(tmp_path / "caches.db"), broker.vault, settings)
    yield st
    st.index.close()


async def _put(st: CacheStore, repo: str, layer: str, ns: str, key: str, data: bytes, **kw) -> cachestore.Entry:
    d, n, chunks, sizes = await st.ingest(repo, _stream(data[:700], data[700:]), 1 << 30, **kw)
    return st.commit(repo, layer, ns, key, d, n, chunks, sizes, writer="t")


async def test_objects_are_encrypted_chunks_named_by_content_and_shared_within_a_repo_only(cs, tmp_path):
    data = os.urandom(2500)
    e = await _put(cs, "acme/app", T, "deps/npm", "any/aa11bb22", data)
    assert e.size == 2500 and len(e.chunks) == 3 and e.digest == hashlib.sha256(data).hexdigest()
    files = sorted((tmp_path / "blobs").rglob("*.blk"))
    assert len(files) == 3
    assert all(data[:64] not in f.read_bytes() and data[1024:1088] not in f.read_bytes() for f in files)
    await _put(cs, "acme/app", T, "deps/npm", "any/cc33dd44", data)  # the same bytes: no new objects
    assert len(list((tmp_path / "blobs").rglob("*.blk"))) == 3
    await _put(cs, "acme/other", T, "deps/npm", "any/aa11bb22", data)  # another repo: its own objects
    assert len(list((tmp_path / "blobs").rglob("*.blk"))) == 6
    got = cs.index.lookup("acme/app", [T], "deps/npm", "any/aa11bb22")
    first = await cs.first(got)
    assert await _bytes(cs.stream(got, first)) == data


async def test_a_damaged_or_missing_chunk_is_a_miss_and_the_entry_goes(cs, tmp_path):
    data = os.urandom(3000)
    e = await _put(cs, "acme/app", T, "sccache", "a/b/c", data)
    victim = tmp_path / "blobs" / cachestore.object_key("acme/app", e.chunks[2])
    victim.write_bytes(cs.vault.encrypt(b"x" * 1024))  # a valid token, but not the content its name says
    first = await cs.first(e)
    with pytest.raises(CacheError):
        await _bytes(cs.stream(e, first))
    assert cs.index.get("acme/app", T, "sccache", "a/b/c") is None
    assert cs.stats["integrity_failures"] == 1
    e2 = await _put(cs, "acme/app", T, "sccache", "d", b"tiny")
    (tmp_path / "blobs" / cachestore.object_key("acme/app", e2.chunks[0])).write_bytes(b"not a fernet token")
    assert await cs.first(e2) is None and cs.index.get("acme/app", T, "sccache", "d") is None


async def test_lookups_take_the_exact_key_then_each_restore_prefix_newest_first_layers_in_order(cs):
    await _put(cs, "r", T, "deps/uv", "any/0001aaaa", b"old")
    await _put(cs, "r", T, "deps/uv", "any/0002bbbb", b"new")
    await _put(cs, "r", "private:s1:-", "deps/uv", "any/0003cccc", b"mine")
    look = cs.index.lookup
    assert look("r", ["private:s1:-", T], "deps/uv", "any/0001aaaa").key == "any/0001aaaa"
    assert look("r", ["private:s1:-", T], "deps/uv", "any/ffffffff", ["any/"]).key == "any/0003cccc"
    assert look("r", [T], "deps/uv", "any/ffffffff", ["any/"]).key == "any/0002bbbb"
    assert look("r", [T], "deps/uv", "py3.12/ffffffff", ["py3.12/"]) is None
    assert look("r", [T], "deps/uv", "any/ffffffff", ["an_/"]) is None  # LIKE's wildcards are literal


async def test_caps_evict_the_least_recently_used_per_repo_and_in_total(cs, settings):
    settings.build_cache_max_gb = 1e6
    for i in range(8):
        e = await _put(cs, "big", T, "turbo", f"{i:04x}", os.urandom(1000))
        cs.index._db.execute("UPDATE entries SET used=? WHERE key=?", (1000 + i, e.key))
    cs.index._db.execute("UPDATE entries SET used=? WHERE key=?", (5000, "0000"))  # used lately: stays
    await cs.drain()
    settings.build_cache_repo_max_gb = 5000 / (1 << 30)
    await _put(cs, "small", T, "turbo", "ffff", os.urandom(1000))  # a write starts the eviction
    await cs.drain()
    assert cs.index.stored("big") <= 4500
    left = {e.key for e in cs.index.where("repo='big'")}
    assert "0000" in left and "0001" not in left and "0007" in left
    assert cs.index.stored("small") == 1000
    settings.build_cache_max_gb = 2500 / (1 << 30)
    await cs.evict()
    await cs.drain()
    assert cs.index.stored() <= 2250
    assert not list(cs.blobs.root.rglob("*.part"))
    assert len(list(cs.blobs.root.rglob("*.blk"))) == len(cs.index.chunk_names())


async def test_a_sessions_private_writes_never_push_the_shared_caches_out(cs, settings):
    settings.build_cache_max_gb = 1e6
    shared = await _put(cs, "r", T, "turbo", "0000", os.urandom(1000))
    cs.index._db.execute("UPDATE entries SET used=1 WHERE key='0000'")  # long unused, yet it stays
    for i in range(1, 6):
        await _put(cs, "r", "private:s:-", "turbo", f"{i:04x}", os.urandom(1000))
    await cs.drain()
    settings.build_cache_repo_max_gb = 3000 / (1 << 30)
    await cs.evict()
    await cs.drain()
    assert cs.index.get("r", T, "turbo", shared.key) is not None
    assert cs.index.stored("r") <= 2700
    assert settings.build_cache_max_gb and cachestore.total_cap_bytes(settings) > 0
    settings.build_cache_max_gb = None
    assert cachestore.total_cap_bytes(settings) == 10 << 30  # on the data volume
    settings.session_store = "gs://bucket/sessions"
    assert cachestore.total_cap_bytes(settings) == 100 << 30
    assert cachestore.location(settings) == "gs://bucket/sessions/caches"


async def test_retention_drops_unused_entries_private_ones_sooner_and_the_sweep_removes_strays(cs, settings):
    t = time.time()
    keep = await _put(cs, "r", T, "turbo", "aa", b"a" * 10)
    old_private = await _put(cs, "r", "private:s:-", "turbo", "bb", b"b" * 10)
    old = await _put(cs, "r", T, "turbo", "cc", b"c" * 10)
    cs.index._db.execute("UPDATE entries SET used=? WHERE key=?", (t - 4 * 86400, old_private.key))
    cs.index._db.execute("UPDATE entries SET used=? WHERE key=?", (t - 15 * 86400, old.key))
    assert await cs.expire(t) == 2
    await cs.drain()
    assert [e.key for e in cs.index.where("1=1")] == [keep.key]
    stray = cs.blobs.root / cachestore.object_key("r", "f" * 64)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"left by an upload a restart cut short")
    os.utime(stray, (t - 7200, t - 7200))
    assert await cs.sweep() == 1 and not stray.exists()
    assert await cs.read_all(keep, 100) == b"a" * 10


async def test_a_build_secret_in_an_upload_is_refused_even_gzipped(cs):
    secret = b"npm_SECRETtoken0123456789"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        body = b"x" * 5000 + b"//registry/:_authToken=" + secret + b"\n"
        info = tarfile.TarInfo(".npm/_cacache/index")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    data = buf.getvalue()
    assert secret not in data  # only inflating it finds the value
    with pytest.raises(CacheError) as ei:
        await cs.ingest("r", _stream(data[:100], data[100:]), 1 << 30, needles=[secret])
    assert ei.value.status == 422
    await cs.drain()
    assert not list(cs.blobs.root.rglob("*.blk"))
    d, *_ = await cs.ingest("r", _stream(gzip.compress(b"clean" * 1000)), 1 << 30, needles=[secret])
    assert d


async def test_an_object_over_the_cap_or_not_matching_its_digest_is_refused(cs):
    with pytest.raises(CacheError) as ei:
        await cs.ingest("r", _stream(b"x" * 3000), 2000)
    assert ei.value.status == 413
    with pytest.raises(CacheError) as ei:
        await cs.ingest("r", _stream(b"abc"), 2000, expect="0" * 64)
    assert ei.value.status == 400
    await cs.drain()
    assert not list(cs.blobs.root.rglob("*.blk"))


# ====================================================================== the protocols and who may do what
class World:
    pass


def _sandbox(w, sid: str, *, labels=None, created_by="ann", person="ann", branch=None, repo="acme/app",
             host="github.com", grant=True, env="plain"):
    sb = Sandbox(id=sid, name=sid, environment=env, provider="docker", state=SandboxState.RUNNING,
                 labels=labels or {}, created_by=created_by, person=person, branch=branch,
                 repo=f"https://{host}/{repo}.git")
    w.store.put(sb)
    tok = w.fleet.gateway.mint(sb, host, [repo], [], "pat") if grant else None
    return sb, tok


def _bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def _running_build(w, repo="acme/app", env="plain") -> envbuilds.Build:
    b = envbuilds.Build(repo=repo, repo_url=f"https://github.com/{repo}.git", environment=env,
                        provider="docker", state="running")
    envbuilds.of(w.fleet)._put(b)
    return b


async def test_a_build_in_one_environment_never_writes_what_another_environments_sandboxes_read(pw):
    """Members make environments, and an environment's image and `setup` run in its builds before `install`: a
    member's own environment's build writes a shared layer only its own environment's sandboxes read."""
    w = pw
    b = _running_build(w, env="mine")
    _, evil = _sandbox(w, "sb-evil", labels={envbuilds.LABEL: b.id}, created_by=f"hangar:build:{b.id}", env="mine")
    _, other = _sandbox(w, "sb-other", person="bob")  # `plain`
    _, same = _sandbox(w, "sb-same", person="bob", env="mine")
    _, moved = _sandbox(w, "sb-moved", labels={envbuilds.LABEL: b.id}, created_by=f"hangar:build:{b.id}")  # `plain`
    async with w.client() as c:
        assert (await c.put(f"/cache/bazel/ac/{'a' * 64}", content=b"mine", headers=_bearer(evil))).status_code == 200
        assert (await c.get(f"/cache/bazel/ac/{'a' * 64}", headers=_bearer(other))).status_code == 404
        assert (await c.get(f"/cache/bazel/ac/{'a' * 64}", headers=_bearer(same))).content == b"mine"
        await c.put(f"/cache/bazel/ac/{'b' * 64}", content=b"x", headers=_bearer(moved))  # not its build's env
        assert (await c.get(f"/cache/bazel/ac/{'b' * 64}", headers=_bearer(other))).status_code == 404


@pytest.fixture
def pw(fleet, settings, store, broker, tmp_path):
    settings.gateway_url = GW
    broker.put("pat", "git", CredentialKind.GIT_TOKEN, "ghp_stubUPSTREAMsecret0987654321")
    w = World()
    w.fleet, w.store, w.settings = fleet, store, settings
    w.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=fleet.gateway.asgi_app()), base_url=GW)
    w.cache = buildcache.of(fleet)
    yield w
    if w.cache._store is not None:
        w.cache._store.index.close()


def _build_sandbox(w, sid="sb-build", repo="acme/app"):
    b = _running_build(w, repo)
    return _sandbox(w, sid, labels={envbuilds.LABEL: b.id}, created_by=f"hangar:build:{b.id}", repo=repo)


async def test_bazel_the_repos_build_writes_the_shared_cache_sessions_read_it_and_write_only_their_own(pw):
    w = pw
    _, build = _build_sandbox(w)
    _, ann = _sandbox(w, "sb-ann", labels={"hangar.session": "nope"})
    _, bob = _sandbox(w, "sb-bob", person="bob")
    _, other_repo = _sandbox(w, "sb-other", repo="acme/other")
    blob = b"compiled output " * 100
    cas = hashlib.sha256(blob).hexdigest()
    ac = "a" * 64
    async with w.client() as c:
        assert (await c.put(f"/cache/bazel/cas/{cas}", content=blob, headers=_bearer(build))).status_code == 200
        assert (await c.put(f"/cache/bazel/ac/{ac}", content=b"action result", headers=_bearer(build))
                ).status_code == 200
        r = await c.get(f"/cache/bazel/cas/{cas}", headers=_bearer(ann))
        assert r.status_code == 200 and r.content == blob
        assert (await c.head(f"/cache/bazel/ac/{ac}", headers=_bearer(bob))).status_code == 200
        # no repo reads another's
        assert (await c.get(f"/cache/bazel/cas/{cas}", headers=_bearer(other_repo))).status_code == 404
        # a session's write goes to its own layer: it reads it back, nobody else does, the shared one is unchanged
        assert (await c.put(f"/cache/bazel/ac/{ac}", content=b"poisoned", headers=_bearer(ann))).status_code == 200
        assert (await c.get(f"/cache/bazel/ac/{ac}", headers=_bearer(ann))).content == b"poisoned"
        assert (await c.get(f"/cache/bazel/ac/{ac}", headers=_bearer(bob))).content == b"action result"
        assert (await c.get(f"/cache/bazel/ac/{ac}", headers=_bearer(build))).content == b"action result"
        # the CAS is content-addressed: a body must hash to its name
        assert (await c.put(f"/cache/bazel/cas/{'b' * 64}", content=b"lies", headers=_bearer(build))
                ).status_code == 400
        assert (await c.get("/cache/bazel/cas/notahash", headers=_bearer(ann))).status_code == 404


async def test_a_build_label_proves_nothing_and_with_private_writes_off_sessions_only_read(pw):
    w = pw
    b = _running_build(w)
    _, fake_build = _sandbox(w, "sb-fake", labels={envbuilds.LABEL: b.id})  # created by a person, not hangar
    _, real = _sandbox(w, "sb-real", labels={envbuilds.LABEL: b.id}, created_by=f"hangar:build:{b.id}")
    _, done_build = _build_sandbox(w, "sb-done")
    b2 = envbuilds.of(w.fleet).get(w.store.get(Sandbox, "sb-done").labels[envbuilds.LABEL])
    b2.state = "succeeded"
    envbuilds.of(w.fleet)._put(b2)
    _, reader = _sandbox(w, "sb-reader", person="bob")
    async with w.client() as c:
        await c.put(f"/cache/bazel/ac/{'c' * 64}", content=b"spoof", headers=_bearer(fake_build))
        await c.put(f"/cache/bazel/ac/{'d' * 64}", content=b"late", headers=_bearer(done_build))
        assert (await c.get(f"/cache/bazel/ac/{'c' * 64}", headers=_bearer(reader))).status_code == 404
        assert (await c.get(f"/cache/bazel/ac/{'d' * 64}", headers=_bearer(reader))).status_code == 404
        await c.put(f"/cache/bazel/ac/{'e' * 64}", content=b"real", headers=_bearer(real))
        assert (await c.get(f"/cache/bazel/ac/{'e' * 64}", headers=_bearer(reader))).content == b"real"
        w.settings.build_cache_private_writes = False
        w.cache.store  # noqa: B018
        server = w.fleet.gateway.__dict__["_cache_server"]
        server._seen.clear()
        r = await c.put(f"/cache/bazel/ac/{'f' * 64}", content=b"x", headers=_bearer(reader))
        assert r.status_code == 403 and "read-only" in r.text
    assert w.cache.store.stats["denied"] == 1


async def test_a_session_label_counts_only_for_the_same_persons_session(pw):
    w = pw
    s = Session(name="ann-s", environment="plain", person="ann")
    w.store.put(s)
    a1, ann1 = _sandbox(w, "sb-a1", labels={"hangar.session": s.id})
    a2, ann2 = _sandbox(w, "sb-a2", labels={"hangar.session": s.id})  # the session's next sandbox
    m, mallory = _sandbox(w, "sb-m", labels={"hangar.session": s.id}, person="mallory")
    assert buildcache.private_layer(w.fleet, a1) == buildcache.private_layer(w.fleet, a2) == f"private:{s.id}:-"
    assert buildcache.private_layer(w.fleet, m) == "private:sb-m:-"
    async with w.client() as c:
        await c.put("/cache/sccache/x/y/obj", content=b"ann's", headers=_bearer(ann1))
        assert (await c.get("/cache/sccache/x/y/obj", headers=_bearer(ann2))).content == b"ann's"
        assert (await c.get("/cache/sccache/x/y/obj", headers=_bearer(mallory))).status_code == 404
        await c.put("/cache/sccache/x/y/obj2", content=b"evil", headers=_bearer(mallory))
        assert (await c.get("/cache/sccache/x/y/obj2", headers=_bearer(ann2))).status_code == 404


async def test_sccache_speaks_webdav(pw):
    w = pw
    _, build = _build_sandbox(w)
    h = _bearer(build)
    async with w.client() as c:
        assert (await c.request("MKCOL", "/cache/sccache/a/", headers=h)).status_code == 201
        r = await c.request("PROPFIND", "/cache/sccache/a/", headers={**h, "Depth": "0"})
        assert r.status_code == 207 and "<D:collection/>" in r.text
        assert (await c.request("PROPFIND", "/cache/sccache/a/b/obj", headers=h)).status_code == 404
        assert (await c.put("/cache/sccache/a/b/obj", content=b"object", headers=h)).status_code == 201
        r = await c.request("PROPFIND", "/cache/sccache/a/b/obj", headers=h)
        assert r.status_code == 207 and "<D:getcontentlength>6</D:getcontentlength>" in r.text
        assert (await c.get("/cache/sccache/a/b/obj", headers=h)).content == b"object"
        basic = {"Authorization": "Basic " + base64.b64encode(f"x:{build}".encode()).decode()}
        assert (await c.head("/cache/sccache/a/b/obj", headers=basic)).headers["content-length"] == "6"
        assert (await c.delete("/cache/sccache/a/b/obj", headers=h)).status_code == 403
        assert (await c.get("/cache/sccache/a/../b", headers=h)).status_code in (400, 404)


async def test_turborepo_speaks_the_remote_cache_api(pw):
    w = pw
    _, build = _build_sandbox(w)
    _, reader = _sandbox(w, "sb-r", person="bob")
    w.settings.build_cache_private_writes = False
    async with w.client() as c:
        base = "/cache/turbo/v8/artifacts"
        assert (await c.get(f"{base}/status", headers=_bearer(build))).json() == {"status": "enabled"}
        r = await c.put(f"{base}/abc123?teamId=team_x", content=b"tarball",
                        headers={**_bearer(build), "x-artifact-duration": "1500", "x-artifact-tag": "c2ln"})
        assert r.status_code == 202 and r.json()["urls"][0].endswith("/cache/turbo/v8/artifacts/abc123")
        r = await c.get(f"{base}/ABC123", headers=_bearer(reader))
        assert r.content == b"tarball" and r.headers["x-artifact-duration"] == "1500"
        assert r.headers["x-artifact-tag"] == "c2ln"
        r = await c.post(base, json={"hashes": ["abc123", "ffff"]}, headers=_bearer(reader))
        assert r.json() == {"abc123": {"size": 7, "taskDurationMs": 1500, "tag": "c2ln"}, "ffff": None}
        r = await c.get(f"{base}/ffff", headers=_bearer(reader))
        assert r.status_code == 404 and r.json()["code"] == "not_found"
        r = await c.put(f"{base}/dddd", content=b"x", headers=_bearer(reader))
        assert r.status_code == 403 and r.json()["code"] == "forbidden"
        r = await c.post(f"{base}/events", json=[{"sessionId": "x", "source": "REMOTE", "event": "HIT",
                                                   "hash": "abc123"}], headers=_bearer(reader))
        assert r.status_code == 200


async def test_only_a_live_sandboxs_repo_grant_gets_in(pw):
    w = pw
    sb, tok = _sandbox(w, "sb-x")
    async with w.client() as c:
        assert (await c.get(f"/cache/bazel/ac/{'a' * 64}")).status_code == 401
        assert (await c.get(f"/cache/bazel/ac/{'a' * 64}", headers=_bearer("hgs_nope"))).status_code == 401
        delivery, _ = w.fleet.gateway.mint_delivery(sb, "refs/heads/x", "a" * 40, 5)
        assert (await c.get(f"/cache/bazel/ac/{'a' * 64}", headers=_bearer(delivery))).status_code == 401
        assert (await c.get(f"/cache/bazel/ac/{'a' * 64}", headers=_bearer(tok))).status_code == 404
        sb.state = SandboxState.STOPPED
        w.store.put(sb)
        w.fleet.gateway.__dict__["_cache_server"]._seen.clear()
        assert (await c.get(f"/cache/bazel/ac/{'a' * 64}", headers=_bearer(tok))).status_code == 401


async def test_dependency_entries_are_immutable_and_restore_keys_fall_back(pw):
    w = pw
    _, build = _build_sandbox(w)
    _, s = _sandbox(w, "sb-s", person="bob")
    async with w.client() as c:
        u = "/cache/deps/npm?key=any/aaaaaaaa"
        assert (await c.put(u, content=b"v1", headers=_bearer(build))).status_code == 201
        assert (await c.put(u, content=b"v2", headers=_bearer(build))).status_code == 409
        r = await c.get("/cache/deps/npm?key=any/bbbbbbbb&restore=any/", headers=_bearer(s))
        assert r.status_code == 200 and r.content == b"v1"
        assert r.headers["x-hangar-cache-key"] == "any/aaaaaaaa"
        assert r.headers["x-hangar-cache-digest"] == hashlib.sha256(b"v1").hexdigest()
        assert (await c.get("/cache/deps/npm?key=any/bbbbbbbb", headers=_bearer(s))).status_code == 404
        assert (await c.head(f"{u}&write=1", headers=_bearer(build))).status_code == 200
        assert (await c.head(f"{u}&write=1", headers=_bearer(s))).status_code == 404  # not in *its* layer
        assert (await c.get("/cache/deps/nope?key=any/aaaaaaaa", headers=_bearer(s))).status_code == 400
        assert (await c.get("/cache/deps/npm?key=../../x", headers=_bearer(s))).status_code == 400
    acts = [e.action for e in w.store.audit_tail(50)]
    assert "cache.write" in acts


# ====================================================================== the sandbox's side, end to end
@pytest.fixture
async def sw(fleet, settings, store, broker):
    port = free_port()
    settings.gateway_url = f"http://127.0.0.1:{port}"
    settings.build_cache_wait_minutes = 1
    broker.put("pat", "git", CredentialKind.GIT_TOKEN, "ghp_stubUPSTREAMsecret0987654321")
    server = gateway._Server(fleet.gateway.asgi_app(), "127.0.0.1", port)
    server.start()
    async with httpx.AsyncClient() as c:
        for _ in range(200):
            with contextlib.suppress(httpx.HTTPError):
                if (await c.get(f"{settings.gateway_url}/healthz")).status_code == 200:
                    break
            await asyncio.sleep(0.05)
    w = World()
    w.fleet, w.store, w.settings, w.fake = fleet, store, settings, fleet.providers["docker"]
    w.cache = buildcache.of(fleet)
    w.spec = fleet.catalog.get("plain")
    yield w
    await server.stop()
    if w.cache._store is not None:
        await w.cache._store.drain()
        w.cache._store.index.close()


async def _box(w, sid, **kw) -> tuple[Sandbox, Path]:
    """A sandbox on the fake provider with a checkout of a small node project, attached as at create."""
    sb, tok = _sandbox(w, sid, **kw)
    await w.fake.create(sb, "img")
    w.fleet.gateway.issued[sb.id] = tok
    env = await w.cache.attach(w.fake, sb, w.spec)
    await w.fleet.broker.inject(sb, w.fake, [], extra_env=env)
    box = w.fake.boxes[sb.id]
    repo = box / "workspace/app"
    subprocess.run([*GIT, "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("node_modules\n")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    sb.info["repo_path"] = "/workspace/app"
    w.store.put(sb)
    return sb, box


async def _warm(box: Path, timeout: float = 30) -> str:
    state = box / "home/agent/.hangar/cache/state"
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if state.exists() and state.read_text().strip() not in ("", "warming"):
            return state.read_text().strip()
        await asyncio.sleep(0.1)
    raise AssertionError("the restore never finished: " + (box / "home/agent/.hangar/cache/restore.log").read_text())


async def test_a_build_saves_the_repos_caches_and_the_next_sandbox_restores_them_in_the_background(sw):
    w = sw
    build, bbox = await _box(w, "sb-build", **_build_kw(w))
    assert build.info["caches"]["write"] == "trusted"
    await w.cache.restore(w.fake, build)  # nothing saved yet: misses
    assert await _warm(bbox) == "warm"
    await w.cache.before_install(w.fake, build)
    (bbox / "home/agent/.npm/_cacache").mkdir(parents=True)
    (bbox / "home/agent/.npm/_cacache/pkg").write_text("tarball bytes")
    (bbox / "workspace/app/node_modules/left-pad").mkdir(parents=True)
    (bbox / "workspace/app/node_modules/left-pad/index.js").write_text("module.exports = 1\n")
    note = await w.cache.after_install(w.fake, build, "/workspace/app", {})
    assert "npm saved any/" in note and "node_modules saved" in note
    assert "restore: state: warm" in note and "npm miss" in note

    sb, box = await _box(w, "sb-sess", person="bob")
    assert sb.info["caches"] == {"state": "configured", "write": "private", "repo": "acme/app"}
    await w.cache.restore(w.fake, sb)
    assert sb.info["caches"]["state"] == "warming"
    lines = "\n".join(buildcache.agent_lines(sb))
    assert "hangar-cache wait" in lines and "private layer" in lines
    block = supervisor.instructions(sb, w.spec, "/workspace/app")
    assert "being restored in the background" in block
    assert await _warm(box) == "warm"
    assert (box / "home/agent/.npm/_cacache/pkg").read_text() == "tarball bytes"
    assert (box / "workspace/app/node_modules/left-pad/index.js").exists()
    res = await w.fake.exec(sb, '"$HOME/.hangar/bin/hangar-cache" status')
    assert "state: warm" in res.stdout and "npm hit any/" in res.stdout and "node_modules hit v" in res.stdout

    # the lockfile moved on: the newest entry for the same toolchain (restore-keys), and the restore never
    # overwrites what the sandbox already has
    sb2, box2 = await _box(w, "sb-sess2", person="bob")
    (box2 / "workspace/app/package-lock.json").write_text('{"lockfileVersion": 3, "x": 1}\n')
    (box2 / "home/agent/.npm/_cacache").mkdir(parents=True)
    (box2 / "home/agent/.npm/_cacache/pkg").write_text("its own")
    await w.cache.restore(w.fake, sb2)
    assert await _warm(box2) == "warm"
    res = await w.fake.exec(sb2, '"$HOME/.hangar/bin/hangar-cache" wait 5')
    assert "npm partial any/" in res.stdout
    assert (box2 / "home/agent/.npm/_cacache/pkg").read_text() == "its own"


def _build_kw(w) -> dict:
    b = _running_build(w)
    return {"labels": {envbuilds.LABEL: b.id}, "created_by": f"hangar:build:{b.id}"}


async def test_a_session_saves_to_its_private_layer_and_nobody_else_restores_it(sw):
    w = sw
    sb, box = await _box(w, "sb-ann")
    (box / "home/agent/.npm").mkdir(parents=True)
    (box / "home/agent/.npm/mine").write_text("ann's install")
    await w.cache.restore(w.fake, sb)
    await _warm(box)
    res = await w.fake.exec(sb, '"$HOME/.hangar/bin/hangar-cache" save npm')
    assert res.exit_code == 0 and "npm saved" in res.stdout, res.stdout + res.stderr
    res = await w.fake.exec(sb, '"$HOME/.hangar/bin/hangar-cache" save npm')
    assert "npm exists" in res.stdout
    other, obox = await _box(w, "sb-bob", person="bob")
    await w.cache.restore(w.fake, other)
    await _warm(obox)
    assert not (obox / "home/agent/.npm/mine").exists()
    assert (await w.fake.exec(other, '"$HOME/.hangar/bin/hangar-cache" status')).stdout.count("npm miss") == 1
    e = w.cache.store.index.where("1=1")
    assert [x.layer for x in e] == ["private:sb-ann:-"]


async def test_a_build_secret_in_a_cache_is_never_saved_and_the_build_says_so(sw):
    w = sw
    build, bbox = await _box(w, "sb-build", **_build_kw(w))
    secret = b"npm_SECRETtoken0123456789abc"
    (bbox / "home/agent/.npm").mkdir(parents=True)
    (bbox / "home/agent/.npm/_auth").write_bytes(b"//registry/:_authToken=" + secret + b"\n")
    note = await w.cache.after_install(w.fake, build, "/workspace/app", {"NPM_TOKEN": secret})
    assert "npm failed HTTP 422" in note and secret.decode() not in note
    assert not w.cache.store.index.where("1=1")


async def test_the_clients_are_pointed_at_hangar_without_touching_the_repo(sw):
    w = sw
    sb, box = await _box(w, "sb-ro", person="bob")
    envfile = (box / "home/agent/.hangar/env").read_text()
    for var in ("SCCACHE_WEBDAV_ENDPOINT", "SCCACHE_WEBDAV_TOKEN", "RUSTC_WRAPPER", "TURBO_API", "TURBO_TOKEN",
                "TURBO_TEAM", "UV_CACHE_DIR", "GOMODCACHE", "GOCACHE", "npm_config_cache", "PIP_CACHE_DIR"):
        assert f"export {var}=" in envfile
    assert "READ_ONLY" not in envfile  # private writes on: sccache and turbo write (to the private layer)
    rc = (box / "home/agent/.bazelrc").read_text()
    assert "build --remote_cache=http://hangar:hgs_" in rc and "/cache/bazel" in rc
    assert "remote_upload_local_results" not in rc
    assert oct((box / "home/agent/.bazelrc").stat().st_mode)[-3:] == "600"
    w.settings.build_cache_private_writes = False
    (box / "home/agent/.bazelrc").write_text("build --jobs=4\n" + rc)
    w.fleet.gateway.issued[sb.id] = w.fleet.gateway.mint(sb, "github.com", ["acme/app"], [], "pat")
    env = await w.cache.attach(w.fake, sb, w.spec.model_copy(update={"env": {"TURBO_API": "https://own.example"}}))
    assert env["SCCACHE_WEBDAV_RW_MODE"] == "READ_ONLY" and env["TURBO_CACHE"] == "local:rw,remote:r"
    assert "TURBO_API" not in env  # the environment's own remote cache stays
    rc = (box / "home/agent/.bazelrc").read_text()
    assert rc.startswith("build --jobs=4\n") and rc.count("remote_cache=") == 1
    assert "build --remote_upload_local_results=false" in rc
    # a fork without a grant of its own keeps nothing of its parent's
    fork = Sandbox(id="sb-fork", name="fork", environment="plain", provider="docker", parent=sb.id,
                   repo=REPO_URL, state=SandboxState.RUNNING)
    w.fake.boxes[fork.id] = box
    fork.provider_ref = fork.id
    assert await w.cache.attach(w.fake, fork, w.spec) == {}
    assert (box / "home/agent/.hangar/cache.conf").read_text() == ""
    assert "hangar build caches" not in (box / "home/agent/.bazelrc").read_text()
    # the rustc wrapper runs rustc itself when the image has no sccache
    res = await w.fake.exec(sb, '"$HOME/.hangar/bin/hangar-rustc-wrapper" echo compiled')
    assert res.stdout.strip() == "compiled"


async def test_hangar_status_shows_the_sizes_and_an_admin_purges(sw):
    w = sw
    buildcache.register(None)
    _, build = _sandbox(w, "sb-b", **_build_kw(w))
    async with httpx.AsyncClient(base_url=w.settings.gateway_url) as c:
        await c.put(f"/cache/bazel/ac/{'a' * 64}", content=b"x" * 2048, headers=_bearer(build))
    v = await opstatus.status(w.fleet, sections={"caches"})
    bc = v["caches"]
    assert bc["enabled"] and bc["bytes"] == 2048 and bc["repos"][0]["repo"] == "acme/app"
    assert bc["repos"][0]["trusted_bytes"] == 2048 and bc["since_start"]["writes"] == 1
    from hangar.ops_cli import cache_lines

    assert "acme/app" in "\n".join(cache_lines(bc))
    assert await w.cache.store.purge("acme/app") == 1
    await w.cache.store.drain()
    assert w.cache.status()["bytes"] == 0
    w.settings.gateway_url = None
    assert w.cache.status()["enabled"] is False


# ====================================================================== before and after, on a fixture repo
@pytest.mark.skipif(os.environ.get("HANGAR_CACHE_BENCH") != "1", reason="HANGAR_CACHE_BENCH=1: real npm installs")
async def test_bench_npm_install_cold_and_warm(sw):  # pragma: no cover - a measurement, not a check
    """The fixture project's dependencies (express, eslint, typescript, lodash: 162 packages) in a sandbox with no
    caches (a repo build: `npm ci`, then hangar saves ~/.npm and node_modules), then in the next sandbox: the
    background restore, and `npm ci` after it. Prints the times (docs/caches.md#measured)."""
    import shutil

    w = sw
    fixture = Path(__file__).parent / "fixtures" / "cache-bench"
    passthrough = "".join(f"export {k}={shlex.quote(v)}; " for k, v in os.environ.items()
                          if re.search(r"proxy|_CA|CERT", k, re.I) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k))
    npm = (f"{passthrough}export PATH={shlex.quote(os.path.dirname(shutil.which('npm') or '/usr/bin/npm'))}:$PATH "
           'npm_config_cache="$HOME/.npm"; cd /workspace/app && ')
    times: dict[str, float] = {}
    for name, kw in (("cold", _build_kw(w)), ("warm", {"person": "bob"})):
        sb, box = await _box(w, f"sb-{name}", **kw)
        for f in ("package.json", "package-lock.json"):
            (box / "workspace/app" / f).write_bytes((fixture / f).read_bytes())
        t = time.monotonic()
        await w.cache.restore(w.fake, sb)
        await _warm(box, 600)
        times[f"{name}: restore"] = time.monotonic() - t
        if name == "warm":
            t = time.monotonic()
            res = await w.fake.exec(sb, npm + "node -e \"require('typescript'); require('express')\"", timeout=60)
            times["warm: usable without installing"] = time.monotonic() - t if res.exit_code == 0 else -1
        t = time.monotonic()
        res = await w.fake.exec(sb, npm + "npm ci --no-audit --no-fund --prefer-offline", timeout=900)
        assert res.exit_code == 0, res.stderr[-2000:]
        times[f"{name}: npm ci"] = time.monotonic() - t
        if name == "cold":
            t = time.monotonic()
            print(await w.cache.after_install(w.fake, sb, "/workspace/app", {}))
            times["cold: save"] = time.monotonic() - t
    print("\n" + "\n".join(f"{k:34} {v:6.1f}s" for k, v in times.items()))
    print(json.dumps({**w.cache.status()["since_start"], "bytes": w.cache.status()["bytes"]}))
