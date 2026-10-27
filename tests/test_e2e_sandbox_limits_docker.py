"""What the control plane holds when a sandbox prints, or hands back, far more than any cap (real Docker engine).

A sandbox decides how much its commands print and how big its files are, and its ~/.hangar/env runs before every
command hangar sends it. Before the review of #48, Docker exec buffered all output and read_file all of the file
before anything was cut, so one sandbox could exhaust the control plane's memory.

Opt-in (needs Docker and the `minimal` environment image, built on first use):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_sandbox_limits_docker.py -v
"""

from __future__ import annotations

import os
import tracemalloc
from pathlib import Path

import pytest

pytestmark = [pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run"),
              pytest.mark.needs("docker")]

BIG = 200 * 1024 * 1024


async def test_exec_output_and_file_reads_are_bounded(tmp_path):
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.providers.docker import MAX_OUTPUT

    s = Settings(data_dir=tmp_path, env_dirs=[Path(__file__).parents[1] / "envs"], reaper_interval_seconds=3600,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    await h.fleet.start()
    try:
        sb = await h.fleet.create("minimal", name="e2e-limits", labels={"e2e-limits": "1"})
        prov = h.fleet.providers["docker"]

        tracemalloc.start()
        try:
            res = await prov.exec(sb, f"head -c {BIG} /dev/zero | tr '\\0' a; echo oops >&2; exit 3")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert res.exit_code == 3 and res.truncated and res.stderr.strip() == "oops"
        assert res.stdout.endswith("a" * 100) and len(res.stdout) <= MAX_OUTPUT + 64
        assert peak < 32 * 1024 * 1024, f"exec held {peak // 1024 // 1024} MB of output"

        assert (await prov.exec(sb, f"head -c {BIG} /dev/zero > /tmp/big && echo hi > /tmp/small")).exit_code == 0
        tracemalloc.start()
        try:
            data = await prov.read_file(sb, "/tmp/big", limit=1000)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert data is not None and len(data) == 1001
        assert peak < 32 * 1024 * 1024, f"read_file held {peak // 1024 // 1024} MB"
        assert await prov.read_file(sb, "/tmp/small", limit=1000) == b"hi\n"
        assert await prov.read_file(sb, "/tmp/small") == b"hi\n"
        assert await prov.read_file(sb, "/tmp/missing", limit=10) is None
        await prov.exec(sb, "rm -f /tmp/big")
    finally:
        await h.fleet.destroy_many(label="e2e-limits")
        await h.fleet.stop()
