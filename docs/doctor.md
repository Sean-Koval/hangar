# The Cloud Doctor

A cloud agent's environment fails silently: the work just gets worse, and people blame the model. Every night the Cloud Doctor reads hangar's own [flight records](observability.md#flight-records) of failed, timed-out and slow runs, separates the failures a retry fixes from the ones that will happen again, finds their root causes with a strong model, and fixes what it is sure of with a pull request. What it isn't sure of, and anything about credentials or network access, goes to the admins. It is modelled on [Cursor's Cloud Doctor](https://cursor.com/blog/cloud-agent-environment): a scheduled automation that reads agent traces, tells transient errors from salient ones, does root-cause analysis, and opens PRs to fix skills or the environment when it is highly confident.

## What a run does

A run happens in the [nightly pass](observability.md#the-nightly-pass) (`HANGAR_DOCTOR_SCHEDULE`, `30 0 * * *` in the power schedule's time zone, half an hour before the sandbox pools power down), or now with `hangar admin doctor run`. One runs at a time; asking again returns the one running.

1. **It reads the records** written since its last run. For each kind of failure among them (a record's `signature`: its cause, the detail without numbers, and the repo or environment), it counts every record of that kind from the last `HANGAR_DOCTOR_LOOKBACK_DAYS` (7). Records of its own jobs are left out. After a fix merged or a finding was dismissed, only failures since count.
2. **Transient or real.** Causes that look transient (`model_api`, `pod_replaced`, `restart`, `power_down`, `sandbox_gone`, `network`) are only counted. One that keeps coming back (`HANGAR_DOCTOR_TRANSIENT_ALARM`, 5 runs) is reported anyway, with a note to look at the provider or the model API's status rather than the repo. A real one is acted on once it has happened in `HANGAR_DOCTOR_MIN_RUNS` (2) separate runs; before that it is watched.
3. **Requests, not grants.** When agents keep being denied the same host, or keep asking for the same secret, the evidence says enough: the doctor files a request ([env requests](computer-use.md)) that waits in the admins' inbox, `hangar requests` and MCP `pending_approvals`, saying how many runs and which jobs, and how to approve it for the environment (`hangar requests approve <id> --scope environment`). The doctor never changes credentials or egress policy itself, and it doesn't ask again for something an admin denied.
4. **Root cause.** Everything else gets one job per repo (at most `HANGAR_DOCTOR_MAX_JOBS`, 3, a run), in the repo's own environment, so the agent can reproduce the failure where it happened. The job:
   - belongs to nobody, so it runs on the team's credentials (a run without a team credential for the harness reports instead);
   - uses the strongest current model: `HANGAR_DOCTOR_MODEL`, or with Claude Code its `best` alias, which "uses the model the `fable` alias resolves to where Fable is available to you, otherwise the same model as `opus`" ([Claude Code model configuration](https://code.claude.com/docs/en/model-config)); with another harness, that harness's default;
   - gets the evidence as `doctor-evidence.json`, marked untrusted: the newest records of each group (stages, the environment doctor's view, egress denies, requests, versions, the check's output, events) and the end of their run logs, all masked when they were recorded, plus the environment's spec without its secret mappings;
   - delivers nothing (`deliver: none`) and answers with a structured result per group: transient or not, the root cause, a confidence (`high` only when it reproduced it or the evidence leaves no doubt), and the fix: `skill`, `repo_environment` (`.hangar/environment.yaml`), `environment_spec`, `egress`, `secret` or `none`, with the files it would change.
5. **Fixes only when sure.** A finding becomes a fix job, and so a PR, only when all of these hold: the analysis says `high` and not transient; the fix is to a skill, the repo's environment file or an environment spec; the failure happened in at least `HANGAR_DOCTOR_MIN_RUNS` runs; and the files it names are ones that kind of fix may change. Egress and secret fixes become requests (above). Everything else becomes a report in the admins' inbox, saying why it wasn't fixed.

Findings close themselves: a merged PR or an approved request resolves one, a closed PR or a denied request reports it, and a reported failure that doesn't come back in the lookback is resolved.

## The fix PR

A fix job is an ordinary hangar job (`deliver: pr`) on the repo, so it goes through the [landing path](landing.md): the GitHub App opens the PR, the review packet goes on it, and it lands only after a person approves it (with `land: auto_after_approval`, the control plane arms auto-merge then). The PR body carries the evidence:

```
**Cloud Doctor** opened this with high confidence (skill fix, finding `dr-3f2a9c1e`).
- Failure: `missing_tool` (pnpm) in acme/web
- Evidence: 4 runs since 2026-10-09; jobs `job-81c2…`, `job-9d0e…`, `job-a114…`, `job-c7f3…`
- Flight records: `fr-…`, `fr-…` (`hangar admin records show <id>`)
- Root cause: the build skill tells agents to run `pnpm install`, and the environment installs npm only.
```

**What a fix may change.** Before anything is pushed, hangar checks the commits in the job's sandbox and refuses to push (the job fails as `push_failed`, the finding goes to the inbox) when they change anything else:

| Fix | Files it may change |
| --- | --- |
| `skill` | skills (`.claude/skills/`, `.codex/skills/`, `.agents/skills/`, `.pi/agent/skills/`, `.gemini/skills/`, `skills/`, any `SKILL.md`) and the agents' instruction files (`CLAUDE.md`, `AGENTS.md`) |
| `repo_environment` | `.hangar/environment.yaml`, which must still pass hangar's strict check |
| `environment_spec` | `HANGAR_DOCTOR_SPECS_PATHS` (`envs/*.yaml`) in `HANGAR_DOCTOR_SPECS_REPO`; without a specs repo, spec fixes are reported |

An environment fix may change only the fields that say what to install and run: in a spec `description`, `tools`, `dependencies`, `install`, `start`, `terminals`, `services`, `ports` and `verify`, and only in the spec of the environment whose agents failed; in `.hangar/environment.yaml` `install`, `start`, `terminals`, `services`, `ports`, `verify` and `lockfiles`. Everything else (`extends`, `secrets`, `egress`, `cloud`, `runtime_class`, `env`, `files`, `sudo`, `tier`, `name`, ...) decides what an environment holds or reaches, and the doctor proposes hosts and secrets as requests instead. In any other YAML file it changes, `secrets`, `credentials`, `credential_mode`, `egress`, `build_secrets`, `cloud`, `image`, `base`, `runtime_class`, `mcp` and `extends` must stay as they were. YAML is compared as loaded, so anchors, aliases and merge keys count as what they expand to, and a rename counts as the file it removes too.

The check runs twice. In the job's sandbox before the push, and then, because the agent shares that sandbox, from GitHub: once hangar has pushed, the sandbox loses its push grant, and the branch's head is compared with its base through GitHub's API. A branch that fails there is deleted and its PR isn't opened. The fix job's sandbox is removed when the job ends. A PR that changes install or start commands says so: they run in the environment's builds, which hold its build secrets.

At most 3 doctor PRs are open (or being made) at once; past that, high-confidence findings go to the inbox until one lands or closes.

## Budget

Every doctor job carries a [job budget](repo-policy.md#budgets) (`HANGAR_DOCTOR_JOB_MAX_USD`, $3, and a few turns), and the jobs of one run, root causes and fixes together, stay under `HANGAR_DOCTOR_MAX_USD` ($10): a job still running counts at its whole budget. A run out of budget reports what it couldn't analyse. Repo and person day caps apply as to any job; the doctor's jobs belong to nobody, so person caps don't.

## Safety

- **The evidence is data.** Run logs and messages in it were written by agents and the tools they ran. They reach the root-cause agent in a file marked untrusted, and the doctor's own instructions to it name only causes and counts. The fix job is told the fix came from an analysis of agents' logs, and to do nothing beyond the files it names.
- **Nothing lands without a person.** A doctor PR is reviewed and approved like any agent PR. Branch protection stays the gate.
- **Nothing is granted.** Egress and secrets are requests a person answers. The doctor never writes credentials, secrets or egress policy, and hangar refuses to push a fix that does.
- **Labels can't be forged into authority.** The doctor acts on a job's result only when the job is the one it started for that finding; anyone can put `hangar.doctor` labels on their own job, and that only fences their own push.

## Using it

```bash
hangar admin doctor run [--wait]      # now, over the records since the last run
hangar admin doctor ls [--all]        # open findings, and the last runs
hangar admin doctor show dr-3f2a9c1e  # the evidence, the root cause, the fix and what was done
hangar admin doctor dismiss dr-3f2a9c1e --note "flaky mirror, fixed upstream"
hangar status --section doctor        # the last run and the open findings
```

REST (admins): `POST /api/admin/doctor/run`, `GET /api/admin/doctor?all=true`, `GET /api/admin/doctor/findings/{id}`, `POST /api/admin/doctor/findings/{id}/dismiss`. Reports are in the inbox (`hangar inbox`, `hangar://inbox`) of every admin, kind `report`. `hangar://status` has a `doctor` section.

| Finding state | Meaning |
| --- | --- |
| `analyzing` | its root-cause job is running |
| `fixing` | its fix job is running |
| `pr_open` | the fix's PR waits for review |
| `proposed` | a request waits for an admin |
| `reported` | in the admins' inbox, with why it wasn't fixed |
| `resolved` | its PR merged, its request was approved, or it stopped happening |
| `dismissed` | an admin closed it; a new failure of its kind opens a new one |

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_DOCTOR_SCHEDULE` | `30 0 * * *` | When the nightly pass runs; `off` turns it (and the nightly credential checks) off |
| `HANGAR_DOCTOR_TIME_ZONE` | the power schedule's | IANA time zone of the schedule |
| `HANGAR_DOCTOR_HARNESS` | Claude Code where the environment has it, else its default | The harness of the doctor's jobs |
| `HANGAR_DOCTOR_MODEL` | `best` with Claude Code; the harness's default otherwise | The model of the doctor's jobs |
| `HANGAR_DOCTOR_MAX_USD` | `10` | All of one run's jobs together |
| `HANGAR_DOCTOR_JOB_MAX_USD` | `3` | Each job |
| `HANGAR_DOCTOR_MAX_JOBS` | `3` | Root-cause jobs a run starts |
| `HANGAR_DOCTOR_MIN_RUNS` | `2` | Separate runs before a failure is acted on, and before a PR |
| `HANGAR_DOCTOR_LOOKBACK_DAYS` | `7` | How far back it counts (records are kept 14 days) |
| `HANGAR_DOCTOR_TRANSIENT_ALARM` | `5` | Runs a transient failure may recur in before it is reported |
| `HANGAR_DOCTOR_SPECS_REPO` | unset | The repo that holds environment specs as files, for spec fixes |
| `HANGAR_DOCTOR_SPECS_PATHS` | `["envs/*.yaml", "envs/*.yml"]` | The spec files in it |
