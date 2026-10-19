"""The outbox (--- outbox #163, #189; docs/api/outbox.md): run capture to a bucket (a local directory here; a fake
GCS for the write-once and outage cases), and retention that waits for it.

End to end on the fake provider with test_interfaces' scripted agent (real git and tmux): every finished job turn
and session turn leaves run.json, manifest.json, trace.otlp.json, tests.json and patch.diff, valid against the
checked-in schema, with no seeded secret (verbatim, base64 or token-shaped) in any object."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import jsonschema
import pytest

from hangar import outbox, testresults
from hangar.jobs import Caller, SubmitJob
from hangar.models import CredentialKind, Job, JobState, Session, SessionTurn, Turn
from hangar.outbox import LocalSink, Outbox, OutboxItem
from tests.test_interfaces import DONE, SECRET, jobs, world  # noqa: F401 - fixtures
from tests.test_jobs import _until

needs_tmux = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
ROOT = Path(__file__).resolve().parents[1]
RUN_SCHEMA = json.loads((ROOT / "docs/api/run.schema.json").read_text())
MANIFEST_SCHEMA = json.loads((ROOT / "docs/api/manifest.schema.json").read_text())
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # GitHub-token-shaped: secretscan masks it
ENCODED = base64.b64encode(SECRET.encode()).decode()


def _def(name: str) -> dict:
    return {**RUN_SCHEMA["$defs"][name], "$defs": RUN_SCHEMA["$defs"]}


def _objects(bucket: Path) -> dict[str, bytes]:
    return {str(p.relative_to(bucket)): p.read_bytes() for p in bucket.rglob("*") if p.is_file()}


def _clean(blob: bytes) -> None:
    text = blob.decode(errors="replace")
    assert SECRET not in text and ENCODED[4:24] not in text and TOKEN not in text


async def _drained(o: Outbox, timeout: float = 30) -> None:
    for _ in range(int(timeout / 0.05)):
        pending = o.store.list_where(OutboxItem, state="pending")
        if not o._tasks and not o._capturing and not pending:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"outbox not drained: {[i.key for i in o.store.list_where(OutboxItem, state='pending')]}")


@pytest.fixture
async def ob(jobs, fleet, settings, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(outbox, "DRAIN_EVERY", 0.05)
    settings.outbox_url = str(tmp_path / "bucket")
    o = outbox.of(fleet)
    o.start(jobs.sessions, jobs)
    yield o
    await o.stop()


def _agent_leaks(world) -> None:  # noqa: F811
    """The scripted agent also leaves the secret base64-encoded and a token-shaped string in its work."""
    agent = world["root"] / "agent.sh"
    text = agent.read_text().replace('cat "$STREAM.after"',
                                     f'echo "{ENCODED} {TOKEN} {SECRET}" > leak.txt\ncat "$STREAM.after"')
    agent.write_text(text)


JUNIT = (f"<testsuite name='s' tests='2' failures='1'><testcase classname='t' name='ok'/>"
         f"<testcase classname='t' name='bad'><failure message='boom {SECRET}'/></testcase></testsuite>")
VERIFY = f"mkdir -p test-results && printf \"%s\" \"{JUNIT}\" > test-results/junit.xml && test -f seen.txt"


# ====================================================================== end to end
@needs_tmux
async def test_a_job_turn_lands_whole_valid_and_redacted(ob, jobs, world, settings):  # noqa: F811
    _agent_leaks(world)
    job = jobs.submit(SubmitJob(instructions=f"Look around. Context: {SECRET}", repo=world["url"],
                                environment="tenv", verify=VERIFY, deliver="none"), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=40)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    await _drained(ob)
    s = jobs._session(job)
    t = s.turns[0]
    bucket = Path(settings.outbox_url)
    objs = _objects(bucket)
    runs = {k.rsplit("/", 1)[0] for k in objs if k.startswith("v1/runs/")}
    assert len(runs) == 1
    prefix = runs.pop()
    date = time.strftime("%Y-%m-%d", time.gmtime(t.finished_at))
    assert prefix == f"v1/runs/{date}/{s.id}.{t.run_id}"
    names = {k.rsplit("/", 1)[1] for k in objs if k.startswith(prefix + "/")}
    assert names == {"run.json", "manifest.json", "trace.otlp.json", "tests.json", "patch.diff"}
    for blob in objs.values():
        _clean(blob)
    run = json.loads(objs[f"{prefix}/run.json"])
    jsonschema.validate(run, RUN_SCHEMA)
    assert set(run) <= set(RUN_SCHEMA["properties"]), set(run) - set(RUN_SCHEMA["properties"])
    assert run["source"] == "job" and run["job_id"] == job.id and run["run_id"] == t.run_id
    assert run["session_id"] == s.id and run["turn"] == 1 and run["job_turn"] == 1 and run["job_source"] == "api"
    assert run["pinned"] is False and run["untrusted"] is False
    assert run["manifest_digest"] == t.manifest and run["harness"] == "tagent"
    assert run["verify"]["exit_code"] == 0 and run["verify"]["passed"] is True
    assert run["objects"] == ["manifest.json", "patch.diff", "tests.json", "trace.otlp.json"]
    assert run["withheld"] == [] and run["start_sha"] == s.base_sha and run["head_sha"]
    assert run["diff_files"] == ["seen.txt"] and run["usage"]["output_tokens"] == 35  # the delivered change's
    m = json.loads(objs[f"{prefix}/manifest.json"])
    jsonschema.validate(m, MANIFEST_SCHEMA)
    assert m["digest"] == t.manifest
    tr = json.loads(objs[f"{prefix}/trace.otlp.json"])
    turn = [sp for rs in tr["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]]
    assert any(a["key"] == "hangar.run.id" and a["value"]["stringValue"] == t.run_id
               for sp in turn for a in sp["attributes"])
    tests = json.loads(objs[f"{prefix}/tests.json"])
    jsonschema.validate(tests, _def("tests"))
    assert tests["runs"][0]["kind"] == "verify" and tests["runs"][0]["trusted"] is True
    assert tests["runs"][0]["counted"] is True  # --- #164: what predicates use, as the trace has it
    assert tests["runs"][0]["cases"] == {"t::ok": "passed", "t::bad": "failed"}  # every case, not only failures
    assert [e["name"] for e in tests["events"]] == ["test.run", "test.case"]
    patch = objs[f"{prefix}/patch.diff"].decode()
    assert "seen.txt" in patch and "leak.txt" in patch  # untracked work too
    assert "+[hangar: secret removed] [hangar: secret removed] [hangar: secret removed]\n" in patch, patch
    # write-once: capturing the same run again queues nothing new and overwrites nothing
    before = _objects(bucket)
    ob.job_turn(jobs, job, job.turns[0], s)
    await _drained(ob)
    assert _objects(bucket) == before
    assert ob.exported(s.id, t.run_id) and ob.exported(s.id)


@needs_tmux
async def test_a_session_turn_is_captured_as_source_session(ob, jobs, world, settings):  # noqa: F811
    from hangar.sessions import NewSession

    svc = jobs.sessions
    s = svc.new(NewSession(name="solo", environment="tenv", repo=world["url"], message="look around"), "admin",
                "admin")
    for _ in range(600):
        s = svc.get("solo")
        if s.turns and s.turns[0].finished_at and not svc.busy(s.id):
            break
        await asyncio.sleep(0.05)
    await _drained(ob)
    objs = _objects(Path(settings.outbox_url))
    key = next(k for k in objs if k.endswith(f"{s.id}.{s.turns[0].run_id}/run.json"))
    run = json.loads(objs[key])
    jsonschema.validate(run, RUN_SCHEMA)
    assert run["source"] == "session" and run["job_id"] is None and run["verify"] is None and not run["pinned"]
    assert "tests.json" not in run["objects"] and {"manifest.json", "trace.otlp.json"} <= set(run["objects"])


@needs_tmux
async def test_jobs_only_leaves_out_sessions(ob, jobs, world, settings):  # noqa: F811
    from hangar.sessions import NewSession

    settings.outbox_sessions = "jobs_only"
    svc = jobs.sessions
    s = svc.new(NewSession(name="private", environment="tenv", repo=world["url"], message="hi"), "admin", "admin")
    for _ in range(600):
        s = svc.get("private")
        if s.turns and s.turns[0].finished_at and not svc.busy(s.id):
            break
        await asyncio.sleep(0.05)
    await _drained(ob)
    assert not ob.store.list_where(OutboxItem, session_id=s.id)
    assert ob.exported(s.id)  # nothing will ever export it: retention doesn't wait


@needs_tmux
async def test_the_upload_is_off_the_turn_path(jobs, world, fleet, settings, monkeypatch):  # noqa: F811
    """A GCS that takes a minute per object doesn't hold a job up: the job finishes, its objects wait."""
    calls: list[httpx.Request] = []

    async def slow(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(60)
        return httpx.Response(200, json={})

    settings.outbox_url = "gs://bucket/hangar"
    o = outbox.of(fleet)
    o.sink = outbox.GCSSink("gs://bucket/hangar", transport=httpx.MockTransport(slow), token="t")
    monkeypatch.setattr(outbox, "DRAIN_EVERY", 0.05)
    o.start(jobs.sessions, jobs)
    try:
        t0 = time.monotonic()
        job = jobs.submit(SubmitJob(instructions="go", repo=world["url"], environment="tenv",
                                    verify="test -f seen.txt", deliver="none"), Caller("svc", "svc"))
        job = await _until(jobs, job.id, DONE, timeout=40)
        took = time.monotonic() - t0
        assert job.state == JobState.SUCCEEDED and took < 30, took
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(0.05)
        assert calls  # it is uploading, in the background
        assert o.store.list_where(OutboxItem, state="pending")
    finally:
        await o.stop()


# ====================================================================== write-once, outages, restarts
async def test_a_local_sink_never_overwrites(tmp_path):
    sink = LocalSink(tmp_path / "b")
    assert await sink.put_once("v1/x/run.json", gzip.compress(b'{"a": 1}'), "application/json") is True
    assert await sink.put_once("v1/x/run.json", gzip.compress(b'{"a": 2}'), "application/json") is False
    assert (tmp_path / "b/v1/x/run.json").read_bytes() == b'{"a": 1}'
    with pytest.raises(ValueError):
        await sink.put_once("../escape", gzip.compress(b"x"), "text/plain")


async def test_gcs_uploads_are_create_only_and_a_412_is_done():
    seen: list[httpx.Request] = []
    have: set[str] = set()

    def gcs(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = request.url.params["name"]
        if name in have:
            return httpx.Response(412, json={"error": {"message": "precondition"}})
        have.add(name)
        return httpx.Response(200, json={"name": name})

    sink = outbox.GCSSink("gs://runs/team", transport=httpx.MockTransport(gcs), token="tok")
    assert await sink.put_once("v1/runs/d/s.r/run.json", gzip.compress(b"{}"), "application/json") is True
    assert await sink.put_once("v1/runs/d/s.r/run.json", gzip.compress(b"{}"), "application/json") is False
    p = seen[0].url.params
    assert p["ifGenerationMatch"] == "0" and p["contentEncoding"] == "gzip"
    assert p["name"] == "team/v1/runs/d/s.r/run.json"
    assert seen[0].headers["content-type"] == "application/json" and seen[0].method == "POST"
    assert all(r.method == "POST" for r in seen)  # never a DELETE, never a rewrite
    await sink.close()


class FlakySink:
    def __init__(self):
        self.down = True
        self.objects: dict[str, bytes] = {}
        self.description = "flaky"

    async def put_once(self, key: str, gz: bytes, content_type: str) -> bool:
        if self.down:
            raise httpx.ConnectError("GCS is down")
        if key in self.objects:
            return False
        self.objects[key] = gzip.decompress(gz)
        return True

    async def close(self) -> None:
        pass


def _staged(o: Outbox, n: int = 2) -> list[str]:
    keys = []
    for i in range(n):
        keys += o._stage(f"ses-x.run-{i:08x}", f"v1/runs/2026-10-16/ses-x.run-{i:08x}/",
                         {"manifest.json": {"m": i}, "patch.diff": f"diff {i}\n"},
                         record={"schema": outbox.SCHEMA}, session_id="ses-x", run_id=f"run-{i:08x}")
    return keys


async def test_an_outage_keeps_items_pending_with_backoff_and_a_restart_keeps_the_queue(fleet, settings, store,
                                                                                         monkeypatch):
    settings.outbox_url = str(settings.data_dir / "unused")
    clock = [time.time() + 1]
    monkeypatch.setattr(outbox, "now", lambda: clock[0])
    o = Outbox(fleet)
    sink = FlakySink()
    o.sink = sink
    keys = _staged(o)
    assert len(keys) == 6
    got = await o.drain_once()
    assert got == {"done": 0, "retry": 4, "failed": 0}  # run.json waits for its run's other objects
    items = {i.key: i for i in store.list_where(OutboxItem, state="pending")}
    first = items[keys[0]]
    assert first.attempts == 1 and first.next_at == clock[0] + 60 and "down" in first.error
    clock[0] += 30
    assert await o.drain_once() == {"done": 0, "retry": 0, "failed": 0}  # not due yet
    clock[0] += 31
    await o.drain_once()
    assert store.get(OutboxItem, keys[0]).next_at == clock[0] + 120  # doubling
    assert [outbox.backoff(n) for n in (1, 2, 3, 9, 10, 30)] == [60, 120, 240, 15360, 6 * 3600, 6 * 3600]
    assert o.status()["pending"] == 6 and not o.exported("ses-x", "run-00000000")

    # the control plane restarts during the outage: the queue and the staged payloads are on the data volume
    o2 = Outbox(fleet)
    o2.sink = sink
    sink.down = False
    clock[0] += 3600
    assert o2.retry() == 6
    await o2.drain_once()
    await o2.drain_once()  # run.json, now that the rest of its run is up
    assert not store.list_where(OutboxItem, state="pending")
    assert set(sink.objects) == set(keys)
    assert json.loads(sink.objects["v1/runs/2026-10-16/ses-x.run-00000000/run.json"])["schema"] == "hangar.run/v1"
    assert o2.exported("ses-x", "run-00000000") and o2.exported("ses-x")
    assert not list((settings.data_dir / "outbox" / "blobs").iterdir())  # staged payloads go once uploaded


async def test_a_payload_that_fails_redaction_is_never_uploaded(fleet, settings, store, broker):
    broker.put("seeded", "webhook", CredentialKind.GENERIC, SECRET)
    settings.outbox_url = str(settings.data_dir / "bucket")
    o = Outbox(fleet)
    clean = o.encode({"text": f"a {SECRET} b {ENCODED} c {TOKEN}", "list": [SECRET]}).decode()
    _clean(clean.encode())
    o._scrub = lambda obj: obj  # a redaction that misses: the check after it catches it
    keys = o._stage("ses-y.run-00000001", "v1/runs/2026-10-16/ses-y.run-00000001/",
                    {"patch.diff": f"+{SECRET}\n", "manifest.json": {"ok": True}}, record={"schema": outbox.SCHEMA},
                    session_id="ses-y", run_id="run-00000001")
    bad = store.get(OutboxItem, keys[0])
    assert bad.state == "failed" and "redaction" in bad.error and not o._blob(bad.key).exists()
    o._scrub = Outbox._scrub.__get__(o)
    await o.drain_once()
    await o.drain_once()
    objs = _objects(Path(settings.outbox_url))
    assert set(objs) == {"v1/runs/2026-10-16/ses-y.run-00000001/manifest.json",
                         "v1/runs/2026-10-16/ses-y.run-00000001/run.json"}
    run = json.loads(objs["v1/runs/2026-10-16/ses-y.run-00000001/run.json"])
    assert run["withheld"] == ["patch.diff"] and run["objects"] == ["manifest.json"]
    assert o.status()["failed_count"] == 1


# ====================================================================== experiments
def test_an_experiment_trial_is_source_experiment_pinned_with_its_grade(fleet, settings, store):
    from hangar import experiments
    from hangar.events import Activity

    settings.outbox_url = str(settings.data_dir / "bucket")
    o = Outbox(fleet)
    exp = Job(title="e", instructions="x", environment="plain", labels={experiments.KIND_LABEL: experiments.KIND})
    exp.result.data = experiments.State(spec={"name": "e", "grader": {"command": "hangar-grade pytest"}}).model_dump(
        mode="json", by_alias=True)
    store.put(exp)
    t = SessionTurn(message="go", run_id="run-000000aa", started_at=time.time() - 5, finished_at=time.time(),
                    outcome="exited", exit_code=0)
    s = Session(name="trial", environment="plain", turns=[t], job_id="job-trial")
    child = Job(id="job-trial", title="t", instructions="x", environment="plain", source="experiment",
                session_id=s.id, state=JobState.SUCCEEDED,
                labels={experiments.EXP_LABEL: exp.id, experiments.TRIAL_LABEL: "fix-bug/b/2",
                        experiments.VARIANT_LABEL: "b"})
    child.turns[:] = [Turn(run_id=t.run_id, outcome="exited")]
    store.put(child)
    store.put(s)
    svc = SimpleNamespace(activity=Activity(settings, fleet.broker.redact), fleet=fleet,
                          get=lambda ref: s)
    files, rec = o.build(svc, s, 1, job=child, job_turn=1)
    jsonschema.validate(rec, RUN_SCHEMA | {"required": [r for r in RUN_SCHEMA["required"]
                                                        if r not in ("objects", "withheld")]})
    assert rec["source"] == "experiment" and rec["pinned"] is True and rec["pin_set"] == f"{exp.id}/b"
    assert rec["experiment"] == {"id": exp.id, "trial_key": "fix-bug/b/2", "task": "fix-bug", "variant": "b",
                                 "trial": 2, "graded": True}
    assert rec["expects"] == ["grade.json"] and rec["job_source"] == "experiment"
    run = experiments.record_grader_run(SimpleNamespace(fleet=fleet, broker=fleet.broker), child,
                                        experiments.Grader(command="hangar-grade pytest"),
                                        {"exit_code": 0, "trust": "isolated"},
                                        b'<testsuite><testcase classname="t" name="ok"/></testsuite>')
    trial = SimpleNamespace(job=child.id, key="fix-bug/b/2",
                            grade={"command": "hangar-grade pytest", "exit_code": 0, "applied": True,
                                   "test_run": run.id})
    o.trial_graded(SimpleNamespace(store=store, sessions=svc), exp, trial)
    key = f"{outbox.run_prefix(s.id, t.run_id, t.finished_at)}grade.json"
    grade = store.get(OutboxItem, key)
    assert grade is not None and grade.state == "pending" and grade.job_id == child.id
    # --- #164: grade.json links the grader's TestRun and carries it as tests.json carries a turn's
    doc = json.loads(gzip.decompress(o._blob(key).read_bytes()))
    assert doc["test_run"] == run.id and doc["tests"]["runs"][0]["kind"] == "grader"
    assert doc["tests"]["runs"][0]["counted"] is True and doc["tests"]["runs"][0]["cases"] == {"t::ok": "passed"}
    assert doc["tests"]["events"][0]["name"] == "test.run"


# ====================================================================== job events
async def test_landing_transitions_become_job_event_objects(fleet, settings, store):
    settings.outbox_url = str(settings.data_dir / "bucket")
    store.kv_set(outbox.KV_SINCE, "0")
    o = Outbox(fleet)
    job = Job(title="t", instructions="x", environment="plain", repo="o/r", state=JobState.SUCCEEDED,
              untrusted_source="github:mallory")
    job.result.pr_url, job.result.pr_number = "https://github.com/o/r/pull/7", 7
    store.put(job)
    assert o.scan_jobs() == 1  # pr.open
    job.landing.approved_at, job.landing.approved_by = 2_000_000_000.0, "alice"
    job.landing.merged_at, job.landing.merged_by, job.landing.pr_state = 2_000_000_100.0, "bob", "merged"
    job.updated_at = time.time()
    store.put(job)
    assert o.scan_jobs() == 2  # review.first, pr.merge
    assert o.scan_jobs() == 0  # idempotent
    await o.drain_once()
    objs = _objects(Path(settings.outbox_url))
    assert sorted(json.loads(v)["kind"] for v in objs.values()) == ["pr.merge", "pr.open", "review.first"]
    merge_key = next(k for k, v in objs.items() if json.loads(v)["kind"] == "pr.merge")
    assert merge_key == f"v1/jobs/2033-05-18/{job.id}/events/2000000100000-pr.merge.json"
    for v in objs.values():
        ev = json.loads(v)
        jsonschema.validate(ev, _def("job_event"))
        assert ev["job_id"] == job.id and ev["untrusted"] is True and ev["pr"]["number"] == 7
    assert json.loads(objs[merge_key])["data"]["by"] == "bob"


# ====================================================================== #189: retention waits for the export
def _session_with_files(svc, settings, run_id: str = "run-0000abcd") -> Session:
    from hangar import manifest
    from hangar.events import RunEvent

    s = Session(name=f"s-{run_id}", environment="plain", turns=[SessionTurn(message="go", run_id=run_id,
                                                                        started_at=time.time() - 10,
                                                                        finished_at=time.time())])
    svc._sessions[s.id] = s
    svc.store.put(s)
    manifest.store(settings).put(s.id, 1, {"schema": "hangar.manifest/v1"})
    svc.activity.append(s.id, [RunEvent(turn=1, run_id=run_id, kind="progress", text="hi")])
    return s


@pytest.fixture
async def sessions(fleet, store):
    from hangar.sessions import SessionService
    from hangar.vault import Vault

    svc = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    yield svc


def _files(settings, s: Session) -> tuple[bool, bool]:
    from hangar import manifest

    return manifest.store(settings).get(s.id, 1) is not None, any(p.exists() for p in
                                                                  _log_paths(settings, s))


def _log_paths(settings, s: Session) -> list[Path]:
    return list((settings.data_dir).rglob(f"{s.id}*.jsonl"))


async def test_with_the_outbox_off_a_delete_drops_everything_now(sessions, settings):
    s = _session_with_files(sessions, settings)
    assert _files(settings, s) == (True, True)
    sessions.delete(s.id, "admin")
    assert not list((settings.data_dir).rglob(f"{s.id}*"))
    assert not (settings.data_dir / outbox.PENDING_DELETE).exists()


async def test_a_deleted_sessions_files_wait_for_the_export(sessions, settings, store, fleet):
    settings.outbox_url = str(settings.data_dir / "bucket")
    o = outbox.of(fleet)
    s = _session_with_files(sessions, settings)
    o._stage(outbox.run_key(s.id, "run-0000abcd"), "v1/runs/2026-10-16/x/", {"manifest.json": {}},
             record={"schema": outbox.SCHEMA}, session_id=s.id, run_id="run-0000abcd")
    assert not o.exported(s.id, "run-0000abcd")
    sessions.delete(s.id, "admin")
    held = settings.data_dir / outbox.PENDING_DELETE / s.id
    assert (held / "manifests" / "1.json").exists() and list((held / "events").iterdir())
    assert store.get(Session, s.id) is None  # the record goes as today
    assert o.sweep_pending_delete() == {"deleted": 0, "expired": 0, "held": 1}
    assert o.status()["pending_delete"]["sessions"] == 1
    await o.drain_once()
    await o.drain_once()
    assert o.exported(s.id, "run-0000abcd")
    assert o.sweep_pending_delete() == {"deleted": 1, "expired": 0, "held": 0}
    assert not held.exists()


async def test_the_grace_period_deletes_unexported_files_audited(sessions, settings, store, fleet, monkeypatch):
    settings.outbox_url = str(settings.data_dir / "bucket")
    settings.export_grace_days = 30
    o = outbox.of(fleet)
    s = _session_with_files(sessions, settings)
    o._stage(outbox.run_key(s.id, "run-0000abcd"), "v1/runs/2026-10-16/x/", {"manifest.json": {}},
             session_id=s.id, run_id="run-0000abcd")
    sessions.delete(s.id, "admin")
    real = time.time()
    monkeypatch.setattr(outbox, "now", lambda: real + 31 * 86400)
    assert o.sweep_pending_delete()["expired"] == 1
    assert not (settings.data_dir / outbox.PENDING_DELETE / s.id).exists()
    rows = [json.loads(r[0]) for r in store._db.execute("SELECT data FROM audit").fetchall()]
    ev = next(a for a in rows if a["action"] == "retention.unexported_deleted")
    assert ev["target"] == s.id and ev["detail"]["runs"] == ["run-0000abcd"] and ev["detail"]["bytes"] > 0


async def test_force_now_deletes_unexported_files_audited(sessions, settings, store, fleet):
    settings.outbox_url = str(settings.data_dir / "bucket")
    o = outbox.of(fleet)
    s = _session_with_files(sessions, settings)
    o._stage(outbox.run_key(s.id, "run-0000abcd"), "v1/runs/2026-10-16/x/", {"manifest.json": {}},
             session_id=s.id, run_id="run-0000abcd")
    sessions.delete(s.id, "admin", force_now=True)
    assert _files(settings, s) == (False, False)
    assert not (settings.data_dir / outbox.PENDING_DELETE / s.id).exists()
    actions = [json.loads(r[0])["action"] for r in store._db.execute("SELECT data FROM audit").fetchall()]
    assert "retention.unexported_deleted" in actions and "session.delete_now" in actions


async def test_archiving_an_attempt_keeps_its_event_log(sessions, settings):
    """strategies archives best-of-N attempt sessions it no longer uses: archive keeps their event logs."""
    s = _session_with_files(sessions, settings)
    sessions.archive(s.id, "job:job-1")
    assert _files(settings, s) == (True, True)


def test_a_failed_jobs_test_runs_outlive_the_windows_until_exported(fleet, settings, store):
    settings.outbox_url = str(settings.data_dir / "bucket")
    o = Outbox(fleet)
    t0 = 1_000_000.0
    failed = Job(title="f", instructions="x", environment="plain", state=JobState.FAILED, session_id="ses-f")
    ok = Job(title="o", instructions="x", environment="plain", state=JobState.SUCCEEDED, session_id="ses-o")
    store.put(failed)
    store.put(ok)
    runs = []
    for job in (failed, ok):
        r = testresults.TestRun(kind="verify", job_id=job.id, session_id=job.session_id, turn=1, at=t0,
                                cases="eJyrVgpILC5WslJQKs1LSS1SqgUAJIQFAQ==")
        store.put(r)
        runs.append(r)
    o._stage(outbox.run_key("ses-f", "run-0000000f"), "v1/runs/x/", {"manifest.json": {}}, session_id="ses-f",
             run_id="run-0000000f")
    outbox.RETAIN = o.retain_test_run
    try:
        testresults.prune(store, t0 + (testresults.CASES_DAYS + 1) * 86400)
        assert store.get(testresults.TestRun, runs[0].id).cases is not None  # held: not exported yet
        assert store.get(testresults.TestRun, runs[1].id).cases is None  # the usual window
        testresults.prune(store, t0 + (testresults.RETENTION_DAYS + 1) * 86400)
        assert store.get(testresults.TestRun, runs[0].id) is not None
        assert store.get(testresults.TestRun, runs[1].id) is None
        for i in store.list_where(OutboxItem, session_id="ses-f"):  # exported
            i.state = "done"
            store.put(i)
        testresults.prune(store, t0 + (testresults.RETENTION_DAYS + 1) * 86400)
        assert store.get(testresults.TestRun, runs[0].id) is None
    finally:
        outbox.RETAIN = None


def test_with_the_outbox_off_test_retention_is_unchanged(store):
    assert outbox.RETAIN is None
    t0 = 1_000_000.0
    store.put(Job(id="job-ff", title="f", instructions="x", environment="plain", state=JobState.FAILED))
    r = testresults.TestRun(kind="verify", job_id="job-ff", at=t0, cases="x")
    store.put(r)
    testresults.prune(store, t0 + (testresults.CASES_DAYS + 1) * 86400)
    assert store.get(testresults.TestRun, r.id).cases is None
    testresults.prune(store, t0 + (testresults.RETENTION_DAYS + 1) * 86400)
    assert store.get(testresults.TestRun, r.id) is None


# ====================================================================== status, doctor, CLI parsing
def test_doctor_warns_on_a_backlog_and_reports_held_bytes(fleet, settings, store, monkeypatch):
    settings.outbox_url = str(settings.data_dir / "bucket")
    o = outbox.of(fleet)
    h = SimpleNamespace(fleet=fleet, settings=settings)
    assert outbox.doctor_checks(h)[0]["status"] == "ok"
    monkeypatch.setattr(outbox, "WARN_PENDING", 3)
    _staged(o, 2)
    checks = {c["name"]: c for c in outbox.doctor_checks(h)}
    assert checks["run capture (outbox)"]["status"] == "warn"
    (settings.data_dir / outbox.PENDING_DELETE / "ses-z").mkdir(parents=True)
    (settings.data_dir / outbox.PENDING_DELETE / "ses-z" / "x.jsonl").write_bytes(b"x" * 2048)
    checks = {c["name"]: c for c in outbox.doctor_checks(h)}
    assert "1 deleted session(s)" in checks["retention held for export"]["detail"]
    o.sink = None  # off: no checks where it is optional
    assert outbox.doctor_checks(h) == []
    fleet.providers["kubernetes"] = fleet.providers["docker"]  # --- #273: on GKE, a warning that it is off
    (off,) = outbox.doctor_checks(h)
    assert off["name"] == "run capture (outbox)" and off["status"] == "warn" and off["detail"].startswith("off:")
    assert "run_bucket" in off["fix"] and "up.sh" in off["fix"]


def test_durations():
    assert outbox.parse_since("7d") == 7 * 86400 and outbox.parse_since("12h") == 43200
    assert outbox.parse_since("90") == 90
    with pytest.raises(ValueError):
        outbox.parse_since("soon")


# ====================================================================== terraform: write-once IAM
def test_terraform_gives_hangar_create_only_on_the_run_bucket():
    """Hangar's service account creates objects in the run bucket and nothing else: objectCreator there, and a deny
    policy takes delete and update away from it (its project-wide storage.admin is for Cloud Build)."""
    import re

    tf = (ROOT / "deploy/terraform/gke/outbox.tf").read_text()
    writer = re.search(r'resource "google_storage_bucket_iam_member" "runs_writer" \{(.*?)\n\}', tf, re.S).group(1)
    assert 'role   = "roles/storage.objectCreator"' in writer and "local.sa_email" in writer
    readers = re.search(r'resource "google_storage_bucket_iam_member" "runs_readers" \{(.*?)\n\}', tf, re.S).group(1)
    assert '"roles/storage.objectViewer"' in readers and "sa_email" not in readers
    grants = re.findall(r'role\s*=\s*"([^"]+)"', tf)
    assert sorted(set(grants)) == ["roles/storage.objectCreator", "roles/storage.objectViewer"]
    deny = re.search(r'resource "google_iam_deny_policy" "runs_write_once" \{(.*?)\n\}', tf, re.S).group(1)
    assert "serviceAccounts/${local.sa_email}" in deny
    assert '"storage.googleapis.com/objects.delete"' in deny and '"storage.googleapis.com/objects.update"' in deny
    assert "resource.matchTagId" in deny
    binding = re.search(r'resource "google_tags_location_tag_binding" "runs" \{(.*?)\n\}', tf, re.S).group(1)
    assert "google_storage_bucket.runs[0].name" in binding
    bucket = re.search(r'resource "google_storage_bucket" "runs" \{(.*?)\n\}', tf, re.S).group(1)
    assert "uniform_bucket_level_access = true" in bucket and 'matches_prefix = ["v1/tapes/"]' in bucket
    main = (ROOT / "deploy/terraform/gke/main.tf").read_text()
    assert "google_storage_bucket.runs" not in main  # no other grant on it
