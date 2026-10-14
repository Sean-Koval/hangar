"""Team scale (docs/team-scale.md): indexed store lookups, sandbox record retention, caps per person, a fair job
queue, job slots from capacity, and a smoke run of the team load test. Fake provider."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from hangar import fairness
from hangar.fairness import CapReached, FairJobQueue, job_cap, job_slots, person_of
from hangar.fleet import ACTIVE, FleetError
from hangar.models import Job, JobState, Sandbox, SandboxState, SessionState
from hangar.retention import Retention
from hangar.sessions import NewSession
from hangar.store import _ACTIVE, Store
from tests.test_session_streams import settle, svc, world  # noqa: F401 - fixtures

needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
DAY = 86400


def _seed(store: Store, n: int, state: SandboxState = SandboxState.STOPPED, age_days: float = 30) -> None:
    """`n` settled sandbox records, written in one transaction (the way months of history pile up)."""
    t = time.time() - age_days * DAY
    rows = [(sb.id, sb.model_dump_json()) for sb in (
        Sandbox(name=f"old-{i}", environment="plain", provider="docker", state=state, created_at=t, last_activity=t,
                info={"runs": [{"run_id": f"run-{k}", "exit_code": 0} for k in range(8)]}) for i in range(n))]
    con = sqlite3.connect(store.path)
    with con:
        con.executemany("INSERT INTO sandboxes(key, data) VALUES(?, ?)", rows)
    con.close()


# ---------------------------------------------------------------------------- store
def test_lookups_by_state_and_name_use_their_index(store):
    plan = " ".join(r[-1] for r in store._db.execute(
        "EXPLAIN QUERY PLAN SELECT data FROM sandboxes WHERE json_extract(data, '$.state') IN (?,?) "
        "AND json_extract(data, '$.name') IS ?", ["running", "paused", "x"]).fetchall())
    assert "USING INDEX sandboxes_by_" in plan, plan
    for table in ("jobs", "sessions"):
        plan = " ".join(r[-1] for r in store._db.execute(
            f"EXPLAIN QUERY PLAN SELECT data FROM {table} WHERE json_extract(data, '$.state') IS ?", ["x"]).fetchall())
        assert f"USING INDEX {table}_by_state" in plan, plan
    assert _ACTIVE[Sandbox] == ACTIVE  # the store's notion of a live sandbox is the fleet's


def test_list_where_matches_values_collections_and_none(store):
    a = Sandbox(name="a", environment="e", provider="docker", state=SandboxState.RUNNING)
    b = Sandbox(name="b", environment="e", provider="docker", state=SandboxState.STOPPED)
    c = Sandbox(name="a", environment="e", provider="docker", state=SandboxState.ERROR, created_by=None)
    for x in (a, b, c):
        store.put(x)
    assert {s.id for s in store.list_where(Sandbox, state=SandboxState.RUNNING)} == {a.id}
    assert {s.id for s in store.list_where(Sandbox, state=["stopped", SandboxState.ERROR])} == {b.id, c.id}
    assert {s.id for s in store.list_where(Sandbox, name="a", state=ACTIVE)} == {a.id}
    assert store.list_where(Sandbox, state=[]) == []
    assert store.count_where(Sandbox, created_by=None) == 3
    assert [s.id for s in store.list_active(Sandbox)] == [a.id]
    with pytest.raises(ValueError):
        store.list_where(Sandbox, **{"state') OR 1=1 --": "x"})
    with pytest.raises(ValueError):
        store.list_where(Sandbox)


async def test_fleet_never_scans_every_sandbox_on_its_hot_paths(fleet, monkeypatch):
    """create, get by name and list read live sandboxes from the index (on main each parsed every record ever)."""
    sb = await fleet.create("plain", name="hot")
    real = fleet.store.list

    def no_scan(cls):
        assert cls is not Sandbox, "scanned every sandbox record"
        return real(cls)

    monkeypatch.setattr(fleet.store, "list", no_scan)
    await fleet.create("plain", name="hot2")
    assert fleet.get("hot").id == sb.id
    assert {s.name for s in fleet.list()} == {"hot", "hot2"}
    await fleet.destroy("hot2")
    assert [s.name for s in fleet.list()] == ["hot"]


async def test_hot_paths_stay_fast_with_months_of_history(fleet):
    _seed(fleet.store, 20_000)
    sb = await fleet.create("plain", name="now")
    t = time.perf_counter()
    for _ in range(20):
        assert fleet.get("now").id == sb.id
        assert [s.id for s in fleet.list()] == [sb.id]
    per = (time.perf_counter() - t) / 20
    assert per < 0.03, f"{per * 1000:.1f} ms per lookup and listing at 20k records (a scan takes ~300 ms)"


# ---------------------------------------------------------------------------- retention
async def test_retention_archives_settled_records_and_get_still_finds_them(fleet, settings):
    settings.sandbox_retention_days = 7
    _seed(fleet.store, 1_200)  # more than one batch
    _seed(fleet.store, 3, age_days=1)  # settled, but recent: kept
    live = await fleet.create("plain", name="live")
    old = Sandbox(name="old-live", environment="plain", provider="docker", state=SandboxState.RUNNING,
                  last_activity=time.time() - 30 * DAY)
    fleet.store.put(old)  # idle for a month, but running: never archived
    gone = next(s for s in fleet.store.list_where(Sandbox, state=SandboxState.STOPPED)
                if s.last_activity < time.time() - 7 * DAY)
    out = await Retention(fleet.store, settings).sweep()
    assert out["archived"] == 1_200
    left = fleet.store.list(Sandbox)
    assert {s.id for s in left} >= {live.id, old.id} and len(left) == 5
    assert fleet.store.count_archived(Sandbox) == 1_200
    assert fleet.store.get(Sandbox, gone.id).state == SandboxState.STOPPED  # by id, from the archive
    assert gone.id not in {s.id for s in fleet.list(include_stopped=True)}
    assert (await Retention(fleet.store, settings).sweep())["archived"] == 0
    # deleted from the archive once old enough; a delete by key reaches the archive too
    settings.sandbox_archive_days = 1
    fleet.store._db.execute("UPDATE sandboxes_archive SET archived_at = archived_at - 2 * 86400 WHERE key != ?",
                            (gone.id,))
    assert (await Retention(fleet.store, settings).sweep())["purged"] == 1_199
    fleet.store.delete(Sandbox, gone.id)
    assert fleet.store.get(Sandbox, gone.id) is None and fleet.store.count_archived(Sandbox) == 0


async def test_retention_off(fleet, settings):
    settings.sandbox_retention_days = 0
    settings.sandbox_archive_days = 0
    _seed(fleet.store, 10)
    assert await Retention(fleet.store, settings).sweep() == {"archived": 0, "purged": 0}
    assert len(fleet.store.list(Sandbox)) == 10


# ---------------------------------------------------------------------------- people and caps
@pytest.mark.parametrize("actor,person", [
    ("mcp:sean", "sean"), ("ui:sean", "sean"), ("sean", "sean"), ("oauth:claude-ai(sean)", "sean"),
    ("e2b:ci-bot", "ci-bot"), ("oauth:cursor(?)", "oauth:cursor(?)"), ("github:octocat", "github:octocat"),
    ("session:ses-1", None), ("job:job-1", None), ("system", None), ("reaper", None), ("jobs", None),
    ("hangar:autofix", None), ("mcp", None), ("", None), (None, None),
])
def test_person_of(actor, person):
    assert person_of(actor) == person


async def test_sandbox_cap_per_person(fleet, settings):
    settings.person_max_sandboxes = 2
    await fleet.create("plain", actor="mcp:sean")
    await fleet.create("plain", actor="ui:sean")  # the same person over REST
    with pytest.raises(CapReached, match="sean already has 2 sandboxes"):
        await fleet.create("plain", actor="oauth:claude-ai(sean)")
    await fleet.create("plain", actor="mcp:erin")  # someone else
    await fleet.create("plain", actor="session:ses-1")  # hangar's own: capped where a person asked
    settings.person_max_sandboxes = 0
    await fleet.create("plain", actor="mcp:sean")


@needs_tools
async def test_session_cap_per_person_counts_new_resumed_and_forked(svc, world, settings):  # noqa: F811
    settings.person_max_sessions = 2
    for n in ("one", "two"):
        svc.new(NewSession(name=n, environment="sesenv", repo=world["url"]), owner="sean", requester="sean")
    with pytest.raises(CapReached, match="sean already has 2 sessions"):
        svc.new(NewSession(name="three", environment="sesenv", repo=world["url"]), owner="ui:sean",
                requester="sean")
    svc.new(NewSession(name="hers", environment="sesenv", repo=world["url"]), owner="erin", requester="erin")
    await settle(svc, "one")
    await settle(svc, "two")
    svc.suspend("two", requester="sean")
    s = await settle(svc, "two")
    assert s.state == SessionState.SUSPENDED
    svc.new(NewSession(name="three", environment="sesenv", repo=world["url"]), owner="sean", requester="sean")
    with pytest.raises(CapReached):  # resuming takes a sandbox
        svc.send("two", "carry on", requester="sean")
    with pytest.raises(CapReached):
        svc.open("two", requester="sean")
    from hangar.strategies import fork_session

    with pytest.raises(CapReached):
        await fork_session(svc, "one", 1, requester="sean")
    await settle(svc, "three")


# ---------------------------------------------------------------------------- jobs: slots and the fair queue
def test_job_slots_from_capacity(settings):
    settings.job_concurrency = 0
    assert job_slots(settings) == 4  # Docker: one machine
    settings.default_provider = "kubernetes"
    assert settings.max_sandboxes == 120 and job_slots(settings) == 24
    settings.max_sandboxes = 30
    assert job_slots(settings) == 6
    settings.max_sandboxes = 5
    assert job_slots(settings) == 4
    settings.job_concurrency = 7
    assert job_slots(settings) == 7
    settings.person_max_running_jobs = 0
    assert job_cap(settings) == 3
    settings.person_max_running_jobs = 5
    assert job_cap(settings) == 5


async def test_fair_queue_serves_the_person_with_fewest_running_first_and_caps_each():
    owner = {"a1": "ann", "a2": "ann", "a3": "ann", "a4": "ann", "b1": "bob", "x1": ""}
    q = FairJobQueue(lambda j: owner.get(j, ""), lambda: 2)
    for j in ("a1", "a2", "a3", "a4", "b1"):
        q.put_nowait(j)
    got: list[str] = []
    release = asyncio.Event()

    async def worker():
        while True:
            j = await q.get()
            got.append(j)
            try:
                await release.wait()
            finally:
                q.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(4)]
    await asyncio.sleep(0.05)
    assert got == ["a1", "b1", "a2"]  # bob's one job goes before ann's second; ann stops at her cap of 2
    assert q.running() == {"ann": 2, "bob": 1} and q.qsize() == 2
    q.put_nowait("x1")  # nobody in particular: never capped
    await asyncio.sleep(0.05)
    assert got[-1] == "x1"
    release.set()
    await asyncio.sleep(0.05)
    assert sorted(got) == ["a1", "a2", "a3", "a4", "b1", "x1"] and q.empty()
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    assert q.running() == {}


def test_job_service_uses_the_fair_queue_and_derived_slots(fleet, settings):
    from hangar.jobs import JobService

    jobs = JobService(fleet, None)  # type: ignore[arg-type] - the queue needs no sessions
    assert isinstance(jobs._queue, FairJobQueue)
    job = Job(title="t", instructions="x", environment="plain", requester="ui:sean", owner="tok-1",
              state=JobState.QUEUED)
    jobs._jobs[job.id] = job
    assert jobs._person(job.id) == "sean" and jobs._person("job-missing") == ""
    assert fairness.job_cap(settings) == 2


# ---------------------------------------------------------------------------- the load test itself, small
@needs_tools
async def test_team_load_smoke():
    """2 people × 2 sessions over MCP and REST against 2,000 settled records: everything finishes, nothing is
    refused, and no request scans the sandbox table on the event loop."""
    bench = Path(__file__).resolve().parents[1] / "scripts" / "bench"
    sys.path.insert(0, str(bench))
    try:
        import team_load
    finally:
        sys.path.remove(str(bench))
    a = team_load.parser().parse_args(["--actors", "2", "--sessions", "2", "--turns", "2", "--jobs", "1",
                                       "--history", "2000", "--steps", "2", "--poll", "1", "--list", "2",
                                       "--exec", "1", "--dashboard", "1", "--timeout", "120", "--watchdog-ms", "0"])
    assert isinstance(a, argparse.Namespace)
    r = await team_load.main(a)
    notes, m = r["notes"], r["metrics"]
    assert notes["errors"] == [] and notes["job_states"] == {"succeeded": 2}
    assert m["session_turn_wall"]["n"] == 8  # 2 people × 2 sessions × 2 turns
    for tool in ("mcp_new_session", "mcp_get_session", "mcp_list_sandboxes", "rest_sandboxes"):
        assert m[tool]["n"] > 0, tool
    scans = {k: v for k, v in notes["server"]["sqlite"]["top"].items() if k.startswith("loop:list:sandboxes")}
    assert not scans, f"full scans of the sandbox table on the event loop: {scans}"


# ---------------------------------------------------------------------------- B12: forks within a memory budget
async def test_memory_budget_bounds_what_is_held_and_runs_an_oversized_request_alone():
    from hangar.membudget import MemoryBudget

    b = MemoryBudget(100)
    peak = [0]
    order: list[str] = []

    async def use(name: str, n: int):
        async with b.hold(n):
            order.append(name)
            peak[0] = max(peak[0], b.used)
            await asyncio.sleep(0.02)

    await asyncio.gather(use("a", 60), use("b", 60), use("c", 30), use("huge", 500))
    assert peak[0] == 500 and b.used == 0  # the oversized one ran, and alone
    assert peak[0] != 60 + 60
    waiter = asyncio.create_task(use("late", 80))
    async with b.hold(50):
        await asyncio.sleep(0.01)
        assert b.waiting == 1
        waiter.cancel()  # a request cancelled while it waits takes nothing with it
        await asyncio.gather(waiter, return_exceptions=True)
    assert b.used == 0 and b.waiting == 0


async def test_fork_restores_within_the_archive_budget(fleet, settings):
    """B12: fork(count=4) restored every child at once, each holding its own copy of the archive; on a 2 GiB control
    plane, ~5 children of a 400 MB workspace (3 on pods/exec) were an OOM kill. Now the budget bounds them."""
    sb = await fleet.create("plain")
    await fleet.write_file(sb.id, "big.bin", bytes(range(256)) * 4096)  # ~1 MB, incompressible enough
    snap = await fleet.snapshot(sb.id, kind="workspace")
    fake = fleet.providers["docker"]
    real_write, live, most = fake.write_file, [0], [0]

    async def write_file(sandbox, path, data, mode=0o644):
        if "/restore-" in path:
            live[0] += 1
            most[0] = max(most[0], live[0])
            await asyncio.sleep(0.05)
            live[0] -= 1
        return await real_write(sandbox, path, data, mode)

    fake.write_file = write_file
    from hangar.fleet import ARCHIVE_WRITE_COST

    fleet.archive_memory.limit = 2 * snap.size_bytes * ARCHIVE_WRITE_COST  # room for two at a time
    kids = await fleet.fork(snap.id, count=4)
    assert len(kids) == 4 and most[0] == 2 and fleet.archive_memory.used == 0
    assert await fleet.read_file(kids[3].id, "big.bin") == bytes(range(256)) * 4096


async def test_oversized_workspace_snapshot_is_refused_before_it_is_read(fleet, settings, monkeypatch):
    settings.snapshot_max_mb = 1
    sb = await fleet.create("plain")
    await fleet.exec(sb.id, "head -c 3000000 /dev/urandom > /workspace/blob")
    fake = fleet.providers["docker"]

    async def no_read(*a, **kw):
        raise AssertionError("read the archive")

    monkeypatch.setattr(fake, "read_file", no_read)
    with pytest.raises(FleetError, match="SNAPSHOT_MAX_MB"):
        await fleet.snapshot(sb.id, kind="workspace")
    assert fleet.archive_memory.used == 0
