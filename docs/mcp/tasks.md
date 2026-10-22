# Tasks, typed results and hints

How hangar's long tools run as MCP tasks for a client that asks, and what its typed results and tool hints promise.
The servers and tools are on [using MCP](../mcp.md).

## Long tools as tasks

Some tools return a task handle at once instead of their usual result, for a client that declares the
[tasks extension](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/2663-tasks-extension.md)
on a call. They are `submit_job`, `follow_up_job`, `new_session` and `send_to_session` (with a message),
`fork_session` (with a message) and `run_agent`.

```jsonc
// tools/call submit_job, with "io.modelcontextprotocol/tasks" in the request's clientCapabilities.extensions
{"resultType": "task", "taskId": "task-5f0c…", "status": "working", "statusMessage": "submit_job started",
 "createdAt": "2026-10-13T06:40:00Z", "lastUpdatedAt": "2026-10-13T06:40:00Z", "ttlMs": 86400000, "pollIntervalMs": 5000}
```

Poll `tasks/get` (`status` is `working`, `input_required`, `completed` or `cancelled`). `statusMessage` carries the
job's latest step or the agent's latest event.

When the turn ends, `completed` carries what the tool would have returned, plus the turn under `turn` (summary, PR,
structured result, artifacts). It also has resource links to the job or session and its artifacts. A failed job is
`completed` with `isError: true`.

If the agent asks a question and the client declared elicitation, the task turns `input_required`. The question is in
`inputRequests` (keys like `answer-2`, never reused). Answer with `tasks/update`:

```json
{"method": "tasks/update", "params": {"taskId": "task-5f0c…",
  "inputResponses": {"answer-2": {"action": "accept", "content": {"answer": "Use the streaming parser"}}}}}
```

The agent continues the same conversation and the task goes back to `working`. Declining completes the task with the
question in the result. A client without elicitation gets `completed` with the question.

`tasks/cancel` cancels a job or stops an agent run. For a session turn it only stops the task (the turn keeps running;
`suspend_session` stops it).

A task belongs to the token that created it: any other caller gets "unknown task". `tasks/get`, `tasks/update` and
`tasks/cancel` need the extension declared on the request too (`-32021` otherwise, as SEP-2663 requires).

Tasks are stored in the control plane's database and derive their state from the job or session, so they survive a
restart. They expire 24 hours after creation.

## Typed results and hints

The main tools publish an `outputSchema`, and their results carry `structuredContent` (with the same JSON as text for
older clients). They are jobs (`JobOut`), sessions (`SessionOut`, `ForkOut`), turns (`TurnOut`), sandboxes
(`SandboxOut`), events (`EventsOut`), `exec` and agent runs. The declared fields are what you can rely on; results keep
every other field of the view.

Every tool is annotated, and clients use these to decide when to ask before calling:

- `readOnlyHint` for the get/list/read tools;
- `destructiveHint` for destroy, cancel, delete, `exec` and file writes;
- `idempotentHint` where repeating is harmless;
- `openWorldHint` for tools whose effects leave hangar (agents that push branches and open PRs, commands with
  network).

The listing is compact: descriptions without docstring indentation, input schemas without pydantic's generated titles
or `anyOf [T, null]` wrappers. The part models read on every turn (names, descriptions, input schemas) went from
31.2 KB to 22.4 KB.

`validate_environment`, `clone_environment` and `destroy_sandboxes` were merged into `create_environment(dry_run)` and
`destroy_sandbox`. The old names are no longer listed but still answer with their old arguments and results, so
existing scripts keep working.
