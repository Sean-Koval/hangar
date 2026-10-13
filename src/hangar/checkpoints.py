"""Where session checkpoints live (spec 0010): the control plane's data volume, or a GCS bucket.

GCS is the durable option on GKE: checkpoints don't fill (or die with) the control plane's zonal disk, and a
bucket can hold any number of sessions. Auth uses the pod's Workload Identity token from the metadata server,
so no Google client library is needed. Blobs arrive already encrypted (vault key).
"""

from __future__ import annotations

import asyncio
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
            tmp.write_bytes(data)
            tmp.replace(p)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes | None:
        p = self._path(key)
        return await asyncio.to_thread(lambda: p.read_bytes() if p.is_file() else None)

    async def delete_prefix(self, prefix: str) -> None:
        p = self._path(prefix.rstrip("/") + "/x").parent
        await asyncio.to_thread(shutil.rmtree, p, True)


class GCSStore:
    """Minimal GCS JSON API client (upload, download, list+delete) with metadata-server tokens."""

    API = "https://storage.googleapis.com"
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

    def _name(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    async def _auth(self) -> dict[str, str]:
        async with self._lock:
            if not self._token or time.time() > self._exp - 60:
                async with httpx.AsyncClient(transport=self._transport, timeout=10) as c:
                    r = await c.get(self.METADATA, headers={"Metadata-Flavor": "Google"})
                    r.raise_for_status()
                    j = r.json()
                self._token, self._exp = j["access_token"], time.time() + int(j.get("expires_in", 300))
        return {"Authorization": f"Bearer {self._token}"}

    async def put(self, key: str, data: bytes) -> None:
        async with httpx.AsyncClient(transport=self._transport, timeout=300) as c:
            r = await c.post(f"{self.API}/upload/storage/v1/b/{self.bucket}/o",
                             params={"uploadType": "media", "name": self._name(key)}, content=data,
                             headers={**await self._auth(), "Content-Type": "application/octet-stream"})
            r.raise_for_status()

    async def get(self, key: str) -> bytes | None:
        async with httpx.AsyncClient(transport=self._transport, timeout=300) as c:
            r = await c.get(f"{self.API}/storage/v1/b/{self.bucket}/o/{quote(self._name(key), safe='')}",
                            params={"alt": "media"}, headers=await self._auth())
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content

    async def delete_prefix(self, prefix: str) -> None:
        async with httpx.AsyncClient(transport=self._transport, timeout=60) as c:
            token = None
            while True:
                params = {"prefix": self._name(prefix.rstrip("/") + "/"), "fields": "items(name),nextPageToken"}
                if token:
                    params["pageToken"] = token
                r = await c.get(f"{self.API}/storage/v1/b/{self.bucket}/o", params=params, headers=await self._auth())
                r.raise_for_status()
                j = r.json()
                for it in j.get("items", []):
                    d = await c.delete(f"{self.API}/storage/v1/b/{self.bucket}/o/{quote(it['name'], safe='')}",
                                       headers=await self._auth())
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
