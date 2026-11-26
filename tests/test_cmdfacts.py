"""--- KAN-287: the files a command writes, the programs it runs and whether its tests only probed, read from the whole
command before its event is clipped."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from hangar import cmdfacts, traces
from hangar.cmdfacts import facts, split
from hangar.events import BRIEF_MAX, Activity, Parser, RunEvent
from hangar.models import Session, SessionTurn


def writes(cmd: str) -> list[str]:
    return split(facts(cmd)["writes"])


def runs(cmd: str) -> list[str]:
    return split(facts(cmd)["runs"])


@pytest.mark.parametrize(("cmd", "want"), [
    ("echo hi > out.txt", ["out.txt"]),
    ("make 2> err.log >> build.log", ["err.log", "build.log"]),
    ("cmd &> all.log; cmd >| forced.txt", ["all.log", "forced.txt"]),
    ("cmd > /dev/null 2>&1; cmd 2>/dev/null; cmd >&2", []),
    ("cat a | tee -a log.txt copy.txt > /dev/null", ["log.txt", "copy.txt"]),
    ("sed -i 's/a/b/' src/a.py src/b.py", ["src/a.py", "src/b.py"]),
    ("sed -i.bak -e 's/a/b/' -e 's/c/d/' src/c.py", ["src/c.py"]),
    ("sed 's/a/b/' src/a.py", []),
    ("perl -pi -e 's/x/y/' a.pl b.pl", ["a.pl", "b.pl"]),
    ("cp a.txt dest/ && mv x y && install -m 755 tool bin/tool", ["dest/", "y", "bin/tool"]),
    ("cp -t out/ a b", ["out/"]),
    ("touch -d 2020-01-01 stamp new.txt", ["stamp", "new.txt"]),
    ("patch -p1 src/a.py < fix.diff; patch -o out.py a.py fix.diff", ["src/a.py", "out.py"]),
    ("cat <<EOF > notes.md\nhello > not-a-file\nEOF", ["notes.md"]),
    ("echo 'a > b' \"c > d\" # e > f", []),
])
def test_writes(cmd: str, want: list[str]) -> None:
    assert writes(cmd) == want


def test_a_diff_fed_to_git_apply_patch_or_apply_patch_writes_its_files() -> None:
    diff = "--- a/x.py\n+++ b/x.py\n@@\n-a\n+b\n--- /dev/null\n+++ b/new.py\n@@\n+c\n--- a/gone.py\n+++ /dev/null\n"
    assert writes(f"git -C repo apply <<'EOF'\n{diff}EOF") == ["x.py", "new.py"]
    assert writes(f"patch -p1 <<'EOF'\n{diff}EOF") == ["x.py", "new.py"]
    codex = "*** Begin Patch\n*** Update File: src/a.py\n@@\n*** Add File: src/b.py\n+x\n*** End Patch\n"
    assert writes(f"apply_patch <<'PATCH'\n{codex}PATCH") == ["src/a.py", "src/b.py"]
    assert writes(f"cat <<'EOF'\n{diff}EOF") == []  # a diff that is only printed writes nothing


def test_python_writes_in_a_heredoc_or_c_string() -> None:
    body = "from pathlib import Path\nPath('gen.txt').write_text('x')\nopen(\"o.json\", \"w\").write('1')\n"
    body += "open('in.txt').read()\nopen('log.txt', mode='a')\nPath(\"b.bin\").write_bytes(b'')\n"
    assert writes(f"python3 - <<'PY'\n{body}PY") == ["gen.txt", "o.json", "log.txt", "b.bin"]
    assert writes("python -c \"open('x.csv', 'w').write('a')\"") == ["x.csv"]
    assert writes("python script.py <<'PY'\nopen('no.txt', 'w')\nPY") == []  # stdin is the script's data


@pytest.mark.parametrize(("cmd", "want"), [
    ("pytest -q", ["pytest"]),
    ("cd /repo && FOO=1 python -m pytest tests -x", ["cd", "python -m pytest"]),
    ("uv run --project svc pytest -k foo && uv sync", ["pytest", "uv sync"]),
    ("git -C repo status && git commit -qm 'x; y'", ["git status", "git commit"]),
    ("gh pr create --fill", ["gh pr create"]),
    ("npm run build && npm run test:unit && npm install", ["npm run build", "npm run test:unit", "npm install"]),
    ("sudo -u app timeout 30 env A=1 ./scripts/check.sh", ["./scripts/check.sh"]),
    ("/usr/bin/git status", ["git status"]),
    ("python3 tools/gen.py --out x && node -e 'x'", ["python3 tools/gen.py", "node -e"]),
    ("for f in *.py; do ruff check $f; done", ["ruff check"]),
    ("if test -f a; then make test; else echo no; fi", ["test", "make test", "echo"]),
    ("bash -c 'echo \"a; b\" > f.txt && go test ./...'", ["bash -c", "echo", "go test"]),
    ("echo 'pytest && git push'", ["echo"]),
    ("cat <<EOF\npytest\nEOF", ["cat"]),
    ("ls \\\n  -la", ["ls"]),
])
def test_runs(cmd: str, want: list[str]) -> None:
    assert runs(cmd) == want


@pytest.mark.parametrize(("cmd", "want"), [
    ("pytest --version", True),
    ("pytest --collect-only -q tests/", True),
    ("python -m pytest --co && npx jest --listTests", True),
    ("npm test -- --listTests", True),
    ("timeout 30 cargo test --version", True),
    ("pytest --version && pytest -q", None),  # a probe, then a real run
    ("pytest -q", None),
    ("ctest -V", None),  # ctest's -V is "verbose": the tests run
    ("ls --help", None),  # not a test runner
    ("echo hi", None),
])
def test_probe(cmd: str, want: bool | None) -> None:
    assert facts(cmd)["probe"] is want


def test_lists_are_bounded() -> None:
    cmd = "; ".join(f"touch f{i}" for i in range(50)) + "; touch " + "x" * 400
    got = split(facts(cmd)["writes"])
    assert len(got) == cmdfacts.MAX_ITEMS and got[0] == "f0"
    assert all(len(w) <= cmdfacts.MAX_LEN for w in split(facts("touch " + "x" * 400)["writes"]))
    assert facts("") == {"writes": None, "runs": None, "probe": None} == facts(None)
    assert split(None) == [] and split("a\n\nb") == ["a", "b"]
    assert facts("echo 'unclosed > x")["runs"] == "echo"  # a broken quote is read as plain words


@pytest.mark.parametrize(("cmd", "w", "r"), [
    # a heredoc body is read as it is: an apostrophe or a quote in it doesn't hide what follows
    ("cat > README.md <<'EOF'\nDon't run this\nEOF\npytest -q", ["README.md"], ["cat", "pytest"]),
    ("cat > a.txt <<EOF\nsay \"hi\nEOF\nmake", ["a.txt"], ["cat", "make"]),
    # Claude Code's commit form: the heredoc sits in quotes, and the next line is still a command
    ("git commit -m \"$(cat <<'EOF'\nfix: it's done\nEOF\n)\"\ngit push", [], ["git commit", "git push"]),
    ("grep -c x <<< hello\npytest -q", [], ["grep", "pytest"]),  # a here-string is not a heredoc
    ("cat <<-EOF > t.txt\n\tbody\n\tEOF\nls", ["t.txt"], ["cat", "ls"]),  # <<- ends at a tab-indented delimiter
    ("cat <<EOF\nrm x > hosed\n  EOF\nEOF\nls", [], ["cat", "ls"]),  # an indented delimiter doesn't end <<EOF
    ("cat <<\\EOF\nrm x > hosed\nEOF\nls", [], ["cat", "ls"]),
    ("echo $((1<<4)) > n.txt\nmake", ["n.txt"], ["echo", "make"]),  # a shift, not a heredoc
    ("echo \\\"hi\\\" > o\npytest", ["o"], ["echo", "pytest"]),  # an escaped quote opens nothing
    ("(cd x && make)>log", ["log"], ["cd", "make"]),
    ("true&&(make test)", [], ["true", "make test"]),
    ("diff <(sort a) <(sort b) > d.txt", ["d.txt"], ["sort", "diff"]),
    ("tee >(cat) out.txt", ["out.txt"], ["cat", "tee"]),
    ("echo \"$(date)\" $(git rev-parse HEAD) > v", ["v"], ["git rev-parse", "echo"]),
    ("ls;#c > x", [], ["ls"]),
    ("sed -i '' 's/a/b/' f.txt", ["f.txt"], ["sed"]),  # BSD sed: -i takes an empty suffix
    ("perl -i -pe 's/a/b/' f", ["f"], ["perl -e"]),
    ("python -W ignore x.py && python -X dev -m pytest --co", [], ["python x.py", "python -m pytest"]),
    ("ruby -I lib t.rb && node --require m app.js", [], ["ruby t.rb", "node app.js"]),
    ("kubectl -n ns get pods && npm --prefix web test && cargo +nightly test", [],
     ["kubectl get", "npm test", "cargo test"]),
    ("pnpm exec jest --listTests", [], ["jest"]),
])
def test_what_a_shell_would_see(cmd: str, w: list[str], r: list[str]) -> None:
    assert (writes(cmd), runs(cmd)) == (w, r)


@pytest.mark.parametrize(("cmd", "w", "r"), [
    # a heredoc whose end line never matches runs to the end of the command, as in bash
    ("cat <<EOF > f\nbody\nEOF  \nmake", ["f"], ["cat"]),
    ("cat <<EOF > f\nbody\n  EOF\nmake", ["f"], ["cat"]),
    ("cat > NOTES.md <<'EOF'\n" + "git push --force origin main > pushed.txt\n" * 3000 + "EOF\npytest",
     ["NOTES.md"], ["cat"]),  # cut at 64 KiB inside the body: nothing in it is a command
    ("cat <<EOF > f\r\nbody\r\nEOF\r\nmake", ["f"], ["cat", "make"]),  # CRLF lines
    # a heredoc in a quoted command substitution, with an odd quote in its body
    ("git commit -m \"$(cat <<'EOF'\nfix: 12\" display\nEOF\n)\"\ngit push\npytest -q", [],
     ["git commit", "git push", "pytest"]),
    ("gh pr create --title x --body \"$(cat <<'EOF'\n## Summary\n- it's \"done\"\nEOF\n)\" && echo ok", [],
     ["gh pr create", "echo"]),
    ("echo \"$(echo \"a; b\")\" > x.txt", ["x.txt"], ["echo"]),
    ("x=$((2<<3)); (( y = 1 << 2 )); echo $x > o", ["o"], ["echo"]),
    ("echo `rm x > y` > z", ["y", "z"], ["rm", "echo"]),
    ("gh api -H \"Authorization: token abc\" /user && gh run list -L 5", [], ["gh api", "gh run list"]),
    ("command -v pytest && time -p make", [], ["command", "make"]),
    ("mv -- -x t && install -d a b", ["t", "a", "b"], ["mv", "install"]),
])
def test_heredocs_and_substitutions(cmd: str, w: list[str], r: list[str]) -> None:
    assert (writes(cmd), runs(cmd)) == (w, r)


def test_command_v_is_not_a_test_run() -> None:
    assert facts("command -v pytest && pytest --version")["probe"] is True


def test_an_item_is_one_line_and_a_list_has_a_byte_cap() -> None:
    odd = "a\rb\x0bc\x0cd\x1ee\x85f\u2028g\u2029h\x00i"
    assert cmdfacts.join([odd]) == "a b c d e f g h i" and len(cmdfacts.join([odd]).splitlines()) == 1
    wide = ["\U0001f600" * 256 + str(i) for i in range(32)]  # 1 KiB an item in UTF-8
    assert len(cmdfacts.join(wide).encode()) <= cmdfacts.MAX_BYTES


def test_python_writes_with_other_arguments_first() -> None:
    assert writes("python -c \"open('a.txt', encoding='utf-8', mode='w')\"") == ["a.txt"]
    assert writes("python -c \"open('b.txt', encoding='latin-1')\"") == []  # its mode is read
    assert writes("python -c 'open(f\"{x}\", \"w\")'") == []  # a path built at run time


def test_probes_through_flags_and_wrappers() -> None:
    assert facts("python -X dev -m pytest --co")["probe"] is True
    assert facts("npm --prefix web test -- --listTests")["probe"] is True
    assert facts("pnpm exec jest --listTests")["probe"] is True
    assert facts("mvn test -V")["probe"] is None  # maven's -V prints the version and builds


@pytest.mark.parametrize("cmd", [
    "if " * 100_000 + "ls", "A=1 " * 262_144, 'python -c "' + "x" * 1_000_000 + '"', "\n" * 1_000_000,
    "a;" * 500_000, "'" * 100_001, "$(" * 50_000, "cat <<E\n" * 10_000, "> a " * 100_000, "bash -c '" * 1_000,
])
def test_a_huge_command_is_read_quickly(cmd: str) -> None:
    started = time.monotonic()
    facts(cmd)
    assert time.monotonic() - started < 5  # about 0.1 s each; a slow CI runner gets room


def _events(*calls: dict) -> dict[str, RunEvent]:
    lines = [{"type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use", **c} for c in calls]}}]
    evs = Parser("claude").feed(("\n".join(json.dumps(x) for x in lines) + "\n").encode())
    return {ev.data["id"]: ev for ev in evs if ev.kind in ("command", "tool_call")}


def test_a_clipped_command_keeps_the_facts_past_its_preview() -> None:
    """A heredoc writes a file and the tests run after the 200-character cut: the event still has both, and the same
    command short enough not to be clipped has the same facts."""
    long_cmd = "cat > src/a.py <<'EOF'\n" + "x = 1\n" * 60 + "EOF\nuv run pytest -q > test.log && git add -A"
    short_cmd = "cat > src/a.py <<'EOF'\nx = 1\nEOF\nuv run pytest -q > test.log && git add -A"
    ev = _events({"id": "t1", "name": "Bash", "input": {"command": long_cmd}},
                 {"id": "t2", "name": "Bash", "input": {"command": short_cmd}},
                 {"id": "t3", "name": "Edit", "input": {"file_path": "src/b.py", "old_string": "a", "new_string": "b"}},
                 {"id": "t4", "name": "Read", "input": {"file_path": "src/c.py"}})
    t1, t2 = ev["t1"].data, ev["t2"].data
    assert len(long_cmd) > BRIEF_MAX and t1["clipped"] is True and "pytest" not in ev["t1"].text
    assert t1["writes"] == "src/a.py\ntest.log" and t1["runs"] == "cat\npytest\ngit add" and "probe" not in t1
    assert {k: t2[k] for k in ("writes", "runs")} == {k: t1[k] for k in ("writes", "runs")}
    assert ev["t3"].data["writes"] == "src/b.py" and "writes" not in ev["t4"].data


def test_codex_patches_and_commands_get_the_facts() -> None:
    p = Parser("codex")
    lines = [{"type": "item.started", "item": {"id": "c1", "type": "command_execution",
                                              "command": "bash -lc 'pytest --version'"}},
             {"type": "item.started", "item": {"id": "p1", "type": "file_change",
                                              "changes": [{"path": "a.py"}, {"path": "b, c.py"}]}}]
    evs = {ev.data["id"]: ev for ev in p.feed(("\n".join(json.dumps(x) for x in lines) + "\n").encode())
           if ev.kind in ("command", "tool_call")}
    assert evs["c1"].data["runs"] == "bash -c\npytest" and evs["c1"].data["probe"] is True
    assert split(evs["p1"].data["writes"]) == ["a.py", "b, c.py"]  # each path, not their joined text


def test_trace_spans_carry_the_facts(settings, broker) -> None:
    """The tool.call span of a shell command has `hangar.shell.writes`, `.runs` and `.probe`; an event recorded before
    the parser stored them is read from its text."""
    only_writes = "A=1 B=2 C=" + "x" * 300 + " > out.txt"  # no program: the event has writes and no runs
    ev = _events({"id": "t1", "name": "Bash", "input": {"command": "pytest --co -q > list.txt"}},
                 {"id": "t3", "name": "Bash", "input": {"command": only_writes}})
    assert ev["t3"].data["writes"] == "out.txt" and "runs" not in ev["t3"].data
    old = RunEvent(turn=1, run_id="run-1", kind="command", text="make test > out.log", ts=1004.0,
                   data={"id": "t2", "tool": "Bash", "ops": "test"})  # recorded before KAN-287
    cut = RunEvent(turn=1, run_id="run-1", kind="command", text="make > build-…", ts=1005.0,
                   data={"id": "t4", "tool": "Bash", "clipped": True})  # an old clipped preview
    activity = Activity(settings, broker.redact)
    svc = SimpleNamespace(activity=activity, fleet=SimpleNamespace(broker=broker))
    t = SessionTurn(message="go", run_id="run-1", harness="claude-code", started_at=1000.0, finished_at=1100.0)
    s = Session(name="s-facts", environment="plain", harness="claude-code", turns=[t])
    activity.append(s.id, [ev["t1"].model_copy(update={"turn": 1, "run_id": "run-1", "ts": 1001.0}),
                           ev["t3"].model_copy(update={"turn": 1, "run_id": "run-1", "ts": 1002.0}), old, cut])
    export = traces.session_export(svc, s, turn=1)
    spans = [sp for rs in export["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]]
    calls = {}
    for sp in spans:
        a = {x["key"]: x["value"] for x in sp.get("attributes", [])}
        if a.get("hangar.kind", {}).get("stringValue") == "tool.call":
            calls[a["gen_ai.tool.call.id"]["stringValue"]] = a

    def strs(a: dict, key: str) -> list[str]:
        return [v["stringValue"] for v in a.get(key, {}).get("arrayValue", {}).get("values", [])]

    assert strs(calls["t1"], "hangar.shell.writes") == ["list.txt"] and strs(calls["t1"], "hangar.shell.runs") == [
        "pytest"]
    assert calls["t1"]["hangar.shell.probe"] == {"boolValue": True}
    assert strs(calls["t2"], "hangar.shell.writes") == ["out.log"] and strs(calls["t2"], "hangar.shell.runs") == [
        "make test"]
    assert "hangar.shell.probe" not in calls["t2"]
    assert strs(calls["t3"], "hangar.shell.writes") == ["out.txt"]  # read from the event, not from the preview
    assert "hangar.shell.writes" not in calls["t4"] and strs(calls["t4"], "hangar.shell.runs") == ["make"]
