"""What the egress proxy reads of a TLS handshake it never terminates (--- egress2; docs/security.md#egress-allowlists).

The CONNECT target is a name the sandbox chose, and the TLS server name (SNI) is the only other signal the proxy sees:
everything after the ClientHello is encrypted. So the hello is checked in full before a byte of it is relayed:

* **Exactly one server name, and it is the CONNECT host.** One `server_name` extension, one `host_name` entry (RFC
  6066 3 allows no more), a valid DNS name. No SNI is accepted only when the CONNECT target is an IP address.
* **No Encrypted ClientHello.** With ECH (RFC 9849) the SNI in clear is the front end's public name and the real one
  is encrypted, so an allowed host on a shared front end (Cloudflare offers ECH to every site) would carry a
  connection to any other site there. The `encrypted_client_hello` extension (0xfe0d, RFC 9849 5), its
  `ech_outer_extensions` (0xfd00, only ever inside the encrypted hello) and draft ESNI's `encrypted_server_name`
  (0xffce) are refused. GREASE ECH (RFC 9849 6.2) looks the same on the wire and is refused too: Chromium sends it,
  so hangar's computer image turns ECH off by policy (computer.py).
* **No form that hides the name:** SSL 2.0-style hellos and SSL 3.0 (no extensions), a hello whose extensions don't
  parse, an extension sent twice (RFC 8446 4.2 forbids it; a server might read the second), and anything after the
  hello in the first flight. A hello split over several records (TLS allows it; post-quantum key shares make hellos
  bigger) is reassembled, up to HELLO_MAX.
* **A second hello is checked too.** After a HelloRetryRequest (RFC 8446 4.1.4) the client sends a new ClientHello
  in clear; it gets the same checks, so a first hello without ECH can't be followed by one with it.

What stays out of reach without terminating TLS is in the threat model: an HTTP `Host` or HTTP/2 `:authority` inside
the encrypted stream that names another site, on a front end that doesn't check it against SNI (domain fronting), and
TLS 1.2 renegotiation, whose hello is encrypted.
"""

from __future__ import annotations

import asyncio
from typing import NamedTuple

from hangar import egress

HANDSHAKE, CCS, APPLICATION_DATA = 0x16, 0x14, 0x17
CLIENT_HELLO, SERVER_HELLO = 1, 2
EXT_SERVER_NAME = 0x0000
EXT_ECH = 0xFE0D  # RFC 9849 5: encrypted_client_hello
EXT_ECH_OUTER = 0xFD00  # RFC 9849 5.1: ech_outer_extensions
EXT_ESNI = 0xFFCE  # draft-ietf-tls-esni-00..05: encrypted_server_name
HIDDEN_NAME = {EXT_ECH: "Encrypted ClientHello", EXT_ECH_OUTER: "ECH outer extensions",
               EXT_ESNI: "encrypted SNI (draft ESNI)"}
RECORD_MAX = 16384 + 2048  # a TLSPlaintext fragment is at most 2^14 bytes; leave room for what clients send
HELLO_MAX = 64 * 1024
EARLY_DATA_MAX = 1024 * 1024  # what may precede a second hello (0-RTT data the server will discard)
# RFC 8446 4.1.3: a ServerHello with this random is a HelloRetryRequest.
HRR_RANDOM = bytes.fromhex("cf21ad74e59a6111be1d8c021e65b891c2a211167abb8c5e079e09e2c8a8339c")
ECH_HINT = ("turn ECH off in the client (Chromium: the EncryptedClientHelloEnabled policy, or "
            "--disable-features=EncryptedClientHello)")


class Refused(ValueError):
    """The hello can't go through; `reason` is the audit reason, the message says why."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class Hello(NamedTuple):
    sni: str | None
    version: int  # legacy_version
    extensions: tuple[int, ...]


# ============================================================================ parsing
def _u16(b: bytes, p: int) -> int:
    if p + 2 > len(b):
        raise IndexError
    return int.from_bytes(b[p:p + 2], "big")


def parse_hello(msg: bytes) -> Hello:
    """A ClientHello handshake message (type, 3-byte length, body: RFC 8446 4.1.2), parsed strictly. Raises Refused
    when it isn't one or doesn't parse, or names its server in a way the proxy can't check."""
    try:
        if len(msg) < 4 or msg[0] != CLIENT_HELLO or int.from_bytes(msg[1:4], "big") != len(msg) - 4:
            raise Refused("bad_tls", "the first TLS message isn't a ClientHello")
        body = msg[4:]
        version = _u16(body, 0)
        p = 2 + 32  # legacy_version, random
        p += 1 + body[p]  # legacy_session_id
        p += 2 + _u16(body, p)  # cipher_suites
        p += 1 + body[p]  # legacy_compression_methods
        if p == len(body):
            exts: list[tuple[int, bytes]] = []  # SSL 3.0 and very old clients: no extensions at all
        else:
            end = p + 2 + _u16(body, p)
            if end != len(body):
                raise Refused("bad_tls", "the ClientHello's extensions don't add up")
            p += 2
            exts = []
            while p < end:
                etype, elen = _u16(body, p), _u16(body, p + 2)
                if p + 4 + elen > end:
                    raise Refused("bad_tls", "a ClientHello extension runs past the hello")
                exts.append((etype, body[p + 4:p + 4 + elen]))
                p += 4 + elen
    except IndexError:
        raise Refused("bad_tls", "the ClientHello is truncated") from None
    types = [t for t, _ in exts]
    if len(set(types)) != len(types):
        raise Refused("bad_tls", "the ClientHello repeats an extension")
    for t in types:
        if t in HIDDEN_NAME:
            raise Refused("ech", f"the ClientHello carries {HIDDEN_NAME[t]}, which hides the server it is for; "
                                 f"{ECH_HINT}")
    if version < 0x0301:
        raise Refused("old_tls", "SSL 3.0 and older carry no server name; use TLS 1.2 or 1.3")
    names = [d for t, d in exts if t == EXT_SERVER_NAME]
    return Hello(_server_name(names[0]) if names else None, version, tuple(types))


def _server_name(data: bytes) -> str:
    """The one host_name in a server_name extension (RFC 6066 3)."""
    try:
        if _u16(data, 0) != len(data) - 2 or len(data) < 5:
            raise IndexError
        ntype, nlen = data[2], _u16(data, 3)
        if 5 + nlen != len(data):
            raise Refused("sni", "the ClientHello names more than one server")
        if ntype != 0:
            raise Refused("sni", "the ClientHello's server name isn't a host name")
        return egress.normalize_host(data[5:].decode("ascii"))
    except IndexError:
        raise Refused("bad_tls", "the ClientHello's server name doesn't parse") from None
    except (UnicodeDecodeError, egress.EgressError):
        raise Refused("sni", "the ClientHello's server name isn't a valid host name") from None


def check(msg: bytes, host: str) -> Hello:
    """Parse and check a ClientHello for a tunnel to `host` (normalized). Raises Refused."""
    hello = parse_hello(msg)
    if hello.sni is None:
        if egress._is_ip(host):
            return hello  # clients send no SNI for an IP address
        raise Refused("no_sni", f"the TLS ClientHello names no server; {host} must be in SNI")
    if hello.sni != host:
        raise Refused("sni_mismatch", f"the TLS ClientHello names {hello.sni[:100]}, not {host}")
    return hello


def sni(record: bytes) -> str | None:
    """The server name in a TLS record holding a whole ClientHello, or None. Never raises."""
    try:
        if len(record) < 9 or record[0] != HANDSHAKE:
            return None
        n = int.from_bytes(record[3:5], "big")
        msg = record[5:5 + n]
        return parse_hello(msg[:4 + int.from_bytes(msg[1:4], "big")]).sni
    except (Refused, IndexError, ValueError):
        return None


# ============================================================================ reading from the wire
async def read_record(reader: asyncio.StreamReader, head: bytes = b"") -> bytes:
    """One TLS record (header and fragment), with `head` its first bytes when already read."""
    head += await reader.readexactly(5 - len(head))
    if head[1] != 3:
        raise Refused("bad_tls", "not a TLS record")
    n = int.from_bytes(head[3:5], "big")
    if n > RECORD_MAX or n == 0:
        raise Refused("bad_tls", "malformed TLS record")
    return head + await reader.readexactly(n)


async def read_hello(reader: asyncio.StreamReader, head: bytes = b"") -> tuple[bytes, bytes]:
    """(the records as received, the ClientHello message): handshake records until one whole ClientHello, which may be
    split over several. A second message in the same flight is refused, and so is anything but handshake records."""
    raw, hs = bytearray(), bytearray()
    while True:
        rec = await read_record(reader, head)
        head = b""
        if rec[0] != HANDSHAKE:
            raise Refused("bad_tls", "the ClientHello is interrupted by another TLS record type")
        raw += rec
        hs += rec[5:]
        if len(raw) > HELLO_MAX:
            raise Refused("bad_tls", f"the ClientHello is over {HELLO_MAX // 1024} KB")
        if len(hs) >= 4:
            if hs[0] != CLIENT_HELLO:
                raise Refused("bad_tls", "the first TLS message isn't a ClientHello")
            want = 4 + int.from_bytes(hs[1:4], "big")
            if want > HELLO_MAX:
                raise Refused("bad_tls", f"the ClientHello is over {HELLO_MAX // 1024} KB")
            if len(hs) > want:
                raise Refused("bad_tls", "more than a ClientHello in the first TLS records")
            if len(hs) == want:
                return bytes(raw), bytes(hs)


def is_hello_retry(record: bytes) -> bool:
    """Whether a server's first record starts with a HelloRetryRequest (a ServerHello with HRR_RANDOM)."""
    return len(record) >= 5 + 4 + 2 + 32 and record[0] == HANDSHAKE and record[5] == SERVER_HELLO \
        and record[5 + 4 + 2:5 + 4 + 2 + 32] == HRR_RANDOM


async def read_second_hello(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """After a HelloRetryRequest: the client's records up to and including its second ClientHello. What may come
    before it goes through as it is: a middlebox-compatibility ChangeCipherSpec (RFC 8446 D.4), and early data sent
    with the first hello, which the server discards after a retry (RFC 8446 4.2.10)."""
    skipped = bytearray()
    while True:
        rec = await read_record(reader)
        if rec[0] == HANDSHAKE:
            raw, msg = await read_hello(_Prefixed(rec, reader))  # type: ignore[arg-type]
            return bytes(skipped) + raw, msg
        if rec[0] == CCS and rec[5:] != b"\x01" or rec[0] not in (CCS, APPLICATION_DATA):
            raise Refused("bad_tls", "the client didn't answer the HelloRetryRequest with a ClientHello")
        skipped += rec
        if len(skipped) > EARLY_DATA_MAX:
            raise Refused("bad_tls", "no second ClientHello after the HelloRetryRequest")


class _Prefixed:
    """A reader that first gives back bytes already read."""

    def __init__(self, data: bytes, reader: asyncio.StreamReader):
        self.data, self.reader = bytearray(data), reader

    async def readexactly(self, n: int) -> bytes:
        take = bytes(self.data[:n])
        del self.data[:n]
        return take + (await self.reader.readexactly(n - len(take)) if len(take) < n else b"")
