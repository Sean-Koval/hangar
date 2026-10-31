# Weekly adoption metrics

The team's weekly numbers are defined once, in flightdeck: **[flightdeck docs/metrics.md](https://sean-koval.github.io/flightdeck/metrics/)**
(Sean-Koval/flightdeck#6). This page does not restate them. It says what hangar records, how hangar computes the same numbers for the standup line, and where the two differ.

| Metric | Definition | hangar's field in `GET /api/metrics/weekly` |
| --- | --- | --- |
| Agent PR yield | [#yield](https://sean-koval.github.io/flightdeck/metrics/#yield) | `yield` (`merged`, `terminal`) |
| Agent share of merged PRs | [#agent-share](https://sean-koval.github.io/flightdeck/metrics/#agent-share) | `agent_share` |
| Review wait | [#review-wait](https://sean-koval.github.io/flightdeck/metrics/#review-wait) | `review_wait` (p50, p90 hours) |
| Green to merge | [#green-to-merge](https://sean-koval.github.io/flightdeck/metrics/#green-to-merge) | `green_to_merge` (p50, p90 hours) |
| Human steers per merged PR | [#steer-rate](https://sean-koval.github.io/flightdeck/metrics/#steer-rate) | `steer_rate` (`corrections` once steers are classified, #162) |
| Abandon rate | [#abandon](https://sean-koval.github.io/flightdeck/metrics/#abandon) | `abandon_rate` (`by_reason`, `unlabelled`) |
| 14-day revert rate | [#revert](https://sean-koval.github.io/flightdeck/metrics/#revert) | `revert_rate` (the cohort merged two weeks earlier) |
| Cost per merged PR | [#cost-per-merged-pr](https://sean-koval.github.io/flightdeck/metrics/#cost-per-merged-pr) | `cost_per_merged_pr` |
| Active delegators | [#active-delegators](https://sean-koval.github.io/flightdeck/metrics/#active-delegators) | `active_delegators` (`value` of `of`) |
| Reviewer attention per merged agent PR | hangar's own (KAN-70): [reviewer attention](#reviewer-attention) | `reviewer_attention` |

hangar follows flightdeck's conventions:

- Weeks are ISO weeks in UTC.
- An agent job is delegated work. Experiment trials and review jobs are not agent jobs.
- Terminal means merged, closed unmerged, abandoned, or (with no PR) ended.
- Counted means the job ran a turn and its last run isn't `infra_error`. Infra errors are reported as `excluded_infra_error`.

hangar computes no intervals and no trends. Flightdeck does that over the outbox's Parquet. The team reads flightdeck's report (`reports/weekly/latest.json`, `flightdeck.weekly/v1`) when it exists. hangar's numbers exist so the standup line works from week 1, and with flightdeck off.

## What hangar records for them

On `job.landing` ([landing.md](landing.md#what-hangar-records)):

| Field | Meaning |
| --- | --- |
| `opened_at` | When hangar opened (or found) the PR. |
| `ready_at` | When the PR was opened non-draft or marked ready. Review wait starts here. |
| `first_review_at`, `first_review_by` | The first review a human submitted, in any state. Bots never count, including the App's own AI review (#161) (`reviewtrust.is_bot`). |
| `merge_commit_sha` | What the merge put on the base branch (a squash merge's own commit). |
| `abandon_reason`, `abandon_by`, `abandon_at` | Why a closed PR was abandoned: one of `wrong_approach`, `bad_ticket`, `flaky_env`, `too_big`, `superseded`, `other`. |
| `reverted_at`, `revert_sha` | A commit on the default branch reverts `merge_commit_sha`: `Revert "…"` / `This reverts commit <sha>`. |
| `fix_forward_pr`, `fix_forward_at` | A later merged PR whose body says `Fixes-forward: #N`. |
| `review_activity` | The merged PR's author, commit authors, reviews and review comments, read once by the nightly pass. |

**Abandon reasons.** A trusted person comments `/hangar abandon <reason>` on the agent PR, or adds the label `agent-abandon:<reason>`. hangar refuses an unknown reason and replies with the list. The inbox lists agent PRs closed without a reason in the last week (kind `abandon`), and so does the digest (#158). Each reason is audited (`landing.abandon_reason`).

**The nightly pass** runs after 03:00 in `HANGAR_STANDUP_TIMEZONE`, on the control plane, with the control token, read-only. For each repo in `HANGAR_GITHUB_REPOS` it does three things:

- It caches every PR merged in the last 37 days, humans' and bots' included. This is the denominator of agent share.
- It scans the last 14 days of the default branch for reverts of merged agent PRs' merge commits. It matches GitHub's `merge_commit_sha`, so squash merges work. It also reads merged PRs for `Fixes-forward:`.
- It reads the reviews and review comments of each agent PR merged in the last 37 days, once per PR.

**Outbox job events** ([outbox.md](api/outbox.md#job-events-hangarjob_eventv1)) carry these to flightdeck:

- `pr.ready` (`ready_at`).
- The first human review on `review.first` (`first_review_at`, `first_review_by`).
- `revert` (`sha`), for reverts the nightly pass finds, including those pushed straight to the branch.
- `abandon` with the reason when one is set on a closed PR.

## Reviewer attention

Reviewer attention is the scarce resource, so hangar counts what agent PRs cost reviewers.

| | |
| --- | --- |
| Primary measure | `events_per_merged_pr`: review events on the week's merged agent PRs, divided by the merged agent PRs whose reviews were read (`measured`). Also `p50`, `p90`, `unreviewed` and `reviewers`. |
| A review event | A submitted review (any state) or a review comment on the diff. Conversation comments don't count. |
| Left out | Bots (`type: Bot` or a `[bot]` login) and hangar's own AI review. Also the agent's account: one the PR's commits are attributed to, unless it opened the PR or is linked to a hangar person. |
| The PR's author | Counts as a reviewer. hangar often opens PRs with the delegator's own account, and their reviews are human attention. |
| Secondary measure | `active_minutes`: active review minutes. Always `null` with a note today: GitHub records when a review was submitted, not how long it took. |
| Data source | The GitHub API: `pulls/N`, `pulls/N/commits`, `pulls/N/reviews` and `pulls/N/comments`, read by the nightly pass into `landing.review_activity`. |
| Until it is read | A merged PR the nightly pass hasn't read yet is left out of the rate, and `note` says so. |

The definition is the ticket owner's default and may change. It lives in one place, `weekly.attention`, over the raw events kept. Take [a repo's baseline](metrics-baseline.md) before hangar works there.

## Where hangar's numbers differ from flightdeck's today

- **Review wait.** hangar measures `ready_at` (else `opened_at`) to the first human review in any state. Flightdeck measures `pr.open` to the first counting approval until it switches to the `pr.ready` and first-human-review fields above (flightdeck#6).
- **Revert.** hangar sees reverts pushed straight to the branch. Flightdeck reads PR text until it reads the `revert` events.
- **Cost per merged PR.** hangar adds a merged job's linked review job (`landing.ai_review.job`, #161) once that link exists.

## Reading them

```sh
hangar metrics weekly [--week 2026-W43] [--repo owner/repo] [--person P] [--json]
hangar metrics standup              # the line as it would be posted now
```

`GET /api/metrics/weekly?week=2026-W43&repo=…&person=…` returns `hangar.metrics.weekly/v1`, and `GET /api/metrics/standup` returns the line. Team and repo numbers are open to any person's token. A `person=` query answers only for that person and admins (403 otherwise), and the standup line is team-level only.

## The standup line

Set `HANGAR_STANDUP_CHANNEL` to a Slack channel id (the bot, `HANGAR_SLACK_BOT_TOKEN_CREDENTIAL`, must be in it).

hangar then posts once per weekday at `HANGAR_STANDUP_TIME` (default `10:18`) in `HANGAR_STANDUP_TIMEZONE` (default `America/New_York`). It posts from the control plane, so the line goes out even when the pools are late. A line not posted by 13:00 is skipped for the day. The line looks like this:

```
Yesterday: 14 agent PRs merged, 3 abandoned (2 flaky_env, 1 unlabelled), review wait p50 2.1 h, 5 jobs held for review capacity, top failure: verify_failed
```

On Mondays the line covers last week (UTC) instead and adds yield, active delegators and review events per merged PR. The line also says how many new jobs review-aware admission (#159) holds for review capacity. Unset the channel to stop the line.
