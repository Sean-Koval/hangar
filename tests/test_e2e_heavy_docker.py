"""--- heavy: scripts/acceptance/gke.sh's heavy check against a real hangar on Docker (docs/acceptance.md): a heavy
environment's image is built with Docker in it, the sandbox starts dockerd for real (hangar-docker), a Compose stack
with Postgres comes up under the supervisor and answers, and a Testcontainers test passes.

On Docker the heavy tier is a privileged runc container (HANGAR_HEAVY_DOCKER_PRIVILEGED): root on the host, which is
why it is only for CI and throwaway machines. It runs the same in-sandbox code as a Kata pod or a microVM, minus the
VM: the loop-mounted disk isn't needed on the host's filesystem, so storage takes the direct path.

Opt-in (needs Docker on Linux, the internet, and a few minutes):
    HANGAR_E2E=1 uv run --all-extras pytest tests/test_e2e_heavy_docker.py -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_mcp_modern import AppThread

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "acceptance" / "gke.sh"
TOKEN = "accept-heavy-e2e-admin-token"


def test_heavy_check_on_docker(tmp_path):
    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.models import Sandbox

    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "accbase.yaml").write_text("name: accbase\nbase: mirror.gcr.io/library/ubuntu:24.04\nharnesses: [git]\n"
                                       "resources: {cpus: 2, memory_gb: 4}\n")
    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    s = Settings(data_dir=tmp_path / "data", env_dirs=[envs, ROOT / "envs"], reaper_interval_seconds=3600,
                 admin_token=TOKEN, envd_dir=envd_dir, tiers={"heavy": ["docker"]}, heavy_docker_privileged=True,
                 heavy_docker_image="mirror.gcr.io/library/docker:29.8.2-dind",
                 heavy_registry_mirrors=["https://mirror.gcr.io"],
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    srv = AppThread(app)
    h = app.state.hangar
    env = {**os.environ, "HANGAR_URL": f"http://127.0.0.1:{srv.port}", "HANGAR_TOKEN": TOKEN,
           "HANGAR_CLI_CONFIG": str(tmp_path / "no-cli.json"), "HANGAR_ACCEPT_PYTHON": sys.executable,
           "TMPDIR": str(tmp_path)}
    try:
        r = subprocess.run(["bash", str(SCRIPT), "--no-kubectl", "--no-expect-envd", "--env", "accbase",
                            "--only", "heavy", "--service-image", "mirror.gcr.io/library/postgres:17",
                            "--report", str(tmp_path / "r.json")],
                           env=env, capture_output=True, text=True, timeout=1500, cwd=tmp_path)
        out = r.stdout + r.stderr
        print(out)
        summary = out[out.rindex("== summary"):]
        assert re.search(r"^PASS\s+heavy .*compose Postgres answered.*Testcontainers Postgres", summary, re.M), summary
        assert r.returncode == 0, out
        doc = json.loads((tmp_path / "r.json").read_text())
        row = next(c for c in doc["checks"] if c["check"] == "heavy")
        assert {"create", "compose_ready", "testcontainers"} <= set(row["data"]["timings"]), row
        assert doc["cleanup"] == "ok", doc
        from hangar import heavy

        assert heavy.vm(h.fleet.catalog.get("accept-heavy")), "the check's environment is kept, heavy"
        made = [x for x in h.store.list(Sandbox) if x.name.startswith("accept-") and x.info.get("heavy")]
        assert made and made[0].info["heavy"]["status"]["ok"], [x.info.get("heavy") for x in made]
        assert not [x for x in h.store.list(Sandbox) if x.name.startswith("accept-") and x.state.value != "stopped"]
    finally:
        srv.stop()
        refs = [x.provider_ref for x in h.store.list(Sandbox) if x.provider_ref]
        if refs:
            subprocess.run(["docker", "rm", "-f", "-v", *refs], capture_output=True)
