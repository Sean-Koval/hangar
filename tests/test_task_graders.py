"""A task's own grader (KAN-192): it replaces the experiment's for that task's trials, with its own command and its own
64 files and 1 MB, so a suite whose tasks are graded differently is one experiment. One canary covers every grader."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hangar import experiments, trialcache
from hangar.models import Job
from tests.test_grade_forgery import CMD, FIX, HIDDEN, grading  # noqa: F401 - grading is a fixture

MB = 1024 * 1024


def _spec(tasks: list[dict], grader: dict | None = None, **extra) -> experiments.ExperimentSpec:
    body = {"name": "e", "environment": "genv", "variants": {"a": {}}, "tasks": tasks, **extra}
    if grader is not None:
        body["grader"] = grader
    return experiments.ExperimentSpec.model_validate(body)


def _task(tid: str, grader: dict | None = None) -> dict:
    return {"id": tid, "instructions": "x", **({"grader": grader} if grader else {})}


def test_a_tasks_own_grader_replaces_the_experiments_for_that_task_only():
    spec = _spec([_task("t", {"command": "own"}), _task("u")], {"command": "shared"})
    assert experiments.grader_for(spec, "t").command == "own"
    assert experiments.grader_for(spec, "u").command == "shared"
    alone = _spec([_task("t", {"command": "a"}), _task("u", {"command": "b"})])  # no experiment grader at all
    assert [experiments.grader_for(alone, x).command for x in ("t", "u")] == ["a", "b"]
    assert experiments.grader_for(_spec([_task("t")]), "t") is None  # ungraded, as before


def test_every_task_has_a_grader_or_none_does():
    with pytest.raises(ValidationError, match="tasks u have no grader"):
        _spec([_task("t", {"command": "own"}), _task("u")])


def test_the_file_caps_are_per_grader_with_a_ceiling_for_the_experiment():
    big = {"command": "c", "files": {"t.py": "#" * (900 * 1024)}}
    two = _spec([_task("t", big), _task("u", big)])  # 1.8 MB: over one grader's 1 MB, fine as two tasks' own
    assert sum(len(g.files["t.py"]) for g in (two.tasks[0].grader, two.tasks[1].grader)) > MB
    with pytest.raises(ValidationError, match="1 MB in all"):
        _spec([_task("t", {"command": "c", "files": {"a.py": "#" * MB, "b.py": "#"}})])
    with pytest.raises(ValidationError, match="64"):
        _spec([_task("t", {"command": "c", "files": {f"t{i}.py": "" for i in range(65)}})])
    with pytest.raises(ValidationError, match="16 MB in all"):
        _spec([_task(f"t{i}", {"command": "c", "files": {"t.py": "#" * MB}}) for i in range(17)])


def test_one_canary_for_every_grader():
    with pytest.raises(ValidationError, match="one canary"):
        _spec([_task("t", {"command": "c", "canary": "HANGAR-CANARY-aaaaaaaaaaaa"})],
              {"command": "c", "canary": "HANGAR-CANARY-bbbbbbbbbbbb"})
    spec = _spec([_task("t", {"command": "own"}), _task("u")], {"command": "shared"})
    stored = spec.model_dump(mode="json")
    assert experiments.give_canary(spec, stored) is True and experiments.give_canary(spec, stored) is False
    canary = spec.grader.canary
    assert canary and spec.tasks[0].grader.canary == canary and spec.tasks[1].grader is None
    assert experiments.spec_canary(stored) == canary and stored["tasks"][0]["grader"]["canary"] == canary
    only = _spec([_task("t", {"command": "own"})])  # no experiment grader: the task's carries it
    stored = only.model_dump(mode="json")
    experiments.give_canary(only, stored)
    assert experiments.spec_canary(stored) == only.tasks[0].grader.canary
    assert "canary" not in json.dumps(experiments.locked_spec(stored, {}))  # a re-run gets a new one


def test_a_tasks_own_grader_is_part_of_its_cache_identity():
    plain = experiments.Task.model_validate(_task("t"))
    shared = experiments.Grader(command="c", files=HIDDEN)
    before = trialcache.task_digest(plain, "genv", shared)
    assert before == trialcache.task_digest(plain.model_dump(mode="json") | {"grader": None}, "genv", shared)

    def own(**g) -> str:
        return trialcache.task_digest(experiments.Task.model_validate(_task("t", {"files": HIDDEN, **g})), "genv")

    assert own(command="c") != before  # graded by its own grader, not the experiment's
    assert own(command="c") != own(command="d") and own(command="c") != own(command="c", network="mirror")
    assert own(command="c") == own(command="c", canary="HANGAR-CANARY-aaaaaaaaaaaa")  # the canary is per experiment


async def test_each_task_is_graded_and_checked_by_its_own_grader(grading):  # noqa: F811 - the fixture
    jobs, trial = grading["jobs"], grading["trial"]
    remote = grading["remote"]
    spec = _spec([{**_task("t", {"command": CMD, "files": HIDDEN, "timeout_seconds": 60}), "repo": f"file://{remote}"},
                  {**_task("u"), "repo": f"file://{remote}"}], {"command": "exit 7", "timeout_seconds": 60})
    st = experiments.State(spec=spec.model_dump(mode="json"),
                           pins={"tasks": {x: {"repo": f"file://{remote}"} for x in ("t", "u")}})
    experiments.give_canary(spec, st.spec)
    exp = Job(title="e", instructions="x", environment="genv")
    t, u = trial("t/a/1", FIX), trial("u/a/1", FIX)
    await experiments._grade(jobs, exp, st, spec, t)
    await experiments._grade(jobs, exp, st, spec, u)
    assert t.grade["exit_code"] == 0 and t.grade["command"] == CMD and t.grade["canary_files"] == 1
    assert u.grade["exit_code"] == 7 and u.grade["command"] == "exit 7"
    assert st.task_checks["t"]["base"]["exit_code"] == 1 and st.task_checks["t"]["invalid"] is False
    assert st.task_checks["u"]["base"]["exit_code"] == 7
