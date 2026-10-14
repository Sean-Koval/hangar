"""Forks and best-of-N jobs (spec 0010 M5, 0009 M3) on the fake provider, with scripted agents and real git."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest

from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, JobError, JobService, SubmitJob
from hangar.models import JobState, SessionState
from hangar.sessions import NewSession, SessionError, SessionService
from hangar.strategies import fork_session, touched_tests
from hangar.vault import Vault
from tests.conftest import scratch_root
from tests.test_jobs import DONE, Hooks, _until

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

# A scripted agent that parses the *real* Claude Code and Codex argument strings (see the harnesses below) and keeps
# conversations on disk the way they do: --session-id/--resume/--fork-session, or `resume <id>` / `fork <id>`.
AGENT = r"""#!/usr/bin/env bash
set -e
echo "argv: $*"
conv="$HOME/.fake/sessions"; mkdir -p "$conv"
mode=new; id=""; src=""
while [ $# -gt 0 ]; do
  case "$1" in
    --session-id) id="$2"; shift 2 ;;
    --resume) src="$2"; [ "$mode" = fork ] || mode=resume; shift 2 ;;
    --fork-session) mode=fork; shift ;;
    resume) src="$2"; mode=resume; shift 2 ;;
    fork) src="$2"; mode=fork; shift 2 ;;
    *) shift ;;
  esac
done
case "$mode" in
  resume) [ -f "$conv/$src.jsonl" ] || { echo "no conversation $src"; exit 7; }; id="$src" ;;
  fork) [ -f "$conv/$src.jsonl" ] || { echo "no conversation $src"; exit 7; }
        id="${id:-th-$(od -An -N4 -tx4 /dev/urandom | tr -d ' ')}"
        cp "$conv/$src.jsonl" "$conv/$id.jsonl"
        echo "forked $src into $id" ;;
  new) id="${id:-th-$(od -An -N4 -tx4 /dev/urandom | tr -d ' ')}" ;;
esac
[ "$CAPTURE" = 1 ] && echo "{\"type\":\"thread.started\",\"thread_id\":\"$id\"}"
echo "$mode $(wc -l < "$conv/$id.jsonl" 2>/dev/null || echo 0) earlier messages"
msg="$(cat "$HANGAR_PROMPT_FILE")"
echo "$msg" | head -1 >> "$conv/$id.jsonl"
case "$msg" in
  *"start"*)
    echo one > a.txt && git add a.txt && git commit -qm "feat: a"
    echo "changed" >> README.md
    echo "scratch" > notes.txt ;;
  *"fork"*)
    test -f a.txt && grep -q changed README.md && test -f notes.txt && test -f late.txt
    echo "$id" > "branch-$id.txt" ;;
esac
echo "{\"type\":\"result\",\"result\":\"did: $(echo "$msg" | head -1)\"}"
"""


@pytest.fixture
def world(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="fork-", dir=scratch_root()))
    remote, seed = root / "remote.git", root / "seed"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    (seed / "tests").mkdir()
    (seed / "tests" / "check.sh").write_text("grep -q good fix.txt\n")
    subprocess.run([*git, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    agent = root / "agent.sh"
    agent.write_text(AGENT)
    claude, codex = HARNESSES["claude-code"].run, HARNESSES["codex"].run
    prefix = 'claude -p "$(cat "$HANGAR_PROMPT_FILE")"'
    assert claude.startswith(prefix) and codex.startswith("codex exec ")
    dirs = [".fake/sessions"]
    monkeypatch.setitem(HARNESSES, "claude-like", Harness(
        name="claude-like", title="Claude-like", run=f"bash {agent}" + claude[len(prefix):], session_dirs=dirs,
        resume="preset", fork=True))
    monkeypatch.setitem(HARNESSES, "codex-like", Harness(
        name="codex-like", title="Codex-like", run=f"CAPTURE=1 bash {agent} " + codex[len("codex exec "):],
        session_dirs=dirs, resume="capture", session_id_regex=r'"thread_id"\s*:\s*"([0-9A-Za-z-]{8,})"', fork=True))
    monkeypatch.setitem(HARNESSES, "noresume", Harness(
        name="noresume", title="No fork", run=f"bash {agent}" + claude[len(prefix):], session_dirs=dirs,
        resume="preset", fork=False))
    envs = root / "envs"
    envs.mkdir()
    (envs / "forkenv.yaml").write_text(
        "name: forkenv\nharnesses: [claude-like, codex-like, noresume]\ndefault_harness: claude-like\n")
    yield {"root": root, "remote": remote, "envs": envs, "url": f"file://{remote}"}
    shutil.rmtree(root, ignore_errors=True)


def _kill_tmux(fleet) -> None:
    """Every fake sandbox's private tmux server, including those of sandboxes already destroyed."""
    for b in fleet.providers["docker"].root.iterdir():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{b}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


@pytest.fixture
async def svc(fleet, world, settings, store):
    settings.session_poll_seconds = 0.1
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    s = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    await s.start()
    yield s
    await s.stop()
    _kill_tmux(fleet)


async def settle(svc: SessionService, ref: str, timeout: float = 20.0):
    for _ in range(int(timeout / 0.05)):
        s = svc.get(ref)
        if s.state.value in ("idle", "suspended", "failed") and not svc.busy(s.id):
            return s
        await asyncio.sleep(0.05)
    s = svc.get(ref)
    raise AssertionError(f"{ref} stuck in {s.state}: {s.reason}")


def box(fleet, sandbox_id) -> Path:
    return fleet.providers["docker"].boxes[sandbox_id]


def run_log(fleet, s) -> str:
    return (box(fleet, s.sandbox_id) / "home/agent/.hangar/runs" / f"{s.turns[-1].run_id}.log").read_text()


async def test_fork_session_carries_work_and_forks_the_conversation(svc, world, fleet):
    svc.new(NewSession(name="base", environment="forkenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    parent = await settle(svc, "base")
    assert parent.state == SessionState.IDLE and parent.harness_session, parent.reason
    conv = parent.harness_session
    # changed since the last turn's checkpoint: the fork must take a fresh one
    (box(fleet, parent.sandbox_id) / "workspace/remote/late.txt").write_text("late\n")
    before = len(parent.checkpoints)

    kids = await fork_session(svc, "base", 2, requester="sean", message="continue on a fork")
    assert len(parent.checkpoints) == before + 1 and parent.checkpoints[-1].reason == "fork"
    assert [k.name for k in kids] == ["base-fork-1", "base-fork-2"]
    kids = [await settle(svc, k.name) for k in kids]
    assert [k.branch for k in kids] == ["hangar/session/base-fork-1", "hangar/session/base-fork-2"]
    assert len({k.sandbox_id for k in kids} | {parent.sandbox_id}) == 3
    ids = set()
    for k in kids:
        assert k.state == SessionState.IDLE, k.reason
        assert k.parent_id == parent.id and k.forked_from_checkpoint == parent.checkpoints[-1].id
        repo = box(fleet, k.sandbox_id) / "workspace/remote"
        # commits, the uncommitted change and untracked files (including the one written after the turn)
        assert (repo / "a.txt").exists() and "changed" in (repo / "README.md").read_text()
        assert (repo / "notes.txt").exists() and (repo / "late.txt").exists()
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        assert head == k.branch
        t = k.turns[-1]
        assert t.mode == "fork" and t.exit_code == 0 and k.turns[0].message == "start the work"
        log = run_log(fleet, k)
        # the harness was invoked with Claude Code's fork flags: a new id, resuming the parent's, forked
        assert f"--session-id {k.harness_session}" in log and f"--resume {conv} --fork-session" in log
        assert k.harness_session != conv and k.harness_session_for == "claude-like" and not k.fork_conversation
        lines = (box(fleet, k.sandbox_id) / "home/agent/.fake/sessions" / f"{k.harness_session}.jsonl").read_text()
        assert lines.splitlines() == ["start the work", "continue on a fork"]
        ids.add(k.harness_session)
    assert len(ids) == 2  # each child has its own conversation
    # the parent's conversation is untouched and still its own
    own = (box(fleet, parent.sandbox_id) / "home/agent/.fake/sessions" / f"{conv}.jsonl").read_text()
    assert own.splitlines() == ["start the work"]
    svc.send("base", "carry on", requester="sean")
    parent = await settle(svc, "base")
    assert parent.turns[-1].mode == "resume" and parent.harness_session == conv
    # a child's next turn resumes its own conversation
    svc.send(kids[0].name, "next step", requester="sean")
    k = await settle(svc, kids[0].name)
    assert k.turns[-1].mode == "resume" and f"--resume {k.harness_session}" in run_log(fleet, k)
    assert "--fork-session" not in run_log(fleet, k)


async def test_fork_without_message_codex_style_and_handoff(svc, world, fleet):
    svc.new(NewSession(name="cx", environment="forkenv", repo=world["url"], harness="codex-like",
                       message="start"), owner="admin", requester="sean")
    p = await settle(svc, "cx")
    assert p.harness_session and p.harness_session.startswith("th-")
    (box(fleet, p.sandbox_id) / "workspace/remote/late.txt").write_text("late\n")
    [k] = await fork_session(svc, "cx", 1, requester="sean", names=["cx-alt"])
    k = await settle(svc, "cx-alt")
    assert k.state == SessionState.IDLE and k.sandbox_id and [t.message for t in k.turns] == ["start"]
    assert (box(fleet, k.sandbox_id) / "workspace/remote/late.txt").exists()
    assert k.fork_conversation == p.harness_session  # forked on the first turn, not before
    svc.send("cx-alt", "fork away", requester="sean")
    k = await settle(svc, "cx-alt")
    assert k.turns[-1].mode == "fork" and f"fork {p.harness_session}" in run_log(fleet, k)
    assert k.harness_session and k.harness_session != p.harness_session  # captured from the new thread

    # a harness without native fork: the fork's first turn gets a summary handoff instead
    [h] = await fork_session(svc, "cx", 1, requester="sean", harness="noresume", message="fork in another harness")
    h = await settle(svc, h.name)
    assert h.turns[-1].mode == "handoff", h.reason
    prompt = (box(fleet, h.sandbox_id) / "home/agent/.hangar/runs" / f"{h.turns[-1].run_id}.prompt").read_text()
    assert "Earlier in this session" in prompt and "did: start" in prompt
    assert "--fork-session" not in run_log(fleet, h)


async def test_two_sessions_never_resume_one_conversation(svc, world, fleet):
    svc.new(NewSession(name="g", environment="forkenv", repo=world["url"], message="start"), owner="admin",
            requester="sean")
    p = await settle(svc, "g")
    [k] = await fork_session(svc, "g", 1, requester="sean", message="first fork turn")
    k = await settle(svc, k.name)
    # simulate a bug or a hand edit: the child claims the parent's conversation id
    k.harness_session = p.harness_session
    svc.send(k.name, "who am i", requester="sean")
    k = await settle(svc, k.name)
    assert k.turns[-1].mode == "fork" and k.harness_session != p.harness_session
    assert f"--resume {p.harness_session} --fork-session" in run_log(fleet, k)
    # the owner (the older session) still resumes it
    svc.send("g", "still mine", requester="sean")
    p = await settle(svc, "g")
    assert p.turns[-1].mode == "resume"


async def test_fork_validation(svc, world, fleet, settings):
    svc.new(NewSession(name="empty", environment="forkenv", repo=world["url"]), owner="a", requester="a")
    await svc.release(await settle(svc, "empty"), "x", checkpoint=False)
    with pytest.raises(SessionError, match="nothing to fork"):
        await fork_session(svc, "empty", 2, requester="a")
    svc.new(NewSession(name="v", environment="forkenv", repo=world["url"], message="start"), owner="a",
            requester="a")
    with pytest.raises(SessionError, match="busy"):
        await fork_session(svc, "v", 2, requester="a")
    await settle(svc, "v")
    settings.fork_max = 3
    with pytest.raises(SessionError, match="between 1 and 3"):
        await fork_session(svc, "v", 4, requester="a")
    with pytest.raises(SessionError, match="distinct names"):
        await fork_session(svc, "v", 2, requester="a", names=["one"])
    with pytest.raises(SessionError, match="already exists"):
        await fork_session(svc, "v", 1, requester="a", names=["v"])


@pytest.mark.parametrize("name,fork_args", [
    ("claude-code", ["--session-id", "NEW", "--resume", "PARENT", "--fork-session"]),  # claude 2.1.296
    ("codex", ["exec", "fork", "PARENT", "--json"]),  # codex 0.162.1: `codex exec fork <id>`
    ("pi", ["--session-id", "NEW", "--fork", "PARENT"]),  # pi 1.1.0: --fork <id> --session-id <new>
])
def test_harness_fork_flags(tmp_path, name, fork_args):
    """Expand each harness's real run string with a stand-in binary that prints its argv."""
    h = HARNESSES[name]
    assert h.fork
    binary = h.run.split()[0]
    (tmp_path / "prompt").write_text("do it")
    (tmp_path / binary).write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    (tmp_path / binary).chmod(0o755)

    def argv(**env) -> list[str]:
        out = subprocess.run(["bash", "-c", h.run], capture_output=True, text=True, check=True,
                             env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HANGAR_PROMPT_FILE": str(tmp_path / "prompt"),
                                  **env}).stdout
        return out.splitlines()

    forked = argv(HANGAR_SESSION_NEW="NEW", HANGAR_SESSION_FORK="PARENT")
    want = [a for a in fork_args if name != "codex" or a != "NEW"]
    it = iter(forked)
    assert all(a in it for a in want), forked  # in this order
    resumed = argv(HANGAR_SESSION_RESUME="PARENT")
    assert "--fork-session" not in resumed and "fork" not in resumed and "--fork" not in resumed
    assert not HARNESSES["antigravity"].fork  # agy 1.3.3 has no fork flag: forks hand off


def test_touched_tests():
    status = ["M\ttests/test_x.py", "A\ttests/test_new.py", "M\tsrc/app.py", "D\ttests/conftest.py",
              "R090\ttests/old.py\ttests/renamed.py", "M\tscripts/check.sh"]
    assert touched_tests(None, status) == []
    assert touched_tests("pytest -q", status) == ["tests/test_x.py", "tests/conftest.py", "tests/old.py"]
    assert touched_tests("pytest tests/test_x.py::test_a -q", status) == ["tests/test_x.py"]
    assert touched_tests("bash ./scripts/check.sh", status) == ["scripts/check.sh"]
    assert touched_tests("make test", ["M\tsrc/test_utils_impl.go", "M\tpkg/a_test.go"]) == ["pkg/a_test.go"]


# ---------------------------------------------------------------------------- best-of-N jobs
JOB_AGENT = r"""#!/usr/bin/env bash
mode=$(cat "$MODE_FILE" 2>/dev/null || echo ok)
if [ -n "$HANGAR_JUDGE_DIR" ]; then
  ls "$HANGAR_JUDGE_DIR" > "$HANGAR_JUDGE_DIR/../judge-seen.txt"
  cat "$JUDGE_FILE" > "$HANGAR_JUDGE_DIR/RANKING"; exit 0
fi
a="${HANGAR_ATTEMPT:-0}"
echo "attempt=$a branch=$(git rev-parse --abbrev-ref HEAD)"
case "$mode:$a" in
  sleep:*) sleep 60 ;;
  gone:1) sleep 60 ;;
  slow:*) sleep 1.5 ;;
esac
case "$mode:$a" in
  followup:0) grep -q "also do Y" "$HANGAR_PROMPT_FILE" && echo y > y.txt && git add y.txt && git commit -qm "y"
     echo "did Y" > "$HANGAR_JOB_DIR/RESULT.md"; exit 0 ;;
  *:1) echo bad > fix.txt && git add fix.txt && git commit -qm "attempt 1: wrong" ;;
  *:2) for i in $(seq 1 20); do echo "good $i" >> fix.txt; done; git add fix.txt && git commit -qm "attempt 2: big" ;;
  tamper:3) echo "exit 0" > tests/check.sh; echo bad > fix.txt; git add -A && git commit -qm "attempt 3: tests" ;;
  *:3) echo good > fix.txt ;;   # left uncommitted: hangar commits it
esac
echo "attempt $a: done" > "$HANGAR_JOB_DIR/RESULT.md"
"""


@pytest.fixture
async def jobs(fleet, world, settings, store):
    settings.session_poll_seconds = 0.1
    settings.job_poll_seconds = 0.1
    agent = world["root"] / "job-agent.sh"
    agent.write_text(JOB_AGENT)
    world["mode"], world["judge"] = world["root"] / "mode", world["root"] / "judge"
    HARNESSES["bon"] = Harness(name="bon", title="Best-of-N",
                               run=f"MODE_FILE={world['mode']} JUDGE_FILE={world['judge']} bash {agent}")
    (world["envs"] / "bonenv.yaml").write_text("name: bonenv\nharnesses: [bon]\n")
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    sessions = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    await sessions.start()
    hooks = Hooks()
    svc = JobService(fleet, sessions, transport=httpx.MockTransport(hooks))
    svc.hooks = hooks
    await svc.start()
    yield svc
    await svc.stop()
    await sessions.stop()
    HARNESSES.pop("bon", None)
    _kill_tmux(fleet)


def bon(world, **kw) -> SubmitJob:
    return SubmitJob(**{"instructions": "make fix.txt say good", "repo": world["url"], "environment": "bonenv",
                        "verify": "bash tests/check.sh", "strategy": "best_of_n", "n": 3, **kw})


async def _idle(jobs, job):
    while job.id in jobs._active:
        await asyncio.sleep(0.05)


async def test_best_of_n_delivers_a_passing_attempt_and_lists_all(jobs, world, fleet):
    job = jobs.submit(bon(world), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=40)
    await _idle(jobs, job)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    a1, a2, a3 = job.attempts
    assert [a.index for a in job.attempts] == [1, 2, 3] and all(a.state == "done" for a in job.attempts)
    assert a1.verify_exit_code != 0 and a2.verify_exit_code == 0 and a3.verify_exit_code == 0
    # both pass; the smaller diff wins (a3 left its change uncommitted: hangar committed it)
    assert a3.rank == 1 and a2.rank == 2 and a1.rank == 3 and a3.diff_lines < a2.diff_lines
    assert a3.score == 1.0 and a1.score == 0.0 and a3.summary == "attempt 3: done"
    out = subprocess.run(["git", "--git-dir", str(world["remote"]), "show", f"{job.branch}:fix.txt"],
                         capture_output=True, text=True).stdout
    assert out == "good\n" and job.result.pushed and job.result.verify_exit_code == 0
    # each attempt ran on its own branch, in its own sandbox, restored from the prepared base
    sessions = [jobs.sessions.get(a.session_id) for a in job.attempts]
    base = jobs.sessions.get(sessions[0].parent_id)
    assert base.job_id == job.id and base.state == SessionState.SUSPENDED and base.checkpoints
    # losers keep their attempt branches; the winner's session now works on the job branch (follow-ups push there)
    assert [s.branch for s in sessions] == [f"{job.branch}-attempt-1", f"{job.branch}-attempt-2", job.branch]
    assert all(s.forked_from_checkpoint == base.checkpoints[-1].id for s in sessions)
    # the winner carries the job on; the others are checkpointed and released
    assert job.session_id == a3.session_id and fleet.get(job.sandbox_id).state == "running"
    assert sessions[0].state == sessions[1].state == SessionState.SUSPENDED and sessions[0].sandbox_id is None
    assert sessions[0].checkpoints and not _remote_has(world["remote"], f"{job.branch}-attempt-1")
    view = jobs.view(job)
    assert [a["index"] for a in view["attempts"]] == [1, 2, 3] and view["strategy"] == "best_of_n"
    body = jobs._pr_body(job)
    assert "Best of 3: attempt 3 delivered" in body and "| 1 | exited" in body and "failed (1)" in body
    # the agent worked on the attempt's own branch and knew which attempt it was
    log =(box(fleet, job.sandbox_id) / "home/agent/.hangar/runs" / f"{a3.run_id}.log").read_text()
    assert f"attempt=3 branch={job.branch}-attempt-3" in log

    # a follow-up continues the winner's session (same sandbox and branch), then delivers to the same PR branch
    world["mode"].write_text("followup")
    jobs.follow_up(job.id, "also do Y", requester="svc")
    job = await _until(jobs, job.id, DONE)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert job.session_id == a3.session_id and len(jobs.sessions.get(a3.session_id).turns) == 2
    out = subprocess.run(["git", "--git-dir", str(world["remote"]), "log", "--format=%s", job.branch],
                         capture_output=True, text=True).stdout
    assert out.splitlines()[0] == "y"


def _remote_has(remote: Path, branch: str) -> bool:
    return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
                          capture_output=True).returncode == 0


async def test_best_of_n_flags_test_tampering_judges_and_pushes_losers(jobs, world, settings):
    world["mode"].write_text("tamper")
    settings.best_of_n_push_attempts = True
    job = jobs.submit(bon(world), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=40)
    await _idle(jobs, job)
    a1, a2, a3 = job.attempts
    # attempt 3 made verify pass by editing the check: flagged and ranked last, even below the failing one
    assert a3.verify_exit_code == 0 and a3.touched_tests == ["tests/check.sh"] and a3.rank == 3 and a3.score == -1
    assert a2.rank == 1 and job.state == JobState.SUCCEEDED
    assert _remote_has(world["remote"], f"{job.branch}-attempt-1") and a1.pushed_branch
    assert not _remote_has(world["remote"], f"{job.branch}-attempt-2")  # the winner is the job branch itself
    assert "changed the verify's tests" in jobs._pr_body(job)

    # judge: two passing attempts, the judge prefers the bigger one
    world["mode"].write_text("ok")
    world["judge"].write_text("I'd take 2, then 3\n")
    job = jobs.submit(bon(world, judge=True), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=40)
    await _idle(jobs, job)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert [a.rank for a in job.attempts] == [3, 1, 2]
    assert any("(judge)" in e.message for e in job.events)


async def test_best_of_n_survives_a_dead_attempt_and_cancels_all(jobs, world, fleet):
    world["mode"].write_text("gone")
    job = jobs.submit(bon(world), Caller("svc", "svc"))
    await _until(jobs, job.id, {JobState.RUNNING})
    while not (job.attempts and job.attempts[0].run_id and job.attempts[1].finished_at):
        await asyncio.sleep(0.05)
    s1 = jobs.sessions.get(job.attempts[0].session_id)
    await fleet.destroy(s1.sandbox_id, actor="reaper")  # the node went away
    job = await _until(jobs, job.id, DONE, timeout=40)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert job.attempts[0].outcome == "gone" and job.attempts[0].rank is None and job.attempts[2].rank == 1

    world["mode"].write_text("sleep")
    job = jobs.submit(bon(world), Caller("svc", "svc"))
    while not (job.attempts and all(a.run_id for a in job.attempts)):
        await asyncio.sleep(0.05)
    jobs.cancel(job.id, requester="svc")
    job = await _until(jobs, job.id, DONE, timeout=10)
    await _idle(jobs, job)
    assert job.state == JobState.CANCELLED and [a.outcome for a in job.attempts] == ["cancelled"] * 3
    for a in job.attempts:
        s = jobs.sessions.get(a.session_id)
        assert s.sandbox_id is None and s.checkpoints


async def test_best_of_n_restart_reattaches_to_attempts(jobs, world, fleet):
    world["mode"].write_text("slow")
    job = jobs.submit(bon(world), Caller("svc", "svc"))
    while not (job.attempts and all(a.run_id for a in job.attempts)):
        await asyncio.sleep(0.05)
    await jobs.stop()  # the control plane goes down mid-attempts
    again = JobService(fleet, jobs.sessions, transport=jobs.transport)
    await again.start()
    try:
        done = await _until(again, job.id, DONE, timeout=40)
        assert done.state == JobState.SUCCEEDED, [e.message for e in done.events]
        assert any("re-attaching to the attempts" in e.message for e in done.events)
        assert [a.rank for a in done.attempts] == [3, 2, 1] and len({a.run_id for a in done.attempts}) == 3
    finally:
        await again.stop()


async def test_best_of_n_attempts_share_one_prompt_and_one_cache_write(jobs, world, settings, monkeypatch):
    """--- overnight/cache: the attempts' prompts are identical up to their branch, named at the very end, and all
    but the first to start wait for it to answer (here: to finish, as this agent prints nothing until then)."""
    settings.cache_stagger_seconds = 20
    world["mode"].write_text("slow")  # every attempt takes 1.5 s
    prompts: dict[str, str] = {}
    begin = jobs.sessions.begin_turn

    async def spy(s, turn, prompt=None, **kw):
        prompts[s.id] = prompt
        return await begin(s, turn, prompt=prompt, **kw)

    monkeypatch.setattr(jobs.sessions, "begin_turn", spy)
    job = jobs.submit(bon(world), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=60)
    await _idle(jobs, job)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    heads = set()
    for a in job.attempts:
        head, _, tail = prompts[a.session_id].partition("# Your branch")
        heads.add(head)
        assert f"branch `{job.branch}-attempt-{a.index}`: commit there" in tail and job.branch not in head
    assert len(heads) == 1 and "on the branch named at the end of this message" in heads.pop()
    waits = [next(t for t in jobs.sessions.get(a.session_id).turns if t.run_id == a.run_id).cache_wait_seconds
             for a in job.attempts]
    assert waits.count(None) == 1 and all(w >= 0.5 for w in waits if w is not None), waits


async def test_best_of_n_validation(jobs, world):
    with pytest.raises(JobError, match="between 2 and"):
        jobs.submit(bon(world, n=1), Caller("svc", "svc"))
    jobs.settings.best_of_n_max = 4
    with pytest.raises(JobError, match="HANGAR_BEST_OF_N_MAX"):
        jobs.submit(bon(world, n=5), Caller("svc", "svc"))
    with pytest.raises(JobError, match="best_of_n"):
        jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="bonenv", judge=True),
                    Caller("svc", "svc"))
    j = jobs.submit(bon(world, n=None), Caller("svc", "svc"))
    assert j.n == 3 and j.strategy == "best_of_n"
    jobs.cancel(j.id, requester="svc")


# ---------------------------------------------------------------------------- surfaces
async def test_mcp_fork_session(svc, world, fleet):
    import json

    from mcp import Client

    from hangar.mcp_server import build_mcp

    svc.new(NewSession(name="m", environment="forkenv", repo=world["url"], message="start"), owner="admin",
            requester="sean")
    await settle(svc, "m")
    async with Client(build_mcp(fleet, None, svc)) as c:
        res = await c.call_tool("fork_session", {"session": "m", "n": 1, "message": "go on, from mcp"})
        out = json.loads(res.content[0].text)
    assert out["parent"] == "m" and out["forks"][0]["name"] == "m-fork-1"
    k = await settle(svc, "m-fork-1")
    assert k.turns[-1].mode == "fork" and k.turns[-1].exit_code == 0


def test_rest_and_cli_surfaces(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from hangar import cli
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = "Bearer t0ken"
        assert c.post("/api/sessions/nope/fork", json={"n": 2}).status_code == 404
        assert c.post("/api/sessions/nope/fork", json={"n": 0}).status_code == 422
        r = c.post("/api/jobs", json={"instructions": "x", "environment": "plain", "deliver": "none",
                                      "strategy": "best_of_n", "n": 9})
        assert r.status_code == 422

    calls = []

    def fake_call(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if path.endswith("/fork"):
            return {"parent": "p", "forks": [{"name": "p-fork-1", "id": "ses-1", "state": "starting",
                                              "branch": "b-fork-1"}]}
        return {"id": "job-1", "title": "t", "state": "queued", "n": 3, "attempts": [
            {"index": 1, "state": "done", "outcome": "exited", "verify_exit_code": 0, "diff_lines": 4,
             "files": ["M a"], "rank": 1, "touched_tests": []},
            {"index": 2, "state": "done", "outcome": "exited", "verify_exit_code": 0, "diff_lines": 2,
             "files": ["M t"], "rank": 2, "touched_tests": ["tests/t.py"]}]}

    monkeypatch.setattr(cli, "_call", fake_call)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["session", "fork", "p", "-n", "3", "try this"])
    assert r.exit_code == 0 and "p-fork-1" in r.output, r.output
    assert calls[-1] == ("POST", "/api/sessions/p/fork", {"n": 3, "message": "try this", "harness": None,
                                                         "names": None})
    r = runner.invoke(cli.app, ["job", "submit", "fix it", "--best-of", "3", "--judge"])
    assert r.exit_code == 0 and calls[-1][2]["strategy"] == "best_of_n" and calls[-1][2]["n"] == 3
    assert calls[-1][2]["judge"] is True
    r = runner.invoke(cli.app, ["job", "attempts", "job-1"])
    assert r.exit_code == 0 and "* #1" in r.output and "changed the verify's tests" in r.output
