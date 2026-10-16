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
    from hangar import egress

    async def proxy_url(fleet_, prov, sb):  # --- sealed (#126): a grader is always proxied; the fake has no network
        return "http://127.0.0.1:9"

    monkeypatch.setattr(egress, "_proxy_url", proxy_url)
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
    assert job.result.files == ["seen.txt"]  # review(#114): the change's files, repo-relative
    assert _attrs(jroot)["hangar.diff.files"] == {"values": [{"stringValue": "seen.txt"}]}
    change = next(e for sp in _spans(jx) for e in sp.get("events", []) if e["name"] == "file.change")
    assert _attrs(change)["hangar.file.repo_path"] == "seen.txt" and _attrs(change)["hangar.file.in_repo"] is True


def test_tool_kinds_are_the_same_for_every_harness():
    k = traces.tool_kind
    assert [k(x) for x in ("Bash", "shell", "bash", "run_command")] == ["shell"] * 4
    assert [k(x) for x in ("Edit", "apply_patch", "edit", "write_to_file")] == ["edit"] * 4
    assert [k(x) for x in ("Read", "read", "view_file")] == ["read"] * 3
    assert [k(x) for x in ("Grep", "Glob", "grep_search")] == ["search"] * 3
    assert [k(x) for x in ("WebFetch", "web_search", "search_web")] == ["web"] * 3
    assert [k(x) for x in ("mcp__github__get_issue", "github.get_issue")] == ["mcp"] * 2
    assert [k(x) for x in ("Skill", "TodoWrite", "ExitPlanMode", "update_plan", "Mystery")] == [
        "skill", "plan", "plan", "plan", "other"]


def test_shell_ops_and_repo_paths():
    ops = traces.shell_ops
    assert ops("cd src && uv run pytest -q && git commit -qm 'fix; tests'") == ["test", "git.commit"]
    assert ops("python3 -m pytest tests/ | tail -5") == ["test"]
    for cmd in ("npm test", "pnpm run test:unit", "npx vitest run", "go test ./...", "cargo nextest run",
                "timeout 600 make test", "FOO=1 ./gradlew check", "bundle exec rspec", "env CI=1 jest"):
        assert ops(cmd) == ["test"], cmd
    assert ops("git -C repo push origin HEAD") == ["git.push"] and ops("gh pr create --fill") == ["pr.open"]
    assert ops("rm -rf build") == ops("git rm old.py") == ops("find . -name '*.pyc' -delete") == ["delete"]
    assert ops("ls -la; cat README.md") == [] and ops("echo pytest") == [] and ops("npm install") == []
    rp = traces.repo_path
    assert rp("/workspace/app/src/a.py", "/workspace/app") == "src/a.py" and rp("src/../a.py", None) == "a.py"
    assert rp("/workspace/app2/x", "/workspace/app") is None and rp("../x", "/workspace/app") is None
    assert rp("/etc/hosts", "/workspace/app") is None and rp("/workspace/app/src/a.py", None) is None
    assert traces.diff_files("M\0src/a.py\0D\0old.py\0A\0new") == (["src/a.py", "old.py"], ["old.py"])
    assert traces.executable("CI=1 /usr/bin/pytest -q") == "pytest" and traces.executable("cd x && make") is None
    assert traces._skill_name('{"skill": "pdf"}') == "pdf" and traces._skill_name("not json") is None


def _behavior(export: dict) -> dict:
    """Behavior specs written against the export alone (what the sister tool does): each is a plain predicate."""
    spans = _spans(export)
    calls = [(_attrs(sp), sp) for sp in spans if _attrs(sp)["hangar.kind"] in ("tool.call", "approval")]
    events = [_attrs(e) for sp in spans for e in sp.get("events", [])]

    def seqs(pred) -> list[int]:
        return sorted(int(a["hangar.seq"]) for a, _ in calls if "hangar.seq" in a and pred(a))

    def ops(a) -> list[str]:
        return [v["stringValue"] for v in a.get("hangar.shell.ops", {}).get("values", [])]

    tool = [a for a, _ in calls if a["hangar.kind"] == "tool.call"]
    first = {k: min(seqs(p), default=None) for k, p in {
        "plan": lambda a: a.get("hangar.tool.kind") == "plan",
        "edit": lambda a: a.get("hangar.tool.kind") == "edit",
        "test": lambda a: a["hangar.kind"] == "tool.call" and "test" in ops(a),
        "commit": lambda a: a["hangar.kind"] == "tool.call" and "git.commit" in ops(a),
        "pr": lambda a: a["hangar.kind"] == "tool.call" and "pr.open" in ops(a)}.items()}
    deletes = [a for a in tool if "delete" in ops(a)]
    approvals = [a for a, _ in calls if a["hangar.kind"] == "approval" and a["hangar.approval.status"] == "allowed"]
    order = [first[k] for k in ("plan", "edit", "test", "pr")]
    return {
        "tests_before_commit": first["test"] is not None and first["commit"] is not None
        and first["test"] < first["commit"],
        "no_edits_outside_src": all(e.get("hangar.file.repo_path", "").startswith("src/")
                                    for e in events if e["hangar.kind"] == "file.change"),
        "asked_before_deleting": all(any("delete" in ops(p) and int(p["hangar.seq"]) < int(d["hangar.seq"])
                                         for p in approvals if "hangar.seq" in p) for d in deletes),
        "plan_edit_test_pr": None not in order and order == sorted(order)}


def test_behavior_specs_can_be_written_from_the_export_alone(settings, broker):
    """review(#114): "tests run before commit", "no edits outside src/", "asked before deleting" and
    "plan -> edit -> test -> PR", from the exported spans and events only. Every line below is read in one poll,
    so they share a timestamp: the event log's order (`hangar.seq`) is what orders them."""
    activity = Activity(settings, broker.redact)
    svc = SimpleNamespace(activity=activity, fleet=SimpleNamespace(broker=broker))
    t = SessionTurn(message="go", run_id="run-1", harness="claude-code", started_at=1000.0, finished_at=1100.0,
                    outcome="exited", exit_code=0, live=True, approval_policy="ask",
                    approvals=[Approval(id="ap-000000000001", tool="Bash", detail="rm -rf docs/old",
                                        requested_at=1001.0, status="allowed", by="alice", decided_at=1001.0)])
    s = Session(name="s1", environment="plain", harness="claude-code", turns=[t], repo="https://github.com/a/app.git")
    root = "/workspace/app"

    def call(i: str, tool: str, text: str, kind: str = "tool_call") -> list[RunEvent]:
        return [RunEvent(turn=1, run_id="run-1", kind=kind, text=text, data={"id": i, "tool": tool}, ts=1001.0),
                RunEvent(turn=1, run_id="run-1", kind="tool_result", text="ok", data={"id": i, "ok": True},
                         ts=1001.0)]

    def change(path: str) -> RunEvent:
        return RunEvent(turn=1, run_id="run-1", kind="file_change", text=path, data={"path": path, "op": "write"},
                        ts=1001.0)

    activity.append(s.id, [
        *call("p", "TodoWrite", "TodoWrite plan"),
        *call("e", "Edit", f"Edit {root}/src/app.py"), change(f"{root}/src/app.py"),
        *call("t", "Bash", "uv run pytest -q", kind="command"),
        RunEvent(turn=1, run_id="run-1", kind="approval_request", text="Bash: rm -rf docs/old", ts=1001.0,
                 data={"id": "ap-000000000001", "tool": "Bash", "status": "pending", "detail": "rm -rf docs/old"}),
        *call("d", "Bash", "rm -rf docs/old", kind="command"),
        *call("c", "Bash", "git add -A && git commit -qm fix", kind="command"),
        *call("r", "Bash", "gh pr create --fill", kind="command")])
    good = _behavior(traces.session_export(svc, s, turn=1))
    assert good == {"tests_before_commit": True, "no_edits_outside_src": True, "asked_before_deleting": True,
                    "plan_edit_test_pr": True}, good

    # the same agent, badly behaved: commits before testing, deletes unasked, edits outside src/
    t2 = SessionTurn(message="go", run_id="run-2", harness="claude-code", started_at=2000.0, finished_at=2100.0,
                     outcome="exited", exit_code=0)
    s2 = Session(name="s2", environment="plain", harness="claude-code", turns=[t2], repo="https://github.com/a/app.git")
    evs = [*call("c", "Bash", "git commit -qm wip", kind="command"), *call("e", "Write", "Write /etc/hosts"),
           change("/etc/hosts"), *call("d", "Bash", "rm -rf src/legacy", kind="command"),
           *call("t", "Bash", "npm test", kind="command")]
    for ev in evs:
        ev.run_id, ev.ts = "run-2", 2001.0
    activity.append(s2.id, evs)
    bad = _behavior(traces.session_export(svc, s2, turn=1))
    assert bad == {"tests_before_commit": False, "no_edits_outside_src": False, "asked_before_deleting": False,
                   "plan_edit_test_pr": False}, bad
    hosts = next(e for sp in _spans(traces.session_export(svc, s2, turn=1)) for e in sp.get("events", [])
                 if e["name"] == "file.change")
    assert _attrs(hosts)["hangar.file.in_repo"] is False and "hangar.file.repo_path" not in _attrs(hosts)


async def test_live_tool_spans_read_the_event_log_off_the_event_loop(settings, monkeypatch):
    """HANGAR_TRACE_LIVE_TOOLS reads the session's event log (up to HANGAR_EVENTS_MAX_MB) as each turn ends: never on
    the event loop, which 60 sessions share."""
    import threading

    from hangar import observe

    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider

    seen: list[int] = []
    monkeypatch.setattr(traces, "live_children", lambda *a: seen.append(threading.get_ident()))
    monkeypatch.setattr(observe, "_tracer", TracerProvider().get_tracer("hangar"))
    settings.trace_live_tools = True

    class Svc:
        def __init__(self):
            self.settings = settings

        @observe.turn_end
        async def finish(self, s, turn):
            return ("exited", None)

    s = Session(name="s", environment="plain", turns=[SessionTurn(message="m", run_id="r", started_at=1.0)])
    assert await Svc().finish(s, s.turns[0]) == ("exited", None)
    assert seen and seen[0] != threading.get_ident()


def test_stored_manifests_and_trial_files_are_redacted_on_the_way_out(settings, broker):
    """A secret stored after a manifest or a trial's files were written is masked when they are read, as trace
    exports are (spec 0014: redacted on the way out)."""
    late = "hglate-SECRET-0a1b2c3d4e5f6a7b"
    s = Session(name="s", environment="plain", turns=[SessionTurn(message="m", run_id="r", manifest="sha256:x")])
    manifest.store(settings).put(s.id, 1, {"schema": manifest.SCHEMA, "instructions": {"prompt": {"text": late}}})
    svc = SimpleNamespace(settings=settings, fleet=SimpleNamespace(broker=broker))
    st = experiments.State(spec={"name": "e"}, stage="done", trials=[
        experiments.Trial(key="t/a/1", task="t", variant="a", trial=1, state="done", stored=["trace.json"])])
    exp = Job(title="e", instructions="x", environment="plain", labels={experiments.KIND_LABEL: experiments.KIND})
    exp.result.data = st.model_dump(mode="json", by_alias=True)
    experiments._write(experiments._dir(settings, exp.id, "t/a/1"), {"trace.json": {"x": [late]}})
    js = SimpleNamespace(settings=settings, broker=broker)
    broker.put("late", "webhook", CredentialKind.GENERIC, late)
    assert late not in json.dumps(manifest.for_session(svc, s, 1))  # type: ignore[arg-type]
    assert late not in json.dumps(manifest.for_session(svc, s))  # type: ignore[arg-type]
    assert experiments.trial_file(js, exp, "t/a/1", "trace.json") == {"x": ["***"]}  # type: ignore[arg-type]


def test_an_egress_change_mid_experiment_stops_new_trials(fleet, tmp_path):
    """Egress parity holds for the whole experiment, not only at submit: an approved egress request (or an edit)
    that widens one environment mid-way would run later trials of its variants with more of the network."""
    fleet.catalog.add({"name": "ea", "harnesses": ["codex"], "egress": {"mode": "allowlist", "allow": ["a.example"]}},
                      save_dir=tmp_path)
    fleet.catalog.add({"name": "eb", "harnesses": ["codex"], "egress": {"mode": "allowlist", "allow": ["a.example"]}},
                      save_dir=tmp_path)
    spec = experiments.ExperimentSpec.model_validate({
        "name": "e", "environment": "ea", "tasks": [{"id": "t", "instructions": "x", "repo": "r"}],
        "variants": {"a": {}, "b": {"environment": "eb"}}})
    st = experiments.State(spec=spec.model_dump(mode="json"), pins={"egress": experiments._egress(fleet, spec)})
    js = SimpleNamespace(fleet=fleet, _jobs={})
    assert experiments._stop_starting(js, Job(title="e", instructions="x", environment="ea"), st, spec) is None
    fleet.catalog.add({"name": "eb", "harnesses": ["codex"],
                       "egress": {"mode": "allowlist", "allow": ["a.example", "b.example"]}}, save_dir=tmp_path)
    why = experiments._stop_starting(js, Job(title="e", instructions="x", environment="ea"), st, spec)
    assert why and "egress policy of eb changed" in why


def test_list_attributes_are_redacted_too(broker):
    broker.put("seeded", "webhook", CredentialKind.GENERIC, SECRET)
    x = traces.Export(traces.trace_id("j"), broker.redact)
    sp = x.span("j", "job", 1.0, 2.0, {"hangar.kind": "job", "hangar.diff.files": [f"src/{SECRET}.py"]})
    x.event(sp, "verify", 1.5, {"hangar.verify.new_failures": (f"test_x[{SECRET}]",)})
    _no_secret(x.request({}))


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
    child = got["execute_tool shell cat"]  # GenAI's command refinement: the executable, when it is one process
    assert child.attributes["process.executable.name"] == "cat"
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
        # an experiment's index and its trials' stored files, over REST
        st = experiments.State(spec={"name": "e"}, stage="done", trials=[
            experiments.Trial(key="t/a/1", task="t", variant="a", trial=1, state="done", stored=["trace.json"])])
        exp = Job(title="e", instructions="x", environment="plain", owner=tok["id"], state=JobState.SUCCEEDED,
                  labels={experiments.KIND_LABEL: experiments.KIND})
        exp.result.data = st.model_dump(mode="json", by_alias=True)
        h.store.put(exp)
        experiments._write(experiments._dir(h.settings, exp.id, "t/a/1"), {"trace.json": {"resourceSpans": []}})
        v = c.get(f"/api/experiments/{exp.id}", headers=svc).json()
        jsonschema.validate(v, INDEX_SCHEMA)
        assert v["trials"][0]["links"]["trace"] == f"/api/experiments/{exp.id}/trials/t/a/1/trace"
        assert c.get(v["trials"][0]["links"]["trace"], headers=svc).json() == {"resourceSpans": []}
        assert c.get(f"/api/experiments/{exp.id}/trials/t/a/1/manifest", headers=svc).status_code == 404
        assert c.get(f"/api/experiments/{exp.id}/trials/t/../../x/trace", headers=svc).status_code == 404
        other_tok = c.post("/api/tokens", json={"name": "svc2", "scopes": ["jobs"]}, headers=admin).json()["token"]
        assert c.get(f"/api/experiments/{exp.id}", headers={"Authorization": f"Bearer {other_tok}"}).status_code \
            == 404


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
        "grader": {"command": "cat seen.txt \"$HANGAR_HIDDEN_TESTS/hidden_test.txt\"; test ! -e hidden_test.txt && "
                              "test \"$HANGAR_REPO\" = \"$PWD\" && test -f \"$HANGAR_HIDDEN_TESTS/hidden_test.txt\" && "
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
        assert t["harness_digest"] == m["harness_digest"] and t["environment_digest"] == m["environment_digest"]
        _no_secret(m)
        tr = experiments.trial_file(jobs, exp, t["key"], "trace.json")
        _check_otlp(tr)
        _no_secret(tr)
        g = t["grade"]
        assert g["exit_code"] == 0 and g["data"] == {"score": 1, "trial": t["key"]}, g
        assert g["applied"] is True and "the agent never saw this" in g["tail"]
        # --- sealed (#126): pristine hidden tests (outside the repo), no network, and the trust that gives
        assert g["files_mode"] == "pristine" and g["network"] == "none" and g["trust"] == "best_effort"
        assert "tamper_signals" not in g
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


def test_labels_alone_never_make_a_trial(fleet):
    """A job or session can carry any labels: only a trial the experiment's runner started gets its overlay."""
    from hangar.fleet import FleetError
    from hangar.models import Sandbox

    overlay = {"skills": [], "instructions": "x"}
    fleet.store.kv_set(f"{experiments.KV_OVERLAY}job-x:a", json.dumps(overlay))
    sb = Sandbox(name="f", environment="plain", provider="docker",
                 labels={experiments.EXP_LABEL: "job-x", experiments.VARIANT_LABEL: "a", "hangar.job": "job-mine"})
    assert experiments.overlay_for(fleet, sb) is None
    spec = fleet.catalog.get("plain")
    assert experiments.effective_spec(fleet, sb, spec) is spec
    fleet.store.kv_set(experiments.KV_TRIAL + "job-mine", "job-x")  # what the runner records as it starts one
    assert experiments.overlay_for(fleet, sb) == overlay
    fleet.store.kv_delete(f"{experiments.KV_OVERLAY}job-x:a")
    with pytest.raises(FleetError, match="overlay for variant a is missing"):  # a real trial fails closed
        experiments.overlay_for(fleet, sb)


async def test_an_experiment_survives_a_control_plane_restart(jobs, world, fleet, monkeypatch):
    """#75: the runner is tied to no request; a restart resumes it where its index says, and its trials re-attach."""
    slow = Path(world["root"]) / "slow.sh"
    slow.write_text("#!/usr/bin/env bash\nsleep 2\necho done > seen.txt && git add seen.txt && git commit -qm s\n")
    monkeypatch.setitem(HARNESSES, "tagent", HARNESSES["tagent"].model_copy(update={"run": f"bash {slow}"}))
    spec = experiments.ExperimentSpec.model_validate({
        "name": "restart", "environment": "tenv", "trials": 2, "parallel": 2,
        "tasks": [{"id": "t", "instructions": "wait", "repo": world["url"], "verify": "test -f seen.txt"}],
        "variants": {"a": {}}})
    exp = experiments.submit(jobs, spec, Caller("svc", "svc"))
    for _ in range(400):
        st = experiments.load(jobs.get(exp.id))
        kids = [jobs.get(t.job) for t in st.trials if t.job]
        if len(kids) == 2 and all(k.turns and k.turns[0].run_id for k in kids):
            break
        await asyncio.sleep(0.05)
    await jobs.stop()  # mid-trial
    again = JobService(fleet, jobs.sessions, transport=jobs.transport)
    await again.start()
    try:
        done = await _until(again, exp.id, DONE, timeout=60)
        assert done.state == JobState.SUCCEEDED, [e.message for e in done.events]
        st = experiments.load(done)
        assert [(t.state, t.outcome) for t in st.trials] == [("done", "succeeded")] * 2
        assert all(t.verify["passed"] and t.stored for t in st.trials)
    finally:
        await again.stop()


# ====================================================================== review(#114)
async def test_the_graders_sandbox_holds_nothing_the_trials_code_could_take(fleet, tmp_path, monkeypatch):
    """The grader runs agent-written code (the trial's patch): like a repo build's or a validation's sandbox it gets
    no credential, no `secrets:`, no cloud token, and only a fetch-only grant (a push-capable one before)."""
    from hangar.autoinstall import validation_credentials
    from hangar.models import Sandbox
    from tests.test_gateway_git import _box, _files_containing

    fleet.broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_graderCLONEtoken0123456789")
    fleet.broker.put("db", "custom", CredentialKind.GENERIC, "db_SECRETvalue", env_var="UNUSED")
    fleet.catalog.add({"name": "gradeenv", "harnesses": ["git"], "credentials": ["gh"],
                       "secrets": {"DB_PASSWORD": "db"}}, save_dir=tmp_path)
    clouds: list[str] = []
    real = fleet.cloud.attach

    async def attach(prov, sb, spec):
        clouds.append(sb.id)
        return await real(prov, sb, spec)

    monkeypatch.setattr(fleet.cloud, "attach", attach)
    from hangar import delivery, egress

    async def proxy_url(fleet_, prov, sb):  # --- sealed (#126): a grader is always proxied; the fake has no network
        return "http://127.0.0.1:9"

    monkeypatch.setattr(egress, "_proxy_url", proxy_url)

    assert delivery.GRADER_LABEL == experiments.GRADER_LABEL
    labels = {experiments.GRADER_LABEL: "job-exp", experiments.TRIAL_LABEL: "t/a/1"}
    spec = fleet.catalog.get("gradeenv")
    sb = await fleet.create("gradeenv", credentials=validation_credentials(fleet, spec), labels=labels)
    box = _box(fleet, sb)
    for value in ("ghp_graderCLONEtoken", "db_SECRETvalue"):
        assert _files_containing(box, value) == [], value
    assert sb.id not in clouds, "no cloud token"
    grader = Sandbox(name="g", environment="gradeenv", provider="docker", labels=labels)
    assert delivery.builds_repo_code(grader)
    assert fleet.gateway.fetch_only(grader) and fleet.gateway.push_scope(grader, "main") == []
    fleet.settings.gateway_url = "https://gw.example"
    assert fleet.gateway.mode_for(spec, grader, None) == "gateway"


async def test_a_trial_never_resizes_the_environments_warm_pool(fleet, tmp_path, monkeypatch):
    """A trial's sandbox didn't come from the warm pool and mounts one pack: refilling the pool with its spec would
    give every later sandbox of the environment only that harness's pack (Kubernetes recreates the template)."""
    from hangar import envbuilds

    fleet.catalog.add({"name": "warmenv", "harnesses": ["git"], "warm_pool": 1}, save_dir=tmp_path)
    refills: list = []

    async def refill(spec, pname, image):
        refills.append(spec)

    async def no_build(*a, **k):  # no repo build: the environment's own image, which its warm pool holds
        return envbuilds.Pick()  # what `pick` answers then (truthy: the refill checks its image)

    monkeypatch.setattr(fleet, "_refill", refill)
    monkeypatch.setattr(envbuilds.EnvBuilds, "pick", no_build)
    await fleet.create("warmenv", labels={experiments.PACKS_LABEL: "selected"})
    await asyncio.sleep(0)
    assert refills == []
    await fleet.create("warmenv")  # everything else refills as before
    await asyncio.sleep(0)
    assert len(refills) == 1


def _digests_of(fleet, tmp_path, **over):
    """The manifest a new sandbox of a small environment would run claude-code with, the environment changed by
    `over` and its one skill's SKILL.md holding `skill` (default: 'Use alpha.')."""
    from hangar.envspec import EnvSpec

    skill = tmp_path / "skills" / "alpha"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(over.pop("skill", "Use alpha."))
    spec = EnvSpec.model_validate({"name": "digests", "harnesses": ["claude-code"],
                                   "skills": [{"path": "skills/alpha"}],
                                   "egress": {"mode": "allowlist", "allow": ["a.example"]}, **over})
    spec.source_dir = tmp_path
    m = manifest.build(fleet, spec, "claude-code")
    jsonschema.validate(m, SCHEMA)
    return m


def test_harness_and_environment_digests_split_config_digest(fleet, tmp_path):
    """#131: the two halves of config_digest. An egress change is the environment's; a skill's content the
    harness's."""
    base = _digests_of(fleet, tmp_path)
    assert base["harness_digest"].startswith("sha256:") and base["environment_digest"].startswith("sha256:")
    wider = _digests_of(fleet, tmp_path, egress={"mode": "allowlist", "allow": ["a.example", "b.example"]})
    assert wider["harness_digest"] == base["harness_digest"]
    assert wider["environment_digest"] != base["environment_digest"]
    assert wider["config_digest"] != base["config_digest"]
    edited = _digests_of(fleet, tmp_path, skill="Use alpha, carefully.")
    assert edited["harness_digest"] != base["harness_digest"]
    assert edited["environment_digest"] == base["environment_digest"]
    # the canonical form spec 0014 §2 writes out, so a harness lock (#133) produces the same value
    import hashlib

    picked = {k: manifest.pick(base, k) for k in manifest.HARNESS_SECTIONS}
    assert picked["skills.declared[*].{name,digest}"] == [
        {"name": "alpha", "digest": base["skills"]["declared"][0]["digest"]}]
    canon = json.dumps(picked, sort_keys=True, separators=(",", ":"))
    assert base["harness_digest"] == "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


def test_manifests_stored_before_the_two_digests_still_validate(fleet, tmp_path):
    """Older stored manifests lack harness_digest and environment_digest; the schema keeps them optional."""
    m = _digests_of(fleet, tmp_path)
    old = {k: v for k, v in m.items() if k not in ("harness_digest", "environment_digest")}
    jsonschema.validate(old, SCHEMA)
    assert "harness_digest" not in SCHEMA["required"] and "environment_digest" not in SCHEMA["required"]


def test_a_drift_message_names_the_half_that_drifted():
    def trial(key, cfg, h, e):
        return experiments.Trial(key=key, task="t", variant="a", trial=1, config_digest=cfg, harness_digest=h,
                                 environment_digest=e)

    first = trial("t/a/1", "sha256:c1", "sha256:h1", "sha256:e1")
    assert experiments.drift_message(trial("t/a/2", "sha256:c2", "sha256:h1", "sha256:e2"), first).startswith(
        "environment drift:")
    assert experiments.drift_message(trial("t/a/2", "sha256:c2", "sha256:h2", "sha256:e1"), first).startswith(
        "harness drift:")
    both = experiments.drift_message(trial("t/a/2", "sha256:c2", "sha256:h2", "sha256:e2"), first)
    assert both.startswith("harness drift and environment drift:")
    older = experiments.drift_message(trial("t/a/2", "sha256:c2", None, None), first)  # a trial from before #131
    assert "differs from trial t/a/1" in older
