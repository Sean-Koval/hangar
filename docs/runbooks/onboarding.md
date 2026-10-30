# Runbook: onboard 2 engineers on 1 repo, then 6 engineers on 3 repos

One ordered checklist for putting a team on hangar. Weeks 1–2 end with 2 engineers merging agent PRs on 1 repo, and
weeks 3–4 with all 6 on 3 repos. Every step names the command and the page it comes from, and every command is on
main. It ends, for each repo, with `hangar repo check` green.

Read it with [team setup](../team.md) (people and credentials), [landing](../landing.md) (the App and branch
protection), [intake](../intake.md) (Jira) and [the security model](../security/README.md) (the
gateway).

| Part | Page |
| --- | --- |
| Phase 1: the control plane, people and credentials (steps 1–3), and the first day (step 8) | this page |
| The per-repo checklist (steps 4–7), run for each repo | [The per-repo checklist](repo-checklist.md) |
| Phase 2: 6 engineers on 3 repos, the daily loop, and rollback | [Phase 2, the daily loop and rollback](onboarding-phase-2.md) |

**Three hard stops.** If any of these is true, the repo isn't onboarded, whatever else is green:

- **CI isn't required on the default branch.** With no required status checks GitHub merges as soon as hangar arms
  auto-merge ([how a PR lands](../landing.md#4-how-a-pr-lands)). `hangar repo check` fails `required checks`.
- **The GitHub App is on a bypass list.** Never add it to "Allow specified actors to bypass required pull requests"
  or to a ruleset's bypass list. Branch protection is what makes a merge wait for a person
  ([creating the App](../landing.md#create-it)).
- **The repo's environment is `credential_mode: direct`, or the gateway is off.** A direct-mode job's sandbox holds a
  GitHub token, and hangar never lands such a job: landing is off for it
  ([what landing doesn't cover](../landing.md#what-it-doesnt-cover)).

## Engineer-hours

The product lead's question: how long does one repo take to onboard? Record it here when the per-repo checklist is
run, from the first command to `hangar repo check` green. Count each person's hands-on time, not waiting for a GitHub
admin or for CI.

| Repo | Who ran it | Date | Phase 1 control-plane steps (1–3), hours | Per-repo checklist (4–7), hours | Notes |
| --- | --- | --- | --- | --- | --- |
| repo 1 | | | *not yet measured* | *not yet measured* | |
| repo 2 | | | — (done once) | | |
| repo 3 | | | — (done once) | | |

**Open item:** these hours are the one thing this runbook doesn't have yet. They are measured on the first real run
of the per-repo checklist (#154).

## Phase 1: 2 engineers, 1 repo (weeks 1–2)

### 1. Control-plane prerequisites

The safe release (#120–#132) is deployed: secure defaults for intake jobs, API keys for automated work, one approval
rule ([the safe release](../releases/safe-release.md)). It changes every environment's image, so deploy it by hand in
the power-down window, **01:00–09:50 ET**. Then the sandbox pools are down and the control plane is up
([applying it](../team-scale.md#applying-it)).

Later routine deploys go through `deploy.yml` while the pools are on, 10:00–01:00 ET
([when to deploy](../upgrading.md#when-to-deploy)). Then comes the
[adopt & capture upgrade](../releases/adopt-capture.md) (#133–#189), which deploys by day.

```bash
hangar doctor                          # clean: no failures
hangar config get HANGAR_INTAKE_POSTURE   # enforce, the only value (warn and off were removed after v0.2.0)
```

### 2. People

Add each engineer and link their accounts elsewhere ([add people](../team.md#1-add-people),
[linked accounts](../team/linked-accounts.md)). Links use ids that don't change hands: GitHub is stored as the
account's numeric id, Slack as `TEAM/USER`, Jira as the Atlassian account id.

```bash
hangar person add alice --name "Alice"        # prints her login token once; she runs hangar login, hangar connect
hangar person link alice github alice-gh
hangar person link alice slack T0123ABCD/U012AB3CD
hangar person link alice jira 5b10ac8d82e05b22cc7d4ef5
```

Two people means approvals work without `HANGAR_SELF_APPROVAL=allow_solo`: nobody approves their own job.

### 3. Credentials

API keys with a spend cap set in the vendor's console, one per engineer or one for the team
([team credentials](../team.md#3-team-credentials)). Never a subscription login: jobs refuse them
([subscription logins](../team/subscription-logins.md)).

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt
#   each engineer, on their machine:
hangar cred add claude-alice --harness claude-code --kind api_key --file anthropic-key.txt
hangar cred ls                         # kind api_key; owner=team or owner=<person>
```

## The per-repo checklist

Steps 4–7 for each repo: repo 1 in phase 1, repos 2 and 3 in phase 2. They are on
[the per-repo checklist](repo-checklist.md): the repo, its policy, its doors, and `hangar repo check` until green.

### 8. First day (phase 1)

Each engineer:

1. moves 2 tickets to **Ready for agent** (or labels 2 issues `hangar`);
2. reviews the agent PRs at the 10:00 triage (`hangar inbox`), and approves one that is ready. hangar lands it once
   the required checks pass. That is a teammate's PR as its second reviewer, or their own only when it is low risk
   with an AI review;
    - past 5 open agent PRs awaiting them, their new jobs wait as `awaiting_capacity`
      ([review capacity](../team-scale.md#review-capacity)): reviewing is what frees them;
3. answers one agent question in its thread or ticket (`/hangar <answer>`, a thread reply, a Jira comment).

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="4-the-repo"></span>4. The repo | [The per-repo checklist](repo-checklist.md#4-the-repo) |
| <span id="5-the-repo-policy"></span>5. The repo policy | [The per-repo checklist](repo-checklist.md#5-the-repo-policy) |
| <span id="6-doors"></span>6. Doors | [The per-repo checklist](repo-checklist.md#6-doors) |
| <span id="7-hangar-repo-check-until-green"></span>7. hangar repo check until green | [The per-repo checklist](repo-checklist.md#7-hangar-repo-check-until-green) |
| <span id="phase-2-6-engineers-3-repos-weeks-34"></span>Phase 2 | [Phase 2, the daily loop and rollback](onboarding-phase-2.md#phase-2-6-engineers-3-repos-weeks-34) |
| <span id="the-daily-loop"></span>The daily loop | [Phase 2, the daily loop and rollback](onboarding-phase-2.md#the-daily-loop) |
| <span id="rollback"></span>Rollback | [Phase 2, the daily loop and rollback](onboarding-phase-2.md#rollback) |
