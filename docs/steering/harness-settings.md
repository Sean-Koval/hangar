# hangar-owned harness settings

This page is part of [Steering a running agent](../steering.md).

The split keeps the driver, its key and your answers out of the agent's reach. The harness itself used to start from what the agent owns:

- its binary, found through the agent's `PATH`;
- its environment, from `~/.hangar/env`;
- its settings, from `~/.claude`, the repository's `.claude/` and `~/.codex`.

So code you allowed could set up a later turn so that it never asks. It could add a Claude Code permission rule or a `PreToolUse` hook that allows every call, or a Codex hook. It could also switch off the verify gate's Stop hook. For an `ask` or `deny-risky` turn in a split sandbox, hangar now decides all of these itself (`src/hangar/harden.py`):

- **The binary.** The harness starts by its path in its pack or baked prefix (`/opt/hangar/harness/<name>/bin`). The setup checked that the agent can't change it, any directory above it, or anything in the prefix. A `claude` the agent puts first on its `PATH` never runs. Codex's wrapper script used to find its prefix with `dirname` and `readlink`, which it looked up on the agent's `PATH`. Codex's pack now has no wrapper: `bin/codex` links to the vendor's binary, and hangar puts the bundled `rg` on `PATH` at claim time. pi's launcher names its prefix. The setup doesn't trust a wrapper from an older pack (a new sandbox has the fixed one).
- **The environment.** When hangar sets up a sandbox, it keeps its own copy of the `~/.hangar/env` it writes there, in its vault, before anything of the agent's has run. For each turn it writes that copy to a 0600 file. The driver reads the file and deletes it before the harness starts, then hands it to the harness on a pipe. What the agent adds to `~/.hangar/env` (`LD_PRELOAD`, `PATH`, settings paths) never reaches the harness. Nothing on the agent's `PATH` runs before the harness does.
- **Claude Code's settings** (verified with 2.1.296 and a scripted model API; `tests/test_harden_real_clis.py`):
  - **`--setting-sources ''`.** No user, project or local settings load. That covers their permission rules, hooks, `defaultMode`, `disableAllHooks` and `.mcp.json` servers. The legacy `allowedTools` in `~/.claude.json` doesn't apply either way.
  - **`--settings`** is a file of hangar's: the environment's own `~/.claude/settings.json` from its spec (`agent_config.files` or `files`), with the job's verify gate added. The two are merged into one file because only the last `--settings` counts.
  - **`--strict-mcp-config --mcp-config`** loads the environment's MCP servers (`mcp:`), as hangar rendered them when it set up the sandbox.
  - **CLAUDE.md.** Without setting sources Claude Code also skips CLAUDE.md files and skills. `--add-dir` with `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` brings back the repository's CLAUDE.md and the environment's `instructions`.
- **Codex** (0.162.1):
  - `--disable hooks`: a command-line override outranks `config.toml`.
  - `approvalsReviewer: "user"` on the thread, next to `approvalPolicy`. An `approvals_reviewer = "auto_review"` in the agent's config doesn't route the turn's approvals elsewhere.

**What still applies, and what doesn't.**

- **Still applied:** the environment's settings, which are its allowed tools, permission rules and hooks; its MCP servers; its instructions; its seed plugins (below); and the repository's CLAUDE.md.
- **Not loaded in these turns:** the repository's `.claude/settings.json` and `.claude/settings.local.json`, and skills. The agent can write all of them. A skill's `allowed-tools` is a permission rule, so a skill could pre-approve calls. Put what should apply in the environment's `agent_config.files`, and skills it should keep in a plugin in its seed.
- **Plugins load only from the environment's read-only seed** ([harnesses.md](../harnesses.md#plugins)). Claude Code otherwise finds an enabled plugin through `~/.claude/plugins` (`installed_plugins.json`, `known_marketplaces.json`), which the agent can write. Verified with 2.1.296: a plugin whose `PreToolUse` hook allows every call let the call through. So hangar's `--settings` enable exactly the plugins the environment declares under `plugins:`, which the image carries root's under `/opt/hangar/claude-seed`, and `CLAUDE_CODE_PLUGIN_CACHE_DIR` points at a new, unguessable directory each turn, so `~/.claude/plugins` is never read and nothing the agent left behind is in it. A seed marketplace also outranks a same-named one the agent registers. `enabledPlugins` entries for anything else, and `extraKnownMarketplaces`, are dropped from the environment's settings, and the turn says so.
- **The environment's hooks run as the agent.** They see the agent's `PATH`, which starts with `~/.hangar/bin`, and its files. A hook that decides permissions (`PreToolUse`, `PermissionRequest`) is only as strong as what it runs: name programs by absolute path in places the agent can't write, with no `#!/usr/bin/env` lookups, and no scripts from the repository or the home directory.
- **Turns that don't ask** (`auto`) start their harness as before. In a split sandbox their harness also runs with `no_new_privs`, so setuid and file-capability programs don't gain privileges there either.

**Managed settings outrank all of this.** Verified: a managed `permissions.allow`, `disableAllHooks` or `allowManagedHooksOnly` wins over `--setting-sources ''` and `--settings`. They are root's, and the split requires an agent without sudo, so the agent can't write them. The setup checks that. hangar doesn't write managed settings of its own: they would replace the image's, and they aren't needed for this.

**Where hangar can't own them, the turn says so.** This happens when the harness isn't in a prefix the agent can't change, for example a prebuilt `image:` with the CLI in `~/.local/bin`. It also happens when the managed settings are writable, or when the sandbox was set up before this change: a new sandbox fixes that, so suspend and resume the session. The turn carries a warning, or fails under `HANGAR_UID_SPLIT=required`.

**What stays open.**

- **Codex command rules.** Codex loads `.rules` files from `~/.codex/rules` and from the repository's `.codex/rules`, even for an untrusted project. A `prefix_rule(..., decision="allow")` lets a command run without asking, and codex 0.162 has no switch for the app server to turn rules off. A turn that asks won't start with one (KAN-169); mid-turn ones aren't checked. `tests/test_harden_real_clis.py::test_codex_rules_still_apply` will fail once Codex gains a switch.
- **The rest of the agent's Codex config still loads**, from `~/.codex/config.toml` and a trusted project's `.codex/config.toml`. That covers the model provider and features other than hooks. The approval policy, reviewer and sandbox are the thread's.
- **Codex MCP servers.** They are part of that config too. Verified with 0.162.1: a server the agent adds with `default_tools_approval_mode = "approve"` runs its tool without asking (`test_codex_mcp_servers_the_agent_adds_still_apply`). Without that key, Codex asks through an MCP elicitation, which the driver declines, so the tool doesn't run. Such servers stop turns starting. hangar can't pass the environment's servers on the command line instead: they carry its secrets.
- **The image's own environment** (its `ENV`) still reaches the harness. It is the operator's.

**Root's commands on Docker.** Docker starts an exec with umask 0000 (verified with Docker 29.8). A directory that root or `hangar` created there without an explicit mode was writable by everyone, so the agent could change it. hangar's commands as those users now start with umask 022.

`hangar-envd` stays the agent's. It runs as the sandbox user and serves everything hangar does as that user; what runs as `hangar` (starting and stopping a turn, its commands, the view of its terminal) goes through `docker exec`. A daemon able to switch users would be the most exposed process in the sandbox and the most privileged one, and Kubernetes couldn't run it anyway.
