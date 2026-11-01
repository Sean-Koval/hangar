# AI first-pass review

A repo with `ai_review` set gets a first-pass review of each non-draft agent PR before a person reads it. Turn it on with `hangar repo set acme/app --ai-review hangar`. It is advisory. It never approves, never counts toward [review capacity](team-scale.md#review-capacity), and creates no inbox item.

## Who reviews

- **`hangar`**: a review job (`source: review`) on another harness than the author's: codex for claude-code's PRs, else claude-code, unless `ai_review_harness` names one. Its model is `ai_review_model`, by default `frontier` resolved to a concrete id.
- **`check:<name>`**: an org review bot the team already licenses. hangar waits for a check run or status named `<name>` on the PR's head. `success` is approve; `neutral` and `skipped` are comment; `failure` and `action_required` are blocking; anything else is an error. No job runs; the rest of this page doesn't apply.

With only the author's harness, the review runs on it in a fresh session. It is flagged `independent: false` and doesn't satisfy the [self-approval rule](landing.md#self-approval-who-may-be-the-only-approver).

## How the review job is fenced

The job runs in the PR job's environment with the PR's branch checked out. The PR diff and the review packet go in as untrusted context: agent-written code is input to the reviewer, never instructions. The job delivers nothing (`deliver: none`). Its sandboxes get egress `none`, and its git gateway grant is fetch-only, so it can't push.

Egress `none` needs hangar's egress proxy (Docker's egress network, or `HANGAR_EGRESS_URL` on GKE). Under `HANGAR_INTAKE_POSTURE=enforce` the job needs the git gateway. Where the sandbox can't be fenced, the review fails rather than running open.

## Two passes: find, then confirm

1. **The review pass** returns a verdict, a summary and findings. Each finding has a `path`, a `line`, a `severity` (`info`, `minor`, `major` or `critical`), the `text` and its `evidence`.
2. **The confirmation pass** checks each finding. It runs on the same harness, fenced the same way, on the cheaper `balanced` model policy. It keeps a finding only with one of two kinds of evidence:
    - `command`: a command or test it ran that shows the problem, with the output lines that show it. hangar doesn't rerun it, so the finding is marked unverified.
    - `quote`: a line the PR adds to the finding's own file, within 5 lines of its `line`. hangar checks the diff for it. An invented quote, a removed or context line, or another file's line confirms nothing.

The check reads the reviewed head, not the branch tip. Findings it can't confirm are dropped. If the check doesn't complete or can't start, none is confirmed and the verdict stands.

## Severity, verdict and inline comments

The verdict fails closed. A `blocking` review becomes `comment` only when the check answered every `major` or `critical` finding and confirmed none.

The App posts one PR review with `event: COMMENT`, never `APPROVE`, so it can't satisfy branch protection. It carries at most `ai_review_inline_max` inline comments (default 5, at most 20), the most severe first. The review body lists every confirmed finding, most severe first, with its evidence. Mentions in agent text are neutralized.

The [review packet](landing.md#6-the-review-packet) counts the findings in the body only and those dropped as unconfirmed.

## Spend

Both passes share `ai_review_budget_usd` (default $1.50). The review pass may spend 70% of it; under a cent, no review runs. The confirmation pass gets the rest, if that is at least $0.05. A pass whose cost isn't known counts as its whole cap.

It counts in the PR's person's daily caps, and shows in usage under `source=review` (`hangar usage --by source`).

## When it runs again

A push asks for a review of the new head, at most one every 5 minutes. A draft (a tainted PR opens as one) is reviewed when it is marked ready.

If either pass's job record is lost, the review becomes an error after 10 minutes and the head is reviewed again. The App's review on GitHub is a bot's, so it starts no [auto-fix](jobs/agent-loop.md#pr-auto-fix) round.

## Self-review before a person looks

With `self_review: true` too (`--self-review`), the PR opens as a draft and waits for its review, with no inbox item or review-capacity count. An independent `blocking` review starts one auto-fix round on the job's session, inside its job budget. The review of the round's push ends the wait, whatever it says.

So does a review that isn't blocking or independent, or a round that fails, pushes nothing or can't start. The PR is then marked ready, unless hangar would have opened it as a draft. The packet's **Self-review** line says what happened.
