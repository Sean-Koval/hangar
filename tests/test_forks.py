"""#179: fork a recorded run at a step. `POST /api/runs/{session}.{run}/fork` is an experiment whose k trials each
replay the run's tape to `at_seq` in a replay sandbox (egress none, no credentials), with the run's own prompt, then go
live with `fork_of`; their trees are checked against the run's afterwards, and a clean prefix on the run's own harness
records whether it reproduced the run's outcome (A/A).

The end-to-end test runs on the fake provider with the scripted agent of tests/test_harness_lock.py (real git and
tmux); the scripted agent has no model gateway path, so its trials are recorded `unreplayed`. The replay itself, step
by step, is tests/test_replay.py's."""

from __future__ import annotations

import json
import shutil
import uuid
from types import SimpleNamespace

import httpx
import pytest

from hangar import experiments, forks, modelgw, pinsets, posture, replay, traces
from hangar.events import RunEvent
from hangar.jobs import Caller
from hangar.models import CredentialKind, Job, JobState, Sandbox, SandboxState, Session, SessionTurn
from tests import test_harness_thaw as th
from tests.test_harness_lock import DONE
from tests.test_jobs import _until
from tests.test_replay import REAL_KEY, Harness, Model, deterministic

skillenv, world, jobs = th.skillenv, th.world, th.jobs
needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


async def _tape_for(jobs, s: Session, turn: SessionTurn) -> str:
    """A tape for the session's run, as the model gateway records it: a stand-in sandbox of the session, a scripted
    harness and model."""
    settings, fleet = jobs.settings, jobs.fleet
    settings.gateway_url = "http://gw.test:8788"
    settings.model_gateway_upstreams = {"anthropic": "http://upstream.test"}
    fleet.broker.put("anthropic-key", "claude-code", CredentialKind.API_KEY, REAL_KEY)
    gw = modelgw.of(fleet)
    fleet.gateway.transport = httpx.MockTransport(Model())
    fleet.gateway._client = None
    sb = Sandbox(id=f"sb-{uuid.uuid4().hex[:8]}", name="stand-in", environment="lockenv", provider="fake",
                 state=SandboxState.RUNNING, labels={"hangar.session": s.id}, created_by="svc")
    fleet.store.put(sb)
    await Harness(deterministic).run(fleet, gw.mint(sb, "anthropic", "anthropic-key"))
    await gw.settle()
    await gw.turn_ended(s, turn)
    gw.revoke(sb.id)
    fleet.gateway.transport, fleet.gateway._client = None, None
    settings.gateway_url = None  # the trials clone the file:// repo directly, as the other end-to-end tests do
    return f"{s.id}.{turn.run_id}"


@needs_tools
async def test_a_fork_starts_k_replay_trials_of_the_runs_prompt_at_its_base(jobs, world, monkeypatch):
    job = await th._run(jobs, world)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    s = jobs._session(job)
    turn = next(t for t in s.turns if t.run_id)
    run = await _tape_for(jobs, s, turn)
    monkeypatch.setattr(forks, "check", lambda *a: None)  # the gateway is off for the file:// repo (see _tape_for)
    from hangar import egress

    async def proxy_url(fleet_, prov, sb):  # egress none; the fake provider has no proxy-only network
        return "http://127.0.0.1:1"

    monkeypatch.setattr(egress, "_proxy_url", proxy_url)
    exp = forks.start(jobs, run, forks.ForkRequest(at_seq=20, continuations=2), Caller("svc", "svc"))
    v = forks.view(jobs, exp.id)
    assert v["status"] == "replaying" and v["trial_ids"] == [f"{exp.id}.1", f"{exp.id}.2"] and v["divergence"] is None
    exp = await _until(jobs, exp.id, DONE, timeout=180)
    st = experiments.load(exp)
    assert st.spec["fork"]["run"] == run and st.spec["fork"]["at_seq"] == 20 and st.spec["fork"]["mode"] == \
        "replay-prefix"
    assert len(st.trials) == 2, [e.message for e in exp.events]
    plans = []
    for t in st.trials:
        child = jobs.get(t.job)
        assert child.source == "experiment" and t.fork_of == run and t.tainted is False
        assert child.labels[replay.REPLAY_LABEL] == t.replay and child.labels[replay.FORK_OF_LABEL] == run
        assert child.labels[posture.FLOOR_LABEL] == "none" and child.deliver == "none"
        assert jobs._session(child).base_sha == s.base_sha  # the run's base commit
        p = replay.load_plan(jobs.settings, t.replay)
        assert (p["run"], p["mode"], p["at_seq"], p["parent"], p["trial"]) == (run, "replay-prefix", 20, exp.id,
                                                                                 t.ref)
        assert p["prompt"] == "Fix the flaky build"
        assert jobs._prompt(child, child.turns[0], "/repo") == "Fix the flaky build"  # the run's own prompt
        assert p["status"] == "unreplayed"  # the scripted agent never calls a model through the gateway
        plans.append(p["id"])
    assert len(set(plans)) == 2
    v = forks.view(jobs, exp.id)
    assert v["status"] == "unreplayed" and [x["fork_of"] for x in v["trials"]] == [run, run]
    tv = pinsets.trial_view(jobs, f"{exp.id}.1")
    assert tv["fork_of"] == run and tv["replay"] == plans[0]


@needs_tools
async def test_a_fork_refuses_what_it_cant_rebuild(jobs, world, monkeypatch):
    job = await th._run(jobs, world)
    s = jobs._session(job)
    turn = next(t for t in s.turns if t.run_id)
    with pytest.raises(replay.ReplayError, match="HANGAR_GATEWAY_URL"):
        forks.check(jobs, "x", s, turn)
    jobs.settings.gateway_url = "http://gw.test:8788"
    with pytest.raises(replay.ReplayError, match="doesn't serve"):
        forks.check(jobs, "x", s, turn)
    monkeypatch.setattr(posture, "gateway_serves", lambda settings, repo: True)
    forks.check(jobs, "x", s, turn)
    later = SessionTurn(message="more", run_id="run-0000ffff")
    s2 = s.model_copy(update={"turns": [*s.turns, later]})
    with pytest.raises(replay.ReplayError, match="only a session's first run"):
        forks.check(jobs, "x", s2, later)
    with pytest.raises(replay.ReplayError, match="base commit"):
        forks.check(jobs, "x", s.model_copy(update={"base_sha": None}), turn)
    with pytest.raises(replay.ReplayError, match="has no tape"):
        forks.start(jobs, f"{s.id}.{turn.run_id}", forks.ForkRequest(at_seq=1), Caller("svc", "svc"))
    with pytest.raises(KeyError):
        forks.start(jobs, "ses-00000000.run-00000000", forks.ForkRequest(at_seq=1), Caller("svc", "svc"))
    with pytest.raises(KeyError):  # someone else's session
        forks.session_of(jobs, f"{s.id}.{turn.run_id}", owner="not-the-owner")


def test_the_fork_endpoints(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings), headers={"Authorization": "Bearer t0ken"}) as c:
        assert c.post("/api/runs/ses-00000000.run-00000000/fork", json={"at_seq": 3}).status_code == 404
        assert c.post("/api/runs/ses-00000000.run-00000000/fork", json={"at_seq": -1}).status_code == 422
        assert c.post("/api/runs/x/fork", json={"at_seq": 1, "continuations": 11}).status_code == 422
        assert c.get("/api/forks/job-00000000").status_code == 404


# ====================================================================== a trial's end: trees and A/A
def _tree(tree, id=None, phase=None):
    return RunEvent(kind="progress", text="tree", data={"action": "tree", "tree": tree, "id": id, "phase": phase})


def _finish(settings, monkeypatch, replay_trees, *, original_passed=True, fork_passed=True, pin_set=None,
            harness_digest="sha256:h"):
    """finish_trial for one fork trial whose replay served steps 1-5 of a 6-step run cleanly."""
    run = "ses-0000000a.run-0000000b"
    entries = [{"seq": n, "resp": {"sse": [{"event": "content_block_start", "data": {
        "content_block": {"type": "tool_use", "id": f"<toolu:{n}>"}}}]}} for n in range(1, 7)]
    index = {"tool_ids": {f"toolu_o{n}": f"<toolu:{n}>" for n in range(1, 7)}}
    p = {"schema": replay.SCHEMA, "id": "rpl-" + uuid.uuid4().hex[:12], "run": run, "mode": "replay-prefix",
         "at_seq": 4, "status": "live", "sandbox_id": "sb-1", "served": [1, 2, 3, 4], "verified_through": 5,
         "divergence": None, "mismatches": [], "ids": {f"toolu_r{n}": f"<toolu:{n}>" for n in range(1, 7)}}
    replay.save_plan(settings, p)
    orig_job = Job(title="t", instructions="i", environment="e", verify="make test")
    orig_job.result.verify_exit_code = 0 if original_passed else 1
    child = Job(title="t", instructions="i", environment="e", verify="make test", state=JobState.SUCCEEDED)
    child.result.verify_exit_code = 0 if fork_passed else 1
    s_orig = Session(id="ses-0000000a", name="o", environment="e", job_id=orig_job.id,
                     turns=[SessionTurn(message="m", run_id="run-0000000b")])
    s_rep = Session(name="r", environment="e", turns=[SessionTurn(message="m", run_id="run-0000000c")])
    original = [_tree("a" * 40, phase="start"), _tree("1" * 40, "toolu_o1"), _tree("2" * 40, "toolu_o2"),
                _tree("5" * 40, "toolu_o5")]
    monkeypatch.setattr(replay, "read_tape", lambda settings_, r: (index, entries))
    monkeypatch.setattr(traces, "read_events", lambda act, sid, run_id=None, limit=0:
                        original if sid == s_orig.id else replay_trees)
    jobs = SimpleNamespace(settings=settings, store=SimpleNamespace(get=lambda cls, k: s_orig if k == s_orig.id
                                                                     else None),
                           _session=lambda j: s_rep, sessions=SimpleNamespace(activity=None),
                           _jobs={orig_job.id: orig_job})
    fork = {"run": run, "at_seq": 4, "continuations": 1, "harness_digest": "sha256:h", "pin_set": pin_set}
    st = SimpleNamespace(spec={"fork": fork})
    t = SimpleNamespace(replay=p["id"], harness_digest=harness_digest, aa=None)
    forks.finish_trial(jobs, None, st, t, child)
    return replay.load_plan(settings, p["id"]), t


def test_a_clean_prefix_on_the_runs_harness_records_whether_it_reproduced(settings, monkeypatch):
    same = [_tree("a" * 40, phase="start"), _tree("1" * 40, "toolu_r1"), _tree("2" * 40, "toolu_r2"),
            _tree("7" * 40, "toolu_r5")]  # the tree after step 5's tool is past the prefix (its result is step 6's)
    p, t = _finish(settings, monkeypatch, same)
    assert p["divergence"] is None and p["trees"] == "verified through step 5"
    assert t.aa["production_harness"] and t.aa["reproduced"] is True
    _, t = _finish(settings, monkeypatch, same, fork_passed=False)
    assert t.aa["reproduced"] is False
    _, t = _finish(settings, monkeypatch, same, pin_set="pin-" + "1" * 16, harness_digest="sha256:other")
    assert t.aa is None  # another harness: not an A/A trial


def test_a_prefix_tree_that_differs_is_a_tree_divergence(settings, monkeypatch):
    other = [_tree("a" * 40, phase="start"), _tree("1" * 40, "toolu_r1"), _tree("9" * 40, "toolu_r2")]
    p, t = _finish(settings, monkeypatch, other)
    d = p["divergence"]
    assert (d["seq"], d["kind"], d["expected"], d["got"]) == (3, "tree", "2" * 40, "9" * 40)
    assert p["trees"] == "diverged" and t.aa is None


def test_the_view_reports_the_earliest_divergence(settings, monkeypatch):
    exp = Job(title="t", instructions="i", environment="e", labels={experiments.KIND_LABEL: experiments.KIND})
    trials = []
    for n, div in ((1, {"seq": 9, "kind": "tool"}), (2, {"seq": 4, "kind": "network"})):
        p = {"id": "rpl-" + uuid.uuid4().hex[:12], "run": "ses-0000000a.run-0000000b", "status": "diverged",
             "divergence": div, "served": list(range(1, div["seq"])), "at_seq": 20}
        replay.save_plan(settings, p)
        trials.append(experiments.Trial(key=f"fork/fork/{n}", task="fork", variant="fork", trial=n, replay=p["id"],
                                        fork_of=p["run"], state="done"))
    st = experiments.State(spec={"fork": {"run": "ses-0000000a.run-0000000b", "at_seq": 20, "mode": "replay-prefix",
                                          "continuations": 2}}, trials=trials)
    exp.result.data = st.model_dump(mode="json", by_alias=True)
    monkeypatch.setattr(experiments, "experiment", lambda jobs_, i, owner=None: exp)
    v = forks.view(SimpleNamespace(settings=settings), exp.id)
    assert v["status"] == "diverged" and v["divergence"] == {"seq": 4, "kind": "network"}
    assert v["trial_ids"] == [f"{exp.id}.1", f"{exp.id}.2"]
    json.dumps(v)
