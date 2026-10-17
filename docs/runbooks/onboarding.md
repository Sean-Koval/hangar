# Runbook: onboard 2 engineers on 1 repo, then 6 engineers on 3 repos

One ordered checklist for putting a team on hangar: weeks 1–2 end with 2 engineers merging agent PRs on 1 repo,
weeks 3–4 with all 6 on 3 repos. Every step names the command and the page it comes from; a step whose command isn't
on main yet names the issue that adds it. It ends, for each repo, with `hangar repo check` green.

Read it with [team.md](../team.md) (people and credentials), [landing.md](../landing.md) (the App and branch
protection), [intake.md](../intake.md) (Jira and GitHub Projects) and [security.md](../security.md) (the gateway).

**Three hard stops.** If any of these is true, the repo isn't onboarded, whatever else is green:

- **CI isn't required on the default branch.** With no required status checks GitHub merges as soon as hangar arms
  auto-merge ([landing.md §4](../landing.md#4-how-a-pr-lands)). `hangar repo check` fails `required checks`.
- **The GitHub App is on a bypass list.** Never add it to "Allow specified actors to bypass required pull requests"
  or to a ruleset's bypass list: branch protection is what makes a merge wait for a person
  ([landing.md §1](../landing.md#create-it)).
- **The repo's environment is `credential_mode: direct`, or the gateway is off.** A direct-mode job's sandbox holds a
  GitHub token, and hangar never lands such a job: landing is off for it
  ([landing.md, what it doesn't cover](../landing.md#what-it-doesnt-cover)).

## Engineer-hours

The product lead's question: how long does one repo take to onboard? Record it here when the per-repo checklist is
run, from the first command to `hangar repo check` green, counting each person's hands-on time (not waiting for a
GitHub admin or for CI).

| Repo | Who ran it | Date | Phase 1 control-plane steps (1–3), hours | Per-repo checklist (4–7), hours | Notes |
| --- | --- | --- | --- | --- | --- |
| repo 1 | | | *not yet measured* | *not yet measured* | |
| repo 2 | | | — (done once) | | |
| repo 3 | | | — (done once) | | |

## Phase 1: 2 engineers, 1 repo (weeks 1–2)

### 1. Control-plane prerequisites

The safe release (#120–#132) is deployed: secure defaults for intake jobs, API keys for automated work, one approval
rule ([upgrading.md, the safe release](../upgrading.md#the-safe-release-120132)). Deploy it in the power-down window,
**01:00–09:50 ET**, while the sandbox pools are down and the control plane is up
([team-scale.md, applying it](../team-scale.md#applying-it)).

```bash
hangar doctor                          # clean: no failures
hangar config get HANGAR_INTAKE_POSTURE   # enforce (not warn, not off)
```

### 2. People

Add each engineer and link their accounts elsewhere ([team.md §1–2](../team.md#1-add-people)). Links use ids that
don't change hands: GitHub is stored as the account's numeric id, Slack as `TEAM/USER`, Jira as the Atlassian
account id.

```bash
hangar person add alice --name "Alice"        # prints her login token once; she runs hangar login, hangar connect
hangar person link alice github alice-gh
hangar person link alice slack T0123ABCD/U012AB3CD
hangar person link alice jira 5b10ac8d82e05b22cc7d4ef5
```

Two people means approvals work without `HANGAR_SELF_APPROVAL=allow_solo`: nobody approves their own job.

### 3. Credentials

API keys with a spend cap set in the vendor's console, one per engineer or one for the team
([team.md §3–4](../team.md#3-team-credentials)). Never a subscription login: jobs refuse them
([team.md, subscription logins](../team.md#subscription-logins)).

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt
#   each engineer, on their machine:
hangar cred add claude-alice --harness claude-code --kind api_key --file anthropic-key.txt
hangar cred ls                         # kind api_key; owner=team or owner=<person>
```

## The per-repo checklist

Steps 4–7 for each repo: repo 1 in phase 1, repos 2 and 3 in phase 2.

### 4. The repo

- **Allowlisted.** In `HANGAR_GITHUB_REPOS` with its environment: `hangar setup github` adds it and its webhook
  ([jobs.md §4](../jobs.md#4-trigger-from-github)).
- **The GitHub App installed on this repo only** (Contents and Pull requests: Read and write, nothing else), and
  `HANGAR_GITHUB_APP_CREDENTIAL` set ([landing.md §1](../landing.md#1-the-github-app)). Not on any bypass list.
- **`credential_mode: gateway`** on the repo's environment, with `HANGAR_GATEWAY_URL` set
  ([security.md, turning it on](../security.md#turning-it-on)).
- **Branch protection** on the default branch ([landing.md §3](../landing.md#3-the-branch-protection-to-enable)):
  a pull request with at least 1 approval, dismiss stale approvals on a push, approval of the most recent push,
  required status checks, and *Allow auto-merge* in Settings → General.
- **CI required.** The repo's CI is among the required status checks (the first hard stop).

```bash
hangar setup github --repo acme/app=python   # token, webhook secret, HANGAR_GITHUB_REPOS, the repo's webhook
hangar config set HANGAR_GITHUB_APP_CREDENTIAL github-app
#   the environment's YAML: credential_mode: gateway (step 7 checks it: `credential mode ENV`)
```

Hangar can't apply branch protection or install the App: the App deliberately lacks Administration. A GitHub admin
does those by hand; step 7 checks them.

### 5. The repo policy

What every job on the repo gets, whichever door it came through ([repo-policy.md](../repo-policy.md)):

```bash
hangar repo set acme/app --land auto_after_approval --approver alice --approver bob \
  --risk-path 'infra/**' --risk-path '**/migrations/**' \
  --verify "uv run pytest -q" --max-usd-per-day 60
hangar repo get acme/app
```

### 6. Doors

- **Jira**: map the project to the repo, with the trigger status `Ready for agent`
  ([intake.md §3](../intake.md#3-the-configuration)); making it the default door, GitHub label and Projects parity and
  an agent ticket template come with #156.
- **GitHub**: the `hangar` label (`HANGAR_GITHUB_TRIGGER_LABEL`) exists on the repo; labelling an issue starts a job
  ([jobs.md §4](../jobs.md#4-trigger-from-github)).
- **Slack**: the team's channel maps to the repo in `HANGAR_SLACK_CHANNELS`, and the app is invited to it
  ([slack.md](../slack.md)).

```bash
hangar config set jira '{ … "projects": {"APP": {"repo": "acme/app"}}, "trigger": {"status": "Ready for agent", "label": "hangar"} … }'
hangar setup slack --channel C0123ABCD=acme/app   # asks for the app's secrets; or edit HANGAR_SLACK_CHANNELS
```

### 7. `hangar repo check` until green

```bash
hangar repo check acme/app
```

It reads the App installation, the webhook, the label, the policy, branch protection and required checks, the gateway,
the environments' credential mode, sudo, egress and logins, and the Jira and Slack mappings, and ends with `ready` or
`not ready (N failing)`, each failure with its fix ([repo-policy.md, is this repo ready](../repo-policy.md#is-this-repo-ready)).
Fix and run it again until it says `ready`; then record the engineer-hours in the table above.

### 8. First day (phase 1)

Each engineer:

1. moves 2 tickets to **Ready for agent** (or labels 2 issues `hangar`);
2. reviews the agent PRs at the 10:00 triage (`hangar inbox`), and approves one that is ready: hangar lands it once
   the required checks pass;
3. answers one agent question in its thread or ticket (`/hangar <answer>`, a thread reply, a Jira comment).

## Phase 2: 6 engineers, 3 repos (weeks 3–4)

1. Steps 2 and 3 for the four new engineers.
2. The per-repo checklist (4–7) for repos 2 and 3, each ending with `hangar repo check` green and its hours recorded.
3. Then turn on, as each lands:
   - inbox DMs for questions and approvals, and the per-person digest at 10:15 and 16:00 (#157, #158);
   - review-aware admission: new jobs wait as `awaiting_capacity` past N open agent PRs per reviewer (#159);
   - the self-approval rule for low-risk packets with an independent AI review, and that AI first-pass review on
     every agent PR (#160, #161);
   - run capture: `HANGAR_OUTBOX_URL` ([observability.md, run capture](../observability.md#run-capture)).
4. The weekly adoption metrics start counting active delegators (#168).

## The daily loop

| When | What | Where |
| --- | --- | --- |
| 10:00–10:20 | Triage: each engineer clears their inbox (PRs to review, questions, approvals, failed jobs) | `hangar inbox` ([landing.md §7](../landing.md#7-the-inbox-what-needs-you)) |
| 10:20 | Standup, with the bot's line at 10:18 ET: yesterday's merged and abandoned agent PRs (by reason), review wait p50, top failure; Mondays, last week's summary | `HANGAR_STANDUP_CHANNEL` ([metrics.md](../metrics.md#the-standup-line)) |
| Friday, 30 min | Harness review: label the first failure on failed or abandoned jobs, until 20 candidate tasks | `hangar review queue` ([harness-review.md](harness-review.md)) |

Sandboxes power on at 09:50 ET, so work queued overnight starts before triage.

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
