"""Container registry reads over the OCI distribution API (Docker Registry HTTP API v2).

Three questions, each answered with an HTTP request or two, never a `gcloud` or `docker` process per tag: does a tag
exist, which digest does it point to (packs are mounted by digest), and which CLI version a harness pack recorded
(its last layer, see harnesses.VERSION_FILE). Used by `hangar images plan|build` in CI and on laptops, and by the
Kubernetes provider in the cluster.

Auth follows the registry's own challenge (`WWW-Authenticate: Bearer realm=...`), anonymously or, for Google's
registries (Artifact Registry, gcr.io), with a Google access token as the `oauth2accesstoken` password. The token
comes from HANGAR_REGISTRY_TOKEN (CI), else Application Default Credentials (Workload Identity in the cluster), else
one `gcloud auth print-access-token` per half hour. Tokens are never logged.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import logging
import os
import re
import tarfile
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("hangar.registry")

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
ACCEPT = ", ".join((*INDEX_TYPES, *MANIFEST_TYPES))
MAX_MANIFEST = 4 * 1024 * 1024
MAX_VERSION_LAYER = 64 * 1024  # a pack's version layer is one small file; anything bigger is not it
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_GOOGLE = re.compile(r"(^|.*\.)(pkg\.dev|gcr\.io)$")


class RegistryError(RuntimeError):
    """The registry couldn't answer (auth, network, a 5xx): the image's state is unknown, not "missing"."""


@dataclass(frozen=True)
class ImageRef:
    host: str
    repo: str
    tag: str | None = None
    digest: str | None = None

    @property
    def reference(self) -> str:
        return self.digest or self.tag or "latest"

    def pinned(self, digest: str) -> str:
        """`host/repo@sha256:...`: the immutable reference packs are mounted by."""
        return f"{self.host}/{self.repo}@{digest}"


def parse(ref: str) -> ImageRef:
    """Split `host/repo[:tag][@sha256:...]`. Only references that name a registry host can be asked about."""
    name, _, digest = ref.partition("@")
    if digest and not _DIGEST.fullmatch(digest):
        raise RegistryError(f"not a sha256 digest: {digest!r}")
    tag = None
    if (colon := name.rfind(":")) > name.rfind("/"):
        name, tag = name[:colon], name[colon + 1:]
    host, _, repo = name.partition("/")
    if not repo or not ("." in host or ":" in host or host == "localhost"):
        raise RegistryError(f"{ref} names no registry (a local image)")
    if host == "docker.io":
        host, repo = "registry-1.docker.io", repo if "/" in repo else f"library/{repo}"
    return ImageRef(host, repo, tag, digest or None)


def is_registry_ref(ref: str) -> bool:
    try:
        parse(ref)
        return True
    except RegistryError:
        return False


def _adc_token() -> tuple[str, float]:
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    expiry = getattr(creds, "expiry", None)
    return creds.token, (expiry.timestamp() - time.time()) if expiry else 1800.0


class _GoogleToken:
    """An OAuth access token for Google's registries, refreshed before it expires."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._until = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        if env := os.environ.get("HANGAR_REGISTRY_TOKEN"):
            return env.strip()
        async with self._lock:
            if self._token and time.monotonic() < self._until:
                return self._token
            self._token, ttl = await self._fetch()
            self._until = time.monotonic() + max(60.0, ttl - 300)
            return self._token

    async def _fetch(self) -> tuple[str, float]:
        try:
            return await asyncio.to_thread(_adc_token)
        except Exception as e:  # noqa: BLE001 - google-auth missing or no ADC here: try gcloud
            log.debug("no application default credentials: %s", type(e).__name__)
        try:
            proc = await asyncio.create_subprocess_exec(
                "gcloud", "auth", "print-access-token", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
        except (FileNotFoundError, TimeoutError) as e:
            raise RegistryError("no Google credentials for the registry: set HANGAR_REGISTRY_TOKEN, application "
                                "default credentials, or log in with gcloud") from e
        if proc.returncode != 0 or not out.strip():
            raise RegistryError("no Google credentials for the registry (`gcloud auth print-access-token` failed)")
        return out.decode().strip(), 1800.0


class Registry:
    """One client for many questions; tokens are cached per repository. Close it (or use `async with`)."""

    def __init__(self, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport)
        self._google = _GoogleToken()
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    async def __aenter__(self) -> Registry:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth
    async def _basic(self, host: str) -> tuple[str, str] | None:
        return ("oauth2accesstoken", await self._google.get()) if _GOOGLE.match(host) else None

    async def _bearer(self, ref: ImageRef, challenge: str) -> str:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", None)
        if not realm or not realm.startswith("https://"):
            raise RegistryError(f"{ref.host} asked for auth at an unusable realm")
        params.setdefault("scope", f"repository:{ref.repo}:pull")
        no_creds: RegistryError | None = None
        auth = None
        try:
            # Credentials go only to the registry's own token endpoint, never to a realm on another host.
            if httpx.URL(realm).host == ref.host:
                auth = await self._basic(ref.host)
        except RegistryError as e:  # public images still answer an anonymous token request
            no_creds = e
        r = await self._http.get(realm, params=params, auth=auth)
        if r.status_code != 200:
            raise no_creds or RegistryError(f"{ref.host} refused a pull token for {ref.repo} (HTTP {r.status_code})")
        body = r.json()
        token = body.get("token") or body.get("access_token")
        if not token:
            raise RegistryError(f"{ref.host} returned no token")
        self._tokens[(ref.host, ref.repo)] = (token, time.monotonic() + max(30, int(body.get("expires_in", 60)) - 30))
        return token

    async def _request(self, method: str, ref: ImageRef, path: str, accept: str | None = None) -> httpx.Response:
        url = f"https://{ref.host}/v2/{ref.repo}/{path}"
        headers = {"Accept": accept} if accept else {}
        cached = self._tokens.get((ref.host, ref.repo))
        if cached and time.monotonic() < cached[1]:
            headers["Authorization"] = f"Bearer {cached[0]}"
        try:
            r = await self._http.request(method, url, headers=headers)
            if r.status_code == 401:
                challenge = r.headers.get("www-authenticate", "")
                if challenge.lower().startswith("bearer "):
                    headers["Authorization"] = f"Bearer {await self._bearer(ref, challenge)}"
                    r = await self._http.request(method, url, headers=headers)
                elif challenge.lower().startswith("basic ") and (basic := await self._basic(ref.host)):
                    r = await self._http.request(method, url, headers=headers, auth=basic)
        except httpx.HTTPError as e:
            raise RegistryError(f"{ref.host}: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise RegistryError(f"{ref.host} denied access to {ref.repo} (HTTP {r.status_code})")
        if r.status_code >= 500 or r.status_code == 429:
            raise RegistryError(f"{ref.host} answered HTTP {r.status_code}")
        return r

    # ------------------------------------------------------------------ questions
    async def digest(self, image: str) -> str | None:
        """The manifest digest `image` points to, or None when the tag (or digest) doesn't exist."""
        ref = parse(image)
        r = await self._request("HEAD", ref, f"manifests/{ref.reference}", ACCEPT)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RegistryError(f"{ref.host} answered HTTP {r.status_code} for {image}")
        if d := r.headers.get("docker-content-digest"):
            if _DIGEST.fullmatch(d):
                return d
        r = await self._request("GET", ref, f"manifests/{ref.reference}", ACCEPT)  # no digest header: hash it
        if r.status_code == 404:
            return None
        return "sha256:" + hashlib.sha256(r.content).hexdigest()

    async def exists(self, image: str) -> bool:
        return await self.digest(image) is not None

    async def pinned(self, image: str) -> str | None:
        """`host/repo@sha256:...` for a tag, or None when it doesn't exist."""
        d = await self.digest(image)
        return parse(image).pinned(d) if d else None

    async def check(self, images: list[str], concurrency: int = 8) -> dict[str, str | None | RegistryError]:
        """Digest (None: missing, RegistryError: unknown) for many images, `concurrency` requests at a time."""
        sem = asyncio.Semaphore(concurrency)

        async def one(image: str) -> str | None | RegistryError:
            async with sem:
                try:
                    return await self.digest(image)
                except RegistryError as e:
                    return e

        return dict(zip(images, await asyncio.gather(*(one(i) for i in images)), strict=True))

    async def _manifest(self, ref: ImageRef, reference: str) -> dict[str, Any]:
        r = await self._request("GET", ref, f"manifests/{reference}", ACCEPT)
        if r.status_code != 200 or len(r.content) > MAX_MANIFEST:
            raise RegistryError(f"no manifest {reference} in {ref.repo} (HTTP {r.status_code})")
        if reference.startswith("sha256:") and hashlib.sha256(r.content).hexdigest() != reference[7:]:
            raise RegistryError(f"manifest {reference} in {ref.repo} doesn't match its digest")
        m = r.json()
        if m.get("mediaType") in INDEX_TYPES or "manifests" in m:
            # An index (buildx adds attestations): the linux/amd64 image, else the first real image.
            imgs = [x for x in m.get("manifests") or []
                    if (x.get("platform") or {}).get("os") not in (None, "unknown")]
            pick = next((x for x in imgs if x["platform"].get("architecture") == "amd64"), imgs[0] if imgs else None)
            if not pick or not _DIGEST.fullmatch(str(pick.get("digest"))):
                raise RegistryError(f"index {reference} in {ref.repo} has no image for linux")
            return await self._manifest(ref, pick["digest"])
        return m

    async def harness_version(self, image: str) -> str | None:
        """The CLI version a harness pack recorded at build (harnesses.VERSION_FILE, its last layer), or None for a
        pack built before hangar recorded it."""
        from hangar.harnesses import VERSION_FILE

        ref = parse(image)
        layers = (await self._manifest(ref, ref.reference)).get("layers") or []
        if not layers:
            return None
        last = layers[-1]
        digest, size = str(last.get("digest")), int(last.get("size") or 0)
        if not _DIGEST.fullmatch(digest) or size > MAX_VERSION_LAYER:
            return None
        r = await self._request("GET", ref, f"blobs/{digest}")
        if r.status_code != 200 or len(r.content) > MAX_VERSION_LAYER:
            raise RegistryError(f"can't read layer {digest} of {image} (HTTP {r.status_code})")
        if hashlib.sha256(r.content).hexdigest() != digest[7:]:
            raise RegistryError(f"layer {digest} of {image} doesn't match its digest")
        data = r.content
        if data[:2] == b"\x1f\x8b":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                    data = gz.read(MAX_VERSION_LAYER + 1)  # bounded: a small layer can't inflate into memory
            except (OSError, EOFError):
                return None
            if len(data) > MAX_VERSION_LAYER:
                return None
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                for m in tf.getmembers():
                    if m.isfile() and m.name.removeprefix("./").lstrip("/") == VERSION_FILE and m.size <= 1024:
                        f = tf.extractfile(m)
                        text = f.read().decode(errors="replace").strip() if f else ""
                        return text.splitlines()[0][:200] if text else None
        except tarfile.TarError:
            return None
        return None
