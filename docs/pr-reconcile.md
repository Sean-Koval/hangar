# When GitHub's webhook doesn't arrive

hangar learns that a job's PR merged or closed from GitHub's `pull_request` webhook ([landing.md](landing.md)). Some installs never get it. The webhook was never registered, or the control plane's token lacks the Webhooks permission, or its deliveries fail. Then a merged PR stays open in hangar's records.

That has three costs. The PR keeps counting against the [review cap](team-scale.md#review-capacity), so new jobs wait as `awaiting_capacity`. The weekly numbers miss the merge. The job's session is never ended.

## What the reconcile does

A pass reads each PR that hangar still has as open or approved, with `GET /repos/{repo}/pulls/{number}`. When GitHub says the PR is merged or closed, hangar records it through the webhook's own path:

- the job's landing state and its "PR merged by @someone" event, with the merge time and commit from GitHub;
- the audit line and the review count, so a job held for review capacity starts at once;
- the end of the job's session, and, for a merge, conflict checks on the other open PRs on that base.

The job also gets one event saying the state came from GitHub's API, not a webhook.

A pass runs at startup and then every `HANGAR_GITHUB_PR_RECONCILE_MINUTES` (15 by default; 0 turns it off). It reads at most 200 PRs per pass. A PR it can't read is left as it is and counted as an error.

The pass is idempotent. A PR it recorded is no longer open, so the next pass doesn't read it. A late webhook for that PR, or a redelivery, is answered `{"ignored": "PR already recorded as merged"}` and changes nothing.

## What `hangar doctor` says

The `github PR reconcile` check reports the last pass: how many open PRs it read and how many it corrected.

- **ok**: the last pass corrected nothing, and nothing was corrected in the last 7 days.
- **warn**: a pass corrected a PR in the last 7 days. The check lists the PRs and says the webhook is missing or failing. Register it with `hangar setup github`, then look at its recent deliveries on GitHub.
- **warn**: the reconcile is off, or no pass has run yet.

A correction is a symptom, not a fix. The reconcile keeps hangar's counts right, but between passes a merge still waits up to 15 minutes. Missed check runs and reviews are not recovered this way; [redelivery](upgrading.md) covers those once a webhook exists.

## Backfilling old merges

Nothing to run by hand. The first pass after an upgrade reads every PR hangar still counts as open, so PRs merged weeks ago are recorded at startup.
