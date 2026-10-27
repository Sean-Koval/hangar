"""Sessions (spec 0010) end to end on a real Docker engine over REST: a turn, suspend (sandbox released),
then a follow-up that boots a new container, restores commits, uncommitted and untracked files and the agent's
conversation file, and resumes the conversation by id. A scripted harness stands in for the model.

Opt-in (needs Docker, git-daemon and network for the first image build):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_sessions_docker.py -v
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest

from tests.test_e2e_jobs_docker import remote  # noqa: F401 - fixture: bare repo served over git://

pytestmark = [pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run"),
              pytest.mark.needs("docker")]

AGENT = r"""set -e
conv="$HOME/.fake/sessions"; mkdir -p "$conv"
id="${HANGAR_SESSION_RESUME:-$HANGAR_SESSION_NEW}"
if [ -n "$HANGAR_SESSION_RESUME" ]; then
  test -f "$conv/$id.jsonl"
  echo "remembered: $(cat "$conv/$id.jsonl" | tr '\n' ' ')"
fi
msg="$(cat "$HANGAR_PROMPT_FILE" | head -1)"
echo "$msg" >> "$conv/$id.jsonl"
if [ -z "$HANGAR_SESSION_RESUME" ]; then
  sed -i 's/helo/hello/' greet.sh && git commit -qam "fix: greeting"
  echo "draft" > NOTES.md; echo "# wip" >> check.sh
else
  test -f NOTES.md && grep -q '# wip' check.sh && git log --oneline | grep -q "fix: greeting"
  git add -A && git commit -qm "docs: notes"
fi
echo "{\"type\":\"result\",\"result\":\"ok: $msg\"}"
"""


def test_session_suspend_and_resume_on_docker(tmp_path, remote, monkeypatch):  # noqa: F811
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
    app = create_app(s)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer admin-e2e"
        r = c.post("/api/sessions", json={"name": "greet", "environment": "sesenv", "repo": remote["url"],
                                           "message": "fix the greeting"})
        assert r.status_code == 200, r.text
        x = _settle(c, "greet")
        assert x["state"] == "idle" and x["turns"][0]["exit_code"] == 0, x
        first = x["sandbox_id"]
        assert c.post("/api/sessions/greet/suspend").status_code == 200
        x = _settle(c, "greet")
        assert x["state"] == "suspended" and x["sandbox_id"] is None
        assert c.get(f"/api/sandboxes/{first}").json()["state"] == "stopped"

        assert c.post("/api/sessions/greet/send", json={"message": "now write notes"}).status_code == 200
        x = _settle(c, "greet")
        t = x["turns"][1]
        assert x["state"] == "idle" and x["sandbox_id"] != first, x
        assert t["mode"] == "resume" and t["exit_code"] == 0, t
        assert t["summary"] == "ok: now write notes" and x["resume_command"].startswith("scripted --resume ")
        out = c.post(f"/api/sandboxes/{x['sandbox_id']}/exec", json={
            "command": "git log --format=%s -3 && git rev-parse --abbrev-ref HEAD && "
                       "grep -h remembered ~/.hangar/runs/*.log"}).json()
        lines = out["stdout"].splitlines()
        assert lines[:3] == ["docs: notes", "fix: greeting", "init"], out
        assert lines[3] == "hangar/session/greet"
        assert "fix the greeting" in lines[4]
        c.post("/api/sessions/greet/suspend")
        _settle(c, "greet")


def _settle(c, ref, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        x = c.get(f"/api/sessions/{ref}").json()
        if x["state"] in ("idle", "suspended", "failed") and not x["busy"]:
            return x
        time.sleep(1)
    raise AssertionError(f"session {ref} stuck: {x['state']} {x['reason']}")
