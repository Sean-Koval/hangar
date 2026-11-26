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
A team harness installed for the session (`.claude/team-harness.json`, KAN-353) names the turns' harness, so
flightdeck's specs for that harness apply. Nothing is run and nothing is sent anywhere. Strings pass through
secretscan's patterns, as hangar's outgoing text does; there are no stored credentials offline to mask.
"""

from __future__ import annotations

import functools
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from hangar import secretscan, traces
from hangar.events import MESSAGE_MAX, Parser, RunEvent, _clip, add_usage
from hangar.models import Session, SessionTurn, Usage

KINDS = ("assistant", "user", "system", "result")
HARNESS = "claude-code"
MANIFEST = Path(".claude") / "team-harness.json"  # team-harness's install_local.py writes it (project or user scope)


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
    branch: str | None = None  # --- KAN-345: the git branch when its prompt was sent


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


def _less(u: Usage, b: Usage | None) -> Usage:
    """`u` less `b`, never below zero: a result less the turns it also covers that have no result of their own."""
    if b is None:
        return u
    def sub(x: int, y: int) -> int:
        return max(0, x - y)
    cost = u.cost_usd if u.cost_usd is None or b.cost_usd is None else max(0.0, u.cost_usd - b.cost_usd)
    return u.model_copy(update={
        "input_tokens": sub(u.input_tokens, b.input_tokens), "output_tokens": sub(u.output_tokens, b.output_tokens),
        "cache_read_tokens": sub(u.cache_read_tokens, b.cache_read_tokens),
        "cache_write_tokens": sub(u.cache_write_tokens, b.cache_write_tokens),
        "cache_write_1h_tokens": sub(u.cache_write_1h_tokens, b.cache_write_1h_tokens), "cost_usd": cost})


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
    git: dict[str, Any] = field(default_factory=dict)  # --- KAN-345: ticket, branch and directory


def find_manifest(cwd: str | None, home: Path | None = None) -> Path | None:
    """The team harness installed for a session on this computer: the nearest `.claude/team-harness.json` in its
    working directory or a parent (a project install), else the user's (`~/.claude/team-harness.json`). None when the
    session's directory isn't on this computer (a cloud session, someone else's transcript): the importer's own
    install says nothing about it. The user's install counts only for a directory inside the importer's home: a
    path such as `/home/user/repo` or `/` exists on many machines, so a transcript from one of them is not the
    importer's own session."""
    if not cwd or not Path(cwd).is_dir():
        return None
    start = Path(cwd).resolve()
    user = (home or Path.home()).resolve()
    bases = [start, *start.parents]
    if start.is_relative_to(user):
        bases.append(user)
    for base in bases:
        f = base / MANIFEST
        if f.is_file():
            return f
    return None


def harness_attrs(manifest: Path) -> dict[str, Any]:
    """A turn's harness attributes from team-harness's manifest: its name (what flightdeck's specs key on), revision,
    channel and `harness_digest`. The agent CLI stays `gen_ai.agent.name`."""
    try:
        m = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{manifest}: not a readable team-harness manifest ({e})") from e
    name = m.get("harness") if isinstance(m, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{manifest}: not a team-harness manifest (no harness name)")
    rev, channel, digest = m.get("revision"), m.get("channel"), m.get("harness_digest")
    return {"hangar.harness": name.strip(),
            "hangar.harness.revision": rev if isinstance(rev, int) and not isinstance(rev, bool) else None,
            "hangar.harness.channel": channel if isinstance(channel, str) else None,
            "hangar.harness.digest": digest if isinstance(digest, str) else None}


def import_transcript(path: Path, session_id: str | None = None, manifest: Path | None | bool = True) -> Imported:
    """`manifest`: the team-harness manifest to stamp the turns with; True finds the installed one (find_manifest),
    False or None stamps none (the turns' harness is then Claude Code's)."""
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
    branches: list[str] = []
    cost_so_far: float | None = None
    since: list[Turn] = []  # turns since the last `result`: one CLI run (one result) can span several prompts
    for ln in lines:
        rec = ln.rec
        m = (rec.get("message") or {}).get("model") if rec.get("type") == "assistant" else None
        model = model or (m if m != "<synthetic>" else None)
        version = version or rec.get("version")
        cwd = cwd or (rec["cwd"] if isinstance(rec.get("cwd"), str) else None)
        if (b := _branch(rec.get("gitBranch"))) and b not in branches:
            branches.append(b)
        text = None if ln.file else prompt_of(rec)
        if text is not None:
            turns.append(Turn(len(turns) + 1, text, ln.ts, branch=_branch(rec.get("gitBranch"))))
            since.append(turns[-1])
            events.append(_stamp(RunEvent(kind="message", text=_clip(text, MESSAGE_MAX), data={"role": "user"}),
                                 ln, turns[-1], sid))
        evs = parser.feed(json.dumps(rec, ensure_ascii=False).encode() + b"\n")
        if not turns:  # steps before any prompt (a transcript that starts mid-session): a turn with no prompt
            if not evs and rec.get("type") != "result":
                continue
            turns.append(Turn(1, "", ln.ts))
            since.append(turns[-1])
        parent = rec.get("parent_tool_use_id")
        turn = turns[tool_turn[parent] - 1] if isinstance(parent, str) and parent in tool_turn else turns[-1]
        if rec.get("type") == "result" and not ln.file:
            u, cost_so_far = _result_usage(rec, cost_so_far)
            # The result covers every turn since the last one. Those without a result of their own keep their
            # calls' usage, and this turn takes the rest, so the session still sums to the result totals.
            covered: Usage | None = None
            for t in since:
                if t is not turn and not t.results:
                    covered = add_usage(covered, t.calls)
            turn.results.append(_less(u, covered))
            since = []
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
    found = find_manifest(cwd) if manifest is True else manifest or None
    harness = harness_attrs(found) if found else {}
    git = git_attrs(branches, cwd)
    if session_id and (other := next((i for i in ids if i != session_id), None)):
        git["hangar.session.continues"] = other  # a resumed or forked session's copied lines keep the earlier id
    git = redact(git)
    return Imported(sid, fmt, events, turns, _export(sid, fmt, turns, model, version, cwd, path, harness, git), git)


# --- KAN-345: the ticket a session was for, from its branch, as KAN-320 tags hangar-run sessions
_KEY_AT_START = re.compile(r"(?:^|/)([A-Z][A-Z0-9]+-\d+)(?=$|[-_/])")


def _branch(v: Any) -> str | None:
    """A recorded branch; `HEAD` (a detached checkout) is none."""
    return v.strip() if isinstance(v, str) and v.strip() and v.strip() != "HEAD" else None


@functools.cache
def _jira_projects() -> frozenset[str]:
    """HANGAR_JIRA's project keys, where this machine configures them: then only those count as tickets."""
    try:
        from hangar.config import Settings

        return frozenset(((Settings().jira or {}).get("projects") or {}).keys())
    except Exception:  # noqa: BLE001 - offline and unconfigured is the usual case
        return frozenset()


def ticket_of(branch: str | None, projects: frozenset[str] | set[str] | None = None) -> str | None:
    """The Jira key a branch starts with (`KAN-35-apply-atomic` -> `KAN-35`), also after a prefix such as
    `feature/KAN-35-x`, and ended by the name's end, `-`, `_` or `/`. None for `main`, `HEAD`, `KAN-35abc` or a key
    in the middle of a name. With HANGAR_JIRA's projects known, a key of another project (`CVE-2024-…`) is none."""
    m = _KEY_AT_START.search(branch or "")
    if not m:
        return None
    projects = _jira_projects() if projects is None else projects
    return m.group(1) if not projects or m.group(1).rsplit("-", 1)[0] in projects else None


def git_attrs(branches: list[str], cwd: str | None) -> dict[str, Any]:
    """`hangar.ticket.key` from the first branch that names one (a session can change branch), the branch (that
    one, else the first) and the session's directory. Empty when the transcript records none of them."""
    projects = _jira_projects()
    branch = next((b for b in branches if ticket_of(b, projects)), branches[0] if branches else None)
    out = {"hangar.ticket.key": ticket_of(branch, projects), "hangar.git.branch": branch, "hangar.repo.dir": cwd}
    return {k: v for k, v in out.items() if v}


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
            path: Path, harness: dict[str, Any] | None = None, git: dict[str, Any] | None = None) -> dict[str, Any]:
    s = Session(id=sid, name=f"import-{sid}"[:100], environment="import", harness=HARNESS, model=model)
    extras: list[dict[str, Any]] = []
    for t in turns:
        u, source = _turn_usage(t)
        s.turns.append(SessionTurn(message=t.prompt[:MESSAGE_MAX], run_id=run_id(sid, t.n), harness=HARNESS,
                                   harness_version=version, conversation=sid, started_at=t.start or t.end,
                                   finished_at=t.end, usage=u))
        extras.append({"hangar.usage.source": source,
                       "hangar.usage.calls.output_tokens": t.calls.output_tokens if t.calls else None,
                       **(harness or {}),
                       # --- KAN-345: a session that moves from KAN-35's branch to KAN-36's tags each turn with its own
                       **({"hangar.git.branch": redact(t.branch), "hangar.ticket.key": ticket_of(t.branch)}
                          if t.branch else {})})
    if s.turns:
        s.created_at = s.turns[0].started_at or s.created_at
        s.last_active_at = s.turns[-1].finished_at or s.last_active_at
    x = traces.Export(traces.trace_id(sid), redact)
    root = traces.session_span(x, s, {"hangar.import.format": fmt, **(harness or {}), **(git or {})})
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
    return {"session": imp.session_id, "format": imp.fmt, "turns": len(imp.turns), "events": kinds, "spans": tools,
            **{k: imp.git[a] for k, a in _GIT_KEYS.items() if a in imp.git}}


_GIT_KEYS = {"ticket": "hangar.ticket.key", "branch": "hangar.git.branch", "repo": "hangar.repo.dir"}


# --- KAN-345: a whole projects folder, incrementally
INDEX = "index.json"
INDEX_SCHEMA = "hangar.trace-import.index/v1"
IMPORTER = 1  # bump when the import's output changes: every session is then imported again
CHECKPOINT = 50  # imports between index writes, so a crash loses little
_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_DIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}")


def projects_dir() -> Path:
    """Claude Code's transcripts: $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "projects"


def parse_since(text: str) -> float:
    """`7d`, `12h`, `30m` or `2w` as seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", text or "")
    if not m:
        raise ValueError(f"--since takes a number and m, h, d or w (7d, 12h), not {text!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


def _files(path: Path) -> list[Path]:
    return [path, *(f for f, _, _ in subagents(path))]


def fingerprint(path: Path) -> list[list[Any]]:
    """What a re-import is decided on: the transcript's and its subagents' names, sizes and mtimes. A background
    subagent can write after its parent's last line, so its own file counts too."""
    out = []
    for f in _files(path):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append([f.name, st.st_size, st.st_mtime_ns])
    return out


def first_cwd(path: Path, limit: int = 200) -> str | None:
    """The session's directory from its first lines, without importing it (for --repo)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("cwd"), str):
                    return rec["cwd"]
    except OSError:
        return None
    return None


def _parts(p: str) -> tuple[str, ...]:
    """A path's parts, for a POSIX or a Windows path, whichever machine wrote the transcript."""
    from pathlib import PurePosixPath, PureWindowsPath

    win = "\\" in p or re.match(r"^[A-Za-z]:", p)
    return tuple(x.lower() if win else x for x in (PureWindowsPath(p) if win else PurePosixPath(p)).parts)


def repo_matches(cwd: str | None, repo: str) -> bool:
    """`--repo`: a path matches the session's directory or one inside it (a relative one is taken from here); a bare
    name matches any directory on the session's path with that name (`hangar` for /home/me/hangar/src)."""
    if not cwd:
        return False
    name = repo.strip().rstrip("/\\") or repo.strip()
    if len(_parts(name)) == 1 and not name.startswith(("~", ".", "/")):
        return name in _parts(cwd) or name.lower() in _parts(cwd)
    windows = "\\" in name or re.match(r"^[A-Za-z]:", name)
    want = {_parts(name if windows else os.path.abspath(os.path.expanduser(name)))}
    if not windows and os.path.exists(os.path.expanduser(name)):
        want.add(_parts(str(Path(name).expanduser().resolve())))
    have = {_parts(cwd if "\\" in cwd else os.path.normpath(cwd))}
    if os.path.exists(cwd):
        have.add(_parts(str(Path(cwd).resolve())))
    return any(h[:len(w)] == w for h in have for w in want)


def _dir_for(sid: str, key: str, taken: dict[str, str]) -> str:
    """The session's directory under --out: its id when that is a plain name (Claude Code's are UUIDs), else a hash
    of it, and never `.`, `..`, `index.json` or a name another transcript already holds."""
    import hashlib

    name = sid if _DIR.fullmatch(sid) else "session-" + hashlib.sha256(sid.encode()).hexdigest()[:16]
    if taken.get(name, key) != key:
        name = f"{name[:100]}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"
    return name


def _read_index(out: Path) -> dict[str, Any]:
    try:
        idx = json.loads((out / INDEX).read_text())
    except (OSError, ValueError):
        return {}
    ok = isinstance(idx, dict) and idx.get("schema") == INDEX_SCHEMA and idx.get("importer") == IMPORTER
    return idx.get("sessions") or {} if ok else {}


def _write_index(out: Path, index: dict[str, Any]) -> None:
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=out)
    with os.fdopen(fd, "w") as f:
        json.dump({"schema": INDEX_SCHEMA, "importer": IMPORTER, "sessions": index}, f, indent=1, sort_keys=True)
    os.replace(tmp, out / INDEX)


def import_all(projects: Path, out: Path, since: float | None = None, repo: str | None = None,
               manifest: Path | None | bool = True, now: float | None = None) -> dict[str, Any]:
    """Every session transcript under `projects` (`<dir>/<session>.jsonl`; subagents come with their parent) into
    `out/<session>/`, the session named by its file, as Claude Code names it. One already imported with the same
    fingerprint is skipped; a grown one is imported again. `out/index.json` (keyed by the transcript's path under
    `projects`) records what was done. Returns what this run did."""
    now = time.time() if now is None else now
    projects, out = projects.resolve(), out.resolve()
    if out == projects or out.is_relative_to(projects):
        raise ValueError(f"--out {out} is inside the projects folder; the next run would read it as transcripts")
    index = _read_index(out)
    # the index is a file anyone can edit, so a folder name from it counts only when it is a plain name
    taken = {e["dir"]: k for k, e in index.items() if isinstance(e, dict) and _DIR.fullmatch(str(e.get("dir", "")))}
    done: dict[str, Any] = {"imported": [], "unchanged": 0, "older": 0, "other_repo": 0, "failed": []}
    seen: set[Path] = set()
    changed = 0
    try:
        for path in sorted(projects.glob("*/*.jsonl")):
            real = path.resolve()
            if real in seen:  # two links to one transcript
                continue
            seen.add(real)
            key = path.relative_to(projects).as_posix()
            fp = fingerprint(path)
            if not fp:
                continue
            if since is not None and max(ns for _, _, ns in fp) / 1e9 < now - since:
                done["older"] += 1
                continue
            if repo and not repo_matches(first_cwd(path), repo):
                done["other_repo"] += 1
                continue
            prev = index.get(key)
            if prev and taken.get(prev.get("dir")) == key and prev.get("fingerprint") == fp \
                    and (out / prev["dir"]).is_dir():
                done["unchanged"] += 1
                continue
            try:
                imp = import_transcript(path, path.stem, manifest)
                d = prev["dir"] if prev and taken.get(prev.get("dir")) == key else _dir_for(imp.session_id, key, taken)
                summary = write(imp, out / d)
            except Exception as e:  # noqa: BLE001 - one bad transcript never stops the folder
                done["failed"].append({"transcript": key, "error": f"{type(e).__name__}: {e}"[:300]})
                continue
            taken[d] = key
            entry = {"fingerprint": fp, "session": imp.session_id, "dir": d, "turns": summary["turns"],
                     **{k: summary[k] for k in _GIT_KEYS if k in summary}}
            index[key] = entry
            done["imported"].append(entry)
            changed += 1
            if changed % CHECKPOINT == 0:
                _write_index(out, index)
    finally:
        if changed:
            _write_index(out, index)
    return done
