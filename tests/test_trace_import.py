"""`hangar trace import` (KAN-338): a Claude Code transcript hangar didn't run, as hangar events and a trace.

Every fixture here is synthetic: a made-up session in each input shape (a local transcript with a subagent's own
transcript next to it, and a cloud session's events), with usage blocks, a Bash call whose test run and commit come
after the 200-character preview, an Edit and a Write, and a subagent's steps."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hangar import cli, trace_import
from hangar.events import RunEvent, parse_log

SID = "0b6c2f6e-5d1a-4c55-9e43-0d5c3c1f7a10"
MODEL = "claude-opus-5-5"
HEREDOC = "cat > notes.txt <<'EOF'\n" + "a line of release notes that goes on for a while\n" * 6 + "EOF\n"
COMMAND = HEREDOC + "uv run pytest -q tests && git commit -am 'fix the parser'"
FLATTEN = Path(__file__).resolve().parents[2] / "flightdeck/src/flightdeck/dataplane/flatten.py"  # a sibling


def _usage(inp: int, out: int, read: int, w5: int, w1h: int) -> dict[str, Any]:
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": w5 + w1h,
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1h}}


def _assistant(mid: str, block: dict, usage: dict, ts: str, **extra: Any) -> dict[str, Any]:
    return {"type": "assistant", "sessionId": SID, "timestamp": ts, "isSidechain": False, "version": "2.1.300",
            "cwd": "/repo", "message": {"id": mid, "model": MODEL, "role": "assistant", "content": [block],
                                        "stop_reason": "tool_use", "usage": usage}, **extra}


def _result(tid: str, text: str, ts: str, res: Any = None, err: bool = False, **extra: Any) -> dict[str, Any]:
    line = {"type": "user", "sessionId": SID, "timestamp": ts, "isSidechain": False,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text,
                                                     "is_error": err}]}, **extra}
    if res is not None:
        line["toolUseResult"] = res
    return line


def _prompt(text: str, ts: str, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "sessionId": SID, "timestamp": ts, "isSidechain": False,
            "message": {"role": "user", "content": text}, **extra}


def _tool(tid: str, name: str, args: dict) -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


MAIN = [
    {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-11-01T10:00:00.000Z"},
    _prompt("<session-context>made up</session-context>", "2026-11-01T10:00:00.100Z", isMeta=True),
    {"type": "attachment", "sessionId": SID, "timestamp": "2026-11-01T10:00:00.200Z", "attachment": {"type": "date"}},
    _prompt("Fix the parser and commit it.", "2026-11-01T10:00:01.000Z"),
    _assistant("msg_a1", {"type": "text", "text": "Running the tests first."}, _usage(3, 40, 1000, 200, 300),
               "2026-11-01T10:00:02.000Z"),
    _assistant("msg_a1", _tool("toolu_bash", "Bash", {"command": COMMAND, "description": "test and commit"}),
               _usage(3, 40, 1000, 200, 300), "2026-11-01T10:00:02.500Z"),
    _result("toolu_bash", "3 passed", "2026-11-01T10:00:05.000Z",
            {"stdout": "3 passed", "stderr": "", "interrupted": False}),
    _assistant("msg_a2", _tool("toolu_edit", "Edit", {"file_path": "/repo/src/parse.py", "old_string": "a",
                                                      "new_string": "b"}),
               _usage(2, 25, 1500, 0, 120), "2026-11-01T10:00:06.000Z"),
    _result("toolu_edit", "The file was updated.", "2026-11-01T10:00:06.500Z",
            {"filePath": "/repo/src/parse.py", "oldString": "a", "newString": "b"}),
    _assistant("msg_a3", _tool("toolu_agent", "Agent", {"subagent_type": "Explore", "description": "find callers",
                                                        "prompt": "Find who calls parse()."}),
               _usage(2, 30, 1700, 0, 0), "2026-11-01T10:00:07.000Z"),
    _result("toolu_agent", "Async agent launched.", "2026-11-01T10:00:07.200Z",
            {"isAsync": True, "status": "async_launched", "agentId": "sub1"}),
    _prompt("Now write the docs.", "2026-11-01T10:01:00.000Z"),
    _assistant("msg_a4", _tool("toolu_write", "Write", {"file_path": "/repo/docs/parse.md", "content": "# Parse"}),
               _usage(4, 60, 2000, 50, 0), "2026-11-01T10:01:02.000Z"),
    _result("toolu_write", "File created.", "2026-11-01T10:01:02.400Z",
            {"type": "create", "filePath": "/repo/docs/parse.md", "content": "# Parse"}),
    _result("toolu_missing", "denied", "2026-11-01T10:01:03.000Z", "Error: denied", err=True),
]
SUB = [
    {**_prompt("Find who calls parse().", "2026-11-01T10:00:07.300Z"), "isSidechain": True, "agentId": "sub1"},
    {**_assistant("msg_s1", _tool("toolu_grep", "Grep", {"pattern": "parse\\(", "path": "/repo"}),
                  _usage(5, 15, 400, 30, 0), "2026-11-01T10:00:08.000Z"), "isSidechain": True, "agentId": "sub1"},
    {**_result("toolu_grep", "src/cli.py", "2026-11-01T10:00:08.500Z", {"mode": "files_with_matches"}),
     "isSidechain": True, "agentId": "sub1"},
]


def _jsonl(path: Path, objs: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(o) + "\n" for o in objs))
    return path


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    p = _jsonl(tmp_path / "projects" / "-repo" / f"{SID}.jsonl", MAIN)
    sub = p.with_suffix("") / "subagents"
    _jsonl(sub / "agent-sub1.jsonl", SUB)
    (sub / "agent-sub1.meta.json").write_text(json.dumps({"agentType": "Explore", "toolUseId": "toolu_agent"}))
    return p


def _attrs(sp: dict) -> dict[str, Any]:
    out = {}
    for kv in sp.get("attributes", []):
        v = kv["value"]
        out[kv["key"]] = ([next(iter(x.values())) for x in v["arrayValue"]["values"]] if "arrayValue" in v
                          else int(v["intValue"]) if "intValue" in v else next(iter(v.values())))
    return out


def _spans(trace: dict) -> list[dict]:
    return [sp for rs in trace["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]]


def _stream(objs: list[dict], parent: str | None = None) -> list[dict]:
    """The stream-json lines Claude Code would have printed for these transcript lines."""
    out = []
    for o in objs:
        if o.get("type") not in ("assistant", "user"):
            continue
        line = {"type": o["type"], "message": o["message"], "parent_tool_use_id": parent, "session_id": SID}
        if "toolUseResult" in o:
            line["tool_use_result"] = o["toolUseResult"]
        out.append(line)
    return out


IMPORT_ONLY = ("line", "agent_id", "sidechain")


def _stream_src(objs: list[dict]) -> list[dict]:
    return [o for o in objs if o.get("type") in ("assistant", "user")]


def test_events_are_the_parsers_on_the_same_stream_json(transcript: Path) -> None:
    imp = trace_import.import_transcript(transcript)
    order = sorted([(o["timestamp"], 0, o) for o in _stream_src(MAIN)] + [(o["timestamp"], 1, o) for o in SUB],
                   key=lambda t: (t[0], t[1]))
    stream = [s for _, f, o in order for s in _stream([o], "toolu_agent" if f else None)]
    want, _ = parse_log("".join(json.dumps(s) + "\n" for s in stream).encode(), harness="claude-code")
    got = [ev for ev in imp.events if ev.data.get("role") != "user"]

    def key(ev: RunEvent) -> tuple:
        return ev.kind, ev.text, {k: v for k, v in ev.data.items() if k not in IMPORT_ONLY}

    assert [key(e) for e in got] == [key(e) for e in want]
    assert [ev.seq for ev in imp.events] == list(range(1, len(imp.events) + 1))
    prompts = [ev for ev in imp.events if ev.data.get("role") == "user"]
    assert [(p.turn, p.text) for p in prompts] == [(1, "Fix the parser and commit it."), (2, "Now write the docs.")]
    assert imp.fmt == "claude-transcript" and imp.session_id == SID and len(imp.turns) == 2



def test_shell_ops_come_from_the_whole_command(transcript: Path) -> None:
    imp = trace_import.import_transcript(transcript)
    cmd = next(ev for ev in imp.events if ev.kind == "command")
    assert len(COMMAND) > 200 and cmd.text.endswith("…") and "pytest" not in cmd.text
    assert cmd.data["ops"] == "test,git.commit" and cmd.data["clipped"] is True and cmd.data["id"] == "toolu_bash"
    sp = next(_attrs(s) for s in _spans(imp.trace) if _attrs(s).get("gen_ai.tool.call.id") == "toolu_bash")
    assert sp["hangar.tool.kind"] == "shell" and sp["hangar.shell.ops"] == ["test", "git.commit"]
    assert sp["hangar.tool.input_clipped"] is True and sp["hangar.tool.ok"] is True
    assert sp["hangar.response.id"] == "msg_a1"


def test_tool_kinds_files_and_turns(transcript: Path) -> None:
    imp = trace_import.import_transcript(transcript)
    spans = [_attrs(s) for s in _spans(imp.trace)]
    kinds = {a["gen_ai.tool.name"]: a["hangar.tool.kind"] for a in spans if a["hangar.kind"] == "tool.call"}
    assert kinds.pop("?") == "other"  # a result whose call isn't in the transcript, as hangar exports one
    assert kinds == {"Bash": "shell", "Edit": "edit", "Grep": "search", "Write": "edit"}
    changes = [(ev.turn, ev.data["path"], ev.data["op"]) for ev in imp.events if ev.kind == "file_change"]
    assert changes == [(1, "/repo/src/parse.py", "write"), (2, "/repo/docs/parse.md", "create")]
    edit = next(a for a in spans if a.get("gen_ai.tool.name") == "Edit")
    assert edit["hangar.file.repo_path"] == "src/parse.py"  # the transcript's cwd is the checkout
    turns = [a for a in spans if a["hangar.kind"] == "turn"]
    assert [t["hangar.turn.number"] for t in turns] == [1, 2]
    assert turns[0]["hangar.run.id"] == f"{SID}/turn/1" and turns[0]["hangar.harness"] == "claude-code"
    assert all(a["hangar.kind"] != "session" or a["hangar.import.format"] == "claude-transcript" for a in spans)
    failed = next(ev for ev in imp.events if ev.kind == "tool_result" and ev.data["id"] == "toolu_missing")
    assert failed.data["ok"] is False and failed.turn == 2


def test_a_subagents_steps_hang_under_its_call(transcript: Path) -> None:
    imp = trace_import.import_transcript(transcript)
    grep = [ev for ev in imp.events if ev.data.get("id") == "toolu_grep"]
    assert grep and all(ev.data["parent_id"] == "toolu_agent" and ev.data["agent_id"] == "sub1" for ev in grep)
    assert all(ev.turn == 1 for ev in grep)  # the turn of the call that started it
    spans = _spans(imp.trace)
    agent = next(s for s in spans if _attrs(s)["hangar.kind"] == "subagent")
    assert _attrs(agent)["gen_ai.agent.name"] == "Explore"
    for s in spans:
        a = _attrs(s)
        if a.get("gen_ai.tool.call.id") == "toolu_grep" or a.get("gen_ai.response.id") == "msg_s1":
            assert s["parentSpanId"] == agent["spanId"] and a["hangar.subagent.call.id"] == "toolu_agent"


def test_each_model_call_keeps_its_own_usage(transcript: Path) -> None:
    imp = trace_import.import_transcript(transcript)
    calls = [ev for ev in imp.events if ev.kind == "usage"]
    assert [ev.data["message_id"] for ev in calls] == ["msg_a1", "msg_a2", "msg_a3", "msg_s1", "msg_a4"]
    first = calls[0].data
    assert (first["call_input_tokens"], first["call_output_tokens"], first["call_cache_read_tokens"],
            first["call_cache_write_tokens"], first["call_cache_write_5m_tokens"],
            first["call_cache_write_1h_tokens"]) == (3, 40, 1000, 500, 200, 300)
    assert first["call_cost_usd"] > 0 and "call_partial" not in first
    models = {_attrs(s)["gen_ai.response.id"]: _attrs(s) for s in _spans(imp.trace)
              if _attrs(s)["hangar.kind"] == "model.call"}
    m = models["msg_a1"]
    assert m["gen_ai.usage.input_tokens"] == 1503 and m["gen_ai.usage.output_tokens"] == 40
    assert m["hangar.usage.cache_write_5m.input_tokens"] == 200 and m["hangar.usage.cache_write_1h.input_tokens"] == 300
    turn1 = next(_attrs(s) for s in _spans(imp.trace) if _attrs(s).get("hangar.turn.number") == 1)
    assert turn1["hangar.usage.source"] == "calls"  # no result lines in a local transcript
    assert turn1["gen_ai.usage.output_tokens"] == 40 + 25 + 30 + 15  # the subagent's call counts in its turn


# A cloud session's events (as list_events returns them), shaped like a real multi-turn session: messages name no
# model, their usage is written mid-stream (no stop reason, a short output count), and each `result` has the run's
# own usage with the process's running total cost. The last result's usage alone undercounts the session.
def _cloud(kind: str, ts: str, **catchall: Any) -> dict[str, Any]:
    return {"created_at": ts, kind: {"uuid": f"u-{ts}", "internal_anthropic_catchall": {
        "session_id": "session_01FAKEcloudSESSION", **catchall}}}


def _cloud_msg(mid: str, block: dict, out: int, ts: str) -> dict[str, Any]:
    return _cloud("assistant", ts, timestamp=ts, parent_tool_use_id=None, message={
        "id": mid, "role": "assistant", "content": [block], "stop_reason": None,
        "usage": _usage(2, out, 5000, 0, 100)})


CLOUD = [
    _cloud("user", "2026-11-02T09:00:00Z", message={"role": "user", "content": "Check the build."}),
    _cloud("system", "2026-11-02T09:00:01Z", subtype="init", model=MODEL),
    _cloud_msg("msg_c1", _tool("toolu_c1", "Bash", {"command": "make test"}), 8, "2026-11-02T09:00:02Z"),
    _cloud("user", "2026-11-02T09:00:03Z", timestamp="2026-11-02T09:00:03Z", parent_tool_use_id=None,
           message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_c1", "content": "ok"}]}),
    _cloud("result", "2026-11-02T09:00:04Z", subtype="success", total_cost_usd=0.75,
           usage=_usage(20, 4022, 900000, 0, 61000)),
    _cloud("system", "2026-11-02T09:00:10Z", subtype="init", model=MODEL),  # a run no prompt started (a wake)
    _cloud_msg("msg_c2", {"type": "text", "text": "Done."}, 3, "2026-11-02T09:00:11Z"),
    _cloud("result", "2026-11-02T09:00:12Z", subtype="success", total_cost_usd=0.95,
           usage=_usage(8, 1470, 470000, 0, 4600)),
    _cloud("user", "2026-11-02T09:01:00Z", message={"role": "user", "content": "And the docs?"}),
    _cloud_msg("msg_c3", {"type": "text", "text": "They build."}, 5, "2026-11-02T09:01:02Z"),
    _cloud("result", "2026-11-02T09:01:03Z", subtype="success", total_cost_usd=1.25,
           usage=_usage(6, 63, 450000, 0, 1400)),
]


def test_a_cloud_session_sums_its_results(tmp_path: Path) -> None:
    p = tmp_path / "session.events.json"
    p.write_text(json.dumps(CLOUD))
    imp = trace_import.import_transcript(p)
    assert imp.fmt == "cloud-events" and imp.session_id == "session_01FAKEcloudSESSION" and len(imp.turns) == 2
    spans = [_attrs(s) for s in _spans(imp.trace)]
    session = next(a for a in spans if a["hangar.kind"] == "session")
    turns = [a for a in spans if a["hangar.kind"] == "turn"]
    assert [t["gen_ai.usage.output_tokens"] for t in turns] == [4022 + 1470, 63]
    assert session["gen_ai.usage.output_tokens"] == 4022 + 1470 + 63  # not the last result's 63
    assert session["hangar.cost_usd"] == pytest.approx(1.25)  # the last running total, not a sum of them
    assert [t["hangar.cost_usd"] for t in turns] == [pytest.approx(0.95), pytest.approx(0.30)]
    assert all(t["hangar.usage.source"] == "result" for t in turns)
    assert turns[0]["hangar.usage.calls.output_tokens"] == 8 + 3  # what the streamed messages claimed
    models = [a for a in spans if a["hangar.kind"] == "model.call"]
    assert len(models) == 3 and all(m["hangar.usage.partial"] is True for m in models)
    assert all(m["gen_ai.response.model"] == MODEL for m in models)  # from the init line
    jsonl = tmp_path / "session.jsonl"  # the same events, one per line
    jsonl.write_text("".join(json.dumps(o) + "\n" for o in CLOUD))
    again = trace_import.import_transcript(jsonl)
    assert [(e.kind, e.text, e.data) for e in again.events] == [(e.kind, e.text, e.data) for e in imp.events]


QUEUED = [  # a second prompt reaches the same CLI run: one result covers both turns
    _cloud("user", "2026-11-02T09:00:00Z", message={"role": "user", "content": "Check the build."}),
    _cloud("system", "2026-11-02T09:00:01Z", subtype="init", model=MODEL),
    _cloud_msg("msg_q1", {"type": "text", "text": "Running it."}, 8, "2026-11-02T09:00:02Z"),
    _cloud("user", "2026-11-02T09:00:03Z", message={"role": "user", "content": "And the docs?"}),
    _cloud_msg("msg_q2", {"type": "text", "text": "Both pass."}, 5, "2026-11-02T09:00:04Z"),
    _cloud("result", "2026-11-02T09:00:05Z", subtype="success", total_cost_usd=0.75,
           usage=_usage(20, 4022, 900000, 0, 61000)),
]


def test_a_result_that_covers_two_prompts_is_counted_once(tmp_path: Path) -> None:
    p = tmp_path / "session.events.json"
    p.write_text(json.dumps(QUEUED))
    spans = [_attrs(s) for s in _spans(trace_import.import_transcript(p).trace)]
    session = next(a for a in spans if a["hangar.kind"] == "session")
    first, second = [a for a in spans if a["hangar.kind"] == "turn"]
    assert first["hangar.usage.source"] == "calls" and second["hangar.usage.source"] == "result"
    assert first["hangar.cost_usd"] + second["hangar.cost_usd"] == pytest.approx(0.75)  # not 0.75 plus the first's
    assert session["hangar.cost_usd"] == pytest.approx(0.75)
    assert first["gen_ai.usage.output_tokens"] + second["gen_ai.usage.output_tokens"] == 4022


def test_secret_patterns_are_masked(tmp_path: Path) -> None:
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    p = _jsonl(tmp_path / f"{SID}.jsonl", [
        _prompt("Use it.", "2026-11-01T10:00:00Z"),
        _assistant("msg_x", _tool("toolu_x", "Bash", {"command": f"echo {token}"}), _usage(1, 1, 0, 0, 0),
                   "2026-11-01T10:00:01Z")])
    imp = trace_import.import_transcript(p)
    assert token not in json.dumps([e.model_dump() for e in imp.events]) and token not in json.dumps(imp.trace)


def test_the_cli_writes_events_and_a_trace_flightdeck_reads(transcript: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    r = CliRunner().invoke(cli.app, ["trace", "import", str(transcript), "--out", str(out)])
    assert r.exit_code == 0, r.output
    summary = json.loads(r.stdout[: r.stdout.rindex("}") + 1])
    assert summary["turns"] == 2 and summary["spans"]["shell"] == 1 and summary["spans"]["subagent"] == 1
    lines = (out / "events.jsonl").read_text().splitlines()
    evs = [RunEvent.model_validate_json(ln) for ln in lines]
    assert [e.seq for e in evs] == list(range(1, len(evs) + 1)) and all(ln.startswith('{"seq":') for ln in lines)
    assert all(e.data.get("id") for e in evs if e.kind in ("command", "tool_call", "tool_result"))
    trace = json.loads((out / "trace.json").read_text())
    res = _attrs(trace["resourceSpans"][0]["resource"])
    assert res["hangar.trace.schema"] == "hangar.trace/v1" and res["hangar.export.scope"] == "session"
    ids = {s["spanId"] for s in _spans(trace)}
    assert all(s.get("parentSpanId") in ids for s in _spans(trace) if _attrs(s)["hangar.kind"] != "session")
    if not FLATTEN.exists():
        pytest.skip("flightdeck isn't checked out next to hangar")
    spec = importlib.util.spec_from_file_location("fd_flatten", FLATTEN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = mod.flatten_trace(trace)
    assert [r["turn"] for r in rows["runs"]] == [1, 2] and rows["runs"][0]["harness"] == "claude-code"
    shell = next(r for r in rows["spans"] if r.get("tool_kind") == "shell")
    assert shell["shell_ops"] == ["test", "git.commit"] and shell["run_id"] == f"{SID}/turn/1"
