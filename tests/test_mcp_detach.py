"""Long MCP tools answer within mcp_tool_wait_seconds and keep working when the client gives up or disconnects."""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client

from hangar.mcp_server import build_mcp
from hangar.models import SandboxState
from tests import test_exchange as xchg
from tests.test_mcp_modern import sc

world, svc, jobs = xchg.world, xchg.svc, xchg.jobs  # the exchange tests' scripted world
pytestmark = pytest.mark.usefixtures("world")


async def until(pred, timeout: float = 20):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def slow_provider(fleet, gate: asyncio.Event):
    fake = fleet.providers["docker"]
    real = fake.create

    async def create(sandbox, image, spec=None):
        await gate.wait()
        return await real(sandbox, image, spec)

    fake.create = create
    return fake


async def test_slow_create_returns_starting_and_finishes_in_the_background(fleet, svc, jobs):
    fleet.settings.mcp_tool_wait_seconds = 0.3
    gate = asyncio.Event()
    slow_provider(fleet, gate)
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        sb = sc(await asyncio.wait_for(c.call_tool("create_sandbox", {"environment": "xenv", "name": "slowpoke"}), 10))
        assert sb["state"] in ("starting", "pending") and sb["name"].startswith("slowpoke")
        gate.set()
        await until(lambda: fleet.get(sb["id"]).state == SandboxState.RUNNING, timeout=30)
        again = sc(await c.call_tool("get_sandbox", {"sandbox": sb["id"]}))
        assert again["state"] == "running" and again["stage"] is None


async def test_fast_create_still_answers_with_the_running_sandbox(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        sb = sc(await c.call_tool("create_sandbox", {"environment": "xenv"}))
        assert sb["state"] == "running"


async def test_a_client_that_disconnects_does_not_cancel_the_create(fleet, svc, jobs):
    gate = asyncio.Event()
    slow_provider(fleet, gate)

    async def call():
        async with Client(build_mcp(fleet, jobs, svc)) as c:
            await c.call_tool("create_sandbox", {"environment": "xenv", "name": "orphan"})

    client = asyncio.create_task(call())
    await until(lambda: any(s.name.startswith("orphan") for s in fleet.list(include_stopped=True)), timeout=10)
    client.cancel()  # the client gives up mid-call and goes away
    try:
        await client
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - however the transport reports it
        pass
    gate.set()
    await until(
        lambda: any(
            s.name.startswith("orphan") and s.state == SandboxState.RUNNING for s in fleet.list(include_stopped=True)
        ),
        timeout=30,
    )


async def test_slow_fork_returns_the_children_as_they_stand(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        parent = sc(await c.call_tool("create_sandbox", {"environment": "xenv"}))
        fleet.settings.mcp_tool_wait_seconds = 0.3
        gate = asyncio.Event()
        slow_provider(fleet, gate)
        res = await asyncio.wait_for(c.call_tool("fork_sandbox", {"source": parent["id"], "count": 2}), 10)
        gate.set()
        kids = sc(res)
        kids = kids if isinstance(kids, list) else kids.get("sandboxes", kids)
        assert kids and all(k["state"] in ("starting", "pending", "running") for k in kids)
        ids = {k["id"] for k in kids}
        await until(lambda: all(fleet.get(i).state == SandboxState.RUNNING for i in ids), timeout=30)
