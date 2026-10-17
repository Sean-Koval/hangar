# Observability: traces, metrics, cost

Operators of a software factory need to see where time and money go, and to notice when something breaks. Hangar gives you three things:

- **OpenTelemetry traces** from the control plane: one trace per job run, session turn, MCP call or REST request, down to each sandbox start, checkpoint and agent turn. Claude Code's and Codex's own spans join the same trace.
- **Prometheus metrics** at `GET /metrics`: fleet, sessions and jobs by state, latencies, tokens and dollars, budget stops, gateway pushes, MCP calls and control-plane health.
- **A cost and usage view**: `GET /api/usage` and the dashboard's **Usage** page.
- **An operator console**: `hangar status`, `GET /api/status` and the MCP resource `hangar://status`, for admins: power, loops, versions, builds, the day's failures by cause, egress denies, waiting requests, SLOs, credential health and recent warnings ([below](#the-operator-console-hangar-status)).
- **Flight records** of every failed, timed-out or slow job and turn, with the end of its run log, sealed ([below](#flight-records)). The [Cloud Doctor](doctor.md) reads them every night.

This builds on the events, usage and budgets in [activity.md](activity.md), and covers the observability parts of [spec 0005](specs/0005-traces-and-decision-model.md) and [spec 0008 §5.7](specs/0008-swarm-improvement-loop-and-rl.md). It does not include 0005's in-process OTLP collector, `traces.db` or decision model.

## Traces

Tracing is off unless the standard OpenTelemetry variables name an exporter. Set one of these on the control plane:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318   # OTLP/HTTP; the SDK appends /v1/traces
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces   # or traces only
OTEL_TRACES_EXPORTER=console                              # spans on stdout, for a quick look
```

Notes on the settings:

- **Nothing set means off.** This differs from the SDK, which defaults to OTLP on localhost. `OTEL_SDK_DISABLED=true` or `OTEL_TRACES_EXPORTER=none` also turn tracing off.
- **The SDK is an extra.** It comes with `pip install 'hangar[otel]'` and is part of `hangar[all]`, which the control-plane image installs.
- **Off costs nothing.** Hangar doesn't import the OpenTelemetry SDK or exporter, and each hook costs one global check. A test asserts both.
- **Protocol.** OTLP goes over HTTP/protobuf. `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` works if `opentelemetry-exporter-otlp-proto-grpc` is installed; `http/json` isn't supported.
- **Standard variables.** The SDK reads `OTEL_EXPORTER_OTLP_HEADERS`, `_TIMEOUT`, `_CERTIFICATE`, `OTEL_SERVICE_NAME` (default `hangar`), `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_TRACES_SAMPLER` / `_ARG` and `OTEL_BSP_*` as usual.
- **Hangar's own TracerProvider.** Hangar doesn't install a process-wide provider. As a result, FastAPI's built-in telemetry (FastAPI ≥ 0.143) stays off and requests aren't traced twice. Don't set `FASTAPI_OTEL_AUTO_CONFIGURE=true`: that would add FastAPI's own request spans, and logs that include exception messages.
- **A failed export never hurts the work.** If the collector is down, spans are dropped.

### What is traced

| Span | Kind | Parent | Attributes |
| --- | --- | --- | --- |
| `GET /api/sandboxes/{ref}` and other REST, dashboard and MCP HTTP requests | server | the caller's `traceparent`, if it sent one | `http.request.method`, `http.route`, `http.response.status_code`. Named by route template, never by raw path. `/metrics` and `/healthz` get no span. |
| `POST /git/git-receive-pack`, `GET /git/info/refs`, `POST /git/git-upload-pack` | server | | Git gateway requests. A push adds `hangar.gateway.push` (`ok` or `failed`), `hangar.sandbox.id` and `hangar.gateway.upstream_status`. A refusal adds `hangar.gateway.denied`. |
| `tools/call <tool>` | server | the HTTP request | MCP semantic conventions: `mcp.method.name`, `gen_ai.tool.name`, `gen_ai.operation.name=execute_tool`. A tool's own failure sets `error.type=tool_error`. |
| `job.process` | internal | root | One per run of a job: the first turn, each follow-up, a restart re-attach. Attributes: `hangar.job.id`, `.source`, `.strategy`, `.turn`, `hangar.environment`. |
| `job.provision`, `job.verify`, `job.deliver` | internal | `job.process` | `hangar.verify.exit_code`, `hangar.job.draft` |
| `session.boot` | internal | job, MCP call or REST request | Starting a sandbox for a session, and restoring its checkpoint |
| `sandbox.create` | internal | | `hangar.environment`, `.provider`, `.sandbox.id`, `.sandbox.warm` (a warm-pool claim), `.sandbox.fork` |
| `sandbox.image`, `sandbox.start`, `sandbox.start_wait`, `sandbox.setup`, `sandbox.repo` | internal | `sandbox.create` | The image lookup or build; the provider's create or claim; waiting for it to run (node scale-up, image pull); harness, credential and MCP setup; the clone and the repo's setup command |
| `sandbox.destroy`, `sandbox.snapshot` | internal | | |
| `sandbox.exec`, `sandbox.read_file`, `sandbox.write_file`, `sandbox.list_files` | internal | only inside another span | `hangar.exec.exit_code`, `hangar.exec.transport` (`envd` or `exec`, see [envd.md](envd.md)), `hangar.file.bytes`. Commands and file contents are never recorded. |
| `checkpoint.capture`, `checkpoint.restore` | internal | the turn, boot or suspend | `hangar.checkpoint.reason`, `.stored`, `.bytes` |
| `db.backup`, `db.restore_stage` | internal | root, or `hangar admin`'s request | A database backup and a staged restore (gke.md §7): `hangar.backup.name`, `.db_bytes`, `.stored_bytes` |
| `invoke_agent <harness>` | internal | `job.process`, or the request that sent the message | One per agent turn, from start until hangar has collected its result and checkpointed. GenAI semantic conventions: `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name` (the harness), `gen_ai.provider.name` (`anthropic` for Claude Code, `openai` for Codex, `gcp.gemini` for Antigravity), `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.conversation.id`, `gen_ai.usage.input_tokens` (cached tokens included, as the convention says), `.output_tokens`, `.cache_read.input_tokens`, `.cache_write.input_tokens`. Hangar adds `hangar.cost_usd`, `.cost_source`, `.turn.outcome`, `.turn.exit_code`, `.run.id`, `.session.id`, `.job.id`. A turn that timed out, went over budget or lost its sandbox is an error span. |

Failed spans carry `error.type`: the exception class, `cancelled`, `tool_error`, or the turn's outcome. Their status message is the error, with stored credential values masked (`broker.redact`). Exceptions and stack traces are not recorded as span events.

Housekeeping makes as few traces as it can:

- Idle checks and the reaper don't open spans of their own.
- An interval checkpoint is one small trace.
- Exec and file I/O are only traced inside another span.

The GenAI and MCP attribute names were checked against the OpenTelemetry semantic conventions on 2026-10-13. The GenAI conventions now live in the [semantic-conventions-genai repository](https://github.com/open-telemetry/semantic-conventions-genai); see its `gen-ai-agent-spans.md` and `mcp.md`. They are still *Development* status, so names may change.

### Into the sandbox: the agents' own telemetry

Every agent run gets the `TRACEPARENT` of its `invoke_agent` span in its environment. The trace flags are reduced to the sampled bit: codex 0.162.1 rejects the W3C level-2 "random" flag (`03`) with "TRACEPARENT is set but invalid; ignoring trace context".

With **`HANGAR_OTEL_SANDBOX_ENDPOINT`** set to an OTLP/HTTP collector that sandboxes can reach, the harnesses also export their own telemetry under hangar's span:

| Harness | How hangar wires it |
| --- | --- |
| Claude Code | Per run, these variables: `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` (traces are beta), `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` and `OTEL_EXPORTER_OTLP_ENDPOINT`. In `-p` mode Claude Code reads `TRACEPARENT`, so its `claude_code.interaction` span is a child of hangar's turn. |
| Codex | Codex takes exporters only from the user-level `~/.codex/config.toml`. Hangar writes a managed `[otel]` block there when the sandbox starts: `exporter`, `trace_exporter` and `metrics_exporter`, each `{ otlp-http = { endpoint = "<endpoint>/v1/{logs,traces,metrics}", protocol = "binary" } }`, with `environment = "hangar"`. Without that block, `metrics_exporter` defaults to `statsig`. If the environment's own config already has `[otel]`, hangar leaves it alone and adds a warning to the sandbox. |
| pi, Antigravity | `TRACEPARENT` only. Neither has OTel export that hangar can configure. |

Every harness also gets `OTEL_RESOURCE_ATTRIBUTES` with `hangar.sandbox.id`, `hangar.run.id`, `hangar.environment` and `hangar.harness`. A sandbox can set its own resource attributes, so treat these as labels for grouping, not as proof of where a span came from.

No headers or credentials go into sandboxes, so the collector must accept unauthenticated OTLP from the sandbox network. On GKE that means the in-cluster collector plus the NetworkPolicy in `deploy/k8s/observability/otel.yaml`.

**Verified on 2026-10-13, offline.** Claude Code 2.1.296 and codex 0.162.1 were run as hangar runs them, against the scripted model in `tests/fixtures/streams/capture` and a local OTLP receiver:

- **Claude Code** exported traces, logs and metrics. Its `claude_code.interaction` span had hangar's trace id and parent span. This held with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, which hangar always sets, so that flag doesn't block export to your own collector.
- **Codex** exported logs, traces and metrics with the block above. `codex.exec` and `turn/start` were children of hangar's span.
- **Volume.** A short Codex run sends about 800 spans, mostly `fs.*`. Sample or filter at the collector if that is too many.

### Your MCP client's trace

Hangar continues an incoming `traceparent` header on REST and MCP requests (tested over streamable HTTP), so hangar's `tools/call` span sits under the client's span.

Claude Code sends that header on HTTP MCP requests when its tracing beta is on. Per its docs, that is only when `ANTHROPIC_BASE_URL` is unset or points at the Anthropic API, unless `CLAUDE_CODE_PROPAGATE_TRACEPARENT=1` is set. With both ends exporting, one trace runs from the laptop agent, through hangar, into the cloud agent's sandbox.

### Each tool call, and traces as files

With `HANGAR_TRACE_LIVE_TOOLS=true`, each turn's tool calls, subagents and approvals also go to the collector when the turn ends, as children of its `invoke_agent` span (`execute_tool {tool}`, with the normalized `hangar.tool.kind`). The same spans, with the session, job and their events (steers, verify, commits, PRs, CI), can be exported as OTLP/JSON files at any time: `hangar trace export <session or job>`, `GET /api/sessions/{name}/trace`, `GET /api/jobs/{id}/trace`. The schema and its taxonomy are a versioned contract: [docs/api/traces.md](api/traces.md).

## Metrics

`GET /metrics` returns the Prometheus text format.

- **Who can read it.** An admin token, or a token that can read metrics and nothing else: `hangar token create prometheus --scope metrics`.
- **Turning it off.** `HANGAR_METRICS_ENABLED=false` turns `/metrics` off (404). Hangar then never imports `prometheus_client`.

| Metric | Type | Labels |
| --- | --- | --- |
| `hangar_sandboxes` | gauge | `state`, `provider` |
| `hangar_sandbox_create_seconds` | histogram | `provider`, `environment`, `result` (`ok`, `error`). Time to a running sandbox, with image build, start, setup and clone. |
| `hangar_warm_pool_claims_total` | counter | `provider`, `environment`, `result` (`hit`, `miss`). Only for environments with a warm pool. |
| `hangar_sessions` | gauge | `state` |
| `hangar_jobs` | gauge | `state`, `reason` (the outcome: `over_budget`, `verify_failed`, `agent_failed`…) |
| `hangar_turn_seconds` | histogram | `harness`, `outcome`. The agent's time, start to finish. |
| `hangar_tokens_total` | counter | `kind` (`input`, `output`, `cache_read`, `cache_write`), `model`, `harness`, `environment`, `requester` |
| `hangar_cost_usd_total` | counter | `model`, `harness`, `environment`, `requester` |
| `hangar_unpriced_turns_total` | counter | `model`, `harness`. Turns whose model has no price; their cost is unknown, not 0. |
| `hangar_budget_stops_total` | counter | `harness`, `environment` |
| `hangar_cost_uncached_usd_total` | counter | `model`, `harness`, `environment`. What priced turns would have cost with no prompt cache; minus `hangar_cost_usd_total`, what caching saved ([sessions.md](sessions.md#prompt-caching)). |
| `hangar_cache_write_1h_tokens_total` | counter | `model`, `harness`. The part of `cache_write` tokens written with the 1-hour TTL (priced at 2x input). |
| `hangar_cache_ttl_turns_total` | counter | `ttl` (`5m`, `1h`, `off`, `none`: the harness has no setting), `harness` |
| `hangar_checkpoint_seconds` | histogram | `op` (`capture`, `restore`), `result` (`stored`, `unchanged`, `ok`, `error`) |
| `hangar_checkpoint_bytes` | histogram | `reason` (`turn`, `interval`, `suspend`, `destroy`, `manual`) |
| `hangar_gateway_pushes_total` | counter | `result` (`ok`, `failed`) |
| `hangar_gateway_denials_total` | counter | `reason` (`repo_not_granted`, `ref_policy`, `too_large`, `protocol`) |
| `hangar_egress_connections_total` | counter | `decision` (`allow`, `deny`), `reason` (for a denial: `not_allowed`, `sni_mismatch`, `private_address`, `too_many`…; for an allow, what matched: `harness`, `preset`, `environment`, `repo`, `approved`), `mode` |
| `hangar_egress_bytes_total` | counter | `direction` (`out` to the internet, `in`) |
| `hangar_egress_open_connections` | gauge | tunnels open through the egress proxy |
| `hangar_sandbox_exec_seconds` | histogram | `transport`: `envd` (hangar-envd ran it), `exec` (the provider's own exec: `docker exec`, pods/exec, E2B), `error`. A falling `envd` share means daemons can't be reached or started. |
| `hangar_mcp_calls_total`, `hangar_mcp_call_seconds` | counter, histogram | `tool`, `status` (`ok`, `tool_error`, `error`) |
| `hangar_http_request_seconds` | histogram | `method` (a method outside HTTP's standard ones is `_OTHER`), `route` (the template), `status` |
| `hangar_db_backups_total`, `hangar_db_backup_seconds`, `hangar_db_backup_last_success_timestamp_seconds` | counter, histogram, gauge | `result` (`ok`, `error`). Alert when `time() - hangar_db_backup_last_success_timestamp_seconds` passes two backup intervals; the gauge starts empty after a restart. |
| `hangar_event_loop_lag_seconds`, `hangar_event_loop_lag_last_seconds` | histogram, gauge | How late a 0.5 s sleep on the event loop wakes up. Sustained lag means something blocks the loop. |
| `hangar_loop_last_tick_timestamp`, `hangar_loop_failures_total` | gauge, counter | `loop` (`reaper`, `reconciler`, `sessions`, `schedules`, `backups`). When each background loop last finished a pass, and passes that raised. Alert when `time() - hangar_loop_last_tick_timestamp` passes three of the loop's intervals (reaper 60 s, reconciler 30 s, sessions 60 s, schedules `HANGAR_SCHEDULE_POLL_SECONDS`, backups `HANGAR_BACKUP_HOURS`); the gauge starts empty after a restart. |
| `hangar_io_pool_queue_depth`, `hangar_io_pool_threads`, `hangar_io_pool_max_threads` | gauge | Blocking provider calls waiting for a worker thread (`HANGAR_IO_THREADS`, see `runtime.py`) |
| `hangar_streams_open`, `hangar_streams_total`, `hangar_stream_bytes_total`, `hangar_stream_first_output_seconds` | gauge, counter, counter, histogram | `kind` (`exec`, `process_attach`, `events`, `watch`, `process`, `turn_log`, ...); the counter also `reason` (`done`, `client`, `shutdown`, `destroyed`, `paused`, `revoked`, `lifetime`, `error`). Streams to clients and the logs and processes hangar follows ([streaming.md](streaming.md)). |

Labels never carry a job, session, sandbox or run id.

- **Capped labels.** Requesters, models, environments, tools, routes and reasons are capped at 200 distinct values each; anything past the cap is `other`.
- **Counters reset on restart.** Use `rate()` and `increase()`.
- **When usage is counted.** Tokens and cost are added when a turn ends, so a running turn shows up in `/api/usage` (live) before it does here.
- **Gauges are read live.** `hangar_sandboxes`, `hangar_sessions` and `hangar_jobs` are computed from the store at scrape time.

Some useful queries:

```promql
sum by (requester) (increase(hangar_cost_usd_total[1d]))             # spend per requester, last day
histogram_quantile(0.95, sum by (le, provider) (rate(hangar_sandbox_create_seconds_bucket[1h])))
sum(rate(hangar_warm_pool_claims_total{result="hit"}[1h])) / sum(rate(hangar_warm_pool_claims_total[1h]))
increase(hangar_budget_stops_total[1d]) > 0
histogram_quantile(0.99, rate(hangar_event_loop_lag_seconds_bucket[5m])) > 0.25   # the control plane is stalling
hangar_io_pool_queue_depth > 0                                                     # provider calls are queueing
# prompt-cache hit ratio per harness: the share of prompt tokens read from the cache
sum by (harness) (rate(hangar_tokens_total{kind="cache_read"}[1d]))
  / sum by (harness) (rate(hangar_tokens_total{kind=~"input|cache_read|cache_write"}[1d]))
sum(increase(hangar_cost_uncached_usd_total[1d])) - sum(increase(hangar_cost_usd_total[1d]))   # saved by caching
```

## Cost and usage

Weekly adoption numbers (yield, agent share, review wait, cost per merged PR, ...) and the morning standup line:
[metrics.md](metrics.md), `hangar metrics weekly`.

```
GET /api/usage?group_by=day|model|environment|requester|repo|harness|session|job|cache_ttl&since=7d&limit=50  (admin)
```

`hangar usage --by session --cache` prints it as a table; `--cache` adds the cache columns ([activity.md](activity.md#prompt-cache)).

`since` takes:

- a relative span: `7d`, `12h` or `30m`;
- an ISO 8601 date or time, such as `2026-10-01`;
- epoch seconds.

The default is 30 days ago. Days are UTC. `limit` caps the rows; for `day` it keeps the most recent rows.

```json
{"group_by": "repo", "since": 1789313019.0,
 "rows": [{"key": "acme/widgets", "turns": 32, "input_tokens": 1732139, "output_tokens": 347633,
           "cache_read_tokens": 6228892, "cache_write_tokens": 0, "tokens": 2079772, "cost_usd": 67.74,
           "unpriced_turns": 0, "budget_stops": 4, "cache_write_1h_tokens": 0, "cache_hit_ratio": 0.7824,
           "saved_usd": 22.43}],
 "total": {"key": "total", ...},
 "budget_stops": [{"ts": 1791866712.4, "session": "job-3fa91c20", "job": "job-3fa91c20", "repo": "acme/widgets",
                   "requester": "github:sean", "reason": "spent $5.12; the budget is $5.00", "cost_usd": 5.12}]}
```

The numbers come from agent turns, each counted once:

- **Session turns.** A job's turns are the turns of the sessions that carry it, so best-of-N attempts, repair runs and the judge are all included. A fork's copy of its parent's history is not counted again.
- **Older jobs.** Jobs from before sessions carried them use their own turn records.
- **Deleted sessions.** A deleted session that carried a job keeps its total on the job, so its spend stays in the view. It counts on the day the job was created, with no turns.
- **Running turns.** A running turn counts with its live usage, on the day it started.
- **Costs.** `cost_usd` adds the known costs. `unpriced_turns` counts turns whose model has no price ([activity.md](activity.md#usage)).
- **Repos.** Repos are shown as `owner/name` for GitHub, and never with credentials from a URL.

The dashboard's **Usage** page (`#usage`) shows, for the last 7, 30 or 90 days:

- spend, tokens, turns and budget stops;
- spend per day, with each day's numbers on hover;
- the top repos and requesters;
- the latest budget stops.

## The operator console: `hangar status`

One view of what you'd otherwise need kubectl for, admins only (`GET /api/status`, the MCP resource `hangar://status`, `hangar status`). It is a resource, not a tool: the MCP tool list is at its cap, and claude.ai caches tool lists. A member's token gets "needs a token with the 'admin' scope".

```
$ hangar status
sandboxes: on
last deploy: 3h ago, 0.1.0+tools.4f1c: passed
loops: ops 21s, reaper 34s, reconciler 12s, schedules 3s, sessions 40s
harnesses: antigravity 1.3.3, claude-code 2.1.296, codex 0.162.1 (pack behind on kubernetes), pi 1.1.0
builds: pack codex, repo acme/api python v4 (installing)
failures (24h): 7, 3 transient
  missing_tool      4             last 2h ago (fr-9a1b…)  python
  model_api         3  transient  last 20m ago (fr-77c2…)  python, node
egress denies (24h): 12
  registry.npmjs.org                           12  3 sandbox(es)  node
requests waiting: 1, oldest 14m
SLOs (24h):
  create     warm  n=41   p50 1.2s p95 2.1s  (p50<=1.5s p95<=2.5s) ok
  create     cold  n=9    p50 18.0s p95 41.0s  (p95<=30.0s) BREACHED
  turn_start any   n=120  p50 2.4s p95 6.0s  (p50<=5.0s p95<=15.0s) ok
  resume     cold  n=6    p50 22.0s p95 38.0s  (p95<=60.0s) ok
credentials: 5 ok, 1 warn
  ! claude-alice (alice): expires on 2026-10-27, in 13 day(s). Run `claude setup-token` and store the new token: …
cloud doctor: 2 open finding(s); last run 9h ago (done)
```

| Section | What it holds | Source |
| --- | --- | --- |
| `power`, `last_deploy` | the sandbox pools' state and when they're back; the last deploy.yml result | [power.py](../src/hangar/power.py) |
| `loops` | each background loop's last pass, interval and failures (`ops` is the console's own) | `hangar_loop_last_tick_timestamp` |
| `harnesses` | each agent CLI's locked version and the version each provider's pack installed; `behind` names providers whose pack differs | `harnesses.lock.yaml`, pack builds |
| `builds` | environment images, harness packs and repo builds running now, with how long | the fleet, [envbuilds](environments.md) |
| `failures` | the last 24 hours' [flight records](#flight-records) by cause, transient or not, and the 10 newest | flight records |
| `egress_denies` | the last 24 hours' denies by host: count (suppressed ones included), sandboxes, environments | the audit log |
| `requests` | secret and egress requests agents filed that wait for an answer | [env requests](computer-use.md) |
| `slo` | p50/p95 over 24 hours against the targets below | in memory, saved every minute |
| `credentials` | [credential health](#credential-health): counts, what needs renewing, pools | [credhealth.py](../src/hangar/credhealth.py) |
| `models` | what jobs run on by default (`balanced`, the id it resolves to and its list price), the job budget and daily cap that bound it, and the repos whose policy caps them; `NO job budget or daily cap` until one is set, which `hangar doctor` also warns about (`model spend`) | [modelpolicy.py](../src/hangar/modelpolicy.py) |
| `nightly`, `doctor` | the nightly pass and the [Cloud Doctor](doctor.md)'s last run and open findings | |
| `log` | the last 50 warnings and errors hangar logged | in memory |

`?section=slo,failures` (or `--section`) returns only those. `--check-credentials` checks every credential now; otherwise status rechecks only results older than an hour, spending at most 15 s.

**The log section** keeps the last 200 records at WARNING and above from hangar's own loggers (`hangar.*`). Uvicorn's loggers never reach it: `uvicorn.access` lines carry request paths and query strings. Each line goes through `broker.redact` before it is shown, so a stored credential value in an exception message comes out masked.

`fleet_overview` (MCP) and `GET /api/overview` carry two of these as fields: `failures_24h` (count by cause) and `credential_health` (counts, and how many need attention). `list_credentials` carries each credential's `health`, `last_ok` and `expires_at`. `get_job` carries `flight_record` on a finished job that has one.

### SLOs

| What | Path | Target |
| --- | --- | --- |
| create | `warm`: a pod claimed from the warm pool | p50 ≤ 1.5 s, p95 ≤ 2.5 s |
| create | `cold`: a new pod, image cached | p95 ≤ 30 s |
| create | `build`: an image or pack was built on the way | none (exceptional by design) |
| turn start | from `begin_turn` to the agent running, the sandbox up (time spent waiting for a best-of-N sibling's cache excluded) | p50 ≤ 5 s, p95 ≤ 15 s |
| resume | `hot`: a parked sandbox woken / `cold`: a new sandbox restored from a checkpoint | p95 ≤ 5 s / ≤ 60 s |

These are the observability review's targets for GKE. Forks aren't creates here. Samples live in memory, are saved to the store every minute by the ops loop and reloaded at start, so a restart keeps the day. `hangar_sandbox_create_seconds` in `/metrics` carries the creates for people who scrape.

## Flight records

A record is written for each job that fails or times out, each session turn outside a job that fails or times out, and each job or turn that ran slow against its environment's norm: more than 3× the median of its environment's last 50 clean runs, and at least 2 minutes over it, once there are 8 to compare with. Cancelled jobs and jobs waiting for an answer get none.

| Field | What |
| --- | --- |
| `cause`, `detail`, `transient` | decided without a model, most specific first: `power_down`, `pod_replaced`, `restart`, `model_api` (a 429/5xx/529 or "overloaded" from the model API), `provision` / `image_build`, `install` (setup failures, or a repo build that fell back to the base image), `missing_tool` (`command not found`, `No module named`, `Cannot find module`…; `detail` names it), `egress` (`detail`: the most denied host), `secret`, `blocked` (a request nobody answered), then the job's reason (`verify_failed`, `timed_out`, `over_budget`, `agent_failed`, `push_failed`…), `slow`. Transient: `model_api`, `power_down`, `restart`, `pod_replaced`, `sandbox_gone`, `network`. |
| `signature` | cause, detail (numbers dropped) and repo, or environment: what the Cloud Doctor groups by |
| `stages`, `failed_stage`, `sandbox_cause` | the sandbox create's stage history, and where and why it stopped |
| `env_doctor` | what `hangar-agent env doctor` reports: build and fallback, setup failures, warnings, services, expected secret names, egress mode |
| `egress_denies` | the sandbox's denies from the audit log (host, port, reason), at most 20 |
| `requests` | the agent's secret and egress requests still pending or denied |
| `versions`, `model`, `harness_version` | hangar, the harness CLI the turn ran, the model, the image and packs |
| `cost_usd`, `tokens`, `seconds`, `norm` | what it used, how long it took, and the environment's median |
| `verify`, `events`, `summary` | the check's command, exit code and tail; the job's last 20 events |
| `log` | where the end of the run log is stored, its size and SHA-256 |

**The run log** is the last 256 KB of the turn's log. It is read when the session goes idle (a job's waits in memory, at most 64 of them and an hour, until the job is reported), then on a task of its own it is masked (stored credential values and their encodings, and the token formats secret scanning knows), gzipped, encrypted with the vault key like a checkpoint part, and stored in the session store under `records/<id>/`. Every free-form field of the record goes through the same masking. Records and logs are deleted after 14 days, hourly. Nothing about a record is ever on the turn's or the job's path: a failure to write one is logged and nothing else.

```bash
hangar admin records ls --hours 48 [--cause missing_tool]
hangar admin records show fr-9a1b2c3d --log
```

REST: `GET /api/records?hours=24&cause=…` (admins), `GET /api/records/{id}` and `GET /api/records/{id}/log` (admins, or the record's person), `GET /api/jobs/{id}/record`. MCP: the resource `hangar://jobs/<id>/record`, for whoever may see the job.

## Run capture

Traces and metrics show the fleet now; run capture keeps every run for later. With `HANGAR_OUTBOX_URL` set (a GCS
bucket on GKE, `terraform output run_bucket`; a local directory in dev), every finished run is written once as
immutable objects: `run.json` (`hangar.run/v1`: who, what, the harness and environment digests, outcome, usage,
verify, steers, the untrusted flag), the turn's manifest, its trace export, its test results and its patch, under
`v1/runs/{date}/{session_id}.{run_id}/`, plus job events (PR opened, first review, merged, closed, abandoned) under
`v1/jobs/`. The sister tool reads them into its own tables while the sandbox pools sleep. The contract (layout,
schema, redaction, write-once IAM, deletion requests) is [docs/api/outbox.md](api/outbox.md).

Nothing of it is on a turn's path: the objects are built and redacted on a task after the turn and staged on the data
volume with a queue record each, and a drain loop uploads them, retrying with backoff (1 minute to 6 hours) through an
outage and across restarts. The control plane stays up overnight, so the queue drains while the pools are off.

| Where | What |
| --- | --- |
| `hangar outbox status` (`GET /api/admin/outbox`) | pending, done and withheld objects, the oldest pending one, the last error |
| `hangar outbox retry` / `backfill --since 7d` | upload now after an outage; capture runs that have nothing queued |
| `hangar_outbox_pending` | objects waiting to be uploaded: alert when it keeps growing |
| `hangar doctor` | warns past 1,000 pending objects or a day of age, lists withheld objects (they still held a secret after redaction, so they are never uploaded), and the bytes deleted sessions hold until their export |

Retention waits for the export: a deleted session's event log and manifests, and a failed job's test results, stay
until the outbox has uploaded its runs (at most `HANGAR_EXPORT_GRACE_DAYS`, 30; [outbox.md](api/outbox.md#retention-and-the-export-189)).

## Credential health

Each agent credential is checked without calling a model:

| Credential | Check | Expiry |
| --- | --- | --- |
| Claude Code `oauth_token` (`claude setup-token`) | the token's age: the command makes "a one-year OAuth token" ([Claude Code authentication](https://code.claude.com/docs/en/authentication)) | a year after it was stored |
| Claude Code and Codex `api_key` | the key's format (a gateway's own key is a warning, not a failure) | none |
| Codex `auth_file` (ChatGPT login) | `codex login status`, which "exits with 0 when credentials are present" ([Codex CLI](https://learn.chatgpt.com/docs/developer-commands)), in the login's sandbox when it is captured; elsewhere the same question answered from the file: an API key, or ChatGPT tokens with a refresh token | none: Codex refreshes its tokens while it is used |
| Google service-account keys (Vertex AI for pi and Antigravity) | in a gcloud config directory of its own: `gcloud auth activate-service-account --key-file`, then `gcloud auth print-access-token` (Google refuses a deleted or disabled key; the token is discarded), then `gcloud iam service-accounts keys list` where the key may list its own account's keys | the key's `validBeforeTime`, when it has one |

Each check writes `health` (`ok`, `warn`, `fail` or `unknown`, with a detail and when), `last_ok` and `expires_at` onto the credential. Storing a new value clears them. Checks run when a login is captured, every night (the [nightly pass](#the-nightly-pass)), from `hangar status`, and with `hangar cred check [NAME…]` (`POST /api/credentials/check`).

A credential that fails, or expires within 14 days, shows in `hangar doctor` (`credential health`; marked `deploy_gate: false`, so `deploy.yml`'s acceptance gate reports it rather than rolling back, since a rollback can't renew a login), in `hangar status`, and in the [inbox](landing.md) of whoever renews it: its person for their own logins, admins for the team's. Pools (several logins one sandbox holds at a time, such as a person's Codex ChatGPT logins) are summed up; a pool with no working login fails the doctor even when each login alone would only warn.

## The nightly pass

The ops loop runs once a night at `HANGAR_DOCTOR_SCHEDULE` (cron, default `30 0 * * *`) in `HANGAR_DOCTOR_TIME_ZONE` (default: the power schedule's zone, else UTC): half an hour before the sandbox pools power down. It checks every credential, then runs the [Cloud Doctor](doctor.md). The first look after a start only remembers the latest slot, so a deploy never runs it on the spot; a slot missed while hangar was down runs once when it is back. `hangar status` shows the last pass under `nightly`.

## Deploying

### Prometheus

| Prometheus | File | |
| --- | --- | --- |
| Prometheus Operator (kube-prometheus-stack) | `deploy/k8s/observability/prometheus-operator.yaml` | A `hangar-metrics` Service and a ServiceMonitor that sends the token from Secret `hangar-metrics-token` |
| Google Cloud Managed Service for Prometheus | `deploy/k8s/observability/gke-managed-prometheus.yaml` | A PodMonitoring with `authorization.credentials.secret`, and a Role that lets Managed Prometheus's collectors (`gmp-system/collector`) read that one Secret |

Both read the token from a Secret:

```bash
hangar token create prometheus --scope metrics          # prints hgr_…
kubectl -n hangar create secret generic hangar-metrics-token --from-literal=token=hgr_...
kubectl apply -f deploy/k8s/observability/gke-managed-prometheus.yaml   # or prometheus-operator.yaml
```

Plain `prometheus.io/scrape` annotations can't send a token, so there is no annotation-only variant. Managed Prometheus's PodMonitoring fields were checked against the [prometheus-engine API reference](https://github.com/GoogleCloudPlatform/prometheus-engine/blob/main/doc/api.md). Its collector service account (`collector` in `gmp-system`) was checked against the project's `manifests/operator.yaml`.

### Traces in Google Cloud Trace

Cloud Trace takes OTLP at the Telemetry API, `https://telemetry.googleapis.com` (`/v1/traces`). That endpoint needs Google OAuth with the `trace.append` or `cloud-platform` scope, which a plain OTLP exporter can't add. So route traces through Google's collector:

1. **Install the [Google-Built OpenTelemetry Collector](https://docs.cloud.google.com/stackdriver/docs/instrumentation/opentelemetry-collector-gke):**

   ```bash
   export GOOGLE_CLOUD_PROJECT=<project> PROJECT_NUMBER=<number>
   kubectl kustomize https://github.com/GoogleCloudPlatform/otlp-k8s-ingest.git/k8s/base | envsubst | kubectl apply -f -
   ```

   Give its Kubernetes service account (`opentelemetry/opentelemetry-collector`, through Workload Identity) the roles Google's page lists: `roles/cloudtrace.agent`, `roles/monitoring.metricWriter` and `roles/logging.logWriter`. The collector exports traces with `otlphttp` to `https://telemetry.googleapis.com`, with the `googleclientauth` extension.

2. **Point hangar at it.** Add these to the `hangar` ConfigMap and restart:

   ```yaml
   OTEL_EXPORTER_OTLP_ENDPOINT: http://opentelemetry-collector.opentelemetry.svc.cluster.local:4318
   OTEL_SERVICE_NAME: hangar
   HANGAR_OTEL_SANDBOX_ENDPOINT: http://opentelemetry-collector.opentelemetry.svc.cluster.local:4318   # optional
   ```

3. **Optional: let sandboxes reach the collector.** If you set `HANGAR_OTEL_SANDBOX_ENDPOINT`, apply `deploy/k8s/observability/otel.yaml`. It lets sandboxes reach the collector's port 4318 and nothing else in the cluster. The collector also forwards the harnesses' metrics and logs to Cloud Monitoring and Cloud Logging.

The endpoint, scopes, collector install and roles were checked against Google's docs on 2026-10-13: the [Telemetry API `v1.traces`](https://docs.cloud.google.com/stackdriver/docs/reference/telemetry/v1.traces.post) and the collector page above. Hangar's spans can also go to any other OTLP backend (Tempo, Jaeger, Honeycomb, Grafana Cloud…) through `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS`. None of those has been tested.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `_TRACES_ENDPOINT` / `OTEL_TRACES_EXPORTER` | unset | Turn tracing on (see above). Every other standard `OTEL_*` variable applies. |
| `HANGAR_OTEL_SANDBOX_ENDPOINT` | unset | OTLP/HTTP base URL that sandboxes export Claude Code and Codex telemetry to. A plain `http(s)` URL without credentials. |
| `HANGAR_METRICS_ENABLED` | `true` | `GET /metrics` |
| `HANGAR_TRACE_LIVE_TOOLS` | `false` | With tracing on, also send each turn's tool, subagent and approval spans ([above](#each-tool-call-and-traces-as-files)) |
| `HANGAR_DOCTOR_SCHEDULE` | `30 0 * * *` | When the [nightly pass](#the-nightly-pass) runs (credential checks, the Cloud Doctor); `off` turns it off |
| `HANGAR_DOCTOR_TIME_ZONE` | the power schedule's | The schedule's IANA time zone (UTC when there's no power schedule) |
| `HANGAR_OUTBOX_URL` | unset | [Run capture](#run-capture): `gs://bucket[/prefix]` or a local directory ([outbox.md](api/outbox.md)) |
| `HANGAR_OUTBOX_SESSIONS` | `all` | `jobs_only` leaves out sessions not attached to a job |
| `HANGAR_EXPORT_GRACE_DAYS` | `30` | How long a deleted session's event log and manifests wait for their export |
