"""overnight/sweep on a real Docker engine: a checkpoint after the agent removed its checkout still keeps the
conversation (Docker refuses an exec whose working directory is gone, so the capture runs from $HOME), and a
database backup and restore through the REST API.

Opt-in (needs Docker and git-daemon):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_sweep_docker.py -v
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from tests.test_e2e_jobs_docker import remote  # noqa: F401 - fixture: bare repo served over git://
from tests.test_e2e_sessions_docker import AGENT, _settle

pytestmark = [pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run"),
              pytest.mark.needs("docker")]


def test_checkpoint_without_the_checkout_on_docker(tmp_path, remote, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.harnesses import HARNESSES, Harness

    script = base64.b64encode(AGENT.encode()).decode()
    monkeypatch.setitem(HARNESSES, "scripted", Harness(
        name="scripted", title="Scripted", run=f"echo {script} | base64 -d > /tmp/agent.sh && bash /tmp/agent.sh",
        session_dirs=[".fake/sessions"], resume="preset", resume_hint="scripted --resume {id}"))
    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "sesenv.yaml").write_text(
        "name: sesenv\nbase: mirror.gcr.io/library/ubuntu:24.04\nharnesses: [scripted]\nharness_mode: bake\n"
        "resources: {cpus: 1, memory_gb: 1}\n")
    s = Settings(data_dir=tmp_path / "data", env_dirs=[envs, Path(__file__).parents[1] / "envs"],
                 reaper_interval_seconds=3600, session_poll_seconds=1, admin_token="admin-e2e",
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(s)) as c:
        c.headers["Authorization"] = "Bearer admin-e2e"
        assert c.post("/api/sessions", json={"name": "gone", "environment": "sesenv", "repo": remote["url"],
                                             "message": "fix the greeting"}).status_code == 200
        x = _settle(c, "gone")
        assert x["state"] == "idle", x
        sb = c.get(f"/api/sandboxes/{x['sandbox_id']}").json()
        repo = sb["info"]["repo_path"]
        r = c.post(f"/api/sandboxes/{sb['id']}/exec", json={
            "command": f"rm -rf {repo} && echo 'later thought' >> ~/.fake/sessions/*.jsonl", "cwd": "/"}).json()
        assert r["exit_code"] == 0, r
        ck = c.post("/api/sessions/gone/checkpoint")
        assert ck.status_code == 200, ck.text
        assert any("is gone" in w for w in ck.json()["checkpoint"]["warnings"]), ck.json()
        assert c.post("/api/sessions/gone/suspend").status_code == 200
        assert _settle(c, "gone")["state"] == "suspended"
        assert c.post("/api/sessions/gone/send", json={"message": "now write notes"}).status_code == 200
        x = _settle(c, "gone")
        t = x["turns"][1]
        assert x["state"] == "idle" and t["exit_code"] == 0, x  # the agent checks the restored commit and files
        out = c.post(f"/api/sandboxes/{x['sandbox_id']}/exec", json={
            "command": "grep -h remembered ~/.hangar/runs/*.log"}).json()
        assert "later thought" in out["stdout"], out

        b = c.post("/api/admin/backup")
        assert b.status_code == 200, b.text
        assert c.post("/api/admin/restore", json={"name": b.json()["name"]}).status_code == 200
        c.post("/api/sessions/gone/suspend")
        _settle(c, "gone")
