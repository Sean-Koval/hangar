# 0001: Agent runs (liveness, structured status, resume, ready-to-test sandboxes)

Status: draft. Date: 2026-10-12. Author: hangar agent session, for review.

## 1. Summary

- A run becomes a stored object with nine states, not a dict in `sandbox.info["runs"]`.
- A supervisor process, a pull-based watcher and the reaper decide if a run is alive. A dead agent is marked `crashed` with a reason within about 45 s.
- Harness output is parsed into one event schema: phase, tool, file, tests, tokens, plan usage. Clients read it by cursor (MCP, REST) or SSE (dashboard).
- `resume_agent`, `cancel_agent` and `send_input` continue the same conversation, with idempotency keys.
- Repo dependencies are baked into the image, keyed by lockfile. Target: first test starts in 15 s or less after `create_sandbox` returns.

## 2. Problem and evidence

Evidence is the first live run on GKE (2026-10-11). Two sandboxes came from the warm pool in 3.1 s. Both agents died at their pytest step. hangar reported them as running for 10+ minutes.

| # | What happened | Where it comes from in the code |
| --- | --- | --- |
| 1 | The agent's tmux was killed (by hangar's own test fake, since fixed in #12). `agent_status` kept saying "running". | `fleet.py:run_agent` runs the harness in a tmux pane as `{ cmd; } 2>&1 \| tee log` and writes `<run>.exit` only after the pipe ends. If tmux dies, bash dies with it and the file is never written. `fleet.py:agent_status` reads `.exit` and returns `RUNNING` when it is missing. There is no pid, heartbeat or provider check. |
| 2 | Even a clean exit leaves the tmux session alive. | The launcher ends with `; exec bash -l`, so "tmux session exists" never means "agent is alive". |
| 3 | The orchestrator wrote its own stream-json parser. | `agent_status` returns `tail -n 60` of the raw log as `output_tail`. Nothing parses `--output-format stream-json` (`harnesses.py:HARNESSES["claude-code"].run`). |
| 4 | A `rate_limit_event` showed Pro 7-day usage at 91%. Nobody saw it. | No code reads usage. All sandboxes share one subscription token (`harnesses.py` OAUTH_TOKEN slot, `CLAUDE_CODE_OAUTH_TOKEN`). There is no concurrency limit except `max_sandboxes=50` (`config.py`). |
| 5 | Recovery lost the conversation. | `run_agent` always starts a fresh `claude -p`. No session id is stored or passed. `Run` data is `{id, harness, started_at, prompt[:200]}` (`fleet.py:run_agent`). |
| 6 | The PR URL was scraped by hand. | No post-run workspace facts: branch, head sha, commits, dirty tree, pushed or not. |
| 7 | Each sandbox ran `uv sync --all-extras` before tests. | `repo.bake` exists (`envspec.py:render`, step 6) but is not keyed to dependencies (see section 5.9). |
| 8 | Runs vanish with their sandbox. | `runs` is inside `Sandbox.info`. `get_sandbox` shows the last 10. `agent_status` calls `_running()` and fails once the sandbox is gone, which is exactly when the orchestrator needs the answer. |

More defects found while reading the code:

- `reconcile()` only runs for sandboxes in RUNNING or PAUSED with a `provider_ref`. It never touches runs.
- `codex exec --full-auto` (`harnesses.py`) is rejected by current Codex. I installed `@openai/codex` 0.162.1 and `codex exec --full-auto x` fails with `unexpected argument '--full-auto'`. Codex runs fail today before doing any work.
- `run_agent(extra_args=...)` is pasted unquoted into a shell script. It is not exposed over MCP or REST. Keep it that way.
- The tmux pane and the agent share one failure domain. `_scrub_inherited_secrets` runs `tmux kill-server` on every fork.
- GKE node pools for sandboxes are Spot by default (`docs/gke.md`). Preemption kills the pod and the run with it.

Research that shapes the design:

- Jarmak, [Engineering Reliable Coding Agents](https://arxiv.org/abs/2608.13867): verify the workspace, not the agent's self-report (100% "submitted", 44% resolved). Keep recovery outside the failure domain. Make retried steps idempotent. Compare repeats.
- Copilot cloud agent [risks and mitigations](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/risks-and-mitigations): commits link to session logs. A run record with events plays that role.
- Codex cloud [internet access](https://learn.chatgpt.com/docs/cloud/internet-access): setup scripts have internet, the agent phase does not. Installing dependencies in a setup phase fits that model (section 5.9).
- MCP [security best practices](https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices): no token passthrough, minimal scopes, session ids are not authentication. See section 6.

## 3. Goals and non-goals

Goals:

1. `Run` is a persisted object that outlives the sandbox. States: `queued, starting, running, waiting_input, succeeded, failed, crashed, cancelled, timed_out`.
2. A dead agent or dead sandbox is detected in at most 45 s (three missed 15 s probes) and 90 s respectively. The cause is recorded.
3. A normalized event stream and summary for Claude Code now, Codex and Gemini CLI next.
4. Show and respect the shared Pro plan: usage, warning, queueing.
5. `resume_agent`, `cancel_agent`, `send_input`. Retries never start a second agent.
6. Ready-to-test sandboxes: dependency installs happen at image build time, keyed by lockfile.
7. Keep existing MCP tools and REST routes working. Breaks are listed in section 7.

Non-goals (separate specs):

- Credential proxying, branch-scoped push, merge gating, CI waiting, approvals, egress allowlists, API token scopes. Section 6 says how this spec stays compatible with them.
- Running more than one agent per worktree.
- Multi-replica control plane. State stays in one SQLite file (`store.py`).
- Cost accounting in money. `total_cost_usd` is shown as "API-equivalent estimate", since a Pro subscription is not billed per token.

## 4. User stories

Orchestrator agent over MCP:

- As an orchestrator, I start 3 agents and poll `agent_events(after=cursor, wait_seconds=20)` instead of parsing logs.
- As an orchestrator, I learn within a minute that an agent died, why, and whether `resume_agent` can continue it.
- As an orchestrator, I retry `resume_agent` after a network timeout with the same `idempotency_key` and get the same run back.
- As an orchestrator, I see "Pro 7-day 91%, resets Thu 14:00" before I start run number 4. If the plan is exhausted, the run is queued with the reason, not failed halfway.
- As an orchestrator, I get branch, head sha, commits ahead and a verification result from hangar. I do not take the agent's word for it.

Human on the dashboard:

- I see which agents are alive, what each is doing right now, and how long since the last sign of life.
- I see plan usage at the top of the page and a queue badge when runs wait for a slot.
- I can cancel a run, type a follow-up into a finished run (resume), or answer an agent that asks a question.
- I open the same live terminal as before. Closing or killing it does not kill the agent.

## 5. Design

### 5.1 Data model (`models.py`, `store.py`)

```python
class RunState(StrEnum):
    QUEUED = "queued"; STARTING = "starting"; RUNNING = "running"; WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"; FAILED = "failed"; CRASHED = "crashed"
    CANCELLED = "cancelled"; TIMED_OUT = "timed_out"

TERMINAL = {SUCCEEDED, FAILED, CRASHED, CANCELLED, TIMED_OUT}

class RunSummary(BaseModel):          # updated by the event ingester
    phase: str | None = None           # thinking|reading|editing|searching|running_tests|running_command|git|waiting
    last_tool: str | None = None       # "Bash"
    last_tool_summary: str | None = None   # "uv run pytest -q tests/" (first 200 chars, redacted)
    current_file: str | None = None
    files_touched: list[str] = []      # last 20 distinct
    turns: int = 0
    input_tokens: int = 0; output_tokens: int = 0; cache_read_tokens: int = 0
    cost_usd: float | None = None      # API-equivalent estimate from the harness, if it reports one
    tests: TestSummary | None = None   # last detected test run
    last_message: str | None = None    # last assistant text, 500 chars
    time_to_first_test_s: float | None = None
    warnings: list[str] = []

class RunResult(BaseModel):           # facts hangar measured, not what the agent said
    branch: str | None; head_sha: str | None; base_sha: str | None
    commits_ahead: int | None; dirty: bool | None; pushed: bool | None
    pr_url: str | None; pr_url_source: Literal["agent_reported"] | None
    verification: dict | None          # {command, exit_code, output_tail}
    final_message: str | None
    snapshot_id: str | None            # optional checkpoint (M10)

class Run(BaseModel):
    id: str                            # "run-" + 8 hex, same as today (agent_status regex still matches)
    sandbox_id: str
    harness: str
    prompt: str                        # redacted with broker.redact before storing, full length
    prompt_sha256: str
    cwd: str
    state: RunState = RunState.QUEUED
    reason: str | None = None          # machine code, see 5.2
    detail: str | None = None          # human sentence
    session_id: str | None = None      # harness conversation id (UUID), chosen by hangar where possible
    parent_run_id: str | None = None   # set by resume_agent
    idempotency_key: str | None = None
    credential: str | None = None      # agent credential used; usage and concurrency are counted per credential
    created_at: float; queued_at: float | None; started_at: float | None; finished_at: float | None
    last_heartbeat_at: float | None; last_event_at: float | None
    pid: int | None; pid_start: str | None     # supervisor pid and /proc start time, guards pid reuse
    exit_code: int | None; signal: str | None
    timeout_minutes: int; verify: str | None
    cancel_requested_at: float | None
    log_offset: int = 0                # bytes of <run>.log already ingested
    event_seq: int = 0
    summary: RunSummary = RunSummary()
    result: RunResult | None = None
    created_by: str | None
```

Store changes. `Store` is a generic key/JSON table. Runs are queried by sandbox and state, and events by cursor, so they get real columns:

```sql
CREATE TABLE runs (key TEXT PRIMARY KEY, data TEXT NOT NULL,
                   sandbox_id TEXT, state TEXT, created_at REAL, idempotency_key TEXT);
CREATE INDEX runs_sandbox ON runs(sandbox_id, created_at);
CREATE INDEX runs_state ON runs(state);
CREATE UNIQUE INDEX runs_idem ON runs(sandbox_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE run_events (run_id TEXT, seq INTEGER, src_offset INTEGER, ts REAL, type TEXT, data TEXT,
                         PRIMARY KEY (run_id, seq), UNIQUE (run_id, src_offset));
```

New `Store` methods: `put(Run)` fills the extra columns, `list_runs(sandbox_id=None, states=None, limit=50)`, `append_events(run_id, events)`, `events_after(run_id, seq, limit, types)`, `prune_events(older_than)`. `put(Run)` uses the existing `INSERT ... ON CONFLICT` pattern. The unique `(run_id, src_offset)` makes event ingestion idempotent: re-reading a byte range after a control-plane restart inserts nothing twice.

Retention: summaries and `Run` rows are kept until the sandbox record is purged. Events are kept 14 days or 20,000 per run (`HANGAR_RUN_EVENT_RETENTION_DAYS`, `HANGAR_RUN_EVENT_MAX`).

Dual write: for one release, `run_agent` also appends `{id, harness, started_at, prompt, state}` to `sandbox.info["runs"]`. The dashboard (`ui/app.js`, `s.info?.runs`) and `get_sandbox` keep working until section 5.8 ships.

### 5.2 State machine

```
            +-----------+  slot free   +----------+  pid + first heartbeat  +---------+
 run_agent->|  queued   |------------->| starting |------------------------>| running |<--+
 resume    +-----+------+              +----+-----+                         +----+----+   | input
                 |cancel                    | no pid in 60 s                    |  |      | received
                 v                          v                                   |  v      |
            cancelled                   crashed(start_failed)                   | waiting_input
                                                                                 |
   exit file / result event ------------------------------------------------------+--> succeeded | failed
   supervisor gone, no exit file --------------------------------------------------+--> crashed
   sandbox gone ------------------------------------------------------------------+--> crashed
   cancel_agent -------------------------------------------------------------------+--> cancelled
   now > started_at + timeout ------------------------------------------------------+--> timed_out
```

Terminal states and their `reason` codes:

| State | Meaning | Reasons |
| --- | --- | --- |
| `succeeded` | Process exited 0, the harness result event is not an error, and `verify` passed if one was given. | `ok` |
| `failed` | The process ended and said it failed, or verification failed. The agent was alive to the end. | `harness_error`, `nonzero_exit`, `verify_failed`, `rate_limited` |
| `crashed` | The agent or its environment disappeared without a normal end. | `process_died`, `start_failed`, `sandbox_gone`, `sandbox_unreachable`, `oom` (best effort), `legacy_lost` |
| `cancelled` | Someone asked for it. | `cancel_agent`, `sandbox_destroyed`, `queue_cancelled` |
| `timed_out` | hangar's deadline passed. | `run_timeout`, `sandbox_ttl` |

Rules:

- A terminal state is final. Later observations only add `result` fields. `resume_agent` makes a new run.
- `queued` means "accepted, waiting for a slot". Reasons while queued: `credential_concurrency`, `plan_limit`, `sandbox_not_ready`. Slots are explained in 5.5.
- `waiting_input` is entered only when the harness emits an input request (normalized `input.requested`). For Claude Code in `-p` mode with bypassed permissions this may never happen. Section 5.6 explains what `send_input` does then.
- `succeeded` is not a claim that the code is right. It means the process finished cleanly and the check the caller asked for passed.

### 5.3 Liveness and crash detection

Principle: the watcher lives in the control plane, and the thing it watches does not depend on tmux.

**Supervisor.** `run_agent` writes a small `hangar-run` shell script (same way it writes `<run>.sh` today, so no image change) and starts it with `setsid nohup`, outside tmux. In the sandbox user's `~/.hangar/runs/`:

| File | Writer | Content |
| --- | --- | --- |
| `<run>.prompt` | hangar | unchanged |
| `<run>.log` | harness, stdout and stderr merged | unchanged path and format, so existing readers still work |
| `<run>.pid` | supervisor | `<supervisor pid> <start time from /proc/<pid>/stat field 22>` and the child pid on line 2 |
| `<run>.hb` | supervisor | mtime touched every 10 s by a background loop in the supervisor |
| `<run>.exit` | supervisor `trap EXIT` | the exit code as a plain integer, as today, written with tmp-and-rename |
| `<run>.exit.json` | supervisor | `{code, signal, ended_at}` (new) |
| `<run>.cancel` | hangar | presence asks the supervisor to stop (TERM to the child's process group, KILL after grace) |

The tmux session `<run>` stays for the dashboard and for `terminal_read`, but it only runs `tail -n +1 -F <run>.log`. Killing tmux, running `tmux kill-server` (as `_scrub_inherited_secrets` does) or closing the dashboard terminal no longer affects the agent. The agent no longer gets a terminal; it never read from the pane's stdin anyway.

**Watcher.** One `Fleet._run_monitor()` task in the control plane, started next to `_reaper_loop`. Every `HANGAR_RUN_PROBE_SECONDS` (default 15) it does one `prov.exec` per sandbox that has non-terminal runs. The probe is a single shell snippet that, for each such run, prints: `.exit.json`, whether the supervisor pid is alive with the same start time (`/proc/<pid>/stat`), the age of `.hb`, the size of `<run>.log`, and up to 256 KiB of log from `log_offset`. It does not call tmux. One exec per sandbox per tick: 10 sandboxes is about 0.7 exec/s, which both the Docker API and the Kubernetes exec path handle.

Decision table, applied in order:

| Observation | Verdict |
| --- | --- |
| `.exit.json` present | Terminal. Code 0 plus a non-error result event gives `succeeded`; `cancel_requested_at` set gives `cancelled`; anything else gives `failed/nonzero_exit` or `failed/harness_error`. |
| No exit file, supervisor dead (or start time differs), heartbeat age over 45 s | `crashed/process_died`. Detail includes the last event and last log line. |
| No exit file, supervisor alive, heartbeat age over 90 s, sandbox not PAUSED | Keep `running`. Add warning `heartbeat_stale`. Frozen processes (SIGSTOP, overload) are not proven dead. |
| `starting` and no pid after 60 s | `crashed/start_failed`. |
| Probe `exec` fails 3 times in a row and `prov.status()` is not RUNNING | `crashed/sandbox_gone`. |
| Probe `exec` fails but `prov.status()` is RUNNING | Add warning `sandbox_unreachable`. After 10 minutes, `crashed/sandbox_unreachable`. |
| `now > started_at + timeout_minutes` | Write `.cancel`, then `timed_out/run_timeout`. |
| Log has not grown for `HANGAR_RUN_IDLE_WARN_MINUTES` (default 10) while the heartbeat is fresh | Warning `no_output` only. A long pytest run produces no events, so silence is never a crash signal. |

Why these signals:

- **Pid plus start time** answers "is this exact process alive?" without tmux. Start time defeats pid reuse after a restart of the pod's init.
- **Heartbeat** is a second signal for processes that are alive but not scheduled, and for provider paths where `/proc` is partial. gVisor's `/proc` coverage is an open question (section 10); the heartbeat file does not need it.
- **Log growth** is not liveness. It is only an idle hint.
- **Exit file** stays the source of truth for a normal end. It is now written by a supervisor that survives tmux death, and a `trap` covers SIGTERM.

**Dead sandboxes.** Three layers:

1. `Fleet.reconcile()` already marks a sandbox STOPPED when `list_refs()` no longer shows it (`fleet.py:reconcile`, `_mark_gone`). `_mark_gone` now also ends the sandbox's live runs as `crashed/sandbox_gone`. Docker and Kubernetes support `list_refs`. E2B returns `None`, so it relies on the probe rule above.
2. `Fleet.destroy()` ends live runs as `cancelled/sandbox_destroyed`. The reaper's TTL expiry (`_reaper_loop`) uses `timed_out/sandbox_ttl`.
3. Spot preemption removes the pod, so `list_refs` no longer lists the claim. Rule 1 catches it on the next reaper tick (60 s). To meet the 90 s target the run monitor calls `reconcile()` itself after any failed probe.

**Restart safety.** All watcher state is in SQLite (`pid`, `log_offset`, `event_seq`, `state`). After a control-plane restart, `start()` calls `reconcile()` and the monitor continues from `log_offset`. `(run_id, src_offset)` uniqueness prevents duplicate events. Because the watcher polls instead of receiving pushes, a restart loses nothing.

**Why pull, not push.** The sandbox cannot reach the control plane (GKE default NetworkPolicy; the live run's probes confirmed it). Allowing it would also let a compromised agent talk to the control plane. Pull over `exec` needs no new network path.

### 5.4 Structured status: event schema and parsers

Each harness profile (`harnesses.py:Harness`) gets new fields:

```python
events: Literal["claude-stream-json", "codex-jsonl", "gemini-stream-json", "text"] = "text"
run_new: str       # replaces `run`; uses $HANGAR_SESSION_ID
run_resume: str | None   # uses $HANGAR_SESSION_ID and the prompt file
session: Literal["assign", "report", "last", "none"]   # how the session id is obtained
```

`run` stays as an alias of `run_new` for environment specs that override harnesses.

A parser class per format lives in a new `src/hangar/runevents.py`: `Parser.feed(line: str) -> list[Event]`. It must tolerate non-JSON lines (stderr is merged into the log), lines over 1 MiB (dropped, counted), and unknown event types (kept as `harness.unknown` with the raw line truncated to 4 KiB). Every text field is passed through `broker.redact` before storage.

Normalized event, stored and served:

```json
{"run_id": "run-3fa91c20", "seq": 41, "ts": 1791766012.4, "type": "tool.start",
 "data": {"tool_id": "toolu_01", "name": "Bash", "summary": "uv run pytest -q tests/", "file": null}}
```

| `type` | `data` | Source |
| --- | --- | --- |
| `run.started` | `session_id, model, cwd, harness_version?` | Claude `system/init`; Gemini `init`; Codex first event |
| `message` | `role, text (2 KiB max), final: bool` | assistant text blocks |
| `tool.start` | `tool_id, name, summary, file?` | Claude `tool_use`; Gemini `tool_use`; Codex item start |
| `tool.end` | `tool_id, ok, summary (200 chars of output), duration_ms?` | Claude `tool_result`; Gemini `tool_result` |
| `phase` | `phase` | derived: Read/Grep/Glob = reading, Edit/Write = editing, Bash with a test runner = running_tests, other Bash = running_command, `git ...` = git |
| `test.result` | `framework, passed, failed, errors, skipped, duration_s, command, source: "parsed"` | derived from a `tool.end` whose command matched a runner |
| `usage` | `input_tokens, output_tokens, cache_read_tokens, cost_usd?, turns` | assistant `message.usage`; final `result` |
| `rate_limit` | `window ("five_hour"/"seven_day"/…), used_pct?, status, resets_at?, raw` | Claude `rate_limit_event` |
| `input.requested` | `kind ("question"/"permission"), prompt, options?` | harness-dependent |
| `error` | `code, message, retryable: bool` | harness error events |
| `run.result` | `ok, subtype, num_turns, duration_ms, cost_usd?, final_text` | Claude `result`; Gemini `result`; Codex turn end |
| `run.state` | `from, to, reason, detail` | emitted by hangar for every transition |
| `harness.unknown` | `raw` | anything not mapped |

Test detection is best effort. It looks at the Bash command (`pytest`, `uv run pytest`, `python -m pytest`, `npm test`, `go test`, `cargo test`) and at the tool output summary line (for pytest: `=+ .* in [0-9.]+s =+`). It reports `source: "parsed"` so clients know it is not harness-native. If nothing matches, `summary.tests` stays null. It never invents numbers.

Claude Code shapes. `claude -p --output-format stream-json --verbose` writes one JSON object per line: `system`/`init` (has `session_id`), `assistant` (`message.content[]` of `text` and `tool_use`, plus `message.usage`), `user` (`tool_result` blocks), and a final `result` (`subtype`, `is_error`, `num_turns`, `duration_ms`, `total_cost_usd`, `usage`, `session_id`). I wrote these from memory of the documented format, not from a capture. Milestone M0 captures the real log from the 2026-10-11 run and turns it into the parser's test fixture. Claude Code 2.1.296's `--help` confirms `--output-format stream-json`, `--include-partial-messages` and `--input-format stream-json`. Partial messages stay off, since they multiply event volume.

`rate_limit_event` is known only from the live run: it carried the 7-day window at 91%. I do not know its field names, so the parser reads fields defensively (`used_pct` from any of `utilization`, `used_percentage`, `used_pct`, normalised to 0-100) and always stores `raw`. Open question 1.

Codex. `codex exec --json` prints events as JSONL (flag confirmed in 0.162.1 `--help`). I could not confirm the event names or where the thread id appears. I believe they are `thread.started`, `turn.started`, `item.*`, `turn.completed` (with usage) and `turn.failed`. The binary also contains `rate_limits`, `used_percent` and `resets_at` strings, so Codex has a usage concept. Marked open (question 2). The `codex-jsonl` parser ships behind a fixture capture, not before.

Gemini CLI. `gemini -o stream-json` is available. Its bundle defines exactly six event types: `init`, `message`, `tool_use`, `tool_result`, `error`, `result` (read from `@google/gemini-cli` 0.63.0). Field names are not verified (question 3). `agy` (Antigravity) is run with `--output-format stream-json` in `harnesses.py`, but I could not install it here, so it stays `text` until verified.

Fallback for `events: "text"`: only `run.state` events, liveness, and `output_tail`. That is still better than today.

### 5.5 Ingestion, streaming and plan usage

**Ingest.** The probe returns new log bytes. The control plane splits complete lines (a partial last line waits for the next tick), runs the parser, assigns `seq` and `src_offset`, appends events in one transaction, updates `Run.summary`, and publishes to an in-memory `asyncio.Condition` per run (same pattern as `fleet.py:BuildJob.follow`). Per tick caps: 256 KiB of log, 2,000 events. If a run exceeds that it gets one `harness.truncated` event and a warning. The offset still advances, so a flood cannot wedge the watcher.

**Cadence.** 15 s idle. When someone is waiting (an SSE client or an `agent_events` long poll), the monitor probes that sandbox every 2 s. That is 0.5 exec/s per watched run. A streaming file-follow on `Provider` would be better but is a larger provider change; not in this spec.

**MCP: cursor polling.** Cursors are `seq` integers. No tokens, no state held for the client.

**REST and SSE.** `GET /api/runs/{id}/stream` is Server-Sent Events like `app.py:build_stream` (ticket auth, `Last-Event-ID` = `seq`, `event: done` on a terminal state). A second feed, `GET /api/runs/stream`, sends only `run.state` and summary changes for the fleet page.

**Plan usage and back-off.** One shared Pro token means usage is per credential, not per sandbox.

- On every `rate_limit` event hangar stores `usage:<credential>:<window>` in the `kv` table: `{used_pct, status, resets_at, seen_at}`.
- Each run records the credential it used (`Run.credential`). It is the name of the claude-code credential attached to the sandbox (`Sandbox.credentials`, matched through `harnesses.slot_for`).
- `fleet_overview()` and `get_usage()` return it. Entries older than 6 hours are marked `stale: true`, because events only appear while runs execute.
- Warn at `used_pct >= HANGAR_USAGE_WARN_PCT` (default 80): `run_agent` returns `warnings: ["claude-max 7-day at 91%, resets 2026-10-16T14:00Z"]` and the dashboard banner turns amber.
- Gate at `>= HANGAR_USAGE_GATE_PCT` (default 95) or status `rejected`: new runs for that credential stay `queued/plan_limit` until `resets_at` or until the percentage drops. `run_agent(ignore_limits=true)` overrides. Running runs are never killed for this.
- A run that ends because the harness reports a rate-limit error becomes `failed/rate_limited` with `retry_after` in `detail`.
- **Concurrency.** `HANGAR_MAX_RUNS_PER_CREDENTIAL` (default 2). More runs wait in `queued/credential_concurrency` and start FIFO when a slot frees. The right number for a Pro plan is unknown (question 5), so the default is deliberately low and the live test measures it. The queue is persisted, so a control-plane restart keeps it. `run_agent` still returns immediately with `state: "queued"`.

### 5.6 MCP tools and REST endpoints

Existing tools keep their current parameters. New parameters have defaults. New return keys are additive.

```python
run_agent(sandbox: str, prompt: str, harness: str | None = None, cwd: str | None = None,
          idempotency_key: str | None = None, timeout_minutes: int | None = None,   # default 120
          verify: str | None = None, allow_concurrent: bool = True,
          ignore_limits: bool = False) -> dict
agent_status(sandbox: str, run_id: str, tail_lines: int = 60, include_tail: bool = True) -> dict
agent_events(sandbox: str, run_id: str, after: int = 0, limit: int = 200,
             types: list[str] | None = None, wait_seconds: int = 0) -> dict      # wait_seconds <= 25
resume_agent(sandbox: str, run_id: str, message: str | None = None,
             idempotency_key: str | None = None, timeout_minutes: int | None = None,
             verify: str | None = None) -> dict
cancel_agent(sandbox: str, run_id: str, grace_seconds: int = 10) -> dict
send_input(sandbox: str, run_id: str, text: str, idempotency_key: str | None = None) -> dict
list_runs(sandbox: str | None = None, state: list[str] | None = None, limit: int = 50) -> list[dict]
get_usage(credential: str | None = None) -> dict
```

`run_agent` response (old keys kept, new keys added):

```json
{"sandbox": "sb-1a2b3c4d", "run_id": "run-3fa91c20", "harness": "claude-code",
 "terminal_session": "run-3fa91c20", "log": "/home/agent/.hangar/runs/run-3fa91c20.log",
 "run_state": "starting", "session_id": "0b9e2c64-5d0c-4f4b-8a64-6a1d0c1f9e11",
 "events_cursor": 0, "warnings": []}
```

`agent_status` response:

```json
{"run_id": "run-3fa91c20", "sandbox": "sb-1a2b3c4d",
 "state": "running", "exit_code": null,
 "run_state": "running", "reason": null, "harness": "claude-code",
 "session_id": "0b9e2c64-5d0c-4f4b-8a64-6a1d0c1f9e11", "parent_run_id": null,
 "started_at": 1791765890.1, "finished_at": null, "elapsed_s": 122.3,
 "alive": {"supervisor": true, "heartbeat_age_s": 4, "last_event_age_s": 3, "warnings": []},
 "summary": {"phase": "running_tests", "last_tool": "Bash",
             "last_tool_summary": "uv run pytest -q tests/", "current_file": "src/hangar/fleet.py",
             "turns": 17, "input_tokens": 51200, "output_tokens": 6100, "cost_usd": 0.84,
             "tests": {"passed": 212, "failed": 0, "errors": 0, "skipped": 3, "source": "parsed"},
             "time_to_first_test_s": 14.2},
 "usage": {"credential": "claude-max", "seven_day_pct": 91, "five_hour_pct": 22,
           "resets_at": "2026-10-16T14:00:00Z", "stale": false},
 "result": null, "events_cursor": 41, "output_tail": "..."}
```

Compatibility of `state`: it stays `"running"` or `"finished"` for one release so clients that test `state == "finished"` keep working. `"finished"` now covers all terminal states. `run_state` carries the nine-state value. `exit_code` keeps its meaning and is `null` for `crashed`. `output_tail` is only filled when the sandbox answers; otherwise it is built from the last stored events. `agent_status` now works after the sandbox is destroyed (it reads the store; `_running()` is only needed for the live tail).

`agent_events`:

```json
{"run_id": "run-3fa91c20", "run_state": "running", "next_cursor": 42, "more": false,
 "events": [{"seq": 41, "ts": 1791766012.4, "type": "tool.start",
             "data": {"tool_id": "toolu_01", "name": "Bash", "summary": "uv run pytest -q tests/"}}]}
```

With `wait_seconds > 0` it returns as soon as an event with `seq > after` exists, or when the wait ends, or when the run turns terminal. 25 s keeps it below common client timeouts (the README example sets Codex `tool_timeout_sec = 900`, but not every client does).

`resume_agent` (details in 5.7):

```json
{"run_id": "run-77d0e1aa", "parent_run_id": "run-3fa91c20", "session_id": "0b9e2c64-...",
 "run_state": "starting", "idempotent_replay": false}
```

`cancel_agent`: `{"run_id": "...", "run_state": "cancelled", "reason": "cancel_agent"}`. On a terminal run it returns the current state and changes nothing.

`send_input` returns `{"run_id": "...", "delivered": true, "mode": "stdin"|"resume", "run_id_after": "run-..."}`.

`get_usage` returns the shape in `agent_status.usage` for each credential, plus `thresholds` and `limits`.

REST (new routes, bearer or UI ticket as in `app.py`):

| Route | Notes |
| --- | --- |
| `POST /api/sandboxes/{ref}/agent` | existing; body gains the new `run_agent` fields |
| `GET /api/sandboxes/{ref}/runs/{run_id}` | existing; returns the `agent_status` shape |
| `GET /api/runs?sandbox=&state=&limit=` | list |
| `GET /api/runs/{id}` | one run, with summary and result |
| `GET /api/runs/{id}/events?after=&limit=&wait=` | same as `agent_events` |
| `GET /api/runs/{id}/stream` | SSE |
| `GET /api/runs/stream` | SSE, state changes only |
| `POST /api/runs/{id}/resume`, `/cancel`, `/input` | JSON bodies as above |
| `GET /api/usage` | per-credential usage |
| `POST /api/environments/{name}/refresh` | rebuild baked image if the lockfile changed (5.9) |

Tools and routes are tagged `runs:read` or `runs:write` in code comments for the future scopes work. Today every token is `admin` (`auth.py:create`), so nothing is enforced.

### 5.7 Resume, cancel, input and idempotency

**Session ids.** hangar picks the id before the process starts, so there is nothing to scrape.

| Harness | New run | Resume | Status |
| --- | --- | --- | --- |
| Claude Code | `claude -p ... --session-id <uuid>` | `claude -p "<msg>" --resume <uuid>` | `--session-id` and `--resume` both confirmed in `claude --help` 2.1.296. The combination on GKE is checked in M0. |
| Codex | `codex exec --json ...`; id read from the first event | `codex exec resume <SESSION_ID> "<msg>"` | `resume [SESSION_ID] [PROMPT]` and `--last` confirmed in 0.162.1. Where the id appears in the JSON is unverified. Fallback: `resume --last` (it filters by working directory; `--all` turns that off). |
| Gemini CLI | `gemini -p ... --yolo -o stream-json --session-id <uuid>` | `gemini -p "<msg>" --resume latest` | `--session-id`, `--resume <latest\|index>` confirmed. `--resume` takes an index or `latest`, not a UUID, so resume depends on one session per project directory. `-p` plus `--resume` is untested. |
| agy | none | none | unverified |

`harnesses.py` also fixes Codex's flags. Proposed `run_new` for Codex: `codex exec --json --dangerously-bypass-approvals-and-sandbox "$(cat "$HANGAR_PROMPT_FILE")"`. That flag exists in 0.162.1 and its help says it is "intended solely for running in environments that are externally sandboxed", which is what hangar is. Whether `-s workspace-write` works inside gVisor is untested (question 4).

**Resume semantics.** `resume_agent(sandbox, run_id, message)`:

1. Load the parent run. It must be terminal (`succeeded|failed|crashed|cancelled|timed_out`) and have a `session_id`. Otherwise: error `run_active` (use `cancel_agent` first) or `no_session`.
2. If `idempotency_key` matches an existing run for this sandbox, return it with `idempotent_replay: true`. This check and the insert of the child run happen in one SQLite transaction, before any side effect.
3. Create a child `Run` (`parent_run_id`, same `session_id`, same `cwd`, default message "Continue where you left off. Check `git status` first." when `message` is null) and go through the normal queue.
4. Start the supervisor with `run_resume`. Same files, new run id.

The sandbox must still exist, and the harness's session file must still be there. For Claude Code that is `~/.claude/projects/<cwd-slug>/<session>.jsonl`. Workspace snapshots tar only `/workspace` (`fleet.py:_workspace_snapshot`), so they do not carry it. Full snapshots (Docker commit, E2B) do. After a sandbox is gone, resume is impossible unless M10 (transcript archive plus checkpoint) is built. `agent_status` reports `resumable: true|false` with the reason.

**Idempotency rules.**

- The client key is unique per `(sandbox, key)`, enforced by the partial unique index. Same key, same sandbox returns the stored run. Same key with a different prompt hash returns an error `idempotency_conflict`.
- Without a key, behaviour is as today: every `run_agent` call starts a new run. `resume_agent` without a key refuses if the parent already has a non-terminal child (`run_active`), which covers blind retries.
- Every server-side step is repeatable. The run row is written in `queued` first. File writes target paths named by the run id. The supervisor start is guarded: `[ -f <run>.pid ] && kill -0 ... || start`. If the control plane dies between steps, the monitor finds a `starting` run older than 60 s with no pid and either re-runs the start (once) or marks `crashed/start_failed`.
- `allow_concurrent=false` rejects `run_agent` when another run is live in the same `cwd`. The default stays `true` for one release (with a warning), then flips, since two writers in one worktree is the failure mode.

**Cancel.** Write `<run>.cancel`, wait `grace_seconds` for the supervisor to TERM the child's process group, then KILL, then confirm with a probe. Sets `cancel_requested_at` first so a racing exit is classified `cancelled`. Idempotent. A `queued` run is cancelled in the store only.

**send_input.**

- If the run is `waiting_input` and the harness supports a stdin channel, write the text to the run's FIFO `<run>.in` and move to `running`. Claude Code's `--input-format stream-json` is the candidate (flag exists; semantics unverified, question 6). This is milestone M9, not v1.
- If the run is terminal and resumable, `send_input` is `resume_agent(message=text)` and returns `mode: "resume"`.
- Otherwise: error `not_waiting`.

This keeps one verb for "say something to that agent" without promising mid-turn steering that I could not verify.

**Verification.** `verify` is a shell command (for example `uv run pytest -q`). After the agent exits 0, hangar runs it with `prov.exec(cwd=run.cwd)`, stores `{command, exit_code, output_tail}` in `result.verification`, and sets `failed/verify_failed` if it fails. Afterwards it fills `RunResult` with read-only git commands: `git rev-parse HEAD`, `git rev-list --count <base>..HEAD`, `git status --porcelain`, `git rev-parse @{u}`. `pr_url` is scraped from the final message and labelled `agent_reported`. CI status and merge are not part of this spec.

### 5.8 Dashboard UX (`ui/app.js`, `ui/index.html`)

- **Banner** at the top of the fleet page: `Claude Pro · 7-day 91% · 5-hour 22% · resets Fri 14:00 · 2 queued`. Amber at 80%, red at 95%, grey with "stale" when no recent event.
- **Bay card** (`app.js` around line 137, replacing the `bay-runs` buttons): one row per run with a state chip and a heartbeat dot.

```
 api-refactor-1   running   branch agent/flaky-1
  run-3fa91c20  claude-code   RUNNING  ● alive 4s   2m02s   17 turns   51k tok
   Running tests · uv run pytest -q tests/        tests: 212 passed, 0 failed
   src/hangar/fleet.py · 3 files touched
   [Open terminal] [Cancel] [Events ▾]
  run-0a77be41  claude-code   CRASHED  process_died · 6m ago   [Resume…] [Events ▾]
```

- **Events drawer** subscribes to `/api/runs/{id}/stream` via the existing ticket flow (the dashboard never puts its token in a URL). Newest at the bottom, tool calls collapsed to one line, errors in red, `rate_limit` events as a thin row. Text is inserted with the existing `esc()` helper, never as HTML.
- **Resume dialog** has a message box and the session id (copyable). **Waiting input** shows the prompt and a reply box.
- **Runs page** lists runs across the fleet, with crashed and waiting_input first.
- **Run result block** after the end: branch, head sha, commits ahead, dirty flag, verification, PR link labelled "reported by agent".

### 5.9 Ready-to-test sandboxes

**What exists.** `repo.bake: true` makes `envspec.render` clone the repo and run `repo.setup` inside the image (step 6). At claim, `fleet.py:_setup_repo` does `git fetch`, checks out the branch, and skips `setup` when the repo was baked (`... and not baked`).

**What is wrong:**

1. The image tag is `sha256(Dockerfile + context files)[:16]` (`envspec.py:render`, `image_tag`). The baked clone and install are identical text whether or not the lockfile changed, so the tag does not change, `_ensure_image` returns the old image, and Docker's layer cache would reuse the stale layer even if a rebuild ran.
2. Claim-time `git pull` can bring a changed lockfile with no install. The sandbox silently has stale dependencies.
3. `render()` is pure and runs on every `create` (`_tag_for`). Fetching a lockfile from the network there would add network latency and failure modes to every claim.
4. A private repo cannot be cloned at build time. The Dockerfile has no credentials.

**Design.**

Spec additions in `RepoSpec`:

```yaml
repo:
  url: https://github.com/Sean-Koval/hangar.git
  branch: main
  bake: true
  deps:
    lockfiles: [uv.lock, pyproject.toml]     # default: autodetect from the table below
    install: uv sync --frozen --all-extras   # default: from the table below
    cache: [~/.cache/uv]                     # kept in the image so claim-time installs are incremental
  setup: null                                # still runs for non-baked repos
```

| Detected file | Default `install` | Default cache dir |
| --- | --- | --- |
| `uv.lock` | `uv sync --frozen --all-extras` | `~/.cache/uv` |
| `package-lock.json` | `npm ci` | `~/.npm` |
| `pnpm-lock.yaml` | `pnpm install --frozen-lockfile` | pnpm store |
| `Cargo.lock` | `cargo fetch --locked` | `~/.cargo/registry` |
| `go.sum` | `go mod download` | `GOMODCACHE` |

1. **Lock digest pin.** A new `Fleet.resolve_repo_pin(spec)` returns `{commit, lock_digest}`: `sha256` over the lockfile contents of `repo@branch`. It runs `git clone --depth 1 --filter=blob:none --no-checkout` into a temp dir and `git show HEAD:<file>` for each lockfile, using the vault's git credential when the repo is private. The result is cached in `kv` (`pin:<env>`, TTL 5 min). `render()` takes the pin as an argument and stays pure. The `lock_digest` goes into the Dockerfile as `ENV HANGAR_LOCK_DIGEST=...` and is written to `/etc/hangar/deps.sha` in the bake step. The commit is not part of the tag: only dependency changes rebuild.
2. **Layer order.** Harnesses, tools and skills layers stay before the bake layer, as now. Inside the bake layer the `ENV HANGAR_LOCK_DIGEST` line comes first, so a lockfile change invalidates only the clone-plus-install layer. When the builder is BuildKit (already assumed, `_build.py:docker_build` and Cloud Build with `DOCKER_BUILDKIT=1`), the install step uses `RUN --mount=type=cache,target=<cache dir>`, so rebuilds reuse downloaded packages without putting a shared writable cache into any sandbox.
3. **The image is a cache, not the source of truth.** At claim, after checkout, `_setup_repo` runs one drift check inside the sandbox:

   ```bash
   cd "$dest" && new=$(cat <lockfiles> | sha256sum | cut -d' ' -f1)
   [ "$new" = "$(cat /etc/hangar/deps.sha 2>/dev/null)" ] || { <deps.install>; echo drift >&2; }
   ```

   In sync, this costs one `sha256sum` (tens of milliseconds). On drift it runs the install against the baked cache (seconds for a few changed packages) and sets `sb.info["deps_drift"] = true`. Correctness never depends on how fresh the image is. This claim-time step is hangar's setup phase. It runs before the agent starts, with network, and not as an agent action. That keeps it compatible with a later "no internet during the agent phase" policy (Codex's model).
4. **Never build on the create path.** `_ensure_image` keeps today's behaviour: use an existing image. If the pin says the lockfile changed, it still creates the sandbox from the old image, sets `deps_drift`, and queues a background `build` (debounced per env). The warm pool refills from the new image through `sync_warm_pool` (`updateStrategy: Recreate`).
5. **Refresh triggers.** `POST /api/environments/{name}/refresh` and `hangar env refresh <name>` (for CI on pushes to the default branch). An opt-in reaper check every `HANGAR_BAKE_CHECK_MINUTES` (default 0 = off) compares the pin to the image's digest.
6. **Private repos at build time.** Local builds (`hangar env publish`, which already builds with the user's Docker) pass the token as a BuildKit secret (`RUN --mount=type=secret,id=git_token`), so it never lands in a layer. Cloud Build for private repos needs Secret Manager wiring; open question 8. Until then a private baked repo is built locally.
7. **Caches.** No shared writable volumes. `docs/security.md` says "no shared volumes", and a package cache written by one sandbox and read by another would be a poisoning path. Per-repo cache means: the image keeps `deps.cache` dirs (read at claim-time drift installs), BuildKit cache mounts speed up builds, and the baked `.git` makes the claim-time fetch a delta. Each sandbox's own cache writes go to its own filesystem layer (Docker) or ephemeral storage (k8s).

**Warm pools.** Baked images work as is: Kubernetes `SandboxWarmPool` pods and Docker warm pools start from the baked image; a claim adds only credentials, fetch/checkout, work branch and the drift check. Set `warm_pool: N` on baked environments. E2B has no warm pools (`Capabilities.warm_pools=False`), so there the baked template is the only lever.

**Expected start-to-first-test.** From `create_sandbox` returning to the first test command starting. Estimates, not measurements.

| Step | Today (unbaked) | Baked, in sync | Baked, drift |
| --- | --- | --- | --- |
| Claim from warm pool (live run) | ~1.5 s each | ~1.5 s | ~1.5 s |
| Clone / fetch + work branch | clone, 2-10 s depending on repo | fetch delta, <1 s | <1 s |
| `uv sync --all-extras` | 30-90 s cold (assumed) | 0 s (skipped) | 3-15 s (assumed) |
| Drift check | none | <0.1 s | included above |
| Agent starts and reaches its test step | same | same | same |
| **Sum before the agent starts** | **~35-100 s** | **~2-3 s** | **~6-20 s** |

The README reports 0.6 s for claim + clone + branch in an earlier test and the live run measured 3.1 s for two sandboxes. The `uv sync` figures are assumptions: the live run did not time it. M0 records the real baseline on the same cluster. Acceptance (section 8) requires p50 of 15 s or less and a max of 30 s, measured from the `run.state` timestamps and `time_to_first_test_s`.

Costs: image size grows by the dependency tree (hundreds of MB for a Python project); Artifact Registry storage grows per environment; a new node pulls the image once. Warm pods absorb the first pull. Whether GKE image streaming works on gVisor nodes is unknown (question 9).

**Provider differences** (all of section 5):

| Topic | Docker | Kubernetes (GKE Agent Sandbox) | E2B |
| --- | --- | --- | --- |
| Probe transport | Docker exec API | Kubernetes exec through the API server | E2B `commands.run` |
| Dead-sandbox signal | `list_refs()` | `list_refs()` over SandboxClaims; Spot preemption removes the claim | none; 3 failed probes plus `status()` |
| `/proc` start time | normal Linux; runc, gVisor or Kata | gVisor: unverified (question 7), heartbeat covers it | Firecracker VM: normal Linux |
| Warm pools | yes | yes (`SandboxWarmPool`) | no |
| Supervisor needs | `setsid`, bash | same; admission policies are unaffected since it adds no pod fields | same |
| Resume after sandbox loss | only with full snapshots (`docker commit`) | no full snapshots (`full_snapshots=False`); needs M10 | E2B snapshots |
| Private-repo bake | local build with secret | local build; Cloud Build open | local build, then registry |

## 6. Security and failure modes

Trust model: the agent controls everything inside the sandbox, including `.hb`, `.exit`, `<run>.log` and the supervisor. Treat every byte from the sandbox as untrusted input to the control plane.

- **Forged status.** An agent can write `.exit` with 0, or fake events. So `succeeded` is only "ended cleanly plus the caller's `verify` passed", and `RunResult` fields come from hangar's own `exec` of git, not from the log. This applies Jarmak's "verify the workspace, not the self-report".
- **Forged rate limits.** A fake `rate_limit` event could make hangar queue runs. The gate affects only queueing, is overridable (`ignore_limits`), and the banner shows the last raw event. It can't kill anything.
- **Event flood and oversized lines.** Per-tick caps (256 KiB, 2,000 events), a 1 MiB line limit, 14-day retention, a per-run event cap. SQLite writes are batched.
- **Secrets in events and prompts.** Text fields are redacted with `broker.redact`, the same as audit entries (`fleet.py:audit`). Redaction only knows vault values (documented in `docs/security.md`). The full prompt is now stored in SQLite, not just 200 characters, so stored runs are as sensitive as audit.
- **Prompt injection through status.** `message.text`, `final_text` and `tool.end.summary` are attacker-influenced strings that an orchestrator model will read. Responses carry `data.source = "agent"` on these fields. hangar-derived fields (`run_state`, `reason`, `alive`, `result`) are separate keys. The orchestrator's instructions in `mcp_server.py:INSTRUCTIONS` get one line saying agent text is untrusted. This is Willison's trifecta: this spec adds a path for untrusted content to reach a model that holds tools, so it labels it.
- **MCP rules.** No token passthrough: usage and events never include `CLAUDE_CODE_OAUTH_TOKEN`; the harness `session_id` is a conversation id, not an auth token, and every call still needs the bearer token. Minimal scopes: new tools are tagged `runs:read` and `runs:write`. They do not enlarge what an admin token can already do (`exec` can read the same files today).
- **Branch safety.** This spec adds no push, merge or credential path. The supervisor runs as the sandbox user with the same capabilities. `resume_agent` re-reads `~/.hangar/env` like the first run. Verification runs `git` read-only. The "agents must never merge or push outside their branch" requirement belongs to the credential-proxy spec (Claude Code on the web keeps git credentials outside the sandbox and checks each push; see Anthropic's [sandboxing post](https://anthropic.com/engineering/claude-code-sandboxing)). This spec's `result.pushed` and `commits_ahead` give that work something to assert on.
- **Shell injection.** `extra_args` stays internal. `verify` and `message` are quoted or written to files. The supervisor script and prompt file names use only the validated run id.
- **Concurrency per credential** is also a WorkOS-style per-agent control: the unit of limiting is the credential, not the sandbox.
- **Failure modes.**

| Failure | Result |
| --- | --- |
| Control plane restarts mid-run | Monitor resumes from `log_offset`; no duplicates (unique offsets). |
| Probe `exec` hangs | Each probe has a 20 s timeout; one slow sandbox does not block others (probes run with `asyncio.gather`, bounded to 8 concurrent). |
| Supervisor itself killed (SIGKILL) | Heartbeat stops, pid dead, no exit file: `crashed/process_died`. The harness child may linger; the probe sends KILL to the recorded child pid before marking it. |
| Agent forks work that outlives the harness | Out of scope; sandbox destroy cleans up. |
| Clock skew between control plane and sandbox | Ages are computed from `stat` mtimes read in the sandbox against `date +%s` read in the same probe, not against the control plane's clock. |
| Pod frozen (paused sandbox) | `PAUSED` suppresses stale-heartbeat warnings; deadline timers pause with it. |
| Two `resume_agent` calls race | Unique key, or the `run_active` check, inside one transaction. |
| Queue starvation if a credential's runs never end | `timeout_minutes` always applies (default 120). |

## 7. Rollout and migration

1. **Schema.** `Store.__init__` creates the new tables with `IF NOT EXISTS`. No change to existing tables.
2. **Backfill.** At startup, for each `Sandbox` with `info["runs"]`, create a `Run` (same `id`) if none exists. Their liveness is unknown: they have no pid file. For these `legacy` runs the monitor uses the old test (`.exit` present = finished, else tmux session present = running). If neither exists after one probe, the run becomes `crashed/legacy_lost`. This clears the "stuck running" runs from before the upgrade.
3. **Feature flag.** `HANGAR_RUNS_V2` (default on after M3). When off, `run_agent` uses the old tmux-wrapper path and still writes a `Run` row, so the store is correct either way.
4. **Dual write.** `sandbox.info["runs"]` keeps being written for one release; the dashboard moves to `/api/runs` in M7.
5. **Behaviour changes** (the explicit list):
   - `agent_status.state` is unchanged (`running`/`finished`). New key `run_state`. Planned removal of the legacy meaning: two releases later, announced in the changelog.
   - The `<run>` tmux session now only follows the log. Anything that typed into it never reached the agent, so this is invisible in practice. `terminal_read` on it still works.
   - `run_agent` may return `run_state: "queued"` when the credential is at its concurrency limit or the plan gate is closed. Clients that assume it starts at once still get a valid `run_id`.
   - `agent_status` no longer needs the sandbox to be RUNNING.
   - Codex runs use new flags (the old ones fail on 0.162.1, so there is no working behaviour to preserve).
   - `allow_concurrent` default flips from `true` to `false` one release after M2.
6. **Docs.** `README.md` MCP table, `docs/clients.md` (polling example), `docs/gke.md` (baked environments, Spot caveat), `docs/security.md` (run data and event handling).
7. **Rollback.** Turn off `HANGAR_RUNS_V2`. The tables stay and are ignored.

## 8. Test plan

### 8.1 Unit tests (fake provider, `tests/conftest.py`)

The fake provider runs commands in a local box with a private `TMUX_TMPDIR` (the #12 fix). New tests use a scripted fake harness: a bash script whose output and exit code are set per test, selected through an env var in the box's `~/.hangar/env`. Time-dependent logic takes an injectable clock (`now` from `models.py`).

| Test file | Cases |
| --- | --- |
| `test_runs_store.py` | put/list/filter; unique idempotency index; `append_events` idempotent on `(run_id, src_offset)`; pruning; backfill from `info["runs"]`. |
| `test_runevents.py` | Claude fixtures from the live log (M0): init, tool use/result, usage, result, `rate_limit_event`; non-JSON lines; 2 MiB line; unknown type kept as `harness.unknown`; redaction of a vault secret in `text`; pytest summary detection; no tests means `tests is None`. Codex and Gemini fixtures from M0 captures. |
| `test_run_liveness.py` | clean exit gives `succeeded`; exit 3 gives `failed/nonzero_exit`; `kill -9` of the child gives `crashed/process_died` within 3 probes of an injected clock; `tmux kill-server` mid-run leaves the run `running` and it ends `succeeded`; stale heartbeat with a live pid warns only; probe failure ×3 plus provider STOPPED gives `crashed/sandbox_gone`; `destroy()` gives `cancelled/sandbox_destroyed`; deadline gives `timed_out` and the child is dead; `starting` with no pid for 60 s gives `crashed/start_failed`. |
| `test_run_resume.py` | resume on a live run gives `run_active`; resume with the same key twice gives one child and `idempotent_replay`; same key, different prompt gives `idempotency_conflict`; new instance of `Fleet` on the same store after a "crash" keeps the queue and does not double-start; cancel is idempotent and ends the process group; `send_input` on a terminal resumable run maps to resume. |
| `test_run_queue.py` | limit 1: second run `queued/credential_concurrency`, starts when the first ends; usage gate at 95 queues, `ignore_limits` bypasses; `rate_limit` event updates `get_usage`; stale marking. |
| `test_run_result.py` | `result` filled from a real temp git repo (commits ahead, dirty, upstream); `verify` failure gives `failed/verify_failed`; agent-reported PR URL labelled. |
| `test_mcp_compat.py` | the old `run_agent(sandbox, prompt, harness, cwd)` and `agent_status(sandbox, run_id, tail_lines)` calls still work; old response keys exist with the old meaning (`state`, `exit_code`, `output_tail`, `terminal_session`, `log`). |
| `test_envspec.py` (extend) | tag changes when lock digest changes; tag does not change when only the repo commit changes; layer order golden file; autodetected install/cache for each lockfile type; drift script outputs. |
| `test_fleet.py` (extend) | claim-time drift check runs install only when the digest differs; `create` never triggers a blocking build for a baked env with an existing image. |

The suite must not call the real `tmux kill-server` outside the private socket. The tests above use the same isolation.

### 8.2 Live acceptance on GKE

Setup: the 2026-10-11 cluster, one Claude Pro token, a baked environment for this repo with `warm_pool: 2`, a throw-away GitHub repo and fine-grained token (same as the first run). Repeat each scenario 3 times and report median and range (Jarmak: single runs vary).

| # | Scenario | Pass criteria |
| --- | --- | --- |
| A | Start-to-first-test. `create_sandboxes(count=2)`, then `run_agent` with a prompt that runs `uv run pytest -q` first. Baseline: same on an unbaked environment. | Baked: `time_to_first_test_s` median ≤ 15 s, max ≤ 30 s. Baseline recorded. No `uv sync` process visible in the sandbox. |
| B | tmux death. While a run is in its test step, `exec` `tmux kill-server`. | Run stays `running`, heartbeat age under 15 s, and the run ends `succeeded` or `failed` by its own exit code. No run left "running" without a live pid. |
| C | Agent death. `exec` `pkill -9 -f claude` plus the supervisor. | `run_state = crashed`, `reason = process_died` within 45 s, `last_event` present, `exit_code = null`. |
| D | Sandbox death. `kubectl delete pod` (stands in for Spot preemption) during a run. | `crashed/sandbox_gone` within 90 s. `agent_status` still answers from the store. |
| E | Event stream. A real 5-minute task. Poll `agent_events(after=cursor, wait_seconds=20)` and watch SSE. | Seqs are contiguous. Event count equals the count of parseable lines in the raw log (`jq -c . log \| wc -l`). Phase changes show within 5 s while watched. Dashboard shows tool and test summary. |
| F | Plan usage. | `get_usage` 7-day percentage equals the last `rate_limit_event` in the raw log. Banner shows it. With `HANGAR_USAGE_GATE_PCT` set below the current value, a new run is `queued/plan_limit`, and starts after the gate is lifted. |
| G | Concurrency. Start 4 runs with `HANGAR_MAX_RUNS_PER_CREDENTIAL=2`. | 2 run, 2 queued; queued ones start when slots free; no harness rate-limit error. Record the largest concurrency the plan sustains (sets the default). |
| H | Resume. Prompt: "create `note.txt` containing a random word, then run the tests". `pkill -9` mid-run. `resume_agent(message="what word did you choose? append it to note.txt")`. | Same `session_id`; the answer matches the word in the first run's events; `pgrep -c claude` is 1 during the run. Calling `resume_agent` twice with one key gives one child run. |
| I | Cancel. `cancel_agent` during a long tool call. | `cancelled` within grace + 5 s; no child processes left, none defunct (`ps`). Second call is a no-op. |
| J | Regression. An unchanged client script calls `run_agent` and polls `agent_status` for `state == "finished"`. Re-run the first run's security probes. | Script works. Metadata server, Kubernetes API, control plane and kubelet unreachable. No new pod fields; admission policies pass. |
| K | Result. Run to the PR step. | `result.branch/head_sha/commits_ahead` match `git` in the sandbox; `pr_url` is labelled `agent_reported`; verification result matches a manual `pytest`. |

## 9. Milestones

Each is one PR unless noted. Order matters; the dashboard (M8) can start once M3 is merged.

| # | Task | Scope | Size |
| --- | --- | --- | --- |
| M0 | Fixtures and flag fixes | Copy stream-json logs from the 2026-10-11 run (scrubbed) into `tests/fixtures/`. Capture Codex and Gemini sample output. Fix Codex flags in `harnesses.py`. Verify `--session-id` plus `--resume` for Claude in the GKE image. Record the unbaked start-to-first-test baseline. | S |
| M1 | Run model and store | `Run`, `RunState`, tables, `Store` methods, backfill, dual write, `list_runs`/`get_run` MCP tools and REST. `run_agent` writes a `Run` (old process path). | M |
| M2 | Supervisor, liveness, cancel | `hangar-run` script, `setsid` start, pid/hb/exit files, run monitor, decision table, hooks in `_mark_gone`/`destroy`/TTL, timeouts, `cancel_agent`. Legacy handling. | L |
| M3 | Events and status | `runevents.py`, Claude parser, ingestion, `agent_events`, new `agent_status` shape, SSE routes, adaptive polling. | L |
| M4 | Usage and queue | Usage store, `get_usage`, warn/gate, per-credential concurrency, `queued` handling and dispatcher. | M |
| M5 | Resume and idempotency | Session ids in harness profiles, `resume_agent`, idempotency keys, `send_input` (resume mode only), `allow_concurrent`. | M |
| M6 | Result and verify | `RunResult` probes, `verify`, PR URL labelling. | S |
| M7 | Codex and Gemini adapters | Parsers, resume commands, fixtures; ship only what M0 confirmed. | M |
| M8 | Dashboard | Banner, run rows, events drawer, resume/cancel dialogs, runs page. | M |
| M9 | Baked dependencies | `repo.deps`, lock pin, layer order, drift check, background rebuild, `refresh` endpoint and CLI, docs. | L |
| M10 | Stdin channel and checkpoints (optional) | FIFO `send_input` for Claude; transcript archive plus workspace checkpoint so resume survives sandbox loss. | L |
| M11 | Docs and flag flips | README, clients, gke, security docs; `allow_concurrent` default; changelog for the `state` deprecation. | S |

Dependencies: M2 before M3 (ingestion reads what the supervisor writes). M4 after M3 (needs `rate_limit` events). M5 after M2. M9 is independent and can run in parallel with M3-M6.

## 10. Open questions

1. **Claude `rate_limit_event` fields.** Names, window labels, and whether it is emitted below a warning threshold. The live run showed 91% only. M0 reads the captured log.
2. **Codex JSONL.** Event names, where the thread id appears, and what usage/rate-limit data is emitted. `--json` and `resume` are confirmed; the schema is not.
3. **Gemini stream-json.** Field names inside the six event types, whether `-p` works together with `--resume`, and a stable way to resume by id instead of `latest`/index.
4. **Codex in gVisor.** Is `--dangerously-bypass-approvals-and-sandbox` the right choice, or does `-s workspace-write` work inside the pod? Removing the inner sandbox is consistent with "externally sandboxed" but is a policy decision for you.
5. **Pro plan concurrency.** How many simultaneous headless sessions a single Pro token tolerates before throttling. The default of 2 is a guess until scenario G.
6. **Claude `--input-format stream-json`.** Does it accept messages while a turn is running? Does it make `waiting_input` reachable? Decides M10.
7. **`/proc/<pid>/stat` under gVisor** (and on Kata). If start time is unreadable, use `ps -o lstart=` or rely on the heartbeat alone.
8. **Cloud Build and private repos.** How to pass the git token to the bake step without it entering a layer (Secret Manager `secretEnv`?).
9. **Image streaming and size.** Does GKE image streaming apply to gVisor nodes? What is an acceptable baked-image size per environment?
10. **Where to archive transcripts** (M10): SQLite blob, a GCS bucket, or not at all.
11. **Retention defaults** (14 days, 20,000 events) are guesses; check real event volume from scenario E.
12. **MCP progress notifications.** Should `agent_events` also push `notifications/progress`? Many clients ignore them; polling stays the contract.
13. **Cost field.** `total_cost_usd` on a subscription is an estimate. Should hangar hide it for subscription credentials?
14. **Fork plus resume.** A full-snapshot fork inherits the session file. Is "resume the parent's conversation in a fork" a feature to expose, or something to block?
