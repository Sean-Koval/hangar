"""scripts/acceptance/gke.sh's team checks against a real hangar on Docker: every one that can run there does, for
real (docs/acceptance.md). The computer (Chromium, a screenshot of the display, 3 s of video, a screen ticket whose
view-only viewer can't move the pointer), egress (an allowlist sandbox on hangar's internal network: the allowed
host through the proxy, 403 for another, no direct route, no DNS), a Postgres service reachable by name, and the
scale smoke test (members over MCP, the per-person cap, nothing left behind). Power answers with the pools on, and
prepull says it's for Kubernetes. The checks that need GitHub, Jira or a model skip, saying what they need.

Opt-in (needs Docker on Linux, the internet, and time to build the computer's image):
    HANGAR_E2E=1 uv run --all-extras pytest tests/test_e2e_acceptance_docker.py -v
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

pytestmark = [pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run"),
              pytest.mark.needs("docker")]

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "acceptance" / "gke.sh"
TOKEN = "accept-team-e2e-admin-token"


def test_team_checks_on_docker(tmp_path, monkeypatch):
    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.harnesses import HARNESSES, Harness
    from hangar.models import ApiToken, Person, Sandbox, Session

    monkeypatch.setitem(HARNESSES, "scripted", Harness(name="scripted", title="Scripted", run="echo '{}'",
                                                       session_dirs=[".fake/sessions"]))
    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "accsession.yaml").write_text(
        "name: accsession\nbase: mirror.gcr.io/library/ubuntu:24.04\nharnesses: [scripted]\nharness_mode: bake\n"
        "resources: {cpus: 1, memory_gb: 1}\n")
    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    s = Settings(data_dir=tmp_path / "data", env_dirs=[envs, ROOT / "envs"], reaper_interval_seconds=3600,
                 session_poll_seconds=1, admin_token=TOKEN, envd_dir=envd_dir, person_max_sessions=3,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    srv = AppThread(app)
    h = app.state.hangar
    env = {**os.environ, "HANGAR_URL": f"http://127.0.0.1:{srv.port}", "HANGAR_TOKEN": TOKEN,
           "HANGAR_CLI_CONFIG": str(tmp_path / "no-cli.json"), "HANGAR_ACCEPT_PYTHON": sys.executable,
           "TMPDIR": str(tmp_path)}
    for k in ("HANGAR_ACCEPT_TEST_REPO", "HANGAR_ACCEPT_FIXTURE_REPO", "HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET"):
        env.pop(k, None)
    try:
        r = subprocess.run(["bash", str(SCRIPT), "--no-kubectl", "--no-expect-envd", "--env", "minimal",
                            "--session-env", "accsession", "--warm-env", "minimal",
                            "--only", "computer,egress,services,scale-smoke,power,prepull,envbuild,landing,intake,"
                            "status,clouddoctor",
                            "--scale-sessions", "4", "--scale-people", "2", "--report", str(tmp_path / "r.json")],
                           env=env, capture_output=True, text=True, timeout=1500, cwd=tmp_path)
        out = r.stdout + r.stderr
        print(out)
        summary = out[out.rindex("== summary"):]
        status = {name: st for st, name in re.findall(r"^(PASS|FAIL|SKIP)\s+([\w-]+)", summary, re.M)}
        assert r.returncode == 0, out
        for check in ("computer", "egress", "services", "scale-smoke", "power", "status", "clouddoctor"):
            assert status.get(check) == "PASS", (check, summary)
        for check in ("prepull", "envbuild", "landing", "intake"):
            assert status.get(check) == "SKIP", (check, summary)
        assert re.search(r"PASS\s+computer .*view-only drops input, control doesn't", summary), summary
        assert re.search(r"PASS\s+egress .*403, no direct route, no DNS", summary), summary
        assert re.search(r"PASS\s+scale-smoke .*session 4 refused at the cap", summary), summary

        doc = json.loads((tmp_path / "r.json").read_text())
        rows = {c["check"]: c for c in doc["checks"]}
        assert doc["exit"] == 0 and doc["cleanup"] == "ok", doc
        assert {"chromium_screenshot", "record_3s", "screen_open"} <= set(rows["computer"]["timings"])
        assert {"create_p95", "teardown"} <= set(rows["scale-smoke"]["timings"])
        assert rows["power"]["core"] and not rows["computer"]["core"]

        # Nothing the run made is left; the test environments stay, so the next run's images are built.
        assert not [x for x in h.store.list(Session) if x.name.startswith("accept-")]
        assert not [x for x in h.store.list(Sandbox) if x.name.startswith("accept-") and x.state.value != "stopped"]
        assert not [t for t in h.store.list(ApiToken) if t.name.startswith("accept-")]
        assert not [p for p in h.store.list(Person) if p.id.startswith("accept-")]
        assert {"accept-computer", "accept-egress", "accept-services"} <= set(h.fleet.catalog.names())
        assert not list(tmp_path.glob("hangar-accept.*"))
    finally:
        srv.stop()
        subprocess.run(["docker", "rm", "-f", *[x.provider_ref for x in h.store.list(Sandbox) if x.provider_ref]],
                       capture_output=True)
