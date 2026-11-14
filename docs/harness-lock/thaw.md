# Thaw and drift

This page is part of [Harness locks](../harness-lock.md).

A session or job can run *from* a lock: `hangar harness thaw team-default@r14 --env python "..."`, or
`harness_lock: "team-default@r14"` on `POST /api/jobs`, `POST /api/sessions` or MCP `submit_job`. An experiment variant
runs from one too: `{harness_lock: "team-default@sha256:…"}`, by digest ([jobs/experiments.md](../jobs/experiments.md), #135).

**The registry.** `HANGAR_HARNESS_REPO` is the [team harness repo](../harness-lock.md#the-team-harness-repo): locks at
`harnesses/<name>/harness.lock` (or `<name>/harness.lock`), channels in `channels.yaml`
(`{team-default: {stable: "sha256:..."}}`). A reference is `name` (the current lock), `name@rN`, `name@sha256:...` or
`name@<channel>`; another revision or digest comes from the harness's release copies (`releases/rN/`), never git
history. A harness without them falls back to history. The registry may sit inside a repo: paths and
history lookups are relative to it.
`GET /api/harness-locks/{ref}` shows what a reference resolves to.

**The claim** is refused, with what to do, when:

- the lock's `runtime.version` isn't the CLI version `harnesses.lock.yaml` pins ("re-freeze the harness from a turn
  on 2.1.296, or bump the lock's runtime.version");
- the environment can't run the lock's harness, or doesn't define an MCP server the lock binds (endpoints and
  credentials live in the environment);
- the request names another `harness`, `model` or `approvals` than the lock's;
- the lock has plugins and isn't a Claude Code or Codex harness, or is a Codex harness and an enabled plugin has no
  [Codex form](#codex-plugins) the lock pins;
- under `HANGAR_HARNESS_DRIFT=fail`, the registry's files differ from the lock (edited without re-freezing).

Otherwise the session (a job's too) is pinned to the lock's harness, concrete model and approval mode, and records
the lock (`harness_lock` on the session and job). The lock's directory is copied out of the registry into
`<data>/harness-locks/<digest>-<content>/`: written once and never changed, so a claimed lock keeps its files whatever
happens to the registry later (a session that resumes days later mounts the same files).

**The mount** (#221). The sandbox runs the lock's files, not the image's:

- **Instructions:** the lock's guide is written to the harness's global instructions file (removed when the lock has
  none); hangar then splices its environment notes into it, as for every turn, and the file becomes root's and
  read-only to the agent.
- **Skills:** exactly the lock's (the image's others are removed; hangar's own `hangar-environment` and `verify-ui`
  stay), each directory root's and read-only to the agent. A reference (a pinned git skill) stays the image's copy.
- **Plugins** (Claude Code): the lock's marketplaces replace the image's seed at `/opt/hangar/claude-seed`, installed
  there with the sandbox's own Claude Code as the image build installs them (`claude plugin marketplace add` /
  `install`), root's and read-only. `CLAUDE_CODE_PLUGIN_SEED_DIR` and hangar's `--settings` (`enabledPlugins`) then
  name the lock's plugins, so an environment that declares no plugins still runs them.
- **Plugins** (Codex): each enabled plugin's [Codex form](#codex-plugins), installed by the sandbox's Codex.

Everything that reads the environment's prefix layer (the manifest's declared skills, instructions and plugins, the
probe, hangar's Claude Code settings, a hardened turn's settings) reads the lock's instead; the binding layer (MCP
servers, hooks, approvals) and the environment's identity (`environment.spec_digest`, `environment_digest`) stay the
environment's. The agent's sudo (an environment's `sudo: true`) can still change root's files: read-only means the
agent's own user can't.

**Turn 1** is compared with the lock once its manifest is recorded:

| Compared | Lock | Turn 1 |
| --- | --- | --- |
| each skill | its `tree` digest | the sandbox's effective skill digest (`skills.effective`); a skill the sandbox has and the lock doesn't is drift too, except hangar's own (`hangar-environment`, `verify-ui`) |
| each plugin | its `digest`, `enabled` and `tree` | the manifest's declared plugin (read from the mounted lock) and what the probe found in the seed (`probe.plugins`) |
| each Codex plugin hook | trusted and enabled | what the sandbox's Codex reports for it (`probe.codex_hooks`; drift `codex-hook:<key>`, or `codex-hooks` when Codex couldn't be asked). Hooks of the plugins outside hangar's trust, and hooks Codex skipped, are drift too ([Codex plugin hooks](../codex-plugin-hooks.md#in-a-sandbox-turn-1)) |
| the instructions file | the `guide:` digest | the declared one (`instructions.global.declared.digest`: hangar splices its environment notes into the sandbox's copy, so that copy isn't compared byte for byte) |
| the CLI | `runtime.version` | the version the sandbox's CLI reported |
| everything else | its `harness_digest` | the manifest's `harness_digest` (model, MCP servers, hooks, approvals) |

The manifest carries `harness.lock: {name, revision, digest}`. Its `harness_digest` is still computed from the
turn's own sections: equal to the lock's when nothing drifted, else what actually ran. Since the files are mounted
from the lock, drift means the lock's own files were stale (the registry edited without re-freezing: the claim
records what `harness check` found in `harness_lock.stale`, and turn 1 names each component and `harness_digest`), or
the sandbox changed them between the mount and turn 1, or a binding (an MCP server, a hook) differs.

**Drift** is recorded as `SessionTurn.drift: [{component, lock, effective}]` (`[]`: matched; `null`: not checked),
a `harness.drift` event in the session's events (`data.drift`, `data.action`), a warning on the turn, and a line in
the job's review packet (`**Harness:** team-default@r14 …: **drift** on turn 1 (skill:review)`). Under
`HANGAR_HARNESS_DRIFT=fail` turn 1 stops: outcome `failed`, reason `harness_drift`; a job fails with that reason and
delivers nothing. (A trial against a [pin set](../api/trials.md) is different: one whose turn 1 drifted from its pins ends
as `outcome_class: infra_error`, `infra_reason: drift`, whatever `HANGAR_HARNESS_DRIFT` says.) The default, `warn`, carries on. A turn that ends
before its check has run waits for it (up to 40 s) under `fail`. Only turn 1 is checked: what the agent changes later
is its behaviour, not drift. Writable state (`~/.claude.json`, plugin caches) is never digested.

## Codex plugins

A `plugin:<name>@<marketplace>` component is a Claude Code plugin. For a Codex harness the same lock carries the
plugin's **Codex form**: the directory `native/codex/<name>/`, a Codex local plugin (`.codex-plugin/plugin.json`
naming `<name>`, its skills and hooks), every file of it pinned as a `native:codex/<name>/...` component. ai-dlc's
workflow plugin ships one (`plugins/ai-dlc-workflow/native/codex/` there; its publish script copies it to
`native/codex/ai-dlc-workflow/`). A Codex claim refuses an enabled plugin without one, a form file the lock doesn't
pin with its digest, and a form whose manifest names another plugin. Disabled plugins load nowhere.

The mount stages each form as a local marketplace at `/opt/hangar/codex-marketplaces/<marketplace>/` (root's,
read-only) and the sandbox's Codex installs it from there as the agent (`codex plugin marketplace add`, `codex plugin
add <name>@<marketplace>`), so the plugin keeps the lock's id.

**Hook trust.** Codex (0.162.1, the version `harnesses.lock.yaml` pins) runs a plugin's hooks only once they are
trusted, and skips untrusted ones silently: installing a plugin doesn't trust its hooks. Trust is per hook,
`hooks.state."<key>".trusted_hash` in the user config or a `-c` flag, where the key is
`<name>@<marketplace>:<hooks file>:<event>:<group>:<handler>` and the hash is Codex's digest of the hook's event,
matcher, command, timeout and options (verified against Codex's source at `rust-v0.162.1` and the CLI's own
`hooks/list`). hangar computes it for exactly the hooks of the lock's Codex forms, from the lock's files, and passes it
to every Codex turn of the sandbox as one `-c hooks.state={...}` (`HANGAR_CODEX_HOOK_TRUST`, in the harness's `run` and
`live_run`). Nothing is written to the agent's config, so no other hook is trusted by hangar, and a hook edited in the
agent's plugin cache no longer matches its hash: Codex reports it `modified` and skips it. A hardened turn starts Codex
with hooks off (`--disable hooks`), plugin hooks included.

Turn 1's probe asks the sandbox's Codex (its app server's `hooks/list`, with the same override; needs `python3` in the
sandbox) for each of those hooks. One that isn't `trusted` and enabled is drift: `codex-hook:<key>` with Codex's status
(`untrusted`, `modified`, `missing`, `disabled`), or `codex-hooks` when Codex couldn't be asked.

The Codex form is pinned but isn't identity: `harness_digest` covers the plugin's Claude Code form (its `digest`,
`enabled`), so a change to the Codex form alone changes its `native:` digests, not the digest.

**In the PR.** A job's PR body and the commit hangar lands end with `Harness-Digest:` (and `Harness-Lock:
team-default@r14` for a thawed job) as git trailers, and its review packet shows the short digest and the lock
([jobs/working-with-the-agent.md](../jobs/working-with-the-agent.md#the-pull-request), #137).

**Upstream drift.** `hangar harness outdated NAME` (`GET /api/harness-locks/{ref}/outdated`) reports a lock's CLI
version against `harnesses.lock.yaml`, and each `path:` skill against its source in the environments that declare
it. It changes nothing; exit 1 when something is outdated.

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_HARNESS_REPO` | unset | The harness registry: a local checkout, or a git URL with an optional `#ref` ([the team harness repo](../harness-lock.md#the-team-harness-repo)) |
| `HANGAR_HARNESS_REPO_REFRESH_SECONDS` | `600` | How often hangar fetches a URL registry (also on a GitHub `push` webhook) |
| `HANGAR_DEFAULT_HARNESS` | unset | The lock intake jobs run from when they name none, e.g. `team-default@stable` |
| `HANGAR_HARNESS_DRIFT` | `warn` | What turn 1's drift from its lock does: `warn` or `fail` |
