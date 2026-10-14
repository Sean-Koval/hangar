"""The MCP deadline contract (mcp_deadline), enforced: every registered tool, called against a provider that never
answers, answers within the budget; only tools whose cut-off loses nothing but the answer may be cut off; and
every argument that makes a tool wait is lowered to the budget.

A new tool fails here until it has an entry in ARGS. If it then fails the contract, run its long work with
mcp_server._detach and list it in mcp_deadline.DETACHED (or, if being stopped half-way costs only its answer, in
INTERRUPTIBLE)."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from typing import Any

import pytest
from mcp import Client

from hangar import mcp_deadline as deadline
from hangar.mcp_server import build_mcp
from hangar.models import ExecResult, SandboxState
from hangar.providers.base import Provider
from hangar.triggers import Triggers
from tests import test_exchange as xchg
from tests.test_mcp_modern import sc

world, svc, jobs = xchg.world, xchg.svc, xchg.jobs  # the exchange tests' scripted world
pytestmark = pytest.mark.usefixtures("world")

BUDGET = 0.5
SLACK = 3.0  # the in-process client, the server and a loaded CI box (a tool stuck on the provider never answers)

# Arguments that look like waits but aren't the call's: each with why.
NOT_WAITS = {
    "timeout_minutes": "a job's run time; the job runs in the background",
    "wait_for": "a regex terminal_send looks for, within wait_ms",
}

# Arguments that send each tool into its slow path in the world `frozen` builds. Strings starting with "@" are
# filled from that world (the scripted repo's URL).
ARGS: dict[str, dict[str, Any]] = {
    "fleet_overview": {},
    "list_environments": {},
    "describe_environment": {"name": "xenv"},
    "build_environment": {"name": "xenv"},
    "create_environment": {"spec": {"name": "contract-env", "extends": "xenv"}, "dry_run": True},
    "list_credentials": {},
    "create_sandbox": {"environment": "xenv", "name": "frozen-new"},
    "create_sandboxes": {"environment": "xenv", "count": 2, "name_prefix": "frozen-herd"},
    "list_sandboxes": {},
    "get_sandbox": {"sandbox": "box"},
    "destroy_sandbox": {"sandbox": "victim"},
    "validate_environment": {"spec": {"name": "contract-env", "extends": "xenv"}},
    "clone_environment": {"source": "xenv", "name": "xenv-contract"},
    "destroy_sandboxes": {"label": "contract=herd"},
    "pause_sandbox": {"sandbox": "box"},
    "resume_sandbox": {"sandbox": "napper"},
    "extend_sandbox": {"sandbox": "box", "minutes": 30},
    "exec": {"sandbox": "box", "command": "sleep 1000", "timeout": 3600},
    "read_file": {"sandbox": "box", "path": "README.md"},
    "write_file": {"sandbox": "box", "path": "x.txt", "content": "x"},
    "list_files": {"sandbox": "box"},
    "terminal_open": {"sandbox": "box"},
    "terminal_send": {"sandbox": "box", "text": "ls", "wait_ms": 3_600_000, "wait_for": "never-there"},
    "terminal_read": {"sandbox": "box"},
    "terminal_list": {"sandbox": "box"},
    "terminal_close": {"sandbox": "box", "session": "main"},
    "run_agent": {"sandbox": "box", "prompt": "hello"},
    "agent_status": {"sandbox": "box", "run_id": "run-00000000", "wait_seconds": 3600},
    "snapshot_sandbox": {"sandbox": "box", "kind": "workspace"},
    "list_snapshots": {},
    "fork_sandbox": {"source": "box", "count": 2},
    "new_session": {"name": "fresh", "environment": "xenv", "repo": "@url"},
    "list_sessions": {},
    "get_session": {"session": "sess"},
    "open_session": {"session": "quiet"},
    "send_to_session": {"session": "busy", "message": "TASK-USE-B again"},
    "keep_session": {"session": "quiet", "idle_minutes": 30},
    "suspend_session": {"session": "quiet"},
    "fork_session": {"session": "sess", "n": 2},
    "wait_for_turn": {"ref": "busy", "timeout_seconds": 3600},
    "get_artifact": {"ref": "sess", "turn": 1, "path": "report.md"},
    "get_session_patch": {"ref": "sess"},
    "steer_turn": {"ref": "busy", "message": "also add tests"},
    "answer_approval": {"ref": "busy", "approval": "apr-00000000", "decision": "allow"},
    "process": {"sandbox": "box", "action": "start", "command": "sleep 1000"},
    "submit_job": {"instructions": "TASK-USE-B", "repo": "@url", "environment": "xenv", "deliver": "none"},
    "get_job": {"job": "job-00000000"},
    "list_jobs": {},
    "follow_up_job": {"job": "job-00000000", "message": "more"},
    "cancel_job": {"job": "job-00000000"},
    "get_events": {"session": "sess"},
    "approve_job": {"job": "job-00000000"},
    "reject_job": {"job": "job-00000000"},
    "create_schedule": {"name": "nightly", "cron": "0 9 * * 1-5", "instructions": "x", "repo": "@url",
                        "environment": "xenv"},
    "list_schedules": {},
    "delete_schedule": {"schedule": "nightly"},
    "run_schedule_now": {"schedule": "nightly-2"},
}


def freeze(provider: Provider) -> asyncio.Event:
    """Every provider call waits until the returned event is set: a cluster that has stopped answering."""
    gate = asyncio.Event()
    for name, _ in inspect.getmembers(type(provider), inspect.iscoroutinefunction):
        if name in ("close", "open_terminal"):
            continue

        def frozen(real=getattr(provider, name)):
            async def call(*a, **kw):
                await gate.wait()
                return await real(*a, **kw)
            return call

        setattr(provider, name, frozen())
    return gate


async def until(pred, timeout: float = 30) -> None:
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def registered(mcp) -> dict[str, Any]:
    return {t.name: t for t in mcp._tool_manager.list_tools()}  # noqa: SLF001 - hidden tools too


def test_every_wait_like_argument_is_clamped_or_explained(fleet):
    from unittest.mock import MagicMock

    tools = registered(build_mcp(fleet, MagicMock(), MagicMock(), triggers=MagicMock()))
    unexplained = sorted(f"{t.name}.{p}" for t in tools.values() for p in (t.parameters.get("properties") or {})
                         if re.search(r"wait|timeout", p) and p not in deadline.WAIT_ARGS and p not in NOT_WAITS)
    assert not unexplained, f"add these to mcp_deadline.WAIT_ARGS (or NOT_WAITS here, with why): {unexplained}"
    unknown = (deadline.DETACHED | deadline.SELF_TIMED | deadline.INTERRUPTIBLE) - set(tools)
    assert not unknown, f"mcp_deadline names tools that don't exist: {unknown}"
    assert not deadline.DETACHED & deadline.INTERRUPTIBLE and not deadline.SELF_TIMED & deadline.DETACHED


def test_clamp_lowers_waits_to_the_budget_in_their_own_unit():
    out = deadline.clamp({"timeout": 3600, "timeout_seconds": 600, "wait_ms": 10**9, "wait_seconds": 5,
                          "command": "x", "stream": True}, 150)
    assert out == {"timeout": 150, "timeout_seconds": 150, "wait_ms": 150_000, "wait_seconds": 5, "command": "x",
                   "stream": True}
    assert deadline.clamp({"timeout": 10}, 0.5)["timeout"] == 1  # never 0, which can mean "no wait" or "forever"
    assert deadline.limit(150) < 230  # claude.ai's cut-off


async def test_exec_timeout_is_capped_and_says_where_longer_commands_go(fleet, svc, jobs):
    fake, seen = fleet.providers["docker"], []
    real = fake.exec

    async def exec_(sb, command, timeout=120, cwd=None, user=None):
        if command == "make all":
            seen.append(timeout)
            return ExecResult(exit_code=124, stdout="", stderr="[hangar] command timed out")
        return await real(sb, command, timeout=timeout, cwd=cwd, user=user)

    fake.exec = exec_
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        sb = sc(await c.call_tool("create_sandbox", {"environment": "xenv"}))
        out = sc(await c.call_tool("exec", {"sandbox": sb["id"], "command": "make all", "timeout": 3600}))
        assert seen == [150] and "process(action='start')" in out["note"]
        out = sc(await c.call_tool("exec", {"sandbox": sb["id"], "command": "echo hi", "timeout": 3600}))
        assert out["stdout"] == "hi\n" and "note" not in out


async def test_every_tool_answers_within_the_budget_when_the_provider_stops_answering(fleet, svc, jobs, world):
    mcp = build_mcp(fleet, jobs, svc, triggers=Triggers(jobs))
    tools = registered(mcp)
    missing = sorted(set(tools) - set(ARGS))
    assert not missing, f"add these tools to ARGS in {__name__} (arguments that reach their slow path): {missing}"
    async with Client(mcp) as c:
        # the world: sandboxes, an idle session with a checkpoint, one to keep busy, one to suspend
        ids = {}
        for name, labels in (("box", None), ("victim", None), ("napper", None), ("herd-1", {"contract": "herd"}),
                             ("herd-2", {"contract": "herd"})):
            sb = sc(await c.call_tool("create_sandbox", {"environment": "xenv", "name": name, "labels": labels}))
            ids[name] = sb["id"]
        sc(await c.call_tool("pause_sandbox", {"sandbox": "napper"}))
        for name in ("sess", "busy", "quiet"):
            sc(await c.call_tool("new_session", {"name": name, "environment": "xenv", "repo": world["url"],
                                                 "message": "TASK-USE-B now"}))
        for name in ("sess", "busy", "quiet"):
            assert sc(await c.call_tool("wait_for_turn", {"ref": name, "timeout_seconds": 60}))["done"]
        sc(await c.call_tool("create_schedule", {"name": "nightly-2", "cron": "0 9 * * 1-5", "instructions": "x",
                                                 "repo": world["url"], "environment": "xenv"}))

        gate = freeze(fleet.providers["docker"])
        fleet.settings.mcp_tool_wait_seconds = BUDGET
        late, cut, vacuous = [], [], []
        try:
            for name in tools:
                args = {k: (world[v[1:]] if isinstance(v, str) and v.startswith("@") else v)
                        for k, v in ARGS[name].items()}
                t0 = time.monotonic()
                try:
                    res = await asyncio.wait_for(c.call_tool(name, args), deadline.limit(BUDGET) + 10)
                except TimeoutError:
                    late.append(f"{name}: no answer at all")
                    continue
                took = time.monotonic() - t0
                if took > deadline.limit(BUDGET) + SLACK:
                    late.append(f"{name}: {took:.1f} s")
                text = " ".join(getattr(b, "text", "") for b in res.content)
                if res.is_error and deadline.CUT_OFF in text and name not in deadline.INTERRUPTIBLE:
                    cut.append(name)
                if name in deadline.DETACHED and took < BUDGET * 0.8:
                    vacuous.append(f"{name} ({text[:120]})")
        finally:
            gate.set()
    assert not late, f"these tools outlive the budget; detach their work (mcp_deadline.DETACHED): {late}"
    assert not cut, f"these tools wait on the provider past the budget and were cut off; detach them: {cut}"
    assert not vacuous, f"ARGS didn't reach these detached tools' long work: {vacuous}"
    # and the work the frozen calls started lands once the provider answers again
    await until(lambda: fleet.get(ids["victim"]).state == SandboxState.STOPPED)
    await until(lambda: all(s.state not in (SandboxState.STARTING, SandboxState.STOPPING, SandboxState.PENDING)
                            for s in fleet.list(include_stopped=True)))
    assert fleet.get(ids["napper"]).state == SandboxState.RUNNING
    assert {s.state for s in fleet.list(include_stopped=True) if s.labels.get("contract") == "herd"} \
        == {SandboxState.STOPPED}
