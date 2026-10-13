"""Context exchange (spec 0011 §2): HANGAR_IO, the hangar-agent helper, local work in, results, questions,
artifacts and code out. On the fake provider with a scripted agent, real git and tmux."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from hangar import exchange
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, JobService, SubmitJob, handle_github
from hangar.models import ContextItem, CredentialKind, JobState, SessionState
from hangar.sessions import NewSession, SessionError, SessionService
from hangar.vault import Vault
from tests.conftest import scratch_root
from tests.test_jobs import Hooks, _until

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]

# A scripted agent that keeps a conversation file (so resume is checkable) and speaks the I/O protocol.
AGENT = r"""#!/usr/bin/env bash
set -e
conv="$HOME/.fake/sessions"; mkdir -p "$conv"
id="${HANGAR_SESSION_RESUME:-$HANGAR_SESSION_NEW}"
if [ -n "$HANGAR_SESSION_RESUME" ]; then [ -f "$conv/$id.jsonl" ] || { echo "no conversation $id"; exit 7; }; fi
msg="$(cat "$HANGAR_PROMPT_FILE")"
[ -n "$id" ] && echo "$msg" | head -1 >> "$conv/$id.jsonl"
mode=$(cat "$MODE_FILE" 2>/dev/null || echo ok)
case "$msg" in
  *"did not leave a usable structured result"*)
    [ "$mode" = "never" ] || hangar-agent result '{"ok": true, "files": 1}' ;;
  *TASK-LOCAL*)
    git log --format=%s | grep -q "local: unpushed"
    grep -q "local edit" README.md
    test -f local-new.txt
    ls "$HANGAR_IO/in/1" > "$HOME/in-seen.txt" 2>/dev/null || true
    hangar-agent progress --pct 50 "halfway \"there\""
    sleep 1.5
    echo "# Report" > report.md && hangar-agent artifact report.md
    mkdir -p shots && printf '\211PNG\r\n\000' > shots/a.png && hangar-agent artifact shots/a.png shots/a.png
    echo done > agent.txt && git add agent.txt && git commit -qm "agent: work"
    echo "wip by agent" > agent-wip.txt
    hangar-agent result '{"ok": true, "files": 2}'
    hangar-agent done "Finished the local work" ;;
  *TASK-BADRESULT*) hangar-agent result '{"ok": "yes"}' ;;
  *TASK-USE-B*) echo B > choice.txt && git add choice.txt && git commit -qm "choose B"
    hangar-agent done "Went with B" ;;
  *TASK-ASK*) echo half > half.txt && git add half.txt && git commit -qm "half done"
    hangar-agent ask "Use A or B?" ;;
  *TASK-ARTIFACTS*)
    for i in 1 2 3; do echo "file $i" > "$HANGAR_IO/out/artifacts/f$i.txt"; done
    ln -s /etc/passwd "$HANGAR_IO/out/artifacts/link"
    echo "secret: sk-test-SECRET-123" > "$HANGAR_IO/out/artifacts/leak.txt" ;;
esac
echo "{\"type\":\"result\",\"result\":\"did: $(echo "$msg" | head -1 | cut -c1-60)\"}"
"""


@pytest.fixture
def world(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="xchg-", dir=scratch_root()))
    remote, seed = root / "remote.git", root / "seed"
    subprocess.run([*GIT, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*GIT, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    subprocess.run([*GIT, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*GIT, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    agent, mode = root / "agent.sh", root / "mode"
    agent.write_text(AGENT)
    monkeypatch.setitem(HARNESSES, "xagent", Harness(
        name="xagent", title="Exchange", run=f"MODE_FILE={mode} bash {agent}", session_dirs=[".fake/sessions"],
        resume="preset", resume_hint="xagent --resume {id}"))
    envs = root / "envs"
    envs.mkdir()
    (envs / "xenv.yaml").write_text("name: xenv\nharnesses: [xagent]\n")
    yield {"root": root, "remote": remote, "mode": mode, "envs": envs, "url": f"file://{remote}"}
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def svc(fleet, world, settings, store):
    settings.session_poll_seconds = 0.1
    settings.job_poll_seconds = 0.1
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    s = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    await s.start()
    yield s
    await s.stop()
    for box in fleet.providers["docker"].boxes.values():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


@pytest.fixture
async def jobs(fleet, svc):
    hooks = Hooks()
    j = JobService(fleet, svc, transport=httpx.MockTransport(hooks))
    j.hooks = hooks

    async def resolve(host):
        return ["93.184.216.34"]

    j.resolve = resolve
    await j.start()
    yield j
    await j.stop()


async def settle(svc: SessionService, ref: str, timeout: float = 20.0):
    for _ in range(int(timeout / 0.05)):
        s = svc.get(ref)
        if s.state.value in ("idle", "suspended", "failed") and not svc.busy(s.id):
            return s
        await asyncio.sleep(0.05)
    raise AssertionError(f"{ref} stuck in {svc.get(ref).state}: {svc.get(ref).reason}")


def git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def local_checkout(world, name: str = "local") -> Path:
    """A developer's clone with an unpushed commit, an uncommitted edit and an untracked file."""
    local = world["root"] / name
    subprocess.run([*GIT, "clone", "-q", world["url"], str(local)], check=True)
    (local / "local.txt").write_text("from my laptop\n")
    git(local, "add", "local.txt")
    git(local, "commit", "-qm", "local: unpushed")
    (local / "README.md").write_text("hello\nlocal edit\n")
    (local / "local-new.txt").write_text("brand new\n")
    return local


def data(res) -> dict:
    assert not res.is_error, res.content[0].text
    return json.loads(res.content[0].text)


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}, "files": {"type": "integer"}},
          "required": ["ok", "files"]}


# ---------------------------------------------------------------------------- the acceptance flow (MCP)
async def test_mcp_round_trip_local_work_in_result_artifacts_and_code_out(svc, world, fleet, broker):
    broker.put("leaky", "webhook", CredentialKind.GENERIC, "sk-test-SECRET-123")
    local = local_checkout(world)
    items, info = exchange.from_local(local)
    assert sorted(i["kind"] for i in items) == ["bundle", "patch"]
    assert info["commits"] == 1 and info["repo"] == world["url"] and info["base_branch"] == "main"

    from mcp import Client

    from hangar.mcp_server import build_mcp

    progress: list[str] = []

    async def on_progress(p, total, message):
        progress.append(message or "")

    async with Client(build_mcp(fleet, None, svc)) as c:
        data(await c.call_tool("new_session", {
            "name": "laptop", "environment": "xenv", "repo": info["repo"], "base_branch": info["base_branch"],
            "message": "TASK-LOCAL finish what I started",
            "context": items + [{"name": "notes.md", "content": "remember X"}], "output_schema": SCHEMA}))
        done = data(await c.call_tool("wait_for_turn", {"ref": "laptop", "timeout_seconds": 60},
                                      progress_callback=on_progress))
        assert done["done"] and done["outcome"] == "exited" and done["exit_code"] == 0, done
        assert done["result"] == {"ok": True, "files": 2} and done["result_error"] is None
        assert done["summary"] == "Finished the local work"
        assert any('[50%] halfway "there"' in m for m in progress), progress
        assert {a["path"] for a in done["artifacts"]} == {"report.md", "shots/a.png"}
        assert done["artifacts"][0]["uri"].startswith("hangar://sessions/laptop/artifacts/1/")

        s = svc.get("laptop")
        box = fleet.providers["docker"].boxes[s.sandbox_id]
        assert (box / "home/agent/in-seen.txt").read_text().split() == ["notes.md"]
        prompt = next((box / "home/agent/.hangar/runs").glob("*.prompt")).read_text()
        assert "hangar-agent ask" in prompt and "out/result.json" in prompt and "unpushed commits" in prompt
        assert "remember X" not in json.dumps(svc.view(s))  # attachment contents never come back in views

        art = data(await c.call_tool("get_artifact", {"ref": "laptop", "turn": 1, "path": "report.md"}))
        assert art["text"] == "# Report\n" and not art["truncated"]
        png = data(await c.call_tool("get_artifact", {"ref": "laptop", "turn": 1, "path": "shots/a.png"}))
        assert base64.b64decode(png["base64"]) == b"\x89PNG\r\n\x00"
        res = await c.read_resource("hangar://sessions/laptop/artifacts/1/report.md")
        assert res.contents[0].text == "# Report\n"
        for bad in ("../../etc/passwd", "/etc/passwd", "nope.md"):
            r = await c.call_tool("get_artifact", {"ref": "laptop", "turn": 1, "path": bad})
            assert r.is_error

        patch = data(await c.call_tool("get_session_patch", {"ref": "laptop"}))
        assert patch["source"] == "sandbox" and not patch["truncated"]
        changed = {f["path"] for f in patch["files"]}
        assert {"local.txt", "README.md", "local-new.txt", "agent.txt", "agent-wip.txt"} <= changed, changed

    # artifacts are encrypted at rest
    raw = (svc.root / s.id / "artifacts" / "1" / "report.md.enc").read_bytes()
    assert b"# Report" not in raw

    # suspended: the patch comes from the checkpoint
    svc.suspend("laptop", requester="sean")
    s = await settle(svc, "laptop")
    assert s.state == SessionState.SUSPENDED
    patch = await exchange.session_patch(svc, s)
    assert patch["source"].startswith("checkpoint") and "agent-wip.txt" in patch["patch"]

    # pull the work into a fresh clone (what `hangar session pull` does after downloading the parts)
    dest = world["root"] / "elsewhere"
    subprocess.run([*GIT, "clone", "-q", world["url"], str(dest)], check=True)
    m = exchange.pull_manifest(s)
    parts = {p: await exchange.pull_part(svc, s, m["checkpoint"], p) for p in m["parts"]}
    notes = exchange.pull_into(dest, "laptop", m, parts)  # no --checkout: the current branch is left alone
    assert git(dest, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(dest, "log", "--format=%s", "hangar/laptop").splitlines()[:2] == ["agent: work", "local: unpushed"]
    assert any("--checkout" in n for n in notes) and not (dest / "agent-wip.txt").exists()
    notes = exchange.pull_into(dest, "laptop", m, parts, checkout=True)
    assert git(dest, "rev-parse", "--abbrev-ref", "HEAD").strip() == "hangar/laptop"
    assert (dest / "README.md").read_text() == "hello\nlocal edit\n"
    assert (dest / "agent-wip.txt").read_text() == "wip by agent\n" and (dest / "local-new.txt").exists()
    with pytest.raises(exchange.ExchangeError, match="working tree has changes"):
        exchange.pull_into(dest, "laptop", m, parts, checkout=True)


async def test_repair_turn_for_an_invalid_result(svc, world):
    svc.new(NewSession(name="fix", environment="xenv", repo=world["url"], message="TASK-BADRESULT",
                       output_schema=SCHEMA), owner="admin", requester="sean")
    s = await settle(svc, "fix")
    t = s.turns[0]
    assert t.repair_run_id and t.result == {"ok": True, "files": 1} and t.result_error is None, t
    assert s.turns[0].mode == "new" and len(s.turns) == 1  # the repair is part of the same turn

    world["mode"].write_text("never")  # the repair doesn't fix it either: recorded, not retried again
    svc.send("fix", "TASK-BADRESULT again", requester="sean", output_schema=SCHEMA)
    s = await settle(svc, "fix")
    t = s.turns[1]
    assert t.repair_run_id and t.result is None and "ok: 'yes' is not of type 'boolean'" in t.result_error, t

    # without a schema a result is optional and never repaired
    svc.send("fix", "plain turn", requester="sean")
    s = await settle(svc, "fix")
    assert s.turns[2].result is None and s.turns[2].result_error is None and not s.turns[2].repair_run_id


async def test_session_question_then_answer_resumes(svc, world, fleet):
    svc.new(NewSession(name="ask", environment="xenv", repo=world["url"], message="TASK-ASK"), owner="admin",
            requester="sean")
    s = await settle(svc, "ask")
    assert s.state == SessionState.IDLE and s.needs_input == "Use A or B?" and s.turns[0].outcome == "needs_input"
    svc.send("ask", "TASK-USE-B", requester="sean")
    s = await settle(svc, "ask")
    assert s.needs_input is None and s.turns[1].mode == "resume" and s.turns[1].summary == "Went with B"
    conv = fleet.providers["docker"].boxes[s.sandbox_id] / "home/agent/.fake/sessions" / f"{s.harness_session}.jsonl"
    assert len(conv.read_text().splitlines()) == 2  # one conversation, two turns


async def test_patch_that_does_not_apply_fails_clearly(svc, world):
    bad = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-nope\n+yes\n"
    svc.new(NewSession(name="badpatch", environment="xenv", repo=world["url"], message="go",
                       context=[ContextItem(name="wip.patch", kind="patch", content=bad)]),
            owner="admin", requester="sean")
    s = await settle(svc, "badpatch")
    assert "does not apply" in (s.reason or "") and s.turns[0].outcome == "error", s.reason
    assert "does not apply" in s.turns[0].summary and s.turns[0].run_id is None


async def test_bundle_with_an_unknown_base_is_refused(svc, world):
    other = world["root"] / "other"
    subprocess.run([*GIT, "init", "-q", str(other)], check=True)
    (other / "x").write_text("x")
    git(other, "add", "x")
    git(other, "commit", "-qm", "unrelated")
    (other / "y").write_text("y")
    git(other, "add", "y")
    git(other, "commit", "-qm", "on top")
    base = git(other, "rev-parse", "HEAD~1").strip()
    git(other, "bundle", "create", "-q", str(world["root"] / "b.bundle"), "HEAD", "--not", base)
    b64 = base64.b64encode((world["root"] / "b.bundle").read_bytes()).decode()
    svc.new(NewSession(name="badbundle", environment="xenv", repo=world["url"], message="go",
                       context=[ContextItem(name="c.bundle", kind="bundle", content=b64, encoding="base64",
                                            base_commit=base)]), owner="admin", requester="sean")
    s = await settle(svc, "badbundle")
    assert "is not in the session's repository" in (s.reason or ""), s.reason


async def test_artifact_caps_symlinks_and_redaction(svc, world, broker):
    broker.put("leaky", "webhook", CredentialKind.GENERIC, "sk-test-SECRET-123")
    svc.settings.exchange_max_artifacts = 2
    svc.new(NewSession(name="arts", environment="xenv", repo=world["url"], message="TASK-ARTIFACTS"),
            owner="admin", requester="sean")
    s = await settle(svc, "arts")
    t = s.turns[0]
    assert [a.path for a in t.artifacts] == ["f1.txt", "f2.txt"]
    assert any("symlink" in w for w in t.warnings) and any("kept the first 2" in w for w in t.warnings), t.warnings
    svc.settings.exchange_max_artifacts = 50
    svc.send("arts", "nothing new", requester="sean")
    s = await settle(svc, "arts")
    # unchanged files from earlier turns are not stored again; the ones skipped before are collected now
    assert [a.path for a in s.turns[1].artifacts] == ["f3.txt", "leak.txt"]
    _, text = await exchange.read_artifact(svc, s, 2, "leak.txt")
    assert b"SECRET" not in text and b"***" in text  # text artifacts are redacted like everything else


async def test_validation():
    for bad in ["", "/etc/passwd", "../x", "a/../b", "a//b", "./a", "a/\0"]:
        with pytest.raises(exchange.ExchangeError):
            exchange.safe_path(bad)
    assert exchange.safe_path("shots/a.png") == "shots/a.png"
    with pytest.raises(exchange.ExchangeError, match="not a valid JSON Schema"):
        exchange.check_schema({"type": "nope"})
    assert exchange.validate({"ok": 1}, SCHEMA) == ["(root): 'files' is a required property",
                                                     "ok: 1 is not of type 'boolean'"]


async def test_context_validation(svc, world):
    def new(name, **kw):
        return svc.new(NewSession(name=name, environment="xenv", repo=world["url"], **kw), owner="a",
                       requester="a")

    b64 = base64.b64encode(b"# v2 git bundle\nabc").decode()
    for kw, msg in [
        (dict(message="x", context=[ContextItem(name="b", kind="bundle", content="not a bundle")]), "not a git bundle"),
        (dict(message="x", context=[ContextItem(name="b", kind="bundle", content=b64, encoding="base64")]),
         "base_commit"),
        (dict(context=[ContextItem(name="n", content="x")]), "go with a message"),
        (dict(message="x", output_schema={"type": 5}), "not a valid JSON Schema"),
    ]:
        with pytest.raises((SessionError, ValueError), match=msg):
            new("v", **kw)
    svc.settings.exchange_max_patch_mb = 0
    with pytest.raises(ValueError, match="limit"):
        new("v", message="x", context=[ContextItem(name="p", kind="patch", content="diff --git a b")])


# ---------------------------------------------------------------------------- jobs: questions
async def test_job_question_github_comment_and_answer_resume_across_a_restart(jobs, world, broker, fleet):
    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_x")
    jobs.settings.github_token_credential = "gh"
    jobs.settings.github_repos = {"o/r": "xenv"}
    job = jobs.submit(SubmitJob(instructions="TASK-ASK pick one", repo=world["url"], environment="xenv",
                                callback_url="https://hooks.example/cb"),
                      Caller("github:sean", "github:sean"), source="github", source_ref="github:o/r#12")
    job = await _until(jobs, job.id, {JobState.NEEDS_INPUT, JobState.SUCCEEDED, JobState.FAILED})
    while job.id in jobs._active:
        await asyncio.sleep(0.05)
    assert job.state == JobState.NEEDS_INPUT and job.result.question == "Use A or B?", [e.message for e in job.events]
    assert job.turns[0].outcome == "needs_input" and not job.result.pushed and job.result.verify_exit_code is None
    assert not jobs.view(job)["active"]
    await asyncio.sleep(0.1)
    assert any("Use A or B?" in c and "/hangar <your answer>" in c for c in jobs.hooks.comments())
    assert "job.needs_input" in [json.loads(c.content)["event"] for c in jobs.hooks.callbacks()]

    # the control plane restarts while the job waits: it keeps waiting
    await jobs.stop()
    again = JobService(fleet, jobs.sessions, transport=jobs.transport)
    again.resolve = jobs.resolve
    await again.start()
    try:
        assert again.get(job.id).state == JobState.NEEDS_INPUT
        await asyncio.sleep(0.2)
        assert again.get(job.id).state == JobState.NEEDS_INPUT
        # a /hangar comment on the issue answers it (instead of starting a second job)
        res = await handle_github(again, "issue_comment", "d-1", {
            "action": "created", "repository": {"full_name": "o/r"}, "issue": {"number": 12, "title": "t"},
            "comment": {"body": "/hangar TASK-USE-B", "author_association": "OWNER", "user": {"login": "sean"}}})
        assert res == {"ok": True, "follow_up": job.id}
        done = await _until(again, job.id, {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})
        assert done.state == JobState.SUCCEEDED, [e.message for e in done.events]
        assert done.turns[1].summary == "Went with B" and done.result.question is None
        assert len(done.result.commits) == 2  # the work from before the question was kept
        s = again.sessions.get(done.session_id)
        assert s.turns[1].mode == "resume"  # same conversation
        box = fleet.providers["docker"].boxes[done.sandbox_id]
        prompt = sorted((box / "home/agent/.hangar/runs").glob("*.prompt"), key=lambda p: p.stat().st_mtime)[-1]
        assert prompt.read_text().startswith("Answer from github:sean to your question")
    finally:
        await again.stop()


async def test_job_from_local_delivers_local_and_agent_work(jobs, world):
    items, info = exchange.from_local(local_checkout(world))
    job = jobs.submit(SubmitJob(instructions="TASK-LOCAL", repo=info["repo"], environment="xenv", deliver="branch",
                                context=[ContextItem.model_validate(i) for i in items], output_schema=SCHEMA),
                      Caller("svc", "svc"))
    job = await _until(jobs, job.id, {JobState.SUCCEEDED, JobState.FAILED})
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert job.result.data == {"ok": True, "files": 2} and job.result.summary == "Finished the local work"
    assert {a.path for a in job.result.artifacts} == {"report.md", "shots/a.png"}
    log = subprocess.run(["git", "--git-dir", str(world["remote"]), "log", "--format=%s", job.branch],
                         capture_output=True, text=True).stdout.splitlines()
    assert log[:3] == [f"hangar: uncommitted changes from {job.id}", "agent: work", "local: unpushed"], log
    assert all(c.content is None for c in job.context if c.kind in exchange.CODE_KINDS)  # not kept around
    assert any("local: unpushed" in c for c in job.result.commits)


async def test_job_structured_result_attachments_and_cancel_while_waiting(jobs, world):
    job = jobs.submit(SubmitJob(instructions="TASK-BADRESULT", repo=world["url"], environment="xenv",
                                output_schema=SCHEMA), Caller("svc", "svc"))
    job = await _until(jobs, job.id, {JobState.SUCCEEDED, JobState.FAILED})
    assert job.result.data == {"ok": True, "files": 1} and job.turns[0].result == job.result.data
    world["mode"].write_text("ok")
    jobs.follow_up(job.id, "TASK-ASK", requester="svc",
                   context=[ContextItem(name="answer-key.txt", content="secret-ish")])
    job = await _until(jobs, job.id, {JobState.NEEDS_INPUT, JobState.SUCCEEDED, JobState.FAILED})
    while job.id in jobs._active:
        await asyncio.sleep(0.05)
    assert job.state == JobState.NEEDS_INPUT
    view = json.dumps(jobs.view(job))
    assert "secret-ish" not in view and "answer-key.txt" in view
    box = jobs.fleet.providers["docker"].boxes[job.sandbox_id]
    assert (box / f"home/agent/.hangar/jobs/{job.id}/in/2/answer-key.txt").read_text() == "secret-ish"
    jobs.cancel(job.id, requester="svc")
    assert jobs.get(job.id).state == JobState.CANCELLED


# ---------------------------------------------------------------------------- REST
def test_rest_artifacts_patch_and_pull(settings, world, monkeypatch):
    from fastapi.testclient import TestClient

    from hangar.app import create_app
    from tests.conftest import FakeProvider

    settings.admin_token = "t0ken"
    settings.session_poll_seconds = 0.1
    app = create_app(settings)
    with TestClient(app) as c:
        h = app.state.hangar
        fake = FakeProvider()
        h.fleet.providers["docker"] = fake
        h.fleet.provider_errors.pop("docker", None)
        h.catalog.dirs.insert(0, world["envs"])
        h.catalog.reload()
        c.headers["Authorization"] = "Bearer t0ken"
        local = local_checkout(world)
        items, _ = exchange.from_local(local)
        r = c.post("/api/sessions", json={"name": "rest", "environment": "xenv", "repo": world["url"],
                                          "message": "TASK-LOCAL", "context": items})
        assert r.status_code == 200, r.text
        for _ in range(400):
            x = c.get("/api/sessions/rest").json()
            if x["state"] == "idle" and not x["busy"]:
                break
            time.sleep(0.05)
        assert x["turns"][0]["exit_code"] == 0 and x["turns"][0]["context"][0]["name"] == "local-commits.bundle"
        assert "content" not in x["turns"][0]["context"][0]
        arts = c.get("/api/sessions/rest/artifacts").json()
        assert {a["path"] for a in arts} == {"report.md", "shots/a.png"}
        r = c.get("/api/sessions/rest/artifacts/1/report.md")
        assert r.content == b"# Report\n" and r.headers["content-disposition"].startswith("attachment")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert c.get("/api/sessions/rest/artifacts/1/..%2F..%2Fx").status_code in (400, 404)
        assert c.get("/api/sessions/rest/artifacts/9/report.md").status_code == 404
        assert c.get("/api/sessions/rest/artifacts/1/report.md", headers={"Authorization": ""}).status_code == 401
        p = c.get("/api/sessions/rest/patch").json()
        assert "agent.txt" in p["patch"]
        m = c.get("/api/sessions/rest/pull").json()
        assert set(m["parts"]) <= {"repo.bundle", "wip.patch", "untracked.tgz"} and "repo.bundle" in m["parts"]
        b = c.get(f"/api/sessions/rest/pull/{m['checkpoint']}/repo.bundle")
        assert b.status_code == 200 and b.content.startswith(b"# v")
        assert c.get(f"/api/sessions/rest/pull/{m['checkpoint']}/agent.tgz").status_code == 400
        for box in fake.boxes.values():
            subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                           capture_output=True)


# ---------------------------------------------------------------------------- the helper itself
@pytest.fixture
def helper(tmp_path):
    script = tmp_path / "hangar-agent"
    script.write_text(exchange.HELPER)
    script.chmod(0o755)
    io = tmp_path / "io"
    io.mkdir()

    def run(*args: str, stdin: str | None = None, env_io: bool = True):
        env = {"PATH": os.environ["PATH"], **({"HANGAR_IO": str(io)} if env_io else {})}
        return subprocess.run(["sh", str(script), *args], input=stdin, capture_output=True, text=True, env=env,
                              cwd=tmp_path)

    return run, io, tmp_path


def test_helper_progress_ask_result_done(helper):
    run, io, _ = helper
    assert run("progress", "--pct", "40", 'say "hi"\tnow\nnext \\ line').returncode == 0
    assert run("progress", "plain").returncode == 0
    assert run("progress", "--pct", "4x", "bad").returncode == 2
    lines = [json.loads(ln) for ln in (io / "out/progress.jsonl").read_text().splitlines()]
    assert lines[0]["message"] == 'say "hi" now\nnext \\ line' and lines[0]["pct"] == 40
    assert lines[1]["message"] == "plain" and "pct" not in lines[1] and isinstance(lines[1]["ts"], int)
    assert run("ask", stdin="Which one?\nA or B").returncode == 0
    assert (io / "out/QUESTION.md").read_text() == "Which one?\nA or B"
    assert run("ask", "   ").returncode == 2 and not (io / "out/QUESTION.md").exists()
    assert run("result", '{"a": 1}').returncode == 0
    assert json.loads((io / "out/result.json").read_text()) == {"a": 1}
    if shutil.which("python3"):
        r = run("result", "{not json")
        assert r.returncode == 2 and "not valid JSON" in r.stderr
        assert json.loads((io / "out/result.json").read_text()) == {"a": 1}  # the good one stays
    assert run("result", "-", stdin='[1, 2]').returncode == 0
    assert json.loads((io / "out/result.json").read_text()) == [1, 2]
    assert run("done", "All good.").returncode == 0 and (io / "RESULT.md").read_text() == "All good.\n"
    r = run("progress", "x", env_io=False)
    assert r.returncode == 2 and "HANGAR_IO is not set" in r.stderr
    assert run("help").returncode == 0 and run("bogus").returncode == 2


def test_helper_artifact(helper):
    run, io, tmp = helper
    (tmp / "report.md").write_text("# r\n")
    (tmp / "dir").mkdir()
    (tmp / "dir/a.txt").write_text("a")
    assert run("artifact", "report.md").returncode == 0
    assert run("artifact", "dir", "screens/today").returncode == 0
    assert (io / "out/artifacts/report.md").read_text() == "# r\n"
    assert (io / "out/artifacts/screens/today/a.txt").read_text() == "a"
    assert run("artifact", "report.md").returncode == 0  # replacing is fine
    for bad in ["../escape", "/abs", "a/../../b", "a/./b"]:
        r = run("artifact", "report.md", bad)
        assert r.returncode == 2 and "relative path" in r.stderr, bad
    assert not (io / "escape").exists() and not (io / "out/escape").exists()
    assert run("artifact", "missing.txt").returncode == 2
    # already in place (the agent wrote straight into out/artifacts)
    assert run("artifact", str(io / "out/artifacts/report.md")).returncode == 0
    assert (io / "out/artifacts/report.md").read_text() == "# r\n"
