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


async def test_create_sandboxes_answers_and_keeps_going(fleet, svc, jobs):
    """It handed asyncio.gather's future to create_task, which takes coroutines: every call failed."""
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        fleet.settings.mcp_tool_wait_seconds = 0.3
        gate = asyncio.Event()
        slow_provider(fleet, gate)
        res = await asyncio.wait_for(c.call_tool("create_sandboxes", {"environment": "xenv", "count": 2}), 10)
        gate.set()
        assert not res.is_error, res.content[0].text
        await until(lambda: len([s for s in fleet.list() if s.state == SandboxState.RUNNING]) == 2, timeout=30)
        fleet.settings.mcp_tool_wait_seconds = 30
        made = sc(await c.call_tool("create_sandboxes", {"environment": "xenv", "count": 2, "name_prefix": "pair"}))
        assert [s["state"] for s in made] == ["running", "running"]


@pytest.mark.parametrize("force", [False, True])
async def test_asking_about_a_running_build_returns_that_build(fleet, svc, jobs, force):
    """B17: build_environment again (as its answer says to) made a second job with an empty log, and with force a
    second build."""
    fake = fleet.providers["docker"]
    gate, built = asyncio.Event(), []
    real = fake.build_template

    async def build_template(spec, ctx, tag, log=None):
        built.append(tag)
        await gate.wait()
        return await real(spec, ctx, tag, log)

    async def image_exists(tag):
        return False

    fake.build_template, fake.image_exists = build_template, image_exists
    fleet.settings.mcp_tool_wait_seconds = 0.3
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        first = sc(await c.call_tool("build_environment", {"name": "xenv", "force": force}))
        again = sc(await c.call_tool("build_environment", {"name": "xenv", "force": force}))
        assert first["state"] == again["state"] == "running" and again["id"] == first["id"]
        assert again["log_tail"]  # the build's own log, not an empty one queued behind it
        gate.set()
        await until(lambda: fleet.builds[first["id"]].state == "succeeded")
        assert len(built) == 1 and len(fleet.builds) == 1
        done = sc(await c.call_tool("build_environment", {"name": "xenv", "force": force}))
        assert done["id"] != first["id"]  # once it's over, asking again is a new build (or check)


async def test_a_creates_build_is_not_mistaken_for_a_full_one(fleet, svc):
    """A create's build covers one harness's pack (#80); a running full build must not stand in for it (the create
    would wait on every pack), nor the other way round."""
    gate = asyncio.Event()
    fake = fleet.providers["docker"]

    async def image_exists(tag):
        await gate.wait()
        return True

    fake.image_exists = image_exists
    full = await fleet.build("xenv", wait=False)
    mine = await fleet.build("xenv", wait=False, for_create=("xagent",))
    assert mine is not full and await fleet.build("xenv", wait=False) is full
    gate.set()
    await asyncio.gather(full.task, mine.task)


# ---------------------------------------------------------------------------- --- deadlines: cancel safety
# The client gives up (or drops) mid-call at about 0.1, 0.5 and 2 s, against a provider whose lifecycle calls take
# a while. Whatever the moment, the work finishes and nothing is left in `error`, `stopping` or holding a lock.
CANCEL_AT = (0.1, 0.5, 2.0)
SLOW = 1.0


def slowed(fleet, *names: str, delay: float = SLOW):
    fake = fleet.providers["docker"]
    for n in names:
        real = getattr(fake, n)

        async def slow(*a, _real=real, **kw):
            await asyncio.sleep(delay)
            return await _real(*a, **kw)

        setattr(fake, n, slow)
    return fake


async def call_and_cancel(fleet, svc, jobs, tool: str, args: dict, at: float) -> None:
    """Call `tool` and give up `at` seconds into the call, closing the client (and the in-process server with it)."""
    calling = asyncio.Event()

    async def call():
        async with Client(build_mcp(fleet, jobs, svc)) as c:
            calling.set()
            await c.call_tool(tool, args)

    task = asyncio.create_task(call())
    await asyncio.wait_for(calling.wait(), 10)
    await asyncio.sleep(at)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - however the transport reports it
        pass


async def nothing_left_behind(fleet, svc) -> None:
    def settled() -> bool:
        return (all(s.state not in (SandboxState.STARTING, SandboxState.PENDING, SandboxState.STOPPING)
                    for s in fleet.list(include_stopped=True))
                and not any(svc.busy(s.id) for s in svc.list(include_archived=True)))

    await until(settled, timeout=30)
    states = {s.name: s.state for s in fleet.list(include_stopped=True)}
    assert SandboxState.ERROR not in states.values() and SandboxState.STOPPING not in states.values(), states
    assert not [k for k, lock in svc._locks.items() if lock.locked()]
    assert not [k for k, lock in fleet._build_locks.items() if lock.locked()]


async def boxes(fleet, n: int, **kw) -> list[str]:
    return [(await fleet.create("xenv", **kw)).id for _ in range(n)]


@pytest.mark.parametrize("form", ["one", "label"])
async def test_a_cancelled_destroy_finishes(fleet, svc, jobs, form):
    for at in CANCEL_AT:
        ids = await boxes(fleet, 2 if form == "label" else 1, labels={"cut": str(at)})
        fake = slowed(fleet, "destroy")
        args = {"label": f"cut={at}"} if form == "label" else {"sandbox": ids[0]}
        await call_and_cancel(fleet, svc, jobs, "destroy_sandbox", args, at)
        await nothing_left_behind(fleet, svc)
        assert {fleet.get(i).state for i in ids} == {SandboxState.STOPPED}, at
        assert not set(ids) & set(fake.boxes)


async def test_fleet_destroy_cancelled_after_stopping_still_stops_the_sandbox(fleet, svc):
    """Hardening B2: the reaper or a session suspend cancelled mid-destroy left the sandbox in STOPPING for good."""
    sb = await fleet.create("xenv")
    fake = slowed(fleet, "destroy")
    task = asyncio.create_task(fleet.destroy(sb.id))
    await until(lambda: fleet.get(sb.id).state == SandboxState.STOPPING)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: fleet.get(sb.id).state == SandboxState.STOPPED, timeout=10)
    assert sb.id not in fake.boxes


async def test_a_cancelled_pause_or_resume_leaves_the_record_and_the_sandbox_agreeing(fleet, svc, jobs):
    fake = slowed(fleet, "pause", "resume")
    paused: set[str] = set()
    pause, resume = fake.pause, fake.resume

    async def track_pause(sb):
        await pause(sb)
        paused.add(sb.id)

    async def track_resume(sb):
        await resume(sb)
        paused.discard(sb.id)

    fake.pause, fake.resume = track_pause, track_resume
    for at in CANCEL_AT:
        (sid,) = await boxes(fleet, 1)
        await call_and_cancel(fleet, svc, jobs, "pause_sandbox", {"sandbox": sid}, at)
        await until(lambda sid=sid: fleet.get(sid).state == SandboxState.PAUSED)
        assert sid in paused
        await call_and_cancel(fleet, svc, jobs, "resume_sandbox", {"sandbox": sid}, at)
        await until(lambda sid=sid: fleet.get(sid).state == SandboxState.RUNNING)
        assert sid not in paused
        await nothing_left_behind(fleet, svc)


async def test_a_cancelled_snapshot_is_still_taken_and_recorded(fleet, svc, jobs):
    slowed(fleet, "snapshot")
    for at in CANCEL_AT:
        (sid,) = await boxes(fleet, 1)
        label = f"cut-{at}"
        await call_and_cancel(fleet, svc, jobs, "snapshot_sandbox", {"sandbox": sid, "label": label}, at)
        await until(lambda sid=sid, label=label: any(s.label == label for s in fleet.snapshots(sid)))
        await nothing_left_behind(fleet, svc)


async def test_a_cancelled_fork_session_still_forks_and_frees_the_parent(fleet, svc, jobs, world):
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        sc(await c.call_tool("new_session", {"name": "trunk", "environment": "xenv", "repo": world["url"],
                                             "message": "TASK-USE-B now"}))
        assert sc(await c.call_tool("wait_for_turn", {"ref": "trunk", "timeout_seconds": 60}))["done"]
    slowed(fleet, "exec", delay=0.2)  # the parent's checkpoint runs several commands: a couple of seconds in all
    for at in CANCEL_AT:
        names = [f"cut{int(at * 10)}-{i}" for i in (1, 2)]
        await call_and_cancel(fleet, svc, jobs, "fork_session", {"session": "trunk", "n": 2, "names": names}, at)
        await until(lambda names=names: {s.name for s in svc.list()} >= set(names), timeout=30)
        await nothing_left_behind(fleet, svc)
        assert not svc.busy(svc.get("trunk").id)
