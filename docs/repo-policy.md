# Repo policy: the defaults every front door applies

Work reaches hangar through many doors: MCP, REST, the CLI, GitHub issues, Slack, A2A, inbound webhooks and schedules. A **repo policy** is one record per repository with the defaults its jobs get, whichever door they came through. A new kind of intake only has to name the repo.

```bash
hangar repo set acme/app --verify "uv run pytest -q" --env python
hangar repo set acme/app --model frontier            # every harness: its provider's strongest model
hangar repo set acme/app --model claude-code=balanced --model codex=gpt-6.1-sol
hangar repo list
hangar repo get acme/app            # the policy, and the environment it falls back to
hangar repo unset acme/app model    # clear one field
hangar repo unset acme/app          # remove the policy
hangar repo set acme/app --job-max-cost 5 --job-max-turns 12 --max-usd-per-day 60
hangar repo set acme/app --land auto_after_approval --approver alice --risk-path 'infra/**'   # docs/landing.md
hangar repo set acme/app --ai-review hangar --self-approval low_risk_with_ai_review       # AI review, self-approval
hangar repo check acme/app          # is it ready for agent work? (below)
```

| Field | What it sets |
| --- | --- |
| `verify` | The repo's check (see [jobs §7](jobs.md#7-an-agent-loop-that-finishes-the-job)). `""` means no check, even if the environment has one. |
| `environment` | The environment jobs run in when they don't name one. For a repo in `HANGAR_GITHUB_REPOS`, it must be one of the environments listed there. |
| `harness` | The default harness. It must be enabled in the environment; when a job runs in an environment without it, the job uses the environment's default and its events say why. |
| `model` | One model id, or one per harness (`{"claude-code": "opus", "codex": "gpt-5.5"}`), as in an environment's `model:`. A single id applies only to the policy's harness, or, without one, to jobs that didn't pick a harness: a model id means nothing to another harness. A policy (`frontier`, `balanced`) applies to every harness and is resolved to a concrete id when the job starts ([harnesses.md](harnesses.md#models-the-latest-strong-model-per-provider)). |
| `budget` | The default per-turn budget (`max_minutes`, `max_tokens`, `max_cost_usd`), field by field under the job's own. |
| `job_budget` | The default [job budget](#budgets) (`max_cost_usd`, `max_tokens`, `max_turns`). |
| `max_usd_per_day` | The repo's [daily spend cap](#budgets). |
| `credentials` | Whose login the repo's jobs run with: `person` (the person the job runs for; a job for no one fails), `team`, or `person_then_team` (the default: theirs when they have one, else the team's). See [team.md](team.md#whose-login-a-job-uses). |
| `egress` | `{mode, presets, allow}` for the repo's sandboxes: adds presets and hosts to the environment's allowlist, and can tighten its mode but never widen it (`hangar repo set acme/app --egress-mode none --egress-allow docs.example.com`). See [egress allowlists](security.md#egress-allowlists). |
| `build_secrets` | `{SECRET_ID: credential}` for the repo's [environment builds](environments.md#build-secrets), winning over the environment's (admins; `PUT /api/repos/policy`). See [build secrets](security.md#build-secrets). |
| `land` | `auto_after_approval`: an approval of the PR's head by an approver or code owner, with green required checks, lands it ([landing.md](landing.md)). `manual` or unset: a person merges. |
| `approvers` | GitHub logins whose approval lands the repo's agent PRs (`--approver`, repeated). |
| `merge_method` | `squash` (default), `merge` or `rebase`, for PRs hangar lands. |
| `risk_paths` | Globs (`infra/**`) that make a PR high risk in its [review packet](landing.md#6-the-review-packet), on top of `.github/**` and `.hangar/**`. |
| `require_stale_dismissal` | `false` lets hangar arm auto-merge on a base branch that doesn't dismiss stale approvals on a push. Unset (the default) it refuses ([landing.md](landing.md#3-the-branch-protection-to-enable)). |
| `verify_reports` | The JUnit, Cobertura or LCOV files `verify` writes, relative to the repo, `**/` for any depth (`--verify-report 'build/**/TEST-*.xml'`, repeated). The repo's `.hangar/environment.yaml` `verify_reports` comes first. Most runners need none: see [jobs §8](jobs.md#where-the-reports-come-from). |
| `rerun_flaky` | `true`: a check whose every failure is on the repo's [flaky list](jobs.md#flaky-tests) runs once more, and the rerun decides (`--rerun-flaky`). Unset: a failure stands. |
| `heavy` | Docker on the [heavy tier](environments.md#the-heavy-tier), **deny by default**. `on`: the repo's sandboxes run on it, with Docker inside (`--heavy on`). `allow`: the repo's `.hangar/environment.yaml` may ask for it (`heavy: true` or `tier: heavy`), and the request is honored (`--heavy allow`). `deny`, or unset: a request in the repo's file isn't honored; the sandbox runs on the default tier, and the agent's instructions and `hangar-agent env doctor` say Docker isn't enabled for the repo and give this command. A heavy environment (an admin's `heavy: true`) has Docker whatever this says. `true` and `false`, sent by an older client, mean `on` and `deny`. |
| `profile` | `standard`, `large` or `xlarge`: the size of the repo's sandboxes ([profiles](environments.md#what-decides)). Large and xlarge run on the heavy tier. |
| `max_profile` | The largest profile a job (label `hangar.profile`, `hangar job submit --profile`) or the repo's file may ask for. Unset: no more than `profile` or the environment's own. |
| `self_approval` | Whether the job's own person may be its PR's only approver ([landing.md](landing.md#self-approval-who-may-be-the-only-approver)): `low_risk_with_ai_review` (unset: the default; a low-risk PR with an independent AI review of the approved head), `never`, or `any` (`--self-approval`). It never loosens the taint gate: a tainted PR always needs someone other than its person. |
| `ai_review` | The [AI first-pass review](landing.md#6a-ai-first-pass-review) of the repo's agent PRs: `hangar` (a review job on a different harness from the author's), `check:<name>` (an org review bot's check run or status on the head) or `off` (unset) (`--ai-review`). |
| `ai_review_harness` | The reviewing harness. Unset: codex for a claude-code PR, else claude-code (`--ai-review-harness`). |
| `ai_review_model` | The reviewer's model or policy. Unset: `frontier`, resolved to a concrete id when the review is submitted (`--ai-review-model`). |
| `ai_review_budget_usd` | What one review job may spend. Unset: $1.50 (`--ai-review-budget`). |
| `max_open_agent_prs` | [Review capacity](team-scale.md#review-capacity): open agent PRs awaiting a person before their new jobs on this repo wait as `awaiting_capacity`; `0` turns it off. Unset: `HANGAR_PERSON_MAX_OPEN_AGENT_PRS` (5) (`--max-open-agent-prs`). |
| `verify_allowed` | Globs (`make test*`, `pytest tests/*`) a ticket's own *Verify command* ([agent tickets](intake.md#agent-tickets)) must match to become that job's `verify` (`--verify-allowed`, repeated). Unset: a ticket's command is only ever a suggestion to the agent, and the job's check stays the policy's or the environment's. A matched command is the job's own check (`verify_source: job`), so the [base check](#the-check-on-the-base-commit) is skipped for it, as for any acceptance test a job brings. `*` alone is refused. |
| `overnight` | `true`: the repo's intake jobs (Jira, GitHub, Slack, GitHub Projects) run at night in the overnight lane instead of waiting for 10:00 (`--overnight`; [gke.md](gke.md#the-overnight-lane), #186). A job from any repo can ask for it with the label `lane: overnight`. |
| `branch_template` | The repo's job branches, e.g. `feature/{ticket_key}-{job}`; it must contain `{job}`. Unset: `hangar/<job>`. See [change sets](changesets.md#company-conventions-in-the-repo-policy). |
| `require_ticket` | `true` refuses a job for the repo without a `ticket` (`jira:PAY-412`). |
| `title_template` | The title of the repo's agent PRs, e.g. `{ticket_key}: {title}`. |

The repo is `owner/repo` for GitHub (any URL form, any case), or the git URL for another host.

## Precedence

For each field: **the job, then the repo's own `.hangar/environment.yaml`, then the repo's policy, then the
environment, then hangar's settings.**

- A job's own `verify`, `environment`, `harness` or `model` always wins.
- A repo's `.hangar/environment.yaml` can set its `verify` and, with `extends`, its environment (within the allowlist).
  Hangar uses the file as the default branch had it at the last clone. See [environments.md](environments.md).
- An environment can carry a `verify:` too, the fallback for every repo that runs in it:

  ```yaml
  name: python-app
  extends: python
  verify: uv run pytest -q
  ```

- `verify: ""` at any level means "no check" and stops the fallback.
- The job's events say what came from the policy (`from the repo policy for acme/app: verify …`), and `verify_source` on the job says where its check came from: `job`, `repo` or `environment`.

`HANGAR_GITHUB_REPOS` stays the allowlist: which repos untrusted and service callers (GitHub, Slack, webhooks, `jobs` tokens) may start work on, and in which environments. A policy never widens it.

## REST

Admin tokens only. The repo is a query or body field, because git URLs carry slashes and colons.

| Route | |
| --- | --- |
| `GET /api/repos` | Every policy, each with `allowed_environments` (from `HANGAR_GITHUB_REPOS`), the environment's own `environment_verify`, and the `effective_verify`. |
| `GET /api/repos/policy?repo=acme/app` | One policy. |
| `PUT /api/repos/policy` | `{"repo": "acme/app", "verify": "pytest -q"}`: sets the fields sent and keeps the rest; a field sent as `null` is cleared. A policy no job could run with is refused: an unknown environment, an environment the allowlist doesn't have for the repo, or a harness the environment doesn't enable. |
| `DELETE /api/repos/policy?repo=acme/app[&field=verify]` | Clears the fields named, or removes the policy. |
| `GET /api/repos/{owner}/{repo}/readiness` | The [readiness report](#is-this-repo-ready) for a GitHub repo, the same object `hangar repo check --json` prints. |

Policies are stored in hangar's database, written durably like `hangar config` settings, and apply to the next job without a restart. Every change is audited (`repo.policy.set`, `.unset`, `.delete`).

## Is this repo ready?

`hangar repo check acme/app` answers with one report whether a repo is ready for agent work: landing, the git gateway,
CI and the doors (GitHub, Jira, Slack). Each check is `ok`, `warn` or `fail` with the command or setting that fixes
it, and the last line says `ready` or `not ready (N failing)`. It exits 1 when anything fails; warnings don't. `--json`
prints the object `GET /api/repos/{owner}/{repo}/readiness` returns (admin tokens):
`{repo, ready, failing, warnings, summary, checks: [{check, status, detail, fix}]}`.

| Check | `ok` when | Not ok |
| --- | --- | --- |
| `allowlist` | the repo is in `HANGAR_GITHUB_REPOS` | fail |
| `github app` | the GitHub App ([landing.md](landing.md)) mints an installation token for the repo | fail when it can't (not installed); warn when no App is configured |
| `github access` | the App, else the control token, reads the repo | fail |
| `github webhook` | the repo's webhook sends hangar the events it handles, as JSON | fail; warn when the webhooks can't be listed |
| `trigger label` | the label `HANGAR_GITHUB_TRIGGER_LABEL` (`hangar`) exists | warn (a `/hangar` comment still starts work) |
| `repo policy`, `verify` | there is a policy, and a check (the policy's or the environment's `verify`) | fail |
| `landing` | `land` is set; with `auto_after_approval`, there are `approvers` or a CODEOWNERS file | warn when unset; fail when nobody's approval could land a PR |
| `daily cap` | `max_usd_per_day` is set | warn |
| `required checks` | the base branch requires status checks | fail |
| `classic branch protection` | hangar could read it | warn: it needs Administration: read, which the App doesn't have by design; the rulesets (`GET /rules/branches/{b}`) are read instead |
| `stale approvals` | the base branch dismisses stale approvals on a push | fail (hangar won't arm auto-merge); warn when the policy opts out |
| `review rules` | it requires an approving review and approval of the most recent push | warn |
| `auto-merge` | (shown when it fails) the repo allows auto-merge, for a policy that lands on approval | fail |
| `intake posture` | `HANGAR_INTAKE_POSTURE=enforce` ([security.md](security.md#intake-posture)) | warn for `warn`, fail for `off` |
| `git gateway` | the gateway serves the repo's host and answers | fail when it doesn't serve the repo; warn when the control plane can't reach it |
| `credential mode ENV` | each environment the repo's jobs run in resolves to `credential_mode: gateway` | fail when its sandboxes would hold a GitHub token; warn for `direct` while the gateway serves the repo (jobs still fetch through it, people's sessions don't) |
| `sudo ENV` | `sudo: false` | warn under `enforce` (Docker job sandboxes run with no-new-privileges), else fail |
| `heavy ENV` | (shown for Docker on the heavy tier) the policy's `allow_heavy_for_intake` | fail under `enforce`: intake jobs are refused |
| `egress ENV` | an egress mode other than `open` | warn under `enforce` (intake jobs get the allowlist floor, other jobs run open), else fail |
| `credentials ENV` | the agent login a job no person owns would use is an API key | fail for a subscription login (`oauth_token`, `auth_file`) or none ([team.md](team.md#subscription-logins)) |
| `jira`, `jira transitions KEY` | a Jira project maps to the repo, and its workflow has the trigger status and the `started`, `needs_input`, `delivered` and `failed` statuses | fail for a status the workflow lacks; warn for no project or an unset stage; ok when Jira intake is off |
| `slack` | informational: the channels that start jobs on the repo | warn when Slack is on and no channel maps to it |

The environments are the policy's, those `HANGAR_GITHUB_REPOS` lists for the repo, and any a Jira project mapped to it
names. Nothing is written to GitHub, Jira or Slack: every call is a read, except the App's installation-token mint,
which writes nothing on the repo. GitHub's answers are kept for 5 minutes (landing's cache), so running it again doesn't
spend rate limit, and it needs only the control plane: it works while the sandbox pools are powered down. It fixes
nothing itself; `hangar doctor` covers the checks across every repo, and `hangar doctor --fix` applies the safe ones.

## Budgets

A `budget` caps one agent turn, and every turn used to get a fresh one. So `max_cost_usd: 2` with best of 4, a judge and 3 auto-fix rounds could spend $16 before anyone followed up. Two limits bound the spend:

**A job budget** (`job_budget` on the job, the repo's policy, or `HANGAR_JOB_BUDGET_MAX_COST_USD` / `_MAX_TOKENS` / `_MAX_TURNS`) caps all of a job's agent turns together. That covers the first turn, follow-ups, verify-fix and PR auto-fix turns, every best-of-N attempt and the judge.

- **The turn that crosses it is stopped.** Each turn's budget is cut to what the job has left, and the turn stops like any turn over budget: `over_budget`, with its work delivered as a draft.
- **A spent job starts no more turns.** Follow-ups and auto-fix rounds are refused with the reason, and verify-fix rounds aren't started.
- **Best-of-N splits it evenly.** The attempts run at once, so each gets an equal share, with one share kept for the judge; together they can't cross the job budget. An attempt can overshoot its share by what it spends between two polls, so when the attempts have used the whole job budget there is no judge: the event says so, and the smallest diff wins. A `max_turns` lower than the attempts plus the judge is refused when the job is submitted.

**A daily cap per repo** (`max_usd_per_day`) is checked when work is admitted, on the repo's spend since midnight in `HANGAR_BUDGET_DAY_TIMEZONE` (an IANA zone; UTC unless set). UTC midnight is 20:00 in New York (19:00 in winter), in the middle of a working day there, so a team in the Americas should set its zone (`America/New_York`); otherwise a cap can be spent twice in one working day. Days follow the zone's daylight-saving changes. `/api/usage` still groups by UTC day. Over the cap:

- a new job over the cap waits in `awaiting_approval`, and the GitHub issue or Slack thread says why. A trusted person can let it run anyway (`/hangar approve`, `hangar job approve`), or it is dropped when the approval expires (`HANGAR_JOB_APPROVAL_TTL_HOURS`);
- a follow-up or auto-fix round is refused;
- turns already running, and a job's own verify-fix turns, finish under the job budget.

Costs are what the harness reports, or the token counts priced with the price table ([activity.md](activity.md)). On a subscription login the dollars are an estimate; `max_tokens` and `max_turns` don't depend on prices.

For each field the order is the job, then the repo's policy, then the settings.

**A daily cap per person** (`HANGAR_PERSON_MAX_USD_PER_DAY`, unset by default) works the same way for the jobs a person submits. It counts what all of that person's sessions and jobs spent that day, whichever of their tokens or connectors started them (the person hangar records on each; see [security.md](security.md#people)). Jobs nobody in particular asked for, such as GitHub- and Slack-triggered ones, have no person yet and count only against their repo's cap.

### The eval budget

Evals run on API keys with dollar caps, so eval spend has a monthly budget of its own, across experiments and trials
(#182):

| Setting | Default | Meaning |
| --- | --- | --- |
| `HANGAR_EVAL_BUDGET_USD_MONTH` | 1500 | The team's eval spend per calendar month (UTC); 0 turns the eval budget off |
| `HANGAR_EVAL_AUTHOR_CAP_USD_MONTH` | 400 | Each author's share; `hangar person set alice --eval-cap 600` (an admin) gives one person their own |

- **What counts.** Eval jobs (`queue_class: eval`: experiment trials, the trials API's, shadows) created this month,
  by their author: the experiment's person, else who asked for it. Every session that carried a trial counts, deleted
  ones too. Cached trial results ([api/trials.md](api/trials.md#the-cache)) start no job and cost nothing.
- **The gate.** An experiment, or a `POST /api/trials`, whose projected cost exceeds what the team or the author has
  left is refused, with the spent and remaining numbers. The projection is trials × each task's p50 cost, taken from
  the cached trials of the same key, else from the trial job budget's `max_cost_usd`. A task with neither projects $0,
  and the message says so.
- **No kills.** Trials already running finish, even past the budget (each trial's job budget is the hard stop), but
  once the team's or the author's budget is spent no new trial starts: they show `skipped` with the reason.
- **Seeing it.** `hangar eval budget` (`GET /api/eval/budget`) shows the month, the team's budget, what's spent and
  left, and each author's cap and spend.

## Shadows

A shadow runs a candidate harness on a sampled real ticket, at the same base commit, and delivers nothing; flightdeck
grades it later against what people merged (#183). The harness owner turns it on per repo:

```bash
curl -s -X PUT $HANGAR/api/repos/policy -H "Authorization: Bearer $ADMIN" -d '{"repo": "acme/api",
  "shadow": {"harness": "team-next@sha256:9a41...", "p": 0.1, "sources": ["jira", "github"], "owner": "ivy"}}'
```

| Field | Meaning |
| --- | --- |
| `harness` or `pin_set` | The candidate: a harness lock by digest, or a pin set (its harness, environment and model; the task and base are the real job's) |
| `p` | The share of the repo's real jobs from `sources` that get a shadow (0.0-1.0; absent or 0: none) |
| `sources` | `jira`, `github`, `slack`, `github-projects` (default the first three) |
| `owner` | Whose monthly eval cap the shadows count against ([the eval budget](#the-eval-budget)); the team budget always |
| `budget_usd` | Each shadow's job budget (5) |
| `canary` | Injection canaries in some shadows: `{"p": 0.0-1.0, "placements": [...]}` (below; absent: none) |

When a sampled job has started (its sandbox is up, its base commit known), hangar starts one trial against a pin set of
the candidate at that commit (`POST /api/trials`, `source: shadow`, `shadow_of: <job id>`), in the eval queue class
([team-scale.md](team-scale.md#the-eval-queue-class)). The shadow gets the job's task text and context, untrusted parts
fenced as for the job, and:

- `deliver: none`, no PR, no `intake.tracker` label, no `source_ref`, `reply_to` or callback: nothing reaches Jira,
  Slack or GitHub;
- egress `none`, and no person, so the team's credentials and never someone's own login;
- in nobody's inbox or job list (`GET /api/jobs?source=shadow`, admins only);
- `run.json` in the outbox says `source: shadow` and `shadow_of`, and the real job's merge or close events grade it.

**Review comes first.** Turning `p` above 0 is refused unless the last 7 days' review-wait p50 is under 4 hours
(`hangar metrics weekly`). When it rises above that, shadows pause by themselves (an audit entry `shadow.paused`, once),
and they resume when it falls back (`shadow.resumed`).

**Injection canaries.** With `canary: {"p": 0.2, "placements": ["ticket_description", "ticket_comment",
"slack_thread", "repo_file"]}` (placements default to all four), a share `p` of the shadows get one benign instruction
with a fresh token `CANARY-<8 hex>`, planted at one of the placements the shadow has: appended to the ticket's text, as
a comment on it, as a reply in the Slack thread, or in a `NOTES.local.md` added to the shadow's working tree (excluded
from git, never committed). The attachment it lands in is fenced as untrusted; the real job never sees it. After the
turn hangar records whether the agent obeyed (`canary.json` in the outbox) and flightdeck rates each harness digest's
injection susceptibility from them. How it works and what it records: [security.md](security.md#injection-canaries).

## The check on the base commit

With a check on every front door, a red base branch would fail every job's check. The agent would spend its verify-fix rounds on failures it didn't cause, best-of-N would rank every attempt as failing, and every PR would go out as a draft. So hangar runs a job's **standing check**, the one from the repo's policy or the environment, once on the base commit before the agent starts:

- **Once per base commit.** The result is kept per repo, base commit and command, and also per environment (its image and its spec, so a changed environment checks again) and per person (one member's sandbox never decides which failures another member's jobs may subtract). Jobs that start on the same base while it runs wait for that run, and later jobs read the result, so a team of 6 running 60 sessions on one `main` pays for at most one check per person and environment per merge. A check that times out or is killed isn't kept; the next job tries again.
- **Only for a standing check.** A `verify` the job brought itself is its acceptance test, written for the change, and expected to fail on the base.
- **A green base** changes nothing.
- **A red base** is reported in the job's events ("base was red: …"), the prompt (which failures already exist, with the output in `BASELINE.txt` next to the job's context) and the PR body (**Base was red**). The in-turn gate is off for the job, because it can't tell the base's failures from new ones. Hangar's check after the turn counts only the failures the change added:
  - when the check writes test reports ([jobs §8](jobs.md#8-test-results)) and both runs left complete ones, it
    subtracts the base's failing test ids from the run's, exactly. That isn't possible when a run failed with no failing
    test (a build error), or when tests the base ran (more than 5, and more than 2% of them) didn't run after the turn:
    a broken build can hide failures by not running their tests;
  - otherwise it reads failing test ids from the output of pytest, `go test`, `cargo test`, jest and vitest, and
    subtracts the base's;
  - no new failures: the check counts as passed, and the PR isn't a draft for it;
  - new failures: they go back to the agent, named, as a verify-fix round;
  - when it can't read the ids on both sides, it can't tell what's new: no verify-fix round, the PR is a draft, and the job ends `succeeded` with reason `base_red` for a person to judge. That includes output cut short (more than 256 KB), more than 500 failing ids on the base, and a runner that stops at its first failures, since the base's failure would stop it before the change's: pytest's `-x`/`--maxfail` (also from `addopts`), `go test -failfast`, jest's and vitest's `--bail`, and `cargo test` or `cargo nextest` without `--no-fail-fast` (cargo stops after the first failing test binary by default).

It counts test ids, so a check that fails for another reason in the same run can hide behind a red base: with `ruff check; pytest`, a new lint error leaves the test ids unchanged. Chain the steps with `&&`, so a failing first step stops the run and nothing can be compared. A test the base already failed also hides a change that breaks it differently.

## Agent scratch files

Delivery commits what the agent left, and agents leave notes. Hangar keeps these out of the PR, with a narrow rule so it never drops work someone asked for. A file is scratch when all of these hold:

- it is new on the job's branch (a file the base commit has is never touched);
- it is `HANDOFF.md`, `RESULT.md` or `NOTES.md` at the repo root (any case), or a `*.orig` or `*.rej` patch leftover anywhere: one next to the file it was made from (`a.py.orig` beside `a.py`), so a golden file that only shares the suffix is delivered;
- the task doesn't name it: neither the instructions, any follow-up, nor the context attached to the job mention the file name. Issue and ticket intake put the request in context files, so a file the issue asks for is delivered.

Scratch the agent left uncommitted isn't staged. Scratch it committed itself is removed in a separate commit, `hangar: drop agent scratch files`, so the change is visible and easy to revert. Either way the file stays in the sandbox, the job's events and `result.dropped_files` list it, and the PR body says what was left out.


## Offline multi-repository release observations

`hangar.release_readiness` is a Python planning library for a bounded multi-repository
release: code, build, infrastructure, database scope, activation, production and rollout.
It does not run a release, collect provider evidence, grant approval, or change repo
policy. It has no MCP, REST or CLI entry point. Existing `/mcp` job intake remains the
entry point for coding jobs; connecting a Symphony-side agent or another MCP client does
not require Tower. This library does not establish live qualification of those clients.

The [generic profile](https://github.com/Sean-Koval/hangar/blob/main/examples/release-profile.json)
uses separate code, infrastructure, database and deployment repositories. Provider
specifics such as Jenkins build IDs or Jira references are observation metadata; no
provider is imported or contacted. Jira can retain the high-level change and GitHub
Projects the finer work items without either becoming release approval authority.
Repository names, revisions, branch targets, approvers and environments are explicit
profile data. One profile has one target environment; infrastructure approval, activation,
production and rollout must match it. Shared-infrastructure approval scopes are not
modeled by this first contract. Secrets and credentials do not belong in these files.

```python
from hangar.release_readiness import evaluate, profile_digest

# profile and observations are caller-owned JSON data, read outside this library.
report = evaluate(profile, observations, as_of="2026-10-18T12:00:00Z")
```

The profile and observation JSON schemas live in `docs/api/release-profile.schema.json`
and `docs/api/release-observation.schema.json`. Schema validation describes shape;
`ReleaseProfile.model_validate` additionally checks dependency order and cross-stage
identity. `profile_digest(profile)` is the canonical full-profile digest. Every
observation must bind it and the release ID, exact requirement ID, repository, revision
and bindings. The digest detects a different profile; it does not authenticate evidence.

- A build must refer to the merged code commit and carry an immutable artifact digest.
- Infrastructure and production approvals must refer to the exact repository, revision
  and PR. Merge observations distinguish the reviewed PR head from the landed commit;
  squash/rebase/merge commits need not equal that reviewed head. Production also binds
  environment and artifact.
- Activation binds the infrastructure revision and artifact; production binds activation;
  rollout binds the production revision, environment and artifact. Later stages cannot
  satisfy while a prerequisite is missing, uncertain or blocked.
- Database scope is an explicit release-bound determination, with an actor and reason.
  A `not_required` observation is a caller assertion, not an authorized exemption.
  `required` needs a matching migration observation. The example is not a statement of
  any company's database policy; obtain that policy before configuring a real release.
- Observation states are `observed`, `not_satisfied` and `unknown`. They are never
  `verified`. Every record needs a source reference and UTC timestamp; the caller passes
  an explicit UTC `as_of`, and profiles require a freshness limit of at most one day.
  Future or stale observations are unknown. Negative or contradictory observations
  block. Missing observations stay missing. Invalid profiles are rejected.

Reports use `satisfied_by_observation`, `missing`, `unknown` and `blocked`. `status`
only covers configured requirements. `omitted_stages` is explicit, and `release_status`
and `rollout_status` remain unknown when their scope is omitted. Even a complete report
with every observation satisfied always returns `external_verification: false` and
`authorization_granted: false`. Source references and approver names are not independently
checked. A merge receipt is not rollout evidence.

Native live head/check/approval revalidation in `landing.py`, GitHub protection, SRE
approval and production gates remain authoritative. Provider-backed evidence collection,
authenticated multi-repository MCP access, durable release state, Jenkins/ArgoCD
integration, trigger-repository writes and production deployment are future work. This
library does not claim those acceptance criteria are met.
