"""Front-door limits shared by every way work enters hangar (REST, MCP, A2A, GitHub, Slack, webhooks, schedules).

- Admission: per-caller and per-source token buckets on job submissions and follow-ups, a cap on each caller's
  unfinished jobs, and an audit entry for every refusal (itself rate limited, so a flood can't fill the audit log).
- Replays: signed deliveries are remembered in the store, so a captured request can't be sent again, even after a
  restart. A signature without a signed timestamp can be replayed forever, so the window is long.
- Outbound: callbacks and push notifications connect to the address that was checked, never to whatever the
  name resolves to a moment later (DNS rebinding).
- Bodies: a request body is capped before anything parses it, and an anonymous one is capped small, because
  FastAPI reads JSON bodies before it runs the auth dependency.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import math
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING, Any

import httpcore
import httpx
from pydantic import ValidationError

from hangar.jobs import Caller, JobError, SubmitJob, _internal
from hangar.models import JOB_TERMINAL, AuditEvent

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.store import Store

ANON_MAX_BODY = 64 * 1024  # an unauthenticated request: a token exchange or consent form is far smaller
# --- overnight/sweep: buffered before the signature is checked, so anyone can send it. GitHub's cap is 25 MB, but
# hangar acts only on issues and issue_comment, whose bodies are at most 65,536 characters each: well under 1 MB
# with the repository and user objects. A bigger delivery (a push of many refs) is one hangar ignores anyway.
GITHUB_MAX_BODY = 2 * 1024 * 1024
_PRUNE_SECONDS = 600
_MAX_BUCKETS = 10_000  # rate-limit buckets kept per Buckets (callers or client addresses)
_EXEMPT_SOURCES = {"api", "mcp", "a2a"}  # ...when the caller is an admin token (not restricted)


class RateLimited(JobError):
    """Too many requests from one caller or one front door. REST answers 429 with Retry-After."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


class TooManyActive(JobError):
    """The caller already has HANGAR_JOB_MAX_ACTIVE_PER_OWNER unfinished jobs."""


# ====================================================================== token buckets
class Buckets:
    """In-memory token buckets keyed by caller or source. A restart refills them, which is fine for a limiter."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._b: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill)

    def take(self, key: str, per_minute: float, burst: int) -> float:
        """0 if a token was taken, else seconds until one is available. per_minute <= 0 means no limit."""
        if per_minute <= 0 or burst <= 0:
            return 0.0
        t = self.clock()
        if key not in self._b and len(self._b) >= _MAX_BUCKETS:
            self._prune(t)  # on every new key: a stream of new keys that each get a token must not grow this
        tokens, last = self._b.get(key, (float(burst), t))
        tokens = min(float(burst), tokens + (t - last) * per_minute / 60)
        if tokens >= 1:
            self._b[key] = (tokens - 1, t)
            return 0.0
        self._b[key] = (tokens, t)
        return (1 - tokens) * 60 / per_minute

    def _prune(self, t: float) -> None:
        """Forget callers quiet for 10 minutes (their buckets have refilled); if that isn't enough, the quieter
        half."""
        self._b = {k: v for k, v in self._b.items() if t - v[1] < 600}
        if len(self._b) >= _MAX_BUCKETS:
            self._b = dict(sorted(self._b.items(), key=lambda kv: kv[1][1])[len(self._b) // 2:])


# ====================================================================== replays
class ReplayGuard:
    """Signed requests seen recently, kept in the store so a replay after a restart is still recognised."""

    def __init__(self, store: Store, prefix: str = "replay:"):
        self.store = store
        self.prefix = prefix
        self._last_prune = 0.0

    @staticmethod
    def digest(*parts: bytes) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update(len(p).to_bytes(8, "big") + p)  # length-prefixed: parts can't be shifted into each other
        return h.hexdigest()

    def get(self, scope: str, digest: str) -> str | None:
        """What was recorded for this delivery (a job id, or ""), or None if it is new or forgotten."""
        raw = self.store.kv_get(f"{self.prefix}{scope}:{digest}")
        if raw is None:
            return None
        try:
            v = json.loads(raw)
            return str(v["v"]) if float(v["exp"]) > time.time() else None
        except (ValueError, KeyError, TypeError):
            return None

    def put(self, scope: str, digest: str, value: str, ttl_seconds: float) -> None:
        t = time.time()
        # --- overnight/sweep: synced like tokens, so a power loss can't forget a delivery hangar already acted on
        self.store.kv_set(f"{self.prefix}{scope}:{digest}", json.dumps({"exp": t + ttl_seconds, "v": value}),
                          durable=True)
        if t - self._last_prune > _PRUNE_SECONDS:
            self._last_prune = t
            for key in self.store.kv_keys(self.prefix):
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    if float(json.loads(self.store.kv_get(key) or "{}")["exp"]) > t:
                        continue
                self.store.kv_delete(key)

    def forget(self, scope: str, digest: str) -> None:
        self.store.kv_delete(f"{self.prefix}{scope}:{digest}", durable=True)  # a redelivery must not come back refused


# ====================================================================== admission
class Admission:
    """Rate limits, the per-caller cap and refusal audits for one JobService."""

    def __init__(self, svc: JobService):
        self.svc = svc
        self.settings = svc.settings
        self.buckets = Buckets()
        self._audit_buckets = Buckets()
        self._suppressed: dict[str, int] = {}
        self.replays = ReplayGuard(svc.store)

    # ------------------------------------------------------------------ refusal audit
    def refused(self, key: str, actor: str, action: str, target: str | None = None, **detail: Any) -> None:
        """Audit a refused request, at most HANGAR_AUDIT_REJECTIONS_PER_MINUTE per key; the rest are counted and
        reported on the next entry that gets through."""
        n = self.settings.audit_rejections_per_minute
        if self._audit_buckets.take(key, n, n):
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            if len(self._suppressed) > 10_000:
                self._suppressed.clear()
            return
        if dropped := self._suppressed.pop(key, 0):
            detail["suppressed_before"] = dropped
        self.svc.store.audit(AuditEvent(actor=actor, action=action, target=target,
                                        detail=self.svc.broker.redact(detail)))

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def exempt(caller: Caller, source: str) -> bool:
        """Admin tokens (they can run anything already) and schedules (once per slot by design)."""
        return source == "schedule" or (source in _EXEMPT_SOURCES and not caller.restricted)

    def _take(self, key: str, per_minute: float, burst: int, who: str) -> None:
        wait = self.buckets.take(key, per_minute, burst)
        if wait:
            raise RateLimited(f"too many jobs from {who}; try again in {math.ceil(wait)}s", wait)

    def admit(self, req: SubmitJob, caller: Caller, source: str, untrusted: bool = False) -> None:
        """Raise RateLimited or JobError if this submission must wait. A repeat of an idempotent request always
        gets through: it returns the job it already started. `untrusted` submissions (they wait for approval) have
        a source bucket of their own, so outsiders with a handful of accounts can't use up the one trusted people
        need."""
        if req.idempotency_key and any(j.idempotency_key == req.idempotency_key and j.owner == caller.owner
                                       for j in self.svc._jobs.values()):
            return
        if self.exempt(caller, source):
            return
        s = self.settings
        cap = s.job_max_active_per_owner
        if cap > 0:
            active = sum(1 for j in self.svc._jobs.values() if j.owner == caller.owner and j.state not in JOB_TERMINAL)
            if active >= cap:
                raise TooManyActive(f"{caller.requester} already has {active} active jobs (the limit is {cap}); "
                                    "wait for one to finish or cancel one")
        self._take(f"owner:{caller.owner}", s.job_rate_per_minute, s.job_rate_burst, caller.requester)
        if untrusted:
            self._take(f"source:{source}:untrusted", s.job_source_rate_per_minute, s.job_source_rate_burst,
                       f"the {source} front door (people whose jobs need approval)")
        else:
            self._take(f"source:{source}", s.job_source_rate_per_minute, s.job_source_rate_burst,
                       f"the {source} front door")

    def admit_follow_up(self, requester: str, owner: str | None) -> None:
        """Follow-ups start agent turns too: the same per-caller bucket, for everyone but admin tokens. `owner` is
        set when a jobs token asks (its token id); GitHub and Slack people are told apart by their requester."""
        if owner is None and not requester.startswith(("github:", "slack:", "webhook:")):
            return
        key = f"owner:{owner or requester}"
        try:
            self._take(key, self.settings.job_rate_per_minute, self.settings.job_rate_burst, requester)
        except RateLimited as e:
            self.refused(key, requester, "job.refused", None, why="rate_limited", detail=str(e))
            raise

    @contextlib.contextmanager
    def refusals(self, caller: Caller, source: str) -> Iterator[None]:
        """Audit every submission that is refused, whatever refused it (a limit, the token's fence, bad input)."""
        try:
            yield
        except (JobError, ValidationError) as e:
            why = "rate_limited" if isinstance(e, RateLimited) else "too_many_active" \
                if isinstance(e, TooManyActive) else "invalid"
            self.refused(f"owner:{caller.owner}", caller.requester, "job.refused", None, source=source, why=why,
                         detail=str(e)[:300])
            raise


# ====================================================================== outbound connections
class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Connects to the address it checked: resolve, refuse internal addresses, then dial that IP. TLS still
    verifies the original name (httpcore passes it as the server name), so pinning the IP doesn't weaken it."""

    def __init__(self, allowed: Callable[[str], bool], resolve: Callable[[str], Awaitable[list[str]]],
                 inner: httpcore.AsyncNetworkBackend | None = None):
        self.allowed = allowed
        self.resolve = resolve
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        kw = {"timeout": timeout, "local_address": local_address, "socket_options": socket_options}
        name = host.strip("[]")
        if self.allowed(name):  # the operator allowed this host (HANGAR_JOB_CALLBACK_HOSTS)
            return await self.inner.connect_tcp(host, port, **kw)
        try:
            addrs = [str(ipaddress.ip_address(name))]  # a literal address is checked as it is
        except ValueError:
            try:
                addrs = await self.resolve(name)
            except OSError as e:
                raise httpcore.ConnectError(f"cannot resolve {name}: {e}") from e
        try:
            bad = not addrs or any(_internal(ipaddress.ip_address(a)) for a in addrs)
        except ValueError:
            bad = True
        if bad:
            raise httpcore.ConnectError(f"{name} resolves to an internal address")
        err: Exception | None = None
        for a in addrs:
            try:
                return await self.inner.connect_tcp(a, port, **kw)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                err = e
        raise httpcore.ConnectError(f"could not connect to {name}: {err}")

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed for outbound webhooks")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose connections go through GuardedBackend. It ignores proxy environment variables:
    through a proxy the check would be about the proxy's address, not the destination's."""

    def __init__(self, allowed: Callable[[str], bool], resolve: Callable[[str], Awaitable[list[str]]]):
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(ssl_context=httpx.create_ssl_context(),
                                                  network_backend=GuardedBackend(allowed, resolve))


def outbound_transport(svc: JobService) -> GuardedTransport:
    """For callbacks and A2A push notifications (spec 0009 §5.5)."""
    return GuardedTransport(svc._callback_allowed, svc.resolve)


# ====================================================================== ASGI: body caps and refusal audit
_AUDITED = {401, 403, 413, 429}


class FrontDoorMiddleware:
    """Caps request bodies before the app reads them, and audits 401/403/413/429 answers."""

    def __init__(self, app, hangar, oauth=None):
        self.app = app
        self.h = hangar
        self.oauth = oauth

    def _fixed_cap(self, path: str) -> int | None:
        s = self.h.settings
        if path == "/git" or path.startswith("/git/"):
            return None  # the git gateway streams pushes under its own limit (HANGAR_GATEWAY_MAX_PUSH_MB)
        if path == "/api/hooks/github":
            return GITHUB_MAX_BODY
        if path.startswith("/api/hooks/slack/"):
            from hangar.slack import MAX_BODY

            return MAX_BODY
        if path.startswith("/api/hooks/inbound/"):
            return s.inbound_max_body_bytes
        if path.startswith(("/api/", "/mcp")) or path == "/a2a":
            return -1  # depends on the caller
        return ANON_MAX_BODY  # the OAuth endpoints, and anything else

    def _authed_cap(self, path: str) -> int:
        """What a request with a valid token may send. /a2a has its own limit (and checks the token before reading
        the body, which this middleware would otherwise undo for anonymous callers)."""
        if path == "/a2a":
            return self.h.settings.a2a_max_request_bytes
        return self.h.settings.max_request_mb * 1024 * 1024

    async def _may_send(self, path: str, headers: dict[str, str]) -> bool:
        """Whether the request's token may use this route, and so send it a big body (--- overnight/sweep: a `jobs`
        or `mcp` token gets the anonymous cap on the routes it would be refused on). Mirrors the routes' own checks:
        `jobs` reaches /api/jobs* and /a2a, `mcp` (and an OAuth token) reaches /mcp, `admin` everything."""
        from hangar.auth import bearer_from_headers

        token = bearer_from_headers(headers)
        if not token:
            return False
        scopes = self.h.auth.scopes_of(token)
        if scopes is None:
            return path.startswith("/mcp") and self.oauth is not None \
                and await self.oauth.load_access_token(token) is not None
        if "admin" in scopes:
            return True
        if path.startswith("/mcp"):
            return "mcp" in scopes
        return "jobs" in scopes and (path in ("/a2a", "/api/jobs") or path.startswith("/api/jobs/"))

    def _audit(self, scope, status: int) -> None:
        client = (scope.get("client") or ("?", 0))[0]
        self.h.jobs.admission.refused(f"ip:{client}", f"ip:{client}", "request.rejected", scope.get("path"),
                                      status=status, method=scope.get("method"))

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        cap = self._fixed_cap(path)
        if scope["type"] != "http" or cap is None:  # the gateway audits its own refusals (gw.git.deny)
            return await self.app(scope, receive, send)

        async def send_(msg):
            if msg["type"] == "http.response.start":
                if msg["status"] in _AUDITED:
                    with contextlib.suppress(Exception):  # auditing must never break the response
                        self._audit(scope, msg["status"])
            await send(msg)

        if scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send_)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        length = headers.get("content-length")
        if cap == -1:
            small = length is not None and length.isdigit() and int(length) <= ANON_MAX_BODY
            cap = ANON_MAX_BODY if not small and not await self._may_send(path, headers) else self._authed_cap(path)
        if length is not None and (not length.isdigit() or int(length) > cap):
            return await _reject(scope, receive, send_)
        body, more, chunks = 0, True, []
        while more:  # buffer the body (up to the cap) before the app sees any of it
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += len(msg.get("body", b""))
            if body > cap:
                return await _reject(scope, receive, send_)
            chunks.append(msg)
            more = msg.get("more_body", False)

        async def replay():
            return chunks.pop(0) if chunks else await receive()

        await self.app(scope, replay, send_)


async def _reject(scope, receive, send) -> None:
    from fastapi.responses import JSONResponse

    await JSONResponse({"error": "request body too large"}, status_code=413)(scope, receive, send)
