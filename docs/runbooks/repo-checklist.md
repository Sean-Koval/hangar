# The per-repo checklist

Steps 4–7 of [the onboarding runbook](onboarding.md), for each repo: repo 1 in phase 1, repos 2 and 3 in phase 2.
Each repo ends with `hangar repo check` green and its engineer-hours recorded.

## 4. The repo

- **Allowlisted.** In `HANGAR_GITHUB_REPOS` with its environment: `hangar setup github` adds it and its webhook
  ([trigger from GitHub](../jobs/from-github-slack-jira.md#4-trigger-from-github)).
- **The GitHub App installed on this repo only** (Contents and Pull requests: Read and write, nothing else), and
  `HANGAR_GITHUB_APP_CREDENTIAL` set ([the GitHub App](../landing.md#1-the-github-app)). Not on any bypass list.
- **`credential_mode: gateway`** on the repo's environment, with `HANGAR_GATEWAY_URL` set
  ([the git gateway, turning it on](../security/git-gateway.md#turning-it-on)).
- **Branch protection** on the default branch ([the branch protection](../landing.md#3-the-branch-protection-to-enable)):
  a pull request with at least 1 approval, and dismiss stale approvals on a push. Also approval of the most recent
  push, required status checks, and *Allow auto-merge* in Settings → General.
- **CI required.** The repo's CI is among the required status checks (the first hard stop).

```bash
hangar setup github --repo acme/app=python   # token, webhook secret, HANGAR_GITHUB_REPOS, the repo's webhook
hangar config set HANGAR_GITHUB_APP_CREDENTIAL github-app
#   the environment's YAML: credential_mode: gateway (step 7 checks it: `credential mode ENV`)
```

hangar can't apply branch protection or install the App: the App deliberately lacks Administration. A GitHub admin
does those by hand; step 7 checks them.

## 5. The repo policy

What every job on the repo gets, whichever door it came through ([repo policy](../repo-policy.md)):

```bash
hangar repo set acme/app --land auto_after_approval --approver alice --approver bob \
  --risk-path 'infra/**' --risk-path '**/migrations/**' \
  --verify "uv run pytest -q" --max-usd-per-day 60 \
  --ai-review hangar --self-approval low_risk_with_ai_review
hangar repo get acme/app
```

**The review rule.** The person who asked for the work may be its PR's only approver only for a low-risk PR (docs and
tests). It also needs an independent AI review of the approved head
([self-approval](../landing.md#self-approval-who-may-be-the-only-approver)).

Anything else, and every tainted PR (every Jira-door job), needs a second reviewer from `--approver`. With
`--ai-review hangar`, a different harness from the author's reviews every agent PR first
([AI review](../landing.md#6a-ai-first-pass-review)).

## 6. Doors

- **Jira**: map the project to the repo with `hangar setup jira`. Moving an issue to *Ready for agent* starts a job
  unless you pass another `--trigger-status` or a `--trigger-label` ([the configuration](../intake.md#3-the-configuration)).
- Create that status in the project's workflow first. Run `hangar setup jira` again with `--project` for repos 2 and
  3: it keeps the projects already mapped. Tickets follow the
  [agent ticket template](https://github.com/Sean-Koval/hangar/blob/main/docs/templates/jira-agent-ticket.md).
- **GitHub**: the `hangar` label (`HANGAR_GITHUB_TRIGGER_LABEL`) exists on the repo; labelling an issue starts a job
  ([trigger from GitHub](../jobs/from-github-slack-jira.md#4-trigger-from-github)).
- **Slack**: the team's channel maps to the repo in `HANGAR_SLACK_CHANNELS`, and the app is invited to it
  ([Slack](../slack.md)).

```bash
hangar setup jira --url https://acme.atlassian.net --email hangar@acme.com --project APP=acme/app \
  --token-file jira-token --webhook-secret-file jira-webhook-secret   # trigger: "Ready for agent"
hangar setup slack --channel C0123ABCD=acme/app   # asks for the app's secrets; or edit HANGAR_SLACK_CHANNELS
```

## 7. `hangar repo check` until green

```bash
hangar repo check acme/app
```

It reads the App installation, the webhook, the label, the policy, branch protection and required checks. It reads
the gateway, the environments' credential mode, sudo, egress and logins, and the Jira and Slack mappings.

It ends with `ready` or `not ready (N failing)`, each failure with its fix
([is this repo ready](../repo-policy.md#is-this-repo-ready)). Fix and run it again until it says `ready`. Then record
the engineer-hours in [the onboarding table](onboarding.md#engineer-hours), and go on to
[the first day](onboarding.md#8-first-day-phase-1).
