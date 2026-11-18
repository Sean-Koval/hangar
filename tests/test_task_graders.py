"""A task's own grader (KAN-192): it replaces the experiment's for that task's trials, with its own command and its own
64 files and 1 MB, so a suite whose tasks are graded differently is one experiment. One canary covers every grader."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hangar import experiments, pinsets, trialcache
from hangar.jobs import JobError
from hangar.models import Job
from tests.test_grade_forgery import CMD, FIX, HIDDEN, grading  # noqa: F401 - grading is a fixture

MB = 1024 * 1024
BUDGET = {"max_cost_usd": 1}


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
    with pytest.raises(ValidationError, match="task u has no grader"):
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
    shared = experiments.Grader(command="c", files={"test_x.py": "def test_x(): assert True\n"})
    # computed on main before KAN-192: a task with no grader of its own keeps the digest it had
    assert trialcache.task_digest(plain, "genv", shared) == \
        "sha256:1fa892bf90b80cc2d2b5bb6f0b3eeaab23569fc25a9b7aecda1e23a5fe0f7ce5"

    def own(**g) -> str:
        return trialcache.task_digest(experiments.Task.model_validate(_task("t", {"files": HIDDEN, **g})), "genv")

    assert own(command="c") != trialcache.task_digest(plain, "genv", experiments.Grader(command="c", files=HIDDEN))
    assert own(command="c") != own(command="d") and own(command="c") != own(command="c", network="mirror")
    assert own(command="c") == own(command="c", canary="HANGAR-CANARY-aaaaaaaaaaaa")  # the canary is per experiment


def _pin(task_grader: str | None = None, lock: str | None = None) -> pinsets.PinSet:
    body = pinsets.PinSetBody(harness="tagent", harness_lock=lock, environment="plain",
                              environment_spec_digest="sha256:x", egress_digest="sha256:y", grader_digest="sha256:g",
                              tasks={"t": pinsets.TaskPin(repo="r", base_sha="b" * 40, grader_digest=task_grader)})
    return pinsets.PinSet(id=pinsets.pin_id(body), **body.model_dump())


def test_a_pin_set_pins_a_tasks_own_grader_and_its_id_is_unchanged_without_one():
    assert _pin(lock="team@sha256:" + "1" * 64).id == "pin-82d1c64f8c1a85bb"  # computed on main before KAN-192
    own = pinsets.grader_digest({"command": "own"})
    p = _pin(own)
    assert p.id != _pin().id and p.body()["tasks"]["t"]["grader_digest"] == own
    assert trialcache.parts_for(p, "t", "d")["grader_digest"] == own  # the cache key has the grader that runs
    assert trialcache.parts_for(_pin(), "t", "d")["grader_digest"] == "sha256:g"


def test_a_trials_request_runs_only_the_grader_its_task_pinned(fleet):
    from types import SimpleNamespace

    svc = SimpleNamespace(fleet=fleet)
    shared, own = {"command": "c"}, {"command": "own"}
    plain = _pin().model_copy(update={"grader_digest": pinsets.grader_digest(experiments.Grader(**shared))})
    pinned = _pin(pinsets.grader_digest(experiments.Grader(**own)))
    for p in (plain, pinned):
        fleet.store.put(p)

    def ask(p: pinsets.PinSet, task_grader: dict | None, grader: dict | None) -> None:
        task = {"id": "t", "instructions": "x", **({"grader": task_grader} if task_grader else {})}
        pinsets.start_trials(svc, pinsets.TrialsRequest(pin_set=p.id, task=task, budget=BUDGET, grader=grader), None)

    with pytest.raises(JobError, match="the grader isn't the pin set's"):  # the pinned grader named, another run
        ask(plain, {"command": "true"}, shared)
    with pytest.raises(JobError, match="the grader isn't the pin set's"):
        ask(pinned, None, shared)
    for p, task_grader, grader in ((plain, None, shared), (pinned, own, shared), (pinned, own, None)):
        with pytest.raises(JobError, match="no harness lock"):  # past the grader check
            ask(p, task_grader, grader)


async def test_an_experiments_pin_set_records_each_tasks_own_grader(fleet):
    from types import SimpleNamespace

    from hangar.models import Job as J

    spec = _spec([_task("t", {"command": "own"}), _task("u")], {"command": "shared"})
    job = J(title="e", instructions="x", environment="plain")
    tasks = {"t": {"repo": "r", "base_sha": "b" * 40}, "u": {"repo": "r", "base_sha": "b" * 40}}
    p = pinsets.experiment_pin(SimpleNamespace(fleet=fleet), job, "a", {"harness": "tagent"}, tasks, "plain",
                               spec.grader, None, {x.id: x.grader for x in spec.tasks if x.grader})
    assert p.grader_digest == pinsets.grader_digest(spec.grader)
    assert p.tasks["t"].grader_digest == pinsets.grader_digest(spec.tasks[0].grader)
    assert p.tasks["u"].grader_digest is None


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


async def test_the_index_hides_a_canary_only_the_tasks_graders_hold(grading):  # noqa: F811 - the fixture
    jobs, trial, remote = grading["jobs"], grading["trial"], grading["remote"]
    spec = _spec([{**_task("t", {"command": CMD, "files": HIDDEN, "timeout_seconds": 60}), "repo": f"file://{remote}"}])
    st = experiments.State(spec=spec.model_dump(mode="json"), pins={"tasks": {"t": {"repo": f"file://{remote}"}}})
    experiments.give_canary(spec, st.spec)
    canary = spec.tasks[0].grader.canary
    exp = Job(title="e", instructions="x", environment="genv")
    t = trial("t/a/1", FIX, summary=f"saw {canary}")
    await experiments._grade(jobs, exp, st, spec, t)
    st.trials = [t]
    exp.labels = {experiments.KIND_LABEL: experiments.KIND}
    exp.result.data = st.model_dump(mode="json", by_alias=True)

    class J:
        broker = jobs.broker

    assert canary and canary not in json.dumps(experiments.view(J(), exp))  # type: ignore[arg-type]
    assert t.contaminated
