# Landing: an approval lands the PR

An agent's PR used to need three things from a person: read it, approve it, then come back after CI and click merge. With landing on, a person reads the PR and approves it. hangar takes it from there, and GitHub's branch protection stays the gate.

- **Agent PRs come from a GitHub App** (`hangar-agents[bot]`), not from a person's token. A PR's author can't approve it, so with the App as author GitHub lets the person who asked for the work approve it. Whether that approval alone lands the PR is the [self-approval rule](landing/how-a-pr-lands.md#self-approval-who-may-be-the-only-approver): by default only for a low-risk PR (docs and tests) with an independent AI review of the approved head, never for a tainted PR; otherwise a second reviewer must approve.
- **An independent AI review comes first** ([section 6a](landing/review.md#6a-ai-first-pass-review)): a hangar job on another harness than the author's (or an org review bot's check) reviews each agent PR and posts one advisory COMMENT review.
- **The control plane, never the agent, arms auto-merge.** It does so after a qualifying approval of the PR's current head and green required checks, and only for that commit.
- **A push after the approval needs a new review.** hangar disarms before any agent turn starts on the job, and GitHub dismisses the stale approval.
- **hangar knows when work lands.** Each job records its PR's state (`open`, `approved`, `merged`, `closed`), when it merged, and how long it took from green checks to merged. A merged or closed PR ends the job's session.
- **Each agent PR gets a review packet**: a comment with the verification, cost, harness and model, files touched, risk and links.
- **One inbox** lists what needs you: `hangar://inbox`, `hangar inbox`, `GET /api/inbox`, and reaches you in Slack: a DM for each question or approval, and a digest twice a day.

## 1. The GitHub App

### Create it

1. GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**. Create it under the organization if the repos are an org's.
2. Name it (the PRs show `<name>[bot]`), and set any homepage URL. Turn **Webhook** off: hangar's repo webhook already delivers what it needs (see [the webhook](#5-the-webhook) for using the App's instead).
3. **Repository permissions**, exactly these:

   | Permission | Access | What hangar does with it |
   | --- | --- | --- |
   | Contents | Read and write | push agent branches (through the [git gateway](security/git-gateway.md)); merge a PR that is already mergeable (`PUT /pulls/{n}/merge`); read CODEOWNERS and the base branch's required checks (`GET /contents/…`, `GET /branches/{b}`) |
   | Pull requests | Read and write | open PRs, write and edit their comments (the review packet), read their files and reviews, arm and disarm auto-merge |
   | Metadata | Read | added by GitHub to every App; also reads rulesets (`GET /rules/branches/{b}`) |

   Leave everything else at *No access*, in particular **Workflows** (without it GitHub refuses pushes that change `.github/workflows/`), **Administration**, **Issues** and **Checks**.
4. Create it, note the **App ID**, and **Generate a private key**.
5. **Install App** on only the repositories agents work on.
6. Store it, and tell hangar to use it:

   ```sh
   jq -n --arg id 123456 --rawfile key ~/Downloads/hangar-agents.*.private-key.pem \
     '{app_id: $id, private_key: $key}' | hangar cred add github-app --harness git --kind github_app
   rm ~/Downloads/hangar-agents.*.private-key.pem
   hangar config set HANGAR_GITHUB_APP_CREDENTIAL github-app
   hangar config set HANGAR_GITHUB_APP_SLUG hangar-agents   # optional: the digest's review link lists only its PRs
   ```

**Don't put the App on a bypass list.** Leave it out of "Allow specified actors to bypass required pull requests" and out of every ruleset's bypass list. The App can merge (that is what `Contents: write` allows), and branch protection is what makes a merge wait for a person.

### What each identity does

| Who | Does what | Token |
| --- | --- | --- |
| The App, for the control plane | opens agent PRs; comments on them (outcome, auto-fix rounds, the packet); arms and disarms auto-merge; merges an approved PR that is already mergeable | an installation token minted per repo with `{"contents": "write", "pull_requests": "write"}` only, cached in memory until shortly before it expires, never written anywhere |
| The App, for the git gateway | pushes agent branches for sandboxes in `credential_mode: gateway` with no git credential of their own | an installation token with `{"contents": "write"}` only |
| The control token (`HANGAR_GITHUB_TOKEN_CREDENTIAL`) | comments on issues, reads issue comments, checks team membership (trust and CODEOWNERS teams), reads Actions logs for auto-fix, reads classic branch protection for `hangar doctor` | as before |

The App's key is a `github_app` credential: control-plane only, refused in any sandbox ([security/credentials.md](security/credentials.md#credential-handling)). If the App can't act on a repo (not installed there), hangar uses the control token and says so in the job's events.

The permissions above were checked against GitHub's "Permissions required for GitHub Apps" page (October 2026): `POST /repos/{o}/{r}/pulls` and `POST`/`PATCH` issue comments need Pull requests: write; `PUT /pulls/{n}/merge` needs Contents: write; `GET /branches/{b}` and `GET /contents/{path}` need Contents: read; `GET /rules/branches/{b}` needs Metadata: read; `GET /branches/{b}/protection` needs Administration: read, which the App deliberately lacks.

## 2. Turn it on for a repo

Landing is per repo, in the [repo policy](repo-policy.md), and off by default:

```sh
hangar repo set acme/app --land auto_after_approval --approver alice --approver bob --merge-method squash \
  --risk-path 'infra/**' --risk-path '**/migrations/**'
```

| Field | |
| --- | --- |
| `land` | `auto_after_approval` lands on approval; `manual` (or unset) never arms anything, and the inbox tells the approver to merge |
| `approvers` | GitHub logins whose approval lands the PR |
| `merge_method` | `squash` (default), `merge` or `rebase` |
| `risk_paths` | globs that make a PR **high risk** in its packet, on top of `.github/**` and `.hangar/**` |
| `require_stale_dismissal` | unset or `true`: arm only when the base branch dismisses stale approvals ([section 3](#3-the-branch-protection-to-enable)); `false` (`--no-require-stale-dismissal`) opts the repo out |
| `self_approval` | whether the job's own person may be its PR's only approver: `never`, `low_risk_with_ai_review` (the default) or `any` (the old behaviour). See [section 4](landing/how-a-pr-lands.md#self-approval-who-may-be-the-only-approver) |
| `ai_review`, `ai_review_harness`, `ai_review_model`, `ai_review_budget_usd`, `ai_review_inline_max` | the [AI first-pass review](ai-review.md): `hangar`, `check:<name>` or `off` (unset). Then the reviewing harness, its model, what one review may spend, and its most inline comments |
| `max_open_agent_prs` | [review capacity](team-scale.md#review-capacity): open agent PRs awaiting a person before their new jobs here wait |

**Who may approve.** A login in `approvers`, or a **code owner of every path the PR touches** (a rename's old path included). A file no line owns needs an approver from the policy, so owning one docs file doesn't land changes elsewhere; a PR with more than 300 files needs a policy approver too. CODEOWNERS is read from the base branch (`.github/CODEOWNERS`, then `CODEOWNERS`, then `docs/CODEOWNERS`, as GitHub does). The last matching line wins, and a line with no owners leaves a file unowned. A team entry (`@org/team`) counts when the control token can see the person's active membership, which needs the org's Members: read. Anyone else's approval is recorded in the job's events and lands nothing. GitHub's own rules still apply on top: with "require review from code owners" on, GitHub waits for a code owner even after an approver's approval.

## 3. The branch protection to enable

hangar arms auto-merge; **GitHub decides when it merges**. These settings on the default branch make that decision safe. Set them under Settings → Branches (classic protection) or Settings → Rules → Rulesets:

| Setting | Classic protection | Ruleset (`pull_request` rule) | Why |
| --- | --- | --- | --- |
| Require a pull request before merging, with at least 1 approval | `required_approving_review_count ≥ 1` | `required_approving_review_count ≥ 1` | no merge without a person |
| Dismiss stale pull request approvals when new commits are pushed | `dismiss_stale_reviews` | `dismiss_stale_reviews_on_push` | an approval covers the commit it was given on |
| Require approval of the most recent reviewable push | `require_last_push_approval` | `require_last_push_approval` | whoever pushed last can't be the one approving it |
| Require status checks to pass | `required_status_checks` | `required_status_checks` rule | CI decides "green", not hangar's view of it |
| Allow auto-merge (Settings → General) | | | otherwise GitHub refuses to arm |

**hangar won't arm auto-merge on a base branch that doesn't dismiss stale approvals.** It disarms on every push it hears of, but a person with write access can push while hangar misses the webhook, and GitHub keeps auto-merge on after such a push: without dismissal it could merge that push on the earlier approval. So before arming, hangar reads the base branch's protection and rulesets (cached for 5 minutes); when neither dismisses stale approvals, or neither can be read, it doesn't arm, and the job's events and `hangar doctor` (a failure) say why and how to fix it. A repo can opt out with `hangar repo set owner/repo --no-require-stale-dismissal`, which `hangar doctor` then reports as a warning.

`hangar doctor` checks these for every repo with `land: auto_after_approval` and warns about what else is missing. It reads classic protection with the control token when that token has Administration: read, and rulesets through `GET /rules/branches/{b}`, which the App can read. It also warns when no App is configured, and when jobs don't use the git gateway. In direct mode a job's sandbox holds a GitHub token that could arm auto-merge itself, so branch protection would be the only gate.

## 5. The webhook

`hangar setup github` (and `hangar doctor --fix`) registers these events on each mapped repo: `issues`, `issue_comment`, `pull_request_review_comment`, `pull_request_review`, `check_run`, `check_suite`, `status` and **`pull_request`**, which is new for landing. A hook set up before needs `pull_request` added; `hangar doctor` names it.

To use the **App's webhook** instead of per-repo hooks, point it at `https://<hangar>/api/hooks/github` with the same secret as `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL`, and subscribe to: Pull request, Pull request review, Pull request review comment, Check run, Check suite, Status, Issues and Issue comment. GitHub only offers an event to an App holding read access to it, so this needs three more read-only permissions on top of section 1: **Checks: Read** (check run, check suite), **Commit statuses: Read** (status) and **Issues: Read** (issues, issue comment). Tokens hangar mints keep asking for contents and pull requests only. Use one webhook or the other, not both.

If the `pull_request` webhook never arrives, a merged PR stays open in hangar's records. [When GitHub's webhook doesn't arrive](pr-reconcile.md) covers the fallback.

## What it doesn't cover

- **Direct-mode sandboxes** hold a GitHub token. If that token can write to the repo, the agent can arm auto-merge on its own PR, or approve it as the token's owner. So hangar lands a PR only when its delivery recorded that the job's sandbox pushed through the gateway: `credential_mode: direct`, or a mode hangar couldn't read, means merging by hand, and once a job's sandbox was direct it stays that way. The inbox says so. "Only the control plane arms" holds only with the [git gateway](security/git-gateway.md); branch protection ("approval of the most recent push", since the token's owner pushed it) is what stops the agent's own approval from merging.
- **Merge queues.** With a merge queue on the branch, GitHub queues instead of merging, and ignores `merge_method`.
- **Check results come from webhooks.** The App has no Checks permission, so hangar can't list a commit's checks. It learns them from `check_run`, `check_suite` and `status` deliveries, and falls back to GitHub's `mergeable_state` when one was missed.
- **The approval is checked against the PR's head when the review arrives.** An approval GitHub accepts on an older commit doesn't count for hangar.

## References (checked October 2026)

- GitHub REST: [Pulls](https://docs.github.com/en/rest/pulls/pulls) (merge with `sha`: 409 when the head moved; `auto_merge`, `mergeable_state`), [Branches](https://docs.github.com/en/rest/branches/branches), [Branch protection](https://docs.github.com/en/rest/branches/branch-protection) (`dismiss_stale_reviews`, `require_last_push_approval`), [Rules for a branch](https://docs.github.com/en/rest/repos/rules) (`dismiss_stale_reviews_on_push`, `require_last_push_approval`, `required_status_checks`), [Permissions required for GitHub Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps).
- GitHub GraphQL: `enablePullRequestAutoMerge` (`pullRequestId`, `expectedHeadOid`, `mergeMethod`, `commitBody`) and `disablePullRequestAutoMerge` ([mutations](https://docs.github.com/en/graphql/reference/pulls)). There is no REST route for enabling auto-merge on a PR.
- [Automatically merging a pull request](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/automatically-merging-a-pull-request): people with write access enable it; it is disabled when someone *without* write access pushes, which is why hangar disarms on every push itself.
- [Webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads): the permission each event needs for an App, and the `pull_request` actions (`closed` with `merged`, `synchronize`, `auto_merge_enabled`, `auto_merge_disabled`, `ready_for_review`, …).

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="4-how-a-pr-lands"></span>4. How a PR lands | [How a PR lands](landing/how-a-pr-lands.md) |
| <span id="tainted-prs"></span>Tainted PRs | [How a PR lands](landing/how-a-pr-lands.md#tainted-prs) |
| <span id="agent-drafted-adrs"></span>Agent-drafted ADRs | [How a PR lands](landing/how-a-pr-lands.md#agent-drafted-adrs) |
| <span id="self-approval-who-may-be-the-only-approver"></span>Self-approval: who may be the only approver | [How a PR lands](landing/how-a-pr-lands.md#self-approval-who-may-be-the-only-approver) |
| <span id="what-hangar-records"></span>What hangar records | [How a PR lands](landing/how-a-pr-lands.md#what-hangar-records) |
| <span id="6-the-review-packet"></span>6. The review packet | [The review packet and the inbox](landing/review.md#6-the-review-packet) |
| <span id="6a-ai-first-pass-review"></span>6a. AI first-pass review | [The review packet and the inbox](landing/review.md#6a-ai-first-pass-review) |
| <span id="7-the-inbox-what-needs-you"></span>7. The inbox: what needs you | [The review packet and the inbox](landing/review.md#7-the-inbox-what-needs-you) |
| <span id="delivery-it-comes-to-you"></span>Delivery: it comes to you | [The review packet and the inbox](landing/review.md#delivery-it-comes-to-you) |
