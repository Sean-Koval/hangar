"""Context exchange (spec 0011 §2) end to end on a real Docker engine over REST: a session gets a developer's
unpushed commit and uncommitted changes, the agent (a scripted harness) uses the `hangar-agent` helper inside a
real container to report progress and ask a question, the answer resumes the same conversation, and the result,
an artifact, the patch and `hangar session pull` bring the work back out.

Opt-in (needs Docker, git-daemon and network for the first image build):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_exchange_docker.py -v
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_e2e_jobs_docker import remote  # noqa: F401 - fixture: bare repo served over git://

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

AGENT = r"""set -e
conv="$HOME/.fake/sessions"; mkdir -p "$conv"
id="${HANGAR_SESSION_RESUME:-$HANGAR_SESSION_NEW}"
[ -z "$HANGAR_SESSION_RESUME" ] || test -f "$conv/$id.jsonl"
msg="$(cat "$HANGAR_PROMPT_FILE")"
echo "$msg" | head -1 >> "$conv/$id.jsonl"
case "$msg" in
  *TASK-ANSWER*)
    sed -i 's/helo/hello/' greet.sh && git commit -qam "fix: greeting"
    echo '<h1>report</h1>' > report.html && hangar-agent artifact report.html
    hangar-agent result '{"fixed": true}'
    hangar-agent done "Fixed greet.sh" ;;
  *TASK-ASK*)
    git log --format=%s | grep -q "local: unpushed"
    grep -q "local edit" check.sh && test -f notes-local.txt
    hangar-agent progress --pct 30 "looked around"
    hangar-agent ask "Fix greet.sh or check.sh?" ;;
esac
echo "{\"type\":\"result\",\"result\":\"ok\"}"
"""

SCHEMA = {"type": "object", "properties": {"fixed": {"type": "boolean"}}, "required": ["fixed"]}


def test_context_exchange_on_docker(tmp_path, remote, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from hangar import exchange
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
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    local = tmp_path / "laptop"
    subprocess.run([*git, "clone", "-q", str(remote["bare"]), str(local)], check=True)
    (local / "local.txt").write_text("x\n")
    subprocess.run([*git, "-C", str(local), "add", "local.txt"], check=True)
    subprocess.run([*git, "-C", str(local), "commit", "-qm", "local: unpushed"], check=True)
    (local / "check.sh").write_text((local / "check.sh").read_text() + "# local edit\n")
    (local / "notes-local.txt").write_text("todo\n")
    items, info = exchange.from_local(local)
    assert info["commits"] == 1 and info["patch_bytes"]

    s = Settings(data_dir=tmp_path / "data", env_dirs=[envs, Path(__file__).parents[1] / "envs"],
                 reaper_interval_seconds=3600, session_poll_seconds=1, admin_token="admin-e2e",
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer admin-e2e"
        r = c.post("/api/sessions", json={"name": "laptop", "environment": "sesenv", "repo": remote["url"],
                                           "message": "TASK-ASK pick up my work", "context": items,
                                           "output_schema": SCHEMA})
        assert r.status_code == 200, r.text
        x = _settle(c, "laptop")
        t = x["turns"][0]
        assert x["needs_input"] == "Fix greet.sh or check.sh?" and t["outcome"] == "needs_input", x
        assert t["progress"]["message"] == "looked around" and t["progress"]["pct"] == 30

        r = c.post("/api/sessions/laptop/send", json={"message": "TASK-ANSWER greet.sh", "output_schema": SCHEMA})
        assert r.status_code == 200, r.text
        x = _settle(c, "laptop")
        t = x["turns"][1]
        assert x["needs_input"] is None and t["mode"] == "resume" and t["exit_code"] == 0, x
        assert t["result"] == {"fixed": True} and t["summary"] == "Fixed greet.sh"
        assert [a["path"] for a in t["artifacts"]] == ["report.html"]
        a = c.get("/api/sessions/laptop/artifacts/2/report.html")
        assert a.content == b"<h1>report</h1>\n" and a.headers["content-type"].startswith("text/plain")

        assert c.post("/api/sessions/laptop/suspend").status_code == 200
        x = _settle(c, "laptop")
        assert x["state"] == "suspended"
        p = c.get("/api/sessions/laptop/patch").json()
        assert p["source"].startswith("checkpoint")
        assert {"greet.sh", "check.sh", "local.txt", "notes-local.txt"} <= {f["path"] for f in p["files"]}, p

        m = c.get("/api/sessions/laptop/pull").json()
        parts = {part: c.get(f"/api/sessions/laptop/pull/{m['checkpoint']}/{part}").content for part in m["parts"]}
    dest = tmp_path / "elsewhere"
    subprocess.run([*git, "clone", "-q", str(remote["bare"]), str(dest)], check=True)
    exchange.pull_into(dest, "laptop", m, parts, checkout=True)
    log = subprocess.run(["git", "-C", str(dest), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.splitlines()[:2] == ["fix: greeting", "local: unpushed"], log
    assert "# local edit" in (dest / "check.sh").read_text() and (dest / "notes-local.txt").exists()


def _settle(c, ref, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        x = c.get(f"/api/sessions/{ref}").json()
        if x["state"] in ("idle", "suspended", "failed") and not x["busy"]:
            return x
        time.sleep(1)
    raise AssertionError(f"session {ref} stuck: {x['state']} {x['reason']}")
