"""Per-edit git tree hashes (#165): the turn driver writes the working tree's id after each edit or shell tool that
changed it, and at the turn's start and end; the parser and the trace export carry them (`hangar.git.tree*`)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hangar import driver, gittree, traces
from hangar.events import RunEvent, parse_log, tree_events
from hangar.models import ExecResult, Session, SessionTurn
from tests.conftest import scratch_root

DRIVER = Path(driver.__file__)
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# A scripted Claude Code: one turn that edits 3 files, runs 2 commands (one changes the tree, one doesn't) and reads
# a file, each as a tool_use and its tool_result, doing the work in its working directory. Like the real CLI, it
# takes a moment between a tool's result and the next call (the model's turn).
FAKE = r'''
import json, os, subprocess, sys, time
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def put(path, text, mode="w"):
    with open(path, mode) as f:
        f.write(text)
sys.stdin.readline()
emit({"type": "system", "subtype": "init", "model": "claude-test", "session_id": "s"})
steps = [("t1", "Write", {"file_path": "a.py", "content": "a = 1\n"}, lambda: put("a.py", "a = 1\n")),
         ("t2", "Read", {"file_path": "a.py"}, lambda: None),
         ("t3", "Edit", {"file_path": "README.md"}, lambda: put("README.md", "more\n", "a")),
         ("t4", "Bash", {"command": "ls"}, lambda: subprocess.run(["ls"], capture_output=True)),
         ("t5", "MultiEdit", {"file_path": "pkg/b.py"}, lambda: (os.makedirs("pkg", exist_ok=True),
                                                                 put("pkg/b.py", "b = 2\n"))),
         ("t6", "Bash", {"command": "echo built > out.txt"}, lambda: put("out.txt", "built\n"))]
for tid, name, inp, act in steps:
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
    act()
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})
    time.sleep(0.4)  # the model's next call: the driver hashes the tree this tool left meanwhile
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "usage": {}})
for _ in sys.stdin:
    pass
'''


def _repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="trees-", dir=scratch_root()))
    repo = root / "repo"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main", "-c", "gc.auto=0",
           "-c", "maintenance.auto=false"]  # no background maintenance writing into .git/objects meanwhile
    subprocess.run([*git, "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("hi\n")
    (repo / ".gitignore").write_text("*.log\n")
    subprocess.run([*git, "-C", str(repo), "add", "."], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-qm", "init"], check=True)
    return repo


def _by_hand(repo: Path) -> str:
    """`git write-tree` of the working tree, in the repo itself, through a scratch index (testresults.tree_script)."""
    from hangar import testresults

    return subprocess.run(["sh", "-c", testresults.tree_script(str(repo))], capture_output=True,
                          text=True).stdout.strip()


def _drive(repo: Path, tmp: Path) -> tuple[str, list[RunEvent], str]:
    fake = tmp / "fake_claude.py"
    fake.write_text(FAKE)
    prompt = tmp / "prompt"
    prompt.write_text("go")
    start = _by_hand(repo)
    res = subprocess.run([sys.executable, str(DRIVER), "--harness", "claude", "--ctl", str(tmp / "ctl"), "--",
                          sys.executable, str(fake)], cwd=repo, capture_output=True, text=True, timeout=60,
                         env={**os.environ, "HANGAR_PROMPT_FILE": str(prompt)})
    assert res.returncode == 0, res.stderr
    evs, _ = parse_log(res.stdout.encode(), "claude-code")
    return start, evs, res.stdout


def test_a_claude_turn_records_a_tree_after_each_tool_that_changed_it(tmp_path):
    repo = _repo()
    start, evs, raw = _drive(repo, tmp_path)
    trees = [e for e in evs if e.kind == "progress" and e.data.get("action") == "tree"]
    after = {e.data["id"]: e.data["tree"] for e in trees if e.data.get("id")}
    # the 3 edits and the command that wrote a file; not the Read, nor `ls` (the tree didn't change)
    assert sorted(after) == ["t1", "t3", "t5", "t6"], raw
    assert len(set(after.values())) == 4
    phases = {e.data["phase"]: e.data["tree"] for e in trees if e.data.get("phase")}
    assert phases["start"] == start and phases["end"] == _by_hand(repo) == after["t6"]
    assert all(isinstance(e.data.get("ms"), int) for e in trees)
    # the scratch index and objects lived in the driver's private dir, gone now; the agent's .git untouched
    assert not (tmp_path / "ctl" / "tree").exists()
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                          text=True).stdout.count("??") == 3  # a.py, out.txt, pkg/: nothing was staged in it

    # the trace: hangar.git.tree on those tool spans, start and end on the turn
    for e in trees:
        e.data["trusted"] = False  # as TurnWatcher stamps a run without the uid split
    seq = 0
    for e in evs:
        seq += 1
        e.seq, e.ts = seq, 1000.0 + seq
    turn = SessionTurn(message="go", run_id="run-0000tree", started_at=1000.0, finished_at=2000.0, outcome="exited",
                       exit_code=0)
    s = Session(name="s", environment="py", turns=[turn])
    x = traces.Export(traces.trace_id(s.id), lambda v: v)
    sp = traces._turn_span(x, None, s, 1, turn, evs)
    assert sp["attrs"]["hangar.git.tree.start"] == start and sp["attrs"]["hangar.git.tree.end"] == after["t6"]
    assert sp["attrs"]["hangar.git.tree.trusted"] is False
    tools = {p["attrs"]["gen_ai.tool.call.id"]: p["attrs"] for p in x.spans if p["attrs"].get("hangar.kind")
             == "tool.call"}
    assert {k: v.get("hangar.git.tree") for k, v in tools.items()} == {
        "t1": after["t1"], "t2": None, "t3": after["t3"], "t4": None, "t5": after["t5"], "t6": after["t6"]}
    assert tools["t1"]["hangar.git.tree.trusted"] is False
    assert not [ev for p in x.spans for ev in p["events"] if "tree" in str(ev["attrs"].get("hangar.text", ""))]


def test_a_slow_hash_stops_hashing_for_the_turn(tmp_path, monkeypatch):
    repo = _repo()
    lines: list[dict] = []

    class Out:
        def event(self, kind, **data):
            lines.append({"type": "hangar." + kind, **data})

    t = driver.Trees(Out(), str(repo), str(tmp_path / "tree"))
    t.point("start")
    monkeypatch.setattr(driver, "TREE_SLOW", 0.0)  # any hash is now slow
    (repo / "x.py").write_text("x")
    t.point(tool_use_id="t1")
    (repo / "y.py").write_text("y")
    t.point(tool_use_id="t2")
    t.point("end")
    assert [ln.get("tree") is not None for ln in lines] == [True, False]
    assert lines[1]["skipped"] == "slow" and lines[1]["tool_use_id"] == "t1"
    evs = tree_events(None, tool_id="t1", ms=3000, skipped="slow")
    sp = traces.tree_attrs([*tree_events(lines[0]["tree"], phase="start"), *evs])
    assert sp["hangar.git.tree.skipped"] == "slow" and sp["hangar.git.tree.end"] is None


def test_hashing_runs_nothing_the_repo_configures_and_writes_nothing_into_it(tmp_path):
    repo = _repo()
    pwned = tmp_path / "PWNED"
    with open(repo / ".git" / "config", "a") as f:
        f.write(f'[filter "evil"]\n\tclean = touch {pwned}\n[core]\n\tfsmonitor = touch {pwned}\n')
    (repo / ".gitattributes").write_text("* filter=evil\n")
    objects = sorted(p.name for p in (repo / ".git" / "objects").iterdir() if not p.name.endswith(".lock"))
    t = driver.Trees(SimpleNamespace(event=lambda *a, **k: None), str(repo), str(tmp_path / "tree"))
    (repo / "new.py").write_text("n")
    sha, _ = t.hash()
    assert sha and not pwned.exists()
    assert sorted(p.name for p in (repo / ".git" / "objects").iterdir() if not p.name.endswith(".lock")) == objects
    # the same script for one-shot turns
    out = subprocess.run(["sh", "-c", gittree.script(str(repo))], capture_output=True, text=True).stdout.strip()
    assert out == sha and not pwned.exists()


def test_outside_a_repo_nothing_is_recorded(tmp_path):
    lines: list = []
    t = driver.Trees(SimpleNamespace(event=lambda k, **d: lines.append(d)), str(tmp_path), str(tmp_path / "tree"))
    t.point("start")
    t.point(tool_use_id="t1")
    assert lines == [] and t.off == "no repo"


def test_driver_tree_lines_become_events():
    log = "\n".join(json.dumps(x) for x in [
        {"type": "hangar.tree", "phase": "start", "tree": "a" * 40, "ms": 4},
        {"type": "hangar.tree", "tool_use_id": "toolu_1", "tree": "b" * 40, "ms": 9},
        {"type": "hangar.tree", "tool_use_id": "toolu_2", "skipped": "slow", "ms": 2400},
        {"type": "hangar.tree", "tool_use_id": "toolu_3", "tree": "not-a-sha"},
    ]).encode() + b"\n"
    evs, _ = parse_log(log, "claude-code")
    assert [e.data for e in evs] == [
        {"source": "hangar", "action": "tree", "tree": "a" * 40, "phase": "start", "ms": 4},
        {"source": "hangar", "action": "tree", "tree": "b" * 40, "id": "toolu_1", "ms": 9},
        {"source": "hangar", "action": "tree", "skipped": "slow", "id": "toolu_2", "ms": 2400}]


async def test_one_shot_turns_record_start_and_end_trees_by_exec(tmp_path):
    repo = _repo()

    async def exec_(sbid, cmd, **kw):
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)
        return ExecResult(exit_code=r.returncode, stdout=r.stdout, stderr=r.stderr)

    svc = SimpleNamespace(fleet=SimpleNamespace(exec=exec_))
    turn = SessionTurn(message="go")
    s = Session(name="s", environment="py", turns=[turn])
    sb = SimpleNamespace(id="sb-1", info={"repo_path": str(repo)})
    a = await gittree.oneshot(svc, s, turn, sb, "run-00000001", "start")
    (repo / "z.py").write_text("z")
    b = await gittree.oneshot(svc, s, turn, sb, "run-00000001", "end")
    assert a and b and a != b and b == _by_hand(repo)
    assert turn.git_trees == {"start": a, "end": b, "trusted": False}  # on the turn: the event log is unchanged
    attrs = traces.tree_attrs([], turn)
    assert attrs["hangar.git.tree.start"] == a and attrs["hangar.git.tree.end"] == b
    assert attrs["hangar.git.tree.trusted"] is False
    live = SessionTurn(message="go", live=True)  # the driver hashes a live turn's trees itself
    assert await gittree.oneshot(svc, s, live, sb, "run-00000002", "start") is None and live.git_trees is None
