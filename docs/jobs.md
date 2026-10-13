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
- that each mapped repo has an active JSON webhook to hangar with the issues, issue_comment and pull_request_review_comment events;
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
| `environment`, `harness`, `model`, `provider` | Where and with what to run. The environment can be left out for repos mapped in `HANGAR_GITHUB_REPOS`. |
| `verify` | The repo's own check, run by hangar after each turn. A failing check still delivers, as a draft PR. |
| `deliver` | `draft_pr` (default), `pr`, `branch` or `none`. |
| `context` | Files the agent is given in `~/.hangar/jobs/<job>/` (outside the repo, mode 0600), plus links listed in the prompt. Up to 50 items and 2 MB. |
| `callback_url`, `callback_secret` | Where to POST `job.started` and `job.finished`. `jobs` tokens always sign with their bound key; admins may name any `webhook` credential. |
| `idempotency_key` | Resubmitting with the same key returns the existing job. |
| `timeout_minutes`, `keep_minutes` | Limit per agent turn, and how long the sandbox stays after a turn for follow-ups. |
| `budget` | `{"max_minutes", "max_tokens", "max_cost_usd"}` per agent turn. A turn that crosses it is stopped, and what it did is delivered as a draft (`reason: "over_budget"`). See [Activity, usage and budgets](activity.md). |
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
   - **Which token:** a [fine-grained token](https://github.com/settings/personal-access-tokens/new) for the repos, with repository permissions *Issues* and *Pull requests*: Read and write, and *Webhooks*: Read and write. *Metadata*: Read is added automatically. A classic token needs the `repo` scope.
   - **What it's for:** the control plane uses it to comment and open PRs; it never goes into a sandbox.
2. **Stores** the token as `gh-control` and a generated webhook secret as `gh-webhook` in the vault. Running the wizard again keeps the secret. `--rotate-secret` makes a new one and stores it only once every repo's webhook has it: if GitHub refuses one (a token with *Webhooks*: Read only), the hooks already changed are set back and the old secret stays, so deliveries keep verifying.
3. **Saves** `HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL` and `HANGAR_GITHUB_REPOS` in hangar's database. They apply at once and survive redeploys, with no config map to edit. If the server's environment (on GKE, `hangar-site`) also sets one of them, the environment wins, and the wizard tells you.
4. **Registers the webhook** on each repo, or updates the existing one: `https://<hangar>/api/hooks/github`, JSON, the stored secret, and the **issues**, **issue_comment** and **pull_request_review_comment** events. A hook that only sent `push` gets the missing events added.
5. **Tests it twice.** First with a signed delivery through hangar's own route, then with a ping from GitHub, whose result it reads back from the hook's recent deliveries.

GitHub can't deliver to `localhost`. On a laptop the wizard still stores everything and skips the webhook; pass `--webhook-url https://<tunnel>` to register one through a tunnel. `--no-webhook` skips it if you'd rather add it by hand. A generated secret is never shown, so bring your own: `openssl rand -hex 32 > ~/.hangar/gh-webhook-secret`, then pass `--webhook-secret-file ~/.hangar/gh-webhook-secret`, and on GitHub use the URL, content type `application/json`, that secret, and the three events above. Updating an existing hook keeps its other events and its SSL verification setting.

To set the same things by hand, use `hangar cred add gh-control --harness git --kind git_token`, `hangar cred add gh-webhook --harness webhook --kind generic`, and `hangar config set HANGAR_GITHUB_REPOS '{"acme/app": "python"}'`, together with the two `*_CREDENTIAL` settings.

Then:

- Label an issue `hangar` (`HANGAR_GITHUB_TRIGGER_LABEL`), or comment `/hangar` on it, to start a job. The issue body and recent comments by trusted people (by default owners, members and collaborators) become context; other people's comments are left out. `/hangar harness=codex model=gpt-5.5 also add a test` picks the harness and model and adds instructions.
- Hangar comments when it starts and when it finishes, with the summary, verification and PR link. The PR says `Closes #N`.
- Comment `/hangar <message>` on the PR to continue in the same sandbox and branch.

A `/hangar` comment or label from someone who isn't trusted (by default: not an owner, member or collaborator; or set `HANGAR_GITHUB_TRUSTED`) creates the job in `awaiting_approval`, and it runs only after a trusted `/hangar approve`. See [triggers.md](triggers.md). Only repos in `HANGAR_GITHUB_REPOS` are accepted, and GitHub's redelivery of the same event does not start a second job.

Jobs can also start from other systems' webhooks and on a schedule: see [triggers.md](triggers.md).

## 4a. Trigger from Slack

`/hangar <instructions>` or `@hangar <instructions>` in a mapped channel starts a job on the channel's repo. Hangar replies in a thread when the agent starts, needs input and finishes, and a reply in the thread is a follow-up. Setup (app manifest, credentials, channel map) is in [slack.md](slack.md).

## 5. Work with the agent

Every job runs in a [session](sessions.md). The agent keeps its conversation between turns, and the job's sandbox, context and work in progress are checkpointed. So a reply always works:
- a `/hangar` comment on the PR a week later;
- a reply in the job's Slack thread;
- `hangar job follow-up`;
- the dashboard's reply box.

If the sandbox was released, hangar starts a new one from the job's last checkpoint, and the agent continues where it left off.

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

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_JOB_CONCURRENCY` | 4 | Jobs provisioning or running at once |
| `HANGAR_JOB_TIMEOUT_MINUTES` | 60 | Per agent turn |
| `HANGAR_JOB_KEEP_MINUTES` | 120 | Sandbox kept after a turn |
| `HANGAR_JOB_VERIFY_TIMEOUT_SECONDS` | 1800 | |
| `HANGAR_JOB_MAX_CONTEXT_BYTES` / `_ITEMS` | 2 MB / 50 | |
| `HANGAR_JOB_CALLBACK_HOSTS` | `[]` | Hosts allowed for http or internal callbacks |
| `HANGAR_BUDGET_MAX_MINUTES` / `_MAX_TOKENS` / `_MAX_COST_USD`, `HANGAR_MODEL_PRICES` | unset | Default budget per turn and the price table; see [activity.md](activity.md) |
| `HANGAR_BEST_OF_N_MAX` | 8 | Most attempts per best-of-N job |
| `HANGAR_BEST_OF_N_PUSH_ATTEMPTS` | false | Also push attempts that weren't delivered |
| `HANGAR_BEST_OF_N_JUDGE_TIMEOUT_MINUTES` | 15 | The judge turn's limit |
| `HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL`, `HANGAR_GITHUB_REPOS`, `HANGAR_GITHUB_TRIGGER_LABEL` | | See §4 |
| `HANGAR_GITHUB_TRUSTED`, `HANGAR_JOB_APPROVAL_TTL_HOURS`, `HANGAR_INBOUND_WEBHOOKS`, `HANGAR_SCHEDULE_POLL_SECONDS` | | See [triggers.md](triggers.md) |
| `HANGAR_SLACK_SIGNING_SECRET_CREDENTIAL`, `HANGAR_SLACK_BOT_TOKEN_CREDENTIAL`, `HANGAR_SLACK_CHANNELS`, `HANGAR_SLACK_USERS` | | See [slack.md](slack.md) |

## Security notes

Issue text, comments and service context are untrusted input to the agent. Today's mitigations:

- the repo and environment allowlist for service tokens and GitHub triggers;
- untrusted triggers wait for approval, only trusted people's comments become context, and an untrusted author's text is marked as untrusted in the prompt ([triggers.md](triggers.md));
- `jobs`-only service tokens that can't reach MCP or OAuth;
- secret redaction of everything the agent writes that leaves hangar (summaries, verification output, PR bodies, comments, callbacks);
- pushes only to new `hangar/` branches, never forced;
- a prompt that marks context as data.

Known gap: the callback check resolves the host before sending but doesn't pin the address, so DNS rebinding is not fully blocked. Without the [git gateway](security.md#git-credential-gateway) the agent's sandbox holds a git credential, so restrict `HANGAR_GITHUB_REPOS` to repos where a mistaken push to a `hangar/*` branch is acceptable, and keep PR merges human-approved. With `HANGAR_GATEWAY_URL` set, jobs push through the gateway: the sandbox has no GitHub token and can push only its own branch and `hangar/**`.
