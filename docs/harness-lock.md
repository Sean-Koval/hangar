# Harness locks

A harness is a git directory of the harness's own native files plus a small `harness.lock` that pins them. Hangar
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
  - {id: "native:claude-code/settings.json", digest: "sha256:...", path: native/claude-code/settings.json}
binding:                        # rendered at claim; names, never values
  mcp: [{name: github, transport: http, endpoint: "https://api.githubcopilot.com/mcp/", header_names: [Authorization]}]
  hooks: [{event: Stop, source: "hangar:verify_gate"}]
  permissions: {approvals: deny-risky}
plugins: {autoUpdate: false}    # always false
remote: []                      # vendor-loop agents: [{id, version}]
```

- **Components** are listed in order: the guide, the skills in the order the turn declared them, then native
  fragments. A file's `digest` is the SHA-256 of its bytes; a directory's is the manifest's declared skill digest
  (for each file in path order: its relative path, a NUL, the raw SHA-256 of its content). A skill also carries
  `tree`, the digest the sandbox probe reports for the same files (the manifest's `skills.effective`), so a turn's
  effective skills can be compared with the lock without the source. A component with `digest: null` is a reference
  (a pinned git skill, hangar's `verify-ui`): it has a `source` and no files.
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
| `plugins` | the `plugin:` components (null without any; the manifest lists plugins once #136 lands) |
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
- native fragments from the environment's config files under the harness's own directory (`agent_config.files` and
  `files`); a settings file's hooks digest must still match the turn's. Claude Code and Codex are supported; pi and
  Antigravity are frozen best effort, with a warning.

It **fails, naming each gap**, when content can't be recovered:

- a skill whose copy in the sandbox at turn start differed from its declared source (changed in the sandbox, for
  example by the agent in an earlier turn): freeze a turn from before the change;
- instructions truncated or redacted in the manifest with no source left that has the declared digest;
- a source edited or removed since the turn, an unpinned git skill, a plugin (until #136);
- any file the broker's redaction would change: it holds a stored secret, and a secret never goes into a harness
  repo.

Skills in the sandbox that the turn didn't declare (hangar's own `hangar-environment`, or one added there) are not
frozen, with a warning. Freezing into a directory that already has a lock keeps its name (unless `--name` is given),
replaces only the paths the old and new lock manage, and keeps the revision when nothing changed or bumps it by one.
Freeze writes files only: it never commits; open the PR to the harness repo yourself.

A member can freeze only their own sessions and jobs; an admin, anyone's; a `jobs` token, the jobs it submitted. A
turn they can't see answers as missing.

## Checking a directory

`hangar harness check DIR` validates the lock and recomputes every component's digest (and each skill's `tree`). It
also fails on a directory under `skills/` or `plugins/`, or a file under `native/`, that the lock doesn't pin, so
nothing ships unpinned. Other files in the directory (a README, CI config) are ignored. Use it as the harness repo's
CI check (#138).

## Thaw and drift

A session or job can run *from* a lock: `hangar harness thaw team-default@r14 --env python "..."`, or
`harness_lock: "team-default@r14"` on `POST /api/jobs`, `POST /api/sessions` or MCP `submit_job`.

**The registry.** `HANGAR_HARNESS_REPO` is a checkout of the team harness repo (#138): locks at
`harnesses/<name>/harness.lock` (or `<name>/harness.lock`), channels in `channels.yaml`
(`{team-default: {stable: "sha256:..."}}`). A reference is `name` (the current lock), `name@rN`, `name@sha256:...` or
`name@<channel>`; a revision or digest that isn't the checkout's current one is found in its git history.
`GET /api/harness-locks/{ref}` shows what a reference resolves to.

**The claim** is refused, with what to do, when:

- the lock's `runtime.version` isn't the CLI version `harnesses.lock.yaml` pins ("re-freeze the harness from a turn
  on 2.1.296, or bump the lock's runtime.version");
- the environment can't run the lock's harness, or doesn't define an MCP server the lock binds (endpoints and
  credentials live in the environment);
- the request names another `harness`, `model` or `approvals` than the lock's;
- the lock enables a plugin: plugins mount through the Claude Code seed dir (#136).

Otherwise the session (a job's too) is pinned to the lock's harness, concrete model and approval mode, and records
the lock (`harness_lock` on the session and job). The environment provides the files: until #136's read-only prefix
layer mounts them from the lock, thaw runs the environment's image and *checks* that it has the lock's files.

**Turn 1** is compared with the lock once its manifest is recorded:

| Compared | Lock | Turn 1 |
| --- | --- | --- |
| each skill | its `tree` digest | the sandbox's effective skill digest (`skills.effective`); a skill the sandbox has and the lock doesn't is drift too, except hangar's own (`hangar-environment`, `verify-ui`) |
| the instructions file | the `guide:` digest | the declared one (`instructions.global.declared.digest`: hangar splices its environment notes into the sandbox's copy, so that copy isn't compared byte for byte) |
| the CLI | `runtime.version` | the version the sandbox's CLI reported |
| everything else | its `harness_digest` | the manifest's `harness_digest` (model, MCP servers, hooks, approvals) |

The manifest carries `harness.lock: {name, revision, digest}`. Its `harness_digest` is still computed from the
turn's own sections: equal to the lock's when nothing drifted, else what actually ran.

**Drift** is recorded as `SessionTurn.drift: [{component, lock, effective}]` (`[]`: matched; `null`: not checked),
a `harness.drift` event in the session's events (`data.drift`, `data.action`), a warning on the turn, and a line in
the job's review packet (`**Harness:** team-default@r14 …: **drift** on turn 1 (skill:review)`). Under
`HANGAR_HARNESS_DRIFT=fail` turn 1 stops: outcome `failed`, reason `harness_drift` (part B's `infra_error` outcome
replaces it); a job fails with that reason and delivers nothing. The default, `warn`, carries on. A turn that ends
before its check has run waits for it (up to 40 s) under `fail`. Only turn 1 is checked: what the agent changes later
is its behaviour, not drift. Writable state (`~/.claude.json`, plugin caches) is never digested.

**Upstream drift.** `hangar harness outdated NAME` (`GET /api/harness-locks/{ref}/outdated`) reports a lock's CLI
version against `harnesses.lock.yaml`, and each `path:` skill against its source in the environments that declare
it. It changes nothing; exit 1 when something is outdated.

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_HARNESS_REPO` | unset | The harness registry: a checkout of the team harness repo (#138 adds fetching it) |
| `HANGAR_HARNESS_DRIFT` | `warn` | What turn 1's drift from its lock does: `warn` or `fail` |

Not yet: experiment variants by lock (#135), mounting a lock's files read-only (#136), warm pools per prefix-layer
digest (a thawed session takes a sandbox like any other).
