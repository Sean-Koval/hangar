"""Control-plane load test: many concurrent sessions and jobs on the fake provider, over REST.

    uv run python scripts/bench/control.py [--sessions 50] [--jobs 50] [--latency-ms 0] [--out FILE]

Starts the real app (fakeserver.py) in its own process. The fake provider runs local bash in worker threads like
the real providers, so what is measured is hangar's own overhead: the event loop, the thread pool, SQLite, polling.
Phases, all at once: N sessions start a turn, M jobs run (deliver=none, verify=true); then every session is
suspended and sent a second message (checkpoint + restore under load). Throughout, probes time API calls.

Metrics (p50/p95): api_* latency per route under load (and api_idle_* before it), exec through the API under
load, session_first_output / job_first_output (request -> first agent event, from server timestamps),
session_turn / job_total, turn_end_detect (agent exited -> hangar noticed), resume_first_output (send to a
suspended session -> first event). Notes: event-loop lag, threads, RSS, CPU seconds of the server process.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from _common import Recorder, emit

from tests.conftest import scratch_root

HERE = Path(__file__).parent
HEADERS = {"Authorization": "Bearer bench-admin"}
ROUTES = {"healthz": "/healthz", "sessions": "/api/sessions", "sandboxes": "/api/sandboxes", "jobs": "/api/jobs"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_remote(root: Path) -> str:
    remote, seed = root / "remote.git", root / "seed"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    subprocess.run([*git, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    return f"file://{remote}"


class Prober:
    """Keeps timing API calls until stopped; samples go to rec under `prefix`."""

    def __init__(self, c: httpx.AsyncClient, rec: Recorder, prefix: str):
        self.c, self.rec, self.prefix = c, rec, prefix
        self.stop = asyncio.Event()
        self.exec_target: str | None = None

    async def routes(self) -> None:
        while not self.stop.is_set():
            for name, path in ROUTES.items():
                t = time.perf_counter()
                r = await self.c.get(path)
                r.raise_for_status()
                self.rec.add(f"{self.prefix}_{name}", time.perf_counter() - t)
            await asyncio.sleep(0.1)

    async def execs(self) -> None:
        while not self.stop.is_set():
            if self.exec_target:
                t = time.perf_counter()
                r = await self.c.post(f"/api/sandboxes/{self.exec_target}/exec", json={"command": "true"})
                if r.status_code == 200:
                    self.rec.add(f"{self.prefix}_exec", time.perf_counter() - t)
            await asyncio.sleep(0.25)


async def wait_all(c: httpx.AsyncClient, names: list[str], jobs: list[str], turns: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ss = {s["name"]: s for s in (await c.get("/api/sessions", params={"limit": 500})).json()}
        done = all(n in ss and not ss[n]["busy"] and ss[n]["state"] in ("idle", "failed", "suspended")
                   and len(ss[n]["turns"]) >= turns and ss[n]["turns"][-1].get("finished_at") for n in names)
        js = {j["id"]: j for j in (await c.get("/api/jobs", params={"limit": 500})).json()} if jobs else {}
        done = done and all(js.get(j, {}).get("state") in ("succeeded", "failed", "cancelled", "needs_input",
                                                            "kept", "delivered") for j in jobs)
        if done:
            return
        await asyncio.sleep(1)
    raise TimeoutError("sessions/jobs did not finish in time")


async def first_event_ts(c: httpx.AsyncClient, path: str, turn: int) -> float | None:
    evs = (await c.get(f"{path}/events", params={"limit": 1000})).json()["events"]
    ts = [e["ts"] for e in evs if e.get("turn") == turn]
    return min(ts) if ts else None


async def main(a: argparse.Namespace) -> dict:
    rec = Recorder("control-plane")
    root = Path(tempfile.mkdtemp(prefix="bench-cp-", dir=scratch_root()))
    url = make_remote(root)
    port = free_port()
    srv = subprocess.Popen([sys.executable, str(HERE / "fakeserver.py"), "--data", str(root / "srv"),
                            "--port", str(port), "--latency-ms", str(a.latency_ms),
                            "--watchdog-ms", str(a.watchdog_ms)])
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=HEADERS, timeout=120) as c:
            for _ in range(200):
                try:
                    if (await c.get("/healthz")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            await c.get("/bench/stats", params={"reset": True})
            idle = Prober(c, rec, "api_idle")
            t = asyncio.create_task(idle.routes())
            await asyncio.sleep(3)
            idle.stop.set()
            await t
            await c.get("/bench/stats", params={"reset": True})

            load = Prober(c, rec, "api_load")
            probes = [asyncio.create_task(load.routes()), asyncio.create_task(load.execs())]
            names = [f"load-{i}" for i in range(a.sessions)]
            sent: dict[str, float] = {}
            jobs: dict[str, float] = {}

            async def new_session(n: str) -> None:
                sent[n] = time.time()
                r = await c.post("/api/sessions", json={"name": n, "environment": "loadenv", "repo": url,
                                                        "message": "do the work", "idle_minutes": 0})
                r.raise_for_status()

            async def submit(i: int) -> None:
                t0 = time.time()
                r = await c.post("/api/jobs", json={"instructions": f"job {i}", "environment": "loadenv",
                                                    "repo": url, "deliver": "none", "verify": "true"})
                r.raise_for_status()
                jobs[r.json()["id"]] = t0

            t0 = time.perf_counter()
            await asyncio.gather(*(new_session(n) for n in names), *(submit(i) for i in range(a.jobs)))
            rec.note("submit_all_s", round(time.perf_counter() - t0, 2))

            async def pick_exec_target() -> None:
                while not load.stop.is_set() and load.exec_target is None:
                    sbs = (await c.get("/api/sandboxes")).json()
                    running = [s["id"] for s in sbs if s["state"] == "running"]
                    load.exec_target = running[0] if running else None
                    await asyncio.sleep(0.5)

            asyncio.create_task(pick_exec_target())
            await wait_all(c, names, list(jobs), 1, a.timeout)
            rec.note("phase1_wall_s", round(time.perf_counter() - t0, 2))
            st1 = (await c.get("/bench/stats", params={"reset": True})).json()
            rec.note("server_phase1", st1)

            for n in names:
                s = (await c.get(f"/api/sessions/{n}")).json()
                turn = s["turns"][0]
                if turn.get("exit_code") != 0:
                    rec.note(f"failed_{n}", s.get("reason"))
                    continue
                if (ts := await first_event_ts(c, f"/api/sessions/{n}", 1)) is not None:
                    rec.add("session_first_output", ts - sent[n])
                rec.add("session_turn", turn["finished_at"] - sent[n])
                r = await c.post(f"/api/sandboxes/{s['sandbox_id']}/exec",
                                 json={"command": f"stat -c %.9Y ~/.hangar/runs/{turn['run_id']}.exit"})
                if r.status_code == 200 and r.json()["exit_code"] == 0:
                    rec.add("turn_end_detect", max(0.0, turn["finished_at"] - float(r.json()["stdout"].strip())))
            states: dict[str, int] = {}
            for jid, t_sub in jobs.items():
                j = (await c.get(f"/api/jobs/{jid}")).json()
                states[j["state"]] = states.get(j["state"], 0) + 1
                if (ts := await first_event_ts(c, f"/api/jobs/{jid}", 1)) is not None:
                    rec.add("job_first_output", ts - t_sub)
                if j.get("events"):  # the last event is the terminal state
                    rec.add("job_total", j["events"][-1]["ts"] - t_sub)
            rec.note("job_states", states)

            # ---- phase 2: suspend everything, then a message to each (checkpoint + restore, all at once)
            load.exec_target = None
            t0 = time.perf_counter()
            await asyncio.gather(*(c.post(f"/api/sessions/{n}/suspend") for n in names))
            deadline = time.monotonic() + a.timeout
            while time.monotonic() < deadline:
                ss = (await c.get("/api/sessions", params={"limit": 500})).json()
                if all(s["state"] == "suspended" for s in ss if s["name"] in names):
                    break
                await asyncio.sleep(0.5)
            rec.note("suspend_all_s", round(time.perf_counter() - t0, 2))
            sent2: dict[str, float] = {}

            async def send(n: str) -> None:
                sent2[n] = time.time()
                (await c.post(f"/api/sessions/{n}/send", json={"message": "continue"})).raise_for_status()

            t0 = time.perf_counter()
            await asyncio.gather(*(send(n) for n in names))
            asyncio.create_task(pick_exec_target())
            await wait_all(c, names, [], 2, a.timeout)
            rec.note("phase2_wall_s", round(time.perf_counter() - t0, 2))
            for n in names:
                if (ts := await first_event_ts(c, f"/api/sessions/{n}", 2)) is not None:
                    rec.add("resume_first_output", ts - sent2[n])
                s = (await c.get(f"/api/sessions/{n}")).json()
                if s["turns"][1].get("mode") != "resume" or s["turns"][1].get("exit_code") != 0:
                    rec.note(f"resume_problem_{n}", {"mode": s["turns"][1].get("mode"), "reason": s.get("reason")})
            load.stop.set()
            await asyncio.gather(*probes)
            rec.note("server_phase2", (await c.get("/bench/stats")).json())
    finally:
        srv.terminate()
        try:
            srv.wait(30)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(root, ignore_errors=True)
    return rec.result(provider="fake", sessions=a.sessions, jobs=a.jobs, latency_ms=a.latency_ms)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sessions", type=int, default=50)
    p.add_argument("--jobs", type=int, default=50)
    p.add_argument("--latency-ms", type=float, default=0.0, help="extra per-call provider latency (network RTT)")
    p.add_argument("--timeout", type=float, default=900)
    p.add_argument("--watchdog-ms", type=float, default=200,
                   help="report the server's stack whenever its event loop is blocked this long (0 = off)")
    p.add_argument("--out")
    a = p.parse_args()
    emit(asyncio.run(main(a)), a.out)
