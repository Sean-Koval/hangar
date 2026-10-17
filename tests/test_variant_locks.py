"""Experiment variants as harness locks (#135; spec 0014 §4): `{harness_lock: name@sha256:..., environment}`
variants claim every trial from the lock (#134) with turn-1 drift 0, overlay fields are refused beside a lock and
deprecated otherwise, and `hangar experiment freeze-variants` turns an overlay experiment into locks that reproduce
its trials' harness digests.

On the fake provider with the scripted agent of tests/test_harness_lock.py, real git and tmux."""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from hangar import experiments, harness_lock
from hangar.jobs import Caller, JobError
from hangar.models import JobState
from tests import test_harness_thaw as th
from tests.test_harness_lock import DONE
from tests.test_jobs import _until

skillenv, world, jobs = th.skillenv, th.world, th.jobs  # the thaw tests' fixtures: lockenv, the registry, the agent
ROOT = Path(__file__).resolve().parents[1]
INDEX_SCHEMA = json.loads((ROOT / "docs/api/experiment.schema.json").read_text())
SMOKE = ROOT / "tests/fixtures/experiments/semver-smoke.yaml"
needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
LOCK = "team@sha256:" + "0" * 64


def test_a_lock_takes_no_overlay_fields():
    v = experiments.Variant.model_validate({"harness_lock": LOCK, "environment": "lockenv"})
    assert v.overlay_fields() == []
    for field, value in (("skills", []), ("model", "claude-opus-5-5"), ("harness", "tagent"),
                         ("approvals", "auto"), ("instructions_append", "x"), ("skills_remove", ["a"])):
        with pytest.raises(ValueError, match=rf"`harness_lock` takes no overlay fields \({field}\)"):
            experiments.Variant.model_validate({"harness_lock": LOCK, field: value})
    for bad in ("team", "team@stable", "team@r3", "team@sha256:abc"):  # a lock by digest only: nothing that moves
        with pytest.raises(ValueError):
            experiments.Variant.model_validate({"harness_lock": bad})
    assert experiments.Variant.model_validate({"skills": [], "model": "m"}).overlay_fields() == ["model", "skills"]


def test_mixing_a_lock_and_skills_is_a_422(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    body = {"name": "e", "environment": "plain", "tasks": [{"id": "t", "instructions": "x", "repo": "r"}],
            "variants": {"a": {"harness_lock": LOCK, "skills": [{"name": "alpha"}]}}}
    with TestClient(create_app(settings)) as c:
        r = c.post("/api/experiments", json=body, headers={"Authorization": "Bearer t0ken"})
    assert r.status_code == 422
    assert "takes no overlay fields (skills)" in json.dumps(r.json())


def test_overlay_variants_are_noted_as_deprecated_and_the_locked_spec_is_printed():
    spec = experiments.ExperimentSpec.model_validate(yaml.safe_load(SMOKE.read_text()))
    notes = experiments.deprecation_notes(spec)
    assert len(notes) == 1 and notes[0].startswith("variant alpha-only: the overlay fields (skills, "
                                                   "instructions_append) are deprecated")
    assert "freeze-variants" in notes[0]
    raw = spec.model_dump(mode="json", exclude_none=True) | {"harness": "tagent", "grader": {
        "command": "true", "canary": experiments.CANARY_MASK}}
    out = experiments.locked_spec(raw, {"alpha-only": f"semver-smoke-alpha-only@sha256:{'1' * 64}",
                                        "env-default": f"semver-smoke-env-default@sha256:{'2' * 64}"})
    assert out["variants"] == {"alpha-only": {"harness_lock": f"semver-smoke-alpha-only@sha256:{'1' * 64}"},
                               "env-default": {"harness_lock": f"semver-smoke-env-default@sha256:{'2' * 64}"}}
    assert "harness" not in out and "canary" not in out["grader"] and out["tasks"] == raw["tasks"]
    experiments.ExperimentSpec.model_validate(out)  # what it prints is a valid spec
    assert experiments.lock_name("Semver Smoke!", "alpha_only") == "semver-smoke-alpha_only"


@needs_tools
async def test_lock_variants_run_pinned_with_no_drift(jobs, world, settings):
    _, digest = await th._frozen_registry(jobs, world)
    ref = f"team@{digest}"
    spec = experiments.ExperimentSpec.model_validate({
        "name": "locked", "environment": "lockenv", "trials": 2, "parallel": 2, "keep_minutes": 5,
        "tasks": [{"id": "t", "instructions": "work", "repo": world["url"]}],
        "variants": {"team": {"harness_lock": ref}}})
    with pytest.raises(JobError, match=r"variant bad: .*not in the registry"):
        experiments.submit(jobs, experiments.ExperimentSpec.model_validate(
            {**spec.model_dump(mode="json", exclude_none=True),
             "variants": {"bad": {"harness_lock": "team@sha256:" + "f" * 64}}}), Caller("svc", "svc"))
    exp = experiments.submit(jobs, spec, Caller("svc", "svc"))
    exp = await _until(jobs, exp.id, DONE, timeout=120)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    v = experiments.view(jobs, exp)
    jsonschema.validate(v, INDEX_SCHEMA)
    assert not any("deprecated" in n for n in v["notes"])
    pin = v["pins"]["variants"]["team"]
    assert pin["harness_lock"] == ref and pin["harness_digest"] == digest and pin["harness_revision"] == 1
    assert pin["model_source"] == "harness_lock" and pin["skills"] is None
    assert len(v["trials"]) == 2
    for t in v["trials"]:
        assert t["outcome"] == "succeeded", t
        assert t["harness_digest"] == digest and t["drift"] == [], t  # drift = 0
        child = jobs.get(t["job"])
        assert child.harness_lock["digest"] == digest and child.labels[experiments.PACKS_LABEL] == "selected"


@needs_tools
async def test_freeze_variants_round_trips_the_semver_smoke_experiment(jobs, world, settings, monkeypatch, tmp_path):
    reg = Path(settings.harness_repo)
    reg.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_load(SMOKE.read_text())
    body["tasks"][0]["repo"] = world["url"]
    exp = experiments.submit(jobs, experiments.ExperimentSpec.model_validate(body), Caller("svc", "svc"))
    exp = await _until(jobs, exp.id, DONE, timeout=120)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    v = experiments.view(jobs, exp)
    assert any(n.startswith("variant alpha-only: the overlay fields") for n in v["notes"])
    overlay_digest = {t["variant"]: t["harness_digest"] for t in v["trials"]}
    assert all(overlay_digest.values()) and len(set(overlay_digest.values())) == 2
    assert v["pins"]["variants"]["alpha-only"]["harness_digest"] == overlay_digest["alpha-only"]
    for t in v["trials"]:
        await th._recorded(jobs, jobs.get(t["job"]), 1)

    from hangar import cli

    class Client:  # cli._client() answered in process: the index and the freeze endpoint
        def request(self, method, path, params=None, **_):
            parts = path.strip("/").split("/")
            if parts[:2] == ["api", "experiments"]:
                data = experiments.view(jobs, jobs.get(parts[2]))
            elif parts[:2] == ["api", "jobs"] and parts[3] == "harness":
                data = harness_lock.wire(harness_lock.freeze_job(jobs, jobs.get(parts[2]), params["turn"],
                                                                 params["name"]))
            else:
                raise AssertionError(path)
            data = json.loads(json.dumps(data, default=str))
            return SimpleNamespace(status_code=200, json=lambda: data, text="")

    @contextlib.contextmanager
    def client():
        yield Client()

    monkeypatch.setattr(cli, "_client", client)
    out = CliRunner().invoke(cli.app, ["experiment", "freeze-variants", exp.id, "--out", str(reg / "harnesses"),
                                       "-o", str(tmp_path / "locked.yaml")])
    assert out.exit_code == 0, out.output
    locked = yaml.safe_load((tmp_path / "locked.yaml").read_text())
    refs = {k: x["harness_lock"] for k, x in locked["variants"].items()}
    assert refs == {k: f"semver-smoke-{k}@{overlay_digest[k]}" for k in ("alpha-only", "env-default")}
    for k in refs:
        assert harness_lock.check(reg / "harnesses" / f"semver-smoke-{k}")["ok"]
    th._commit(reg, "semver-smoke locks")

    # the printed spec runs, and each locked variant reproduces its overlay trial's harness
    again = experiments.submit(jobs, experiments.ExperimentSpec.model_validate(locked), Caller("svc", "svc"))
    again = await _until(jobs, again.id, DONE, timeout=120)
    assert again.state == JobState.SUCCEEDED, [e.message for e in again.events]
    v2 = experiments.view(jobs, again)
    assert not any("deprecated" in n for n in v2["notes"])
    for t in v2["trials"]:
        assert t["outcome"] == "succeeded" and t["drift"] == [], t
        assert t["harness_digest"] == overlay_digest[t["variant"]], t
