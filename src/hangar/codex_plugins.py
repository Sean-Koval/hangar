"""Codex plugins from a harness lock, with exactly their hooks trusted (#244; docs/harness-lock.md#codex-plugins).

A lock's `plugin:<name>@<marketplace>` is a Claude Code plugin (plugins.py, #221). Its Codex form is the directory
`native/codex/<name>/` of the same lock: a Codex local plugin (`.codex-plugin/plugin.json`, as ai-dlc's workflow
plugin builds it), every file of it pinned by a `native:codex/<name>/...` component. A Codex sandbox mounted from the
lock gets the Codex form of each enabled plugin: `mount` stages it as a local marketplace under `MARKETPLACES`
(root's, read-only), and the sandbox's Codex installs it from there (`codex plugin marketplace add`, `codex plugin
add`), so the plugin's id is the lock's `<name>@<marketplace>`.

Codex runs a plugin's hooks only once they are trusted, and skips untrusted ones silently. Verified against Codex
0.162.1 (harnesses.lock.yaml), its source at tag rust-v0.162.1 and the CLI itself:

* trust is per hook handler: `hooks.state."<key>".trusted_hash`, read from the user config and the session's `-c`
  flags only (codex-rs/hooks/src/config_rules.rs). A hook whose `trusted_hash` is its current hash is `trusted`, one
  with another hash `modified` (skipped), one without `untrusted` (skipped);
* `<key>` is `<plugin id>:<hooks file, relative to the plugin root>:<event label>:<group index>:<handler index>`
  (`plugin.json#hooks[<i>]` for an inline hooks object), and the hash is `sha256:` over the compact, key-sorted JSON
  of the normalized handler (`hook_hash` and `version_for_toml` there; `hash_handler` here). It covers the hook's
  event, matcher, command, timeout, async flag, status message and context limit, not the plugin's path;
* Codex's own app server trusts the hooks of workspace plugins the same way: a `trusted_hash` write per hook of the
  plugin, with the hash Codex computed (codex-rs/app-server/src/effective_plugin_change.rs).

So hangar computes the trust of exactly the lock's Codex forms (`trust`) and passes it to every Codex turn as one
`-c hooks.state={...}` override (`TRUST_ENV`, in the harness's `run` and `live_run`): nothing is written to the agent's
config, the agent's other hooks keep whatever trust they had, and a hook the agent edits in its plugin cache no longer
matches its hash and stops running. A hardened turn runs Codex with hooks off (harden.CODEX_FLAGS), plugins' included.

Turn 1 checks it (`probe_lines`, harness_thaw.drift): the probe asks the sandbox's Codex for its hooks (app server
`hooks/list`, with the same override) and reports each trusted key's status; one that isn't `trusted` and enabled is
drift (`codex-hook:<key>`). With the lock's plugin ids it also reports every other hook Codex lists for those
plugins (not in hangar's trust, so `untrusted`) and a plugin whose hooks Codex warns it skipped (`skipped`: a prompt
hook, say), both drift too (KAN-72). `hangar doctor` checks the same before any sandbox (`doctor_checks`): each
Codex harness in the team harness repo whose Codex forms a claim refuses, or that declare a hook hangar wouldn't
trust (`untrusted`), fails `codex plugin hooks`.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import shlex
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hangar import harness_lock

if TYPE_CHECKING:
    from hangar.fleet import Fleet
    from hangar.models import Sandbox

HARNESS = "codex"
FORMS = "native/codex"  # a lock's Codex forms: native/codex/<plugin name>/
MANIFEST = ".codex-plugin/plugin.json"
DEFAULT_HOOKS = "hooks/hooks.json"  # codex-rs/core-plugins/src/loader.rs DEFAULT_HOOKS_CONFIG_FILE
MARKETPLACE = ".agents/plugins/marketplace.json"  # the first of Codex's marketplace manifest paths
MARKETPLACES = "/opt/hangar/codex-marketplaces"
TRUST_ENV = "HANGAR_CODEX_HOOK_TRUST"
PROBE_TAG = "codexhook"
PROBE_SECONDS = 10

# Codex 0.162.1's hook events (HookEventsToml) and their labels in state keys (hook_event_key_label)
EVENTS = {"PreToolUse": "pre_tool_use", "PermissionRequest": "permission_request", "PostToolUse": "post_tool_use",
          "PreCompact": "pre_compact", "PostCompact": "post_compact", "SessionStart": "session_start",
          "SessionEnd": "session_end", "UserPromptSubmit": "user_prompt_submit", "SubagentStart": "subagent_start",
          "SubagentStop": "subagent_stop", "Stop": "stop", "Interrupt": "interrupt"}
NO_MATCHER = {"UserPromptSubmit", "Stop", "Interrupt"}  # matcher_pattern_for_event: their matcher is ignored
CONTEXT_EVENTS = {"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart"}
DEFAULT_CONTEXT_LIMIT = 2_500  # DEFAULT_HOOK_OUTPUT_TOKEN_LIMIT: left out of the hash
SHORT_EVENTS = {"SessionEnd", "Interrupt"}  # timeout default 1 s, at most 3 s; the rest default 600 s


# ====================================================================== the trust Codex expects
def _uint(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def hash_handler(event: str, matcher: Any, handler: dict[str, Any]) -> str | None:
    """Codex's `current_hash` for one command hook handler of `event` in a group with `matcher` (None for a handler
    Codex wouldn't run: not a command, or an empty one)."""
    if handler.get("type") != "command":
        return None  # mcp_tool, prompt and agent hooks: not trusted by hangar
    command = handler.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    timeout = handler.get("timeout")
    if event in SHORT_EVENTS:
        timeout = min(max(1 if timeout is None else timeout, 1), 3)
    else:
        timeout = max(600 if timeout is None else timeout, 1)
    cfg: dict[str, Any] = {"type": "command", "command": command, "timeout": timeout,
                           "async": bool(handler.get("async", False))}
    if handler.get("statusMessage") is not None:
        cfg["statusMessage"] = handler["statusMessage"]
    limit = handler.get("additionalContextLimit")
    if event in CONTEXT_EVENTS and limit is not None and limit != DEFAULT_CONTEXT_LIMIT:
        cfg["additionalContextLimit"] = limit
    ident: dict[str, Any] = {"event_name": EVENTS[event], "hooks": [cfg]}
    if matcher is not None and event not in NO_MATCHER:
        ident["matcher"] = matcher
    data = json.dumps(ident, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def _valid_handler(h: Any) -> bool:
    """Whether Codex parses the handler (a file with one it can't parse is skipped whole)."""
    if not isinstance(h, dict) or h.get("type") not in ("command", "mcp_tool", "prompt", "agent"):
        return False
    if h["type"] != "command":
        return True
    return (isinstance(h.get("command"), str) and (h.get("timeout") is None or _uint(h["timeout"]))
            and isinstance(h.get("async", False), bool)
            and (h.get("statusMessage") is None or isinstance(h["statusMessage"], str))
            and (h.get("additionalContextLimit") is None or _uint(h["additionalContextLimit"])))


def _events(hooks: Any) -> dict[str, list[dict[str, Any]]] | None:
    """A hooks object's events as Codex reads them, or None when it wouldn't parse."""
    if not isinstance(hooks, dict):
        return None
    out: dict[str, list[dict[str, Any]]] = {}
    for event, groups in hooks.items():
        if event not in EVENTS:
            continue  # not an event of this Codex: ignored
        if not isinstance(groups, list):
            return None
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks", []), list) \
                    or not (g.get("matcher") is None or isinstance(g["matcher"], str)) \
                    or not all(_valid_handler(h) for h in g.get("hooks", [])):
                return None
        out[event] = groups
    return out


def _hooks_file(data: bytes) -> dict[str, list[dict[str, Any]]] | None:
    """A plugin's hooks file (`{"description"?, "hooks"}`, nothing else) as Codex parses it."""
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    if not isinstance(doc, dict) or set(doc) - {"description", "hooks"}:
        return None
    return _events(doc.get("hooks", {}))


def _manifest_path(p: Any) -> str | None:
    """A manifest path as Codex resolves it: `./` relative to the plugin root, without `..`."""
    if not isinstance(p, str) or not p.startswith("./") or len(p) == 2:
        return None
    rel = p[2:]
    if rel.startswith("/") or ".." in rel.split("/"):
        return None
    return posixpath.normpath(rel)


def _load(files: dict[str, bytes]) -> tuple[list[tuple[str, dict[str, list[dict[str, Any]]]]], list[str]]:
    """([(key source path, events)], [what Codex skips]) of a Codex plugin given as {relative path: content}, as
    Codex 0.162.1 loads them (load_plugin_hooks): the manifest's `hooks` (a path, paths, an object or objects), else
    `hooks/hooks.json`. A hooks file it can't find or parse is skipped whole."""
    try:
        man = json.loads(files[MANIFEST])
    except (KeyError, ValueError):
        return [], [f"{MANIFEST} doesn't parse"]
    hooks = man.get("hooks") if isinstance(man, dict) else None
    out: list[tuple[str, dict[str, list[dict[str, Any]]]]] = []
    skipped: list[str] = []
    if hooks is None:
        paths = [DEFAULT_HOOKS] if DEFAULT_HOOKS in files else []
    elif isinstance(hooks, str) or isinstance(hooks, list) and all(isinstance(x, str) for x in hooks):
        paths = []
        for raw in [hooks] if isinstance(hooks, str) else hooks:
            if (rel := _manifest_path(raw)) is None:
                skipped.append(f"hooks path {raw!r} isn't a ./ path inside the plugin")
            else:
                paths.append(rel)
    else:
        inline = [hooks] if isinstance(hooks, dict) else hooks if isinstance(hooks, list) else []
        if not inline:
            skipped.append("the manifest's hooks are neither paths nor hooks objects")
        for i, obj in enumerate(inline):
            ev = _events(obj.get("hooks", {})) if isinstance(obj, dict) and not set(obj) - {"description",
                                                                                              "hooks"} else None
            if ev is None:
                skipped.append(f"plugin.json#hooks[{i}] doesn't parse")
            elif ev:
                out.append((f"plugin.json#hooks[{i}]", ev))
        return out, skipped
    for rel in dict.fromkeys(paths):
        if rel not in files:
            skipped.append(f"{rel} isn't in the plugin")
        elif (ev := _hooks_file(files[rel])) is None:
            skipped.append(f"{rel} doesn't parse")
        elif ev:
            out.append((rel, ev))
    return out, skipped


def hook_sources(files: dict[str, bytes]) -> list[tuple[str, dict[str, list[dict[str, Any]]]]]:
    """[(key source path, events)] of a Codex plugin given as {relative path: content}, as Codex 0.162.1 loads them."""
    return _load(files)[0]


def untrusted(files: dict[str, bytes]) -> list[str]:
    """--- KAN-72: what of one Codex plugin's hooks never runs under hangar's trust: a hooks file Codex skips (missing,
    outside the plugin, unparseable), and each handler hangar doesn't trust: an empty command, or not a command (Codex
    0.162.1 skips prompt and agent hooks as unsupported and loads mcp_tool hooks, which hangar never trusts). [] when
    every hook it declares is trusted."""
    sources, out = _load(files)
    for src, events in sources:
        for event, groups in events.items():
            for gi, g in enumerate(groups):
                for hi, h in enumerate(g.get("hooks", [])):
                    if hash_handler(event, g.get("matcher"), h) is None:
                        what = f"a {h.get('type')} hook" if h.get("type") != "command" else "an empty command"
                        out.append(f"{src}:{EVENTS[event]}:{gi}:{hi} is {what}")
    return out


def plugin_trust(plugin_id: str, files: dict[str, bytes]) -> dict[str, str]:
    """{hook state key: trusted hash} for every command hook of one Codex plugin."""
    out: dict[str, str] = {}
    for src, events in hook_sources(files):
        for event, groups in events.items():
            for gi, g in enumerate(groups):
                for hi, h in enumerate(g.get("hooks", [])):
                    if (digest := hash_handler(event, g.get("matcher"), h)) is not None:
                        out[f"{plugin_id}:{src}:{EVENTS[event]}:{gi}:{hi}"] = digest
    return out


# ====================================================================== the lock's Codex forms
def forms(d: Path, lock: dict[str, Any]) -> list[dict[str, Any]]:
    """The Codex form of each enabled plugin of the lock in directory `d`: [{id, name, marketplace, files, trust}].
    Raises ValueError when an enabled plugin has none, or the form holds a file the lock doesn't pin with its
    digest: hangar never installs or trusts what the lock doesn't pin."""
    comps = lock.get("components") or []
    pinned = {c["path"]: c.get("digest") for c in comps
              if str(c.get("id", "")).startswith("native:") and c.get("path")}
    out = []
    for c in comps:
        if not str(c.get("id", "")).startswith("plugin:") or not c.get("enabled", True):
            continue
        ref = harness_lock.plugin_ref(c["id"])
        rel = f"{FORMS}/{ref['name']}"
        root = d / rel
        if not (root / MANIFEST).is_file():
            raise ValueError(f"{c['id']} has no Codex form: {rel}/{MANIFEST} is not in the lock's directory")
        files = harness_lock.read_dir(root)
        for f, data in files.items():
            if pinned.get(f"{rel}/{f}") != harness_lock._sha(data):
                raise ValueError(f"{c['id']}: {rel}/{f} is not pinned by the lock (native:codex/{ref['name']}/{f} "
                                 "with its digest)")
        try:
            name = json.loads(files[MANIFEST]).get("name")
        except (ValueError, AttributeError):
            name = None
        if name != ref["name"]:
            raise ValueError(f"{c['id']}: {rel}/{MANIFEST} names plugin {name!r}, not {ref['name']!r}")
        pid = f"{ref['name']}@{ref['marketplace']}"
        out.append({"id": pid, "name": ref["name"], "marketplace": ref["marketplace"], "files": files,
                    "trust": plugin_trust(pid, files)})
    return out


def check(d: Path, lock: dict[str, Any]) -> str | None:
    """Why a Codex sandbox can't run the lock's plugins, or None (the claim's check)."""
    try:
        forms(d, lock)
    except ValueError as e:
        return str(e)
    return None


def trust(d: Path, lock: dict[str, Any]) -> dict[str, str]:
    """{hook state key: trusted hash} of the lock's Codex forms: exactly their hooks, nothing else."""
    out: dict[str, str] = {}
    for f in forms(d, lock):
        out.update(f["trust"])
    return out


def override(t: dict[str, str]) -> str:
    """Codex's `-c` value that trusts exactly `t`: `hooks.state={"<key>"={trusted_hash="sha256:..."},...}` (JSON
    strings are TOML basic strings)."""
    return "hooks.state={" + ",".join(f"{json.dumps(k)}={{trusted_hash={json.dumps(v)}}}"
                                      for k, v in sorted(t.items())) + "}"


def mounted_forms(fleet: Fleet, sb: Sandbox | None, harness: str | None) -> tuple[dict[str, str], list[str]]:
    """(trust, enabled plugin ids) for a Codex sandbox mounted from a lock with plugins (({}, []) otherwise). When the
    lock's directory no longer has their Codex forms the trust is {}: the hooks then stay untrusted and turn 1 says
    so."""
    from hangar import harness_thaw

    if harness != HARNESS:
        return {}, []
    got = harness_thaw.mounted(fleet, sb)
    if got is None:
        return {}, []
    ids = []
    for c in got[1].get("components") or []:
        if str(c.get("id", "")).startswith("plugin:") and c.get("enabled", True):
            ref = harness_lock.plugin_ref(c["id"])
            ids.append(f"{ref['name']}@{ref['marketplace']}")
    try:
        return trust(*got), ids
    except ValueError:
        return {}, ids


def mounted_trust(fleet: Fleet, sb: Sandbox | None, harness: str | None) -> dict[str, str]:
    """The trust for a Codex sandbox mounted from a lock with Codex forms ({} otherwise)."""
    return mounted_forms(fleet, sb, harness)[0]


def turn_env(fleet: Fleet, sb: Sandbox | None, harness: str | None) -> dict[str, str]:
    """`TRUST_ENV` for a Codex turn whose sandbox has the lock's Codex forms; {} otherwise."""
    t = mounted_trust(fleet, sb, harness)
    return {TRUST_ENV: override(t)} if t else {}


# ====================================================================== mounting
def marketplace_doc(mkt: str, names: list[str]) -> bytes:
    return (json.dumps({"name": mkt, "plugins": [{"name": n, "source": {"source": "local", "path": f"./{n}"}}
                                                 for n in names]}, indent=2) + "\n").encode()


async def mount(prov: Any, sb: Sandbox, d: Path, lock: dict[str, Any], codex: str) -> tuple[str, str] | None:
    """Stage the lock's Codex forms in the sandbox; (root shell that makes them local marketplaces under
    `MARKETPLACES`, root's and read-only; the agent user's shell that installs them with the sandbox's Codex at
    `codex`), or None without any."""
    fs = forms(d, lock)
    if not fs:
        return None
    q = shlex.quote
    stage = f"/tmp/hangar-thaw-codex-{uuid.uuid4().hex}"
    by_mkt: dict[str, list[str]] = {}
    for f in fs:
        by_mkt.setdefault(f["marketplace"], []).append(f["name"])
        for rel, data in f["files"].items():
            await prov.write_file(sb, f"{stage}/{f['marketplace']}/{f['name']}/{rel}", data, mode=0o644)
    for mkt, names in by_mkt.items():
        await prov.write_file(sb, f"{stage}/{mkt}/{MARKETPLACE}", marketplace_doc(mkt, names), mode=0o644)
    root = [f"mkdir -p {MARKETPLACES}"]
    for mkt in by_mkt:
        dst = q(f"{MARKETPLACES}/{mkt}")
        root += [f"rm -rf {dst}", f"cp -R {q(f'{stage}/{mkt}')} {dst}",
                 f'if [ "$(id -u)" = 0 ]; then chown -R 0:0 {dst}; fi', f"chmod -R a-w,a+rX {dst}"]
    root.append(f"rm -rf {q(stage)}")
    user = ['mkdir -p "${CODEX_HOME:-$HOME/.codex}"']  # Codex refuses a CODEX_HOME that doesn't exist
    user += [f"{q(codex)} plugin marketplace add {q(f'{MARKETPLACES}/{mkt}')}" for mkt in by_mkt]
    user += [f"{q(codex)} plugin add {q(f['id'])}" for f in fs]
    return "\n".join(root), "\n".join(user)


# ====================================================================== turn 1: are the hooks active?
_PROBE = r"""
import json, subprocess, sys
codex, override, cwd, want = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
plugins = json.loads(sys.argv[5]) if len(sys.argv) > 5 else []
p = subprocess.Popen([codex, "app-server", "-c", override], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True)
def call(i, method, params):
    p.stdin.write(json.dumps({"id": i, "method": method, "params": params}) + "\n"); p.stdin.flush()
    for line in p.stdout:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == i:
            return m
    return {}
try:
    call(1, "initialize", {"clientInfo": {"name": "hangar-probe", "version": "1"}})
    p.stdin.write(json.dumps({"method": "initialized"}) + "\n"); p.stdin.flush()
    res = call(2, "hooks/list", {"cwds": [cwd]}).get("result") or {}
    seen = {h.get("key"): h for e in res.get("data") or [] for h in e.get("hooks") or []}
    extra = [k for k, h in seen.items() if h.get("pluginId") in plugins and k not in want]
    for k in [*want, *sorted(extra)]:
        h = seen.get(k)
        print("TAG\t%s\t%s\t%d" % (k, h.get("trustStatus") or "unknown" if h else "missing",
                                   1 if h and h.get("enabled") else 0))
    notes = [str(m) for e in res.get("data") or [] for m in (e.get("warnings") or []) + (e.get("errors") or [])]
    for pid in plugins:
        name, _, mkt = pid.partition("@")
        if any("/plugins/cache/%s/%s/" % (mkt, name) in m for m in notes):
            print("TAG\t%s\tskipped\t0" % pid)
finally:
    p.kill()
""".replace("TAG", PROBE_TAG)


def probe_lines(t: dict[str, str], codex: str, cwd: str, keys: list[str] | None = None,
                plugins: list[str] | None = None) -> list[str]:
    """Shell for manifest.probe_script: the status of each trusted key (or of `keys`) as the sandbox's Codex reports
    it with the turn's override (`codexhook\\t<key>\\t<trusted|untrusted|modified|missing|...>\\t<enabled 1|0>`; key
    `-` when Codex couldn't be asked). With `plugins` (the lock's plugin ids, KAN-72) also every other hook Codex
    lists for them, which hangar didn't trust, and `codexhook\\t<plugin id>\\tskipped\\t0` when Codex warns that
    it skipped some of a plugin's hooks."""
    keys = sorted(t) if keys is None else keys
    plugins = sorted(plugins or [])
    if not keys and not plugins:
        return []
    q = shlex.quote
    args = " ".join(q(x) for x in (codex, override(t), cwd, json.dumps(keys), json.dumps(plugins)))
    fail = f"printf '{PROBE_TAG}\\t-\\t%s\\t0\\n'"
    return [f"if command -v python3 >/dev/null 2>&1; then timeout {PROBE_SECONDS} python3 -I -c {q(_PROBE)} {args} "
            f"|| {fail} error; else {fail} no-python3; fi"]


def parse_probe_line(parts: list[str]) -> dict[str, Any] | None:
    if len(parts) != 4 or parts[0] != PROBE_TAG:
        return None
    return {"key": parts[1][:300], "status": parts[2][:40], "enabled": parts[3].strip() == "1"}


def drift(probe: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Turn 1's drift for the lock's Codex hooks: each one Codex doesn't run (not trusted, or disabled), and each
    plugin whose hooks Codex skipped in part."""
    out = []
    for x in (probe or {}).get("codex_hooks") or []:
        if x.get("status") == "trusted" and x.get("enabled"):
            continue
        key = x.get("key")
        out.append({"component": f"codex-hook:{key}" if key and key != "-" else "codex-hooks", "lock": "trusted",
                    "effective": x.get("status") if x.get("status") != "trusted" else "disabled"})
    return out


# ====================================================================== hangar doctor (KAN-72)
def doctor_checks(h: Any) -> list[dict[str, Any]]:
    """`codex plugin hooks`: for each Codex harness in the team harness repo (its current revision) whose lock enables
    plugins, whether a sandbox would run every hook of their Codex forms. Fails for a form a claim refuses (missing,
    or a file the lock doesn't pin) and for a hook that would stay untrusted (`untrusted`). [] without such a lock."""
    from hangar import harness_repo, harness_thaw
    from hangar.doctor import _check

    s = h.settings
    if not getattr(s, "harness_repo", None):
        return []
    try:
        reg = harness_thaw.registry(s)
    except harness_thaw.ThawError:
        return []  # the `harness repo` check says why
    bad: list[str] = []
    locks = plugins = hooks = 0
    for name in harness_repo.names(reg):
        try:
            r = harness_thaw.resolve(s, name)
        except harness_thaw.ThawError:
            continue  # `harness channels` reports it
        lock = r.lock
        if lock["runtime"].get("harness") != HARNESS or not any(
                str(c.get("id", "")).startswith("plugin:") and c.get("enabled", True)
                for c in lock.get("components") or []):
            continue
        locks += 1
        try:
            fs = forms(reg / Path(r.path).parent, lock)
        except ValueError as e:
            bad.append(f"{name}: {e}")
            continue
        for f in fs:
            plugins += 1
            hooks += len(f["trust"])
            bad += [f"{name} {f['id']}: {why}" for why in untrusted(f["files"])]
    if not locks:
        return []
    detail = (f"{locks} Codex harness(es), {plugins} plugin(s), {hooks} hook(s) trusted by hash" if not bad else
              "; ".join(bad[:6]) + (f" and {len(bad) - 6} more" if len(bad) > 6 else ""))
    return [_check("codex plugin hooks", not bad, detail,
                   "fix the plugin's Codex form (command hooks only, in a hooks file its manifest names), then "
                   "re-freeze the harness and check it (`hangar harness check`; docs/harness-lock.md#codex-plugins)")]
