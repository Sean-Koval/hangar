"""Restarts and the nightly power cycle lose nothing and strand nothing (reconciler.py, restarts.py, redeliver.py,
drain.py). Each bug test failed on main: the row stayed `starting`/`stopping` with its lease, a replaced pod passed
for the sandbox, an interrupted job failed as `interrupted`, a half-handled delivery was refused as a replay."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import shutil
import time
import uuid
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi.testclient import TestClient

from hangar import metrics, redeliver, restarts
from hangar.drain import Drain
from hangar.envspec import EnvCatalog
from hangar.fleet import Fleet
from hangar.jobs import Caller, JobService, SubmitJob
from hangar.models import CredentialKind, Job, JobState, Sandbox, SandboxState, Turn
from tests.conftest import ENVS, FakeProvider
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures

needs_git = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
S = SandboxState


class Blocking(FakeProvider):
    """The first sandbox to start never reports running: a create stuck there when the control plane went down."""

    def __init__(self):
        super().__init__()
        self.block_next = True
        self.slow_reads = 0.0

    async def status(self, sandbox):
        if self.block_next:
            self.block_next = False
            await asyncio.Event().wait()
        return await super().status(sandbox)

    async def read_file(self, sandbox, path, limit=None):
        if self.slow_reads:
            await asyncio.sleep(self.slow_reads)
        return await super().read_file(sandbox, path, limit)

    async def find_ref(self, sandbox):
        return sandbox.id if sandbox.id in self.boxes else None


def restarted(settings, store, broker, prov) -> Fleet:
    """A new control-plane process on the same database: nothing in it owns any row yet."""
    f = Fleet(settings, store, broker, EnvCatalog([ENVS]))
    f.providers["docker"] = prov
    return f


async def finish(tasks):
    await asyncio.gather(*tasks)


# ====================================================================== orphaned creates (B1 / brief B2)
async def test_a_restart_mid_create_frees_the_lease_and_tears_the_sandbox_down(fleet, broker, store, settings):
    prov = Blocking()
    fleet.providers["docker"] = prov
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, "{}")
    stuck = asyncio.create_task(fleet.create("plain", credentials=["codex"]))
    try:
        while not prov.boxes:
            await asyncio.sleep(0.01)
        sb = store.list(Sandbox)[0]
        assert sb.state == S.STARTING and broker.get("codex").leased_to == sb.id
        # the running process owns the row: its own passes leave it alone, however old
        assert await fleet.reconciler.run(boot=True) == [] and store.get(Sandbox, sb.id).state == S.STARTING

        again = restarted(settings, store, broker, prov)
        await finish(await again.reconciler.run(boot=True))
        row = store.get(Sandbox, sb.id)
        assert row.state == S.ERROR and sb.provider_ref not in prov.boxes
        assert row.info["cause"] == "orphaned_by_restart" and row.info["failed_stage"] == "starting the sandbox"
        assert row.error.startswith("orphaned by a control-plane restart while: starting the sandbox")
        assert "starting the sandbox" in [m for _, m in row.info["stages"]]
        assert broker.get("codex").leased_to is None
        ok = await again.create("plain", credentials=["codex"])  # on main: "exclusive ... in use" for 4 hours
        assert ok.state == S.RUNNING
    finally:
        stuck.cancel()
        with contextlib.suppress(BaseException):
            await stuck


async def test_a_create_that_never_stored_its_provider_ref_is_found_by_label(fleet, store, settings, broker):
    prov = Blocking()
    sb = Sandbox(name="x", environment="plain", provider="docker", state=S.STARTING)
    await prov.create(sb, "img")
    sb.provider_ref = None  # the restart came between the provider's create and the store write
    store.put(sb)
    await finish(await restarted(settings, store, broker, prov).reconciler.run(boot=True))
    row = store.get(Sandbox, sb.id)
    assert row.state == S.ERROR and row.provider_ref == sb.id and sb.id not in prov.boxes


async def test_a_young_unowned_row_waits_for_startup_or_a_minute(fleet, store, settings, broker):
    sb = Sandbox(name="x", environment="plain", provider="docker", state=S.PENDING)
    store.put(sb)
    assert await fleet.reconciler.run() == [] and store.get(Sandbox, sb.id).state == S.PENDING
    sb.created_at -= 600
    store.put(sb)
    await finish(await fleet.reconciler.run())
    assert store.get(Sandbox, sb.id).state == S.ERROR


# ====================================================================== destroys (hardening B2)
async def test_a_cancelled_destroy_still_finishes(fleet, broker, store):
    prov = Blocking()
    prov.block_next = False
    fleet.providers["docker"] = prov
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, "{}")
    sb = await fleet.create("plain", credentials=["codex"])
    prov.slow_reads = 0.5  # writing the auth file back takes a while
    t = asyncio.create_task(fleet.destroy(sb.id))
    await asyncio.sleep(0.2)
    t.cancel()  # a client that gave up, or shutdown
    with contextlib.suppress(asyncio.CancelledError):
        await t
    for _ in range(60):
        if store.get(Sandbox, sb.id).state == S.STOPPED:
            break
        await asyncio.sleep(0.05)
    # on main: `stopping` for good, still running, still holding the lease
    assert store.get(Sandbox, sb.id).state == S.STOPPED
    assert broker.get("codex").leased_to is None and sb.provider_ref not in prov.boxes


async def test_a_destroy_orphaned_by_a_restart_is_finished_with_the_auth_file_written_back(fleet, broker, store,
                                                                                          settings):
    prov = Blocking()
    prov.block_next = False
    fleet.providers["docker"] = prov
    cred = broker.put("codex", "codex", CredentialKind.AUTH_FILE, "{}")
    sb = await fleet.create("plain", credentials=["codex"])
    path = cred.file_path or broker.get("codex").file_path
    (prov.boxes[sb.provider_ref] / "home/agent" / path.removeprefix("~/")).write_text('{"rotated": 1}')
    row = store.get(Sandbox, sb.id)
    row.state = S.STOPPING  # the process died in the middle of destroying it
    store.put(row)
    await finish(await restarted(settings, store, broker, prov).reconciler.run(boot=True))
    row = store.get(Sandbox, sb.id)
    assert row.state == S.STOPPED and row.info["cause"] == "orphaned_by_restart"
    assert sb.provider_ref not in prov.boxes and broker.get("codex").leased_to is None
    assert broker.vault.get("cred:codex") == b'{"rotated": 1}'


# ====================================================================== replaced pods (B3)
class Replacing(FakeProvider):
    def __init__(self):
        super().__init__()
        self.blank: set[str] = set()

    async def replaced(self, sandboxes):
        return {sb.id: "its pod was replaced" for sb in sandboxes if sb.id in self.blank}


async def test_a_replaced_pod_counts_as_gone_and_its_claim_goes(fleet, broker, store):
    prov = Replacing()
    fleet.providers["docker"] = prov
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, "{}")
    a = await fleet.create("plain", credentials=["codex"])
    b = await fleet.create("plain")
    prov.blank.add(a.id)
    await fleet.reconcile()
    row = store.get(Sandbox, a.id)
    assert row.state == S.STOPPED and row.info["cause"] == "pod_replaced" and "replaced" in row.error
    assert broker.get("codex").leased_to is None
    assert store.get(Sandbox, b.id).state == S.RUNNING
    for _ in range(50):  # the blank pod's claim is destroyed in the background
        if a.provider_ref not in prov.boxes:
            break
        await asyncio.sleep(0.02)
    assert a.provider_ref not in prov.boxes


class SlowPause(Replacing):
    """GKE pause: the pod goes away (snapshot, then Suspended) long before fleet.pause writes `paused`."""

    def __init__(self):
        super().__init__()
        self.pausing = asyncio.Event()
        self.release = asyncio.Event()

    async def pause(self, sandbox):
        self.blank.add(sandbox.id)  # its pod is gone now, as `replaced` sees it
        self.pausing.set()
        await self.release.wait()
        self.blank.discard(sandbox.id)


async def test_a_pass_during_a_pause_leaves_the_sandbox_alone(fleet, broker, store):
    prov = SlowPause()
    fleet.providers["docker"] = prov
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, "{}")
    sb = await fleet.create("plain", credentials=["codex"])
    pausing = asyncio.create_task(fleet.pause(sb.id))
    await prov.pausing.wait()
    await fleet.reconcile()  # the row still says running and the pod is gone: not a replaced pod, a pause
    await asyncio.sleep(0.05)
    prov.release.set()
    await pausing
    row = store.get(Sandbox, sb.id)
    # on the PR head: marked gone mid-pause, its lease released and its claim (with the snapshot) destroyed
    assert row.state == S.PAUSED and "cause" not in row.info
    assert broker.get("codex").leased_to == sb.id and sb.provider_ref in prov.boxes


async def test_a_pod_judged_replaced_is_left_alone_if_the_row_moved_to_a_new_pod_meanwhile(fleet, store):
    prov = Replacing()
    fleet.providers["docker"] = prov
    sb = await fleet.create("plain")
    row = store.get(Sandbox, sb.id)
    row.info["pod_uid"] = "uid-a"
    store.put(row)
    real = prov.replaced

    async def replaced(sandboxes):
        out = await real(sandboxes)
        moved = store.get(Sandbox, sb.id)  # paused and resumed onto a new pod while the list was answering
        moved.info["pod_uid"] = "uid-b"
        store.put(moved)
        return out

    prov.replaced = replaced
    prov.blank.add(sb.id)
    await fleet.reconcile()
    assert store.get(Sandbox, sb.id).state == S.RUNNING and sb.provider_ref in prov.boxes


async def test_the_reconciler_runs_on_its_own_task(fleet, store, settings):
    """A reaper stuck in a warm-pool sync (a Cloud Build) no longer holds up reconcile."""
    prov = Replacing()
    fleet.providers["docker"] = prov
    sb = await fleet.create("plain")
    prov.blank.add(sb.id)
    stuck = asyncio.Event()

    async def hang():
        await stuck.wait()

    fleet.sync_scheduled_pools = hang
    settings.reaper_interval_seconds = 0
    settings.reconcile_interval_seconds = 1
    reaper = asyncio.create_task(fleet._reaper_loop())
    fleet.reconciler.start()
    try:
        for _ in range(60):
            if store.get(Sandbox, sb.id).state == S.STOPPED:
                break
            await asyncio.sleep(0.05)
        assert store.get(Sandbox, sb.id).state == S.STOPPED
    finally:
        reaper.cancel()
        await fleet.reconciler.stop()


# --- the Kubernetes side: pod UIDs (kubernetes is an optional extra; these tests skip without it)


def _k8s():
    pytest.importorskip("kubernetes")
    from kubernetes.client.rest import ApiException

    from hangar.config import Settings
    from hangar.providers.kubernetes import KubernetesProvider

    class Core:
        def __init__(self):
            self.pods: dict[str, str] = {}  # name -> uid

        def list_namespaced_pod(self, ns, label_selector=None):
            return NS(items=[NS(metadata=NS(name=n, uid=u)) for n, u in self.pods.items()])

        def read_namespaced_pod(self, name, ns):
            if name not in self.pods:
                raise ApiException(status=404)
            return NS(metadata=NS(name=name, uid=self.pods[name]), status=NS(phase="Running", pod_ip="10.0.0.9"))

    class CRD:
        def __init__(self):
            self.claims: dict[str, dict] = {}

        def get_namespaced_custom_object(self, group, version, ns, plural, name):
            if name not in self.claims:
                raise ApiException(status=404)
            return self.claims[name]

        def list_namespaced_custom_object(self, group, version, ns, plural, label_selector=None):
            k, _, v = (label_selector or "").partition("=")
            return {"items": [c for c in self.claims.values() if c["metadata"]["labels"].get(k) == v]}

    p = object.__new__(KubernetesProvider)
    p.settings = Settings()
    p.ns = "hangar"
    p.core, p.crd = Core(), CRD()
    return p


def _ksb(pod: str, uid: str | None = None) -> Sandbox:
    info = {"pod": pod, **({"pod_uid": uid} if uid else {})}
    return Sandbox(name=pod, environment="py", provider="kubernetes", provider_ref=f"hangar-{pod}", info=info,
                   state=S.RUNNING)


async def test_kubernetes_tells_a_replaced_pod_from_the_sandboxs_own():
    p = _k8s()
    p.core.pods = {"p-a": "u2", "p-c": "u4", "p-d": "u5"}
    a, b, c, d = _ksb("p-a", "u1"), _ksb("p-b", "u3"), _ksb("p-c", "u4"), _ksb("p-d")
    out = await p.replaced([a, b, c, d])
    assert set(out) == {a.id, b.id} and "replaced" in out[a.id] and "gone" in out[b.id]
    assert d.info["pod_uid"] == "u5"  # from before UIDs were recorded: its current pod is adopted


async def test_kubernetes_status_and_pod_lookup_refuse_a_replaced_pod():
    from hangar.providers.base import ProviderError

    p = _k8s()
    sb = _ksb("p-a")
    p.crd.claims[sb.provider_ref] = {"metadata": {"name": sb.provider_ref, "labels": {"dev.hangar/sandbox": sb.id}},
                                     "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    p.core.pods = {"p-a": "u1"}
    assert await p.status(sb) == S.RUNNING and sb.info["pod_uid"] == "u1"  # recorded on first sight
    p.core.pods = {"p-a": "u2"}  # node repair recreated it under the same name
    assert await p.status(sb) == S.STOPPED
    p.core.pods = {}
    assert await p.status(sb) == S.STOPPED
    sb.info.pop("pod")
    with pytest.raises(ProviderError, match="replaced"):  # on main the blank pod's name came back
        p.crd.claims[sb.provider_ref]["status"]["sandbox"] = {"name": "p-a"}
        p.crd.get_namespaced_custom_object = (lambda g, v, ns, plural, name, _o=p.crd.get_namespaced_custom_object:
                                              {"metadata": {"name": "p-a"}} if plural == "sandboxes"
                                              else _o(g, v, ns, plural, name))
        p.core.pods = {"p-a": "u2"}
        await p._pod(sb)
    assert await p.find_ref(sb) == sb.provider_ref


async def test_envd_endpoints_are_cached_per_pod(settings):
    from pathlib import Path

    from hangar.envd import Endpoint, Envd

    settings.envd = "off"
    e = Envd(FakeProvider(), settings)
    e.binaries = {"x86_64": Path("/dev/null")}
    started = []

    async def bring_up(sb):
        started.append(sb.id)

    e._bring_up = bring_up
    sb = _ksb("p-a", "u1")
    e._eps[sb.id] = Endpoint(url="http://10.0.0.1:49983", token="t", ref=e.instance(sb))
    assert await e.endpoint(sb) is not None
    sb.info["pod_uid"] = "u2"  # same claim, another pod: on main the old address was still used
    assert await e.endpoint(sb) is None
    await asyncio.sleep(0)
    assert started == [sb.id]
    assert Envd.instance(Sandbox(name="d", environment="e", provider="docker", provider_ref="c1")) == "c1"


# ====================================================================== jobs after a restart (B5)
@needs_git
async def test_a_job_interrupted_by_a_restart_resumes_from_its_checkpoint(jobs, world, fleet):  # noqa: F811
    world["mode"].write_text("sleep")
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("svc", "svc"))
    await _until(jobs, job.id, {JobState.RUNNING})
    while not jobs.get(job.id).turns[0].run_id:
        await asyncio.sleep(0.05)
    ses = jobs.sessions
    s = ses.get(jobs.get(job.id).session_id)
    assert await ses.checkpoint_now(s.id, requester="test") is not None  # a periodic checkpoint
    # 01:00: the control plane goes down, and the sandbox with the nodes
    await jobs.stop()
    await ses.stop()
    sb = fleet.get(jobs.get(job.id).sandbox_id)
    await fleet.providers["docker"].destroy(sb)
    fleet._mark_gone(sb, "disappeared from provider")
    world["mode"].write_text("ok")
    await ses.start()
    again = JobService(fleet, ses, transport=jobs.transport)
    await again.start()
    try:
        j = again.get(job.id)
        assert j.state in (JobState.QUEUED, JobState.PROVISIONING, JobState.RUNNING) and j.restarts == 1
        assert any("resuming from its last checkpoint" in e.message for e in j.events)
        done = await _until(again, job.id, DONE)
        # on main: failed, reason interrupted, "follow up to continue from its last checkpoint"
        assert done.state == JobState.SUCCEEDED and done.result.pushed, [e.message for e in done.events]
        assert done.turns[0].outcome == "gone" and done.turns[-1].trigger == "restart"
        box_ = fleet.providers["docker"].boxes[done.sandbox_id]
        prompts = sorted((box_ / "home/agent/.hangar/runs").glob("*.prompt"), key=lambda p: p.stat().st_mtime)
        assert "control plane restarted" in prompts[-1].read_text()
    finally:
        await again.stop()


@needs_git
async def test_a_job_caught_provisioning_starts_over(jobs, world, fleet):  # noqa: F811
    await jobs.stop()
    job = Job(title="t", instructions="x", repo=world["url"], environment="jobenv", branch="hangar/restart-prov",
              state=JobState.PROVISIONING, owner="svc", requester="svc", turns=[Turn()])
    jobs.store.put(job)
    again = JobService(fleet, jobs.sessions, transport=jobs.transport)
    await again.start()
    try:
        done = await _until(again, job.id, DONE)
        assert done.state == JobState.SUCCEEDED and done.restarts == 1, [e.message for e in done.events]
        assert any("starting over" in e.message for e in done.events)
    finally:
        await again.stop()


def test_a_job_resumes_on_its_own_only_so_often():
    job = Job(title="t", instructions="x", environment="e", state=JobState.RUNNING, restarts=2, session_id="ses-1",
              turns=[Turn(run_id="run-00000001")])
    svc = NS(settings=NS(job_restart_resumes=2), _session=lambda j: NS(), sessions=NS(resumable=lambda s: True))
    assert not restarts.resume(svc, job) and not restarts.start_over(svc, job)


async def test_resumed_jobs_reenter_the_queue_apart():
    tasks = []
    svc = NS(settings=NS(job_restart_stagger_seconds=0.3), _queue=asyncio.Queue(),
             _spawn=lambda c: tasks.append(asyncio.ensure_future(c)))
    restarts.release(svc, ["a", "b", "c"])
    await asyncio.sleep(0.1)
    assert svc._queue.qsize() == 1
    await asyncio.gather(*tasks)
    assert [svc._queue.get_nowait() for _ in range(3)] == ["a", "b", "c"]


# ====================================================================== GitHub deliveries (B5 / H7)
def _signed(body: bytes, key: str = "ghsec") -> dict[str, str]:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest(),
            "X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": uuid.uuid4().hex}


def _hook_app(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    return create_app(settings)


@needs_git
def test_a_half_handled_delivery_runs_again_and_a_handled_one_does_not(settings):
    app = _hook_app(settings)
    with TestClient(app) as client:
        h = app.state.hangar
        h.fleet.providers["docker"] = FakeProvider()
        h.jobs._queue = asyncio.Queue()
        h.broker.put("gh-hook", "webhook", CredentialKind.GENERIC, "ghsec")
        h.settings.github_webhook_secret_credential = "gh-hook"
        h.settings.github_repos = {"o/r": "plain"}
        job = h.jobs.submit(SubmitJob(instructions="x", environment="plain", deliver="none",
                                      repo="https://github.com/o/r.git"), Caller("admin", "admin"))
        job.state, job.result.pr_number = JobState.SUCCEEDED, 7
        calls = []
        h.jobs.follow_up = lambda *a, **kw: calls.append(a) or job
        body = json.dumps({"action": "created", "repository": {"full_name": "o/r"},
                           "issue": {"number": 7, "pull_request": {}},
                           "comment": {"id": 99, "body": "/hangar also do Y", "author_association": "OWNER",
                                       "user": {"login": "sean"}}}).encode()
        replays = h.jobs.admission.replays
        digest = replays.digest(body)
        # the pod that took this delivery died mid-handler: on main the mark made GitHub's redelivery a "replay"
        replays.put("github", digest, redeliver.PENDING + "an-earlier-process", 3600)
        r = client.post("/api/hooks/github", content=body, headers=_signed(body))
        assert r.status_code == 202 and len(calls) == 1
        assert replays.get("github", digest) == redeliver.DONE
        again = client.post("/api/hooks/github", content=body, headers=_signed(body))
        assert "duplicate" in again.json()["ignored"] and len(calls) == 1
        # a duplicate that arrives while this process is still handling the first copy is refused too
        replays.put("github", digest, redeliver.PENDING + redeliver.BOOT, 3600)
        assert "duplicate" in client.post("/api/hooks/github", content=body, headers=_signed(body)).json()["ignored"]


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_failed_deliveries_since_the_last_start_are_redelivered(settings, store):
    from hangar.github import GitHub

    t = time.time()
    settings.github_webhook_secret_credential = "gh-hook"
    settings.github_repos = {"o/r": "plain", "https://github.com/o/r.git": "plain"}
    settings.public_url = "https://hangar.example"
    pages = {
        None: ([{"id": 11, "guid": "g1", "event": "issues", "status_code": 502, "delivered_at": _iso(t - 3600)},
                {"id": 12, "guid": "g2", "event": "issue_comment", "status_code": 202, "delivered_at": _iso(t - 3600)},
                {"id": 14, "guid": "g3", "event": "issues", "status_code": 200, "redelivery": True,
                 "delivered_at": _iso(t - 1800)},
                {"id": 13, "guid": "g3", "event": "issues", "status_code": 0, "delivered_at": _iso(t - 7200)},
                {"id": 15, "guid": "g4", "event": "ping", "status_code": 502, "delivered_at": _iso(t - 600)}], "c2"),
        "c2": ([{"id": 16, "guid": "g5", "event": "issues", "status_code": 503, "delivered_at": _iso(t - 30 * 3600)}],
               "c3"),
    }
    seen, redelivered = [], []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path == "/repos/o/r/hooks":
            return httpx.Response(200, json=[{"id": 5, "config": {"url": "https://hangar.example/api/hooks/github"}}])
        if req.url.path == "/repos/o/r/hooks/5/deliveries":
            items, nxt = pages[req.url.params.get("cursor")]
            link = f'<https://api.github.com/repos/o/r/hooks/5/deliveries?per_page=100&cursor={nxt}>; rel="next"'
            return httpx.Response(200, json=items, headers={"Link": link})
        if req.method == "POST" and req.url.path.endswith("/attempts"):
            redelivered.append(int(req.url.path.split("/")[-2]))
            return httpx.Response(202, json={})
        return httpx.Response(404, json={"message": "nope"})

    audits = []
    h = NS(settings=settings, store=store, fleet=NS(audit=lambda *a, **kw: audits.append((a, kw))),
           jobs=NS(_github=lambda: GitHub("tok", transport=httpx.MockTransport(handle)), transport=True))
    assert await redeliver.replay_missed(h) == 1
    assert redelivered == [11]  # failed, recent, an event hangar handles, with no successful attempt since
    assert ("GET", "/repos/o/r/hooks/5/deliveries", {"per_page": "100", "cursor": "c2"}) in seen
    assert not any(p.get("cursor") == "c3" for _, _, p in seen)  # stopped at deliveries older than the window
    assert float(store.kv_get(redeliver.SINCE_KEY + "o/r")) >= t
    redelivered.clear()
    assert await redeliver.replay_missed(h) == 0 and not redelivered  # the next start begins where this one did


# ====================================================================== power-down drain and heartbeats
async def test_a_drain_holds_jobs_that_have_not_started_and_lets_them_go_after():
    svc = NS(_jobs={"j1": NS(state=JobState.QUEUED), "j2": NS(state=JobState.RUNNING)}, _queue=asyncio.Queue())
    d = Drain(svc)
    assert not d.hold("j1")
    d.begin(5)
    assert d.hold("j1") and not d.hold("j2") and d.held == ["j1"]
    d.end()
    assert svc._queue.get_nowait() == "j1" and not d.on and not d.held


def test_drain_routes_report_what_is_in_flight(settings):
    app = _hook_app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer t0ken"
        r = client.post("/api/admin/drain", json={"minutes": 30}).json()
        assert r["draining"] and r["settled"] and r["creating"] == [] and "suspending" in r["sessions"]
        assert client.get("/api/admin/drain").json()["draining"]
        assert not client.delete("/api/admin/drain").json()["draining"]
        assert client.post("/api/admin/drain", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_loop_heartbeats_are_exported():
    m = metrics.setup(True)
    try:
        metrics.loop_tick("reconciler")
        metrics.loop_failed("reaper")
        out = m.render().decode()
        assert 'hangar_loop_last_tick_timestamp{loop="reconciler"}' in out
        assert 'hangar_loop_failures_total{loop="reaper"} 1.0' in out
    finally:
        metrics.setup(False)
