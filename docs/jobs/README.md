# Jobs: handing work to cloud agents

A job is a piece of development work handed to hangar by a service, an app, an MCP client, the CLI, a GitHub issue or a Slack channel. hangar runs a coding agent on it in a fresh sandbox, checks the result with the repo's own command, and delivers a branch and a (draft) PR. Engineers can follow along, reply, or take over. The design is in [spec 0009](../specs/0009-jobs-and-the-factory-front-door.md).

```
caller ──POST /api/jobs──► queue ─► sandbox (env + harness) ─► agent turn ─► verify ─► commit, push hangar/<job>, PR
   ▲                                                      ▲                                  │
   └──── signed callback: job.started / job.finished ─────┼──────────────────────────────────┘
                     engineer: follow-up (API, MCP, CLI, `/hangar` on the PR) or attach to the terminal
```

This page gets a first job through: log in, a token for the service, submit, callbacks. The rest is by task:

| Page | What it covers |
| --- | --- |
| [Jobs from GitHub, Slack and Jira](from-github-slack-jira.md) | `/hangar` on an issue or PR, a Slack thread, a ticket moved to a column |
| [Working with the agent](working-with-the-agent.md) | Follow along, reply, take over; the pull request; forks and best-of-N |
| [An agent loop that finishes the job](agent-loop.md) | The verify gate inside the turn; PR auto-fix |
| [Test results](test-results.md) | Structured results from every check, flaky tests, where they show |
| [Experiments](experiments.md) | Trials, grading, shadows, the eval budget and queue |
| [Reference](reference.md) | Failure labels, outcome classes, settings, tainted jobs, security notes |

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

For one repo, `hangar repo check acme/app` puts what matters for agent work in one report (the GitHub App, branch
protection and required checks, the gateway, the environments' credential mode, sudo, egress and logins, and the Jira and
Slack mappings) and ends with `ready` or `not ready (N failing)`: see [repo-policy.md](../repo-policy.md#is-this-repo-ready).

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

A job runs on an **API key** for its harness: its person's own, else the team's ([team.md](../team.md#whose-login-a-job-uses)). Subscription logins (Claude Pro or Max, a ChatGPT or Google sign-in) are for interactive sessions only; a job that would need one is refused at submit with the `hangar cred add … --kind api_key` command to run ([subscription logins](../team.md#subscription-logins)). Cap spend at the vendor (a workspace or project limit on the key) and in hangar (job budgets, daily caps).

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
| `environment`, `harness`, `model`, `provider` | Where and with what to run. Left out: the repo's [policy](../repo-policy.md), then the first environment mapped in `HANGAR_GITHUB_REPOS`. |
| `harness_lock` | Run from a [harness lock](../harness-lock.md#thaw-and-drift) in the registry: `name`, `name@rN`, `name@sha256:…` or `name@stable`. It fixes the harness, model and approvals (a conflicting `harness` or `model` is refused); a lock on another CLI version than `harnesses.lock.yaml` pins is refused at submit. Turn 1 is checked against it: drift is a warning, an event and a review-packet line, or, under `HANGAR_HARNESS_DRIFT=fail`, the job fails with `reason: "harness_drift"` and delivers nothing. |
| `verify` | The repo's own check. It runs inside the agent's turn, where a failure goes back to the agent ([the agent loop](agent-loop.md)), and hangar runs it again after the turn. A failing check still delivers, as a draft PR. Left out: the repo's [policy](../repo-policy.md), then the environment's `verify:`; `""` means no check. |
| `verify_in_loop`, `max_verify_rounds` | Whether `verify` also runs inside the turn (default `true`), and how many failures go back to the agent per turn (default 3). See [the agent loop](agent-loop.md). |
| `autofix` | What the job's PR reacts to by itself: `ci,reviews` (default), `ci`, `reviews` or `off`. See [the agent loop](agent-loop.md). |
| `deliver` | `draft_pr` (default), `pr`, `branch` or `none`. |
| `context` | Files the agent is given in `~/.hangar/jobs/<job>/` (outside the repo, mode 0600), plus links listed in the prompt. Up to 50 items and 2 MB. |
| `callback_url`, `callback_secret` | Where to POST `job.started` and `job.finished`. `jobs` tokens always sign with their bound key; admins may name any `webhook` credential. |
| `idempotency_key` | Resubmitting with the same key returns the existing job. |
| `timeout_minutes`, `keep_minutes` | Limit per agent turn, and how long the sandbox stays after a turn for follow-ups. |
| `budget` | `{"max_minutes", "max_tokens", "max_cost_usd"}` per agent turn. A turn that crosses it is stopped, and what it did is delivered as a draft (`reason: "over_budget"`). See [Activity, usage and budgets](../activity.md). |
| `job_budget` | `{"max_cost_usd", "max_tokens", "max_turns"}` for all the job's turns together: follow-ups, verify-fix and auto-fix turns, best-of-N attempts and the judge. Left out: the repo's policy, then `HANGAR_JOB_BUDGET_*`. See [repo-policy.md](../repo-policy.md#budgets). |
| `output_schema` | JSON Schema for a structured result; it comes back as `result.data`. See [context exchange](../context-exchange.md). |

`context` can also carry your local work in progress: `patch` (uncommitted changes) and `bundle` (unpushed commits) items, applied before the agent starts. `hangar job submit --from-local` builds them from the current checkout. See [context exchange](../context-exchange.md).

**States and reasons.** A job is `queued`, `awaiting_approval`, `provisioning`, `running`, `needs_input`, then `succeeded`, `failed` or `cancelled`. `reason` says more: why it ended (`verify_failed`, `over_budget`, …) or why a queued job waits: `powered_down` (the sandbox pools are off for the night) or `awaiting_capacity` (its person already has `HANGAR_PERSON_MAX_OPEN_AGENT_PRS` agent PRs awaiting review; only a job's first turn waits, see [review capacity](../team-scale.md#review-capacity)). `GET /api/jobs?reason=awaiting_capacity` and `hangar job list --reason awaiting_capacity` list the held ones; an admin lets one through with `hangar job admit <id>`. `source` says which door it came through (`api`, `mcp`, `cli`, `github`, `slack`, `jira`, `schedule`, …); `review` is an [AI first-pass review](../landing.md#6a-ai-first-pass-review) of an agent PR, which delivers nothing and never waits for review capacity.

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

From an MCP client (Claude Code, Codex, …): `submit_job`, `get_job`, `list_jobs`, `follow_up_job`, `cancel_job`, `get_events`, `wait_for_turn(job)` to block with progress notifications until the turn ends, and `approve_job` / `reject_job` for jobs waiting for approval (admins and the repo's policy approvers, with a login token, never on their own job: [who may approve](../triggers.md#who-may-approve)).

A `jobs` token can carry a budget cap (`hangar token create svc --scope jobs --max-cost 2`); its jobs never get more.

**Questions.** An agent that needs a decision runs `hangar-agent ask "…"`. The job goes to state `needs_input` with `result.question`: nothing is verified or pushed yet, a `job.needs_input` callback is sent, and a GitHub job comments the question on its issue or PR. Answer with a follow-up (`/hangar <answer>` on GitHub); the agent continues in the same conversation. Cancelling a job that needs input ends it.

To run several attempts and keep the best, see [forks and best-of-N](working-with-the-agent.md#6-forks-and-best-of-n).

From another agent (Google ADK's `RemoteA2aAgent`, any A2A client): hangar is an A2A agent at `/a2a`, with its card at `/.well-known/agent-card.json`. A task is a job, and a message in the same context is a follow-up. See [a2a.md](../a2a.md).

## 3. Verify callbacks

Each callback is `POST` with `{"event": "job.started" | "job.needs_input" | "job.finished", "job": {...}}` and the header `X-Hangar-Signature: sha256=<hex HMAC-SHA256 of the raw body>`. A receiver checks it like this:

```python
import hashlib, hmac
ok = hmac.compare_digest(request.headers["X-Hangar-Signature"],
                         "sha256=" + hmac.new(KEY, request.body, hashlib.sha256).hexdigest())
```

Callbacks go only to public `https` hosts. Plain `http`, non-public addresses and internal names (including names that *resolve* to non-public addresses just before sending) need the host listed in `HANGAR_JOB_CALLBACK_HOSTS`. Context contents are never echoed in callbacks, and the agent's summary and verification output are redacted for known secrets before they leave hangar.

