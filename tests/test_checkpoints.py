"""Checkpoint stores (spec 0010): local volume and GCS (JSON API + metadata-server token)."""

from __future__ import annotations

import httpx
import pytest

from hangar.checkpoints import GCSStore, LocalStore, make_store


async def test_local_store(tmp_path):
    s = LocalStore(tmp_path)
    await s.put("ses-1/ckpt-a/agent.tgz.enc", b"x")
    await s.put("ses-1/ckpt-b/wip.patch.enc", b"y")
    assert await s.get("ses-1/ckpt-a/agent.tgz.enc") == b"x" and await s.get("nope/x") is None
    await s.delete_prefix("ses-1/ckpt-a/")
    assert await s.get("ses-1/ckpt-a/agent.tgz.enc") is None and await s.get("ses-1/ckpt-b/wip.patch.enc") == b"y"
    with pytest.raises(ValueError):
        await s.put("../escape", b"z")


async def test_gcs_store_round_trip():
    objects: dict[str, bytes] = {}
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.host, req.url.path))
        if req.url.host == "metadata.google.internal":
            assert req.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer tok"
        p = req.url.path
        if req.method == "POST" and p == "/upload/storage/v1/b/bkt/o":
            objects[req.url.params["name"]] = req.content
            return httpx.Response(200, json={})
        if req.method == "GET" and p == "/storage/v1/b/bkt/o":
            pre = req.url.params["prefix"]
            return httpx.Response(200, json={"items": [{"name": n} for n in objects if n.startswith(pre)]})
        name = httpx.URL(str(req.url)).path.split("/o/", 1)[1]
        from urllib.parse import unquote

        name = unquote(name)
        if req.method == "GET":
            return httpx.Response(200, content=objects[name]) if name in objects else httpx.Response(404)
        if req.method == "DELETE":
            objects.pop(name, None)
            return httpx.Response(204)
        return httpx.Response(400)

    s = GCSStore("gs://bkt/hangar/sessions", transport=httpx.MockTransport(handler))
    await s.put("ses-1/ckpt-a/agent.tgz.enc", b"\x00secret")
    assert "hangar/sessions/ses-1/ckpt-a/agent.tgz.enc" in objects
    assert await s.get("ses-1/ckpt-a/agent.tgz.enc") == b"\x00secret"
    assert await s.get("ses-1/missing") is None
    await s.delete_prefix("ses-1/")
    assert not objects
    assert sum(1 for c in calls if c[1] == "metadata.google.internal") == 1  # token cached


def test_make_store(tmp_path):
    assert isinstance(make_store(None, tmp_path), LocalStore)
    g = make_store("gs://b/p", tmp_path)
    assert isinstance(g, GCSStore) and g.bucket == "b" and g.prefix == "p"
