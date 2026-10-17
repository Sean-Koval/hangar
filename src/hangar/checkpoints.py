"""Where session checkpoints live (spec 0010): the control plane's data volume, or a GCS bucket.

GCS is the durable option on GKE: checkpoints don't fill (or die with) the control plane's zonal disk, and a
bucket can hold any number of sessions. Auth uses the pod's Workload Identity token from the metadata server,
so no Google client library is needed. Blobs arrive already encrypted (vault key).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx


class CheckpointStore(Protocol):
    description: str

    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def delete_prefix(self, prefix: str) -> None: ...

    async def copy(self, src: str, dst: str) -> bool:
        """Copy a blob inside the store (an unchanged checkpoint part). False when `src` does not exist."""
        ...

    # --- overnight/sweep: database backups (backup.py) list theirs to keep the newest and age the last one
    async def list(self, prefix: str) -> list[str]:
        """Keys under `prefix` (a directory, ending in "/")."""
        ...

    async def delete(self, key: str) -> None: ...


class LocalStore:
    def __init__(self, root: Path):
        self.root = root
        self.description = f"local volume ({root})"

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve()) + "/"):
            raise ValueError(f"bad checkpoint key {key!r}")
        return p

    async def put(self, key: str, data: bytes) -> None:
        p = self._path(key)

        def write() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".part")
            tmp.unlink(missing_ok=True)  # review(perf): a leftover may be a hard link to another checkpoint's blob
            tmp.write_bytes(data)
            tmp.replace(p)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes | None:
        p = self._path(key)
        return await asyncio.to_thread(lambda: p.read_bytes() if p.is_file() else None)

    async def delete_prefix(self, prefix: str) -> None:
        p = self._path(prefix.rstrip("/") + "/x").parent
        await asyncio.to_thread(shutil.rmtree, p, True)

    async def copy(self, src: str, dst: str) -> bool:
        s, d = self._path(src), self._path(dst)

        def link() -> bool:
            if not s.is_file():
                return False
            d.parent.mkdir(parents=True, exist_ok=True)
            tmp = d.with_suffix(d.suffix + ".part")
            tmp.unlink(missing_ok=True)
            try:
                os.link(s, tmp)  # blobs are never modified in place, so sharing the inode is safe
            except OSError:
                shutil.copyfile(s, tmp)
            tmp.replace(d)
            return True

        return await asyncio.to_thread(link)

    async def list(self, prefix: str) -> list[str]:  # --- overnight/sweep; --- durable: "" lists everything
        d = self._path(prefix.rstrip("/") + "/x").parent if prefix.strip("/") else self.root
        root = self.root.resolve()

        def walk() -> list[str]:
            return sorted(str(p.relative_to(root)) for p in d.rglob("*") if p.is_file() and p.suffix != ".part")

        return await asyncio.to_thread(walk) if d.is_dir() else []

    async def delete(self, key: str) -> None:
        p = self._path(key)
        await asyncio.to_thread(p.unlink, True)


class GCSStore:
    """Minimal GCS JSON API client (upload, download, copy, list+delete) with metadata-server tokens."""

    API = "https://storage.googleapis.com"
    ATTEMPTS = 5
    BACKOFF = 0.5
    METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None, token: str | None = None):
        if not url.startswith("gs://"):
            raise ValueError("expected gs://bucket[/prefix]")
        bucket, _, prefix = url[5:].partition("/")
        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.description = f"GCS bucket gs://{bucket}" + (f"/{self.prefix}" if self.prefix else "")
        self._transport = transport
        self._token, self._exp = token, (float("inf") if token else 0.0)
        self._lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None

    def _client(self) -> httpx.AsyncClient:
        """One connection pool per event loop, so parts uploaded in parallel and back-to-back checkpoints reuse
        connections instead of each paying a TLS handshake."""
        loop = asyncio.get_running_loop()
        if self._http is None or self._http_loop is not loop or self._http.is_closed:
            self._http, self._http_loop = httpx.AsyncClient(transport=self._transport, timeout=60), loop
        return self._http

    async def close(self) -> None:
        if self._http is not None and self._http_loop is asyncio.get_running_loop():
            await self._http.aclose()
        self._http = None

    def _name(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    async def _auth(self, refresh: bool = False) -> dict[str, str]:
        async with self._lock:
            if refresh and self._exp != float("inf"):
                self._token = None
            if not self._token or time.time() > self._exp - 60:
                async with httpx.AsyncClient(transport=self._transport, timeout=10) as c:
                    r = await c.get(self.METADATA, headers={"Metadata-Flavor": "Google"})
                    r.raise_for_status()
                    j = r.json()
                self._token, self._exp = j["access_token"], time.time() + int(j.get("expires_in", 300))
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, c: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
        """Retries what GCS says to retry (429, 5xx, dropped connections) with backoff, and refreshes the token
        once on 401. Uploads are whole-object and idempotent, so re-sending is safe."""
        headers = kw.pop("headers", {})
        refreshed = False
        for attempt in range(self.ATTEMPTS):
            try:
                r = await c.request(method, url, headers={**headers, **await self._auth()}, **kw)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == self.ATTEMPTS - 1:
                    raise
            else:
                if r.status_code == 401 and not refreshed:
                    refreshed = True
                    await self._auth(refresh=True)
                    continue
                if r.status_code not in (408, 429) and r.status_code < 500:
                    return r
                if attempt == self.ATTEMPTS - 1:
                    return r
            await asyncio.sleep(min(8.0, self.BACKOFF * 2 ** attempt))
        raise RuntimeError("unreachable")

    async def put(self, key: str, data: bytes) -> None:
        r = await self._request(self._client(), "POST", f"{self.API}/upload/storage/v1/b/{self.bucket}/o",
                                params={"uploadType": "media", "name": self._name(key)}, content=data,
                                headers={"Content-Type": "application/octet-stream"}, timeout=300)
        r.raise_for_status()

    async def put_once(self, key: str, data: bytes, content_type: str = "application/octet-stream",
                       content_encoding: str | None = None) -> bool:
        """--- outbox (#163): create `key` only if it doesn't exist (ifGenerationMatch=0). True: created; False: it
        was already there (412), which a write-once upload counts as done. Never overwrites."""
        params = {"uploadType": "media", "name": self._name(key), "ifGenerationMatch": "0"}
        if content_encoding:
            params["contentEncoding"] = content_encoding
        r = await self._request(self._client(), "POST", f"{self.API}/upload/storage/v1/b/{self.bucket}/o",
                                params=params, content=data, headers={"Content-Type": content_type}, timeout=300)
        if r.status_code == 412:
            return False
        r.raise_for_status()
        return True

    async def get(self, key: str) -> bytes | None:
        r = await self._request(self._client(), "GET",
                                f"{self.API}/storage/v1/b/{self.bucket}/o/{quote(self._name(key), safe='')}",
                                params={"alt": "media"}, timeout=300)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content

    async def copy(self, src: str, dst: str) -> bool:
        """Server-side copy (objects.rewrite): nothing is downloaded or uploaded. Within one bucket (same location
        and storage class) it finishes in one call; the token loop covers the general case."""
        url = (f"{self.API}/storage/v1/b/{self.bucket}/o/{quote(self._name(src), safe='')}"
               f"/rewriteTo/b/{self.bucket}/o/{quote(self._name(dst), safe='')}")
        params: dict[str, str] = {"fields": "done,rewriteToken"}
        for _ in range(1000):
            r = await self._request(self._client(), "POST", url, params=params, timeout=300)
            if r.status_code == 404:
                return False
            r.raise_for_status()
            j = r.json()
            if j.get("done"):
                return True
            params = {"fields": "done,rewriteToken", "rewriteToken": j["rewriteToken"]}
        raise RuntimeError(f"copy of {src} did not finish")

    async def list(self, prefix: str) -> list[str]:  # --- overnight/sweep; --- durable: "" lists everything
        out: list[str] = []
        params = {"prefix": self._name(prefix.rstrip("/") + "/") if prefix.strip("/") else
                  (self.prefix + "/" if self.prefix else ""), "fields": "items(name),nextPageToken"}
        while True:
            r = await self._request(self._client(), "GET", f"{self.API}/storage/v1/b/{self.bucket}/o", params=params)
            r.raise_for_status()
            j = r.json()
            out += [it["name"][len(self.prefix) + 1:] if self.prefix else it["name"] for it in j.get("items", [])]
            if not j.get("nextPageToken"):
                return sorted(out)
            params = {**params, "pageToken": j["nextPageToken"]}

    async def delete(self, key: str) -> None:
        r = await self._request(self._client(), "DELETE",
                                f"{self.API}/storage/v1/b/{self.bucket}/o/{quote(self._name(key), safe='')}")
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    async def delete_prefix(self, prefix: str) -> None:
        async with httpx.AsyncClient(transport=self._transport, timeout=60) as c:
            token = None
            while True:
                params = {"prefix": self._name(prefix.rstrip("/") + "/"), "fields": "items(name),nextPageToken"}
                if token:
                    params["pageToken"] = token
                r = await self._request(c, "GET", f"{self.API}/storage/v1/b/{self.bucket}/o", params=params)
                r.raise_for_status()
                j = r.json()
                for it in j.get("items", []):
                    d = await self._request(c, "DELETE",
                                            f"{self.API}/storage/v1/b/{self.bucket}/o/{quote(it['name'], safe='')}")
                    if d.status_code not in (200, 204, 404):
                        d.raise_for_status()
                token = j.get("nextPageToken")
                if not token:
                    return


def make_store(location: str | None, data_dir: Path) -> CheckpointStore:
    """`HANGAR_SESSION_STORE`: empty = the data volume; gs://bucket[/prefix] = GCS."""
    if location and location.startswith("gs://"):
        return GCSStore(location)
    return LocalStore(Path(location) if location else data_dir / "sessions")
