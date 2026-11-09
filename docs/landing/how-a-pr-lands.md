# How a PR lands

This page is part of [Landing: an approval lands the PR](../landing.md).

<figure class="af-figure af-figure--wide">
--8<-- "landing-states.svg"
<figcaption>How a PR lands: open, approved, armed, merged; a push, changes requested, a dismissed approval or a new agent turn takes it back to open</figcaption>
</figure>
<figure class="af-figure af-figure--stacked">
--8<-- "landing-states-stacked.svg"
<figcaption>How a PR lands: open, approved, armed, merged; a push, changes requested, a dismissed approval or a new agent turn takes it back to open</figcaption>
</figure>

- **Only the approved commit.** hangar arms with `enablePullRequestAutoMerge(expectedHeadOid: <approved sha>)`. GitHub has no REST route for auto-merge, so this is GraphQL. When the PR is already mergeable, GitHub refuses to arm ("Pull request is in clean status") and hangar merges it with `PUT /pulls/{n}/merge` and `sha: <approved sha>`. Either call fails if the head moved.
- **A push clears the approval.** Any new head, from the agent, auto-fix, a follow-up or a person, clears the approval in hangar's record and disarms. A later push needs a new review, which "dismiss stale approvals" and "most recent push" enforce on GitHub's side.
- **No agent runs while armed.** Before arming, the job's session takes a checkpoint and its sandbox goes, and with it the sandbox's gateway grant, so nothing the agent left running can push. Before any later agent turn on the job (a follow-up, an auto-fix round, a verify-fix turn), hangar disarms; if it can't, the turn doesn't start. After a turn that pushed nothing, the approval still covers the head and hangar arms again. Arming also waits for any queued or running turn on the job.
- **Checks.** "Green" means every check the base branch requires (from its protection summary and rulesets) passed on the head: `success`, `neutral` or `skipped`. GitHub saying `mergeable_state: clean` also counts, in case a webhook was missed. A failed check holds landing even when it isn't required: an agent's red PR goes to [auto-fix](../jobs/agent-loop.md), not to main. With no required checks, GitHub merges as soon as auto-merge is armed, so make CI required (`hangar doctor` warns).
- **Requests for changes win.** A "changes requested" review from anyone, or a dismissed approval, takes the PR back to open and disarms.
- **GitHub's record decides.** Review webhooks can arrive late, out of order (hangar redelivers missed ones after a restart) or replayed, so right before arming hangar lists the PR's reviews (`GET /pulls/{n}/reviews`, Pull requests: read): the approver's latest review must still be an approval of the head, and nobody's latest may request changes. A push, dismissal or follow-up that arrives while GitHub is arming is caught after the arm and disarmed; a disarm GitHub refused is tried again after the job's next turn and on restart.
- **The merge commit says which harness wrote it** (#137). The message hangar gives GitHub is the agent's summary
  followed by the job's [trailers](../jobs/working-with-the-agent.md#the-pull-request) (`Hangar-Job`, `Harness-Digest`, `Environment-Digest`,
  `Harness-Lock`): `commitBody` when it arms auto-merge, `commit_message` when it merges an already mergeable PR, so
  the merged commit carries them whichever way it lands. That holds for the `squash` and `merge` methods; `rebase`
  keeps the agent's commits as they are, so the trailers are in the PR body only. A metric that reads them from the
  PR body through the API doesn't depend on the method.
- **Drafts wait.** A PR hangar opened as a draft (the check failed, the agent changed the check's own tests, or it read untrusted content) isn't armed until someone marks it ready for review.

## Tainted PRs

When the job's agent read untrusted content (`hangar:tainted`, [security/taint.md](../security/taint.md#taint-gates)), the approval
that lands it must come from someone other than the job's own person and its requester: a reviewer whose GitHub
account is linked to the job's person, or who asked for the job, doesn't land it (the job's timeline says why), and
another approver or code owner does. The rule is `approvals.can_decide(kind="land")`, so
`HANGAR_SELF_APPROVAL=allow_solo` lets a one-person install land its own. The review packet lists what untrusted
content was read. Each decision is audited as `taint.gate` (`sink: land`).

## Agent-drafted ADRs

A PR from [`@hangar adr`](../slack.md#hangar-adr-a-thread-becomes-a-decision-record) (labels `agent-drafted` and `adr`)
follows the tainted-PR rule even if nothing tainted it: the job's own person and its requester can't be the approval
that lands it, whatever `self_approval` says (`any` included); another approver or code owner can. The packet's
Landing rule line says so, and an ADR line gives the thread, the content hash, a warning when the PR touches files
outside `docs/decisions/`, and any other open ADR job that took the same number.

**Loosening a decision's checks is high risk.** For any PR, not only these: removing or changing a line inside an
existing ADR's `checks:` block (or deleting the ADR) makes the packet's risk `high`, naming the file, so the
self-approval rule needs a second reviewer. Adding a check doesn't.

## Self-approval: who may be the only approver

The job's own person (their GitHub account, matched by its numeric user id through `hangar person link`, never by
login) can approve their agent's PR on GitHub. That approval lands it only under the repo policy's `self_approval`:

| `self_approval` | The requester's own approval lands the PR when |
| --- | --- |
| `low_risk_with_ai_review` (default) | the PR is **low** risk (docs and tests only, [the packet's tier](review.md#6-the-review-packet)) **and** an [independent AI review](review.md#6a-ai-first-pass-review) of the approved head exists with a verdict other than `blocking` |
| `never` | never: a second reviewer always approves |
| `any` | always (the behaviour before #160) |

Otherwise the approval is recorded but doesn't arm anything: the job's timeline and `landing.note` say why ("self-approval doesn't land this PR (risk normal): it needs a second reviewer"), `pr_state` stays `open`, the audit log has `landing.self_approval` (`decision: refused`), and the [inbox](review.md#7-the-inbox-what-needs-you) gives the repo's other approvers (the people linked to the policy's `approvers`; admins when none is linked) a `review` item. Another approver's or code owner's approval lands it as usual. An AI review of an older head stops counting after a push; when the review of the approved head arrives after the approval, hangar looks at that approval again.

**When it applies, precisely.** Only to a *person's* job (team jobs from GitHub issues, Slack or webhooks have no person and are unchanged) and only where an approval lands the PR (`land: auto_after_approval`). It runs **after** the [taint gate](#tainted-prs), which has already refused the job's own person and requester on a tainted PR, whatever `self_approval` says (low risk and an AI review included; `any` too). So every Jira-door job, tainted from turn 1 by its ticket text, still needs a second human. The rule can only refuse more, never allow what the taint gate refused. As for every other approval, `HANGAR_SELF_APPROVAL=allow_solo` lets the one person of a one-person install land their own. GitHub's "require approval of the most recent reviewable push" still applies on its side.

Everything that needs GitHub runs in the background, under a per-job lock, so a webhook is answered at once. hangar's record lives on the job, so a restart loses nothing: approved PRs are evaluated again and merged PRs' sessions ended at startup. Deliveries missed while hangar was down are [redelivered](../upgrading.md).

## What hangar records

`job.landing` (in `GET /api/jobs/<id>`, `get_job` and `hangar://jobs/<id>`):

| Field | |
| --- | --- |
| `pr_state` | `open`, `approved`, `merged` or `closed` |
| `approved_by`, `approved_sha`, `approved_at` | the approval that counts |
| `armed_sha`, `armed_at` | auto-merge armed by hangar, for that commit |
| `checks`, `checks_sha` | each check run, suite or status context on the head and its conclusion |
| `ci_green_at`, `ci_green_sha` | when the required checks first all passed on that commit |
| `merged_at`, `merged_by`, `closed_at` | from GitHub |
| `green_to_merge_seconds` | from `ci_green_at` to `merged_at`, when the merged head is the one that went green |
| `files`, `risk` | what the PR touches, and its tier |
| `note` | why it isn't landing yet |
| `ai_review` | the [AI first-pass review](../ai-review.md): `{source, job, confirm_job, harness, model, head_sha, verdict, findings, unconfirmed, inline, summary, independent, at, review_id, url}`. `findings` are the confirmed ones, each with `severity` and `evidence`. `verdict` is `null` while pending and `error` when there was none |
| `approver_people`, `needs_second` | the hangar people linked to the policy's approvers, and whether the requester's own approval was refused ([self-approval](#self-approval-who-may-be-the-only-approver)) |
| `opened_at`, `ready_at` | when hangar opened the PR; when it was opened non-draft or marked ready (#168) |
| `first_review_at`, `first_review_by` | the first review a human submitted, any state; bots, the App's AI review included, never count (#168) |
| `merge_commit_sha` | what the merge put on the base branch: reverts of a squash merge are matched against it (#168) |
| `abandon_reason`, `abandon_by`, `abandon_at` | why a PR closed unmerged was abandoned (below) |
| `reverted_at`, `revert_sha`, `fix_forward_pr`, `fix_forward_at` | a revert of it on the default branch, or a PR that fixed it forward, found by the nightly pass ([metrics.md](../metrics.md)) |

**Abandon reasons** (#168). When you close an agent PR without merging it, say why: comment `/hangar abandon <reason>` on it (a trusted person), or add the label `agent-abandon:<reason>`. The reason is one of `wrong_approach`, `bad_ticket`, `flaky_env`, `too_big`, `superseded` or `other`; anything else is refused with the list. The inbox lists closed agent PRs without one. The weekly abandon rate counts them by reason ([metrics.md](../metrics.md)).

The audit log has `landing.approved`, `landing.arm`, `landing.disarm`, `landing.merge`, `landing.merged`, `landing.self_approval` and `landing.abandon_reason`.

**The end of the session.** A merged or closed PR ends the job's session: it takes a final checkpoint, then the sandbox is released (a paused hot-resume sandbox too). A turn still running when the PR closes is stopped first. A job still waiting on a question is cancelled, because nobody needs the answer any more. If the checkpoint fails, the sandbox stays until it idles out, so no work is lost.

**Schedules update their own PR.** When a schedule's last job still has an open PR, the next run is a follow-up on that job: the agent updates the PR instead of opening another each week. Once that PR is merged or closed, the next run is a new job. If the old job can't take a follow-up (no checkpoint, budget spent), a new job runs and the old job's events say why.
