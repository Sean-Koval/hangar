"""A harness lock with a Claude Code plugin, frozen and thawed on a real Docker engine with the real Claude Code
against a scripted model (#221; harness_thaw.mount_files, docs/harness-lock.md#plugins).

1. A session on an environment whose image seeds a plugin runs a turn: the locked Claude Code talks to the fake model
   (tests/fixtures/live/fake_models.py), and the plugin's skill is in what it sends.
2. That turn is frozen over REST into a registry (`plugin:workflow@team` with its files), whose digest is the turn's
   `harness_digest`; `stable` points at it.
3. A session thawed from `team@stable` on an environment *without* the plugin runs turn 1: the plugin is mounted from
   the lock as the seed (root's, read-only), Claude Code loads it from there and sends its skill to the model, turn 1
   has no drift and the turn's `harness_digest` is the lock's.

Opt-in (needs Docker, git-daemon and network for the image builds):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_lock_plugins_docker.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from hangar import harness_lock, plugins
from tests.fixtures.live import fake_models
from tests.test_e2e_jobs_docker import _bridge_gateway, remote  # noqa: F401 - fixture: bare repo served over git://

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")
BASE = "mirror.gcr.io/library/ubuntu:24.04"  # not Docker Hub, which rate-limits pulls
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]


class Seen(fake_models.Handler):
    bodies: list = []

    def _anthropic(self, body):
        self.bodies.append(json.dumps(body))
        return super()._anthropic(body)


def _marketplace(root: Path) -> None:
    files = {".claude-plugin/marketplace.json": {"name": "team", "owner": {"name": "x"}, "plugins": [
                 {"name": "workflow", "source": "./workflow", "description": "x"}]},
             "workflow/.claude-plugin/plugin.json": {"name": "workflow", "version": "1.0.0", "description": "x"}}
    for rel, obj in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(json.dumps(obj))
    (root / "workflow/skills/plan").mkdir(parents=True)
    (root / "workflow/skills/plan/SKILL.md").write_text(
        "---\nname: plan\ndescription: Use before changing code: write the plan zq-lockplug-7731 first.\n---\nPlan.\n")


def _settle(c, ref, timeout=900):
    deadline = time.time() + timeout
    x = {}
    while time.time() < deadline:
        x = c.get(f"/api/sessions/{ref}").json()
        if x["state"] in ("idle", "suspended", "failed") and not x["busy"] and x.get("turns"):
            return x
        time.sleep(1)
    raise AssertionError(f"session {ref} stuck: {x.get('state')} {x.get('reason')}")


def _commit(root: Path, msg: str) -> None:
    if not (root / ".git").exists():
        subprocess.run([*GIT, "init", "-q", str(root)], check=True)
    subprocess.run([*GIT, "-C", str(root), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-qm", msg], check=True)


def test_freeze_and_thaw_a_plugin_with_real_claude_code(tmp_path, remote):  # noqa: F811
    from fastapi.testclient import TestClient

    from hangar.app import create_app
    from hangar.config import Settings

    gw = _bridge_gateway()
    srv = ThreadingHTTPServer((gw, 0), Seen)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    envs = tmp_path / "envs"
    _marketplace(envs / "mkt")
    common = {"base": BASE, "harnesses": ["claude-code"], "harness_mode": "bake", "approvals": "auto",
              "resources": {"cpus": 1, "memory_gb": 2},
              "agent_config": {"instructions": "Plan before you build."},
              "env": {"ANTHROPIC_BASE_URL": f"http://{gw}:{srv.server_address[1]}", "ANTHROPIC_API_KEY": "sk-ant-fake",
                      "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}}
    (envs / "plugenv.yaml").write_text(yaml.safe_dump({"name": "plugenv", **common,
                                                       "plugins": [{"path": "mkt", "name": "workflow"}]}))
    (envs / "bareenv.yaml").write_text(yaml.safe_dump({"name": "bareenv", **common}))
    reg = tmp_path / "registry"
    reg.mkdir()
    s = Settings(data_dir=tmp_path / "data", env_dirs=[envs, Path(__file__).parents[1] / "envs"],
                 reaper_interval_seconds=3600, session_poll_seconds=1, admin_token="admin-e2e",
                 harness_repo=str(reg), extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    try:
        with TestClient(create_app(s)) as c:
            c.headers["Authorization"] = "Bearer admin-e2e"
            # --- 1. a turn on the environment that seeds the plugin
            r = c.post("/api/sessions", json={"name": "src", "environment": "plugenv", "repo": remote["url"],
                                               "message": "RUN make the probe file"})
            assert r.status_code == 200, r.text
            x = _settle(c, "src")
            assert x["state"] == "idle" and x["turns"][0]["exit_code"] == 0, x
            assert any("zq-lockplug-7731" in b for b in Seen.bodies), "the seed's plugin skill never reached the model"
            m = c.get("/api/sessions/src/manifest", params={"turn": 1}).json()
            assert [p["name"] for p in m["plugins"]] == ["workflow"] and m["probe"]["plugins"][0]["present"], m

            # --- 2. frozen into the registry
            got = c.get("/api/sessions/src/harness", params={"turn": 1, "name": "team"})
            assert got.status_code == 200, got.text
            lock, files = harness_lock.unwire(got.json())
            assert got.json()["harness_digest"] == m["harness_digest"]
            assert "plugin:workflow@team" in [x["id"] for x in lock["components"]]
            harness_lock.write(reg / "harnesses/team", lock, files)
            assert harness_lock.check(reg / "harnesses/team")["ok"]
            (reg / "channels.yaml").write_text(yaml.safe_dump({"team": {"stable": m["harness_digest"]}}))
            _commit(reg, "team r1")

            # --- 3. thawed on the environment without it
            Seen.bodies.clear()
            r = c.post("/api/sessions", json={"name": "thawed", "environment": "bareenv", "repo": remote["url"],
                                               "message": "RUN make the probe file", "harness_lock": "team@stable"})
            assert r.status_code == 200, r.text
            x = _settle(c, "thawed")
            t = x["turns"][0]
            assert x["state"] == "idle" and t["exit_code"] == 0, x
            assert any("zq-lockplug-7731" in b for b in Seen.bodies), "the lock's plugin skill never reached the model"
            for _ in range(60):
                t = c.get("/api/sessions/thawed").json()["turns"][0]
                if t.get("drift") is not None:
                    break
                time.sleep(1)
            assert t["drift"] == [], t
            m2 = c.get("/api/sessions/thawed/manifest", params={"turn": 1}).json()
            assert m2["harness_digest"] == m["harness_digest"] == m2["harness"]["lock"]["digest"]
            assert m2["environment"]["name"] == "bareenv" and m2["probe"]["plugins"][0]["present"]
            seed = plugins.SEED
            out = c.post(f"/api/sandboxes/{x['sandbox_id']}/exec", json={"command": (
                f"stat -c %U {seed}; w=$(find {seed} -writable | head -1); echo \"writable ${{w:-none}}\"; "
                f"ls {seed}/marketplaces; stat -c %U ~/.claude/CLAUDE.md; head -1 ~/.claude/CLAUDE.md")}).json()
            lines = out["stdout"].splitlines()
            assert lines[:5] == ["root", "writable none", "team", "root", "Plan before you build."], out
    finally:
        srv.shutdown()
