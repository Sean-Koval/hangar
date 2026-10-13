"""Live turns (workstream steer): steer, interrupt and tool approvals, end to end through real tmux and the real
driver, with scripted harnesses that speak Claude Code's stream-json and Codex's app-server protocols as verified
against claude 2.1.296 and codex 0.162.1 (tests/fixtures/live)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
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
held="$(dirname "$HANGAR_PROMPT_FILE")/held"  # HOLD (a job's task): the first run works for a while
grep -q HOLD "$HANGAR_PROMPT_FILE" && [ ! -e "$held" ] && { touch "$held"; sleep 30; }
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
    # refused either way; after a deny the turn goes on and may already be over (a loaded machine)
    with pytest.raises(steering.SteerError, match="already answered|no running turn"):
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


async def test_allow_always_is_never_written_to_settings(svc, live):
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
    forged = {"id": "c-forged", "op": "approve", "args": {"request": a.id, "decision": "allow"}, "mac": "00"}
    (ctl / "in" / "c-forged.json").write_text(json.dumps(forged))
    for _ in range(40):
        if (ctl / "acks").exists() and "c-forged" in (ctl / "acks").read_text():
            break
        await asyncio.sleep(0.05)
    assert '"error": "bad signature"' in (ctl / "acks").read_text()
    assert steering.pending(svc.get("forge"))[0].id == a.id
    await steering.answer(svc, s, a.id, "deny", "ana")
    await settle(svc, "forge")


# ---------------------------------------------------------------------------- review: races, tampering, jobs
def driver_proc(svc: SessionService, s, t) -> int:
    """The pid of the turn's driver (by its --ctl argument)."""
    want = str(inbox(svc, s, t).parent).encode()
    for p in Path("/proc").iterdir():
        try:
            argv = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"driver.py") for a in argv) and want in argv:
            return int(p.name)
    raise AssertionError(f"no driver for {want!r}")


async def test_a_fork_keeps_the_sessions_approval_policy(svc, live):
    from hangar import strategies

    new(svc, live, "careful", "work", approvals="ask")
    await settle(svc, "careful")
    child = (await strategies.fork_session(svc, "careful", 1, "ana", message="TOOL make the file"))[0]
    a = await pending_approval(svc, child.name)  # asked, not run with permission checks off
    c = svc.get(child.name)
    assert c.approvals == "ask" and c.turns[-1].approval_policy == "ask"
    assert not repo_file(svc, c, "tool-ran.txt").exists()
    await steering.answer(svc, c, a.id, "deny", "ana")
    await settle(svc, child.name)


async def test_a_forged_acknowledgement_is_not_believed(svc, live):
    """Something in the sandbox (the agent's uid) swallows hangar's interrupt and writes `ok` in its place: hangar
    must not report the turn interrupted while it runs on. The driver signs its answers; an interrupt it does not
    confirm stops the run outright."""
    new(svc, live, "swallow", "SLOW keep going")
    s = await running(svc, "swallow")
    t = s.turns[0]
    box_in = inbox(svc, s, t)
    pid = driver_proc(svc, s, t)
    os.kill(pid, signal.SIGSTOP)  # the agent wins the race for the command file
    try:
        async def swallow() -> None:
            while True:
                for f in box_in.glob("*.json"):
                    f.unlink()
                    with (box_in.parent / "acks").open("a") as acks:
                        acks.write(json.dumps({"id": f.name[:-5], "ok": True, "ts": 1}) + "\n")
                    return
                await asyncio.sleep(0.01)

        thief = asyncio.create_task(swallow())
        await steering.interrupt(svc, s, "ana")
        await thief
    finally:
        # The kill may already have ended it: a stopped process left in an orphaned group gets SIGHUP + SIGCONT
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)
    s = await settle(svc, "swallow")
    assert (s.turns[0].outcome, s.turns[0].exit_code) == ("interrupted", 130), (s.turns[0].outcome, s.turns[0].summary)


async def test_a_steer_that_meets_a_harness_that_just_exited_starts_the_next_turn(svc, live):
    """The harness exits on its own while a message is landing: the driver must not take it (nothing would read
    it), and hangar must start the next turn with it rather than call it failed."""
    new(svc, live, "exits", "SLOW work")
    s = await running(svc, "exits")
    t = s.turns[0]
    pid = driver_proc(svc, s, t)
    harness = int(subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()[0])
    os.kill(pid, signal.SIGSTOP)
    try:
        svc.send("exits", "the follow-up", requester="ana")  # a steer, on its way into the turn
        for _ in range(100):
            if list(inbox(svc, s, t).glob("*.json")):
                break
            await asyncio.sleep(0.05)
        os.kill(harness, signal.SIGKILL)  # the harness goes away by itself
        await asyncio.sleep(0.2)
    finally:
        os.kill(pid, signal.SIGCONT)
    s = await settle(svc, "exits")
    st = s.turns[0].steers[0]
    assert st.status == "queued", st
    assert [x.message for x in s.turns] == ["SLOW work", "the follow-up"]
    assert s.turns[1].summary == "did: the follow-up"


async def test_the_driver_does_not_load_modules_planted_next_to_it(svc, live):
    """~/.hangar is the agent's to write: a json.py there must not run inside the driver (python3 -I)."""
    svc.new(NewSession(name="planted", environment="liveenv", repo=live["url"]), owner="admin", requester="sean")
    s = await settle(svc, "planted")
    box = svc.fleet.providers["docker"].boxes[s.sandbox_id]
    (box / "home/agent/.hangar/json.py").write_text("raise SystemExit('planted module ran')\n")
    svc.send("planted", "work", requester="ana")
    s = await settle(svc, "planted")
    assert (s.turns[0].outcome, s.turns[0].exit_code, s.turns[0].summary) == ("exited", 0, "did: work")


@pytest.fixture
async def jobsvc(svc, fleet, settings):
    from hangar.jobs import JobService

    settings.job_poll_seconds = 0.1
    j = JobService(fleet, svc)
    await j.start()
    yield j
    await j.stop()


async def job_turn(jobsvc, svc: SessionService, job_id: str):
    for _ in range(400):
        s = next((x for x in svc._sessions.values() if x.job_id == job_id), None)
        if s is not None and s.turns and s.turns[-1].run_id and s.state == SessionState.RUNNING:
            t = s.turns[-1]
            if not t.live or inbox(svc, s, t).is_dir():
                return s
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never ran a turn: {[e.message for e in jobsvc.get(job_id).events]}")


async def job_until(jobsvc, job_id: str, states, timeout: float = 30):
    for _ in range(int(timeout / 0.05)):
        job = jobsvc.get(job_id)
        if job.state in states:
            return job
        await asyncio.sleep(0.05)
    job = jobsvc.get(job_id)
    raise AssertionError(f"{job_id} stuck in {job.state}: {[e.message for e in job.events]}")


async def test_interrupting_a_job_pauses_it_for_the_next_instruction(svc, live, jobsvc):
    """Stopping a job's turn is a person taking over, not a failure: no verify, no draft delivered; the job waits
    (needs_input) for what to do instead, and a follow-up carries on in the same conversation."""
    from hangar.jobs import Caller, SubmitJob
    from hangar.models import JobState

    job = jobsvc.submit(SubmitJob(instructions="SLOW rewrite the parser", repo=live["url"], environment="liveenv"),
                        Caller("svc", "svc"))
    s = await job_turn(jobsvc, svc, job.id)
    await steering.interrupt(svc, s, "ana")
    job = await job_until(jobsvc, job.id, {JobState.NEEDS_INPUT, JobState.FAILED, JobState.SUCCEEDED})
    assert job.state == JobState.NEEDS_INPUT, [e.message for e in job.events]
    assert "interrupted by ana" in (job.result.question or "").lower()
    assert job.turns[0].outcome == "interrupted" and not job.result.pushed
    jobsvc.follow_up(job.id, "only fix the bug in parse()", "ana")
    job = await job_until(jobsvc, job.id, {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})
    assert job.state == JobState.SUCCEEDED and len(job.turns) == 2, [e.message for e in job.events]


async def test_a_one_shot_job_interrupted_with_a_message_carries_on_with_it(svc, live, jobsvc):
    from hangar.jobs import Caller, SubmitJob
    from hangar.models import JobState

    job = jobsvc.submit(SubmitJob(instructions="HOLD the wrong thing", repo=live["url"], environment="liveenv",
                                  harness="one-shot"), Caller("svc", "svc"))
    s = await job_turn(jobsvc, svc, job.id)
    assert not s.turns[-1].live
    st = await steering.interrupt(svc, s, "ana", "do this instead")
    assert st.status == "queued"
    job = await job_until(jobsvc, job.id, {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
                                           JobState.NEEDS_INPUT})
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert [t.message for t in job.turns][1:] == ["do this instead"] and job.turns[0].outcome == "interrupted"
