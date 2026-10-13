"""The verify gate inside the agent's turn (--- overnight/autofix, spec 0012 item 5).

A scripted one-shot harness gets a follow-up turn with the failing check; a fake Claude Code that follows the
documented Stop hook contract gets the check as a hook passed with `--settings`; the driver runs it for Codex.
tests/test_verify_gate_real_clis.py runs the same gates against the real CLIs.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from hangar import driver, verifyloop
from hangar.events import parse_log
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, SubmitJob
from hangar.models import Budget, Job, JobState, Turn, Usage, now
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures

FAKES = Path(__file__).parent / "fixtures" / "live"

AGENT = r"""#!/usr/bin/env bash
mode=$(cat "$MODE_FILE" 2>/dev/null || echo ok)
p="$HANGAR_PROMPT_FILE"
case "$mode" in
  verifyfix)  # does part of the work; fixes the rest only once hangar hands back the failing check
    if grep -q "hangar ran the job's check after your turn" "$p"; then
      echo fixed > fix.txt && git add fix.txt && git commit -qm fix && echo "fixed it" > "$HANGAR_JOB_DIR/RESULT.md"
    else
      echo part > part.txt && git add part.txt && git commit -qm part && echo "did part" > "$HANGAR_JOB_DIR/RESULT.md"
    fi ;;
  never) date +%N >> never.txt && git add never.txt && git commit -qm never ;;
  tests) printf 'def test_x():\n    assert True\n' > tests/test_x.py && echo fixed > fix.txt && git add -A \
           && git commit -qm "make it pass" ;;
  fail) exit 3 ;;
esac
"""

# A stand-in for `claude -p` that follows the Stop hook contract in the hooks reference: when the agent is done,
# each Stop hook command runs with the event's JSON on stdin; exit 2 blocks the stop and its stderr is the reason
# Claude continues with; after 8 blocks in a row the CLI ends the turn anyway.
FAKE_CLAUDE = r"""
import json, os, subprocess, sys
args = sys.argv[1:]
settings = json.loads(args[args.index("--settings") + 1]) if "--settings" in args else {}
hooks = [h for g in (settings.get("hooks") or {}).get("Stop", []) for h in g.get("hooks", [])]
mode = open(os.environ["MODE_FILE"]).read().strip() if os.path.exists(os.environ["MODE_FILE"]) else "ok"
said, blocks = [], 0
while True:
    feedback = said[-1] if said else ""
    if mode == "never" or "failed" not in feedback:
        open("part.txt", "a").write("x\n")
    else:
        open("fix.txt", "w").write("fixed\n")
    subprocess.run("git add -A && git commit -qm work", shell=True)
    reasons = []
    for h in hooks:
        event = {"hook_event_name": "Stop", "stop_hook_active": blocks > 0, "cwd": os.getcwd(),
                 "last_assistant_message": "done"}
        r = subprocess.run(["bash", "-c", h["command"]], input=json.dumps(event), capture_output=True, text=True,
                           timeout=h.get("timeout", 600))
        if r.returncode == 2:
            reasons.append(r.stderr)
    if not reasons or blocks >= 8:
        break
    blocks += 1
    said.append("\n".join(reasons))
open(os.path.join(os.environ["HANGAR_JOB_DIR"], "RESULT.md"), "w").write(f"blocked {blocks} time(s)")
print(json.dumps({"type": "result", "subtype": "success", "result": f"blocked {blocks}", "feedback": said}))
"""


@pytest.fixture
def gated(world, monkeypatch):  # noqa: F811
    """test_jobs' world with this file's scripted agent, a fake Claude Code and an environment for each."""
    agent = world["root"] / "gate-agent.sh"
    agent.write_text(AGENT)
    monkeypatch.setitem(HARNESSES, "scripted", Harness(
        name="scripted", title="Scripted", run=f"MODE_FILE={world['mode']} bash {agent}"))
    fake = world["root"] / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE)
    real = HARNESSES["claude-code"].run
    flag = '${HANGAR_CLAUDE_SETTINGS:+--settings "$HANGAR_CLAUDE_SETTINGS"}'
    assert flag in real and flag in HARNESSES["claude-code"].live_run  # what the real harness passes, verbatim
    monkeypatch.setitem(HARNESSES, "claude-code", Harness(
        name="claude-code", title="Fake Claude Code", run=f"MODE_FILE={world['mode']} python3 {fake} {flag}"))
    (world["envs"] / "claudeenv.yaml").write_text("name: claudeenv\nharnesses: [claude-code]\n")
    return world


async def _job(jobs, world, env="jobenv", **kw) -> Job:  # noqa: F811
    jobs.fleet.catalog.reload()
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment=env, **kw), Caller("svc", "svc"))
    return await _until(jobs, job.id, DONE, timeout=40)


def _prompts(jobs, job) -> list[str]:  # noqa: F811
    box = jobs.fleet.providers["docker"].boxes[job.sandbox_id]
    files = sorted((box / "home/agent/.hangar/runs").glob("*.prompt"), key=lambda p: p.stat().st_mtime)
    return [p.read_text() for p in files]


async def test_one_shot_harness_gets_the_failing_check_as_a_follow_up_turn(jobs, gated):  # noqa: F811
    gated["mode"].write_text("verifyfix")
    job = await _job(jobs, gated, verify="test -f fix.txt || { echo 'fix.txt is missing'; exit 1; }")
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert [t.trigger for t in job.turns] == [None, "verify"] and job.turns[1].requester == "hangar"
    assert job.result.verify_exit_code == 0 and job.result.summary == "fixed it" and len(job.result.commits) == 2
    fix = _prompts(jobs, job)[-1]
    assert "fix.txt is missing" in fix and "round 1 of 3" in fix and "Don't change the check" in fix
    assert any("sending it back to the agent (round 1 of 3)" in e.message for e in job.events)


async def test_rounds_are_capped_and_the_gate_can_be_turned_off(jobs, gated):  # noqa: F811
    gated["mode"].write_text("never")
    job = await _job(jobs, gated, verify="false", max_verify_rounds=2)
    assert job.state == JobState.FAILED and job.reason == "verify_failed"
    assert [t.trigger for t in job.turns] == [None, "verify", "verify"]  # two rounds, then delivered as is
    assert any("still fails after 2 round(s)" in e.message for e in job.events)
    off = await _job(jobs, gated, verify="false", verify_in_loop=False)
    assert len(off.turns) == 1 and off.reason == "verify_failed"
    gated["mode"].write_text("fail")  # an agent that didn't finish gets no verify-fix turn: report that instead
    bad = await _job(jobs, gated, verify="false")
    assert len(bad.turns) == 1 and bad.reason == "agent_failed"


async def test_claude_code_stop_hook_blocks_until_the_check_passes(jobs, gated):  # noqa: F811
    gated["mode"].write_text("verifyfix")
    job = await _job(jobs, gated, env="claudeenv", verify="test -f fix.txt || { echo 'fix.txt is missing'; exit 1; }")
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert len(job.turns) == 1 and job.turns[0].verify_rounds == 1  # fixed inside the turn: no second turn
    assert job.result.summary == "blocked 1 time(s)"
    assert any("failed 1 time(s) during the turn" in e.message for e in job.events)
    box = jobs.fleet.providers["docker"].boxes[job.sandbox_id]
    log = next((box / "home/agent/.hangar/runs").glob("*.log")).read_text()
    feedback = json.loads(log.strip().splitlines()[-1])["feedback"][0]
    assert "fix.txt is missing" in feedback and "round 1 of 3" in feedback  # the hook's stderr reached the agent
    script = next((box / "home/agent/.hangar/runs").glob("*.sh")).read_text()
    assert "HANGAR_CLAUDE_SETTINGS=" in script and "disableAllHooks" in script  # inline: no settings file
    assert not list((box / "workspace").rglob(".claude"))  # nothing written into the repo


async def test_stop_hook_rounds_are_shared_with_follow_up_turns(jobs, gated):  # noqa: F811
    gated["mode"].write_text("never")
    job = await _job(jobs, gated, env="claudeenv", verify="false", max_verify_rounds=2)
    assert job.state == JobState.FAILED and job.reason == "verify_failed"
    assert len(job.turns) == 1 and job.turns[0].verify_rounds == 2  # the hook used both rounds; no extra turn
    assert job.result.summary == "blocked 2 time(s)"


async def test_editing_the_checks_own_tests_is_flagged_and_drafted(jobs, gated):  # noqa: F811
    seed = gated["root"] / "seed"
    (seed / "tests").mkdir()
    (seed / "tests" / "test_x.py").write_text("def test_x():\n    assert False\n")
    git = ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "tests"], check=True)
    subprocess.run([*git, "push", "-q", str(gated["remote"]), "HEAD:refs/heads/main"], check=True)
    gated["mode"].write_text("tests")
    job = await _job(jobs, gated, verify="test -f fix.txt && grep -q True tests/test_x.py")
    assert job.state == JobState.SUCCEEDED and job.result.touched_tests == ["tests/test_x.py"]
    assert any("changed the check's own tests" in e.message for e in job.events)
    assert "changed the check's own tests" in jobs._pr_body(job)


def test_remaining_budget_is_shared_by_the_verify_fix_turns():
    job = Job(title="t", instructions="x", environment="e", branch="hangar/j", verify="pytest",
              budget=Budget(max_minutes=30, max_tokens=1000, max_cost_usd=1.0))
    job.turns = [Turn(started_at=now() - 600, usage=Usage(input_tokens=400, cost_usd=0.4)),
                 Turn(trigger="verify", started_at=now() - 60, usage=Usage(input_tokens=100, cost_usd=0.1))]
    left, spent = verifyloop.remaining_budget(job)
    assert spent is None and left.max_tokens == 500 and abs(left.max_cost_usd - 0.5) < 1e-6
    assert 18 <= left.max_minutes <= 20
    job.turns.append(Turn(trigger="verify", usage=Usage(input_tokens=600)))
    assert verifyloop.remaining_budget(job) == (None, "the turn's 1,000 tokens are used up")
    job.turns.append(Turn(kind="follow_up", message="more"))  # a person's follow-up starts a new unit
    assert verifyloop.rounds_used(job) == 0 and verifyloop.remaining_budget(job)[1] is None


def test_stop_hook_script(tmp_path):
    """The hook itself, as Claude Code runs it: blocks with the output on stderr (exit 2) while rounds are left
    and the deadline isn't past, then lets the turn end."""
    home = tmp_path / "home"
    (home / ".hangar" / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"HOME": str(home), "HANGAR_RUN_ID": "run-1", "PATH": os.environ["PATH"]}
    cmd = verifyloop.stop_hook("echo 'it broke'; echo \"x'y\"; exit 4", str(repo), 2, int(now()) + 600)

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-c", cmd], input="{}", capture_output=True, text=True, env=env)

    first = run()
    assert first.returncode == 2 and "it broke" in first.stderr and "x'y" in first.stderr
    assert "exit 4), round 1 of 2" in first.stderr and first.stdout == ""
    assert run().returncode == 2 and run().returncode == 0  # two rounds, then it lets go
    assert (home / ".hangar/runs/run-1.verify").read_text().splitlines() == ["fail 4", "fail 4"]
    ok = verifyloop.stop_hook("true", str(repo), 2, int(now()) + 600)
    env["HANGAR_RUN_ID"] = "run-2"
    assert subprocess.run(["bash", "-c", ok], capture_output=True, env=env).returncode == 0
    assert (home / ".hangar/runs/run-2.verify").read_text() == "pass\n"
    late = verifyloop.stop_hook("false", str(repo), 2, int(now()) - 1)
    env["HANGAR_RUN_ID"] = "run-3"
    assert subprocess.run(["bash", "-c", late], capture_output=True, env=env).returncode == 0  # past its deadline
    settings = json.loads(verifyloop.claude_settings("pytest -q", "/w/r", 3, 0, 1800))
    assert settings["disableAllHooks"] is False
    assert settings["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 1830


def test_turn_env_per_harness(settings):
    job = Job(title="t", instructions="x", environment="e", branch="hangar/j", verify="make test")
    job.turns = [Turn()]
    claude = verifyloop.turn_env(settings, job, "claude-code", "/w/r", 60, None)
    assert list(claude) == [verifyloop.CLAUDE_ENV] and "make test" in claude[verifyloop.CLAUDE_ENV]
    gate = json.loads(verifyloop.turn_env(settings, job, "codex", "/w/r", 60, None)[verifyloop.GATE_ENV])
    assert gate["command"] == "make test" and gate["rounds"] == 3 and gate["cwd"] == "/w/r"
    assert gate["deadline"] <= now() + 60 * 60 - 15 * 60 + 1  # room left for one more check and fix
    assert verifyloop.turn_env(settings, job, "pi", "/w/r", 60, None) == {}  # one-shot: hangar's follow-up turn
    assert verifyloop.turn_env(settings, job, "codex", None, 60, None) == {}  # no repo, nothing to check
    job.verify_in_loop = False
    assert verifyloop.turn_env(settings, job, "claude-code", "/w/r", 60, None) == {}


def _drive(tmp_path: Path, gate: dict | None, prompt: str = "hello") -> tuple[int, list[dict]]:
    home, ctl = tmp_path / "home", tmp_path / "ctl"
    (home / ".hangar" / "runs").mkdir(parents=True)
    ctl.mkdir()
    (ctl / "key").write_bytes(b"k")
    (tmp_path / "prompt").write_text(prompt)
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex"), "HANGAR_RUN_ID": "run-1",
           "HANGAR_PROMPT_FILE": str(tmp_path / "prompt")}
    env.pop(verifyloop.GATE_ENV, None)
    if gate is not None:
        env[verifyloop.GATE_ENV] = json.dumps(gate)
    p = subprocess.run([sys.executable, driver.__file__, "--harness", "codex", "--ctl", str(ctl), "--approvals",
                        "auto", "--", sys.executable, str(FAKES / "fake_codex.py")],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    lines = [json.loads(ln) for ln in p.stdout.splitlines() if ln.startswith("{")]
    return p.returncode, lines


def test_driver_gate_steers_the_failing_check_back_into_the_conversation(tmp_path):
    """Codex: the turn ends without the tool; the check fails, its output ("TOOL ...") goes back as a message,
    the fake runs the tool in the next turn of the same thread, and the check passes."""
    check = "test -f tool-ran.txt || { echo 'TOOL run the tool first'; exit 1; }"
    code, lines = _drive(tmp_path, {"command": check, "cwd": str(tmp_path), "rounds": 3,
                                    "deadline": int(now()) + 600, "timeout": 30})
    assert code == 0 and (tmp_path / "tool-ran.txt").exists()
    verdicts = [(x["passed"], x["exit_code"]) for x in lines if x.get("type") == "hangar.verify"]
    assert verdicts == [(False, 1), (True, 0)]
    threads = {x["thread_id"] for x in lines if x.get("type") == "thread.started"}
    assert len(threads) == 1 and sum(1 for x in lines if x.get("type") == "turn.completed") == 2
    assert (tmp_path / "home/.hangar/runs/run-1.verify").read_text().splitlines() == ["fail 1", "pass"]
    events, _ = parse_log("\n".join(json.dumps(x) for x in lines).encode(), "codex")
    assert any(e.data.get("action") == "verify" and not e.data["passed"] for e in events)


def test_driver_gate_stops_at_its_rounds_and_ignores_a_bad_spec(tmp_path):
    code, lines = _drive(tmp_path / "a", {"command": "echo still broken; false", "cwd": str(tmp_path), "rounds": 2,
                                          "deadline": int(now()) + 600, "timeout": 30})
    assert code == 0  # the turn itself went fine; hangar's own check after it decides delivery
    assert [x["passed"] for x in lines if x.get("type") == "hangar.verify"] == [False, False]
    assert sum(1 for x in lines if x.get("type") == "turn.completed") == 3
    for bad in ({"command": "false"}, {"command": "", "cwd": "/", "rounds": 1, "deadline": 9e9, "timeout": 1},
                {"command": "false", "cwd": "/", "rounds": "3", "deadline": 0, "timeout": 1}):
        (tmp_path / "b").mkdir(exist_ok=True)
        sub = tmp_path / "b" / str(abs(hash(json.dumps(bad))))
        sub.mkdir()
        code, lines = _drive(sub, bad)
        assert code == 0 and not [x for x in lines if x.get("type") == "hangar.verify"], bad
    code, lines = _drive(tmp_path / "c", {"command": "false", "cwd": str(tmp_path), "rounds": 3,
                                          "deadline": int(now()) - 1, "timeout": 30})
    assert not [x for x in lines if x.get("type") == "hangar.verify"]  # past the deadline: no more rounds


def test_hook_command_quotes_everything():
    cmd = verifyloop.stop_hook("pytest -k 'a and b'; rm -rf $HOME", "/w/my repo", 3, 100)
    assert shlex.quote("pytest -k 'a and b'; rm -rf $HOME") in cmd and shlex.quote("/w/my repo") in cmd
