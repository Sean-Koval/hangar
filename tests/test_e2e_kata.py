"""Every sandbox is a microVM: Docker provider + Kata runtime, driven through the MCP tools.

Opt-in; needs a KVM host with Kata registered in Docker (deploy/local/kata-install.sh):
    HANGAR_E2E_KATA=1 uv run pytest tests/test_e2e_kata.py -v
"""

import os
from pathlib import Path

import pytest

from tests.test_e2e_docker import data

pytestmark = [pytest.mark.skipif(os.environ.get("HANGAR_E2E_KATA") != "1", reason="set HANGAR_E2E_KATA=1 to run"),
              pytest.mark.needs("kvm")]


async def test_microvm_fleet_over_mcp(tmp_path):
    from mcp import Client

    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.mcp_server import build_mcp

    s = Settings(data_dir=tmp_path, env_dirs=[Path(__file__).parents[1] / "envs"], reaper_interval_seconds=3600,
                 docker_runtime=os.environ.get("HANGAR_KATA_RUNTIME", "kata"))
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    await h.fleet.start()
    try:
        async with Client(build_mcp(h.fleet)) as c:
            assert data(await c.call_tool("fleet_overview", {}))["providers"]["docker"]["capabilities"]["microvm"]
            sb = data(await c.call_tool("create_sandbox", {"environment": "minimal", "name": "vm-a",
                                                           "labels": {"e2e": "kata"}}))
            assert sb["state"] == "running"
            r = data(await c.call_tool("exec", {"sandbox": "vm-a", "command": "uname -r"}))
            assert r["stdout"].strip() != os.uname().release, "sandbox shares the host kernel: not a microVM"
            await c.call_tool("write_file", {"sandbox": "vm-a", "path": "hello.txt", "content": "hi\n"})
            await c.call_tool("pause_sandbox", {"sandbox": "vm-a"})
            await c.call_tool("resume_sandbox", {"sandbox": "vm-a"})
            forks = data(await c.call_tool("fork_sandbox", {"source": "vm-a", "count": 2}))
            boot_ids = set()
            for f in forks:
                read = data(await c.call_tool("read_file", {"sandbox": f["id"], "path": "hello.txt"}))
                assert read["content"] == "hi\n"
                cmd = "cat /proc/sys/kernel/random/boot_id"
                boot = data(await c.call_tool("exec", {"sandbox": f["id"], "command": cmd}))
                boot_ids.add(boot["stdout"])
            assert len(boot_ids) == 2, "forks must boot separate VMs"
    finally:
        await h.fleet.destroy_many(label="e2e")
        await h.fleet.stop()
