"""Live turns over MCP (workstream steer): tool approvals put to the client while it waits for a turn or holds a
task, and the steer_turn / answer_approval tools. Same scripted harnesses and real tmux as test_steer."""

from __future__ import annotations

import asyncio
from typing import Any

import mcp_types as types
from mcp import Client
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim

from hangar import steer as steering
from hangar.mcp_server import build_mcp
from tests import test_mcp_modern as modern
from tests import test_steer as live_tests

live, svc, settle, running, repo_file = (live_tests.live, live_tests.svc, live_tests.settle, live_tests.running,
                                         live_tests.repo_file)
pytestmark = live_tests.pytestmark


def deciding(decision: str | None, action: str = "accept", asked: list[str] | None = None):
    async def cb(ctx, params):
        if asked is not None:
            asked.append(params.message)
        if action != "accept":
            return types.ElicitResult(action=action)
        return types.ElicitResult(action="accept", content={"decision": decision, "message": "from the client"})

    return cb


def sc(res: types.CallToolResult) -> Any:
    return modern.sc(res)


async def test_wait_for_turn_asks_for_the_approval_modern_and_legacy(fleet, svc, live):
    for mode, harness in (("auto", "live-claude"), ("legacy", "live-codex")):
        asked: list[str] = []
        name = f"mcp-{mode}"
        async with Client(build_mcp(fleet, None, svc), mode=mode, elicitation_callback=deciding("allow",
                                                                                                asked=asked)) as c:
            sc(await c.call_tool("new_session", {"name": name, "environment": "liveenv", "repo": live["url"],
                                                 "message": "TOOL make the file", "harness": harness,
                                                 "approvals": "ask"}))
            t = sc(await c.call_tool("wait_for_turn", {"ref": name, "timeout_seconds": 60}))
        assert t["done"] and t["approval"] == "answered" and t["outcome"] == "exited", t
        assert len(asked) == 1 and "touch tool-ran.txt" in asked[0], asked
        s = svc.get(name)
        assert repo_file(svc, s, "tool-ran.txt").exists()
        assert s.turns[0].approvals[0].status == "allowed" and s.turns[0].approvals[0].by == "mcp"


async def test_declined_approval_is_not_asked_again_and_times_out(fleet, svc, live, settings):
    settings.approval_timeout_seconds = 2
    asked: list[str] = []
    async with Client(build_mcp(fleet, None, svc), elicitation_callback=deciding(None, "decline", asked)) as c:
        sc(await c.call_tool("new_session", {"name": "decline", "environment": "liveenv", "repo": live["url"],
                                             "message": "TOOL make the file", "approvals": "ask"}))
        t = sc(await c.call_tool("wait_for_turn", {"ref": "decline", "timeout_seconds": 60}))
    assert t["done"] and t["approval"] == "declined" and len(asked) == 1, (t, asked)
    s = svc.get("decline")
    assert [(a.status, a.by) for a in s.turns[0].approvals] == [("denied", "timeout")]
    # no elicitation capability: the pending approval is in the result instead, and answer_approval answers it
    async with Client(build_mcp(fleet, None, svc)) as c:
        sc(await c.call_tool("send_to_session", {"session": "decline", "message": "TOOL again"}))
        await running(svc, "decline")
        t = sc(await c.call_tool("wait_for_turn", {"ref": "decline", "timeout_seconds": 1}))
        if not t["pending_approvals"]:
            for _ in range(100):
                if steering.pending(svc.get("decline")):
                    break
                await asyncio.sleep(0.05)
            t = sc(await c.call_tool("wait_for_turn", {"ref": "decline", "timeout_seconds": 1}))
        assert not t["done"] and len(t["pending_approvals"]) == 1, t
        aid = t["pending_approvals"][0]["id"]
        got = sc(await c.call_tool("get_session", {"session": "decline"}))
        assert got["pending_approvals"][0]["id"] == aid
        a = sc(await c.call_tool("answer_approval", {"ref": "decline", "approval": aid, "decision": "allow"}))
        assert a["status"] == "allowed"
        bad = await c.call_tool("answer_approval", {"ref": "decline", "approval": aid, "decision": "allow"})
        assert bad.is_error
    s = await settle(svc, "decline")
    assert repo_file(svc, s, "tool-ran.txt").exists()


async def test_steer_turn_tool(fleet, svc, live):
    async with Client(build_mcp(fleet, None, svc)) as c:
        sc(await c.call_tool("new_session", {"name": "mcp-steer", "environment": "liveenv", "repo": live["url"],
                                             "message": "SLOW write it", "harness": "live-codex"}))
        await running(svc, "mcp-steer")
        out = sc(await c.call_tool("steer_turn", {"ref": "mcp-steer", "message": "in Rust"}))
        assert out["status"] == "delivered" and out["kind"] == "steer"
        empty = await c.call_tool("steer_turn", {"ref": "mcp-steer"})
        assert empty.is_error and "empty" in empty.content[0].text
        t = sc(await c.call_tool("wait_for_turn", {"ref": "mcp-steer", "timeout_seconds": 60}))
    assert t["summary"] == "did: SLOW write it | steered: in Rust", t


class ApprovingTasks(ClientExtension):
    """A tasks-extension client that answers approval requests with a decision."""

    identifier = modern.TasksClient.identifier

    def __init__(self, decision: str):
        self.decision = decision
        self.seen: list[dict[str, Any]] = []

    def claims(self):
        async def resolve(created: modern.CreateTask, ctx: ClaimContext) -> types.CallToolResult:
            for _ in range(1200):
                t = await modern.task_call(ctx.session, "tasks/get", created.task_id)
                self.seen.append(t)
                if t["status"] == "completed":
                    return types.CallToolResult.model_validate(t["result"])
                if t["status"] == "input_required":
                    responses = {k: {"action": "accept", "content": {"decision": self.decision}}
                                 for k in t["inputRequests"]}
                    await modern.task_call(ctx.session, "tasks/update", created.task_id, inputResponses=responses)
                await asyncio.sleep(0.05)
            raise AssertionError("task never finished")

        return [ResultClaim(result_type="task", model=modern.CreateTask, resolve=resolve)]


async def test_a_task_turns_input_required_for_an_approval(fleet, svc, live):
    ext = ApprovingTasks("deny")
    async with Client(build_mcp(fleet, None, svc), extensions=[ext],
                      elicitation_callback=deciding("unused")) as c:
        out = sc(await c.call_tool("new_session", {"name": "task-ask", "environment": "liveenv", "repo": live["url"],
                                                   "message": "TOOL make the file", "approvals": "ask"}))
    assert out["turn"]["done"] and out["turn"]["outcome"] == "exited", out
    asks = [t for t in ext.seen if t["status"] == "input_required"]
    assert asks and next(iter(asks[0]["inputRequests"])).startswith("approve-ap-"), asks
    s = svc.get("task-ask")
    assert s.turns[0].approvals[0].status == "denied" and not repo_file(svc, s, "tool-ran.txt").exists()
