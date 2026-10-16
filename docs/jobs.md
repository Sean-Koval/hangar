# Jobs: handing work to cloud agents

A job is a piece of development work handed to hangar by a service, an app, an MCP client, the CLI, a GitHub issue or a Slack channel. Hangar runs a coding agent on it in a fresh sandbox, checks the result with the repo's own command, and delivers a branch and a (draft) PR. Engineers can follow along, reply, or take over. The design is in [spec 0009](specs/0009-jobs-and-the-factory-front-door.md).

```
caller ──POST /api/jobs──► queue ─► sandbox (env + harness) ─► agent turn ─► verify ─► commit, push hangar/<job>, PR
   ▲                                                      ▲                                  │
   └──── signed callback: job.started / job.finished ─────┼──────────────────────────────────┘
                     engineer: follow-up (API, MCP, CLI, `/hangar` on the PR) or attach to the terminal
```

## 0. Log in and check your setup

```bash
hangar login https://<your hangar> --token-file ~/.hangar/gke-admin-token   # once; no more HANGAR_URL/TOKEN exports
hangar doctor                                                               # what's broken, and the fix for each
hangar doctor --fix                                                         # apply the fixes it can make itself
```

`hangar doctor` checks:
- the providers;
- Cloud Build permissions (the `actAs` grant);
- recent failed sandbox starts (quota);
- the GitHub token's access to each mapped repo;
- the webhook secret;
- that each mapped repo has an active JSON webhook to hangar with the events hangar handles (issues, issue_comment, pull_request_review_comment, pull_request_review, check_run, check_suite, status and pull_request);
- that each mapped environment exists, has credentials, and is built;
- Slack, when configured: its credentials, the bot token, and that each mapped channel's repo is allowed;
- that checkpoint storage is writable.

`hangar doctor --fix` offers each fix it can make safely on its own: building an environment, or creating or repairing a repo's webhook (events, JSON, and the stored secret). `--yes` applies them without asking. For the rest it prints the exact command.

## 1. Give a service a token

A `jobs` token can submit and read its own jobs and nothing else. It gets 403 on every other route and 401 on `/mcp`, and it can't be exchanged for an MCP OAuth grant. Within jobs it is fenced in:

- it can only target repos listed in `HANGAR_GITHUB_REPOS`, in the environments mapped to them, and it can't pick a provider;
- its callbacks are signed with the key bound to the token by an admin, not one the caller names;
- it sees only the jobs it submitted (ownership is the token id, not its name).

```bash
hangar cred add support-hook-key --harness webhook --kind generic       # the shared signing key
HANGAR_GITHUB_REPOS='{"acme/app": "python", "https://git.acme.com/tools.git": "node"}'
hangar token create support-tool --scope jobs --callback-secret support-hook-key   # prints the token once
```

`HANGAR_GITHUB_REPOS` maps `owner/repo` (GitHub) or an exact git URL to one or more comma-separated environments; the first is the default. Admin tokens and MCP clients can submit against any repo and environment.

## 2. Submit a job

```bash
curl -sS https://hangar.example.com/api/jobs \
  -H "Authorization: Bearer $HANGAR_JOB_TOKEN" -H 'Content-Type: application/json' -d @- <<'EOF'
{
  "instructions": "CSV export drops non-ASCII characters. Fix it and add a regression test.",
  "repo": "https://github.com/acme/app",
  "environment": "python",
  "verify": "pytest -q",
  "context": [
    {"name": "trace.txt", "kind": "log", "content": "Traceback (most recent call last): ..."},
    {"name": "sample.csv", "kind": "file", "content": "bmFtZQrDqQo=", "encoding": "base64"},
    {"name": "ticket", "kind": "url", "url": "https://support.acme.com/t/4411"}
  ],
  "callback_url": "https://support.acme.com/hooks/hangar",
  "idempotency_key": "ticket-4411"
}
EOF
```

| Field | Meaning |
| --- | --- |
| `instructions` | What to do (required, up to 64 KB). |
| `repo`, `base_branch`, `branch` | The repo to work in; the base defaults to the repo's default branch, and the branch defaults to `hangar/<job>`. A custom branch must be under `hangar/` and must not exist yet: jobs never push to existing branches. |
| `environment`, `harness`, `model`, `provider` | Where and with what to run. Left out: the repo's [policy](repo-policy.md), then the first environment mapped in `HANGAR_GITHUB_REPOS`. |
| `verify` | The repo's own check. It runs inside the agent's turn, where a failure goes back to the agent ([§7](#7-an-agent-loop-that-finishes-the-job)), and hangar runs it again after the turn. A failing check still delivers, as a draft PR. Left out: the repo's [policy](repo-policy.md), then the environment's `verify:`; `""` means no check. |
| `verify_in_loop`, `max_verify_rounds` | Whether `verify` also runs inside the turn (default `true`), and how many failures go back to the agent per turn (default 3). See [§7](#7-an-agent-loop-that-finishes-the-job). |
| `autofix` | What the job's PR reacts to by itself: `ci,reviews` (default), `ci`, `reviews` or `off`. See [§7](#7-an-agent-loop-that-finishes-the-job). |
| `deliver` | `draft_pr` (default), `pr`, `branch` or `none`. |
| `context` | Files the agent is given in `~/.hangar/jobs/<job>/` (outside the repo, mode 0600), plus links listed in the prompt. Up to 50 items and 2 MB. |
| `callback_url`, `callback_secret` | Where to POST `job.started` and `job.finished`. `jobs` tokens always sign with their bound key; admins may name any `webhook` credential. |
| `idempotency_key` | Resubmitting with the same key returns the existing job. |
| `timeout_minutes`, `keep_minutes` | Limit per agent turn, and how long the sandbox stays after a turn for follow-ups. |
| `budget` | `{"max_minutes", "max_tokens", "max_cost_usd"}` per agent turn. A turn that crosses it is stopped, and what it did is delivered as a draft (`reason: "over_budget"`). See [Activity, usage and budgets](activity.md). |
| `job_budget` | `{"max_cost_usd", "max_tokens", "max_turns"}` for all the job's turns together: follow-ups, verify-fix and auto-fix turns, best-of-N attempts and the judge. Left out: the repo's policy, then `HANGAR_JOB_BUDGET_*`. See [repo-policy.md](repo-policy.md#budgets). |
| `output_schema` | JSON Schema for a structured result; it comes back as `result.data`. See [context exchange](context-exchange.md). |

`context` can also carry your local work in progress: `patch` (uncommitted changes) and `bundle` (unpushed commits) items, applied before the agent starts. `hangar job submit --from-local` builds them from the current checkout. See [context exchange](context-exchange.md).

Then poll `GET /api/jobs/{id}`, or wait for the callback. The job view carries `state`, the `events` timeline, `result` (commits, diffstat, `pr_url`, verification exit code and output tail, the agent's summary), `usage` (tokens and USD), `sandbox_id` and `terminal_session`. `GET /api/jobs/{id}/events?since=` streams what the agent is doing (messages, commands, file changes, errors), in the same shape for every harness.

From the CLI:

```bash
hangar job submit "fix the unicode bug in CSV export" --repo https://github.com/acme/app \
  --env python -c trace.txt --verify "pytest -q" --wait
hangar job list
hangar job follow-up job-3fa2c1d0 "also handle the empty-file case" --wait
hangar job cancel job-3fa2c1d0
hangar job events job-3fa2c1d0 --follow    # what the agent is doing
```

From an MCP client (Claude Code, Codex, …): `submit_job`, `get_job`, `list_jobs`, `follow_up_job`, `cancel_job`, `get_events`, `wait_for_turn(job)` to block with progress notifications until the turn ends, and `approve_job` / `reject_job` for jobs waiting for approval.

A `jobs` token can carry a budget cap (`hangar token create svc --scope jobs --max-cost 2`); its jobs never get more.

**Questions.** An agent that needs a decision runs `hangar-agent ask "…"`. The job goes to state `needs_input` with `result.question`: nothing is verified or pushed yet, a `job.needs_input` callback is sent, and a GitHub job comments the question on its issue or PR. Answer with a follow-up (`/hangar <answer>` on GitHub); the agent continues in the same conversation. Cancelling a job that needs input ends it.

To run several attempts and keep the best, see [§6](#6-forks-and-best-of-n).

From another agent (Google ADK's `RemoteA2aAgent`, any A2A client): hangar is an A2A agent at `/a2a`, with its card at `/.well-known/agent-card.json`. A task is a job, and a message in the same context is a follow-up. See [a2a.md](a2a.md).

## 3. Verify callbacks

Each callback is `POST` with `{"event": "job.started" | "job.needs_input" | "job.finished", "job": {...}}` and the header `X-Hangar-Signature: sha256=<hex HMAC-SHA256 of the raw body>`. A receiver checks it like this:

```python
import hashlib, hmac
ok = hmac.compare_digest(request.headers["X-Hangar-Signature"],
                         "sha256=" + hmac.new(KEY, request.body, hashlib.sha256).hexdigest())
```

Callbacks go only to public `https` hosts. Plain `http`, non-public addresses and internal names (including names that *resolve* to non-public addresses just before sending) need the host listed in `HANGAR_JOB_CALLBACK_HOSTS`. Context contents are never echoed in callbacks, and the agent's summary and verification output are redacted for known secrets before they leave hangar.

## 4. Trigger from GitHub

```bash
hangar setup github                    # asks for the repo, its environment and a token
hangar setup github --repo acme/app=python --token-file ~/.hangar/gh-token --non-interactive
```

The wizard:
1. **Checks the token** before storing anything. It must be able to read each repo and write to it, and to manage its webhooks. The token is read from a hidden prompt, `--token-file`, `--token-stdin` or `--token-from-gh` (the `gh` CLI's login), and it is never echoed or printed.
   - **Which token:** a [fine-grained token](https://github.com/settings/personal-access-tokens/new) for the repos, with repository permissions *Issues* and *Pull requests*: Read and write, *Webhooks*: Read and write, and *Actions*: Read (auto-fix reads failing CI logs with it). *Metadata*: Read is added automatically. A classic token needs the `repo` scope. GitHub doesn't open the check-runs API to fine-grained tokens, so with one, auto-fix describes a failing check from its webhook (title, summary, text) and its Actions log, without the check's annotations; a classic token gets those too.
   - **What it's for:** the control plane uses it to comment and open PRs; it never goes into a sandbox.
2. **Stores** the token as `gh-control` and a generated webhook secret as `gh-webhook` in the vault. Running the wizard again keeps the secret. `--rotate-secret` makes a new one and stores it only once every repo's webhook has it: if GitHub refuses one (a token with *Webhooks*: Read only), the hooks already changed are set back and the old secret stays, so deliveries keep verifying.
3. **Saves** `HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL` and `HANGAR_GITHUB_REPOS` in hangar's database. They apply at once and survive redeploys, with no config map to edit. If the server's environment (on GKE, `hangar-site`) also sets one of them, the environment wins, and the wizard tells you.
4. **Registers the webhook** on each repo, or updates the existing one: `https://<hangar>/api/hooks/github`, JSON, the stored secret, and the **issues**, **issue_comment**, **pull_request_review_comment**, **pull_request_review**, **check_run**, **check_suite**, **status** and **pull_request** events (`pull_request_review` through `status` drive [auto-fix](#7-an-agent-loop-that-finishes-the-job); with `pull_request` they also track [landing](landing.md)). A hook that lacks any of them, such as one set up before auto-fix that sent only the first three, gets the missing events added; `hangar doctor` names them and `hangar doctor --fix` adds them.
5. **Tests it twice.** First with a signed delivery through hangar's own route, then with a ping from GitHub, whose result it reads back from the hook's recent deliveries.

GitHub can't deliver to `localhost`. On a laptop the wizard still stores everything and skips the webhook; pass `--webhook-url https://<tunnel>` to register one through a tunnel. `--no-webhook` skips it if you'd rather add it by hand. A generated secret is never shown, so bring your own: `openssl rand -hex 32 > ~/.hangar/gh-webhook-secret`, then pass `--webhook-secret-file ~/.hangar/gh-webhook-secret`, and on GitHub use the URL, content type `application/json`, that secret, and the seven events above. Updating an existing hook keeps its other events and its SSL verification setting.

To set the same things by hand, use `hangar cred add gh-control --harness git --kind git_token`, `hangar cred add gh-webhook --harness webhook --kind generic`, and `hangar config set HANGAR_GITHUB_REPOS '{"acme/app": "python"}'`, together with the two `*_CREDENTIAL` settings.

Then:

- Label an issue `hangar` (`HANGAR_GITHUB_TRIGGER_LABEL`), or comment `/hangar` on it, to start a job. The issue body and recent comments by trusted people (by default owners, members and collaborators) become context; other people's comments are left out. `/hangar harness=codex model=gpt-5.5 also add a test` picks the harness and model and adds instructions.
- Hangar comments when it starts and when it finishes, with the summary, verification and PR link. The PR says `Closes #N`.
- Comment `/hangar <message>` on the PR to continue in the same sandbox and branch.

A `/hangar` comment or label from someone who isn't trusted (by default: not an owner, member or collaborator; or set `HANGAR_GITHUB_TRUSTED`) creates the job in `awaiting_approval`, and it runs only after a trusted `/hangar approve`. See [triggers.md](triggers.md). Only repos in `HANGAR_GITHUB_REPOS` are accepted, and GitHub's redelivery of the same event does not start a second job.

Jobs can also start from other systems' webhooks and on a schedule: see [triggers.md](triggers.md).

Jobs from every front door get the repo's defaults (`verify`, environment, harness, model) from its [policy](repo-policy.md), then from the environment: `hangar repo set acme/app --verify "pytest -q"`. A `/hangar env=…` option still picks one of the repo's allowed environments.

## 4a. Trigger from Slack

`/hangar <instructions>` or `@hangar <instructions>` in a mapped channel starts a job on the channel's repo. Hangar replies in a thread when the agent starts, needs input and finishes, and a reply in the thread is a follow-up. Setup (app manifest, credentials, channel map) is in [slack.md](slack.md).

## 4b. Trigger from Jira or GitHub Projects

An issue moved to a configured status (or given a label) in a mapped Jira project, or an issue moved to a configured column on a GitHub Projects board, starts a job. Hangar comments on the issue when the agent starts, asks its questions there (a comment answers), and moves the ticket's status as the job goes. Setup is in [intake.md](intake.md).

## 5. Work with the agent

Every job runs in a [session](sessions.md). The agent keeps its conversation between turns, and the job's sandbox, context and work in progress are checkpointed. So a reply always works:
- a `/hangar` comment on the PR a week later;
- a reply in the job's Slack thread;
- `hangar job follow-up`;
- the dashboard's reply box.

If the sandbox was released, hangar starts a new one from the job's last checkpoint, and the agent continues where it left off.

The same happens on its own after a control-plane restart (a deploy) that took the job's sandbox with it, and when the nightly power-down takes the sandbox pools away under a running turn (hangar itself stays up; the job waits for power-up, [gke.md](gke.md#6-sandbox-nodes-off-at-night-optional)). A job caught mid-turn whose session has a checkpoint goes back to the queue with a follow-up turn that resumes from it and tells the agent the restart happened; a job caught provisioning starts over, its check on the base commit included (also when its sandbox goes away under that check while hangar stays up). A best-of-N attempt the power-down cuts off resumes from its own session's checkpoint at power-up, the job waiting meanwhile. Each job (and each attempt) does this at most `HANGAR_JOB_RESTART_RESUMES` times, and such jobs re-enter the queue `HANGAR_JOB_RESTART_STAGGER_SECONDS` apart, so a power-up doesn't ask a cold cluster for every sandbox at once. A job with no checkpoint to resume from still fails as `interrupted`.

- **Watch:** attach to the job's sandbox terminal from the dashboard, or with `hangar sb attach <sandbox_id>` (the run is tmux session `terminal_session`).
- **Steer:** follow up from the API, MCP, CLI or a PR comment. Each follow-up is a new agent turn with your message and the previous summary, on the same branch. The PR updates in place. While a Claude Code or Codex turn runs, you can also steer or interrupt it, and answer its tool approvals when its environment uses `approvals: ask` (`POST /api/jobs/{id}/steer`, `/interrupt`, `/approvals`; MCP `steer_turn`, `answer_approval`; see [steering](steering.md)). An interrupt with a message carries on with it; without one, the job pauses in `needs_input` until you follow up or cancel, and nothing is delivered.
- **Take over:** work in the sandbox yourself. Your commits go out with the next follow-up's delivery.
- After a turn the sandbox stays up while anyone is using it, and at least `keep_minutes` (default 120). Then it is checkpointed and released.

## 6. Forks and best-of-N

For work where one try often misses (a flaky test, a tricky bug, an ambiguous refactor), ask for several attempts and keep the best:

```bash
hangar job submit "fix the flaky test_export" --repo https://github.com/acme/app --verify "pytest tests/test_export.py -q" \
  --best-of 3 --wait          # add --judge to have an agent compare the attempts that pass
hangar job attempts job-3fa2c1d0
```

Over REST or MCP: `"strategy": "best_of_n", "n": 3` (2–8), and optionally `"judge": true`.

How it runs:
1. Hangar prepares the job once: a sandbox, the branch checks and the context files. Then it checkpoints that state.
2. It [forks](sessions.md#forks-and-best-of-n) the job's session into N attempt sessions. Each attempt restores the checkpoint into its own sandbox, on branch `<job branch>-attempt-<i>`, and runs the agent on the same prompt. The agent sees `$HANGAR_ATTEMPT` (1…N). Each attempt is a full first turn: it gets the job's local work (`patch`/`bundle` context), its `output_schema` (with the repair run when the result is missing) and the [`HANGAR_IO`](context-exchange.md) protocol, and the job's `budget` caps each attempt (and the judge) as it caps any agent turn.
3. Each attempt is verified in its own sandbox, and what it left uncommitted is committed there. An attempt stopped over budget or by its timeout is still ranked (below those that finished cleanly), and delivered as a draft if it wins.
4. Ranking:
   - attempts that pass `verify` come first;
   - among those, a short judge turn orders them when `judge` is set (it reads each attempt's diff and summary); otherwise the smallest diff wins, then the earliest finish;
   - attempts whose diff changes, renames or deletes the verify command's own test files are flagged (`touched_tests`) and ranked last. These are the paths the command names or, when it names none (`pytest -q`, `make test`), any test file. New test files are fine.
5. The winner is delivered as the job's branch and PR. Its summary, structured result and artifacts are the job's. The PR body has a table of all attempts. The other attempts are checkpointed and their sandboxes released. With `HANGAR_BEST_OF_N_PUSH_ATTEMPTS=true`, they are also pushed as `<job branch>-attempt-<i>`.

If no attempt finished but one asked a question (`hangar-agent ask`), the first to ask carries the job: it goes to `needs_input` with that question, and the answer (a follow-up, a `/hangar` comment, a Slack thread reply or an A2A message) continues that attempt's session.

`get_job` and `GET /api/jobs/{id}` list `attempts`: session, branch, outcome, exit code, verify result, diffstat, lines changed, `touched_tests`, score, rank, summary and cost (from the attempt's [usage](activity.md)). The job's `usage` is the sum over every attempt, its repair run and the judge. `GET /api/jobs/{id}/events` is one stream across all of them (each event from an attempt carries `data.attempt`), so one `since` cursor works before and after delivery.

- **Follow-ups** continue the winner's session: same conversation, sandbox and branch.
- **Cancel** stops every attempt. A control-plane restart re-attaches to each running attempt. One attempt's sandbox dying (or its agent failing) doesn't fail the job; it is recorded and left out of the ranking.
- To carry on with an attempt that wasn't delivered, fork its session: `hangar session fork job-3fa2c1d0-attempt-2 "..."`.

**Cost:** N attempts are N sandboxes running at the same time and N agent runs (the judge adds one more short run in a sandbox of its own). The preparation runs once. A job holds one worker slot (`HANGAR_JOB_CONCURRENCY`) however many attempts it runs, so the sandbox count, not the job count, is what to watch: `HANGAR_BEST_OF_N_MAX` caps N, and `HANGAR_MAX_SANDBOXES` caps the fleet.

## 7. An agent loop that finishes the job

Two loops keep the agent working until the job's own checks agree it is done, without someone pasting failures back to it. Both are on by default. The design is items 5 and 6 of [spec 0012](specs/0012-cutting-edge.md).

### The verify gate inside the turn

With `verify` set, the agent sees a failing check *before* its turn ends, and fixes it in the same conversation:

- **Claude Code:** hangar passes a [Stop hook](https://code.claude.com/docs/en/hooks#stop) with `--settings '<json>'`. When Claude finishes, the hook runs the check in the repo. If the check fails, the hook exits 2 with the check's output on stderr, which sends Claude back to work with that output. The check has to pass, or the rounds have to run out, before the turn can end.
- **Codex** (in a [live turn](steering.md)): hangar's driver runs the check when the turn completes. A failure goes back into the same conversation as the next message.
- **Other harnesses** (pi, Antigravity, or Codex without python3 in the image): hangar starts a follow-up turn with the failure. It does this when the agent finished cleanly and hangar's own check then failed.

Rounds and limits:

- **Per turn:** each failure sent back is a round. A turn and any follow-up turns it needs share `max_verify_rounds` (default 3, or `HANGAR_MAX_VERIFY_ROUNDS`).
- **Time:** the in-turn gate stops sending failures back when the turn's time limit is close. It leaves room for one more check and a fix.
- **Budget:** the rounds share the turn's [budget](activity.md). Follow-up turns get what is left of it, not a fresh one.
- **Job view:** each turn shows `verify_rounds`, and hangar's follow-up turns show `trigger: "verify"`.

**Hangar's own check after the turn still decides delivery.** The gate is advisory: it helps an agent that wants to pass the check, and it is not a control against one that doesn't. It runs inside the sandbox, as the agent's user, so the agent can get past it:

- **Settings files don't switch the hook off.** It lives on the command line, not in a file, and its `disableAllHooks: false` outranks the user, project and local settings files. This was verified with Claude Code 2.1.296: `disableAllHooks: true` in all three files leaves the hook running.
- **Other ways around it remain.** Managed settings (`/etc/claude-code/managed-settings.json`) outrank the command line, and the agent can write them when the image gives it `sudo`, which is the default. The agent can also use up the rounds by writing to the rounds file, or put its own `claude`, `pytest` or other command first on `PATH` for this turn or, through `~/.hangar/env`, for later ones.
- **In a split sandbox with `approvals: ask` or `deny-risky`**, hangar starts Claude Code itself: by its path, with its own environment and settings ([hangar-owned harness settings](steering.md#hangar-owned-harness-settings)). There the agent can't switch the Stop hook off for a later turn. It can still use up the rounds, and the check it runs is the repository's own code.

What decides is hangar's check after the turn. The hook's result never feeds delivery: hangar runs the job's `verify` itself, and only that exit code decides between a PR and a draft.

That check also runs in the sandbox, through the agent's `~/.hangar/env` (see the [threat model](security.md)). So it tells you whether the work passes, and it can't prove that the agent didn't game it. Review the PR as you would any contributor's. Two things help:

- **The check stays the job's own.** The command is the job's `verify`, stored by hangar, and the agent can't change it.
- **Edited tests are flagged.** The agent can change files the check reads. So when the gate is on, hangar runs [best-of-N's test check](#6-forks-and-best-of-n) on every job: a diff that changes, renames or deletes the check's own test files is listed in `result.touched_tests`. It is also called out in the PR body, and the PR is delivered as a draft. The check covers the test files only. When `verify` names paths, it covers those paths; otherwise, any test file and `conftest.py`. It doesn't cover runner configs such as `pyproject.toml`, `pytest.ini`, `package.json` or a `Makefile`.

Turn the gate off per job with `"verify_in_loop": false` (`--no-verify-in-loop`), or for everything with `HANGAR_VERIFY_IN_LOOP=false`.

**A red base.** A standing check (from the repo's policy or the environment) runs once on the base commit before the agent starts, shared by every job on that base. When the base already fails it, the gate is off, and only the failures the change adds count against the job. See [repo-policy.md](repo-policy.md#the-check-on-the-base-commit).

**Scratch files.** Notes the agent leaves in the repo (`HANDOFF.md`, `RESULT.md`, `NOTES.md` at the root, `*.orig` and `*.rej`) are kept out of the PR unless the task names them. See [repo-policy.md](repo-policy.md#agent-scratch-files).

### PR auto-fix

After hangar opens a job's PR, it reacts to the PR's CI results and reviews. It does this by starting a follow-up turn on the job's session, as a `/hangar` comment would:

- **CI:** a `check_run` or `check_suite` that completes as `failure`, `timed_out` or `startup_failure`, or a commit `status` of `failure` or `error`. It counts only on the commit hangar last pushed to the PR (the job's `result.head_sha`).
  - The agent gets `ci-failures.md` with each failing check's output and annotations, and, for GitHub Actions, the end of the job's log (`HANGAR_AUTOFIX_LOG_BYTES`, plus the `##[error]` lines before it).
  - Logs are read with the control plane's token. The download link GitHub redirects to is fetched without the token, and only from GitHub's log storage (`*.actions.githubusercontent.com`, `*.blob.core.windows.net`) or the API's own host. For other hosts the agent gets the check's output without the log.
  - Only checks tied to the job's own PR count: by its number or the job's branch, and the job's head commit. A fork's PR can reuse the branch name and the commit, but its checks arrive without either.
  - Anyone who can post a check or a status on that commit can start a round: people with write access, and apps with the *Checks* or *Commit statuses* permission. What they post is fenced as untrusted text in the agent's prompt, and the round limit applies.
- **Reviews:** a review that requests changes, or that comments with inline comments, and inline review comments.
  - The round runs with the job's person's own login, so a review counts only from someone they or the repo policy trust ([who is trusted](team.md#who-is-trusted), rule 6): the job's person (their linked GitHub account), an admin's linked account, a login in the repo policy's `approvers`, or a code owner of every path the PR touches. On a team job (no person), also anyone the GitHub front door trusts (a linked teammate, the repo's owners, members and collaborators, or `HANGAR_GITHUB_TRUSTED`). Never a bot, hangar's own App (`<slug>[bot]`) or hangar's own account and comments. A refused review is audited (`github.refused`, `untrusted review`).
  - The agent gets `review-comments.md` with each comment's author, file, line and diff hunk. Every comment is fenced and the file is an untrusted attachment, whoever wrote it: the instruction is hangar's ("address each comment that asks for a change to this task's code"), never the reviewer's. The agent is told to ask (`hangar-agent ask`, so the job goes to `needs_input`) when a comment is ambiguous, instead of guessing.

Loop control:

- **Coalescing:** everything that arrives within `HANGAR_AUTOFIX_COALESCE_SECONDS` (60) becomes one turn. Ten checks failing on one push make one round.
- **One CI round per push:** if the agent pushed nothing new, the same commit failing again doesn't start another round. Neither do hangar's own comments.
- **Round limit:** at most `HANGAR_AUTOFIX_MAX_ROUNDS` (3) rounds per PR, at least `HANGAR_AUTOFIX_COOLDOWN_SECONDS` (120) apart. After the last round, hangar says once on the PR that auto-fix stopped.
- **Someone else's push:** if the PR's head isn't the commit hangar pushed, hangar won't push over it. It says so once on the PR and pauses until it pushes again.
- **On the PR:** hangar comments at the start of each round with what failed. The turn's result is reported as usual.
- **Job state:** a round waits while a turn is running. Rounds don't start for a job that is cancelled or waiting for an answer.
- **Restarts:** triggers still waiting for their window survive a control-plane restart.

Settings:

- **Per repo:** `HANGAR_GITHUB_AUTOFIX='{"acme/app": "ci"}'`, with the values `ci,reviews`, `ci`, `reviews` or `off`.
- **Per job:** `"autofix"` in the request (`--autofix` on the CLI, `autofix` in MCP `submit_job`). A job's setting can only narrow its repo's.
- **Default:** `ci,reviews` for every PR hangar opens. PRs that hangar didn't open are never touched.
- **The job view:** `autofix_state` shows the rounds, the pending triggers and why the loop stopped.

The webhook needs the `pull_request_review`, `check_run`, `check_suite` and `status` events ([§4](#4-trigger-from-github)): run `hangar setup github` again, or `hangar doctor --fix`.

## 8. Test results

Every check hangar runs leaves structured results, not just an exit code and the end of its output: the check on
the base commit, the check after each turn (and its [flaky rerun](#flaky-tests)), best-of-N attempts, agent-led
setup's validation, and the checks the [in-turn gate](#the-verify-gate-inside-the-turn) runs for the agent. The deploy
acceptance checks are recorded the same way. The design is in `src/hangar/testresults.py`; the one report parser is
`src/hangar/testreports.py`.

### Where the reports come from

Around the check, in the same shell and the same exec, hangar:

1. makes a fresh report directory, `$HANGAR_TESTS_DIR` (`~/.hangar/tests/<run>/`), and marks the run's start;
2. sets the variables the common runners read, unless the environment already sets them:
   - **pytest:** `PYTEST_ADDOPTS=--junitxml=$HANGAR_TESTS_DIR/pytest.xml`, added to what `PYTEST_ADDOPTS` already holds,
     and not at all when it already names a JUnit file, when the check's command names `junitxml`, or when the
     repo's `pytest.ini`, `pyproject.toml`, `setup.cfg` or `tox.ini` does (PYTEST_ADDOPTS comes after the ini's
     `addopts`, so it would replace a report path the repo sets, and `-p no:junitxml` would make pytest stop with a
     usage error). Then the repo's own report is read where it puts it.
   - **gotestsum:** `GOTESTSUM_JUNITFILE` (gotestsum reads the variable as it reads `--junitfile`).
   - **jest-junit:** `JEST_JUNIT_OUTPUT_DIR`, when the repo uses the `jest-junit` reporter.
3. runs the check, unchanged: its output and exit code are its own;
4. reads the report files the run wrote (newer than its start, regular files, not links) from that directory, from the
   paths the repo declares, and from the usual places: Maven surefire and failsafe and Gradle (`TEST-*.xml`),
   cargo-nextest (`target/nextest/<profile>/junit.xml`), vitest and jest (`*junit*.xml`), `test-results/` and
   `test-reports/`, and coverage: `coverage.xml` (coverage.py, pytest-cov), `cobertura*.xml` (istanbul), `lcov.info`
   and `*.lcov`.

vitest's `junit` reporter, nextest's JUnit output and a `--junitxml` elsewhere aren't set by a variable. Name where
they go with `verify_reports`, in the repo's `.hangar/environment.yaml` (it comes first) or its
[policy](repo-policy.md):

```yaml
verify: npx vitest run --reporter=junit --outputFile=reports/vitest.xml
verify_reports: [reports/*.xml, "**/coverage/lcov.info"]   # relative to the repo; **/ for any depth
```

`HANGAR_TEST_REPORTS=collect` reads reports without setting any variable; `off` does neither.

**Reports are untrusted.** The repo's code writes them. They are read as the sandbox's user, the same one that ran
the check, and parsed with defusedxml's rules: an entity declaration or an external entity reference stops the parse
(no XXE, no entity expansion), no DTD is fetched, and a report is capped at 8 MiB, 2 million elements, 64 levels deep
and 100,000 test cases (64 files and 32 MiB a run). Messages are clipped and pass through `broker.redact` before they
are kept.

### What is kept

A **test run** per run of a check: the counts (passed, failed, error, skipped), the first 200 failures with their
messages, every case's outcome keyed by test id (compressed, up to 20,000, kept 3 days, and a base check's 14), the
coverage totals, and the commit and tree it ran on. Runs are kept 14 days. A test id is `classname::name`, as the
runner wrote it (`tests.test_math::test_div[0-1]`, `example.com/app/server::TestServe/timeout`).

On a red base, the failures the change adds are the after-turn run's failing ids minus the base run's, exactly,
whenever both left complete reports ([repo-policy.md](repo-policy.md#the-check-on-the-base-commit)). The text of the
output is the fallback. Reports cover only the runners that write one: when they show nothing new but the output
names a failure the base didn't have (a second runner without a JUnit reporter), hangar can't tell, and the PR goes
out as a draft that says so.

### Flaky tests

A test is **flaky** when, in one repo and environment:

- it passed and failed on the same tree: the working tree's content, uncommitted changes included (two base checks of
  one commit by two people, a verify-fix round that changed nothing, a rerun); or
- the runner retried it and it passed (surefire's and nextest's `flakyFailure`).

Each repo keeps a list of them, each test dropped 30 days after it was last seen flaky. The in-turn gate's runs never
add to it: the agent's own sandbox wrote them while the agent worked. With the repo policy's `rerun_flaky`, a check
whose every failure is on the list (on a red base: every new failure) runs once more, and the rerun decides. The
review packet marks the failing tests that are on the list, and the [Cloud Doctor](doctor.md) counts a failed check
whose every failure is on it as transient.

The list is advisory. Even hangar's own checks run the repo's code, which the agent may have written, and that code
writes the reports (a `flakyFailure`, a test that passes once): so a job's failures are excused (rerun, "flaky" in its
packet, "transient" for the doctor) only by entries on the list before the job started, never by what its own checks
added. The tree a run is filed under is read in an exec of its own before the check, so the check can't change it.

### Where it shows

- **The job:** `result.tests` on `get_job` and `wait_for_turn`, and on each best-of-N attempt: counts, the first
  failing ids, `new_failures` against the base, `flaky`, line `coverage` and `coverage_delta` against the base.
- **Events:** a `progress` event with `data.action: "tests"` in the job's and the session's events, and a line in the
  job's timeline.
- **The agent:** `TEST-RESULTS.json` in the job's directory, rewritten after each of hangar's runs: every failure
  with its message (up to 2,000), what is new against the base, the base's own failures, the flaky ones and coverage.
  The prompt and each verify-fix message point at it, so the agent searches a file instead of scrolling output.
- **The review packet:** pass, fail and skip counts, new failures against the base, flaky tests, and the coverage
  change against the base.
- **REST:** `GET /api/jobs/{id}/tests` and `GET /api/sessions/{ref}/tests`: the summary, every run, and the latest in
  full. `?run=<id>` gives one run, `&cases=true` every case, and `?format=otel` the runs as spans (below).
  `GET /api/repos/tests/flaky?repo=` is a repo's list (admins). The resource `hangar://jobs/<id>/tests`.
- **`hangar status`:** the repos with the most flaky tests, with their worst.

### In traces

Each run is span events in the trace export's taxonomy (`hangar.trace/v1`, which reserves `test.run` and `test.case`
for them), with OpenTelemetry's
[test semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/test/) (stability:
development) alongside:

- `test.run`: `test.suite.name` (the command), `test.suite.run.status` (`success`, `failure`, `timed_out`,
  `aborted`), and the totals under `hangar.test.*` (`total`, `passed`, `failed`, `errors`, `skipped`, `flaky`,
  `complete`, `coverage.line_rate`, `run.id`, `run.kind`, `exit_code`), with `vcs.ref.head.revision`;
- `test.case`, one per failing or flaky test: `test.case.name`, `test.case.result.status` (`pass` or `fail`),
  `hangar.test.outcome` (`failed` or `error`), `hangar.test.flaky` and the start of `hangar.test.message`. Passing tests
  are counted, not listed.

With tracing on ([observability.md](observability.md)) they go on the span that ran the check (`job.verify`, the
job's provisioning for the base). `GET /api/jobs/{id}/tests?format=otel` serves them as data, and
`testresults.add_to_export` puts them on a job export's `job` span.

## 9. Experiments

An experiment runs the same tasks under several configurations, each several times, so a tool outside hangar can
compare them. It is a job of kind `experiment`; each trial is an ordinary job of its own (`source: experiment`,
`deliver: none`).

```yaml
# skills-ab.yaml
name: skills-ab
environment: hangar
tasks:
  - {id: fix-retry, instructions: "Make the reaper retry a failed destroy.",
     repo: https://github.com/Sean-Koval/hangar.git, verify: "uv run pytest -q tests/test_fleet.py"}
variants:
  control: {}
  no-skills: {skills: []}
  codex: {harness: codex, model: balanced}
trials: 3
trial_job_budget: {max_cost_usd: 3}
budget: {max_cost_usd: 40}
```

```bash
hangar experiment run skills-ab.yaml --wait   # or POST /api/experiments
hangar experiment get job-1a2b3c4d             # the index: pins, and per trial its job, verify, grade
hangar experiment export job-1a2b3c4d -o out/  # index.json and each trial's manifest.json and trace.json
```

Hangar pins the harness version, the model id and each task's base commit, mounts only the trial's harness, writes
exactly the variant's skills and instructions, and keeps egress the same for every variant. It records what each
trial ran with (its [manifest](api/manifest.schema.json)), what it did (its [trace](api/traces.md)) and its
verification, and runs an optional `grader.command` in a clean sandbox on each trial's patch. It doesn't compare the
variants: that is the sister tool's job. The details are in [spec 0014](specs/0014-sister-tool-interfaces.md#4-experiment-jobs-hangarexperimentv1).

### Grading

```yaml
grader:
  command: hangar-grade pytest -q       # the hidden tests, from their pristine copy
  files: {"test_retry.py": "..."}       # written to $HANGAR_HIDDEN_TESTS, outside the repo
  network: none                         # or mirror: hangar's read-only package mirror, for a grader that installs
  # files_mode: in_repo                 # the old layout (files in the repo); the grade says trust: low
  # canary: HANGAR-CANARY-...           # generated per experiment if unset; never reuse one
tasks:
  - {id: fix-retry, ..., reference_patch: "diff --git ..."}   # optional: a known fix the hidden tests must pass
```

The grader runs agent-written code beside the hidden tests, so its sandbox is sealed: no credential, `secrets:` or
cloud token, a fetch-only git grant that is revoked before the command runs, none of the environment's `mcp:`,
`computer:` or services, and egress `none` with no host at all, not even the harness's model API, on the proxy-only
network whatever the environment's egress says ([security](security.md#graders)). A dependency that isn't in the
image (the environment's, or its repo build's) can't be fetched: use `network: mirror` or a repo build.

The command runs in the repo with the trial's patch applied, and gets `HANGAR_HIDDEN_TESTS` (the grader's `files`,
outside the repo; on Docker root-owned, so the trial's code can read them and not change them), `HANGAR_REPO`,
`HANGAR_GRADE_OUT`, `HANGAR_TRIAL`, `HANGAR_TASK`, `HANGAR_VARIANT` and `HANGAR_EXPERIMENT`.

`hangar-grade pytest [args]` runs the hidden tests so that nothing the patch added loads first: `python -I` (no
`sitecustomize.py`, `usercustomize.py`, user site or `PYTHON*` variables), `--noconftest`, `-c /dev/null` and
`addopts` cleared (no repo `conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg` or `pyproject` settings), no cache, no
bytecode, and the repo on `sys.path` only once pytest has started (a plugin the patch declares never loads). A
`conftest.py` among the hidden tests still works (its fixtures and hooks; not options it adds). Its junit counts are
stored as `grade.hidden_tests`.

Other ecosystems get no safe runner yet; run them from a copy you control and treat the grade as best effort: `jest
--config "$HANGAR_HIDDEN_TESTS/jest.config.js" --rootDir "$HANGAR_REPO"` with a config of your own, or copy the
hidden `_test.go` files into a fresh copy of the package before `go test`.

Each grade records `files_mode`, `network`, `runner` (`hangar-grade` or `command`) and `tamper_signals`: files the
patch adds or changes that can hook a test run (`conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth`,
`pytest.ini`, a pytest section in `tox.ini`, `setup.cfg` or `pyproject.toml`). They are signals, not refusals.

**Who writes the grade.** On Docker, where the sandbox has the [uid split](steering.md#the-uid-split), a wrapper
running as hangar's own user starts the command as the agent's user (no capabilities, `no_new_privs`) and writes the
grade itself, in a 0700 directory the agent can't reach: the exit code, the output, `$HANGAR_GRADE_OUT` and the junit
`hangar-grade` writes come to it through pipes it created (`$HANGAR_GRADE_OUT` is `/proc/self/fd/198`: write it, don't
look for a file). A sandbox where the agent has `sudo` (the environment's default) can't be split.

**Trust.** Each grade, and each trial in the index, says how far it can be trusted:

| `trust` | When |
| --- | --- |
| `isolated` | The uid split, the pristine copy and `hangar-grade` |
| `best_effort` | No uid split (GKE today, or an environment with `sudo`): the trial's code could write `$HANGAR_GRADE_OUT` |
| `low` | `files_mode: in_repo`, or a command that isn't `hangar-grade` itself |

Code under test runs inside the test runner's process, so it can still bend the runner while it runs (patch pytest's
reporting from the module under test). Isolation stops writes to the grade, not lies through the runner. These catch
what it doesn't:

- **Base must fail.** Once per task per experiment, the hidden tests run on the pinned base without a patch. If they
  pass there the task can't tell a fix from no fix: `task_checks.<task>.invalid` with `hidden tests pass on base`, and
  every grade of the task says `task_invalid`. With `reference_patch` they also run on it, and must pass. One grader
  run per task, not per trial.
- **Canaries.** Hangar writes the grader's canary as a comment line into every hidden test file that takes comments
  (`canary_files` counts them). After each trial it looks for it in the trial's patch, its `RESULT.md`, its trace
  export and the egress proxy's records of its sandbox. A hit makes the trial `contaminated` (with `contamination`
  saying where), adds a note to the experiment and a `grade.canary_seen` audit entry. The canary is kept only in the
  experiment's spec, and is masked in every export.
- **Touched tests.** `touched_tests`: existing test files the trial changed (the job's own check, else from its
  patch).

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_JOB_CONCURRENCY` | 0 (auto) | Jobs provisioning or running at once. Auto: 4 on Docker, else `HANGAR_MAX_SANDBOXES` / 5 between 4 and 8 ([team-scale.md](team-scale.md)) |
| `HANGAR_PERSON_MAX_RUNNING_JOBS` | 0 (half the slots, at least 4) | Jobs one person runs at once; the queue serves whoever has the fewest running first |
| `HANGAR_JOB_TIMEOUT_MINUTES` | 60 | Per agent turn |
| `HANGAR_JOB_KEEP_MINUTES` | 120 | Sandbox kept after a turn |
| `HANGAR_JOB_VERIFY_TIMEOUT_SECONDS` | 1800 | |
| `HANGAR_JOB_MAX_CONTEXT_BYTES` / `_ITEMS` | 2 MB / 50 | |
| `HANGAR_JOB_CALLBACK_HOSTS` | `[]` | Hosts allowed for http or internal callbacks |
| `HANGAR_JOB_RESTART_RESUMES`, `HANGAR_JOB_RESTART_STAGGER_SECONDS` | 2, 20 | How often a job a restart interrupted resumes on its own, and how far apart such jobs re-enter the queue (§5) |
| `HANGAR_BUDGET_MAX_MINUTES` / `_MAX_TOKENS` / `_MAX_COST_USD`, `HANGAR_MODEL_PRICES` | unset | Default budget per turn and the price table; see [activity.md](activity.md) |
| `HANGAR_JOB_BUDGET_MAX_COST_USD` / `_MAX_TOKENS` / `_MAX_TURNS` | unset | Default budget for a whole job; see [repo-policy.md](repo-policy.md#budgets) |
| `HANGAR_BUDGET_DAY_TIMEZONE` | `UTC` | The IANA zone whose midnight starts a day for daily spend caps, e.g. `America/New_York` |
| `HANGAR_PERSON_MAX_USD_PER_DAY` | unset | What one person's sessions and jobs may spend in a day before their new jobs wait for approval; see [repo-policy.md](repo-policy.md#budgets) |
| `HANGAR_BEST_OF_N_MAX` | 8 | Most attempts per best-of-N job |
| `HANGAR_BEST_OF_N_PUSH_ATTEMPTS` | false | Also push attempts that weren't delivered |
| `HANGAR_BEST_OF_N_JUDGE_TIMEOUT_MINUTES` | 15 | The judge turn's limit |
| `HANGAR_VERIFY_IN_LOOP`, `HANGAR_MAX_VERIFY_ROUNDS` | true, 3 | The verify gate inside the turn; see [§7](#7-an-agent-loop-that-finishes-the-job) |
| `HANGAR_TEST_REPORTS` | `inject` | `inject`: set the reporter variables and read the reports; `collect`: only read them; `off`. See [§8](#8-test-results) |
| `HANGAR_GITHUB_AUTOFIX`, `HANGAR_AUTOFIX_MAX_ROUNDS`, `HANGAR_AUTOFIX_COOLDOWN_SECONDS`, `HANGAR_AUTOFIX_COALESCE_SECONDS`, `HANGAR_AUTOFIX_LOG_BYTES` | `{}`, 3, 120, 60, 16000 | PR auto-fix; see [§7](#7-an-agent-loop-that-finishes-the-job) |
| `HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL`, `HANGAR_GITHUB_REPOS`, `HANGAR_GITHUB_TRIGGER_LABEL` | | See §4 |
| `HANGAR_GITHUB_TRUSTED`, `HANGAR_JOB_APPROVAL_TTL_HOURS`, `HANGAR_INBOUND_WEBHOOKS`, `HANGAR_SCHEDULE_POLL_SECONDS` | | See [triggers.md](triggers.md) |
| `HANGAR_SLACK_SIGNING_SECRET_CREDENTIAL`, `HANGAR_SLACK_BOT_TOKEN_CREDENTIAL`, `HANGAR_SLACK_CHANNELS`, `HANGAR_SLACK_USERS` | | See [slack.md](slack.md) |
| `HANGAR_JIRA`, `HANGAR_GITHUB_PROJECTS` | `{}` | See [intake.md](intake.md) |

## Security notes

Issue text, comments and service context are untrusted input to the agent. Today's mitigations:

- the repo and environment allowlist for service tokens and GitHub triggers;
- untrusted triggers wait for approval, only trusted people's comments become context, and an untrusted author's text is marked as untrusted in the prompt ([triggers.md](triggers.md));
- `jobs`-only service tokens that can't reach MCP or OAuth;
- secret redaction of everything the agent writes that leaves hangar (summaries, verification output, PR bodies, comments, callbacks);
- pushes only to new `hangar/` branches, never forced;
- a prompt that marks context as data;
- CI output, logs and review comments handed to auto-fix turns are untrusted attachments, fenced in the prompt like an outsider's issue text, and only reviewers the job's person or the repo policy trusts start a turn.

Known gap: the callback check resolves the host before sending but doesn't pin the address, so DNS rebinding is not fully blocked. Without the [git gateway](security.md#git-credential-gateway) the agent's sandbox holds a git credential, so restrict `HANGAR_GITHUB_REPOS` to repos where a mistaken push to a `hangar/*` branch is acceptable, and keep PR merges human-approved. With `HANGAR_GATEWAY_URL` set, a job's sandbox has no GitHub token and pushes nothing: its grant is fetch-only, and hangar pushes the commit it checked, after verify, the secret scan and the landing checks ([security.md](security.md#jobs-only-fetch-hangar-delivers)).
