"""The overnight lane (#186): with power state `night`, intake jobs of `overnight: true` repos (or labelled
`lane: overnight`) and eval-class jobs run on the one node kept up, at most HANGAR_OVERNIGHT_LANE_SANDBOXES at once;
everything else waits for the morning; a preempted lane job waits for the morning and resumes from its checkpoint;
`hangar power overnight on` needs 60% agent PR yield over 14 days, or --force (audited)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from hangar import power, weekly
from hangar.fleet import PoweredDown
from hangar.jobs import Caller, SubmitJob
from hangar.models import Job, JobState
from hangar.power import Power, PowerError
from hangar.repopolicy import RepoPolicy, repo_key
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures
from tests.test_reconciler import needs_git


def night(p: Power) -> None:
    p.mark("night", "power.sh", up_cron="50 9 * * *", time_zone="America/New_York", warm_minutes=10)


def _queued(svc, **kw) -> Job:
    job = Job(title="t", instructions="x", environment="jobenv", provider="docker", state=JobState.QUEUED, **kw)
    svc._jobs[job.id] = job
    return job


# ====================================================================== admission
async def test_night_admits_overnight_repos_lane_jobs_and_evals_up_to_the_lanes_size(jobs, fleet, settings):  # noqa: F811
    settings.power_providers = ["docker"]
    settings.overnight_lane_sandboxes = 3
    p = fleet.power
    night(p)
    assert p.off and p.night and "except the overnight lane" in p.message()
    jobs.policies.put(RepoPolicy(repo=repo_key("https://github.com/acme/api"), overnight=True))
    ev = _queued(jobs, queue_class="eval")
    tagged = _queued(jobs, source="jira", labels={"lane": "overnight"})
    repo = _queued(jobs, source="github", repo="https://github.com/acme/api")
    other_repo = _queued(jobs, source="github", repo="https://github.com/acme/web")
    not_intake = _queued(jobs, source="api", repo="https://github.com/acme/api", labels={"lane": "overnight"})
    assert [p.hold(jobs, j.id) for j in (ev, tagged, repo)] == [False, False, False]  # they run now
    assert sorted(p.lane) == sorted([ev.id, tagged.id, repo.id])
    assert any("overnight lane" in e.message for e in jobs.get(repo.id).events)
    assert p.hold(jobs, other_repo.id) and p.hold(jobs, not_intake.id)  # the morning
    full = _queued(jobs, queue_class="eval")
    assert p.hold(jobs, full.id)  # the lane is full: it waits too
    # a lane job's create goes through; any other create is still refused with when the pools are back
    p.check("docker", {"hangar.job": ev.id})
    with pytest.raises(PoweredDown, match="except the overnight lane"):
        p.check("docker", {"hangar.job": other_repo.id})
    with pytest.raises(PoweredDown):
        p.check("docker", {"hangar.session": "s1"})
    ev.state = JobState.SUCCEEDED  # a seat frees up
    assert not p.hold(jobs, full.id) and full.id in p.lane
    assert p.overview()["power"]["overnight"]["running"] == 3
    # `down` (the lane off) and `up` end the lane's night
    p.mark("down", "power.sh")
    assert not p.lane and p.hold(jobs, tagged.id)
    p.mark("up", "power.sh")
    assert not p.hold(jobs, other_repo.id)


@needs_git
async def test_a_ticket_for_an_overnight_repo_becomes_a_pr_at_night_and_another_waits(
        jobs, world, fleet, settings):  # noqa: F811
    settings.power_providers = ["docker"]
    settings.job_restart_stagger_seconds = 0
    night(fleet.power)
    waits = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("jira", "jira"),
                        source="jira")  # 23:30, a repo that isn't overnight
    for _ in range(100):
        if jobs.get(waits.id).reason == power.WAITING:
            break
        await asyncio.sleep(0.05)
    assert jobs.get(waits.id).state == JobState.QUEUED and waits.id not in fleet.power.lane
    jobs.policies.put(RepoPolicy(repo=repo_key(world["url"]), overnight=True))
    runs = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("jira", "jira"),
                       source="jira")
    done = await _until(jobs, runs.id, DONE)
    assert done.state == JobState.SUCCEEDED and done.result.pushed, [e.message for e in done.events]
    assert fleet.power.state()["state"] == "night"


@needs_git
async def test_a_preempted_lane_job_waits_for_the_morning_and_resumes(jobs, world, fleet, settings):  # noqa: F811
    settings.power_providers = ["docker"]
    settings.job_restart_stagger_seconds = 0
    jobs.policies.put(RepoPolicy(repo=repo_key(world["url"]), overnight=True))
    night(fleet.power)
    world["mode"].write_text("sleep")
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("jira", "jira"),
                      source="jira")
    await _until(jobs, job.id, {JobState.RUNNING})
    while not jobs.get(job.id).turns[0].run_id:
        await asyncio.sleep(0.05)
    s = jobs.sessions.get(jobs.get(job.id).session_id)
    assert await jobs.sessions.checkpoint_now(s.id, requester="test") is not None
    sb = fleet.get(jobs.get(job.id).sandbox_id)  # the Spot node goes, and its pod with it
    await fleet.providers["docker"].destroy(sb)
    fleet._mark_gone(sb, "disappeared from provider")
    for _ in range(200):
        if jobs.get(job.id).reason == power.WAITING:
            break
        await asyncio.sleep(0.05)
    j = jobs.get(job.id)
    assert j.state == JobState.QUEUED and j.restarts == 1, [e.message for e in j.events]
    assert (j.turns[0].outcome_class, j.turns[0].infra_reason) == ("infra_error", "preempted")
    assert job.id in fleet.power.lane_cut and job.id not in fleet.power.lane
    assert fleet.power.hold(jobs, job.id)  # not again tonight
    world["mode"].write_text("ok")
    fleet.power.mark("up", "power.sh")  # 10:00
    fleet.power.release(jobs)
    done = await _until(jobs, job.id, DONE)
    assert done.state == JobState.SUCCEEDED and done.result.pushed, [e.message for e in done.events]


# ====================================================================== the yield gate
async def test_turning_the_lane_on_needs_sixty_percent_yield_or_force(jobs, fleet, monkeypatch):  # noqa: F811
    p = fleet.power
    rate = {"v": 0.5}
    monkeypatch.setattr(weekly, "compute", lambda svc, lo, hi, repo=None: {"yield": {"value": rate["v"]}})
    with pytest.raises(PowerError, match=r"60% or more, and it is 50%.*--force"):
        p.set_overnight(jobs, True, "admin")
    assert not p.overnight()["on"]
    rec = p.set_overnight(jobs, True, "admin", force=True)
    assert rec["on"] and rec["forced"] and rec["yield"] == 0.5
    audit = [a for a in jobs.store.audit_tail(20) if a.action == "power.overnight.on"]
    assert audit and audit[0].detail["forced"] is True
    rate["v"] = None
    with pytest.raises(PowerError, match="no agent PR reached a final state"):
        p.set_overnight(jobs, True, "admin")
    rate["v"] = 0.72
    assert p.set_overnight(jobs, True, "admin") == {**p.overnight(), "forced": False}
    assert not p.set_overnight(jobs, False, "admin")["on"]


def test_rest_and_cli(settings, monkeypatch):
    from typer.testing import CliRunner

    import hangar.cli as cli
    from hangar.app import create_app
    from tests.test_connect import _Keep

    settings.admin_token = "t0ken"
    monkeypatch.setattr(weekly, "compute", lambda svc, lo, hi, repo=None: {"yield": {"value": 0.4}})
    with TestClient(create_app(settings), headers={"Authorization": "Bearer t0ken"}) as c:
        r = c.post("/api/admin/power/overnight", json={"on": True})
        assert r.status_code == 409 and "40%" in r.json()["detail"]
        assert c.post("/api/admin/power/overnight", json={"on": "yes"}).status_code == 400
        assert c.post("/api/admin/power/overnight", json={"on": True, "force": True}).json()["forced"] is True
        member = c.post("/api/people", json={"id": "mo", "role": "member"}).json()["login"]["token"]
        assert c.post("/api/admin/power/overnight", json={"on": False},
                      headers={"Authorization": f"Bearer {member}"}).status_code == 403
        assert c.get("/api/admin/power").json()["power"]["overnight"]["on"] is True
        assert c.post("/api/admin/power/state", json={"state": "night"}).json()["state"]["state"] == "night"
        monkeypatch.setattr(cli, "_client", lambda: _Keep(c))
        r = CliRunner().invoke(cli.app, ["power"])
        assert "powered down except the overnight lane" in r.output and "overnight lane: on" in r.output, r.output
        r = CliRunner().invoke(cli.app, ["power", "overnight", "off"])
        assert r.exit_code == 0 and "overnight lane off" in r.output
        r = CliRunner().invoke(cli.app, ["power", "overnight", "on"])
        assert r.exit_code != 0 and "40%" in r.output


# ====================================================================== placement
def test_gvisor_sandboxes_and_the_prepull_tolerate_the_lanes_node(settings):
    pytest.importorskip("kubernetes")
    from hangar.envspec import EnvSpec
    from hangar.providers.kubernetes import KubernetesProvider

    prov = KubernetesProvider.__new__(KubernetesProvider)
    prov.settings = settings.model_copy(update={"k8s_runtime_class": "gvisor"})
    pod = prov._pod_template(EnvSpec(name="e"), "img")["spec"]
    assert power.LANE_TOLERATION in pod["tolerations"]
    assert pod["nodeSelector"] == {"sandbox.gke.io/runtime": "gvisor"}  # no lane selector: by day, any gVisor node
    ds = power.prepull_manifest(prov.settings, ["r/x@sha256:1"])
    assert power.LANE_TOLERATION in ds["spec"]["template"]["spec"]["tolerations"]
