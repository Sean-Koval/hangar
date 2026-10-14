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
from tests.test_observe import by_name, one, otel, sample  # noqa: F401 - otel is a fixture

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


async def test_an_approval_answer_does_not_undo_a_cancel_that_came_meanwhile(store, monkeypatch):
    """tasks/update now awaits the driver (up to its ack wait) between reading and saving the task record: a
    tasks/cancel in that window must stand (a terminal state never changes)."""
    from hangar import mcp_steer, mcp_tasks

    tasks = mcp_tasks.Tasks(store, None, None, None, lambda: "alice")
    created = tasks.create("send_to_session", types.CallToolResult(content=[], structured_content={"id": "ses-1"}),
                           {})
    tid = created["taskId"]
    rec = tasks._load(tid)
    rec.asked["approve-ap-0123456789ab"] = 1
    tasks._save(rec)

    async def answered_while_cancelled(*_a, **_k):
        await tasks.cancel(tid)
        return "answered"

    monkeypatch.setattr(mcp_steer, "answer_elicited", answered_while_cancelled)
    await tasks.update(tid, {"approve-ap-0123456789ab": {"action": "accept", "content": {"decision": "allow"}}})
    final = tasks._load(tid).final
    assert final is not None and final["status"] == "cancelled", final


async def test_a_shutdown_ends_wait_for_turn_and_the_approval_it_asks_about(fleet, svc, live):
    """integrate (#57 x #59): on shutdown, wait_for_turn answers with the turn as it stands. A legacy client being
    asked about an approval stops being asked (that wait was up to 15 min), and a wait with nothing to ask about
    returns instead of spinning on the shutdown's instant answers until the turn ends."""
    asked, release = asyncio.Event(), asyncio.Event()

    async def never(ctx, params):
        asked.set()
        await release.wait()
        return types.ElicitResult(action="cancel")

    async with Client(build_mcp(fleet, None, svc), mode="legacy", elicitation_callback=never) as c:
        sc(await c.call_tool("new_session", {"name": "down-ask", "environment": "liveenv", "repo": live["url"],
                                             "message": "TOOL make the file", "approvals": "ask"}))
        sc(await c.call_tool("new_session", {"name": "down-slow", "environment": "liveenv", "repo": live["url"],
                                             "message": "SLOW write it"}))
        await running(svc, "down-slow")
        waits = [asyncio.create_task(c.call_tool("wait_for_turn", {"ref": ref, "timeout_seconds": 120}))
                 for ref in ("down-ask", "down-slow")]
        await asyncio.wait_for(asked.wait(), 30)
        t0 = asyncio.get_running_loop().time()
        fleet.closing.set()
        ask, slow = [sc(r) for r in await asyncio.wait_for(asyncio.gather(*waits), 10)]
        took = asyncio.get_running_loop().time() - t0
        release.set()
    assert took < 3, took
    assert not ask["done"] and len(ask["pending_approvals"]) == 1 and "approval" not in ask, ask
    assert not slow["done"], slow


async def test_live_turns_and_the_steer_tools_are_traced(fleet, svc, live, otel):  # noqa: F811
    """integrate (#55 x #59): a turn under the driver gets the same invoke_agent span and /metrics as a one-shot
    turn, an interrupt is a person stopping it rather than an error, and the new tools get tool-call spans."""
    async with Client(build_mcp(fleet, None, svc)) as c:
        sc(await c.call_tool("new_session", {"name": "traced", "environment": "liveenv", "repo": live["url"],
                                             "message": "SLOW write it", "harness": "live-codex"}))
        await running(svc, "traced")
        assert sc(await c.call_tool("steer_turn", {"ref": "traced", "message": "in Rust"}))["status"] == "delivered"
        sc(await c.call_tool("steer_turn", {"ref": "traced", "interrupt": True}))
        s = await settle(svc, "traced")
    t = s.turns[0]
    assert (t.outcome, t.exit_code, t.live) == ("interrupted", 130, True)
    spans = by_name(otel)
    sp = one(spans, "invoke_agent live-codex")
    assert sp.attributes["hangar.turn.outcome"] == "interrupted" and sp.attributes["hangar.run.id"] == t.run_id
    assert "error.type" not in sp.attributes and sp.status.status_code.name != "ERROR"
    assert len(spans["tools/call steer_turn"]) == 2
    assert sample("hangar_mcp_calls_total", tool="steer_turn", status="ok") == 2
    assert sample("hangar_turn_seconds_count", harness="live-codex", outcome="interrupted") == 1
