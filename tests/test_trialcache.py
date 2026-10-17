"""The trial-result cache and its drift sentinel (#180): trials keyed by (harness digest, environment, task digest,
model, base commit, grader digest); `reuse: {min_trials}` returns cached trials and starts only the missing ones;
infra errors never count; a sentinel re-runs configured tasks bypassing the cache and writes v1/sentinel/{date}.json;
invalidation drops entries.

On the fake provider with the scripted agent of tests/test_harness_lock.py, real git and tmux."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hangar import outbox, pinsets, trialcache
from hangar.jobs import Caller
from hangar.models import JobBudget, JobState
from tests import test_harness_thaw as th
from tests.test_harness_lock import DONE
from tests.test_jobs import _until
from tests.test_pinsets import _pinned, _task

skillenv, world, jobs = th.skillenv, th.world, th.jobs
needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
BUDGET = JobBudget(max_turns=4)


def _req(p, world, n=3, k=3, **kw) -> pinsets.TrialsRequest:
    return pinsets.TrialsRequest(pin_set=p.id, task=_task(world), n=n, budget=BUDGET,
                                 reuse={"min_trials": k} if k else None, **kw)


async def _done(jobs, out):
    if out["experiment"]:
        exp = await _until(jobs, out["experiment"], DONE, timeout=180)
        assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]


# ====================================================================== keys
def test_every_key_component_counts():
    p = pinsets.PinSet(id="pin-" + "a" * 16, harness="tagent", harness_lock="team@sha256:" + "1" * 64,
                       harness_digest="sha256:h", environment="e", environment_spec_digest="sha256:e",
                       model_id="claude-opus-5-5", grader_digest="sha256:g", egress_digest="sha256:x",
                       tasks={"t": pinsets.TaskPin(repo="r", base_sha="b" * 40)})
    base = trialcache.parts_for(p, "t", "sha256:task")
    k = trialcache.key_of(base)
    for field, value in (("model_id", "claude-opus-5-6"), ("base_sha", "c" * 40), ("grader_digest", "sha256:g2"),
                         ("harness_digest", "sha256:h2"), ("task_digest", "sha256:t2"), ("environment", "sha256:e2")):
        assert trialcache.key_of({**base, field: value}) != k, field
    assert trialcache.parts_for(p.model_copy(update={"environment_digest": "sha256:full"}), "t", "x")["environment"] \
        == "sha256:full"  # the stricter environment pin, when the pin set has it
    # a task's digest is its definition, not its name; hidden tests count by content
    from hangar.experiments import Grader, Task

    a = Task(id="a", instructions="fix it", repo="r", verify="pytest")
    assert trialcache.task_digest(a, "e") == trialcache.task_digest(a.model_copy(update={"id": "b"}), "e")
    assert trialcache.task_digest(a, "e") != trialcache.task_digest(a.model_copy(update={"verify": "make"}), "e")
    assert trialcache.task_digest(a, "e", Grader(command="x", files={"t.py": "1"})) != \
        trialcache.task_digest(a, "e", Grader(command="x", files={"t.py": "2"}))


def test_only_counted_trials_of_a_pinned_key_are_cached(store):
    parts = {"harness_digest": "sha256:h", "environment": "sha256:e", "task_digest": "sha256:t",
             "model_id": "m", "base_sha": "b" * 40, "grader_digest": None}
    assert not trialcache.record(store, parts, "job-00000001.1", "j1", "failed", "infra_error")
    assert not trialcache.record(store, parts, "job-00000001.2", "j2", "cancelled", "cancelled")
    assert trialcache.record(store, parts, "job-00000001.3", "j3", "failed", "agent")  # a failure counts too
    assert not trialcache.record(store, parts, "job-00000001.3", "j3", "failed", "agent")  # once
    assert [x["trial_id"] for x in trialcache.lookup(store, parts)] == ["job-00000001.3"]
    unlocked = {**parts, "harness_digest": None}  # an overlay variant's: not fully pinned
    assert not trialcache.record(store, unlocked, "job-00000002.1", "j", "succeeded", "agent")
    assert trialcache.lookup(store, unlocked) == []
    assert trialcache.invalidate(store, model_id="other") == 0
    assert trialcache.invalidate(store, model_id="m") == 1 and trialcache.lookup(store, parts) == []
    with pytest.raises(ValueError):
        trialcache.invalidate(store)


# ====================================================================== reuse
@needs_tools
async def test_the_same_request_twice_starts_trials_only_the_first_time(jobs, world):
    p, _ = await _pinned(jobs, world)
    first = pinsets.start_trials(jobs, _req(p, world), Caller("svc", "svc"))
    assert first["experiment"] and first["cached"] == [] and len(first["trial_ids"]) == 3
    await _done(jobs, first)
    second = pinsets.start_trials(jobs, _req(p, world), Caller("svc", "svc"))
    assert second["experiment"] is None and sorted(second["cached"]) == sorted(first["trial_ids"])
    assert second["cached"] == second["trial_ids"]
    assert second["cache_key"] == first["cache_key"]
    t = pinsets.trial_view(jobs, second["trial_ids"][0])  # cached trials stay inspectable
    assert t["outcome_class"] == "agent" and t["pin_set"] == p.id
    # more than the cache has: the cached ones and only the missing ones start
    more = pinsets.start_trials(jobs, _req(p, world, n=4), Caller("svc", "svc"))
    assert sorted(more["cached"]) == sorted(first["trial_ids"]) and len(more["trial_ids"]) == 4 and more["experiment"]
    await _done(jobs, more)
    # not enough cached for min_trials: nothing reused
    fresh = pinsets.start_trials(jobs, _req(p, world, n=1, k=9), Caller("svc", "svc"))
    assert fresh["cached"] == [] and fresh["experiment"]
    await _done(jobs, fresh)
    # without reuse, trials always run
    again = pinsets.start_trials(jobs, _req(p, world, n=1, k=0), Caller("svc", "svc"))
    assert again["cached"] == [] and again["experiment"]
    await _done(jobs, again)
    # another task digest (flightdeck's own) misses
    other = pinsets.start_trials(jobs, _req(p, world, n=1, k=1, task_digest="sha256:flightdeck"), Caller("s", "s"))
    assert other["cached"] == [] and other["experiment"]
    await _done(jobs, other)


@needs_tools
async def test_drifted_trials_never_satisfy_the_cache(jobs, world):
    p, _ = await _pinned(jobs, world)
    spec_file = world["envs"] / "lockenv.yaml"
    d = json.loads(spec_file.read_text())
    d["description"] = "moved on"
    spec_file.write_text(json.dumps(d))
    jobs.fleet.catalog.reload()
    out = pinsets.start_trials(jobs, _req(p, world, n=1, k=1), Caller("svc", "svc"))
    await _done(jobs, out)
    assert pinsets.trial_view(jobs, out["trial_ids"][0])["infra_reason"] == "drift"
    assert trialcache.lookup(jobs.store, out["cache_parts"]) == []
    again = pinsets.start_trials(jobs, _req(p, world, n=1, k=1), Caller("svc", "svc"))
    assert again["cached"] == [] and again["experiment"]


# ====================================================================== the sentinel
@needs_tools
async def test_the_sentinel_runs_uncached_and_writes_its_report(jobs, world, settings, tmp_path):
    settings.outbox_url = str(tmp_path / "bucket")
    jobs.fleet.__dict__.pop("_outbox", None)
    p, _ = await _pinned(jobs, world)
    cached = pinsets.start_trials(jobs, _req(p, world, n=2, k=2), Caller("svc", "svc"))
    await _done(jobs, cached)
    settings.trial_sentinel = [{"pin_set": p.id, "task": _task(world), "n": 2}]
    run = trialcache.start(jobs, date="2026-10-17")
    [entry] = run["entries"]
    assert len(entry["trial_ids"]) == 2 and entry["parts"] == cached["cache_parts"]
    h = type("H", (), {"jobs": jobs})()
    await _until(jobs, entry["experiment"], DONE, timeout=180)
    await trialcache.minutely(h)
    rep = json.loads((Path(settings.data_dir) / "sentinel" / "2026-10-17.json").read_text())
    [task] = rep["tasks"]
    assert rep["schema"] == "hangar.sentinel/v1" and task["key"] == cached["cache_key"]
    assert task["sentinel"]["trials"] == 2 and task["cached"]["trials"] == 2
    assert task["sentinel"]["outcomes"] == {"succeeded": 2} == task["cached"]["outcomes"]
    # the sentinel's own trials didn't fill the cache (it bypasses it both ways)
    assert sorted(x["trial_id"] for x in trialcache.lookup(jobs.store, cached["cache_parts"])) == \
        sorted(cached["trial_ids"])
    await outbox.of(jobs.fleet).drain_once()
    assert json.loads((tmp_path / "bucket" / "v1" / "sentinel" / "2026-10-17.json").read_text())["date"] == \
        "2026-10-17"
    # flightdeck saw a change: invalidation drops the model's (or the harness's) entries
    assert trialcache.invalidate(jobs.store, harness_digest=p.harness_digest) == 1
    assert trialcache.lookup(jobs.store, cached["cache_parts"]) == []


async def test_the_sentinel_runs_at_its_slot_not_on_a_deploy(settings, monkeypatch):
    from types import SimpleNamespace

    from hangar.store import Store

    store = Store(settings.db_path)
    started: list[str] = []
    settings.trial_sentinel = [{"pin_set": "pin-" + "a" * 16, "task": {"id": "t", "instructions": "x"}}]
    settings.trial_sentinel_schedule, settings.eval_tz = "0 20 * * *", "UTC"
    jobs = SimpleNamespace(store=store, settings=settings)
    h = SimpleNamespace(jobs=jobs)
    monkeypatch.setattr(trialcache, "start", lambda j, caller=None, date=None: started.append(date))
    from datetime import UTC, datetime

    clock = {"t": datetime(2026, 10, 15, 20, 0, tzinfo=UTC).timestamp()}
    monkeypatch.setattr(trialcache, "now", lambda: clock["t"])
    await trialcache.minutely(h)
    assert started == []  # the first look only remembers the slot
    clock["t"] += 3600
    await trialcache.minutely(h)
    assert started == []  # same slot
    clock["t"] += 86400
    await trialcache.minutely(h)
    assert started == ["2026-10-16"]


def test_invalidation_over_rest(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings), headers={"Authorization": "Bearer t0ken"}) as c:
        store = c.app.state.hangar.store
        parts = {"harness_digest": "sha256:h", "environment": "e", "task_digest": "t", "model_id": "m",
                 "base_sha": "b" * 40, "grader_digest": None}
        trialcache.record(store, parts, "job-00000001.1", "j", "succeeded", "agent")
        assert c.post("/api/trials/cache/invalidate", json={}).status_code == 422
        assert c.post("/api/trials/cache/invalidate", json={"model_id": "m"}).json() == {"dropped": 1}
        assert c.post("/api/trials/sentinel/run").status_code == 409  # nothing configured
