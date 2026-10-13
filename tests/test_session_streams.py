"""Sessions (spec 0010): checkpoint, suspend and resume across sandboxes, on the fake provider."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from hangar.harnesses import HARNESSES, Harness
from hangar.models import SessionState
from hangar.sessions import NewSession, SessionError, SessionService, extract_summary
from hangar.vault import Vault

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

# A scripted "agent" that keeps a conversation file like Claude Code / pi do, and works on the repo.
AGENT = r"""#!/usr/bin/env bash
set -e
conv="$HOME/.fake/sessions"; mkdir -p "$conv"
id="${HANGAR_SESSION_RESUME:-$HANGAR_SESSION_NEW}"
[ -n "$HANGAR_THREAD" ] && { id="$HANGAR_THREAD"; echo "{\"type\":\"thread.started\",\"thread_id\":\"$id\"}"; }
if [ -n "$HANGAR_SESSION_RESUME" ]; then
  [ -f "$conv/$id.jsonl" ] || { echo "no conversation $id"; exit 7; }
  echo "resumed $id with $(wc -l < "$conv/$id.jsonl") earlier messages"
fi
msg="$(cat "$HANGAR_PROMPT_FILE")"
[ -n "$id" ] && echo "$msg" | head -1 >> "$conv/$id.jsonl"
case "$msg" in
  *"start"*)
    echo one > a.txt && git add a.txt && git commit -qm "feat: a"
    echo "changed" >> README.md            # uncommitted
    echo "scratch" > notes.txt              # untracked
    mkdir -p .venv && echo big > .venv/x    # ignored: must not be checkpointed
    ;;
  *"continue"*)
    test -f a.txt && grep -q changed README.md && test -f notes.txt
    test ! -e .venv/x
    echo two > b.txt && git add -A && git commit -qm "feat: b"
    ;;
esac
echo "{\"type\":\"result\",\"result\":\"did: $(echo "$msg" | head -1)\"}"
"""


@pytest.fixture
def world(monkeypatch):
    base = Path.home() / ".cache" / "hangar-tests"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="ses-", dir=base))
    remote, seed = root / "remote.git", root / "seed"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    (seed / ".gitignore").write_text(".venv/\n")
    subprocess.run([*git, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    agent = root / "agent.sh"
    agent.write_text(AGENT)
    run = f"bash {agent}"
    monkeypatch.setitem(HARNESSES, "preset-agent", Harness(
        name="preset-agent", title="Preset", run=run, session_dirs=[".fake/sessions"], resume="preset",
        resume_hint="agent --resume {id}"))
    monkeypatch.setitem(HARNESSES, "capture-agent", Harness(
        name="capture-agent", title="Capture", run=f"HANGAR_THREAD=${{HANGAR_SESSION_RESUME:-th-0123456789}} {run}",
        session_dirs=[".fake/sessions"], resume="capture", session_id_regex=r'"thread_id"\s*:\s*"([0-9A-Za-z-]{8,})"'))
    monkeypatch.setitem(HARNESSES, "plain-agent", Harness(name="plain-agent", title="Plain", run=run))
    envs = root / "envs"
    envs.mkdir()
    (envs / "sesenv.yaml").write_text(
        "name: sesenv\nharnesses: [preset-agent, capture-agent, plain-agent]\ndefault_harness: preset-agent\n")
    yield {"root": root, "remote": remote, "envs": envs, "url": f"file://{remote}"}
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def svc(fleet, world, settings, store):
    settings.session_poll_seconds = 0.1
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    s = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    await s.start()
    yield s
    await s.stop()
    for box in fleet.providers["docker"].boxes.values():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


async def settle(svc: SessionService, ref: str, states=frozenset({"idle", "suspended", "failed"}), timeout=20.0):
    for _ in range(int(timeout / 0.05)):
        s = svc.get(ref)
        if s.state.value in states and s.id not in svc._busy:
            return s
        await asyncio.sleep(0.05)
    s = svc.get(ref)
    raise AssertionError(f"{ref} stuck in {s.state}: {s.reason}")


def box(fleet, sandbox_id) -> Path:
    return fleet.providers["docker"].boxes[sandbox_id]


async def test_resume_restores_conversation_commits_and_changes(svc, world, fleet):
    s = svc.new(NewSession(name="csv-fix", environment="sesenv", repo=world["url"], message="start the work"),
                owner="admin", requester="sean")
    s = await settle(svc, "csv-fix")
    assert s.state == SessionState.IDLE, s.reason
    first_sb = s.sandbox_id
    t1 = s.turns[0]
    assert t1.mode == "new" and t1.exit_code == 0 and t1.summary == "did: start the work"
    assert s.harness_session and s.harness_session_for == "preset-agent"
    ck = s.checkpoints[-1]
    assert ck.reason == "turn" and {"agent.tgz", "repo.bundle", "wip.patch", "untracked.tgz"} <= set(ck.parts)
    # checkpoints are encrypted at rest
    raw = (svc.root / s.id / ck.id / "wip.patch.enc").read_bytes()
    assert b"changed" not in raw

    # suspend: checkpoint + sandbox released
    svc.suspend("csv-fix", requester="sean")
    s = await settle(svc, "csv-fix")
    assert s.state == SessionState.SUSPENDED and s.sandbox_id is None
    assert fleet.get(first_sb).state == "stopped"

    # days later: send resumes in a new sandbox with everything restored, and the harness resumes its conversation
    svc.send("csv-fix", "continue with b", requester="sean")
    s = await settle(svc, "csv-fix")
    assert s.state == SessionState.IDLE, s.reason
    assert s.sandbox_id and s.sandbox_id != first_sb
    t2 = s.turns[1]
    assert t2.mode == "resume" and t2.exit_code == 0, t2.summary
    repo = box(fleet, s.sandbox_id) / "workspace" / "remote"
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.splitlines()[:3] == ["feat: b", "feat: a", "init"]
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip() == "hangar/session/csv-fix"
    conv = box(fleet, s.sandbox_id) / "home/agent/.fake/sessions" / f"{s.harness_session}.jsonl"
    assert conv.read_text().splitlines() == ["start the work", "continue with b"]
    run_log = next((box(fleet, s.sandbox_id) / "home/agent/.hangar/runs").glob("*.log")).read_text()
    assert "resumed" in run_log and "1 earlier messages" in run_log
    v = svc.view(s)
    assert v["resume_command"] == f"agent --resume {s.harness_session}" and v["last_summary"] == "did: continue with b"


async def test_destroy_from_outside_checkpoints_first(svc, world, fleet):
    svc.new(NewSession(name="hook", environment="sesenv", repo=world["url"], message="start"),
            owner="admin", requester="sean")
    s = await settle(svc, "hook")
    n = len(s.checkpoints)
    sb = s.sandbox_id
    (box(fleet, sb) / "workspace/remote/late.txt").write_text("written after the last turn\n")
    await fleet.destroy(sb, actor="reaper")  # e.g. TTL expiry
    s = svc.get("hook")
    assert s.state == SessionState.SUSPENDED and s.sandbox_id is None
    assert len(s.checkpoints) == n + 1 and s.checkpoints[-1].reason == "destroy"
    svc.open("hook", requester="sean")
    s = await settle(svc, "hook")
    assert (box(fleet, s.sandbox_id) / "workspace/remote/late.txt").exists()


async def test_idle_suspend_and_interval_checkpoint(svc, world, fleet, settings):
    svc.new(NewSession(name="idle", environment="sesenv", repo=world["url"], idle_minutes=5), owner="admin",
            requester="sean")
    s = await settle(svc, "idle")
    assert s.state == SessionState.IDLE and not s.turns
    # changed + checkpoint interval elapsed -> interval checkpoint; unchanged -> skipped
    (box(fleet, s.sandbox_id) / "workspace/remote/wip.txt").write_text("x\n")
    s.created_at -= 3600
    await svc.tick()
    assert [c.reason for c in s.checkpoints] == ["interval"]
    s.checkpoints[-1].ts -= 3600
    await svc.tick()
    assert len(s.checkpoints) == 1  # nothing changed since
    # idle past idle_minutes -> suspended
    s.last_active_at -= 6 * 60
    await svc.tick()
    s = await settle(svc, "idle")
    assert s.state == SessionState.SUSPENDED and s.sandbox_id is None


async def test_capture_harness_and_handoff(svc, world, fleet):
    svc.new(NewSession(name="cap", environment="sesenv", repo=world["url"], harness="capture-agent",
                       message="start"), owner="admin", requester="sean")
    s = await settle(svc, "cap")
    assert s.harness_session == "th-0123456789" and s.turns[0].mode == "new"
    svc.send("cap", "more please", requester="sean")
    s = await settle(svc, "cap")
    assert s.turns[1].mode == "resume" and s.turns[1].exit_code == 0

    # a harness without native resume gets a summary handoff on later turns
    svc.new(NewSession(name="plain", environment="sesenv", repo=world["url"], harness="plain-agent",
                       message="start"), owner="admin", requester="sean")
    s = await settle(svc, "plain")
    svc.send("plain", "more please", requester="sean")
    s = await settle(svc, "plain")
    assert s.turns[1].mode == "handoff"
    runs = box(fleet, s.sandbox_id) / "home/agent/.hangar/runs"
    prompts = sorted(runs.glob("*.prompt"), key=lambda p: p.stat().st_mtime)
    text = prompts[-1].read_text()
    assert "Earlier in this session" in text and "did: start" in text and "more please" in text


async def test_validation_and_restart(svc, world, fleet, store, settings):
    with pytest.raises(SessionError):
        svc.new(NewSession(name="Bad Name", environment="sesenv"), owner="a", requester="a")
    with pytest.raises(SessionError, match="environment"):
        svc.new(NewSession(name="noenv"), owner="a", requester="a")
    svc.new(NewSession(name="dup", environment="sesenv", repo=world["url"]), owner="a", requester="a")
    with pytest.raises(SessionError, match="already exists"):
        svc.new(NewSession(name="dup", environment="sesenv"), owner="a", requester="a")
    s = await settle(svc, "dup")
    with pytest.raises(SessionError, match="suspend"):
        svc.archive("dup", requester="a")
    # a control-plane restart keeps live sessions attached to their sandboxes
    again = SessionService(fleet, svc.vault)
    await again.start()
    try:
        assert again.get("dup").state == SessionState.IDLE and again.get("dup").sandbox_id == s.sandbox_id
    finally:
        await again.stop()


def test_extract_summary():
    claude = '{"type":"system"}\n{"type":"result","result":"Fixed it."}\n'
    codex = ('{"type":"thread.started","thread_id":"t1"}\n'
             '{"type":"item.completed","item":{"type":"agent_message","text":"Codex done."}}\n')
    pi = json.dumps({"type": "message_end", "message": {"role": "assistant",
                                                        "content": [{"type": "text", "text": "pi done"}]}})
    assert extract_summary("claude-code", claude) == "Fixed it."
    assert extract_summary("codex", codex) == "Codex done."
    assert extract_summary("pi", pi) == "pi done"
    assert extract_summary(None, "plain output\nlast line") .endswith("last line")
