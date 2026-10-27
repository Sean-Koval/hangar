"""The stock e2b SDK against hangar on the real Docker provider: the E2B API creates a Docker sandbox, hangar-envd
starts in it, and commands, files, a terminal and an app's port go through the sandbox router.

Opt-in (needs Docker, the `minimal` environment's image or the time to build it, and envd/build.sh's binary):
    HANGAR_E2E=1 uv run --extra e2b pytest tests/test_e2e_e2b_docker.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from hangar.envd import find_binaries
from tests.test_mcp_modern import AppThread

pytestmark = [pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run"),
              pytest.mark.needs("docker")]
e2b = pytest.importorskip("e2b")

ROOT = Path(__file__).parents[1]
DOMAIN = "e2b.test"


async def test_e2b_sdk_on_docker(tmp_path):
    from e2b import AsyncSandbox, CommandExitException, PtySize

    from hangar.app import create_app
    from hangar.config import Settings

    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    if not find_binaries(Settings(envd_dir=envd_dir)):
        pytest.skip("no hangar-envd binary (run envd/build.sh)")
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs"], reaper_interval_seconds=3600, envd_dir=envd_dir,
                 admin_token="e2e-admin-token", e2b_api_domain=DOMAIN,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    srv = AppThread(app)
    h = app.state.hangar
    url = f"http://127.0.0.1:{srv.port}"
    key, _ = h.auth.create("e2e", ["sandboxes"])
    opts = {"api_key": key, "api_url": f"{url}/e2b", "sandbox_url": f"{url}/e2b/sandbox", "retries": 0,
            "request_timeout": 600}
    sb = None
    try:
        sb = await AsyncSandbox.create(template="minimal", timeout=600, envs={"FROM_E2B": "yes"}, **opts)
        r = await sb.commands.run("whoami; echo $FROM_E2B; cat /proc/1/cgroup >/dev/null && hostname")
        assert r.stdout.split()[:2] == ["agent", "yes"], r
        with pytest.raises(CommandExitException):
            await sb.commands.run("false")
        await sb.files.write("/workspace/e2b/hello.txt", "hi from the sdk")
        assert await sb.files.read("/workspace/e2b/hello.txt") == "hi from the sdk"
        assert (await sb.commands.run("stat -c %U /workspace/e2b/hello.txt")).stdout == "agent\n"
        assert [e.name for e in await sb.files.list("/workspace/e2b")] == ["hello.txt"]

        out = []
        t = await sb.pty.create(size=PtySize(rows=24, cols=80), on_data=out.append)
        await sb.pty.send_stdin(t.pid, b"echo $((6*7)); exit\n")
        await t.wait()
        assert b"42" in b"".join(out)

        # An app's port, through the router, with the traffic token (the container's address on this host).
        await sb.commands.run("cd /workspace/e2b && python3 -m http.server 8000 >/dev/null 2>&1", background=True)
        host = {"Host": f"8000-{sb.sandbox_id}.{DOMAIN}", "e2b-traffic-access-token": sb.traffic_access_token}
        async with httpx.AsyncClient(base_url=url) as c:
            for _ in range(100):
                r = await c.get("/hello.txt", headers=host)
                if r.status_code == 200:
                    break
                await asyncio.sleep(0.1)
            assert r.text == "hi from the sdk"

        # Pause freezes the container; connect thaws it, and its processes are still there.
        bg = await sb.commands.run("sleep 600", background=True)
        await sb.pause()
        sb = await AsyncSandbox.connect(sb.sandbox_id, **opts)
        assert bg.pid in [p.pid for p in await sb.commands.list()]
        await sb.kill()
        assert h.fleet.get(sb.sandbox_id).state.value == "stopped"
        sb = None
    finally:
        if sb is not None:
            await AsyncSandbox.kill(sb.sandbox_id, **opts)
        srv.stop()
