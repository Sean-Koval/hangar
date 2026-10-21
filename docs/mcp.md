# Using hangar from an MCP client

Claude Code, Codex, Cursor, VS Code and other agents are hangar's main users. This page covers what hangar's MCP
server gives them beyond plain tool calls, and how each piece looks on the wire. To connect a client, see
[connecting clients](clients.md).

hangar speaks every protocol revision its SDK does: the `initialize`-handshake revisions up to **2025-11-25**, and the
stateless **2026-07-28** revision (no handshake, capabilities on every request). Each endpoint serves both; each
request is routed by its `MCP-Protocol-Version` header.

| Question | Page |
| --- | --- |
| How do I wait for a turn, answer the agent's question or approve its tool call? How do I follow work without polling? | [Waiting, answering and subscribing](mcp/waiting.md) |
| How do long tools run as tasks, and what do typed results and hints promise? | [Tasks, typed results and hints](mcp/tasks.md) |
| How long does a call take, and what happens to sessions and old tool lists? | [Deployment notes](mcp/deployment.md) |

## Two servers

hangar's tools are split across two MCP servers (#140). So a client that only hands off work doesn't pay, on every
turn, for the tools that drive sandboxes by hand:

| Server | Endpoint | For | Tools |
| --- | --- | --- | --- |
| `hangar` | `<public_url>/mcp` | callers who delegate work | jobs, sessions, schedules, discovery (below) |
| `hangar-sandbox` | `<public_url>/mcp/sandbox` | callers who drive sandboxes themselves | sandboxes, commands, files, terminals, agents in a sandbox, snapshots, building environments |

Both take the same tokens and OAuth grants (`mcp`, `user` and admin tokens). Each has its own `serverInfo` (name and
version hash) and its own protected-resource metadata.

Resources and prompts stay on the server whose tools they serve. Jobs' and sessions' resources and the workflow prompts
are on `hangar`; `hangar://sandboxes/<id>/watch` is on both.

`hangar connect claude-code` (and `codex`) writes both entries, `hangar` and `hangar-sandbox`. Other clients get
`hangar` only unless you pass `--with-sandbox` ([connecting clients](clients.md)). `hangar doctor` checks both
endpoints.

**Moving clients over.** For one release the sandbox tools also stay callable on `/mcp`, hidden from its list. A call
there works and carries a note, once an hour per tool: *"exec moved to the hangar-sandbox server; re-add the connector
to get it there."*

Clients that cache tool lists keep calling the old names until the connector is re-added, and the note says so. The
claude.ai apps never refresh them. The sandbox server answers a work tool's name with where it lives.

**Budgets.** Each server's model-visible listing (names, descriptions and input schemas) has a byte budget: `hangar`
14,000 and `hangar-sandbox` 16,000. It must keep 15% headroom under it (`tests/test_mcp_modern.py`). The single
server's old 24,700-byte cap is gone.

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

Every tool accepts a sandbox id or name. Relative paths resolve against the sandbox's repo checkout, falling back to
`/workspace`. Tools carry read-only/destructive hints and typed results. Long work can run as MCP tasks, agents'
questions reach you as elicitations, and jobs and sessions are subscribable resources.

## Which client gets what

| Feature | 2026-07-28 clients | 2025-era clients |
| --- | --- | --- |
| Typed results (`outputSchema` / `structuredContent`), tool hints, resource links | yes | yes |
| An agent's question put to you (elicitation) | the question comes back as an `input_required` result and the client retries with the answer | an `elicitation/create` request on the client's session |
| Long tools as tasks | with the `io.modelcontextprotocol/tasks` extension | no: the plain result, then `wait_for_turn` |
| Follow a job or session without polling | `subscriptions/listen` | `resources/subscribe` |
| Workflow prompts | yes | yes |

What the major clients support today, from their own sources (October 2026):

- **Claude Code** (2.1.296 changelog): negotiates 2026-07-28 with HTTP servers by default. It has form and URL
  elicitation, `structuredContent`, `resource_link`, tool annotations, prompts, resources and `subscriptions/listen`.
  Its changelog does not mention the tasks extension.
- **Codex** (`codex-rs/rmcp-client`): 2025-06-18 by default (2026-07-28 is opt-in), form elicitation,
  `structuredContent`. It skips the approval prompt for tools marked `readOnlyHint`, or `destructiveHint: false` with
  `openWorldHint: false`.
- **VS Code** (`src/vs/workbench/contrib/mcp`): 2025-11-25, elicitation, `structuredContent`, `resource_link`,
  `resources/subscribe`, and the *experimental 2025-11-25 tasks* (`tasks/result`).
- Those VS Code tasks were removed from the core spec in 2026-07-28 and from the Python SDK v2 hangar runs on. So VS
  Code gets plain calls.
- **Cursor**: not verified (closed source); the official extension matrix lists MCP Apps only.

## Running from a harness lock

`submit_job` takes `harness_lock` (`name@rN`, `name@sha256:…`, `name@stable`) to run a job from a
[harness lock](harness-lock.md#thaw-and-drift). Sessions are thawed with `hangar harness thaw` or `harness_lock` on
`POST /api/sessions` (the tool listing is at its size cap, #140).

## Prompts

| Prompt | Arguments | What it does |
| --- | --- | --- |
| `hand_off_to_cloud` | `task`, `name` | Continue this checkout's unpushed commits and uncommitted changes in a cloud session, follow it, bring the code back |
| `best_of_n` | `issue`, `repo`, `n`, `verify` | Run N attempts on an issue and report the one delivered and why |
| `pick_up_session` | `session` | Recap a session's last turns and open question, then continue it |

Claude Code and VS Code show them as slash commands.

Claude Code and VS Code show them as slash commands.

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="waiting-for-work-and-answering-the-agent"></span>Waiting for work, and answering the agent | [Waiting, answering and subscribing](mcp/waiting.md) |
| <span id="tool-approvals-and-steering"></span>Tool approvals and steering | [Waiting, answering and subscribing](mcp/waiting.md#tool-approvals-and-steering) |
| <span id="resources-and-subscriptions"></span>Resources and subscriptions | [Waiting, answering and subscribing](mcp/waiting.md#resources-and-subscriptions) |
| <span id="long-tools-as-tasks"></span>Long tools as tasks | [Tasks, typed results and hints](mcp/tasks.md) |
| <span id="typed-results-and-hints"></span>Typed results and hints | [Tasks, typed results and hints](mcp/tasks.md#typed-results-and-hints) |
| <span id="deployment-notes"></span>Deployment notes | [Deployment notes](mcp/deployment.md) |
