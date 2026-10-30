# Outcome labels on a merged PR

What did the merge keep of the agent's work? Each `pr.merge` [job event](outbox.md#job-events-hangarjob_eventv1)
answers that for an agent PR. flightdeck turns the fields into code survival and human-edit share (KAN-232).

## When they are read

The outbox's job scan sees the merge, and hangar reads the PR, two compares and the reviews from GitHub. The event
waits until GitHub answers, retried each minute. After 6 hours it goes out without the labels, with `outcome_error`
instead. With no GitHub token credential it goes out at once, with `outcome_error`.

## The fields

The fields were added within `hangar.job_event/v1`: a reader that ignores them is unchanged.

| Field | What it is |
| --- | --- |
| `agent_head_sha` | the head hangar last pushed: the agent's diff is base to here |
| `final_head_sha` | the PR's head at merge: the merged diff is base to here |
| `merged_sha` | GitHub's merge commit on the base branch |
| `agent_diff_lines` | lines the agent's diff adds and deletes |
| `agent_added_lines`, `merged_added_lines` | non-blank lines each diff adds |
| `surviving_lines` | the agent's added lines the merged diff also adds |
| `survival_at_merge` | `surviving_lines / agent_added_lines`, rounded to 4 places; null when the agent added none |
| `human_commits` | commits on the branch that hangar didn't author |
| `review_rounds` | head commits that got a human review |
| `outcome_error` | why the labels are missing; then none of the above is set |

## How lines and commits are matched

A line survives when the merged diff adds the same text, stripped of surrounding space, in the same file. Each copy
counts once, so a repeated line survives as often as the merge keeps it. A file GitHub sends without a patch (a
binary or a very large one) adds no lines to either side.

hangar's identity is the agent head's author, `hangar@localhost`, and any bot account. A commit by anyone else is a
human commit. A bot's review isn't a round, and neither is a pending one.
