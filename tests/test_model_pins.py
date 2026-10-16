"""#167: an experiment's pin set holds only concrete, dated model ids; jobs and sessions keep working with aliases, and
their manifests say they weren't pinned (`model.concrete: false`).

On the fake provider with test_interfaces' scripted agent (`tagent`), which is given a model list of its own here
(Anthropic's), so its policies resolve as Claude Code's do."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from hangar import experiments, manifest, modelpolicy
from hangar.jobs import Caller, JobError, SubmitJob
from hangar.models import JobState
from tests.test_interfaces import DONE, jobs, world  # noqa: F401 - fixtures
from tests.test_jobs import _until

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs/api/manifest.schema.json").read_text())
INDEX_SCHEMA = json.loads((ROOT / "docs/api/experiment.schema.json").read_text())


@pytest.fixture
def listed_tagent(monkeypatch):
    """tagent resolves policies like Claude Code: from Anthropic's list, else the built-in ids."""
    monkeypatch.setitem(modelpolicy.PROVIDER, "tagent", "anthropic")
    monkeypatch.setitem(modelpolicy.SEED, "tagent", dict(modelpolicy.SEED["claude-code"]))


def test_an_experiment_on_a_floating_alias_is_refused_naming_a_concrete_id(fleet, settings):
    js = SimpleNamespace(fleet=fleet, settings=settings)
    base = {"name": "e", "environment": "agents", "tasks": [{"id": "t1", "instructions": "x", "repo": "r"}]}
    for alias in ("claude-opus-latest", "opus"):
        with pytest.raises(JobError) as e:
            experiments.submit(js, experiments.ExperimentSpec.model_validate(  # type: ignore[arg-type]
                {**base, "variants": {"a": {"harness": "claude-code", "model": alias}}}), None)
        msg = str(e.value)
        assert f"variant a: model `{alias}` is a floating alias" in msg
        assert "`balanced` resolves to `claude-opus-5-5` now" in msg and "claude-fable-5-1" in msg
    # the environment's own alias (agents.yaml: `model: sonnet`) is refused just the same
    with pytest.raises(JobError, match="model `sonnet` is a floating alias"):
        experiments.submit(js, experiments.ExperimentSpec.model_validate(  # type: ignore[arg-type]
            {**base, "variants": {"a": {"harness": "claude-code"}}}), None)
    # OpenRouter's aliases and an id of no known shape (no list cached) too
    with pytest.raises(JobError, match="openrouter/auto"):
        experiments.submit(js, experiments.ExperimentSpec.model_validate(  # type: ignore[arg-type]
            {**base, "variants": {"a": {"harness": "pi", "model": "openrouter/auto"}}}), None)
    with pytest.raises(JobError, match="not a model id hangar can tell is concrete.*judged by its shape alone"):
        experiments.submit(js, experiments.ExperimentSpec.model_validate(  # type: ignore[arg-type]
            {**base, "variants": {"a": {"harness": "codex", "model": "gpt-5.3-codex"}}}), None)


async def test_an_experiment_on_balanced_pins_the_concrete_id_and_runs(jobs, world, fleet, listed_tagent):  # noqa: F811
    spec = experiments.ExperimentSpec.model_validate({
        "name": "pinned", "environment": "tenv", "model": "balanced",
        "tasks": [{"id": "look", "instructions": "Look around.", "repo": world["url"], "verify": "test -f seen.txt"}],
        "variants": {"a": {}, "fable": {"model": "claude-fable-5-1"}}, "keep_minutes": 0})
    exp = experiments.submit(jobs, spec, Caller("svc", "svc"))
    exp = await _until(jobs, exp.id, DONE, timeout=90)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    v = experiments.view(jobs, exp)
    jsonschema.validate(v, INDEX_SCHEMA)
    a, fable = v["pins"]["variants"]["a"], v["pins"]["variants"]["fable"]
    assert (a["model"], a["model_policy"], a["model_concrete"], a["model_concrete_basis"]) == \
        ("claude-opus-5-5", "balanced", True, "pattern")
    assert (fable["model"], fable["model_policy"], fable["model_concrete"]) == ("claude-fable-5-1", None, True)
    assert any("judged by its shape alone" in n for n in v["notes"])  # no list was cached: said so
    for t in v["trials"]:
        child = jobs.get(t["job"])
        assert child.model == v["pins"]["variants"][t["variant"]]["model"]
        assert t["outcome"] == "succeeded"
        m = experiments.trial_file(jobs, exp, t["key"], "manifest.json")
        assert m["model"]["concrete"] is True


async def test_a_job_on_an_alias_runs_and_its_manifest_says_it_isnt_pinned(jobs, world, fleet, listed_tagent):  # noqa: F811
    job = jobs.submit(SubmitJob(instructions="Look around.", repo=world["url"], environment="tenv", model="opus",
                                verify="test -f seen.txt", deliver="none"), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=40)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    s = jobs._session(job)
    for _ in range(100):  # recorded in the background as the turn starts
        if s.turns[0].manifest:
            break
        await asyncio.sleep(0.05)
    m = manifest.for_session(jobs.sessions, s, 1)
    jsonschema.validate(m, SCHEMA)
    assert m["model"]["id"] == "opus" and m["model"]["concrete"] is False
