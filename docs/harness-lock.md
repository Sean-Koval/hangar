# Harness locks

A harness is a git directory of the harness's own native files plus a small `harness.lock` that pins them. hangar
writes one from a turn that went well (`hangar harness freeze`), and CI in the team's harness repo checks it
(`hangar harness check`). There is no harness spec language and no compiler: the files are what the CLI reads, and
the lock is a frozen observation of a turn's [manifest](specs/0014-sister-tool-interfaces.md#2-the-effective-harness-manifest-hangarmanifestv1).

| | |
| --- | --- |
| Format | `hangar.harness-lock/v1`, YAML on disk; schema [docs/api/harness-lock.schema.json](api/harness-lock.schema.json) |
| Identity | `harness_digest`: equal to the `harness_digest` of the turn it was frozen from |
| Write one | `hangar harness freeze <session or job> [--turn N] --out DIR [--name NAME]` |
| Check one | `hangar harness check DIR` (or `hangar harness freeze --check DIR`); exit 1 on a mismatch |
| REST | `GET /api/sessions/{name}/harness?turn=N&name=NAME`, `GET /api/jobs/{id}/harness?turn=N&name=NAME` |
| Run from one | `hangar harness thaw NAME@rN --env ENV [--session NAME]`; `harness_lock` on a job or session ([below](#thaw-and-drift)) |
| Upstream drift | `hangar harness outdated NAME` (report only) |

## A harness directory

```
team-default/
  harness.lock
  CLAUDE.md                        # the harness's global instructions file (AGENTS.md for Codex)
  skills/review/SKILL.md           # one directory per skill
  plugins/team/                    # a Claude Code directory marketplace (#221)
    .claude-plugin/marketplace.json  # lists exactly the lock's plugins of this marketplace
    workflow/...                   # one directory per plugin, at its marketplace entry's source
  native/claude-code/settings.json # config fragments under the harness's own directory (~/.claude, ~/.codex)
```

```yaml
schema: hangar.harness-lock/v1
name: team-default
revision: 14                    # monotonic; freeze bumps it when anything but the name changes
runtime: {harness: claude-code, version: 2.1.296, pack: "reg/hangar-pack-claude-code:..."}  # harnesses.lock.yaml
model: {id: claude-opus-5-5, policy: balanced}   # only the concrete id is identity
components:
  - {id: "guide:CLAUDE.md", digest: "sha256:...", path: CLAUDE.md}
  - {id: "skill:review", digest: "sha256:...", tree: "sha256:...", path: skills/review, source: "path:skills/review"}
  - {id: "skill:verify-ui", digest: null, source: "hangar:computer"}          # a reference: no files
  - {id: "plugin:workflow@team", digest: "sha256:...", tree: "sha256:...", path: plugins/team/workflow, enabled: true}
  - {id: "native:claude-code/settings.json", digest: "sha256:...", path: native/claude-code/settings.json}
binding:                        # rendered at claim; names, never values
  mcp: [{name: github, transport: http, endpoint: "https://api.githubcopilot.com/mcp/", header_names: [Authorization]}]
  hooks: [{event: Stop, source: "hangar:verify_gate"}]
  permissions: {approvals: deny-risky}
plugins: {autoUpdate: false}    # always false
remote: []                      # vendor-loop agents: [{id, version}]
```

- **Components** are listed in order: the guide, the skills in the order the turn declared them, the plugins in the
  order the environment declared them, then native fragments. A file's `digest` is the SHA-256 of its bytes; a directory's is the manifest's declared skill digest
  (for each file in path order: its relative path, a NUL, the raw SHA-256 of its content). A skill also carries
  `tree`, the digest the sandbox probe reports for the same files (the manifest's `skills.effective`), so a turn's
  effective skills can be compared with the lock without the source. A component with `digest: null` is a reference
  (a pinned git skill, hangar's `verify-ui`): it has a `source` and no files.
- **Plugins** (#221) are Claude Code seed plugins ([harnesses.md](harnesses.md#plugins)): `plugin:<name>@<marketplace>`,
  the plugin's directory at `plugins/<marketplace>/<its entry's source>`, and its marketplace entry in
  `plugins/<marketplace>/.claude-plugin/marketplace.json`, so `plugins/<marketplace>` is a directory marketplace
  Claude Code installs from. `digest` is the manifest's (the entry as canonical JSON, a newline, the directory's
  digest), `tree` what the probe reports for the directory, `enabled` whether hangar's settings enable it (an
  installed, disabled plugin is pinned too). Only a plugin with a relative (`./`) source can be frozen: one its
  marketplace fetches from elsewhere at build has no files to pin.
- **The binding layer** holds names only, as the manifest does: MCP endpoints without query or user info, header and
  env variable *names*, hook sources, the approval mode. Credentials stay in the environment.
- **Not in a lock:** the task repo's own `AGENTS.md` / `CLAUDE.md` (task context; the manifest keeps it under
  `instructions.repo`), knowledge snapshot ids (manifest and pin set), egress, credentials and approvals policy (the
  environment and governance).

## Identity

`harness_digest` is the lock projected onto the manifest's harness sections and digested with
[spec 0014 §2](specs/0014-sister-tool-interfaces.md#2-the-effective-harness-manifest-hangarmanifestv1)'s canonical
form (`harness_lock.identity`, `manifest.section_digest`):

| Manifest section | From the lock |
| --- | --- |
| `harness.name`, `harness.locked` | `runtime.harness`, `runtime.version` |
| `model.id` | `model.id` |
| `instructions.global.declared.digest` | the `guide:` component's digest (null without one) |
| `skills.declared[*].{name,digest}` | the `skill:` components, in order |
| `plugins[*].{name,marketplace,digest,enabled}` | the `plugin:` components, in order (null without any) |
| `mcp_servers[*].{name,transport,url,headers,env,command,args}` | `binding.mcp` (`endpoint`, `header_names`, `env_names`) |
| `hooks[*].{event,source,digest}` | `binding.hooks` |
| `permissions.approvals` | `binding.permissions.approvals` |

So a lock frozen from a turn has that turn's `harness_digest`, and a turn that runs from the lock reports it again.
The name, revision, paths, provenance (`source`, `pack`), the model policy and native fragments that carry no hooks
are not identity: renaming a harness or moving a file doesn't change it. `hangar harness check` prints it.

## Freezing a turn

```sh
uv run hangar harness freeze job-1a2b3c4d --turn 1 --out ../team-harnesses/team-default --name team-default
uv run hangar harness check ../team-harnesses/team-default
```

Freeze reads the turn's stored manifest and recovers each piece from where the manifest says it came from, then
checks it hashes to what the turn declared:

- the instructions file from the manifest's declared text, or, when that was truncated (over 16,000 characters) or
  redacted, from the experiment overlay or the environment, if either still has the declared digest;
- each skill from an experiment trial's stored files (the experiment read them when it started), else the
  environment's skill path;
- each plugin from the environment's marketplace directory (its entry and its directory must still have the turn's
  digest); a seed whose copy differed at turn start (an image built before the plugin changed) is a warning: the
  lock pins the declared files;
- native fragments from the environment's config files under the harness's own directory (`agent_config.files` and
  `files`); a settings file's hooks digest must still match the turn's. Claude Code and Codex are supported; pi and
  Antigravity are frozen best effort, with a warning.

It **fails, naming each gap**, when content can't be recovered:

- a skill whose copy in the sandbox at turn start differed from its declared source (changed in the sandbox, for
  example by the agent in an earlier turn): freeze a turn from before the change;
- instructions truncated or redacted in the manifest with no source left that has the declared digest;
- a source edited or removed since the turn, an unpinned git skill, a plugin its marketplace fetches from elsewhere;
- any file the broker's redaction would change: it holds a stored secret, and a secret never goes into a harness
  repo.

Skills in the sandbox that the turn didn't declare (hangar's own `hangar-environment`, or one added there) are not
frozen, with a warning. Freezing into a directory that already has a lock keeps its name (unless `--name` is given),
replaces only the paths the old and new lock manage, and keeps the revision when nothing changed or bumps it by one.
Before it writes, it checks that directory against its own lock (as `hangar harness check` does): a file edited by
hand, a missing component or a skill directory the lock doesn't pin would be lost, so freeze refuses, listing them,
until you commit or revert them; `--force` overwrites anyway, with a warning per change (#251).
Freeze writes files only: it never commits; open the PR to the harness repo yourself.

A member can freeze only their own sessions and jobs; an admin, anyone's; a `jobs` token, the jobs it submitted. A
turn they can't see answers as missing.

## Checking a directory

`hangar harness check DIR` validates the lock and recomputes every component's digest (and each skill's and plugin's
`tree`). It also fails on what the lock doesn't pin, so nothing ships unpinned: a directory under `skills/`, a file
under `native/`, a marketplace under `plugins/` none of the lock's plugins comes from, a directory in one that isn't a
locked plugin's, or a marketplace entry for a plugin the lock doesn't have. Other files in the directory (a README, CI config) are ignored. Use it as the harness repo's
CI check (#138).

## Thaw and drift

A session or job can run *from* a lock: `hangar harness thaw team-default@r14 --env python "..."`, or
`harness_lock: "team-default@r14"` on `POST /api/jobs`, `POST /api/sessions` or MCP `submit_job`. An experiment variant
runs from one too: `{harness_lock: "team-default@sha256:…"}`, by digest ([jobs.md](jobs.md#9-experiments), #135).

**The registry.** `HANGAR_HARNESS_REPO` is the team harness repo ([below](#the-team-harness-repo)): locks at
`harnesses/<name>/harness.lock` (or `<name>/harness.lock`), channels in `channels.yaml`
(`{team-default: {stable: "sha256:..."}}`). A reference is `name` (the current lock), `name@rN`, `name@sha256:...` or
`name@<channel>`; a revision or digest that isn't the checkout's current one is found in its git history. The
registry can also be a subdirectory of a larger repo: its paths, and the history lookups, are relative to that
directory.
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
| each Codex plugin hook | trusted and enabled | what the sandbox's Codex reports for it (`probe.codex_hooks`; drift `codex-hook:<key>`, or `codex-hooks` when Codex couldn't be asked) |
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
delivers nothing. (A trial against a [pin set](api/trials.md) is different: one whose turn 1 drifted from its pins ends
as `outcome_class: infra_error`, `infra_reason: drift`, whatever `HANGAR_HARNESS_DRIFT` says.) The default, `warn`, carries on. A turn that ends
before its check has run waits for it (up to 40 s) under `fail`. Only turn 1 is checked: what the agent changes later
is its behaviour, not drift. Writable state (`~/.claude.json`, plugin caches) is never digested.

### Codex plugins

A `plugin:<name>@<marketplace>` component is a Claude Code plugin. For a Codex harness the same lock carries the
plugin's **Codex form**: the directory `native/codex/<name>/`, a Codex local plugin (`.codex-plugin/plugin.json`
naming `<name>`, its skills and hooks), every file of it pinned as a `native:codex/<name>/...` component. AI-DLC's
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
`hooks/list`). Hangar computes it for exactly the hooks of the lock's Codex forms, from the lock's files, and passes it
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
([jobs.md](jobs.md#the-pull-request), #137).

**Upstream drift.** `hangar harness outdated NAME` (`GET /api/harness-locks/{ref}/outdated`) reports a lock's CLI
version against `harnesses.lock.yaml`, and each `path:` skill against its source in the environments that declare
it. It changes nothing; exit 1 when something is outdated.

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_HARNESS_REPO` | unset | The harness registry: a local checkout, or a git URL with an optional `#ref` ([below](#the-team-harness-repo)) |
| `HANGAR_HARNESS_REPO_REFRESH_SECONDS` | `600` | How often hangar fetches a URL registry (also on a GitHub `push` webhook) |
| `HANGAR_DEFAULT_HARNESS` | unset | The lock intake jobs run from when they name none, e.g. `team-default@stable` |
| `HANGAR_HARNESS_DRIFT` | `warn` | What turn 1's drift from its lock does: `warn` or `fail` |

## The team harness repo

Locks live in a git repo of their own, not in hangar (#138): a harness change is a content change the team reviews
there, on its own cadence and `CODEOWNERS`, without a hangar release. hangar and flightdeck read it; neither owns it.
Where it lives is the owner's choice (a team's real harness may be internal to its employer); a public example is
built separately in agent-factory (agent-factory#4).

```
team-harnesses/
  harnesses/team-default/{harness.lock, CLAUDE.md, skills/*/SKILL.md, plugins/<marketplace>/..., native/...}
  channels.yaml                 # {team-default: {stable: sha256:..., canary: sha256:...}}; moved only by promotion PRs
  .github/workflows/check.yml   # `hangar harness check` on every harness directory
  CODEOWNERS                    # channels.yaml: two reviewers (approver != author, with branch protection)
```

**Pointing hangar at it.** `HANGAR_HARNESS_REPO` is either a local directory (a checkout you keep current yourself)
or a git URL with an optional `#ref` (a branch, tag or commit; default: the repo's default branch), for example
`https://github.com/acme/team-harnesses#main`. For a URL, hangar keeps a full checkout of its own under
`<data>/harness-repo/` on the control plane (full history, so `name@rN` and older digests resolve) and fetches it at
startup, every `HANGAR_HARNESS_REPO_REFRESH_SECONDS` (10 minutes) and at once when GitHub sends a `push` webhook for
that repo (add the `push` event to the App's or repo's webhook; otherwise the 10-minute refresh picks it up). A
private repo is read with the control plane's git credential (`HANGAR_GATEWAY_CREDENTIAL`, else the GitHub App with
`contents: read`), never a sandbox token; the token reaches git through its environment, not its command line or the
checkout's config. A failed fetch keeps the last good checkout. The control plane stays up when the sandbox pools
are powered down, so refreshes go on overnight. `POST /api/harness-locks/refresh` (admin) fetches it now.

**Listing.** `GET /api/harness-locks` lists each harness with its current revision and digest, and each channel's
digest and the revision it resolves to, plus the registry's state (source, commit, last refresh, error).

**`hangar doctor`** reports the repo: reachable (with the commit and the last successful refresh, or the error and
the fix), and whether every channel in `channels.yaml` resolves to a lock. It fails when `HANGAR_DEFAULT_HARNESS` is
set without a repo, or names a harness the repo doesn't have.

**The default for intake jobs.** `HANGAR_DEFAULT_HARNESS=team-default@stable` (off by default) makes intake jobs
(from GitHub, GitHub Projects, Jira, Slack and inbound webhooks) run from that lock when they name no harness, model
or lock of their own, with an event saying which revision. A default that can't be claimed (the repo unreachable,
the environment unable to run the lock) never blocks intake: the job runs as before, with a warning event. Jobs from
the API, MCP and CLI are unchanged; they pass `harness_lock` themselves.

**Bootstrapping `team-default`.** Pick a job that went well (green verify, merged PR), then
`hangar harness freeze <job> --out harnesses/team-default --name team-default`, set `stable` in `channels.yaml` to
the digest `hangar harness check` prints, and open the PR to the harness repo. Thaw it once on a real ticket
(`harness_lock: team-default@stable`) and record that turn 1 had no drift.

Not yet: warm pools per prefix-layer digest (a thawed session takes a sandbox like
any other and mounts the lock into it), mounting native fragments from the lock (they still come from the
environment's `agent_config.files`; their hooks are compared through `harness_digest`).
