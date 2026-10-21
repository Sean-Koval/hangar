# Deployment notes

How long an MCP call can take on hangar, and what a long tool answers with past that. Then what happens to client
sessions and old tool lists. The servers and tools are on [using MCP](../mcp.md).

## Every tool answers in time

Every tool answers within `HANGAR_MCP_TOOL_WAIT_SECONDS` (150 s). That is below the few minutes MCP clients wait on a
call: claude.ai gives up after about 230 s. No client that gives up or disconnects cancels work that must not stop
half-way (`src/hangar/mcp_deadline.py`).

`tests/test_mcp_contract.py` calls every tool against a provider that never answers, and fails on any that doesn't.

- **Long tools run their work on a task of their own:** `create_sandbox`, `create_sandboxes`, `fork_sandbox`,
  `build_environment`, `destroy_sandbox`, `pause_sandbox`, `resume_sandbox`, `extend_sandbox`, `snapshot_sandbox`,
  `fork_session` and `run_agent`.
    - Past the wait they answer with the work as it stands. A sandbox is `starting`, with a `stage` saying what it
      waits on. A build is `running`. A snapshot or fork has a `note` saying where it will show up.
    - Poll `get_sandbox`, `list_snapshots` or `list_sessions`. Calling `build_environment` again while its build runs
      returns that build.
- **Waits are capped at the budget:** `wait_for_turn`'s `timeout_seconds` (default 150; it says `done: false`, so call
  it again), and `process` `attach`'s `wait_seconds` and `terminal_send`'s `wait_ms`.
- `exec`'s `timeout` is capped too: a command that runs into the cap says so and points at `process`.
- **Everything else is cut off just past the budget** (165 s by default) with an error saying the sandbox didn't
  answer. Only reads, commands and keystrokes, which lose nothing but their answer, can get there.

## Old tool lists

`serverInfo.version` is the package version and a hash of the tool list, `0.1.0+tools.<12 hex>`. It changes whenever
a tool, its description or its schema does.

The claude.ai apps keep the tool list they got when the connector was added and never ask again
([claude-code#38324](https://github.com/anthropics/claude-code/issues/38324)). So hangar remembers which list each
caller last fetched.

A call from a client working from an older list gets one extra line asking to remove and re-add the connector. It
comes at most once an hour per caller. An older list lacks tools that exist now, or a tool's new arguments, or uses an
old name.

A call to a tool that doesn't exist says the same in its error. See
[when hangar's tools change](../clients/formats.md#when-hangars-tools-change).

## Sessions and replicas

Legacy clients keep a session (`Mcp-Session-Id`), so hangar can ask them questions mid-call and notify them about
subscriptions. Sessions live in the control plane's memory. With one replica (the supported setup) nothing else is
needed; after a restart, clients start a new session.

With several replicas and no sticky routing, set `HANGAR_MCP_STATELESS_HTTP=true`: legacy clients then lose
elicitation and subscriptions. 2026-07-28 clients are sessionless either way.

The SDK seals the `requestState` of an `input_required` round with a per-process key that lives 15 minutes. After a
restart, or a longer wait, the client's retry is refused (`-32602`, "Invalid or expired requestState"). Calling
`wait_for_turn` again asks the question again.
