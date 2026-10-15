"""Flight records (--- clouddoctor): failed, timed-out and slow jobs and turns leave a redacted, sealed record."""

from __future__ import annotations

import asyncio
import gzip
import shutil

import pytest

from hangar import flightrec
from hangar.flightrec import FlightRecord, classify, signature
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, SubmitJob
from hangar.models import AuditEvent, CredentialKind, JobState, Session, SessionTurn, now
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures

needs_git = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

LEAK = "s3cr3t-stored-value-0123456789"
GH = "ghp_" + "A1b2C3d4" * 4 + "Zz9x"  # a GitHub token format secretscan knows, never stored
AGENT = f"""#!/usr/bin/env bash
mode=$(cat "$MODE_FILE" 2>/dev/null || echo ok)
case "$mode" in
  missing) echo "using {LEAK} and {GH}"; nosuchtool --version; exit 127 ;;
  ok) echo fixed > fix.txt && git add fix.txt && git commit -qm "fix"; echo done > "$HANGAR_JOB_DIR/RESULT.md" ;;
esac
"""


@pytest.fixture
def agent(world, monkeypatch):  # noqa: F811
    script = world["root"] / "rec-agent.sh"
    script.write_text(AGENT)
    monkeypatch.setitem(HARNESSES, "scripted", Harness(name="scripted", title="Scripted",
                                                       run=f"MODE_FILE={world['mode']} bash {script}"))
    return world


# ====================================================================== classification
def test_infrastructure_explains_a_failure_before_what_the_retry_printed():
    base = dict(texts=["bash: line 1: pnpm: command not found"], reason="agent_failed", outcome="exited",
                sandbox_cause=None, failed_stage=None, env_doctor=None, denies=[], requests=[], powered_down=False,
                restarted=False, slow=False, verify_tail=None)
    assert classify(**base) == ("missing_tool", "pnpm")
    assert classify(**{**base, "sandbox_cause": "pod_replaced"})[0] == "pod_replaced"
    assert classify(**{**base, "powered_down": True})[0] == "power_down"
    api = classify(**{**base, "texts": ['API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}']})
    assert api[0] == "model_api"
    egress = classify(**{**base, "texts": ["curl failed"], "denies": [{"host": "registry.npmjs.org"}] * 3})
    assert egress == ("egress", "registry.npmjs.org")
    install = classify(**{**base, "env_doctor": {"setup_failures": [{"step": "install", "message": "exit 1"}]}})
    assert install == ("install", "install")
    fallback = classify(**{**base, "texts": [], "env_doctor": {"build": {"fallback": True, "reason": "npm ci failed"}}})
    assert fallback == ("install", "npm ci failed")
    secret = classify(**{**base, "texts": [], "requests": [{"kind": "secret", "target": "NPM_TOKEN",
                                                            "state": "pending"}]})
    assert secret == ("secret", "NPM_TOKEN")
    assert classify(**{**base, "texts": [], "reason": "verify_failed",
                       "verify_tail": "ModuleNotFoundError: No module named 'yaml'"}) == ("missing_tool", "yaml")
    assert classify(**{**base, "texts": ["error: failed to create sandbox: boom"], "reason": "error",
                       "failed_stage": "building the codex harness pack"})[0] == "image_build"
    assert classify(**{**base, "texts": [], "reason": "bad_branch"})[0] == "bad_request"


def test_signatures_ignore_numbers_that_change_between_runs():
    a = signature("egress", "api.example.com:443", "o/r", "py")
    assert a == signature("egress", "api.example.com:8443", "o/r", "py")
    assert a != signature("egress", "api.example.com:443", "o/other", "py")
    assert signature("missing_tool", "pnpm", None, "node") == "missing_tool:pnpm@env:node"


# ====================================================================== jobs
@needs_git
async def test_a_failed_job_leaves_a_redacted_sealed_record(jobs, agent, fleet, broker, sessions):  # noqa: F811
    broker.put("leaky", "custom", CredentialKind.GENERIC, LEAK, env_var="LEAKY")
    agent["mode"].write_text("missing")
    job = jobs.submit(SubmitJob(instructions="x", repo=agent["url"], environment="jobenv", deliver="none"),
                      Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE)
    assert job.state == JobState.FAILED, [e.message for e in job.events]
    rec = flightrec.of(fleet)
    await rec.settle()
    r = rec.for_job(job.id)
    assert r is not None and r.kind == "job" and r.trigger == "failed"
    assert (r.cause, r.detail, r.transient) == ("missing_tool", "nosuchtool", False)
    assert r.signature == f"missing_tool:nosuchtool@{agent['url']}"
    assert r.run_ids and r.stages and r.versions["hangar"] and r.harness == "scripted"
    assert r.env_doctor is not None and r.env_doctor["egress"]["mode"]
    # the log is sealed: the blob is ciphertext, and what comes back is redacted
    blob = await sessions.blobs.get(r.log["key"])
    assert blob and LEAK.encode() not in blob and b"nosuchtool" not in blob
    text = await rec.read_log(sessions, r)
    assert "nosuchtool" in text and LEAK not in text and GH not in text
    assert LEAK not in r.model_dump_json() and GH not in r.model_dump_json()
    # the job's view and its resource name the record
    assert jobs.view(job)["flight_record"] == r.id


@needs_git
async def test_a_clean_job_leaves_no_record_unless_slow(jobs, agent, fleet, monkeypatch):  # noqa: F811
    agent["mode"].write_text("ok")
    job = jobs.submit(SubmitJob(instructions="x", repo=agent["url"], environment="jobenv", deliver="none"),
                      Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE)
    assert job.state == JobState.SUCCEEDED
    rec = flightrec.of(fleet)
    await rec.settle()
    assert rec.for_job(job.id) is None
    # against a norm of 0.01 s, the next one is slow
    monkeypatch.setattr(flightrec, "SLOW_FLOOR", 0.0)
    fleet.store.kv_set_json("flight:norm:job:jobenv", [0.01] * flightrec.NORM_MIN)
    job = jobs.submit(SubmitJob(instructions="y", repo=agent["url"], environment="jobenv", deliver="none"),
                      Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE)
    await rec.settle()
    r = rec.for_job(job.id)
    assert r is not None and r.trigger == "slow" and r.cause == "slow" and r.norm["p50"] == 0.01


# ====================================================================== session turns
async def test_a_failed_session_turn_is_recorded_as_transient(fleet, sessions):  # noqa: F811
    s = Session(name="solo", environment="plain", harness="claude-code")
    t = now()
    turn = SessionTurn(message="x", run_id="run-1", started_at=t - 30, finished_at=t, exit_code=1)
    rec = flightrec.of(fleet)
    rec.turn_ended(sessions, s, turn, "exited", "...\nAPI Error: 529 overloaded_error\n")
    await rec.settle()
    [r] = rec.list()
    assert r.kind == "turn" and r.cause == "model_api" and r.transient and r.session == "solo"
    assert "overloaded_error" in await rec.read_log(sessions, r)
    # a clean turn leaves nothing; a job's turn waits for its job
    rec.turn_ended(sessions, s, SessionTurn(message="y", run_id="run-2", started_at=t, finished_at=t, exit_code=0),
                   "exited", "fine")
    rec.turn_ended(sessions, Session(name="j", environment="plain", job_id="job-x"),
                   SessionTurn(message="z", run_id="run-3", started_at=t, finished_at=t, exit_code=1), "exited", "x")
    await rec.settle()
    assert len(rec.list()) == 1 and "run-3" in rec._pending


async def test_pending_logs_are_bounded(fleet, monkeypatch):
    monkeypatch.setattr(flightrec, "PENDING_MAX", 3)
    rec = flightrec.of(fleet)
    for i in range(6):
        rec.turn_ended(None, Session(name="j", environment="plain", job_id="job-x"),  # type: ignore[arg-type]
                       SessionTurn(message="z", run_id=f"r{i}"), "exited", "x" * (flightrec.LOG_TAIL + 10))
    assert list(rec._pending) == ["r3", "r4", "r5"]
    assert all(len(v[1]) == flightrec.LOG_TAIL for v in rec._pending.values())


async def test_records_and_their_logs_are_deleted_after_two_weeks(fleet, sessions):  # noqa: F811
    rec = flightrec.of(fleet)
    old = FlightRecord(kind="turn", trigger="failed", cause="error", environment="plain",
                       at=now() - (flightrec.RETENTION_DAYS + 1) * 86400)
    old.log = await rec._seal(sessions, old.id, "old log")
    fleet.store.put(old)
    new = FlightRecord(kind="turn", trigger="failed", cause="error", environment="plain")
    fleet.store.put(new)
    assert await rec.prune(sessions) == 1
    assert [r.id for r in rec.list()] == [new.id]
    assert await sessions.blobs.get(old.log["key"]) is None
    assert gzip is not None


def test_audit_since_finds_the_window_without_reading_older_rows(store):
    t = now()
    for i in range(50):
        store.audit(AuditEvent(ts=t - 1000 + i * 10, actor="sandbox:sb-1", action="egress.deny", target="sb-1",
                               detail={"host": f"h{i}"}))
        store.audit(AuditEvent(ts=t - 1000 + i * 10, actor="x", action="sandbox.exec", target="sb-1"))
    got = store.audit_since(t - 605, ("egress.deny",), target="sb-1")  # i >= 40
    assert [e.detail["host"] for e in got] == [f"h{i}" for i in range(49, 39, -1)]
    assert store.audit_since(t + 1, ("egress.deny",)) == []
    assert store.audit_since(t - 2000, ("egress.deny",), target="sb-2") == []


def test_list_range_is_newest_first(store):
    for i in range(5):
        store.put(FlightRecord(id=f"fr-{i}", kind="turn", trigger="failed", cause="error", environment="e",
                               at=1000.0 + i))
    assert [r.id for r in store.list_range(FlightRecord, "at", 1002.0)] == ["fr-4", "fr-3", "fr-2"]
    assert [r.id for r in store.list_range(FlightRecord, "at", 0, hi=1002.0)] == ["fr-1", "fr-0"]


async def test_a_record_never_fails_the_turn(fleet):
    rec = flightrec.of(fleet)
    # no session service at all: the hook logs and returns
    rec.turn_ended(None, None, None, "exited", "x")  # type: ignore[arg-type]
    rec.job_done(None, None)  # type: ignore[arg-type]
    await asyncio.sleep(0)
