# Traces

This page is part of [Observability: traces, metrics, cost](../observability.md).

Tracing is off unless the standard OpenTelemetry variables name an exporter. Set one of these on the control plane:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318   # OTLP/HTTP; the SDK appends /v1/traces
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces   # or traces only
OTEL_TRACES_EXPORTER=console                              # spans on stdout, for a quick look
```

Notes on the settings:

- **Nothing set means off.** This differs from the SDK, which defaults to OTLP on localhost. `OTEL_SDK_DISABLED=true` or `OTEL_TRACES_EXPORTER=none` also turn tracing off.
- **The SDK is an extra.** It comes with `pip install 'hangar[otel]'` and is part of `hangar[all]`, which the control-plane image installs.
- **Off costs nothing.** hangar doesn't import the OpenTelemetry SDK or exporter, and each hook costs one global check. A test asserts both.
- **Protocol.** OTLP goes over HTTP/protobuf. `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` works if `opentelemetry-exporter-otlp-proto-grpc` is installed; `http/json` isn't supported.
- **Standard variables.** The SDK reads `OTEL_EXPORTER_OTLP_HEADERS`, `_TIMEOUT`, `_CERTIFICATE`, `OTEL_SERVICE_NAME` (default `hangar`), `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_TRACES_SAMPLER` / `_ARG` and `OTEL_BSP_*` as usual.
- **hangar's own TracerProvider.** hangar doesn't install a process-wide provider. As a result, FastAPI's built-in telemetry (FastAPI ≥ 0.143) stays off and requests aren't traced twice. Don't set `FASTAPI_OTEL_AUTO_CONFIGURE=true`: that would add FastAPI's own request spans, and logs that include exception messages.
- **A failed export never hurts the work.** If the collector is down, spans are dropped.

## What is traced

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
| `sandbox.exec`, `sandbox.read_file`, `sandbox.write_file`, `sandbox.list_files` | internal | only inside another span | `hangar.exec.exit_code`, `hangar.exec.transport` (`envd` or `exec`, see [envd.md](../envd.md)), `hangar.file.bytes`. Commands and file contents are never recorded. |
| `checkpoint.capture`, `checkpoint.restore` | internal | the turn, boot or suspend | `hangar.checkpoint.reason`, `.stored`, `.bytes` |
| `db.backup`, `db.restore_stage` | internal | root, or `hangar admin`'s request | A database backup and a staged restore (gke.md §7): `hangar.backup.name`, `.db_bytes`, `.stored_bytes` |
| `invoke_agent <harness>` | internal | `job.process`, or the request that sent the message | One per agent turn, from start until hangar has collected its result and checkpointed. GenAI semantic conventions: `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name` (the harness), `gen_ai.provider.name` (`anthropic` for Claude Code, `openai` for Codex, `gcp.gemini` for Antigravity), `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.conversation.id`, `gen_ai.usage.input_tokens` (cached tokens included, as the convention says), `.output_tokens`, `.cache_read.input_tokens`, `.cache_write.input_tokens`. hangar adds `hangar.cost_usd`, `.cost_source`, `.turn.outcome`, `.turn.exit_code`, `.run.id`, `.session.id`, `.job.id`. A turn that timed out, went over budget or lost its sandbox is an error span. |

Failed spans carry `error.type`: the exception class, `cancelled`, `tool_error`, or the turn's outcome. Their status message is the error, with stored credential values masked (`broker.redact`). Exceptions and stack traces are not recorded as span events.

Housekeeping makes as few traces as it can:

- Idle checks and the reaper don't open spans of their own.
- An interval checkpoint is one small trace.
- Exec and file I/O are only traced inside another span.

The GenAI and MCP attribute names were checked against the OpenTelemetry semantic conventions on 2026-10-13. The GenAI conventions now live in the [semantic-conventions-genai repository](https://github.com/open-telemetry/semantic-conventions-genai); see its `gen-ai-agent-spans.md` and `mcp.md`. They are still *Development* status, so names may change.

## Into the sandbox: the agents' own telemetry

Every agent run gets the `TRACEPARENT` of its `invoke_agent` span in its environment. The trace flags are reduced to the sampled bit: codex 0.162.1 rejects the W3C level-2 "random" flag (`03`) with "TRACEPARENT is set but invalid; ignoring trace context".

With **`HANGAR_OTEL_SANDBOX_ENDPOINT`** set to an OTLP/HTTP collector that sandboxes can reach, the harnesses also export their own telemetry under hangar's span:

| Harness | How hangar wires it |
| --- | --- |
| Claude Code | Per run, these variables: `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` (traces are beta), `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` and `OTEL_EXPORTER_OTLP_ENDPOINT`. In `-p` mode Claude Code reads `TRACEPARENT`, so its `claude_code.interaction` span is a child of hangar's turn. |
| Codex | Codex takes exporters only from the user-level `~/.codex/config.toml`. hangar writes a managed `[otel]` block there when the sandbox starts: `exporter`, `trace_exporter` and `metrics_exporter`, each `{ otlp-http = { endpoint = "<endpoint>/v1/{logs,traces,metrics}", protocol = "binary" } }`, with `environment = "hangar"`. Without that block, `metrics_exporter` defaults to `statsig`. If the environment's own config already has `[otel]`, hangar leaves it alone and adds a warning to the sandbox. |
| pi, Antigravity | `TRACEPARENT` only. Neither has OTel export that hangar can configure. |

Every harness also gets `OTEL_RESOURCE_ATTRIBUTES` with `hangar.sandbox.id`, `hangar.run.id`, `hangar.environment` and `hangar.harness`. A sandbox can set its own resource attributes, so treat these as labels for grouping, not as proof of where a span came from.

No headers or credentials go into sandboxes, so the collector must accept unauthenticated OTLP from the sandbox network. On GKE that means the in-cluster collector plus the NetworkPolicy in `deploy/k8s/observability/otel.yaml`.

**Verified on 2026-10-13, offline.** Claude Code 2.1.296 and codex 0.162.1 were run as hangar runs them, against the scripted model in `tests/fixtures/streams/capture` and a local OTLP receiver:

- **Claude Code** exported traces, logs and metrics. Its `claude_code.interaction` span had hangar's trace id and parent span. This held with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, which hangar always sets, so that flag doesn't block export to your own collector.
- **Codex** exported logs, traces and metrics with the block above. `codex.exec` and `turn/start` were children of hangar's span.
- **Volume.** A short Codex run sends about 800 spans, mostly `fs.*`. Sample or filter at the collector if that is too many.

## Your MCP client's trace

hangar continues an incoming `traceparent` header on REST and MCP requests (tested over streamable HTTP), so hangar's `tools/call` span sits under the client's span.

Claude Code sends that header on HTTP MCP requests when its tracing beta is on. Per its docs, that is only when `ANTHROPIC_BASE_URL` is unset or points at the Anthropic API, unless `CLAUDE_CODE_PROPAGATE_TRACEPARENT=1` is set. With both ends exporting, one trace runs from the laptop agent, through hangar, into the cloud agent's sandbox.

## Each tool call, and traces as files

With `HANGAR_TRACE_LIVE_TOOLS=true`, each turn's tool calls, subagents and approvals also go to the collector when the turn ends, as children of its `invoke_agent` span (`execute_tool {tool}`, with the normalized `hangar.tool.kind`). The same spans, with the session, job and their events (steers, verify, commits, PRs, CI), can be exported as OTLP/JSON files at any time: `hangar trace export <session or job>`, `GET /api/sessions/{name}/trace`, `GET /api/jobs/{id}/trace`. The schema and its taxonomy are a versioned contract: [docs/api/traces.md](../api/traces.md).
