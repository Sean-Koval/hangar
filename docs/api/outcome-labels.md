# Outcome labels on a merged PR

What did the merge keep of the agent's work? Each `pr.merge` [job event](outbox.md#job-events-hangarjob_eventv1)
answers that for an agent PR. flightdeck turns the fields into code survival and human-edit share (KAN-232).

## When they are read

The outbox's job scan sees the merge and holds its event. A separate task reads the PR, its files, commits and
reviews, and one compare from GitHub. It asks as the GitHub App when hangar has one, else with the token credential.

The event goes out only once the labels are stored. While GitHub fails, it is asked again with backoff. After 6
hours the event goes out with `outcome_error` instead of the labels. With no GitHub credential at all it goes at once.

## The fields

The fields were added within `hangar.job_event/v1`: a reader that ignores them is unchanged.

| Field | What it is |
| --- | --- |
| `agent_head_sha` | the head hangar last pushed: the agent's diff is base to here |
| `final_head_sha` | the PR's head at merge: the merged diff is the PR's own files, from its merge base to here |
| `merged_sha` | GitHub's merge commit on the base branch |
| `agent_diff_lines` | lines the agent's diff adds and deletes |
| `agent_added_lines`, `merged_added_lines` | non-blank lines each diff adds |
| `surviving_lines` | the agent's added lines the merged diff also adds |
| `survival_at_merge` | `surviving_lines / agent_added_lines`, rounded to 4 places; null when the agent added none |
| `human_commits` | the PR's commits that hangar didn't deliver |
| `review_rounds` | head commits that got a human review |
| `outcome_truncated` | each list that hit GitHub's cap (`agent_files`, `agent_commits`, `merged_files`, `merged_commits`); its counts are a floor |
| `outcome_error` | why the labels are missing; then none of the above is set |

## How lines and commits are matched

A line survives when the merged diff adds the same text, stripped of surrounding space, in the same file. Each copy
counts once, so a repeated line survives as often as the merge keeps it. A file GitHub sends without a patch (a
binary or a very large one) adds no lines to either side.

The merged diff is the PR's own: an update from the base branch, by merge or rebase, adds none of the base's lines.

hangar's commits are the ones it delivered: those under the agent head. After a rebase, a commit with the same author
date and message is one too. The account doesn't matter, as hangar may push as the owner.

A human commit is any other PR commit, except a merge commit or a bot's. A bot's review isn't a round, and neither is a pending one.
