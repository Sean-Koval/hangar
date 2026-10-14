"""The full hangar app (REST, MCP, sessions, jobs) on the threaded fake provider, for the load test.

    uv run python scripts/bench/fakeserver.py --data DIR --port PORT [--latency-ms 0]

Started by control.py and team_load.py in its own process, so its CPU time and memory are its own. Adds GET
/bench/stats: event-loop lag (how late a 50 ms timer fires; the loop is blocked for that long), threads, RSS, CPU
time and (--- scale) the time spent in SQLite, per store method and table, on the event loop and off it.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
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


def seed_history(db: Path, n: int) -> None:
    """A team's months of history: `n` settled sandboxes with the run records and stage notes real ones keep, and
    n/4 finished jobs with a timeline. Written straight into the database, in one transaction."""
    import sqlite3

    from hangar.models import Job, JobEvent, JobState, Sandbox, SandboxState
    from hangar.store import Store

    Store(db)  # creates the tables (and whatever indexes this version of the store keeps)
    t0 = time.time() - 90 * 86400
    runs = [{"run_id": f"run-{i}", "harness": "claude-code", "started_at": t0, "exit_code": 0} for i in range(8)]
    sbs = []
    for i in range(n):
        sb = Sandbox(name=f"old-{i}", environment="loadenv", provider="docker", provider_ref=f"gone-{i}",
                     state=SandboxState.STOPPED if i % 7 else SandboxState.ERROR, created_by=f"mcp:eng-{i % 6}",
                     labels={"hangar.session": f"ses-old{i}"}, created_at=t0 + i, last_activity=t0 + i,
                     info={"harness": "claude-code", "runs": runs, "repo_path": "/workspace/repo"})
        sbs.append((sb.id, sb.model_dump_json()))
    jobs = []
    for i in range(n // 4):
        j = Job(title=f"old job {i}", instructions="fix the thing " * 20, environment="loadenv", owner=f"eng-{i % 6}",
                requester=f"eng-{i % 6}", branch=f"hangar/old-{i}", state=JobState.SUCCEEDED, created_at=t0 + i,
                events=[JobEvent(state="running", message=f"step {k}", ts=t0 + i + k) for k in range(30)])
        jobs.append((j.id, j.model_dump_json()))
    con = sqlite3.connect(db)
    with con:
        con.executemany("INSERT INTO sandboxes(key, data) VALUES(?, ?)", sbs)
        con.executemany("INSERT INTO jobs(key, data) VALUES(?, ?)", jobs)
    con.close()


class SqlTimer:
    """Wraps the Store's methods: calls, total and worst time per method and table, split by whether the call ran
    on the event loop (it blocked every request meanwhile) or in a worker thread."""

    def __init__(self) -> None:
        from hangar.store import _TABLES, Store

        self.main = threading.get_ident()
        self.stats: dict[str, list[float]] = {}
        for name in [m for m in vars(Store) if not m.startswith("_") and not m.endswith("_json")
                     and callable(getattr(Store, m))]:
            orig = getattr(Store, name)

            def wrapped(store, *args, _orig=orig, _name=name, **kw):
                t = time.perf_counter()
                try:
                    return _orig(store, *args, **kw)
                finally:
                    dt = time.perf_counter() - t
                    cls = args[0] if args and isinstance(args[0], type) else type(args[0]) if args else None
                    table = _TABLES.get(cls, ("",))[0] if cls in _TABLES else ""
                    where = "loop" if threading.get_ident() == self.main else "thread"
                    st = self.stats.setdefault(f"{where}:{_name}:{table}".rstrip(":"), [0, 0.0, 0.0])
                    st[0] += 1
                    st[1] += dt
                    st[2] = max(st[2], dt)

            setattr(Store, name, wrapped)

    def reset(self) -> None:
        self.stats.clear()

    def report(self) -> dict:
        rows = sorted(self.stats.items(), key=lambda kv: -kv[1][1])
        loop = sum(v[1] for k, v in rows if k.startswith("loop:"))
        return {"loop_total_s": round(loop, 3),
                "top": {k: {"calls": v[0], "total_ms": round(v[1] * 1000, 1), "max_ms": round(v[2] * 1000, 2)}
                        for k, v in rows[:15]}}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--latency-ms", type=float, default=0.0)
    p.add_argument("--watchdog-ms", type=float, default=0.0, help="record the loop thread's stack when blocked")
    # --- scale: team_load.py
    p.add_argument("--steps", type=int, default=6, help="agent events per turn, 0.5 s apart")
    p.add_argument("--history", type=int, default=0,
                   help="settled sandbox rows to seed (plus a quarter as many finished jobs) before starting")
    p.add_argument("--set", action="append", default=[], metavar="FIELD=JSON", help="a Settings override")
    a = p.parse_args()

    from hangar import fleet as fleet_mod
    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.harnesses import HARNESSES, Harness

    data = Path(a.data)
    root = data / "world"
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.sh").write_text(AGENT.replace("${BENCH_STEPS:-6}", str(a.steps)))
    HARNESSES["scripted"] = Harness(name="scripted", title="Scripted", run=f"bash {root / 'agent.sh'}",
                                    session_dirs=[".fake/sessions"], resume="preset")
    envs = root / "envs"
    envs.mkdir(exist_ok=True)
    (envs / "loadenv.yaml").write_text("name: loadenv\nharnesses: [scripted]\n")
    fake = ThreadedFake(a.latency_ms)
    fleet_mod.load_provider = lambda name, settings: fake  # every provider name resolves to the fake

    overrides = {k: json.loads(v) for k, _, v in (x.partition("=") for x in a.set)}
    s = Settings(**{"data_dir": data / "state", "env_dirs": [envs], "default_provider": "docker",
                    "enabled_providers": [], "admin_token": "bench-admin", "max_sandboxes": 500,
                    "reaper_interval_seconds": 3600, **overrides})
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    if a.history:
        seed_history(s.db_path, a.history)
    sql = SqlTimer()
    app = create_app(s)
    lag: list[float] = []
    cpu0 = os.times()
    since = [time.monotonic()]

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
               "cpu_s": round(t.user + t.system - cpu0.user - cpu0.system, 2), "sqlite": sql.report()}
        span = max(1e-3, time.monotonic() - since[0])
        out["provider_calls"] = {k: {"n": n, "per_s": round(n / span, 2)} for k, n in sorted(fake.calls.items())}
        if a.watchdog_ms:
            out["blocked_stacks"] = [{"samples": n, "stack": k.splitlines()} for k, n in blocked.most_common(8)]
        if reset:
            lag.clear()
            blocked.clear()
            sql.reset()
            fake.calls.clear()
            since[0] = time.monotonic()
        return out

    threading.Thread(target=watchdog, args=(threading.get_ident(),), daemon=True).start()

    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    finally:
        fake.kill_all()
        subprocess.run(["rm", "-rf", str(fake.root)])


if __name__ == "__main__":
    main()
