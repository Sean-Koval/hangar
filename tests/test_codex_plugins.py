"""Codex plugins from a harness lock and trust for exactly their hooks (#244; docs/harness-lock.md#codex-plugins).

The hashes below are what Codex 0.162.1 itself reported (app server `hooks/list`, `currentHash`) for these plugins
installed from a local marketplace; hangar must compute the same, or the hooks it trusts stay untrusted. With
HANGAR_TEST_CODEX naming the real CLI, the last tests install a lock's Codex form with the commands `mount` runs, ask
that Codex whether it runs the hooks, and run a turn with hangar's Codex command to see them fire (KAN-72):

    HANGAR_TEST_CODEX=$(command -v codex) uv run pytest tests/test_codex_plugins.py
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

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


def test_untrusted_names_every_hook_codex_would_skip():
    """--- KAN-72: what of a form's hooks never runs under hangar's trust, for `hangar doctor`."""
    files = _files(AIDLC)
    assert codex_plugins.untrusted(files) == []
    odd = {**files, "hooks.json": json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "prompt", "prompt": "x"}, {"type": "command", "command": "x"}, {"type": "command", "command": " "},
        {"type": "mcp_tool", "server": "s", "tool": "t"}]}]}}).encode()}
    assert codex_plugins.untrusted(odd) == ["hooks.json:stop:0:0 is a prompt hook",
                                            "hooks.json:stop:0:2 is an empty command",
                                            "hooks.json:stop:0:3 is a mcp_tool hook"]
    man = AIDLC[".codex-plugin/plugin.json"]
    for hooks, why in (("./nope.json", "nope.json isn't in the plugin"),
                       ("../hooks.json", "hooks path '../hooks.json' isn't a ./ path inside the plugin"),
                       ([{"hooks": {}, "extra": 1}], "plugin.json#hooks[0] doesn't parse"),
                       (3, "the manifest's hooks are neither paths nor hooks objects")):
        man_file = json.dumps({**man, "hooks": hooks}).encode()
        assert codex_plugins.untrusted({**files, ".codex-plugin/plugin.json": man_file}) == [why], hooks
    bad = {**files, "hooks.json": json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "x", "timeout": "10"}]}]}}).encode()}
    assert codex_plugins.untrusted(bad) == ["hooks.json doesn't parse"]
    assert codex_plugins.untrusted({}) == [".codex-plugin/plugin.json doesn't parse"]
    # an empty hooks file, or none at all, is nothing to trust and nothing skipped
    assert codex_plugins.untrusted({**files, "hooks.json": b'{"hooks": {}}'}) == []
    assert codex_plugins.untrusted({".codex-plugin/plugin.json": b'{"name": "x"}'}) == []


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


def test_doctor_flags_a_codex_lock_whose_hooks_would_stay_untrusted(fleet, registry, monkeypatch,  # noqa: F811
                                                                    tmp_path):
    """--- KAN-72: `hangar doctor`'s `codex plugin hooks` check, over the team harness repo's Codex locks."""
    h = SimpleNamespace(settings=fleet.settings)
    assert codex_plugins.doctor_checks(h) == []  # a Claude Code lock: not this check's
    _codex_registry(fleet, registry, monkeypatch, tmp_path)
    [chk] = codex_plugins.doctor_checks(h)
    assert chk["name"] == "codex plugin hooks" and chk["status"] == "ok", chk
    assert chk["detail"] == "1 Codex harness(es), 1 plugin(s), 3 hook(s) trusted by hash"

    d = registry["reg"] / "harnesses/team/native/codex/workflow"
    (d / "hooks/extra.json").write_text("{}")  # a file the lock doesn't pin: a claim refuses the lock
    [chk] = codex_plugins.doctor_checks(h)
    assert chk["status"] == "fail" and "team: plugin:workflow@team: native/codex/workflow/hooks/extra.json is not " \
        "pinned" in chk["detail"] and "hangar harness check" in chk["fix"]
    (d / "hooks/extra.json").unlink()

    form = _files({**AIDLC, ".codex-plugin/plugin.json": {**AIDLC[".codex-plugin/plugin.json"], "name": "workflow"},
                   "hooks.json": {"hooks": {**AIDLC["hooks.json"]["hooks"], "PostToolUse": [{"hooks": [
                       {"type": "prompt", "prompt": "review it"}]}]}}, "hooks/aidlc_hook.py": "print('hi')\n"})
    _codex_registry(fleet, registry, monkeypatch, tmp_path, form=form)  # pinned, but a hook Codex skips
    [chk] = codex_plugins.doctor_checks(h)
    assert chk["status"] == "fail"
    assert chk["detail"] == "team workflow@team: hooks.json:post_tool_use:0:0 is a prompt hook"


def test_turn_1_asks_codex_about_every_hook_of_the_locks_plugins(fleet, registry, monkeypatch, tmp_path):  # noqa: F811
    """--- KAN-72: the probe names the lock's plugins too, so a hook of theirs hangar didn't trust is drift."""
    _codex_registry(fleet, registry, monkeypatch, tmp_path)
    p = harness_thaw.pin(fleet, "team", "lockenv")
    sb = Sandbox(name="x", environment="lockenv", provider="docker")
    sb.info[harness_thaw.INFO] = harness_thaw.info(p)
    t, ids = codex_plugins.mounted_forms(fleet, sb, "codex")
    assert len(t) == 3 and ids == ["workflow@team"]  # extra@team is disabled
    [line] = manifest._codex_hook_lines(_Svc(fleet), sb, "codex", "/home/u")
    assert line.endswith(" " + shlex.quote(json.dumps(["workflow@team"])) + " || printf 'codexhook\\t-\\t%s\\t0\\n' "
                         "error; else printf 'codexhook\\t-\\t%s\\t0\\n' no-python3; fi")
    # a lock plugin with no hooks hangar trusts is still asked about
    assert codex_plugins.probe_lines({}, "codex", "/w", plugins=["workflow@team"])
    assert codex_plugins.probe_lines({}, "codex", "/w") == []
    probe = manifest.parse_probe("codexhook\tworkflow@team:hooks.json:post_tool_use:0:0\tuntrusted\t1\n"
                                 "codexhook\tworkflow@team:hooks.json:stop:0:0\tmodified\t0\n"
                                 "codexhook\tworkflow@team\tskipped\t0\n")
    assert codex_plugins.drift(probe) == [
        {"component": "codex-hook:workflow@team:hooks.json:post_tool_use:0:0", "lock": "trusted",
         "effective": "untrusted"},
        {"component": "codex-hook:workflow@team:hooks.json:stop:0:0", "lock": "trusted", "effective": "modified"},
        {"component": "codex-hook:workflow@team", "lock": "trusted", "effective": "skipped"}]
    # the lock's directory without its forms: nothing trusted, the plugins still asked about
    monkeypatch.setattr(codex_plugins, "trust", lambda *a: (_ for _ in ()).throw(ValueError("gone")))
    assert codex_plugins.mounted_forms(fleet, sb, "codex") == ({}, ["workflow@team"])


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


def _install(tmp: Path, forms: dict[str, dict[str, bytes]]) -> dict[str, str]:
    """Install Codex forms as `mount` does, into a fresh CODEX_HOME; the environment to run that Codex with."""
    mkt = tmp / "opt/team-harnesses"
    for name, files in forms.items():
        for rel, data in files.items():
            (mkt / name / rel).parent.mkdir(parents=True, exist_ok=True)
            (mkt / name / rel).write_bytes(data)
    (mkt / ".agents/plugins").mkdir(parents=True)
    (mkt / codex_plugins.MARKETPLACE).write_bytes(codex_plugins.marketplace_doc("team-harnesses", sorted(forms)))
    home = tmp / "home"
    (home / ".codex").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    for args in (["plugin", "marketplace", "add", str(mkt)],
                 *[["plugin", "add", f"{n}@team-harnesses"] for n in sorted(forms)]):
        subprocess.run([CODEX, *args], env=env, check=True, capture_output=True, timeout=60)
    return env


@pytest.mark.skipif(not CODEX, reason="set HANGAR_TEST_CODEX to the real codex CLI")
def test_the_real_codex_reports_the_lock_plugins_hooks_hangar_didnt_trust(tmp_path):
    """--- KAN-72: with the lock's plugin ids, the probe also reports a hook of theirs outside the trust, and a plugin
    whose hooks Codex skipped in part (a prompt hook: unsupported in 0.162.1). Codex loads an mcp_tool hook, which
    hangar never trusts."""
    doc = json.loads(json.dumps(AIDLC))
    doc["hooks.json"]["hooks"]["Stop"][0]["hooks"] += [{"type": "prompt", "prompt": "check"},
                                                       {"type": "mcp_tool", "server": "s", "tool": "t"}]
    files = _files(doc)
    env = _install(tmp_path, {"ai-dlc-workflow": files})
    t = codex_plugins.plugin_trust("ai-dlc-workflow@team-harnesses", files)
    assert codex_plugins.untrusted(files) == ["hooks.json:stop:0:1 is a prompt hook",
                                              "hooks.json:stop:0:2 is a mcp_tool hook"]
    mcp = "ai-dlc-workflow@team-harnesses:hooks.json:stop:0:2"
    stop = "ai-dlc-workflow@team-harnesses:hooks.json:stop:0:0"
    partial = {k: v for k, v in t.items() if k != stop}
    line = codex_plugins.probe_lines(partial, CODEX, str(tmp_path), plugins=["ai-dlc-workflow@team-harnesses"])[0]
    out = subprocess.run(["bash", "-c", line], env=env, capture_output=True, text=True, timeout=60).stdout
    probe = manifest.parse_probe(out)
    assert {x["key"]: x["status"] for x in probe["codex_hooks"]} == {
        **dict.fromkeys(partial, "trusted"), stop: "untrusted", mcp: "untrusted",
        "ai-dlc-workflow@team-harnesses": "skipped"}
    assert [x["component"] for x in codex_plugins.drift(probe)] == [
        f"codex-hook:{stop}", f"codex-hook:{mcp}", "codex-hook:ai-dlc-workflow@team-harnesses"]


class _Responses(BaseHTTPRequestHandler):
    """A minimal OpenAI Responses API: every turn's answer is one assistant message ("hi"), streamed."""

    def log_message(self, *a):
        pass

    def _empty(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # Codex tries a websocket first, then falls back to HTTP
        self._empty(404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self.path.endswith("/responses"):
            return self._empty(404)

        def ev(kind: str, **kw) -> str:
            return f"event: {kind}\ndata: {json.dumps({'type': kind, **kw})}\n\n"

        item = {"type": "message", "role": "assistant", "id": "msg_1",
                "content": [{"type": "output_text", "text": "hi"}]}
        usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        body = (ev("response.created", response={"id": "resp_1"}) + ev("response.output_item.done", item=item)
                + ev("response.completed", response={"id": "resp_1", "usage": usage})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.skipif(not CODEX, reason="set HANGAR_TEST_CODEX to the real codex CLI")
def test_a_real_codex_turn_fires_the_workflow_plugins_hooks_only_when_trusted(tmp_path):
    """--- KAN-72: hangar's own Codex turn command (HARNESSES["codex"].run, with TRUST_ENV as fleet.py sets it) runs
    the workflow plugin's SessionStart and Stop hooks; without the trust Codex skips them silently. The model is a
    local stand-in for the Responses API, so no key or network is needed."""
    fired = tmp_path / "fired.log"
    files = {**_files(AIDLC), "hooks/aidlc_hook.py": f"import sys\nopen({str(fired)!r}, 'a').write(sys.argv[1] + "
                                                     "'\\n')\n".encode()}
    env = _install(tmp_path, {"ai-dlc-workflow": files})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Responses)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    cfg = Path(env["CODEX_HOME"]) / "config.toml"  # `codex plugin add` enabled the plugin there: keep that
    cfg.write_text('model_provider = "stand-in"\n' + cfg.read_text()
                   + '\n[model_providers.stand-in]\nname = "stand-in"\n'
                   f'base_url = "{base}"\nenv_key = "OPENAI_API_KEY"\nwire_api = "responses"\n'
                   "request_max_retries = 0\nstream_max_retries = 0\n")
    (tmp_path / "prompt.txt").write_text("say hi")
    env.pop("HANGAR_MODEL", None)
    env.update(OPENAI_API_KEY="sk-stand-in", HANGAR_PROMPT_FILE=str(tmp_path / "prompt.txt"),
               PATH=f"{Path(CODEX).parent}{os.pathsep}{env['PATH']}")
    t = codex_plugins.plugin_trust("ai-dlc-workflow@team-harnesses", files)

    def turn(extra: dict[str, str]) -> str:
        fired.unlink(missing_ok=True)
        r = subprocess.run(["bash", "-c", HARNESSES["codex"].run], env={**env, **extra}, cwd=tmp_path,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and '"text":"hi"' in r.stdout.replace(" ", ""), r.stderr[-2000:]
        return fired.read_text() if fired.exists() else ""

    try:
        assert turn({}) == ""  # untrusted: skipped, and the turn still succeeds
        assert turn({codex_plugins.TRUST_ENV: codex_plugins.override(t)}).split() == ["session-start", "stop"]
    finally:
        srv.shutdown()

