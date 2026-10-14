"""Hangar-owned harness settings for split turns that ask (workstream harden; docs/steering.md).

The uid split (isolation.py) keeps the driver, its key and the answers out of the agent's reach, but the harness is
still started from what the agent owns: its binary through the agent's PATH, its environment from ~/.hangar/env, and
its settings from ~/.claude, the repository's .claude/ and ~/.codex. Code a person allowed to run could change any of
them so that a later turn's tool calls are never asked about (a Claude Code permission rule or PreToolUse hook that
allows them, a Codex hook) or the verify gate's Stop hook is off. For an `ask` or `deny-risky` turn in a split
sandbox, hangar decides all of that itself:

* **the binary**: started by its path in the harness's pack or baked prefix, which the setup checked the agent can't
  change (isolation.SETUP), nor the managed settings that outrank hangar's own (/etc/claude-code, /etc/codex);
* **the environment**: hangar's own copy of ~/.hangar/env as hangar wrote it when it set the sandbox up (kept in its
  vault), handed to the driver in a 0600 file it reads and deletes before the harness starts (driver --base-env);
* **Claude Code's settings** (verified with 2.1.296 against a scripted model API, tests/test_harden_real_clis.py):
  `--setting-sources ''` loads no user, project or local settings, so none of their permission rules, hooks,
  `defaultMode` or project MCP servers; `--settings` is the environment's own ~/.claude/settings.json from its spec
  with the job's verify gate added; `--strict-mcp-config --mcp-config` the environment's MCP servers as rendered when
  the sandbox was set up. Without setting sources Claude Code also skips CLAUDE.md files and skills: `--add-dir` with
  CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1 brings back the repository's CLAUDE.md and the environment's
  instructions. Skills stay off: their `allowed-tools` are permission rules the agent could write. So do plugins,
  even ones the environment enables: Claude Code finds them through ~/.claude/plugins, the agent's.
* **Codex** (0.162.1): `--disable hooks` (a command-line override outranks config.toml) and `approvalsReviewer: user`
  on the thread (the driver). Codex still loads `.rules` files from ~/.codex/rules and the repository's .codex/rules,
  even for an untrusted project, and has no switch to turn them off: a rule can let a command run without asking.
  So can an MCP server the agent adds to ~/.codex/config.toml with its tools set to `approve`. The turn says so.

Managed settings outrank all of this (verified: a managed `permissions.allow` or `disableAllHooks` wins over
`--setting-sources ''` and `--settings`). They are root's: without sudo, which the split requires, the agent can't write
them, and hangar doesn't write any of its own (they would replace the image's).
"""

from __future__ import annotations

import contextlib
import json
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hangar import isolation
from hangar.envspec import _resolve_home_path
from hangar.harnesses import mcp_config
from hangar.verifyloop import CLAUDE_ENV

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.harnesses import Harness, McpServer
    from hangar.models import Sandbox
    from hangar.providers.base import Provider

VAULT = "harness:"  # + sandbox id: what hangar set the harness up with (its env, its MCP servers)
CODEX_RULES = ("Codex reads command rules from ~/.codex/rules and the repository's .codex/rules, and MCP servers from "
               "~/.codex/config.toml, all of which the agent can write, and has no switch to turn them off: a rule, or "
               "an MCP server whose tools are set to `approve`, can let a call run without asking "
               "(docs/steering.md#hangar-owned-harness-settings)")
# review(#69): Claude Code loads an enabled plugin (its hooks, skills, MCP servers) from what ~/.claude/plugins says,
# which the agent can write; verified with 2.1.296, a plugin's PreToolUse hook then allows every call
PLUGIN_KEYS = ("enabledPlugins", "extraKnownMarketplaces")
CLAUDE_PLUGINS = ("the environment's Claude Code plugins don't load in a turn that asks: Claude Code reads them from "
                  "~/.claude/plugins, which the agent can write (docs/steering.md#hangar-owned-harness-settings)")


@dataclass
class Plan:
    """How a hardened turn starts its harness: `live_run` replaces the harness's own, `driver_args` go to the driver,
    `files` are written into isolation.RUNS as hangar's user first."""

    live_run: str
    driver_args: str
    files: dict[str, tuple[bytes | None, int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def keep(fleet: Any, prov: Provider, sb: Sandbox, h: Harness, servers: dict[str, McpServer], env: dict[str, str],
         prelude: list[str]) -> None:
    """When a sandbox is set up for a harness with a structured mode: keep hangar's own copy of the environment it
    writes to ~/.hangar/env (its PATH lines and exports, as broker.inject writes them) and of the MCP servers it
    configured. Built from what hangar wrote, never read back: a fork starts from its parent's files, and the parent's
    agent may have left something there that runs when hangar sets the fork up (its ~/.bashrc, for one). The
    credentials' post-inject scripts of the harnesses with a structured mode add nothing to that file."""
    if not h.live or not isolation.supported(prov) or fleet.settings.uid_split == "off":
        return  # no split there: nothing would use it
    text = "\n".join([*prelude, *(f"export {k}={shlex.quote(v)}" for k, v in env.items())]) + "\n"
    mcp = mcp_config("claude", servers)[1] if h.mcp_format == "claude" else None
    fleet.broker.vault.put(VAULT + sb.id, json.dumps({"env": text, "claude_mcp": mcp}))


def forget(fleet: Any, sandbox_id: str) -> None:
    with contextlib.suppress(Exception):
        fleet.broker.vault.delete(VAULT + sandbox_id)


def claude_settings(spec: EnvSpec, user: str, gate: str | None) -> dict[str, Any]:
    """Hangar's `--settings`: the environment's own ~/.claude/settings.json from its spec (what it allows, its hooks),
    with the job's verify gate (verifyloop.claude_settings) added. Only one `--settings` counts (the last, verified
    with 2.1.296), so the two are merged here."""
    target = _resolve_home_path(".claude/settings.json", user)
    out: dict[str, Any] = {}
    for rel, content in {**spec.agent_config.files, **spec.files}.items():
        if _resolve_home_path(rel, user) == target:
            with contextlib.suppress(ValueError):
                loaded = json.loads(content)
                out = loaded if isinstance(loaded, dict) else {}
    if gate:
        extra = json.loads(gate)
        hooks = dict(out.get("hooks") or {})
        for event, groups in (extra.pop("hooks", None) or {}).items():
            hooks[event] = [*(hooks.get(event) or []), *groups]
        out = {**out, **extra, "hooks": hooks}
    return out


def claude_flags(run: str, wd: str, memory: bool) -> list[str]:
    """Claude Code's arguments for a hardened turn; `run` is the run's path prefix in isolation.RUNS."""
    flags = ["--setting-sources", "", "--settings", f"{run}.settings.json", "--strict-mcp-config",
             "--mcp-config", f"{run}.mcp.json", "--add-dir", wd]
    return flags + (["--add-dir", f"{run}.memory"] if memory else [])


CODEX_FLAGS = ["--disable", "hooks"]  # before the subcommand; `-c features.hooks=false`


def plan(fleet: Any, sb: Sandbox, h: Harness, run_id: str, wd: str, env: dict[str, str]) -> Plan | str:
    """The hardened start of a split turn's harness, or why hangar can't own its settings in this sandbox."""
    binary = isolation.pinned(sb)
    if not binary:
        return (sb.info.get("isolation") or {}).get("harness_why") or "the harness wasn't checked"
    if binary != isolation.live_bin(h):
        return f"the sandbox was set up for another harness ({binary})"
    raw = fleet.broker.vault.get(VAULT + sb.id)
    if raw is None:
        return ("it was set up before hangar kept its own copy of the harness's environment; a new sandbox has it "
                "(suspend and resume the session)")
    kept = json.loads(raw)
    r = f"{isolation.RUNS}/{run_id}"
    base = kept.get("env") or ""
    rest = h.live_run.split(None, 1)[1] if len(h.live_run.split(None, 1)) > 1 else ""  # shell words, expanded later
    p = Plan(live_run="", driver_args=f"--base-env {shlex.quote(r + '.agent-env')}")
    if h.live == "claude":
        try:
            spec = fleet.catalog.get(sb.environment)
        except (KeyError, ValueError):
            return f"its environment {sb.environment} is no longer defined"
        settings = claude_settings(spec, fleet.settings.sandbox_user, env.get(CLAUDE_ENV))
        if [k for k in PLUGIN_KEYS if settings.pop(k, None)]:  # review(#69)
            p.warnings.append(CLAUDE_PLUGINS)
        p.files[f"{run_id}.settings.json"] = (json.dumps(settings).encode(), 0o644)
        p.files[f"{run_id}.mcp.json"] = (json.dumps(kept.get("claude_mcp") or {"mcpServers": {}}).encode(), 0o644)
        memory = bool(spec.agent_config.instructions)
        if memory:  # the environment's CLAUDE.md, from its spec rather than the agent's copy
            p.files[f"{run_id}.memory"] = (None, 0o755)
            p.files[f"{run_id}.memory/CLAUDE.md"] = (spec.agent_config.instructions.encode(), 0o644)
        base += "\nexport CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1\n"
        p.live_run = f"{shlex.quote(binary)} {rest} {shlex.join(claude_flags(r, wd, memory))}"
    else:
        p.live_run = f"{shlex.quote(binary)} {shlex.join(CODEX_FLAGS)} {rest}"
        p.warnings.append(CODEX_RULES)
    p.files[f"{run_id}.agent-env"] = (base.encode(), 0o600)
    return p
