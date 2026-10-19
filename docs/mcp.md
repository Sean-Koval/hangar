# Using hangar from an MCP client

Claude Code, Codex, Cursor, VS Code and other agents are hangar's main users. This page covers what hangar's MCP server gives them beyond plain tool calls, and how each piece looks on the wire. To connect a client, see [clients.md](clients.md).

hangar speaks every protocol revision its SDK does: the `initialize`-handshake revisions up to **2025-11-25**, and the stateless **2026-07-28** revision (no handshake, capabilities on every request). Each endpoint serves both; each request is routed by its `MCP-Protocol-Version` header.

## Two servers

hangar's tools are split across two MCP servers (#140), so a client that only hands off work doesn't pay, on every turn, for the tools that drive sandboxes by hand:

| Server | Endpoint | For | Tools |
| --- | --- | --- | --- |
| `hangar` | `<public_url>/mcp` | callers who delegate work | jobs, sessions, schedules, discovery (below) |
| `hangar-sandbox` | `<public_url>/mcp/sandbox` | callers who drive sandboxes themselves | sandboxes, commands, files, terminals, agents in a sandbox, snapshots, building environments |

Both take the same tokens and OAuth grants (`mcp`, `user` and admin tokens), each has its own `serverInfo` (name and version hash) and its own protected-resource metadata, and resources and prompts stay on the server whose tools they serve: jobs' and sessions' resources and the workflow prompts on `hangar`, `hangar://sandboxes/<id>/watch` on both. `hangar connect claude-code` (and `codex`) writes both entries, `hangar` and `hangar-sandbox`; other clients get `hangar` only unless you pass `--with-sandbox` ([clients.md](clients.md)). `hangar doctor` checks both endpoints.

**Moving clients over.** For one release the sandbox tools also stay callable on `/mcp`, hidden from its list. A call there works and carries a note, once an hour per tool: *"exec moved to the hangar-sandbox server; re-add the connector to get it there."* Clients that cache tool lists (the claude.ai apps never refresh them) keep calling the old names until the connector is re-added, and the note says so. The sandbox server answers a work tool's name with where it lives.

**Budgets.** Each server's model-visible listing (names, descriptions and input schemas) has a byte budget, `hangar` 14,000 and `hangar-sandbox` 16,000, and must keep 15% headroom under it (`tests/test_mcp_modern.py`); the single server's old 24,700-byte cap is gone.

## The tools

| Server | Area | Tools |
| --- | --- | --- |
| `hangar` | Discover | `fleet_overview`, `list_environments`, `describe_environment`, `list_credentials` |
| `hangar` | Jobs | `submit_job`, `get_job`, `list_jobs`, `follow_up_job`, `cancel_job`, `approve_job`, `reject_job`, `get_events` |
| `hangar` | Sessions | `new_session`, `list_sessions`, `get_session`, `send_to_session`, `fork_session`, `keep_session`, `suspend_session`, `wait_for_turn`, `get_artifact`, `get_session_patch`, `steer_turn`, `answer_approval` |
| `hangar` | Schedules | `create_schedule`, `list_schedules`, `delete_schedule`, `run_schedule_now` |
| `hangar-sandbox` | Lifecycle | `create_sandbox`, `create_sandboxes` (N in parallel), `list_sandboxes`, `get_sandbox`, `destroy_sandbox` (one, several or by label), `pause_sandbox`, `resume_sandbox`, `extend_sandbox` |
| `hangar-sandbox` | Commands and files | `exec`, `process` (background commands), `read_file`, `write_file`, `list_files` |
| `hangar-sandbox` | Terminals | `terminal_open`, `terminal_send` (supports `wait_for` regex), `terminal_read`, `terminal_list`, `terminal_close` |
| `hangar-sandbox` | Agents | `run_agent`, `agent_status`, `open_session` (a session's sandbox, to work in by hand) |
| `hangar-sandbox` | Clone and build | `snapshot_sandbox`, `list_snapshots`, `fork_sandbox`, `create_environment` (`dry_run` validates; `extends` clones), `build_environment` |

Every tool accepts a sandbox id or name. Relative paths resolve against the sandbox's repo checkout, falling back to `/workspace`. Tools carry read-only/destructive hints and typed results; long work can run as MCP tasks, agents' questions reach you as elicitations, and jobs and sessions are subscribable resources.

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

## Running from a harness lock

`submit_job` takes `harness_lock` (`name@rN`, `name@sha256:…`, `name@stable`) to run a job from a
[harness lock](harness-lock.md#thaw-and-drift); sessions are thawed with `hangar harness thaw` or `harness_lock` on
`POST /api/sessions` (the tool listing is at its size cap, #140).

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

Those are a turn's tool approvals. Releasing a whole job that waits in `awaiting_approval` is `approve_job` / `reject_job`: admins and the repo's policy approvers only, with a login token (an `mcp`-only `hangar connect` token is refused), never on their own job; the same rule and refusal text as REST ([who may approve](triggers.md#who-may-approve)).

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
| `.../events?since=<seq>` | only the events after `seq` (what a subscriber reads after a notification) |
| `hangar://sandboxes/<sandbox>/watch?path=<dir>` | file changes under a directory, watched while you subscribe ([streaming](streaming.md#file-watches)) |
| `hangar://inbox` | what needs you: PRs to review, agents' questions, approvals, failed jobs ([landing](landing.md#7-the-inbox-what-needs-you)); it changes with any job or session |

`exec(stream=true)` sends a command's output as progress notifications while it runs, and `process` runs
background commands you attach to later; see [streaming.md](streaming.md).

Subscribe to follow work without polling: a 2026-07-28 client opens `subscriptions/listen` with `resourceSubscriptions: ["hangar://jobs/job-1a2b"]`; a 2025-era client sends `resources/subscribe`. hangar sends `notifications/resources/updated` whenever the job or session changes (state, a turn starting or ending, new events), at most every 250 ms per URI. A notification only says "read it again".

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

- Every tool answers within `HANGAR_MCP_TOOL_WAIT_SECONDS` (150 s), below the few minutes MCP clients wait on a call (claude.ai gives up after about 230 s), and no client that gives up or disconnects cancels work that must not stop half-way (`src/hangar/mcp_deadline.py`; `tests/test_mcp_contract.py` calls every tool against a provider that never answers and fails on any that doesn't):
  - **Long tools run their work on a task of their own:** `create_sandbox`, `create_sandboxes`, `fork_sandbox`, `build_environment`, `destroy_sandbox`, `pause_sandbox`, `resume_sandbox`, `extend_sandbox`, `snapshot_sandbox`, `fork_session` and `run_agent`. Past the wait they answer with the work as it stands: a sandbox `starting` (with a `stage` saying what it waits on), a build `running`, a snapshot or fork with a `note` saying where it will show up. Poll `get_sandbox`, `list_snapshots` or `list_sessions`. Calling `build_environment` again while its build runs returns that build.
  - **Waits are capped at the budget:** `wait_for_turn`'s `timeout_seconds` (default 150; it says `done: false`, so call it again), `exec`'s `timeout` (a command that runs into the cap says so and points at `process`), `process` `attach`'s `wait_seconds` and `terminal_send`'s `wait_ms`.
  - **Everything else is cut off just past the budget** (165 s by default) with an error saying the sandbox didn't answer. Only reads, commands and keystrokes, which lose nothing but their answer, can get there.
- `serverInfo.version` is the package version and a hash of the tool list, `0.1.0+tools.<12 hex>`; it changes whenever a tool, its description or its schema does. The claude.ai apps keep the tool list they got when the connector was added and never ask again ([claude-code#38324](https://github.com/anthropics/claude-code/issues/38324)), so hangar remembers which list each caller last fetched. A call from a client working from an older list (it lacks tools that exist now, the tool has new arguments, or it uses an old name) gets one extra line, at most once an hour per caller, asking to remove and re-add the connector; a call to a tool that doesn't exist says the same in its error. See [clients.md](clients.md#when-hangars-tools-change).
- Legacy clients keep a session (`Mcp-Session-Id`), so hangar can ask them questions mid-call and notify them about subscriptions. Sessions live in the control plane's memory: with one replica (the supported setup) nothing else is needed; after a restart, clients start a new session. With several replicas and no sticky routing, set `HANGAR_MCP_STATELESS_HTTP=true`: legacy clients then lose elicitation and subscriptions. 2026-07-28 clients are sessionless either way.
- The SDK seals the `requestState` of an `input_required` round with a per-process key that lives 15 minutes. After a restart, or a longer wait, the client's retry is refused (`-32602`, "Invalid or expired requestState"); calling `wait_for_turn` again asks the question again.
