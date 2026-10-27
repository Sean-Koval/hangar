"""The SLO signals (KAN-144, src/hangar/slolog.py): the log lines Cloud Monitoring's log-based metrics read, in the
shape deploy/terraform/gke/alerting.tf extracts from, and the same signals as Prometheus metrics."""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace

import pytest

from hangar import governance, metrics, slolog
from hangar.fleet import PoweredDown
from hangar.models import Job, JobEvent, JobState

# The regular expressions alerting.tf's log-based metrics use (kept in step by test_alerting.py).
QUEUED = re.compile(r"queued_s=([0-9.]+)")
SECONDS = re.compile(r"seconds=([0-9.]+)")


@pytest.fixture
def m():
    yield metrics.setup(True)
    metrics.setup(False)


def _job(events: list[tuple[float, str]], state: str = "queued", **kw) -> Job:
    kw.setdefault("created_at", events[0][0] if events else 0)
    j = Job(title="t", instructions="x", environment="base", **kw)
    j.state = JobState(state)
    j.events = [JobEvent(ts=t, state=s) for t, s in events]
    return j


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "hangar.slo"]


def test_a_start_logs_ok_with_its_time_queued(caplog, m):
    caplog.set_level(logging.INFO, "hangar.slo")
    job = _job([(100.0, "queued"), (110.0, "queued"), (130.0, "provisioning")], state="provisioning")
    slolog.job_transition(job, "provisioning", "running", at=142.5)
    (line,) = _lines(caplog)
    assert line == "event=job_start result=ok queued_s=42.5 held=no reason=-"
    assert QUEUED.search(line).group(1) == "42.5"
    assert m.job_starts.labels("ok")._value.get() == 1 and m.job_queue._sum.get() == 42.5


def test_a_follow_up_is_timed_from_when_it_was_queued_again():
    job = _job([(100.0, "queued"), (120.0, "running"), (200.0, "succeeded"), (500.0, "queued")])
    assert slolog._queued_at(job) == (500.0, False)
    assert slolog._queued_at(_job([], created_at=7.0)) == (7.0, False)


def test_a_start_held_on_purpose_is_left_out_of_the_queue_slo(caplog, m):
    caplog.set_level(logging.INFO, "hangar.slo")
    job = _job([(0.0, "queued"), (10.0, "queued")])
    job.events[1].message = "waiting: the sandbox pools are off until 09:50"
    slolog.job_transition(job, "queued", "running", at=36000.0)
    assert _lines(caplog) == ["event=job_start result=ok queued_s=36000.0 held=yes reason=-"]
    assert m.job_starts.labels("ok")._value.get() == 1 and m.job_queue._sum.get() == 0


def test_failed_starts_are_errors_unless_the_requester_caused_them(caplog, m):
    caplog.set_level(logging.INFO, "hangar.slo")
    job = _job([(0.0, "queued"), (5.0, "provisioning")], state="failed", reason="error")
    slolog.job_transition(job, "provisioning", "failed", at=10.0)
    job.reason = "bad_branch"
    slolog.job_transition(job, "provisioning", "failed", at=10.0)
    assert _lines(caplog) == ["event=job_start result=error queued_s=10.0 held=no reason=error",
                              "event=job_start result=rejected queued_s=10.0 held=no reason=bad_branch"]
    assert m.job_starts.labels("error")._value.get() == 1 and m.job_starts.labels("rejected")._value.get() == 1


def test_only_starts_count(caplog):
    caplog.set_level(logging.INFO, "hangar.slo")
    job = _job([(0.0, "queued")])
    for before, after in (("queued", "cancelled"), ("running", "failed"), ("running", "verifying"),
                          ("queued", "provisioning"), ("needs_input", "queued")):
        slolog.job_transition(job, before, after)
    assert _lines(caplog) == []


def test_sandbox_creates_log_ok_and_errors_but_not_power_downs(caplog):
    caplog.set_level(logging.INFO, "hangar.slo")
    slolog.sandbox_create("kubernetes", "base", 3.21, None)
    slolog.sandbox_create("kubernetes", "my env", 12.5, RuntimeError("no node"))
    slolog.sandbox_create("kubernetes", "base", 1.0, PoweredDown("off"))
    slolog.sandbox_create("kubernetes", "base", 1.0, KeyboardInterrupt())
    ok, err = _lines(caplog)
    assert ok == "event=sandbox_create result=ok provider=kubernetes environment=base seconds=3.21 error=-"
    assert err == ("event=sandbox_create result=error provider=kubernetes environment=my_env seconds=12.5 "
                   "error=RuntimeError")
    assert SECONDS.search(err).group(1) == "12.5"


def test_failing_closed_is_counted_and_never_allows(caplog, m):
    caplog.set_level(logging.INFO, "hangar.slo")
    d = governance.fail_closed("hangar.tool.call", "unreachable", "ConnectError")
    assert d.outcome != "allow" and d.status == "unavailable"
    assert _lines(caplog) == ["event=fail_closed kind=hangar.tool.call failure=unreachable"]
    assert m.fail_closed.labels("hangar.tool.call", "unreachable")._value.get() == 1


def test_stuck_jobs_leave_out_holds_and_the_power_schedule():
    t = 100_000.0
    jobs = SimpleNamespace(_jobs={j.id: j for j in [
        _job([(t - 4000, "queued")]),  # runnable for over an hour: stuck
        _job([(t - 4000, "queued")], reason="powered_down"),  # held on purpose
        _job([(t - 600, "queued")]),  # ten minutes: fine
        _job([(t - 100, "queued"), (t - 1500, "provisioning")][::-1], state="provisioning"),
        _job([(t - 9000, "running")], state="running", timeout_minutes=60),  # past 60 min + 30 min grace
        _job([(t - 4000, "running")], state="running", timeout_minutes=60),
        _job([(t - 9000, "succeeded")], state="succeeded"),
    ]})
    found = sorted(s for _, s in slolog.stuck_jobs(jobs, power_off=False, t=t))
    assert found == ["provisioning", "queued", "running"]
    assert [s for _, s in slolog.stuck_jobs(jobs, power_off=True, t=t)] == ["running"]


async def test_the_stuck_tick_sets_the_gauge_and_logs(caplog, m):
    caplog.set_level(logging.INFO, "hangar.slo")
    stuck = _job([(0.0, "queued")])
    h = SimpleNamespace(jobs=SimpleNamespace(_jobs={stuck.id: stuck}),
                        fleet=SimpleNamespace(power=SimpleNamespace(off=False)))
    await slolog.stuck_tick(h)
    assert m.jobs_stuck._value.get() == 1
    assert re.fullmatch(r"event=jobs_stuck n=1 oldest_s=\d+ states=queued", _lines(caplog)[0])
    h.jobs._jobs.clear()
    await slolog.stuck_tick(h)
    assert m.jobs_stuck._value.get() == 0 and len(_lines(caplog)) == 1


def test_the_stuck_check_rides_the_ops_loop_once():
    from hangar import opstatus

    slolog.register()
    slolog.register()
    assert opstatus.MINUTELY.count(slolog.stuck_tick) == 1


def test_values_are_one_token():
    assert slolog.emit("x", a="two words", b=None, c="") == "event=x a=two_words b=- c=-"


async def test_a_create_is_logged_with_tracing_and_metrics_off(fleet, caplog):
    from hangar import observe

    await observe.stop()
    metrics.setup(False)
    caplog.set_level(logging.INFO, "hangar.slo")
    sb = await fleet.create("plain")
    await fleet.destroy(sb.id)
    (line,) = [ln for ln in _lines(caplog) if ln.startswith("event=sandbox_create")]
    assert line.startswith(f"event=sandbox_create result=ok provider={sb.provider} environment=plain seconds=")
