"""Team load test: 6 engineers × 10 sessions on one control plane, over MCP and REST, on the fake provider.

    uv run python scripts/bench/team_load.py [--actors 6] [--sessions 10] [--turns 2] [--jobs 2] \\
        [--history 20000] [--steps 16] [--out FILE]

Each actor has its own token and does what an engineer's MCP client and open dashboard do (docs/team-scale.md):
- starts its sessions over MCP (`new_session` with a message), a second apart, and submits its jobs (`submit_job`);
- polls every session with `get_session` every --poll seconds and sends the next message when a turn ends;
- calls `list_sessions`, `list_sandboxes` and `list_jobs` every --list seconds;
- runs `exec` in one of its running sandboxes every --exec seconds, addressed by name as MCP clients do;
- keeps a dashboard open: GET /api/sessions, /api/sandboxes and /api/jobs every --dashboard seconds;
- and, once a minute, creates and destroys a bare sandbox over REST (the create path's own control-plane cost).

--history seeds months of settled sandboxes and finished jobs first, since hangar keeps them (--job-history and
--session-history set how many jobs and sessions, spread over six months). The server is the
real app (fakeserver.py) in its own process; what is measured is hangar's own overhead. Metrics: p50/p95 of every
MCP tool and REST route above, under load; notes: event-loop lag, RSS, CPU seconds and SQLite time (on the event
loop and in threads, per store method), the sessions' turn times and the jobs' queue waits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import httpx2
from _common import Recorder, emit
from control import free_port, make_remote
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import scratch_root

HERE = Path(__file__).parent
ADMIN = {"Authorization": "Bearer bench-admin"}
DONE_JOB = {"succeeded", "failed", "cancelled", "needs_input"}


def _content(res: Any) -> Any:
    """A tool result's structured content (or its JSON text)."""
    if getattr(res, "is_error", False) or getattr(res, "isError", False):
        raise RuntimeError(str(res.content[0].text if res.content else res))
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


class Actor:
    """One engineer: a token, an MCP client, a dashboard, N sessions and M jobs."""

    def __init__(self, i: int, a: argparse.Namespace, base: str, repo: str, rec: Recorder):
        self.i, self.a, self.base, self.repo, self.rec = i, a, base, repo, rec
        self.name = f"eng-{i}"
        self.sessions = [f"e{i}-s{k}" for k in range(a.sessions)]
        self.jobs: dict[str, float] = {}
        self.turns: dict[str, int] = {}  # session -> turns finished
        self.sent: dict[tuple[str, int], float] = {}  # (session, turn) -> when the message was sent
        self.sandboxes: list[str] = []  # names of this actor's running sandboxes
        self.done = asyncio.Event()
        self.errors: list[str] = []

    async def call(self, mcp: Client, tool: str, args: dict[str, Any]) -> Any:
        t = time.perf_counter()
        try:
            res = await mcp.call_tool(tool, args)
        finally:
            self.rec.add(f"mcp_{tool}", time.perf_counter() - t)
        return _content(res)

    async def rest(self, c: httpx.AsyncClient, method: str, path: str, metric: str, **kw: Any) -> httpx.Response:
        t = time.perf_counter()
        r = await c.request(method, path, **kw)
        self.rec.add(f"rest_{metric}", time.perf_counter() - t)
        return r

    async def run(self, token: str) -> None:
        try:
            await self._run(token)
        except BaseExceptionGroup:
            if not self.done.is_set():
                raise
            # the work is done; the MCP client's stream closing under it on the way out is not a result

    async def _run(self, token: str) -> None:
        a = self.a
        # No keep-alive: uvicorn closes an idle connection after 5 s, and a request sent on it just then fails with a
        # ReadError the MCP client can't survive (a property of the test client, not of hangar). Localhost connects
        # cost ~0.1 ms.
        auth = {"Authorization": f"Bearer {token}"}
        http = httpx2.AsyncClient(headers=auth, timeout=httpx2.Timeout(30, read=300),
                                  limits=httpx2.Limits(max_keepalive_connections=0))
        transport = streamable_http_client(f"{self.base}/mcp", http_client=http, terminate_on_close=False)
        async with http, Client(transport) as mcp, \
                httpx.AsyncClient(base_url=self.base, headers=auth, timeout=120,
                                  limits=httpx.Limits(max_keepalive_connections=0)) as c:
            await asyncio.sleep(random.uniform(0, 2))
            for n in self.sessions:  # an engineer starts work over a few seconds, not in one burst
                self.sent[(n, 1)] = time.time()
                try:
                    await self.call(mcp, "new_session", {"name": n, "environment": "loadenv", "repo": self.repo,
                                                         "message": "do the work", "idle_minutes": 0})
                except Exception as e:  # noqa: BLE001 - a refusal (a cap) is a result too
                    self.errors.append(f"new_session {n}: {e}")
                    self.turns[n] = a.turns
                await asyncio.sleep(random.uniform(0.5, 1.5))
            for k in range(a.jobs):
                t0 = time.time()
                try:
                    j = await self.call(mcp, "submit_job", {"instructions": f"{self.name} job {k}",
                                                            "environment": "loadenv", "repo": self.repo,
                                                            "deliver": "none", "verify": "true"})
                    self.jobs[j["id"]] = t0
                except Exception as e:  # noqa: BLE001
                    self.errors.append(f"submit_job: {e}")
            loops = [self.poll(mcp), self.lists(mcp), self.execs(mcp), self.dashboard(c), self.creates(c)]
            tasks = [asyncio.create_task(x) for x in loops]
            try:
                await asyncio.wait_for(self.done.wait(), a.timeout)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def poll(self, mcp: Client) -> None:
        a = self.a
        while True:
            for n in self.sessions:
                if self.turns.get(n, 0) >= a.turns:
                    continue
                try:
                    s = await self.call(mcp, "get_session", {"session": n})
                except Exception as e:  # noqa: BLE001
                    self.errors.append(f"get_session {n}: {e}")
                    continue
                turns = s.get("turns") or []
                finished = [t for t in turns if t.get("finished_at")]
                if s.get("state") == "failed":
                    self.errors.append(f"{n} failed: {s.get('reason')}")
                    self.turns[n] = a.turns
                    continue
                if len(finished) > self.turns.get(n, 0) and not s.get("busy"):
                    k = len(finished)
                    self.turns[n] = k
                    self.rec.add("session_turn_wall", finished[-1]["finished_at"] - self.sent[(n, k)])
                    if k < a.turns:
                        self.sent[(n, k + 1)] = time.time()
                        try:
                            await self.call(mcp, "send_to_session", {"session": n, "message": "continue"})
                        except Exception as e:  # noqa: BLE001
                            self.errors.append(f"send_to_session {n}: {e}")
                            self.turns[n] = a.turns
                await asyncio.sleep(a.poll / max(1, len(self.sessions)))
            if all(self.turns.get(n, 0) >= a.turns for n in self.sessions) and await self.jobs_done(mcp):
                self.done.set()
                return

    async def jobs_done(self, mcp: Client) -> bool:
        if not self.jobs:
            return True
        js = await self.call(mcp, "list_jobs", {"limit": 50})
        states = {j["id"]: j["state"] for j in js}
        return all(states.get(j) in DONE_JOB for j in self.jobs)

    async def lists(self, mcp: Client) -> None:
        while True:
            await asyncio.sleep(self.a.list * random.uniform(0.8, 1.2))
            await self.call(mcp, "list_sessions", {"limit": 30})
            sbs = await self.call(mcp, "list_sandboxes", {})
            self.sandboxes = [s["name"] for s in sbs if s.get("state") == "running"
                              and any(s["name"].startswith(f"ses-{n}-") for n in self.sessions)]
            await self.call(mcp, "list_jobs", {"limit": 20})

    async def execs(self, mcp: Client) -> None:
        while True:
            await asyncio.sleep(self.a.exec * random.uniform(0.8, 1.2))
            if self.sandboxes:
                try:
                    await self.call(mcp, "exec", {"sandbox": random.choice(self.sandboxes), "command": "true"})
                except Exception:  # noqa: BLE001 - the sandbox may have just gone (its turn and session ended)
                    pass

    async def dashboard(self, c: httpx.AsyncClient) -> None:
        while True:
            await asyncio.sleep(self.a.dashboard * random.uniform(0.8, 1.2))
            await self.rest(c, "GET", "/api/sessions", "sessions")
            await self.rest(c, "GET", "/api/sandboxes", "sandboxes")
            await self.rest(c, "GET", "/api/jobs", "jobs")

    async def creates(self, c: httpx.AsyncClient) -> None:
        while True:
            await asyncio.sleep(random.uniform(20, 60))
            r = await self.rest(c, "POST", "/api/sandboxes", "create_sandbox",
                                json={"environment": "loadenv", "name": f"{self.name}-scratch"})
            if r.status_code == 200:
                sb = r.json()
                sb = sb[0] if isinstance(sb, list) else sb
                await self.rest(c, "DELETE", f"/api/sandboxes/{sb['id']}", "destroy_sandbox")


async def main(a: argparse.Namespace) -> dict:
    rec = Recorder("team-load")
    root = Path(tempfile.mkdtemp(prefix="bench-team-", dir=scratch_root()))
    url = make_remote(root)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    cmd = [sys.executable, str(HERE / "fakeserver.py"), "--data", str(root / "srv"), "--port", str(port),
           "--latency-ms", str(a.latency_ms), "--watchdog-ms", str(a.watchdog_ms), "--steps", str(a.steps),
           "--history", str(a.history), *(["--job-history", str(a.job_history)] if a.job_history is not None else []),
           "--session-history", str(a.session_history), *(x for kv in a.set for x in ("--set", kv))]
    srv = subprocess.Popen(cmd)
    try:
        async with httpx.AsyncClient(base_url=base, headers=ADMIN, timeout=120) as c:
            for _ in range(600):
                try:
                    if (await c.get("/healthz")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            actors = [Actor(i, a, base, url, rec) for i in range(a.actors)]
            tokens = []
            for act in actors:  # --- durable: a person each (their own caps, as on a team), admins for the dashboard
                r = await c.post("/api/people", json={"id": act.name, "role": "admin"})
                r.raise_for_status()
                tokens.append(r.json()["login"]["token"])
            await c.get("/bench/stats")  # starts the loop monitor (its first answer has no samples yet)
            await asyncio.sleep(1)
            idle = (await c.get("/bench/stats", params={"reset": True})).json()
            rec.note("server_idle", {k: idle[k] for k in ("rss_mb", "threads")})
            rec.note("host_load_start", [round(x, 2) for x in os.getloadavg()])  # the machine is shared
            t0 = time.perf_counter()
            await asyncio.gather(*(act.run(tok) for act, tok in zip(actors, tokens, strict=True)))
            rec.note("wall_s", round(time.perf_counter() - t0, 1))
            rec.note("host_load_end", [round(x, 2) for x in os.getloadavg()])
            rec.note("server", (await c.get("/bench/stats")).json())
            jobs = (await c.get("/api/jobs", params={"limit": 500})).json()
            mine = {j for act in actors for j in act.jobs}
            states: dict[str, int] = {}
            for j in jobs:
                if j["id"] not in mine:
                    continue
                states[j["state"]] = states.get(j["state"], 0) + 1
                evs = j.get("events") or []
                started = next((e["ts"] for e in evs if e["state"] == "provisioning"), None)
                if started:
                    rec.add("job_queue_wait", started - j["created_at"])
            rec.note("job_states", states)
            rec.note("errors", [e for act in actors for e in act.errors][:40])
    finally:
        srv.terminate()
        try:
            srv.wait(30)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(root, ignore_errors=True)
    return rec.result(provider="fake", actors=a.actors, sessions=a.sessions, turns=a.turns, jobs=a.jobs,
                      history=a.history, job_history=a.job_history, session_history=a.session_history,
                      steps=a.steps, latency_ms=a.latency_ms, settings=a.set)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--actors", type=int, default=6)
    p.add_argument("--sessions", type=int, default=10, help="per actor")
    p.add_argument("--turns", type=int, default=2, help="per session")
    p.add_argument("--jobs", type=int, default=2, help="per actor")
    p.add_argument("--history", type=int, default=20000, help="settled sandbox rows seeded first")
    p.add_argument("--job-history", type=int, default=None,
                   help="finished jobs over six months seeded first (default: --history / 4)")
    p.add_argument("--session-history", type=int, default=0, help="settled sessions over six months seeded first")
    p.add_argument("--steps", type=int, default=16, help="agent events per turn, 0.5 s apart")
    p.add_argument("--poll", type=float, default=5.0, help="seconds between two get_session calls on a session")
    p.add_argument("--list", type=float, default=10.0, help="seconds between list_* rounds")
    p.add_argument("--exec", type=float, default=4.0, help="seconds between execs")
    p.add_argument("--dashboard", type=float, default=5.0, help="seconds between dashboard refreshes")
    p.add_argument("--latency-ms", type=float, default=0.0, help="extra per-call provider latency (network RTT)")
    p.add_argument("--timeout", type=float, default=900)
    p.add_argument("--watchdog-ms", type=float, default=200)
    p.add_argument("--set", action="append", default=[], metavar="FIELD=JSON", help="a server Settings override")
    p.add_argument("--out")
    return p


if __name__ == "__main__":
    args = parser().parse_args()
    emit(asyncio.run(main(args)), args.out)
