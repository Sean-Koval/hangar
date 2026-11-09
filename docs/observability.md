# Observability: traces, metrics, cost

Operators of a software factory need to see where time and money go, and to notice when something breaks. hangar gives you three things:

- **OpenTelemetry traces** from the control plane: one trace per job run, session turn, MCP call or REST request, down to each sandbox start, checkpoint and agent turn. Claude Code's and Codex's own spans join the same trace.
- **Prometheus metrics** at `GET /metrics`: fleet, sessions and jobs by state, latencies, tokens and dollars, budget stops, gateway pushes, MCP calls and control-plane health.
- **A cost and usage view**: `GET /api/usage` and the dashboard's **Usage** page.
- **An operator console**: `hangar status`, `GET /api/status` and the MCP resource `hangar://status`, for admins: power, loops, versions, builds, the day's failures by cause, egress denies, waiting requests, SLOs, credential health and recent warnings ([the operator console](observability/operator.md#the-operator-console-hangar-status)).
- **Flight records** of every failed, timed-out or slow job and turn, with the end of its run log, sealed ([flight records](observability/operator.md#flight-records)). The [Cloud Doctor](doctor.md) reads them every night.

This builds on the events, usage and budgets in [activity.md](activity.md), and covers the observability parts of [spec 0005](specs/0005-traces-and-decision-model.md) and [spec 0008 §5.7](specs/0008-swarm-improvement-loop-and-rl.md). It does not include 0005's in-process OTLP collector, `traces.db` or decision model.

## Metrics

`GET /metrics` returns the Prometheus text format.

- **Who can read it.** An admin token, or a token that can read metrics and nothing else: `hangar token create prometheus --scope metrics`.
- **Turning it off.** `HANGAR_METRICS_ENABLED=false` turns `/metrics` off (404). hangar then never imports `prometheus_client`.

| Metric | Type | Labels |
| --- | --- | --- |
| `hangar_sandboxes` | gauge | `state`, `provider` |
| `hangar_sandbox_create_seconds` | histogram | `provider`, `environment`, `result` (`ok`, `error`). Time to a running sandbox, with image build, start, setup and clone. |
| `hangar_warm_pool_claims_total` | counter | `provider`, `environment`, `result` (`hit`, `miss`). Only for environments with a warm pool. |
| `hangar_sessions` | gauge | `state` |
| `hangar_jobs` | gauge | `state`, `reason` (the outcome: `over_budget`, `verify_failed`, `agent_failed`…) |
| `hangar_job_retries_total` | counter | `mode`: `same` (the deck's Retry, a canned follow-up) or `fresh` (Start over, a new job) ([working with the agent](jobs/working-with-the-agent.md#5-work-with-the-agent)) |
| `hangar_turn_seconds` | histogram | `harness`, `outcome`. The agent's time, start to finish. |
| `hangar_tokens_total` | counter | `kind` (`input`, `output`, `cache_read`, `cache_write`), `model`, `harness`, `environment`, `requester` |
| `hangar_cost_usd_total` | counter | `model`, `harness`, `environment`, `requester` |
| `hangar_unpriced_turns_total` | counter | `model`, `harness`. Turns whose model has no price; their cost is unknown, not 0. |
| `hangar_budget_stops_total` | counter | `harness`, `environment` |
| `hangar_cost_uncached_usd_total` | counter | `model`, `harness`, `environment`. What priced turns would have cost with no prompt cache; minus `hangar_cost_usd_total`, what caching saved ([sessions.md](sessions/prompt-caching.md)). |
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
| `hangar_loop_last_tick_timestamp`, `hangar_loop_failures_total` | gauge, counter | `loop` (`reaper`, `reconciler`, `sessions`, `schedules`, `backups`, `ops`, `egress`, `cloud-tokens`). When each background loop last finished a pass, and passes that raised. Alert when `time() - hangar_loop_last_tick_timestamp` passes three of the loop's intervals (reaper 60 s, reconciler 30 s, sessions 60 s, schedules `HANGAR_SCHEDULE_POLL_SECONDS`, backups `HANGAR_BACKUP_HOURS`); the gauge starts empty after a restart. |
| `hangar_job_starts_total`, `hangar_job_queue_seconds`, `hangar_jobs_stuck` | counter, histogram, gauge | `result` (`ok`, `error`) on starts. The job-start SLO signals and the stuck-job alert ([SLOs](slos.md#job-starts), [a stuck job](runbooks/stuck-job.md)) |
| `hangar_jobs_awaiting_capacity` | gauge | Queued jobs held for [review capacity](team-scale.md#review-capacity); no person label |
| `hangar_eval_observed_tpm`, `hangar_eval_tpm_budget`, `hangar_eval_jobs_waiting`, `hangar_eval_window_open` | gauge | `provider` on the first two, `reason` on waiting jobs ([the eval queue](team-scale.md#the-eval-queue-class)) |
| `hangar_governance_fail_closed_total`, `hangar_governance_disagreements_total` | counter | `kind`, plus `failure` on fail-closed. Tower's answers hangar had to make itself, and shadow disagreements ([Tower](runbooks/tower.md)) |
| `hangar_deploy_lag_commits`, `hangar_deploy_lag_working_days`, `hangar_deploy_lag_over` | gauge | How far production is behind main; -1 while unknown ([a deploy waiting too long](runbooks/deploy.md)) |
| `hangar_outbox_pending` | gauge | Run capture objects not uploaded yet ([run capture](observability/operator.md#run-capture)) |
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

The endpoint, scopes, collector install and roles were checked against Google's docs on 2026-10-13: the [Telemetry API `v1.traces`](https://docs.cloud.google.com/stackdriver/docs/reference/telemetry/v1.traces.post) and the collector page above. hangar's spans can also go to any other OTLP backend (Tempo, Jaeger, Honeycomb, Grafana Cloud…) through `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS`. None of those has been tested.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `_TRACES_ENDPOINT` / `OTEL_TRACES_EXPORTER` | unset | Turn tracing on (see above). Every other standard `OTEL_*` variable applies. |
| `HANGAR_OTEL_SANDBOX_ENDPOINT` | unset | OTLP/HTTP base URL that sandboxes export Claude Code and Codex telemetry to. A plain `http(s)` URL without credentials. |
| `HANGAR_METRICS_ENABLED` | `true` | `GET /metrics` |
| `HANGAR_TRACE_LIVE_TOOLS` | `false` | With tracing on, also send each turn's tool, subagent and approval spans ([traces](observability/traces.md#each-tool-call-and-traces-as-files)) |
| `HANGAR_DOCTOR_SCHEDULE` | `30 0 * * *` | When the [nightly pass](#the-nightly-pass) runs (credential checks, the Cloud Doctor); `off` turns it off |
| `HANGAR_DOCTOR_TIME_ZONE` | the power schedule's | The schedule's IANA time zone (UTC when there's no power schedule) |
| `HANGAR_OUTBOX_URL` | unset | [Run capture](observability/operator.md#run-capture): `gs://bucket[/prefix]` or a local directory ([outbox.md](api/outbox.md)) |
| `HANGAR_OUTBOX_SESSIONS` | `all` | `jobs_only` leaves out sessions not attached to a job |
| `HANGAR_EXPORT_GRACE_DAYS` | `30` | How long a deleted session's event log and manifests wait for their export |
| `HANGAR_DETERMINISM_SCHEDULE` | `0 21 * * *` | When the [determinism report](observability/operator.md#the-determinism-report)'s replays start, in `HANGAR_EVAL_TZ`; `off` turns it off |
| `HANGAR_DETERMINISM_RUNS_PER_REPO` | `50` | Previous-day turns replayed per repo (at most 200) |
| `HANGAR_DETERMINISM_MAX_REPOS` | `10` | Repos replayed per night, the busiest first |
| `HANGAR_DETERMINISM_REPLAY_BUDGET_USD` | `1` | Each replay's job budget (sandbox time only) |

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="traces"></span>Traces | [Traces](observability/traces.md) |
| <span id="what-is-traced"></span>What is traced | [Traces](observability/traces.md#what-is-traced) |
| <span id="into-the-sandbox-the-agents-own-telemetry"></span>Into the sandbox: the agents' own telemetry | [Traces](observability/traces.md#into-the-sandbox-the-agents-own-telemetry) |
| <span id="your-mcp-clients-trace"></span>Your MCP client's trace | [Traces](observability/traces.md#your-mcp-clients-trace) |
| <span id="each-tool-call-and-traces-as-files"></span>Each tool call, and traces as files | [Traces](observability/traces.md#each-tool-call-and-traces-as-files) |
| <span id="the-operator-console-hangar-status"></span>The operator console: `hangar status` | [The operator console, flight records and run capture](observability/operator.md#the-operator-console-hangar-status) |
| <span id="slos"></span>SLOs | [The operator console, flight records and run capture](observability/operator.md#slos) |
| <span id="flight-records"></span>Flight records | [The operator console, flight records and run capture](observability/operator.md#flight-records) |
| <span id="run-capture"></span>Run capture | [The operator console, flight records and run capture](observability/operator.md#run-capture) |
| <span id="the-determinism-report"></span>The determinism report | [The operator console, flight records and run capture](observability/operator.md#the-determinism-report) |
| <span id="credential-health"></span>Credential health | [The operator console, flight records and run capture](observability/operator.md#credential-health) |
