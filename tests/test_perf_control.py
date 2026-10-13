"""Control-plane responsiveness (spec 0004 M1): turn ends noticed from inside the sandbox, a worker pool sized for
blocking provider I/O, cheap list responses, fewer database writes. Fake provider, real tmux and git."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from hangar.auth import TokenAuth
from hangar.harnesses import HARNESSES, Harness
from hangar.models import ApiToken, SessionState
from hangar.runtime import FastJSON, install_io_executor
from hangar.sessions import NewSession
from tests.conftest import scratch_root
from tests.test_session_streams import box, settle, svc, world  # noqa: F401 - fixtures

needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


@needs_tools
async def test_turn_end_is_noticed_without_waiting_a_poll_interval(svc, world, fleet, settings):  # noqa: F811
    settings.session_poll_seconds = 4.0  # the old loop slept this long between looks
    svc.new(NewSession(name="quick", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "quick", timeout=30)
    assert s.state == SessionState.IDLE and s.turns[0].exit_code == 0
    t = s.turns[0]
    exit_file = box(fleet, s.sandbox_id) / "home/agent/.hangar/runs" / f"{t.run_id}.exit"
    lag = t.finished_at - exit_file.stat().st_mtime
    assert lag < 1.5, f"noticed {lag:.2f}s after the agent exited"


@needs_tools
async def test_turns_still_end_when_every_waiter_slot_is_taken(svc, world, fleet, settings):  # noqa: F811
    settings.session_poll_seconds = 0.2
    fleet._waiters = asyncio.Semaphore(1)
    await fleet._waiters.acquire()  # someone else holds the only slot: fall back to sleeping between looks
    svc.new(NewSession(name="busy", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "busy", timeout=30)
    assert s.state == SessionState.IDLE and s.turns[0].exit_code == 0


@needs_tools
async def test_agent_status_long_polls_for_the_end_and_for_first_output(fleet, monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="slow-", dir=scratch_root()))  # fake sandboxes remap /tmp/ paths
    script = root / "slow.sh"
    script.write_text("sleep 0.6; echo first; sleep 0.6; echo done\n")
    monkeypatch.setitem(HARNESSES, "slow", Harness(name="slow", title="Slow", run=f"bash {script}"))
    envs = root / "envs"
    envs.mkdir()
    (envs / "slowenv.yaml").write_text("name: slowenv\nharnesses: [slow]\n")
    fleet.catalog.dirs.insert(0, envs)
    fleet.catalog.reload()
    sb = await fleet.create("slowenv")
    try:
        run = await fleet.run_agent(sb.id, "go")
        t0 = time.monotonic()
        st = await fleet.agent_status(sb.id, run["run_id"], wait_seconds=10, grow_from=0)
        assert st["state"] == "running" and "first" in st["output_tail"] and time.monotonic() - t0 < 3
        st = await fleet.agent_status(sb.id, run["run_id"], wait_seconds=10)
        assert st["state"] == "finished" and st["exit_code"] == 0 and "done" in st["output_tail"]
        assert time.monotonic() - t0 < 4
        t1 = time.monotonic()  # a finished run answers at once
        assert (await fleet.agent_status(sb.id, run["run_id"], wait_seconds=10))["state"] == "finished"
        assert time.monotonic() - t1 < 1
    finally:
        for b in fleet.providers["docker"].boxes.values():
            subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{b}/tmp", "PATH": "/usr/bin:/bin"},
                           capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


async def test_io_pool_runs_many_blocking_calls_at_once(settings):
    settings.io_threads = 64
    pool = install_io_executor(settings)
    try:
        t0 = time.monotonic()
        await asyncio.gather(*(asyncio.to_thread(time.sleep, 0.3) for _ in range(40)))
        assert time.monotonic() - t0 < 1.0  # the default pool (CPUs + 4 threads) takes several rounds
    finally:
        pool.shutdown(wait=False)


def test_fast_json_matches_the_default_encoding():
    content = [{"id": "ses-1", "n": 1.5, "ok": True, "none": None, "uni": "é", "nested": {"l": [1, 2]}},
               {"when": dt.datetime(2026, 10, 13, 1, 2, 3)}]
    body = json.loads(FastJSON(content).body)
    assert body[0] == content[0] and body[1] == {"when": "2026-10-13T01:02:03"}


def test_token_last_used_is_written_at_most_once_a_minute(store):
    auth = TokenAuth(store, None)
    raw, tok = auth.create("ci", scopes=["jobs"])
    writes = []
    put = store.put
    store.put = lambda obj: (writes.append(obj), put(obj))
    for _ in range(5):
        assert auth.principal(raw).name == "ci"
    assert len(writes) == 1
    t = store.get(ApiToken, tok.id)
    t.last_used -= 120
    put(t)
    auth.principal(raw)
    assert len(writes) == 2


def test_store_syncs_in_wal_normal_mode(store):
    assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


async def test_long_polls_cannot_take_every_io_thread(fleet, settings, monkeypatch):
    """`agent_status(wait_seconds)` holds a worker thread for the whole wait (an MCP client may ask for 25 s). However
    many clients wait at once, half the pool stays free for everything else."""
    settings.io_threads = 8
    pool = install_io_executor(settings)
    prov = fleet.providers["docker"]
    exec_ = prov.exec

    async def threaded_exec(sb, cmd, **kw):  # like docker-py and the kubernetes client: a worker thread per call
        return await asyncio.to_thread(asyncio.run, exec_(sb, cmd, **kw))

    monkeypatch.setattr(prov, "exec", threaded_exec)
    sb = await fleet.create("base")
    try:
        polls = [asyncio.create_task(fleet.agent_status(sb.id, "run-0000abcd", wait_seconds=3)) for _ in range(12)]
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        assert (await fleet.exec(sb.id, "echo ok")).stdout.strip() == "ok"
        assert time.monotonic() - t0 < 1.5, "a plain exec queued behind long-polls"
        assert all(st["state"] == "running" for st in await asyncio.gather(*polls))
    finally:
        pool.shutdown(wait=False)
