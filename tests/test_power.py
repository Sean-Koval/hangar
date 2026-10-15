"""The control plane stays up while the sandbox pools are powered down (power.py, owner's decision 1): creates answer
at once with when sandboxes are back, jobs from every front door wait queued and start at power-up, a turn the
power-down cuts off resumes from its checkpoint, warm pools are sized for the morning, and the operator sees the
power state, the last deploy and the loops' heartbeats in doctor and the overview."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from hangar import metrics, power, powerhold
from hangar.envspec import warm_pool_now
from hangar.fleet import PoweredDown
from hangar.jobs import Caller, SubmitJob
from hangar.models import JobState, Sandbox, SandboxState
from hangar.power import Power, PowerError
from hangar.providers.base import Capabilities
from tests.conftest import FakeProvider
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures
from tests.test_reconciler import needs_git

NY = ZoneInfo("America/New_York")


def at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=NY).timestamp()


def off(p: Power, clock: float | None = None) -> None:
    if clock is not None:
        p.clock = lambda: clock
    p.mark("down", "power.sh", up_cron="50 9 * * *", time_zone="America/New_York", warm_minutes=10)


# ====================================================================== state and the message people see
def test_down_says_when_the_sandboxes_are_back(settings, store):
    p = Power(store, settings, clock=lambda: at("2026-10-14T01:00:00"))
    off(p)
    st = p.state()
    assert st["state"] == "down" and st["ready_at"] == at("2026-10-14T10:00:00")  # 09:50 job + 10 min warm-up
    assert p.when() == "10:00 EDT (in 9 h 0 min)"
    msg = p.message()
    assert "powered down for the night and power on at 10:00 EDT" in msg and "hangar power up" in msg
    assert p.status_line() == "sandboxes power on at 10:00 EDT (in 9 h 0 min)"
    p.mark("starting", "power.sh")
    assert p.off and "powering on now" in p.message()
    p.mark("up", "power.sh", prepull={"images": 7, "seconds": 42.4, "nodes": 1, "ready": True, "junk": "x"})
    st = p.state()
    assert not p.off and st["last_up"]["prepull"] == {"images": 7, "seconds": 42.4, "nodes": 1, "ready": True}
    with pytest.raises(ValueError):
        p.mark("sideways", "x")
    with pytest.raises(ValueError):
        p.mark("down", "x", time_zone="Mars/Olympus")


def test_a_down_state_an_hour_past_its_ready_time_counts_as_up(settings, store):
    """A power-up job that failed to report must not leave hangar refusing work all day."""
    t = at("2026-10-14T01:00:00")
    p = Power(store, settings, clock=lambda: t)
    off(p)
    settings.power_providers = ["docker"]
    assert p.off and p.refusal("docker")
    p.clock = lambda: at("2026-10-14T11:01:00")
    assert not p.off and p.state()["stale"] and p.refusal("docker") is None


async def test_a_held_job_lets_itself_go_when_power_up_is_never_reported(settings, store):
    """Held jobs look again every RECHECK seconds: a stale state, or a cancel, doesn't leave them waiting."""
    from types import SimpleNamespace as NS

    settings.power_providers = ["docker"]
    t = [at("2026-10-14T01:00:00")]
    p = Power(store, settings, clock=lambda: t[0])
    off(p)
    events = []
    job = NS(state=JobState.QUEUED, provider=None, cancel_requested=False, reason=None)
    svc = NS(_jobs={"j1": job}, _queue=asyncio.Queue(), _event=lambda j, m, *a, **kw: events.append(m))
    settings.default_provider = "docker"
    assert p.hold(svc, "j1") and p.held == ["j1"] and events[0].startswith("waiting: sandboxes power on at 10:00")
    p._recheck(svc, "j1")  # still off: keeps waiting
    assert p.held == ["j1"] and svc._queue.empty()
    t[0] = at("2026-10-14T11:01:00")  # an hour past 10:00 and nothing reported
    p._recheck(svc, "j1")
    assert not p.held and svc._queue.get_nowait() == "j1"
    t[0] = at("2026-10-14T01:00:00")
    job2 = NS(state=JobState.QUEUED, provider=None, cancel_requested=False, reason=None)
    svc._jobs["j2"] = job2
    assert p.hold(svc, "j2")
    job2.state = JobState.CANCELLED  # cancelled while it waited
    p._recheck(svc, "j2")
    assert not p.held and svc._queue.empty()


def test_only_the_powered_providers_are_refused(settings, store):
    p = Power(store, settings)
    off(p)
    assert settings.power_providers == ["kubernetes"]
    assert p.refusal("kubernetes") and p.refusal("docker") is None
    settings.default_provider = "kubernetes"
    assert p.refusal(None)


# ====================================================================== creates, sessions and the REST answer
async def test_creates_answer_at_once_while_the_pools_are_off(fleet, settings, store):
    settings.power_providers = ["docker"]
    off(fleet.power)
    t0 = time.monotonic()
    with pytest.raises(PoweredDown, match="power on at"):
        await fleet.create("plain")
    assert time.monotonic() - t0 < 1 and not store.list(Sandbox)  # nothing was started, nothing to clean up
    fleet.power.mark("up", "power.sh")
    sb = await fleet.create("plain")
    assert sb.state == SandboxState.RUNNING


async def test_mcp_create_answers_at_once_with_when(fleet, settings):
    """What claude.ai sees at night: a plain tool error with the time, well inside the 150 s contract."""
    from mcp import Client

    from hangar import mcp_server
    from hangar.jobs import JobService

    settings.power_providers = ["docker"]
    off(fleet.power)
    svc = JobService(fleet, None)  # type: ignore[arg-type] - create_sandbox needs no sessions
    async with Client(mcp_server.build_mcp(fleet, svc, None)) as c:
        t0 = time.monotonic()
        res = await asyncio.wait_for(c.call_tool("create_sandbox", {"environment": "plain"}), 10)
        assert time.monotonic() - t0 < 5
    assert res.is_error and "Sandboxes are powered down for the night and power on at" in res.content[0].text


async def test_sessions_refuse_up_front_instead_of_failing(fleet, settings, sessions):  # noqa: F811
    from hangar.sessions import NewSession

    settings.power_providers = ["docker"]
    off(fleet.power)
    with pytest.raises(PoweredDown):
        sessions.new(NewSession(name="night", environment="agent"), owner="o", requester="o")
    assert not sessions.list()  # no session left behind in `failed`


def _app(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.power_providers = ["docker"]
    return create_app(settings)


def test_rest_create_is_a_503_with_retry_after_and_a_job_is_accepted(settings):
    app = _app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer t0ken"
        h = app.state.hangar
        h.fleet.providers["docker"] = FakeProvider()
        r = client.post("/api/admin/power/state", json={"state": "down", "up_cron": "50 9 * * *",
                                                        "time_zone": "America/New_York", "warm_minutes": 10})
        assert r.status_code == 200 and r.json()["state"]["state"] == "down", r.text
        r = client.post("/api/sandboxes", json={"environment": "plain"})
        assert r.status_code == 503 and "power on at" in r.json()["error"], r.text
        assert int(r.headers["Retry-After"]) >= 60 and r.json()["power"]["state"] == "down"
        r = client.post("/api/jobs", json={"instructions": "x", "environment": "agent", "deliver": "none"})
        assert r.status_code == 200 and r.json()["state"] == "queued", r.text
        assert client.get("/api/overview").json()["power"]["state"] == "down"
        bad = client.post("/api/admin/power/state", json={"state": "down", "time_zone": "Nope/Nope"})
        assert bad.status_code == 400
        assert client.get("/api/admin/power", headers={"Authorization": "Bearer nope"}).status_code == 401


# ====================================================================== jobs
@needs_git
async def test_jobs_wait_with_the_eta_and_start_at_power_up(jobs, world, fleet, settings):  # noqa: F811
    settings.power_providers = ["docker"]
    settings.job_restart_stagger_seconds = 0
    off(fleet.power)
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("gh", "gh"),
                      source="github")
    for _ in range(100):
        if jobs.get(job.id).reason == power.WAITING:
            break
        await asyncio.sleep(0.05)
    j = jobs.get(job.id)
    assert j.state == JobState.QUEUED and j.reason == power.WAITING and job.id in fleet.power.held
    assert any(e.message.startswith("waiting: sandboxes power on at") for e in j.events), [e.message for e in j.events]
    assert not fleet.store.list(Sandbox)
    fleet.power.mark("up", "power.sh")
    assert fleet.power.release(jobs) == [job.id]
    done = await _until(jobs, job.id, DONE)
    assert done.state == JobState.SUCCEEDED and done.reason is None, [e.message for e in done.events]


@needs_git
async def test_a_create_refused_between_the_check_and_the_create_waits_instead_of_failing(
        jobs, world, fleet, settings):  # noqa: F811
    """The pools go off just after a worker took the job: its create is refused, and it waits like the rest."""
    settings.power_providers = ["docker"]
    settings.job_restart_stagger_seconds = 0
    off(fleet.power)
    fleet.power.hold = lambda svc, jid: False  # the worker looked before the power-down
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("svc", "svc"))
    for _ in range(100):
        if jobs.get(job.id).reason == power.WAITING:
            break
        await asyncio.sleep(0.05)
    j = jobs.get(job.id)
    assert j.state == JobState.QUEUED and j.session_id is None, [e.message for e in j.events]  # on main: failed
    del fleet.power.hold
    fleet.power.mark("up", "power.sh")
    fleet.power.release(jobs)
    done = await _until(jobs, job.id, DONE)
    assert done.state == JobState.SUCCEEDED, [e.message for e in done.events]


@needs_git
async def test_a_turn_the_power_down_cuts_off_resumes_at_power_up(jobs, world, fleet, settings):  # noqa: F811
    """Hangar stays up now, so no restart re-queues the job: the turn ends `gone` while it runs. On main the job
    failed (`sandbox_gone`); now it resumes from its checkpoint once the pools are back."""
    settings.power_providers = ["docker"]
    settings.job_restart_stagger_seconds = 0
    world["mode"].write_text("sleep")
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("svc", "svc"))
    await _until(jobs, job.id, {JobState.RUNNING})
    while not jobs.get(job.id).turns[0].run_id:
        await asyncio.sleep(0.05)
    s = jobs.sessions.get(jobs.get(job.id).session_id)
    assert await jobs.sessions.checkpoint_now(s.id, requester="power.sh") is not None  # the drain's checkpoint
    off(fleet.power)  # 01:00: power.sh tells hangar, then the pool and its pods go
    sb = fleet.get(jobs.get(job.id).sandbox_id)
    await fleet.providers["docker"].destroy(sb)
    fleet._mark_gone(sb, "disappeared from provider")
    for _ in range(200):
        if jobs.get(job.id).reason == power.WAITING:
            break
        await asyncio.sleep(0.05)
    j = jobs.get(job.id)
    assert j.state == JobState.QUEUED and j.restarts == 1, [e.message for e in j.events]
    assert j.turns[0].outcome == "gone" and j.turns[0].outcome_reason == "the sandbox pools powered down"
    world["mode"].write_text("ok")
    fleet.power.mark("up", "power.sh")
    fleet.power.release(jobs)
    done = await _until(jobs, job.id, DONE)
    assert done.state == JobState.SUCCEEDED and done.result.pushed, [e.message for e in done.events]
    box = fleet.providers["docker"].boxes[done.sandbox_id]
    prompts = sorted((box / "home/agent/.hangar/runs").glob("*.prompt"), key=lambda p: p.stat().st_mtime)
    assert "powered down for the night" in prompts[-1].read_text()


# ====================================================================== warm pools for the morning
def _sb(env: str, when: str, provider: str = "docker", parent: str | None = None) -> Sandbox:
    return Sandbox(name=f"s-{env}-{when}", environment=env, provider=provider, parent=parent, created_at=at(when))


def test_warm_pools_are_sized_from_last_weeks_creates_in_that_hour(settings, store):
    settings.power_providers = ["docker"]
    p = Power(store, settings)
    ready = at("2026-10-14T10:00:00")
    boxes = [_sb("py", "2026-10-13T10:05:00"), _sb("py", "2026-10-13T10:40:00"),  # yesterday: 2 in the hour
             _sb("py", "2026-10-12T10:59:00"),  # 1
             _sb("py", "2026-10-13T11:00:00"), _sb("py", "2026-10-13T09:59:00"),  # outside the hour
             _sb("py", "2026-10-06T10:30:00"),  # 8 days ago: outside the week
             _sb("py", "2026-10-13T10:10:00", parent="sb-1"),  # a fork: never from a warm pool
             _sb("go", "2026-10-09T10:01:00", provider="kubernetes"),  # not on the powered pools
             *[_sb("node", f"2026-10-10T10:{m:02d}:00") for m in range(0, 50, 10)],  # 5, capped at 3
             _sb("gone", "2026-10-13T10:20:00")]  # no longer in the catalog
    envs = ["py", "go", "node"]
    assert p.warm_plan(boxes, envs, ready, NY) == {"py": 2, "node": 3}
    settings.power_warm_pools = "py=1, go=4, nope=2, node=x"
    assert p.warm_plan(boxes, envs, ready, NY) == {"py": 1, "go": 4}
    settings.power_warm_pools = "off"
    assert p.warm_plan(boxes, envs, ready, NY) == {}


class WarmFake(FakeProvider):
    def __init__(self):
        super().__init__()
        self.synced: list[tuple[str, int]] = []

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(full_snapshots=True, warm_pools=True, pause=True)

    async def sync_warm_pool(self, spec, image):
        self.synced.append((spec.name, spec.warm_pool))


async def test_power_up_syncs_the_days_warm_pools_and_power_down_clears_them(fleet, settings, store):
    settings.power_providers = ["docker"]
    prov = WarmFake()
    fleet.providers["docker"] = prov
    store.kv_set("image:docker:plain", "img:plain")
    p = fleet.power
    p.clock = lambda: at("2026-10-14T09:58:00")
    own = warm_pool_now(fleet.catalog.get("plain"))  # the environment's own setting (at most 2)
    for m in (15, 16, 17):  # yesterday, in the hour after 10:00 (the ready time, not when power.sh reported)
        store.put(_sb("plain", f"2026-10-13T10:{m}:00"))
    p.mark("starting", "power.sh", time_zone="America/New_York", ready_at=at("2026-10-14T10:00:00"))
    p.mark("up", "power.sh")
    sizes = await p.size_warm_pools(fleet)
    assert sizes == {"plain": 3} and prov.synced == [("plain", 3)]
    assert fleet._warm(fleet.catalog.get("plain")).warm_pool == 3  # creates and refills keep that size
    assert store.kv_get_json(power.STATE_KEY)["warm"] == {"plain": 3}
    assert Power(store, settings).warm_for("plain") == 3  # a restart during the day keeps them
    off(p)
    await p.clear_warm_pools(fleet)
    assert prov.synced[-1] == ("plain", own) and fleet._warm(fleet.catalog.get("plain")).warm_pool == own


# ====================================================================== early power-up
async def test_power_up_runs_the_cloud_run_job_as_hangars_service_account(settings, store):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "metadata.google.internal":
            assert req.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "ya29.x", "expires_in": 3599})
        if req.url.path.endswith(":run"):
            return httpx.Response(200, json={"name": "projects/p/locations/r/operations/op-1",
                                             "metadata": {"name": "projects/p/locations/r/jobs/hangar-power-up/"
                                                                  "executions/hangar-power-up-abc"}})
        return httpx.Response(404)

    p = Power(store, settings)
    off(p)
    with pytest.raises(PowerError, match="HANGAR_POWER_UP_JOB"):
        await p.request_up("admin", httpx.MockTransport(handler))
    settings.power_up_job = "projects/p/locations/us-central1/jobs/hangar-power-up"
    out = await p.request_up("admin", httpx.MockTransport(handler))
    run = seen[-1]
    assert run.method == "POST" and str(run.url) == \
        "https://run.googleapis.com/v2/projects/p/locations/us-central1/jobs/hangar-power-up:run"
    assert run.headers["Authorization"] == "Bearer ya29.x"
    assert out["state"] == "starting" and out["execution"].endswith("hangar-power-up-abc") and p.off
    again = await p.request_up("admin", httpx.MockTransport(handler))  # already starting: no second execution
    assert again["state"] == "starting" and sum(r.url.path.endswith(":run") for r in seen) == 1
    p.mark("up", "power.sh")
    assert (await p.request_up("admin", httpx.MockTransport(handler)))["message"] == "Sandboxes are already on."

    def denied(req: httpx.Request) -> httpx.Response:
        if req.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(403, json={"error": {"message": "Permission 'run.jobs.run' denied"}})

    off(p)
    with pytest.raises(PowerError, match=r"403 Permission 'run.jobs.run' denied.*roles/run.invoker"):
        await p.request_up("admin", httpx.MockTransport(denied))
    assert p.state()["state"] == "down"


# ====================================================================== prepull
def test_the_prepull_daemonset_pulls_every_image_onto_sandbox_nodes_and_runs_none(settings):
    refs = ["r/hangar-env-py@sha256:aa", "r/hangar-pack-codex@sha256:bb"]
    m = power.prepull_manifest(settings, refs)
    pod = m["spec"]["template"]["spec"]
    assert m["kind"] == "DaemonSet" and m["metadata"] == {"name": "hangar-prepull", "namespace": "hangar",
                                                         "labels": {"app": "hangar-prepull",
                                                                    "app.kubernetes.io/managed-by": "hangar"}}
    assert [v["image"]["reference"] for v in pod["volumes"]] == refs
    assert all(v["image"]["pullPolicy"] == "IfNotPresent" for v in pod["volumes"])
    (c,) = pod["containers"]
    assert c["image"] == settings.power_prepull_image and len(c["volumeMounts"]) == 2
    assert c["securityContext"]["capabilities"] == {"drop": ["ALL"]} and pod["automountServiceAccountToken"] is False
    assert pod["runtimeClassName"] == "gvisor" and pod["nodeSelector"] == {"sandbox.gke.io/runtime": "gvisor"}
    assert pod["tolerations"][0]["key"] == "sandbox.gke.io/runtime"
    assert m["spec"]["updateStrategy"]["rollingUpdate"]["maxUnavailable"] == "100%"


# ====================================================================== the operator's view
def test_doctor_and_the_overview_show_power_the_last_deploy_and_the_loops(settings):
    app = _app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer t0ken"
        r = client.post("/api/admin/deploys", json={
            "result": "rolled_back", "image": "r/control-plane:abc@sha256:1", "version": "0.1.0+tools.x",
            "run_url": "https://github.com/o/r/actions/runs/1", "summary": "failed: mcp", "checks": {"mcp": "fail"}})
        assert r.status_code == 200 and r.json()["result"] == "rolled_back", r.text
        assert client.post("/api/admin/deploys", json={"result": "great"}).status_code == 400
        assert client.post("/api/admin/deploys", json={"result": "passed", "run_url": "javascript:x"}).json().get(
            "run_url") is None
        assert [d["result"] for d in client.get("/api/admin/deploys").json()] == ["passed", "rolled_back"]
        app.state.hangar.fleet.power.started = time.time() - 6 * 3600  # up for 6 hours
        metrics.TICKS["reaper"] = time.time() - 4 * 3600 - 300  # the test settings pass every hour
        checks = {c["name"]: c for c in client.get("/api/doctor").json()}
        assert checks["sandbox power"]["status"] == "ok"
        assert checks["last deploy"]["status"] == "ok"
        assert "reaper last ran" in checks["background loops"]["detail"]
        assert checks["background loops"]["status"] == "warn"
        client.post("/api/admin/deploys", json={"result": "rolled_back", "version": "v2", "summary": "failed: mcp"})
        checks = {c["name"]: c for c in client.get("/api/doctor").json()}
        assert checks["last deploy"]["status"] == "warn" and "rolled back" in checks["last deploy"]["detail"]
        client.post("/api/admin/power/state", json={"state": "down", "up_cron": "50 9 * * *",
                                                    "time_zone": "America/New_York"})
        checks = {c["name"]: c for c in client.get("/api/doctor").json()}
        assert checks["sandbox power"]["status"] == "ok" and "power on at" in checks["sandbox power"]["detail"]
        o = client.get("/api/overview").json()
        assert o["power"]["state"] == "down" and o["last_deploy"]["version"] == "v2"
        assert o["loops"]["reaper"]["ok"] is False
    metrics.TICKS.pop("reaper", None)


def test_power_up_route_explains_what_is_missing(settings):
    app = _app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer t0ken"
        client.post("/api/admin/power/state", json={"state": "down"})
        r = client.post("/api/admin/power/up")
        assert r.status_code == 409 and "HANGAR_POWER_UP_JOB" in r.json()["detail"], r.text


def test_the_prepull_route_needs_the_kubernetes_provider(settings):
    app = _app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer t0ken"
        r = client.get("/api/admin/power/prepull")
        assert r.status_code == 409 and "kubernetes" in r.json()["detail"]
        assert client.get("/api/admin/power/prepull", params={"namespace": "../x"}).status_code == 400
        assert json.loads(json.dumps(power.prepull_manifest(app.state.hangar.settings, [])))["kind"] == "DaemonSet"


def test_hangar_power_cli(monkeypatch):
    from typer.testing import CliRunner

    from hangar import cli

    t = time.time()
    last_up = {"at": t - 3600, "by": "power.sh", "prepull": {"images": 7, "seconds": 41.0, "nodes": 1}}
    view = {"power": {"state": "down", "stale": False, "when": "10:00 EDT (in 8 h 0 min)", "jobs_waiting": 2,
                      "warm_pools": {}, "last_up": last_up, "last_down": t - 60},
            "last_deploy": {"at": t, "result": "passed", "version": "0.1.0+tools.abc", "run_url": "https://x/1"},
            "loops": {"reaper": {"last": t - 5, "ok": True}, "sessions": {"last": None, "ok": False}}}
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path))
        return view if method == "GET" else {"state": "starting", "message": "Sandboxes are powering on now.",
                                             "execution": "projects/p/locations/r/jobs/hangar-power-up/executions/e"}

    monkeypatch.setattr(cli, "_call", fake)
    r = CliRunner().invoke(cli.app, ["power"])
    assert r.exit_code == 0, r.output
    assert "sandboxes: powered down; ready at 10:00 EDT (in 8 h 0 min)" in r.output
    assert "jobs waiting for power: 2" in r.output and "7 images pulled onto 1 node(s) in 41.0 s" in r.output
    assert "last deploy:" in r.output and "passed" in r.output and "overdue: sessions" in r.output
    r = CliRunner().invoke(cli.app, ["power", "up"])
    assert r.exit_code == 0 and "powering on" in r.output and calls[-1] == ("POST", "/api/admin/power/up")


# ====================================================================== --- infra-state: holds, the pending deploy
def test_a_hold_keeps_the_pools_off_until_the_first_power_up_after_it(settings, store):
    """The power-up the power-down named is skipped (the jobs honour the hold), so hangar mustn't call the pools
    stale an hour after it and send creates to nodes that won't come."""
    p = Power(store, settings, clock=lambda: at("2026-10-14T01:00:00"))
    off(p)
    powerhold.set_hold(store, {"until": at("2026-10-17T00:00:00")}, "sean", NY, p.clock())
    p.clock = lambda: at("2026-10-14T12:00:00")  # past 10:00 and the hour's grace
    st = p.state()
    assert not st.get("stale") and p.off and st["ready_at"] == at("2026-10-17T10:00:00")
    msg = p.message()
    assert "held off until Sat 17 Oct 00:00 EDT" in msg and "power on at 10:00 EDT" in msg, msg
    assert p.hold_view()["by"] == "sean" and "held off until" in p.status_line()
    assert p.overview()["power"]["hold"]["when"] == "Sat 17 Oct 00:00 EDT"
    p.clock = lambda: at("2026-10-17T09:00:00")  # the hold is over: off until the next scheduled power-up
    assert p.off and p.state()["ready_at"] == at("2026-10-17T10:00:00") and p.hold_view() is None
    p.clock = lambda: at("2026-10-17T11:30:00")  # and past that, stale as before
    assert p.state().get("stale") and not p.off
    powerhold.end_hold(store, None, "sean", at("2026-10-17T11:30:00"))
    p.clock = lambda: at("2026-10-14T12:00:00")
    assert p.state().get("stale")  # a released hold changes nothing


def test_hold_and_release_over_rest_admins_only(settings):
    app = _app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer t0ken"
        r = client.post("/api/admin/power/hold", json={"days": 7})
        assert r.status_code == 200 and "Held until" in r.json()["message"], r.text
        hold = client.get("/api/admin/power").json()["power"]["hold"]
        assert 7 * 86400 - 60 < hold["until"] - time.time() <= 7 * 86400 and hold["by"]
        assert client.get("/api/admin/power/hold").json()["record"]["until"] == hold["until"]
        assert client.get("/api/status").json()["power"]["hold"]["until"] == hold["until"]  # `hangar status`
        checks = {c["name"]: c for c in client.get("/api/doctor").json()}
        assert checks["power hold"]["status"] == "ok" and "held until" in checks["power hold"]["detail"]
        for bad in ({}, {"days": 0}, {"days": 91}, {"days": "x"}, {"days": True}, {"days": 1, "until": 2e9},
                    {"until": "2001-01-01"}, {"until": "soon"}, {"until": time.time() + 100 * 86400}):
            assert client.post("/api/admin/power/hold", json=bad).status_code == 400, bad
        # power.sh copies the cluster's hold with when it was set there, never later than now
        r = client.post("/api/admin/power/hold", json={"until": "2099-01-01", "set_at": time.time() + 3600})
        assert r.status_code == 400  # more than 90 days
        until = time.time() + 86400
        assert client.post("/api/admin/power/hold", json={"until": until, "set_at": time.time() + 3600}).is_success
        assert client.get("/api/admin/power/hold").json()["record"]["set_at"] <= time.time()
        assert client.post("/api/admin/power/release").json()["hold"] is None
        assert client.get("/api/admin/power").json()["power"]["hold"] is None
        assert "power hold" not in {c["name"] for c in client.get("/api/doctor").json()}
        jobs = client.post("/api/tokens", json={"name": "svc", "scopes": ["jobs"]}).json()["token"]
        client.headers["Authorization"] = f"Bearer {jobs}"
        for path, body in (("/api/admin/power/hold", {"days": 1}), ("/api/admin/power/release", None),
                           ("/api/admin/deploys/pending", {"head": None})):
            assert client.post(path, json=body).status_code == 403, path
        assert client.get("/api/admin/power/hold").status_code == 403


async def test_hangar_power_up_ends_the_hold(settings, store):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, json={"metadata": {"name": "e"}})

    def denied(req: httpx.Request) -> httpx.Response:
        if req.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(403, json={"error": {"message": "denied"}})

    settings.power_up_job = "projects/p/locations/r/jobs/hangar-power-up"
    p = Power(store, settings)
    off(p)
    powerhold.set_hold(store, {"days": 5}, "sean", NY, time.time())
    assert p.hold_view()
    with pytest.raises(PowerError):  # the job didn't start: the hold stays
        await p.request_up("admin", httpx.MockTransport(denied))
    assert p.hold_view()["by"] == "sean"
    await p.request_up("admin", httpx.MockTransport(handler))
    assert p.hold_view() is None and powerhold.record(store)["until"] is None  # the job reads this before it decides


def test_a_deploy_waiting_for_a_power_up_shows_until_one_is_recorded(settings):
    app = _app(settings)
    head, old = "a" * 40, "b" * 40
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer t0ken"
        body = {"repo": "o/r", "head": head, "deployed": old, "ahead": 3, "started": False}
        assert client.post("/api/admin/deploys/pending", json=body).status_code == 200
        for bad in ({**body, "head": "x; rm"}, {**body, "repo": "o/r --x"}, {**body, "ahead": "many"}):
            assert client.post("/api/admin/deploys/pending", json=bad).status_code == 400, bad
        checks = {c["name"]: c for c in client.get("/api/doctor").json()}
        assert checks["deploy pending"]["status"] == "warn"
        assert checks["deploy pending"]["fix"] == "gh workflow run deploy.yml --repo o/r --ref main, then approve it"
        assert client.get("/api/admin/power").json()["power"]["deploy_pending"]["ahead"] == 3
        client.post("/api/admin/deploys/pending", json={**body, "started": True})
        assert {c["name"]: c for c in client.get("/api/doctor").json()}["deploy pending"]["status"] == "ok"
        client.post("/api/admin/deploys", json={"result": "passed", "sha": head})
        assert client.get("/api/admin/power").json()["power"]["deploy_pending"] is None
        client.post("/api/admin/deploys/pending", json=body)
        client.post("/api/admin/deploys/pending", json={"head": None})  # a power-up found nothing to do
        assert "deploy pending" not in {c["name"] for c in client.get("/api/doctor").json()}


def test_hangar_power_hold_and_release_cli(monkeypatch):
    from typer.testing import CliRunner

    from hangar import cli

    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if path == "/api/admin/power":
            return {"power": {"state": "down", "stale": False, "when": "10:00 EDT", "jobs_waiting": 0,
                              "warm_pools": {}, "hold": {"until": 1, "by": "sean", "when": "Sat 17 Oct 00:00 EDT"},
                              "deploy_pending": {"head": "a" * 40, "ahead": 2, "started": False,
                                                 "command": "gh workflow run deploy.yml --repo o/r --ref main"}},
                    "loops": {}}
        return {"message": "Held until Sat 17 Oct 00:00 EDT"}

    monkeypatch.setattr(cli, "_call", fake)
    r = CliRunner().invoke(cli.app, ["power", "hold", "--days", "3"])
    assert r.exit_code == 0 and calls[-1] == ("POST", "/api/admin/power/hold", {"days": 3.0}), r.output
    r = CliRunner().invoke(cli.app, ["power", "hold", "--until", "2026-10-22T09:00-04:00"])
    assert r.exit_code == 0 and calls[-1][2] == {"until": datetime.fromisoformat("2026-10-22T09:00-04:00").timestamp()}
    assert CliRunner().invoke(cli.app, ["power", "hold"]).exit_code != 0  # --days or --until
    assert CliRunner().invoke(cli.app, ["power", "hold", "--until", "someday"]).exit_code != 0
    r = CliRunner().invoke(cli.app, ["power", "release"])
    assert r.exit_code == 0 and calls[-1][:2] == ("POST", "/api/admin/power/release")
    r = CliRunner().invoke(cli.app, ["power"])
    assert "held until Sat 17 Oct 00:00 EDT by sean" in r.output
    assert "deploy pending: main (aaaaaaaaaaaa) is 2 commit(s) ahead" in r.output and "gh workflow run" in r.output


# ====================================================================== deploy.yml
def _workflow(name: str) -> tuple[str, dict]:
    import yaml

    from tests.test_acceptance_script import ROOT

    text = (ROOT / ".github" / "workflows" / name).read_text()
    return text, yaml.safe_load(text)


def test_the_deploy_secrets_are_behind_the_approval():
    """The admin token and the deploy identity are `production` environment secrets: as repository secrets any
    workflow on any branch could read them, so the required reviewer would gate nothing. No fallback to the images
    identity (impersonable from every ref), and no trigger that runs a pull request's code with them."""
    from tests.test_acceptance_script import ROOT

    text, wf = _workflow("deploy.yml")
    # --- infra-state: a merge to main too, only while the infra is up (test_power_script)
    assert set(wf[True]) == {"workflow_dispatch", "push"} and wf[True]["push"] == {"branches": ["main"],
                                                                                  "tags": ["v*"]}
    for f in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "pull_request_target" not in f.read_text(), f
    deploy = wf["jobs"]["deploy"]
    assert deploy["environment"] == "production"
    for name, job in wf["jobs"].items():
        if name != "deploy":
            named = json.dumps(job)
            assert "HANGAR_DEPLOY_TOKEN" not in named and "GCP_DEPLOY_SERVICE_ACCOUNT" not in named, name
    assert "secrets.GCP_SERVICE_ACCOUNT" not in json.dumps(deploy)
    assert "environment secrets" in text


def test_the_rollback_restores_the_image_it_replaced():
    """`rollout undo` goes back one revision: when `set image` failed, or the run redeployed the running image (no
    new revision), it rolled back to the version *before* the running one. A cancelled or timed-out run rolls back
    too, and a doctor failure from before the deploy doesn't roll it back (test_acceptance_script)."""
    _, wf = _workflow("deploy.yml")
    steps = {s.get("name") or s.get("uses"): s for s in wf["jobs"]["deploy"]["steps"]}
    rb = steps["Roll back"]
    assert "rollout undo" not in rb["run"] and 'hangar="$PREVIOUS"' in rb["run"]
    assert "cancelled()" in rb["if"] and "${{" not in rb["run"]
    assert steps["Acceptance"].get("timeout-minutes") and "--doctor-baseline" in steps["Acceptance"]["run"]
    assert "doctor-failing" in steps["Before (power state, the server's tools, the running image)"]["run"]
