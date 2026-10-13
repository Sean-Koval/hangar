"""Hot resume (hot_resume.py, spec 0010 M4): on a provider that restores a paused sandbox from memory, a suspended
session's sandbox is paused after its checkpoint and comes back as it was; anything that makes it doubtful falls back
to the checkpoint, which is always taken first."""

from __future__ import annotations

import asyncio

import pytest

from hangar import hot_resume
from hangar.models import SandboxState, SessionState
from hangar.providers.base import Capabilities
from hangar.sessions import NewSession, SessionService
from tests.conftest import FakeProvider
from tests.test_session_streams import box, settle, svc, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.usefixtures("world")


@pytest.fixture
def hot(fleet, monkeypatch):
    """The fake provider, as a microVM provider: pause and resume are memory snapshots."""
    fake = fleet.providers["docker"]
    calls: list[tuple[str, str]] = []

    async def pause(sb):
        calls.append(("pause", sb.id))

    async def resume(sb):
        calls.append(("resume", sb.id))

    monkeypatch.setattr(FakeProvider, "capabilities", property(
        lambda self: Capabilities(full_snapshots=True, pause=True, hot_resume=True)))
    monkeypatch.setattr(fake, "pause", pause)
    monkeypatch.setattr(fake, "resume", resume)
    fake.calls = calls
    return fake


async def start(svc, world, name: str):  # noqa: F811 - svc is the fixture's value
    svc.new(NewSession(name=name, environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, name)
    assert s.state == SessionState.IDLE, s.reason
    return s


async def suspend(svc, name: str):  # noqa: F811
    svc.suspend(name, requester="sean")
    s = await settle(svc, name)
    assert s.state == SessionState.SUSPENDED and s.sandbox_id is None
    return s


async def test_suspend_pauses_after_the_checkpoint_and_resume_wakes_it(svc, world, fleet, hot, settings):  # noqa: F811
    s = await start(svc, world, "hot")
    sb_id, n = s.sandbox_id, len(s.checkpoints)
    s = await suspend(svc, "hot")
    assert len(s.checkpoints) == n + 1 and s.checkpoints[-1].reason == "suspend", "the checkpoint comes first"
    parked = fleet.get(sb_id)
    assert parked.state == SandboxState.PAUSED and ("pause", sb_id) in hot.calls
    assert parked.expires_at > parked.created_at + settings.session_hot_resume_minutes * 60 - 120
    created = len(hot.created)

    svc.send("hot", "more work", requester="sean")
    s = await settle(svc, "hot")
    assert s.state == SessionState.IDLE, s.reason
    assert s.sandbox_id == sb_id and ("resume", sb_id) in hot.calls and len(hot.created) == created
    assert s.turns[-1].exit_code == 0 and s.turns[-1].mode == "resume"
    # what a checkpoint leaves out (an ignored directory) is still there: this is the same machine
    assert (box(fleet, sb_id) / "workspace/remote/.venv/x").exists()
    assert hot_resume._record(svc, s.id) is None


async def test_a_newer_checkpoint_or_a_lost_sandbox_means_a_cold_restore(svc, world, fleet, hot):  # noqa: F811
    s = await start(svc, world, "cold")
    first = s.sandbox_id
    await suspend(svc, "cold")
    rec = hot_resume._record(svc, s.id)
    rec["checkpoint"] = "ck-older"  # as if the session resumed elsewhere and checkpointed since
    svc.store.kv_set_json(hot_resume.KEY + s.id, rec)
    svc.open("cold", requester="sean")
    s = await settle(svc, "cold")
    assert s.state == SessionState.IDLE and s.sandbox_id != first
    assert fleet.get(first).state == SandboxState.STOPPED, "the stale paused sandbox is removed"
    assert not (box(fleet, s.sandbox_id) / "workspace/remote/.venv/x").exists(), "restored from the checkpoint"

    second = s.sandbox_id
    await suspend(svc, "cold")
    await fleet.destroy(second, actor="reaper")  # the parked sandbox outlived HANGAR_SESSION_HOT_RESUME_MINUTES
    assert svc.get("cold").state == SessionState.SUSPENDED
    svc.open("cold", requester="sean")
    s = await settle(svc, "cold")
    assert s.state == SessionState.IDLE and s.sandbox_id not in (first, second)
    assert (box(fleet, s.sandbox_id) / "workspace/remote/a.txt").exists()


async def test_a_failed_wake_restores_the_checkpoint(svc, world, fleet, hot, monkeypatch):  # noqa: F811
    s = await start(svc, world, "flaky")
    first = s.sandbox_id
    await suspend(svc, "flaky")

    async def broken(sb):
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(hot, "resume", broken)
    svc.open("flaky", requester="sean")
    s = await settle(svc, "flaky")
    assert s.state == SessionState.IDLE and s.sandbox_id != first
    assert fleet.get(first).state == SandboxState.STOPPED
    assert (box(fleet, s.sandbox_id) / "workspace/remote/a.txt").exists()


async def test_no_hot_resume_without_the_capability_or_when_off(svc, world, fleet, settings):  # noqa: F811
    s = await start(svc, world, "plainres")
    first = s.sandbox_id
    await suspend(svc, "plainres")
    assert fleet.get(first).state == SandboxState.STOPPED  # the fake can pause, but not hot-resume: destroyed

    settings.session_hot_resume_minutes = 0
    assert not await hot_resume.park(svc, s, "sb-missing")


async def test_parked_sandboxes_survive_a_restart_and_go_with_archive(svc, world, fleet, hot):  # noqa: F811
    s = await start(svc, world, "keep")
    parked = s.sandbox_id
    await suspend(svc, "keep")
    again = SessionService(fleet, svc.vault)
    await again.start()
    try:
        await asyncio.sleep(0.2)
        assert fleet.get(parked).state == SandboxState.PAUSED, "a parked sandbox isn't an orphan"
        assert parked in hot_resume.parked_ids(again)
        again.archive("keep", requester="sean")
        for _ in range(100):
            if fleet.get(parked).state == SandboxState.STOPPED:
                break
            await asyncio.sleep(0.05)
        assert fleet.get(parked).state == SandboxState.STOPPED and not hot_resume.parked_ids(again)
    finally:
        await again.stop()


async def test_parking_is_bounded(svc, world, fleet, hot, settings, broker):  # noqa: F811
    """Paused sandboxes are a cache: capped, never holding a rotating refresh token, never crowding the fleet."""
    from hangar.models import CredentialKind

    settings.session_hot_resume_max = 1
    a = (await start(svc, world, "first")).sandbox_id
    await suspend(svc, "first")
    b = (await start(svc, world, "second")).sandbox_id
    await suspend(svc, "second")
    assert fleet.get(a).state == SandboxState.STOPPED, "the longest-parked one was evicted"
    assert fleet.get(b).state == SandboxState.PAUSED
    assert hot_resume.parked_ids(svc) == {b}

    settings.session_hot_resume_max = 20
    cred = broker.put("claude-oauth", "claude-code", CredentialKind.AUTH_FILE, '{"refresh": "r"}',
                      file_path=".claude/.credentials.json")
    cred.exclusive = True  # a rotating refresh token, as Claude Code's OAuth login is
    fleet.store.put(cred)
    c = (await start(svc, world, "leased")).sandbox_id
    broker.acquire(["claude-oauth"], c)  # as an OAuth login would be
    await suspend(svc, "leased")
    assert fleet.get(c).state == SandboxState.STOPPED, "a rotating token goes back to the vault"
    assert broker.get("claude-oauth").leased_to is None

    settings.max_sandboxes = 2  # b, parked, already takes three-quarters of it (rounded down)
    d = (await start(svc, world, "crowded")).sandbox_id
    await suspend(svc, "crowded")
    assert fleet.get(d).state == SandboxState.STOPPED and hot_resume.parked_ids(svc) == {b}
