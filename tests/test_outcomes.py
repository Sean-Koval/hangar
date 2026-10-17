"""#166: outcome classes. Unit tests for the classification (outcomes.py) and the job-level class; the experiment's
retries are in test_trial_retries.py."""

from __future__ import annotations

import json

import pytest

from hangar import outcomes
from hangar.models import Attempt, Job, JobState, SessionTurn, Turn


def _lines(*objs: dict) -> str:
    return "".join(json.dumps(o) + "\n" for o in objs)


EDITS = [  # an agent that edits a file and runs a command whose output mentions a 503 and a 429
    {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": "c1"},
    {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [
        {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "app.py", "old_string": "a",
                                                                    "new_string": "b"}},
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "cat server.log"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
         "content": "HTTP 503 Service Unavailable\nHTTP 429 Too Many Requests: rate limit"}]}},
]
RATE_LIMITED = _lines(*EDITS, {"type": "system", "subtype": "api_retry", "attempt": 10, "error_status": 429},
                      {"type": "result", "subtype": "success", "is_error": True,
                       "result": 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":'
                                 '"Number of requests has exceeded your rate limit"}}'})
OVERLOADED = _lines(*EDITS, {"type": "result", "subtype": "success", "is_error": True,
                             "result": 'API Error: 529 {"type":"error","error":{"type":"overloaded_error",'
                                       '"message":"Overloaded"}}'})
AGENT_FAILED = _lines(*EDITS, {"type": "assistant", "message": {"id": "m2", "model": "claude-opus-5-5", "content": [
    {"type": "text", "text": "The tests still fail; I could not fix it."}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "The tests still fail."})
CODEX_429 = _lines({"type": "thread.started", "thread_id": "x"}, {"type": "turn.started"},
                   {"type": "turn.failed", "error": {"message": "exceeded retry limit, last status: 429 Too Many "
                                                                "Requests"}})


@pytest.mark.parametrize(("outcome", "exit_code", "kw", "want"), [
    # node-level loss and the power-down are infrastructure; a sandbox that died with no such record is the agent's
    ("gone", None, {"cause": "pod_replaced"}, ("infra_error", "preempted")),
    ("gone", None, {"cause": "gone"}, ("agent", None)),
    ("gone", None, {}, ("agent", None)),
    ("gone", None, {"cause": "gone", "powered_down": True}, ("infra_error", "power_down")),
    ("gone", None, {"cause": "orphaned_by_restart"}, ("infra_error", "control_plane_restart")),
    ("error", None, {"cause": "pod_replaced"}, ("infra_error", "preempted")),
    # the harness's own report of a provider error, when the run failed
    ("exited", 1, {"raw": RATE_LIMITED}, ("infra_error", "rate_limited")),
    ("exited", 1, {"raw": OVERLOADED}, ("infra_error", "provider_5xx")),
    ("exited", 1, {"raw": CODEX_429, "harness": "codex"}, ("infra_error", "rate_limited")),
    ("timed_out", -1, {"raw": RATE_LIMITED}, ("infra_error", "rate_limited")),
    # an agent that exits 1 after editing files is the agent, whatever its tools printed
    ("exited", 1, {"raw": AGENT_FAILED}, ("agent", None)),
    ("exited", 0, {"raw": AGENT_FAILED}, ("agent", None)),
    ("exited", 0, {"raw": RATE_LIMITED}, ("agent", None)),  # it got through in the end
    ("exited", 137, {}, ("agent", None)),  # OOM-killed: its own process
    ("over_budget", -1, {"raw": RATE_LIMITED}, ("agent", None)),
    # a person or a stop
    ("cancelled", -1, {}, ("cancelled", None)),
    ("interrupted", 130, {}, ("human", None)),
    ("needs_input", 0, {}, ("human", None)),
    # what the caller knows wins
    ("gone", None, {"infra": "control_plane_restart"}, ("infra_error", "control_plane_restart")),
    (None, None, {}, (None, None)),
])
def test_classify_turn(outcome, exit_code, kw, want):
    assert outcomes.classify_turn(outcome, exit_code, **kw) == want


def test_a_provider_error_only_counts_from_the_harness_never_a_tool():
    assert outcomes.provider_error(AGENT_FAILED) is None
    assert outcomes.provider_error(RATE_LIMITED) == "rate_limited"
    assert outcomes.provider_error("") is None and outcomes.provider_error(b"not json at all\n") is None
    big = ("x" * 1000 + "\n") * 400 + RATE_LIMITED  # past the tail the classifier reads: the end still decides
    assert outcomes.provider_error(big) == "rate_limited"


def test_gone_context_reads_the_flight_record_and_the_power_state():
    from types import SimpleNamespace

    from hangar.models import Sandbox

    sb = Sandbox(name="x", environment="e", provider="docker", info={"cause": "pod_replaced"})
    up = SimpleNamespace(power=SimpleNamespace(off=False, affects=lambda p: True))
    down = SimpleNamespace(power=SimpleNamespace(off=True, affects=lambda p: p == "docker"))
    assert outcomes.gone_context(up, sb) == {"cause": "pod_replaced", "powered_down": False}
    assert outcomes.gone_context(down, sb)["powered_down"] is True
    assert outcomes.gone_context(down, None) == {"cause": None, "powered_down": False}


def test_records_from_before_read_as_unknown_never_as_agent():
    assert Turn.model_validate({"outcome": "exited"}).outcome_class is None
    assert SessionTurn.model_validate({"message": "m", "outcome": "gone"}).outcome_class is None
    assert Attempt.model_validate({"index": 1, "outcome": "exited"}).outcome_class is None
    old = Job(title="t", instructions="i", requester="r", environment="e",
              turns=[Turn(run_id="run-1", outcome="exited")])
    old.state = JobState.SUCCEEDED
    assert outcomes.job_class(old) == (None, None)
    from hangar.experiments import Trial

    assert Trial(key="a/b/1", task="a", variant="b", trial=1).outcome_class is None
    with pytest.raises(ValueError):
        Turn.model_validate({"outcome_class": "bad"})


def test_job_class():
    def job(state, *turns, **kw):
        j = Job(title="t", instructions="i", requester="r", environment="e", turns=list(turns), **kw)
        j.state = state
        return j

    ok = Turn(run_id="run-1", outcome="exited", outcome_class="agent")
    cut = Turn(run_id="run-1", outcome="gone", outcome_class="infra_error", infra_reason="preempted")
    assert outcomes.job_class(job(JobState.SUCCEEDED, ok)) == ("agent", None)
    assert outcomes.job_class(job(JobState.FAILED, cut)) == ("infra_error", "preempted")
    # resumed after a restart and finished: the last turn decides the job's class
    restarted = Turn(run_id="run-1", outcome="gone", outcome_class="infra_error", infra_reason="control_plane_restart")
    assert outcomes.job_class(job(JobState.SUCCEEDED, restarted, ok)) == ("agent", None)
    assert outcomes.job_class(job(JobState.CANCELLED, ok)) == ("cancelled", None)
    assert outcomes.job_class(job(JobState.FAILED, ok, reason="interrupted")) == \
        ("infra_error", "control_plane_restart")
    j = job(JobState.FAILED)
    outcomes.mark(j.result, "infra_error", "sandbox_create")
    assert outcomes.job_class(j) == ("infra_error", "sandbox_create")
    assert outcomes.job_class(job(JobState.FAILED, reason="bad_branch")) == ("agent", None)
    asked = Turn(run_id="run-2", outcome="exited", outcome_class="human")
    assert outcomes.job_class(job(JobState.SUCCEEDED, asked)) == ("agent", None)
    assert outcomes.run_fields(cut) == {"outcome_class": "infra_error", "infra_reason": "preempted"}
