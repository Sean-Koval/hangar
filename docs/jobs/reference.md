# Jobs reference: labels, outcomes and settings

Failure labels, outcome classes, every setting, tainted jobs and the security notes. Part of [jobs](README.md).

## Failure labels

In the Friday harness review ([runbook](../runbooks/harness-review.md), #169), the team labels where failed and abandoned
jobs first went wrong:

```sh
hangar review queue [--since 7d]
hangar review label <job> --seq N --category env --fix env --note "…" [--task]
hangar review status
```

`GET /api/review/queue`, `POST /api/jobs/{id}/failure-label` and `GET /api/review/status` are the REST equivalents.
A job has one `failure_label` (`seq`, `category`, `fix`, `note`, `candidate_task`, `labelled_by`, `at`), shown on
the job view and the trace (`hangar.failure.*`). Relabelling replaces it and is audited. Each label is also a `label`
job event in the outbox for flightdeck, and a labelled job's runs are kept until exported.

## Outcome classes

Every agent turn, best-of-N attempt, experiment trial and job (`result.outcome_class`, set as it ends) carries an
`outcome_class` beside its outcome, so infrastructure trouble and agent failure no longer look alike:

| Class | Means | Counted |
| --- | --- | --- |
| `agent` | the agent's own result, success or failure (an agent that exits 1 after editing files; one that crashed its own sandbox) | yes |
| `infra_error` | infrastructure ended it; `infra_reason` says what: `preempted`, `power_down`, `rate_limited`, `provider_5xx`, `sandbox_create`, `checkpoint` (a restore failed), `control_plane_restart`, `drift` (a trial's turn 1 didn't run its pin set, #178), `no_login` (the turn's harness had no login, so hangar refused the turn before starting it, #293) | no: trials are retried ([experiments](experiments.md#infrastructure-errors-are-retried-and-left-out)); weekly yield leaves such jobs out |
| `cancelled` | someone (or an experiment) stopped it | no |
| `human` | a person stepped in: an interrupt, or the agent asked a question | no (a job that then finishes is `agent`) |

Records from before this have no class: unknown, never `agent`. hangar errs towards `agent`: a turn is `infra_error`
only on a recorded node event (the reconciler's `pod_replaced`: node repair, scale-down, preemption), the power-down,
a control-plane restart, a failed sandbox create or checkpoint restore, or the harness's own error report of a
provider error (HTTP 429 after its retries, a 5xx, `overloaded_error`) on a run that failed. What a tool printed never
counts, and a sandbox that died with no such record (a fork bomb, the agent's process killed for memory) stays
`agent`. Jobs aren't retried beyond today's restart and power-down resume; the class is recorded. Turn spans carry it
as `hangar.outcome.class` and `hangar.outcome.infra_reason` ([traces.md](../api/traces.md#turn)).

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_JOB_CONCURRENCY` | 0 (auto) | Jobs provisioning or running at once. Auto: 4 on Docker, else `HANGAR_MAX_SANDBOXES` / 5 between 4 and 8 ([team-scale.md](../team-scale.md)) |
| `HANGAR_PERSON_MAX_RUNNING_JOBS` | 0 (half the slots, at least 4) | Jobs one person runs at once; the queue serves whoever has the fewest running first |
| `HANGAR_PERSON_MAX_OPEN_AGENT_PRS` | 5 (0 = off) | Open agent PRs awaiting a person before their new jobs wait as `awaiting_capacity`; see [team-scale.md](../team-scale.md#review-capacity) |
| `HANGAR_JOB_TIMEOUT_MINUTES` | 60 | Per agent turn |
| `HANGAR_JOB_KEEP_MINUTES` | 120 | Sandbox kept after a turn |
| `HANGAR_JOB_VERIFY_TIMEOUT_SECONDS` | 1800 | |
| `HANGAR_JOB_MAX_CONTEXT_BYTES` / `_ITEMS` | 2 MB / 50 | |
| `HANGAR_JOB_CALLBACK_HOSTS` | `[]` | Hosts allowed for http or internal callbacks |
| `HANGAR_JOB_RESTART_RESUMES`, `HANGAR_JOB_RESTART_STAGGER_SECONDS` | 2, 20 | How often a job a restart interrupted resumes on its own, and how far apart such jobs re-enter the queue (§5) |
| `HANGAR_BUDGET_MAX_MINUTES` / `_MAX_TOKENS` / `_MAX_COST_USD`, `HANGAR_MODEL_PRICES` | unset | Default budget per turn and the price table; see [activity.md](../activity.md) |
| `HANGAR_JOB_BUDGET_MAX_COST_USD` / `_MAX_TOKENS` / `_MAX_TURNS` | unset | Default budget for a whole job; see [repo-policy.md](../repo-policy.md#budgets) |
| `HANGAR_BUDGET_DAY_TIMEZONE` | `UTC` | The IANA zone whose midnight starts a day for daily spend caps, e.g. `America/New_York` |
| `HANGAR_PERSON_MAX_USD_PER_DAY` | unset | What one person's sessions and jobs may spend in a day before their new jobs wait for approval; see [repo-policy.md](../repo-policy.md#budgets) |
| `HANGAR_TRIAL_INFRA_RETRIES` | 2 | How often an experiment trial that ended in `infra_error` runs again; an experiment's retries together stop at 10% of its trials ([experiments](experiments.md#infrastructure-errors-are-retried-and-left-out)) |
| `HANGAR_EVAL_WINDOW` | `20:00-00:15` | When eval jobs (experiment trials) may start, `HH:MM-HH:MM` (may cross midnight) or `always` ([experiments](experiments.md#the-eval-queue)) |
| `HANGAR_EVAL_TZ` | `America/New_York` | The eval window's time zone |
| `HANGAR_EVAL_MAX_RUNNING` | 0 (half the job slots) | Eval jobs running at once |
| `HANGAR_EVAL_TPM_BUDGET` | unset (no cap) | Tokens per minute eval admission keeps each provider under: `400000` or `anthropic=400000,openai=300000` |
| `HANGAR_EVAL_TPM_PER_TRIAL` | 20000 | One trial's tokens per minute until recent eval turns on that provider give a median |
| `HANGAR_TRIAL_SENTINEL` | `[]` (off) | Up to 5 `{pin_set, task, n, grader?, budget}` the trial cache's drift sentinel re-runs, bypassing the cache ([api/trials.md](../api/trials.md#the-cache)) |
| `HANGAR_TRIAL_SENTINEL_SCHEDULE` | `0 20 * * *` | When it runs, in `HANGAR_EVAL_TZ` |
| `HANGAR_BEST_OF_N_MAX` | 8 | Most attempts per best-of-N job |
| `HANGAR_BEST_OF_N_PUSH_ATTEMPTS` | false | Also push attempts that weren't delivered |
| `HANGAR_BEST_OF_N_JUDGE_TIMEOUT_MINUTES` | 15 | The judge turn's limit |
| `HANGAR_VERIFY_IN_LOOP`, `HANGAR_MAX_VERIFY_ROUNDS` | true, 3 | The verify gate inside the turn; see [the agent loop](agent-loop.md) |
| `HANGAR_TEST_REPORTS` | `inject` | `inject`: set the reporter variables and read the reports; `collect`: only read them; `off`. See [test results](test-results.md) |
| `HANGAR_GITHUB_AUTOFIX`, `HANGAR_AUTOFIX_MAX_ROUNDS`, `HANGAR_AUTOFIX_COOLDOWN_SECONDS`, `HANGAR_AUTOFIX_COALESCE_SECONDS`, `HANGAR_AUTOFIX_LOG_BYTES` | `{}`, 3, 120, 60, 16000 | PR auto-fix; see [the agent loop](agent-loop.md) |
| `HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL`, `HANGAR_GITHUB_REPOS`, `HANGAR_GITHUB_TRIGGER_LABEL` | | See §4 |
| `HANGAR_GITHUB_TRUSTED`, `HANGAR_JOB_APPROVAL_TTL_HOURS`, `HANGAR_INBOUND_WEBHOOKS`, `HANGAR_SCHEDULE_POLL_SECONDS` | | See [triggers.md](../triggers.md) |
| `HANGAR_SLACK_SIGNING_SECRET_CREDENTIAL`, `HANGAR_SLACK_BOT_TOKEN_CREDENTIAL`, `HANGAR_SLACK_CHANNELS`, `HANGAR_SLACK_USERS` | | See [slack.md](../slack.md) |
| `HANGAR_JIRA`, `HANGAR_GITHUB_PROJECTS` | `{}` | See [intake.md](../intake.md) |
| `HANGAR_HARNESS_REPO`, `HANGAR_HARNESS_DRIFT` | unset, `warn` | The harness registry `harness_lock` resolves against, and what turn 1's drift from the lock does (`warn` or `fail`); see [harness-lock.md](../harness-lock.md#thaw-and-drift) |
| `HANGAR_SUBSCRIPTION_LOGINS` | `interactive` | Jobs never use a subscription login (`oauth_token`, `auth_file`): they need an API key. `any` allows them, with a warning, and `hangar doctor` fails. See [team.md](../team.md#subscription-logins) |

## Tainted jobs

A job whose agent read untrusted content (its input was untrusted, or it fetched a page, or called an untrusted MCP
server: [security/taint.md](../security/taint.md)) delivers its PR as a **draft**, whatever `deliver` says, labelled
`hangar:tainted`, and the timeline says so. Landing then needs a reviewer other than the job's own person
([landing.md](../landing.md#tainted-prs)). `deliver: branch` is unchanged: hangar pushes `hangar/<job>` after its checks.

## Security notes

Issue text, comments and service context are untrusted input to the agent. Today's mitigations:

- the repo and environment allowlist for service tokens and GitHub triggers;
- untrusted triggers wait for approval, only trusted people's comments become context, and an untrusted author's text is marked as untrusted in the prompt ([triggers.md](../triggers.md));
- `jobs`-only service tokens that can't reach MCP or OAuth;
- secret redaction of everything the agent writes that leaves hangar (summaries, verification output, PR bodies, comments, callbacks);
- pushes only to new `hangar/` branches, never forced;
- a prompt that marks context as data;
- CI output, logs and review comments handed to auto-fix turns are untrusted attachments, fenced in the prompt like an outsider's issue text, and only reviewers the job's person or the repo policy trusts start a turn;
- the [intake posture](../security/front-doors.md#intake-posture) (`HANGAR_INTAKE_POSTURE`, default `enforce`): a job from GitHub, GitHub Projects, Jira, Slack or a webhook, or one with an untrusted author or attachment (an auto-fix round), needs the git gateway (otherwise `submit` refuses it, and the front door posts why) and runs with at least `allowlist` egress, in a cold-started sandbox when its environment is `open`. Jobs a person submits over MCP, REST or the CLI are unaffected. `warn` runs them as before for one release, with a `posture.warn` job event and audit entry.

Known gap: the callback check resolves the host before sending but doesn't pin the address, so DNS rebinding is not fully blocked. Without the [git gateway](../security/git-gateway.md) the agent's sandbox holds a git credential, so restrict `HANGAR_GITHUB_REPOS` to repos where a mistaken push to a `hangar/*` branch is acceptable, and keep PR merges human-approved. With `HANGAR_GATEWAY_URL` set, a job's sandbox has no GitHub token and pushes nothing: its grant is fetch-only, and hangar pushes the commit it checked, after verify, the secret scan and the landing checks ([security/git-gateway.md](../security/git-gateway.md#jobs-only-fetch-hangar-delivers)).
