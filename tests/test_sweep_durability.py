"""overnight/sweep, durability: what the wave 1 reviews of #53 and #49 left open.

Forks copy checkpoints inside the store, a checkpoint without the repo still keeps the conversation, the database is
backed up and restorable, shutdown doesn't wait on long-polls, and a deleted session's usage stays on its job. Each
test failed on overnight/sweep (the security-caps PR) before the fix."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
import signal
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import hangar.cli as cli
from hangar import backup, exchange, shutdown
from hangar.checkpoints import GCSStore, LocalStore
from hangar.config import Settings
from hangar.models import ApiToken, Job, Session, SessionState, SessionTurn, Usage
from hangar.sessions import NewSession, SessionError
from hangar.strategies import fork_session
from tests.test_jobs import jobs, sessions  # noqa: F401 - fixtures (with test_strategies' world)
from tests.test_strategies import box, settle, svc, world  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]
needs_git = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


# ====================================================================== 4. forks copy inside the store
@needs_git
async def test_fork_copies_checkpoint_parts_inside_the_store(svc, world, fleet, monkeypatch):  # noqa: F811
    svc.new(NewSession(name="base", environment="forkenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    parent = await settle(svc, "base")
    ck = parent.checkpoints[-1]
    fetched: list[str] = []
    get = svc.blobs.get

    async def spy(key):
        fetched.append(key)
        return await get(key)

    monkeypatch.setattr(svc.blobs, "get", spy)
    kids = await fork_session(svc, "base", 2, requester="sean")
    assert not [k for k in fetched if k.startswith(f"{parent.id}/")]  # nothing went through memory
    for k in kids:
        for part in ck.parts:  # LocalStore: a hard link to the parent's blob
            src, dst = svc.root / parent.id / ck.id / f"{part}.enc", svc.root / k.id / ck.id / f"{part}.enc"
            assert os.stat(src).st_ino == os.stat(dst).st_ino
    k = await settle(svc, kids[0].name)
    assert k.state == SessionState.IDLE, k.reason
    repo = box(fleet, k.sandbox_id) / "workspace/remote"
    assert (repo / "a.txt").exists() and (repo / "notes.txt").exists()

    (svc.root / parent.id / ck.id / "repo.bundle.enc").unlink()  # a part gone from the store: no child is made
    names = {s.name for s in svc._sessions.values()}
    with pytest.raises(SessionError, match="missing repo.bundle"):
        await fork_session(svc, "base", 2, requester="sean")
    assert {s.name for s in svc._sessions.values()} == names
    await asyncio.sleep(0.2)  # the partial children are dropped in the background
    assert sorted(p.name for p in svc.root.iterdir()) == sorted([parent.id, *(c.id for c in kids)])


# ====================================================================== 5. capture without the repo
@needs_git
async def test_a_checkpoint_without_the_repo_keeps_the_conversation_and_hangar_io(svc, world, fleet):  # noqa: F811
    svc.new(NewSession(name="nr", environment="forkenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "nr")
    first = s.checkpoints[-1]
    b = box(fleet, s.sandbox_id)
    shutil.rmtree(b / "workspace/remote")  # the agent removed its checkout
    conv = b / "home/agent/.fake/sessions" / f"{s.harness_session}.jsonl"
    conv.write_text(conv.read_text() + "after the repo went\n")
    assert exchange.io_rel(s) in s.extra_dirs
    (b / "home/agent" / exchange.io_rel(s) / "out").mkdir(parents=True, exist_ok=True)
    (b / "home/agent" / exchange.io_rel(s) / "out/note.md").write_text("kept\n")

    ck = await svc.checkpoint_now("nr", requester="sean")
    assert ck is not None and any("is gone" in w and first.id in w for w in ck.warnings)
    assert ck.digests["agent.tgz"] != first.digests["agent.tgz"]
    for part in ("repo.bundle", "wip.patch", "untracked.tgz"):  # the code as of the last checkpoint
        assert ck.digests[part] == first.digests[part] and ck.parts[part] == first.parts[part]
    assert ck.head == first.head

    svc.suspend("nr", requester="sean")
    assert (await settle(svc, "nr")).state == SessionState.SUSPENDED
    svc.open("nr", requester="sean")
    s = await settle(svc, "nr")
    assert s.state == SessionState.IDLE, s.reason
    b = box(fleet, s.sandbox_id)
    assert (b / "workspace/remote/a.txt").exists() and (b / "workspace/remote/notes.txt").exists()
    assert "after the repo went" in (b / "home/agent/.fake/sessions" / f"{s.harness_session}.jsonl").read_text()
    assert (b / "home/agent" / exchange.io_rel(s) / "out/note.md").read_text() == "kept\n"


# ====================================================================== 6. database backup and restore
def _app(settings: Settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    return create_app(settings)


def _ticking(monkeypatch) -> None:
    """Backups are named by the second; give each one its own."""
    t = [time.time() - 1000]

    def tick() -> float:
        t[0] += 1
        return t[0]

    monkeypatch.setattr(backup.time, "time", tick)


def test_backup_restore_roundtrip(settings, monkeypatch):
    _ticking(monkeypatch)
    hdr = {"Authorization": "Bearer t0ken"}
    with TestClient(_app(settings)) as c:
        h = c.app.state.hangar
        h.auth.create("before", scopes=["jobs"])
        r = c.post("/api/admin/backup", headers=hdr)
        assert r.status_code == 200, r.text
        name = r.json()["name"]
        h.auth.create("after", scopes=["jobs"])
        listed = c.get("/api/admin/backups", headers=hdr).json()
        assert [b["name"] for b in listed["backups"]] == [name] and listed["every_hours"] == 0
        blob = (settings.data_dir / "sessions" / "_backups" / name).read_bytes()
        assert b"SQLite" not in blob and not blob.startswith(b"\x1f\x8b")  # encrypted, not just gzipped
        assert gzip.decompress(h.vault.decrypt(blob)).startswith(b"SQLite format 3\x00")
        assert c.post("/api/admin/restore", json={"name": "../x"}, headers=hdr).status_code == 400
        r = c.post("/api/admin/restore", json={"name": "latest"}, headers=hdr)
        assert r.status_code == 200 and r.json()["name"] == name
        assert c.post("/api/admin/backup", headers={"Authorization": "Bearer nope"}).status_code == 401
    with TestClient(_app(settings)) as c:  # the next start swaps it in
        names = {t.name for t in c.app.state.hangar.store.list(ApiToken)}
    assert "before" in names and "after" not in names
    assert not (settings.data_dir / backup.PENDING).exists()
    assert list(settings.data_dir.glob("hangar.db.before-restore-*"))


async def test_backups_keep_the_newest_and_refuse_a_foreign_key(settings, store, monkeypatch, tmp_path):
    from hangar.vault import Vault

    _ticking(monkeypatch)
    settings.backup_keep = 2
    blobs = LocalStore(tmp_path / "blobs")
    b = backup.Backups(settings, Vault(store, b"0" * 43 + b"="), blobs)
    names = [(await b.run())["name"] for _ in range(3)]
    assert [x["name"] for x in await b.list()] == names[:0:-1]  # the oldest was deleted
    other = backup.Backups(settings, Vault(store, b"1" * 43 + b"="), blobs)
    with pytest.raises(backup.BackupError, match="HANGAR_SECRET_KEY"):
        await other.stage("latest")
    await blobs.put(f"{backup.PREFIX}hangar-20260101T000000Z.db.gz.enc", b.vault.encrypt(gzip.compress(b"junk")))
    with pytest.raises(backup.BackupError, match="not a SQLite database"):
        await b.stage("hangar-20260101T000000Z.db.gz.enc")


def test_a_backup_holds_commits_still_in_the_wal(tmp_path):
    from hangar.store import Store

    s = Store(tmp_path / "h.db")
    s._db.execute("PRAGMA wal_autocheckpoint=0")
    for i in range(200):
        s.kv_set(f"k{i}", "v" * 100)
    assert (tmp_path / "h.db-wal").stat().st_size > (tmp_path / "h.db").stat().st_size
    backup.snapshot(tmp_path / "h.db", tmp_path / "copy.db")
    backup.check((tmp_path / "copy.db").read_bytes())
    assert (tmp_path / "copy.db").stat().st_mode & 0o777 == 0o600
    assert Store(tmp_path / "copy.db").kv_get("k199") == "v" * 100


async def test_scheduled_backups_and_the_doctor_check(settings, store, monkeypatch, tmp_path):
    from hangar.vault import Vault

    monkeypatch.setattr(backup, "_FIRST_DELAY", 0.05)
    b = backup.Backups(settings, Vault(store, b"0" * 43 + b"="), LocalStore(tmp_path / "blobs"))
    assert await backup.doctor_check(b, settings, on_kubernetes=False) is None  # off, on a laptop: nothing to say
    assert (await backup.doctor_check(b, settings, on_kubernetes=True))["status"] == "warn"
    settings.session_store = "gs://bkt"  # a bucket turns daily backups on
    assert settings.backup_interval_hours == 24
    assert (await backup.doctor_check(b, settings, on_kubernetes=True))["status"] == "warn"  # none yet
    b.start()
    try:
        for _ in range(100):
            if await b.list():
                break
            await asyncio.sleep(0.05)
    finally:
        await b.stop()
    assert len(await b.list()) == 1
    assert (await backup.doctor_check(b, settings, on_kubernetes=True))["status"] == "ok"
    newest = (await b.list())[0]["at"]
    monkeypatch.setattr(backup.time, "time", lambda: newest + 4 * 86400)
    stale = await backup.doctor_check(b, settings, on_kubernetes=True)
    assert stale["status"] == "fail" and "96.0 h old" in stale["detail"]


async def test_gcs_store_lists_pages_and_deletes():
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.append((req.method, req.url.raw_path.decode()))
        if req.method == "DELETE":
            return httpx.Response(404 if "gone" in req.url.raw_path.decode() else 204)
        assert req.url.params["prefix"] == "pre/_backups/"
        if req.url.params.get("pageToken") == "p2":
            return httpx.Response(200, json={"items": [{"name": "pre/_backups/b"}]})
        return httpx.Response(200, json={"items": [{"name": "pre/_backups/c"}, {"name": "pre/_backups/a"}],
                                         "nextPageToken": "p2"})

    s = GCSStore("gs://bkt/pre", transport=httpx.MockTransport(handler))
    assert await s.list("_backups/") == ["_backups/a", "_backups/b", "_backups/c"]
    await s.delete("_backups/a")
    await s.delete("_backups/gone")  # already deleted: fine
    assert ("DELETE", "/storage/v1/b/bkt/o/pre%2F_backups%2Fa") in seen
    await s.close()


def test_restore_local_from_the_cli(settings, monkeypatch):
    with TestClient(_app(settings)) as c:
        c.app.state.hangar.auth.create("kept", scopes=["jobs"])
        assert c.post("/api/admin/backup", headers={"Authorization": "Bearer t0ken"}).status_code == 200
        c.app.state.hangar.auth.create("later", scopes=["jobs"])
    monkeypatch.setenv("HANGAR_DATA_DIR", str(settings.data_dir))
    monkeypatch.delenv("HANGAR_SECRET_KEY", raising=False)
    monkeypatch.delenv("HANGAR_SESSION_STORE", raising=False)
    r = CliRunner().invoke(cli.app, ["admin", "restore", "--local", "latest"])
    assert r.exit_code == 0, r.output
    assert "staged hangar-" in r.output
    with TestClient(_app(settings)) as c:
        names = {t.name for t in c.app.state.hangar.store.list(ApiToken)}
    assert "kept" in names and "later" not in names


def _deployment_pod() -> dict:
    docs = list(yaml.safe_load_all((ROOT / "deploy/k8s/deployment.yaml").read_text()))
    return next(d for d in docs if d and d["kind"] == "Deployment")["spec"]["template"]["spec"]


def test_docs_restore_pod_matches_the_deployment():
    """The one-off restore pod in docs/gke.md (for a hangar that can't start) must see what the deployment sees:
    its settings, vault key, volume and service account (GCS), and no service links (HANGAR_PORT=tcp://...).
    The command was also run with --dry-run=server against a k3s API server (PR notes)."""
    import re

    doc = (ROOT / "docs/gke.md").read_text()
    overrides = re.search(r"--overrides='(\{.*?\})'", doc, re.S).group(1).replace('\'"$IMAGE"\'', "img:1")
    spec, dep = json.loads(overrides)["spec"], _deployment_pod()
    [c], [d] = spec["containers"], dep["containers"]
    assert c["command"] == ["hangar", "admin", "restore", "--local", "latest"]
    assert c["envFrom"] == d["envFrom"] and c["volumeMounts"] == d["volumeMounts"]
    assert spec["volumes"] == dep["volumes"] and spec["serviceAccountName"] == dep["serviceAccountName"]
    assert spec["enableServiceLinks"] is dep["enableServiceLinks"] is False
    assert spec["securityContext"] == dep["securityContext"]


# ====================================================================== 7. graceful shutdown
async def _running_run(fleet):
    sb = await fleet.create("plain")
    run = "run-0000abcd"
    await fleet.write_file(sb.id, f"/home/agent/.hangar/runs/{run}.log", b"working\n")
    return sb, run


async def test_a_long_poll_answers_at_once_on_shutdown(fleet):
    sb, run = await _running_run(fleet)
    t0 = time.monotonic()
    task = asyncio.create_task(fleet.agent_status(sb.id, run, wait_seconds=20))
    await asyncio.sleep(0.6)
    assert not task.done()
    fleet.closing.set()
    st = await asyncio.wait_for(task, 5)
    assert st["state"] == "running" and "working" in st["output_tail"] and time.monotonic() - t0 < 4
    assert not list((fleet.providers["docker"].boxes[sb.id] / "home/agent/.hangar/runs").glob(".wake-*"))
    t0 = time.monotonic()  # once shutting down, a new long-poll doesn't wait at all
    assert (await fleet.agent_status(sb.id, run, wait_seconds=20))["state"] == "running"
    assert time.monotonic() - t0 < 2


async def test_a_long_poll_sleeping_for_a_slot_wakes_on_shutdown(fleet):
    sb, run = await _running_run(fleet)
    fleet._waiters = asyncio.Semaphore(1)
    await fleet._waiters.acquire()  # every wait slot taken: it sleeps in the control plane instead
    task = asyncio.create_task(fleet.agent_status(sb.id, run, wait_seconds=20))
    await asyncio.sleep(0.2)
    fleet.closing.set()
    assert (await asyncio.wait_for(task, 3))["state"] == "running"


async def test_wait_for_turn_returns_on_shutdown(fleet, monkeypatch):
    from types import SimpleNamespace

    s = Session(name="w", environment="plain")
    monkeypatch.setattr(exchange, "_lookup", lambda *a: (s, None))
    monkeypatch.setattr(exchange, "_settled", lambda *a: False)
    monkeypatch.setattr(exchange, "turn_view", lambda *a: {"session": "w"})

    async def no_progress(*a):
        return []

    monkeypatch.setattr(exchange, "poll_progress", no_progress)

    async def report(*a):
        pass

    stub = SimpleNamespace(fleet=fleet, settings=SimpleNamespace(session_poll_seconds=2.0))
    task = asyncio.create_task(exchange.wait_for_turn(stub, None, "w", 60, report))
    await asyncio.sleep(0.3)
    fleet.closing.set()
    assert await asyncio.wait_for(task, 2) == {"done": False, "session": "w"}


@needs_git
async def test_a_turn_being_waited_on_keeps_its_pace_on_shutdown(jobs, monkeypatch):  # noqa: F811
    """Review of overnight/sweep: once shutting down, status polls no longer wait in the sandbox, so end_turn paces
    them itself instead of spinning on provider execs (an API-server call each on GKE) until the app's shutdown
    cancels it."""
    ss = jobs.sessions
    ss.settings.session_poll_seconds = 0.2
    sb, run = await _running_run(ss.fleet)
    turn = SessionTurn(message="m", run_id=run, started_at=time.time())
    s = Session(name="spin", environment="plain", sandbox_id=sb.id, turns=[turn])
    ss._sessions[s.id] = s
    calls = 0
    real = ss.fleet.agent_status

    async def counting(*a, **kw):
        nonlocal calls
        calls += 1
        return await real(*a, **kw)

    monkeypatch.setattr(ss.fleet, "agent_status", counting)
    ss.fleet.closing.set()
    task = asyncio.create_task(ss.end_turn(s, turn))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 2 <= calls <= 8, calls  # about one per HANGAR_SESSION_POLL_SECONDS, not one per exec round trip


async def test_the_shutdown_signal_reaches_the_app_before_uvicorn():
    seen: list[str] = []
    before = signal.signal(signal.SIGTERM, lambda sig, frame: seen.append("uvicorn"))
    try:
        closing = asyncio.Event()
        restore = shutdown.on_signal(asyncio.get_running_loop(), closing.set)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(closing.wait(), 2)
        assert seen == ["uvicorn"]  # and uvicorn still gets it
        restore()
        signal.raise_signal(signal.SIGTERM)
        assert seen == ["uvicorn", "uvicorn"]
    finally:
        signal.signal(signal.SIGTERM, before)


def test_a_backup_name_is_matched_whole():
    """Review of overnight/sweep: `$` also matches before a trailing newline, so a restore name with one passed."""
    assert backup.taken_at("hangar-20261013T010203Z.db.gz.enc") is not None
    for bad in ("hangar-20261013T010203Z.db.gz.enc\n", "xhangar-20261013T010203Z.db.gz.enc"):
        assert backup.taken_at(bad) is None


def test_pod_grace_covers_the_shutdown_drain():
    """k8s sends SIGKILL terminationGracePeriodSeconds after SIGTERM: uvicorn's drain (HANGAR_SHUTDOWN_GRACE_SECONDS)
    and the app's own shutdown must fit inside it."""
    assert _deployment_pod()["terminationGracePeriodSeconds"] >= Settings().shutdown_grace_seconds + 20


# ====================================================================== 9. usage of a deleted session
@needs_git
async def test_a_deleted_sessions_usage_stays_on_its_job(jobs):  # noqa: F811
    ss = jobs.sessions
    job = Job(title="t", instructions="x", environment="plain")
    jobs._jobs[job.id] = job
    jobs._save(job)
    for i, cost in ((1, 0.5), (2, 0.25)):
        s = Session(name=f"att-{i}", environment="plain", job_id=job.id,
                    turns=[SessionTurn(message="m",
                                       usage=Usage(input_tokens=100 * i, output_tokens=10, cost_usd=cost))])
        ss._sessions[s.id] = s
        ss.store.put(s)
    assert jobs.usage(job).cost_usd == 0.75
    ss.delete("att-1", requester="sean")
    u = jobs.usage(job)
    assert u.cost_usd == 0.75 and u.input_tokens == 300
    assert jobs.store.get(Job, job.id).deleted_session_usage  # persisted with the job, so a restart keeps it
