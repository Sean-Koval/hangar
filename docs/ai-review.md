# AI first-pass review

A repo with `ai_review` set gets a first-pass review of each non-draft agent PR before a person reads it. Turn it on with `hangar repo set acme/app --ai-review hangar`. It is advisory. It never approves, never counts toward [review capacity](team-scale.md#review-capacity), and creates no inbox item.

## Who reviews

- **`hangar`**: hangar runs a review job (`source: review`) on a different harness from the author's. Codex reviews claude-code's PRs and claude-code reviews everyone else's, unless `ai_review_harness` names one. Its model is `ai_review_model`, by default the `frontier` policy resolved to a concrete id.
- **`check:<name>`**: an org review bot the team already licenses. hangar waits for a check run or status named `<name>` on the PR's head. `success` is approve; `neutral` and `skipped` are comment; `failure` and `action_required` are blocking; anything else is an error. No job runs, and none of the rest of this page applies.

When the environment has only the author's harness, the review runs on it in a fresh session. It is flagged `independent: false` and doesn't satisfy the [self-approval rule](landing.md#self-approval-who-may-be-the-only-approver).

## How the review job is fenced

The job runs in the PR job's environment with the PR's branch checked out. The PR diff and the review packet go in as untrusted context: agent-written code is input to the reviewer, never instructions. The job delivers nothing (`deliver: none`). Its sandboxes get egress `none`, and its git gateway grant is fetch-only, so it can't push.

Under `HANGAR_INTAKE_POSTURE=enforce` it needs the git gateway like any intake job. Egress `none` needs hangar's egress proxy (Docker's egress network, or `HANGAR_EGRESS_URL` on GKE). Where the sandbox can't be fenced, the review job fails rather than running open, and the packet says the review failed.

## Two passes: find, then confirm

1. **The review pass** returns a verdict, a summary and findings. Each finding has a `path`, a `line`, a `severity` (`info`, `minor`, `major` or `critical`), the `text` and its `evidence`.
2. **The confirmation pass** checks each finding. It runs on the same harness, fenced the same way, on the cheaper `balanced` model policy. It keeps a finding only with one of two kinds of evidence:
    - `command`: a command or test it ran in the sandbox that shows the problem, with the output lines that show it. hangar doesn't rerun it, so the finding is marked unverified.
    - `quote`: a line the PR adds to the finding's own file, within 5 lines of its `line`. hangar checks the diff for it. An invented quote, a removed or context line, or another file's line confirms nothing.

The check reads the head the review pass reviewed, not the branch tip. Findings it can't confirm are dropped. If the check fails, times out, can't start or has no budget, no finding is confirmed and the verdict stands. A review with no findings needs no check.

## Severity, verdict and inline comments

The confirmed findings are ranked most severe first. The verdict fails closed. A `blocking` review becomes `comment` only when the check answered every `major` or `critical` finding and confirmed none. A check that didn't complete, or a review with no such finding, leaves it `blocking`.

The App posts one PR review with `event: COMMENT`, never `APPROVE`, so it can't satisfy branch protection. It carries at most `ai_review_inline_max` inline comments (default 5, at most 20), the most severe first. The review body lists every confirmed finding with its evidence. Mentions in agent text are neutralized.

The [review packet](landing.md#6-the-review-packet) counts what didn't go inline. That is the confirmed findings in the body only, and the findings dropped as unconfirmed.

## Spend

Both passes share `ai_review_budget_usd` (default $1.50). The review pass may spend 70% of it; under a cent, no review runs. The confirmation pass gets the rest, and doesn't run with less than $0.05. A pass whose cost isn't known counts as having spent its whole cap.

The spend counts in the daily caps under the PR's person. It shows in usage under `source=review` (`hangar usage --by source`).

## When it runs again

The result is recorded on `landing.ai_review` with the head it reviewed. A push asks for a review of the new head, at most one every 5 minutes. A draft (a tainted PR opens as one) is reviewed when it is marked ready.

If either pass's job record is lost, the review becomes an error after 10 minutes and the head is reviewed again. The App's review is a bot's, so it starts no [auto-fix](jobs/agent-loop.md#pr-auto-fix) round.
