"""Claude Code plugins in harness locks (#221; docs/harness-lock.md#plugins): freeze records each seed plugin as
`plugin:<name>@<marketplace>` with the manifest's digest and its files as a directory marketplace; the lock's digest is
the turn's harness_digest; a lock thawed on an environment without the plugin reads it from the lock (manifest, probe,
hangar's settings, the seed it mounts) with the same identity; changing the plugin changes the digest and shows as
drift. Mounting it into a real sandbox with the real Claude Code is tests/test_e2e_lock_plugins_docker.py."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from hangar import harness_lock, harness_thaw, manifest, plugins
from hangar.envspec import EnvSpec
from hangar.models import Sandbox, Session, SessionTurn
from tests.test_plugins import _mkt

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / harness_lock.SCHEMA_FILE).read_text())
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]


def _env(tmp: Path, with_plugins: bool = True) -> EnvSpec:
    if not (tmp / "mkt").exists():
        _mkt(tmp / "mkt", plugins_=("workflow", "extra"))
    d = {"name": "lockenv", "harnesses": ["claude-code"], "agent_config": {"instructions": "Plan, then build."}}
    if with_plugins:
        d["plugins"] = [{"path": "mkt", "name": "workflow"}, {"path": "mkt", "name": "extra", "enabled": False}]
    spec = EnvSpec.model_validate(d)
    spec.source_dir = tmp
    return spec


def _turn(fleet, spec: EnvSpec, sb: Sandbox | None = None, probe_plugins: list | None = None) -> dict:
    s = Session(name="s1", environment="lockenv", harness="claude-code", model="claude-opus-5-5", person=None)
    t = SessionTurn(message="x", run_id="run-1")
    s.turns.append(t)
    probe = {"global": None, "repo": [], "head": None, "skills": [], "plugins": probe_plugins}
    return manifest.build(fleet, spec, "claude-code", s=s, turn=t, n=1, probe=probe, sb=sb)


def _seen(spec: EnvSpec) -> list[dict]:
    """What the probe reports for the declared plugins' directories."""
    out = []
    for p in spec.plugins:
        r = plugins.resolved(spec, p)
        out.append({"id": r["id"], "present": True, "files": 2,
                    "digest": harness_lock.tree_digest(harness_lock.read_dir(r["dir"]))})
    return out


def _commit(root: Path, msg: str) -> None:
    if not (root / ".git").exists():
        subprocess.run([*GIT, "init", "-q", str(root)], check=True)
    subprocess.run([*GIT, "-C", str(root), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-qm", msg], check=True)


def test_freeze_records_plugins_and_reproduces_the_turns_harness_digest(fleet, tmp_path):
    spec = _env(tmp_path)
    m = _turn(fleet, spec, probe_plugins=_seen(spec))
    assert [p["name"] for p in m["plugins"]] == ["workflow", "extra"]
    b = harness_lock.freeze(fleet, m, spec, name="team")
    assert b["digest"] == m["harness_digest"]
    lock = b["lock"]
    jsonschema.validate(lock, SCHEMA)
    pl = [c for c in lock["components"] if c["id"].startswith("plugin:")]
    assert [(c["id"], c["path"], c["enabled"]) for c in pl] == [
        ("plugin:workflow@team", "plugins/team/workflow", True), ("plugin:extra@team", "plugins/team/extra", False)]
    assert pl[0]["digest"] == m["plugins"][0]["digest"] and pl[0]["tree"] == _seen(spec)[0]["digest"]
    mkt = json.loads(b["files"]["plugins/team/.claude-plugin/marketplace.json"])
    assert mkt["name"] == "team" and [e["name"] for e in mkt["plugins"]] == ["workflow", "extra"]
    assert b"Plan first." in b["files"]["plugins/team/workflow/skills/plan/SKILL.md"]
    out = tmp_path / "harness"
    harness_lock.write(out, lock, b["files"])
    res = harness_lock.check(out)
    assert res["ok"] and res["digest"] == m["harness_digest"], res

    # the identity is {name, marketplace, digest, enabled}: not the path or version, which a lock's mount changes
    moved = json.loads(json.dumps(m))
    moved["plugins"][0].update(path="/elsewhere", version="9", files=99, effective=False)
    assert manifest.section_digest(moved, manifest.HARNESS_SECTIONS) == m["harness_digest"]
    moved["plugins"][1]["enabled"] = True
    assert manifest.section_digest(moved, manifest.HARNESS_SECTIONS) != m["harness_digest"]

    # check: an unpinned marketplace entry or directory, an edited plugin file
    (out / "plugins/team/stray").mkdir()
    (out / "plugins/other").mkdir()
    doc = json.loads((out / "plugins/team/.claude-plugin/marketplace.json").read_text())
    doc["plugins"].append({"name": "sneaky", "source": "./stray"})
    (out / "plugins/team/.claude-plugin/marketplace.json").write_text(json.dumps(doc))
    (out / "plugins/team/workflow/skills/plan/SKILL.md").write_text("Skip the plan.\n")
    probs = harness_lock.check(out)["problems"]
    for want in ("plugins/team/stray is not in the lock", "plugins/other is not in the lock",
                 "lists sneaky, which the lock doesn't pin", "plugin:workflow@team: plugins/team/workflow/ has"):
        assert any(want in p for p in probs), (want, probs)


def test_a_changed_plugin_changes_the_digest_and_cant_be_frozen_from_the_old_turn(fleet, tmp_path):
    spec = _env(tmp_path)
    m = _turn(fleet, spec)
    (tmp_path / "mkt/workflow/skills/plan/SKILL.md").write_text("---\nname: plan\n---\nPlan twice.\n")
    m2 = _turn(fleet, spec)
    assert m2["harness_digest"] != m["harness_digest"]
    assert harness_lock.freeze(fleet, m2, spec, name="team")["digest"] == m2["harness_digest"]
    with pytest.raises(harness_lock.FreezeError, match="plugin:workflow@team: its marketplace changed since the turn"):
        harness_lock.freeze(fleet, m, spec, name="team")
    # a plugin fetched from elsewhere at build has no files to freeze
    doc = json.loads((tmp_path / "mkt/.claude-plugin/marketplace.json").read_text())
    doc["plugins"][1]["source"] = {"source": "github", "repo": "o/r"}
    (tmp_path / "mkt/.claude-plugin/marketplace.json").write_text(json.dumps(doc))
    with pytest.raises(harness_lock.FreezeError, match="plugin:extra@team: its marketplace entry fetches it"):
        harness_lock.freeze(fleet, _turn(fleet, spec), spec, name="team")


@pytest.fixture
def registry(fleet, tmp_path):
    """A registry holding `team` frozen from a turn with the plugins; the environment no longer declares them."""
    spec = _env(tmp_path)
    m = _turn(fleet, spec, probe_plugins=_seen(spec))
    b = harness_lock.freeze(fleet, m, spec, name="team")
    reg = tmp_path / "registry"
    harness_lock.write(reg / "harnesses/team", b["lock"], b["files"])
    _commit(reg, "team r1")
    fleet.settings.harness_repo = str(reg)
    return {"reg": reg, "digest": b["digest"], "lock": b["lock"], "manifest": m}


def test_a_lock_thawed_without_the_plugin_has_it_with_the_same_identity(fleet, tmp_path, registry):
    r = harness_thaw.resolve(fleet.settings, "team")
    d, stale = harness_thaw.materialize(fleet.settings, r)
    assert stale == [] and (d / "plugins/team/workflow/skills/plan/SKILL.md").is_file()
    assert harness_thaw.materialize(fleet.settings, r)[0] == d  # content-addressed: the same claim, the same dir
    bare = _env(tmp_path, with_plugins=False)
    assert bare.plugins == []
    sb = Sandbox(name="x", environment="lockenv", provider="docker")
    sb.info[harness_thaw.INFO] = harness_thaw.info({"name": "team", "revision": 1, "digest": r.digest, "dir": d.name})
    eff = harness_thaw.effective_spec(fleet, sb, bare)
    assert [(p.name, p.marketplace, p.enabled) for p in eff.plugins] == [("workflow", "team", True),
                                                                         ("extra", "team", False)]
    assert plugins.env(eff, "claude-code")["CLAUDE_CODE_PLUGIN_SEED_DIR"] == plugins.SEED
    assert plugins.settings(eff)["enabledPlugins"] == {"workflow@team": True, "extra@team": False}
    assert eff.agent_config.instructions == "Plan, then build." and eff.source_dir == d
    assert "/opt/hangar/claude-seed/marketplaces/team/workflow" in "\n".join(plugins.probe_lines(eff, "claude-code"))
    m = _turn(fleet, bare, sb=sb, probe_plugins=_seen(eff))
    assert m["harness_digest"] == registry["digest"] == registry["manifest"]["harness_digest"]
    assert m["environment"]["spec_digest"] == manifest._spec_digest(bare)  # the environment's identity is its own
    p = {"name": "team", "revision": 1, "digest": r.digest, "version": None,
         "components": [{k: c[k] for k in ("id", "digest", "tree", "enabled") if c.get(k) is not None}
                        for c in r.lock["components"]]}
    assert harness_thaw.drift(p, m) == []

    # the registry's plugin edited without re-freezing: a stale thaw names it, and turn 1 reports it as drift
    (registry["reg"] / "harnesses/team/plugins/team/workflow/skills/plan/SKILL.md").write_text("Skip the plan.\n")
    d2, stale = harness_thaw.materialize(fleet.settings, harness_thaw.resolve(fleet.settings, "team"))
    assert d2 != d and any("plugin:workflow@team" in x for x in stale)
    sb.info[harness_thaw.INFO]["dir"] = d2.name
    eff2 = harness_thaw.effective_spec(fleet, sb, bare)
    m2 = _turn(fleet, bare, sb=sb, probe_plugins=_seen(eff2))
    got = harness_thaw.drift(p, m2)
    assert [x["component"] for x in got] == ["plugin:workflow@team", "harness_digest"], got
    wf = next(c for c in registry["lock"]["components"] if c["id"] == "plugin:workflow@team")
    assert got[0]["lock"] == wf["digest"] != got[0]["effective"]
    # the seed lacking it (an image's seed instead of the lock's): drift on the effective side
    m3 = _turn(fleet, bare, sb=sb.model_copy(update={"info": {**sb.info, harness_thaw.INFO: {
        **sb.info[harness_thaw.INFO], "dir": d.name}}}), probe_plugins=[])
    assert [x["component"] for x in harness_thaw.drift(p, m3)] == ["plugin:workflow@team", "plugin:extra@team"]


def test_pin_carries_the_lock_dir_and_plugins_need_claude_code(fleet, tmp_path, registry, monkeypatch):
    fleet.catalog.dirs.insert(0, tmp_path / "envs")
    (tmp_path / "envs").mkdir()
    (tmp_path / "envs/lockenv.yaml").write_text("name: lockenv\nharnesses: [claude-code]\n")
    fleet.catalog.reload()
    from hangar.harnesses import HARNESSES

    monkeypatch.setattr(HARNESSES["claude-code"], "version", registry["lock"]["runtime"]["version"])
    p = harness_thaw.pin(fleet, "team", "lockenv")
    assert harness_thaw.lock_dir(fleet.settings, p) is not None and p["stale"] == []
    assert {"id": "plugin:extra@team", "enabled": False} | {"digest": p["components"][2]["digest"],
                                                            "tree": p["components"][2]["tree"]} in p["components"]
    lock = {**registry["lock"], "revision": 2, "runtime": {**registry["lock"]["runtime"], "harness": "codex"}}
    (registry["reg"] / "harnesses/team/harness.lock").write_text(harness_lock.dump(lock))
    (tmp_path / "envs/lockenv.yaml").write_text("name: lockenv\nharnesses: [claude-code, codex]\n")
    fleet.catalog.reload()
    monkeypatch.setattr(HARNESSES["codex"], "version", registry["lock"]["runtime"]["version"])
    with pytest.raises(harness_thaw.ThawError, match="plugins load only in claude-code"):
        harness_thaw.pin(fleet, "team", "lockenv")


class _Prov:
    """Records what mount_files does to a sandbox."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.cmds: list[tuple[str, str | None]] = []

    async def exec(self, sb, command, timeout=120, cwd=None, user=None):
        from hangar.providers.base import ExecResult

        self.cmds.append((command, user))
        out = "alpha\0hangar-environment\0" if command.startswith("mkdir -p") else ""
        return ExecResult(exit_code=0, stdout=out, stderr="")

    async def write_file(self, sb, path, data, mode=0o644):
        self.files[path] = data


async def test_mount_writes_the_lock_and_builds_the_seed_as_root(fleet, tmp_path, registry):
    r = harness_thaw.resolve(fleet.settings, "team")
    d, _ = harness_thaw.materialize(fleet.settings, r)
    sb = Sandbox(name="x", environment="lockenv", provider="docker")
    sb.info[harness_thaw.INFO] = {"name": "team", "revision": 1, "digest": r.digest, "dir": d.name}
    prov = _Prov()
    await harness_thaw.mount_files(fleet, prov, sb, "claude-code")
    home = f"/home/{fleet.settings.sandbox_user}"
    assert prov.files[f"{home}/.claude/CLAUDE.md"] == b"Plan, then build."
    staged = {k.split("/", 3)[3]: v for k, v in prov.files.items() if k.startswith("/tmp/hangar-thaw-")}
    assert set(staged) == {"team/.claude-plugin/marketplace.json", "team/workflow/.claude-plugin/plugin.json",
                           "team/workflow/skills/plan/SKILL.md", "team/extra/.claude-plugin/plugin.json",
                           "team/extra/skills/plan/SKILL.md"}
    drop = next(c for c, _ in prov.cmds if "rm -rf -- alpha" in c)  # the image's skill goes; hangar's own stays
    assert "hangar-environment" not in drop
    script, user = prov.cmds[-1]
    assert user == "root"
    for want in (f"rm -rf {plugins.SEED}", f"plugin marketplace add {plugins.SEED}/marketplaces/team",
                 "plugin install workflow@team", "plugin install extra@team", f"chown -R root:root {plugins.SEED}"):
        assert want in script, (want, script)
    # without a lock (or a pin from before #221), nothing happens
    prov2 = _Prov()
    await harness_thaw.mount_files(fleet, prov2, Sandbox(name="y", environment="lockenv", provider="docker"),
                                   "claude-code")
    assert not prov2.files and not prov2.cmds
    assert harness_thaw.info({"name": "team", "revision": 1, "digest": r.digest}) is None
