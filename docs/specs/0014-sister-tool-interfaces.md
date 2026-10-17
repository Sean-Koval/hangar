# 0014: The sister tool's interfaces

Status: built (PR #114) · Date: 2026-10-15 · Builds on: 0005 (traces), 0006 (environments and packs), 0007 (experiments), #83/#105 (the harness lock, model pins), #85 (budgets), #88 (people), #89 (fairness), #93 (environment builds), #97 (egress), #59/#69 (approvals)

> Part of a set. Ownership, cross-spec decisions and the combined build order are in [docs/specs/README.md](README.md); where this spec conflicts with that page, the page wins.

## 1. The boundary

Hangar is the runtime and control plane: sandboxes, sessions, jobs, intake, landing, policy, and *emitting* data. A sister tool, in a separate repository, *interprets* runs:

- **harness inspection**: what an agent ran with, and how two configurations differ;
- **trace visualization**: timelines of tool calls, subagents, approvals and diffs;
- **behavior specs**: declarative flows and assertions over an agent's event stream, and LLM-judged rubrics;
- **evaluation**: running those specs across harness, model and environment variants, and reporting on them.

The rule for what goes into hangar: something belongs here only if it needs the sandbox lifecycle, credentials, or to sit inside the control plane's trust boundary. Everything else uses the three interfaces below and lives in the sister tool. In particular, hangar has **no spec language, no evaluators, no rubrics, no effect sizes, no leaderboards and no visualization** of runs.

| | Hangar | Sister tool |
| --- | --- | --- |
| What a run ran with | Captures it at turn start, stores it with the turn (manifest) | Inspects, diffs, explains |
| What a run did | Records events; exports them as OTLP/JSON (traces) | Timelines, behavior specs, assertions, rubrics |
| Variants of a configuration | Runs task × variant × trial isolated and pinned; records each trial; runs a named grader command outside the agent's sandbox (experiments) | Defines experiments, grades with specs and rubrics, computes effect sizes, reports |

Two of the interfaces are read-only views of data hangar keeps anyway. The experiment job is runtime: it starts sandboxes, holds credentials and pins conditions, which only hangar can do.

A fourth interface carries the first two out of hangar: **run capture** (§4a, the outbox, `hangar.run/v1`) writes each finished run's manifest, trace, test results and patch, with a `run.json` summary, once to a bucket the sister tool reads while the cluster sleeps.

All of them are **versioned** (`hangar.manifest/v1`, `hangar.trace/v1`, `hangar.experiment/v1`, `hangar.run/v1`). Within a version hangar only adds optional fields, kinds and events; anything removed or renamed is a new version. All three are **owner-scoped** like the records they describe (#88: a member sees their own, an admin everyone's, a `jobs` token the jobs it submitted; anything else answers as missing) and **redacted** with the broker's redaction (`broker.redact`) on the way out. A test seeds a stored secret and checks it appears in no export.

## 2. The effective harness manifest (`hangar.manifest/v1`)

The exact configuration an agent ran with. Schema: [docs/api/manifest.schema.json](../api/manifest.schema.json), validated in the tests.

| Section | Contents | Source |
| --- | --- | --- |
| `harness` | name, the CLI version the pack reported, the locked version (#83/#105), the pack image (by digest where known), pack or bake, live | sandbox, session pin, `harnesses.lock.yaml` |
| `model` | the concrete id, the policy it came from (`frontier`, `balanced`), the provider, the prompt-cache TTL | the session's pin (#105) |
| `instructions` | `global`: the harness's global instructions file, as the environment declares it (text, digest) and as the sandbox had it (digest); `repo`: `AGENTS.md`, `CLAUDE.md`, `.claude/CLAUDE.md`, `GEMINI.md` by reference (repo, commit, path) and digest; `prompt`: the turn's prompt as hangar wrote it | spec; the sandbox, read as the turn starts |
| `skills` | declared (name, source, digest of the files) and effective (the skill directories in the sandbox, digest each) | spec; sandbox |
| `mcp_servers` | name, transport, endpoint (no user info, query or fragment), header and env **names**, command and args, tools where known | the environment's `mcp:`, the computer's browser MCP |
| `hooks` | hangar's verify gate (Claude Code's Stop hook, Codex's driver gate), and hooks in the environment's settings files | the turn's env, spec |
| `permissions` | approvals (`auto`, `ask`, `deny-risky`, #59), live, whether the harness's own permission checks were on, the uid split and hangar-owned settings (#69) | turn, sandbox |
| `egress` | declared, the default, and what the sandbox got (#97) | spec, sandbox |
| `environment` | name, image, spec digest, provider, tier, runtime class, the repo build it started from (#93), services, the names of secret variables | spec, sandbox |
| `resources`, `policy` | CPUs, memory, disk; the turn's budget and the job's (verify, budgets, delivery, strategy) | sandbox, session, job |

**Never credentials.** MCP headers and env, and the environment's `secrets:`, appear as names only; MCP URLs lose their query (tokens live there); everything else passes through the redaction.

**Captured at turn start.** `SessionService.begin_turn` starts one command in the sandbox before the agent starts (it reads the instruction files and hashes the skill directories), and records the manifest in the background once the agent is running, so a turn's start isn't slower. It is stored with the turn (`<data>/manifests/<session>/<turn>.json`, dropped with the session), and the turn records its digest (`SessionTurn.manifest`; also `hangar.manifest.digest` on the turn's span, with `hangar.harness.digest` and `hangar.environment.digest`). A recording failure never fails the turn.

**Four digests.** `digest` covers everything but when and for what it was captured. `config_digest` covers the harness configuration and its environment together (harness, model, global instructions, skills, MCP servers, permissions, egress, environment identity, resources) and not the task, repo or prompt: two trials of one experiment variant share it and two variants don't. It stays, unchanged, for compatibility. Its two halves (#131; optional fields, absent from manifests stored before them) tell a harness change from an environment change:

- `harness_digest`, the prefix and binding layer, everything a harness author controls: `harness.name`, `harness.locked`, `model.id`, `instructions.global.declared.digest`, `skills.declared[*].{name,digest}`, `plugins`, `mcp_servers[*].{name,transport,url,headers,env,command,args}`, `hooks[*].{event,source,digest}`, `permissions.approvals`.
- `environment_digest`, what the sandbox is: `environment.name`, `environment.image`, `environment.spec_digest`, `environment.build.id`, `egress.effective`, `resources`, `permissions.uid_split`, `permissions.isolation`.

Canonical form, for both: build an object whose keys are the section strings exactly as listed and whose values are what each picks from the redacted manifest. `a.b.c` is that value, `null` when any step is missing; `a[*].{x,y}` is the list `a` with each item reduced to `{x, y}` (a missing field is `null`; the whole value is `null` when `a` is not a list). Serialize it as JSON with keys sorted and no whitespace (`separators=(",", ":")`), UTF-8; the digest is `"sha256:"` + the lowercase hex SHA-256 of those bytes (`manifest.section_digest`). Identity uses **declared** skills and instructions, never effective ones: drift is effective ≠ declared, reported separately (#134). `model.id` is the resolved concrete id; a policy name (`frontier`) never enters a digest. `plugins` is not in the manifest yet, so it is `null`. Redaction runs before digesting, so every digest is over what anyone can see. A harness lock (#133; [harness-lock.md](../harness-lock.md), schema [docs/api/harness-lock.schema.json](../api/harness-lock.schema.json)) computes its `harness_digest` by this same rule over its projection onto these sections (`harness_lock.identity`: `runtime` → `harness.name`/`harness.locked`, the `guide:` and `skill:` components → the declared instructions and skills, `binding` → `mcp_servers`, `hooks`, `permissions.approvals`), so a lock frozen from a turn has exactly that turn's `harness_digest`; its name, revision, paths and provenance are not identity. A turn of a session thawed from a lock (#134) also carries `harness.lock: {name, revision, digest}`; its `harness_digest` is still computed from its own sections, so it equals the lock's digest unless the turn drifted from the lock (then `harness_digest` says what ran, `harness.lock.digest` what was asked; `SessionTurn.drift` lists the differences).

**Frozen as a harness.** `hangar harness freeze <session|job> --turn N --out DIR` (`GET /api/sessions/{name}/harness?turn=N`, `GET /api/jobs/{id}/harness?turn=N`, owner-scoped like the manifest) writes a turn's harness as native files plus `harness.lock`, recovering each file from its declared source and refusing, by name, what can't be recovered (a skill the sandbox had changed, truncated instructions, a secret); `hangar harness check DIR` recomputes a directory's digests against its lock ([harness-lock.md](../harness-lock.md)).

**Where.** `GET /api/sessions/{name}/manifest?turn=N`, `GET /api/jobs/{id}/manifest?turn=N`, `GET /api/environments/{name}/manifest?harness=H` (what a new sandbox would get); MCP resources `hangar://sessions/{name}/manifest` and `hangar://jobs/{id}/manifest`; `hangar session|job|env manifest`. A session without a captured turn answers with what it would run with now (`subject.kind: session`).

## 3. Trace export (`hangar.trace/v1`)

Contract: [docs/api/traces.md](../api/traces.md). Summary:

- **Format.** OTLP/JSON (`ExportTraceServiceRequest`, opentelemetry-proto 1.11.1), per run (one turn), per session and per job. `GET /api/sessions/{name}/trace?turn=N`, `GET /api/jobs/{id}/trace`, `hangar trace export`, `hangar://sessions/{name}/trace`, `hangar://jobs/{id}/trace`. It POSTs as is to a collector's `/v1/traces`.
- **Conventions.** The OpenTelemetry GenAI semantic conventions, which moved in 2026 from the core repository to [semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai). Pinned: its registry at `f3c6d0c9bb66ebd474668a3a0be25c61226f02c2` (2026-10-14; no release tags yet), schema `gen-ai-dev/1.42.0-dev`, on core semantic conventions 1.44.0. **Status: development** for every GenAI span and attribute used. Agents are `invoke_agent` spans, tools `execute_tool`, sessions and jobs `invoke_workflow`; usage follows the convention that `gen_ai.usage.input_tokens` includes cached tokens. The rest is under `hangar.*`.
- **Taxonomy** (what behavior specs match on): spans carry `hangar.kind` (`session`, `job`, `turn`, `tool.call`, `subagent`, `approval`), span events are named by kind (`message`, `progress`, `error`, `file.change`, `steer`, `interrupt`, `verify`, `question`, `git.commit`, `git.push`, `pr.open`, `ci.check`, `pr.merge`, `job.state`; `test.run` and `test.case` reserved for test results), and tool calls have a normalized `hangar.tool.kind` (`shell`, `edit`, `read`, `search`, `web`, `mcp`, `skill`, `plan`, `other`) mapped from every harness's own tool names. Shell calls and approvals carry `hangar.shell.ops` (`test`, `git.commit`, `git.push`, `pr.open`, `delete`), file events a repo-relative `hangar.file.repo_path`, jobs the change's files (`hangar.diff.files`), and everything from the event log its position `hangar.seq` (timestamps can tie). The review of #114 checked that "tests before commit", "no edits outside src/", "asked before deleting" and "plan → edit → test → PR" are each a predicate over the export alone.
- **Live.** A control plane with tracing on already sends each turn's `invoke_agent` span (observe.py). `HANGAR_TRACE_LIVE_TOOLS=true` adds the turn's tool, subagent and approval spans as its children when the turn ends, through the same tracer, with the same attributes (observe.py extended, not duplicated).
- **Ids** are derived from the session's or job's id, so an export is reproducible; live export uses the SDK's.

Test results: the `testresults` work stores parsed results on jobs; the export carries them as `test.run` / `test.case` events under `hangar.test.*` once they land (§7).

## 4. Experiment jobs (`hangar.experiment/v1`)

The slice of [0007 §5.4](0007-experiments-policy-and-team-learning.md#54-experiments-ab-testing-with-environments) hangar must own: isolation and pinning need the sandbox lifecycle and credentials. Index schema: [docs/api/experiment.schema.json](../api/experiment.schema.json).

```yaml
name: skills-ab
environment: hangar
tasks:
  - {id: fix-retry, instructions: "Make the reaper retry a failed destroy.", repo: https://github.com/Sean-Koval/hangar.git, verify: "uv run pytest -q tests/test_fleet.py"}
  - {id: issue-412, issue: "Sean-Koval/hangar#412"}          # its text reaches the agent as untrusted context
variants:
  control: {}
  with-conventions: {skills: [{name: repo-conventions}]}       # the whole set: [] means none
  codex: {harness: codex@0.162.1, model: balanced}             # must be the locked version
trials: 3
parallel: 6
trial_job_budget: {max_cost_usd: 3}
budget: {max_cost_usd: 40}
grader: {command: "pytest -q tests/hidden", files: {"tests/hidden/test_retry.py": "..."}}
```

**Shape.** A `kind: experiment` job (label `hangar.kind: experiment`) takes a task set (prompts with repo refs, or GitHub issues), variants (an environment plus an overlay: `harness[@version]`, `model` (an id or a policy), `approvals` (`auto` or `deny-risky`), `skills`, `skills_remove`, `instructions` or `instructions_append`), and a trial count. `POST /api/experiments`, `hangar experiment run spec.yaml`.

**The only overlay.** A variant is the one overlay hangar lays on an environment, and it gains no new fields. Its `skills` means what an environment's `skills` means under `extends` (#132): the whole set, `[]` for none. `skills_remove: [name]` drops named skills from that set (else from the environment's), and the trial's manifest declares what remains. Once harness locks exist (#133) a variant shrinks to `{harness: name@sha256, environment}` (#135).

**Runs.** The experiment runs no agent itself. A background runner (no job slot) starts one ordinary job per task × variant × trial (`source: experiment`, `deliver: none`, no auto-fix, the in-turn verify gate off unless asked, since it differs by harness), interleaved so a task's variants run side by side, at most `parallel` at once. They are jobs: each gets the per-trial budget (#85), daily budgets apply, and they queue in the fair queue under the experiment's person (#89). The experiment's own `budget` stops new trials once its trials together spent it. A trial's agent that asks a question is cancelled: nobody is there to answer.

**Pinned.** At the start: the harness version (the lock; a lock change mid-way stops new trials), the concrete model id (a policy is resolved once), the approvals policy, and each task's base commit (from GitHub when hangar can ask; else the first trial's clone pins it and every later trial checks it out).

**Isolated** (0007 finding 3). A trial's sandbox mounts only its own harness's pack (`hangar.packs: selected`), never comes from the environment's warm pool (whose sandboxes mount every pack; on Kubernetes it gets a template of its own), and gets exactly the skills and instructions its variant declares, written when the sandbox is created (every time, so a resumed trial has them; the environment's other skills are removed). Hangar's own additions stay, alike for every variant on an environment: `verify-ui` with a computer, and for a repo with a `.hangar/environment.yaml` or a build, the environment section spliced into the instructions and the `hangar-environment` skill (written after the overlay; the manifest's effective skills and instruction digest include them). Hangar-owned settings for a turn that asks read the variant's instructions too. Egress is fixed per experiment: variants whose environments differ in egress are refused (0007 finding 4), and each environment's policy is pinned at the start, so a change mid-way (an approved egress request widens an environment) stops new trials like a lock change does. A trial's sandbox never refills the environment's warm pool either (it would re-template it with one pack).

**Output.** Per trial, the index records its job, session, each turn's manifest digest and the first turn's `config_digest`, `harness_digest` and `environment_digest`, its verification (hangar's check after the turn, with new failures against a red base), its test results when parsed, usage and cost, and keeps its manifest and trace export as files (`GET /api/experiments/{id}/trials/{task}/{variant}/{n}/manifest|trace`, `hangar experiment export -o DIR`). Trials of one variant whose `config_digest` differ are flagged `drift`, and the message names the half that drifted ("harness drift", "environment drift", or both).

**No analysis.** Hangar computes no metric across trials. It does run the experiment's optional external grader command, because that needs a sandbox: after the trial ends, in a clean, sealed sandbox of the same environment (`delivery.sealed`: no credential, no `secrets:`, no cloud token, a fetch-only git grant revoked before the command runs, no `mcp:`, `computer:` or services, and egress `none` with no host at all, not even the harness's; `grader.network: mirror` adds hangar's read-only package mirror), on the pinned base with the trial's patch applied. The grader's own files (the agent never had them) are written outside the repo, to `$HANGAR_HIDDEN_TESTS` (`files_mode: pristine`, the default; `in_repo` keeps the old layout and the grade says `trust: low`), and `hangar-grade pytest` runs them so that nothing the patch adds loads before them ([jobs.md](../jobs.md#grading)). Its exit code, the end of its output, the hidden tests' junit counts, the patch's tamper signals and the JSON it writes to `$HANGAR_GRADE_OUT` are stored with the trial, with the grade's `trust` (#127). Once per task the hidden tests also run on the base (they must fail) and on the task's optional `reference_patch` (it must pass): a task that fails that check is `invalid_task` and its grades say `task_invalid`. Each trial also records `touched_tests` and `contaminated` (the grader's canary, written into every hidden test file, seen in what the trial produced).

**Access.** CLI and REST. On MCP only the resource `hangar://experiments/{id}`: the tool list is at its cap, so no tool and no field were added.

## 4a. Run capture: the outbox (`hangar.run/v1`)

Contract: [docs/api/outbox.md](../api/outbox.md); `run.json` schema: [docs/api/run.schema.json](../api/run.schema.json) (#163, #189). Summary:

- **What.** Every finished run (a session turn; a job turn after hangar's check and the delivery; a best-of-N attempt after its check; an experiment trial's turns) as immutable objects under `v1/runs/{date}/{session_id}.{run_id}/`: `run.json` (written last: the completion marker), `manifest.json` (§2), `trace.otlp.json` (§3, that run only), `tests.json` (its test runs with every case, and their `test.run` / `test.case` events), `grade.json` (trials) and `patch.diff`. Facts that arrive later (PR opened, first review, merged, closed, abandoned) are separate event objects under `v1/jobs/`, never rewrites.
- **Where.** `HANGAR_OUTBOX_URL`: a GCS bucket (terraform: `run_bucket`) or a local directory for dev and tests.
- **Write-once and redacted.** Created with `ifGenerationMatch=0` by a service account that can create and nothing else there; every object passes `broker.redact` and secretscan's masking, and one that still holds a secret is withheld, never uploaded.
- **Never on a turn's path.** Built and staged on a task after the turn, uploaded by a drain loop with backoff (1 minute to 6 hours) through outages and restarts, overnight included.
- **Hangar pushes, the sister tool owns.** The GCS → Parquet compactor and its tables live in the sister tool (Sean-Koval/flightdeck#5). Hangar owns only the object contract, and does no analysis over it.
- **Retention waits for it** (#189): a deleted session's event log and manifests, and a failed job's test results, are kept until the outbox exported them.

## 5. What moved to the sister tool

From 0007: the paired analysis and effect sizes (§5.4 rule 4), saturated-task flagging (rule 5), `experiment_report` and its dashboard and doc export, the label taxonomy and labeler cascade (§5.5), process mining and routing on labels, team-learning miners and proposals (§5.6, M8 and M10). From 0005: the evaluation and insights milestones (M9–M10) that read traces. Hangar keeps 0007's runtime parts: policy enforcement, the MCP gateway, budgets, `run_experiment` as the experiment job kind, and the cost gate (budgets).

## 6. Security

- Exports never carry a credential: hangar never records one in events or manifests, the broker's redaction runs again on every export, MCP header and env values and `secrets:` values are never read into a manifest, and MCP URLs lose their query. Tested with a seeded secret in the agent's command, output, message, the task text, the environment's instructions, MCP headers, URL and env.
- Owner scoping follows #88 on REST and MCP; a record the caller may not see answers as missing.
- A grader runs code the experiment's author names on agent-written code: in its own sandbox, destroyed afterwards. That sandbox is treated as running repo code (`delivery.builds_repo_code`): no credential is injected, no `secrets:`, no cloud token, and its git grant is fetch-only (review of #114: it used to get the environment's git token, `secrets:` and cloud tokens, and a push-capable grant). It is also sealed (#126): egress `none` with no host at all, always on the proxy-only network, so a prompt-injected patch can't send the hidden tests out; and the hidden tests run from a pristine copy outside the repo, where a `conftest.py`, pytest settings or a `sitecustomize.py` in the patch can't load before them under `hangar-grade pytest`. With the uid split (Docker) the grade is hangar's own (#127): its user runs the command as the agent and takes the exit code, output, `$HANGAR_GRADE_OUT` and junit through pipes it created, writing the grade in a 0700 directory, so `trust: isolated`. Without it (GKE) `trust: best_effort`; with `in_repo` or a raw command, `low`. Code under test still runs in the test runner's process and can lie through it: the base-must-fail check, canaries and `touched_tests` are hangar's detectors, outcome graders and hidden-test freshness the sister tool's. The canary is stored only in the experiment's spec (owner-scoped) and masked in every export.
- An experiment can spend a lot: per-trial and experiment budgets, daily budgets, `HANGAR_EXPERIMENT_MAX_TRIALS` (200), `parallel`, cancel.

## 7. Open items

1. **Test results in traces.** The `testresults` work stores parsed results on jobs; when it merges, map them to `test.run` / `test.case` events (reserved names above) and to the trial's `tests`.
2. **MCP tool lists.** `tools` is null until hangar proxies MCP (0007 §5.1's gateway); then the manifest lists each server's allowed tools.
3. **Repo-build pinning.** Trials record the repo build they started from (`environment.build`), but a rebuild mid-experiment (a merge to the base branch) isn't prevented; the manifests show it.
4. **Timestamps.** Event times are when hangar read them from the run log (within a second when the log is followed live, else up to the poll interval); per-tool durations are approximate.
