"""The gateway listener caps open connections per peer, idle ones included (KAN-174, known gap 10)."""

from __future__ import annotations

import asyncio
import socket

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from hangar.gateway import _Server


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _closed(reader: asyncio.StreamReader, wait: float = 1.0) -> bool:
    """True when the server closed the connection (EOF) within `wait`; False if it's still open."""
    try:
        return await asyncio.wait_for(reader.read(1), wait) == b""
    except TimeoutError:
        return False
    except ConnectionError:
        return True


async def _get(port: int) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    await w.drain()
    out = await asyncio.wait_for(r.read(), 5)
    w.close()
    return out


async def test_one_peer_holding_idle_sockets_runs_out_of_its_own_share():
    app = Starlette(routes=[Route("/healthz", lambda r: PlainTextResponse("ok"))])
    port = _port()
    server = _Server(app, "127.0.0.1", port, per_peer=3)
    server.start()
    try:
        for _ in range(100):
            if server.server.started:
                break
            await asyncio.sleep(0.05)
        idle = [await asyncio.open_connection("127.0.0.1", port) for _ in range(3)]  # never send a request
        for r, _ in idle:
            assert not await _closed(r, 0.2)
        r4, w4 = await asyncio.open_connection("127.0.0.1", port)
        assert await _closed(r4), "a fourth connection from the same peer is closed at once"
        w4.close()
        idle[0][1].close()  # one goes away: its slot is free again
        await asyncio.sleep(0.2)
        assert b"200 OK" in await _get(port) and b"ok" in await _get(port)
        for _, w in idle[1:]:
            w.close()
    finally:
        await server.stop()


async def test_no_cap_when_turned_off():
    app = Starlette(routes=[Route("/healthz", lambda r: PlainTextResponse("ok"))])
    port = _port()
    server = _Server(app, "127.0.0.1", port, per_peer=0)
    server.start()
    try:
        for _ in range(100):
            if server.server.started:
                break
            await asyncio.sleep(0.05)
        idle = [await asyncio.open_connection("127.0.0.1", port) for _ in range(10)]
        assert b"200 OK" in await _get(port)
        for _, w in idle:
            w.close()
    finally:
        await server.stop()
