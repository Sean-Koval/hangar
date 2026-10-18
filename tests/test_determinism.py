"""#179: the nightly environment determinism report. The previous day's recorded production turns of each repo are
replayed strictly (`tolerate: tool`), and the steps whose outputs don't reproduce on an identical tree are ranked by
frequency into `v1/determinism/{date}/{repo}.json`; a flaky-list test seen in a mismatched output gets the step as
evidence."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from hangar import determinism, experiments, replay, testresults
from hangar.models import Job, JobState, Session, SessionTurn
from tests.test_replay import Model, deterministic, gw, record, replay_it  # noqa: F401 - gw is a fixture

REPO = "https://github.com/acme/api.git"


def _date(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")


def _with_repo(store, run: str, repo: str = REPO) -> Session:
    s = store.get(Session, run.split(".")[0])
    s.repo = repo
    store.put(s)
    return s


async def test_candidates_are_yesterdays_first_runs_of_production_sessions(gw, fleet, store, settings):  # noqa: F811
    runs = [await record(gw, fleet, store, Model()) for _ in range(4)]
    for r in runs:
        _with_repo(store, r)
    today = _date(time.time())
    trial = store.get(Session, runs[1].split(".")[0])  # a trial's session: not production
    trial.labels[experiments.EXP_LABEL] = "job-00000001"
    store.put(trial)
    later = store.get(Session, runs[2].split(".")[0])  # its tape is of a later turn
    later.turns.insert(0, SessionTurn(message="first", run_id="run-0000aaaa"))
    store.put(later)
    other = _with_repo(store, runs[3], "https://github.com/acme/web")
    assert other.repo
    got = determinism.candidates(SimpleNamespace(settings=settings, store=store), today)
    assert got == {"acme/api": [runs[0]], "acme/web": [runs[3]]}
    assert determinism.candidates(SimpleNamespace(settings=settings, store=store), "2001-01-01") == {}
    settings.determinism_runs_per_repo = 0
    assert determinism.candidates(SimpleNamespace(settings=settings, store=store), today) == {"acme/api": [],
                                                                                             "acme/web": []}


async def test_the_report_lists_the_injected_flaky_step_first(gw, fleet, store, settings, tmp_path):  # noqa: F811
    clock = iter(range(10_000, 20_000))

    def flaky(cmd: str) -> str:  # step 7's test reads the clock; step 12's only once
        if cmd == "make step7":
            return f"FAILED tests/test_sync.py::test_export_timestamp now={next(clock)}"
        return deterministic(cmd)

    runs, plans = [], []
    for i in range(3):
        run = await record(gw, fleet, store, Model(), flaky)
        _with_repo(store, run)
        runs.append(run)
        rare = (lambda c: "rare " + c if c == "make step12" else flaky(c)) if i == 0 else flaky
        p, _ = await replay_it(gw, fleet, store, run, rare, mode="replay-strict", tolerate="tool",
                               purpose="determinism")
        p["status"] = "done"
        replay.save_plan(settings, p)
        plans.append(p)
    exps = {}
    entries = []
    for run, p in zip(runs, plans, strict=True):
        exp = Job(title="t", instructions="i", environment="e", state=JobState.SUCCEEDED,
                  labels={experiments.KIND_LABEL: experiments.KIND})
        st = experiments.State(spec={"fork": {"run": run, "at_seq": p["at_seq"], "mode": "replay-strict"}},
                               trials=[experiments.Trial(key="fork/fork/1", task="fork", variant="fork", trial=1,
                                                         replay=p["id"], state="done")])
        exp.result.data = st.model_dump(mode="json", by_alias=True)
        exps[exp.id] = exp
        entries.append({"repo": "acme/api", "run": run, "fork": exp.id})
    entries.append({"repo": "acme/api", "run": "ses-00000000.run-00000000", "error": "no tape"})
    jobs = SimpleNamespace(settings=settings, store=store, _jobs=exps, fleet=fleet)
    reps = determinism.report(jobs, {"date": "2026-10-17", "entries": entries})
    rep = reps["acme/api"]
    assert (rep["replays"], rep["reproduced"], rep["diverged"]) == (3, 0, 0)
    first = rep["steps"][0]
    assert (first["command"], first["kind"], first["count"], first["runs"]) == ("make step7", "tool", 3, 3)
    assert first["evidence"][0]["seq"] == 8 and "test_export_timestamp" in first["evidence"][0]["got"]
    assert [s["command"] for s in rep["steps"]] == ["make step7", "make step12"]
    assert rep["errors"] == [{"run": "ses-00000000.run-00000000", "error": "no tape"}]
    # written locally and to the outbox; the flaky list gets step-level evidence
    settings.outbox_url = str(tmp_path / "bucket")
    fleet.__dict__.pop("_outbox", None)
    store.kv_set_json(testresults.KV_FLAKY + "acme/api", {
        "tests/test_sync.py::test_export_timestamp": {"n": 2, "first": time.time(), "last": time.time()},
        "tests/test_other.py::test_unrelated_thing": {"n": 1, "first": time.time(), "last": time.time()}})
    path = determinism.write(jobs, rep)
    assert path == settings.data_dir / "determinism" / "2026-10-17" / "acme" / "api.json"
    assert json.loads(path.read_text())["steps"][0]["command"] == "make step7"
    flaky_list = testresults.flaky(store, REPO)
    ev = flaky_list["tests/test_sync.py::test_export_timestamp"]["evidence"]
    assert ev[0]["report"] == "v1/determinism/2026-10-17/acme/api.json" and ev[0]["seq"] == 8
    assert "evidence" not in flaky_list["tests/test_other.py::test_unrelated_thing"]
    from hangar import outbox

    await outbox.of(fleet).drain_once()
    assert (tmp_path / "bucket" / "v1/determinism/2026-10-17/acme/api.json").exists()


def test_the_report_waits_for_every_replay(settings, store):
    exp = Job(title="t", instructions="i", environment="e", state=JobState.RUNNING)
    jobs = SimpleNamespace(settings=settings, store=store, _jobs={exp.id: exp})
    assert determinism.report(jobs, {"date": "d", "entries": [{"repo": "r", "run": "x", "fork": exp.id}]}) is None


async def test_the_schedule_starts_yesterdays_replays_once_per_slot(settings, store, monkeypatch):
    started = []
    monkeypatch.setattr(determinism, "start", lambda jobs, caller=None, date=None: started.append(date))
    settings.determinism_schedule = "* * * * *"
    h = SimpleNamespace(jobs=SimpleNamespace(settings=settings, store=store))
    await determinism.minutely(h)
    assert started == []  # the first look only remembers the slot
    store.kv_set_json(determinism.SLOT_KEY, time.time() - 120)
    await determinism.minutely(h)
    assert len(started) == 1 and started[0] in {_date(time.time() - 86400), _date(time.time() - 86400 - 60)}
    settings.determinism_schedule = "off"
    store.kv_set_json(determinism.SLOT_KEY, 0)
    await determinism.minutely(h)
    assert len(started) == 1


def test_report_paths_stay_under_the_date():
    assert determinism._path("acme/api") == "acme/api.json"
    assert determinism._path("https://git.example.com/a/../b") == "https/git.example.com/a/_/b.json"


@pytest.mark.parametrize("bad", ["x", "2026-13-01"])
def test_the_run_endpoint_checks_the_date(settings, bad):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings), headers={"Authorization": "Bearer t0ken"},
                    raise_server_exceptions=False) as c:
        assert c.post("/api/determinism/run", json={"date": bad}).status_code >= 400
        r = c.post("/api/determinism/run", json={})
        assert r.status_code == 200 and r.json()["entries"] == []
