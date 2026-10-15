# Trace export: `hangar.trace/v1`

Hangar exports what an agent did as OpenTelemetry traces in OTLP/JSON, so tools outside hangar can read runs without
knowing hangar's internals: the sister tool's trace timelines and behavior specs, an OTLP collector, Jaeger, Tempo or
Phoenix. This page is the contract. It is versioned: a change that removes or renames anything below is a new version
(`hangar.trace/v2`); adding a kind, an attribute or an event is not.

The boundary and the other two interfaces (the effective harness manifest and experiments) are in
[spec 0014](../specs/0014-sister-tool-interfaces.md).

## Getting a trace

| What | REST | CLI | MCP resource |
| --- | --- | --- | --- |
| One run (one turn of a session) | `GET /api/sessions/{name}/trace?turn=N` | `hangar trace export <session> --turn N` | |
| A whole session | `GET /api/sessions/{name}/trace` | `hangar trace export <session>` | `hangar://sessions/{name}/trace` |
| A job: its turns, best-of-N attempts, judge, verify, git, PR and CI | `GET /api/jobs/{id}/trace` | `hangar trace export <job id>` | `hangar://jobs/{id}/trace` |
| An experiment trial (stored when the trial ended) | `GET /api/experiments/{id}/trials/{key}/trace` | `hangar experiment export <id>` | |

`-o FILE` writes the export to a file. Every export is one OTLP `ExportTraceServiceRequest` in the
[OTLP/JSON encoding](https://github.com/open-telemetry/opentelemetry-proto/blob/v1.11.1/docs/specification.md#json-protobuf-encoding)
(opentelemetry-proto 1.11.1): ids are hex strings, `kind` and status codes are integers, 64-bit integers and
timestamps are decimal strings. It can be POSTed as is to a collector's `/v1/traces` with
`Content-Type: application/json`.

**Access.** Exports are owner-scoped like the records they come from (#88): a member gets their own sessions and jobs,
an admin everyone's, a `jobs` token only the jobs it submitted. A record the caller may not see answers 404, as a
missing one does.

**Secrets.** Every string in an export passes through the broker's redaction (`broker.redact`) when it is exported,
on top of the redaction applied when events are recorded. Credential values never appear: hangar never records them
in events, and a stored secret that turns up in a command, a tool's output or a message is masked (`***`).
Tool inputs, outputs and messages are clipped (500, 500 and 2,000 characters), as the event log keeps them.

**Live export.** Separately from these files, a control plane with tracing on (`OTEL_EXPORTER_OTLP_ENDPOINT`,
[observability.md](../observability.md)) already sends each turn's `invoke_agent` span to the collector. With
`HANGAR_TRACE_LIVE_TOOLS=true` it also sends the turn's `tool.call`, `subagent` and `approval` spans, as children of
that span, when the turn ends. They carry the attributes below; the session, job and event records are only in the
exported files.

## Conventions followed

- **OpenTelemetry GenAI semantic conventions** for agent and tool spans: `invoke_agent`, `execute_tool` and
  `invoke_workflow` operations, `gen_ai.*` attributes. Target: the
  [semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai) registry at commit
  `f3c6d0c` (2026-10-14), schema `https://opentelemetry.io/schemas/gen-ai-dev/1.42.0-dev`, built on core semantic
  conventions **1.44.0**. **Stability: development.** Every `gen_ai.*` attribute and GenAI span used here is marked
  *Development* in that registry, so upstream may still rename them; hangar pins this version and will move to a later
  one only with a new `hangar.trace` version. `error.type` is stable; `process.exit.code` is release candidate.
- The GenAI conventions moved out of the core repository in 2026: opentelemetry.io's GenAI pages now point to the
  separate registry above.
- Everything GenAI has no name for is under **`hangar.*`**, listed below.
- Each export's scope is `{"name": "hangar.trace", "version": "1"}` with `schemaUrl`
  `https://opentelemetry.io/schemas/1.44.0`; the resource carries `hangar.trace.schema = "hangar.trace/v1"` and
  `hangar.semconv.genai = "gen-ai-dev/1.42.0-dev"`.

## The taxonomy

Behavior specs match on this. Every **span** has a `hangar.kind` attribute; every **span event**'s name is its kind.
Spans have a duration; events are points in time. Kinds are stable within `hangar.trace/v1`.

### Spans

| `hangar.kind` | Span name | `gen_ai.operation.name` | Parent | What it is |
| --- | --- | --- | --- | --- |
| `session` | `invoke_workflow hangar.session` | `invoke_workflow` | root | A session export's root: first turn's start to last turn's end. |
| `job` | `invoke_workflow hangar.job` | `invoke_workflow` | root | A job export's root, from submission to its last event. |
| `turn` | `invoke_agent {harness}` | `invoke_agent` | `session` or `job` (or root, in a one-run export) | One agent run: a turn of a session or job. |
| `tool.call` | `execute_tool {tool}` | `execute_tool` | `turn` | One tool call, from the call to its result. |
| `subagent` | `invoke_agent {subagent type}` | `invoke_agent` | `turn` | A subagent the agent started (Claude Code's `Task`/`Agent` tool). Its start is the subagent's start. |
| `approval` | `approval {tool}` | — | `turn` | A tool permission asked during a live turn, from the request to the answer. |

### Normalized tool kinds

`tool.call` spans carry `hangar.tool.kind`, the same for every harness:

| `hangar.tool.kind` | Claude Code | Codex | pi | Antigravity |
| --- | --- | --- | --- | --- |
| `shell` | `Bash`, `BashOutput` | `shell` (command_execution) | `bash` | `run_command` |
| `edit` | `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `apply_patch` (file_change) | `write`, `edit` | `write_to_file`, `replace_file_content`, `multi_replace_file_content` |
| `read` | `Read`, `NotebookRead`, `LS` | | `read`, `ls` | `view_file`, `view_file_outline`, `list_dir` |
| `search` | `Grep`, `Glob` | | `grep`, `find` | `grep_search`, `find_by_name`, `codebase_search` |
| `web` | `WebFetch`, `WebSearch` | `web_search` | | `read_url_content`, `search_web` |
| `mcp` | `mcp__{server}__{tool}` | `{server}.{tool}` (mcp_tool_call) | | `mcp_*` |
| `skill` | `Skill` | | | |
| `todo` | `TodoWrite` | | | |
| `other` | anything else | | | |

A `subagent` call is a `subagent` span, not a `tool.call`.

### Events

| Name (= kind) | On | Attributes |
| --- | --- | --- |
| `message` | `turn` | `hangar.text` (the assistant's text, clipped) |
| `progress` | `turn` | `hangar.text`, `hangar.source` (`harness` or `hangar`) |
| `error` | `turn` | `hangar.text`, `hangar.fatal` |
| `file.change` | `turn` | `hangar.file.path`, `hangar.file.op` (`write`, `create`, `update`, `add`, `delete`...), `gen_ai.tool.name` |
| `steer` | `turn` | `hangar.text`, `hangar.requester`, `hangar.steer.status` |
| `interrupt` | `turn` | `hangar.text`, `hangar.requester`, `hangar.steer.status` |
| `verify` | `turn` (the in-turn gate) or `job` (hangar's check after the turn) | `hangar.verify.phase` (`in_turn`, `after_turn`), `hangar.verify.passed`, `hangar.verify.exit_code`, `hangar.verify.round`, `hangar.verify.command` (job only), `hangar.verify.new_failures` (job only) |
| `question` | `turn` | `hangar.text`: the agent asked a person (the job waits for an answer) |
| `git.commit` | `job` | `vcs.ref.head.revision` (the commit), `hangar.git.subject` |
| `git.push` | `job` | `vcs.ref.head.name` (the branch), `vcs.ref.head.revision`, `hangar.git.pushed` |
| `pr.open` | `job` | `hangar.pr.number`, `hangar.pr.url`, `hangar.pr.draft` |
| `ci.check` | `job` | `hangar.ci.name`, `hangar.ci.conclusion`, `vcs.ref.head.revision` |
| `pr.merge` | `job` | `hangar.pr.number`, `hangar.pr.merged_by` |
| `job.state` | `job` | `hangar.job.state`, `hangar.text`: each entry of the job's timeline |

Reserved for test results (the `testresults` work): `test.run` (one run of a test command: totals) and `test.case`
(one test's outcome), as events on `job` or `turn`, with attributes under `hangar.test.*`.

## Attributes

### Resource

| Attribute | Value |
| --- | --- |
| `service.name` | `hangar` |
| `service.version` | hangar's version |
| `hangar.trace.schema` | `hangar.trace/v1` |
| `hangar.semconv.genai` | `gen-ai-dev/1.42.0-dev` |
| `hangar.export.scope` | `run`, `session` or `job` |
| `hangar.session.id`, `hangar.session.name` | session exports |
| `hangar.job.id` | job exports, and sessions that carry a job |
| `hangar.environment` | the environment |
| `hangar.person` | whose work it is |

### `turn`

GenAI: `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name` (the harness), `gen_ai.provider.name` (`anthropic`
for Claude Code, `openai` for Codex, `gcp.gemini` for Antigravity; unset for pi, which routes to many),
`gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.conversation.id` (the harness's conversation id),
`gen_ai.usage.input_tokens` (all input, cached included, as the convention says), `gen_ai.usage.output_tokens`,
`gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens`, `error.type` when it failed.

Hangar: `hangar.turn.number` (1-based, in the session), `hangar.run.id`, `hangar.session.id`, `hangar.turn.mode` (`new`,
`resume`, `handoff`, `fork`), `hangar.turn.outcome` (`exited`, `timed_out`, `cancelled`, `over_budget`, `gone`,
`error`, `needs_input`, `interrupted`), `hangar.turn.exit_code`, `hangar.harness`, `hangar.harness.version`,
`hangar.turn.live`, `hangar.approvals` (`auto`, `ask`, `deny-risky`), `hangar.cost_usd`, `hangar.cost_source`
(`harness`, `prices`, `mixed`), `hangar.manifest.digest` (the effective harness manifest it ran with: fetch it with
`GET /api/sessions/{name}/manifest?turn=N`), `hangar.attempt` (best-of-N attempt number, job exports).

Status: `ERROR` when the outcome is anything but `exited`/`needs_input`/`interrupted` with exit code 0.

### `tool.call`

GenAI: `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name` (as the harness names it), `gen_ai.tool.call.id`,
`gen_ai.tool.type` (`extension` for `mcp` and `web`, `function` for the rest: the harness runs them),
`gen_ai.agent.name`, `gen_ai.conversation.id`; for `shell`, `process.exit.code` when the harness reports it.

Hangar: `hangar.tool.kind`, `hangar.tool.input` (a one-line summary: the command, the path, the pattern or the URL;
clipped, redacted), `hangar.tool.output` (the start of the result; clipped, redacted), `hangar.tool.ok`,
`hangar.file.path` (edit and read), `hangar.mcp.server` and `hangar.mcp.tool` (mcp), `hangar.tool.incomplete=true`
when the turn ended before the result (the span then ends with the turn). Status `ERROR` with
`error.type=tool_error` when the tool failed.

### `subagent`

`gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name` (the subagent type), `gen_ai.tool.call.id`,
`hangar.tool.input` (its task description), `hangar.tool.output`, `hangar.tool.ok`.

### `approval`

`hangar.approval.id`, `hangar.approval.tool`, `hangar.approval.detail` (what it asked to run; clipped, redacted),
`hangar.approval.status` (`allowed`, `allowed_always`, `denied`, `cancelled`, `pending`), `hangar.approval.by` (who
answered: a person, `policy`, `timeout` or `harness`), `hangar.approval.message`.

### `session` and `job`

`gen_ai.operation.name=invoke_workflow`, `gen_ai.workflow.name` (`hangar.session` or `hangar.job`),
`hangar.environment`, `hangar.harness`, `gen_ai.request.model`, totals over all turns (`gen_ai.usage.*`,
`hangar.cost_usd`), and for jobs `hangar.job.state`, `hangar.job.reason`, `hangar.job.strategy`, `hangar.repo`,
`hangar.diff.stat` (the delivered diff's stat), `hangar.pr.url`.

## Ids

Ids are derived, not random, so the same record exports the same ids every time: the trace id is the first 16 bytes
of `sha256("hangar:" + job id or session id)`, and span ids come from the same hash over the span's own key (the run
id, the tool call id). A per-run export uses its session's trace id. Live export uses the SDK's own ids.

## Limits

An export stops at 20,000 spans and events; `hangar.export.truncated=true` on the resource says it did. A session's
event log keeps the last `HANGAR_EVENTS_MAX_MB` (20 MB, two files) of events: older turns of a very long session have
no tool spans left.

Span status is set only on failure (`code: 2`, `ERROR`); a span that succeeded leaves it unset, as the conventions
recommend. Event and tool-call times are when hangar read the line from the agent's run log: within a second when the
log is followed live, else up to the poll interval (`HANGAR_SESSION_POLL_SECONDS`), so tool durations are close, not
exact. Turn start and end times are exact.

## Example

```json
{"resourceSpans": [{
  "resource": {"attributes": [
    {"key": "service.name", "value": {"stringValue": "hangar"}},
    {"key": "hangar.trace.schema", "value": {"stringValue": "hangar.trace/v1"}},
    {"key": "hangar.export.scope", "value": {"stringValue": "run"}}]},
  "scopeSpans": [{
    "scope": {"name": "hangar.trace", "version": "1"},
    "schemaUrl": "https://opentelemetry.io/schemas/1.44.0",
    "spans": [
      {"traceId": "5b8efff798038103d269b633813fc60c", "spanId": "eee19b7ec3c1b174", "name": "invoke_agent claude-code",
       "kind": 1, "startTimeUnixNano": "1760530000000000000", "endTimeUnixNano": "1760530090000000000",
       "attributes": [{"key": "hangar.kind", "value": {"stringValue": "turn"}},
                      {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                      {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "1830"}}],
       "events": [{"timeUnixNano": "1760530080000000000", "name": "file.change",
                   "attributes": [{"key": "hangar.file.path", "value": {"stringValue": "src/app.py"}}]}]},
      {"traceId": "5b8efff798038103d269b633813fc60c", "spanId": "aa19b7ec3c1b1740",
       "parentSpanId": "eee19b7ec3c1b174", "name": "execute_tool Bash", "kind": 1,
       "startTimeUnixNano": "1760530010000000000", "endTimeUnixNano": "1760530012000000000",
       "attributes": [{"key": "hangar.kind", "value": {"stringValue": "tool.call"}},
                      {"key": "hangar.tool.kind", "value": {"stringValue": "shell"}},
                      {"key": "gen_ai.tool.name", "value": {"stringValue": "Bash"}},
                      {"key": "hangar.tool.input", "value": {"stringValue": "pytest -q"}}]}]}]}]}
```
