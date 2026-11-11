# Importing a Claude Code session

`hangar trace import` turns a Claude Code session that hangar didn't run into hangar's own records. It reads a
transcript on disk, runs nothing and sends nothing. The result reads like any session hangar ran, so flightdeck and
the trace compiler take it as is.

```sh
hangar trace import ~/.claude/projects/-home-me-repo/<session>.jsonl --out analysis/<session>/
```

It writes two files into `--out`, and prints a count of events by kind and spans by tool kind:

| File | What it holds |
| --- | --- |
| `events.jsonl` | One event per line, numbered by `seq` from 1, in the shape `GET /api/sessions/{name}/events` returns. The trace compiler reads it with its `hangar-events/v1` profile. |
| `trace.json` | A `hangar.trace/v1` session export ([the contract](traces.md)): one `session` span, one `turn` per prompt. |

## What it reads

It tells the shapes apart line by line:

| Input | Where it comes from |
| --- | --- |
| A local transcript | `~/.claude/projects/<dir>/<session>.jsonl`. Its subagents' transcripts in `<session>/subagents/` are read too. |
| A cloud session's events | A JSON array or JSONL of `{"created_at", "assistant": {"uuid", "internal_anthropic_catchall": {...}}}` objects. |
| stream-json | What `claude -p --output-format stream-json --verbose` prints. |

Each line becomes the stream-json line it stands for. hangar's own Claude Code parser then reads it. So the events
are the ones a hangar run of the same session records, with the same clipping. Shell calls carry `data.ops` from the
whole command, and clipped calls carry `data.clipped`.

## Turns and subagents

A turn starts at each prompt: a top-level `user` line that answers no tool call. Meta lines, compact summaries and
interrupt notes start none. A run started without a prompt, such as a wake, stays in the turn before it.

Each prompt is a `message` event with `data.role` `user`. Its span event carries `hangar.message.role`. A turn's
run id is `<session id>/turn/<n>`.

A subagent's events carry `data.parent_id`, the id of the `Task` or `Agent` call that started it. They belong to
that call's turn, even when it ran in the background. In the trace, its tool calls and model calls are children of
its `subagent` span, with `hangar.subagent.call.id`.

## Usage, per model call and per turn

Every model response is a `usage` event. Beside the running totals, it carries its own `call_input_tokens`,
`call_output_tokens`, `call_cache_read_tokens`, `call_cache_write_tokens` and its 5-minute and 1-hour parts. It also
carries `call_cost_usd`, priced from hangar's table, and `message_id`.

In the trace each response is a `model.call` span named `chat {model}`. It holds the GenAI usage attributes,
`gen_ai.response.id` and `hangar.cost_usd`. A tool call's `hangar.response.id` names the response that asked for it.
Spend can then be put on a step.

A turn's usage comes from its `result` lines when the input has them: Claude Code's own totals for each run.
`total_cost_usd` there is the process's running total, so each run's cost is the difference from the last one.
`hangar.usage.source` says which was used: `result` or `calls`.

A cloud session writes a message's usage while it is still streaming, so its output count is short. Such a span has
`hangar.usage.partial`. `hangar.usage.calls.output_tokens` on the turn shows the gap. A local transcript has no
`result` lines; its per-call usage is final.

## Secrets

Strings pass through hangar's secret patterns, as its outgoing text does, and a match is masked (`***`). There are
no stored credentials offline to mask. A transcript holds whatever the session saw, so keep its output as private
as the transcript.
