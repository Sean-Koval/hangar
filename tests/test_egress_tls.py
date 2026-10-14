"""The egress proxy's ClientHello checks (--- egress2): one SNI that is the CONNECT host, no Encrypted ClientHello
(GREASE included), no form that hides the name, and the second hello after a HelloRetryRequest checked like the
first. Parsing on crafted hellos, then the proxy end to end on loopback (the `proxied` fixture of test_egress)."""

from __future__ import annotations

import asyncio
import os

import pytest

from hangar import egress, egress_proxy, egress_tls
from tests.test_egress import ask, audits, auth, client_hello, proxied  # noqa: F401 - the fixture

ECH, ECH_OUTER, ESNI = 0xFE0D, 0xFD00, 0xFFCE


def _ext(etype: int, data: bytes) -> bytes:
    return etype.to_bytes(2, "big") + len(data).to_bytes(2, "big") + data


def sni_ext(*names: str, ntype: int = 0) -> bytes:
    entries = b"".join(bytes([ntype]) + len(n).to_bytes(2, "big") + n.encode() for n in names)
    return _ext(0, len(entries).to_bytes(2, "big") + entries)


def hello(*exts: bytes, version: int = 0x0303, no_extensions: bool = False) -> bytes:
    """A ClientHello handshake message with the given extensions (TLS 1.3 shaped: supported_versions included)."""
    body = version.to_bytes(2, "big") + os.urandom(32) + b"\x20" + os.urandom(32)
    body += (4).to_bytes(2, "big") + b"\x13\x01\x13\x02" + b"\x01\x00"
    if not no_extensions:
        allx = b"".join(exts) + _ext(0x002B, b"\x02\x03\x04")  # supported_versions: TLS 1.3
        body += len(allx).to_bytes(2, "big") + allx
    return b"\x01" + len(body).to_bytes(3, "big") + body


def records(msg: bytes, size: int = 16384, rtype: int = 0x16) -> bytes:
    out = b""
    for i in range(0, len(msg), size):
        frag = msg[i:i + size]
        out += bytes([rtype, 3, 1]) + len(frag).to_bytes(2, "big") + frag
    return out


def grease_ech() -> bytes:
    """What Chromium sends without an ECH config: a well-formed outer ECH extension with random contents."""
    return _ext(ECH, b"\x00" + b"\x00\x01\x00\x01" + b"\x2a" + (32).to_bytes(2, "big") + os.urandom(32)
                + (144).to_bytes(2, "big") + os.urandom(144))


# ============================================================================ parsing
def test_a_real_hello_parses_and_names_its_server():
    msg = client_hello("pypi.org")[5:]
    h = egress_tls.check(msg, "pypi.org")
    assert h.sni == "pypi.org" and 0 in h.extensions
    assert egress_proxy.sni(client_hello("pypi.org")) == "pypi.org"


@pytest.mark.parametrize("etype", [ECH, ECH_OUTER, ESNI])
def test_encrypted_client_hello_and_its_relatives_are_refused_even_with_the_right_sni(etype):
    """GREASE ECH and real ECH look the same: both are refused, whatever the SNI in clear says."""
    data = grease_ech() if etype == ECH else _ext(etype, os.urandom(40))
    with pytest.raises(egress_tls.Refused) as e:
        egress_tls.check(hello(sni_ext("registry.npmjs.org"), data), "registry.npmjs.org")
    assert e.value.reason == "ech" and "EncryptedClientHelloEnabled" in str(e.value)


@pytest.mark.parametrize("msg,reason", [
    (hello(sni_ext("a.example"), sni_ext("a.example")), "bad_tls"),  # the extension twice
    (hello(sni_ext("a.example", "b.example")), "sni"),  # two names in one list
    (hello(sni_ext("a.example", ntype=1)), "sni"),  # not a host_name
    (hello(sni_ext("a.exa\x00mple")), "sni"),
    (hello(sni_ext("a.example"), version=0x0300), "old_tls"),  # SSL 3.0
    (hello(no_extensions=True), "no_sni"),  # nothing names the server
    (hello(sni_ext("b.example")), "sni_mismatch"),
], ids=["sni-twice", "two-names", "not-a-host-name", "nul", "ssl3", "no-extensions", "another-host"])
def test_hellos_that_hide_or_blur_the_server_are_refused(msg, reason):
    with pytest.raises(egress_tls.Refused) as e:
        egress_tls.check(msg, "a.example")
    assert e.value.reason == reason


def test_malformed_hellos_are_refused_and_never_raise_anything_else():
    good = hello(sni_ext("a.example"))
    for bad in (good[:30], good[:-3], b"\x02" + good[1:], good[:4] + b"\x03\x03",
                good[:-6] + b"\xff\xff" + good[-4:]):
        with pytest.raises(egress_tls.Refused):
            egress_tls.check(bad, "a.example")
    for junk in (b"", b"\x16\x03\x01", bytes(range(256)) * 4):
        assert egress_tls.sni(junk) is None


def test_no_sni_is_fine_for_an_ip_address_only():
    msg = hello()
    assert egress_tls.check(msg, "203.0.113.7").sni is None
    with pytest.raises(egress_tls.Refused):
        egress_tls.check(msg, "a.example")


async def _reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


async def test_a_hello_split_over_records_is_reassembled_and_extra_messages_are_refused():
    msg = hello(sni_ext("a.example"), _ext(0x0015, b"\x00" * 3000))  # padding: a big hello, as with PQ key shares
    raw, got = await egress_tls.read_hello(await _reader(records(msg, 700)))
    assert got == msg and raw == records(msg, 700)
    with pytest.raises(egress_tls.Refused):  # a second handshake message in the first flight
        await egress_tls.read_hello(await _reader(records(msg + b"\x01\x00\x00\x00")))
    with pytest.raises(egress_tls.Refused):  # interrupted by another record type
        await egress_tls.read_hello(await _reader(records(msg[:100]) + records(msg[100:], rtype=0x17)))
    with pytest.raises(egress_tls.Refused):  # too big to be a hello
        await egress_tls.read_hello(await _reader(records(b"\x01\x02\x00\x00" + b"\x00" * 0x20000)))


# ============================================================================ the proxy, end to end
async def _connect(p, data: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    first, r, w = await ask(p.px, f"CONNECT upstream.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}", data)
    assert first.startswith(b"HTTP/1.1 200")
    return r, w


async def test_the_proxy_cuts_a_hello_with_ech_before_a_byte_goes_upstream(proxied):  # noqa: F811
    p = proxied
    r, w = await _connect(p, records(hello(sni_ext("upstream.test"), grease_ech())))
    assert await asyncio.wait_for(r.read(), 5) == b""  # closed: the echo server never saw it
    [d] = audits(p.store, "egress.deny")
    assert d.detail["reason"] == "ech" and "Encrypted ClientHello" in d.detail["message"]


async def test_the_proxy_reassembles_a_split_hello_and_refuses_ssl2(proxied):  # noqa: F811
    p = proxied
    msg = hello(sni_ext("upstream.test"))
    r, w = await _connect(p, records(msg, 50))
    assert await asyncio.wait_for(r.read(65536), 5) == b"ECHO:" + records(msg, 50)
    w.close()
    ssl2 = b"\x80\x2e\x01\x00\x02" + b"\x00" * 0x29  # an SSL 2.0-style ClientHello (msg type 1 in byte 2)
    r, w = await _connect(p, ssl2)
    assert await asyncio.wait_for(r.read(), 5) == b""
    assert audits(p.store, "egress.deny")[0].detail["reason"] == "old_tls"


@pytest.fixture
async def retrying(proxied, monkeypatch):  # noqa: F811
    """The proxied fixture, with an upstream that answers the first hello with a HelloRetryRequest and then echoes
    everything after it (so a test sees whether the second hello reached it)."""
    p = proxied
    hrr_body = b"\x03\x03" + egress_tls.HRR_RANDOM + b"\x00\x13\x01\x00\x00\x06\x00\x2b\x00\x02\x03\x04"
    hrr = records(b"\x02" + len(hrr_body).to_bytes(3, "big") + hrr_body)

    async def upstream(reader, writer):
        try:
            await egress_tls.read_hello(reader)
            writer.write(hrr)
            await writer.drain()
            data = await reader.read(65536)
            writer.write(b"SECOND:" + data)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    p.up = server.sockets[0].getsockname()[1]
    egress.approve(p.store, "session", "ses-1", "upstream.test", p.up, "ann")
    p.fleet.egress.invalidate()
    p.hrr = hrr
    yield p
    server.close()


async def test_after_a_hello_retry_the_second_hello_is_checked_like_the_first(retrying):
    p = retrying
    ccs = bytes([0x14, 3, 3, 0, 1, 1])  # middlebox compatibility (RFC 8446 D.4)
    # a good second hello goes through, CCS first
    r, w = await _connect(p, records(hello(sni_ext("upstream.test"))))
    assert await asyncio.wait_for(r.readexactly(len(p.hrr)), 5) == p.hrr
    ok = records(hello(sni_ext("upstream.test")))
    w.write(ccs + ok)
    await w.drain()
    assert await asyncio.wait_for(r.read(65536), 5) == b"SECOND:" + ccs + ok
    w.close()
    # one that adds ECH, or names another host, is cut
    for bad, reason in ((hello(sni_ext("upstream.test"), grease_ech()), "ech"),
                        (hello(sni_ext("evil.example")), "sni_mismatch")):
        r, w = await _connect(p, records(hello(sni_ext("upstream.test"))))
        assert await asyncio.wait_for(r.readexactly(len(p.hrr)), 5) == p.hrr
        w.write(records(bad))
        await w.drain()
        assert await asyncio.wait_for(r.read(), 5) == b""
        assert audits(p.store, "egress.deny")[0].detail["reason"] == reason
