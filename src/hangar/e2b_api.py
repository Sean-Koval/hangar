"""E2B-compatible control API (docs/e2b.md): the e2b SDKs, and everything built on them, get hangar sandboxes.

A client changes `E2B_API_KEY` (a hangar token with the `sandboxes` scope) and `E2B_DOMAIN` or `E2B_API_URL`, and
`Sandbox.create(template=…)` starts a sandbox from a hangar environment, on hangar's provider, under hangar's
budgets, limits and audit log. Commands, files and terminals then go to the sandbox's hangar-envd daemon through
the sandbox router (sandbox_router.py), which speaks E2B's sandbox-URL scheme.

The mapping, decided in spec 0013 §8.1 and refined here:
- template -> environment (or a name in HANGAR_E2B_TEMPLATES); sandbox -> sandbox; metadata -> labels.
- `timeout` -> the sandbox's expiry, which hangar's reaper enforces. E2B's `timeout` may shorten it, `refreshes`
  only extends it, and `connect` only extends it.
- pause/connect -> the provider's pause and resume (Docker freezes the container, GKE takes a Pod snapshot): the
  memory, processes and every file survive, which is what an E2B client expects of a pause. Hangar's session
  suspend was the other candidate, but it keeps only the repo, the uncommitted work and the agent conversation, and
  boots a fresh sandbox to resume, so files and processes an E2B client left behind would be gone.
- envVars -> the sandbox's default environment, added to every process the router starts (as E2B envd's /init).

Each sandbox exposed here has a record (kv `e2b:sb:<id>`): its owner (the token id), the template name the client
used, a nonce, and its envVars encrypted with the vault key. Its two tokens are HMACs of the vault key over the
sandbox id and that nonce, so nothing secret is stored and both are the same after a restart. Neither is the
daemon's own token: the router checks them and signs its request to the daemon itself.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from hangar.auth import Principal, bearer_from_headers
from hangar.fleet import ACTIVE, FleetError
from hangar.frontdoor import Buckets
from hangar.models import Sandbox, SandboxState, now
from hangar.providers.base import ProviderError

if TYPE_CHECKING:
    from hangar.app import Hangar

log = logging.getLogger("hangar.e2b")

# The newest envd whose SDK-gated features hangar-envd fully implements (docs/envd.md, "E2B compatibility"); the
# SDKs read it from here, not from the daemon.
ENVD_VERSION = "0.5.7"
CLIENT_ID = "hangar"  # E2B's deprecated `clientID`, still a required field
RECORD = "e2b:sb:"
IDEMPOTENCY = "e2b:idem:"
IDEMPOTENCY_TTL = 24 * 3600
REFRESH_MIN, REFRESH_MAX = 15, 3600  # E2B's /refreshes bounds (seconds)
# Each fits, with the other, in the 64 KB the front door lets a request body have before its token is checked.
MAX_METADATA, MAX_ENV_VARS, MAX_MAP_BYTES = 64, 256, 24 * 1024
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TEMPLATE_UUID = uuid.UUID("6f6b2c1e-2d0c-4a8e-9a55-3b1e2b0e2b10")  # namespace for stable template build ids


class E2BError(Exception):
    """An error in E2B's shape: `{"code": <status>, "message": ...}` with that HTTP status."""

    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


def not_found(sandbox_id: str) -> E2BError:
    return E2BError(404, f"Sandbox {json.dumps(sandbox_id)} doesn't exist or you don't have access to it")


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def _int(body: dict[str, Any], key: str, default: int | None, lo: int, hi: int | None = None) -> int | None:
    v = body.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise E2BError(400, f"Error when parsing request: {key} must be an integer")
    if v < lo or (hi is not None and v > hi):
        raise E2BError(400, f"Error when parsing request: {key} must be between {lo} and {hi}" if hi is not None
                       else f"Error when parsing request: {key} must be at least {lo}")
    return v


def _strmap(v: Any, what: str, max_items: int, key_re: re.Pattern[str] | None = None) -> dict[str, str]:
    """A JSON object of strings, bounded, as E2B's metadata and envVars are."""
    if v is None:
        return {}
    if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
        raise E2BError(400, f"Error when parsing request: {what} must map strings to strings")
    if len(v) > max_items or sum(len(k) + len(x) for k, x in v.items()) > MAX_MAP_BYTES:
        raise E2BError(400, f"{what} is too large (at most {max_items} entries and {MAX_MAP_BYTES // 1024} KB)")
    for k in v:
        if not k or len(k) > 256 or "\0" in k or (key_re is not None and not key_re.match(k)):
            raise E2BError(400, f"invalid {what} key {k[:64]!r}")
    if any("\0" in x for x in v.values()):
        raise E2BError(400, f"{what} values can't contain NUL")
    return dict(v)


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        d = json.loads(raw)
    except ValueError as e:
        raise E2BError(400, f"Error when parsing request: {e}") from e
    if not isinstance(d, dict):
        raise E2BError(400, "Error when parsing request: the body must be a JSON object")
    return d


class E2BService:
    """Sandboxes as E2B sees them: records and tokens, ownership, limits, and the lifecycle calls."""

    def __init__(self, h: Hangar):
        self.h, self.fleet, self.store, self.settings = h, h.fleet, h.store, h.settings
        self.buckets = Buckets()
        self._key: bytes | None = None
        self._records: dict[str, dict[str, Any]] = {}
        self._envs: dict[str, dict[str, str]] = {}
        self._creating: collections.Counter[str] = collections.Counter()
        self._idem_busy: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._ports_http: httpx.AsyncClient | None = None

    @property
    def ports_http(self) -> httpx.AsyncClient:
        """The sandbox router's client for sandboxes' own ports (envd calls use the provider's, which signs)."""
        if self._ports_http is None:
            self._ports_http = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(None, connect=5.0),
                                                 limits=httpx.Limits(max_connections=1024,
                                                                     max_keepalive_connections=64))
        return self._ports_http

    async def close(self) -> None:
        if self._ports_http is not None:
            await self._ports_http.aclose()
            self._ports_http = None

    # ------------------------------------------------------------------ records and tokens
    def _mac(self, label: bytes, sandbox_id: str, nonce: str) -> str:
        if self._key is None:
            from hangar.vault import load_or_create_key

            self._key = load_or_create_key(self.settings.secret_key, self.settings.data_dir)
        return hmac.new(self._key, label + b"\0" + sandbox_id.encode() + b"\0" + nonce.encode(),
                        hashlib.sha256).hexdigest()

    def envd_token(self, sandbox_id: str, rec: dict[str, Any]) -> str:
        """E2B's `envdAccessToken` (X-Access-Token), checked by the router; never the daemon's own token."""
        return self._mac(b"hangar-e2b-envd", sandbox_id, rec["nonce"])

    def traffic_token(self, sandbox_id: str, rec: dict[str, Any]) -> str:
        """E2B's `trafficAccessToken` (e2b-traffic-access-token): the sandbox's other ports."""
        return self._mac(b"hangar-e2b-traffic", sandbox_id, rec["nonce"])

    def record(self, sandbox_id: str) -> dict[str, Any] | None:
        rec = self._records.get(sandbox_id)
        if rec is None:
            rec = self.store.kv_get_json(RECORD + sandbox_id)
            if isinstance(rec, dict) and isinstance(rec.get("nonce"), str):
                self._records[sandbox_id] = rec
            else:
                rec = None
        return rec

    def _save(self, sandbox_id: str, rec: dict[str, Any]) -> dict[str, Any]:
        self.store.kv_set_json(RECORD + sandbox_id, rec)
        self._records[sandbox_id] = rec
        return rec

    def adopt(self, sb: Sandbox, owner: str) -> dict[str, Any]:
        """The record of a sandbox hangar started some other way (a job's, a session's), made the first time an
        admin reaches it through this API."""
        return self.record(sb.id) or self._save(sb.id, {"owner": owner, "nonce": secrets.token_hex(16),
                                                        "template": sb.environment})

    def forget(self, sandbox_id: str) -> None:
        self.store.kv_delete(RECORD + sandbox_id)
        self._records.pop(sandbox_id, None)
        self._envs.pop(sandbox_id, None)
        self._locks.pop(sandbox_id, None)

    def env_vars(self, sandbox_id: str, rec: dict[str, Any]) -> dict[str, str]:
        """The sandbox's envVars, decrypted once and cached."""
        if not rec.get("env"):
            return {}
        env = self._envs.get(sandbox_id)
        if env is None:
            env = json.loads(self.h.vault.decrypt(rec["env"].encode()))
            self._envs[sandbox_id] = env
        return env

    def check_envd(self, sandbox_id: str, rec: dict[str, Any], given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.envd_token(sandbox_id, rec).encode())

    def check_traffic(self, sandbox_id: str, rec: dict[str, Any], given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.traffic_token(sandbox_id, rec).encode())

    def owner_live(self, rec: dict[str, Any]) -> bool:
        """Whether the hangar token that made the sandbox (or an admin's adopted it) still exists: revoking a token
        revokes its sandboxes' tokens too."""
        return self.h.auth.exists(rec.get("owner"))

    # ------------------------------------------------------------------ callers
    def principal(self, request: Request) -> Principal:
        raw = request.headers.get("x-api-key") or bearer_from_headers(request.headers)
        p = self.h.auth.principal(raw)
        if p is None:
            raise E2BError(401, "Invalid API key: send a hangar token with the sandboxes scope as X-API-Key")
        if not (p.admin or "sandboxes" in p.scopes):
            raise E2BError(403, f"token scopes {p.scopes} do not allow the E2B API (it needs the sandboxes scope)")
        if not p.admin:
            self._take(p, "requests", self.settings.e2b_rate_per_minute, self.settings.e2b_rate_burst)
        return p

    def _take(self, p: Principal, kind: str, per_minute: float, burst: int) -> None:
        wait = self.buckets.take(f"{kind}:{p.id}", per_minute, burst)
        if wait:
            raise E2BError(429, f"Rate limit exceeded: too many {kind} from token {p.name}; try again in "
                                f"{math.ceil(wait)}s", {"Retry-After": str(max(1, math.ceil(wait)))})

    @staticmethod
    def actor(p: Principal) -> str:
        return f"e2b:{p.name}"

    def owned(self, p: Principal, sandbox_id: str) -> tuple[Sandbox, dict[str, Any]]:
        """A live sandbox the caller may use, with its record. Anything else is the same 404, so a token can't tell
        another owner's sandbox from one that doesn't exist."""
        sb = self.store.get(Sandbox, sandbox_id)
        if sb is None or sb.state not in ACTIVE:
            raise not_found(sandbox_id)
        rec = self.record(sb.id)
        if p.admin:
            return sb, rec or self.adopt(sb, p.id)
        if rec is None or rec.get("owner") != p.id:
            raise not_found(sandbox_id)
        return sb, rec

    def visible(self, p: Principal) -> list[tuple[Sandbox, dict[str, Any] | None]]:
        out = []
        for sb in self.store.list(Sandbox):
            if sb.state not in ACTIVE:
                continue
            rec = self.record(sb.id)
            if p.admin or (rec is not None and rec.get("owner") == p.id):
                out.append((sb, rec))
        return out

    def prune(self) -> None:
        """Drop the records of sandboxes that are gone (the reaper and other callers destroy without telling us),
        and idempotency keys past their day."""
        for key in self.store.kv_keys(RECORD):
            sid = key[len(RECORD):]
            sb = self.store.get(Sandbox, sid)
            if sb is None or sb.state not in ACTIVE:
                self.forget(sid)
        t = now()
        for key in self.store.kv_keys(IDEMPOTENCY):
            v = self.store.kv_get_json(key)
            if not isinstance(v, dict) or v.get("exp", 0) < t:
                self.store.kv_delete(key)

    # ------------------------------------------------------------------ views
    def domain(self) -> str | None:
        return (self.settings.e2b_api_domain or "").strip(".").lower() or None

    def sandbox_view(self, sb: Sandbox, rec: dict[str, Any]) -> dict[str, Any]:
        """E2B's `Sandbox`: what create and connect return, tokens included."""
        return {"templateID": sb.environment, "sandboxID": sb.id, "alias": rec.get("template") or sb.environment,
                "clientID": CLIENT_ID, "envdVersion": ENVD_VERSION, "envdAccessToken": self.envd_token(sb.id, rec),
                "trafficAccessToken": self.traffic_token(sb.id, rec), "domain": self.domain()}

    def listed_view(self, sb: Sandbox, rec: dict[str, Any] | None) -> dict[str, Any]:
        r = sb.resources
        end = sb.expires_at or sb.created_at + 100 * 365 * 86400  # no expiry: hangar keeps it until it's killed
        return {"templateID": sb.environment, "alias": (rec or {}).get("template") or sb.environment,
                "sandboxID": sb.id, "clientID": CLIENT_ID, "startedAt": iso(sb.created_at), "endAt": iso(end),
                "cpuCount": max(1, math.ceil(r.cpus)), "memoryMB": max(128, int(r.memory_gb * 1024)),
                "diskSizeMB": int((r.disk_gb or 0) * 1024), "metadata": dict(sb.labels),
                "state": "paused" if sb.state == SandboxState.PAUSED else "running", "envdVersion": ENVD_VERSION}

    def detail_view(self, sb: Sandbox, rec: dict[str, Any]) -> dict[str, Any]:
        d = self.listed_view(sb, rec)
        d.update({"envdAccessToken": self.envd_token(sb.id, rec), "domain": self.domain(),
                  "allowInternetAccess": None, "network": {"allowPublicTraffic": False},
                  "lifecycle": {"autoResume": False, "onTimeout": "kill"}})
        if sb.state == SandboxState.PAUSED:
            d["pauseMode"] = "full"
        return d

    # ------------------------------------------------------------------ templates
    def environment(self, template: str) -> str:
        name = self.settings.e2b_templates.get(template, template)
        self.fleet.catalog.reload()
        if name not in self.fleet.catalog.names():
            raise E2BError(404, f"template {template!r} not found")
        return name

    def template_view(self, e: dict[str, Any]) -> dict[str, Any]:
        name, res = e["name"], e.get("resources") or {}
        built = bool(e.get("images"))
        aliases = [name, *sorted(k for k, v in self.settings.e2b_templates.items() if v == name and k != name)]
        t = iso(0)
        build = uuid.uuid5(_TEMPLATE_UUID, f"{name}:{sorted((e.get('images') or {}).items())}")
        return {"templateID": name, "buildID": str(build),
                "cpuCount": max(1, math.ceil(res.get("cpus") or 1)),
                "memoryMB": max(128, int((res.get("memory_gb") or 1) * 1024)),
                "diskSizeMB": int((res.get("disk_gb") or 0) * 1024), "public": False, "aliases": aliases,
                "names": aliases, "createdAt": t, "updatedAt": t, "createdBy": None, "lastSpawnedAt": None,
                "spawnCount": 0, "buildCount": 1 if built else 0, "envdVersion": ENVD_VERSION,
                "buildStatus": "ready"}

    def templates(self) -> list[dict[str, Any]]:
        return [self.template_view(e) for e in self.fleet.environments() if "error" not in e]

    # ------------------------------------------------------------------ lifecycle
    def _cap_timeout(self, p: Principal, timeout: int) -> int:
        limit = self.settings.e2b_max_timeout_seconds
        if p.max_budget and p.max_budget.max_minutes:
            limit = min(limit, p.max_budget.max_minutes * 60)  # a token's budget caps how long its sandboxes live
        if timeout > limit:
            raise E2BError(400, f"Timeout cannot be greater than {limit} seconds for this token")
        return timeout

    @staticmethod
    def _within_budget(p: Principal, sb: Sandbox, until: float) -> float:
        """A token's budget caps how long its sandboxes live, from their start, not just each timeout it asks for:
        refreshes, timeouts and connects can't keep one going past it."""
        if p.max_budget and p.max_budget.max_minutes:
            return min(until, sb.created_at + p.max_budget.max_minutes * 60)
        return until

    async def _set_expiry(self, sb_id: str, expires_at: float | None) -> Sandbox:
        sb = self.store.get(Sandbox, sb_id)
        if sb is None:
            raise not_found(sb_id)
        sb.expires_at = expires_at
        self.store.put(sb)
        self.fleet.gateway.extend(sb)
        prov = self.fleet.providers.get(sb.provider)
        if prov is not None and expires_at is not None:
            with contextlib.suppress(Exception):  # the backend's own timeout is best effort; the reaper enforces
                await prov.set_expiry(sb, expires_at)
        return sb

    def _check_body(self, body: dict[str, Any]) -> None:
        """Refuse what hangar can't do, rather than quietly doing less (a client asking for no internet, say)."""
        if body.get("autoPause") is True or (body.get("autoResume") or {}).get("enabled") is True:
            raise E2BError(400, "autoPause and autoResume aren't supported: a hangar sandbox is killed when its "
                                "timeout ends (pause it yourself and connect to resume)")
        if body.get("allow_internet_access") is False:
            raise E2BError(400, "allow_internet_access=false isn't supported per sandbox; use an environment whose "
                                "egress is locked down")
        net = body.get("network")
        if net is not None:
            if not isinstance(net, dict):
                raise E2BError(400, "Error when parsing request: network must be an object")
            if net.get("allowPublicTraffic") is True:
                raise E2BError(400, "hangar keeps sandbox ports private: allowPublicTraffic must be false (send "
                                    "the trafficAccessToken as the e2b-traffic-access-token header)")
            extra = sorted(k for k, v in net.items() if k != "allowPublicTraffic" and v not in (None, [], {}, ""))
            if extra:
                raise E2BError(400, f"network {', '.join(extra)} isn't supported yet (docs/e2b.md)")
        for k in ("mcp", "iam", "volumeMounts"):
            if body.get(k):
                raise E2BError(400, f"{k} isn't supported by hangar (docs/e2b.md)")

    async def create(self, p: Principal, body: dict[str, Any], v2: bool, idem_key: str | None,
                     raw: bytes) -> dict[str, Any]:
        template = body.get("templateID")
        if not isinstance(template, str) or not template or len(template) > 256:
            raise E2BError(400, "Error when parsing request: templateID is required")
        timeout = _int(body, "timeout", 300 if v2 else 15, 1 if v2 else 0) or 15  # v1 allowed 0: its default
        timeout = self._cap_timeout(p, timeout)
        self._check_body(body)
        metadata = _strmap(body.get("metadata"), "metadata", MAX_METADATA)
        if any(k.startswith("hangar.") for k in metadata):
            raise E2BError(400, "metadata keys starting with 'hangar.' are reserved")
        env_vars = _strmap(body.get("envVars"), "envVars", MAX_ENV_VARS, _ENV_KEY)
        env = self.environment(template)
        spec = self.fleet.catalog.get(env)
        trusted = p.admin or env in self.settings.e2b_trusted_environments
        if not trusted and (spec.secrets or spec.credential_mode == "gateway"):
            raise E2BError(403, f"environment {env} hands its sandboxes secrets or a git grant; a sandboxes token "
                                "can use it only if it is in HANGAR_E2B_TRUSTED_ENVIRONMENTS")
        try:
            prov = self.fleet.provider(None)
        except FleetError as e:
            raise E2BError(500, str(e)) from e
        envd = getattr(prov, "envd", None)
        if envd is None or not envd.enabled:
            raise E2BError(500, f"the {prov.name} provider has no hangar-envd, which E2B clients talk to "
                                "(docs/envd.md: build it with envd/build.sh or set HANGAR_ENVD_DIR)")

        idem = None
        if idem_key:
            if len(idem_key) > 256:
                raise E2BError(400, "Idempotency-Key is too long")
            idem = f"{IDEMPOTENCY}{p.id}:{hashlib.sha256(idem_key.encode()).hexdigest()}"
            if idem in self._idem_busy:
                raise E2BError(409, "a request with this Idempotency-Key is still in progress")
            prior = self.store.kv_get_json(idem)
            if isinstance(prior, dict) and prior.get("exp", 0) > now():
                if prior.get("body") != hashlib.sha256(raw).hexdigest():
                    raise E2BError(422, "this Idempotency-Key was used with a different request")
                sb, rec = self.owned(p, prior["sandbox"])
                return self.sandbox_view(sb, rec)
        if not p.admin:
            cap = self.settings.e2b_max_sandboxes_per_owner
            if cap > 0 and len(self.visible(p)) + self._creating[p.id] >= cap:
                raise E2BError(429, f"You have reached the maximum number of concurrent sandboxes ({cap}) for "
                                    f"token {p.name}; kill one first")
            self._take(p, "sandbox creations", self.settings.e2b_create_per_minute, self.settings.e2b_create_burst)

        if idem:
            self._idem_busy.add(idem)
        self._creating[p.id] += 1
        try:
            try:
                # The reaper must not take a sandbox whose image is still building, so the expiry is set exactly
                # once it runs.
                sb = await self.fleet.create(env, name=f"e2b-{env}", labels=metadata, actor=self.actor(p),
                                             ttl_minutes=max(60, math.ceil(timeout / 60)),
                                             credentials=None if trusted else [])
            except FleetError as e:
                if "max_sandboxes" in str(e):
                    raise E2BError(429, f"hangar is at its sandbox limit: {e}") from e
                raise E2BError(500, f"Failed to create sandbox: {e}") from e
            try:
                rec: dict[str, Any] = {"owner": p.id, "nonce": secrets.token_hex(16), "template": template}
                if env_vars:
                    rec["env"] = self.h.vault.encrypt(json.dumps(env_vars).encode()).decode()
                self._save(sb.id, rec)
                sb = await self._set_expiry(sb.id, now() + timeout)
                if await envd.ready(sb, timeout=60) is None:
                    raise E2BError(500, f"hangar-envd did not start in sandbox {sb.id}; it was removed (the "
                                        "control plane log says why)")
            except BaseException:  # nobody will get this sandbox's tokens: don't leave it running
                await asyncio.shield(self._discard(sb.id, p))
                raise
            if idem:
                self.store.kv_set_json(idem, {"sandbox": sb.id, "body": hashlib.sha256(raw).hexdigest(),
                                              "exp": now() + IDEMPOTENCY_TTL})
            return self.sandbox_view(sb, rec)
        finally:
            self._creating[p.id] -= 1
            if self._creating[p.id] <= 0:
                del self._creating[p.id]
            if idem:
                self._idem_busy.discard(idem)

    async def _discard(self, sandbox_id: str, p: Principal) -> None:
        try:
            await self.fleet.destroy(sandbox_id, actor=self.actor(p))
        except Exception:  # noqa: BLE001 - the reaper takes it at its expiry
            log.exception("could not remove sandbox %s after a failed create", sandbox_id)
        self.forget(sandbox_id)

    async def kill(self, p: Principal, sandbox_id: str) -> None:
        async with self._locks[sandbox_id]:
            sb, _ = self.owned(p, sandbox_id)
            await self.fleet.destroy(sb.id, actor=self.actor(p))
            self.forget(sb.id)

    async def set_timeout(self, p: Principal, sandbox_id: str, timeout: int) -> None:
        async with self._locks[sandbox_id]:
            sb, _ = self.owned(p, sandbox_id)
            if sb.state == SandboxState.PAUSED:
                raise not_found(sandbox_id)  # as E2B: only a running sandbox has a timeout to set
            await self._set_expiry(sb.id, self._within_budget(p, sb, now() + self._cap_timeout(p, max(0, timeout))))
            self.fleet.audit(self.actor(p), "sandbox.timeout", sb.id, seconds=timeout)

    async def refresh(self, p: Principal, sandbox_id: str, duration: int) -> None:
        async with self._locks[sandbox_id]:
            sb, _ = self.owned(p, sandbox_id)
            if sb.state == SandboxState.PAUSED:
                raise not_found(sandbox_id)
            until = self._within_budget(p, sb, now() + max(REFRESH_MIN, duration))
            if sb.expires_at is not None and sb.expires_at < until:
                await self._set_expiry(sb.id, until)

    async def pause(self, p: Principal, sandbox_id: str) -> None:
        async with self._locks[sandbox_id]:
            sb, _ = self.owned(p, sandbox_id)
            if sb.state != SandboxState.RUNNING:
                raise E2BError(409, f"Sandbox {json.dumps(sb.id)} cannot be paused while in '{sb.state.value}' state")
            prov = self.fleet.provider(sb.provider)
            if not prov.capabilities.pause:
                raise E2BError(400, f"the {prov.name} provider can't pause sandboxes (on Kubernetes, pausing needs "
                                    "GKE Pod snapshots: HANGAR_K8S_POD_SNAPSHOTS)")
            try:
                await self.fleet.pause(sb.id, actor=self.actor(p))
            except (ProviderError, FleetError) as e:
                raise E2BError(500, f"Error pausing sandbox: {e}") from e
            self._forget_address(sb)
            # Paused sandboxes don't run out (E2B's semantics), but they hold the provider's resources, so hangar
            # keeps one for the longest timeout before removing it.
            await self._set_expiry(sb.id, self._within_budget(p, sb, now() + self.settings.e2b_max_timeout_seconds))

    def _forget_address(self, sb: Sandbox) -> None:
        """Paused or resumed, a sandbox may come back at another address (GKE restores a Pod snapshot as a new Pod):
        its daemon is found and proved again on the next call rather than looked for where it was."""
        envd = getattr(self.fleet.providers.get(sb.provider), "envd", None)
        if envd is not None and hasattr(envd, "_lost"):
            envd._lost(sb, "paused or resumed")

    async def connect(self, p: Principal, sandbox_id: str, timeout: int, memory: bool | None) -> tuple[int, dict]:
        """200 and the tokens for a running sandbox (its expiry only ever extended), 201 after resuming a paused one."""
        timeout = self._cap_timeout(p, timeout)
        async with self._locks[sandbox_id]:
            sb, rec = self.owned(p, sandbox_id)
            prov = self.fleet.providers.get(sb.provider)
            envd = getattr(prov, "envd", None)
            if envd is None or not envd.enabled:
                raise E2BError(400, f"sandbox {sb.id} runs on the {sb.provider} provider, which has no hangar-envd")
            if sb.state in (SandboxState.PENDING, SandboxState.STARTING):
                raise E2BError(409, f"Sandbox {json.dumps(sb.id)} is still starting")
            if sb.state == SandboxState.PAUSED:
                if memory is False:
                    raise E2BError(400, "resuming from the filesystem alone (memory=false) isn't available: a "
                                        "paused hangar sandbox always resumes with its memory")
                try:
                    await self.fleet.resume(sb.id, actor=self.actor(p))
                except (ProviderError, FleetError) as e:
                    raise E2BError(500, f"Error resuming sandbox: {e}") from e
                self._forget_address(sb)
                sb = await self._set_expiry(sb.id, self._within_budget(p, sb, now() + timeout))
                await envd.ready(sb, timeout=30)
                return 201, self.sandbox_view(sb, rec)
            until = self._within_budget(p, sb, now() + timeout)
            if sb.expires_at is not None and sb.expires_at < until:
                sb = await self._set_expiry(sb.id, until)
            return 200, self.sandbox_view(sb, rec)

    def list(self, p: Principal, q: dict[str, str], v2: bool) -> tuple[list[dict[str, Any]], str | None]:
        self.prune()
        rows = self.visible(p)
        if meta := q.get("metadata"):
            want = dict(parse_qsl(meta, keep_blank_values=True))
            rows = [(sb, r) for sb, r in rows if all(sb.labels.get(k) == v for k, v in want.items())]
        if not v2:
            return [self.listed_view(sb, r) for sb, r in rows if sb.state != SandboxState.PAUSED], None
        states = {s for s in (q.get("state") or "running,paused").split(",") if s}
        if states - {"running", "paused"}:
            raise E2BError(400, f"invalid state filter {q.get('state')!r}: running, paused")
        rows = [(sb, r) for sb, r in rows if ("paused" if sb.state == SandboxState.PAUSED else "running") in states]
        if tpl := q.get("template"):
            rows = [(sb, r) for sb, r in rows if tpl in (sb.environment, (r or {}).get("template"))]
        if after := q.get("startedAfter"):
            try:
                t = datetime.fromisoformat(after).timestamp()
            except ValueError as e:
                raise E2BError(400, f"invalid startedAfter {after!r}") from e
            rows = [(sb, r) for sb, r in rows if sb.created_at > t]
        order = q.get("order") or "desc"
        if order not in ("asc", "desc"):
            raise E2BError(400, "order must be asc or desc")
        rows.sort(key=lambda x: (x[0].created_at, x[0].id), reverse=order == "desc")
        try:
            limit = int(q.get("limit") or 100)
        except ValueError as e:
            raise E2BError(400, "limit must be an integer") from e
        if not 1 <= limit <= 100:
            raise E2BError(400, "limit must be between 1 and 100")
        start = 0
        if token := q.get("nextToken"):
            # A position (the next page's first sandbox's start time and id), not a sandbox that must still exist.
            t, _, sid = token.partition("_")
            try:
                at = (float(t), sid)
            except ValueError as e:
                raise E2BError(400, "invalid nextToken") from e
            desc = order == "desc"
            start = next((i for i, (sb, _) in enumerate(rows)
                          if ((sb.created_at, sb.id) <= at if desc else (sb.created_at, sb.id) >= at)), len(rows))
        page = rows[start:start + limit]
        nxt = None
        if len(rows) > start + limit:
            first = rows[start + limit][0]
            nxt = f"{first.created_at!r}_{first.id}"
        return [self.listed_view(sb, r) for sb, r in page], nxt

    def logs(self, p: Principal, sandbox_id: str, start_ms: int | None, limit: int, search: str | None,
             backward: bool) -> list[dict[str, Any]]:
        """The sandbox's entries in hangar's audit log (create, exec, pause...): what hangar knows happened to it.
        Output of the sandbox's own processes isn't collected."""
        sb, _ = self.owned(p, sandbox_id)
        out = []
        for ev in reversed(self.store.audit_tail(5000)):
            if ev.target != sb.id or (start_ms is not None and ev.ts * 1000 < start_ms):
                continue
            msg = f"{ev.action} by {ev.actor}"
            if search and search not in msg:
                continue
            fields = {"actor": ev.actor, "action": ev.action}
            fields.update({k: str(v)[:500] for k, v in ev.detail.items()})
            out.append({"timestamp": iso(ev.ts), "level": "info", "message": msg, "fields": fields})
        return (out[::-1] if backward else out)[:limit]


# ====================================================================== the API
def build_api(svc: E2BService) -> FastAPI:
    """The control API as an ASGI app, mounted at HANGAR_E2B_API_PATH. Errors are E2B's shape throughout."""
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @api.exception_handler(E2BError)
    async def _e2b_error(_: Request, e: E2BError):
        return JSONResponse({"code": e.status, "message": e.message}, status_code=e.status, headers=e.headers)

    @api.exception_handler(RequestValidationError)
    async def _bad_request(_: Request, e: RequestValidationError):
        msg = "; ".join(f"{'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg')}" for err in e.errors())
        return JSONResponse({"code": 400, "message": f"Error when parsing request: {msg}"}, status_code=400)

    @api.exception_handler(FleetError)
    @api.exception_handler(ProviderError)
    async def _fleet_error(_: Request, e: Exception):
        return JSONResponse({"code": 500, "message": str(e)}, status_code=500)

    @api.get("/health")
    async def health():
        return PlainTextResponse("Health check successful")

    # ------------------------------------------------------------------ sandboxes
    async def _create(request: Request, v2: bool):
        p = svc.principal(request)
        raw = await request.body()
        body = await _json_body(request)
        return JSONResponse(await svc.create(p, body, v2, request.headers.get("idempotency-key"), raw),
                            status_code=201)

    @api.post("/v2/sandboxes")
    async def create_v2(request: Request):
        return await _create(request, True)

    @api.post("/sandboxes")
    async def create_v1(request: Request):
        return await _create(request, False)

    @api.get("/v2/sandboxes")
    async def list_v2(request: Request):
        rows, nxt = svc.list(svc.principal(request), dict(request.query_params), True)
        return JSONResponse(rows, headers={"x-next-token": nxt} if nxt else None)

    @api.get("/sandboxes")
    async def list_v1(request: Request):
        return svc.list(svc.principal(request), dict(request.query_params), False)[0]

    @api.get("/sandboxes/metrics")
    async def metrics_many(request: Request):
        svc.principal(request)
        return {"sandboxes": {}}  # not collected (docs/e2b.md)

    @api.get("/sandboxes/{sandbox_id}")
    async def get(sandbox_id: str, request: Request):
        sb, rec = svc.owned(svc.principal(request), sandbox_id)
        return svc.detail_view(sb, rec)

    @api.delete("/sandboxes/{sandbox_id}")
    async def kill(sandbox_id: str, request: Request):
        await svc.kill(svc.principal(request), sandbox_id)
        return Response(status_code=204)

    @api.post("/sandboxes/{sandbox_id}/timeout")
    async def timeout(sandbox_id: str, request: Request):
        p = svc.principal(request)
        body = await _json_body(request)
        if "timeout" not in body:
            raise E2BError(400, "Error when parsing request: timeout is required")
        t = _int(body, "timeout", None, -(2**31))
        await svc.set_timeout(p, sandbox_id, t or 0)
        return Response(status_code=204)

    @api.post("/sandboxes/{sandbox_id}/refreshes")
    async def refreshes(sandbox_id: str, request: Request):
        p = svc.principal(request)
        d = _int(await _json_body(request), "duration", REFRESH_MIN, 0, REFRESH_MAX)
        await svc.refresh(p, sandbox_id, d or REFRESH_MIN)
        return Response(status_code=204)

    @api.post("/sandboxes/{sandbox_id}/pause")
    async def pause(sandbox_id: str, request: Request):
        p = svc.principal(request)
        await _json_body(request)  # {memory}: hangar's pause always keeps the memory (see the module docstring)
        await svc.pause(p, sandbox_id)
        return Response(status_code=204)

    async def _connect(sandbox_id: str, request: Request, default_timeout: int):
        p = svc.principal(request)
        body = await _json_body(request)
        t = _int(body, "timeout", default_timeout, 0) or default_timeout
        memory = body.get("memory")
        if memory is not None and not isinstance(memory, bool):
            raise E2BError(400, "Error when parsing request: memory must be a boolean")
        status, view = await svc.connect(p, sandbox_id, t, memory)
        return JSONResponse(view, status_code=status)

    @api.post("/v2/sandboxes/{sandbox_id}/connect")
    async def connect_v2(sandbox_id: str, request: Request):
        return await _connect(sandbox_id, request, 300)

    @api.post("/sandboxes/{sandbox_id}/connect")
    async def connect_v1(sandbox_id: str, request: Request):
        return await _connect(sandbox_id, request, 300)

    @api.post("/sandboxes/{sandbox_id}/resume")
    async def resume_v1(sandbox_id: str, request: Request):
        resp = await _connect(sandbox_id, request, 15)
        if resp.status_code == 200:  # the deprecated /resume only resumes
            raise E2BError(409, f"Sandbox {json.dumps(sandbox_id)} is already running")
        return resp

    @api.get("/sandboxes/{sandbox_id}/metrics")
    async def metrics(sandbox_id: str, request: Request):
        svc.owned(svc.principal(request), sandbox_id)
        return []  # not collected (docs/e2b.md)

    @api.get("/v2/sandboxes/{sandbox_id}/logs")
    async def logs_v2(sandbox_id: str, request: Request, cursor: int | None = None, limit: int = 1000,
                      direction: str = "forward", search: str | None = None):
        p = svc.principal(request)
        limit = max(1, min(limit, 1000))
        return {"logs": svc.logs(p, sandbox_id, cursor, limit, search, direction == "backward")}

    @api.get("/sandboxes/{sandbox_id}/logs")
    async def logs_v1(sandbox_id: str, request: Request, start: int | None = None, limit: int = 1000):
        entries = svc.logs(svc.principal(request), sandbox_id, start, max(1, min(limit, 1000)), None, False)
        return {"logs": [{"timestamp": e["timestamp"], "line": e["message"]} for e in entries], "logEntries": entries}

    # ------------------------------------------------------------------ templates (environments)
    @api.get("/templates")
    @api.get("/v2/templates")
    async def templates(request: Request):
        svc.principal(request)
        return svc.templates()

    @api.get("/templates/{template_id}")
    async def template(template_id: str, request: Request):
        svc.principal(request)
        name = svc.environment(template_id)
        t = next((x for x in svc.templates() if x["templateID"] == name), None)
        if t is None:
            raise E2BError(404, f"template {template_id!r} not found")
        build = {"buildID": t["buildID"], "status": t["buildStatus"], "createdAt": t["createdAt"],
                 "updatedAt": t["updatedAt"], "cpuCount": t["cpuCount"], "memoryMB": t["memoryMB"],
                 "diskSizeMB": t["diskSizeMB"], "envdVersion": ENVD_VERSION}
        return {k: t[k] for k in ("templateID", "public", "aliases", "names", "createdAt", "updatedAt",
                                  "lastSpawnedAt", "spawnCount")} | {"builds": [build]}

    # ------------------------------------------------------------------ everything else
    @api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def unsupported(path: str, request: Request):
        svc.principal(request)
        raise E2BError(501, f"{request.method} /{path} isn't supported by hangar's E2B API: fork, snapshots, "
                            "network updates, template builds, volumes, secrets and API keys aren't (docs/e2b.md)")

    return api
