"""Codex plugins from a harness lock and trust for exactly their hooks (#244; docs/harness-lock.md#codex-plugins).

The hashes below are what Codex 0.162.1 itself reported (app server `hooks/list`, `currentHash`) for these plugins
installed from a local marketplace; hangar must compute the same, or the hooks it trusts stay untrusted. With
HANGAR_TEST_CODEX naming the real CLI, the last test installs a lock's Codex form with the commands `mount` runs and
asks that Codex whether it runs the hooks:

    HANGAR_TEST_CODEX=$(command -v codex) uv run pytest tests/test_codex_plugins.py
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest

from hangar import codex_plugins, harness_lock, harness_thaw, manifest
from hangar.harnesses import HARNESSES
from hangar.models import Sandbox
from tests.test_harness_lock_plugins import _commit, _Prov, registry  # noqa: F401 - the registry fixture

CODEX = os.environ.get("HANGAR_TEST_CODEX")

# ai-dlc's workflow plugin, Codex form (plugins/ai-dlc-workflow/native/codex/, 0.4.0-r3)
_SHIM = 'python3 "${PLUGIN_ROOT:-${CODEX_HOME:-$HOME/.codex}/ai-dlc-workflow}/hooks/aidlc_hook.py" '
AIDLC = {
    ".codex-plugin/plugin.json": {"name": "ai-dlc-workflow", "version": "0.4.0-r3", "hooks": "./hooks.json",
                                  "skills": "./skills/"},
    "hooks.json": {"hooks": {
        "PreToolUse": [{"hooks": [{"command": _SHIM + "pre-tool", "timeout": 10, "type": "command"}],
                        "matcher": "Bash|exec_command"}],
        "SessionStart": [{"hooks": [{"command": _SHIM + "session-start", "timeout": 10, "type": "command"}]}],
        "Stop": [{"hooks": [{"command": _SHIM + "stop", "timeout": 10, "type": "command"}]}]}},
}
AIDLC_TRUST = {
    "ai-dlc-workflow@team-harnesses:hooks.json:pre_tool_use:0:0":
        "sha256:7fc91402c7e34f17a1dbad415f2871553ff661986a66b8bb2e05d235a339ef57",
    "ai-dlc-workflow@team-harnesses:hooks.json:session_start:0:0":
        "sha256:1abeaee9bcebabde1acbc2d47bf9d294147af9b12f467a47ce85d93036b04d3d",
    "ai-dlc-workflow@team-harnesses:hooks.json:stop:0:0":
        "sha256:91c41b49328c07dbac2723d2719cbac36444c2d9170f5389c14acc827813a749",
}

# Every field the hash covers, and each normalization: hooks files from a list of paths, an inline object list
# (with an empty one in it), defaults and clamps for timeouts, the default context limit left out (and ignored for
# Stop), a Stop matcher ignored, an empty command skipped, commandWindows ignored on Linux, non-ASCII and escapes.
VARIANTS = {
    "p": {
        ".codex-plugin/plugin.json": {"name": "p", "version": "1.0.0", "hooks": ["./hooks/a.json", "./b.json"]},
        "hooks/a.json": {"description": "variants", "hooks": {
            "PreToolUse": [
                {"matcher": "^Bash$", "hooks": [
                    {"type": "command", "command": "echo a", "statusMessage": "checking", "async": True},
                    {"type": "command", "command": "echo b", "timeout": 30, "additionalContextLimit": 2500}]},
                {"hooks": [{"type": "command", "command": 'echo ü "q"\ttab', "additionalContextLimit": 100}]}],
            "Stop": [{"matcher": "ignored", "hooks": [
                {"type": "command", "command": "echo stop", "additionalContextLimit": 7}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": "echo end", "timeout": 30},
                                      {"type": "command", "command": "echo end2"}]}],
            "Interrupt": [{"hooks": [{"type": "command", "command": "echo int", "timeout": 0}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "   "},
                                            {"type": "command", "command": "echo ups", "commandWindows": "echo win"}]}],
        }},
        "b.json": {"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo post", "timeout": 0}]}]}},
    },
    "q": {".codex-plugin/plugin.json": {"name": "q", "version": "1.0.0", "hooks": [
        {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo q"}]}]}},
        {"hooks": {}},
        {"hooks": {"SubagentStart": [{"hooks": [
            {"type": "command", "command": "echo sub", "additionalContextLimit": 0}]}]}}]}},
}
VARIANT_TRUST = {
    "p@m:b.json:post_tool_use:0:0": "sha256:5155b5411c91a25bdc2f0142c4bf9aaec2b23157a9646c72c8730a1839a7f215",
    "p@m:hooks/a.json:interrupt:0:0": "sha256:6b89a2afec063e62643cbe5c88a99575a94d1cdc30dd20ee7ee13ad24d1fbbfe",
    "p@m:hooks/a.json:pre_tool_use:0:0": "sha256:b1284bb2ae7777ceade93e8162b1b31dda53ebfcf74e8b118af95dff104958a9",
    "p@m:hooks/a.json:pre_tool_use:0:1": "sha256:ca1ebf7622b6b0a2353324895dbf8d737ad8ce56e5ff7e91dca1878bda0eb659",
    "p@m:hooks/a.json:pre_tool_use:1:0": "sha256:a40e5597c1defa53a3bc0d0afe584c88ac4d162db0b82096e712e3920dbd1b09",
    "p@m:hooks/a.json:session_end:0:0": "sha256:84be7971dba2dbb128add226e0537c753853bf675df8efb85a5dcbb611374b4f",
    "p@m:hooks/a.json:session_end:0:1": "sha256:225b63e785881b5ac9aab5e63e1f4e094bfb56561908bfa0ec553f798f614967",
    "p@m:hooks/a.json:stop:0:0": "sha256:236335e9e512004a0f5f42af65a07ea9059bd9a23dbb68286ed57fecf2b69ddf",
    "p@m:hooks/a.json:user_prompt_submit:0:1":
        "sha256:9c4ab9d6e10ed8234dcda93f0586d119be57dcd3f988d610c58b2f9d127f6564",
    "q@m:plugin.json#hooks[0]:session_start:0:0":
        "sha256:fe6c18753f9152f01be6092c225eb68751d8fe2f6ca048f539508729de0bb470",
    "q@m:plugin.json#hooks[2]:subagent_start:0:0":
        "sha256:64ba6ad23137fac4b07f07e9ddbeec80e0787114c3116fce98ef5cbc02b45cd0",
}


def _files(doc: dict[str, object]) -> dict[str, bytes]:
    return {k: (json.dumps(v, indent=2) if not isinstance(v, str) else v).encode() for k, v in doc.items()}


def test_hashes_and_keys_are_codex_0_162_1s():
    assert codex_plugins.plugin_trust("ai-dlc-workflow@team-harnesses", _files(AIDLC)) == AIDLC_TRUST
    got = {**codex_plugins.plugin_trust("p@m", _files(VARIANTS["p"])),
           **codex_plugins.plugin_trust("q@m", _files(VARIANTS["q"]))}
    assert got == VARIANT_TRUST
    # the hash is over the hook, not where the plugin is: the same hooks under another id hash the same
    other = codex_plugins.plugin_trust("ai-dlc-workflow@elsewhere", _files(AIDLC))
    assert sorted(other.values()) == sorted(AIDLC_TRUST.values())


def test_only_the_hooks_codex_loads_are_trusted():
    files = _files(AIDLC)
    # hooks files the manifest doesn't name, one Codex can't parse, paths that leave the plugin: nothing
    files["hooks/hooks.json"] = files["hooks.json"]  # the default path, ignored once the manifest names hooks
    assert codex_plugins.plugin_trust("x@m", files).keys() == {k.replace("ai-dlc-workflow@team-harnesses", "x@m")
                                                               for k in AIDLC_TRUST}
    for hooks in ("../hooks.json", "hooks.json", "./", ["./nope.json"], 3):
        man = {**AIDLC[".codex-plugin/plugin.json"], "hooks": hooks}
        assert codex_plugins.plugin_trust("x@m", {**files, ".codex-plugin/plugin.json": json.dumps(man).encode()}) \
            == {}, hooks
    bad = {**files, "hooks.json": json.dumps({"hooks": {}, "extra": 1}).encode()}  # deny_unknown_fields
    assert codex_plugins.plugin_trust("x@m", bad) == {}
    bad = {**files, "hooks.json": json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "x", "timeout": "10"}]}]}}).encode()}
    assert codex_plugins.plugin_trust("x@m", bad) == {}
    mcp = {**files, "hooks.json": json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "mcp_tool", "server": "s", "tool": "t"}, {"type": "command", "command": "x"}]}]}}).encode()}
    assert list(codex_plugins.plugin_trust("x@m", mcp)) == ["x@m:hooks.json:stop:0:1"]  # mcp_tool hooks: never
    # no manifest `hooks`: the default hooks/hooks.json
    man = {k: v for k, v in AIDLC[".codex-plugin/plugin.json"].items() if k != "hooks"}
    dflt = {**files, ".codex-plugin/plugin.json": json.dumps(man).encode()}
    assert {k.split(":")[1] for k in codex_plugins.plugin_trust("x@m", dflt)} == {"hooks/hooks.json"}


def test_override_is_one_toml_table_of_exactly_the_trust():
    t = {**AIDLC_TRUST, 'odd"key\\é:x.json:stop:0:0': "sha256:" + "0" * 64}
    doc = tomllib.loads(codex_plugins.override(t))
    assert doc == {"hooks": {"state": {k: {"trusted_hash": v} for k, v in t.items()}}}


def _codex_registry(fleet, registry, monkeypatch, tmp: Path, form: dict[str, bytes] | None = None):  # noqa: F811
    """The registry's `team` lock as a Codex harness with workflow@team's Codex form pinned (extra@team stays
    disabled, without one)."""
    monkeypatch.setattr(HARNESSES["codex"], "version", registry["lock"]["runtime"]["version"])
    hdir = registry["reg"] / "harnesses/team"
    form = form if form is not None else _files({**AIDLC, ".codex-plugin/plugin.json": {
        **AIDLC[".codex-plugin/plugin.json"], "name": "workflow"}, "hooks/aidlc_hook.py": "print('hi')\n"})
    comps = [c for c in registry["lock"]["components"] if not c["id"].startswith("native:")]
    for rel, data in sorted(form.items()):
        (hdir / "native/codex/workflow" / rel).parent.mkdir(parents=True, exist_ok=True)
        (hdir / "native/codex/workflow" / rel).write_bytes(data)
        comps.append({"id": f"native:codex/workflow/{rel}", "digest": harness_lock._sha(data),
                      "path": f"native/codex/workflow/{rel}"})
    lock = {**registry["lock"], "revision": 2, "components": comps,
            "runtime": {**registry["lock"]["runtime"], "harness": "codex"}}
    (hdir / "harness.lock").write_text(harness_lock.dump(lock))
    _commit(registry["reg"], "team r2: codex")
    envs = tmp / "envs-codex"
    envs.mkdir(exist_ok=True)
    (envs / "lockenv.yaml").write_text("name: lockenv\nharnesses: [codex]\n")
    fleet.catalog.dirs.insert(0, envs)
    fleet.catalog.reload()
    return lock


async def test_a_codex_lock_mounts_its_forms_and_trusts_only_their_hooks(fleet, registry, monkeypatch,  # noqa: F811
                                                                         tmp_path):
    lock = _codex_registry(fleet, registry, monkeypatch, tmp_path)
    assert harness_lock.check(registry["reg"] / "harnesses/team")["ok"]
    p = harness_thaw.pin(fleet, "team", "lockenv")
    d = harness_thaw.lock_dir(fleet.settings, p)
    want = {k.replace("ai-dlc-workflow@team-harnesses", "workflow@team"): v for k, v in AIDLC_TRUST.items()}
    assert codex_plugins.trust(d, lock) == want  # extra@team is disabled: nothing of it

    sb = Sandbox(name="x", environment="lockenv", provider="docker")
    sb.info[harness_thaw.INFO] = harness_thaw.info(p)
    assert codex_plugins.turn_env(fleet, sb, "codex") == {codex_plugins.TRUST_ENV: codex_plugins.override(want)}
    assert codex_plugins.turn_env(fleet, sb, "claude-code") == {}
    assert codex_plugins.turn_env(fleet, Sandbox(name="y", environment="lockenv", provider="docker"), "codex") == {}
    # every Codex turn carries it, before any subcommand argument
    h = HARNESSES["codex"]
    assert h.run.startswith('codex exec ${HANGAR_CODEX_HOOK_TRUST:+-c "$HANGAR_CODEX_HOOK_TRUST"} ')
    assert h.live_run == 'codex app-server ${HANGAR_CODEX_HOOK_TRUST:+-c "$HANGAR_CODEX_HOOK_TRUST"}'

    prov = _Prov()
    await harness_thaw.mount_files(fleet, prov, sb, "codex")
    staged = {k.split("/", 3)[3]: v for k, v in prov.files.items() if k.startswith("/tmp/hangar-thaw-codex-")}
    assert set(staged) == {"team/.agents/plugins/marketplace.json", "team/workflow/.codex-plugin/plugin.json",
                           "team/workflow/hooks.json", "team/workflow/hooks/aidlc_hook.py"}
    assert json.loads(staged["team/.agents/plugins/marketplace.json"]) == {
        "name": "team", "plugins": [{"name": "workflow", "source": {"source": "local", "path": "./workflow"}}]}
    root = next(c for c, u in prov.cmds if u == "root" and codex_plugins.MARKETPLACES in c)
    assert f"chmod -R a-w,a+rX {codex_plugins.MARKETPLACES}/team" in root
    install, user = prov.cmds[-1]
    assert user is None  # the agent's Codex, into the agent's ~/.codex
    codex = f"{h.prefix}/bin/codex"
    assert f"{codex} plugin marketplace add {codex_plugins.MARKETPLACES}/team" in install
    assert f"{codex} plugin add workflow@team" in install and "extra" not in install

    # turn 1: the probe asks the sandbox's Codex with the same override; any hook not trusted and on is drift
    lines = manifest._codex_hook_lines(_Svc(fleet), sb, "codex", "/home/u")
    assert len(lines) == 1 and shlex.quote(codex_plugins.override(want)) in lines[0]
    assert f"{h.prefix}/bin/codex" in lines[0] and "hooks/list" in lines[0]
    out = "\n".join(f"codexhook\t{k}\t{'trusted' if 'stop' not in k else 'untrusted'}\t1" for k in sorted(want))
    probe = manifest.parse_probe(out + "\nskill\tplan\t" + "a" * 64 + "\t1\n")
    assert len(probe["codex_hooks"]) == 3 and probe["skills"][0]["name"] == "plan"
    assert codex_plugins.drift(probe) == [{"component": "codex-hook:workflow@team:hooks.json:stop:0:0",
                                           "lock": "trusted", "effective": "untrusted"}]
    m = {"probe": {"ok": True, "codex_hooks": probe["codex_hooks"], "plugins": None}, "harness": {"name": "codex"}}
    assert "codex-hook:workflow@team:hooks.json:stop:0:0" in [x["component"] for x in harness_thaw.drift(p, m)]
    assert codex_plugins.drift(manifest.parse_probe("codexhook\t-\tno-python3\t0\n")) == [
        {"component": "codex-hooks", "lock": "trusted", "effective": "no-python3"}]
    assert codex_plugins.drift(manifest.parse_probe(
        "codexhook\tworkflow@team:hooks.json:stop:0:0\ttrusted\t0\n"))[0]["effective"] == "disabled"


class _Svc:
    def __init__(self, fleet):
        self.fleet, self.settings = fleet, fleet.settings


def test_a_form_the_lock_doesnt_pin_is_never_installed_or_trusted(fleet, registry, monkeypatch, tmp_path):  # noqa: F811
    lock = _codex_registry(fleet, registry, monkeypatch, tmp_path)
    d = registry["reg"] / "harnesses/team"
    (d / "native/codex/workflow/hooks/extra.json").write_text("{}")  # a file the lock doesn't pin
    with pytest.raises(ValueError, match="hooks/extra.json is not pinned by the lock"):
        codex_plugins.forms(d, lock)
    (d / "native/codex/workflow/hooks/extra.json").unlink()
    (d / "native/codex/workflow/hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "curl evil"}]}]}}))  # edited after the lock
    with pytest.raises(ValueError, match="native/codex/workflow/hooks.json is not pinned"):
        codex_plugins.forms(d, lock)
    assert "is not pinned" in (codex_plugins.check(d, lock) or "")
    renamed = {**lock, "components": [c for c in lock["components"] if c["id"] != "plugin:workflow@team"] + [
        {**next(c for c in lock["components"] if c["id"] == "plugin:workflow@team"), "id": "plugin:other@team"}]}
    with pytest.raises(ValueError, match="plugin:other@team has no Codex form"):
        codex_plugins.forms(d, renamed)
    off = {**lock, "components": [{**c, "enabled": False} if c["id"].startswith("plugin:") else c
                                  for c in lock["components"]]}
    assert codex_plugins.forms(d, off) == []  # disabled plugins load nowhere


def test_a_form_named_otherwise_is_refused_at_claim(fleet, registry, monkeypatch, tmp_path):  # noqa: F811
    _codex_registry(fleet, registry, monkeypatch, tmp_path, form=_files(AIDLC))  # plugin.json says ai-dlc-workflow
    with pytest.raises(harness_thaw.ThawError, match="names plugin 'ai-dlc-workflow', not 'workflow'"):
        harness_thaw.pin(fleet, "team", "lockenv")


@pytest.mark.skipif(not CODEX, reason="set HANGAR_TEST_CODEX to the real codex CLI")
def test_the_real_codex_runs_exactly_the_trusted_hooks(tmp_path):
    """Install ai-dlc's Codex form and another plugin the way `mount` does, then ask Codex (app server
    `hooks/list`) with and without hangar's override, and after the agent edits the installed hook."""
    mkt = tmp_path / "opt/team-harnesses"
    for name, doc in (("ai-dlc-workflow", AIDLC), ("other", {**AIDLC, ".codex-plugin/plugin.json": {
            **AIDLC[".codex-plugin/plugin.json"], "name": "other"}})):
        for rel, data in _files(doc).items():
            (mkt / name / rel).parent.mkdir(parents=True, exist_ok=True)
            (mkt / name / rel).write_bytes(data)
    (mkt / ".agents/plugins").mkdir(parents=True)
    (mkt / codex_plugins.MARKETPLACE).write_bytes(codex_plugins.marketplace_doc("team-harnesses",
                                                                                ["ai-dlc-workflow", "other"]))
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    for args in (["plugin", "marketplace", "add", str(mkt)], ["plugin", "add", "ai-dlc-workflow@team-harnesses"],
                 ["plugin", "add", "other@team-harnesses"]):
        subprocess.run([CODEX, *args], env=env, check=True, capture_output=True, timeout=60)
    t = codex_plugins.plugin_trust("ai-dlc-workflow@team-harnesses", _files(AIDLC))
    other = codex_plugins.plugin_trust("other@team-harnesses", _files(AIDLC))

    def ask(trust: dict[str, str], keys: list[str]) -> dict[str, str]:
        line = codex_plugins.probe_lines(trust, CODEX, str(tmp_path), keys)[0]
        out = subprocess.run(["bash", "-c", line], env=env, capture_output=True, text=True, timeout=60).stdout
        return {x["key"]: x["status"] for x in manifest.parse_probe(out).get("codex_hooks") or []}

    keys = sorted([*t, *other])
    assert ask(t, keys) == {**dict.fromkeys(t, "trusted"), **dict.fromkeys(other, "untrusted")}
    assert set(ask({}, keys).values()) == {"untrusted"}  # Codex's default: skipped
    hooks = next((home / ".codex/plugins/cache/team-harnesses/ai-dlc-workflow").rglob("hooks.json"))
    doc = json.loads(hooks.read_text())
    doc["hooks"]["Stop"][0]["hooks"][0]["command"] = "curl evil"
    hooks.write_text(json.dumps(doc))  # the agent edits its copy: that hook no longer runs
    got = ask(t, keys)
    assert got["ai-dlc-workflow@team-harnesses:hooks.json:stop:0:0"] == "modified"
    assert got["ai-dlc-workflow@team-harnesses:hooks.json:pre_tool_use:0:0"] == "trusted"
