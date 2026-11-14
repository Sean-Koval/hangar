"""The local runner's egress sidecar (KAN-472): the only way out of a local agent container's network.

`hangar env local` (localenv.py) runs this file in a stock Python container, pinned by digest, beside an agent that
sits on an internal Docker network. It is standard library only, so it needs no image of its own, and it decides
nothing: localenv.py asks `egress.resolve` for the policy a hangar sandbox of the environment would get and passes
its rules in `EGRESS_RULES` (JSON `[[host, wildcard, port], ...]`).

The rule match and the public-address test live here, once: `egress.Rule.matches` and `egress_proxy.public` call
them, so the control plane's proxy and this sidecar give the same answers.

It serves two things, a subset of the control plane's proxy (docs/environments/local-run.md):

* an HTTP CONNECT proxy on `EGRESS_PORT`. Anything but CONNECT gets 405, so plain HTTP fails closed. The host and
  port must match a rule, and the name must resolve to public addresses only. The tunnel goes to an address it
  checked. It doesn't read the TLS hello (no SNI check).
* a DNS server on `DNS_PORT` (53) that answers every query REFUSED. The agent's Docker resolver forwards to it, so the
  agent resolves no name on the internet, and DNS can't carry data out. Names resolve only here, for CONNECT.

Each decision is one line on stderr.
"""

from __future__ import annotations

import ipaddress
import json
import os
import select
import socket
import socketserver
import sys
import threading

DEFAULT_PORTS = (443, 80)  # a rule without a port: HTTPS, and plain HTTP for mirrors that still serve it

# IPv6 ranges that carry an IPv4 address in their last 32 bits and reach it wherever a translator exists: NAT64's
# well-known prefix (RFC 6052), IPv4-compatible (RFC 4291, deprecated) and SIIT's IPv4-translated (RFC 2765). Python
# calls them global.
_EMBEDS_V4 = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96"))

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


# ============================================================================ the shared decisions
def rule_matches(rule_host: str, wildcard: bool, rule_port: int | None, host: str, port: int,
                 default_ports: tuple[int, ...] = DEFAULT_PORTS) -> bool:
    """One rule against a normalized host and a port: the exact host, or (`wildcard`) any name below `rule_host`
    but not itself. A rule without a port allows `default_ports`."""
    if (port not in default_ports) if rule_port is None else port != rule_port:
        return False
    if wildcard:
        return host.endswith("." + rule_host)
    return host == rule_host


def addresses(ip: Address) -> list[Address]:
    """The address and any IPv4 address inside it (mapped, 6to4, NAT64, compatible, translated): a connection may
    end up at either, so both must pass."""
    out: list[Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            out.append(ip.ipv4_mapped)
        elif ip.sixtofour:
            out.append(ip.sixtofour)
        elif any(ip in n for n in _EMBEDS_V4):
            out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def public(ip: Address) -> bool:
    addrs = addresses(ip)
    if len(addrs) > 1:  # judged by the IPv4 address it reaches; 6to4 needs a relay hangar doesn't trust
        return not (isinstance(ip, ipaddress.IPv6Address) and ip.sixtofour) and addrs[1].is_global \
            and not addrs[1].is_multicast and not ip.is_loopback
    return ip.is_global and not ip.is_multicast


def allowed(rules: list[list], host: str, port: int) -> bool:
    """Whether any of `rules` ([host, wildcard, port], as `egress.resolve` made them) lets host:port through."""
    host = host.strip().lower().rstrip(".")
    return any(rule_matches(h, bool(w), p, host, port) for h, w, p in rules)


def public_address(host: str, port: int) -> str | None:
    """The first address `host` resolves to, when every address it resolves to is public; else None."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    addrs = list(dict.fromkeys(i[4][0].split("%")[0] for i in infos))
    if not addrs or not all(public(ipaddress.ip_address(a)) for a in addrs):
        return None
    return addrs[0]


def log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# ============================================================================ the CONNECT proxy
class Proxy(socketserver.StreamRequestHandler):
    rules: list[list] = []

    def _reply(self, status: str) -> None:
        self.wfile.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())

    def handle(self) -> None:
        line = self.rfile.readline(8192).decode("latin-1").strip()
        for _ in range(100):  # the rest of the request head
            if not self.rfile.readline(8192).strip():
                break
        parts = line.split()
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            log(f"egress deny: not CONNECT: {line[:120]!r}")
            return self._reply("405 Method Not Allowed")
        host, _, port_text = parts[1].rpartition(":")
        host = host.strip("[]").lower().rstrip(".")
        port = int(port_text) if port_text.isdigit() and 0 < int(port_text) < 65536 else 0
        if not allowed(self.rules, host, port):
            log(f"egress deny: {host}:{port} is not on the allowlist")
            return self._reply("403 Forbidden")
        addr = public_address(host, port)
        if addr is None:
            log(f"egress deny: {host}:{port} does not resolve to public addresses only")
            return self._reply("403 Forbidden")
        try:
            upstream = socket.create_connection((addr, port), timeout=15)
        except OSError as e:
            log(f"egress fail: {host}:{port}: {e}")
            return self._reply("502 Bad Gateway")
        log(f"egress allow: {host}:{port}")
        self._reply("200 Connection Established")
        self._relay(self.connection, upstream)

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket) -> None:
        a.settimeout(None)
        b.settimeout(None)
        try:
            while True:
                ready, _, _ = select.select([a, b], [], [], 900)
                if not ready:
                    return
                for s in ready:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            return
        finally:
            b.close()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ============================================================================ the DNS server that refuses
def refused(query: bytes) -> bytes | None:
    """The REFUSED answer to a DNS query (RFC 1035 4.1.1: QR set, RCODE 5, the question echoed), or None when it
    isn't one."""
    if len(query) < 12 or query[2] & 0x80:  # too short for a header, or a response
        return None
    flags = bytes([0x80 | (query[2] & 0x79), 0x05])  # QR, the opcode and RD kept; AA, TC off; RA off; REFUSED
    return query[:2] + flags + query[4:6] + b"\0\0\0\0\0\0" + _question(query)


def _question(query: bytes) -> bytes:
    """The first question of a query (the name and its type and class), or nothing when malformed."""
    if query[4:6] == b"\0\0":
        return b""
    i = 12
    while i < len(query) and query[i] and query[i] < 64:
        i += query[i] + 1
    end = i + 5
    return query[12:end] if i < len(query) and query[i] == 0 and end <= len(query) else b""


def qname(query: bytes) -> str:
    labels, i = [], 12
    while i < len(query) and 0 < query[i] < 64:
        labels.append(query[i + 1:i + 1 + query[i]].decode("ascii", "replace"))
        i += query[i] + 1
    return ".".join(labels) or "?"


class DnsUdp(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        if (answer := refused(data)) is not None:
            log(f"dns refuse: {qname(data)}")
            sock.sendto(answer, self.client_address)


class DnsTcp(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        size = int.from_bytes(self.rfile.read(2) or b"\0\0", "big")
        data = self.rfile.read(size) if size else b""
        if (answer := refused(data)) is not None:
            log(f"dns refuse: {qname(data)}")
            self.wfile.write(len(answer).to_bytes(2, "big") + answer)


class UdpServer(socketserver.ThreadingUDPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    Proxy.rules = [[str(h), bool(w), p] for h, w, p in json.loads(os.environ.get("EGRESS_RULES") or "[]")]
    port = int(os.environ.get("EGRESS_PORT") or 3128)
    dns_port = int(os.environ.get("DNS_PORT") or 53)
    if dns_port:
        for srv in (UdpServer(("0.0.0.0", dns_port), DnsUdp), Server(("0.0.0.0", dns_port), DnsTcp)):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
    with Server(("0.0.0.0", port), Proxy) as server:
        log(f"egress sidecar: proxy on :{port}, {len(Proxy.rules)} rule(s); dns on :{dns_port or 'off'}, refusing all")
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
