# Landing: an approval lands the PR

An agent's PR used to need three things from a person: read it, approve it, then come back after CI and click merge. With landing on, a person reads the PR and approves it. Hangar takes it from there, and GitHub's branch protection stays the gate.

- **Agent PRs come from a GitHub App** (`hangar-agents[bot]`), not from a person's token. A PR's author can't approve it, so with the App as author the person who asked for the work can be its reviewer.
- **The control plane, never the agent, arms auto-merge.** It does so after a qualifying approval of the PR's current head and green required checks, and only for that commit.
- **A push after the approval needs a new review.** Hangar disarms before any agent turn starts on the job, and GitHub dismisses the stale approval.
- **Hangar knows when work lands.** Each job records its PR's state (`open`, `approved`, `merged`, `closed`), when it merged, and how long it took from green checks to merged. A merged or closed PR ends the job's session.
- **Each agent PR gets a review packet**: a comment with the verification, cost, harness and model, files touched, risk and links.
- **One inbox** lists what needs you: `hangar://inbox`, `hangar inbox`, `GET /api/inbox`, and reaches you in Slack: a DM for each question or approval, and a digest twice a day.

## 1. The GitHub App

### Create it

1. GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**. Create it under the organization if the repos are an org's.
2. Name it (the PRs show `<name>[bot]`), and set any homepage URL. Turn **Webhook** off: hangar's repo webhook already delivers what it needs (see [the webhook](#5-the-webhook) for using the App's instead).
3. **Repository permissions**, exactly these:

   | Permission | Access | What hangar does with it |
   | --- | --- | --- |
   | Contents | Read and write | push agent branches (through the [git gateway](security.md#git-credential-gateway)); merge a PR that is already mergeable (`PUT /pulls/{n}/merge`); read CODEOWNERS and the base branch's required checks (`GET /contents/…`, `GET /branches/{b}`) |
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

The App's key is a `github_app` credential: control-plane only, refused in any sandbox ([security.md](security.md#credential-handling)). If the App can't act on a repo (not installed there), hangar uses the control token and says so in the job's events.

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

**Who may approve.** A login in `approvers`, or a **code owner of every path the PR touches** (a rename's old path included). A file no line owns needs an approver from the policy, so owning one docs file doesn't land changes elsewhere; a PR with more than 300 files needs a policy approver too. CODEOWNERS is read from the base branch (`.github/CODEOWNERS`, then `CODEOWNERS`, then `docs/CODEOWNERS`, as GitHub does). The last matching line wins, and a line with no owners leaves a file unowned. A team entry (`@org/team`) counts when the control token can see the person's active membership, which needs the org's Members: read. Anyone else's approval is recorded in the job's events and lands nothing. GitHub's own rules still apply on top: with "require review from code owners" on, GitHub waits for a code owner even after an approver's approval.

## 3. The branch protection to enable

Hangar arms auto-merge; **GitHub decides when it merges**. These settings on the default branch make that decision safe. Set them under Settings → Branches (classic protection) or Settings → Rules → Rulesets:

| Setting | Classic protection | Ruleset (`pull_request` rule) | Why |
| --- | --- | --- | --- |
| Require a pull request before merging, with at least 1 approval | `required_approving_review_count ≥ 1` | `required_approving_review_count ≥ 1` | no merge without a person |
| Dismiss stale pull request approvals when new commits are pushed | `dismiss_stale_reviews` | `dismiss_stale_reviews_on_push` | an approval covers the commit it was given on |
| Require approval of the most recent reviewable push | `require_last_push_approval` | `require_last_push_approval` | whoever pushed last can't be the one approving it |
| Require status checks to pass | `required_status_checks` | `required_status_checks` rule | CI decides "green", not hangar's view of it |
| Allow auto-merge (Settings → General) | | | otherwise GitHub refuses to arm |

**Hangar won't arm auto-merge on a base branch that doesn't dismiss stale approvals.** It disarms on every push it hears of, but a person with write access can push while hangar misses the webhook, and GitHub keeps auto-merge on after such a push: without dismissal it could merge that push on the earlier approval. So before arming, hangar reads the base branch's protection and rulesets (cached for 5 minutes); when neither dismisses stale approvals, or neither can be read, it doesn't arm, and the job's events and `hangar doctor` (a failure) say why and how to fix it. A repo can opt out with `hangar repo set owner/repo --no-require-stale-dismissal`, which `hangar doctor` then reports as a warning.

`hangar doctor` checks these for every repo with `land: auto_after_approval` and warns about what else is missing. It reads classic protection with the control token when that token has Administration: read, and rulesets through `GET /rules/branches/{b}`, which the App can read. It also warns when no App is configured, and when jobs don't use the git gateway. In direct mode a job's sandbox holds a GitHub token that could arm auto-merge itself, so branch protection would be the only gate.

## 4. How a PR lands

```
opened ──▶ open ──approval of the head by an approver or code owner──▶ approved
                                                                           │ required checks green, no agent turn
                                                                           ▼
                                        armed (auto-merge for exactly that commit) ──GitHub's rules met──▶ merged
 push, changes requested, approval dismissed, an agent turn starting ──▶ back to open (disarmed)
```

- **Only the approved commit.** Hangar arms with `enablePullRequestAutoMerge(expectedHeadOid: <approved sha>)`. GitHub has no REST route for auto-merge, so this is GraphQL. When the PR is already mergeable, GitHub refuses to arm ("Pull request is in clean status") and hangar merges it with `PUT /pulls/{n}/merge` and `sha: <approved sha>`. Either call fails if the head moved.
- **A push clears the approval.** Any new head, from the agent, auto-fix, a follow-up or a person, clears the approval in hangar's record and disarms. A later push needs a new review, which "dismiss stale approvals" and "most recent push" enforce on GitHub's side.
- **No agent runs while armed.** Before arming, the job's session takes a checkpoint and its sandbox goes, and with it the sandbox's gateway grant, so nothing the agent left running can push. Before any later agent turn on the job (a follow-up, an auto-fix round, a verify-fix turn), hangar disarms; if it can't, the turn doesn't start. After a turn that pushed nothing, the approval still covers the head and hangar arms again. Arming also waits for any queued or running turn on the job.
- **Checks.** "Green" means every check the base branch requires (from its protection summary and rulesets) passed on the head: `success`, `neutral` or `skipped`. GitHub saying `mergeable_state: clean` also counts, in case a webhook was missed. A failed check holds landing even when it isn't required: an agent's red PR goes to [auto-fix](jobs.md#7-an-agent-loop-that-finishes-the-job), not to main. With no required checks, GitHub merges as soon as auto-merge is armed, so make CI required (`hangar doctor` warns).
- **Requests for changes win.** A "changes requested" review from anyone, or a dismissed approval, takes the PR back to open and disarms.
- **GitHub's record decides.** Review webhooks can arrive late, out of order (hangar redelivers missed ones after a restart) or replayed, so right before arming hangar lists the PR's reviews (`GET /pulls/{n}/reviews`, Pull requests: read): the approver's latest review must still be an approval of the head, and nobody's latest may request changes. A push, dismissal or follow-up that arrives while GitHub is arming is caught after the arm and disarmed; a disarm GitHub refused is tried again after the job's next turn and on restart.
- **Drafts wait.** A PR hangar opened as a draft (the check failed, the agent changed the check's own tests, or it read untrusted content) isn't armed until someone marks it ready for review.

### Tainted PRs

When the job's agent read untrusted content (`hangar:tainted`, [security.md](security.md#taint-gates)), the approval
that lands it must come from someone other than the job's own person and its requester: a reviewer whose GitHub
account is linked to the job's person, or who asked for the job, doesn't land it (the job's timeline says why), and
another approver or code owner does. The rule is `approvals.can_decide(kind="land")`, so
`HANGAR_SELF_APPROVAL=allow_solo` lets a one-person install land its own. The review packet lists what untrusted
content was read. Each decision is audited as `taint.gate` (`sink: land`).

Everything that needs GitHub runs in the background, under a per-job lock, so a webhook is answered at once. Hangar's record lives on the job, so a restart loses nothing: approved PRs are evaluated again and merged PRs' sessions ended at startup. Deliveries missed while hangar was down are [redelivered](upgrading.md).

### What hangar records

`job.landing` (in `GET /api/jobs/<id>`, `get_job` and `hangar://jobs/<id>`):

| Field | |
| --- | --- |
| `pr_state` | `open`, `approved`, `merged` or `closed` |
| `approved_by`, `approved_sha`, `approved_at` | the approval that counts |
| `armed_sha`, `armed_at` | auto-merge armed by hangar, for that commit |
| `checks`, `checks_sha` | each check run, suite or status context on the head and its conclusion |
| `ci_green_at`, `ci_green_sha` | when the required checks first all passed on that commit |
| `merged_at`, `merged_by`, `closed_at` | from GitHub |
| `green_to_merge_seconds` | from `ci_green_at` to `merged_at`, when the merged head is the one that went green |
| `files`, `risk` | what the PR touches, and its tier |
| `note` | why it isn't landing yet |
| `opened_at`, `ready_at` | when hangar opened the PR; when it was opened non-draft or marked ready (#168) |
| `first_review_at`, `first_review_by` | the first review a human submitted, any state; bots, the App's AI review included, never count (#168) |
| `merge_commit_sha` | what the merge put on the base branch: reverts of a squash merge are matched against it (#168) |
| `abandon_reason`, `abandon_by`, `abandon_at` | why a PR closed unmerged was abandoned (below) |
| `reverted_at`, `revert_sha`, `fix_forward_pr`, `fix_forward_at` | a revert of it on the default branch, or a PR that fixed it forward, found by the nightly pass ([metrics.md](metrics.md)) |

**Abandon reasons** (#168). When you close an agent PR without merging it, say why: comment `/hangar abandon <reason>` on it (a trusted person), or add the label `agent-abandon:<reason>`. The reason is one of `wrong_approach`, `bad_ticket`, `flaky_env`, `too_big`, `superseded` or `other`; anything else is refused with the list. The inbox lists closed agent PRs without one. The weekly abandon rate counts them by reason ([metrics.md](metrics.md)).

The audit log has `landing.approved`, `landing.arm`, `landing.disarm`, `landing.merge`, `landing.merged` and `landing.abandon_reason`.

**The end of the session.** A merged or closed PR ends the job's session: it takes a final checkpoint, then the sandbox is released (a paused hot-resume sandbox too). A turn still running when the PR closes is stopped first. A job still waiting on a question is cancelled, because nobody needs the answer any more. If the checkpoint fails, the sandbox stays until it idles out, so no work is lost.

**Schedules update their own PR.** When a schedule's last job still has an open PR, the next run is a follow-up on that job: the agent updates the PR instead of opening another each week. Once that PR is merged or closed, the next run is a new job. If the old job can't take a follow-up (no checkpoint, budget spent), a new job runs and the old job's events say why.

## 5. The webhook

`hangar setup github` (and `hangar doctor --fix`) registers these events on each mapped repo: `issues`, `issue_comment`, `pull_request_review_comment`, `pull_request_review`, `check_run`, `check_suite`, `status` and **`pull_request`**, which is new for landing. A hook set up before needs `pull_request` added; `hangar doctor` names it.

To use the **App's webhook** instead of per-repo hooks, point it at `https://<hangar>/api/hooks/github` with the same secret as `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL`, and subscribe to: Pull request, Pull request review, Pull request review comment, Check run, Check suite, Status, Issues and Issue comment. GitHub only offers an event to an App holding read access to it, so this needs three more read-only permissions on top of section 1: **Checks: Read** (check run, check suite), **Commit statuses: Read** (status) and **Issues: Read** (issues, issue comment). Tokens hangar mints keep asking for contents and pull requests only. Use one webhook or the other, not both.

## 6. The review packet

Hangar writes one comment on each agent PR when it delivers, and edits it on every later push, rather than posting again. It is built from the job record, so it works for any repo without a CI job:

- **Verification**: the command and its result, "base was red" when the [check on the base](repo-policy.md#the-check-on-the-base-commit) failed, and whether the agent changed the check's own tests;
- **Risk**: `high` when a path matches `.github/**`, `.hangar/**` (the repo's environment, which runs in every build and sandbox) or the policy's `risk_paths`, `low` for docs and tests only, `normal` otherwise;
- **Files** touched, with the diffstat;
- **Cost**: tokens and dollars so far, the job budget, and the agent turns;
- **Agent**: harness and the version its pack installed, model, environment;
- **Session**: its name, and how to pull it locally or continue it (`/hangar <message>`);
- **Recorded demos**: links to video and GIF artifacts the agent handed back;
- **Landing**: who can land it and how.

## 7. The inbox: what needs you

One list per person, newest first, from the records hangar keeps:

| Kind | When | What to do |
| --- | --- | --- |
| `review` | your agent's PR is open and its job is done; or it is approved but can't land (`land: manual`, a draft, a failed check) | review and approve it, or merge it |
| `question` | a job in `needs_input`, or a session's open question | answer with a follow-up or `send_to_session` |
| `approval` | a job waiting for approval (an untrusted trigger, a spend cap); a turn waiting on a tool approval; requests other modules add (egress, secrets) | approve or deny |
| `failed` | a job that failed in the last 7 days and has no open PR | read why, then follow up or resubmit |
| `alert` | admins only: a front door saw use that a real sender wouldn't make, such as a Jira Automation token used for an issue nobody just moved ([intake.md](intake.md#fallback-an-automation-rule)); kept 7 days | check the rule, rotate the token if it doesn't explain it |

It is per person, as everything is since [people](security.md#people): a member sees items for their own jobs and sessions. An admin sees their own, plus work nobody in particular owns (jobs from GitHub issues, Slack and inbound webhooks). Read it:

- MCP: the resource `hangar://inbox` (not a tool, so it adds nothing to the tool list). Subscribe to it to hear when it changes; it changes with any job or session.
- CLI: `hangar inbox` (`--json` for the raw list).
- REST: `GET /api/inbox` with an admin token or a person's `user` token: `{"person", "counts": {"review": 2, …}, "items": [{"id", "kind", "reason", "action", "since", "job", "pr", "links", …}]}`.

Each item carries a stable `id` (a digest of its kind, the job, session or request, and the approval or question: reading the inbox again gives the same id) and `links` to the native object where you act on it: `{"pr", "ticket", "thread", "job"}`. `ticket` is the Jira issue (`<HANGAR_JIRA url>/browse/ABC-12`) or the GitHub issue the job came from; `thread` the Slack thread it talks in (Slack's own permalink once hangar has fetched it with `chat.getPermalink`); `job` the job on the dashboard (`<HANGAR_PUBLIC_URL>/#jobs?job=<id>`).

### Delivery: it comes to you

Nobody runs `hangar inbox` to find out an agent is blocked on them, so hangar sends it, in Slack, to each person with a linked Slack account ([team.md](team.md#inbox-delivery-dms-and-the-digest)):

- **At once: questions and approvals.** A new `question` or `approval` item goes to its person in a DM within a minute: the reason, "Answer here: <link>" (the Slack thread, the Jira issue, the PR, else the job), and the CLI fallback. There are no buttons: the answer is given where it's recorded with your identity. A burst (five approvals in ten seconds) is one DM, and an item is sent once (hangar remembers it for 7 days).
- **Twice a day: the digest**, at 10:15 and 16:00 in your time zone on weekdays: questions oldest first with their age, approvals with their expiry, jobs that failed since the last digest, approved PRs that aren't landing and why; then one line, "N agent PRs await your review", linking GitHub's review-request queue (reviews are never listed one by one: GitHub stays the review queue), and a footer with your open agent PRs (against the review-capacity cap, when set) and jobs held for it. At most 20 items, then "and N more: hangar inbox". An empty digest isn't sent. It runs on the control plane, which stays up while the sandbox pools are powered down (01:00-10:00), so the 10:15 digest goes out even when power-up is late; it then says "sandboxes not up yet". A restart never sends a slot twice; a slot missed while hangar was down goes out on recovery if it is under 2 hours late.
- **Not sent:** `failed` items wait for the digest, and `review` items stay in GitHub. Work nobody in particular owns (GitHub, Slack and webhook jobs) goes only to admins who chose `--dm all`.

The digest's review link needs your GitHub login: hangar looks it up from your linked GitHub user id (`GET /user/{id}`, cached), and with `HANGAR_GITHUB_APP_SLUG` set narrows it to the App's PRs (`author:app/<slug>`). Without a GitHub token the line has no link.

Other modules add kinds of request through `inbox.SOURCES`: a function from the job service and the caller to items, which decides what that caller may see.

## What it doesn't cover

- **Direct-mode sandboxes** hold a GitHub token. If that token can write to the repo, the agent can arm auto-merge on its own PR, or approve it as the token's owner. So hangar lands a PR only when its delivery recorded that the job's sandbox pushed through the gateway: `credential_mode: direct`, or a mode hangar couldn't read, means merging by hand, and once a job's sandbox was direct it stays that way. The inbox says so. "Only the control plane arms" holds only with the [git gateway](security.md#git-credential-gateway); branch protection ("approval of the most recent push", since the token's owner pushed it) is what stops the agent's own approval from merging.
- **Merge queues.** With a merge queue on the branch, GitHub queues instead of merging, and ignores `merge_method`.
- **Check results come from webhooks.** The App has no Checks permission, so hangar can't list a commit's checks. It learns them from `check_run`, `check_suite` and `status` deliveries, and falls back to GitHub's `mergeable_state` when one was missed.
- **The approval is checked against the PR's head when the review arrives.** An approval GitHub accepts on an older commit doesn't count for hangar.

## References (checked October 2026)

- GitHub REST: [Pulls](https://docs.github.com/en/rest/pulls/pulls) (merge with `sha`: 409 when the head moved; `auto_merge`, `mergeable_state`), [Branches](https://docs.github.com/en/rest/branches/branches), [Branch protection](https://docs.github.com/en/rest/branches/branch-protection) (`dismiss_stale_reviews`, `require_last_push_approval`), [Rules for a branch](https://docs.github.com/en/rest/repos/rules) (`dismiss_stale_reviews_on_push`, `require_last_push_approval`, `required_status_checks`), [Permissions required for GitHub Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps).
- GitHub GraphQL: `enablePullRequestAutoMerge` (`pullRequestId`, `expectedHeadOid`, `mergeMethod`) and `disablePullRequestAutoMerge` ([mutations](https://docs.github.com/en/graphql/reference/pulls)). There is no REST route for enabling auto-merge on a PR.
- [Automatically merging a pull request](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/automatically-merging-a-pull-request): people with write access enable it; it is disabled when someone *without* write access pushes, which is why hangar disarms on every push itself.
- [Webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads): the permission each event needs for an App, and the `pull_request` actions (`closed` with `merged`, `synchronize`, `auto_merge_enabled`, `auto_merge_disabled`, `ready_for_review`, …).
