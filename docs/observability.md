# Observability: traces, metrics, cost

Operators of a software factory need to see where time and money go, and to notice when something breaks. Hangar gives you three things:

- **OpenTelemetry traces** from the control plane: one trace per job run, session turn, MCP call or REST request, down to each sandbox start, checkpoint and agent turn. Claude Code's and Codex's own spans join the same trace.
- **Prometheus metrics** at `GET /metrics`: fleet, sessions and jobs by state, latencies, tokens and dollars, budget stops, gateway pushes, MCP calls and control-plane health.
- **A cost and usage view**: `GET /api/usage` and the dashboard's **Usage** page.

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
| `sandbox.exec`, `sandbox.read_file`, `sandbox.write_file`, `sandbox.list_files` | internal | only inside another span | `hangar.exec.exit_code`, `hangar.file.bytes`. Commands and file contents are never recorded. |
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
| `hangar_checkpoint_seconds` | histogram | `op` (`capture`, `restore`), `result` (`stored`, `unchanged`, `ok`, `error`) |
| `hangar_checkpoint_bytes` | histogram | `reason` (`turn`, `interval`, `suspend`, `destroy`, `manual`) |
| `hangar_gateway_pushes_total` | counter | `result` (`ok`, `failed`) |
| `hangar_gateway_denials_total` | counter | `reason` (`repo_not_granted`, `ref_policy`, `too_large`, `protocol`) |
| `hangar_mcp_calls_total`, `hangar_mcp_call_seconds` | counter, histogram | `tool`, `status` (`ok`, `tool_error`, `error`) |
| `hangar_http_request_seconds` | histogram | `method` (a method outside HTTP's standard ones is `_OTHER`), `route` (the template), `status` |
| `hangar_db_backups_total`, `hangar_db_backup_seconds`, `hangar_db_backup_last_success_timestamp_seconds` | counter, histogram, gauge | `result` (`ok`, `error`). Alert when `time() - hangar_db_backup_last_success_timestamp_seconds` passes two backup intervals; the gauge starts empty after a restart. |
| `hangar_event_loop_lag_seconds`, `hangar_event_loop_lag_last_seconds` | histogram, gauge | How late a 0.5 s sleep on the event loop wakes up. Sustained lag means something blocks the loop. |
| `hangar_io_pool_queue_depth`, `hangar_io_pool_threads`, `hangar_io_pool_max_threads` | gauge | Blocking provider calls waiting for a worker thread (`HANGAR_IO_THREADS`, see `runtime.py`) |

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
```

## Cost and usage

```
GET /api/usage?group_by=day|model|environment|requester|repo|harness&since=7d&limit=50     (admin)
```

`since` takes:

- a relative span: `7d`, `12h` or `30m`;
- an ISO 8601 date or time, such as `2026-10-01`;
- epoch seconds.

The default is 30 days ago. Days are UTC. `limit` caps the rows; for `day` it keeps the most recent rows.

```json
{"group_by": "repo", "since": 1789313019.0,
 "rows": [{"key": "acme/widgets", "turns": 32, "input_tokens": 1732139, "output_tokens": 347633,
           "cache_read_tokens": 6228892, "cache_write_tokens": 0, "tokens": 2079772, "cost_usd": 67.74,
           "unpriced_turns": 0, "budget_stops": 4}],
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
