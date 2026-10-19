# 0011 The factory roadmap: trust, context exchange, throughput

!!! note "Design record"
    Written 2026-10-13; **built**. The current behaviour is on [triggers.md](../triggers.md), [context-exchange.md](../context-exchange.md) and [the git gateway](../security/git-gateway.md); the status line below says what shipped.

Status: built (2026-10-15): workstreams #40–#46. · Date: 2026-10-13 · Builds on: 0003 (gateway), 0007 (budgets, parsers), 0008 (swarm), 0009 (jobs), 0010 (sessions)

Jobs (0009) and sessions (0010) are live: work arrives from GitHub, REST and MCP, runs in a resumable session on our own cluster, and comes back as a PR. This spec picks the next slice of the earlier specs that turns that into a factory other systems can safely hand work to, and adds the piece none of them covered well: **how context moves in and out of an agent session**, for MCP clients above all.

## 1. Where we are

| Works today | Gap |
| --- | --- |
| GitHub label/comment, REST and MCP `submit_job` start jobs | Anyone who can comment on an allowlisted repo can start a job. No approval step, no Slack, no generic webhooks, no schedules. |
| The agent gets instructions, context files in `~/.hangar/jobs/<id>/` and a repo checkout | A session (`send_to_session`) takes text only. There is no way to hand over local work in progress (a patch or commits that aren't pushed). |
| The agent returns `RESULT.md`, commits, a branch and a PR | There is no structured result, no artifacts (reports, screenshots, generated files), and no way to pull a session's work back into a local checkout. |
| A turn ends when the agent stops | An agent that needs a decision has no way to say so. A client can only poll `get_job`, with no progress and no "needs input" state. |
| `timeout_minutes` | No token or cost budget, no usage per job or session, and no common activity format across the four harnesses. |
| One attempt per job | No `fork_session`, no best-of-N. |
| The agent holds a GitHub token in the sandbox | It can push to any branch that token reaches (0003 M3–M4 are not built). |
| hangar speaks MCP and REST | Other agents (Google ADK, AX, any A2A client) can't hand work to hangar. |

## 2. Context exchange: the session I/O protocol

MCP clients (Claude Code, Codex, Cursor, other agents) are the main way people hand work to hangar. A context round trip has three parts:

- **in:** the request, its context and the client's local state;
- **during:** progress, and questions the agent can't answer itself;
- **out:** a summary, structured data, artifacts, and code the client can apply locally.

### 2.1 The agent side: one directory, plain files

Every turn gets `HANGAR_IO`, a directory the agent reads and writes with ordinary file tools. Files work with every harness, need no network or auth inside the sandbox, are checkpointed with the session, and are easy to tell the agent about in the prompt.

```
$HANGAR_IO/                      jobs: ~/.hangar/jobs/<job-id>   sessions: ~/.hangar/sessions/<session-name>
  in/<turn>/...                  attachments for this turn (files, decoded)
  RESULT.md                      summary for the people who asked (existing jobs contract)
  out/result.json                structured result, validated against the turn's output_schema
  out/QUESTION.md                "I need a decision": the turn ends as needs_input
  out/progress.jsonl             {"ts": ..., "message": "...", "pct": 40} lines; optional
  out/artifacts/**               files to hand back (reports, screenshots, generated assets)
```

hangar clears `out/` (except `artifacts/` from earlier turns, which are already collected) and `RESULT.md` at the start of each turn. At the end of a turn it reads these files and records them on the turn. A small `hangar-agent` shell helper in every image (`hangar-agent progress "…"`, `hangar-agent ask "…"`, `hangar-agent artifact <path>`, `hangar-agent result <json>`) writes the same files, so prompts can say "run `hangar-agent ask`" instead of describing paths. An MCP server inside the sandbox, with the same four tools, comes later with the 0003/0007 gateway, because it needs a sandbox-scoped credential.

### 2.2 The client side

| Direction | REST / MCP / CLI | Notes |
| --- | --- | --- |
| In: attachments | `context=[…]` on `submit_job`, `follow_up_job`, `new_session`, `send_to_session` | Same `ContextItem` shape as jobs (`text`, `file` base64, `url`). Written to `in/<turn>/`. |
| In: local work in progress | `context` items `kind: "patch"` (git diff, applied on top of the base) and `kind: "bundle"` (git bundle of unpushed commits, plus `base_commit`) | "Continue this in the cloud": a local Claude Code hands its branch to a cloud session without pushing. `hangar session new --from-local` builds both from the current checkout. |
| In: structured request | `output_schema` (JSON Schema) on jobs and session turns | The agent is told to write `out/result.json`. hangar validates it; if it's invalid, the agent gets one repair turn. |
| During: progress | `wait_for_turn(session or job, timeout)` MCP tool that sends MCP progress notifications while it waits, and the `/api/.../events` stream (§3) | Clients stop polling. |
| During: questions | Turn outcome `needs_input`, job state `needs_input`, with the question | Surfaced on `get_job`/`get_session`, as a GitHub comment, in the Slack thread and as A2A `input-required`. The answer is an ordinary follow-up and resumes the conversation. |
| Out: result | `get_job` / `get_session` turn `result` (JSON), `summary`, `artifacts[]` | Artifacts are stored with checkpoints (encrypted) and read with `get_artifact` or the MCP resource `hangar://sessions/<name>/artifacts/<turn>/<path>`. |
| Out: code | `get_session_patch` (diff against base, size-capped), `hangar session pull NAME` (fetch the session's commits from its checkpoint bundle into the local repo, then apply the WIP patch) | A local client can apply cloud work without the branch being pushed. |

## 3. Workstreams

Each workstream is one agent and one PR against `main`, built in parallel.

| # | Workstream | Specs | Scope | Acceptance |
| --- | --- | --- | --- | --- |
| A | **Trust, approvals, inbound webhooks, schedules** | 0009 M2 follow-up, 0009 M6 | `HANGAR_GITHUB_TRUSTED` (users, `@org/team`, or the default author associations); untrusted triggers create a job in `awaiting_approval`, and a trusted `/hangar approve` (or REST/MCP `approve_job`) starts it; untrusted authors' text is marked as untrusted in the prompt. Generic signed inbound webhooks at `/api/hooks/inbound/<source>` (HMAC secret per source, a field-mapping template into `SubmitJob`, idempotency from a delivery id). Scheduled jobs (`ScheduledJob`: cron and timezone, job template, enabled, last/next run) with REST/MCP/CLI CRUD and a scheduler loop. | A non-collaborator's `/hangar` comment produces `awaiting_approval` and a comment saying so; `/hangar approve` by a collaborator runs it. A webhook with a bad signature is a 401, and a redelivery doesn't start a second job. A schedule set to every minute submits exactly one job per minute and survives a restart. |
| B | **Slack front door** | 0009 M6 | Slack app: slash command `/hangar <repo> <instructions>`, app mention, signing-secret verification, replies in the thread (start, needs_input question, finish with summary and PR), thread replies become follow-ups, a mapping from Slack user to hangar requester, and a channel/repo allowlist. A manifest and setup doc. | A signed slash command starts a job and the thread gets start and finish messages; a reply in the thread runs a follow-up; bad signatures and requests older than 5 min are rejected. |
| C | **Activity events, usage, budgets** | 0007 M6, 0007 M3 (budgets), 0003 M9 (usage part) | `events.py`: incremental parsers for Claude Code, Codex, pi and Antigravity stream output into one `RunEvent` shape (message, tool_call, tool_result, command, file_change, error, usage, progress). Usage per turn, session and job (tokens in/out/cache, cost as reported or from a price table). `Budget` (max_minutes, max_tokens, max_cost_usd) on jobs and sessions, defaults from settings, enforced while a turn runs, with a new `over_budget` outcome. `GET /api/{jobs,sessions}/{id}/events?since=`, MCP `get_events`, and a timeline on the Jobs/Sessions pages. | The parsers produce the same event kinds from recorded fixtures of all four harnesses. A job with `max_cost_usd` is stopped when the reported cost crosses it, and the job says why. |
| D | **Fork sessions, best-of-N jobs** | 0010 M5, 0009 M3, 0008 M3 (best_of_n part) | `fork_session(ref, n)`: restore the latest checkpoint into N child sessions (each with a `parent`, its own branch) and fork the harness conversation natively where the CLI supports it (verify each CLI's current flags), else hand off a summary. Jobs `strategy: best_of_n, n`: N attempts as child sessions in parallel, each verified; ranking (verify passes, then a judge turn or the smallest passing diff); the best is delivered as the PR, the others are kept as branches and listed on the job. | `fork_session` of a session with uncommitted work gives N sandboxes with that work and resumed conversations. A best_of_n job with one failing and two passing attempts delivers a passing one and lists all three. |
| E | **Context exchange (§2)** | new | `HANGAR_IO` for jobs and sessions; attachments on session turns and follow-ups; `patch` and `bundle` context items; `output_schema` with validation and one repair turn; `out/QUESTION.md` → `needs_input` (job state, turn outcome, GitHub comment); artifacts collected into the checkpoint store, with `get_artifact`, an MCP resource and REST; `get_session_patch`, `hangar session pull`, `hangar session new --from-local`; `wait_for_turn` with MCP progress notifications; the `hangar-agent` helper. | An MCP client hands over an uncommitted local patch and unpushed commits, gets back a schema-valid `result.json` and an artifact, and applies the session's work locally with `hangar session pull`. An agent that runs `hangar-agent ask` leaves the job in `needs_input` with the question, and a follow-up resumes the same conversation. |
| F | **Git credential gateway** | 0003 M2–M4 | Push and fetch go through the control plane: a git smart-HTTP proxy at `/git/…` authenticated by a per-sandbox grant token (`hgs_…`, revoked on destroy). Ref updates in `git-receive-pack` are inspected: only the sandbox's own branch (or `refs/heads/hangar/**`), with no deletes of other refs and no tags. The upstream credential (PAT, or a GitHub App installation token minted per grant) never enters the sandbox. Sandboxes get `url.<gateway>.insteadOf` and a credential helper. `credential_mode: gateway` per environment, the default for jobs when it's configured. | From a sandbox, a push to the job branch succeeds and a push to `main` is rejected with a clear message. No GitHub token is readable inside the sandbox. A destroyed sandbox's grant stops working. |
| G | **A2A endpoint; AX assessment** | new | hangar as an A2A server: an agent card at `/.well-known/agent-card.json`, JSON-RPC `message/send`, `message/stream` (SSE), `tasks/get`, `tasks/cancel` and push-notification config. A2A tasks map to jobs and `contextId` to follow-ups; message parts map to instructions and context items; artifacts carry the summary, PR link and structured result; job states map to A2A states (`needs_input` → `input-required`). Auth with `jobs`-scoped tokens. A written assessment of Google's AX and Agent Substrate (§4), with a provider design. | The A2A reference client (or ADK's `RemoteA2aAgent`) sends a task and gets back the PR link as an artifact; a second message in the same context runs a follow-up. |

Later, not in this round: task graphs and the supervisor (0004 M6–M7), hot resume with Pod Snapshots (0010 M4), the MCP gateway and in-sandbox MCP server (0007 M5), and egress policy (0003 M6–M7).

## 4. Google AX

[AX](https://github.com/google/ax) (Agent Executor, open-sourced 2026-05-20, Apache-2.0, Go, early preview with breaking changes expected) is a Kubernetes-native orchestrator for agent workloads on Agent Substrate. Its primitives are:

- `Task`: an isolated sandbox with CPU and memory limits;
- `Workspace`: Git repos, MCP servers and skill packages, pre-wired;
- `Model`.

It persists actor state with an event log and snapshots, supports `ax suspend`/`ax resume`, and branches trajectories from checkpoints. It is, in effect, a peer of hangar's sandbox layer, not a library hangar would call.

The position for now:

- **Don't build on it yet.** It's a preview with breaking changes promised. Bring-your-own-harness is still on its roadmap, and hangar's value sits above the sandbox: harness-native conversation resume, jobs, GitHub and Slack delivery, budgets, the MCP surface. Moving the core onto AX now would trade working code for churn.
- **Interoperate through A2A** (workstream G). ADK and AX agents can delegate coding work to hangar as an A2A agent today, with no dependency on AX internals.
- **Keep AX as a candidate provider.** hangar's `Provider` interface (create, exec, files, destroy, pause and resume) maps onto AX `Task`s, and AX suspend/resume could back hot resume (0010 M4). Workstream G writes the provider design. Build it when AX stabilises and if Agent Substrate beats our GKE pools on start time or density.
- **Borrow ideas:** declarative workspaces that pre-wire MCP servers (0006 already has `mcp:`), a single-writer event log for run state (C's events are a step toward it), and trajectory branching (D's `fork_session`).

## 5. How the parallel work fits together

- **Shared files** (`models.py`, `config.py`, `jobs.py`, `sessions.py`, `mcp_server.py`, `app.py`, `cli.py`) get small, additive edits. New logic goes in new modules: `triggers.py`, `slack.py`, `events.py`, `strategies.py`, `exchange.py`, `gateway.py`, `a2a.py`. Settings go under a comment naming the workstream. No reformatting or reordering of existing code.
- **States and outcomes added this round:**
  - job states `awaiting_approval` (A) and `needs_input` (E);
  - turn outcomes `over_budget` (C) and `needs_input` (E).
  
  B and G consume `needs_input` and treat it as optional until E merges.
- **Merge order:** A, C, E, then D, B, F, G. Later PRs rebase on earlier ones; the integrator resolves conflicts.
- **Every PR:** passes unit tests and ruff, adds tests for the acceptance cases (the fake provider, plus Docker e2e where it matters), updates the docs (`docs/jobs/README.md`, `docs/sessions.md`, or a new page), and states which shared files it touched.
