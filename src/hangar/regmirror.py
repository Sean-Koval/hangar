"""Docker Hub through hangar, read-only (--- heavy; docs/environments.md#docker-images).

A heavy sandbox's dockerd pulls Docker Hub images through registry mirrors (dockerd's `registry-mirrors`, which only
ever apply to Docker Hub). With HANGAR_HEAVY_MIRROR on, the first one is this: a read-only pull-through of the
Registry HTTP API on the git gateway's port, in the pattern of the package mirrors (pkgmirror.py, #107):

* **Read-only by construction.** GET and HEAD of the API root, of manifests and blobs by name and reference, and of
  tag lists; nothing else, and nothing that uploads. Redirects (Docker Hub sends blobs to its CDN) are followed here,
  only over HTTPS and only to the upstream's own storage hosts.
* **Upstream.** Docker Hub, with the anonymous pull tokens it hands out, or an Artifact Registry remote repository for
  Docker Hub (HANGAR_HEAVY_MIRROR_UPSTREAM, deploy/terraform/gke's dockerhub_mirror): a cache with Docker Hub
  credentials of its own, so a team's pulls don't hit the anonymous rate limit. Hangar reads it with its own Google
  credentials; sandboxes never get *.pkg.dev, which serves every project's writable repositories too.
* **Who.** dockerd sends no credentials to a mirror, and refuses a mirror URL with userinfo in it (moby's
  ValidateMirror), so the sandbox's egress credential is in the mirror's path: `/registry/<sandbox>/<token>/v2/...`.
  The token is the egress proxy's HMAC(vault key, sandbox), never stored and worthless once the sandbox stops. Only
  running heavy sandboxes with Docker are served.
* **Use** is summed per sandbox into the `egress.allow` audit entries like the package mirrors (host
  `docker.mirror.hangar`). When the mirror can't answer, dockerd falls back to Docker Hub itself, which the sandbox's
  egress policy then decides.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import re
import time
from collections import Counter
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from hangar import egress, keys
from hangar.models import Sandbox, SandboxState

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.gateway import Gateway

log = logging.getLogger("hangar.heavy")

HOST = "docker.mirror.hangar"  # what audit entries and egress metrics call it
DOCKER_HUB = "https://registry-1.docker.io"
INFLIGHT_PER_SANDBOX = 32  # dockerd pulls 3 layers at a time per image by default
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_NAME = re.compile(rf"^{_COMPONENT}(?:/{_COMPONENT})*$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PATH = re.compile(r"^(?P<name>.+)/(?:(?P<kind>manifests|blobs)/(?P<ref>[^/]+)|tags/list)$")
_REDIRECT_HOSTS = ("storage.googleapis.com",)  # Artifact Registry's downloads
_REDIRECT_SUFFIXES = (".docker.com", ".docker.io", ".r2.cloudflarestorage.com", ".pkg.dev")  # Docker Hub's CDN, AR
_PASS = ("content-type", "content-length", "docker-content-digest", "etag", "last-modified")


def mirror_url(settings: Settings, sandbox_id: str, password: str) -> str:
    """The mirror a sandbox's dockerd is given (its `registry-mirrors`)."""
    return f"{settings.gateway_url.rstrip('/')}/registry/{sandbox_id}/{password}"


def parse(rest: str) -> tuple[str, str, str] | None:
    """(name, kind, reference) of a read the mirror serves, `kind` manifests | blobs | tags; None for anything else."""
    m = _PATH.match(rest)
    if not m or len(m.group("name")) > 255 or not _NAME.match(m.group("name")):
        return None
    kind, ref = m.group("kind") or "tags", m.group("ref") or "list"
    if kind == "blobs" and not _DIGEST.match(ref):
        return None
    if kind == "manifests" and not (_DIGEST.match(ref) or _TAG.match(ref)):
        return None
    return m.group("name"), kind, ref


def upstream(settings: Settings) -> tuple[str, str]:
    """(origin, path prefix) of the upstream: Docker Hub's API is at /v2/<name>, an Artifact Registry repository's at
    /v2/<project>/<repository>/<name>."""
    u = urlsplit(settings.heavy_mirror_upstream or DOCKER_HUB)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("HANGAR_HEAVY_MIRROR_UPSTREAM must be an https URL")
    return f"https://{u.netloc}", u.path.strip("/")


def _google(host: str) -> bool:
    return host.endswith(".pkg.dev") or host == "gcr.io" or host.endswith(".gcr.io")


def _redirect_ok(url: str, origin: str) -> bool:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == urlsplit(origin).hostname or host in _REDIRECT_HOSTS
                                    or host.endswith(_REDIRECT_SUFFIXES))


class RegistryMirror:
    """The /registry routes on the gateway's app."""

    def __init__(self, gw: Gateway):
        self.gw, self.fleet, self.settings = gw, gw.fleet, gw.settings
        self._inflight: Counter[str] = Counter()
        self._tokens: dict[str, tuple[str, float]] = {}  # name -> (upstream pull token, until)
        self._google: Any = None

    def routes(self) -> list[Route]:
        return [Route("/registry/{sid}/{token}/v2/{rest:path}", self.handle,
                      methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])]

    def _sandbox(self, sid: str, token: str) -> Sandbox | None:
        """The running heavy sandbox the path's credential belongs to (any vault key not yet retired: a sandbox from
        before a rotation keeps its token, as the egress proxy checks)."""
        if not any(egress.check_token(k, sid, token) for k in keys.verify_keys(self.settings)):
            return None
        sb = self.fleet.store.get(Sandbox, sid)
        if sb is None or sb.state not in (SandboxState.STARTING, SandboxState.RUNNING):
            return None
        return sb if (sb.info.get("heavy") or {}).get("docker") else None

    async def handle(self, request: Request) -> Response:
        src = request.client.host if request.client else "unknown"
        if self.gw._failures.blocked(src):
            return PlainTextResponse("hangar: too many requests with invalid credentials; wait a minute\n", 429)
        sb = self._sandbox(request.path_params["sid"], request.path_params["token"])
        if sb is None:
            if self.gw._failures.add(src):
                self.fleet.audit("gateway", "gw.auth.blocked", None, source=src)
            return JSONResponse({"errors": [{"code": "UNAUTHORIZED", "message": "hangar's Docker Hub mirror is for "
                                             "heavy sandboxes, at the URL hangar gave their dockerd"}]}, 401)
        mode = (sb.info.get("egress") or {}).get("mode") or "open"
        if request.method not in ("GET", "HEAD"):
            self.fleet.egress._denied(sb, HOST, 443, _denied("read_only", "hangar's Docker Hub mirror is read-only"))
            return JSONResponse({"errors": [{"code": "UNSUPPORTED", "message": "read-only mirror"}]}, 405,
                                headers={"Allow": "GET, HEAD"})
        raw = (request.scope.get("raw_path") or b"").decode("latin-1").split("?", 1)[0]
        rest = raw.split("/v2/", 1)[1] if "/v2/" in raw else ""
        headers = {"Docker-Distribution-Api-Version": "registry/2.0"}
        if rest == "":  # dockerd's ping
            return JSONResponse({}, 200, headers=headers)
        what = parse(rest)
        if what is None:
            return JSONResponse({"errors": [{"code": "NAME_INVALID", "message": "not a read this mirror serves"}]},
                                404, headers=headers)
        if self._inflight[sb.id] >= INFLIGHT_PER_SANDBOX:
            return JSONResponse({"errors": [{"code": "TOOMANYREQUESTS", "message": "too many pulls at once"}]}, 429,
                                headers={**headers, "Retry-After": "2"})
        self._inflight[sb.id] += 1
        try:
            return await self._serve(request, sb, mode, *what)
        finally:
            self._inflight[sb.id] -= 1
            if self._inflight[sb.id] <= 0:
                del self._inflight[sb.id]

    async def _serve(self, request: Request, sb: Sandbox, mode: str, name: str, kind: str, ref: str) -> Response:
        try:
            origin, prefix = upstream(self.settings)
        except ValueError as e:
            return PlainTextResponse(f"hangar: {e}\n", 502)
        path = f"{prefix}/{name}" if prefix else name
        url = f"{origin}/v2/{path}/{'tags/list' if kind == 'tags' else f'{kind}/{ref}'}"
        accept = ", ".join(request.headers.getlist("accept"))[:2000] or "*/*"
        try:
            resp = await self._send(request.method, url, origin, path, accept)
        except Exception as e:  # noqa: BLE001 - shown to the client, never a secret
            log.warning("docker mirror: %s %s failed: %s", kind, name[:100], type(e).__name__)
            return PlainTextResponse(f"hangar: Docker Hub didn't answer ({type(e).__name__})\n", 502)
        out = {k: v for k, v in resp.headers.items() if k.lower() in _PASS}
        out["Docker-Distribution-Api-Version"] = "registry/2.0"
        if request.method == "HEAD" or resp.status_code != 200:
            await resp.aclose()
            self.fleet.egress.record(sb, HOST, 443, "mirror:docker", mode, 0, 0)
            status = 502 if resp.status_code >= 500 else 404 if resp.status_code in (401, 403) else resp.status_code
            return Response(b"", status_code=status, headers=out)

        async def stream() -> Any:
            n = 0
            try:
                async for chunk in resp.aiter_bytes():
                    n += len(chunk)
                    yield chunk
            finally:
                await resp.aclose()
                self.fleet.egress.record(sb, HOST, 443, "mirror:docker", mode, 0, n)

        out.pop("content-length", None)  # streamed: the upstream's length may be for a compressed body
        return StreamingResponse(stream(), status_code=200, headers=out)

    async def _send(self, method: str, url: str, origin: str, path: str, accept: str) -> Any:
        """The upstream's answer: the pull token its challenge asks for (Google credentials only to a Google
        registry's own token endpoint), and redirects followed only to its storage hosts, without credentials."""
        client = self.gw._http()
        headers = {"Accept": accept, "User-Agent": "hangar-regmirror"}
        cached = self._tokens.get(path)
        if cached and time.monotonic() < cached[1]:
            headers["Authorization"] = f"Bearer {cached[0]}"
        resp = await client.send(client.build_request(method, url, headers=headers), stream=True)
        if resp.status_code == 401:
            challenge = resp.headers.get("www-authenticate", "")
            await resp.aclose()
            headers["Authorization"] = f"Bearer {await self._token(origin, path, challenge)}"
            resp = await client.send(client.build_request(method, url, headers=headers), stream=True)
        for _ in range(4):
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            await resp.aclose()
            nxt = str(resp.url.join(resp.headers.get("location", "")))
            if not _redirect_ok(nxt, origin):
                raise PermissionError("redirect off the upstream's storage")
            if urlsplit(nxt).hostname != urlsplit(url).hostname:
                headers = {k: v for k, v in headers.items() if k != "Authorization"}
            url = nxt
            resp = await client.send(client.build_request(method, url, headers=headers), stream=True)
        await resp.aclose()
        raise PermissionError("too many redirects")

    async def _token(self, origin: str, path: str, challenge: str) -> str:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", None)
        if not challenge.lower().startswith("bearer ") or not realm or not realm.startswith("https://"):
            raise PermissionError("the upstream asked for auth at an unusable realm")
        params.setdefault("scope", f"repository:{path}:pull")
        host = urlsplit(origin).hostname or ""
        auth = None
        if _google(host) and urlsplit(realm).hostname == host:
            from hangar.registry import _GoogleToken

            if self._google is None:
                self._google = _GoogleToken()
            auth = "Basic " + base64.b64encode(f"oauth2accesstoken:{await self._google.get()}".encode()).decode()
        client = self.gw._http()
        r = await client.get(realm, params=params, headers={"Authorization": auth} if auth else {})
        if r.status_code != 200:
            raise PermissionError(f"the upstream refused a pull token (HTTP {r.status_code})")
        body: Any = {}
        with contextlib.suppress(ValueError):
            body = r.json()
        token = (body.get("token") or body.get("access_token")) if isinstance(body, dict) else None
        if not token:
            raise PermissionError("the upstream returned no token")
        self._tokens[path] = (token, time.monotonic() + max(30, int(body.get("expires_in", 60) or 60) - 30))
        if len(self._tokens) > 5000:
            self._tokens.clear()
        return token


def _denied(reason: str, message: str) -> Any:
    from hangar.egress_proxy import Denied

    return Denied(403, reason, message, "policy")
