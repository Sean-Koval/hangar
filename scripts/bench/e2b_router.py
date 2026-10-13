"""What the sandbox router costs: the stock e2b SDK against a Docker sandbox's hangar-envd, through hangar's router
and straight to the daemon, interleaved so both see the same machine.

    HANGAR_ENVD_DIR=envd/dist uv run --extra e2b python scripts/bench/e2b_router.py [--runs 200] [--out f.json]

Hangar runs in a process of its own, as in production: sharing the client's process (and its GIL) adds ~40 ms to
anything it serves, which is the harness, not hangar.

Metrics (p50/p95):
  stat_direct / stat_router          a Connect unary call (filesystem.Filesystem/Stat) on a kept-alive connection
  run_true_direct / run_true_router  SDK commands.run("true") (a Connect stream; bash -l is most of it)
  read_4k_direct / read_4k_router    SDK files.read of a 4 KiB file
  create                             SDK Sandbox.create from the `minimal` environment (no warm pool)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import docker
import httpx
from _common import ROOT, Recorder, emit


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main(runs: int, out: str | None) -> None:
    from e2b import AsyncSandbox
    from e2b.connection_config import ConnectionConfig
    from packaging.version import Version

    data = Path(tempfile.mkdtemp(prefix="bench-e2b-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None))
    port = _free_port()
    env = {**os.environ, "HANGAR_DATA_DIR": str(data), "HANGAR_ENV_DIRS": json.dumps([str(ROOT / "envs")]),
           "HANGAR_ADMIN_TOKEN": "bench-admin", "HANGAR_REAPER_INTERVAL_SECONDS": "3600",
           "HANGAR_ENVD_DIR": os.environ.get("HANGAR_ENVD_DIR") or str(ROOT / "envd" / "dist")}
    server = subprocess.Popen([sys.executable, "-m", "hangar.cli", "serve", "--port", str(port)], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    opts = {"api_key": "bench-admin", "api_url": f"{url}/e2b", "sandbox_url": f"{url}/e2b/sandbox",
            "request_timeout": 600}
    rec = Recorder("e2b-router")
    sbs = []
    try:
        async with httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer bench-admin"}) as admin:
            for _ in range(300):
                with contextlib.suppress(httpx.HTTPError):
                    if (await admin.get("/healthz")).status_code == 200:
                        break
                await asyncio.sleep(0.1)
            for _ in range(3):
                t = time.perf_counter()
                sbs.append(await AsyncSandbox.create(template="minimal", timeout=1800, **opts))
                rec.add("create", time.perf_counter() - t)
            sb = sbs[0]
            ref = (await admin.get(f"/api/sandboxes/{sb.sandbox_id}")).json()["provider_ref"]
        nets = docker.from_env().api.inspect_container(ref)["NetworkSettings"]["Networks"]
        daemon_url = f"http://{next(n['IPAddress'] for n in nets.values() if n.get('IPAddress'))}:49983"
        key = (data / "secret.key").read_bytes().strip()
        daemon_token = hmac.new(key, b"hangar-envd:" + sb.sandbox_id.encode(), hashlib.sha256).hexdigest()
        cfg = ConnectionConfig(sandbox_url=daemon_url, extra_sandbox_headers={"X-Access-Token": daemon_token})
        direct = AsyncSandbox(sandbox_id=sb.sandbox_id, sandbox_domain=None, envd_version=Version("0.5.7"),
                              envd_access_token=daemon_token, traffic_access_token=None, connection_config=cfg)
        await sb.files.write("/workspace/bench.txt", "x" * 4096)
        unary = {"Content-Type": "application/json", "Connect-Protocol-Version": "1"}
        async with httpx.AsyncClient(base_url=daemon_url, headers={**unary, "X-Access-Token": daemon_token}) as d, \
                httpx.AsyncClient(base_url=f"{url}/e2b/sandbox", headers={
                    **unary, "X-Access-Token": sb._envd_access_token, "E2b-Sandbox-Id": sb.sandbox_id}) as r:
            for c in (d, r):  # warm both connections
                resp = await c.post("/filesystem.Filesystem/Stat", json={"path": "/workspace"})
                assert resp.status_code == 200, resp.text
            for _ in range(runs):
                for name, c in (("direct", d), ("router", r)):
                    t = time.perf_counter()
                    resp = await c.post("/filesystem.Filesystem/Stat", json={"path": "/workspace"})
                    rec.add(f"stat_{name}", time.perf_counter() - t)
                    assert resp.status_code == 200
        for _ in range(max(10, runs // 4)):
            for name, box in (("direct", direct), ("router", sb)):
                with rec.time(f"run_true_{name}"):
                    await box.commands.run("true")
                with rec.time(f"read_4k_{name}"):
                    await box.files.read("/workspace/bench.txt")
        for name in ("stat", "run_true", "read_4k"):
            a, b = sorted(rec.samples[f"{name}_direct"]), sorted(rec.samples[f"{name}_router"])
            rec.note(f"{name}_router_overhead_p50_ms", round((b[len(b) // 2] - a[len(a) // 2]) * 1000, 2))
    finally:
        for x in sbs:
            with contextlib.suppress(Exception):
                await AsyncSandbox.kill(x.sandbox_id, **opts)
        server.terminate()
        server.wait(30)
        shutil.rmtree(data, ignore_errors=True)
    emit(rec.result(provider="docker", runs=runs), out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--out")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main(a.runs, a.out))
