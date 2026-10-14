"""A fake provider for control-plane benchmarks: the test suite's fake (each sandbox is a directory, exec runs
local bash), but its blocking work runs in worker threads like the real providers do (docker-py, the
kubernetes client and the e2b SDK all block), so thread-pool and event-loop pressure show up as in production.
`latency_ms` adds a fixed per-call delay inside the worker thread, standing in for a network round trip."""

from __future__ import annotations

import asyncio
import subprocess
import time

from _common import ROOT  # noqa: F401 - puts the repo root on sys.path

from hangar.models import ExecResult
from tests.conftest import FakeProvider


class ThreadedFake(FakeProvider):
    def __init__(self, latency_ms: float = 0.0):
        super().__init__()
        self.latency = latency_ms / 1000
        self.every_box: list = []

    def _wait(self) -> None:
        if self.latency:
            time.sleep(self.latency)

    def _exec(self, sandbox, command: str, cwd: str | None) -> ExecResult:
        self._wait()
        box = self.boxes[sandbox.provider_ref]
        cmd = command.replace("/home/agent", f"{box}/home/agent").replace("/workspace", f"{box}/workspace") \
            .replace("/tmp/", f"{box}/tmp/")
        env = {"HOME": f"{box}/home/agent", "PATH": "/usr/bin:/bin:/usr/local/bin", "TMUX_TMPDIR": f"{box}/tmp"}
        mapped = self._map(sandbox, cwd) if cwd else None
        p = subprocess.run(["bash", "-c", f"set -a; . {box}/home/agent/.hangar/env; set +a; {cmd}"],
                           capture_output=True, text=True, env=env,
                           cwd=str(mapped) if mapped and mapped.exists() else str(box))
        return ExecResult(exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr)

    async def exec(self, sandbox, command, timeout=120, cwd=None, user=None):
        return await asyncio.to_thread(self._exec, sandbox, command, cwd)

    async def read_file(self, sandbox, path, limit=None):
        parent = super().read_file

        def read():
            self._wait()
            return asyncio.run(parent(sandbox, path, limit))  # the parent's body is synchronous

        return await asyncio.to_thread(read)

    async def write_file(self, sandbox, path, data, mode=0o644):
        parent = super().write_file

        def write():
            self._wait()
            asyncio.run(parent(sandbox, path, data, mode))  # the parent's body is synchronous

        await asyncio.to_thread(write)

    async def create(self, sandbox, image, spec=None):
        parent = super().create
        await asyncio.to_thread(lambda: (self._wait(), asyncio.run(parent(sandbox, image, spec))))
        self.every_box.append(self.boxes[sandbox.provider_ref])

    def kill_all(self) -> None:
        """Stop the tmux servers of every sandbox ever created (destroy only forgets the directory)."""
        for box in self.every_box:
            subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                           capture_output=True)
