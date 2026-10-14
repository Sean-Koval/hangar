"""Read-only package mirrors for proxied sandboxes (--- egress2; docs/security.md#package-mirrors).

An allowlist that names pypi.org or registry.npmjs.org lets a sandbox publish there with an attacker's own token:
the hosts that serve packages also take uploads (npm's registry takes `npm publish` on the same host; pypi.org's web
forms change an account's data). So in a proxied environment the `pypi`, `npm` and `go` presets don't open those
hosts. The sandbox's package managers are pointed at hangar instead, at `/pkg/<ecosystem>/` on the git gateway's
port, which every proxied sandbox reaches:

* **Read-only by construction.** GET and HEAD only, under the configured upstream's base URL only, without the
  client's credentials or headers. There is no path from a sandbox to a publish endpoint, and nothing to log in to.
* **Upstreams.** By default the public registries (PyPI and its file host, npm, the Go module proxy and checksum
  database). On GKE, point HANGAR_PACKAGE_MIRRORS at Artifact Registry remote repositories (pull-through caches that
  can't be published to); hangar reads them with its own Google credentials. Their host, `*.pkg.dev`, serves every
  project's repositories, an attacker's writable ones included, which is why sandboxes never get it as a host.
* **Go** module paths name their origin, and proxy.golang.org fetches a module it hasn't seen from that origin, so a
  lookup of `attacker.example/<data>` would carry data out. Hangar asks the proxy with `Disable-Module-Fetch: true`
  (it then serves only modules it already has), and passes a checksum-database lookup only for a module version the
  proxy already has.
* **A cache** on the control plane's disk keeps immutable files (wheels and sdists, npm tarballs, Go module zips),
  up to HANGAR_PACKAGE_MIRROR_CACHE_MB, the least recently used going first. That is the simple caching mirror for
  Docker; Artifact Registry is the cache on GKE.
* **Who.** A sandbox authenticates with its egress-proxy credential (in the URLs hangar sets), or its git grant.
  Each ecosystem is served only to sandboxes whose policy has its preset; use is summed per sandbox into the
  `egress.allow` audit entries like proxied traffic, and a refusal is an `egress.deny`.

Index pages and npm metadata name file URLs at the upstream; they are rewritten to the mirror, so every download
comes back here.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from hangar import egress
from hangar.models import Sandbox, SandboxState

if TYPE_CHECKING:
    from hangar.gateway import Gateway
    from hangar.providers.base import Provider

log = logging.getLogger("hangar.egress")

ECOSYSTEMS = ("pypi", "npm", "go")
# {ecosystem: {mount under /pkg/<ecosystem>/: upstream base}}. Checked 2026-10-14 against each registry's docs: PyPI's
# simple index and its file host (docs.pypi.org/api), npm's registry, the Go module proxy protocol (go.dev/ref/mod
# "GOPROXY protocol") and its checksum database proxied as <proxy>/sumdb/sum.golang.org/ ("Checksum database").
DEFAULTS: dict[str, dict[str, str]] = {
    "pypi": {"": "https://pypi.org", "files": "https://files.pythonhosted.org"},
    "npm": {"": "https://registry.npmjs.org"},
    "go": {"": "https://proxy.golang.org", "sumdb/sum.golang.org": "https://sum.golang.org"},
}
GO_NO_FETCH = {"Disable-Module-Fetch": "true"}  # pkgsite's proxy client: "prevents the proxy from fetching uncached
# modules" (golang.org/x/pkgsite/internal/proxy)
METADATA_MAX = 64 * 1024 * 1024  # an index page or packument held to rewrite its URLs
# --- review (#107): all the metadata held at once, across sandboxes; past it a request gets 503 and Retry-After
METADATA_HELD = 256 * 1024 * 1024
INFLIGHT_PER_SANDBOX = 64  # npm opens ~15 connections per origin; pip and Go fewer
_PATH = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
_IMMUTABLE = {"pypi": re.compile(r"^files/packages/.+"), "npm": re.compile(r".+/-/[^/]+\.tgz$"),
              "go": re.compile(r".+/@v/[^/]+\.(zip|mod|info)$")}
_REWRITE_TYPES = ("json", "html", "xml", "vnd.pypi.simple")


def upstreams(settings: Any) -> dict[str, dict[str, str]]:
    """Each ecosystem's mounts, with HANGAR_PACKAGE_MIRRORS replacing the main upstream (e.g. an Artifact Registry
    remote repository); the other mounts (PyPI's file host, Go's checksum database) stay for links that name them."""
    out = {e: dict(m) for e, m in DEFAULTS.items()}
    for eco, url in (settings.package_mirrors or {}).items():
        if eco in out and url.startswith("https://"):
            out[eco][""] = url.rstrip("/")
        else:
            log.warning("HANGAR_PACKAGE_MIRRORS: ignoring %s (ecosystems: %s; https URLs only)", eco,
                        ", ".join(ECOSYSTEMS))
    return out


def available(settings: Any) -> bool:
    """Whether proxied sandboxes get the mirror: it is on, and they have the gateway to reach it at."""
    return bool(settings.package_mirror and settings.gateway_url)


def _google(url: str) -> bool:
    return (urlsplit(url).hostname or "").endswith(".pkg.dev")


# ============================================================================ the sandbox's side
def sandbox_env(settings: Any, ecosystems: tuple[str, ...], user: str, password: str) -> tuple[dict[str, str], str]:
    """The variables that point a sandbox's package managers at the mirror, and the ~/.npmrc line with npm's
    credential (npm takes credentials per registry only from its config files)."""
    base = settings.gateway_url.rstrip("/")
    u = urlsplit(base)
    hostport = u.netloc
    authed = f"{u.scheme}://{user}:{password}@{hostport}"
    env: dict[str, str] = {}
    npmrc = ""
    if "pypi" in ecosystems:
        index = f"{authed}/pkg/pypi/simple/"
        env.update(PIP_INDEX_URL=index, PIP_TRUSTED_HOST=hostport, UV_DEFAULT_INDEX=index, UV_INDEX_URL=index)
    if "npm" in ecosystems:
        reg = f"{base}/pkg/npm/"
        env.update(NPM_CONFIG_REGISTRY=reg, npm_config_registry=reg, YARN_NPM_REGISTRY_SERVER=reg,
                   YARN_NPM_AUTH_IDENT=f"{user}:{password}", YARN_NPM_ALWAYS_AUTH="true",
                   YARN_UNSAFE_HTTP_WHITELIST=u.hostname or "")
        npmrc = f"//{hostport}/pkg/npm/:_auth={base64.b64encode(f'{user}:{password}'.encode()).decode()}"
    if "go" in ecosystems:
        env.update(GOPROXY=f"{authed}/pkg/go", GOSUMDB="sum.golang.org")
    return env, npmrc


async def write_npmrc(prov: Provider, sb: Sandbox, home: str, line: str) -> None:
    """Add (or replace) hangar's registry line in ~/.npmrc, keeping the rest of the file."""
    if not line:
        return
    key = line.split(":_auth=", 1)[0]
    cur = await prov.read_file(sb, f"{home}/.npmrc", limit=1024 * 1024) or b""
    kept = [x for x in cur.decode(errors="replace").splitlines() if not x.startswith(key)]
    await prov.write_file(sb, f"{home}/.npmrc", ("\n".join([*kept, line]) + "\n").encode(), mode=0o600)


# ============================================================================ the mirror
class Mirror:
    """The /pkg routes on the gateway's app."""

    def __init__(self, gw: Gateway):
        self.gw, self.fleet, self.settings = gw, gw.fleet, gw.settings
        self._inflight: Counter[str] = Counter()
        self._held = 0  # --- review (#107): bytes of metadata held for rewriting, all requests together
        self._token: Any = None
        self._cache_size: int | None = None
        self._evicting = False
        self._ups: tuple[Any, dict[str, dict[str, str]]] | None = None

    def mounts(self, eco: str) -> dict[str, str]:
        key = tuple(sorted((self.settings.package_mirrors or {}).items()))
        if self._ups is None or self._ups[0] != key:
            self._ups = (key, upstreams(self.settings))
        return self._ups[1][eco]

    def routes(self) -> list[Route]:
        return [Route("/pkg/{eco}/{rest:path}", self.handle,
                      methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])]

    @property
    def cache_dir(self) -> Path:
        return self.settings.data_dir / "pkgcache"

    # ------------------------------------------------------------------ who
    def _sandbox(self, request: Request) -> Sandbox | None:
        """The running sandbox behind the request's credential: its egress-proxy credential, or its git grant."""
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        user = password = ""
        if scheme.lower() == "basic":
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                user, _, password = base64.b64decode(value.strip(), validate=True).decode().partition(":")
        elif scheme.lower() == "bearer":
            password = value.strip()
        if user and egress.check_token(self.fleet.egress.key, user, password):
            sb = self.fleet.store.get(Sandbox, user)
            return sb if sb is not None and sb.state in (SandboxState.STARTING, SandboxState.RUNNING) else None
        if password.startswith("hgs_") and (grant := self.gw.verify(password)) is not None:
            return self.fleet.store.get(Sandbox, grant.sandbox_id)
        return None

    # ------------------------------------------------------------------ one request
    async def handle(self, request: Request) -> Response:
        eco = request.path_params["eco"]
        raw = (request.scope.get("raw_path") or b"").decode("latin-1").split("?", 1)[0]
        rest = raw.split(f"/pkg/{eco}/", 1)[1] if f"/pkg/{eco}/" in raw else ""
        if eco not in ECOSYSTEMS:
            return PlainTextResponse(f"hangar: no package mirror for '{eco[:40]}' ({', '.join(ECOSYSTEMS)})\n", 404)
        src = request.client.host if request.client else "unknown"
        if self.gw._failures.blocked(src):
            return PlainTextResponse("hangar: too many requests with invalid credentials; wait a minute\n", 429)
        sb = self._sandbox(request)
        if sb is None:
            if request.headers.get("authorization") and self.gw._failures.add(src):
                self.fleet.audit("gateway", "gw.auth.blocked", None, source=src)
            return PlainTextResponse("hangar: the package mirror is for hangar's sandboxes; use the index URL hangar "
                                     "set (PIP_INDEX_URL, NPM_CONFIG_REGISTRY, GOPROXY)\n", 401,
                                     headers={"WWW-Authenticate": 'Basic realm="hangar packages"'})
        host = f"{eco}.mirror.hangar"
        if request.method not in ("GET", "HEAD"):
            self._deny(sb, host, "read_only", f"the {eco} mirror is read-only: publishing from a sandbox isn't "
                                              "possible")
            return PlainTextResponse(f"hangar: the {eco} mirror is read-only; nothing can be published from a "
                                     "sandbox\n", 405, headers={"Allow": "GET, HEAD"})
        try:
            pol = self.fleet.egress._policy(sb)
        except Exception:  # noqa: BLE001 - its environment is gone: deny
            pol = None
        if pol is None or eco not in pol.mirrors:
            self._deny(sb, host, "not_allowed", f"the {eco} mirror isn't in this sandbox's egress policy (preset "
                                                f"'{eco}' or 'packages')")
            return PlainTextResponse(f"hangar: this sandbox's egress policy has no '{eco}' preset\n", 403)
        if not _PATH.match(rest) or "//" in rest or any(s in ("..", ".") for s in unquote(rest).split("/")):
            return PlainTextResponse("hangar: bad path\n", 400)
        if self._inflight[sb.id] >= INFLIGHT_PER_SANDBOX:
            return PlainTextResponse("hangar: too many package requests at once from this sandbox\n", 429,
                                     headers={"Retry-After": "2"})
        self._inflight[sb.id] += 1
        try:
            return await self._serve(request, sb, eco, rest, pol.mode)
        finally:
            self._inflight[sb.id] -= 1
            if self._inflight[sb.id] <= 0:
                del self._inflight[sb.id]

    def _deny(self, sb: Sandbox, host: str, reason: str, message: str) -> None:
        from hangar.egress_proxy import Denied

        self.fleet.egress._denied(sb, host, 443, Denied(403, reason, message, "policy"))

    async def _serve(self, request: Request, sb: Sandbox, eco: str, rest: str, mode: str) -> Response:
        mounts = self.mounts(eco)
        mount = max((m for m in mounts if not m or rest == m or rest.startswith(m + "/")), key=len)
        sub = rest[len(mount):].lstrip("/") if mount else rest
        url = f"{mounts[mount]}/{sub}"
        if request.url.query:
            url += f"?{request.url.query}"
        headers = {"User-Agent": "hangar-pkgmirror", "Accept": request.headers.get("accept", "*/*")[:500]}
        if eco == "go" and not mount:
            headers.update(GO_NO_FETCH)
        if eco == "go" and mount == "sumdb/sum.golang.org":
            if sub == "supported":  # tells the go command to use this proxy for the checksum database
                return PlainTextResponse("", 200)
            if sub.startswith("lookup/") and not await self._go_cached(mounts[""], sub[7:]):
                self._deny(sb, f"{eco}.mirror.hangar", "not_cached", f"go checksum lookup for {sub[7:][:200]}, which "
                                                                     "the module proxy hasn't cached")
                return PlainTextResponse("hangar: not found in the module proxy's cache\n", 404)
        cache = self._cached(eco, rest)
        if cache is not None:
            try:
                os.utime(cache)  # a hit counts as a use: the least recently used files go first
                size = cache.stat().st_size
            except OSError:
                pass  # not cached (or evicted just now)
            else:
                self.fleet.egress.record(sb, f"{eco}.mirror.hangar", 443, f"mirror:{eco}", mode, 0, size)
                return FileResponse(cache, media_type=_media(rest))
        if _google(mounts[mount]):
            headers["Authorization"] = await self._google_auth(eco)
        client = self.gw._http()
        try:
            resp = await self._send(client, request.method, url, headers, mounts)
        except Exception as e:  # noqa: BLE001 - shown to the client, never a secret
            log.warning("package mirror: %s %s failed: %s", eco, sub[:100], type(e).__name__)
            return PlainTextResponse(f"hangar: the {eco} upstream didn't answer ({type(e).__name__})\n", 502)
        ctype = resp.headers.get("content-type", "application/octet-stream")
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() in ("etag", "last-modified")}
        if request.method == "HEAD" or resp.status_code != 200:
            await resp.aclose()
            if resp.status_code >= 500:
                return PlainTextResponse(f"hangar: the {eco} upstream answered {resp.status_code}\n", 502)
            return Response(b"", status_code=resp.status_code, media_type=ctype, headers=out_headers)
        public = f"{request.url.scheme}://{request.headers.get('host', '')}/pkg/{eco}"
        if eco != "go" and cache is None and any(t in ctype for t in _REWRITE_TYPES):
            body = bytearray()
            try:
                async for chunk in resp.aiter_bytes():
                    if self._held + len(chunk) > METADATA_HELD:  # --- review (#107)
                        return PlainTextResponse(f"hangar: the {eco} mirror is busy; retry\n", 503,
                                                 headers={"Retry-After": "2"})
                    body += chunk
                    self._held += len(chunk)
                    if len(body) > METADATA_MAX:
                        return PlainTextResponse(f"hangar: {eco} metadata over {METADATA_MAX >> 20} MB\n", 502)
                data = _rewrite(bytes(body), mounts, public)
            finally:
                self._held -= len(body)
                await resp.aclose()
            self.fleet.egress.record(sb, f"{eco}.mirror.hangar", 443, f"mirror:{eco}", mode, 0, len(data))
            return Response(data, media_type=ctype)

        async def stream() -> Any:
            n, tmp, f = 0, None, None
            if cache is not None:
                with contextlib.suppress(OSError):
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache.with_name(f".{cache.name}.{os.getpid()}.{time.monotonic_ns()}")
                    f = tmp.open("wb")
            try:
                async for chunk in resp.aiter_bytes():
                    n += len(chunk)
                    if f is not None:
                        f.write(chunk)
                    yield chunk
                if f is not None:
                    f.close()
                    f = None
                    os.replace(tmp, cache)  # type: ignore[arg-type]
                    tmp = None
                    self._added(n)
            finally:
                await resp.aclose()
                if f is not None:
                    f.close()
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                self.fleet.egress.record(sb, f"{eco}.mirror.hangar", 443, f"mirror:{eco}", mode, 0, n)

        return StreamingResponse(stream(), media_type=ctype, headers=out_headers)

    async def _send(self, client: Any, method: str, url: str, headers: dict[str, str],
                    mounts: dict[str, str]) -> Any:
        """The upstream's answer, following redirects only within the ecosystem's own upstream hosts, or to Google
        Cloud Storage, where Google's registries may send a download."""
        hosts = {urlsplit(b).hostname for b in mounts.values()}
        for _ in range(4):
            resp = await client.send(client.build_request(method, url, headers=headers), stream=True)
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            await resp.aclose()
            nxt = str(resp.url.join(resp.headers.get("location", "")))
            host = urlsplit(nxt).hostname or ""
            if urlsplit(nxt).scheme != "https" or not (host in hosts or host == "storage.googleapis.com"):
                raise PermissionError("redirect off the upstream")
            if urlsplit(nxt).hostname != urlsplit(url).hostname:
                headers = {k: v for k, v in headers.items() if k != "Authorization"}
            url = nxt
        raise PermissionError("too many redirects")

    async def _go_cached(self, proxy: str, modver: str) -> bool:
        """Whether the module proxy already has `module@version` (a checksum lookup for one it hasn't would make the
        checksum database fetch it from its origin)."""
        mod, _, ver = modver.rpartition("@")
        if not mod or not ver or not _PATH.match(modver):
            return False
        headers = {"User-Agent": "hangar-pkgmirror", **GO_NO_FETCH}
        if _google(proxy):
            headers["Authorization"] = await self._google_auth("go")
        try:
            r = await self.gw._http().get(f"{proxy}/{mod}/@v/{ver}.info", headers=headers)
        except Exception:  # noqa: BLE001
            return False
        return r.status_code == 200

    async def _google_auth(self, eco: str) -> str:
        """Artifact Registry with hangar's own Google credentials: npm takes a bearer token, the others Basic with
        the user `oauth2accesstoken` (cloud.google.com/artifact-registry docs; docs.astral.sh/uv Google guide)."""
        from hangar.registry import _GoogleToken

        if self._token is None:
            self._token = _GoogleToken()
        token = await self._token.get()
        if eco == "npm":
            return f"Bearer {token}"
        return "Basic " + base64.b64encode(f"oauth2accesstoken:{token}".encode()).decode()

    # ------------------------------------------------------------------ the disk cache
    def _cached(self, eco: str, rest: str) -> Path | None:
        if self.settings.package_mirror_cache_mb <= 0 or not _IMMUTABLE[eco].match(rest):
            return None
        h = hashlib.sha256(f"{eco}\0{rest}".encode()).hexdigest()
        return self.cache_dir / h[:2] / h

    def _added(self, n: int) -> None:
        """Count a new file; past the cap (or on the first one since start), trim the cache in a thread."""
        if self._cache_size is not None:
            self._cache_size += n
        cap = self.settings.package_mirror_cache_mb * 1024 * 1024
        if (self._cache_size is None or self._cache_size > cap) and not self._evicting:
            self._evicting = True
            asyncio.get_running_loop().run_in_executor(None, self._evict, cap)

    def _evict(self, cap: int) -> None:
        """Least recently used first (a hit touches the file), down to 90% of the cap."""
        try:
            files = []
            for p in self.cache_dir.rglob("*"):
                with contextlib.suppress(OSError):
                    if p.is_file() and not p.name.startswith("."):
                        st = p.stat()
                        files.append((st.st_mtime, st.st_size, p))
            files.sort(key=lambda x: x[0])
            total = sum(s for _, s, _ in files)
            if total > cap:
                for _, size, p in files:
                    if total <= cap * 0.9:
                        break
                    with contextlib.suppress(OSError):
                        p.unlink()
                        total -= size
            self._cache_size = total
        finally:
            self._evicting = False


def _rewrite(body: bytes, mounts: dict[str, str], public: str) -> bytes:
    """Upstream URLs in an index page or metadata, pointed at the mirror (longest upstream first)."""
    for mount, base in sorted(mounts.items(), key=lambda m: -len(m[1])):
        body = body.replace(base.encode(), (public + (f"/{mount}" if mount else "")).encode())
    return body


def _media(rest: str) -> str:
    if rest.endswith(".info"):
        return "application/json"
    return "text/plain; charset=utf-8" if rest.endswith(".mod") else "application/octet-stream"
