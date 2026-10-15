"""The sister tool's interfaces (--- interfaces; spec 0014): the effective harness manifest, trace export and
experiment jobs. On the fake provider with a scripted agent that speaks Claude Code's stream-json, real git and tmux.

Each interface: its schema, redaction (a stored secret is seeded and must never appear in any export), owner scoping,
and for experiments a 2 variant x 2 trial run with an external grader."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import jsonschema
import pytest

from hangar import experiments, manifest, traces
from hangar.events import Activity, RunEvent
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, JobService, SubmitJob
from hangar.models import Approval, CredentialKind, Job, JobState, Session, SessionTurn, Steer, Usage
from hangar.sessions import SessionService
from hangar.vault import Vault
from tests.conftest import ENVS, scratch_root
from tests.test_jobs import Hooks, _until

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

SECRET = "hgsecret-SEEDED-7f3a9c21d4e8b6"  # stored in the vault; must never leave in any export
ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs/api/manifest.schema.json").read_text())
INDEX_SCHEMA = json.loads((ROOT / "docs/api/experiment.schema.json").read_text())
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]

# A scripted agent: Claude Code stream-json on stdout (tool calls of every kind, a subagent, a file edit), the
# skills and instructions it sees written into the repo, and the seeded secret in a command, its output and a message.
M = "claude-sonnet-5-5"


def _use(tid: str, name: str, **inp) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _res(tid: str, content: str, err: bool = False) -> dict:
    return {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": err}


BEFORE = [
    {"type": "system", "subtype": "init", "model": M, "session_id": "conv-1"},
    {"type": "assistant", "message": {"id": "m1", "model": M, "content": [
        {"type": "text", "text": f"Looking around; the token is {SECRET}"},
        _use("t1", "Bash", command=f"echo {SECRET}")],
        "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50}}},
    {"type": "user", "message": {"content": [_res("t1", SECRET)]}},
    {"type": "assistant", "message": {"id": "m2", "model": M, "content": [
        _use("t2", "Read", file_path="README.md"), _use("t3", "Grep", pattern="hello"),
        _use("t4", "mcp__github__get_issue", number=1),
        _use("t5", "Task", description="review the change", prompt="x", subagent_type="code-reviewer")],
        "usage": {"input_tokens": 10, "output_tokens": 10}}},
    {"type": "user", "message": {"content": [_res("t2", "hello"), _res("t3", "README.md:1:hello"),
                                             _res("t4", "denied", err=True), _res("t5", "looks fine")]}},
]
AFTER = [
    {"type": "assistant", "message": {"id": "m3", "model": M, "content": [
        _use("t6", "Write", file_path="seen.txt", content="x")], "usage": {"input_tokens": 5, "output_tokens": 5}}},
    {"type": "user", "message": {"content": [_res("t6", "ok")]},
     "tool_use_result": {"type": "create", "filePath": "seen.txt"}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.0123,
     "usage": {"input_tokens": 115, "output_tokens": 35, "cache_read_input_tokens": 50}, "modelUsage": {M: {}}},
]
AGENT = r"""#!/usr/bin/env bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cat "$STREAM.before"
S="$HOME/.tagent"
{ ls "$S/skills" 2>/dev/null | sort | tr '\n' ' '; echo; cat "$S/AGENTS.md" 2>/dev/null; } > seen.txt
git add seen.txt && git commit -qm "agent: what I saw"
cat "$STREAM.after"
"""


@pytest.fixture
def world(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="ifc-", dir=scratch_root()))
    remote, seed = root / "remote.git", root / "seed"
    subprocess.run([*GIT, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*GIT, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    (seed / "AGENTS.md").write_text("Run the tests before you finish.\n")
    subprocess.run([*GIT, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*GIT, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    agent = root / "agent.sh"
    agent.write_text(AGENT)
    for part, lines in (("before", BEFORE), ("after", AFTER)):
        (root / f"stream.{part}").write_text("".join(json.dumps(x) + "\n" for x in lines))
    monkeypatch.setitem(HARNESSES, "tagent", Harness(
        name="tagent", title="Trace agent", run=f"STREAM={root}/stream bash {agent}", skills_dir=".tagent/skills",
        instructions_file=".tagent/AGENTS.md"))
    envs = root / "envs"
    for name in ("alpha", "beta"):
        (envs / "skills" / name).mkdir(parents=True)
        (envs / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\nUse {name}.\n")
    (envs / "tenv.yaml").write_text(json.dumps({
        "name": "tenv", "harnesses": ["tagent"], "skills": [{"path": "skills/alpha"}, {"path": "skills/beta"}],
        "agent_config": {"instructions": f"Base instructions. Never print {SECRET}."},
        "mcp": {"github": {"url": f"https://api.example/mcp?token={SECRET}", "headers": {"X-Key": SECRET}},
                "local": {"command": "npx", "args": ["some-mcp"], "env": {"API_KEY": SECRET}}}}))
    yield {"root": root, "remote": remote, "envs": envs, "url": f"file://{remote}"}
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def jobs(fleet, world, settings, store, broker, monkeypatch):
    settings.session_poll_seconds = 0.1
    settings.job_poll_seconds = 0.1
    settings.job_concurrency = 4
    broker.put("seeded", "webhook", CredentialKind.GENERIC, SECRET)
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    fake = fleet.providers["docker"]
    create = fake.create

    async def create_with_baked_skills(sandbox, image, spec=None):  # the image's skills, as a build bakes them
        await create(sandbox, image, spec)
        for name in ("alpha", "beta"):
            d = fake.boxes[sandbox.provider_ref] / "home/agent/.tagent/skills" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(f"baked {name}\n")

    monkeypatch.setattr(fake, "create", create_with_baked_skills)
    sessions = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    await sessions.start()
    svc = JobService(fleet, sessions, transport=httpx.MockTransport(Hooks()))
    await svc.start()
    yield svc
    await svc.stop()
    await sessions.stop()
    for box in fake.boxes.values():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


DONE = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


def _no_secret(obj) -> None:
    assert SECRET not in json.dumps(obj, default=str)


def _spans(export: dict) -> list[dict]:
    return [sp for rs in export["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]]


def _attrs(x: dict) -> dict:
    out = {}
    for a in x.get("attributes", []):
        (k, v), = a["value"].items()
        out[a["key"]] = v
    return out


def _check_otlp(export: dict) -> None:
    """OTLP/JSON as the spec says: hex ids, integer enums, 64-bit integers and timestamps as strings."""
    rs = export["resourceSpans"]
    assert len(rs) == 1 and _attrs(rs[0]["resource"])["hangar.trace.schema"] == "hangar.trace/v1"
    scope = rs[0]["scopeSpans"][0]
    assert scope["scope"] == {"name": "hangar.trace", "version": "1"}
    assert scope["schemaUrl"] == "https://opentelemetry.io/schemas/1.44.0"
    ids = {sp["spanId"] for sp in scope["spans"]}
    for sp in scope["spans"]:
        assert len(sp["traceId"]) == 32 and int(sp["traceId"], 16) >= 0
        assert len(sp["spanId"]) == 16 and int(sp["spanId"], 16) >= 0
        assert sp["kind"] == 1 and isinstance(sp["startTimeUnixNano"], str) and sp["startTimeUnixNano"].isdigit()
        assert int(sp["endTimeUnixNano"]) >= int(sp["startTimeUnixNano"])
        assert "parentSpanId" not in sp or sp["parentSpanId"] in ids
        assert _attrs(sp)["hangar.kind"] in ("session", "job", "turn", "tool.call", "subagent", "approval")
        for a in sp["attributes"]:
            (t, v), = a["value"].items()
            assert t in ("stringValue", "intValue", "doubleValue", "boolValue", "arrayValue")
            if t == "intValue":
                assert isinstance(v, str) and v.lstrip("-").isdigit()
        for ev in sp.get("events", []):
            assert ev["timeUnixNano"].isdigit() and ev["name"]
        if "status" in sp:
            assert sp["status"]["code"] == 2


# ====================================================================== a job end to end: manifest + trace
async def test_a_jobs_manifest_and_trace(jobs, world, fleet, settings):
    job = jobs.submit(SubmitJob(instructions=f"Look around. Context: {SECRET}", repo=world["url"],
                                environment="tenv", verify="test -f seen.txt", deliver="none"),
                      Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=40)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    s = jobs._session(job)
    for _ in range(100):  # recorded in the background as the turn starts
        if s.turns[0].manifest:
            break
        await asyncio.sleep(0.05)
    t = s.turns[0]
    assert t.manifest and t.manifest.startswith("sha256:")

    # --- the manifest: stored with the turn, valid against the checked-in schema, never a credential
    m = manifest.for_session(jobs.sessions, s, 1)
    jsonschema.validate(m, SCHEMA)
    assert set(m) <= set(SCHEMA["properties"]), set(m) - set(SCHEMA["properties"])  # the schema describes it all
    assert m["digest"] == t.manifest and m["subject"] == {**m["subject"], "kind": "turn", "turn": 1,
                                                          "run_id": t.run_id, "job": job.id}
    assert m["harness"]["name"] == "tagent" and m["environment"]["name"] == "tenv"
    names = {x["name"] for x in m["mcp_servers"]}
    assert names == {"github", "local"}
    gh = next(x for x in m["mcp_servers"] if x["name"] == "github")
    assert gh["url"] == "https://api.example/mcp" and gh["headers"] == ["X-Key"] and gh["transport"] == "http"
    assert next(x for x in m["mcp_servers"] if x["name"] == "local")["env"] == ["API_KEY"]
    assert {x["name"] for x in m["skills"]["declared"]} == {"alpha", "beta"}
    assert {x["name"] for x in m["skills"]["effective"]} == {"alpha", "beta"}  # what the sandbox had
    assert all(x["digest"].startswith("sha256:") for x in m["skills"]["declared"])
    repo_files = m["instructions"]["repo"]["files"]
    assert [f["path"] for f in repo_files] == ["AGENTS.md"] and repo_files[0]["bytes"] == 33
    assert m["instructions"]["repo"]["commit"] == s.base_sha
    assert m["instructions"]["global"]["declared"]["text"].startswith("Base instructions")
    assert m["policy"]["verify"] == "test -f seen.txt" and m["policy"]["deliver"] == "none"
    assert m["permissions"]["approvals"] == "auto" and m["permissions"]["harness_permission_checks"] == "off"
    _no_secret(m)
    assert "***" in m["instructions"]["global"]["declared"]["text"]
    # the same configuration gives the same config digest; the environment's prospective one agrees on it
    env_m = manifest.for_environment(fleet, "tenv")
    jsonschema.validate(env_m, SCHEMA)
    assert env_m["subject"]["kind"] == "environment" and env_m["config_digest"] != ""
    _no_secret(env_m)
    assert manifest.for_job(jobs, job, 1)["digest"] == m["digest"]
    assert manifest.for_job(jobs, job)["digest"] == m["digest"]

    # --- the trace: one run, the session, the job
    run = await traces.export_session(jobs.sessions, s, turn=1)
    _check_otlp(run)
    _no_secret(run)
    spans = _spans(run)
    turn = next(x for x in spans if _attrs(x)["hangar.kind"] == "turn")
    ta = _attrs(turn)
    assert turn["name"] == "invoke_agent tagent" and "parentSpanId" not in turn
    assert ta["gen_ai.operation.name"] == "invoke_agent" and ta["hangar.manifest.digest"] == t.manifest
    assert ta["gen_ai.usage.output_tokens"] == "35" and ta["gen_ai.usage.input_tokens"] == "165"  # cached included
    assert ta["gen_ai.usage.cache_read.input_tokens"] == "50" and ta["hangar.cost_usd"] == 0.0123
    tools = {_attrs(x)["gen_ai.tool.name"]: _attrs(x) for x in spans if _attrs(x)["hangar.kind"] == "tool.call"}
    assert {k: v["hangar.tool.kind"] for k, v in tools.items()} == {
        "Bash": "shell", "Read": "read", "Grep": "search", "mcp__github__get_issue": "mcp", "Write": "edit"}
    assert tools["Bash"]["hangar.tool.input"] == "echo ***"
    assert tools["mcp__github__get_issue"]["hangar.mcp.server"] == "github"
    assert tools["mcp__github__get_issue"]["gen_ai.tool.type"] == "extension"
    assert tools["mcp__github__get_issue"]["error.type"] == "tool_error"
    assert tools["Read"]["hangar.file.path"] == "README.md"
    assert all(x.get("parentSpanId") == turn["spanId"] for x in spans if x is not turn)
    sub = next(x for x in spans if _attrs(x)["hangar.kind"] == "subagent")
    assert sub["name"] == "invoke_agent code-reviewer" and _attrs(sub)["hangar.tool.output"] == "looks fine"
    events = {e["name"] for e in turn["events"]}
    assert {"message", "file.change", "progress"} <= events
    whole = await traces.export_session(jobs.sessions, s)
    _check_otlp(whole)
    root = next(x for x in _spans(whole) if _attrs(x)["hangar.kind"] == "session")
    assert root["name"] == "invoke_workflow hangar.session"
    assert {x["traceId"] for x in _spans(whole)} == {x["traceId"] for x in spans}  # ids are derived, stable
    jx = await traces.export_job(jobs, job)
    _check_otlp(jx)
    _no_secret(jx)
    jroot = next(x for x in _spans(jx) if _attrs(x)["hangar.kind"] == "job")
    names = [e["name"] for e in jroot["events"]]
    assert "verify" in names and "job.state" in names
    v = next(e for e in jroot["events"] if e["name"] == "verify")
    assert _attrs(v)["hangar.verify.passed"] is True and _attrs(v)["hangar.verify.phase"] == "after_turn"
    assert _attrs(jroot)["hangar.job.state"] == "succeeded"


def test_tool_kinds_are_the_same_for_every_harness():
    k = traces.tool_kind
    assert [k(x) for x in ("Bash", "shell", "bash", "run_command")] == ["shell"] * 4
    assert [k(x) for x in ("Edit", "apply_patch", "edit", "write_to_file")] == ["edit"] * 4
    assert [k(x) for x in ("Read", "read", "view_file")] == ["read"] * 3
    assert [k(x) for x in ("Grep", "Glob", "grep_search")] == ["search"] * 3
    assert [k(x) for x in ("WebFetch", "web_search", "search_web")] == ["web"] * 3
    assert [k(x) for x in ("mcp__github__get_issue", "github.get_issue")] == ["mcp"] * 2
    assert [k(x) for x in ("Skill", "TodoWrite", "Mystery")] == ["skill", "todo", "other"]


def test_approvals_steers_and_redaction_without_a_sandbox(settings, broker):
    """Approvals are spans from the request to the answer; steers, interrupts and the in-turn verify gate are
    events; a stored secret in any of them never leaves."""
    broker.put("seeded", "webhook", CredentialKind.GENERIC, SECRET)
    activity = Activity(settings, broker.redact)
    svc = SimpleNamespace(activity=activity, fleet=SimpleNamespace(broker=broker))
    t = SessionTurn(message="go", run_id="run-1", harness="claude-code", started_at=1000.0, finished_at=1100.0,
                    outcome="exited", exit_code=0, live=True, approval_policy="ask", conversation="c-1",
                    usage=Usage(input_tokens=1, output_tokens=2, cost_usd=0.5, cost_source="prices"),
                    approvals=[Approval(id="apr-1", tool="Bash", detail=f"curl -H {SECRET}", requested_at=1010.0,
                                        status="denied", by="alice", decided_at=1020.0)],
                    steers=[Steer(text=f"use {SECRET}", requester="alice", ts=1030.0, status="delivered"),
                            Steer(text="stop", kind="interrupt", requester="bob", ts=1040.0, status="queued")])
    s = Session(name="s1", environment="plain", harness="claude-code", model="claude-opus-5-5", turns=[t],
                person="alice")
    activity.append(s.id, [
        RunEvent(turn=1, run_id="run-1", kind="command", text=f"echo {SECRET}", data={"id": "c1", "tool": "Bash"},
                 ts=1001.0),
        RunEvent(turn=1, run_id="run-1", kind="tool_result", text="ok", data={"id": "c1", "ok": True}, ts=1002.0),
        RunEvent(turn=1, run_id="run-1", kind="tool_call", text="WebFetch https://x", ts=1003.0,
                 data={"id": "w1", "tool": "WebFetch"}),  # never answered: the turn ended first
        RunEvent(turn=1, run_id="run-1", kind="progress", text="the job's check failed", ts=1050.0,
                 data={"source": "hangar", "action": "verify", "passed": False, "exit_code": 1, "round": 1}),
        RunEvent(turn=1, run_id="run-1", kind="error", text="boom", data={"fatal": False}, ts=1060.0)])
    x = traces.session_export(svc, s, turn=1)
    _check_otlp(x)
    _no_secret(x)
    spans = _spans(x)
    apr = next(sp for sp in spans if _attrs(sp)["hangar.kind"] == "approval")
    assert _attrs(apr)["hangar.approval.status"] == "denied" and _attrs(apr)["hangar.approval.by"] == "alice"
    assert int(apr["endTimeUnixNano"]) - int(apr["startTimeUnixNano"]) == 10 * 10**9
    web = next(sp for sp in spans if _attrs(sp).get("gen_ai.tool.name") == "WebFetch")
    assert _attrs(web)["hangar.tool.incomplete"] is True and _attrs(web)["hangar.tool.kind"] == "web"
    assert web["endTimeUnixNano"] == str(int(1100.0 * 1e9))
    turn = next(sp for sp in spans if _attrs(sp)["hangar.kind"] == "turn")
    evs = [(e["name"], _attrs(e)) for e in turn["events"]]
    assert [n for n, _ in evs] == ["steer", "interrupt", "verify", "error"]
    assert evs[0][1]["hangar.text"] == "use ***" and evs[1][1]["hangar.requester"] == "bob"
    assert evs[2][1]["hangar.verify.passed"] is False and evs[2][1]["hangar.verify.phase"] == "in_turn"
    assert _attrs(turn)["gen_ai.provider.name"] == "anthropic" and _attrs(turn)["hangar.approvals"] == "ask"


def test_an_export_stops_at_its_limit(settings, broker):
    x = traces.Export(traces.trace_id("s"), broker.redact, limit=3)
    sp = x.span("a", "a", 1.0, 2.0, {"hangar.kind": "turn"})
    for i in range(5):
        x.event(sp, "message", 1.5, {"hangar.text": str(i)})
    out = x.request({})
    assert _attrs(out["resourceSpans"][0]["resource"])["hangar.export.truncated"] is True
    assert len(_spans(out)[0]["events"]) == 2


def test_live_export_adds_the_tool_spans_under_the_turn(settings, broker):
    """HANGAR_TRACE_LIVE_TOOLS: observe's tracer gets the turn's tool spans, as children of its invoke_agent span."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from hangar import observe

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    old = observe._tracer
    observe._tracer = provider.get_tracer("hangar")
    try:
        broker.put("seeded", "webhook", CredentialKind.GENERIC, SECRET)
        activity = Activity(settings, broker.redact)
        svc = SimpleNamespace(activity=activity, fleet=SimpleNamespace(broker=broker))
        t = SessionTurn(message="go", run_id="run-9", harness="codex", started_at=10.0, finished_at=20.0)
        s = Session(name="s9", environment="plain", harness="codex", turns=[t])
        activity.append(s.id, [
            RunEvent(turn=1, run_id="run-9", kind="command", text=f"cat {SECRET}", data={"id": "i1", "tool": "shell"},
                     ts=11.0),
            RunEvent(turn=1, run_id="run-9", kind="tool_result", text="x", data={"id": "i1", "ok": False,
                                                                                  "exit_code": 2}, ts=12.0)])
        parent = observe._tracer.start_span("invoke_agent codex")
        traces.live_children(parent, svc, s, t)
        parent.end()
    finally:
        observe._tracer = old
    got = {sp.name: sp for sp in exp.get_finished_spans()}
    child = got["execute_tool shell"]
    assert child.parent.span_id == got["invoke_agent codex"].context.span_id
    assert child.attributes["hangar.tool.kind"] == "shell" and child.attributes["process.exit.code"] == 2
    assert child.attributes["hangar.tool.input"] == "cat ***"
    assert child.status.status_code.name == "ERROR" and child.start_time == 11 * 10**9


# ====================================================================== owner scoping over REST
def test_manifests_and_traces_are_owner_scoped(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.env_dirs = [ENVS]
    admin = {"Authorization": "Bearer t0ken"}
    with TestClient(create_app(settings)) as c:
        h = c.app.state.hangar
        h.broker.put("seeded", "webhook", CredentialKind.GENERIC, SECRET)
        login = c.post("/api/people", json={"id": "alice"}, headers=admin).json()["login"]
        alice = {"Authorization": f"Bearer {login['token']}"}
        mine = Session(name="alices", environment="plain", person="alice", title=SECRET)
        theirs = Session(name="bobs", environment="plain", person="bob")
        h.store.put(mine)
        h.store.put(theirs)
        for path in ("manifest", "trace"):
            assert c.get(f"/api/sessions/alices/{path}", headers=alice).status_code == 200
            r = c.get(f"/api/sessions/bobs/{path}", headers=alice)
            assert r.status_code == 404 and "no session" in r.json()["error"]  # as if it didn't exist
            assert c.get(f"/api/sessions/bobs/{path}", headers=admin).status_code == 200
        m = c.get("/api/sessions/alices/manifest", headers=alice).json()
        jsonschema.validate(m, SCHEMA)
        assert m["subject"]["kind"] == "session" and m["subject"]["person"] == "alice"
        assert c.get("/api/environments/plain/manifest", headers=alice).status_code == 200
        # jobs: a jobs token sees only the jobs it submitted
        tok = c.post("/api/tokens", json={"name": "svc", "scopes": ["jobs"]}, headers=admin).json()
        other = Job(title="t", instructions="x", environment="plain", owner="tok-someone-else")
        own = Job(title="t", instructions="x", environment="plain", owner=tok["id"])
        h.store.put(other)
        h.store.put(own)
        svc = {"Authorization": f"Bearer {tok['token']}"}
        assert c.get(f"/api/jobs/{other.id}/trace", headers=svc).status_code == 404
        assert c.get(f"/api/jobs/{other.id}/trace", headers=admin).status_code == 200
        r = c.get(f"/api/jobs/{own.id}/trace", headers=svc)
        assert r.status_code == 200 and r.json()["resourceSpans"]
        assert c.get(f"/api/jobs/{own.id}/manifest", headers=svc).status_code == 404  # not started: no turn yet
        assert c.get(f"/api/experiments/{own.id}", headers=svc).status_code == 404  # a job, not an experiment


# ====================================================================== experiments
def test_a_trial_mounts_only_its_harness_and_never_a_warm_sandbox(fleet):
    from hangar.models import Sandbox

    spec = fleet.catalog.get("agents")
    spec.packs = {"claude-code": "pack-cc", "codex": "pack-cx", "pi": "pack-pi"}
    spec.warm_pool = 2
    sb = Sandbox(name="x", environment="agents", provider="docker", labels={experiments.PACKS_LABEL: "selected"},
                 info={"harness": "codex"})
    experiments.select_packs(spec, sb, "codex")
    assert spec.packs == {"codex": "pack-cx"}
    p = experiments.provider_spec(spec, sb)
    assert p.name == "agents-pcodex" and p.warm_pool == 0 and p.packs == {"codex": "pack-cx"}
    plain = Sandbox(name="y", environment="agents", provider="docker")
    assert experiments.provider_spec(spec, plain) is spec  # everything else is as before


def test_an_experiment_spec_is_checked_before_it_runs(fleet, settings, tmp_path):
    from hangar.jobs import JobError

    (tmp_path / "egressy.yaml").write_text("name: egressy\nharnesses: [codex]\negress: {mode: none}\n")
    fleet.catalog.dirs.append(tmp_path)
    fleet.catalog.reload()
    js = SimpleNamespace(fleet=fleet, settings=settings)
    base = {"name": "e", "environment": "agents", "tasks": [{"id": "t1", "instructions": "x", "repo": "r"}]}
    with pytest.raises(JobError, match="isn't what hangar runs"):
        experiments.submit(js, experiments.ExperimentSpec.model_validate(  # type: ignore[arg-type]
            {**base, "variants": {"a": {"harness": "codex@0.0.1"}}}), None)
    with pytest.raises(JobError, match="not enabled"):
        experiments.submit(js, experiments.ExperimentSpec.model_validate(  # type: ignore[arg-type]
            {**base, "variants": {"a": {"harness": "antigravity"}}}), None)
    with pytest.raises(JobError, match="egress is fixed per experiment"):
        experiments.submit(js, experiments.ExperimentSpec.model_validate(  # type: ignore[arg-type]
            {**base, "variants": {"a": {}, "b": {"environment": "egressy"}}}), None)
    settings.experiment_max_trials = 3
    with pytest.raises(JobError, match="HANGAR_EXPERIMENT_MAX_TRIALS"):
        experiments.submit(js, experiments.ExperimentSpec.model_validate(  # type: ignore[arg-type]
            {**base, "variants": {"a": {}, "b": {}}, "trials": 2}), None)
    with pytest.raises(ValueError, match="`instructions` or an `issue`"):
        experiments.Task.model_validate({"id": "t"})
    with pytest.raises(ValueError):
        experiments.Variant.model_validate({"approvals": "ask"})  # nobody answers an experiment's agents


async def test_an_experiment_of_two_variants_and_two_trials(jobs, world, fleet, settings):
    spec = experiments.ExperimentSpec.model_validate({
        "name": "skills-ab", "environment": "tenv",
        "tasks": [{"id": "look", "instructions": f"Look around ({SECRET}).", "repo": world["url"],
                   "verify": "test -f seen.txt"}],
        "variants": {"with-alpha": {"skills": [{"path": "skills/alpha"}],
                                    "instructions_append": "Prefer small diffs."},
                     "none": {"skills": [], "instructions": "Only these instructions."}},
        "trials": 2, "parallel": 4, "keep_minutes": 5,
        "grader": {"command": "cat seen.txt hidden_test.txt; test -f hidden_test.txt && "
                              "printf '{\"score\": 1, \"trial\": \"%s\"}' \"$HANGAR_TRIAL\" > \"$HANGAR_GRADE_OUT\"",
                   "files": {"hidden_test.txt": "the agent never saw this\n"}, "timeout_seconds": 60}})
    exp = experiments.submit(jobs, spec, Caller("alice", "tok-a", person="alice"))
    assert experiments.is_experiment(exp) and exp.deliver == "none"
    exp = await _until(jobs, exp.id, DONE, timeout=120)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    v = experiments.view(jobs, exp)
    jsonschema.validate(v, INDEX_SCHEMA)
    _no_secret(v)
    trials = v["trials"]
    assert [t["key"] for t in trials] == ["look/with-alpha/1", "look/none/1", "look/with-alpha/2", "look/none/2"]
    assert v["counts"] == {"done": 4}
    pin = v["pins"]["variants"]
    assert pin["with-alpha"]["skills"][0]["name"] == "alpha" and pin["none"]["skills"] == []
    children = [jobs.get(t["job"]) for t in trials]
    assert {c.source for c in children} == {"experiment"} and {c.person for c in children} == {"alice"}
    assert {c.deliver for c in children} == {"none"} and {c.verify_in_loop for c in children} == {False}
    # isolation: each variant's agent saw exactly its skills and its instructions
    for t, c in zip(trials, children, strict=True):
        assert t["outcome"] == "succeeded" and t["verify"]["passed"] is True, t
        box = fleet.providers["docker"].boxes[c.sandbox_id]
        seen = (box / "workspace" / "remote" / "seen.txt").read_text().splitlines()
        if t["variant"] == "with-alpha":
            assert seen[0].strip() == "alpha"
            assert "Base instructions" in seen[1] and seen[-1] == "Prefer small diffs."
        else:
            assert seen[0].strip() == "" and seen[1:] == ["Only these instructions."]
        assert c.labels[experiments.PACKS_LABEL] == "selected"
    # pinned: one base commit for every trial
    assert len({jobs._session(c).base_sha for c in children}) == 1
    # per trial: the manifest (one config per variant, different between them), the trace, the grade
    by_variant: dict[str, set] = {}
    for t in trials:
        assert t["stored"] == ["trace.json", "manifest.json"] and not t["drift"]
        by_variant.setdefault(t["variant"], set()).add(t["config_digest"])
        m = experiments.trial_file(jobs, exp, t["key"], "manifest.json")
        jsonschema.validate(m, SCHEMA)
        assert m["digest"] == t["manifests"][0] and m["subject"]["job"] == t["job"]
        _no_secret(m)
        tr = experiments.trial_file(jobs, exp, t["key"], "trace.json")
        _check_otlp(tr)
        _no_secret(tr)
        g = t["grade"]
        assert g["exit_code"] == 0 and g["data"] == {"score": 1, "trial": t["key"]}, g
        assert g["applied"] is True and "the agent never saw this" in g["tail"]
    assert all(len(d) == 1 for d in by_variant.values()) and len(set().union(*by_variant.values())) == 2
    # the grader ran outside the agent's sandbox, and its sandbox is gone
    graders = [sb for sb in fleet.list(include_stopped=True) if sb.labels.get(experiments.GRADER_LABEL) == exp.id]
    assert len(graders) == 4 and all(sb.id not in {c.sandbox_id for c in children} for sb in graders)
    assert all(sb.state.value in ("stopped", "error") for sb in graders)
    # no follow-ups on an experiment
    from hangar.jobs import JobError

    with pytest.raises(JobError, match="takes no follow-ups"):
        jobs.follow_up(exp.id, "again", "alice")


async def test_cancelling_an_experiment_stops_its_trials(jobs, world, fleet, settings, monkeypatch):
    slow = Path(world["root"]) / "slow.sh"
    slow.write_text("#!/usr/bin/env bash\nsleep 30\n")
    monkeypatch.setitem(HARNESSES, "tagent", HARNESSES["tagent"].model_copy(update={"run": f"bash {slow}"}))
    spec = experiments.ExperimentSpec.model_validate({
        "name": "stop-me", "environment": "tenv", "parallel": 1, "trials": 2,
        "tasks": [{"id": "t", "instructions": "wait", "repo": world["url"]}], "variants": {"a": {}}})
    exp = experiments.submit(jobs, spec, Caller("svc", "svc"))
    for _ in range(200):
        st = experiments.load(jobs.get(exp.id))
        if st.trials and st.trials[0].job and jobs.get(st.trials[0].job).state == JobState.RUNNING:
            break
        await asyncio.sleep(0.05)
    jobs.cancel(exp.id, "svc")
    exp = await _until(jobs, exp.id, DONE, timeout=30)
    assert exp.state == JobState.CANCELLED
    st = experiments.load(exp)
    assert [t.state for t in st.trials] == ["cancelled", "cancelled"]
    first = jobs.get(st.trials[0].job)
    await _until(jobs, first.id, DONE, timeout=30)
    assert st.trials[1].job is None  # never started
