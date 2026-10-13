"""Live turns (workstream steer): steer, interrupt and tool approvals, end to end through real tmux and the real
driver, with scripted harnesses that speak Claude Code's stream-json and Codex's app-server protocols as verified
against claude 2.1.296 and codex 0.162.1 (tests/fixtures/live)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from hangar import steer as steering
from hangar.harnesses import HARNESSES, Harness
from hangar.models import SessionState
from hangar.sessions import NewSession, SessionError, SessionService
from hangar.vault import Vault
from tests.conftest import scratch_root

pytestmark = pytest.mark.skipif(not (shutil.which("tmux") and shutil.which("git") and shutil.which("python3")),
                                reason="needs tmux, git and python3")

FAKES = Path(__file__).parent / "fixtures" / "live"
SESSION_FLAGS = ('${HANGAR_SESSION_NEW:+--session-id "$HANGAR_SESSION_NEW"} '
                 '${HANGAR_SESSION_RESUME:+--resume "$HANGAR_SESSION_RESUME"} '
                 '${HANGAR_SESSION_FORK:+--resume "$HANGAR_SESSION_FORK" --fork-session}')
# The one-shot fallback (no driver): works for a while on SLOW, then prints a stream-json result.
ONE_SHOT = r"""#!/usr/bin/env bash
asked="$(awk 'f&&NF{print;exit} /^# Now$/{f=1}' "$HANGAR_PROMPT_FILE")"  # a handoff prompt: the new message
[ -n "$asked" ] || asked="$(head -1 "$HANGAR_PROMPT_FILE")"
case "$asked" in SLOW*) sleep 30 ;; esac
echo "{\"type\":\"result\",\"result\":\"one-shot: $asked\"}"
"""
KEY = b"0" * 43 + b"="


@pytest.fixture
def live(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="steer-", dir=scratch_root()))
    remote, seed = root / "remote.git", root / "seed"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    subprocess.run([*git, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    oneshot = root / "oneshot.sh"
    oneshot.write_text(ONE_SHOT)
    monkeypatch.setitem(HARNESSES, "live-claude", Harness(
        name="live-claude", title="Claude (scripted)", run=f"bash {oneshot}", live="claude",
        live_run=f"python3 {FAKES}/fake_claude.py --model x {SESSION_FLAGS}", session_dirs=[".claude/projects"],
        resume="preset", fork=True))
    monkeypatch.setitem(HARNESSES, "live-codex", Harness(
        name="live-codex", title="Codex (scripted)", run=f"bash {oneshot}", live="codex",
        live_run=f"python3 {FAKES}/fake_codex.py", session_dirs=[".codex/sessions"], resume="capture",
        session_id_regex=HARNESSES["codex"].session_id_regex, fork=True))
    monkeypatch.setitem(HARNESSES, "one-shot", Harness(name="one-shot", title="One-shot", run=f"bash {oneshot}"))
    envs = root / "envs"
    envs.mkdir()
    (envs / "liveenv.yaml").write_text("name: liveenv\nharnesses: [live-claude, live-codex, one-shot]\n"
                                       "default_harness: live-claude\n")
    yield {"root": root, "envs": envs, "url": f"file://{remote}"}
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def svc(fleet, live, settings, store):
    settings.session_poll_seconds = 0.1
    fleet.catalog.dirs.insert(0, live["envs"])
    fleet.catalog.reload()
    s = SessionService(fleet, Vault(store, KEY))
    await s.start()
    yield s
    await s.stop()
    for box in fleet.providers["docker"].boxes.values():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


async def settle(svc: SessionService, ref: str, timeout: float = 30.0):
    for _ in range(int(timeout / 0.05)):
        s = svc.get(ref)
        if s.state.value in ("idle", "suspended", "failed") and not svc.busy(s.id):
            return s
        await asyncio.sleep(0.05)
    s = svc.get(ref)
    raise AssertionError(f"{ref} stuck in {s.state}: {s.reason}")


async def running(svc: SessionService, ref: str, timeout: float = 20.0):
    """Wait until the session's turn runs under its driver (the inbox exists)."""
    for _ in range(int(timeout / 0.05)):
        s = svc.get(ref)
        t = s.turns[-1] if s.turns else None
        if t and t.run_id and s.state == SessionState.RUNNING and s.sandbox_id:
            if not t.live or inbox(svc, s, t).is_dir():
                return s
        await asyncio.sleep(0.05)
    raise AssertionError(f"{ref} never started a turn: {svc.get(ref).reason}")


def inbox(svc: SessionService, s, t) -> Path:
    box = svc.fleet.providers["docker"].boxes[s.sandbox_id]
    return box / steering.ctl_dir(svc, t.run_id).lstrip("/") / "in"


async def pending_approval(svc: SessionService, ref: str, timeout: float = 20.0):
    for _ in range(int(timeout / 0.05)):
        if p := steering.pending(svc.get(ref)):
            return p[0]
        await asyncio.sleep(0.05)
    raise AssertionError(f"no approval request in {ref}: {svc.events(ref, tail=10)}")


def repo_file(svc: SessionService, s, name: str) -> Path:
    sb = svc.fleet.get(s.sandbox_id)
    return svc.fleet.providers["docker"].boxes[sb.id] / sb.info["repo_path"].lstrip("/") / name


def new(svc: SessionService, live, name: str, message: str, harness: str = "live-claude", **kw):
    return svc.new(NewSession(name=name, environment="liveenv", repo=live["url"], message=message, harness=harness,
                              **kw), owner="admin", requester="sean")


# ---------------------------------------------------------------------------- steer
@pytest.mark.parametrize("harness", ["live-claude", "live-codex"])
async def test_steer_mid_turn(svc, live, harness):
    new(svc, live, "steer", "SLOW build the thing", harness)
    s = await running(svc, "steer")
    assert s.turns[-1].live and s.turns[-1].approval_policy == "auto"
    svc.send("steer", "also add a test", requester="ana")  # a send to a running live turn steers it
    s = await settle(svc, "steer")
    assert len(s.turns) == 1, [t.message for t in s.turns]
    t = s.turns[0]
    assert t.outcome == "exited" and t.exit_code == 0, (t.outcome, t.summary)
    assert t.summary == "did: SLOW build the thing | steered: also add a test", t.summary
    assert [(x.text, x.status, x.requester) for x in t.steers] == [("also add a test", "delivered", "ana")]
    texts = [e["text"] for e in svc.events("steer")]
    assert "steered: also add a test" in texts, texts
    assert t.usage and t.usage.tokens > 0  # the driver's result lines keep usage collection working
    # the next turn resumes the same conversation, under the driver again
    svc.send("steer", "now the docs", requester="ana")
    s = await settle(svc, "steer")
    assert s.turns[1].mode == "resume" and s.turns[1].summary == "did: now the docs" and s.turns[1].live


async def test_steer_api_and_a_late_steer_starts_the_next_turn(svc, live):
    new(svc, live, "late", "SLOW work")
    s = await running(svc, "late")
    st = await steering.steer(svc, s, "focus on the parser", "ana")
    assert st.status == "delivered"
    s = await settle(svc, "late")
    # the turn is over: a steer now is refused, and a send starts a new turn as before
    with pytest.raises(steering.SteerError, match="no running turn"):
        await steering.steer(svc, s, "too late", "ana")
    # the race: the message comes while the turn is stopping. Either the driver still takes it (and carries on
    # with it) or it has closed its inbox and the message starts the next turn; it is never lost.
    svc.send("late", "SLOW second", requester="ana")
    s = await running(svc, "late")
    t = s.turns[-1]
    await steering.interrupt(svc, s, "ana")
    svc.send("late", "after the end", requester="ana")
    s = await settle(svc, "late")
    st = next(x for x in t.steers if x.kind == "steer")
    assert st.status in ("queued", "delivered"), st
    assert s.turns[-1].summary == "did: after the end", [(x.message, x.summary, x.outcome) for x in s.turns]
    assert len(s.turns) == (2 if st.status == "delivered" else 3)


# ---------------------------------------------------------------------------- interrupt
@pytest.mark.parametrize("harness", ["live-claude", "live-codex"])
async def test_interrupt_then_continue(svc, live, harness):
    new(svc, live, "stop", "SLOW refactor everything", harness)
    s = await running(svc, "stop")
    await steering.interrupt(svc, s, "ana")
    s = await settle(svc, "stop")
    t = s.turns[0]
    assert (t.outcome, t.exit_code) == ("interrupted", 130), (t.outcome, t.exit_code, t.summary)
    assert s.state == SessionState.IDLE
    svc.send("stop", "just fix the bug", requester="ana")  # continue: the same conversation
    s = await settle(svc, "stop")
    assert s.turns[1].mode == "resume" and s.turns[1].summary == "did: just fix the bug"


@pytest.mark.parametrize("harness", ["live-claude", "live-codex"])
async def test_interrupt_with_a_message_carries_on_in_the_same_turn(svc, live, harness):
    new(svc, live, "redirect", "SLOW the wrong thing", harness)
    s = await running(svc, "redirect")
    st = await steering.interrupt(svc, s, "ana", "do the right thing")
    assert st.status == "delivered" and st.kind == "interrupt"
    s = await settle(svc, "redirect")
    assert len(s.turns) == 1
    t = s.turns[0]
    assert (t.outcome, t.exit_code, t.summary) == ("exited", 0, "did: do the right thing"), t.summary


# ---------------------------------------------------------------------------- approvals
@pytest.mark.parametrize("harness", ["live-claude", "live-codex"])
async def test_approval_allow(svc, live, harness):
    new(svc, live, "ask", "TOOL make the file", harness, approvals="ask")
    a = await pending_approval(svc, "ask")
    s = svc.get("ask")
    assert s.turns[0].approval_policy == "ask" and a.status == "pending" and "touch tool-ran.txt" in a.detail
    assert svc.view(s)["pending_approvals"][0]["id"] == a.id  # get_session shows it
    assert not repo_file(svc, s, "tool-ran.txt").exists()
    done = await steering.answer(svc, s, a.id, "allow", "ana")
    assert (done.status, done.by) == ("allowed", "ana")
    s = await settle(svc, "ask")
    assert repo_file(svc, s, "tool-ran.txt").exists()
    t = s.turns[0]
    assert t.outcome == "exited" and [x.status for x in t.approvals] == ["allowed"]
    kinds = [(e["kind"], e["data"].get("status")) for e in svc.events("ask") if e["kind"] == "approval_request"]
    assert kinds == [("approval_request", "pending"), ("approval_request", "allowed")], kinds
    with pytest.raises(steering.SteerError):  # the turn is over
        await steering.answer(svc, s, a.id, "allow", "ana")


@pytest.mark.parametrize("harness", ["live-claude", "live-codex"])
async def test_approval_deny_lets_the_turn_go_on(svc, live, harness):
    new(svc, live, "deny", "TOOL make the file", harness, approvals="ask")
    a = await pending_approval(svc, "deny")
    s = svc.get("deny")
    with pytest.raises(steering.SteerError, match="decision"):
        await steering.answer(svc, s, a.id, "maybe", "ana")
    with pytest.raises(KeyError):
        await steering.answer(svc, s, "ap-000000000000", "deny", "ana")
    await steering.answer(svc, s, a.id, "deny", "ana", "not in this repo")
    with pytest.raises(steering.SteerError, match="already answered"):
        await steering.answer(svc, s, a.id, "allow", "mallory")
    s = await settle(svc, "deny")
    t = s.turns[0]
    assert t.outcome == "exited" and t.exit_code == 0 and t.summary == "did: TOOL make the file"
    assert not repo_file(svc, s, "tool-ran.txt").exists()
    assert (t.approvals[0].status, t.approvals[0].message) == ("denied", "not in this repo")


async def test_unanswered_approval_is_denied_after_the_timeout(svc, live, settings):
    settings.approval_timeout_seconds = 1
    new(svc, live, "late-answer", "TOOL make the file", approvals="ask")
    s = await settle(svc, "late-answer")
    t = s.turns[0]
    assert t.outcome == "exited" and not repo_file(svc, s, "tool-ran.txt").exists()
    assert [(a.status, a.by) for a in t.approvals] == [("denied", "timeout")]


async def test_deny_risky_answers_without_asking(svc, live, settings):
    settings.approvals = "deny-risky"  # the deployment default; the session sets nothing
    new(svc, live, "risky", "TOOL make the file", harness="live-codex")
    s = await settle(svc, "risky")
    t = s.turns[0]
    assert t.approval_policy == "deny-risky" and repo_file(svc, s, "tool-ran.txt").exists()
    assert [(a.status, a.by) for a in t.approvals] == [("allowed", "policy")]


async def test_allow_always_is_for_this_conversation_only(svc, live):
    new(svc, live, "always", "TOOL make the file", approvals="ask")
    a = await pending_approval(svc, "always")
    await steering.answer(svc, svc.get("always"), a.id, "allow_always", "ana")
    s = await settle(svc, "always")
    box = svc.fleet.providers["docker"].boxes[s.sandbox_id]
    conv = next((box / "home/agent/.claude/projects/fake").glob("*.jsonl")).read_text()
    assert '"destination": "session"' in conv and "localSettings" not in conv
    assert s.turns[0].approvals[0].status == "allowed_always"


# ---------------------------------------------------------------------------- restart
async def test_restart_mid_turn_reattaches_and_takes_the_answer(fleet, live, settings, store, svc):
    new(svc, live, "restart", "TOOL make the file", approvals="ask")
    a = await pending_approval(svc, "restart")
    await svc.stop()  # the control plane goes away; the turn keeps waiting in its sandbox
    svc2 = SessionService(fleet, Vault(store, KEY))
    await svc2.start()
    try:
        s = svc2.get("restart")
        assert svc2.busy(s.id) and [x.id for x in steering.pending(s)] == [a.id]
        await steering.answer(svc2, s, a.id, "allow", "ana")  # the key survived the restart
        s = await settle(svc2, "restart")
        assert s.turns[0].outcome == "exited" and repo_file(svc2, s, "tool-ran.txt").exists()
        assert store.kv_get(steering.KV + s.turns[0].run_id) is None  # forgotten with the turn
    finally:
        await svc2.stop()


# ---------------------------------------------------------------------------- fallback and safety
async def test_one_shot_harness_falls_back(svc, live):
    new(svc, live, "plain", "SLOW work", harness="one-shot")
    s = await running(svc, "plain")
    assert not s.turns[0].live
    with pytest.raises(SessionError, match="busy"):  # no steering into a one-shot turn
        svc.send("plain", "more", requester="ana")
    with pytest.raises(steering.NotLive):
        await steering.steer(svc, s, "more", "ana")
    st = await steering.interrupt(svc, s, "ana", "do this instead")  # killed; the message is the next turn
    assert st.status == "queued"
    s = await settle(svc, "plain")
    assert (s.turns[0].outcome, s.turns[0].exit_code) == ("interrupted", 130)
    assert s.turns[1].message == "do this instead" and s.turns[1].summary == "one-shot: do this instead"


async def test_ask_needs_a_live_harness(svc, live, settings):
    new(svc, live, "noprompt", "work", harness="one-shot", approvals="ask")
    s = await settle(svc, "noprompt")
    assert "approvals=ask needs a live turn" in (s.reason or ""), s.reason
    settings.live_turns = False  # turned off: auto falls back to one-shot even for live harnesses
    new(svc, live, "off", "work")
    s = await settle(svc, "off")
    assert not s.turns[0].live and s.turns[0].summary == "one-shot: work"


async def test_forged_commands_are_refused(svc, live):
    new(svc, live, "forge", "TOOL make the file", approvals="ask")
    a = await pending_approval(svc, "forge")
    s = svc.get("forge")
    t = s.turns[0]
    ctl = inbox(svc, s, t).parent
    assert not (ctl / "key").exists()  # the driver read its key and deleted it
    # something else in the sandbox writes an approval without the key: refused, and the agent keeps waiting
    (ctl / "in" / "c-forged.json").write_text(
        '{"id": "c-forged", "op": "approve", "args": {"request": "%s", "decision": "allow"}, "mac": "00"}' % a.id)
    for _ in range(40):
        if (ctl / "acks").exists() and "c-forged" in (ctl / "acks").read_text():
            break
        await asyncio.sleep(0.05)
    assert '"error": "bad signature"' in (ctl / "acks").read_text()
    assert steering.pending(svc.get("forge"))[0].id == a.id
    await steering.answer(svc, s, a.id, "deny", "ana")
    await settle(svc, "forge")
