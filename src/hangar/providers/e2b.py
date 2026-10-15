"""E2B provider: sandboxes are microVMs on E2B's cloud, a self-hosted E2B, or Tencent CubeSandbox (docs/microvm.md).

One provider with three flavours (HANGAR_E2B_FLAVOR), because all three answer E2B's control API and run E2B's envd
in the sandbox (spec 0013 §8.2):
- cloud: E2B's cloud or BYOC; the API is https://api.<domain>.
- selfhosted: E2B's own stack (Embed) on your machines, at your domain, and at HANGAR_E2B_API_URL when the API isn't
  at api.<domain>. Hangar's own /e2b API (e2b_api.py) is one too.
- cube: CubeSandbox's CubeAPI. It serves only the v1 create and connect routes (`POST /v2/sandboxes` is a 405), and
  returns no envd access token, so its envd answers anyone who can reach it: such a sandbox is refused (and killed)
  unless HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD says the network is private.

The lifecycle goes through a thin httpx client here, the same for every flavour: the SDK posts to the v2 routes,
which CubeAPI lacks, and nothing else in it is needed. Commands, files and terminals go to the sandbox's envd through
hangar's own Connect client (envd.py), with the same output and read caps as the Docker and Kubernetes providers.
Only template builds on E2B itself use the e2b SDK.

Pause keeps the microVM's memory, and resume restores it in tens of milliseconds, so the provider declares
`hot_resume`: a suspended session's sandbox is paused rather than destroyed (hot_resume.py).
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import json
import logging
import re
import shlex
import time
from base64 import b64encode, urlsafe_b64encode
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from hangar.config import Settings
from hangar.envd import PORT, SKIP, Endpoint, Envd
from hangar.envspec import BuildContext, EnvSpec
from hangar.models import ExecResult, Sandbox, SandboxState
from hangar.providers._build import docker_build
from hangar.providers.base import Capabilities, LogFn, Provider, ProviderError, TerminalStream, Unavailable

log = logging.getLogger("hangar.e2b")

MAX_TIMEOUT = 24 * 3600
# E2B's own domains share one envd host and route by header (the SDK's sandbox_domains.py); others use a host per port.
E2B_DOMAINS = ("e2b.app", "e2b.dev", "e2b.pro", "e2b-staging.dev")
UNAVAILABLE = {429, 502, 503, 504}  # full or down: a tier may try another provider
MAX_API_BODY = 8 << 20  # one control API answer (a list page is 100 sandboxes)
RAW_UPLOADS = (0, 5, 7)  # envd versions from here take /files as a raw body (spec 0013 §3.7); older, multipart
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")  # sandbox and template ids, as they go into URL paths
_DOMAIN = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_USER: contextvars.ContextVar[str | None] = contextvars.ContextVar("hangar_e2b_user", default=None)


def _version(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split(".")[:3]:
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def _message(r: httpx.Response) -> str:
    try:
        d = r.json()
        return str(d.get("message") or d)[:500] if isinstance(d, dict) else r.text[:500]
    except ValueError:
        return r.text[:500]


class E2BApiError(ProviderError):
    def __init__(self, msg: str, status: int):
        super().__init__(msg)
        self.status = status


class E2BUnavailable(E2BApiError, Unavailable):
    pass


class ControlAPI:
    """E2B's control API, as each flavour serves it. Errors carry the API's own message, never the key."""

    def __init__(self, s: Settings):
        self.flavor = s.e2b_flavor
        self.base = (s.e2b_api_url or f"https://api.{s.e2b_domain or 'e2b.app'}").rstrip("/")
        self.deadline = s.e2b_request_timeout_seconds
        # identity: a compressed answer could inflate past MAX_API_BODY before it is counted
        headers = {"User-Agent": "hangar", "Accept-Encoding": "identity"}
        if s.e2b_api_key:
            headers["X-API-Key"] = s.e2b_api_key
        self.http = httpx.AsyncClient(base_url=self.base, headers=headers,
                                      timeout=httpx.Timeout(s.e2b_request_timeout_seconds, connect=10.0))

    async def _send(self, method: str, path: str, body: dict[str, Any] | None,
                    params: dict[str, Any] | None) -> httpx.Response:
        """One call, read up to MAX_API_BODY within one deadline: httpx's timeouts are per read, so an API that
        drips bytes (or never stops sending) would otherwise hold the caller, and its memory, without end."""
        async with asyncio.timeout(self.deadline):
            r = await self.http.send(self.http.build_request(method, path, json=body, params=params), stream=True)
            try:
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_API_BODY:
                        raise ProviderError(f"e2b ({self.flavor}) {method} {path}: the answer is over "
                                            f"{MAX_API_BODY >> 20} MiB")
            finally:
                await r.aclose()
        headers = [(k, v) for k, v in r.headers.multi_items()
                   if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        return httpx.Response(r.status_code, headers=headers, content=bytes(data), request=r.request)

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None,
                   params: dict[str, Any] | None = None, allow: tuple[int, ...] = ()) -> httpx.Response:
        try:
            r = await self._send(method, path, body, params)
        except TimeoutError as e:
            raise E2BUnavailable(f"e2b ({self.flavor}) API at {self.base}: {method} {path} took over "
                                 f"{self.deadline:g}s", 0) from e
        except httpx.HTTPError as e:
            raise E2BUnavailable(f"e2b ({self.flavor}) API at {self.base} unreachable: {e or type(e).__name__}",
                                 0) from e
        if r.is_success or r.status_code in allow:
            return r
        err = E2BUnavailable if r.status_code in UNAVAILABLE else E2BApiError
        raise err(f"e2b ({self.flavor}) {method} {path}: {r.status_code} {_message(r)}", r.status_code)

    @staticmethod
    def _id(v: str) -> str:
        if not isinstance(v, str) or not _ID.match(v):
            raise ProviderError(f"unexpected E2B id {str(v)[:80]!r}")
        return quote(v, safe="")

    @staticmethod
    def _json(r: httpx.Response) -> Any:
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError(f"e2b API: {r.request.method} {r.request.url.path} returned non-JSON") from e

    async def create(self, template: str, timeout: int, metadata: dict[str, str]) -> dict[str, Any]:
        # Private ports (deny by default): the edge wants the traffic token, which only hangar holds.
        body: dict[str, Any] = {"templateID": template, "timeout": timeout, "metadata": metadata,
                                "network": {"allowPublicTraffic": False}}
        # CubeAPI routes POST /v2/sandboxes as GET-only and answers 405 (spec 0013 §5.4): v1 there.
        path = "/sandboxes" if self.flavor == "cube" else "/v2/sandboxes"
        d = self._json(await self.call("POST", path, body))
        if not isinstance(d, dict) or not isinstance(d.get("sandboxID"), str):
            raise ProviderError("e2b API: create returned no sandboxID")
        return d

    async def connect(self, sandbox_id: str, timeout: int) -> dict[str, Any]:
        """Tokens for a sandbox, resuming it if paused (E2B: 201 when it resumed). The TTL is only extended."""
        sid = self._id(sandbox_id)
        path = f"/sandboxes/{sid}/connect" if self.flavor == "cube" else f"/v2/sandboxes/{sid}/connect"
        d = self._json(await self.call("POST", path, {"timeout": timeout}))
        if not isinstance(d, dict):
            raise ProviderError("e2b API: connect returned no sandbox")
        return d

    async def get(self, sandbox_id: str) -> dict[str, Any] | None:
        r = await self.call("GET", f"/sandboxes/{self._id(sandbox_id)}", allow=(404,))
        if r.status_code == 404:
            return None
        d = self._json(r)
        return d if isinstance(d, dict) else None

    async def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(1000):  # 100 per page; a server that never stops paging doesn't hold us forever
            params: dict[str, Any] = {"limit": 100}
            if token:
                params["nextToken"] = token
            r = await self.call("GET", "/v2/sandboxes", params=params)
            page = self._json(r)
            if not isinstance(page, list):
                raise ProviderError("e2b API: the sandbox list isn't a list")
            out += [x for x in page if isinstance(x, dict)]
            token = r.headers.get("x-next-token")
            if not token or not page:
                return out
        raise ProviderError("e2b API: the sandbox list did not end")

    async def kill(self, sandbox_id: str) -> None:
        await self.call("DELETE", f"/sandboxes/{self._id(sandbox_id)}", allow=(404,))

    async def set_timeout(self, sandbox_id: str, seconds: int) -> None:
        await self.call("POST", f"/sandboxes/{self._id(sandbox_id)}/timeout", {"timeout": seconds})

    async def pause(self, sandbox_id: str) -> None:
        """Pause with memory (E2B's default). 409 is "not running": fine if that means already paused."""
        r = await self.call("POST", f"/sandboxes/{self._id(sandbox_id)}/pause", {}, allow=(409,))
        if r.status_code == 409 and ((await self.get(sandbox_id)) or {}).get("state") != "paused":
            raise E2BApiError(f"e2b ({self.flavor}): sandbox {sandbox_id} can't be paused: {_message(r)}", 409)

    async def snapshot(self, sandbox_id: str, name: str) -> str:
        d = self._json(await self.call("POST", f"/sandboxes/{self._id(sandbox_id)}/snapshots", {"name": name}))
        sid = d.get("snapshotID") if isinstance(d, dict) else None
        if not isinstance(sid, str) or not sid:
            raise ProviderError("e2b API: the snapshot has no snapshotID")
        return sid

    async def delete_template(self, template_id: str) -> None:
        await self.call("DELETE", f"/templates/{self._id(template_id)}", allow=(404,))

    async def template_by_alias(self, alias: str) -> str | None:
        r = await self.call("GET", f"/templates/aliases/{self._id(alias)}", allow=(400, 403, 404, 405, 501))
        d = self._json(r) if r.is_success else None
        tid = d.get("templateID") if isinstance(d, dict) else None
        return tid if isinstance(tid, str) and tid else None

    async def close(self) -> None:
        await self.http.aclose()


@dataclass
class SandboxEndpoint(Endpoint):
    headers: dict[str, str] = field(default_factory=dict)


class E2BEnvd(Envd):
    """hangar's Connect client (envd.py) pointed at E2B's own envd: authenticated with E2B's X-Access-Token instead of
    hangar-envd's request signatures, and found through the control API instead of started by hangar. The token comes
    from create and connect and is kept in memory only (a restart asks for it again)."""

    def __init__(self, provider: E2BProvider, settings: Settings):
        super().__init__(provider, settings.model_copy(update={"envd": "off"}))  # nothing to install
        self.s = settings
        self.p: E2BProvider = provider

    @property
    def enabled(self) -> bool:
        return True

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            # The environment's proxy settings apply, as with the e2b SDK: E2B's cloud is often reached through one.
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                                           limits=httpx.Limits(max_connections=1024, max_keepalive_connections=128))
        return self._http

    async def endpoint(self, sb: Sandbox) -> Endpoint | None:
        if self._closed or not sb.provider_ref:
            return None
        ep = self._eps.get(sb.id)
        if ep is not None and ep.ref == sb.provider_ref:
            return ep
        try:
            ep = await self.p.lookup(sb)
        except ProviderError as e:
            log.info("e2b: no envd endpoint for %s: %s", sb.id, e)
            return None
        self._eps[sb.id] = ep
        return ep

    async def ready(self, sb: Sandbox, timeout: float = 0) -> Endpoint | None:
        return await self.endpoint(sb)

    def remember(self, sb: Sandbox, ep: SandboxEndpoint) -> None:
        self._eps[sb.id] = ep

    def _headers(self, ep: Endpoint) -> dict[str, str]:
        user = _USER.get() or self.s.sandbox_user
        h = {"Authorization": "Basic " + b64encode(f"{user}:".encode()).decode()}  # picks the user; not auth
        h.update(getattr(ep, "headers", {}))
        return h

    def request(self, ep: Endpoint, method: str, path: str, *, params: dict[str, str] | None = None,
                content: bytes = b"", headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None, upload: bool = False) -> httpx.Request:
        if path == "/files":
            params = {**(params or {}), "username": _USER.get() or self.s.sandbox_user}
            params.pop("mode", None)  # hangar-envd's; E2B's envd has none (write_file chmods)
        h = {**(headers or {}), **self._headers(ep), "Accept-Encoding": "identity"}
        return self.http.build_request(method, httpx.URL(f"{ep.url}{path}", params=params), content=content,
                                       headers=h, timeout=timeout or httpx.Timeout(30.0, connect=10.0))

    @staticmethod
    def check_proof(req: httpx.Request, resp: httpx.Response, started: bool = False) -> None:
        """E2B's envd proves nothing about itself: the access token and TLS (or a private network) are what there is."""

    async def write_file(self, sb: Sandbox, path: str, data: bytes, mode: int = 0o644) -> bool:  # type: ignore[override]
        ep = await self.endpoint(sb)
        if ep is None:
            raise ProviderError(f"sandbox {sb.id}: no envd to write {path} through")
        url = httpx.URL(f"{ep.url}/files", params={"path": path, "username": _USER.get() or self.s.sandbox_user})
        h = {**self._headers(ep), "Accept-Encoding": "identity"}
        to = httpx.Timeout(60.0 + len(data) / (1 << 20), connect=10.0)
        if _version(ep.version) >= RAW_UPLOADS:
            req = self.http.build_request("POST", url, content=data, timeout=to,
                                          headers={**h, "Content-Type": "application/octet-stream"})
        else:  # every part is "file" and its filename the full path (spec 0013 §3.5)
            req = self.http.build_request("POST", url, files={"file": (path, data)}, headers=h, timeout=to)
        try:
            status, body = await self.send_capped(req, 65536)
        except httpx.HTTPError as e:
            self._lost(sb, str(e) or type(e).__name__)
            raise ProviderError(f"write of {path} failed: {e or type(e).__name__}") from e
        if status in (401, 403):
            self._lost(sb, f"envd answered {status}")
            return False  # the token may have changed (a resume): the provider asks for it again and retries
        if status != 200:
            why = "the sandbox's disk is full" if status == 507 else body[:200].decode(errors="replace")
            raise ProviderError(f"write of {path} failed ({status}): {why}")
        return True


class E2BProvider(Provider):
    name = "e2b"

    def __init__(self, settings: Settings):
        flavor = settings.e2b_flavor
        if flavor == "cube" and not settings.e2b_api_url:
            raise ProviderError("HANGAR_E2B_FLAVOR=cube needs HANGAR_E2B_API_URL: CubeAPI's address, e.g. "
                                "http://cubeapi.internal:3000")
        if flavor == "selfhosted" and not (settings.e2b_api_url or settings.e2b_domain):
            raise ProviderError("HANGAR_E2B_FLAVOR=selfhosted needs HANGAR_E2B_DOMAIN (and HANGAR_E2B_API_URL when "
                                "the API isn't at api.<domain>)")
        if flavor != "cube" and not settings.e2b_api_key:
            raise ProviderError("set HANGAR_E2B_API_KEY to use the e2b provider")
        self.settings = settings
        self.flavor = flavor
        self.api = ControlAPI(settings)
        # Not `envd`: hangar's E2B API and sandbox router look for a hangar-envd under that name, and an e2b
        # sandbox must never be served through hangar's own E2B API (that would loop back here).
        self.dataplane = E2BEnvd(self, settings)
        self._aliases: dict[str, str] = {}

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(full_snapshots=True, warm_pools=False, pause=True, microvm=True, hot_resume=True)

    # ------------------------------------------------------------------ templates
    def _mapped(self, tag: str) -> str | None:
        env = tag.rpartition(":")[0].rsplit("/", 1)[-1] if ":" in tag else tag.rsplit("/", 1)[-1]
        return self.settings.e2b_environment_templates.get(env)

    def _base(self, tag: str):
        """Start an E2B template from our pushed image, with whatever registry auth is configured."""
        from e2b import Template

        s = self.settings
        if s.e2b_gcp_service_account_json:
            v = s.e2b_gcp_service_account_json.strip()
            # The SDK treats strings as paths, so inline JSON is parsed here and passed as a dict.
            sa = json.loads(v) if v.startswith("{") else str(Path(v).expanduser().resolve())
            return Template().from_gcp_registry(tag, sa)
        if s.e2b_aws_access_key_id and s.e2b_aws_secret_access_key:
            m = re.search(r"\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/", tag)
            return Template().from_aws_registry(tag, access_key_id=s.e2b_aws_access_key_id,
                                                secret_access_key=s.e2b_aws_secret_access_key,
                                                region=s.e2b_aws_region or (m.group(1) if m else "us-east-1"))
        if s.e2b_registry_username and s.e2b_registry_password:
            return Template().from_image(tag, username=s.e2b_registry_username, password=s.e2b_registry_password)
        if any(h in tag for h in (".pkg.dev/", "gcr.io/", ".amazonaws.com/")):
            raise ProviderError(
                f"{tag.split('/')[0]} is a private registry: set HANGAR_E2B_GCP_SERVICE_ACCOUNT_JSON (Artifact "
                "Registry/GCR) or HANGAR_E2B_AWS_* (ECR) so E2B can pull the image")
        return Template().from_image(tag)

    def image_ref(self, tag: str) -> str:
        return self._mapped(tag) or self._from_tag(tag)

    def _from_tag(self, tag: str) -> str:
        repo, _, digest = tag.rpartition(":")
        name = repo.rsplit("/", 1)[-1]
        if self.flavor == "cube":  # CubeAPI keeps only the name part of `name:tag` as the alias
            return f"hangar-{name}-{digest[:16]}".lower()
        return f"hangar-{name}:{digest}"

    async def image_exists(self, tag: str) -> bool:
        if self._mapped(tag):
            return True
        try:
            if self.flavor == "cube":
                return await self.api.template_by_alias(self._from_tag(tag)) is not None
            return await self._sdk_exists(self._from_tag(tag))
        except ProviderError:
            return False

    def _sdk_opts(self) -> dict[str, Any]:
        s = self.settings
        o: dict[str, Any] = {"api_key": s.e2b_api_key}
        if s.e2b_domain:
            o["domain"] = s.e2b_domain
        if s.e2b_api_url:
            o["api_url"] = s.e2b_api_url
        return o

    async def _sdk_exists(self, name: str) -> bool:
        try:
            from e2b import Template
        except ImportError:
            return False
        return await asyncio.to_thread(Template.exists, name, **self._sdk_opts())

    async def build_template(self, spec: EnvSpec, ctx: BuildContext, tag: str, log: LogFn | None = None) -> str:
        if mapped := self._mapped(tag):
            return mapped  # built out of band (HANGAR_E2B_ENVIRONMENT_TEMPLATES)
        if not self.settings.registry:
            raise ProviderError("set HANGAR_REGISTRY: E2B and CubeSandbox build templates from an image in a "
                                "registry they can pull (or map the environment to a template you built: "
                                "HANGAR_E2B_ENVIRONMENT_TEMPLATES)")
        await docker_build(ctx, tag, log=log, platform="linux/amd64", push=True)
        if self.flavor == "cube":
            return await self._cube_build(spec, tag, log)
        try:
            from e2b import Template
        except ImportError as e:
            raise ProviderError("building E2B templates needs the e2b extra: pip install 'hangar[e2b]'") from e
        name = self._from_tag(tag)
        tmpl = self._base(tag).set_user(self.settings.sandbox_user).set_workdir(self.settings.workspace_dir)
        loop = asyncio.get_running_loop()

        def on_log(entry) -> None:
            if log:
                r = log(f"[e2b] {getattr(entry, 'message', entry)}")
                if asyncio.iscoroutine(r):
                    asyncio.run_coroutine_threadsafe(r, loop)

        await asyncio.to_thread(Template.build, tmpl, name, cpu_count=max(1, int(spec.resources.cpus)),
                                memory_mb=int(spec.resources.memory_gb * 1024), on_build_logs=on_log,
                                **self._sdk_opts())
        return name

    async def _cube_build(self, spec: EnvSpec, tag: str, log: LogFn | None) -> str:
        """CubeAPI's image-based build (POST /templates): Cube boots the image, waits for envd's /health on 49983,
        and snapshots it. The image must start E2B's envd itself (docs/microvm.md, "Cube templates")."""
        alias = self._from_tag(tag)
        s = self.settings
        body: dict[str, Any] = {"image": tag, "name": alias, "cpu": max(1000, int(spec.resources.cpus * 1000)),
                                "memory": max(512, int(spec.resources.memory_gb * 1024)), "exposedPorts": [PORT],
                                "probePort": PORT, "probePath": "/health"}
        if spec.resources.disk_gb:
            body["writableLayerSize"] = f"{int(spec.resources.disk_gb)}G"
        if s.e2b_registry_username and s.e2b_registry_password:
            body["registryUsername"], body["registryPassword"] = s.e2b_registry_username, s.e2b_registry_password
        job = self.api._json(await self.api.call("POST", "/templates", body))
        tid, jid = (job or {}).get("templateID"), (job or {}).get("jobID")
        if not isinstance(tid, str) or not tid:
            raise ProviderError(f"CubeAPI accepted the build of {tag} but returned no templateID")
        deadline, last = time.monotonic() + s.e2b_template_build_timeout_seconds, ""
        while time.monotonic() < deadline:
            r = await self.api.call("GET", f"/templates/{self.api._id(tid)}/builds/{self.api._id(jid or tid)}/status")
            st = self.api._json(r) or {}
            status = str(st.get("status", "")).lower()
            line = f"[cube] {status} {st.get('progress', '')}% {st.get('message', '')}".strip()
            if log and line != last:
                rv = log(line)
                if asyncio.iscoroutine(rv):
                    await rv
                last = line
            if status in ("ready", "success", "succeeded", "completed"):
                self._aliases[alias] = tid
                return alias
            if status in ("failed", "failure", "error", "cancelled"):
                raise ProviderError(f"Cube template build of {tag} failed: {st.get('message') or status}")
            await asyncio.sleep(2)
        raise ProviderError(f"Cube template build of {tag} did not finish in {s.e2b_template_build_timeout_seconds}s")

    async def _template_id(self, ref: str) -> str:
        """CubeAPI creates from a template id (tpl-…); hangar names its builds by alias."""
        if self.flavor != "cube" or ref.startswith("tpl-"):
            return ref
        if ref not in self._aliases:
            self._aliases[ref] = await self.api.template_by_alias(ref) or ref
        return self._aliases[ref]

    # ------------------------------------------------------------------ lifecycle
    def _endpoint(self, sb: Sandbox, d: dict[str, Any]) -> SandboxEndpoint:
        """Where the sandbox's envd answers, and with which tokens, from a create or connect answer."""
        sid = sb.provider_ref or ""
        token = d.get("envdAccessToken")
        traffic = d.get("trafficAccessToken") or self._traffic(sb)  # review(#64): CubeAPI's connect has none
        if not token and not self.settings.e2b_allow_unauthenticated_envd:
            raise ProviderError(
                f"e2b ({self.flavor}) returned no envd access token for {sid}: its envd answers anyone who can reach "
                "it. Refusing it; on a private network, set HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD=true "
                "(docs/microvm.md, Security)")
        if not token and not traffic:
            # review(#64): the flag rests on the edge wanting a token only hangar holds; with none, the sandbox is
            # public (CubeAPI omits it when CubeMaster made the sandbox reachable by anyone).
            raise ProviderError(
                f"e2b ({self.flavor}) returned neither an envd access token nor a traffic token for {sid}: anyone "
                "who can reach its edge could run commands in it. Refusing it (docs/microvm.md, Security)")
        headers = {"E2b-Sandbox-Id": sid, "E2b-Sandbox-Port": str(PORT)}
        if token:
            headers["X-Access-Token"] = str(token)
        if traffic:
            headers["e2b-traffic-access-token"] = str(traffic)
        tpl, domain = self.settings.e2b_sandbox_url, d.get("domain") or self.settings.e2b_domain or "e2b.app"
        if not isinstance(domain, str) or not _DOMAIN.match(domain):  # the tokens go to this host: a name, nothing else
            raise ProviderError(f"e2b ({self.flavor}): unexpected sandbox domain {str(domain)[:80]!r}")
        if tpl:
            url = tpl.format(sandbox_id=sid, port=PORT) if "{sandbox_id}" in tpl else tpl
        elif domain in E2B_DOMAINS:
            url = f"https://sandbox.{domain}"
        else:
            url = f"https://{PORT}-{sid}.{domain}"
        return SandboxEndpoint(url=url.rstrip("/"), token=str(token or ""), ref=sid,
                               version=str(d.get("envdVersion") or ""), headers=headers)

    # review(#64): CubeAPI hands out the traffic token only at create (connect and resume read the sandbox back
    # without it), and CubeProxy wants it on every port of a private sandbox. So it is kept with the sandbox,
    # sealed with a key derived from hangar's own: a resume or a control plane restart can still reach envd.
    def _fernet(self):
        """--- durable: sealed with the newest vault key's, opened with any not yet retired (a key rotation)."""
        from cryptography.fernet import Fernet, MultiFernet

        from hangar import keys

        return MultiFernet([Fernet(urlsafe_b64encode(hmac.new(k, b"hangar-e2b-traffic", hashlib.sha256).digest()))
                            for k in keys.verify_keys(self.settings)])

    def _seal_traffic(self, sb: Sandbox, traffic: str) -> None:
        sb.info.setdefault("e2b", {})["traffic"] = self._fernet().encrypt(traffic.encode()).decode()

    def _traffic(self, sb: Sandbox) -> str | None:
        sealed = (sb.info.get("e2b") or {}).get("traffic")
        if not isinstance(sealed, str):
            return None
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet().decrypt(sealed.encode()).decode()
        except (InvalidToken, ValueError):
            return None

    def _ttl(self, sb: Sandbox) -> int:
        left = (sb.expires_at - time.time()) if sb.expires_at else 3600
        return int(min(MAX_TIMEOUT, max(300, left)))

    async def lookup(self, sb: Sandbox) -> SandboxEndpoint:
        """The envd endpoint of a running sandbox, asked for again (after a restart, a resume, or a refused token).
        connect() would resume a paused sandbox, so a paused one is left alone."""
        if sb.state == SandboxState.PAUSED:
            raise ProviderError(f"sandbox {sb.id} is paused; resume it first")
        return self._endpoint(sb, await self.api.connect(sb.provider_ref or "", self._ttl(sb)))

    async def create(self, sandbox: Sandbox, image: str, spec: EnvSpec | None = None) -> None:
        template = await self._template_id(self._mapped(image) or image)
        d = await self.api.create(template, self._ttl(sandbox), {"hangar_id": sandbox.id,
                                                                  "hangar_env": sandbox.environment})
        sandbox.provider_ref = d["sandboxID"]
        try:
            ControlAPI._id(sandbox.provider_ref)
            ep = self._endpoint(sandbox, d)
        except ProviderError:
            await self._kill_quietly(sandbox.provider_ref)
            raise
        sandbox.info.setdefault("e2b", {}).update({"flavor": self.flavor, "envd_version": ep.version})
        if traffic := ep.headers.get("e2b-traffic-access-token"):
            self._seal_traffic(sandbox, traffic)
        self.dataplane.remember(sandbox, ep)

    async def _kill_quietly(self, ref: str) -> None:
        try:
            await self.api.kill(ref)
        except ProviderError as e:
            log.warning("e2b: could not kill %s: %s", ref, e)

    async def status(self, sandbox: Sandbox) -> SandboxState:
        # get, not connect: connect resumes a paused sandbox.
        d = await self.api.get(sandbox.provider_ref or "")
        if d is None:
            return SandboxState.STOPPED
        return {"paused": SandboxState.PAUSED, "pausing": SandboxState.PAUSED}.get(str(d.get("state", "running")),
                                                                                   SandboxState.RUNNING)

    async def list_refs(self) -> dict[str, SandboxState] | None:
        try:
            rows = await self.api.list()
        except ProviderError as e:
            log.info("e2b: can't list sandboxes (%s); skipping reconcile", e)
            return None
        return {r["sandboxID"]: SandboxState.PAUSED if r.get("state") in ("paused", "pausing")
                else SandboxState.RUNNING
                for r in rows if isinstance(r.get("sandboxID"), str) and "hangar_id" in (r.get("metadata") or {})}

    async def set_expiry(self, sandbox: Sandbox, expires_at: float) -> None:
        if sandbox.state == SandboxState.PAUSED:
            return  # a paused sandbox has no running timeout; hangar's reaper removes it at its expiry
        await self.api.set_timeout(sandbox.provider_ref or "", int(min(MAX_TIMEOUT, max(60, expires_at - time.time()))))

    async def destroy(self, sandbox: Sandbox) -> None:
        self.dataplane.forget(sandbox)
        if sandbox.provider_ref:
            await self.api.kill(sandbox.provider_ref)

    async def pause(self, sandbox: Sandbox) -> None:
        """A memory snapshot of the microVM (E2B: pause with memory; CubeSandbox: its own snapshot)."""
        await self.api.pause(sandbox.provider_ref or "")
        self.dataplane.forget(sandbox)

    async def resume(self, sandbox: Sandbox) -> None:
        """connect resumes from the memory snapshot and hands out the tokens again (they may change)."""
        d = await self.api.connect(sandbox.provider_ref or "", self._ttl(sandbox))
        self.dataplane.remember(sandbox, self._endpoint(sandbox, d))

    # ------------------------------------------------------------------ I/O
    async def _retry(self, sandbox: Sandbox, fn, user: str | None = None, nothing: tuple = (None,)) -> Any:
        """One call through envd, then once more after asking the API for the endpoint again: the token may have
        rotated (resume) or the sandbox moved. `nothing` is what envd.py returns for "nothing happened, use another
        path" (None, or SKIP for reads), so a retry is safe."""
        if sandbox.state == SandboxState.PAUSED:
            raise ProviderError(f"sandbox {sandbox.id} is paused; resume it first")
        tok = _USER.set(user or self.settings.sandbox_user)
        try:
            for attempt in (0, 1):
                out = await fn()
                if not any(out is n for n in nothing):
                    return out
                if attempt == 0:
                    self.dataplane._lost(sandbox, "refused or unreachable")
            return out
        finally:
            _USER.reset(tok)

    async def exec(self, sandbox, command, timeout=120, cwd=None, user=None) -> ExecResult:
        # One login shell: envd.py runs `timeout … bash -lc <command>` itself (the old path wrapped it in
        # `bash -lc` and the SDK in another `bash -l -c`, so the profile ran twice per call; spec 0013 §8.2).
        r = await self._retry(sandbox, lambda: self.dataplane.exec(sandbox, command, timeout, cwd, None), user)
        if r is None:
            raise ProviderError(f"exec in {sandbox.id} failed: its envd refused the command or couldn't be reached"
                                + (f" (does {cwd} exist?)" if cwd else ""))
        return r

    async def read_file(self, sandbox, path, limit=None) -> bytes | None:
        r = await self._retry(sandbox, lambda: self.dataplane.read_file(sandbox, path, limit=limit),
                              nothing=(SKIP,))
        if r is SKIP:
            raise ProviderError(f"could not read {path} in {sandbox.id}"
                                + (" (over 64 MiB: read it with a limit)" if limit is None else ""))
        return r

    async def write_file(self, sandbox, path, data, mode=0o644) -> None:
        if not await self._retry(sandbox, lambda: self.dataplane.write_file(sandbox, path, data), nothing=(False,)):
            raise ProviderError(f"write of {path} in {sandbox.id} failed: its envd refused the access token")
        if mode != 0o644:
            await self.exec(sandbox, f"chmod {mode & 0o7777:o} {shlex.quote(path)}", timeout=30)

    async def open_terminal(self, sandbox, command, cols=120, rows=32) -> TerminalStream:
        t = await self._retry(sandbox, lambda: self.dataplane.open_terminal(sandbox, command, cols, rows))
        if t is None:
            raise ProviderError(f"could not open a terminal in {sandbox.id}")
        return t

    # ------------------------------------------------------------------ snapshots
    async def snapshot(self, sandbox, snapshot_id) -> str:
        return await self.api.snapshot(sandbox.provider_ref or "", snapshot_id)

    async def delete_snapshot(self, ref: str) -> None:
        await self.api.delete_template(ref)  # an E2B snapshot is a template

    async def close(self) -> None:
        await self.dataplane.close()
        await self.api.close()
