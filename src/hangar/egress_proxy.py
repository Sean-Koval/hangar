"""The egress proxy (--- egress): an HTTP CONNECT proxy in the control plane, next to the git gateway.

Sandboxes in an `allowlist` or `none` environment can reach nothing but this proxy and the git gateway, with no DNS
that recurses to the internet; their tools find the proxy in HTTPS_PROXY/HTTP_PROXY (egress.py). For each connection
the proxy:

1. authenticates the sandbox (Basic auth: sandbox id and HMAC(vault key, sandbox id); a sandbox that isn't running
   gets nothing);
2. checks host:port against the sandbox's policy (egress.for_sandbox, cached a few seconds);
3. resolves the name itself and connects only to public addresses (an allowed name that resolves into the VPC, the
   cluster or the metadata server is refused), to the address it checked;
4. for CONNECT, reads the client's first bytes before relaying anything: a TLS ClientHello must name the same host
   in SNI, a plain HTTP request must carry it in Host. Nothing else gets through. TLS is never terminated, so the
   proxy holds no CA and sees no content;
5. relays bytes both ways until either side closes or the tunnel is idle for IDLE_SECONDS.

Why here and not a separate proxy: the decision needs what the control plane has (the sandbox, its person, its
session's approvals, the audit log and the activity stream), and a connection costs one dictionary lookup and a
SQLite read by primary key at most. Relaying is plain socket copying on the event loop with 64 KB reads.

Every connection is accounted for per sandbox and person: a denied one is an `egress.deny` audit entry (at most
DENY_AUDITS_PER_MINUTE per sandbox, the rest counted in the next entry) and an event in the sandbox's session;
allowed ones are summed per sandbox and host into `egress.allow` entries every FLUSH_SECONDS (connections, bytes).
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import ipaddress
import logging
import socket
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from hangar import egress, egress_tls, metrics
from hangar.models import Sandbox, SandboxState

if TYPE_CHECKING:
    from hangar.fleet import Fleet

log = logging.getLogger("hangar.egress")

HEAD_MAX = 16 * 1024  # a request head: CONNECT lines are tiny; plain HTTP requests keep their headers short
HEAD_SECONDS = 15.0  # to send the request head, and (CONNECT) the first TLS record or HTTP head
CONNECT_SECONDS = 15.0
IDLE_SECONDS = 900.0  # no byte either way for this long ends a tunnel (model streams send keep-alives)
CLOSE_GRACE_SECONDS = 15.0  # after the far side closes, how long the client side may still send
# What one tunnel may hold per direction: a stream reader pauses its socket at 2 * BUF, a socket read takes at most
# BUF (asyncio's default is 256 KB), and a write waits for the 64 KB high-water mark. A client that stops reading
# then pins at most about 5 * BUF (320 KB), so even MAX_CONNECTIONS stalled tunnels fit the control plane's memory.
BUF = 64 * 1024
MAX_CONNECTIONS = 4096  # the process's file limit is the real bound; leave room for everything else
POLICY_SECONDS = 5.0  # an approval or a policy change applies within this long (approve() also invalidates)
FLUSH_SECONDS = 60.0
DENY_AUDITS_PER_MINUTE = 30
AUTH_FAILURES, AUTH_WINDOW = 30, 60.0
_HOP = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "te", "trailer", "upgrade",
        "transfer-encoding-proxy"}


class Denied(Exception):
    def __init__(self, status: int, reason: str, message: str, kind: str):
        super().__init__(message)
        self.status, self.reason, self.kind = status, reason, kind


# ============================================================================ parsing
def parse_head(raw: bytes) -> tuple[str, str, str, list[tuple[str, str]]]:
    """(method, target, version, headers) of a request head ending in CRLFCRLF. ValueError when malformed."""
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise ValueError("malformed request line")
    headers = []
    for ln in lines[1:]:
        if not ln:
            continue
        if ln[0] in " \t" or ":" not in ln or "\n" in ln or "\r" in ln or "\0" in ln:  # no folding, no bare LF
            raise ValueError("malformed header")
        k, _, v = ln.partition(":")
        headers.append((k.strip(), v.strip()))
    return parts[0].upper(), parts[1], parts[2], headers


def header(headers: list[tuple[str, str]], name: str) -> str | None:
    return next((v for k, v in headers if k.lower() == name), None)


def split_hostport(target: str, default: int | None) -> tuple[str, int]:
    """`host:port` (or `[v6]:port`) → (normalized host, port). ValueError when it isn't one."""
    t = target.strip()
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        port_s = rest[1:] if rest.startswith(":") else ""
    elif t.count(":") == 1:
        host, _, port_s = t.partition(":")
    else:
        host, port_s = t, ""
    if not port_s:
        if default is None:
            raise ValueError("a port is required")
        port = default
    elif port_s.isdigit() and 0 < int(port_s) < 65536:
        port = int(port_s)
    else:
        raise ValueError("bad port")
    return egress.normalize_host(host), port


sni = egress_tls.sni  # --- egress2: the ClientHello checks live in egress_tls.py


# IPv6 ranges that carry an IPv4 address in their last 32 bits and reach it wherever a translator exists: NAT64's
# well-known prefix (RFC 6052), IPv4-compatible (RFC 4291, deprecated) and SIIT's IPv4-translated (RFC 2765). Python
# calls them global.
_EMBEDS_V4 = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96"))
_THIS_NET = ipaddress.ip_network("0.0.0.0/8")


def _addresses(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[ipaddress.IPv4Address |
                                                                          ipaddress.IPv6Address]:
    """The address and any IPv4 address inside it (mapped, 6to4, NAT64, compatible, translated): a connection may
    end up at either, so both must pass."""
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            out.append(ip.ipv4_mapped)
        elif ip.sixtofour:
            out.append(ip.sixtofour)
        elif any(ip in n for n in _EMBEDS_V4):
            out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    addrs = _addresses(ip)
    if len(addrs) > 1:  # judged by the IPv4 address it reaches; 6to4 needs a relay hangar doesn't trust
        return not ip.sixtofour and addrs[1].is_global and not addrs[1].is_multicast and not ip.is_loopback
    return ip.is_global and not ip.is_multicast


def reachable_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """A private address an operator may open to named hosts (an internal mirror): never loopback, link-local
    (the metadata servers), unspecified or multicast, written as IPv4 or inside an IPv6 address."""
    return not any(a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast or a.is_reserved
                   or a in _THIS_NET for a in _addresses(ip))


def _response(status: int, text: str, extra: dict[str, str] | None = None) -> bytes:
    reason = {200: "Connection established", 400: "Bad Request", 403: "Forbidden", 407: "Proxy Authentication "
              "Required", 429: "Too Many Requests", 502: "Bad Gateway", 503: "Service Unavailable",
              504: "Gateway Timeout"}.get(status, "Error")
    body = (text + "\n").encode()
    hdrs = {"Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(body)), "Connection": "close",
            **(extra or {})}
    return (f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n").encode() \
        + body


# ============================================================================ the proxy
class EgressProxy:
    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self.settings = fleet.settings
        self._servers: dict[tuple[str, int], asyncio.AbstractServer] = {}
        self._listen_lock: asyncio.Lock | None = None
        self._key: bytes | None = None
        self._policies: dict[str, tuple[float, egress.Policy]] = {}
        self._open: collections.Counter[str] = collections.Counter()
        self._total = 0
        self._peers: collections.Counter[str] = collections.Counter()
        # (sandbox, host, port) -> [connections, bytes out, bytes in, rule]: allowed traffic not yet audited
        self._usage: dict[tuple[str, str, int], list[Any]] = {}
        self._deny_log: dict[str, collections.deque[float]] = {}
        self._deny_dropped: collections.Counter[str] = collections.Counter()
        self._failures: Any = None
        self._flusher: asyncio.Task | None = None
        self.activity: Any = None  # SessionService.activity, set by the app: denials become session events
        self._conns: set[asyncio.StreamWriter] = set()  # --- egress2: open client connections, for the drain
        self._closing_at: float | None = None
        self._watcher: asyncio.Task | None = None

    @property
    def key(self) -> bytes:
        if self._key is None:
            from hangar.vault import load_or_create_key

            self._key = load_or_create_key(self.settings.secret_key, self.settings.data_dir)
        return self._key

    @property
    def port(self) -> int:
        return self.settings.egress_port

    # ------------------------------------------------------------------ listeners
    async def start(self) -> None:
        """Listen where sandboxes reach the proxy: HANGAR_EGRESS_URL's listener (GKE), and the Docker egress
        network's gateway if it exists already (sandboxes from before a restart)."""
        if not self.port:
            return
        if self.settings.egress_url:
            host = self.settings.egress_host or self.settings.gateway_host or self.settings.host
            try:
                await self.listen(host, relay=False)
            except OSError as e:  # keep the control plane up; proxied sandboxes fail to connect and say so
                log.error("egress proxy: can't listen on %s:%d: %s", host, self.port, e)
        prov = self.fleet.providers.get("docker")
        net = getattr(prov, "network", None) if prov is not None else None
        if net is not None:
            with contextlib.suppress(Exception):
                if ip := await net.egress_gateway(create=False):
                    await self.listen(ip)

    async def listen(self, host: str, relay: bool = True) -> None:
        """Serve the proxy on host:HANGAR_EGRESS_PORT (idempotent). With `relay` (the Docker egress network, whose
        sandboxes reach only its gateway address) also relay the git gateway's port there."""
        if self._listen_lock is None:
            self._listen_lock = asyncio.Lock()
        if self._closing_at is not None:  # --- egress2: shutting down
            raise OSError("hangar is shutting down")
        if self._watcher is None and (closing := getattr(self.fleet, "closing", None)) is not None:
            self._watcher = asyncio.create_task(self._close_on(closing))
        async with self._listen_lock:
            if (host, self.port) not in self._servers:
                self._servers[(host, self.port)] = await asyncio.start_server(
                    self._serve, host, self.port, limit=HEAD_MAX * 2, reuse_address=True)
                log.info("egress proxy listening on %s:%d", host, self.port)
            gw = self.settings.gateway_port
            if relay and self.settings.gateway_url and gw and (host, gw) not in self._servers:
                bind = self.settings.gateway_host or self.settings.host
                if bind not in ("0.0.0.0", "::", "", host):  # the gateway itself already answers there otherwise
                    self._servers[(host, gw)] = await asyncio.start_server(
                        lambda r, w: self._relay(r, w, bind, gw), host, gw, reuse_address=True)
            if self._flusher is None or self._flusher.done():
                self._flusher = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """--- egress2: drain. No new connection from the shutdown signal on (close_listeners); open tunnels (a
        model's stream mid-answer) get until HANGAR_EGRESS_DRAIN_SECONDS after it to finish, and are cut then."""
        closing = getattr(self.fleet, "closing", None)
        shutdown = closing is not None and closing.is_set()  # a process shutdown, not a stop of the proxy alone
        self.close_listeners()
        loop = asyncio.get_running_loop()
        deadline = (self._closing_at or loop.time()) + (self.settings.egress_drain_seconds if shutdown else 0)
        while self._conns and loop.time() < deadline:
            await asyncio.sleep(0.1)
        for w in list(self._conns):
            with contextlib.suppress(Exception):
                w.close()
        for s in self._servers.values():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(s.wait_closed(), 5)
        self._servers.clear()
        if self._flusher:
            self._flusher.cancel()
        if self._watcher:
            self._watcher.cancel()
            self._watcher = None
        self._closing_at = None  # a proxy stopped on its own (tests, a restart in process) may listen again
        self.flush()

    async def _close_on(self, closing: asyncio.Event) -> None:
        await closing.wait()
        self.close_listeners()

    def close_listeners(self) -> None:
        """Stop accepting connections (the shutdown signal: Fleet.closing); those open carry on."""
        if self._closing_at is None:
            self._closing_at = asyncio.get_running_loop().time()
        for s in self._servers.values():
            s.close()

    def invalidate(self, sandbox_id: str | None = None) -> None:
        """Forget cached policies (all, or one sandbox's): an approval applies to the next connection."""
        if sandbox_id is None:
            self._policies.clear()
        else:
            self._policies.pop(sandbox_id, None)

    # ------------------------------------------------------------------ one connection
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Connections per source address, authenticated or not: one sandbox holding sockets open (or trickling
        # request heads) can't use up the proxy for the others.
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        if self._total >= MAX_CONNECTIONS or self._peers[peer] >= 2 * self.settings.egress_max_connections_per_sandbox:
            writer.close()
            return
        self._total += 1
        self._peers[peer] += 1
        self._conns.add(writer)
        try:
            await self._handle(reader, writer)
        except Exception:  # noqa: BLE001 - one connection must never take the listener down
            log.exception("egress proxy: connection failed")
        finally:
            self._conns.discard(writer)
            self._total -= 1
            self._peers[peer] -= 1
            if self._peers[peer] <= 0:
                del self._peers[peer]
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_SECONDS)
            method, target, version, headers = parse_head(raw)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError, ValueError):
            with contextlib.suppress(Exception):
                writer.write(_response(400, "hangar egress: not an HTTP proxy request"))
                await writer.drain()
            return
        sb = self._authenticate(header(headers, "proxy-authorization"), peer)
        if sb is None:
            writer.write(_response(407, "hangar egress: this proxy is for hangar sandboxes; use the HTTPS_PROXY "
                                        "hangar set", {"Proxy-Authenticate": 'Basic realm="hangar egress"'}))
            await writer.drain()
            return
        try:
            if method == "CONNECT":
                host, port = split_hostport(target, None)
                origin = None
            else:
                u = urlsplit(target)
                if u.scheme != "http" or not u.hostname:
                    raise Denied(400, "bad_request", "only CONNECT and absolute http:// requests go through this "
                                 "proxy", "protocol")
                host, port = split_hostport(u.netloc.rpartition("@")[2], 80)
                origin = (u.path or "/") + (f"?{u.query}" if u.query else "")
        except ValueError as e:  # egress.EgressError included: not a hostname
            return await self._refuse(writer, sb, target[:200], 0, Denied(400, "bad_request", str(e), "protocol"))
        except Denied as d:
            return await self._refuse(writer, sb, target[:200], 0, d)
        pol: egress.Policy | None = None
        try:
            pol, rule = self._decide(sb, host, port)
            first = b"" if origin is None else self._rewrite(method, origin, version, headers, host, port)
            if self._open[sb.id] >= self.settings.egress_max_connections_per_sandbox:
                raise Denied(429, "too_many", f"this sandbox has {self._open[sb.id]} connections open through the "
                             "proxy already", "limit")
            who = f"person:{sb.person or '-'}"  # --- egress2: one person's sandboxes share a cap, so 6 people fit
            if self._open[who] >= self.settings.egress_max_connections_per_person:
                raise Denied(429, "too_many_person", f"{sb.person or 'this'}'s sandboxes have {self._open[who]} "
                             "connections open through the proxy already", "limit")
        except Denied as d:
            return await self._refuse(writer, sb, host, port, d, pol.mode if pol else None)
        self._open[sb.id] += 1  # taken before connecting, so a burst of CONNECTs can't all pass the check
        self._open[who] += 1
        metrics.egress_open(1)
        n = [0, 0]  # bytes out (to the internet), in
        relayed = False
        up_w: asyncio.StreamWriter | None = None
        try:
            try:
                up_r, up_w = await self._connect(host, port)
            except Denied as d:
                return await self._refuse(writer, sb, host, port, d, pol.mode)
            tls = False
            if origin is None:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                try:
                    first, tls = await asyncio.wait_for(self._first(reader, host), HEAD_SECONDS)
                except Denied as d:
                    self._denied(sb, host, port, d, pol.mode)  # the 200 is sent: close without an answer
                    return
                except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                    self._denied(sb, host, port, Denied(400, "no_hello", "the client sent no TLS ClientHello or HTTP "
                                                        "request", "protocol"), pol.mode)
                    return
            self._allowed(sb, host, port, rule, pol.mode)
            relayed = True
            up_w.write(first)
            n[0] += len(first)
            if tls:  # --- egress2: a HelloRetryRequest brings a second ClientHello in clear; it is checked too
                try:
                    await self._retry_hello(reader, writer, up_r, up_w, host, n)
                except Denied as d:
                    self._denied(sb, host, port, d, pol.mode)
                    return
            await self._splice(reader, writer, up_r, up_w, n)
        finally:
            for k in (sb.id, who):
                self._open[k] -= 1
                if self._open[k] <= 0:
                    del self._open[k]
            metrics.egress_open(-1)
            metrics.egress_bytes(n[0], n[1])
            if relayed:  # the bytes go to the current summary, even if it was flushed while the tunnel was open
                u = self._usage.setdefault((sb.id, host, port), [0, 0, 0, rule.text, rule.source, pol.mode])
                u[1] += n[0]
                u[2] += n[1]
            if up_w is not None:
                with contextlib.suppress(Exception):
                    up_w.close()

    def _authenticate(self, value: str | None, peer: str) -> Sandbox | None:
        if self._failures is None:
            from hangar.gateway import _AuthFailures

            self._failures = _AuthFailures(AUTH_FAILURES, AUTH_WINDOW)
        if not value or self._failures.blocked(peer):
            return None
        scheme, _, cred = value.partition(" ")
        sb = None
        if scheme.lower() == "basic":
            try:
                user, _, password = base64.b64decode(cred.strip(), validate=True).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                user = password = ""
            if egress.check_token(self.key, user, password):
                sb = self.fleet.store.get(Sandbox, user)
                if sb is not None and sb.state not in (SandboxState.STARTING, SandboxState.RUNNING):
                    sb = None
        if sb is None and self._failures.add(peer):
            log.warning("egress proxy: %s sent %d invalid credentials in %ds; refusing it for now", peer,
                        AUTH_FAILURES, int(AUTH_WINDOW))
            self.fleet.audit("egress", "egress.auth.blocked", None, source=peer)
        return sb

    def _policy(self, sb: Sandbox) -> egress.Policy:
        hit = self._policies.get(sb.id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        pol = egress.for_sandbox(self.fleet, sb)
        if len(self._policies) > 10_000:
            self._policies.clear()
        self._policies[sb.id] = (time.monotonic() + POLICY_SECONDS, pol)
        return pol

    def _decide(self, sb: Sandbox, host: str, port: int) -> tuple[egress.Policy, egress.Rule]:
        try:
            pol = self._policy(sb)
        except Exception as e:  # noqa: BLE001 - its environment is gone or unreadable: deny
            raise Denied(403, "no_policy", f"no egress policy for this sandbox ({type(e).__name__})", "policy") from e
        rule = pol.allows(host, port)
        # --- egress2: a registry the package mirror stands in for: say so, rather than suggest asking for the host
        if rule is None and (eco := next((e for e in pol.mirrors if host in egress.PRESETS[e]), None)):
            raise Denied(403, "mirrored", f"{host} isn't reachable from a sandbox: {eco} packages come through "
                         "hangar's read-only mirror, which the sandbox's package tools are set up for (PIP_INDEX_URL, "
                         "NPM_CONFIG_REGISTRY, GOPROXY in ~/.hangar/env)", "policy")
        if rule is None:
            raise Denied(403, "not_allowed",
                         f"{host}:{port} is not on this sandbox's egress allowlist (mode {pol.mode}, environment "
                         f"{sb.environment}). To ask the owner for it: {egress.request_hint(host, port)}", "policy")
        return pol, rule

    async def _connect(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Resolve the name here and connect to an address checked here, so a sandbox can't reach the VPC, the
        cluster or the metadata server through an allowed name (or one that re-resolves after the check)."""
        private_ok = host in {h.lower() for h in self.settings.egress_private_hosts}
        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), CONNECT_SECONDS)
            except (OSError, TimeoutError) as e:
                raise Denied(502, "dns", f"could not resolve {host}", "upstream") from e
            addrs = list(dict.fromkeys(ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos))
        ok = [a for a in addrs if public(a) or (private_ok and reachable_private(a))]
        if not ok:
            raise Denied(403, "private_address", f"{host} resolves to a private or reserved address; hangar's "
                         "egress proxy reaches public addresses only (HANGAR_EGRESS_PRIVATE_HOSTS lists exceptions)",
                         "policy")
        last: Exception | None = None
        for a in ok[:4]:
            try:
                return await asyncio.wait_for(asyncio.open_connection(str(a), port, limit=BUF), CONNECT_SECONDS)
            except (OSError, TimeoutError) as e:
                last = e
        raise Denied(502, "connect", f"could not connect to {host}:{port} ({type(last).__name__})", "upstream")

    async def _first(self, reader: asyncio.StreamReader, host: str) -> tuple[bytes, bool]:
        """The client's first bytes in a tunnel, checked to name `host`, and whether they are TLS: a whole ClientHello
        (egress_tls: one SNI, the CONNECT host, no ECH), or an HTTP request whose Host is `host`."""
        head = await reader.readexactly(5)
        if head[0] == egress_tls.HANDSHAKE:
            try:
                raw, msg = await egress_tls.read_hello(reader, head)
                egress_tls.check(msg, host)
            except egress_tls.Refused as e:
                raise Denied(403, e.reason, str(e), "sni") from None
            return raw, True
        if head[0] & 0x80 and head[2] == egress_tls.CLIENT_HELLO:  # an SSL 2.0-style hello: no extensions, no SNI
            raise Denied(403, "old_tls", "an SSL 2.0-style ClientHello carries no server name; use TLS 1.2 or 1.3",
                         "sni")
        rest = await reader.readuntil(b"\r\n\r\n")
        try:
            _, _, _, headers = parse_head(head + rest)
            h = header(headers, "host")
            if h is not None and split_hostport(h, 80)[0] == host:
                return head + rest, False
        except ValueError:
            raise Denied(403, "not_tls_or_http", "only TLS or HTTP goes through this proxy", "protocol") from None
        raise Denied(403, "host_mismatch", f"the HTTP request's Host isn't {host}", "sni")

    async def _retry_hello(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter, ur: asyncio.StreamReader,
                           uw: asyncio.StreamWriter, host: str, n: list[int]) -> None:
        """--- egress2: pass the server's first record to the client; when it is a HelloRetryRequest, the client's
        second ClientHello must pass the same checks as the first before it goes upstream. Raises Denied."""
        try:
            rec = await asyncio.wait_for(ur.readexactly(5), HEAD_SECONDS)
        except asyncio.IncompleteReadError as e:
            rec = e.partial  # the server closed: pass on what it sent, the splice ends the tunnel
        except TimeoutError:
            raise Denied(504, "no_server_hello", f"{host} didn't answer the ClientHello", "upstream") from None
        size = int.from_bytes(rec[3:5], "big") if len(rec) == 5 else 0
        if rec[:1] == bytes([egress_tls.HANDSHAKE]) and 0 < size <= egress_tls.RECORD_MAX:
            try:
                rec += await asyncio.wait_for(ur.readexactly(size), HEAD_SECONDS)
            except asyncio.IncompleteReadError as e:
                rec += e.partial
            except TimeoutError:
                raise Denied(504, "no_server_hello", f"{host} didn't finish its ServerHello", "upstream") from None
        cw.write(rec)
        n[1] += len(rec)
        await cw.drain()
        if not egress_tls.is_hello_retry(rec):
            return
        try:
            raw, msg = await asyncio.wait_for(egress_tls.read_second_hello(cr), HEAD_SECONDS)
            egress_tls.check(msg, host)
        except egress_tls.Refused as e:
            raise Denied(403, e.reason, f"second ClientHello: {e}", "sni") from None
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            raise Denied(400, "no_hello", "the client sent no second ClientHello after a HelloRetryRequest",
                         "protocol") from None
        uw.write(raw)
        n[0] += len(raw)

    @staticmethod
    def _rewrite(method: str, origin: str, version: str, headers: list[tuple[str, str]], host: str,
                 port: int) -> bytes:
        """A proxied plain-HTTP request in origin form, one request per connection (Connection: close)."""
        h = header(headers, "host")
        if h is not None:
            try:
                if split_hostport(h, 80)[0] != host:
                    raise Denied(403, "host_mismatch", f"the Host header isn't {host}", "sni")
            except ValueError as e:
                raise Denied(400, "bad_request", "bad Host header", "protocol") from e
        lines = [f"{method} {origin} {version}"]
        lines += [f"{k}: {v}" for k, v in headers if k.lower() not in _HOP and k.lower() != "host"]
        lines += [f"Host: {host}" + (f":{port}" if port != 80 else ""), "Connection: close"]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    async def _splice(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter, ur: asyncio.StreamReader,
                      uw: asyncio.StreamWriter, n: list[int]) -> None:
        loop = asyncio.get_running_loop()
        last = [loop.time()]
        for w in (cw, uw):  # bound each socket read too (see BUF); a transport without the attribute just ignores it
            with contextlib.suppress(Exception):
                if getattr(w.transport, "max_size", 0) > BUF:
                    w.transport.max_size = BUF

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, i: int) -> None:
            try:
                while data := await src.read(BUF):
                    dst.write(data)
                    n[i] += len(data)
                    last[0] = loop.time()
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    if dst.can_write_eof():
                        dst.write_eof()

        down = asyncio.create_task(pipe(ur, cw, 1))
        tasks = {asyncio.create_task(pipe(cr, uw, 0)), down}
        try:
            while tasks:
                done, tasks = await asyncio.wait(tasks, timeout=min(60.0, IDLE_SECONDS / 4))
                if down in done and tasks:  # the far side is done: the client gets a moment, then the tunnel goes
                    _, tasks = await asyncio.wait(tasks, timeout=CLOSE_GRACE_SECONDS)
                    break
                if tasks and loop.time() - last[0] > IDLE_SECONDS:
                    break
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(BaseException):
                    await t

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, port: int) -> None:
        """Docker egress network: the git gateway's port on the network's gateway address, relayed as bytes to
        where the gateway listens (it authenticates every request itself)."""
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_SECONDS)
        except (OSError, TimeoutError):
            writer.close()
            return
        self._conns.add(writer)
        try:
            await self._splice(reader, writer, ur, uw, [0, 0])
        finally:
            self._conns.discard(writer)
            uw.close()
            writer.close()

    # ------------------------------------------------------------------ accounting
    async def _refuse(self, writer: asyncio.StreamWriter, sb: Sandbox, host: str, port: int, d: Denied,
                      mode: str | None = None) -> None:
        self._denied(sb, host, port, d, mode)
        with contextlib.suppress(Exception):
            writer.write(_response(d.status, f"hangar egress: {d}",
                                   {"Retry-After": "5"} if d.status == 429 else None))
            await writer.drain()

    def _who(self, sb: Sandbox) -> dict[str, Any]:
        return {"person": sb.person, "environment": sb.environment, "session": sb.labels.get("hangar.session"),
                "job": sb.labels.get("hangar.job")}

    def _denied(self, sb: Sandbox, host: str, port: int, d: Denied, mode: str | None = None) -> None:
        hit = self._policies.get(sb.id)
        mode = mode or (hit[1].mode if hit else "unknown")
        metrics.egress_connection("deny", d.reason, mode)
        now_ = time.monotonic()
        q = self._deny_log.setdefault(sb.id, collections.deque())
        while q and q[0] < now_ - 60:
            q.popleft()
        if len(q) >= DENY_AUDITS_PER_MINUTE:
            self._deny_dropped[sb.id] += 1
            return
        q.append(now_)
        dropped = self._deny_dropped.pop(sb.id, 0)
        detail = {"host": host, "port": port, "reason": d.reason, "mode": mode, "message": str(d)[:300],
                  **self._who(sb)}
        if dropped:
            detail["suppressed_before"] = dropped
        self.fleet.audit(f"sandbox:{sb.id}", "egress.deny", sb.id, **detail)
        self._event(sb, f"egress denied: {host}:{port} ({d.reason})", {"egress": "deny", "host": host, "port": port,
                                                                       "reason": d.reason,
                                                                       "hint": egress.request_hint(host, port)})

    def _allowed(self, sb: Sandbox, host: str, port: int, rule: egress.Rule, mode: str) -> None:
        metrics.egress_connection("allow", rule.source.split(":")[0], mode)
        key = (sb.id, host, port)
        u = self._usage.get(key)
        if u is None:
            if len(self._usage) > 50_000:
                self.flush()
            u = self._usage[key] = [0, 0, 0, rule.text, rule.source, mode]
        u[0] += 1

    def record(self, sb: Sandbox, host: str, port: int, rule: str, mode: str, out: int, in_: int) -> None:
        """--- egress2: a request hangar served the sandbox itself (the package mirror), summed into the same
        egress.allow entries as proxied traffic."""
        metrics.egress_connection("allow", "mirror", mode)
        if len(self._usage) > 50_000:
            self.flush()
        u = self._usage.setdefault((sb.id, host, port), [0, 0, 0, rule, "mirror", mode])
        u[0] += 1
        u[1] += out
        u[2] += in_

    def _event(self, sb: Sandbox, text: str, data: dict[str, Any]) -> None:
        sid = sb.labels.get("hangar.session")
        if not sid or self.activity is None:
            return
        from hangar.events import RunEvent

        with contextlib.suppress(Exception):
            self.activity.append(sid, [RunEvent(kind="progress", text=text, data=data)])

    def flush(self) -> None:
        """Write the allowed traffic summed since the last flush to the audit log, one entry per sandbox and host."""
        usage, self._usage = self._usage, {}
        for (sid, host, port), (count, out, in_, rule, source, mode) in usage.items():
            sb = self.fleet.store.get(Sandbox, sid)
            who = self._who(sb) if sb is not None else {}
            with contextlib.suppress(Exception):
                self.fleet.audit(f"sandbox:{sid}", "egress.allow", sid, host=host, port=port, connections=count,
                                 bytes_out=out, bytes_in=in_, rule=rule, source=source, mode=mode, **who)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                self.flush()
                self._deny_log = {k: v for k, v in self._deny_log.items() if v}
                metrics.loop_tick("egress")
            except Exception:  # noqa: BLE001
                log.exception("egress proxy: flushing the audit summary failed")
                metrics.loop_failed("egress")
