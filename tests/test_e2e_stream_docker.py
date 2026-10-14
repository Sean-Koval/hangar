"""Streaming on the real Docker provider (workstream stream): a command's output, a background process, a file watch
and a followed turn log, all through hangar-envd in a container; and the streams' end when the sandbox goes.

Opt-in (needs Docker and the binary from envd/build.sh):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_stream_docker.py -v
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from mcp import Client

from hangar import sandbox_streams
from hangar.envd import find_binaries
from hangar.streams import hub

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

ROOT = Path(__file__).parents[1]


async def test_streams_in_a_docker_sandbox(tmp_path):
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.events import Activity, TurnWatcher
    from hangar.mcp_server import build_mcp
    from hangar.models import Session, SessionTurn

    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    if not find_binaries(Settings(envd_dir=envd_dir)):
        pytest.skip("no hangar-envd binary (run envd/build.sh)")
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs"], reaper_interval_seconds=3600, envd_dir=envd_dir,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    try:
        sb = await fleet.create("minimal", name="e2e-stream", labels={"e2e-stream": "1"}, actor="test")
        assert await fleet.provider("docker").envd.ready(fleet.get(sb.id)) is not None
        async with Client(build_mcp(fleet)) as c:
            # exec(stream=true): lines arrive one by one, in order, while the command runs
            got: list[tuple[float, str]] = []
            t0 = time.monotonic()

            async def progress(p, total, message):
                got.append((time.monotonic() - t0, message))

            res = await c.call_tool("exec", {"sandbox": sb.id, "stream": True,
                                             "command": "for i in $(seq 10); do echo tick$i; sleep 0.1; done"},
                                    progress_callback=progress)
            out = res.structured_content
            assert out["streamed"] and out["exit_code"] == 0 and "".join(m for _, m in got) == out["stdout"]
            assert len(got) >= 5 and got[0][0] < 0.5, got

            # a background process in the container, with input and a kill
            p = (await c.call_tool("process", {"sandbox": sb.id, "action": "start",
                                               "command": "while read l; do echo got:$l; done"})).structured_content
            await c.call_tool("process", {"sandbox": sb.id, "action": "input", "process_id": p["id"],
                                          "input": "x\n"})
            a = (await c.call_tool("process", {"sandbox": sb.id, "action": "attach", "process_id": p["id"],
                                               "wait_seconds": 1})).structured_content
            assert a["output"] == "got:x\n" and a["transport"] == "envd"

            # a watch on the workspace
            uri = f"hangar://sandboxes/{sb.id}/watch?path=/workspace"
            async with c.listen(resource_subscriptions=[uri]):
                for _ in range(100):
                    v = json.loads((await c.read_resource(uri)).contents[0].text)
                    if v["state"] == "watching":
                        break
                    await asyncio.sleep(0.05)
                await fleet.exec(sb.id, "echo hi > /workspace/seen.txt")
                for _ in range(100):
                    v = json.loads((await c.read_resource(uri)).contents[0].text)
                    if any(e["name"] == "seen.txt" for e in v["events"]):
                        break
                    await asyncio.sleep(0.05)
                assert any(e["name"] == "seen.txt" for e in v["events"]), v

        # a turn log followed in the container: events between polls
        log = "/home/agent/.hangar/runs/run-0000beef.log"
        lines = (ROOT / "tests" / "fixtures" / "streams" / "claude-code.jsonl").read_bytes().splitlines(True)
        await fleet.write_file(sb.id, "/tmp/replay.jsonl", b"".join(lines))
        await fleet.exec(sb.id, f"mkdir -p /home/agent/.hangar/runs; head -2 /tmp/replay.jsonl > {log}")
        activity = Activity(s, lambda x: x)
        turn = SessionTurn(message="m", run_id="run-0000beef", harness="claude-code")
        ses = Session(name="e2e", environment="minimal", sandbox_id=sb.id, turns=[turn], harness="claude-code")
        w = TurnWatcher(activity, fleet, ses, turn, lambda: None)
        await w.poll()
        before = len(activity.read(ses.id, limit=1000))
        await fleet.exec(sb.id, f"tail -n +3 /tmp/replay.jsonl >> {log}")
        for _ in range(100):
            if len(activity.read(ses.id, limit=1000)) > before + 3:
                break
            await asyncio.sleep(0.05)
        assert len(activity.read(ses.id, limit=1000)) > before + 3, "nothing arrived between polls"
        await fleet.exec(sb.id, "echo 0 > /home/agent/.hangar/runs/run-0000beef.exit")
        await w.finish()

        # destroying the sandbox ends a streamed command at once
        async with hub(fleet).open("t", "exec", sb.id) as st:
            async def emit(kind, data):
                pass
            task = asyncio.create_task(sandbox_streams.run(fleet, sb.id, "echo hi; sleep 30", cwd=None, timeout=60,
                                                           actor="t", st=st, emit=emit))
            await asyncio.sleep(1)
            await fleet.destroy(sb.id)
            res = await asyncio.wait_for(task, 15)
        assert res["closed"] == "destroyed"
        procs = sandbox_streams.processes(fleet)
        assert all(x.state != "running" for x in procs.procs.values())
    finally:
        await fleet.destroy_many(label="e2e-stream")
        await fleet.stop()
