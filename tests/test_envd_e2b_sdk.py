"""The stock e2b Python SDK, pointed straight at a hangar-envd daemon: how far E2B compatibility goes.

    uv run --extra e2b pytest tests/test_envd_e2b_sdk.py

Needs the binary from envd/build.sh. The SDK is built from E2B's own protos and talks Connect over HTTP/2 (h2c)
by default; the daemon is reached the way a future E2B-compatible hangar API would hand it out: the sandbox URL
plus the X-Access-Token header.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from tests.test_envd import ARCH, BINARIES, free_port

e2b = pytest.importorskip("e2b")
pytestmark = pytest.mark.skipif(ARCH not in BINARIES, reason="no hangar-envd binary (run envd/build.sh)")

TOKEN = "e2b-sdk-test-token-0123456789"


@pytest.fixture(params=["2", "1.1"])
async def sandbox(request, tmp_path):
    from e2b.connection_config import ConnectionConfig
    from packaging.version import Version

    port = free_port()
    subprocess.run([str(BINARIES[ARCH]), "--listen", "127.0.0.1", "--port", str(port), "--daemon",
                    "--workdir", str(tmp_path)], env={**os.environ, "HANGAR_ENVD_TOKEN": TOKEN}, check=True,
                   capture_output=True)
    cfg = ConnectionConfig(sandbox_url=f"http://127.0.0.1:{port}", http_version=request.param,
                           extra_sandbox_headers={"X-Access-Token": TOKEN, "E2b-Sandbox-Id": "local"})
    sb = e2b.AsyncSandbox(sandbox_id="local", sandbox_domain=None, envd_version=Version("0.6.4"),
                          envd_access_token=TOKEN, traffic_access_token=None, connection_config=cfg)
    yield sb, tmp_path
    subprocess.run(["pkill", "-f", f"hangar-envd.*--port {port}"], check=False)


async def test_commands(sandbox):
    sb, tmp = sandbox
    r = await sb.commands.run("echo $((6*7)); echo err >&2", timeout=10)
    assert (r.exit_code, r.stdout, r.stderr) == (0, "42\n", "err\n")
    with pytest.raises(e2b.CommandExitException) as ei:
        await sb.commands.run("exit 3", timeout=10)
    assert ei.value.exit_code == 3
    r = await sb.commands.run("pwd; echo $FOO", envs={"FOO": "bar"}, cwd="/tmp")
    assert r.stdout == "/tmp\nbar\n"
    h = await sb.commands.run("sleep 30", background=True)
    assert h.pid in [p.pid for p in await sb.commands.list()]
    assert await sb.commands.kill(h.pid)
    h = await sb.commands.run("read x; echo got $x", background=True, stdin=True)
    await sb.commands.send_stdin(h.pid, "abc\n")
    assert (await h.wait()).stdout == "got abc\n"


async def test_files(sandbox):
    sb, tmp = sandbox
    p = str(tmp / "sdk/w.txt")
    info = await sb.files.write(p, "written by the sdk")
    assert info.path == p
    assert await sb.files.read(p) == "written by the sdk"
    assert bytes(await sb.files.read(p, format="bytes")) == b"written by the sdk"
    assert [e.name for e in await sb.files.list(str(tmp / "sdk"))] == ["w.txt"]
    assert await sb.files.exists(p)
    assert (await sb.files.get_info(p)).size == 18
    assert await sb.files.make_dir(str(tmp / "sdk/d"))
    moved = await sb.files.rename(p, str(tmp / "sdk/d/w2.txt"))
    assert moved.name == "w2.txt"
    await sb.files.remove(str(tmp / "sdk/d"))
    assert not await sb.files.exists(str(tmp / "sdk/d"))


async def test_watch_and_pty(sandbox):
    sb, tmp = sandbox
    events = []
    h = await sb.files.watch_dir(str(tmp), on_event=events.append)
    await sb.files.write(str(tmp / "x.txt"), "x")
    await asyncio.sleep(0.3)
    await h.stop()
    assert ("x.txt", e2b.FilesystemEventType.CREATE) in [(e.name, e.type) for e in events]

    out = []
    t = await sb.pty.create(size=e2b.PtySize(rows=24, cols=80), on_data=out.append)
    await sb.pty.resize(t.pid, e2b.PtySize(rows=30, cols=100))
    await sb.pty.send_stdin(t.pid, b"stty size; exit\n")
    await t.wait()
    assert b"30 100" in b"".join(out)


async def test_wrong_token_is_an_authentication_error(sandbox):
    from e2b.connection_config import ConnectionConfig
    from packaging.version import Version

    sb, _ = sandbox
    cfg = ConnectionConfig(sandbox_url=sb.connection_config._sandbox_url,
                           extra_sandbox_headers={"X-Access-Token": "wrong", "E2b-Sandbox-Id": "local"})
    bad = e2b.AsyncSandbox(sandbox_id="local", sandbox_domain=None, envd_version=Version("0.6.4"),
                           envd_access_token="wrong", traffic_access_token=None, connection_config=cfg)
    with pytest.raises(e2b.AuthenticationException):
        await bad.commands.run("true")
    with pytest.raises(e2b.AuthenticationException):
        await bad.files.read("/etc/hostname")
