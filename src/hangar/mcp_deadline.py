"""The MCP deadline contract: every tool answers within `mcp_tool_wait_seconds`, and no work that must not be left
half-done dies with the request.

MCP clients give up on a call after a few minutes (claude.ai after about 230 s) and cancel it when they do, or when
they disconnect. So each tool is one of:

* DETACHED: its long work runs on a task of its own (`mcp_server._detach`); the tool answers with the result, or
  with the work as it stands once the budget is spent, and the work carries on.
* SELF_TIMED: it waits on its own clamped deadline (and on a person it puts a question to), never past it.
* every other async tool runs under a hard limit just past the budget. Only INTERRUPTIBLE ones may reach it: being
  cut off costs their answer, nothing else.

Wait-like arguments (WAIT_ARGS) are lowered to the budget before the tool sees them. `tests/test_mcp_contract.py`
calls every registered tool against a provider that never answers and holds them to all of this.
"""

from __future__ import annotations

from typing import Any

# Long work on its own task: lifecycle changes a cancel would leave half-made (a sandbox stuck in `stopping`, a
# paused pod hangar still thinks runs, a fork without its children).
DETACHED = frozenset({
    "build_environment", "create_sandbox", "create_sandboxes", "fork_sandbox", "destroy_sandbox", "destroy_sandboxes",
    "pause_sandbox", "resume_sandbox", "extend_sandbox", "snapshot_sandbox", "fork_session", "run_agent",
})

# Waits on its own deadline (clamped) and on the person it asks an agent's question, which is not hangar's time.
SELF_TIMED = frozenset({"wait_for_turn"})

# May be cut off at the hard limit. Reads, the caller's own command, terminal keystrokes, a steer message: stopping
# them loses the answer and nothing else (a background process started as the call was cut off is adopted again on
# the next `process` call).
INTERRUPTIBLE = frozenset({
    "exec", "read_file", "write_file", "list_files", "terminal_open", "terminal_send", "terminal_read",
    "terminal_list", "terminal_close", "agent_status", "get_artifact", "get_session_patch", "process", "steer_turn",
    "answer_approval",
})

# Arguments that make a tool wait, and their unit per second.
WAIT_ARGS = {"timeout": 1, "timeout_seconds": 1, "wait_seconds": 1, "wait_ms": 1000}

CUT_OFF = "got no answer within"  # part of the message a cut-off tool returns (the contract test looks for it)


def clamp(kwargs: dict[str, Any], budget: float) -> dict[str, Any]:
    """`kwargs` with every wait-like argument lowered to the budget (in its own unit)."""
    out = dict(kwargs)
    for k, per_second in WAIT_ARGS.items():
        v = out.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool) and v > budget * per_second:
            out[k] = type(v)(budget * per_second) or 1
    return out


def limit(budget: float) -> float:
    """When a tool that is neither detached nor self-timed is cut off: the budget, plus a tenth for a command run
    to its clamped timeout to come back (`timeout --kill-after=5`). 165 s by default, well inside 230 s."""
    return budget * 1.1


def cut_off_message(tool: str, budget: float) -> str:
    return (f"{tool} {CUT_OFF} {budget:g} s (HANGAR_MCP_TOOL_WAIT_SECONDS), so hangar stopped waiting: the sandbox "
            "is slow or unreachable. Check it with get_sandbox and try again.")
