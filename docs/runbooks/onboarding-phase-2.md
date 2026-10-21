# Onboarding: phase 2, the daily loop and rollback

The second half of [the onboarding runbook](onboarding.md): from 2 engineers on 1 repo to 6 on 3. Then the team's
daily loop, and how to back out.

## Phase 2: 6 engineers, 3 repos (weeks 3–4)

1. Steps [2](onboarding.md#2-people) and [3](onboarding.md#3-credentials) for the four new engineers.
2. [The per-repo checklist](repo-checklist.md) (4–7) for repos 2 and 3, each ending with `hangar repo check` green and
   its hours recorded.
3. Check the team features, all shipped with the adopt & capture release; the first three are on by default:
    - inbox DMs for questions and approvals, and the per-person digest at 10:15 and 16:00 (#157, #158). They are on
      for everyone with a linked Slack account (step 2); `hangar person set <id> --dm off` opts out;
    - review-aware admission: new jobs wait as `awaiting_capacity` past 5 open agent PRs per reviewer (#159;
      `HANGAR_PERSON_MAX_OPEN_AGENT_PRS`, or `--max-open-agent-prs` per repo,
      [review capacity](../team-scale.md#review-capacity));
    - the self-approval rule (#160, the default `low_risk_with_ai_review`): a requester's own approval lands only a
      low-risk PR with an independent AI review;
    - the AI first-pass review (#161) is off until `hangar repo set <repo> --ai-review hangar`. Turn it on for each
      repo where people approve their own docs and test PRs;
    - run capture: on in the GKE deploy, where `deploy/gke/up.sh` sets `HANGAR_OUTBOX_URL` (#273). Elsewhere it is off
      until that is set ([run capture](../observability.md#run-capture); operating it: [the outbox runbook](outbox.md)).
4. The weekly adoption metrics start counting active delegators (#168).
5. Once the last 14 days' agent PR yield is at least 60% (`hangar metrics weekly`), consider the overnight lane. That
   is one Spot node 01:00–10:00 (~$87 a month). Evening tickets of the repos set to `overnight: true` are then PRs
   by the 10:00 triage ([the overnight lane](../gke.md#the-overnight-lane)).
6. Below 60% the lane only adds to the morning's review queue, and `hangar power overnight on` refuses.

## The daily loop

| When | What | Where |
| --- | --- | --- |
| Any time | An agent's question or approval reaches its person in a Slack DM within a minute, linked to the thread, ticket or PR to answer in | [landing.md, delivery](../landing.md#delivery-it-comes-to-you); `hangar person set <id> --dm …` |
| 10:00–10:20 | Triage: each engineer clears their inbox (PRs to review, questions, approvals, failed jobs). The 10:15 Slack digest lists it, questions first; reviews are a count and a link to GitHub's review queue | `hangar inbox` ([landing.md §7](../landing.md#7-the-inbox-what-needs-you)) |
| 10:20 | Standup, with the bot's line at 10:18 ET: yesterday's merged and abandoned agent PRs (by reason), review wait p50, top failure; Mondays, last week's summary | `HANGAR_STANDUP_CHANNEL` ([metrics.md](../metrics.md#the-standup-line)) |
| 16:00 | The afternoon digest catches what came in since | Slack DM ([team.md](../team/linked-accounts.md#inbox-delivery-dms-and-the-digest)) |
| Friday, 30 min | Harness review: label the first failure on failed or abandoned jobs, until 20 candidate tasks | `hangar review queue` ([harness-review.md](harness-review.md)) |

Sandboxes power on at 09:50 ET, so work queued overnight starts before triage. The digests and DMs come from the
control plane, which stays up while the pools are off. A 10:15 digest sent before power-up finished says "sandboxes
not up yet".

Each engineer needs their Slack account linked (`hangar person link <id> slack T…/U…`); `hangar doctor` lists anyone
the inbox can't reach.

## Rollback

From lightest to heaviest:

1. **Stop landing on a repo**: a person merges again.

   ```bash
   hangar repo set acme/app --land manual
   ```

2. **Stop the Jira door**: remove the project from `HANGAR_JIRA`'s `projects` (`hangar config set jira '{…}'`
   without it). Issues moved to *Ready for agent* then start nothing.
3. **Stop all agent work for a while**: after the power-down (01:00), hold the power schedule so the sandbox pools
   stay down; jobs queue and wait. `hangar power release` (or `hangar power up`) ends it.

   ```bash
   hangar power hold --days 2
   ```

Nothing here deletes work: jobs, sessions and their PRs stay as they are.
