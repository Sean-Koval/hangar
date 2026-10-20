"""KAN-140: `GET /api/fleet`, the Fleet page's one read: the humaniser, stage derivation, the server-side join, its
visibility, and its speed."""

from __future__ import annotations

import statistics
import time

import pytest
from fastapi.testclient import TestClient

from hangar import fleet_api, livestate
from hangar.events import RunEvent
from hangar.livestate import LiveState, StageStats, humanise, stage_signal
from hangar.models import (
    Approval,
    CredentialKind,
    Job,
    JobEvent,
    JobState,
    Sandbox,
    SandboxState,
    Session,
    SessionState,
    SessionTurn,
    now,
)

ADMIN = {"Authorization": "Bearer t0ken"}


def ev(kind: str, text: str = "", ts: float | None = None, **data) -> RunEvent:
    return RunEvent(kind=kind, text=text, data=data, ts=ts if ts is not None else now())


# ====================================================================== the humaniser
@pytest.mark.parametrize("event, line", [
    (ev("file_change", "src/refunds/models.py", path="src/refunds/models.py"), "editing src/refunds/models.py"),
    (ev("command", "pytest -q tests/test_refunds.py -x --maxfail=3 --durations=5 -p no:cacheprovider", tool="Bash"),
     "running pytest -q tests/test_refunds.py -x --maxfail=3 --durations=…"),  # the command at most 60 chars
    (ev("command", "git push -u origin kan-140", tool="Bash"), "running git push -u origin kan-140"),
    (ev("progress", "191 passed, writing the PR body", pct=80), "191 passed, writing the PR body"),
    (ev("progress", "tree 0123456789ab after a tool", source="hangar", action="tree"), None),
    (ev("progress", "the job's check passed", source="hangar", action="verify", passed=True),
     "the job's check passed"),
    (ev("approval_request", "Bash: git push", id="apr-1", tool="Bash", status="pending"), "waiting on you: Bash"),
    (ev("approval_request", "allowed by sean", id="apr-1", status="allowed", by="sean"), "approved; carrying on"),
    (ev("approval_request", "denied by policy", id="apr-1", status="denied", by="policy"), "denied by policy"),
    (ev("approval_request", "cancelled", id="apr-1", status="cancelled"), None),
    (ev("tool_call", "Read src/app.py", id="t1", tool="Read"), "Read src/app.py"),
    (ev("tool_result", "ok", id="t1", tool="Read", ok=True), None),
    (ev("usage", "1.2k tokens, $0.01", input_tokens=1000, output_tokens=200), None),
    (ev("error", "the model request failed"), "error: the model request failed"),
    (ev("message", "I'll start by reading the refunds module.\n\nThen the tests."),
     "I'll start by reading the refunds module."),
    (ev("message", "   "), None),
])
def test_the_humaniser_table(event, line):
    assert humanise(event) == line
    assert line is None or len(line) <= livestate.NOW_MAX


def test_now_keeps_the_last_line_that_said_something():
    st = LiveState("ses-1")
    st.observe(ev("command", "pytest -q"))
    assert st.now == "running pytest -q"
    st.observe(ev("tool_result", "191 passed"))  # says nothing new
    st.observe(ev("usage", "x", input_tokens=5))
    assert st.now == "running pytest -q" and st.events == 2
    st.observe(ev("file_change", "a.py", path="a.py"))
    assert st.now == "editing a.py"


# ====================================================================== stages
def test_stage_signals_and_the_timeline():
    assert stage_signal(ev("tool_call", "Read x", tool="Read")) == "plan"
    assert stage_signal(ev("file_change", "x", path="x")) == "edit"
    assert stage_signal(ev("command", "npm run test -- --ci")) == "test"
    assert stage_signal(ev("command", "git push origin HEAD")) == "pr"
    assert stage_signal(ev("command", "gh pr create --fill")) == "pr"
    assert stage_signal(ev("command", "ls -la")) is None
    assert stage_signal(ev("tool_result", "x")) is None
    st, t0 = LiveState("ses-1"), 1000.0
    assert st.stage is None and st.stages == []  # no signal: no stage
    st.observe(ev("tool_call", "Read x", ts=t0, tool="Read"))
    assert st.stage == "plan"
    st.observe(ev("tool_call", "Grep y", ts=t0 + 5, tool="Grep"))
    st.observe(ev("file_change", "a.py", ts=t0 + 60, path="a.py"))
    assert st.stage == "edit"
    st.observe(ev("tool_call", "Read z", ts=t0 + 70, tool="Read"))  # a read while editing is not a new plan
    assert st.stage == "edit"
    st.observe(ev("command", "pytest -q", ts=t0 + 300))
    st.observe(ev("command", "git commit -am fix", ts=t0 + 400))
    st.observe(ev("command", "git push", ts=t0 + 500))
    assert [s["stage"] for s in st.stages] == ["plan", "edit", "test", "pr"]
    assert st.stages[0] == {"stage": "plan", "started_at": t0, "ended_at": t0 + 60}
    assert st.stages[-1]["ended_at"] is None and st.stage_elapsed(t0 + 530) == 30
    assert st.commits == 1 and list(st.files) == ["a.py"]
    st.finish(t0 + 600)
    assert st.stages[-1]["ended_at"] == t0 + 600 and st.stage_elapsed() is None


def test_stage_stats_feed_the_estimate():
    stats, key = StageStats(), ("checkout", "claude-code")
    for d in (100, 200, 300):
        stats.add(key, "edit", d)
    assert stats.typical(key, "edit") == (200, 3)
    assert stats.typical(("other", "claude-code"), "edit") == (200, 3)  # the harness's, when the env has none
    assert stats.typical(("other", "codex"), "edit") == (200, 3)  # any, when the harness has none
    assert stats.typical(key, "test") is None
    stats.add(key, "pr", 0.5)  # a sub-second stage is a mis-signal, not a sample
    assert stats.typical(key, "pr") is None
    st = LiveState("ses-1")
    st.observe(ev("file_change", "a.py", ts=1000.0, path="a.py"))

    class NoJobs:
        def where(self, **_):
            return []

    est = fleet_api.stage_estimate(st, "edit", "checkout", "claude-code", stats, fleet_api.Typical(NoJobs()), 1100.0)
    assert est == {"pct": 50, "elapsed_s": 100, "typical_s": 200, "basis": "median of 3 recent edit stages",
                   "samples": 3}
    est = fleet_api.stage_estimate(st, "edit", "checkout", "claude-code", StageStats(), fleet_api.Typical(NoJobs()),
                                   1100.0)
    assert est["basis"] == "default" and est["typical_s"] == 750 and est["pct"] == 13
    assert fleet_api.stage_estimate(st, "test", "checkout", "claude-code", stats, fleet_api.Typical(NoJobs()),
                                    1100.0) is None  # not the stage it is in
    far = fleet_api.stage_estimate(st, "edit", "checkout", "claude-code", stats, fleet_api.Typical(NoJobs()), 9e9)
    assert far["pct"] == 99  # never claims done


def test_stage_of_combines_the_job_phase_and_the_live_signal():
    job = Job(title="t", instructions="x", environment="e", state=JobState.RUNNING)
    live = LiveState("s")
    assert fleet_api.stage_of(job, None, live, None, False) is None  # running, nothing seen: no stage
    live.observe(ev("tool_call", "Read x", tool="Read"))
    assert fleet_api.stage_of(job, None, live, None, False) == "plan"
    job.state = JobState.VERIFYING
    assert fleet_api.stage_of(job, None, live, None, False) == "test"
    job.state = JobState.DELIVERING
    assert fleet_api.stage_of(job, None, live, None, False) == "pr"
    job.state = JobState.NEEDS_INPUT
    assert fleet_api.stage_of(job, None, None, "Bash: git push origin x", False) == "pr"
    ses = Session(name="s", environment="e", state=SessionState.IDLE)
    assert fleet_api.stage_of(None, ses, live, None, False) is None
    ses.state = SessionState.RUNNING
    assert fleet_api.stage_of(None, ses, live, None, False) == "plan"


# ====================================================================== the join
def _world(c: TestClient):
    """Three sandboxes: alice's job mid-edit, bob's session waiting on a tool approval, and a bare one of the
    owner's. Records go straight into the store and the services' memory, as the loops would put them."""
    h = c.app.state.hangar
    t = now()
    sb_a = Sandbox(name="refunds-1", environment="e", provider="fake", state=SandboxState.RUNNING,
                   created_by="job:job-aaaaaaaa", person="alice", created_at=t - 1800, expires_at=t + 1800)
    sb_b = Sandbox(name="bob-1", environment="e", provider="fake", state=SandboxState.RUNNING, person="bob",
                   created_at=t - 600)
    sb_o = Sandbox(name="bare", environment="e", provider="fake", state=SandboxState.RUNNING, created_by="ui:ops",
                   person="owner", created_at=t - 60)
    for sb in (sb_a, sb_b, sb_o):
        h.store.put(sb)
    ses_a = Session(name="job-aaaaaaaa", environment="e", person="alice", state=SessionState.RUNNING,
                    sandbox_id=sb_a.id, job_id="job-aaaaaaaa", harness="claude-code",
                    turns=[SessionTurn(message="Fix the refunds export", run_id="run-1", live=True,
                                       started_at=t - 1700)])
    job_a = Job(id="job-aaaaaaaa", title="Fix the refunds export", instructions="Fix it", environment="e",
                repo="https://github.com/o/r.git", state=JobState.RUNNING, person="alice", requester="ui:alice",
                sandbox_id=sb_a.id, session_id=ses_a.id, harness="claude-code", model="claude-opus-5-5",
                events=[JobEvent(state="running", message="agent turn 1 running")])
    turn_b = SessionTurn(message="Ship it", run_id="run-2", live=True, started_at=t - 500,
                         approvals=[Approval(id="apr-1", tool="Bash", detail="git push origin main",
                                             requested_at=t - 100, expires_at=t + 3000)])
    ses_b = Session(name="bob-ses", title="Ship the thing", environment="e", person="bob", owner="ui:bob",
                    state=SessionState.RUNNING, sandbox_id=sb_b.id, harness="codex", turns=[turn_b])
    h.jobs._jobs[job_a.id] = job_a
    h.sessions._sessions.update({ses_a.id: ses_a, ses_b.id: ses_b})
    live = h.sessions.activity.live
    live.observe(ses_a, 1, ses_a.turns[0], [ev("tool_call", "Read src/refunds.py", ts=t - 1600, tool="Read"),
                                            ev("file_change", "src/refunds.py", ts=t - 900, path="src/refunds.py"),
                                            ev("command", "git commit -am wip", ts=t - 800),
                                            ev("usage", "u", ts=t - 800, input_tokens=1000, output_tokens=100),
                                            ev("command", "pytest -q tests", ts=t - 300)])
    live.observe(ses_b, 1, turn_b, [ev("file_change", "x.py", ts=t - 400, path="x.py"),
                                    ev("approval_request", "Bash: git push origin main", ts=t - 100, id="apr-1",
                                       tool="Bash", status="pending")])
    return h, {"alice": sb_a, "bob": sb_b, "owner": sb_o}, job_a, ses_b


@pytest.fixture
def client(settings):
    from hangar.app import create_app

    settings.admin_token, settings.public_url = "t0ken", "http://localhost:8787"
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        yield c


def test_the_join_one_unit_per_sandbox_with_the_live_picture(client):
    h, sbs, job_a, ses_b = _world(client)
    r = client.get("/api/fleet", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"at", "person", "units", "counts", "owners", "inbox", "slots"} <= set(body)
    units = {u["name"]: u for u in body["units"]}
    assert set(units) == {"refunds-1", "bob-1", "bare"}
    assert [u["name"] for u in body["units"]][0] == "bob-1"  # attention first
    a = units["refunds-1"]
    assert a["id"] == sbs["alice"].id and a["tail"] == sbs["alice"].id[-4:] and a["job"] == job_a.id
    assert a["kind"] == "job" and a["owner"] == "alice" and a["role"] == "running" and a["label"] == "Running"
    assert a["work"] == "Fix the refunds export" and a["harness"] == "claude-code" and a["model"] == "claude-opus-5-5"
    assert a["now"] == "running pytest -q tests" and a["last_activity_at"] == pytest.approx(now() - 300, abs=5)
    assert a["stage"] == "test" and a["stage_pct"] is None
    assert a["stage_estimate"]["basis"] == "default" and a["stage_estimate"]["elapsed_s"] == pytest.approx(300, abs=5)
    assert [s["stage"] for s in a["stages"]] == ["plan", "edit", "test"] and a["stages"][-1]["ended_at"] is None
    assert a["files_changed"] == 1 and a["commits"] == 1
    assert a["usage"] is None  # the session's turns carry no usage yet (the watcher sets it)
    assert a["session"]["name"] == "job-aaaaaaaa" and a["inbox"] == [] and a["expires_at"] == sbs["alice"].expires_at
    b = units["bob-1"]
    assert b["kind"] == "session" and b["owner"] == "bob" and b["role"] == "approval" and b["label"] == "Needs approval"
    assert b["ask"] == {"kind": "tool", "approval": "apr-1", "job": None, "session": ses_b.id}
    assert b["reason"] == {"title": "Held", "text": "Bash: git push origin main"}
    assert b["now"] == "waiting on you: Bash" and b["stage"] == "edit" and b["work"] == "Ship the thing"
    assert b["ask_expires_at"] == pytest.approx(now() + 3000, abs=5)
    # the inbox is the caller's own (plus what nobody owns, for an admin): bob's approval is bob's to decide
    assert b["inbox"] == [] and body["inbox"] == {"counts": {}, "items": []}
    o = units["bare"]
    assert o["kind"] == "sandbox" and o["owner"] == "owner" and o["role"] == "running" and o["stage"] is None
    assert o["now"] is None and o["stages"] == [] and o["work"] == "bare"
    assert body["counts"] == {"approval": 1, "failed": 0, "denied": 0, "running": 2, "queued": 0, "off": 0}
    assert body["slots"] == {"used": 3, "max": h.settings.max_sandboxes}
    assert [(x["id"], x["count"]) for x in body["owners"]] == [("alice", 1), ("bob", 1), ("owner", 1)]


def test_owner_filter_and_stopped_sandboxes(client):
    h, sbs, _, _ = _world(client)
    assert [u["owner"] for u in client.get("/api/fleet?owner=bob", headers=ADMIN).json()["units"]] == ["bob"]
    assert client.get("/api/fleet?owner=nobody", headers=ADMIN).json()["units"] == []
    assert client.get("/api/fleet?owner=", headers=ADMIN).status_code == 400
    sb = Sandbox(name="old", environment="e", provider="fake", state=SandboxState.STOPPED, person="owner")
    h.store.put(sb)
    assert "old" not in {u["name"] for u in client.get("/api/fleet", headers=ADMIN).json()["units"]}
    got = {u["name"]: u for u in client.get("/api/fleet?include_stopped=true", headers=ADMIN).json()["units"]}
    assert got["old"]["role"] == "off" and got["old"]["label"] == "Spun down" and got["old"]["live"] is False


def test_the_job_phases_and_the_agents_own_progress(client):
    h, sbs, job_a, _ = _world(client)
    ses = h.sessions.get(job_a.session_id)
    t = now()
    live = h.sessions.activity.live
    live.observe(ses, 1, ses.turns[0], [ev("progress", "half way", ts=t - 10, pct=50)])
    u = client.get("/api/fleet", headers=ADMIN).json()["units"]
    a = next(x for x in u if x["job"] == job_a.id)
    assert a["stage_pct"] == 50 and a["stage_estimate"] is None and a["now"] == "half way"
    # hangar takes over: verifying is the test stage, delivering the PR, whatever the agent last said
    job_a.state = JobState.VERIFYING
    job_a.events.append(JobEvent(state="verifying", message="running the check: pytest -q", ts=t - 5))
    ses.turns[0].finished_at = t - 6
    a = next(x for x in client.get("/api/fleet", headers=ADMIN).json()["units"] if x["job"] == job_a.id)
    assert a["stage"] == "test" and a["now"] == "running the check: pytest -q" and a["stage_pct"] is None
    assert a["stages"][-1] == {"stage": "test", "started_at": t - 5, "ended_at": None}
    job_a.state = JobState.FAILED
    job_a.result.verify_exit_code = 1
    a = next(x for x in client.get("/api/fleet", headers=ADMIN).json()["units"] if x["job"] == job_a.id)
    assert a["role"] == "failed" and a["reason"] == {"title": "Failed", "text": "The check failed (exit 1)."}
    assert a["stage"] is None and a["stages"][-1]["ended_at"] == job_a.updated_at


def test_visibility_is_the_jobs_lists(client):
    """A member sees their own units (and their own inbox); an admin everything; a service token nothing."""
    h, sbs, job_a, _ = _world(client)
    alice = client.post("/api/people", json={"id": "alice"}, headers=ADMIN).json()["login"]["token"]
    bob = client.post("/api/people", json={"id": "bob"}, headers=ADMIN).json()["login"]["token"]
    got = client.get("/api/fleet", headers={"Authorization": f"Bearer {alice}"}).json()
    assert [u["name"] for u in got["units"]] == ["refunds-1"] and got["person"] == "alice"
    assert got["inbox"]["items"] == [] and got["owners"] == [{"id": "alice", "name": "alice", "count": 1}]
    got = client.get("/api/fleet", headers={"Authorization": f"Bearer {bob}"}).json()
    assert [u["name"] for u in got["units"]] == ["bob-1"] and got["inbox"]["counts"] == {"approval": 1}
    item = got["inbox"]["items"][0]
    assert got["units"][0]["inbox"] == [item["id"]] and item["kind"] == "approval" and item["session"] == "bob-ses"
    assert client.get("/api/fleet?owner=alice", headers={"Authorization": f"Bearer {bob}"}).json()["units"] == []
    svc = client.post("/api/tokens", json={"name": "ci", "scopes": ["jobs"]}, headers=ADMIN).json()["token"]
    assert client.get("/api/fleet", headers={"Authorization": f"Bearer {svc}"}).status_code == 403
    assert client.get("/api/fleet").status_code == 401
    shadow = Job(title="shadow work", instructions="x", environment="e", state=JobState.RUNNING, source="shadow",
                 sandbox_id=sbs["owner"].id)  # --- #183: a shadow job is on nobody's fleet; its sandbox is bare
    h.jobs._jobs[shadow.id] = shadow
    bare = next(u for u in client.get("/api/fleet", headers=ADMIN).json()["units"] if u["name"] == "bare")
    assert bare["job"] is None and bare["work"] == "bare"


def test_text_is_redacted(client):
    h, sbs, job_a, _ = _world(client)
    h.broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_sekrit12345678901234567890")
    ses = h.sessions.get(job_a.session_id)
    ses.needs_input = None
    job_a.title = "Use ghp_sekrit12345678901234567890 to push"
    h.sessions.activity.live.observe(ses, 1, ses.turns[0],
                                     [ev("command", "curl -H 'Authorization: ghp_sekrit12345678901234567890' x")])
    a = next(x for x in client.get("/api/fleet", headers=ADMIN).json()["units"] if x["job"] == job_a.id)
    assert "sekrit" not in a["work"] and "sekrit" not in a["now"]


def test_two_hundred_sandboxes_read_in_under_fifty_ms(client):
    """The list path is one in-memory pass: p95 under 50 ms for 200 sandboxes, each with a job, a session and a
    live turn (the budget the Decision deck's 2 s refresh and the stream's coalescing assume)."""
    h = client.app.state.hangar
    t = now()
    live = h.sessions.activity.live
    for i in range(200):
        person = f"p{i % 7}"
        sb = Sandbox(name=f"sb-{i}", environment="e", provider="fake", state=SandboxState.RUNNING, person=person,
                     created_at=t - 60 * i)
        h.store.put(sb)
        job = Job(title=f"job {i}", instructions="x" * 2000, environment="e", state=JobState.RUNNING,
                  person=person, requester=f"ui:{person}", sandbox_id=sb.id, harness="claude-code",
                  events=[JobEvent(state="running", message="agent turn 1 running")])
        turn = SessionTurn(message="m", run_id=f"run-{i}", live=True, started_at=t - 60 * i)
        ses = Session(name=f"job-{i}", environment="e", person=person, state=SessionState.RUNNING,
                      sandbox_id=sb.id, job_id=job.id, harness="claude-code", turns=[turn])
        job.session_id = ses.id
        h.jobs._jobs[job.id] = job
        h.sessions._sessions[ses.id] = ses
        evs = [ev("tool_call", f"Read f{k}.py", ts=t - 50 * i + k, tool="Read") for k in range(5)]
        evs += [ev("file_change", f"f{k}.py", ts=t - 40 * i + k, path=f"f{k}.py") for k in range(20)]
        evs += [ev("command", "pytest -q", ts=t - 30), ev("usage", "u", input_tokens=10000, output_tokens=2000)]
        live.observe(ses, 1, turn, evs)
    reader = h.fleet_read
    from hangar import identity
    from hangar.auth import Principal

    tok = identity.current.set(Principal("ops", "tok", ["admin"], person="owner", role="admin"))
    try:
        assert len(reader.units(identity.principal())["units"]) == 200
        times = []
        for _ in range(30):
            t0 = time.perf_counter()
            reader.units(identity.principal())
            times.append((time.perf_counter() - t0) * 1000)
    finally:
        identity.current.reset(tok)  # never leak the principal into the next test
    times.sort()
    p50, p95 = statistics.median(times), times[int(len(times) * 0.95) - 1]
    print(f"\nGET /api/fleet x200: p50 {p50:.1f} ms, p95 {p95:.1f} ms")
    assert p95 < 50, f"p95 {p95:.1f} ms"
    body = client.get("/api/fleet", headers=ADMIN)
    assert body.status_code == 200 and len(body.json()["units"]) == 200


# ====================================================================== S2: tokens over the last hour
def test_tokens_1h_buckets_the_increase_per_five_minutes():
    st, base = LiveState("ses-1"), 1_000_000 * livestate.TOKEN_BUCKET_S  # a bucket boundary
    assert st.tokens_1h(base) == [0] * 12  # nothing counted (as after a restart)
    # running totals within a turn: the bucket gets each increase; cache reads are not new tokens
    st.observe(ev("usage", "u", ts=base + 10, input_tokens=1000, output_tokens=100, cache_read_tokens=50000))
    st.observe(ev("usage", "u", ts=base + 20, input_tokens=1500, output_tokens=200, cache_write_tokens=300))
    assert st.tokens_1h(base + 30) == [0] * 11 + [2000]
    st.observe(ev("usage", "u", ts=base + 30, input_tokens=1400, output_tokens=200, final=True))  # lower: nothing
    st.observe(ev("usage", "u", ts=base + 400, input_tokens=3000, output_tokens=200, cache_write_tokens=300))
    assert st.tokens_1h(base + 400) == [0] * 10 + [2000, 1500]
    # a new turn starts its totals over; the hour's buckets carry on
    st.reset(2, "run-2", base + 500)
    st.observe(ev("usage", "u", ts=base + 610, input_tokens=700, output_tokens=0))
    assert st.tokens_1h(base + 610) == [0] * 9 + [2000, 1500, 700]
    # an hour on, the old buckets have scrolled out
    assert st.tokens_1h(base + 3600) == [1500, 700] + [0] * 10
    assert st.tokens_1h(base + 3 * 3600) == [0] * 12
    for k in range(20):  # never more than twelve buckets held
        st.observe(ev("usage", "u", ts=base + 3600 * 5 + 300 * k, input_tokens=1000 * (k + 1)))
    assert len(st.token_buckets) == 12 and st.view()["tokens_1h"] is not None


def test_tokens_1h_on_the_fleet(client):
    h, sbs, job_a, _ = _world(client)
    a = next(x for x in client.get("/api/fleet", headers=ADMIN).json()["units"] if x["job"] == job_a.id)
    assert len(a["tokens_1h"]) == 12 and sum(a["tokens_1h"]) == 1100  # the usage event _world recorded
    bare = next(x for x in client.get("/api/fleet", headers=ADMIN).json()["units"] if x["name"] == "bare")
    assert bare["tokens_1h"] == [0] * 12
