"""#166: an experiment trial that infrastructure ended (a preemption, the power-down) is recorded `infra_error`,
left out (`excluded`) and run again as a fresh trial with the same key; the retry is the one that counts.

On the fake provider with test_interfaces' scripted agent (`tagent`), which sleeps on its first run so the test can
take its sandbox away mid-turn."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import jsonschema
import pytest

from hangar import experiments, reconciler, traces
from hangar.harnesses import HARNESSES
from hangar.jobs import Caller
from hangar.models import JobState
from tests.test_interfaces import DONE, _attrs, _spans, jobs, world  # noqa: F401 - fixtures
from tests.test_jobs import _until

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

INDEX_SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "docs/api/experiment.schema.json").read_text())


@pytest.fixture
def slow_first(world, monkeypatch):  # noqa: F811
    """The first run sleeps (long enough to be cut off); every later one is the scripted agent."""
    root = Path(world["root"])
    wrapper = root / "first.sh"
    wrapper.write_text(f"if mkdir {root}/ran-once 2>/dev/null; then sleep 60; fi\nexec bash {root}/agent.sh\n")
    monkeypatch.setitem(HARNESSES, "tagent", HARNESSES["tagent"].model_copy(
        update={"run": f"STREAM={root}/stream bash {wrapper}"}))
    return root


def _spec(world) -> experiments.ExperimentSpec:  # noqa: F811
    return experiments.ExperimentSpec.model_validate({
        "name": "retry", "environment": "tenv", "trials": 1, "keep_minutes": 0,
        "tasks": [{"id": "look", "instructions": "Look around.", "repo": world["url"], "verify": "test -f seen.txt"}],
        "variants": {"a": {}}})


async def _first_turn(jobs, exp_id: str):  # noqa: F811
    for _ in range(600):
        st = experiments.load(jobs.get(exp_id))
        if st.trials and st.trials[0].job:
            child = jobs.get(st.trials[0].job)
            if child.turns and child.turns[0].run_id:
                return child
        await asyncio.sleep(0.05)
    raise AssertionError("the first trial's turn never started")


async def test_a_preempted_trial_is_excluded_and_retried_once(jobs, world, fleet, slow_first):  # noqa: F811
    exp = experiments.submit(jobs, _spec(world), Caller("svc", "svc"))
    child = await _first_turn(jobs, exp.id)
    # a spot preemption: the node goes, the reconciler finds the pod gone and records why
    sb = fleet.store.get(type(fleet.get(child.sandbox_id)), child.sandbox_id)
    await fleet.providers["docker"].destroy(sb)
    reconciler.record(sb, reconciler.POD_REPLACED)
    fleet._mark_gone(sb, "its pod is gone (node repair, scale-down or preemption)")
    exp = await _until(jobs, exp.id, DONE, timeout=120)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    v = experiments.view(jobs, exp)
    jsonschema.validate(v, INDEX_SCHEMA)
    first, retry = v["trials"]
    assert (first["key"], first["attempt"], first["outcome_class"], first["infra_reason"], first["excluded"]) == \
        ("look/a/1", 1, "infra_error", "preempted", True)
    assert first["state"] == "done" and first["grade"] is None
    assert (retry["key"], retry["attempt"], retry["retry_of"], retry["excluded"]) == \
        ("look/a/1", 2, first["job"], False)
    assert retry["outcome"] == "succeeded" and retry["outcome_class"] == "agent" and retry["verify"]["passed"]
    assert retry["job"] != first["job"]
    assert "excluded (infra_error)" in exp.result.summary and "1 succeeded" in exp.result.summary
    # each attempt keeps its own files; the key alone names the one that counts
    assert experiments.trial_file(jobs, exp, "look/a/1", "manifest.json")["subject"]["job"] == retry["job"]
    assert experiments.trial_file(jobs, exp, "look/a/1", "manifest.json", attempt=1)["subject"]["job"] == \
        first["job"]
    assert retry["links"]["trace"].endswith("/trials/look/a/1/trace?attempt=2")
    # the job and its turn carry the class; so does the turn span of its trace
    cut = jobs.get(first["job"])
    assert cut.result.outcome_class == "infra_error" and cut.result.infra_reason == "preempted"
    assert (cut.turns[0].outcome, cut.turns[0].outcome_class) == ("gone", "infra_error")
    turn = next(sp for sp in _spans(await traces.export_job(jobs, cut)) if _attrs(sp).get("hangar.kind") == "turn")
    assert _attrs(turn)["hangar.outcome.class"] == "infra_error"
    assert _attrs(turn)["hangar.outcome.infra_reason"] == "preempted"
    assert jobs.get(retry["job"]).result.outcome_class == "agent"


async def test_the_power_down_cuts_a_trial_off_and_it_runs_again_at_power_up(jobs, world, fleet, settings,  # noqa: F811
                                                                             slow_first):
    from tests.test_power import off

    settings.power_providers = ["docker"]
    settings.job_restart_stagger_seconds = 0
    exp = experiments.submit(jobs, _spec(world), Caller("svc", "svc"))
    child = await _first_turn(jobs, exp.id)
    s = jobs.sessions.get(child.session_id)
    assert await jobs.sessions.checkpoint_now(s.id, requester="power.sh") is not None  # the drain's checkpoint
    off(fleet.power)  # 01:00
    sb = fleet.get(child.sandbox_id)
    await fleet.providers["docker"].destroy(sb)
    fleet._mark_gone(sb, "disappeared from provider")
    for _ in range(400):  # the job would resume from its checkpoint at power-up: the experiment ends it instead
        st = experiments.load(jobs.get(exp.id))
        if len(st.trials) == 2 and st.trials[1].job:
            break
        await asyncio.sleep(0.05)
    st = experiments.load(jobs.get(exp.id))
    first = st.trials[0]
    assert (first.outcome_class, first.infra_reason, first.excluded) == ("infra_error", "power_down", True)
    assert jobs.get(first.job).state == JobState.CANCELLED
    assert st.trials[1].attempt == 2 and st.trials[1].retry_of == first.job
    fleet.power.mark("up", "power.sh")
    fleet.power.release(jobs)
    exp = await _until(jobs, exp.id, DONE, timeout=120)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    retry = experiments.load(exp).trials[1]
    assert retry.outcome == "succeeded" and retry.outcome_class == "agent" and not retry.excluded


def test_retries_stop_at_their_caps(fleet, settings):
    from types import SimpleNamespace

    js = SimpleNamespace(settings=settings)
    st = experiments.State(trials=[experiments.Trial(key=f"t/a/{i}", task="t", variant="a", trial=i)
                                   for i in range(1, 21)])
    spec = None
    assert experiments.infra_retry_cap(st) == 2  # 10% of 20 trials
    t = st.trials[0]
    t.outcome_class, t.infra_reason, t.job = "infra_error", "rate_limited", "job-1"
    r1 = experiments._retry(js, st, spec, t)  # type: ignore[arg-type]
    assert r1 is not None and (r1.key, r1.attempt, r1.retry_of) == ("t/a/1", 2, "job-1") and r1.ref == "t/a/1~2"
    r1.outcome_class, r1.job = "infra_error", "job-2"
    r2 = experiments._retry(js, st, spec, r1)  # type: ignore[arg-type]
    assert r2 is not None and r2.attempt == 3
    r2.outcome_class, r2.job = "infra_error", "job-3"
    assert experiments._retry(js, st, spec, r2) is None  # HANGAR_TRIAL_INFRA_RETRIES (2) per trial
    assert "on all 3 attempts" in st.notes[-1]
    other = st.trials[1]
    other.outcome_class, other.job = "infra_error", "job-4"
    assert experiments._retry(js, st, spec, other) is None  # the experiment's 2 are used: an outage, probably
    assert "probably a provider or cluster outage" in st.notes[-1]
    assert experiments._stop_starting(SimpleNamespace(), None, st, spec) == st.notes[-1]  # type: ignore[arg-type]
