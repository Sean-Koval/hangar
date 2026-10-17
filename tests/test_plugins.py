"""Claude Code plugins from the read-only seed (#136; plugins.py, docs/harnesses.md#plugins).

What an environment declares, the image build's seed stage, the settings and environment hangar gives the harness (a
hardened turn's included), the manifest's `plugins` section and the version floor. What Claude Code does with the
seed is tests/test_harden_real_clis.py; the seed in a real image is tests/test_e2e_plugins_docker.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from hangar import canary, harden, isolation, manifest, plugins
from hangar.config import Settings
from hangar.envspec import EnvSpec, render
from hangar.harnesses import HARNESSES
from hangar.models import Sandbox
from hangar.verifyloop import CLAUDE_ENV, claude_settings

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs/api/manifest.schema.json").read_text())
GATE = claude_settings("pytest -q", "/workspace/r", 2, 2**31, 60)


def _mkt(root: Path, name: str = "team", plugins_: tuple[str, ...] = ("workflow",), body: str = "Plan first.") -> Path:
    for p in plugins_:
        (root / p / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / p / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": p, "version": "1.2.0"}))
        (root / p / "skills" / "plan").mkdir(parents=True, exist_ok=True)
        (root / p / "skills" / "plan" / "SKILL.md").write_text(f"---\nname: plan\ndescription: x\n---\n{body}\n")
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": name, "owner": {"name": "x"}, "plugins": [{"name": p, "source": f"./{p}"} for p in plugins_]}))
    return root


def _spec(tmp_path: Path, **over) -> EnvSpec:
    if not (tmp_path / "mkt").exists():
        _mkt(tmp_path / "mkt")
    spec = EnvSpec.model_validate({"name": "e", "harnesses": ["claude-code", "codex"],
                                   "plugins": [{"path": "mkt", "name": "workflow"}], **over})
    spec.source_dir = tmp_path
    return spec


# ====================================================================== the declaration
def test_plugins_are_claude_codes_and_need_a_built_image(tmp_path):
    with pytest.raises(ValueError, match="add claude-code"):
        EnvSpec.model_validate({"name": "e", "harnesses": ["codex"], "plugins": [{"path": "m", "name": "p"}]})
    with pytest.raises(ValueError, match="prebuilt `image:`"):
        EnvSpec.model_validate({"name": "e", "harnesses": ["claude-code"], "image": "x:1",
                                "plugins": [{"path": "m", "name": "p"}]})
    with pytest.raises(ValueError):
        EnvSpec.model_validate({"name": "e", "harnesses": ["claude-code"], "plugins": [{"path": "m", "name": "a b"}]})


def test_a_plugin_resolves_to_its_marketplace_and_a_path_under_the_seed(tmp_path):
    spec = _spec(tmp_path)
    r = plugins.resolved(spec, spec.plugins[0], strict=True)
    assert (r["id"], r["version"]) == ("workflow@team", "1.2.0")
    assert r["path"] == f"{plugins.SEED}/marketplaces/team/workflow"  # loaded in place (verified with 2.1.296)
    assert plugins.enabled(spec) == {"workflow@team": True}
    assert plugins.enabled(spec, "codex") == {} and plugins.env(spec, "codex") == {}
    with pytest.raises(ValueError, match="not 'other'"):
        plugins.resolved(spec, plugins.Plugin(path="mkt", name="workflow", marketplace="other"))
    with pytest.raises(ValueError, match="escapes"):
        plugins.resolved(spec, plugins.Plugin(path="../x", name="workflow"))
    with pytest.raises(ValueError, match="has no plugin"):
        plugins.resolved(spec, plugins.Plugin(path="mkt", name="nope"), strict=True)


def test_hangars_settings_enable_exactly_the_seeds_plugins(tmp_path):
    spec = _spec(tmp_path, plugins=[{"path": "mkt", "name": "workflow"},
                                    {"path": "mkt", "name": "workflow", "enabled": False}])
    assert plugins.settings(spec) == {"enabledPlugins": {"workflow@team": False}, "syncClaudeAiPlugins": False}
    spec = _spec(tmp_path)
    assert plugins.env(spec, "claude-code") == {"CLAUDE_CODE_PLUGIN_SEED_DIR": plugins.SEED,
                                                "CLAUDE_CODE_SYNC_PLUGIN_INSTALL": "1"}
    merged = json.loads(plugins.turn_env(spec, "claude-code", {CLAUDE_ENV: GATE})[CLAUDE_ENV])
    assert merged["hooks"] == json.loads(GATE)["hooks"]  # the verify gate stays
    assert merged["enabledPlugins"] == {"workflow@team": True} and merged["syncClaudeAiPlugins"] is False
    assert plugins.turn_env(spec, "codex", {}) == {} and plugins.turn_env(EnvSpec(name="x"), "claude-code", {}) == {}


# ====================================================================== the image build
def test_the_image_builds_the_seed_with_the_locked_claude_code_and_keeps_it_roots(tmp_path):
    spec = _spec(tmp_path, agent_config={"files": {".claude/settings.json": json.dumps({"extraKnownMarketplaces": {
        "m": {"source": {"source": "github", "repo": "o/m"}}}})}})
    ctx = render(spec)
    claude = HARNESSES["claude-code"]
    assert "FROM mirror.gcr.io/library/node:22-bookworm AS pack-claude-code" in ctx.dockerfile
    assert "FROM pack-claude-code AS hangar-claude-seed" in ctx.dockerfile
    assert f"COPY plugins/team/ {plugins.SEED}/marketplaces/team/" in ctx.dockerfile
    assert f"COPY --chown=root:root --from=hangar-claude-seed {plugins.SEED} {plugins.SEED}" in ctx.dockerfile
    assert f"COPY --from=pack-claude-code {claude.prefix}" not in ctx.dockerfile  # the binary isn't baked for it
    script = ctx.files["plugins/seed.sh"].decode()
    assert f"CLAUDE_CODE_PLUGIN_CACHE_DIR={plugins.SEED}" in script
    assert f"{claude.prefix}/bin/claude plugin marketplace add {plugins.SEED}/marketplaces/team" in script
    assert f"{claude.prefix}/bin/claude plugin install workflow@team" in script
    assert "go-w" in script and ctx.files["plugins/team/workflow/skills/plan/SKILL.md"].startswith(b"---")
    # an environment's own marketplaces don't update in a sandbox (the seed's are off already)
    staged = next(v for k, v in ctx.files.items() if k.startswith("files/") and k.endswith("settings.json"))
    assert json.loads(staged)["extraKnownMarketplaces"]["m"]["autoUpdate"] is False
    # a plugin's content is a build input: the image changes with it
    _mkt(tmp_path / "mkt", body="Plan twice.")
    assert render(spec).digest != ctx.digest
    baked = render(spec, bake=["claude-code"]).dockerfile
    assert baked.count("AS pack-claude-code") == 1


def test_the_build_refuses_what_it_cant_install(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="marketplace.json"):
        render(_spec(tmp_path, plugins=[{"path": "empty", "name": "workflow", "marketplace": "x"}]))
    _mkt(tmp_path / "other", name="team")
    with pytest.raises(ValueError, match="two directories"):
        render(_spec(tmp_path, plugins=[{"path": "mkt", "name": "workflow"}, {"path": "other", "name": "workflow"}]))


# ====================================================================== a hardened turn
def _plan(spec: EnvSpec):
    vault = {harden.VAULT + "sb-1": json.dumps({"env": "export A=1\n", "claude_mcp": None})}
    fleet = SimpleNamespace(settings=Settings(), broker=SimpleNamespace(vault=SimpleNamespace(get=vault.get)),
                            catalog=SimpleNamespace(get=lambda name: spec))
    h = HARNESSES["claude-code"]
    sb = Sandbox(id="sb-1", name="s", environment="e", provider="docker", harness=h.name, info={
        "isolation": {"mode": "split", "v": isolation.VERSION, "harness": isolation.live_bin(h)}})
    return harden.plan(fleet, sb, h, "run-0000beef", "/workspace/r", {CLAUDE_ENV: GATE})


def test_a_hardened_turn_loads_the_seeds_plugins_and_reads_no_plugins_of_the_agents(tmp_path):
    """The gap the cross-repo review found: a hardened turn dropped every plugin, so a plugin arm equalled none."""
    theirs = {"enabledPlugins": {"workflow@team": True}, "permissions": {"allow": ["Bash(make *)"]}}
    p = _plan(_spec(tmp_path, agent_config={"files": {".claude/settings.json": json.dumps(theirs)}}))
    assert not isinstance(p, str) and p.warnings == []  # the environment enables only what the seed has
    settings = json.loads(p.files["run-0000beef.settings.json"][0])
    assert settings["enabledPlugins"] == {"workflow@team": True} and settings["syncClaudeAiPlugins"] is False
    assert settings["permissions"] == {"allow": ["Bash(make *)"]} and settings["hooks"]["Stop"]  # the gate stays
    env = p.files["run-0000beef.agent-env"][0].decode()
    assert f"export CLAUDE_CODE_PLUGIN_CACHE_DIR={plugins.RUN_CACHE}\n" in env  # not ~/.claude/plugins
    assert f"export CLAUDE_CODE_PLUGIN_SEED_DIR={plugins.SEED}\n" in env
    assert "export CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1\n" in env
    # a new directory each turn, named by bash builtins as the harness starts (nothing on the agent's PATH runs)
    import subprocess

    got = {subprocess.run(["bash", "-c", f"export X={plugins.RUN_CACHE}; printf %s \"$X\""], capture_output=True,
                          text=True, env={}).stdout for _ in range(3)}
    assert len(got) == 3 and all(g.startswith("/tmp/hangar-claude-plugins.") for g in got)


def test_a_hardened_turn_still_drops_plugins_from_outside_the_seed(tmp_path):
    theirs = {"enabledPlugins": {"workflow@team": True, "other@elsewhere": True},
              "extraKnownMarketplaces": {"elsewhere": {"source": {"source": "directory", "path": "/opt/x"}}}}
    p = _plan(_spec(tmp_path, agent_config={"files": {".claude/settings.json": json.dumps(theirs)}}))
    settings = json.loads(p.files["run-0000beef.settings.json"][0])
    assert settings["enabledPlugins"] == {"workflow@team": True} and "extraKnownMarketplaces" not in settings
    assert p.warnings == [harden.CLAUDE_PLUGINS]
    # without a seed: nothing enabled, still no ~/.claude/plugins
    p = _plan(EnvSpec(name="e", harnesses=["claude-code"]))
    assert "enabledPlugins" not in json.loads(p.files["run-0000beef.settings.json"][0])
    assert b"CLAUDE_CODE_PLUGIN_CACHE_DIR=" in p.files["run-0000beef.agent-env"][0]
    assert b"CLAUDE_CODE_PLUGIN_SEED_DIR" not in p.files["run-0000beef.agent-env"][0]


# ====================================================================== the manifest
def test_the_manifest_lists_the_seeds_plugins_and_harness_digest_follows_their_content(fleet, tmp_path):
    spec = _spec(tmp_path)
    m = manifest.build(fleet, spec, "claude-code")
    jsonschema.validate(m, SCHEMA)
    [p] = m["plugins"]
    assert {k: p[k] for k in ("name", "marketplace", "version", "path", "enabled", "effective", "source")} == {
        "name": "workflow", "marketplace": "team", "version": "1.2.0",
        "path": f"{plugins.SEED}/marketplaces/team/workflow", "enabled": True, "effective": True, "source": "seed"}
    assert p["digest"].startswith("sha256:") and p["files"] == 2
    assert manifest.pick(m, "plugins") == m["plugins"]  # a section of harness_digest, whole
    _mkt(tmp_path / "mkt", body="Plan twice.")
    edited = manifest.build(fleet, spec, "claude-code")
    assert edited["plugins"][0]["digest"] != p["digest"]
    assert edited["harness_digest"] != m["harness_digest"]
    assert edited["environment_digest"] == m["environment_digest"]  # the plugin is the harness's, not the sandbox's
    off = manifest.build(fleet, _spec(tmp_path, plugins=[{"path": "mkt", "name": "workflow", "enabled": False}]),
                         "claude-code")
    assert off["plugins"][0]["effective"] is False and off["harness_digest"] != edited["harness_digest"]
    plain = manifest.build(fleet, EnvSpec(name="e", harnesses=["claude-code"]), "claude-code")
    assert plain["plugins"] is None  # unchanged digests for environments without plugins


def test_the_probe_reads_the_seeds_plugins_in_the_sandbox(tmp_path):
    spec = _spec(tmp_path)
    script = manifest.probe_script(None, "/home/agent", "claude-code", spec)
    assert f"{plugins.SEED}/marketplaces/team/workflow" in script and "workflow@team" in script
    assert "workflow@team" not in manifest.probe_script(None, "/home/agent", "codex", spec)
    got = manifest.parse_probe("plugin\tworkflow@team\tabc\t2\nplugin\tgone@team\t-\t0\n")
    assert got["plugins"] == [{"id": "workflow@team", "present": True, "digest": "sha256:abc", "files": 2},
                              {"id": "gone@team", "present": False, "digest": None, "files": 0}]


# ====================================================================== the version floor
def test_the_lock_cant_go_below_the_seed_floor():
    lock = canary.load_lock()
    assert canary.version_key(lock["claude-code"]["version"]) >= canary.version_key(plugins.MIN_VERSION)
    canary.harnesses_for(lock)
    old = {**lock, "claude-code": {**lock["claude-code"], "version": "2.1.272"}}
    with pytest.raises(ValueError, match="older than 2.1.273"):
        canary.harnesses_for(old)
