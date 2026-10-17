# 0009: Jobs, the front door of the software factory

Status: built (2026-10-15): #30 and the waves after it. · Date: 2026-10-12 · Builds on: 0001 (runs), 0002 (results and landing), 0003 (security), 0004 (tasks), 0006 (environments), 0008 (swarms)

> Part of a set. Ownership, cross-spec decisions and the combined build order are in [docs/specs/README.md](README.md); where this spec conflicts with that page, the page wins.

## 1. Summary

The owner's goal is a **software factory**: coding agents running on our own infrastructure that do development work for engineers, triggered by the tools engineers and services already use. Hangar already has the factory floor: environments, harness packs, sandboxes, credentials, agent runs. What it lacks is the **front door**, a way for a client, application or service to say "do this work, here is what you need to know, tell me when it's done".

That front door is the **job**:

- **Submitting:** a caller submits a job with instructions, a repo, and a **context bundle** (issue text, logs, stack traces, files, links). Callers are a service with a narrow token, an MCP client, the CLI, or a GitHub issue or comment.
- **Running:** hangar queues the job, starts a sandbox from the right environment and harness, places the context next to the repo, and runs the agent with a prompt hangar writes. It then verifies the result with the repo's own command.
- **Delivering:** hangar commits anything left uncommitted, pushes a `hangar/<job>` branch, and opens a (draft) PR. Hangar does this, not the agent, so delivery doesn't depend on the agent remembering to.
- **Collaborating:** engineers stay in the loop. They can follow the job and attach to its terminal while it runs. They can reply on the PR or through the API (`follow_up`), and the agent continues in the same sandbox. They can also take over the sandbox themselves.
- **Reporting back:** the caller hears back through a signed webhook and, for GitHub-triggered jobs, comments on the issue.

A job is the unit of *requested work*: who asked, with what context, and where results go. It sits above runs (0001) and, later, owns a 0004 task graph or a 0008 swarm. M1 runs one agent turn per request, M3 adds swarm strategies, and M4 maps jobs onto 0004 tasks.

## 2. Why

- Every hangar run so far started from a human at an MCP client wiring up `create_sandbox → run_agent → agent_status → exec git push`. That doesn't scale to a factory, and the evals showed the cost of hand orchestration: slow, error-prone, and it needs someone babysitting.
- Services that know about work (an issue tracker, CI, an alerting system, an internal app) have no way to hand it to agents with context, and no way to get a result back.
- Engineers need to work *with* agents, not just receive their PRs. They need to steer mid-way, answer questions, and pick up where the agent left off.

## 3. Goals / non-goals

Goals:

- One authenticated `POST /api/jobs` (or MCP `submit_job`, `hangar job submit`, GitHub label or `/hangar` comment) turns into a verified branch and a PR, with no further calls.
- Context travels with the job and lands in the sandbox as files the agent is told about. It never travels as environment variables or command-line arguments.
- Narrow service tokens: a token with only the `jobs` scope can submit and read jobs and nothing else.
- Engineers can follow up on a job from the API, MCP, CLI or a PR comment, and the agent continues in the same sandbox on the same branch.
- Callers get completion through a signed webhook. GitHub-triggered jobs report on the issue.
- Restart safety: jobs survive a control-plane restart (re-queued or re-attached), and nothing is delivered twice.

Non-goals (this spec):

- Planning and multi-step task graphs (0004), and fan-out strategies (0008). Jobs will use them.
- Merging. Jobs open PRs; landing stays human-approved (0002).
- Moving git credentials out of the sandbox (0003). Jobs work with today's in-sandbox git credential and switch to the gateway when 0003 lands.

## 4. User stories

1. A support tool files "export to CSV drops unicode" with the customer's sample file and the stack trace. It calls `POST /api/jobs` with a `jobs`-scoped token. Twenty minutes later its webhook receives `job.finished` with a draft PR link and the verification result.
2. Sean labels a GitHub issue `hangar`. Hangar comments "On it: job-3fa2…", and later comments the summary and a PR link. Sean comments `/hangar also handle the empty-file case` on the PR, and the agent continues on the same branch.
3. From Claude Code, Sean calls `submit_job(repo=…, instructions=…, context=[{name:"trace.txt", content:…}])`, keeps working, and checks `get_job` later.
4. While a job runs, an engineer opens the dashboard terminal on the job's sandbox (`terminal_session` in the job view) and watches, or takes over.
5. CI posts a failing test's log as a job ("fix the flake in test_x, here's the log"), with `verify="pytest tests/test_x.py -q --count 20"`.

## 5. Design

### 5.1 Model (`hangar/jobs.py`, table `jobs`)

```python
class JobState(StrEnum):
    QUEUED; PROVISIONING; RUNNING; VERIFYING; DELIVERING   # active
    SUCCEEDED; FAILED; CANCELLED                            # terminal (follow-ups re-open SUCCEEDED/FAILED)

class ContextItem(BaseModel):
    name: str                 # file name in the context dir, sanitized
    kind: Literal["text", "file", "log", "issue", "diff", "url"] = "text"
    content: str | None       # utf-8 text, or base64 when encoding="base64"
    encoding: Literal["utf-8", "base64"] = "utf-8"
    url: str | None           # links are listed in the prompt; hangar does not fetch them

class Job(BaseModel):
    id: str                   # "job-" + 8 hex
    title: str; instructions: str
    source: Literal["api", "mcp", "cli", "github"]; source_ref: str | None   # "github:owner/repo#12"
    requester: str            # token or user name
    repo: str | None; base_branch: str | None; branch: str                   # default hangar/<id>
    environment: str; harness: str | None; model: str | None; provider: str | None
    verify: str | None        # the repo's own check, run by hangar after each turn
    deliver: Literal["draft_pr", "pr", "branch", "none"] = "draft_pr"
    context: list[ContextItem]
    callback_url: str | None; callback_secret: str | None   # vault credential name for the HMAC key
    idempotency_key: str | None
    timeout_minutes: int = 60; keep_minutes: int = 120      # sandbox stays for follow-ups, then TTL
    state: JobState; reason: str | None
    sandbox_id: str | None
    turns: list[Turn]         # one per agent run: kind (initial|follow_up), message, run_id, exit, summary
    result: JobResult         # head_sha, commits, diffstat, pushed, pr_url, pr_number, verify {exit, tail}, summary
    events: list[JobEvent]    # ts, state, message: the timeline shown to callers
```

Limits (per job): at most 50 context items, 2 MB of context, 64 KB of instructions. Names are reduced to `[A-Za-z0-9._-]`, and duplicate names get a numeric suffix.

### 5.2 Lifecycle

```
submit ──► QUEUED ──► PROVISIONING ──► RUNNING ──► VERIFYING ──► DELIVERING ──► SUCCEEDED
                         │ create sandbox  │ agent turn  │ verify cmd   │ commit leftovers,     │
                         │ (work branch,   │ (poll exit, │ (repo's own  │ push branch, open PR, │
                         │  job label)     │  timeout,   │  check)      │ callbacks, comments   │
                         │ write context   │  cancel)    │              │                       ▼
                         ▼                 ▼             ▼              ▼                FAILED / CANCELLED
   follow_up(msg) on SUCCEEDED/FAILED with a live sandbox ──► RUNNING (same sandbox and branch)
```

- **Concurrency:** a worker pool of `HANGAR_JOB_CONCURRENCY` (default 4) jobs at a time. The rest wait in `QUEUED`, in FIFO order.
- **Outcome:**
  - A failed verify still delivers. The PR is a draft, and the summary says verification failed, so engineers see the work.
  - An agent crash with no commits fails the job and delivers nothing.
  - A timeout kills the run's tmux session and fails the job with `reason=timed_out`.
- **Restart:**
  - On start, `QUEUED` jobs are re-queued.
  - `RUNNING` jobs whose sandbox is alive are re-attached: polling resumes, and the run's exit file decides the outcome.
  - Other active jobs fail with `reason=interrupted`, since their step's side effects are unknown.

  Delivery is idempotent: push is a fast-forward to the job branch, and the PR is looked up by head branch before one is created.

### 5.3 What the agent sees

The context goes to `~/.hangar/jobs/<id>/`, outside the repo so it is never committed, with mode 0600. The prompt that hangar writes contains:

- the task title and instructions, and who asked;
- an index of the context files (name, kind, size) and the links;
- the repo path, the branch, and the base branch;
- how to finish:
  - keep the change focused;
  - run the verify command;
  - commit on the branch, but don't push or open a PR (hangar delivers);
  - write a short summary for the requester to `~/.hangar/jobs/<id>/RESULT.md` (what changed, how it was verified, open questions);
  - if the task is unclear, write questions there instead of guessing.

A follow-up prompt carries the engineer's message, the previous turn's summary, and the same finishing rules. Each turn is a fresh harness session until 0001 M5 adds harness resume. The job's context dir and branch carry the state between turns.

### 5.4 Delivery

After each turn, hangar runs these steps in the repo checkout:

1. Run `verify`, if set, and record its exit code and output tail.
2. If the working tree has changes, commit them as `hangar: uncommitted changes from <job>`. Agents forget.
3. Count commits ahead of `origin/<base>`. If there are none, record "no changes" and skip delivery.
4. Push with `git push origin HEAD:refs/heads/<branch>`. A force push is never used.
5. Open a PR when `deliver` is `pr` or `draft_pr`, a GitHub repo can be parsed from the URL, and a GitHub token is configured. The token is a vault credential named by `HANGAR_GITHUB_TOKEN_CREDENTIAL`, held by the control plane, not the sandbox. The PR body holds the summary, the verification result, the job id, and `Closes #N` for issue-triggered jobs. Hangar looks up an existing PR for the branch before creating one.

### 5.5 Callers

| Caller | How | Auth |
| --- | --- | --- |
| Service / app | `POST /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/follow-up`, `POST /api/jobs/{id}/cancel`, `GET /api/jobs` | Bearer token with scope `jobs` (or `admin`). `hangar token create svc --scope jobs`. |
| MCP client | `submit_job`, `get_job`, `list_jobs`, `follow_up_job`, `cancel_job` | Existing MCP auth (admin tokens or OAuth) |
| CLI | `hangar job submit/list/get/follow-up/cancel`, `--wait`, `--context FILE` | `HANGAR_TOKEN` |
| GitHub | Webhook `POST /api/hooks/github` | HMAC (`X-Hub-Signature-256`) with the secret in the vault credential named by `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL` |

**Scopes.** `admin` can do everything. `jobs` can use only `/api/jobs*`, and is rejected on the MCP endpoint, at OAuth consent, and on every other route. Scope enforcement is new; tokens created before this change keep `admin`.

**Service-token fence.** A `jobs` token:
- may target only repos in `HANGAR_GITHUB_REPOS` (`owner/repo` or an exact git URL), in the environments mapped to them, and can't pick a provider;
- signs callbacks with the `webhook` credential an admin bound to it (`hangar token create svc --scope jobs --callback-secret KEY`), so one service can't get payloads signed with another's key;
- sees only its own jobs. Ownership is the token id, not the name; token names may not contain `:` or be `admin`/`system`.

**Callbacks.**
- **Payload:** `POST callback_url` with `{"event": "job.started" | "job.finished", "job": <job view>}`, sent on start and on every terminal state.
- **Signing:** `X-Hangar-Signature: sha256=<hmac>` over the body, the same scheme as 0002's notifier webhook, keyed by the vault credential named in `callback_secret`.
- **Retries:** 3 attempts with backoff. A failed callback is recorded in the job's events and never fails the job.
- **Allowed hosts:** callback URLs must be `https` (or `http://localhost` for testing) unless `HANGAR_JOB_CALLBACK_HOSTS` allows the host.

**GitHub trigger.** Only repos listed in `HANGAR_GITHUB_REPOS` are accepted (`{"owner/repo": "environment"}`). Triggers:
- an issue labeled `HANGAR_GITHUB_TRIGGER_LABEL` (default `hangar`);
- an issue comment starting with `/hangar`.

Comments count only when the author's association is `OWNER`, `MEMBER` or `COLLABORATOR`. A label already needs triage access. Options parsed after `/hangar`: `harness=… model=… env=…`, then free text that becomes extra instructions.

- **Context:** the issue body and up to 20 recent comments become context files. The webhook delivery id is the idempotency key, so GitHub retries don't start duplicates.
- **Follow-ups:** a `/hangar …` comment on a PR that a job opened becomes a follow-up on that job.
- **Status comments:** hangar comments on the issue when it starts (job id, environment, harness, model) and when it finishes (summary, PR link, verification).

### 5.6 Engineer collaboration

- **Watching:** the job view carries `sandbox`, `terminal_session` (the run's tmux session, attachable from the dashboard or `hangar sb attach`), `branch` and `pr_url`.
- **Following up:** `follow_up(job, message)` from the API, MCP, CLI or a PR comment queues another turn in the same sandbox. It is rejected while a turn is running or after the sandbox is gone. The sandbox's expiry is pushed out by `keep_minutes` after each turn.
- **Taking over:** an engineer attaches to the sandbox's terminal and works directly. Their commits are picked up by the next follow-up's delivery, or by `deliver` called on its own (M2).

### 5.7 Security

- **Untrusted context:** issue text, comments and service-supplied context are untrusted input to the agent (prompt injection). Mitigations in this spec:
  - the GitHub repo allowlist and author-association check;
  - service tokens limited to `jobs`;
  - the prompt marks context as data, not instructions.

  The structural fix is 0003: egress policy, credentials out of the sandbox, and branch-scoped pushes.
- **Secrets:**
  - Context is stored in the job record and passed through `broker.redact` before it lands in the audit log.
  - Context files are 0600, outside the repo.
  - Webhook and callback secrets live in the vault, never in settings or job records.
- **Push scope:** hangar pushes only to new branches under `hangar/` (a caller's `branch` must be under `hangar/` and must not exist on the remote) and never force-pushes.
- **Output redaction:** the agent's summary, the verify output tail and the fallback output tail go through `broker.redact` before they are stored, so views, callbacks, PR bodies and GitHub comments never carry known secrets.
- **GitHub context:** only comments by owners, members and collaborators become context.
- **Callbacks:** non-global addresses are refused, both literally at submit and after DNS resolution just before sending. DNS rebinding between resolution and connect remains a known gap.

## 6. Milestones

| # | Milestone | Size | Depends on |
| --- | --- | --- | --- |
| M1 | Job model, store, runner (queue, provision, context, prompt, poll, verify, commit/push, PR, callbacks, follow-up, cancel, restart), REST with `jobs` scope, MCP tools, CLI | L | — |
| M2 | GitHub trigger: webhook, label/comment triggers, issue status comments, PR-comment follow-ups; `deliver` on demand | M | M1 |
| M3 (partly ✓) | Strategies: `strategy=best_of_n\|map` through 0008 `swarm`; job view lists attempts. Done: `best_of_n` on session forks (0010 M5), with verify-first ranking, an optional judge turn and a test-tampering flag. Not yet: `map`. | M | 0008 M3 |
| M4 | Jobs own 0004 tasks (plan, sub-tasks, supervision); a job's PR stack | L | 0004 M6 |
| M5 | Dashboard Jobs page: queue, timeline, context, attach, follow-up box | M | M1 |
| M6 | More triggers: Slack slash command and app mention, generic signed inbound webhook, scheduled jobs | M | M1 |
| M7 | Harness resume for follow-ups (0001 M5), git through the gateway (0003) | M | 0001 M5, 0003 M4 |

## 7. Acceptance

1. A `jobs`-scoped token submits a job against a test repo. Without further calls, a branch is pushed, a draft PR opens, the verify result is recorded, and the webhook receives a valid signature. The same token gets 403 on `/api/sandboxes` and 401 on `/mcp`.
2. A `/hangar` comment by a collaborator on an allowlisted repo starts one job, even with GitHub retrying the delivery. A comment by a non-collaborator, or on a non-allowlisted repo, starts nothing.
3. A `/hangar …` comment on the job's PR adds a commit to the same branch.
4. Restarting the control plane mid-run re-attaches the job and delivers once.
5. Cancel stops the agent within one poll interval.

## 8. Open questions

1. Should a job's PR be opened by a GitHub App identity (0002 OQ12) rather than the owner's token? This is recommended before other people use the factory.
2. Default `deliver` for service callers: `draft_pr` (proposed) or `branch`?
3. Per-requester budgets and concurrency (0007 M3) before opening the front door beyond the owner.
