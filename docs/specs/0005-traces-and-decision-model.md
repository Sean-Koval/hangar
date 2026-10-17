# 0005: Session traces and a decision model for run classification

Status: deferred (2026-10-15): the `DecisionService` and Decision-1 are not scheduled; trace export moved to [0014](0014-sister-tool-interfaces.md), and evaluation to flightdeck. Was: draft. · Date: 2026-10-12 · Builds on: 0001 (run events), 0002 (verification, approvals), 0003 (gateway, egress) · Consumed by: 0004 (supervisor)

> Part of a set. Ownership, cross-spec decisions and the combined build order are in [docs/specs/README.md](README.md); where this spec conflicts with that page, the page wins.

## 1. Summary

- Collect OpenTelemetry from Claude Code sessions through an OTLP endpoint on the gateway listener. Tag every signal with hangar's sandbox, run and task ids on the server side.
- 0001's event stream stays the spine. OTel adds what the stream lacks (API latency, retries, errors, permission decisions, span timing) as extra rows in the same table, not a second timeline.
- A provider-neutral `DecisionService` scores fixed options for six decision tasks. Microsoft Decision-1 is the first backend. A rules baseline and a local or cheap-LLM backend are the fallbacks.
- Decisions are advisory and logged next to ground truth (0002 verification, human approvals) until measured calibration says otherwise. Nothing acts on a decision in v1.
- 0004 uses two calls: `classify_run` and an event/span query. A separate loop turns traces, decisions and outcomes into dashboards, environment fixes and paired evaluations.

## 2. Problem and evidence

From the 2026-10-11 GKE run, where two Claude Code agents in gVisor sandboxes opened PRs #13 and #14:

1. **An agent was dead for 10+ minutes while `agent_status` said `running`.** Status today is "no `.exit` file yet" (`fleet.py:agent_status`). Nothing watched the process. 0001 fixes this with a pid probe. That is a deterministic check and stays deterministic here. A model must never be the thing that decides "the process is gone".
2. **Plan usage at 91% was visible only in the raw log.** A `rate_limit_event` line sat in the stream-json output. No code read it.
3. **The orchestrator wrote its own stream-json parser** to get anything useful out of `run_agent`. 0001 moves that parser into hangar. This spec makes sure a second orchestrator does not write a second parser for traces.

What is still missing after 0001:

- Stream-json says what the agent did. It does not say how the API behaved: request latency, retries, overload and 429 errors, or whether a tool call needed a permission decision. Those explain slow or failed runs.
- 0001 detects *dead* and *waiting for input*. It cannot say whether a live run is making progress, looping on the same failing command, or blocked by something in the environment. Those are judgement calls over text.
- Nobody can answer "which environment fails most, and why?" or "did the new instructions help?" Outcome data exists (0002 verification) but is not joined to run behaviour.
- Jarmak, "Engineering Reliable Coding Agents" (arXiv 2608.13867), reports that single-run results vary by 2–6 points and that one model "submitted" 100% of trials while resolving 44% under an external oracle. Any optimization claim built on one run, or on the agent's own claim, is noise.

What I verified for this spec:

| Item | Status |
| --- | --- |
| Claude Code OTel: `CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_{METRICS,LOGS,TRACES}_EXPORTER`, `OTEL_EXPORTER_OTLP_*`, `OTEL_LOG_USER_PROMPTS` / `_TOOL_DETAILS` / `_TOOL_CONTENT`, `OTEL_RESOURCE_ATTRIBUTES`, beta tracing via `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`, inbound `TRACEPARENT` | Documented at code.claude.com/docs/en/monitoring-usage. The variable names also appear in the installed 2.1.296 binary. Behaviour not run. |
| Metric and event names (`claude_code.token.usage`, `cost.usage`, `commit.count`, `pull_request.count`, `user_prompt`, `tool_result`, `tool_decision`, `api_request`, `api_error`) | From the docs. Attribute names inside each are not copied here. M0 captures real payloads. |
| Whether `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (set in `harnesses.py`) blocks OTel export | **Unknown.** Open question 1. |
| pi `--mode json` | Event types as given to me: session header, `agent_start/end`, `turn_start/end`, `message_*`, `tool_execution_start/update/end`. pi is not installed here. No OTel that I can verify. |
| Codex `codex exec --json` | JSONL, schema unverified (0001 OQ2). I did not find verified OTel support. Open question 2. |
| Antigravity (`agy`), Gemini CLI | Not installed. Unknown. Open question 2. |
| Microsoft Decision-1 | Facts as given in the brief (release date, size, price, latency). Not independently checked. The API request shape is **unknown**. Open question 6. |

## 3. Goals / non-goals

**Goals**

1. Capture Claude Code OTel (metrics, logs, and the beta traces) per run, on docker, GKE and E2B, with ids that tie each signal to a sandbox, run and task.
2. Join OTel to 0001's event stream without a second timeline or duplicated normalization.
3. Keep prompts, tool output and secrets out of stored telemetry by default. Redact on the server, not only at the source.
4. A `DecisionService` with fixed, versioned decision tasks. Advisory first. Every decision logged with the ground truth that later arrives.
5. Extra model spend small, capped and visible. No new spend at all in no-egress mode.
6. A loop from traces, decisions and outcomes to dashboards, environment fixes, instruction changes and routing, evaluated with repeats and paired comparisons.
7. A small, stable interface for 0004.

**Non-goals**

- Replacing 0001's liveness watcher, 0002's verification, or 0003's enforcement. A model score is never a security control.
- Letting a decision merge, push, cancel or spend without a human in v1.
- Fine-tuning or hosting Decision-1. There are no open weights.
- Building a general observability product. We store enough to answer hangar's questions and forward the rest to an external backend if the owner runs one.
- Telemetry for Codex, pi, Antigravity in v1 beyond what 0001-style log parsers give. Parsers for pi are in scope (M3). OTel for them waits on open question 2.
- Using the Claude subscription token for decision calls. Whether that is allowed is 0003 OQ5, and it is a model-API call anyway. Decision backends use their own API keys.

## 4. User stories

1. As the owner, I open a run and see a timeline: which tool calls were slow, which API requests retried, where 3 minutes went to a 529 overload error.
2. As an orchestrator (Claude Code via MCP), I call `classify_run` and get "looping, 0.81" with a pointer to the repeated command, before I wait out a timeout.
3. As the owner, I see on the fleet page that `claude-python` fails 4 of 10 runs on "missing dependency", with the exact package, and a proposed spec change to review.
4. As the owner, I can see what I spent on decision calls this week (cents) and turn them off with one setting.
5. As a security-conscious user, I can run with no data leaving the cluster. Decisions fall back to rules and a local model.
6. As the owner, I can compare two instruction sets on 20 tasks with 3 repeats each. The report says effect size, interval and cost, and refuses to call a 3-point difference a win.
7. As the 0004 supervisor author, I get one call to classify a run and one query for events and spans, with documented JSON.

## 5. Design

### 5.1 Overview

```
 sandbox (claude -p ...)                         control plane pod
 ┌──────────────────────┐   OTLP/HTTP protobuf   ┌──────────────────────────────────────────┐
 │ OTEL_* env per run   │ ─────────────────────► │ gateway listener :8788   /otlp/v1/*      │
 │ Authorization: run   │   (same hop 0003       │   auth: run ingest key → run_id          │
 │ ingest key           │    already allows)     │   redact → map → write                   │
 │ stream-json log ─────┼─ 0001 probe ─────────► │ :8787 supervisor/ingester (0001)         │
 └──────────────────────┘                        │                                          │
                                                 │ run_events (0001, SQLite hangar.db)      │
                                                 │   + OTel-derived rows (src="otel")       │
                                                 │ traces.db (separate SQLite file)         │
                                                 │   spans, otel_logs, run_metrics          │
                                                 │ DecisionService ──► backends             │
                                                 │   rules · local · decision-1 · openrouter│
                                                 │ optional OTLP forward ─► external backend│
                                                 └──────────────────────────────────────────┘
```

Decisions and why:

- **The collector is a route set in the control plane, not a new pod.** It needs the run table to authenticate and tag, and the redactor. A stock OpenTelemetry Collector pod would need a copy of both. Cost: ingestion shares a process with the API. Mitigation: a bounded queue and a separate SQLite file (5.3). If volume outgrows this, the route set can move behind the same URL.
- **It lives on the gateway listener (:8788), the one 0003 already opens to sandboxes.** Under 0003's default-deny egress, sandboxes may reach "the matching proxy pods and the gateway Service". An OTLP route on the gateway needs no new NetworkPolicy rule and no new allowlist entry. Before 0003 M2 exists, the same router is mounted on :8787 with the same auth. Nothing in this spec waits on 0003.
- **OTLP over HTTP/protobuf only** (`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`). It passes through the gateway, plain HTTP proxies and E2B's public path. gRPC would need a second listener and HTTP/2 end to end. Whether Claude Code supports `http/json` as well is documented in the monitoring page; we pick protobuf because the library is smaller on the Python side. Revisit if protobuf parsing is a dependency problem (open question 9).
- **0001 stays the spine.** Every OTel item that has an equivalent in 0001's schema is used to *enrich* that event, not to emit a duplicate. Only items 0001 lacks become new rows.

### 5.2 Getting telemetry out of the sandbox

`Fleet.run_agent` already writes a per-run script (`fleet.py`, `run_agent`). It exports `HANGAR_RUN_ID` and sources `~/.hangar/env`. Telemetry variables go in that script, per run, not into the image. The run id and ingest key do not exist at image build time.

For `claude-code` the script adds:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT="$HANGAR_OTLP_ENDPOINT"            # per provider, see 5.6
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $HANGAR_OTLP_KEY"
export OTEL_RESOURCE_ATTRIBUTES="hangar.run_id=run-3fa91c20,hangar.sandbox_id=sb-1a2b3c4d,hangar.task_id=...,hangar.environment=claude-python,hangar.harness=claude-code"
# traces (beta), on by default only when the environment sets telemetry.traces: true
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 OTEL_TRACES_EXPORTER=otlp
export TRACEPARENT="00-<trace_id>-<root_span_id>-01"
# content stays redacted at the source
# OTEL_LOG_USER_PROMPTS, OTEL_LOG_TOOL_CONTENT: unset.  OTEL_LOG_TOOL_DETAILS: see below.
```

Details:

- **Resource attributes are a convenience, not the source of truth.** The server tags every signal from the *ingest key*, not from attributes the sandbox sent. A compromised or confused agent could set any `OTEL_RESOURCE_ATTRIBUTES`. The server drops client-sent `hangar.*` keys and writes its own. The attributes are still set so an external backend that receives a forwarded copy can group by them.
- **Ingest key.** A random `hgt_` token per run, minted by `run_agent`, stored hashed in `kv` as `otlp_key:<sha256> → {run_id, sandbox_id, expires_at}`. It authorizes `POST /otlp/v1/*` and nothing else. It expires `timeout_minutes + 30 min` after start and is deleted when the run is terminal and drained. Under 0003 the key is a field on the sandbox's grant instead (`SandboxGrant.scopes += "telemetry:write"`); until then it is its own table. Open question 8: one key per run or per sandbox.
- **`TRACEPARENT`.** Hangar generates the trace id at `run_agent` time and records a root span `hangar.run` (start = run start, end = terminal state, attributes = run id, environment, harness). The docs say an inbound `TRACEPARENT` in `claude -p` makes Claude Code's spans children of the caller's trace, so the interaction tree hangs under the root. Resumed runs (0001 `resume_agent`) get a new trace id and a span link to the parent run's root. Unverified: whether the id survives into the beta trace output in 2.1.296. M0 checks.
- **`OTEL_LOG_TOOL_DETAILS`.** Tool details (command lines, file paths) are what the guardrail and failure-triage tasks need, and 0001 already stores a 200-character redacted summary of the same thing. Default: on, then scrubbed server-side (5.4). `OTEL_LOG_USER_PROMPTS` and `OTEL_LOG_TOOL_CONTENT` stay off. A "debug capture" switch per run turns on tool content for 1 run, stores it for 24 h only, and is audit-logged.
- **Export interval.** Metrics and logs flush on the SDK defaults. For `agent_events` long-polls 0001 already probes every 2 s; OTel does not need to be faster than that. Whether Claude Code exposes `OTEL_METRIC_EXPORT_INTERVAL` / `OTEL_LOGS_EXPORT_INTERVAL` is in the docs; I did not copy their defaults. M0 records real latency from event to receipt.
- **Failure of the sink must not hurt the agent.** OTel SDK exporters drop on failure. If the control plane restarts, some telemetry is lost. That is acceptable. The stream-json log (0001) is the lossless path.

Other harnesses:

| Harness | Source in v1 | Notes |
| --- | --- | --- |
| claude-code | stream-json (0001) + OTLP | Both. |
| pi | `pi-json` parser (`--mode json`), no OTel | Maps `tool_execution_start/end` to `tool.start`/`tool.end`, `turn_end` to `usage`, `agent_end` to `run.result`. Field names must be captured from a real run first. |
| codex | `codex-jsonl` parser (0001 M7) | OTel unknown. |
| antigravity (agy) | `text` only | Unknown until installed. |

A harness without OTel still gets decisions: the decision windows are built from normalized 0001 events (5.5), not from OTel. OTel improves some windows, it is never required.

### 5.3 Data model and storage

**Principle: one event timeline.** 0001's `run_events` holds normalized events. OTel adds columns and event types to it. Spans and raw telemetry go in a separate store.

Changes to 0001's `run_events` (an amendment to 0001 M3, which has not shipped):

```sql
-- 0001:  (run_id, seq, src_offset, ts, type, data), UNIQUE (run_id, src_offset)
ALTER ... ADD COLUMN src TEXT NOT NULL DEFAULT 'log';   -- 'log' | 'otel' | 'hangar'
ALTER ... ADD COLUMN src_key TEXT;                       -- otel: sha1(trace_id|span_id|name|ts_ns); else NULL
CREATE UNIQUE INDEX run_events_src_key ON run_events(run_id, src_key) WHERE src_key IS NOT NULL;
-- src_offset stays NULL for otel rows. SQLite UNIQUE allows many NULLs, so 0001's constraint still holds.
```

`seq` is still assigned per run in arrival order. Clients that poll by `seq` see log-derived and OTel-derived events in one ordered stream, each tagged `src`.

New event types (added to 0001 §5.4's table; no existing type changes meaning):

| `type` | `data` | `src` | Why 0001 lacks it |
| --- | --- | --- | --- |
| `api.request` | `model, duration_ms, ttft_ms?, input_tokens, output_tokens, cache_read_tokens, cost_usd?, attempt, ok, status?` | otel (`claude_code.api_request`) | Latency and retries. |
| `api.error` | `status?, error_class, retryable, attempt, message (200 chars, scrubbed)` | otel (`claude_code.api_error`) | 0001 `error` only covers errors the harness prints to the stream. |
| `tool.decision` | `tool_id?, name, decision ("accept"/"reject"), source (config/user/…)` | otel (`claude_code.tool_decision`) | Permission outcomes. With `--dangerously-skip-permissions` these are all accepts, so low value on the current run command. |
| `decision` | `decision_id, task, label, p_top, mode` | hangar | One row per `DecisionService` result, so cursors see it (5.5). |

**Test results (implemented, diverging from the sketch above; [jobs.md §8](../jobs.md#8-test-results)).** The `test.result` this spec's `failure_triage` reads is not a run event: each run of a check hangar makes is a `TestRun` record (counts, failures with messages, every case's outcome by test id, coverage, commit and tree), and a `progress` event with `data.action: "tests"` and the counts goes into the job's and session's events. In traces it is a `test.run` span event and one `test.case` event per failing or flaky test (the kinds [`hangar.trace/v1`](../api/traces.md) reserves, attributes under `hangar.test.*`), with OpenTelemetry's test semantic conventions alongside (`test.suite.name`, `test.suite.run.status`, `test.case.name`, `test.case.result.status`; development stability); `GET /api/jobs/{id}/tests?format=otel` serves the same events as data.

Enrichment rules, not new rows:

- `claude_code.tool_result` with duration and success enriches the matching `tool.end` (`duration_ms`, `ok`). Matching key: `tool_id` if the OTel event carries the tool-use id (unverified, open question 3), else `(name, start time within 2 s)`. An unmatched OTel tool event is stored as span data only.
- `claude_code.token.usage` / `cost.usage` metrics are folded into `Run.summary.input_tokens/output_tokens/cost_usd` only when the stream-json `usage` event is absent. Stream-json wins on conflict, and a difference over 5% adds a warning `usage_mismatch`. Both are API-equivalent estimates on a subscription (0001 OQ13).
- `claude_code.commit.count` and `claude_code.pull_request.count` go into `run_metrics` only. They are the agent's tool counts, **claims** in the 0002 sense. PR truth comes from GitHub.

**Spans and raw telemetry: `traces.db`** (a second SQLite file in `HANGAR_DATA_DIR`, same WAL setup as `store.py`):

```sql
CREATE TABLE spans (run_id TEXT, trace_id TEXT, span_id TEXT, parent_id TEXT, name TEXT,
                    start_ns INTEGER, end_ns INTEGER, status TEXT, attrs TEXT,   -- scrubbed JSON, <= 2 KiB
                    PRIMARY KEY (run_id, span_id));
CREATE INDEX spans_run ON spans(run_id, start_ns);
CREATE TABLE otel_logs (run_id TEXT, ts_ns INTEGER, name TEXT, attrs TEXT, src_key TEXT,
                        PRIMARY KEY (run_id, src_key));
CREATE TABLE run_metrics (run_id TEXT, name TEXT, attrs_key TEXT, window_start INTEGER,
                          value REAL, PRIMARY KEY (run_id, name, attrs_key, window_start));
CREATE TABLE decisions (...);                -- 5.5
CREATE TABLE decision_labels (...);          -- 5.5
CREATE TABLE decision_spend (day TEXT, backend TEXT, task TEXT, calls INTEGER, tokens INTEGER, usd REAL,
                             PRIMARY KEY (day, backend, task));
```

Why a second file: ingestion writes in bursts. WAL allows one writer. A busy trace writer must not make the main store's `busy_timeout=5000` fire on `put(Sandbox)`. A separate file also lets retention `VACUUM` and a "drop all traces" action run without touching state. Cost: joins across files happen in Python, not SQL. The joins are by `run_id`, so that is cheap.

Writer: one asyncio task drains a bounded queue (10,000 items), batches by 200 or 250 ms, one transaction per batch. When the queue is full, OTLP requests get `429` with `Retry-After`, and the SDK retries or drops. Counters `otlp_dropped_total` and `otlp_queue_depth` are on `/metrics` if 0001 adds one; otherwise in `get_telemetry_summary`.

**What goes where**

| Data | Store | Kept | Why |
| --- | --- | --- | --- |
| Normalized events (log + otel-derived + decisions) | `run_events` in `hangar.db` | 0001 default: 14 days / 20,000 per run | Spine. Small rows. |
| Run summary, metrics rollup, decision rows, labels | `hangar.db` / `traces.db` | Until the sandbox record is purged; decisions and labels 180 days | These are the evaluation set. They are small. |
| Spans, OTel logs | `traces.db` | 14 days, 50,000 spans per run | Used for debugging and timelines. Disposable. |
| Raw OTLP payloads | Not stored | — | Re-derivable data only matters if the mapper has a bug. M0 keeps fixtures. |
| Long-term, high-volume, cross-run analytics | External OTLP backend, optional | Owner's policy | Not a hangar concern. |

Volume estimate, to be replaced by measurement in M0: a run with 300 tool calls yields roughly 900 spans and 2,000 log records. At about 0.5 KiB each after scrubbing that is on the order of 1.5 MiB per run. 100 runs a day for 14 days is about 2 GiB. The PVC is 20 GiB (`deploy/k8s/deployment.yaml`). That fits, but the estimate is a guess, so a size cap `HANGAR_TRACE_MAX_MB` (default 2048) deletes oldest-first.

**External backend.** `HANGAR_OTLP_FORWARD_ENDPOINT` (+ `_HEADERS`) makes the collector re-export the *already scrubbed* spans, logs and metrics over OTLP, with hangar resource attributes. Targets that speak OTLP (Tempo/Jaeger, Grafana Cloud, Honeycomb, Langfuse, Phoenix, ClickHouse-based stores) should work. I have not tested any of them. Forwarding is off by default, is egress from the control plane to a third party, and is listed in 6.2. Hangar's own dashboard never depends on it.

### 5.4 Redaction and PII

Today `broker.redact` replaces only the literal values of vault secrets (length 8 or more). It does not catch a token the agent found in a file, a `.env` dump, or a key printed by `env`. Telemetry and decision windows need more.

Pipeline, applied at ingest before anything is stored, forwarded or sent to a decision backend:

1. **Attribute allowlist.** Per signal name, a fixed list of attribute keys is kept. Unknown keys are dropped. This is the main control: new SDK attributes do not leak by default.
2. **Vault scrub.** `broker.redact` on every string.
3. **Pattern scrub** (new, `hangar/scrub.py`): `sk-ant-…`, `ghp_`/`github_pat_`/`gho_…`, `hgs_`/`hgt_`/`hgr_` (hangar's own), AWS `AKIA…`, Google API keys (`AIza…`), PEM blocks, JWT-shaped strings, `Authorization:` header values, URL userinfo (`https://user:pass@`), and `KEY=value` / `"key": "value"` where the key name contains `token|secret|password|passwd|api_key|credential` — replaced with `***`.
4. **Truncate.** Strings to 2 KiB in attributes. Command lines to 500 characters.
5. **Path normalization.** Absolute paths under the sandbox home or repo become `~/…` or `<repo>/…`. This also makes decision windows cacheable across sandboxes.

Limits, stated plainly: pattern scrubbing is best effort. An unrecognized secret format passes through. That is why the defaults avoid content at all (prompts and tool content off at the source; allowlist), why decision windows are built from summaries (5.5), and why `features_only` is the default egress profile for decisions (6.1). A test corpus of real-looking secrets (M1) pins the patterns. A scrubber miss is a defect with a regression test, not an accepted loss.

Personal data: prompts may contain names and emails. `user_prompt` content is off by default, so mostly absent. Email-like strings are masked in the pattern scrub. Account identifiers that Claude Code attaches (user/org ids) are dropped by the allowlist.

### 5.5 The decision model

#### Interface and types (`hangar/decisions/`)

```python
class Option(BaseModel):
    id: str                       # "stuck"
    description: str              # one line, goes to the model
class WindowSpec(BaseModel):
    source: Literal["events", "spans", "result", "task"]
    max_events: int = 40; max_tokens: int = 3000     # hard cap; the 32K context is not a target
    include: list[str] = []       # event types
    profile: Literal["features_only", "redacted_text"] = "features_only"
class Action(BaseModel):
    kind: Literal["none", "annotate", "notify", "suggest"]     # no "cancel", "merge", "push" in v1
    target: str | None = None
class DecisionTask(BaseModel):
    id: str; version: int
    question: str
    options: list[Option]         # includes an explicit "unclear" option where it makes sense
    window: WindowSpec
    threshold: dict[str, float]   # per-option min probability to count as "actionable"
    min_margin: float = 0.2       # top1 - top2; below this the result abstains
    action: Action
    mode: Literal["off", "shadow", "advisory", "acting"] = "shadow"
    ttl_s: int                    # cache lifetime
    trigger: list[str]            # when it is evaluated (below)

class DecisionResult(BaseModel):
    decision_id: str; run_id: str; task: str; task_version: int
    label: str | None             # None when abstaining or unavailable
    probabilities: dict[str, float]
    abstain: bool; actionable: bool
    source: Literal["model", "cache", "rules_fallback"]
    model: dict                   # {backend, name, revision?}
    decided_at: float; latency_ms: int; cost_usd: float
    input: dict                   # {from_seq, to_seq, sha256, tokens}

class DecisionBackend(Protocol):
    name: str
    async def score(self, task: DecisionTask, text: str, *, timeout_s: float) -> dict[str, float]: ...

class DecisionService:
    async def decide(self, run_id: str, task_id: str, *, max_age_s: int | None = None) -> DecisionResult: ...
```

`score` returns one probability per option. Backends:

| Backend | What it is | Egress | Cost | Notes |
| --- | --- | --- | --- | --- |
| `rules` | Deterministic heuristics over events and summary (no events for N min, same tool+args repeated k times, last error class). Always on. | none | 0 | Baseline every other backend must beat (8.3). Also the failure fallback. |
| `decision1` | Microsoft Decision-1 through Foundry | yes | Reported $0.042 per 1M input tokens, output free | Request/response shape and options-passing convention **unknown** (OQ 6). The adapter is the only code that knows it. |
| `openrouter` | Same model as `microsoft/microsoft-decision-1` | yes | Announced; price unverified | Alternate route. Useful if Foundry setup is heavy. |
| `local` | A small open model run locally, scored by comparing next-token log-probabilities of the option labels. | none | Compute only | Which model and runtime: OQ 7. CPU latency on the control-plane pod is likely seconds. Decision-1 is not available locally. |
| `llm` | A cheap hosted LLM with constrained output (an enum field, or logprobs) | yes | Per token, API key | Needs an API key, not the Pro subscription. Probabilities are not calibrated by training; they come from logprobs or sampling and must be calibrated by us. |

Selection: `HANGAR_DECISION_BACKENDS='["decision1","local"]'` is an ordered list; first healthy one wins; `rules` is always last. `HANGAR_DECISION_EGRESS=none` removes every backend that leaves the cluster.

#### The six decision tasks

Each task is data in `hangar/decisions/tasks/*.yaml`, versioned. Changing a question, an option or a window bumps `version`. Calibration and labels are per version.

| Task id | Question | Options | Window | When evaluated | Action (max) |
| --- | --- | --- | --- | --- | --- |
| `run_state` | What is this running agent doing right now? | `progressing`, `stuck`, `looping`, `waiting_input`, `blocked_env`, `done`, `unclear` | Last 40 events as one-line features + seconds since last event + counters (edits, tests, errors in last 10 min) | A live run: no event for 3 min while the process is alive; the same `tool.start` summary repeated 3 times; every 5 min otherwise; on `error` events | `annotate` the run; `notify` after 2 consecutive `stuck`/`looping` above threshold |
| `failure_triage` | Why did this run fail or this test fail? | `test_failure`, `env_dependency`, `auth`, `rate_limit`, `harness_bug`, `task_unclear`, `other` | Last `error`/`api.error` events, last failing `test.result` and a 1.5 KiB scrubbed output tail | Run enters `failed`/`crashed`/`timed_out`, or a failed `verification` (0002) | `annotate`; feed the environment-fix aggregator (5.8) |
| `tool_risk` | Is this tool call risky? | `routine`, `sensitive`, `destructive`, `exfiltration` | One `tool.start` (name, scrubbed command, path) + previous 3 for context | Post hoc, asynchronously, on each `Bash` and write tool start | `annotate`; `notify` on `destructive`/`exfiltration`. **Not in the execution path.** |
| `pr_readiness` | Is this change ready for human review? | rating `1`–`5` (one call per criterion: tests cover the change, scope matches the task, no unrelated files, description matches diff) | Diff stat + file list, `Verification`, `checks` rollup, PR title/body. No diff text. | When 0002 `stage` reaches `done` | `annotate` on the land request page |
| `route_task` | What kind of task is this? (the label then selects from outcome statistics, 5.8) | `bugfix`, `feature`, `refactor`, `test`, `docs`, `dependency`, `unknown` | Task prompt (first 1 KiB, scrubbed) | At `run_agent` | `suggest` an environment/harness/model |
| `needs_human` | Does this run need a person? | `yes`, `no` | Last assistant message, any `input.requested` event, last 10 events | State is `waiting_input`, or the run ended with a question as its last message | `notify` |

Design choices worth stating:

- **`run_state` excludes "dead".** Dead and exited are 0001's deterministic states. The model is consulted only when 0001 says the process is alive. Rules decide first: if 0001 says `crashed`, no call is made.
- **`failure_triage` is rules first.** `api.error` with status 401/403 → `auth`; 429 or a `rate_limit` event → `rate_limit`. The model is called only if no rule fires. A rule match is stored as a decision with `source="rules_fallback"` so labels line up.
- **`tool_risk` is advisory and post hoc.** A synchronous pre-tool check would need a Claude Code hook inside the sandbox calling hangar on every tool call. At ~0.5 s a call that is slow, makes agent progress depend on the service, and is a bypassable control anyway. 0003's grants, branch protection and egress limits enforce. This task finds patterns to feed back into those policies. A pre-execution variant is a later question (OQ 10).
- **`route_task` classifies, statistics choose.** Decision-1 is not trained on hangar's outcomes. It is good for "what type of task is this". The choice of environment/harness/model comes from outcome data keyed by task type (5.8). This also keeps routing explainable.
- **`pr_readiness` never gates a merge.** Merge needs a human approval (0002). The score is a hint on the approval page. No diff text goes out.
- **An explicit `unclear` option** where it makes sense, plus an abstain rule (`min_margin`). Scoring a fixed set of options forces a distribution even when the input fits none. Whether Decision-1 includes any "none of the above" behaviour is unknown (OQ 6).

#### Windows, features and caching

A window is built from `run_events` (and spans if needed) by a pure function. Same events → same text. Example `run_state` window (`features_only` profile):

```
task: run_state v1
elapsed_s: 1840  since_last_event_s: 212  turns: 31
last_10min: edits=0 tests_run=4 tests_failed=4 errors=2 api_errors=0
recent (oldest first):
 [-212s] tool Bash "uv run pytest -q tests/test_sync.py" -> failed (exit 1) tests: 0 passed 1 failed
 [-340s] tool Edit "src/sync.py"
 [-402s] tool Bash "uv run pytest -q tests/test_sync.py" -> failed (exit 1) tests: 0 passed 1 failed
 ...
```

It contains tool names, scrubbed 200-character summaries, test counts and timings. It does not contain file contents or assistant prose. `redacted_text` additionally includes the last assistant message (500 chars) and output tails. It is opt-in per task and per environment.

Cache: key = `sha256(task_id, version, backend, window_text)`. The window text drops absolute timestamps in favour of relative offsets rounded to 10 s, so an unchanged run hits the cache. TTL per task (`run_state`: 60 s). A cache hit is returned with `source="cache"` and costs nothing. The service also de-duplicates concurrent calls for the same key.

#### Advisory first: shadow, log, compare, promote

Modes per task: `off` → `shadow` (computed and logged, shown nowhere except the Decisions page) → `advisory` (shown to humans: badges, notifications) → `acting` (may trigger the task's one allowed action). Default for every task is `shadow`. `acting` exists in the schema; no task ships there in this spec. Promotion is a manual setting after the criteria below are met, and is audit-logged.

Ground truth, collected automatically wherever possible:

| Task | Label sources (arrive later) |
| --- | --- |
| `run_state` | Hindsight rule: window labelled `stuck`/`looping` if no edit, commit or test-pass followed for the next 10 min and the run ended `cancelled`/`timed_out`; `progressing` if a file change or passing test followed; `done` if the run ended within 2 min. Plus human labels from the dashboard ("was this right?"). Ambiguous windows are left unlabelled. |
| `failure_triage` | Human label on the run page; for `env_dependency`, a later fix to the environment that made the same signature stop. 0002 verification output tail classified by rules as a cross-check. |
| `tool_risk` | Human review of flagged calls (1 click); 0003 egress/gateway denials in the same run as a positive signal for `exfiltration`. |
| `pr_readiness` | 0002 outcome: approved/rejected, PR merged or closed unmerged, CI conclusion, verification pass/fail. |
| `route_task` | Human label on a sample; later the outcome data itself. |
| `needs_human` | Did a human message or approval follow within 30 min? Did the run resume after input? |

Every `DecisionResult` is a row in `decisions`. Every label is a row in `decision_labels(decision_id, label, source, at, by)`. The join is what `8.3` evaluates.

Promotion criteria for `shadow → advisory` (per task, per version): ≥ 150 labelled decisions from ≥ 30 distinct runs; macro-F1 beats the `rules` baseline by ≥ 0.05 on a held-out split **by run**, not by window; expected calibration error ≤ 0.08. For any option that will trigger `notify`: precision at the configured threshold ≥ 0.90 with the lower end of the 95% Wilson interval ≥ 0.80. Numbers are starting values to tune once real data exists, and I would rather they be too strict than loose. For `acting` (not in this spec): precision lower bound ≥ 0.95 and a human-reviewable undo.

Demotion: a rolling window of the last 50 labelled decisions drops below the bar → the task falls back to `shadow` and the owner is notified.

#### Calibration

Decision-1's probabilities are described as calibrated. Independent evaluation is thin, so we check on our own data and do not assume it.

- Reliability table: bin probabilities into 5 bins (not 10: the sample is small), report observed frequency and counts per bin, ECE, Brier score. Per task, per option for the top-1.
- If ECE > 0.08 with ≥ 200 labels, fit a per-task temperature (one parameter) on a training split. Apply it in `DecisionService`. Store the calibrator version in the result's `model` field. Platt/isotonic only if data is plentiful; with a few hundred labels isotonic overfits.
- Thresholds are chosen on the precision target above, not on the 0.5 default.
- Drift: backend/model revision changes reset `acting`/`advisory` back to `shadow` until re-checked. The service records the model revision string it receives, if any (OQ 6).
- Per-class counts are shown with intervals. A class with fewer than 20 labels is shown as "not enough data", not as an accuracy number.

#### Cost, latency, privacy and failures

**Cost.** Pricing as reported: $0.042 per 1M input tokens, output free. A `run_state` window of 3,000 tokens is about $0.000126. A 2-hour run evaluated every 5 minutes plus triggers, say 40 calls, is about 120,000 tokens, or $0.005. Six tasks on a typical run: under $0.02. 100 runs a day: a couple of dollars a month at most. That arithmetic is from the reported price. M4 measures the real bill, including whether Foundry charges anything beyond tokens (deployment or provisioned capacity: OQ 6).

Guardrails, because "small and visible" is a requirement:

- `HANGAR_DECISION_RUN_BUDGET_USD` (default 0.05) and `HANGAR_DECISION_DAILY_BUDGET_USD` (default 1.00). When exceeded: backend calls stop, `rules` continue, and the result says `source="rules_fallback"`, with a banner.
- Token counts are estimated before the call (characters / 3.5, conservative) and recorded exactly after if the API reports them.
- Spend is in `decision_spend`, shown on the Insights page and in `get_telemetry_summary`. A per-backend kill switch: `HANGAR_DECISION_BACKENDS=[]`.
- Decision calls use API keys held in the vault (`harness="decision"` credential kind), never the Pro subscription, so they do not consume the subscription's plan limit.
- Telemetry itself costs no model spend.

**Latency budget.** An independent test measured about 459 ms median through Foundry. Vendor claims are lower. Budget: p50 < 1 s, hard timeout 3 s per call, no decision on any request path that a human or the supervisor blocks on. `classify_run` returns the cached result immediately if it is fresher than `max_age_s` and otherwise calls with the timeout (5.7). Call volume is low (a few per run-minute at most), so no batching in v1.

**Privacy.** What leaves the cluster, per profile (full discussion in 6.1): the task's question and option text (static), and the window text after scrubbing. Not sent: credentials, vault values, run prompts (unless `route_task` is enabled with `redacted_text`), diff text, file contents, repository URL, sandbox/run ids. The `input.sha256` is stored locally; the window text is stored locally for evaluation (scrubbed, 90 days, can be disabled — then labels can't be re-scored offline). `HANGAR_DECISION_EGRESS=none` is the no-egress mode: only `rules` and `local`.

**Failure behaviour.** Service down, slow or erroring:

1. Timeout or 5xx → one retry only if the remaining budget allows; otherwise next backend in the list; then `rules`.
2. Circuit breaker per backend: 5 failures in 60 s opens it for 120 s. State is in memory.
3. The result is still a `DecisionResult`, with `source="rules_fallback"`, `model.backend="rules"`, and `label` from the rules (or `null` if rules abstain). Callers never get an exception from `classify_run` for a backend problem.
4. 0004 and 0001 behave as before when every decision is `null`. Decisions can only add warnings. A test pins that the supervisor's liveness path does not import or await the decision service.
5. Malformed or unparsable backend output (probabilities missing, not summing near 1) is treated as a failure, logged, and not cached.

### 5.6 Collector deployment per provider

The endpoint string `HANGAR_OTLP_ENDPOINT` is chosen per sandbox at claim time and passed through the run script env. It always ends in `/otlp`; exporters append `/v1/traces` etc.

| Provider | Endpoint the sandbox uses | Reachability under today's network | Under 0003 default-deny |
| --- | --- | --- | --- |
| kubernetes (GKE Agent Sandbox) | `http://hangar-gateway.hangar.svc:8788/otlp` (in-cluster Service) | Egress is open today and DNS works. | 0003's NetworkPolicy allows the gateway Service on 8788. The OTLP route is on that listener, so allowed. 0003 puts the gateway host in `NO_PROXY`, so the OTLP exporter bypasses the egress proxy. The exporter's proxy behaviour is unverified (OQ 4); setting the in-cluster host in `NO_PROXY` is the safe route. DNS is not allowed under 0003 ("the proxy resolves names"), so the Service must be reached by ClusterIP or 0003 must inject the gateway address. Same dependency as the git gateway; whatever 0003 decides applies here. |
| docker | `http://host.docker.internal:8788/otlp` (Docker Desktop/OrbStack) or the bridge gateway IP on Linux | Works with the default network. | 0003's per-policy internal network publishes the alias `hangar-gateway` that forwards to the host's 8788. Same route. |
| e2b | `https://<public-gateway-host>/otlp` | Needs a publicly reachable hostname. 0003 already requires the gateway to be internet-reachable for E2B. | Auth is the ingest key, so no change. E2B egress is advisory today (0003 5.5), so telemetry reachability is not a new constraint. |

If the endpoint is unreachable the SDK drops data and the run is unaffected. `get_telemetry_summary` reports `otlp_last_received_at` per run, and a run with a `log`-source stream but no OTel for 5 minutes gets a warning `telemetry_silent`. That doubles as the check for open question 1 (does the disable-nonessential-traffic flag block export).

Control plane changes:

- `deploy/k8s`: add port 8788 to the Service (shared with 0003), no new Deployment. Request limits: ingestion CPU is small; bump `requests.memory` by 256Mi for the queue and protobuf buffers (estimate).
- `Settings`: `otlp_enabled` (default false until M2 is verified), `otlp_endpoint_override`, `trace_retention_days`, `trace_max_mb`, `decision_*` as above.
- Request limits on the OTLP route: body ≤ 4 MiB, 20 requests/s per run key, gzip accepted. Over the limit gets 429 or 413.

### 5.7 MCP tools and REST

Four new MCP tools. All are read-mostly. Scope tags follow 0001: `runs:read`, `runs:write`.

```python
classify_run(run_id: str, task: str = "run_state", max_age_s: int = 60,
             allow_stale: bool = True) -> dict                       # runs:read
get_run_trace(run_id: str, after_ns: int = 0, limit: int = 500,
              name: str | None = None, include_attrs: bool = False) -> dict   # runs:read
get_telemetry_summary(run_id: str | None = None) -> dict             # runs:read
record_decision_label(decision_id: str, label: str, note: str | None = None) -> dict   # runs:write
```

Events are read with 0001's `agent_events`, extended with an optional `sources: list[str] | None` filter (`["log","otel","hangar"]`) and the new event types. No new event tool.

**`classify_run` example.**

```json
{"decision_id": "dec-9c41e07a", "run_id": "run-3fa91c20", "task": "run_state", "task_version": 1,
 "label": "looping",
 "probabilities": {"progressing": 0.04, "stuck": 0.12, "looping": 0.78, "waiting_input": 0.01,
                   "blocked_env": 0.03, "done": 0.00, "unclear": 0.02},
 "abstain": false, "actionable": false, "mode": "shadow",
 "source": "model", "model": {"backend": "decision1", "name": "microsoft-decision-1", "calibrator": null},
 "decided_at": 1791766412.8, "latency_ms": 512, "cost_usd": 0.00013,
 "input": {"from_seq": 112, "to_seq": 151, "sha256": "4be1…", "tokens": 3100},
 "evidence": [{"seq": 142, "type": "tool.start", "summary": "uv run pytest -q tests/test_sync.py"},
              {"seq": 148, "type": "tool.start", "summary": "uv run pytest -q tests/test_sync.py"}]}
```

`actionable` is true only when the task is in `advisory` or `acting` mode, the label is not an abstain, and the label's probability is at or above its configured threshold. 0004 reads this flag and does not re-implement the policy. `evidence` lists the events the rules or the window highlighted; for the model path it is the repeated-key heuristic's output, since the model gives no rationale.

Fallback example (service down):

```json
{"decision_id": "dec-9c41e07b", "run_id": "run-3fa91c20", "task": "run_state", "task_version": 1,
 "label": "unclear", "probabilities": {"unclear": 1.0}, "abstain": true, "actionable": false,
 "mode": "shadow", "source": "rules_fallback",
 "model": {"backend": "rules", "name": "run_state_rules", "calibrator": null},
 "decided_at": 1791766420.1, "latency_ms": 3004, "cost_usd": 0,
 "warnings": ["decision1: timeout after 3.0s", "circuit open until 1791766540"]}
```

**`get_run_trace`**

```json
{"run_id": "run-3fa91c20", "trace_id": "5b8efff798038103d269b633813fc60c", "next_after_ns": 1791766013000000000,
 "spans": [
  {"span_id": "00f067aa0ba902b7", "parent_id": null, "name": "hangar.run",
   "start_ns": 1791765890100000000, "end_ns": null, "status": "unset"},
  {"span_id": "a1b2c3d4e5f60718", "parent_id": "00f067aa0ba902b7", "name": "claude_code.interaction",
   "start_ns": 1791765891000000000, "end_ns": 1791766012000000000, "status": "ok"},
  {"span_id": "b2c3d4e5f6071829", "parent_id": "a1b2c3d4e5f60718", "name": "claude_code.tool",
   "start_ns": 1791765990000000000, "end_ns": 1791765996000000000, "status": "ok",
   "attrs": {"tool_name": "Bash"}}],
 "more": false}
```

Attribute names inside spans are placeholders until M0 captures real payloads.

**`get_telemetry_summary`**

```json
{"run_id": "run-3fa91c20", "otlp_last_received_at": 1791766010.2, "telemetry": "ok",
 "api": {"requests": 34, "errors": 2, "retries": 3, "p50_ms": 4100, "p95_ms": 11800},
 "tokens": {"input": 51200, "output": 6100, "cache_read": 40100}, "cost_usd_estimate": 0.84,
 "agent_counts": {"commits": 1, "pull_requests": 0},
 "decision_spend": {"run_usd": 0.0021, "day_usd": 0.12, "day_budget_usd": 1.0}}
```

With no `run_id` it returns fleet-level OTLP health (queue depth, drops, last receipt per run) and decision spend.

**REST** (bearer or UI ticket, as in `app.py`):

| Route | Notes |
| --- | --- |
| `POST /otlp/v1/{traces,metrics,logs}` | Gateway listener (and :8787 before 0003 M2). Auth: run ingest key. `Content-Type: application/x-protobuf`, gzip ok. Returns OTLP `ExportXxxServiceResponse`; `429` when the queue is full. |
| `POST /api/runs/{id}/classify` | Body `{task, max_age_s, allow_stale}`. Same as `classify_run`. |
| `GET /api/runs/{id}/decisions?task=&after=` | History, newest last. |
| `GET /api/runs/{id}/trace?after_ns=&limit=&name=` | Spans. |
| `GET /api/runs/{id}/telemetry` | Summary. |
| `POST /api/decisions/{id}/label` | Body `{label, note}`. |
| `GET /api/decision-tasks` / `PATCH /api/decision-tasks/{id}` | List tasks with mode and metrics; admin can change `mode` and thresholds. |
| `GET /api/decisions/eval?task=&version=` | Calibration table, confusion matrix, comparison with `rules`, spend. |
| `GET /api/insights/{summary,failures,environments,routing}` | Optimization views (5.8). |
| `POST /api/evals`, `GET /api/evals/{id}` | Paired evaluations (5.8). |

#### Interface for 0004

0004 needs three things. They are the whole contract; anything else in this spec may change.

```python
# Python, in-process (hangar.decisions.api)
async def classify_run(run_id: str, task: str = "run_state", *, max_age_s: int = 60) -> DecisionResult
def query_events(run_id: str, after: int = 0, limit: int = 200, types: list[str] | None = None,
                 sources: list[str] | None = None) -> EventPage         # = 0001 agent_events
def query_spans(run_id: str, after_ns: int = 0, limit: int = 500, name: str | None = None) -> SpanPage
```

The same three are exposed as `classify_run`, `agent_events`, `get_run_trace`. Guarantees:

- `classify_run` never raises for a backend problem. It returns a result with `source: "rules_fallback"` or `label: null`.
- `classify_run` is safe to call at the supervisor's tick rate: cached results return in under 5 ms (in-memory), fresh calls take up to the 3 s timeout.
- Each result also appends a `decision` event to `run_events`, so a supervisor following the 0001 cursor sees decisions in order.
- Labels and probability keys are the task's option ids, stable within a `task_version`. A version bump is visible in the result.
- 0004 must treat `actionable: false` as "do not act". It must not infer an action from a label alone.

### 5.8 Optimization loop

```
 runs ─► events + spans ─► decisions ─┐
  │                                   ├─► insights (SQL views) ─► dashboard
  └─► 0002 outcomes (verified,        │        │
      approved, merged) ──────────────┘        ├─► env-fix suggestions  ─► human reviews spec diff
                                               ├─► instruction suggestions ─► paired eval ─► adopt
                                               └─► routing stats ─► recommend config
```

**Metrics (computed from stored data, no model calls).**

| Metric | Source |
| --- | --- |
| Cost per task: API-equivalent USD (`summary.cost_usd`), tokens, plan % consumed (0001 usage), decision spend | 0001 usage events, OTel metrics |
| Time to first edit, time to first test, time to green | 0001 `time_to_first_test_s`, events |
| Verified resolution rate (0002 `Verification.passed`) per environment/harness/model/task type, with Wilson interval | 0002 |
| Agent-claimed success vs verified success (the Jarmak gap) | `run.result.ok` vs `Verification.passed` |
| Failure classes per environment and harness | `failure_triage` decisions + rules |
| API error and retry rate, p95 API latency | `api.request`, `api.error` |
| Tool failure rate by tool; repeated-command rate | `tool.end`, windows |
| Queue wait, time in `waiting_input` | 0001 |

The headline metric is the verified resolution rate. "Agent said done" is shown but is never an optimization target.

**Environment fixes.** A nightly job (and on demand) groups failures by a normalized signature:

- Rules (free): `ModuleNotFoundError: No module named 'x'`, `command not found`, `error: Unable to find` style messages from package managers, and agents running `pip install` / `npm i` / `apt-get install` at run time (seen in `tool.start`). The last one is a strong signal that a dependency is missing from the image.
- Model (`failure_triage = env_dependency`): used to catch cases rules miss. Its output only affects grouping, not what gets proposed.
- A suggestion appears when the same signature occurs in ≥ 3 runs on one environment within 14 days. It shows the evidence runs and a proposed YAML change (`dependencies.pip: [x]`, or `repo.setup`) as a diff against the environment spec, with the expected effect ("would have avoided ~N minutes in M runs"). A human applies it by hand or with one click that opens a PR against the env spec repo path. Hangar never rewrites a spec on its own. After the change ships, the same signature is tracked and the suggestion is marked "confirmed" or "not effective".

**Prompt and instruction improvements.** Aggregate patterns, not single runs: repeated `waiting_input` on the same question, the same wrong command (`pytest` instead of `make test`) before the right one, repeated discovery of repo layout. Output is a suggested line for `agent_config.instructions` with its evidence. Adoption is by evaluation (below), not by reading the suggestion.

**Routing.** Outcome table keyed `(task_type, environment, harness, model)`. Task type comes from `route_task` (or a human label). For each cell: n, verified-pass count, mean cost, mean time. Recommendation only when the cell has ≥ 10 runs and the posterior probability that it beats the current choice is ≥ 0.9 (Beta-binomial, uniform prior). It is a *recommendation* in `run_agent` warnings and on the dashboard, never an automatic switch. Exploration (trying a non-incumbent config) happens only inside an explicit evaluation, because with one Pro subscription every exploratory run uses plan quota.

**Evaluation done properly.** `POST /api/evals` takes two configs (A, B), a task set, and repeats:

```json
{"name": "instructions-v2", "a": {"environment": "claude-python"},
 "b": {"environment": "claude-python", "instructions_patch": "…"},
 "tasks": ["task-01", "…"], "repeats": 3, "credential": "claude-max",
 "metric": "verified_resolved", "holdout": ["task-21", "…"]}
```

Rules the tool enforces:

1. **Outcome is external.** `Verification.passed` from a clean sandbox (0002), never the agent's claim.
2. **Paired.** The same task at the same `base_sha` runs under A and B, with order randomized. The analysis is per task: difference in pass rate across repeats, then a paired bootstrap over tasks. Report effect size, 95% interval, and n. No p-value alone.
3. **Repeats.** Default 3 per cell, minimum 2. Single-run comparisons are rejected. Jarmak's 2–6 point run-to-run spread is the reason; our own repeat variance is measured and shown.
4. **Config fingerprint.** Every run stores `environment image tag, harness version, model, instructions hash, hangar version` in `Run.config_fingerprint` (new field, small addition to 0001's `Run`). Runs with different fingerprints are never pooled.
5. **Holdout.** Suggestions are developed on one task set and confirmed on another. The report marks results on tuning tasks as exploratory.
6. **Power, in plain terms.** If the per-task difference has standard deviation s (assumed 0.3 here; measured after the first eval), the smallest effect detectable with ~80% power at 5% is about 2.8 × s / √n tasks. With 30 tasks that is about 15 points. Detecting 5 points would need about 280 tasks. The tool prints this for the chosen design before running and says "this design cannot detect effects under X". The 0.3 is a guess and should be replaced with a measured value.
7. **Cost gate.** Before starting, the tool shows runs × expected tokens and expected plan % (from 0001 usage, once 91%-type data exists), and requires confirmation. Evals are queued behind normal work by the 0001 credential queue. On one Pro plan, 30 tasks × 3 repeats × 2 configs is 180 runs, which will not fit in one plan window. The honest options are fewer or cheaper tasks, or an API-key credential with a stated dollar cap. The design does not hide this.
8. Evals that touch routing or instructions never auto-apply.

Decision tasks get the same discipline: their evaluation is 8.3, with run-level splits and intervals.

**External tools, for the "other services" part of the brief.** None is a dependency.
- An OTLP-compatible backend, if the owner runs one, receives forwarded telemetry (5.3).
- Notifier hooks from 0002 carry `needs_human` and `stuck` alerts.
- Existing LLM-eval or tracing products (Langfuse, Phoenix and similar) accept OTLP, to my understanding; unverified (OQ 11). The offline eval harness in 8.3 is plain Python and runs without them.

### 5.9 Dashboard UX

Extends the combined dashboard 0001 M8 and 0002 M9 will design. It adds:

**Run page → Trace tab.**

```
 run-3fa91c20  claude-code  claude-python   RUNNING  31 turns  $0.84 est  tokens 51k/6k
 state (model, shadow):  ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇ progressing ░░ stuck? ▇▇ looping 78%        [was this right? ✓ ✗]
 ──────────────────────────────────────────────────────────────────────────────
 interaction ─┬ api   ████ 4.1s
              ├ tool  Bash uv run pytest …  ██ 6.0s   ✗ exit 1
              ├ api   ███ 3.2s  retry×2 (529)
              └ tool  Edit src/sync.py  ▏
 ──────────────────────────────────────────────────────────────────────────────
 telemetry: ok (last 4 s ago)   api p95 11.8s   errors 2   retries 3
```

State strip: decisions over time as colored bands with the probability shown on hover. A "Show raw telemetry" toggle is admin-only. When `telemetry_silent`, the tab says so and falls back to the 0001 event list.

**Fleet page.** Run cards show 0001's state first. A model-derived badge appears only in `advisory` mode and is labelled "suggested" with the probability ("looping? 78%"). Shadow-mode results never appear on the fleet page.

**Decisions page** (admin). One row per task: mode, version, labelled count, macro-F1 vs rules, ECE, reliability plot, confusion matrix, precision at threshold, spend today, backend health and circuit state. A mode switch with the promotion criteria shown beside it and greyed out if unmet (an admin can still override, audit-logged).

**Insights page.** Tiles: verified resolution rate (with interval), claimed vs verified gap, median cost per task, median time to first test, decision spend this week. Tables: failure classes by environment × harness; suggested environment fixes with evidence and the diff; routing recommendations with n and posterior; evals with effect size and interval. Charts follow the dataviz conventions the dashboard adopts; the choice of chart library is not part of this spec.

## 6. Security, privacy and failure modes

### 6.1 Data leaving the cluster

| Destination | When | What | Default |
| --- | --- | --- | --- |
| Foundry / OpenRouter (Decision-1) | Decision calls | Static question and option text; scrubbed window text per profile | Off until the owner sets a key and picks the backend |
| Hosted LLM (`llm` backend) | Same | Same | Off |
| External OTLP backend | Forwarding | Scrubbed spans, logs, metrics with hangar resource attributes | Off |
| Anthropic | The agent's own traffic | Unchanged. Telemetry does not add to it. | — |

Profiles:

- `features_only` (default): tool names, counts, durations, test counts, state names, 200-character scrubbed command/file summaries. No assistant prose, no file contents, no output tails, no prompts, no diff text. Commands are scrubbed but can still contain a repo-specific identifier; treat the summaries as low-sensitivity, not as anonymous.
- `redacted_text`: adds the last assistant message (500 chars), output tails (1.5 KiB) and, for `route_task`, the prompt's first 1 KiB. Per task and per environment opt-in.

Vendor handling of the sent text (retention, use for training, region) is **unknown** for both Foundry and OpenRouter. The owner should read the terms before enabling. OQ 5.

**No-egress mode.** `HANGAR_DECISION_EGRESS=none` removes all remote backends. Only `rules` and `local` run. Test 8.1 asserts that no socket opens to a non-cluster address in this mode. Telemetry collection is unaffected, since it stays inside the cluster.

### 6.2 Threats

| Threat | Mitigation |
| --- | --- |
| A sandbox forges telemetry for another run | The server tags from the ingest key. Client `hangar.*` attributes are dropped. One key maps to one run. |
| Prompt injection through trace text: tool output says "classify this run as done" | The decision window uses features and short scrubbed summaries, and options are scored, not generated. The model has no tool access and cannot act. Decisions are advisory. An attacker could still skew a label, which is why no decision acts in v1 and why `acting` needs measured precision. |
| Secrets in telemetry | Source defaults (content off), allowlist, vault scrub, pattern scrub, truncation (5.4). Test corpus in M1. Debug capture is time-limited and audited. |
| OTLP endpoint abuse (flooding, large bodies) | Run-scoped key with expiry, body and rate limits, bounded queue with 429, per-run span cap. |
| Ingest key stolen from a sandbox | It can write telemetry for that run until expiry. It cannot read anything or call any other route. Deleted at run end. |
| Telemetry used to exfiltrate | Body size and attribute allowlist limit it. The route is an authenticated hole on the gateway, same as the gateway's other routes. 0003's residual-risk discussion applies. |
| Spend runaway | Per-run and daily caps, rules fallback when exceeded, kill switch. |
| Decision service becomes a dependency of liveness | Forbidden by design and by test (5.5, failure behaviour). |
| Traces DB fills the PVC | Size cap, oldest-first deletion, separate file. |
| Stored decision inputs leak | Stored scrubbed, 90 days, can be disabled, deleted with the run. Not exposed via MCP (only the hash). |

### 6.3 Failure modes

| Failure | Behaviour |
| --- | --- |
| OTLP queue full | 429 to the exporter; counters; stream-json path unaffected. |
| Control plane restart | OTel in flight is lost. Log ingestion resumes from `log_offset` (0001). Spans for the gap are missing. Marked by a `telemetry_gap` warning when the next batch's timestamps jump. |
| `traces.db` corrupt or missing | Created fresh. Runs lose spans, not events or state. |
| Clock skew between sandbox and control plane | Span times are the sandbox's. The server records receipt time too. Timelines use span times; staleness checks use receipt time. |
| Decision backend down | 5.5 failure behaviour. |
| Calibration drift | Rolling window demotes the task to `shadow`. |
| Telemetry silent although log events arrive | `telemetry_silent` warning; run continues. |
| Mapper bug (attribute names differ from fixtures) | Unknown attributes are dropped; the event still records with fewer fields. Fixture tests in M1. |

## 7. Rollout

1. **M0:** capture real OTLP payloads from a live run, decide the attribute mapping, answer OQ 1, 3. Flags default off.
2. **Telemetry on, decisions off.** `HANGAR_OTLP_ENABLED=true` for `claude-code` only, on the owner's GKE deployment. Check volume, scrub quality and the `telemetry_silent` rate for a week.
3. **Rules backend and shadow mode.** Decisions from `rules` only, logged with labels. This gives the baseline and the label pipeline for free, with no vendor and no spend.
4. **Decision-1 in shadow** on `run_state` and `failure_triage`, with a $1/day cap, once the owner has chosen Foundry or OpenRouter and read the data terms. Others follow one at a time.
5. **Advisory** per task when the criteria in 5.5 are met. Nothing in this spec goes to `acting`.
6. **Insights and evals** after at least 100 runs with verification results exist.

Migration: `run_events` gets two columns with defaults, so old rows stay valid. New files and tables are created on startup like the rest of `store.py`. Disabling everything (`HANGAR_OTLP_ENABLED=false`, `HANGAR_DECISION_BACKENDS=[]`) returns hangar to 0001 behaviour. Image rebuild is not needed, because env vars are in the per-run script.

## 8. Test plan

### 8.1 Unit tests (fake provider; no network)

- **OTLP ingest:** protobuf fixtures from M0 → expected `spans`, `otel_logs`, `run_metrics` and `run_events` rows. Idempotent: the same batch twice inserts once (`src_key`). Unknown attributes dropped. Bad key → 401; expired key → 401; key for run A cannot write run B; client-sent `hangar.run_id` ignored.
- **Join with 0001:** a stream-json fixture plus an OTel fixture for the same run yields one ordered `seq` timeline. `tool.end` is enriched, not duplicated. Usage mismatch over 5% yields `usage_mismatch`.
- **Scrub:** corpus of ~60 real-looking secrets and ~60 benign look-alikes (git SHAs, UUIDs, base64 test data). All secrets masked; benign strings unchanged except documented cases. Allowlist test: a span with an unknown attribute `x.secret=…` stores without it.
- **Queue:** full queue returns 429; no loss of already accepted items; writer crash restarts.
- **Retention:** size cap deletes oldest runs' spans first; events retention follows 0001.
- **Decision windows:** deterministic (same events → same text and hash); token cap enforced; `features_only` window contains no assistant text (assert on a fixture that has assistant prose and a planted secret).
- **DecisionService:** with a fake backend: threshold/margin/abstain logic; `actionable` false in `shadow`; cache hit on equal windows; concurrent calls dedupe; budget exceeded → `rules_fallback`; timeout → next backend; circuit opens after 5 failures and closes after the cool-down; malformed probabilities → failure.
- **No-egress mode:** with `HANGAR_DECISION_EGRESS=none` and a socket-blocking fixture, `classify_run` completes via `rules` and opens no connection.
- **Supervisor independence:** liveness code path does not import or await `DecisionService` (import-graph assertion).
- **Interface:** `classify_run`, `get_run_trace`, `agent_events(sources=…)` return the JSON shapes in 5.7 (golden files).
- **Evals:** paired bootstrap on synthetic data with a known effect recovers it within tolerance; an eval with `repeats=1` is rejected; mixed fingerprints are not pooled; the power message is printed.
- **Aggregator:** three runs with the same `ModuleNotFoundError` on one environment produce one suggestion with a YAML diff; two runs produce none.
- **Pi parser:** fixture from a real `pi --mode json` capture maps to `tool.start/end`, `usage`, `run.result`. (Fixture required first; no capture = no test, and the parser doesn't ship.)

### 8.2 Live acceptance on GKE

Done in the phase-closing PR, recorded there. One cluster, the current `claude-python` environment, the owner's Pro credential, one control plane.

| # | Scenario | Pass criteria |
| --- | --- | --- |
| T1 | One short Claude Code run with OTLP on (HTTP/protobuf to the gateway Service) | Within 30 s of the first tool call, `get_telemetry_summary` shows `telemetry: ok`; ≥ 1 `api.request` event; `agent_events` returns log and otel events in one `seq` order; no duplicate `tool.end`. |
| T2 | Same, with `TRACEPARENT` and beta traces | `get_run_trace` returns a tree rooted at `hangar.run` with `claude_code.interaction` as a descendant. If not, record what 2.1.296 does and mark OQ 3 answered. |
| T3 | Disable-nonessential-traffic flag as in `harnesses.py` | Record whether OTel arrives (answers OQ 1). Either result passes; the doc is updated. |
| T4 | Secrets canary: the task prompt makes the agent `cat` a file containing a fake `ghp_…` token and a fake `sk-ant-…` key | Neither string appears in `spans`, `otel_logs`, `run_events`, any decision window, or the forwarded copy (test sink). Query all four stores with `LIKE`. |
| T5 | Forged identity: from inside the sandbox, send OTLP with a different `hangar.run_id` attribute and with another run's key | First lands under the real run only. Second returns 401. |
| T6 | Control-plane restart mid-run | Run continues; log events resume without duplicates; telemetry resumes within one export interval; a `telemetry_gap` warning appears. |
| T7 | Under 0003 egress enforce (when shipped): default-deny policy active | OTLP still arrives through the gateway; a probe to an arbitrary public host from the same sandbox fails. If 0003 is not yet shipped, the test is recorded as pending. |
| T8 | Docker and E2B once each | Docker: T1 passes via `host.docker.internal`. E2B: T1 passes via the public gateway host, or the gap is documented. |
| T9 | Decision-1 in shadow on 10 real runs | Every run has ≥ 1 `run_state` decision; p50 latency < 1 s and no call over 3 s timeout; measured cost matches the estimate within 2× and the day total is under $0.10; ledger matches the vendor invoice/usage page within 10%. |
| T10 | Failure injection: block the backend host from the control plane | Within 5 s, results show `rules_fallback`; supervisor and `agent_status` unaffected; circuit opens; recovers after unblocking. |
| T11 | Stuck-run reproduction: a run told to retry an impossible test | `run_state` flips to `looping` or `stuck` in shadow within 10 min; the dashboard (advisory off) shows nothing on the fleet page, and the Decisions page shows it. |
| T12 | Volume | Measure spans, bytes and rows per run on 10 real runs; update the 5.3 estimate; trace store stays under the size cap. |

### 8.3 Offline evaluation of the decision tasks

A script `scripts/eval_decisions.py` (no cluster, no network for the `rules` and `local` backends) replays stored windows against any backend and scores them against labels.

Dataset:

- **Seed set.** The 0001 M0 fixtures from the 2026-10-11 live run, plus every run from the first two weeks of shadow mode. Windows are sampled at fixed times, at trigger events, and at run end.
- **Labels.** Automatic where the table in 5.5 allows. The rest are labelled by the owner in the dashboard (a labelling queue sorted by model disagreement with rules; budget about 10 minutes per 50 windows).
- **Split by run.** 60/40 train (for thresholds, calibrator) and test. No window from a test run is used for tuning. Class counts and run counts are printed. A class under 20 test labels is reported as "insufficient".
- Windows from the same run are highly correlated. Intervals are computed with a bootstrap over runs.

Per task, report: accuracy, per-class precision and recall with Wilson intervals, macro-F1, confusion matrix, Brier, reliability table (5 bins) and ECE, precision at the configured threshold, abstain rate, p50/p95 latency, cost per decision and per run, and the same for the `rules` baseline and for each backend side by side.

| Task | Minimum labelled test set | Pass to enter `advisory` |
| --- | --- | --- |
| `run_state` | 150 windows, ≥ 30 runs, each of `progressing`, `stuck`/`looping` ≥ 20 | Macro-F1 ≥ rules + 0.05; ECE ≤ 0.08; for `stuck`/`looping`: precision ≥ 0.90 (Wilson lower ≥ 0.80) at threshold; false "stuck" on runs that later pass verification < 5% of runs |
| `failure_triage` | 100 failed runs/tests | Macro-F1 ≥ rules + 0.05; ECE ≤ 0.08; `env_dependency` precision ≥ 0.85 |
| `tool_risk` | 60 flagged + 200 unflagged tool calls | Recall on human-confirmed `destructive`/`exfiltration` ≥ 0.9 at ≤ 5% routine calls flagged per run |
| `pr_readiness` | 60 runs with outcomes | Rating correlates with human approval (Spearman ≥ 0.4, interval excludes 0) and beats "verification passed" alone on AUC, or the task stays in shadow |
| `route_task` | 100 prompts | Accuracy ≥ 0.75 vs human labels; `unknown` rate reported |
| `needs_human` | 80 runs | Recall ≥ 0.85 at precision ≥ 0.7 |

These bars are starting values chosen before seeing data. They are expected to change; changes go through the PR that changes the task file, with the reason. The point is that the bar exists before the result.

Not testable offline: vendor latency and availability. T9/T10 cover those.

## 9. Milestones

PR-sized, in order. Sizes: S ≤ 1 day, M ≈ 2–3 days, L ≈ 4–6 days. "Phase" refers to the README build order.

| # | Task | Scope | Size | Depends on | Slot |
| --- | --- | --- | --- | --- | --- |
| M0 | Capture OTLP fixtures | Run Claude Code once with each exporter against a throwaway local receiver, with and without the nonessential-traffic flag and `TRACEPARENT`. Save protobuf/JSON fixtures. Answer OQ 1 and 3. Record the real attribute names. Doc update of 5.2–5.3. | S | — | Phase 0, next to 0001 M0 |
| M1 | Scrub + allowlist | `hangar/scrub.py`, attribute allowlist per signal, secret corpus tests, wiring into 0001's ingester too (log events get the same pattern scrub). | M | — | Phase 1; helps 0001 M3 |
| M2 | OTLP receiver + storage | `/otlp/v1/*` routes on :8787 (and gateway when it exists), run ingest keys, `traces.db`, queue and writer, retention and size cap, `run_events` columns (`src`, `src_key`), mapping to `api.request`/`api.error`/`tool.decision`, enrichment, `run_agent` env wiring for claude-code, `get_telemetry_summary`, `get_run_trace`. Live T1–T6. | L | 0001 M3, M0, M1 | Phase 1 end / phase 2 start |
| M3 | pi parser | `pi-json` parser in `runevents.py`, from a real capture. | S | 0001 M3, a pi capture | Phase 6 with 0001 M7 |
| M4 | Decision core + rules backend | `DecisionService`, task schema and YAML, windows, cache, budget, circuit breaker, `decisions` / `decision_labels`, `rules` backend for `run_state` and `failure_triage`, `classify_run`, REST, labelling endpoint, `decision` events, `scripts/eval_decisions.py`. Shadow only. | L | 0001 M2–M3 | Phase 2; this is what 0004 integrates against |
| M5 | Decision-1 backend + spend ledger | Foundry adapter (and OpenRouter if easy), key in vault, `decision_spend`, caps, no-egress mode test. Live T9, T10. Needs OQ 6 answered. | M | M4, owner's Foundry/OpenRouter account | Phase 2–3 |
| M6 | Remaining tasks | `tool_risk`, `pr_readiness`, `route_task`, `needs_human` task files, windows, labels from 0002 outcomes. | M | M4, 0002 M3 for `pr_readiness` | Phase 4 |
| M7 | Local / cheap-LLM backend | One fallback behind `DecisionBackend`, chosen after OQ 7. Eval vs `rules` and Decision-1. | M | M4 | Phase 3–4, optional |
| M8 | Dashboard: trace tab and Decisions page | Waterfall, state strip, labelling UI, calibration view, mode switches. | M | M2, M4; part of the combined dashboard (0001 M8 / 0002 M9) | Phase 6 |
| M9 | Insights: metrics and env-fix suggestions | SQL views, aggregator, suggestions UI, `Run.config_fingerprint`. | M | M2, 0002 M3 | Phase 6 |
| M10 | Eval runner and routing stats | `POST /api/evals`, paired analysis, power message, cost gate, routing table and recommendations. Live check with a small real eval. | L | M9, 0002 M5 fan-out, 0001 M4 queue | Phase 6 |
| M11 | Docs and acceptance | `docs/telemetry.md`, `docs/decisions.md`, acceptance script for T1–T12, results in the PR. | M | all | Phase-closing PRs |

What 0004 needs and when: `classify_run` against the `rules` backend lands with M4, so 0004 can develop against real JSON before any vendor is involved.

## 10. Open questions

1. **Does `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (set in `harnesses.py`) stop OTel export?** The docs I read did not say. T3 answers it. If it does, either drop the flag for telemetry runs or pick another way to keep traffic minimal.
2. **OTel in other harnesses.** Does Codex, pi, Antigravity or Gemini CLI export OpenTelemetry? I could not verify any. (My recollection is that Codex has an OTel section in `config.toml` and Gemini CLI has telemetry settings. Both unchecked.) Install each and check.
3. **Claude Code span and event attributes.** Do events carry the tool-use id (for exact `tool.end` joins)? Is the `session.id` attribute present and equal to the id hangar assigns with `--session-id`? Does an inbound `TRACEPARENT` produce spans under the caller's trace id in 2.1.296? What are the real names for duration and success fields? M0.
4. **OTLP exporter and `HTTPS_PROXY`.** Does Claude Code's exporter honor proxy variables, and does `NO_PROXY` cover it? Matters for 0003's design where the proxy is mandatory.
5. **Vendor data terms.** Does Foundry or OpenRouter retain or train on submitted text? Is there a zero-retention option? Region? Unknown for both.
6. **Decision-1 API.** Request shape, how options and instructions are passed, whether probabilities include an "other" mass, whether a model revision is returned, whether Foundry bills by token only (the brief says $0.042 per 1M input tokens) or also by deployment, rate limits, and OpenRouter pricing. Needs a trial deployment before M5.
7. **Local fallback model.** Which small open model, which runtime, what latency on the control-plane pod's CPU (request 250m today), and whether the pod needs more resources. Do we accept a worse backend, or only rules, in no-egress mode?
8. **Ingest key granularity.** One per run (current design) or per sandbox? Per-run keys are tighter. Per-sandbox keys fit 0003's grants and survive `resume_agent`.
9. **OTLP encoding.** Protobuf needs the `opentelemetry-proto` package (or hand-rolled decoding). Is adding that dependency acceptable? JSON is simpler if Claude Code supports `http/json`.
10. **Pre-execution `tool_risk`.** Is a synchronous Claude Code hook worth its latency and dependency, given 0003 enforces already? Probably not, but the post-hoc results will show how much it would have caught.
11. **External backends.** Is there one the owner already runs or would pick (Grafana/Tempo, Langfuse, Phoenix, Honeycomb)? I assumed OTLP compatibility without testing any.
12. **Cost field on subscriptions.** Same as 0001 OQ13: `cost.usage` is an API-equivalent estimate on Pro. The dashboard should label it so. Should plan % be the primary cost number for subscription credentials?
13. **Labelling effort.** The promotion bars need about 150 labels per task. Is the owner willing to label ~1 hour in total, or should human labels be limited to `run_state` and `failure_triage` first?
14. **Stored decision inputs.** Keep scrubbed window text for 90 days to enable re-scoring, or keep only hashes for privacy and give up offline re-evaluation of new backends?
15. **Retention numbers.** 14 days, 50,000 spans per run, 2 GiB are guesses. T12 measures them.
16. **Eval budget on one Pro plan.** Even small paired evals use many runs. Is the owner willing to fund an API-key credential for evaluations, with a dollar cap?
