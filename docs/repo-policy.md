# Repo policy: the defaults every front door applies

Work reaches hangar through many doors: MCP, REST, the CLI, GitHub issues, Slack, inbound webhooks and schedules. A **repo policy** is one record per repository with the defaults its jobs get, whichever door they came through. A new kind of intake only has to name the repo.

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
hangar repo set acme/app --branch-template 'feature/{ticket_key}-{job}' --require-ticket \
  --title-template '{ticket_key}: {title}'                                               # company naming
hangar repo check acme/app          # is it ready for agent work? (below)
```

| Field | What it sets |
| --- | --- |
| `verify` | The repo's check (see [jobs §7](jobs/agent-loop.md)). `""` means no check, even if the environment has one. |
| `environment` | The environment jobs run in when they don't name one. For a repo in `HANGAR_GITHUB_REPOS`, it must be one of the environments listed there. |
| `harness` | The default harness. It must be enabled in the environment; when a job runs in an environment without it, the job uses the environment's default and its events say why. |
| `model` | One model id, or one per harness (`{"claude-code": "opus", "codex": "gpt-5.5"}`), as in an environment's `model:`. A single id applies only to the policy's harness, or, without one, to jobs that didn't pick a harness: a model id means nothing to another harness. A policy (`frontier`, `balanced`) applies to every harness and is resolved to a concrete id when the job starts ([harnesses.md](harnesses/models.md)). |
| `budget` | The default per-turn budget (`max_minutes`, `max_tokens`, `max_cost_usd`), field by field under the job's own. |
| `job_budget` | The default [job budget](repo-policy/budgets.md#budgets) (`max_cost_usd`, `max_tokens`, `max_turns`). |
| `max_usd_per_day` | The repo's [daily spend cap](repo-policy/budgets.md#budgets). |
| `credentials` | Whose login the repo's jobs run with: `person` (the person the job runs for; a job for no one fails), `team`, or `person_then_team` (the default: theirs when they have one, else the team's). See [team.md](team.md#whose-login-a-job-uses). |
| `egress` | `{mode, presets, allow}` for the repo's sandboxes: adds presets and hosts to the environment's allowlist, and can tighten its mode but never widen it (`hangar repo set acme/app --egress-mode none --egress-allow docs.example.com`). See [egress allowlists](security/egress.md). |
| `build_secrets` | `{SECRET_ID: credential}` for the repo's [environment builds](environments/build-secrets.md#build-secrets), winning over the environment's (admins; `PUT /api/repos/policy`). See [build secrets](security/credentials.md#build-secrets). |
| `land` | `auto_after_approval`: an approval of the PR's head by an approver or code owner, with green required checks, lands it ([landing.md](landing.md)). `manual` or unset: a person merges. |
| `approvers` | GitHub logins whose approval lands the repo's agent PRs (`--approver`, repeated). |
| `merge_method` | `squash` (default), `merge` or `rebase`, for PRs hangar lands. |
| `risk_paths` | Globs (`infra/**`) that make a PR high risk in its [review packet](landing/review.md#6-the-review-packet), on top of `.github/**` and `.hangar/**`. |
| `diff_budget_lines` | Changed lines (insertions plus deletions) above which the [review packet](landing/review.md#6-the-review-packet) warns that the PR is big (`--diff-budget-lines`). Unset: no warning. |
| `require_stale_dismissal` | `false` lets hangar arm auto-merge on a base branch that doesn't dismiss stale approvals on a push. Unset (the default) it refuses ([landing.md](landing.md#3-the-branch-protection-to-enable)). |
| `verify_reports` | The JUnit, Cobertura or LCOV files `verify` writes, relative to the repo, `**/` for any depth (`--verify-report 'build/**/TEST-*.xml'`, repeated). The repo's `.hangar/environment.yaml` `verify_reports` comes first. Most runners need none: see [jobs §8](jobs/test-results.md#where-the-reports-come-from). |
| `rerun_flaky` | `true`: a check whose every failure is on the repo's [flaky list](jobs/test-results.md#flaky-tests) runs once more, and the rerun decides (`--rerun-flaky`). Unset: a failure stands. |
| `heavy` | Docker on the [heavy tier](environments/heavy-tier.md), **deny by default**. `on`: the repo's sandboxes run on it, with Docker inside (`--heavy on`). `allow`: the repo's `.hangar/environment.yaml` may ask for it (`heavy: true` or `tier: heavy`), and the request is honored (`--heavy allow`). `deny`, or unset: a request in the repo's file isn't honored; the sandbox runs on the default tier, and the agent's instructions and `hangar-agent env doctor` say Docker isn't enabled for the repo and give this command. A heavy environment (an admin's `heavy: true`) has Docker whatever this says. `true` and `false`, sent by an older client, mean `on` and `deny`. |
| `profile` | `standard`, `large` or `xlarge`: the size of the repo's sandboxes ([profiles](environments/heavy-tier.md#what-decides)). Large and xlarge run on the heavy tier. |
| `max_profile` | The largest profile a job (label `hangar.profile`, `hangar job submit --profile`) or the repo's file may ask for. Unset: no more than `profile` or the environment's own. |
| `self_approval` | Whether the job's own person may be its PR's only approver ([landing.md](landing/how-a-pr-lands.md#self-approval-who-may-be-the-only-approver)): `low_risk_with_ai_review` (unset: the default; a low-risk PR with an independent AI review of the approved head), `never`, or `any` (`--self-approval`). It never loosens the taint gate: a tainted PR always needs someone other than its person. |
| `ai_review` | The [AI first-pass review](landing/review.md#6a-ai-first-pass-review) of the repo's agent PRs: `hangar` (a review job on a different harness from the author's), `check:<name>` (an org review bot's check run or status on the head) or `off` (unset) (`--ai-review`). |
| `ai_review_harness` | The reviewing harness. Unset: codex for a claude-code PR, else claude-code (`--ai-review-harness`). |
| `ai_review_model` | The reviewer's model or policy. Unset: `frontier`, resolved to a concrete id when the review is submitted (`--ai-review-model`). |
| `ai_review_budget_usd` | What one review may spend, its confirmation pass included. Unset: $1.50 (`--ai-review-budget`). |
| `ai_review_inline_max` | The most inline comments one review posts, most severe confirmed findings first. Unset: 5; at most 20 (`--ai-review-inline-max`). |
| `self_review` | With `ai_review: hangar`, the PR opens as a draft and waits for its review ([ai-review.md](ai-review.md#self-review-before-a-person-looks)). A blocking one starts one auto-fix round first, inside the job budget. Unset: false (`--self-review`). |
| `max_open_agent_prs` | [Review capacity](team-scale.md#review-capacity): open agent PRs awaiting a person before their new jobs on this repo wait as `awaiting_capacity`; `0` turns it off. Unset: `HANGAR_PERSON_MAX_OPEN_AGENT_PRS` (5) (`--max-open-agent-prs`). |
| `verify_allowed` | Globs (`make test*`, `pytest tests/*`) a ticket's own *Verify command* ([agent tickets](intake.md#agent-tickets)) must match to become that job's `verify` (`--verify-allowed`, repeated). Unset: a ticket's command is only ever a suggestion to the agent, and the job's check stays the policy's or the environment's. A matched command is the job's own check (`verify_source: job`), so the [base check](repo-policy/readiness.md#the-check-on-the-base-commit) is skipped for it, as for any acceptance test a job brings. `*` alone is refused. |
| `overnight` | `true`: the repo's intake jobs (Jira, GitHub, Slack) run at night in the overnight lane instead of waiting for 10:00 (`--overnight`; [gke.md](gke/power.md#the-overnight-lane), #186). A job from any repo can ask for it with the label `lane: overnight`. |
| `branch_template` | The repo's job branches, e.g. `feature/{ticket_key}-{job}`. It must contain `{job}`, or the set is refused and nothing changes. Unset: `hangar/<job>` (`--branch-template`; `''` clears it). See [change sets](changesets.md#company-conventions-in-the-repo-policy). |
| `require_ticket` | `true` refuses a job for the repo without a `ticket` (`jira:PAY-412`) (`--require-ticket`, `--no-require-ticket`). |
| `title_template` | The title of the repo's agent PRs, e.g. `{ticket_key}: {title}` (`--title-template`; `''` clears it). Placeholders: `{job}`, `{ticket}`, `{ticket_key}`, `{title}`, `{role}`. |
| `best_of_n` | `{sequential, escalate_at, max_n}` for best-of-N jobs. Attempt 1 runs alone first, may use `escalate_at` of the job budget, and `max_n` caps attempts per risk tier. See [sequential best-of-N](jobs/sequential-best-of-n.md). |

The repo is `owner/repo` for GitHub (any URL form, any case), or the git URL for another host.

## Precedence

For each field: **the job, then the repo's own `.hangar/environment.yaml`, then the repo's policy, then the
environment, then hangar's settings.**

- A job's own `verify`, `environment`, `harness` or `model` always wins.
- A repo's `.hangar/environment.yaml` can set its `verify` and, with `extends`, its environment (within the allowlist).
  hangar uses the file as the default branch had it at the last clone. See [environments.md](environments.md).
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
| `GET /api/repos/{owner}/{repo}/readiness` | The [readiness report](repo-policy/readiness.md#is-this-repo-ready) for a GitHub repo, the same object `hangar repo check --json` prints. |

Policies are stored in hangar's database, written durably like `hangar config` settings, and apply to the next job without a restart. Every change is audited (`repo.policy.set`, `.unset`, `.delete`).

## Agent scratch files

Delivery commits what the agent left, and agents leave notes. hangar keeps these out of the PR, with a narrow rule so it never drops work someone asked for. A file is scratch when all of these hold:

- it is new on the job's branch (a file the base commit has is never touched);
- it is `HANDOFF.md`, `RESULT.md` or `NOTES.md` at the repo root (any case), or a `*.orig` or `*.rej` patch leftover anywhere: one next to the file it was made from (`a.py.orig` beside `a.py`), so a golden file that only shares the suffix is delivered;
- the task doesn't name it: neither the instructions, any follow-up, nor the context attached to the job mention the file name. Issue and ticket intake put the request in context files, so a file the issue asks for is delivered.

Scratch the agent left uncommitted isn't staged. Scratch it committed itself is removed in a separate commit, `hangar: drop agent scratch files`, so the change is visible and easy to revert. Either way the file stays in the sandbox, the job's events and `result.dropped_files` list it, and the PR body says what was left out.

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="is-this-repo-ready"></span>Is this repo ready? | [Is this repo ready?](repo-policy/readiness.md#is-this-repo-ready) |
| <span id="the-check-on-the-base-commit"></span>The check on the base commit | [Is this repo ready?](repo-policy/readiness.md#the-check-on-the-base-commit) |
| <span id="budgets"></span>Budgets | [Budgets and shadows](repo-policy/budgets.md#budgets) |
| <span id="the-eval-budget"></span>The eval budget | [Budgets and shadows](repo-policy/budgets.md#the-eval-budget) |
| <span id="shadows"></span>Shadows | [Budgets and shadows](repo-policy/budgets.md#shadows) |
| <span id="offline-multi-repository-release-observations"></span>Offline multi-repository release observations | [Offline multi-repository release observations](repo-policy/release-observations.md) |
