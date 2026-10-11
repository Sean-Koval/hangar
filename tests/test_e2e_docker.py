"""End-to-end against a real Docker engine, driven through the MCP tools.

Opt-in (needs Docker and network for the first image build):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_docker.py -v
"""

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")


def data(res):
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    text = res.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def test_fleet_over_mcp(tmp_path):
    from mcp import Client

    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.mcp_server import build_mcp

    s = Settings(data_dir=tmp_path, env_dirs=[Path(__file__).parents[1] / "envs"], reaper_interval_seconds=3600,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    await h.fleet.start()
    try:
        async with Client(build_mcp(h.fleet)) as c:
            await _scenario(c)
    finally:
        await h.fleet.destroy_many(label="e2e")
        await h.fleet.stop()


async def _scenario(c):
    sb = data(await c.call_tool("create_sandbox", {"environment": "minimal", "name": "e2e-a", "labels": {"e2e": "1"}}))
    assert sb["state"] == "running"
    r = data(await c.call_tool("exec", {"sandbox": "e2e-a", "command": "whoami && echo hi > /workspace/f.txt"}))
    assert r["exit_code"] == 0 and r["stdout"].strip() == "agent"
    await c.call_tool("terminal_open", {"sandbox": "e2e-a", "session": "dev"})
    screen = data(await c.call_tool("terminal_send", {"sandbox": "e2e-a", "session": "dev",
                                                      "text": "echo $((6*7))", "wait_for": "^42$", "wait_ms": 5000}))
    assert "42" in screen
    forks = data(await c.call_tool("fork_sandbox", {"source": "e2e-a", "count": 2}))
    assert len(forks) == 2
    for f in forks:
        got = data(await c.call_tool("read_file", {"sandbox": f["id"], "path": "/workspace/f.txt"}))
        assert got["content"] == "hi\n"
    gone = data(await c.call_tool("destroy_sandboxes", {"label": "e2e"}))
    assert len(gone) == 3
