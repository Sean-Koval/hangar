"""KAN-374: harness_digest covers the hook scripts a settings file runs.

A harness's settings file pins its sibling native files with `env.HARNESS_NATIVE_DIGEST` (the team harness's
renderer writes it, KAN-473). hangar checks that pin wherever it reads a settings file: the turn manifest records a
digest no lock can hold when the scripts aren't the pinned ones (so the turn's harness_digest drifts from its lock's),
freeze refuses such a turn, and `hangar harness check` fails a harness whose scripts changed under the pin. A settings
file without the pin keeps its own digest, so every existing lock keeps its identity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from hangar import harness_lock, manifest
from hangar.harnesses import HARNESSES, Harness
from hangar.models import Session, SessionTurn

HOOK = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"
WEAK = "#!/usr/bin/env python3\n# the guard, weakened\n"
MANIFEST_SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "docs/api/manifest.schema.json").read_text())


def _render_pin(files: dict[str, str]) -> str:
    """The team harness's formula (scripts/render_envs.py `native_digest`), written out independently."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(f"{path}\0{hashlib.sha256(files[path].encode()).hexdigest()}\n".encode())
    return "sha256:" + h.hexdigest()


def _settings(pin: str | None) -> str:
    doc: dict = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": 'python3 "$HOME/.tagent/hooks/guard.py"'}]}]}}
    if pin:
        doc["env"] = {manifest.NATIVE_PIN: pin}
    return json.dumps(doc, indent=2) + "\n"


@pytest.fixture
def hookenv(tmp_path, monkeypatch):
    monkeypatch.setitem(HARNESSES, "tagent", Harness(name="tagent", title="t", run="true",
                                                     skills_dir=".tagent/skills",
                                                     instructions_file=".tagent/AGENTS.md"))
    envs = tmp_path / "envs"
    envs.mkdir()

    def write(hook: str, *, pinned_to: str | None = HOOK, extra: dict[str, str] | None = None) -> None:
        pin = _render_pin({"hooks/guard.py": pinned_to}) if pinned_to is not None else None
        files = {".tagent/hooks/guard.py": hook, ".tagent/settings.json": _settings(pin), **(extra or {})}
        (envs / "hookenv.yaml").write_text(yaml.safe_dump({
            "name": "hookenv", "harnesses": ["tagent"],
            "agent_config": {"instructions": "Run the tests first.", "files": files}}))

    write(HOOK)
    return envs, write


def _turn(fleet, envs: Path) -> tuple[dict, object]:
    if envs not in fleet.catalog.dirs:
        fleet.catalog.dirs.insert(0, envs)
    fleet.catalog.reload()
    spec = fleet.catalog.get("hookenv")
    s = Session(name="s1", environment="hookenv", harness="tagent", model="claude-opus-5-5", person=None)
    t = SessionTurn(message="x", run_id="run-1")
    s.turns.append(t)
    return manifest.build(fleet, spec, "tagent", s=s, turn=t, n=1, probe=None), spec


def _env_hooks(m: dict) -> list[dict]:
    return [h for h in m["hooks"] if h["source"] == "environment:.tagent/settings.json"]


def test_native_digest_is_the_team_harness_formula():
    files = {"hooks/guard.py": HOOK, "hooks/b.py": "x", "README": ""}
    assert manifest.native_digest(files) == _render_pin(files)
    assert manifest.native_digest({k: v.encode() for k, v in files.items()}) == _render_pin(files)


def test_a_matching_pin_keeps_the_settings_files_own_digest():
    content = _settings(_render_pin({"hooks/guard.py": HOOK}))
    assert manifest.hooks_digest(content, {"hooks/guard.py": HOOK}) == (manifest._sha(content), None)
    # no pin (a lock from before KAN-473), or not JSON: the file's own digest, whatever sits beside it
    for plain in (_settings(None), "not json"):
        assert manifest.hooks_digest(plain, {"hooks/guard.py": WEAK}) == (manifest._sha(plain), None)


def test_a_turn_with_a_different_hook_script_reports_drift(fleet, hookenv):
    envs, write = hookenv
    m, _ = _turn(fleet, envs)
    hooks = _env_hooks(m)
    assert hooks and all(h["digest"] == manifest._sha(_settings(_render_pin({"hooks/guard.py": HOOK}))) for h in hooks)
    assert not any("native" in h for h in hooks)

    write(WEAK)  # the same settings file (same pin), a weakened guard beside it
    drifted, _ = _turn(fleet, envs)
    assert drifted["harness_digest"] != m["harness_digest"]
    assert all(h["native"] == {"pinned": _render_pin({"hooks/guard.py": HOOK}),
                               "actual": _render_pin({"hooks/guard.py": WEAK})} for h in _env_hooks(drifted))
    jsonschema.validate(drifted, MANIFEST_SCHEMA)

    write(HOOK, extra={".tagent/hooks/extra.py": "print(1)\n"})  # a script the pin doesn't name
    added, _ = _turn(fleet, envs)
    assert added["harness_digest"] != m["harness_digest"]

    write(HOOK)
    again, _ = _turn(fleet, envs)
    assert again["harness_digest"] == m["harness_digest"]


def test_a_frozen_lock_has_the_turns_digest_and_its_scripts_are_covered(fleet, hookenv, tmp_path):
    envs, write = hookenv
    m, spec = _turn(fleet, envs)
    b = harness_lock.freeze(fleet, m, spec, name="team")
    assert b["digest"] == m["harness_digest"]
    assert b["files"]["native/tagent/hooks/guard.py"] == HOOK.encode()
    assert not any("hooks/guard.py" in w for w in b["warnings"])  # the pin covers it
    out = tmp_path / "harness"
    harness_lock.write(out, b["lock"], b["files"])
    assert harness_lock.check(out)["ok"]

    # the guard is weakened and the lock's component digest edited to match: the pin still names the old guard
    (out / "native/tagent/hooks/guard.py").write_text(WEAK)
    lock = harness_lock.load(out)
    for c in lock["components"]:
        if c["id"] == "native:tagent/hooks/guard.py":
            c["digest"] = manifest._sha(WEAK)
    (out / harness_lock.LOCK_FILE).write_text(harness_lock.dump(lock))
    res = harness_lock.check(out)
    assert not res["ok"] and res["digest"] == m["harness_digest"]
    assert res["problems"] == [f"native/tagent/settings.json: its {manifest.NATIVE_PIN} is "
                               f"{_render_pin({'hooks/guard.py': HOOK})}, but the native files beside it digest to "
                               f"{_render_pin({'hooks/guard.py': WEAK})}: a hook script changed without the settings "
                               "file, so the harness_digest would not show it"]


def test_freeze_refuses_a_hook_script_that_changed_since_the_turn(fleet, hookenv):
    envs, write = hookenv
    m, _ = _turn(fleet, envs)
    write(WEAK)
    fleet.catalog.reload()
    with pytest.raises(harness_lock.FreezeError) as e:
        harness_lock.freeze(fleet, m, fleet.catalog.get("hookenv"), name="team")
    assert any(g.startswith(f"native:tagent/settings.json: its {manifest.NATIVE_PIN} is") for g in e.value.gaps)


def test_without_a_pin_nothing_changes(fleet, hookenv, tmp_path):
    envs, write = hookenv
    write(HOOK, pinned_to=None)
    m, spec = _turn(fleet, envs)
    assert all(h["digest"] == manifest._sha(_settings(None)) for h in _env_hooks(m))
    b = harness_lock.freeze(fleet, m, spec, name="team")
    assert any(w.startswith("native:tagent/hooks/guard.py: read from environment") for w in b["warnings"])
    out = tmp_path / "harness"
    harness_lock.write(out, b["lock"], b["files"])
    assert harness_lock.check(out)["ok"]


def test_a_guard_placed_at_an_absolute_home_path_still_drifts(fleet, hookenv):
    """`files` is staged after `agent_config.files`, so an absolute path under /home/<user>/ replaces the guard."""
    envs, write = hookenv
    m, _ = _turn(fleet, envs)
    write(HOOK)
    doc = yaml.safe_load((envs / "hookenv.yaml").read_text())
    doc["files"] = {"/home/agent/.tagent/hooks/guard.py": WEAK}
    (envs / "hookenv.yaml").write_text(yaml.safe_dump(doc))
    drifted, spec = _turn(fleet, envs)
    assert drifted["harness_digest"] != m["harness_digest"]
    assert manifest.home_files(spec)[".tagent/hooks/guard.py"] == WEAK
    with pytest.raises(harness_lock.FreezeError):
        harness_lock.freeze(fleet, drifted, spec, name="team")


def test_a_pin_with_nothing_beside_it_drifts():
    content = _settings(_render_pin({"hooks/guard.py": HOOK}))
    digest, mismatch = manifest.hooks_digest(content, {})
    assert digest != manifest._sha(content) and mismatch["actual"] == manifest.native_digest({})


def _harness_dir(tmp_path: Path, settings_rel: str, pin_files: dict[str, str], on_disk: dict[str, str],
                 keyed_to: str) -> Path:
    """A claude-code harness directory with a pinned settings file at `settings_rel`, its lock keying the hooks to
    `keyed_to` (the settings file's own text, as freeze writes it, or another, as the team harness renders it)."""
    root = tmp_path / "h"
    files = {f"native/claude-code/{settings_rel}": _settings(_render_pin(pin_files)).encode(),
             **{f"native/claude-code/{k}": v.encode() for k, v in on_disk.items()}}
    comps = [{"id": f"native:claude-code/{k.removeprefix('native/claude-code/')}", "digest": manifest._sha(v),
              "path": k} for k, v in sorted(files.items())]
    lock = {"schema": harness_lock.SCHEMA, "name": "team", "revision": 1,
            "runtime": {"harness": "claude-code", "version": "2.1.296", "pack": None},
            "model": {"id": "claude-opus-5-5", "policy": None}, "components": comps,
            "binding": {"mcp": [], "hooks": [{"event": "PreToolUse", "source": f"environment:.claude/{settings_rel}",
                                              "digest": keyed_to}], "permissions": {"approvals": "deny-risky"}},
            "plugins": {"autoUpdate": False}, "remote": []}
    harness_lock.write(root, lock, files)
    return root


@pytest.mark.parametrize("settings_rel", ["settings.json", "project/settings.json"])
def test_check_fails_a_stale_pin_the_lock_keys_its_hooks_to(tmp_path, settings_rel):
    own = manifest._sha(_settings(_render_pin({"hooks/guard.py": HOOK})))
    stale = _harness_dir(tmp_path, settings_rel, {"hooks/guard.py": HOOK}, {"hooks/guard.py": WEAK}, own)
    res = harness_lock.check(stale)
    assert not res["ok"] and any(p.startswith(f"native/claude-code/{settings_rel}: its") for p in res["problems"])


def test_check_leaves_a_pin_the_lock_keyed_to_another_text(tmp_path):
    """The team harness keeps an old pin on disk, renders a fresh one and locks the rendered text: not stale."""
    rendered = manifest._sha(_settings(_render_pin({"hooks/guard.py": WEAK})))
    h = _harness_dir(tmp_path, "settings.json", {"hooks/guard.py": HOOK}, {"hooks/guard.py": WEAK}, rendered)
    assert harness_lock.check(h)["ok"], harness_lock.check(h)["problems"]
