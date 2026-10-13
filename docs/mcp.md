# Using hangar from an MCP client

Claude Code, Codex, Cursor, VS Code and other agents are hangar's main users. This page covers what hangar's MCP server gives them beyond plain tool calls, and how each piece looks on the wire. To connect a client, see [clients.md](clients.md).

Hangar speaks every protocol revision its SDK does: the `initialize`-handshake revisions up to **2025-11-25**, and the stateless **2026-07-28** revision (no handshake, capabilities on every request). One endpoint, `<public_url>/mcp`, serves both; each request is routed by its `MCP-Protocol-Version` header.

## Which client gets what

| Feature | 2026-07-28 clients | 2025-era clients |
| --- | --- | --- |
| Typed results (`outputSchema` / `structuredContent`), tool hints, resource links | yes | yes |
| An agent's question put to you (elicitation) | the question comes back as an `input_required` result and the client retries with the answer | an `elicitation/create` request on the client's session |
| Long tools as tasks | with the `io.modelcontextprotocol/tasks` extension | no: the plain result, then `wait_for_turn` |
| Follow a job or session without polling | `subscriptions/listen` | `resources/subscribe` |
| Workflow prompts | yes | yes |

What the major clients support today, from their own sources (October 2026):

- **Claude Code** (2.1.296 changelog): negotiates 2026-07-28 with HTTP servers by default, form and URL elicitation, `structuredContent`, `resource_link`, tool annotations, prompts, resources, `subscriptions/listen`. Its changelog does not mention the tasks extension.
- **Codex** (`codex-rs/rmcp-client`): 2025-06-18 by default (2026-07-28 is opt-in), form elicitation, `structuredContent`; it skips the approval prompt for tools marked `readOnlyHint`, or `destructiveHint: false` with `openWorldHint: false`.
- **VS Code** (`src/vs/workbench/contrib/mcp`): 2025-11-25, elicitation, `structuredContent`, `resource_link`, `resources/subscribe`, and the *experimental 2025-11-25 tasks* (`tasks/result`). Those tasks were removed from the core spec in 2026-07-28 and from the Python SDK v2 hangar runs on, so VS Code gets plain calls.
- **Cursor**: not verified (closed source); the official extension matrix lists MCP Apps only.

## Waiting for work, and answering the agent

`wait_for_turn(ref)` waits for a job's or session's current turn (up to 10 minutes per call) and sends progress notifications while it runs. It returns the turn: `outcome`, `summary`, the structured `result`, `artifacts` (as `resource_link` blocks too), the PR for a job.

When the agent stops on a question (`hangar-agent ask`, state `needs_input`), hangar asks you instead of returning:

- **2026-07-28**: the call returns `resultType: "input_required"` with an elicitation. Your client shows the form, then retries the same call with your answer; hangar sends it to the agent as the next message, in the same conversation, and keeps waiting.

  ```json
  {"resultType": "input_required",
   "inputRequests": {"answer-1": {"method": "elicitation/create", "params": {"mode": "form",
     "message": "Session csv-fix asks: Use the streaming parser or load the whole file?",
     "requestedSchema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}}}},
   "requestState": "<sealed by the SDK>"}
  ```
- **2025-era clients**: hangar sends `elicitation/create` on your session and waits up to 15 minutes for the answer.
- The turn it finally returns says what happened in `elicitation`: `answered`, `declined` or `cancelled`. If you decline, nothing is sent and the question stays open.
- A client that declared no elicitation capability gets the question in the result, as before; answer with `follow_up_job` or `send_to_session`.

An answer goes to the agent at most once: if the question was already answered (another client, a retry), hangar sends nothing.

### Tool approvals and steering

When a session or its environment uses `approvals: ask`, the agent waits for a person before it runs a command or edits a file. `wait_for_turn` puts each such request to you in the same way as a question:
- an `input_required` round for 2026-07-28 clients, or `elicitation/create` for earlier ones;
- the form has `decision` (`allow`, `deny` or `allow_always`) and an optional `message` for the agent;
- the result's `approval` says what happened.

A task turns `input_required` with the same form (keys `approve-<id>`).

Clients without elicitation see the requests in `pending_approvals` (on `wait_for_turn`, `get_session` and `get_job`) and answer with `answer_approval`. `steer_turn(ref, message)` tells a running agent something mid-turn; `steer_turn(ref, interrupt=true)` stops it. See [steering](steering.md).

## Long tools as tasks

For a client that declares the [tasks extension](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/2663-tasks-extension.md) on a call, these tools return a task handle at once instead of their usual result: `submit_job`, `follow_up_job`, `new_session` and `send_to_session` (with a message), `fork_session` (with a message) and `run_agent`.

```jsonc
// tools/call submit_job, with "io.modelcontextprotocol/tasks" in the request's clientCapabilities.extensions
{"resultType": "task", "taskId": "task-5f0c…", "status": "working", "statusMessage": "submit_job started",
 "createdAt": "2026-10-13T06:40:00Z", "lastUpdatedAt": "2026-10-13T06:40:00Z", "ttlMs": 86400000, "pollIntervalMs": 5000}
```

Poll `tasks/get` (`status` is `working`, `input_required`, `completed` or `cancelled`). `statusMessage` carries the job's latest step or the agent's latest event. When the turn ends, `completed` carries what the tool would have returned, plus the turn under `turn` (summary, PR, structured result, artifacts) and resource links to the job or session and its artifacts. A failed job is `completed` with `isError: true`.

If the agent asks a question and the client declared elicitation, the task turns `input_required` with the question in `inputRequests` (keys like `answer-2`, never reused). Answer with `tasks/update`:

```json
{"method": "tasks/update", "params": {"taskId": "task-5f0c…",
  "inputResponses": {"answer-2": {"action": "accept", "content": {"answer": "Use the streaming parser"}}}}}
```

The agent continues the same conversation and the task goes back to `working`. Declining completes the task with the question in the result. A client without elicitation gets `completed` with the question.

`tasks/cancel` cancels a job or stops an agent run. For a session turn it only stops the task (the turn keeps running; `suspend_session` stops it).

A task belongs to the token that created it: any other caller gets "unknown task". `tasks/get`, `tasks/update` and `tasks/cancel` need the extension declared on the request too (`-32021` otherwise, as SEP-2663 requires). Tasks are stored in the control plane's database and derive their state from the job or session, so they survive a restart. They expire 24 hours after creation.

## Resources and subscriptions

| URI | Content |
| --- | --- |
| `hangar://sessions/<name>` | the session (JSON): state, turns, summaries, question, checkpoints |
| `hangar://sessions/<name>/events` | its last 200 activity events |
| `hangar://sessions/<name>/patch` | its work as one patch against the base branch (`git apply`) |
| `hangar://sessions/<name>/artifacts/<turn>/<path>` | a file the agent handed back |
| `hangar://jobs/<id>` | the job (JSON): state, timeline, result, attempts |
| `hangar://jobs/<id>/events` | its agent's last 200 events |

Subscribe to follow work without polling: a 2026-07-28 client opens `subscriptions/listen` with `resourceSubscriptions: ["hangar://jobs/job-1a2b"]`; a 2025-era client sends `resources/subscribe`. Hangar sends `notifications/resources/updated` whenever the job or session changes (state, a turn starting or ending, new events), at most every 250 ms per URI. A notification only says "read it again".

## Typed results and hints

The main tools publish an `outputSchema`, and their results carry `structuredContent` (with the same JSON as text for older clients): jobs (`JobOut`), sessions (`SessionOut`, `ForkOut`), turns (`TurnOut`), sandboxes (`SandboxOut`), events (`EventsOut`), `exec` and agent runs. The declared fields are what you can rely on; results keep every other field of the view.

Every tool is annotated: `readOnlyHint` for the get/list/read tools, `destructiveHint` for destroy, cancel, delete, `exec` and file writes, `idempotentHint` where repeating is harmless, and `openWorldHint` for tools whose effects leave hangar (agents that push branches and open PRs, commands with network). Clients use these to decide when to ask before calling.

The listing is compact: descriptions without docstring indentation, input schemas without pydantic's generated titles or `anyOf [T, null]` wrappers. The part models read on every turn (names, descriptions, input schemas) went from 31.2 KB to 22.4 KB. `validate_environment`, `clone_environment` and `destroy_sandboxes` were merged into `create_environment(dry_run)` and `destroy_sandbox`; the old names are no longer listed but still answer with their old arguments and results, so existing scripts keep working.

## Prompts

| Prompt | Arguments | What it does |
| --- | --- | --- |
| `hand_off_to_cloud` | `task`, `name` | Continue this checkout's unpushed commits and uncommitted changes in a cloud session, follow it, bring the code back |
| `best_of_n` | `issue`, `repo`, `n`, `verify` | Run N attempts on an issue and report the one delivered and why |
| `pick_up_session` | `session` | Recap a session's last turns and open question, then continue it |

Claude Code and VS Code show them as slash commands.

## Deployment notes

- Legacy clients keep a session (`Mcp-Session-Id`), so hangar can ask them questions mid-call and notify them about subscriptions. Sessions live in the control plane's memory: with one replica (the supported setup) nothing else is needed; after a restart, clients start a new session. With several replicas and no sticky routing, set `HANGAR_MCP_STATELESS_HTTP=true`: legacy clients then lose elicitation and subscriptions. 2026-07-28 clients are sessionless either way.
- The SDK seals the `requestState` of an `input_required` round with a per-process key that lives 15 minutes. After a restart, or a longer wait, the client's retry is refused (`-32602`, "Invalid or expired requestState"); calling `wait_for_turn` again asks the question again.
