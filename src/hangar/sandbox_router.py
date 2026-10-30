"""The proxy to a sandbox's own ports, under preview.py's PreviewRouter (`<port>-<sandbox id>.<HANGAR_PREVIEW_DOMAIN>`).

What a subclass decides is who may reach which port (`_route`, `_authorize`); what is here is how a request gets there:
plain HTTP and WebSocket proxying to the address the sandbox's hangar-envd proved (X-Hangar-Proof in the last few
seconds), never loopback, link-local or another sandbox's. Responses stream back as they arrive, and a client that goes
away closes the upstream request. Hangar's own tokens and headers never cross it.

(--- KAN-217: this was also the router of the E2B-compatible API's sandbox URLs, removed after v0.2.0; what preview
ports use stayed.)
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import time
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any, NamedTuple

import httpx

from hangar import observe
from hangar.auth import PREFIX as TOKEN_PREFIX
from hangar.envd import PORT as ENVD_PORT
from hangar.envd import Endpoint
from hangar.models import Sandbox, now

log = logging.getLogger("hangar.sandbox_router")

_SID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length"}
_SMALL = 1 << 20  # responses up to this size (with a length) are relayed in one piece
_WS_MAX = 16 << 20  # largest WebSocket message either way (uvicorn's own default for clients)
_RECHECK = 10.0  # seconds a sandbox's port address is used before its daemon proves it again


class Disconnected(Exception):
    """The client went away before its request body arrived."""


class Refused(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(body.get("message", ""))
        self.status, self.body = status, body


def _gone(sid: str, message: str = "The sandbox was not found") -> Refused:
    """A sandbox that isn't running (or can't be reached) answers 502, as a gateway's upstream would."""
    return Refused(502, {"sandboxId": sid, "message": message, "code": 502})


async def _send_json(send, status: int, body: Any, headers: list[tuple[bytes, bytes]] | None = None):
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                            *(headers or [])]})
    await send({"type": "http.response.body", "body": data})


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


class _Label(NamedTuple):
    """What observe.HTTPMiddleware reads as the matched route (`scope["route"].path`)."""

    path: str


def _upstream(scheme: str, host: str, port: int, raw_path: bytes, query: bytes) -> httpx.URL:
    """A URL on the sandbox's own address, built from parts: the client's path can't name another host."""
    return httpx.URL(scheme=scheme, host=host, port=port, raw_path=raw_path + (b"?" + query if query else b""))


class SandboxRouter:
    """ASGI middleware base: a subclass routes (`_route`, `__call__`) and authorizes (`_authorize`); requests it
    hands to `_sandbox` as `scope["hangar.sandbox"]` = ("sb", (id, port, raw path)) or ("bad", Refused) are proxied
    to the sandbox's port. A subclass sets `app`, `svc` (settings, store, fleet, ports_http), `_inflight`, `_addr`
    and `_observed`."""

    app: Any
    svc: Any
    _inflight: Counter[str]
    _addr: dict[str, tuple[Endpoint, float, str]]  # sandbox -> (proved endpoint, when, host)
    _observed: Any

    def _authorize(self, scope, sid: str, port: int, raw_path: bytes) -> Sandbox:
        raise NotImplementedError

    async def _sandbox(self, scope, receive, send):
        kind, arg = scope["hangar.sandbox"]
        try:
            if kind == "bad":
                raise arg
            sid, port_s, raw_path = arg
            if not _SID.match(sid) or not port_s.isdigit() or not 0 < int(port_s) < 65536:
                raise Refused(400, {"message": "Invalid sandbox ID or port", "code": 400})
            if not raw_path.startswith(b"/"):  # absolute-form, `@host`, `*`: uvicorn's h11 server passes them on
                raise Refused(400, {"message": "the request target must be a path", "code": 400})
            sb = self._authorize(scope, sid, int(port_s), raw_path)  # first: nobody else learns which ports exist
            port = int(port_s)
            if port == ENVD_PORT:  # hangar-envd's own port is never proxied
                raise Refused(404, {"message": "no such port", "code": 404})
            if observe.enabled() and scope["type"] == "http":
                from opentelemetry import trace

                observe.set_attrs(trace.get_current_span(), {"hangar.sandbox.id": sb.id, "server.port": port})
        except Refused as e:
            if scope["type"] == "websocket":
                await receive()  # websocket.connect
                return await send({"type": "websocket.close", "code": 1008, "reason": e.body.get("message", "")})
            return await _send_json(send, e.status, e.body)
        key = sb.id
        if self._inflight[key] >= self.svc.settings.preview_max_streams_per_sandbox:
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

    # ------------------------------------------------------------------ the sandbox's daemon
    def _endpoint_envd(self, sb: Sandbox):
        prov = self.svc.fleet.providers.get(sb.provider)
        envd = getattr(prov, "envd", None)
        if envd is None or not envd.enabled:
            raise _gone(sb.id, f"sandbox runs on the {sb.provider} provider, which has no hangar-envd")
        return envd

    async def _ready(self, sb: Sandbox):
        envd = self._endpoint_envd(sb)
        if getattr(envd, "_closed", False):  # integrate (#60): shutting down, its pool is gone; don't open another
            raise Refused(503, {"sandboxId": sb.id, "code": 503, "message": "hangar is restarting; try again"})
        ep = await envd.endpoint(sb) or await envd.ready(sb, timeout=15)
        if ep is None:
            raise _gone(sb.id, "hangar-envd isn't answering in this sandbox")
        return envd, ep

    async def _relay(self, resp: httpx.Response, receive, send) -> None:
        """Stream the response back as it comes; if the client goes away first, close the upstream request. A stream
        (no length) also ends when hangar starts shutting down (#57's Fleet.closing), so open streams don't hold the
        pod for the whole grace period."""
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
            if n is None:
                tasks.append(asyncio.ensure_future(self.svc.fleet.closing.wait()))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for t in done:
                if t is tasks[0] and (e := t.exception()) is not None and not isinstance(e, httpx.HTTPError):
                    raise e
            if tasks[0] not in done and tasks[1] not in done:  # shutting down
                await send({"type": "http.response.body", "body": b"", "more_body": False})
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
        """What a sandbox's app gets of the client's headers: its own Host too, without hangar's tokens."""
        return _passed(scope.get("headers", []), _HOP - {"host"})

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
        skip = _HOP | {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
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

        # --- integrate (#57): a shutdown closes it with 1012 (service restart) rather than waiting out the grace
        tasks = [asyncio.ensure_future(down()), asyncio.ensure_future(upstream()),
                 asyncio.ensure_future(self.svc.fleet.closing.wait())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await up.close()
        if tasks[2] in done and tasks[0] not in done and tasks[1] not in done:
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": 1012, "reason": "hangar is restarting"})
