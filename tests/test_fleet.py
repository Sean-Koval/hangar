import pytest

from hangar.fleet import FleetError
from hangar.models import CredentialKind, SandboxState, SnapshotKind


async def test_create_builds_once_and_injects(fleet, broker):
    broker.put("key", "claude-code", CredentialKind.API_KEY, "sk-1")
    fake = fleet.providers["docker"]
    a = await fleet.create("plain", name="One Two", credentials=["key"])
    b = await fleet.create("plain")
    assert a.state == SandboxState.RUNNING and a.name == "one-two"
    assert len(fake.images) == 1, "second sandbox reuses the built image"
    res = await fleet.exec(a.id, "echo $ANTHROPIC_API_KEY")
    assert res.stdout.strip() == "sk-1"
    assert fleet.get("one-two").id == a.id
    assert {s.id for s in fleet.list()} == {a.id, b.id}


async def test_unknown_credential_fails_fast(fleet):
    with pytest.raises(Exception, match="unknown credential"):
        await fleet.create("plain", credentials=["nope"])
    assert fleet.list() == []


async def test_exclusive_credential_blocks_second_sandbox(fleet, broker):
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, "{}")
    a = await fleet.create("plain", credentials=["codex"])
    with pytest.raises(FleetError, match="exclusive"):
        await fleet.create("plain", credentials=["codex"])
    await fleet.destroy(a.id)
    assert broker.get("codex").leased_to is None
    await fleet.create("plain", credentials=["codex"])


async def test_files_and_listing(fleet):
    sb = await fleet.create("plain")
    path = await fleet.write_file(sb.id, "notes/todo.txt", b"ship it")
    assert path == "/workspace/notes/todo.txt"
    assert await fleet.read_file(sb.id, "notes/todo.txt") == b"ship it"
    assert await fleet.read_file(sb.id, "missing.txt") is None


async def test_full_fork_scrubs_secrets_and_keeps_exclusive(fleet, broker):
    broker.put("key", "claude-code", CredentialKind.API_KEY, "sk-1")
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, '{"r":1}')
    parent = await fleet.create("plain", name="parent", credentials=["key", "codex"])
    await fleet.write_file(parent.id, "work.txt", b"progress")
    forks = await fleet.fork(parent.id, count=2, work_branch_prefix=None)
    assert len(forks) == 2
    for f in forks:
        assert f.credentials == ["key"], "exclusive creds stay with the parent"
        assert f.labels["fork-of"] == parent.id
        assert await fleet.read_file(f.id, "work.txt") == b"progress"
        assert await fleet.read_file(f.id, "/home/agent/.codex/auth.json") is None
        env = (await fleet.read_file(f.id, "/home/agent/.hangar/env")).decode()
        assert "sk-1" in env  # re-issued, not inherited
    assert broker.get("codex").leased_to == parent.id
    snaps = fleet.snapshots(parent.id)
    assert snaps and snaps[0].kind == SnapshotKind.FULL


async def test_workspace_snapshot_roundtrip(fleet):
    sb = await fleet.create("plain")
    await fleet.write_file(sb.id, "src/app.py", b"print('hi')")
    snap = await fleet.snapshot(sb.id, label="ws", kind="workspace")
    assert snap.kind == SnapshotKind.WORKSPACE and snap.size_bytes
    [copy] = await fleet.fork(snap.id, count=1)
    assert await fleet.read_file(copy.id, "src/app.py") == b"print('hi')"
    await fleet.delete_snapshot(snap.id)
    assert fleet.snapshots() == [s for s in fleet.snapshots() if s.id != snap.id]


async def test_limits_and_validation(fleet, settings):
    settings.max_sandboxes = 1
    await fleet.create("plain")
    with pytest.raises(FleetError, match="max_sandboxes"):
        await fleet.create("plain")
    with pytest.raises(FleetError, match="between 1 and 20"):
        await fleet.fork("whatever", count=50)


async def test_destroy_many_requires_target(fleet):
    a = await fleet.create("plain", labels={"batch": "x"})
    b = await fleet.create("plain", labels={"batch": "y"})
    gone = await fleet.destroy_many(label="batch=x")
    assert gone == [a.id]
    assert [s.id for s in fleet.list()] == [b.id]


async def test_extend_and_pause(fleet):
    sb = await fleet.create("plain", ttl_minutes=10)
    before = sb.expires_at
    assert (await fleet.extend(sb.id, 30)).expires_at == pytest.approx(before + 1800, abs=2)
    await fleet.pause(sb.id)
    with pytest.raises(FleetError, match="paused"):
        await fleet.exec(sb.id, "true")
    await fleet.resume(sb.id)
    assert (await fleet.exec(sb.id, "echo ok")).stdout.strip() == "ok"


async def test_audit_trail(fleet, store):
    sb = await fleet.create("plain", actor="mcp:codex")
    await fleet.exec(sb.id, "ls", actor="mcp:codex")
    actions = [e.action for e in store.audit_tail()]
    assert "sandbox.create" in actions and "sandbox.exec" in actions


async def test_build_follow_survives_long_logs():
    from hangar.fleet import BuildJob

    job = BuildJob("e", "docker")
    for i in range(4500):
        await job.emit(f"line {i}")
    job.state = "succeeded"
    got = [line async for line in job.follow()]
    assert got[-1] == "line 4499" and len(got) == 4000


def test_login_token_capture_stops_at_token_boundary():
    import re

    from hangar.harnesses import HARNESSES

    rx = HARNESSES["claude-code"].logins[0].token_regex + r"(?![0-9A-Za-z_\-])"
    out = "Your token:\nsk-ant-oat01-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\nStore it somewhere safe"
    assert re.findall(rx, out) == ["sk-ant-oat01-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"]


async def test_fork_of_repo_sandbox_does_not_reclone(fleet, monkeypatch):
    calls = []

    async def fake_setup(prov, sb, spec, work_branch, stage=None):
        calls.append(sb.id)
        sb.info["repo_path"] = "/workspace/proj"

    monkeypatch.setattr(fleet, "_setup_repo", fake_setup)
    parent = await fleet.create("agent", name="p")
    forks = await fleet.fork(parent.id, count=2)
    assert calls == [parent.id], "forks restore the parent's checkout instead of cloning again"
    assert all(f.repo is None and f.info["repo_path"] == "/workspace/proj" for f in forks)


async def test_duplicate_names_get_suffix(fleet):
    a = await fleet.create("plain", name="same")
    b = await fleet.create("plain", name="same")
    assert a.name == "same" and b.name.startswith("same-") and fleet.get("same").id == a.id


async def test_audit_redacts_stored_secrets(fleet, broker, store):
    broker.put("key", "claude-code", CredentialKind.API_KEY, "sk-ant-api-verysecretvalue")
    sb = await fleet.create("plain")
    await fleet.exec(sb.id, "echo sk-ant-api-verysecretvalue > /dev/null")
    assert "verysecretvalue" not in "".join(e.model_dump_json() for e in store.audit_tail())


async def test_reconcile_marks_dead_and_vanished(fleet, broker):
    from hangar.models import SandboxState as S

    broker.put("codex", "codex", CredentialKind.AUTH_FILE, "{}")
    a = await fleet.create("plain", credentials=["codex"])
    b = await fleet.create("plain")
    fake = fleet.providers["docker"]

    async def refs():
        return {b.provider_ref: S.STOPPED}  # a vanished, b exited

    fake.list_refs = refs
    await fleet.reconcile()
    assert fleet.get(a.id).state == S.STOPPED and fleet.get(b.id).state == S.STOPPED
    assert broker.get("codex").leased_to is None


async def test_gc_lists_and_removes_orphans(fleet):
    fake = fleet.providers["docker"]
    sb = await fleet.create("plain")
    removed = []

    async def orphans(known):
        return [{"ref": r, "name": f"hangar-sb-{r}", "status": "exited"} for r in ("x1", sb.provider_ref)
                if r not in known]

    async def remove_ref(ref):
        removed.append(ref)

    fake.orphans, fake.remove_ref = orphans, remove_ref
    assert [o["ref"] for o in await fleet.gc()] == ["x1"] and removed == []
    await fleet.gc(delete=True)
    assert removed == ["x1"]


async def test_fork_scrub_does_not_touch_the_callers_tmux(fleet):
    """Regression: the fork test's `tmux kill-server` once killed the agent running this suite in a sandbox.

    The bystander lives on the default tmux server, where an agent's session (or a developer's) would be.
    """
    import os
    import shutil
    import subprocess

    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_TMPDIR")}
    name = f"hangar-bystander-{os.getpid()}"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 300"], env=env, check=True)
    try:
        parent = await fleet.create("plain", name="tmux-parent")
        await fleet.fork(parent.id, count=1, work_branch_prefix=None)
        alive = subprocess.run(["tmux", "has-session", "-t", name], env=env, capture_output=True)
        assert alive.returncode == 0, "fork scrubbing killed a tmux server outside the fake sandbox"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], env=env, capture_output=True)


async def test_prebuild_and_scheduled_pools(fleet):
    fake = fleet.providers["docker"]
    before = len(fake.images)
    await fleet.prebuild(["plain", "plain", "nope"])  # duplicates and unknown names are fine
    assert len(fake.images) == before + 1
    synced = []

    async def sync(spec, image):
        synced.append((spec.name, spec.warm_pool))

    fake.sync_warm_pool = sync
    raw = fleet.catalog.raw("plain")
    fleet.catalog.add({**raw, "warm_pool": 2, "warm_pool_hours": "Mon-Sun 00:00-00:00"})  # all day, every day
    fleet.store.kv_set("image:docker:plain", "img")
    await fleet.sync_scheduled_pools()
    await fleet.sync_scheduled_pools()  # unchanged: not re-applied
    assert synced == [("plain", 2)]
