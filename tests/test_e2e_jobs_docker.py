"""Jobs (spec 0009) end to end on a real Docker engine, driven over the REST API with a `jobs`-scoped token.

A scripted harness stands in for the model so the test is deterministic; everything else is real: image
build, sandbox, clone over git://, tmux run, verify, commit, push, signed callback, follow-up, scopes.

Opt-in (needs Docker, git-daemon and network for the first image build):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_jobs_docker.py -v
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.conftest import free_port as _free_port

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

AGENT = r"""set -e
cd "${HANGAR_REPO:-$PWD}"
if grep -q "also add a changelog" "$HANGAR_PROMPT_FILE"; then
  echo "- fixed greeting" > CHANGELOG.md && git add CHANGELOG.md && git commit -qm "docs: changelog"
  echo "Added CHANGELOG.md" > "$HANGAR_JOB_DIR/RESULT.md"
else
  test -f "$HANGAR_JOB_DIR/bug.log"
  sed -i 's/helo/hello/' greet.sh && git commit -qam "fix: greeting typo"
  echo "Fixed the typo in greet.sh; check.sh passes." > "$HANGAR_JOB_DIR/RESULT.md"
fi
"""


def _bridge_gateway() -> str:
    out = subprocess.run(["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


class _Receiver(BaseHTTPRequestHandler):
    got: list[tuple[dict, bytes]] = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Receiver.got.append((dict(self.headers), body))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def remote():
    root = Path(tempfile.mkdtemp(prefix="hangar-e2e-jobs-"))
    bare = root / "repos" / "app.git"
    seed = root / "seed"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "greet.sh").write_text("echo helo\n")
    (seed / "check.sh").write_text('test "$(bash greet.sh)" = hello\n')
    subprocess.run([*git, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "-C", str(seed), "push", "-q", str(bare), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    port = _free_port()
    daemon = subprocess.Popen(
        ["git", "daemon", "--reuseaddr", "--export-all", "--enable=receive-pack", f"--port={port}",
         "--listen=0.0.0.0", f"--base-path={root / 'repos'}", str(root / "repos")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    yield {"bare": bare, "url": f"git://{_bridge_gateway()}:{port}/app.git"}
    daemon.terminate()
    shutil.rmtree(root, ignore_errors=True)


def test_job_over_rest_on_docker(tmp_path, remote, monkeypatch):
    from fastapi.testclient import TestClient

    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.harnesses import HARNESSES, Harness
    from hangar.models import CredentialKind

    script = base64.b64encode(AGENT.encode()).decode()
    monkeypatch.setitem(HARNESSES, "scripted", Harness(
        name="scripted", title="Scripted", run=f"echo {script} | base64 -d > /tmp/agent.sh && bash /tmp/agent.sh"))
    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "jobenv.yaml").write_text(
        "name: jobenv\nbase: mirror.gcr.io/library/ubuntu:24.04\nharnesses: [scripted]\nharness_mode: bake\n"
        "resources: {cpus: 1, memory_gb: 1}\n")
    srv = HTTPServer(("127.0.0.1", _free_port()), _Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = Settings(data_dir=tmp_path / "data", env_dirs=[envs, Path(__file__).parents[1] / "envs"],
                 reaper_interval_seconds=3600, job_poll_seconds=1, admin_token="admin-e2e",
                 job_callback_hosts=["localhost"], github_repos={remote["url"]: "jobenv"},
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    with TestClient(app) as c:
        h = app.state.hangar
        h.broker.put("cb-key", "webhook", CredentialKind.GENERIC, "k3y")
        admin = {"Authorization": "Bearer admin-e2e"}
        raw = c.post("/api/tokens", json={"name": "support-tool", "scopes": ["jobs"],
                                                   "callback_secret": "cb-key"}, headers=admin).json()["token"]
        svc = {"Authorization": f"Bearer {raw}"}
        assert c.get("/api/sandboxes", headers=svc).status_code == 403

        job = c.post("/api/jobs", headers=svc, json={
            "instructions": "The greeting prints 'helo'. Fix it.", "repo": remote["url"], "environment": "jobenv",
            "verify": "bash check.sh", "deliver": "branch",
            "context": [{"name": "bug.log", "kind": "log", "content": "$ bash greet.sh\nhelo\n"}],
            "callback_url": f"http://localhost:{srv.server_port}/cb"}).json()
        jid = job["id"]
        job = _wait(c, svc, jid)
        assert job["state"] == "succeeded", job["events"]
        assert job["result"]["verify_exit_code"] == 0 and job["result"]["pushed"]
        assert job["result"]["summary"].startswith("Fixed the typo")
        log = subprocess.run(["git", "--git-dir", str(remote["bare"]), "log", "--format=%s", job["branch"]],
                             capture_output=True, text=True, check=True).stdout
        assert log.splitlines()[0] == "fix: greeting typo"

        # an engineer follows up; the agent continues in the same sandbox and branch
        r = c.post(f"/api/jobs/{jid}/follow-up", headers=svc, json={"message": "also add a changelog entry"})
        assert r.status_code == 200, r.text
        job = _wait(c, svc, jid)
        assert job["state"] == "succeeded" and len(job["turns"]) == 2, job["events"]
        log = subprocess.run(["git", "--git-dir", str(remote["bare"]), "log", "--format=%s", job["branch"]],
                             capture_output=True, text=True, check=True).stdout.splitlines()
        assert log[:2] == ["docs: changelog", "fix: greeting typo"]

        # signed callbacks arrived: started, finished (turn 1), finished (turn 2)
        time.sleep(1)
        events = [json.loads(b)["event"] for _, b in _Receiver.got]
        assert events == ["job.started", "job.finished", "job.finished"], events
        for hdrs, body in _Receiver.got:
            want = "sha256=" + hmac.new(b"k3y", body, hashlib.sha256).hexdigest()
            assert hdrs["X-Hangar-Signature"] == want
        c.delete(f"/api/sandboxes/{job['sandbox_id']}", headers=admin)
    srv.shutdown()


def _wait(c, headers, jid, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = c.get(f"/api/jobs/{jid}", headers=headers).json()
        if not job["active"]:
            return job
        time.sleep(2)
    raise AssertionError(f"job {jid} still active: {job['events']}")
