"""Import a Claude Code transcript as hangar events and a trace, offline (KAN-338; docs/api/trace-import.md).

A session that hangar didn't run, such as a local Claude Code session or a cloud one, read from its transcript. Each
line becomes the Claude Code `stream-json` line it stands for and goes through hangar's own parser (events.Parser),
so the events are the ones a hangar run of the same session records. They are written as `events.jsonl`, in the
events API's shape, and as `trace.json`, a `hangar.trace/v1` session export built by traces.py.

Inputs, detected line by line:

* a local transcript, `~/.claude/projects/<dir>/<session>.jsonl`, with its subagents' transcripts next to it
  (`<session>/subagents/agent-<id>.jsonl`, each named by a `.meta.json` with the `toolUseId` that started it);
* cloud-session events as a JSON array or JSONL: `{"created_at", "assistant" | "user" | "system" | "result": {"uuid",
  "internal_anthropic_catchall": {...}}}`, the catchall holding the stream-json line's fields;
* stream-json itself.

A turn starts at each prompt a person (or the platform) sent: a top-level `user` line that answers no tool call.
Nothing is run and nothing is sent anywhere. Strings pass through secretscan's patterns, as hangar's outgoing text
does; there are no stored credentials offline to mask.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from hangar import secretscan, traces
from hangar.events import MESSAGE_MAX, Parser, RunEvent, _clip, add_usage
from hangar.models import Session, SessionTurn, Usage

KINDS = ("assistant", "user", "system", "result")
HARNESS = "claude-code"


@dataclass
class Line:
    rec: dict[str, Any]  # the stream-json line
    ts: float | None
    line: int  # 1-based, in its file
    file: int  # 0: the main transcript, then each subagent's
    agent: str | None = None  # the subagent's id (its transcript's name)
    sidechain: bool = False


@dataclass
class Turn:
    n: int
    prompt: str
    start: float | None
    end: float | None = None
    events: list[RunEvent] = field(default_factory=list)
    results: list[Usage] = field(default_factory=list)  # each `result` line's usage, its cost the turn's share
    calls: Usage | None = None  # the sum of its model calls' own usage


def redact(obj: Any) -> Any:
    if isinstance(obj, str):
        return secretscan.mask(obj)[0]
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _ts(v: Any) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v) / (1000 if v > 1e11 else 1)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def read_json(path: Path) -> list[Any]:
    """A JSON array, or JSON lines (a line that isn't JSON is skipped)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.lstrip().startswith("["):
        data = json.loads(text)
        return data if isinstance(data, list) else []
    out: list[Any] = []
    for ln in text.splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append(None)  # keeps line numbers
    return out


def normalize(obj: Any) -> tuple[dict[str, Any], Any, str] | None:
    """(the stream-json line, its timestamp, the input format) for one transcript line, or None to skip it."""
    if not isinstance(obj, dict):
        return None
    for k in KINDS:  # a cloud session's event
        v = obj.get(k)
        if isinstance(v, dict) and isinstance(v.get("internal_anthropic_catchall"), dict):
            rec = {**v["internal_anthropic_catchall"], "type": k, "uuid": v.get("uuid")}
            return rec, rec.get("timestamp") or obj.get("created_at"), "cloud-events"
    if obj.get("type") not in KINDS:
        return None  # attachments, queue operations, summaries: the transcript's own bookkeeping
    rec = dict(obj)
    fmt = "stream-json"
    if "toolUseResult" in rec or "sessionId" in rec:  # a local transcript's names for stream-json's fields
        fmt = "claude-transcript"
        rec.setdefault("tool_use_result", rec.pop("toolUseResult", None))
        rec.setdefault("session_id", rec.get("sessionId"))
    return rec, rec.get("timestamp"), fmt


def prompt_of(rec: dict[str, Any]) -> str | None:
    """The text of a prompt that starts a turn; None for any other line."""
    if rec.get("type") != "user" or rec.get("parent_tool_use_id") or rec.get("isSidechain"):
        return None
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    else:
        return None
    return None if text.startswith("[Request interrupted by user") else text


def _lines(objs: list[Any], file: int, agent: str | None = None, parent: str | None = None) -> tuple[list[Line], set]:
    out, fmts, last = [], set(), None
    for i, obj in enumerate(objs, 1):
        got = normalize(obj)
        if got is None:
            continue
        rec, ts, fmt = got
        fmts.add(fmt)
        if parent and not rec.get("parent_tool_use_id"):
            rec["parent_tool_use_id"] = parent
        t = _ts(ts)
        last = t if t is not None else last
        out.append(Line(rec, last, i, file, agent, bool(rec.get("isSidechain"))))
    return out, fmts


def subagents(path: Path) -> list[tuple[Path, str | None, str]]:
    """A local transcript's subagent transcripts: (file, the Task/Agent call id that started it, its agent id)."""
    d = path.with_suffix("") / "subagents"
    out = []
    for f in sorted(d.glob("agent-*.jsonl")) if d.is_dir() else []:
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(f.with_suffix(".meta.json").read_text())
        except (OSError, ValueError):
            pass
        tid = meta.get("toolUseId") if isinstance(meta.get("toolUseId"), str) else None
        out.append((f, tid, f.stem.removeprefix("agent-")))
    return out


def _result_usage(rec: dict[str, Any], prev_cost: float | None) -> tuple[Usage, float | None]:
    """A `result` line's usage (that run's tokens) and the session's cost so far. Its `total_cost_usd` is the
    process's running total, so this result's share is the difference from the last one (unless it went down: a new
    process)."""
    u = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
    cc = u.get("cache_creation")
    long = cc.get("ephemeral_1h_input_tokens") if isinstance(cc, dict) else 0
    total = rec.get("total_cost_usd")
    total = float(total) if isinstance(total, int | float) and not isinstance(total, bool) else None
    cost = None if total is None else total - prev_cost if prev_cost is not None and total >= prev_cost else total
    return Usage(input_tokens=u.get("input_tokens") or 0, output_tokens=u.get("output_tokens") or 0,
                 cache_read_tokens=u.get("cache_read_input_tokens") or 0,
                 cache_write_tokens=u.get("cache_creation_input_tokens") or 0,
                 cache_write_1h_tokens=long if isinstance(long, int) else 0, cost_usd=cost,
                 cost_source="harness" if cost is not None else None), total if total is not None else prev_cost


def _call_usage(d: dict[str, Any]) -> Usage:
    return Usage(input_tokens=d.get("call_input_tokens") or 0, output_tokens=d.get("call_output_tokens") or 0,
                 cache_read_tokens=d.get("call_cache_read_tokens") or 0,
                 cache_write_tokens=d.get("call_cache_write_tokens") or 0,
                 cache_write_1h_tokens=d.get("call_cache_write_1h_tokens") or 0, cost_usd=d.get("call_cost_usd"),
                 cost_source="prices" if d.get("call_cost_usd") is not None else None, model=d.get("model"))


@dataclass
class Imported:
    session_id: str
    fmt: str
    events: list[RunEvent]
    turns: list[Turn]
    trace: dict[str, Any]


def import_transcript(path: Path, session_id: str | None = None) -> Imported:
    objs = read_json(path)
    lines, fmts = _lines(objs, 0)
    for i, (f, tid, agent) in enumerate(subagents(path), 1):
        more, _ = _lines(read_json(f), i, agent, tid)
        lines += more
    first = next((ln.ts for ln in lines if ln.ts is not None), None)
    lines.sort(key=lambda ln: (ln.ts if ln.ts is not None else first or 0.0, ln.file, ln.line))
    # A cloud session names two: the platform's (`session_...`) and its CLI's conversation; the platform's wins
    ids = [str(ln.rec["session_id"]) for ln in lines if ln.rec.get("session_id")]
    sid = session_id or next((i for i in ids if i.startswith("session_")), ids[0] if ids else path.stem)
    fmt = "claude-transcript" if "claude-transcript" in fmts else min(fmts) if fmts else "unknown"

    parser = Parser("claude")
    turns: list[Turn] = []
    tool_turn: dict[str, int] = {}  # tool call id -> its turn: a subagent's steps belong to its caller's turn
    events: list[RunEvent] = []
    model = version = cwd = None
    cost_so_far: float | None = None
    for ln in lines:
        rec = ln.rec
        m = (rec.get("message") or {}).get("model") if rec.get("type") == "assistant" else None
        model = model or (m if m != "<synthetic>" else None)
        version, cwd = version or rec.get("version"), cwd or rec.get("cwd")
        text = None if ln.file else prompt_of(rec)
        if text is not None:
            turns.append(Turn(len(turns) + 1, text, ln.ts))
            events.append(_stamp(RunEvent(kind="message", text=_clip(text, MESSAGE_MAX), data={"role": "user"}),
                                 ln, turns[-1], sid))
        evs = parser.feed(json.dumps(rec, ensure_ascii=False).encode() + b"\n")
        if not turns:  # steps before any prompt (a transcript that starts mid-session): a turn with no prompt
            if not evs and rec.get("type") != "result":
                continue
            turns.append(Turn(1, "", ln.ts))
        parent = rec.get("parent_tool_use_id")
        turn = turns[tool_turn[parent] - 1] if isinstance(parent, str) and parent in tool_turn else turns[-1]
        if rec.get("type") == "result" and not ln.file:
            u, cost_so_far = _result_usage(rec, cost_so_far)
            turn.results.append(u)
        for ev in evs:
            _stamp(ev, ln, turn, sid)
            if ev.kind in ("tool_call", "command") and ev.data.get("id"):
                tool_turn[str(ev.data["id"])] = turn.n
            if ev.kind == "usage" and ev.data.get("message_id"):
                turn.calls = add_usage(turn.calls, _call_usage(ev.data))
            events.append(ev)
    for seq, ev in enumerate(events, 1):  # as EventLog.append numbers them
        ev.seq = seq
        ev.text, ev.data = redact(ev.text), redact(ev.data)
    for t in turns:
        t.events = [ev for ev in events if ev.turn == t.n]
        t.end = max([ev.ts for ev in t.events] + [t.start or 0.0]) or None
    return Imported(sid, fmt, events, turns, _export(sid, fmt, turns, model, version, cwd, path))


def _stamp(ev: RunEvent, ln: Line, turn: Turn, sid: str) -> RunEvent:
    ev.ts = ln.ts if ln.ts is not None else ev.ts
    ev.turn, ev.run_id, ev.off = turn.n, run_id(sid, turn.n), None
    ev.data["line"] = ln.line  # where it came from: its line in the transcript (or the subagent's)
    if ln.agent:
        ev.data["agent_id"] = ln.agent
    elif ln.sidechain:
        ev.data["sidechain"] = True  # an older transcript's inline subagent line: no call id to link it to
    return ev


def run_id(sid: str, n: int) -> str:
    return f"{sid}/turn/{n}"


def _turn_usage(t: Turn) -> tuple[Usage | None, str | None]:
    """The turn's usage: its `result` lines' (Claude Code's own per-run totals, subagents included), else the sum of
    its model calls'. A streamed message's usage can be written before its output is done, so the results win."""
    if t.results:
        u: Usage | None = None
        for r in t.results:
            u = add_usage(u, r)
        if u is not None and t.calls is not None:
            u.model = t.calls.model
        return u, "result"
    return t.calls, "calls" if t.calls else None


def _export(sid: str, fmt: str, turns: list[Turn], model: str | None, version: str | None, cwd: str | None,
            path: Path) -> dict[str, Any]:
    s = Session(id=sid, name=f"import-{sid}"[:100], environment="import", harness=HARNESS, model=model)
    extras: list[dict[str, Any]] = []
    for t in turns:
        u, source = _turn_usage(t)
        s.turns.append(SessionTurn(message=t.prompt[:MESSAGE_MAX], run_id=run_id(sid, t.n), harness=HARNESS,
                                   harness_version=version, conversation=sid, started_at=t.start or t.end,
                                   finished_at=t.end, usage=u))
        extras.append({"hangar.usage.source": source,
                       "hangar.usage.calls.output_tokens": t.calls.output_tokens if t.calls else None})
    if s.turns:
        s.created_at = s.turns[0].started_at or s.created_at
        s.last_active_at = s.turns[-1].finished_at or s.last_active_at
    x = traces.Export(traces.trace_id(sid), redact)
    root = traces.session_span(x, s, {"hangar.import.format": fmt})
    for n, (t, turn) in enumerate(zip(s.turns, turns, strict=True), 1):
        traces._turn_span(x, root["id"] if root else None, s, n, t, turn.events, extras[n - 1], root=cwd)
    return x.request({"hangar.export.scope": "session", "hangar.session.id": sid, "hangar.session.name": s.name,
                      "hangar.environment": "import", "hangar.import.format": fmt,
                      "hangar.import.source": path.name})


def write(imp: Imported, out: Path) -> dict[str, Any]:
    """`events.jsonl` (one event per line, as `GET /api/sessions/{name}/events` returns them) and `trace.json`.
    Returns a summary: events by kind, tool calls by tool kind, turns."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "events.jsonl").write_text("".join(ev.model_dump_json(exclude_none=True) + "\n" for ev in imp.events))
    (out / "trace.json").write_text(json.dumps(imp.trace, indent=1))
    kinds: dict[str, int] = {}
    tools: dict[str, int] = {}
    for ev in imp.events:
        kinds[ev.kind] = kinds.get(ev.kind, 0) + 1
    for rs in imp.trace["resourceSpans"]:
        for sp in rs["scopeSpans"][0]["spans"]:
            a = {kv["key"]: kv["value"] for kv in sp["attributes"]}
            k = a.get("hangar.kind", {}).get("stringValue")
            label = a["hangar.tool.kind"]["stringValue"] if k == "tool.call" else k
            tools[label] = tools.get(label, 0) + 1
    return {"session": imp.session_id, "format": imp.fmt, "turns": len(imp.turns), "events": kinds, "spans": tools}
