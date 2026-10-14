"""Jobs and sessions out of memory (resident.py), listings a page at a time from the store's indexes, and the
archive of finished jobs and sessions (retention.py). Fake provider; history is seeded straight into the store."""

from __future__ import annotations

import asyncio
import itertools
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from hangar import autofix, identity, intake, usage
from hangar.auth import Principal
from hangar.jobs import JobService
from hangar.mcp_server import build_mcp
from hangar.models import (
    Checkpoint,
    Job,
    JobState,
    ScheduledJob,
    Session,
    SessionState,
    SessionTurn,
    Usage,
)
from hangar.resident import after, cursor
from hangar.retention import Retention
from hangar.sessions import SessionService
from hangar.store import _TABLES
from hangar.vault import Vault
from tests.test_mcp_modern import sc

DAY = 86400
_SEQ = itertools.count()
ALICE = Principal("alice-laptop", "tok-a", ["mcp"], person="alice", role="member")


class As:
    def __init__(self, p: Principal | None):
        self.p = p

    def __enter__(self):
        self.tok = identity.current.set(self.p)

    def __exit__(self, *exc):
        identity.current.reset(self.tok)


def _job(age_days: float = 60, **kw) -> Job:
    t = time.time() - age_days * DAY - next(_SEQ) * 0.001
    kw.setdefault("state", JobState.SUCCEEDED)
    return Job(id=f"job-d{next(_SEQ):07d}", title="old job", instructions="fix the thing", environment="plain",
               created_at=t, updated_at=t, **kw)


def _session(age_days: float = 60, **kw) -> Session:
    t = time.time() - age_days * DAY - next(_SEQ) * 0.001
    kw.setdefault("state", SessionState.SUSPENDED)
    n = next(_SEQ)
    return Session(id=f"ses-d{n:07d}", name=kw.pop("name", f"old-{n}"), environment="plain", created_at=t,
                   last_active_at=t, updated_at=t, **kw)


def _seed(store, records: list[BaseModel], archived: bool = False) -> None:
    """Rows written straight into the store (or its archive), the way months of history are already there."""
    if not records:
        return
    table, key = _TABLES[type(records[0])]
    con = sqlite3.connect(store.path)
    with con:
        if archived:
            con.executemany(f"INSERT INTO {table}_archive(key, data, archived_at) VALUES(?, ?, ?)",
                            [(getattr(r, key), r.model_dump_json(), time.time()) for r in records])
        else:
            con.executemany(f"INSERT INTO {table}(key, data) VALUES(?, ?)",
                            [(getattr(r, key), r.model_dump_json()) for r in records])
    con.close()


@pytest.fixture
async def services(fleet, store, settings):
    """Session and job services over whatever the test seeded first; started by `start(services)`."""
    ss = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    js = JobService(fleet, ss)
    yield ss, js
    await js.stop()
    await ss.stop()


async def start(services) -> tuple[SessionService, JobService]:
    ss, js = services
    await ss.start()
    await js.start()
    return ss, js


def _age(svc_map, key: str, seconds: float = 2 * 3600) -> None:
    """As if the record was last saved and asked for `seconds` ago."""
    rec = svc_map.peek(key)
    rec.updated_at -= seconds
    svc_map._seen[key] = time.time() - seconds


# ====================================================================== memory
async def test_only_live_and_recent_jobs_and_sessions_are_held_in_memory(services, store):
    """Every job and session ever was loaded at start and kept (~30 KB a finished job): memory grew with history."""
    old_jobs = [_job() for _ in range(300)]
    waiting = _job(state=JobState.NEEDS_INPUT)  # unfinished, however old
    open_pr = _job()
    open_pr.landing.pr_state, open_pr.result.pr_number = "open", 12  # its PR is still on its way
    merged = _job()
    merged.landing.pr_state = "merged"  # merged, but its session hasn't ended yet
    recent = _job(age_days=0)
    _seed(store, [*old_jobs, waiting, open_pr, merged, recent])
    old_sessions = [_session() for _ in range(200)]
    fresh = _session(age_days=0)
    _seed(store, [*old_sessions, fresh])

    ss, js = await start(services)
    assert set(js._jobs) == {waiting.id, open_pr.id, merged.id, recent.id}
    assert set(ss._sessions) == {fresh.id}

    # older ones are read back by id (and a session by name), and stay one object while in use
    old = js.get(old_jobs[7].id)
    assert old.title == "old job" and js.get(old.id) is old and old.id in js._jobs
    s = ss.get(old_sessions[3].name)
    assert s.id == old_sessions[3].id and ss.get(s.id) is s

    # let go of once nothing used them for the window; what is pinned stays whatever its age
    for m, key in ((js._jobs, old.id), (js._jobs, recent.id), (js._jobs, waiting.id), (js._jobs, open_pr.id),
                   (ss._sessions, s.id), (ss._sessions, fresh.id)):
        _age(m, key)
    assert js.evict() == 2 and ss.evict() == 2
    assert set(js._jobs) == {waiting.id, open_pr.id, merged.id} and not ss._sessions
    assert js.get(old.id).id == old.id  # and back again when asked for


async def test_a_job_that_finished_long_ago_lets_go_of_its_live_sessions_restart_hold(services, fleet, store):
    """A job's live session is held through a restart until its job says it won't run again. The job was found in
    memory; one that finished hours ago isn't there any more, and its session must not stay held (and up) forever."""
    job = _job(age_days=0.2)
    sb = await fleet.create("plain", name="kept")
    s = _session(age_days=0.2, state=SessionState.IDLE, job_id=job.id, sandbox_id=sb.id)
    job.session_id = s.id
    _seed(store, [job])
    _seed(store, [s])
    ss, js = await start(services)
    assert s.id not in ss._restart_holds and not ss.busy(s.id)


# ====================================================================== listings
async def test_a_member_lists_all_of_their_own_work_past_the_teams_newest(services, fleet, store):
    """list_jobs and list_sessions kept the newest 500 of the whole team and filtered after: a member's older work
    dropped out. They filter in the store now, a page at a time, archive included."""
    mine = [_job(age_days=40 + i, person="alice") for i in range(25)]
    archived = [_job(age_days=90 + i, person="alice") for i in range(5)]
    theirs = [_job(age_days=1 + i / 1000, person="bob") for i in range(600)]
    _seed(store, [*mine, *theirs])
    _seed(store, archived, archived=True)
    my_sessions = [_session(age_days=40 + i, person="alice") for i in range(12)]
    _seed(store, [*my_sessions, *(_session(age_days=1, person="bob") for _ in range(600))])
    ss, js = await start(services)

    async def walk(c, tool: str, **args) -> list[str]:
        seen, cur = [], None
        while True:
            page = sc(await c.call_tool(tool, {**args, **({"cursor": cur} if cur else {})}))
            if not page:
                return seen
            seen += [x["id"] for x in page]
            cur = page[-1]["cursor"]

    from mcp import Client

    async with Client(build_mcp(fleet, js, ss)) as c:
        with As(ALICE):
            first = sc(await c.call_tool("list_jobs", {"limit": 10}))
            assert [j["id"] for j in first] == [j.id for j in mine[:10]]  # newest first
            assert await walk(c, "list_jobs", limit=10) == [j.id for j in [*mine, *archived]]
            assert await walk(c, "list_sessions", limit=5) == [s.id for s in my_sessions]
            failed = await c.call_tool("list_jobs", {"cursor": "not-a-cursor"})
            assert failed.is_error and "cursor" in failed.content[0].text
    assert len(js._jobs) < 100  # pages walked stay in memory only while they're used


def test_rest_listings_page_with_cursors(settings, tmp_path):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    app = create_app(settings)
    h = app.state.hangar
    jobs = [_job(age_days=i) for i in range(1, 8)]
    _seed(h.store, jobs[:5])
    _seed(h.store, jobs[5:], archived=True)
    auth = {"Authorization": "Bearer t0ken"}
    with TestClient(app) as c:
        seen, cur = [], None
        while True:
            r = c.get("/api/jobs", params={"limit": 3, **({"cursor": cur} if cur else {})}, headers=auth)
            assert r.status_code == 200
            seen += [j["id"] for j in r.json()]
            cur = r.headers.get("X-Next-Cursor")
            if cur is None:
                break
            assert cur == r.json()[-1]["cursor"]
        assert seen == [j.id for j in jobs]
        assert c.get("/api/jobs", params={"cursor": "%%%"}, headers=auth).status_code == 400
        assert c.get("/api/sessions", params={"limit": 2}, headers=auth).status_code == 200


def test_cursors_round_trip_and_reject_anything_else():
    assert after(cursor(1.5, "job-1")) == (1.5, "job-1")
    assert after(None) is None
    for bad in ("x", cursor(1.0, "a")[:-3], "WzEsMl0"):  # garbage, cut short, [1, 2]
        with pytest.raises(ValueError, match="cursor"):
            after(bad)


def test_pages_walk_their_index_in_order(store):
    """A member's page reads (person, created_at) in order and stops at the limit, in the archive too."""
    plan = " ".join(r[-1] for r in store._db.execute(
        "EXPLAIN QUERY PLAN SELECT key, data, json_extract(data, '$.created_at') FROM jobs "
        "WHERE json_extract(data, '$.person') IS ? AND json_extract(data, '$.created_at') <= ? "
        "UNION ALL SELECT key, data, json_extract(data, '$.created_at') FROM jobs_archive "
        "WHERE json_extract(data, '$.person') IS ? AND json_extract(data, '$.created_at') <= ? "
        "ORDER BY 3 DESC, 1 DESC LIMIT 10", ["a", 1.0, "a", 1.0]).fetchall())
    assert "jobs_by_person_created_at" in plan and "jobs_archive_by_person_created_at" in plan, plan
    for table, field in (("jobs", "result.pr_number"), ("jobs_archive", "idempotency_key"), ("sessions", "name")):
        plan = " ".join(r[-1] for r in store._db.execute(
            f"EXPLAIN QUERY PLAN SELECT data FROM {table} WHERE json_extract(data, '$.{field}') IS ?", ["x"]))
        assert "USING INDEX" in plan, plan


# ====================================================================== lookups that see every job
async def test_lookups_by_pr_branch_ticket_and_key_find_jobs_not_in_memory(services, store):
    """A PR's events, a ticket's last job and an idempotent retry looked at the jobs in memory (or the newest 500)."""
    old = _job(age_days=200, repo="https://github.com/o/r.git", source_ref="jira:OPS-7", idempotency_key="k1",
               owner="tok-a", branch="hangar/job-old")
    old.result.pr_number, old.result.head_sha = 7, "abc123"
    _seed(store, [*(_job(age_days=1) for _ in range(600)), old])
    ss, js = await start(services)
    assert old.id not in js._jobs
    assert autofix.find_job(js, "o/r", [7], [], None).id == old.id
    assert autofix.find_job(js, "o/r", [], ["hangar/job-old"], None).id == old.id
    assert autofix.find_job(js, "o/r", [], [], "abc123").id == old.id
    assert autofix.find_job(js, "o/x", [7], [], None) is None
    assert intake.latest_job(js, "jira:OPS-7").id == old.id
    assert [j.id for j in js.where(idempotency_key="k1", owner="tok-a")] == [old.id]
    # a change made in memory and not saved yet is what counts
    old = js.get(old.id)
    old.result.pr_number = 8
    assert autofix.find_job(js, "o/r", [7], [], None) is None and autofix.find_job(js, "o/r", [8], [], None)


# ====================================================================== the archive
async def test_finished_jobs_and_sessions_are_archived_unless_still_referenced(services, store, settings):
    settings.job_retention_days = settings.session_retention_days = 30
    plain = _job()
    open_pr = _job()
    open_pr.landing.pr_state = "approved"
    ended = _job()
    ended.landing.pr_state, ended.landing.ended = "merged", True
    not_ended = _job()
    not_ended.landing.pr_state = "closed"
    fixing = _job(state=JobState.FAILED)
    fixing.autofix_state.pending = [{"kind": "check_run", "id": 1}]
    scheduled = _job()
    unfinished = _job(state=JobState.NEEDS_INPUT)
    young = _job(age_days=5)
    s_plain = _session(state=SessionState.ARCHIVED)
    s_of_job = _session(job_id=plain.id)  # follows its job into the archive
    plain.session_id = s_of_job.id
    s_of_pr = _session(job_id=open_pr.id)  # its job stays, so it does
    open_pr.session_id = s_of_pr.id
    s_ckpt = _session(checkpoints=[Checkpoint(reason="suspend")])
    s_live = _session(state=SessionState.IDLE)
    s_used = _session(turns=[SessionTurn(message="m", run_id="run-00000001", started_at=time.time() - 60 * DAY,
                                         usage=Usage(input_tokens=10, cost_usd=0.5))])
    _seed(store, [plain, open_pr, ended, not_ended, fixing, scheduled, unfinished, young])
    _seed(store, [s_plain, s_of_job, s_of_pr, s_ckpt, s_live, s_used])
    store.put(ScheduledJob(name="nightly", cron="0 3 * * *", template={}, last_job_id=scheduled.id))
    ss, js = await start(services)

    r = Retention(store, settings, js, ss)
    out = await r.sweep()
    assert out["jobs_archived"] == 2  # plain and ended
    assert {j.id for j in store.list(Job)} == {open_pr.id, not_ended.id, fixing.id, scheduled.id, unfinished.id,
                                               young.id}
    assert out["sessions_archived"] == 3  # s_plain, s_used and s_of_job (its job went first)
    assert {s.id for s in store.list(Session)} == {s_of_pr.id, s_ckpt.id, s_live.id}
    assert (await r.sweep())["sessions_archived"] == 0

    # still readable: by id and name, in listings, in the usage report
    assert js.get(plain.id).session_id == s_of_job.id and ss.get(s_of_job.id).job_id == plain.id
    assert ss.get(s_plain.id).state == SessionState.ARCHIVED and ss.get(s_used.name).id == s_used.id
    assert plain.id in {j.id for j in js.list(limit=50)}
    assert s_used.id in {s.id for s in ss.list(limit=50)}
    assert usage.aggregate(ss, js, "session", 0)["total"]["cost_usd"] == pytest.approx(0.5)
    # changed again (a follow-up), it is back in the main table, and only there
    job = js.get(plain.id)
    js._event(job, "follow-up from sean", JobState.QUEUED)
    assert store.count_archived(Job) == 1 and plain.id in {j.id for j in store.list(Job)}
    assert [j.id for j in js.list(limit=50)].count(plain.id) == 1


async def test_retention_off_for_jobs_and_sessions(services, store, settings):
    settings.job_retention_days = settings.session_retention_days = 0
    _seed(store, [_job()])
    _seed(store, [_session()])
    ss, js = await start(services)
    out = await Retention(store, settings, js, ss).sweep()
    assert out["jobs_archived"] == out["sessions_archived"] == 0


async def test_metrics_count_jobs_and_sessions_in_the_store_off_the_loop(store):
    from hangar.metrics import Metrics

    _seed(store, [_job(), _job(state=JobState.FAILED, reason="timed_out")])
    _seed(store, [_session()])
    m = Metrics()
    assert m.state_counts(store) == {}  # counting started, in a thread
    counts: dict = {}
    for _ in range(200):
        await asyncio.sleep(0.01)
        if counts := m.state_counts(store):
            break
    assert counts["jobs"] == {("succeeded", None): 1, ("failed", "timed_out"): 1}
    assert counts["sessions"] == {("suspended",): 1}
