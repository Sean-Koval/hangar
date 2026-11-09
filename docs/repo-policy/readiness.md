# Is this repo ready?

This page is part of [Repo policy: the defaults every front door applies](../repo-policy.md).

## Is this repo ready?

`hangar repo check acme/app` answers with one report whether a repo is ready for agent work: landing, the git gateway,
CI and the doors (GitHub, Jira, Slack). Each check is `ok`, `warn` or `fail` with the command or setting that fixes
it, and the last line says `ready` or `not ready (N failing)`. It exits 1 when anything fails; warnings don't. `--json`
prints the object `GET /api/repos/{owner}/{repo}/readiness` returns (admin tokens):
`{repo, ready, failing, warnings, summary, checks: [{check, status, detail, fix}]}`.

| Check | `ok` when | Not ok |
| --- | --- | --- |
| `allowlist` | the repo is in `HANGAR_GITHUB_REPOS` | fail |
| `github app` | the GitHub App ([landing.md](../landing.md)) mints an installation token for the repo | fail when it can't (not installed); warn when no App is configured |
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
| `intake posture` | `HANGAR_INTAKE_POSTURE=enforce` ([security/front-doors.md](../security/front-doors.md#intake-posture)) | always ok: `enforce` is the only posture since KAN-74 |
| `git gateway` | the gateway serves the repo's host and answers | fail when it doesn't serve the repo; warn when the control plane can't reach it |
| `credential mode ENV` | each environment the repo's jobs run in resolves to `credential_mode: gateway` | fail when its sandboxes would hold a GitHub token; warn for `direct` while the gateway serves the repo (jobs still fetch through it, people's sessions don't) |
| `sudo ENV` | `sudo: false` | warn under `enforce` (Docker job sandboxes run with no-new-privileges), else fail |
| `heavy ENV` | (shown for Docker on the heavy tier) the policy's `allow_heavy_for_intake` | fail under `enforce`: intake jobs are refused |
| `egress ENV` | an egress mode other than `open` | warn under `enforce` (intake jobs get the allowlist floor, other jobs run open), else fail |
| `credentials ENV` | the agent login a job no person owns would use is an API key | fail for a subscription login (`oauth_token`, `auth_file`) or none ([team.md](../team/subscription-logins.md)) |
| `jira`, `jira transitions KEY` | a Jira project maps to the repo, and its workflow has the trigger status and the `started`, `needs_input`, `delivered` and `failed` statuses | fail for a status the workflow lacks; warn for no project or an unset stage; ok when Jira intake is off |
| `slack` | informational: the channels that start jobs on the repo | warn when Slack is on and no channel maps to it |

The environments are the policy's, those `HANGAR_GITHUB_REPOS` lists for the repo, and any a Jira project mapped to it
names. Nothing is written to GitHub, Jira or Slack: every call is a read, except the App's installation-token mint,
which writes nothing on the repo. GitHub's answers are kept for 5 minutes (landing's cache), so running it again doesn't
spend rate limit, and it needs only the control plane: it works while the sandbox pools are powered down. It fixes
nothing itself; `hangar doctor` covers the checks across every repo, and `hangar doctor --fix` applies the safe ones.

## The check on the base commit

With a check on every front door, a red base branch would fail every job's check. The agent would spend its verify-fix rounds on failures it didn't cause, best-of-N would rank every attempt as failing, and every PR would go out as a draft. So hangar runs a job's **standing check**, the one from the repo's policy or the environment, once on the base commit before the agent starts:

- **Once per base commit.** The result is kept per repo, base commit and command, and also per environment (its image and its spec, so a changed environment checks again) and per person (one member's sandbox never decides which failures another member's jobs may subtract). Jobs that start on the same base while it runs wait for that run, and later jobs read the result, so a team of 6 running 60 sessions on one `main` pays for at most one check per person and environment per merge. A check that times out or is killed isn't kept; the next job tries again.
- **Only for a standing check.** A `verify` the job brought itself is its acceptance test, written for the change, and expected to fail on the base.
- **A green base** changes nothing.
- **A red base** is reported in the job's events ("base was red: …"), the prompt (which failures already exist, with the output in `BASELINE.txt` next to the job's context) and the PR body (**Base was red**). The in-turn gate is off for the job, because it can't tell the base's failures from new ones. hangar's check after the turn counts only the failures the change added:
  - when the check writes test reports ([jobs §8](../jobs/test-results.md)) and both runs left complete ones, it
    subtracts the base's failing test ids from the run's, exactly. That isn't possible when a run failed with no failing
    test (a build error), or when tests the base ran (more than 5, and more than 2% of them) didn't run after the turn:
    a broken build can hide failures by not running their tests;
  - otherwise it reads failing test ids from the output of pytest, `go test`, `cargo test`, jest and vitest, and
    subtracts the base's;
  - no new failures: the check counts as passed, and the PR isn't a draft for it;
  - new failures: they go back to the agent, named, as a verify-fix round;
  - when it can't read the ids on both sides, it can't tell what's new: no verify-fix round, the PR is a draft, and the job ends `succeeded` with reason `base_red` for a person to judge. That includes output cut short (more than 256 KB), more than 500 failing ids on the base, and a runner that stops at its first failures, since the base's failure would stop it before the change's: pytest's `-x`/`--maxfail` (also from `addopts`), `go test -failfast`, jest's and vitest's `--bail`, and `cargo test` or `cargo nextest` without `--no-fail-fast` (cargo stops after the first failing test binary by default).

It counts test ids, so a check that fails for another reason in the same run can hide behind a red base: with `ruff check; pytest`, a new lint error leaves the test ids unchanged. Chain the steps with `&&`, so a failing first step stops the run and nothing can be compared. A test the base already failed also hides a change that breaks it differently.
