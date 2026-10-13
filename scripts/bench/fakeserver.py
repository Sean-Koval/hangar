"""The full hangar app (REST, MCP, sessions, jobs) on the threaded fake provider, for the load test.

    uv run python scripts/bench/fakeserver.py --data DIR --port PORT [--latency-ms 0]

Started by control.py in its own process, so its CPU time and memory are its own. Adds GET /bench/stats:
event-loop lag (how late a 50 ms timer fires; the loop is blocked for that long), threads, RSS and CPU time.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import resource
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import uvicorn
from _common import pct
from fakeprov import ThreadedFake

# A Claude-like stream-json agent: an event every 0.5 s for a few seconds, a conversation file, one commit.
AGENT = r"""#!/usr/bin/env bash
conv="$HOME/.fake/sessions"; mkdir -p "$conv"
id="${HANGAR_SESSION_RESUME:-$HANGAR_SESSION_NEW}"
echo '{"type":"system","subtype":"init","model":"fake"}'
for i in $(seq 1 ${BENCH_STEPS:-6}); do
  usage='"usage":{"input_tokens":100,"output_tokens":20}'
  echo "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"step $i\"}],$usage}}"
  [ -n "$id" ] && echo "{\"step\":$i}" >> "$conv/$id.jsonl"
  sleep 0.5
done
echo "$(date +%s%N)" > "work-$(date +%s%N).txt" && git add -A && git -c user.email=b@b -c user.name=b commit -qm step
echo '{"type":"result","result":"done","usage":{"input_tokens":600,"output_tokens":120}}'
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--latency-ms", type=float, default=0.0)
    p.add_argument("--watchdog-ms", type=float, default=0.0, help="record the loop thread's stack when blocked")
    a = p.parse_args()

    from hangar import fleet as fleet_mod
    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.harnesses import HARNESSES, Harness

    data = Path(a.data)
    root = data / "world"
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.sh").write_text(AGENT)
    HARNESSES["scripted"] = Harness(name="scripted", title="Scripted", run=f"bash {root / 'agent.sh'}",
                                    session_dirs=[".fake/sessions"], resume="preset")
    envs = root / "envs"
    envs.mkdir(exist_ok=True)
    (envs / "loadenv.yaml").write_text("name: loadenv\nharnesses: [scripted]\n")
    fake = ThreadedFake(a.latency_ms)
    fleet_mod.load_provider = lambda name, settings: fake  # every provider name resolves to the fake

    s = Settings(data_dir=data / "state", env_dirs=[envs], default_provider="docker", enabled_providers=[],
                 admin_token="bench-admin", max_sandboxes=500, reaper_interval_seconds=3600)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    lag: list[float] = []
    cpu0 = os.times()

    beat = [time.perf_counter()]
    blocked: collections.Counter[str] = collections.Counter()

    async def monitor() -> None:
        while True:
            t = time.perf_counter()
            await asyncio.sleep(0.05)
            beat[0] = time.perf_counter()
            lag.append(beat[0] - t - 0.05)

    def watchdog(main_id: int) -> None:
        """When the loop has not ticked for --watchdog-ms, record what the event-loop thread is doing."""
        while a.watchdog_ms:
            time.sleep(a.watchdog_ms / 4000)
            if running and time.perf_counter() - beat[0] > a.watchdog_ms / 1000:
                frames = sys._current_frames()
                frame = frames.get(main_id)
                stack = traceback.extract_stack(frame)[-12:] if frame else []
                lines = [f"{f.filename.split('site-packages/')[-1]}:{f.lineno} {f.name}" for f in stack]
                for tid, fr in frames.items():  # what the other threads are doing meanwhile
                    if tid not in (main_id, threading.get_ident()):
                        top = traceback.extract_stack(fr)[-3:]
                        lines.append("  other: " + " < ".join(f"{f.filename.split('/')[-1]}:{f.lineno} {f.name}"
                                                             for f in reversed(top)))
                blocked["\n".join(lines)] += 1

    running: list[asyncio.Task] = []

    @app.get("/bench/stats")
    async def stats(reset: bool = False):
        if not running:  # FastAPI ignores startup hooks when the app has a lifespan; start on first call
            running.append(asyncio.get_running_loop().create_task(monitor()))
        ms = [x * 1000 for x in lag]
        t = os.times()
        out = {"loop_lag_p50_ms": round(pct(ms, 50), 2), "loop_lag_p95_ms": round(pct(ms, 95), 2),
               "loop_lag_max_ms": round(max(ms), 2) if ms else None, "samples": len(ms),
               "threads": threading.active_count(),
               "rss_mb": round(int(next(ln.split()[1] for ln in open("/proc/self/status")
                                        if ln.startswith("VmRSS"))) / 1024, 1),
               "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
               "cpu_s": round(t.user + t.system - cpu0.user - cpu0.system, 2)}
        if a.watchdog_ms:
            out["blocked_stacks"] = [{"samples": n, "stack": k.splitlines()} for k, n in blocked.most_common(8)]
        if reset:
            lag.clear()
            blocked.clear()
        return out

    threading.Thread(target=watchdog, args=(threading.get_ident(),), daemon=True).start()

    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    finally:
        fake.kill_all()
        subprocess.run(["rm", "-rf", str(fake.root)])


if __name__ == "__main__":
    main()
