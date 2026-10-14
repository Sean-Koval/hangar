"""Streaming versus polling on the real Docker provider (workstream stream; docs/streaming.md).

    uv run python scripts/bench/streams.py [--runs 5] [--out bench/results/streams-docker.json]

Needs Docker and the hangar-envd binary (envd/build.sh); uses the `minimal` environment from envs/.

A command that prints a line every 100 ms for 3 s, each line stamped with the time the sandbox printed it. From the
MCP call to the first line a client can show (`first_output_*`), and for every line, from printed to seen
(`line_latency_*`; Docker shares the host's clock):
  *_exec_stream          exec(stream=true): progress notifications (in-process MCP client)
  *_exec                 exec without stream: the result, once the command has ended
  *_terminal_poll_1s     terminal_open, then terminal_read every second (how a client watched a running command
                         before this workstream)
  *_process_attach       process start, then attach(wait_seconds): the background buffer, streamed

Turn events, from a run log line being written to its event being recorded (and pushed to live readers), for a real
Claude Code stream-json log replayed a line every 100 ms:
  event_latency_follow   the turn watcher following the log through hangar-envd (`tail -F`)
  event_latency_poll     the turn watcher reading it every session_poll_seconds (5 s), as before
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import tempfile
import time
from pathlib import Path

from _common import ROOT, Recorder, emit

TICKS = "for i in $(seq 30); do sleep 0.1; echo tick$i $(date +%s.%N); done"
LOG = "/home/agent/.hangar/runs/run-0000beef.log"


def _lines(text: str, seen: set[str], now: float, out: list[float]) -> None:
    """The delay of each line not seen before: `now` minus the time the sandbox printed it."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("tick") and parts[0] not in seen:
            try:
                out.append(max(0.0, now - float(parts[1])))
            except ValueError:
                continue
            seen.add(parts[0])


class Watcher:
    """A progress callback that records the first notification and each line's delay."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.first: float | None = None
        self.lat: list[float] = []
        self.seen: set[str] = set()

    async def __call__(self, progress: float, total: float | None, message: str | None) -> None:
        if self.first is None:
            self.first = time.perf_counter() - self.t0
        _lines(message or "", self.seen, time.time(), self.lat)


async def outputs(rec: Recorder, fleet, sb, runs: int) -> None:
    from mcp import Client

    from hangar.mcp_server import build_mcp

    async with Client(build_mcp(fleet)) as c:
        for _ in range(runs):
            w = Watcher()
            res = await c.call_tool("exec", {"sandbox": sb.id, "command": TICKS, "stream": True},
                                    progress_callback=w)
            assert not res.is_error and res.structured_content["streamed"], res
            rec.add("first_output_exec_stream", w.first)
            for x in w.lat:
                rec.add("line_latency_exec_stream", x)

            t0 = time.perf_counter()
            res = await c.call_tool("exec", {"sandbox": sb.id, "command": TICKS})
            rec.add("first_output_exec", time.perf_counter() - t0)
            lat: list[float] = []
            _lines(res.structured_content["stdout"], set(), time.time(), lat)
            for x in lat:
                rec.add("line_latency_exec", x)

            t0, first, seen, lat = time.perf_counter(), None, set(), []
            await c.call_tool("terminal_open", {"sandbox": sb.id, "session": "bench", "command": TICKS})
            while len(seen) < 30 and time.perf_counter() - t0 < 15:
                r = await c.call_tool("terminal_read", {"sandbox": sb.id, "session": "bench", "lines": 100})
                _lines(r.content[0].text, seen, time.time(), lat)
                if seen and first is None:
                    first = time.perf_counter() - t0
                await asyncio.sleep(1.0)
            rec.add("first_output_terminal_poll_1s", first)
            for x in lat:
                rec.add("line_latency_terminal_poll_1s", x)
            await c.call_tool("terminal_close", {"sandbox": sb.id, "session": "bench"})

            w = Watcher()
            p = await c.call_tool("process", {"sandbox": sb.id, "action": "start", "command": TICKS})
            await c.call_tool("process", {"sandbox": sb.id, "action": "attach", "wait_seconds": 10,
                                          "process_id": p.structured_content["id"]}, progress_callback=w)
            rec.add("first_output_process_attach", w.first)
            for x in w.lat:
                rec.add("line_latency_process_attach", x)


async def event_latency(rec: Recorder, fleet, sb, follow: bool) -> None:
    """Replay a real Claude Code log into the sandbox a line every 100 ms, stamping each write; the turn watcher
    polls every session_poll_seconds as a turn's loop does (and, following, records events in between too)."""
    from hangar.events import Activity, TurnWatcher
    from hangar.models import Session, SessionTurn

    fleet.settings.stream_turn_logs = follow
    lines = (ROOT / "tests" / "fixtures" / "streams" / "claude-code.jsonl").read_bytes().splitlines(keepends=True)
    await fleet.write_file(sb.id, "/tmp/replay.jsonl", b"".join(lines))
    done = LOG.removesuffix(".log") + ".exit"
    await fleet.exec(sb.id, f"mkdir -p {LOG.rsplit('/', 1)[0]}; rm -f {done} /tmp/stamps; : > {LOG}")
    activity = Activity(fleet.settings, lambda x: x)
    turn = SessionTurn(message="m", run_id="run-0000beef", harness="claude-code")
    s = Session(name="bench", environment="minimal", sandbox_id=sb.id, turns=[turn], harness="claude-code")
    w = TurnWatcher(activity, fleet, s, turn, lambda: None)
    writer = (f"while IFS= read -r l; do printf '%s\\n' \"$l\" >> {LOG}; date +%s.%N >> /tmp/stamps; sleep 0.1; "
              f"done < /tmp/replay.jsonl; echo 0 > {done}")
    await fleet.exec(sb.id, f"nohup bash -c {shlex.quote(writer)} >/dev/null 2>&1 &")
    deadline = time.time() + len(lines) * 0.1 + 20
    while time.time() < deadline:
        await w.poll()
        if (await fleet.exec(sb.id, f"test -s {done}")).exit_code == 0:
            break
        await asyncio.sleep(fleet.settings.session_poll_seconds)
    await w.finish()
    stamps = [float(x) for x in (await fleet.exec(sb.id, "cat /tmp/stamps")).stdout.split()]
    starts, off = {}, 0
    for i, line in enumerate(lines):
        starts[off] = i
        off += len(line)
    name = "event_latency_follow" if follow else "event_latency_poll"
    for ev in activity.read(s.id, limit=1000):
        i = starts.get(ev.off) if ev.off is not None else None
        if i is not None and i < len(stamps):
            rec.add(name, max(0.0, ev.ts - stamps[i]))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--out")
    args = ap.parse_args()

    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.envd import find_binaries

    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    if not find_binaries(Settings(envd_dir=envd_dir)):
        raise SystemExit("no hangar-envd binary: run envd/build.sh")
    data = Path(tempfile.mkdtemp(prefix="bench-streams-", dir="/dev/shm" if Path("/dev/shm").is_dir() else None))
    s = Settings(data_dir=data, env_dirs=[ROOT / "envs"], reaper_interval_seconds=3600, envd_dir=envd_dir)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    rec = Recorder("streams")
    try:
        sb = await fleet.create("minimal", name="bench-streams", labels={"bench-streams": "1"}, actor="bench")
        assert await fleet.provider("docker").envd.ready(fleet.get(sb.id)) is not None
        await outputs(rec, fleet, sb, args.runs)
        await event_latency(rec, fleet, sb, follow=True)
        await event_latency(rec, fleet, sb, follow=False)
    finally:
        await fleet.destroy_many(label="bench-streams")
        await fleet.stop()
    rec.note("command", TICKS)
    rec.note("session_poll_seconds", s.session_poll_seconds)
    emit(rec.result(provider="docker"), args.out)


if __name__ == "__main__":
    asyncio.run(main())
