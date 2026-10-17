"""Pin sets and trials (#178; docs/api/trials.md): an immutable, content-addressed pin set (harness lock, model,
environment, each task's base commit, grader, egress), `POST /api/trials` running n trials against it, turn 1 of each
checked against it (drift: `infra_error`/`drift`, not retried), and experiments recording a pin set per variant.

On the fake provider with the scripted agent of tests/test_harness_lock.py, real git and tmux."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import jsonschema
import pytest

from hangar import experiments, pinsets
from hangar.jobs import Caller, JobError
from hangar.models import JobBudget, JobState
from tests import test_harness_thaw as th
from tests.test_harness_lock import DONE, GIT
from tests.test_jobs import _until

skillenv, world, jobs = th.skillenv, th.world, th.jobs  # lockenv, the registry, the scripted agent
ROOT = Path(__file__).resolve().parents[1]
INDEX_SCHEMA = json.loads((ROOT / "docs/api/experiment.schema.json").read_text())
needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
BUDGET = JobBudget(max_turns=4)


def _head(world) -> str:
    remote = world["url"].removeprefix("file://")
    return subprocess.run(["git", "--git-dir", remote, "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def _advance(world) -> str:
    """A new commit on the remote's main (time passes: the branch moves on)."""
    root = Path(world["root"])
    seed = root / "seed"
    (seed / "later.txt").write_text("a month later\n")
    subprocess.run([*GIT, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(seed), "commit", "-qm", "later"], check=True)
    subprocess.run([*GIT, "-C", str(seed), "push", "-q", world["url"].removeprefix("file://"), "HEAD:refs/heads/main"],
                   check=True)
    return _head(world)


async def _pinned(jobs, world) -> tuple[pinsets.PinSet, str]:
    _, digest = await th._frozen_registry(jobs, world)
    body = await pinsets.resolve(jobs, f"team@{digest}", "lockenv",
                                 {"t": pinsets.TaskPin(repo=world["url"], base_sha=_head(world))})
    pinsets.check(jobs.fleet, body)
    return pinsets.put(jobs.fleet, body, "flightdeck"), digest


def _task(world) -> dict:
    return {"id": "t", "instructions": "work", "repo": world["url"]}


async def _trials(jobs, world, p, n: int = 1):
    out = pinsets.start_trials(jobs, pinsets.TrialsRequest(pin_set=p.id, task=_task(world), n=n, budget=BUDGET),
                               Caller("svc", "svc"))
    exp = await _until(jobs, out["experiment"], DONE, timeout=180)
    return out, exp


# ====================================================================== pin sets
@needs_tools
async def test_the_same_pin_set_twice_is_one_and_it_never_changes(jobs, world):
    p, digest = await _pinned(jobs, world)
    assert pinsets.ID.match(p.id) and p.harness_digest == digest and p.harness_lock == f"team@{digest}"
    assert p.tasks["t"].base_sha == _head(world) and p.created_by == "flightdeck"
    again = pinsets.put(jobs.fleet, pinsets.PinSetBody.model_validate(p.body()), "someone-else")
    assert again.id == p.id and again.created_by == "flightdeck"  # content-addressed: the first one stays
    changed = p.model_copy(update={"knowledge_snapshot": "kb-2026-10"})
    with pytest.raises(sqlite3.DatabaseError, match="pin sets are immutable"):
        jobs.store.put(changed)  # the database itself refuses
    with pytest.raises(sqlite3.DatabaseError, match="pin sets are immutable"):
        jobs.store.delete(pinsets.PinSet, p.id)
    assert pinsets.get(jobs.fleet, p.id).knowledge_snapshot is None


def test_a_pin_set_needs_a_lock_digest_a_concrete_model_and_base_commits(fleet, monkeypatch):
    from hangar import harness_thaw, modelpolicy

    base = {"harness": "claude-code", "environment": "plain", "environment_spec_digest": "sha256:x",
            "egress_digest": "sha256:y", "tasks": {"t": {"repo": "https://github.com/o/r.git", "base_sha": "a" * 40}}}
    with pytest.raises(pinsets.PinSetError, match="needs a harness lock and its digest"):
        pinsets.check(fleet, pinsets.PinSetBody.model_validate(base))
    lock = {"harness": "claude-code", "digest": "sha256:" + "1" * 64, "model": None, "version": "2", "approvals": None,
            "revision": 1}
    monkeypatch.setattr(harness_thaw, "pin", lambda *a, **k: dict(lock))
    locked = {**base, "harness_lock": "team@sha256:" + "1" * 64, "harness_digest": "sha256:" + "1" * 64}
    monkeypatch.setattr(type(modelpolicy.of(fleet)), "concreteness",
                        lambda self, h, m: (m != "claude-opus-latest", "alias" if m == "claude-opus-latest" else "list",
                                            None))
    with pytest.raises(pinsets.PinSetError, match="`claude-opus-latest` is not a concrete, dated model id"):
        pinsets.check(fleet, pinsets.PinSetBody.model_validate({**locked, "model_id": "claude-opus-latest"}))
    with pytest.raises(pinsets.PinSetError, match="have no base_sha"):
        pinsets.check(fleet, pinsets.PinSetBody.model_validate({**locked, "tasks": {"t": {"repo": "r"}}}))
    with pytest.raises(pinsets.PinSetError, match="harness_digest"):
        pinsets.check(fleet, pinsets.PinSetBody.model_validate({**locked, "harness_digest": "sha256:" + "2" * 64}))
    pinsets.check(fleet, pinsets.PinSetBody.model_validate({**locked, "model_id": "claude-opus-5-5"}))


def test_rest_creates_and_reads_pin_sets_and_has_no_put(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    monkeypatch.setattr(pinsets, "check", lambda fleet, body: None)
    settings.admin_token = "t0ken"
    body = {"harness": "claude-code", "harness_lock": "team@sha256:" + "1" * 64,
            "harness_digest": "sha256:" + "1" * 64, "environment": "plain", "environment_spec_digest": "sha256:x",
            "model_id": "claude-opus-5-5", "egress_digest": "sha256:y",
            "tasks": {"t": {"repo": "https://github.com/o/r.git", "base_sha": "a" * 40}}}
    with TestClient(create_app(settings), headers={"Authorization": "Bearer t0ken"}) as c:
        a, b = c.post("/api/pinsets", json=body).json(), c.post("/api/pinsets", json=body).json()
        assert a["id"] == b["id"] and a["schema"] == "hangar.pinset/v1" and a["model_id"] == "claude-opus-5-5"
        assert c.get(f"/api/pinsets/{a['id']}").json() == a
        assert c.put(f"/api/pinsets/{a['id']}", json=body).status_code == 405
        assert c.delete(f"/api/pinsets/{a['id']}").status_code == 405
        assert c.get("/api/pinsets/pin-0000000000000000").status_code == 404
        assert c.post("/api/pinsets", json={**body, "extra": 1}).status_code == 422
        r = c.post("/api/trials", json={"pin_set": a["id"], "task": {"id": "t", "instructions": "x"}, "n": 1})
        assert r.status_code == 422  # a per-trial budget is required
        assert c.get("/api/trials/job-00000000.1").status_code == 404


# ====================================================================== trials
@needs_tools
async def test_three_trials_carry_the_pin_sets_digests(jobs, world):
    p, digest = await _pinned(jobs, world)
    out, exp = await _trials(jobs, world, p, n=3)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    assert out["trial_ids"] == [f"{exp.id}.{i}" for i in (1, 2, 3)] and out["pin_set"] == p.id
    v = experiments.view(jobs, exp)
    jsonschema.validate(v, INDEX_SCHEMA)
    assert v["pins"]["variants"][p.id]["pin_set"] == p.id
    for tid in out["trial_ids"]:
        t = pinsets.trial_view(jobs, tid)
        assert t["pin_set"] == p.id and t["outcome"] == "succeeded" and t["outcome_class"] == "agent", t
        assert t["harness_digest"] == digest and t["environment_digest"] and t["drift"] == []
        assert t["runs"] and t["runs"][0]["outbox"].startswith("v1/runs/")
        child = jobs.get(t["job"])
        assert child.queue_class == "eval" and child.harness_lock["digest"] == digest
        assert jobs._session(child).base_sha == p.tasks["t"].base_sha
    with pytest.raises(KeyError):
        pinsets.trial_view(jobs, f"{exp.id}.9")


@needs_tools
async def test_a_drifted_environment_ends_the_trial_as_infra_error_drift(jobs, world):
    p, _ = await _pinned(jobs, world)
    spec_file = world["envs"] / "lockenv.yaml"
    d = json.loads(spec_file.read_text())
    d["description"] = "changed after the pin set"  # the environment moved on: its spec digest differs
    spec_file.write_text(json.dumps(d))
    jobs.fleet.catalog.reload()
    out, exp = await _trials(jobs, world, p, n=1)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    t = pinsets.trial_view(jobs, out["trial_ids"][0])
    assert (t["outcome_class"], t["infra_reason"], t["excluded"], t["attempts"]) == ("infra_error", "drift", True, 1)
    assert any("spec_digest" in x for x in t["drift"]), t["drift"]
    st = experiments.load(exp)
    assert any("drift): excluded, not retried" in n for n in st.notes)


@needs_tools
async def test_a_month_old_pin_set_reruns_the_same_commit_lock_and_model(jobs, world, monkeypatch):
    p, digest = await _pinned(jobs, world)
    pinned = p.tasks["t"].base_sha
    assert _advance(world) != pinned  # main moved on since
    out, exp = await _trials(jobs, world, p, n=1)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    t = pinsets.trial_view(jobs, out["trial_ids"][0])
    child = jobs.get(t["job"])
    assert jobs._session(child).base_sha == pinned and child.harness_lock["digest"] == digest
    assert jobs._session(child).model == p.model_id
    # hangar's harness lock moved on (an upgrade): the pin set's CLI can't be claimed, and says so
    from hangar.harnesses import HARNESSES

    monkeypatch.setitem(HARNESSES, "tagent", HARNESSES["tagent"].model_copy(update={"version": "9.9.9",
                                                                                   "install": "true"}))
    with pytest.raises(JobError, match="this hangar runs tagent 9.9.9"):
        pinsets.start_trials(jobs, pinsets.TrialsRequest(pin_set=p.id, task=_task(world), budget=BUDGET),
                             Caller("svc", "svc"))


def test_a_trials_request_must_match_its_pin_set(fleet):
    from types import SimpleNamespace

    p = pinsets.PinSet(id="pin-" + "a" * 16, harness="tagent", harness_lock="team@sha256:" + "1" * 64,
                       harness_digest="sha256:1", environment="plain", environment_spec_digest="sha256:x",
                       egress_digest="sha256:y", tasks={"t": pinsets.TaskPin(repo="r", base_sha="b" * 40)})
    fleet.store.put(p)
    svc = SimpleNamespace(fleet=fleet)
    for task, why in (({"id": "u", "instructions": "x"}, "isn't one of pin set"),
                      ({"id": "t", "instructions": "x", "base_sha": "c" * 40}, "isn't the pin set's"),
                      ({"id": "t", "instructions": "x", "repo": "other"}, "repo other isn't")):
        with pytest.raises(JobError, match=why):
            pinsets.start_trials(svc, pinsets.TrialsRequest(pin_set=p.id, task=task, budget=BUDGET), None)
    with pytest.raises(JobError, match="the grader isn't the pin set's"):
        pinsets.start_trials(svc, pinsets.TrialsRequest(pin_set=p.id, task={"id": "t", "instructions": "x"},
                                                        budget=BUDGET, grader={"command": "true"}), None)


# ====================================================================== experiments on top
@needs_tools
async def test_an_experiment_records_a_pin_set_per_variant(jobs, world):
    _, digest = await th._frozen_registry(jobs, world)
    spec = experiments.ExperimentSpec.model_validate({
        "name": "both", "environment": "lockenv", "trials": 1, "keep_minutes": 5,
        "tasks": [{"id": "t", "instructions": "work", "repo": world["url"]}],
        "variants": {"locked": {"harness_lock": f"team@{digest}"}, "overlay": {"skills": []}}})
    exp = experiments.submit(jobs, spec, Caller("svc", "svc"))
    exp = await _until(jobs, exp.id, DONE, timeout=180)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    v = experiments.view(jobs, exp)
    jsonschema.validate(v, INDEX_SCHEMA)
    locked, overlay = (v["pins"]["variants"][k]["pin_set"] for k in ("locked", "overlay"))
    assert pinsets.ID.match(locked) and pinsets.ID.match(overlay) and locked != overlay
    lp, op = pinsets.get(jobs.fleet, locked), pinsets.get(jobs.fleet, overlay)
    assert lp.harness_digest == digest and lp.harness_lock == f"team@{digest}"
    assert op.harness_digest is None and op.harness_lock is None and op.overlay_digest  # its first trial knows it
    assert {t["variant"]: t["pin_set"] for t in v["trials"]} == {"locked": locked, "overlay": overlay}
    assert all(t["outcome"] == "succeeded" and not t["excluded"] for t in v["trials"]), v["trials"]
