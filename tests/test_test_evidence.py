"""Trusted `test.run` events everywhere (#164): a `counted` rule behavior predicates use, experiment graders' hidden
tests as `grader` TestRuns, and a session's trace export carrying each turn's runs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from hangar import experiments, testreports, testresults, traces
from hangar.app import create_app
from hangar.models import Attempt, Job, JobState, Session, SessionTurn, Turn
from hangar.store import Store
from tests.conftest import FakeProvider

FIX = Path(__file__).parent / "fixtures" / "testreports"
JUNIT = b"""<?xml version="1.0"?>
<testsuite name="hidden" tests="2"><testcase classname="test_add" name="test_add"/>
<testcase classname="test_add" name="test_sub"><failure message="assert 1 == 2">boom HANGAR-CANARY-abc</failure>
</testcase></testsuite>"""


def _results(**outcomes: str) -> testreports.Results:
    r = testreports.Results()
    for cid, o in outcomes.items():
        r.cases[cid] = testreports.Case(id=cid, name=cid, outcome=o)  # type: ignore[arg-type]
    return r


def _attr(attrs: list[dict], key: str):
    v = next((a["value"] for a in attrs if a["key"] == key), None)
    return None if v is None else next(iter(v.values()))


def _spans(out: dict) -> list[dict]:
    return [sp for rs in out["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]]


# ====================================================================== counted
def test_counted_is_trusted_with_tests_and_a_complete_report(tmp_path):
    store = Store(tmp_path / "db")
    ok = testresults.record(store, results=_results(a="passed", b="failed"), kind="verify", repo="o/r")
    assert testresults.counted(ok) is True
    collect_only = testresults.record(store, results=_results(), kind="verify", repo="o/r", exit_code=0)
    assert collect_only.counts.get("total", 0) == 0 and testresults.counted(collect_only) is False
    partial = _results(a="passed")
    partial.complete = False
    incomplete = testresults.record(store, results=partial, kind="verify", repo="o/r")
    assert testresults.counted(incomplete) is False
    agent = testresults.record(store, results=_results(a="passed"), kind="in_turn", repo="o/r")
    assert testresults.counted(agent) is False
    # everywhere the run is shown: the REST views, tests.json's records and the span events
    assert testresults.view(ok)["counted"] is True and testresults.brief(collect_only)["counted"] is False
    ev = {e["name"]: e for e in testresults.trace_events(incomplete)}["test.run"]
    assert ev["attributes"]["hangar.test.run.counted"] is False
    assert testresults.trace_events(ok)[0]["attributes"]["hangar.test.run.counted"] is True


def test_a_trusted_kind_can_be_recorded_untrusted(tmp_path):
    store = Store(tmp_path / "db")
    r = testresults.record(store, results=_results(a="passed"), kind="grader", trusted=False)
    assert not r.trusted and not testresults.counted(r) and "grader" in testresults.TRUSTED


# ====================================================================== graders
def _jobs(store: Store) -> SimpleNamespace:
    broker = SimpleNamespace(redact=lambda s: s.replace("hunter2", "[redacted]") if isinstance(s, str) else s)
    return SimpleNamespace(fleet=SimpleNamespace(store=store), broker=broker)


def _grader(**kw) -> experiments.Grader:
    return experiments.Grader.model_validate({"command": "hangar-grade pytest -q", **kw})


def test_an_isolated_grade_with_a_junit_report_is_a_trusted_grader_run(tmp_path):
    store = Store(tmp_path / "db")
    trial = Job(title="t", instructions="x", environment="py", repo="https://github.com/acme/app.git",
                session_id="ses-1", turns=[Turn(run_id="run-0000aaaa")])
    g = _grader(canary="HANGAR-CANARY-abc")
    r = experiments.record_grader_run(_jobs(store), trial, g, {"exit_code": 1, "trust": "isolated"}, JUNIT)
    assert r.kind == "grader" and r.trusted and r.complete and testresults.counted(r)
    assert r.job_id == trial.id and r.agent_run_id == "run-0000aaaa" and r.counts["total"] == 2
    assert r.n_failing == 1 and "HANGAR-CANARY-abc" not in str(r.failing) and experiments.CANARY_MASK in str(r.failing)
    assert testresults.flaky(store, "acme/app") == {}  # hidden tests never reach the repo's list
    # below `isolated`, the code under test could have written the report: recorded, never trusted
    weak = experiments.record_grader_run(_jobs(store), trial, g, {"exit_code": 0, "trust": "best_effort"}, JUNIT)
    assert not weak.trusted and not testresults.counted(weak)
    # no report at all: a run from the exit code alone, incomplete
    bare = experiments.record_grader_run(_jobs(store), trial, g, {"exit_code": 0, "trust": "isolated"}, None)
    assert bare.exit_code == 0 and not bare.complete and bare.counts.get("total", 0) == 0
    assert not testresults.counted(bare) and bare.errors


def test_the_grader_run_is_on_the_trials_tests_and_trace(settings):
    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        h = app.state.hangar
        h.fleet.providers["docker"] = FakeProvider()
        c.headers["Authorization"] = "Bearer t0ken"
        trial = Job(title="t", instructions="x", environment="agent", branch="hangar/j", repo="acme/app",
                    state=JobState.SUCCEEDED)
        h.store.put(trial)
        jobs = SimpleNamespace(fleet=h.fleet, broker=h.fleet.broker)
        r = experiments.record_grader_run(jobs, trial, _grader(), {"exit_code": 1, "trust": "isolated"}, JUNIT)
        got = c.get(f"/api/jobs/{trial.id}/tests").json()
        assert got["runs"][0]["kind"] == "grader" and got["runs"][0]["trusted"] and got["runs"][0]["counted"]
        out = traces.job_export(h.jobs, trial, sessions=[])
        root = next(sp for sp in _spans(out) if sp["name"] == "invoke_workflow hangar.job")
        run = next(e for e in root["events"] if e["name"] == "test.run")
        assert _attr(run["attributes"], "hangar.test.run.id") == r.id
        assert _attr(run["attributes"], "hangar.test.run.kind") == "grader"
        assert _attr(run["attributes"], "hangar.test.run.trusted") is True
        assert _attr(run["attributes"], "hangar.test.run.counted") is True


# ====================================================================== session exports
def _svc(store: Store) -> SimpleNamespace:
    log = SimpleNamespace(_paths=lambda sid: [])
    fleet = SimpleNamespace(broker=SimpleNamespace(redact=lambda s: s), settings=SimpleNamespace(), catalog=None,
                            store=store)
    return SimpleNamespace(fleet=fleet, activity=SimpleNamespace(log=log), store=store)


def test_a_sessions_export_carries_each_turns_test_runs(tmp_path):
    store = Store(tmp_path / "db")
    t1 = SessionTurn(message="go", run_id="run-00000001", started_at=100.0, finished_at=200.0,
                     outcome="exited", exit_code=0)
    t2 = SessionTurn(message="go", run_id="run-00000002", started_at=300.0, finished_at=400.0,
                     outcome="exited", exit_code=0)
    s = Session(name="s", environment="py", turns=[t1, t2])
    job = Job(title="t", instructions="x", environment="py", session_id=s.id,
              turns=[Turn(run_id="run-00000001"), Turn(run_id="run-00000002")])
    store.put(job)
    a = testresults.record(store, results=_results(a="passed"), kind="in_turn", job_id=job.id, session_id=s.id,
                           turn=1, agent_run_id="run-00000001")
    # recorded before agent_run_id: found through the job turn that ran as run 2
    b = testresults.record(store, results=_results(a="failed"), kind="in_turn", job_id=job.id, session_id=s.id,
                           turn=2)
    base = testresults.record(store, results=_results(a="passed"), kind="base", job_id=job.id, session_id=s.id)
    assert [r.id for r in testresults.turn_runs(store, s, 1)] == [a.id]
    assert [r.id for r in testresults.turn_runs(store, s, 2)] == [b.id]
    out = traces.session_export(_svc(store), s)
    turns = [sp for sp in _spans(out) if _attr(sp["attributes"], "hangar.kind") == "turn"]
    runs = {_attr(sp["attributes"], "hangar.run.id"): [e for e in sp.get("events", []) if e["name"] == "test.run"]
            for sp in turns}
    assert [_attr(e["attributes"], "hangar.test.run.id") for e in runs["run-00000001"]] == [a.id]
    assert [_attr(e["attributes"], "hangar.test.run.id") for e in runs["run-00000002"]] == [b.id]
    every = [e for evs in runs.values() for e in evs]
    assert every and all(_attr(e["attributes"], "hangar.test.run.trusted") is False for e in every)
    assert all(_attr(e["attributes"], "hangar.test.run.counted") is False for e in every)
    assert base.id not in str(out)  # the base check is no turn's
    one = traces.session_export(_svc(store), s, turn=2)
    assert b.id in str(one) and a.id not in str(one)


def test_an_attempts_runs_are_its_sessions(tmp_path):
    store = Store(tmp_path / "db")
    s = Session(name="att", environment="py", turns=[SessionTurn(message="go", run_id="run-0000000a", started_at=1.0)])
    job = Job(title="t", instructions="x", environment="py", attempts=[Attempt(index=1, run_id="run-0000000a")])
    store.put(job)
    r = testresults.record(store, results=_results(a="passed"), kind="attempt", job_id=job.id, session_id=s.id,
                           attempt=1)
    assert [x.id for x in testresults.turn_runs(store, s, 1)] == [r.id]
