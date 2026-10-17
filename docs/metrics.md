# Weekly adoption metrics

The team's weekly numbers are defined once, in flightdeck: **[flightdeck docs/metrics.md](https://sean-koval.github.io/flightdeck/metrics/)**
(Sean-Koval/flightdeck#6). This page does not restate those definitions. It says what hangar records for them, how hangar computes the same numbers from its own run store for the standup line, and where the two differ today.

| Metric | Definition | Hangar's field in `GET /api/metrics/weekly` |
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

Hangar follows flightdeck's conventions:

- Weeks are ISO weeks in UTC.
- An agent job is delegated work. Experiment trials and review jobs are not agent jobs.
- Terminal means merged, closed unmerged, abandoned, or (with no PR) ended.
- Counted means the job ran a turn and its last run isn't `infra_error`. Infra errors are reported as `excluded_infra_error`.

Hangar computes no intervals and no trends. Flightdeck does that over the outbox's Parquet. The team reads flightdeck's report (`reports/weekly/latest.json`, `flightdeck.weekly/v1`) when it exists. Hangar's numbers exist so the standup line works from week 1, and with flightdeck off.

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

**Abandon reasons.** A trusted person comments `/hangar abandon <reason>` on the agent PR, or adds the label `agent-abandon:<reason>`. Hangar refuses an unknown reason and replies with the list. The inbox lists agent PRs closed without a reason in the last week (kind `abandon`), and so does the digest (#158). Each reason is audited (`landing.abandon_reason`).

**The nightly pass** runs after 03:00 in `HANGAR_STANDUP_TIMEZONE`, on the control plane, with the control token, read-only. For each repo in `HANGAR_GITHUB_REPOS` it does two things:

- It caches every PR merged in the last 37 days, humans' and bots' included. This is the denominator of agent share.
- It scans the default branch's commits of the last 14 days for reverts of merged agent PRs' merge commits (matched against GitHub's `merge_commit_sha`, so squash merges work), and merged PRs for `Fixes-forward:`.

**Outbox job events** ([outbox.md](api/outbox.md#job-events-hangarjob_eventv1)) carry these to flightdeck:

- `pr.ready` (`ready_at`).
- The first human review on `review.first` (`first_review_at`, `first_review_by`).
- `revert` (`sha`), for reverts the nightly pass finds, including those pushed straight to the branch.
- `abandon` with the reason when one is set on a closed PR.

## Where hangar's numbers differ from flightdeck's today

- **Review wait.** Hangar measures `ready_at` (else `opened_at`) to the first human review in any state. Flightdeck measures `pr.open` to the first counting approval until it switches to the `pr.ready` and first-human-review fields above (flightdeck#6).
- **Revert.** Hangar sees reverts pushed straight to the branch. Flightdeck reads PR text until it reads the `revert` events.
- **Cost per merged PR.** Hangar adds a merged job's linked review job (`landing.ai_review.job`, #161) once that link exists.

## Reading them

```sh
hangar metrics weekly [--week 2026-W43] [--repo owner/repo] [--person P] [--json]
hangar metrics standup              # the line as it would be posted now
```

`GET /api/metrics/weekly?week=2026-W43&repo=…&person=…` returns `hangar.metrics.weekly/v1`, and `GET /api/metrics/standup` returns the line. Team and repo numbers are open to any person's token. A `person=` query answers only for that person and admins (403 otherwise), and the standup line is team-level only.

## The standup line

Set `HANGAR_STANDUP_CHANNEL` to a Slack channel id (the bot, `HANGAR_SLACK_BOT_TOKEN_CREDENTIAL`, must be in it). Hangar then posts once per weekday at `HANGAR_STANDUP_TIME` (default `10:18`) in `HANGAR_STANDUP_TIMEZONE` (default `America/New_York`). It posts from the control plane, so the line goes out even when the pools are late. A line not posted by 13:00 is skipped for the day. The line looks like this:

```
Yesterday: 14 agent PRs merged, 3 abandoned (2 flaky_env, 1 unlabelled), review wait p50 2.1 h, 5 jobs held for review capacity, top failure: verify_failed
```

On Mondays the line covers last week (UTC) instead and adds yield and active delegators. The line also says how many new jobs review-aware admission (#159) holds for review capacity. Unset the channel to stop the line.
