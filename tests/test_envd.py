"""hangar.envd against a real hangar-envd daemon on this machine (and against a hostile fake one).

Needs the binary: `envd/build.sh` (or HANGAR_ENVD_DIR). The Docker end-to-end test is test_e2e_envd_docker.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hangar.config import Settings
from hangar.envd import MAX_OUTPUT, SKIP, Envd, EnvdError, find_binaries
from hangar.models import Sandbox
from hangar.providers.base import ProviderError

ARCH = os.uname().machine
BINARIES = find_binaries(Settings(envd_dir=os.environ.get("HANGAR_ENVD_DIR") or None))
needs_binary = pytest.mark.skipif(ARCH not in BINARIES, reason="no hangar-envd binary (run envd/build.sh)")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalEnvd(Envd):
    """The daemon runs on this machine, as this user: `install` starts it, `urls` points at it."""

    def __init__(self, settings: Settings, port: int, wrong_token: bool = False):
        super().__init__(SimpleNamespace(name="local"), settings)
        self.port, self.installs, self.wrong_token = port, 0, wrong_token

    def token(self, sb: Sandbox) -> str:
        return "not-the-token-0123456789" if self.wrong_token else super().token(sb)

    async def urls(self, sb: Sandbox) -> list[str]:
        return [f"http://127.0.0.1:{self.port}"]

    async def install(self, sb: Sandbox, token: str) -> None:
        self.installs += 1
        argv = [str(self.binaries[ARCH]), "--listen", "127.0.0.1", "--port", str(self.port), "--daemon",
                "--workdir", self.s.workspace_dir]
        p = await asyncio.to_thread(subprocess.run, argv, env={**os.environ, "HANGAR_ENVD_TOKEN": token},
                                    capture_output=True, text=True)
        if p.returncode != 0:
            raise EnvdError(p.stderr.strip())


def kill_daemon(port: int) -> None:
    subprocess.run(["pkill", "-f", f"hangar-envd.*--port {port}"], check=False)


@pytest.fixture
def env(tmp_path):
    port = free_port()
    s = Settings(data_dir=tmp_path / "data", workspace_dir=str(tmp_path), envd_dir=next(iter(BINARIES.values())).parent
                 if BINARIES else None)
    s.data_dir.mkdir()
    e = LocalEnvd(s, port)
    sb = Sandbox(name="t", environment="x", provider="local", provider_ref="local-1")
    yield e, sb, tmp_path
    kill_daemon(port)


@needs_binary
async def test_exec_and_files_go_through_the_daemon(env):
    e, sb, tmp = env
    assert await e.endpoint(sb) is None  # first call: starts it in the background and uses the provider
    assert await e.ready(sb) is not None
    assert sb.info["envd"]["url"].endswith(str(e.port))
    r = await e.exec(sb, "echo hi; echo err >&2; pwd; exit 3")
    assert (r.exit_code, r.stdout, r.stderr, r.truncated) == (3, f"hi\n{tmp}\n", "err\n", False)
    r = await e.exec(sb, "sleep 5", timeout=1)
    assert r.exit_code == 124 and "timed out after 1s" in r.stderr

    data = os.urandom(300_000)
    assert await e.write_file(sb, str(tmp / "d/f.bin"), data, 0o600) is True
    assert (tmp / "d/f.bin").read_bytes() == data
    assert stat.S_IMODE((tmp / "d/f.bin").stat().st_mode) == 0o600
    assert await e.read_file(sb, str(tmp / "d/f.bin")) == data
    assert await e.read_file(sb, str(tmp / "d/f.bin"), limit=1000) == data[:1001]
    assert await e.read_file(sb, str(tmp / "missing")) is None
    assert await e.read_file(sb, str(tmp)) is None  # a directory
    assert e.installs == 1


@needs_binary
async def test_output_is_capped(env):
    e, sb, _ = env
    await e.ready(sb)
    r = await e.exec(sb, "head -c 5000000 /dev/zero | tr '\\0' x; echo; echo tail-marker")
    assert r.exit_code == 0 and r.truncated
    assert len(r.stdout) <= MAX_OUTPUT + 100 and r.stdout.endswith("tail-marker\n")


@needs_binary
async def test_cwd_missing_or_root_user_uses_the_provider(env):
    e, sb, _ = env
    await e.ready(sb)
    assert await e.exec(sb, "true", cwd="/nonexistent/dir") is None  # refused before starting: provider decides
    assert await e.exec(sb, "true", user="root") is None
    assert await e.endpoint(sb) is not None  # neither counts against the daemon


async def test_unreachable_daemon_falls_back(tmp_path):
    s = Settings(data_dir=tmp_path, envd_dir=tmp_path)
    (tmp_path / f"hangar-envd-{ARCH}").write_bytes(b"#!/bin/sh\nexit 1\n")
    e = LocalEnvd(s, free_port())
    sb = Sandbox(name="t", environment="x", provider="local", provider_ref="local-1")
    assert await e.ready(sb) is None
    assert await e.exec(sb, "true") is None
    assert await e.read_file(sb, "/etc/hostname") is SKIP
    assert await e.write_file(sb, str(tmp_path / "x"), b"x") is False
    assert e.installs == 1  # and not again until RETRY_AFTER


@needs_binary
async def test_wrong_token_is_refused(env):
    e, sb, _ = env
    assert await e.ready(sb) is not None
    bad = LocalEnvd(e.s, e.port, wrong_token=True)
    assert await bad.ready(sb) is None  # its probe is refused, and a second daemon can't take the port
    assert await bad.exec(sb, "true") is None


@needs_binary
async def test_a_dead_daemon_is_noticed_and_restarted(env):
    e, sb, _ = env
    await e.ready(sb)
    kill_daemon(e.port)
    await asyncio.sleep(0.1)
    assert await e.exec(sb, "true") is None  # connection refused before anything ran: the provider runs it
    assert await e.ready(sb) is not None
    assert (await e.exec(sb, "echo back")).stdout == "back\n"
    assert e.installs == 2


@needs_binary
async def test_terminal(env):
    e, sb, _ = env
    await e.ready(sb)
    term = await e.open_terminal(sb, ["sh"], 80, 24)
    assert term is not None
    await term.resize(100, 30)
    await term.write(b"stty size; echo $((6*7))\n")
    seen = b""
    while b"42" not in seen or b"30 100" not in seen:
        chunk = await asyncio.wait_for(term.read(), 5)
        assert chunk
        seen += chunk
    await term.close()
    assert await term.read() == b""


# ---------------------------------------------------------------------- a hostile daemon
class FakeDaemon:
    """Answers every request with the same canned HTTP response; `/info` says it is a daemon."""

    def __init__(self, body: bytes, headers: str = "content-type: application/connect+json\r\n"):
        self.body, self.headers = body, headers

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        n = next((int(x.split(b":")[1]) for x in head.split(b"\r\n") if x.lower().startswith(b"content-length")), 0)
        await r.readexactly(n)
        info = b" /info " in head.split(b"\r\n")[0]
        body, headers = (b'{"version":"fake"}', "") if info else (self.body, self.headers)
        w.write(f"HTTP/1.1 200 OK\r\nconnection: close\r\n{headers}content-length: {len(body)}\r\n\r\n".encode() + body)
        await w.drain()
        w.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self.server.close()


def frame(obj: dict | bytes, flags: int = 0, size: int | None = None) -> bytes:
    p = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return bytes([flags]) + (len(p) if size is None else size).to_bytes(4, "big") + p


async def hostile(tmp_path, body: bytes, headers: str | None = None):
    s = Settings(data_dir=tmp_path, envd_dir=tmp_path)
    (tmp_path / f"hangar-envd-{ARCH}").write_bytes(b"")
    fake = FakeDaemon(body, *([headers] if headers else []))
    await fake.__aenter__()
    e = LocalEnvd(s, fake.port)
    sb = Sandbox(name="t", environment="x", provider="local", provider_ref="local-1")
    assert await e.ready(sb) is not None
    return e, sb, fake


async def test_oversized_frames_are_refused(tmp_path):
    e, sb, fake = await hostile(tmp_path, frame(b"x" * 10, size=64 << 20))
    try:
        assert await e.exec(sb, "true") is None  # before the start event: nothing ran
    finally:
        await fake.__aexit__()
    start = frame({"event": {"start": {"pid": 1}}})
    e, sb, fake = await hostile(tmp_path, start + frame(b"x", size=64 << 20))
    try:
        with pytest.raises(ProviderError, match="over the limit"):
            await e.exec(sb, "true")
    finally:
        await fake.__aexit__()


async def test_compressed_or_malformed_output_is_refused(tmp_path):
    start = frame({"event": {"start": {"pid": 1}}})
    for bad in (frame(b"\x1f\x8b", flags=1), frame(b"not json"), frame({"event": {"data": {"stdout": "%%%"}}})):
        e, sb, fake = await hostile(tmp_path, start + bad)
        try:
            with pytest.raises(ProviderError):
                await e.exec(sb, "true")
        finally:
            await fake.__aexit__()


async def test_file_reads_are_not_decompressed_and_stop_at_the_limit(tmp_path):
    import gzip

    bomb = gzip.compress(b"\0" * (20 << 20))
    e, sb, fake = await hostile(tmp_path, bomb, "content-encoding: gzip\r\n")
    try:
        got = await e.read_file(sb, "/x", limit=100)
        assert got == bomb[:101]
        assert await e.read_file(sb, "/x") == bomb  # raw bytes, never 20 MB of zeros
    finally:
        await fake.__aexit__()


def test_binaries_are_found_by_arch(tmp_path):
    (tmp_path / "hangar-envd-x86_64").write_bytes(b"a")
    (tmp_path / "hangar-envd-aarch64").write_bytes(b"b")
    (tmp_path / "hangar-envd-mips").write_bytes(b"c")
    assert set(find_binaries(Settings(envd_dir=tmp_path))) == {"x86_64", "aarch64"}
    assert find_binaries(Settings(envd_dir=Path(tmp_path / "none"))) == {}
    off = LocalEnvd(Settings(envd="off", envd_dir=tmp_path), 1)
    assert not off.enabled
