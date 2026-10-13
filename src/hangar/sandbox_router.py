"""Sandbox URLs for E2B clients (docs/e2b.md): `https://<port>-<sandbox id>.<domain>` to the sandbox's ports.

The e2b SDKs send every command, file and terminal call to the sandbox's envd at port 49983, and an app's traffic
to its own port, by host name. This ASGI middleware sits in front of hangar's app and routes:
- `<port>-<id>.<HANGAR_E2B_API_DOMAIN>`: E2B's per-sandbox host (split at the first `-`: hangar ids contain one);
- `sandbox.<domain>`, and the path `<HANGAR_E2B_API_PATH>/sandbox/...` on any host: the `E2b-Sandbox-Id` and
  `E2b-Sandbox-Port` headers, which the SDKs send on every request (`E2B_SANDBOX_URL` points them at one URL);
- `api.<domain>`: the control API, so `E2B_DOMAIN` alone configures a client.

Port 49983 goes to the sandbox's hangar-envd. The client authenticates with the sandbox's E2B access token (or a
pre-signed `/files` URL made with it); the router checks it and signs its own request to the daemon (X-Hangar-Sig,
over the method, path and body), so the daemon's token never leaves the control plane and E2B's token is useless
anywhere but here. RPC bodies (up to 8 MiB) are read whole and signed with their hash; uploads stream through as
signed frames (`hangar.envd.chunked_body`), inflated on the way if they came gzip-encoded (the daemon doesn't), and
every answer must carry the daemon's X-Hangar-Proof. Envd also gets the sandbox's envVars added to every process
it starts. Other ports are plain HTTP and WebSocket proxying to the address the daemon proved, authenticated with
the traffic token or the access token; no port is public.

Responses stream back as they arrive (process output, watch events), and a client that goes away closes the
upstream request. Python is fast enough here for now (docs/e2b.md has the numbers); a Rust edge is the follow-up.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
import zlib
from collections import Counter
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, urlencode

import httpx

from hangar.auth import PREFIX as TOKEN_PREFIX
from hangar.envd import PORT as ENVD_PORT
from hangar.envd import UPLOAD_CHUNK, Endpoint, EnvdError, frames, proof, sign
from hangar.fleet import ACTIVE
from hangar.models import Sandbox, SandboxState, now

if TYPE_CHECKING:
    from hangar.e2b_api import E2BService

log = logging.getLogger("hangar.e2b")

MAX_RPC_BODY = 8 << 20  # hangar-envd checks signed bodies up to this size in memory
_SMALL = 1 << 20  # responses up to this size (with a length) are relayed in one piece
_TIMEOUTS = httpx.Timeout(None, connect=2.0).as_dict()  # streams live for hours; a dead daemon fails fast
_SID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length"}
# Request headers envd needs; everything else (the E2B token, routing headers, cookies) stays here.
_ENVD_HEADERS = {"content-type", "connect-protocol-version", "connect-timeout-ms", "connect-accept-encoding",
                 "connect-content-encoding", "keepalive-ping-interval", "user-agent", "accept"}
_DROP_TO_PORTS = {"x-access-token", "e2b-traffic-access-token", "e2b-sandbox-id", "e2b-sandbox-port", "x-api-key"}
_E2B_DEFAULT_USER = "user"  # E2B templates' user; to hangar it means the sandbox's own user
_INFLATE_STEP = 1 << 20  # a gzip upload is inflated at most this much at a time
_WS_MAX = 16 << 20  # largest WebSocket message either way (uvicorn's own default for clients)
_RECHECK = 10.0  # seconds a sandbox's port address is used before its daemon proves it again


class Disconnected(Exception):
    """The client went away before its request body arrived."""


class Refused(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(body.get("message", ""))
        self.status, self.body = status, body


def _gone(sid: str, message: str = "The sandbox was not found") -> Refused:
    """E2B's edge answers 502 for a sandbox that isn't running; the SDKs read it that way."""
    return Refused(502, {"sandboxId": sid, "message": message, "code": 502})


async def _send_json(send, status: int, body: Any, headers: list[tuple[bytes, bytes]] | None = None):
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                            *(headers or [])]})
    await send({"type": "http.response.body", "body": data})


def _is_files(raw_path: bytes) -> bool:
    return raw_path.rstrip(b"/") == b"/files"


def _passed(headers: list[tuple[bytes, bytes]], drop: set[str]) -> list[tuple[bytes, bytes]]:
    """Headers that cross the router, either way: not `drop`, not hop-by-hop ones the Connection header names, not
    hangar's own (X-Hangar-*: signatures and proofs), and not a hangar token in Authorization."""
    named = {t.strip().lower() for k, v in headers if k.lower() == b"connection"
             for t in v.decode("latin-1").split(",")}
    out = []
    for k, v in headers:
        name = k.decode("latin-1").lower()
        if name in drop or name in named or name.startswith("x-hangar-"):
            continue
        if name == "authorization":
            scheme, _, cred = v.decode("latin-1").strip().partition(" ")
            if scheme.lower() == "bearer" and cred.strip().startswith(TOKEN_PREFIX):
                continue
        out.append((k, v))
    return out


def _upstream(scheme: str, host: str, port: int, raw_path: bytes, query: bytes) -> httpx.URL:
    """A URL on the sandbox's own address, built from parts: the client's path can't name another host."""
    return httpx.URL(scheme=scheme, host=host, port=port, raw_path=raw_path + (b"?" + query if query else b""))


def file_signature(path: str, operation: str, user: str, token: str, expiration: str | None) -> str:
    """E2B's pre-signed `/files` URL signature (e2b SDK `get_signature`, envd `internal/api/auth.go`)."""
    raw = f"{path}:{operation}:{user}:{token}" + (f":{expiration}" if expiration else "")
    return "v1_" + base64.b64encode(hashlib.sha256(raw.encode()).digest()).decode().rstrip("=")


class SandboxRouter:
    """ASGI middleware: sandbox traffic is handled here, everything else goes on to `app`."""

    def __init__(self, app, svc: E2BService):
        self.app, self.svc = app, svc
        s = svc.settings
        base = s.e2b_api_path.rstrip("/")
        self.api_path = base
        self.prefix = f"{base}/sandbox" if base else ""
        self.domain = svc.domain() if base else None
        self._inflight: Counter[str] = Counter()
        self._addr: dict[str, tuple[Endpoint, float, str]] = {}  # sandbox -> (proved endpoint, when, host)

    # ------------------------------------------------------------------ routing
    def _route(self, scope) -> tuple[str, Any] | None:
        """("api", path) to rewrite for the control API, ("sb", (id, port, raw path)), ("bad", Refused), or None."""
        if not self.api_path:
            return None
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1").lower()
        host = host.rsplit(":", 1)[0] if host and not host.endswith("]") else host
        raw_path: bytes = scope.get("raw_path") or scope["path"].encode()
        by_headers = False
        if self.domain and host.endswith("." + self.domain):
            label = host[: -len(self.domain) - 1]
            if label == "api":
                if scope["path"].startswith(self.api_path + "/") or scope["path"] == self.api_path:
                    return None
                return "api", self.api_path + scope["path"]
            if label == "sandbox":
                by_headers = True
            elif "." not in label:
                port, _, sid = label.partition("-")
                return "sb", (sid, port, raw_path)
        if not by_headers and self.prefix:
            if raw_path == self.prefix.encode() or raw_path.startswith(self.prefix.encode() + b"/"):
                raw_path = raw_path[len(self.prefix):] or b"/"
                by_headers = True
        if not by_headers:
            return None
        sid = headers.get(b"e2b-sandbox-id", b"").decode("latin-1")
        port = headers.get(b"e2b-sandbox-port", str(ENVD_PORT).encode()).decode("latin-1")
        if not sid:
            return "bad", Refused(400, {"message": "missing header E2b-Sandbox-Id", "code": 400})
        return "sb", (sid, port, raw_path)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        route = self._route(scope)
        if route is None:
            return await self.app(scope, receive, send)
        kind, arg = route
        if kind == "api":
            return await self.app(dict(scope, path=arg, raw_path=arg.encode()), receive, send)
        try:
            if kind == "bad":
                raise arg
            sid, port_s, raw_path = arg
            if not _SID.match(sid) or not port_s.isdigit() or not 0 < int(port_s) < 65536:
                raise Refused(400, {"message": "Invalid sandbox ID or port", "code": 400})
            if not raw_path.startswith(b"/"):  # absolute-form, `@host`, `*`: uvicorn's h11 server passes them on
                raise Refused(400, {"message": "the request target must be a path", "code": 400})
            sb, rec, port, by_url = self._authorize(scope, sid, int(port_s), raw_path)
        except Refused as e:
            if scope["type"] == "websocket":
                await receive()  # websocket.connect
                return await send({"type": "websocket.close", "code": 1008, "reason": e.body.get("message", "")})
            return await _send_json(send, e.status, e.body)
        key = sb.id
        if self._inflight[key] >= self.svc.settings.e2b_max_streams_per_sandbox:
            if scope["type"] == "websocket":
                await receive()
                return await send({"type": "websocket.close", "code": 1013, "reason": "too many connections"})
            return await _send_json(send, 429, {"sandboxId": sid, "code": 429,
                                                "message": "too many requests in flight to this sandbox"},
                                    [(b"retry-after", b"1")])
        self._inflight[key] += 1
        try:
            if now() - (sb.last_activity or 0) >= 30:
                self.svc.fleet.touch(sb.id)  # traffic is activity (dashboards and idle checks read it)
            if scope["type"] == "websocket":
                await self._websocket(scope, receive, send, sb, port, raw_path)
            elif port == ENVD_PORT:
                await self._envd(scope, receive, send, sb, rec, raw_path, by_url)
            else:
                await self._port(scope, receive, send, sb, port, raw_path)
        except Refused as e:
            if scope["type"] == "http":
                await _send_json(send, e.status, e.body)
        except Disconnected:
            pass
        finally:
            self._inflight[key] -= 1
            if self._inflight[key] <= 0:
                del self._inflight[key]

    # ------------------------------------------------------------------ auth
    def _authorize(self, scope, sid: str, port: int, raw_path: bytes) -> tuple[Sandbox, dict[str, Any], int, bool]:
        """Deny by default: a live sandbox exposed through the E2B API, and its token on every request."""
        sb = self.svc.store.get(Sandbox, sid)
        rec = self.svc.record(sid) if sb is not None and sb.state in ACTIVE else None
        if sb is None or rec is None:
            self._addr.pop(sid, None)
            raise _gone(sid)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        access = headers.get("x-access-token")
        by_url = False
        if port == ENVD_PORT:
            ok = self.svc.check_envd(sid, rec, access)
            if not ok and access is None and scope["type"] == "http" and _is_files(raw_path):
                ok = by_url = self._signed_file_url(scope, sid, rec)
            if not ok:
                self._refused(scope, sid, "e2b.envd.denied")
                raise Refused(401, {"code": 401, "message": "unauthorized access, invalid or missing access token"})
        elif not (self.svc.check_traffic(sid, rec, headers.get("e2b-traffic-access-token"))
                  or self.svc.check_envd(sid, rec, access)):
            self._refused(scope, sid, "e2b.port.denied")
            raise Refused(403, {"sandboxId": sid, "code": 403, "message": "missing or invalid traffic access token "
                                "(send the sandbox's trafficAccessToken as e2b-traffic-access-token)"})
        if not self.svc.owner_live(rec):
            # The sandbox's tokens came from a hangar token that has since been revoked: they go with it.
            self._refused(scope, sid, "e2b.owner.revoked")
            raise Refused(401, {"code": 401, "message": "the API key that created this sandbox was revoked"})
        if sb.state == SandboxState.PAUSED:
            raise _gone(sid, "The sandbox is paused; connect to it (Sandbox.connect) to resume it")
        if sb.state != SandboxState.RUNNING:
            raise _gone(sid, "The sandbox is still starting")
        return sb, rec, port, by_url

    def _signed_file_url(self, scope, sid: str, rec: dict[str, Any]) -> bool:
        """A pre-signed `/files` URL: good for that path, operation and user, until its expiry, and nothing else.
        Each signed parameter may appear once, so the one checked is the one the daemon gets."""
        if scope["method"] not in ("GET", "POST"):
            return False
        pairs = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        q = dict(pairs)
        if len(q) != len(pairs):
            return False
        sig, exp = q.get("signature"), q.get("signature_expiration")
        if not sig:
            return False
        if exp is not None and (not exp.isdigit() or int(exp) < time.time()):
            return False
        want = file_signature(q.get("path", ""), "read" if scope["method"] == "GET" else "write",
                              q.get("username", ""), self.svc.envd_token(sid, rec), exp)
        return hmac.compare_digest(sig.encode(), want.encode())

    def _refused(self, scope, sid: str, action: str) -> None:
        client = (scope.get("client") or ("?", 0))[0]
        with contextlib.suppress(Exception):  # auditing never breaks the answer
            self.svc.h.jobs.admission.refused(f"ip:{client}", f"ip:{client}", action, sid,
                                              path=scope.get("path", "")[:200])

    # ------------------------------------------------------------------ envd (port 49983)
    def _endpoint_envd(self, sb: Sandbox):
        prov = self.svc.fleet.providers.get(sb.provider)
        envd = getattr(prov, "envd", None)
        if envd is None or not envd.enabled:
            raise _gone(sb.id, f"sandbox runs on the {sb.provider} provider, which has no hangar-envd")
        return envd

    async def _ready(self, sb: Sandbox):
        envd = self._endpoint_envd(sb)
        ep = await envd.endpoint(sb) or await envd.ready(sb, timeout=15)
        if ep is None:
            raise _gone(sb.id, "hangar-envd isn't answering in this sandbox")
        return envd, ep

    def _envd_target(self, scope, raw_path: bytes) -> tuple[str, list[tuple[str, str]]]:
        """The path and query to send the daemon, and the headers it gets."""
        path = raw_path.decode("latin-1")
        query = scope.get("query_string", b"").decode("latin-1")
        user = self.svc.settings.sandbox_user
        if query and _is_files(raw_path):
            pairs = parse_qsl(query, keep_blank_values=True)
            kept = [(k, v) for k, v in pairs if k not in ("signature", "signature_expiration")
                    and not (k == "username" and v == _E2B_DEFAULT_USER != user)]
            if kept != pairs:
                query = urlencode(kept, quote_via=quote)
        headers = []
        for k, v in scope.get("headers", []):
            name, value = k.decode("latin-1").lower(), v.decode("latin-1")
            if name in _ENVD_HEADERS:
                headers.append((name, value))
            elif name == "authorization" and value.startswith("Basic "):
                if not self._is_default_user(value[6:].strip(), user):
                    headers.append((name, value))
        return path + (f"?{query}" if query else ""), headers

    @staticmethod
    def _is_default_user(b64: str, user: str) -> bool:
        with contextlib.suppress(ValueError):
            name = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode().split(":", 1)[0]
            return name == _E2B_DEFAULT_USER != user
        return False

    async def _envd(self, scope, receive, send, sb: Sandbox, rec: dict[str, Any], raw_path: bytes,
                    by_url: bool = False) -> None:
        envd, ep = await self._ready(sb)
        method = scope["method"]
        target, headers = self._envd_target(scope, raw_path)
        hdr = {k: v for k, v in headers}
        if method == "POST" and _is_files(raw_path):
            gz = any(k.lower() == b"content-encoding" and v.strip().lower() == b"gzip"
                     for k, v in scope.get("headers", []))
            body = self._body(receive, gz)
            if hdr.get("content-type", "").lower().startswith("multipart/form-data"):
                return await self._multipart(scope, send, sb, envd, ep, target, body, by_url)
            # Streamed as signed frames: the daemon writes nothing it hasn't checked and replaces the file only once
            # the last frame arrives, so an upload cut short here (a 413, a client gone) changes nothing.
            return await self._forward(receive, send, sb, envd, _signed(ep, method, target, body, hdr, "chunked"))
        if raw_path == b"/health" and method == "GET":
            return await self._health(send, sb, envd, ep)
        data = await self._read(receive, MAX_RPC_BODY)
        if raw_path == b"/process.Process/Start" and (env := self.svc.env_vars(sb.id, rec)):
            data = _with_env(data, hdr.get("content-type", ""), env)
        digest = hashlib.sha256(data).hexdigest()
        await self._forward(receive, send, sb, envd, _signed(ep, method, target, data, hdr, digest))

    async def _forward(self, receive, send, sb: Sandbox, envd, req: httpx.Request) -> None:
        try:
            resp = await envd.http.send(req, stream=True)
        except httpx.HTTPError as e:
            envd._lost(sb, str(e))
            raise _gone(sb.id, f"hangar-envd didn't answer: {type(e).__name__}") from e
        try:
            _proven(envd, sb, req, resp)
        except Refused:
            await resp.aclose()
            raise
        await self._relay(resp, receive, send)

    async def _health(self, send, sb: Sandbox, envd, ep) -> None:
        """The daemon's /health answers anyone, so it proves nothing about who answered: ask its signed /info."""
        req = _signed(ep, "GET", "/info", b"", {}, hashlib.sha256(b"").hexdigest())
        try:
            status, _ = await envd.send_capped(req, 65536)
        except (httpx.HTTPError, EnvdError) as e:
            envd._lost(sb, str(e))
            raise _gone(sb.id, f"hangar-envd didn't answer: {type(e).__name__}") from e
        if status != 200:
            envd._lost(sb, f"/info answered {status}")
            raise _gone(sb.id, f"hangar-envd answered {status}")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def _multipart(self, scope, send, sb: Sandbox, envd, ep, target: str, body: AsyncIterator[bytes],
                         by_url: bool = False) -> None:
        """E2B's multipart upload (one part per file, its filename the path) as one signed upload per file: the
        daemon takes signed uploads as octet-stream only."""
        from starlette.datastructures import Headers, UploadFile
        from starlette.formparsers import MultiPartException, MultiPartParser

        path, _, query = target.partition("?")
        pairs = parse_qsl(query, keep_blank_values=True)
        try:
            form = await MultiPartParser(Headers(scope=scope), body, max_files=1000, max_fields=100).parse()
        except MultiPartException as e:
            raise Refused(400, {"code": 400, "message": f"bad multipart body: {e}"}) from e
        try:
            files = [v for _, v in form.multi_items() if isinstance(v, UploadFile)]
            if not files:
                raise Refused(400, {"code": 400, "message": "the multipart body has no file"})
            query_path = dict(pairs).get("path")
            if by_url and (len(files) != 1 or not query_path):
                # A pre-signed URL is signed for its `path`: a part's filename can't send it anywhere else.
                raise Refused(400, {"code": 400, "message": "a pre-signed upload URL takes one file, for its path"})
            written: list[Any] = []
            for up in files:
                dest = query_path if len(files) == 1 and query_path else up.filename or query_path
                if not dest:
                    raise Refused(400, {"code": 400, "message": "a file part has no path (its filename)"})
                q = urlencode([(k, v) for k, v in pairs if k != "path"] + [("path", dest)], quote_via=quote)
                await asyncio.to_thread(up.file.seek, 0)
                req = _signed(ep, "POST", f"{path}?{q}", self._chunks(up.file),
                              {"content-type": "application/octet-stream"}, "chunked")
                try:
                    status, raw = await envd.send_capped(req, 1 << 20)
                except (httpx.HTTPError, EnvdError) as e:
                    envd._lost(sb, str(e))
                    raise _gone(sb.id, f"hangar-envd didn't answer: {type(e).__name__}") from e
                if status == 401:
                    envd._lost(sb, "the daemon refused hangar's signature")
                    raise _gone(sb.id, "hangar-envd refused hangar's signature (its token or clock)")
                try:
                    out = json.loads(raw)
                except ValueError:
                    out = None
                if status != 200 or not isinstance(out, list):
                    raise Refused(status if status != 200 else 502,
                                  out if isinstance(out, dict) else {"code": status, "message": "upload failed"})
                written.extend(out)
            await _send_json(send, 200, written)
        finally:
            await form.close()

    async def _read(self, receive, cap: int) -> bytes:
        body = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                raise Disconnected
            body += msg.get("body", b"")
            if len(body) > cap:
                raise Refused(413, {"code": 413, "message": f"request body over {cap >> 20} MiB"})
            if not msg.get("more_body", False):
                return bytes(body)

    async def _body(self, receive, gz: bool) -> AsyncIterator[bytes]:
        """An upload's bytes, inflated if they came gzip-encoded (the daemon takes them as is), at most
        HANGAR_MAX_REQUEST_MB of them: a gzip bomb stops at the cap. Inflating goes _INFLATE_STEP at a time, so a
        small compressed message never becomes the whole cap in memory at once."""
        cap = self.svc.settings.max_request_mb << 20
        inflate = zlib.decompressobj(16 + zlib.MAX_WBITS) if gz else None
        size = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                raise Disconnected
            data = msg.get("body", b"")
            while data:
                if inflate is None:
                    chunk, data = data, b""
                else:
                    try:
                        chunk = inflate.decompress(data, _INFLATE_STEP)
                    except zlib.error as e:
                        raise Refused(400, {"code": 400, "message": f"malformed gzip body: {e}"}) from e
                    data = inflate.unconsumed_tail
                size += len(chunk)
                if size > cap:
                    raise Refused(413, {"code": 413,
                                        "message": f"upload over {cap >> 20} MiB (HANGAR_MAX_REQUEST_MB)"})
                if chunk:
                    yield chunk
            if not msg.get("more_body", False):
                break
        if inflate is not None and not inflate.eof:
            raise Refused(400, {"code": 400, "message": "truncated gzip body"})
        if inflate is not None and inflate.unused_data:
            raise Refused(400, {"code": 400, "message": "data after the end of the gzip body"})

    @staticmethod
    async def _chunks(f) -> AsyncIterator[bytes]:
        while chunk := await asyncio.to_thread(f.read, 256 * 1024):
            yield chunk

    async def _relay(self, resp: httpx.Response, receive, send) -> None:
        """Stream the response back as it comes; if the client goes away first, close the upstream request (a
        process started through it keeps running, as with E2B)."""
        try:
            headers = _passed(resp.headers.raw, _HOP | {"date", "server"})
            n = resp.headers.get("content-length")
            if n is not None:
                headers.append((b"content-length", n.encode()))
            if n is not None and n.isdigit() and int(n) <= _SMALL:  # most RPCs: one write, no watcher task
                body = b"".join([c async for c in resp.aiter_raw()])
                await send({"type": "http.response.start", "status": resp.status_code, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
            await send({"type": "http.response.start", "status": resp.status_code, "headers": headers})

            async def pump() -> None:
                async for chunk in resp.aiter_raw():
                    if chunk:
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b"", "more_body": False})

            async def gone() -> None:
                while (await receive())["type"] != "http.disconnect":
                    pass

            tasks = [asyncio.ensure_future(pump()), asyncio.ensure_future(gone())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for t in done:
                if t is tasks[0] and (e := t.exception()) is not None and not isinstance(e, httpx.HTTPError):
                    raise e
        finally:
            await resp.aclose()

    # ------------------------------------------------------------------ the sandbox's other ports
    async def _host(self, sb: Sandbox) -> str:
        """The sandbox's own address for its other ports: where its daemon answered with a proof of its token
        (X-Hangar-Proof) in the last _RECHECK seconds. An address merely remembered could be another sandbox's by
        now (a GKE Pod resumed from a snapshot comes back with a new IP, and the old one goes to the next Pod). Never
        loopback, link-local (the metadata server) or unspecified, and only the daemon's own port's address: one
        published elsewhere (Docker Desktop's loopback ports) leads to envd alone."""
        envd, ep = await self._ready(sb)
        cached = self._addr.get(sb.id)
        if cached and cached[0] is ep and time.monotonic() - cached[1] < _RECHECK:
            return cached[2]
        u = httpx.URL(ep.url)
        try:
            ip = ipaddress.ip_address(u.host)
        except ValueError:
            ip = None
        if (ip is None or u.port != ENVD_PORT or ip.is_loopback or ip.is_link_local or ip.is_unspecified
                or ip.is_multicast):
            raise _gone(sb.id, "the control plane can't reach this sandbox's ports (only hangar-envd is published)")
        if await envd._probe(sb, [ep.url], ep.token) is None:
            envd._lost(sb, "no proof from the daemon's address")
            self._addr.pop(sb.id, None)
            raise _gone(sb.id, "hangar-envd isn't answering in this sandbox")
        self._addr[sb.id] = (ep, time.monotonic(), u.host)
        return u.host

    def _port_headers(self, scope) -> list[tuple[bytes, bytes]]:
        """What a sandbox's app gets of the client's headers: its own Host too, without E2B's or hangar's tokens."""
        return _passed(scope.get("headers", []), (_HOP - {"host"}) | _DROP_TO_PORTS)

    async def _port(self, scope, receive, send, sb: Sandbox, port: int, raw_path: bytes) -> None:
        host = await self._host(sb)
        url = _upstream("http", host, port, raw_path, scope.get("query_string", b""))
        headers = self._port_headers(scope)
        names = {k.lower(): v for k, v in scope.get("headers", [])}
        if (length := names.get(b"content-length")) is not None:
            headers.append((b"content-length", length))

        async def body() -> AsyncIterator[bytes]:
            more = True
            while more:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    raise Disconnected
                more = msg.get("more_body", False)
                if chunk := msg.get("body", b""):
                    yield chunk

        has_body = (length not in (None, b"0")) or b"transfer-encoding" in names
        req = self.svc.ports_http.build_request(scope["method"], url, headers=headers,
                                                content=body() if has_body else b"")
        try:
            resp = await self.svc.ports_http.send(req, stream=True)
        except httpx.HTTPError as e:
            self._addr.pop(sb.id, None)
            raise _gone(sb.id, f"The sandbox is running but port {port} is not open") from e
        await self._relay(resp, receive, send)

    async def _websocket(self, scope, receive, send, sb: Sandbox, port: int, raw_path: bytes) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake

        await receive()  # websocket.connect
        if port == ENVD_PORT:  # envd speaks Connect over HTTP only
            return await send({"type": "websocket.close", "code": 1008, "reason": "envd has no WebSocket routes"})
        try:
            host = await self._host(sb)
        except Refused as e:
            return await send({"type": "websocket.close", "code": 1011, "reason": e.body["message"][:120]})
        url = str(_upstream("ws", host, port, raw_path, scope.get("query_string", b"")))
        skip = _HOP | _DROP_TO_PORTS | {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
                                        "sec-websocket-protocol"}
        extra = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in _passed(scope.get("headers", []), skip)]
        try:
            # The sandbox is untrusted: messages (inflated, with permessage-deflate) are capped as uvicorn caps the
            # client's, and only a few wait unread, instead of whatever it sends being held in memory here.
            up = await connect(url, additional_headers=extra, subprotocols=scope.get("subprotocols") or None,
                               open_timeout=10, max_size=_WS_MAX, max_queue=4, proxy=None)
        except (OSError, InvalidHandshake, TimeoutError) as e:
            return await send({"type": "websocket.close", "code": 1011,
                               "reason": f"port {port} refused the connection: {type(e).__name__}"[:120]})
        await send({"type": "websocket.accept", "subprotocol": up.subprotocol})

        async def down() -> None:
            too_big = False
            try:
                async for m in up:
                    await send({"type": "websocket.send", **({"text": m} if isinstance(m, str) else {"bytes": m})})
            except ConnectionClosed as e:
                too_big = e.sent is not None and e.sent.code == 1009
            code = 1009 if too_big else (up.close_code or 1000)
            if code in (1005, 1006, 1015):  # reserved: never sent in a close frame
                code = 1011
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": code})

        async def upstream() -> None:
            while True:
                m = await receive()
                if m["type"] == "websocket.disconnect":
                    return
                with contextlib.suppress(ConnectionClosed):
                    await up.send(m["text"] if m.get("text") is not None else m.get("bytes") or b"")

        tasks = [asyncio.ensure_future(down()), asyncio.ensure_future(upstream())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await up.close()


def _signed(ep: Endpoint, method: str, target: str, content: Any, headers: dict[str, str],
            body: str) -> httpx.Request:
    """A request to the daemon signed with its token (X-Hangar-Sig over the method, path, query and `body`: the
    body's SHA-256, or "chunked" for an upload, whose `content` (an async iterator) then goes as signed frames).
    The answer's expected X-Hangar-Proof rides in the request's extensions (`_proven`). Built directly rather than
    with the client's build_request, which costs more than everything else here."""
    url = httpx.URL(ep.url + target)
    sig, mac = sign(ep.token, method, url.raw_path, body)
    if body == "chunked":
        content = _framed(content, ep.token, mac)
    h = {**headers, "accept-encoding": "identity", "x-hangar-sig": sig}
    return httpx.Request(method, url, headers=h, content=content,
                         extensions={"timeout": _TIMEOUTS, "hangar_proof": proof(ep.token, mac)})


async def _framed(chunks: AsyncIterator[bytes], token: str, mac: bytes) -> AsyncIterator[bytes]:
    """An upload's bytes as `chunked_body` frames of UPLOAD_CHUNK bytes, each signed (chained from the request's
    MAC) as it goes, then the empty frame that tells the daemon the upload is complete."""
    buf, prev, end = bytearray(), mac, False
    while not end:
        chunk = await anext(chunks, None)
        if chunk is None:
            end = True
        else:
            buf += chunk
        while len(buf) >= UPLOAD_CHUNK or (end and buf):
            part = bytes(buf[:UPLOAD_CHUNK])
            del buf[:UPLOAD_CHUNK]
            frame = next(frames(token, prev, [memoryview(part)]))
            prev = frame[-32:]
            yield frame
    yield next(frames(token, prev, [memoryview(b"")]))


def _proven(envd, sb: Sandbox, req: httpx.Request, resp: httpx.Response) -> None:
    """The answer must come from this sandbox's daemon (X-Hangar-Proof): anything else at its address (a sandbox
    that got its old IP) is refused and the address looked up again. A 401 is the daemon refusing hangar's own
    signature (its token or clock), which the E2B client can do nothing about: a 502 too, not a token error."""
    try:
        envd.check_proof(req, resp)
    except EnvdError as e:
        envd._lost(sb, str(e))
        raise _gone(sb.id, "something other than this sandbox's hangar-envd answered at its address") from e
    if resp.status_code == 401:
        envd._lost(sb, "the daemon refused hangar's signature")
        raise _gone(sb.id, "hangar-envd refused hangar's signature (its token or clock)")


def _with_env(body: bytes, content_type: str, env: dict[str, str]) -> bytes:
    """A process.Process/Start request with the sandbox's envVars under the request's own `envs` (the request
    wins, as with E2B's default env). Only the JSON codec (what the SDKs send); anything else passes unchanged."""
    framed = content_type.startswith("application/connect+json")
    if not framed and not content_type.startswith("application/json"):
        return body
    payload = body
    if framed:
        if len(body) < 5 or body[0] != 0 or int.from_bytes(body[1:5], "big") != len(body) - 5:
            return body  # compressed or several messages: not what an SDK sends; the daemon will judge it
        payload = body[5:]
    try:
        msg = json.loads(payload)
        proc = msg.get("process")
        if not isinstance(proc, dict):
            return body
        own = proc.get("envs") if isinstance(proc.get("envs"), dict) else {}
        proc["envs"] = {**env, **own}
    except (ValueError, AttributeError):
        return body
    out = json.dumps(msg).encode()
    return (b"\0" + len(out).to_bytes(4, "big") + out) if framed else out
