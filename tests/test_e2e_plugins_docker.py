"""The Claude Code plugin seed in a real environment image, on a real Docker engine (#136; plugins.py).

The image is built from an environment that declares a plugin (envspec.render: the seed stage runs the locked Claude
Code's `plugin marketplace add` / `plugin install` into /opt/hangar/claude-seed). Then, as the sandbox user, in a
container of it:

* the seed is root's and the agent can't write anything in it;
* Claude Code started with the seed's environment and hangar's `--settings` loads the plugin from the seed (the
  `init` event lists it with its path there, before any model call);
* `claude plugin marketplace update` on the seed's marketplace fails, naming the seed, and changes nothing.

Opt-in (needs Docker, and network for the image build):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_plugins_docker.py -v
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path

import pytest

from hangar import plugins
from hangar.envspec import EnvSpec, render
from hangar.harnesses import HARNESSES

BASE = "mirror.gcr.io/library/ubuntu:24.04"  # not Docker Hub, which rate-limits pulls

pytestmark = [pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run"),
              pytest.mark.needs("docker")]


def _marketplace(root: Path) -> None:
    files = {".claude-plugin/marketplace.json": {"name": "team", "owner": {"name": "x"}, "plugins": [
                 {"name": "workflow", "source": "./workflow", "description": "x"}]},
             "workflow/.claude-plugin/plugin.json": {"name": "workflow", "version": "1.0.0", "description": "x"}}
    for rel, obj in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(json.dumps(obj))
    (root / "workflow/skills/plan").mkdir(parents=True)
    (root / "workflow/skills/plan/SKILL.md").write_text("---\nname: plan\ndescription: x\n---\nPlan first.\n")


def test_the_seed_is_roots_loads_and_cant_be_updated(tmp_path):
    _marketplace(tmp_path / "mkt")
    spec = EnvSpec.model_validate({"name": "plugins-e2e", "harnesses": ["claude-code"], "base": BASE,
                                   "plugins": [{"path": "mkt", "name": "workflow"}]})
    spec.source_dir = tmp_path
    # HANGAR_E2E_PLUGINS_IMAGE: an image already built from this render (where the build needs a proxy's CA)
    tag = os.environ.get("HANGAR_E2E_PLUGINS_IMAGE") or f"hangar-e2e-plugins:{uuid.uuid4().hex[:8]}"
    if not os.environ.get("HANGAR_E2E_PLUGINS_IMAGE"):
        ctx = render(spec, bake=["claude-code"])
        build = subprocess.run(["docker", "build", "-q", "-t", tag, "-"], input=ctx.tar(), capture_output=True,
                               timeout=1800)
        assert build.returncode == 0, build.stderr[-3000:]
    seed = plugins.SEED
    claude = f"{HARNESSES['claude-code'].prefix}/bin/claude"
    settings = json.dumps(plugins.settings(spec))
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in {
        **plugins.env(spec, "claude-code"), "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_API_KEY": "sk-ant-fake", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}.items())
    script = f"""
set -u
echo "owner $(stat -c %U {seed})"
w=$(find {seed} -writable | head -1); echo "writable ${{w:-none}}"
cd /tmp
env {env} timeout 30 {claude} -p hello --output-format stream-json --verbose --max-turns 1 \\
    --settings {shlex.quote(settings)} </dev/null > /tmp/out.json 2>/dev/null
grep -m1 '"subtype":"init"' /tmp/out.json
before=$(find {seed} -type f -exec sha256sum {{}} + | sort | sha256sum)
env {env} timeout 120 {claude} plugin marketplace update team 2>&1; echo "update exit $?"
after=$(find {seed} -type f -exec sha256sum {{}} + | sort | sha256sum)
[ "$before" = "$after" ] && echo "seed unchanged"
"""
    try:
        out = subprocess.run(["docker", "run", "--rm", "--network", "none", "-u", "1000:1000", tag, "bash", "-c",
                              script], capture_output=True, text=True, timeout=600)
    finally:
        if not os.environ.get("HANGAR_E2E_PLUGINS_IMAGE"):
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
    said = out.stdout
    assert "owner root" in said and "writable none" in said, said + out.stderr
    init = json.loads(next(ln for ln in said.splitlines() if '"subtype":"init"' in ln))
    loaded = {p["name"]: p.get("path") for p in init.get("plugins") or [] if p.get("path") != "builtin"}
    assert loaded == {"workflow": f"{seed}/marketplaces/team/workflow"}, init.get("plugins")
    assert "workflow:plan" in init["skills"]
    assert f"is seed-managed ({seed})" in said, said  # Claude Code's own refusal, naming the seed
    assert "update exit 0" not in said and "seed unchanged" in said, said
