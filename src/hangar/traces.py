"""Trace export in an open schema: `hangar.trace/v1` (--- interfaces; docs/api/traces.md, spec 0014).

A run, a session or a job, as one OTLP/JSON `ExportTraceServiceRequest` that any OpenTelemetry tool reads: the sister
tool's timelines and behavior specs, a collector, Jaeger. Spans and attributes follow the OpenTelemetry GenAI semantic
conventions (development; the version pinned below) for agents and tools, and `hangar.*` for the rest. Every span has
a `hangar.kind` and every span event is named by its kind: that taxonomy is what behavior specs match on, and it is
stable within `hangar.trace/v1`.

Read-only: built on demand from records hangar keeps anyway (the session's event log, its turns' approvals and steers,
the job's timeline and result). Every string is redacted again on the way out, whatever the log already did.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from hangar.events import RunEvent, total_usage
from hangar.models import Job, Session, SessionTurn, Usage, now

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.traces")

SCHEMA = "hangar.trace/v1"
# OpenTelemetry GenAI semantic conventions, github.com/open-telemetry/semantic-conventions-genai at f3c6d0c
# (2026-10-14): its registry's schema; it builds on the core conventions 1.44.0. Every GenAI name used here is
# "development" there.
GENAI = "gen-ai-dev/1.42.0-dev"
SCHEMA_URL = "https://opentelemetry.io/schemas/1.44.0"
SCOPE = {"name": "hangar.trace", "version": "1"}
MAX_ITEMS = 20_000  # spans and events in one export
INTERNAL = 1  # OTLP SpanKind
ERROR = 2  # OTLP StatusCode
PROVIDERS = {"claude-code": "anthropic", "codex": "openai", "antigravity": "gcp.gemini"}  # as observe.py
TEXT = 2000

# Normalized tool kinds (docs/api/traces.md): the same for every harness.
_KINDS: dict[str, set[str]] = {
    "shell": {"Bash", "BashOutput", "KillShell", "bash", "shell", "run_command", "command_status"},
    "edit": {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit", "apply_patch", "write_to_file",
             "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit"},
    "read": {"Read", "NotebookRead", "LS", "read", "ls", "view_file", "view_file_outline", "view_code_item",
             "list_dir", "read_file"},
    "search": {"Grep", "Glob", "grep", "find", "grep_search", "find_by_name", "codebase_search"},
    "web": {"WebFetch", "WebSearch", "web_search", "read_url_content", "search_web", "browser_preview"},
    "skill": {"Skill"},
    "todo": {"TodoWrite", "todo_write", "update_plan"},
}
_BY_NAME = {n: k for k, names in _KINDS.items() for n in names}
SUBAGENT_TOOLS = {"Task", "Agent"}  # Claude Code's subagent tool (named Agent in newer releases)


def hangar_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("hangar")
    except PackageNotFoundError:
        return "unknown"


def tool_kind(name: str | None) -> str:
    """The normalized kind of a tool, by the name its harness gives it."""
    n = name or ""
    if n in _BY_NAME:
        return _BY_NAME[n]
    if n.startswith(("mcp__", "mcp_")) or re.fullmatch(r"[\w-]+\.[\w-]+", n):  # Claude / agy; Codex server.tool
        return "mcp"
    return "other"


def _mcp_parts(name: str) -> tuple[str | None, str | None]:
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        return server or None, tool or None
    if "." in name:
        server, _, tool = name.partition(".")
        return server, tool
    return None, None


# ====================================================================== OTLP/JSON building blocks
def _value(v: Any) -> dict[str, Any] | None:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, list | tuple):
        vals = [x for x in (_value(i) for i in v) if x is not None]
        return {"arrayValue": {"values": vals}}
    return None if v is None else {"stringValue": str(v)}


def _ns(ts: float | None) -> str:
    return str(int((ts or 0) * 1e9))


def _hex(*parts: str, n: int) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[: n * 2]


def trace_id(key: str) -> str:
    """Derived from the session's or job's id: the same record exports the same ids every time."""
    return _hex("hangar:" + key, n=16)


class Export:
    """Spans and events of one export, redacted as they are added; rendered to OTLP/JSON by `request`."""

    def __init__(self, trace: str, redact: Callable[[Any], Any], limit: int = MAX_ITEMS):
        self.trace, self.redact, self.limit = trace, redact, limit
        self.spans: list[dict[str, Any]] = []  # {"id", "parent", "name", "start", "end", "attrs", "events", "error"}
        self.items = 0
        self.truncated = False

    def _clean(self, attrs: dict[str, Any]) -> dict[str, Any]:
        return {k: self.redact(v) if isinstance(v, str) else v for k, v in attrs.items()
                if v is not None and v != ""}

    def room(self) -> bool:
        if self.items >= self.limit:
            self.truncated = True
            return False
        return True

    def span(self, key: str, name: str, start: float | None, end: float | None, attrs: dict[str, Any],
             parent: str | None = None, error: str | None = None) -> dict[str, Any] | None:
        """A span (None once the export is full). `error`: its status message when it failed ("" for none)."""
        if not self.room():
            return None
        self.items += 1
        s = start or end or now()
        sp = {"id": _hex(self.trace, key, n=8), "parent": parent, "name": self.redact(name), "start": s,
              "end": max(s, end or s), "attrs": self._clean(attrs), "events": [],
              "error": None if error is None else self.redact(error)[:300]}
        self.spans.append(sp)
        return sp

    def event(self, sp: dict[str, Any] | None, name: str, ts: float | None, attrs: dict[str, Any]) -> None:
        if sp is None or not self.room():
            return
        self.items += 1
        sp["events"].append({"name": name, "ts": ts or now(), "attrs": self._clean(attrs)})

    def request(self, resource: dict[str, Any]) -> dict[str, Any]:
        """The OTLP/JSON ExportTraceServiceRequest: hex ids, integer enums, 64-bit integers as strings."""
        res = {"service.name": "hangar", "service.version": hangar_version(), "hangar.trace.schema": SCHEMA,
               "hangar.semconv.genai": GENAI, **resource}
        if self.truncated:
            res["hangar.export.truncated"] = True
        spans = []
        for sp in sorted(self.spans, key=lambda x: x["start"]):
            out: dict[str, Any] = {"traceId": self.trace, "spanId": sp["id"], "name": sp["name"], "kind": INTERNAL,
                                   "startTimeUnixNano": _ns(sp["start"]), "endTimeUnixNano": _ns(sp["end"]),
                                   "attributes": _attrs(sp["attrs"])}
            if sp["parent"]:
                out["parentSpanId"] = sp["parent"]
            if sp["events"]:
                out["events"] = [{"timeUnixNano": _ns(e["ts"]), "name": e["name"], "attributes": _attrs(e["attrs"])}
                                 for e in sorted(sp["events"], key=lambda e: e["ts"])]
            if sp["error"] is not None:
                out["status"] = {"code": ERROR, **({"message": sp["error"]} if sp["error"] else {})}
            spans.append(out)
        return {"resourceSpans": [{"resource": {"attributes": _attrs(self._clean(res))},
                                   "scopeSpans": [{"scope": dict(SCOPE), "schemaUrl": SCHEMA_URL, "spans": spans}]}]}


def _attrs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": val} for k, v in attrs.items() if (val := _value(v)) is not None]


def _usage_attrs(u: Usage | None) -> dict[str, Any]:
    if u is None:
        return {}
    return {"gen_ai.usage.input_tokens": u.input_tokens + u.cache_read_tokens + u.cache_write_tokens,
            "gen_ai.usage.output_tokens": u.output_tokens,
            "gen_ai.usage.cache_read.input_tokens": u.cache_read_tokens,
            "gen_ai.usage.cache_write.input_tokens": u.cache_write_tokens,
            "gen_ai.response.model": u.model, "hangar.cost_usd": u.cost_usd, "hangar.cost_source": u.cost_source}


# ====================================================================== events -> spans
def read_events(activity: Any, sid: str, run_id: str | None = None, limit: int = MAX_ITEMS) -> list[RunEvent]:
    """A session's recorded events (only `run_id`'s, when given), oldest first: the last `limit` of them. One pass
    over its log files (paging with `since` would rescan them from the start for every page)."""
    import collections
    import contextlib

    out: collections.deque[RunEvent] = collections.deque(maxlen=limit)
    needle = run_id.encode() if run_id else None
    for p in activity.log._paths(sid):  # the rotated file first, then the current one
        with contextlib.suppress(OSError), open(p, "rb") as f:
            for line in f:
                if needle is not None and needle not in line:
                    continue
                with contextlib.suppress(ValueError):
                    ev = RunEvent.model_validate_json(line)
                    if run_id is None or ev.run_id == run_id:
                        out.append(ev)
    return list(out)


def _after_name(text: str, name: str | None) -> str:
    return text[len(name) + 1:] if name and text.startswith(name + " ") else text


def turn_children(x: Export, parent: str | None, key: str, turn: SessionTurn, harness: str | None,
                  events: list[RunEvent], turn_span: dict[str, Any] | None) -> None:
    """The turn's tool calls, subagents and approvals as child spans, and its points in time as events on
    `turn_span` (None: spans only, for live export)."""
    end = turn.finished_at or now()
    open_: dict[str, RunEvent] = {}
    conv = turn.conversation
    for ev in events:
        if ev.kind in ("tool_call", "command"):
            tid = str(ev.data.get("id") or f"seq{ev.seq}")
            open_[tid] = ev
        elif ev.kind == "tool_result":
            tid = str(ev.data.get("id") or "")
            start = open_.pop(tid, None)
            _tool_span(x, parent, key, harness, conv, start, ev, end)
        elif turn_span is not None:
            _point(x, turn_span, ev)
    for start in open_.values():  # the turn ended (or the log was cut) before these answered
        _tool_span(x, parent, key, harness, conv, start, None, end)
    for a in turn.approvals:
        status = a.status
        x.span(f"{key}/approval/{a.id}", f"approval {a.tool or '?'}", a.requested_at, a.decided_at or end, {
            "hangar.kind": "approval", "hangar.approval.id": a.id, "hangar.approval.tool": a.tool,
            "hangar.approval.detail": a.detail[:TEXT], "hangar.approval.status": status,
            "hangar.approval.by": a.by, "hangar.approval.message": a.message}, parent=parent)
    if turn_span is None:
        return
    for st in turn.steers:
        x.event(turn_span, st.kind, st.ts, {"hangar.kind": st.kind, "hangar.text": st.text[:TEXT],
                                            "hangar.requester": st.requester, "hangar.steer.status": st.status})
    if turn.question:
        x.event(turn_span, "question", end, {"hangar.kind": "question", "hangar.text": turn.question[:TEXT]})


def _tool_span(x: Export, parent: str | None, key: str, harness: str | None, conv: str | None,
               start: RunEvent | None, result: RunEvent | None, turn_end: float) -> None:
    first = start or result
    if first is None:
        return
    name = str((start.data.get("tool") if start else None) or (result.data.get("tool") if result else None)
               or ("shell" if start is not None and start.kind == "command" else "?"))
    tid = str(first.data.get("id") or f"seq{first.seq}")
    t0 = start.ts if start else result.ts if result else turn_end
    t1 = result.ts if result else turn_end
    inp = start.text if start is not None and start.kind == "command" else _after_name(start.text, name) \
        if start else None
    ok = bool(result.data.get("ok")) if result is not None else None
    common = {"gen_ai.tool.call.id": tid, "hangar.tool.input": inp,
              "hangar.tool.output": result.text if result else None, "hangar.tool.ok": ok,
              "gen_ai.conversation.id": conv}
    if name in SUBAGENT_TOOLS:
        agent = (start.data.get("subagent") if start else None) or name
        x.span(f"{key}/tool/{tid}", f"invoke_agent {agent}", t0, t1, {
            "hangar.kind": "subagent", "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent,
            **common, "hangar.tool.incomplete": True if result is None else None},
            parent=parent, error="" if ok is False else None)
        return
    kind = "shell" if start is not None and start.kind == "command" and tool_kind(name) == "other" \
        else tool_kind(name)
    attrs = {"hangar.kind": "tool.call", "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
             "gen_ai.tool.type": "extension" if kind in ("mcp", "web") else "function",
             "gen_ai.agent.name": harness, "hangar.tool.kind": kind, **common}
    if kind in ("edit", "read") and inp:
        attrs["hangar.file.path"] = inp
    if kind == "mcp":
        attrs["hangar.mcp.server"], attrs["hangar.mcp.tool"] = _mcp_parts(name)
    if result is not None and isinstance(result.data.get("exit_code"), int):
        attrs["process.exit.code"] = result.data["exit_code"]
    if result is None:
        attrs["hangar.tool.incomplete"] = True
    if ok is False:
        attrs["error.type"] = "tool_error"
    x.span(f"{key}/tool/{tid}", f"execute_tool {name}", t0, t1, attrs, parent=parent,
           error="" if ok is False else None)


def _point(x: Export, sp: dict[str, Any], ev: RunEvent) -> None:
    d = ev.data
    if ev.kind == "message":
        x.event(sp, "message", ev.ts, {"hangar.kind": "message", "hangar.text": ev.text[:TEXT]})
    elif ev.kind == "file_change":
        x.event(sp, "file.change", ev.ts, {"hangar.kind": "file.change", "hangar.file.path": d.get("path") or ev.text,
                                           "hangar.file.op": d.get("op"), "gen_ai.tool.name": d.get("tool")})
    elif ev.kind == "error":
        x.event(sp, "error", ev.ts, {"hangar.kind": "error", "hangar.text": ev.text[:TEXT],
                                     "hangar.fatal": d.get("fatal") if isinstance(d.get("fatal"), bool) else None})
    elif ev.kind == "progress" and d.get("action") == "verify":
        x.event(sp, "verify", ev.ts, {"hangar.kind": "verify", "hangar.verify.phase": "in_turn",
                                      "hangar.verify.passed": bool(d.get("passed")),
                                      "hangar.verify.exit_code": d.get("exit_code"),
                                      "hangar.verify.round": d.get("round")})
    elif ev.kind == "progress" and d.get("action") in ("steer", "interrupt"):
        return  # from the turn's own record of steers (who, and whether it was delivered)
    elif ev.kind == "progress":
        x.event(sp, "progress", ev.ts, {"hangar.kind": "progress", "hangar.text": ev.text[:TEXT],
                                        "hangar.source": d.get("source")})


def _turn_span(x: Export, parent: str | None, s: Session, n: int, turn: SessionTurn, events: list[RunEvent],
               extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    key = turn.run_id or f"{s.id}/turn/{n}"
    harness = turn.harness or s.harness
    u = total_usage([turn])
    failed = turn.outcome not in (None, "exited", "needs_input", "interrupted") or (
        turn.outcome == "exited" and (turn.exit_code or 0) != 0)
    attrs = {"hangar.kind": "turn", "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": harness,
             "gen_ai.provider.name": PROVIDERS.get(harness or ""), "gen_ai.request.model": s.model,
             "gen_ai.conversation.id": turn.conversation, "hangar.turn.number": n, "hangar.run.id": turn.run_id,
             "hangar.session.id": s.id, "hangar.turn.mode": turn.mode, "hangar.turn.outcome": turn.outcome,
             "hangar.turn.exit_code": turn.exit_code, "hangar.harness": harness,
             "hangar.harness.version": turn.harness_version, "hangar.turn.live": turn.live,
             "hangar.approvals": turn.approval_policy, "hangar.manifest.digest": turn.manifest,
             **_usage_attrs(u), **(extra or {})}
    if failed:
        attrs["error.type"] = turn.outcome if turn.outcome != "exited" else "agent_exit_code"
    sp = x.span(key, f"invoke_agent {harness or ''}".rstrip(), turn.started_at, turn.finished_at or now(), attrs,
                parent=parent, error=(turn.outcome_reason or turn.outcome or "") if failed else None)
    if sp is not None:
        turn_children(x, sp["id"], key, turn, harness, events, sp)
    return sp


def _by_turn(events: Iterable[RunEvent]) -> dict[str, list[RunEvent]]:
    out: dict[str, list[RunEvent]] = {}
    for ev in events:
        out.setdefault(ev.run_id or f"#{ev.turn}", []).append(ev)
    return out


def _events_of(grouped: dict[str, list[RunEvent]], n: int, turn: SessionTurn) -> list[RunEvent]:
    return grouped.get(turn.run_id or "", []) if turn.run_id else grouped.get(f"#{n}", [])


# ====================================================================== exports
def session_export(svc: SessionService, s: Session, turn: int | None = None) -> dict[str, Any]:
    """A session (every turn under a `session` root) or, with `turn`, one run on its own."""
    redact = svc.fleet.broker.redact
    x = Export(trace_id(s.id), redact)
    resource = {"hangar.export.scope": "run" if turn else "session", "hangar.session.id": s.id,
                "hangar.session.name": s.name, "hangar.job.id": s.job_id, "hangar.environment": s.environment,
                "hangar.person": s.person}
    if turn is not None:
        if not 1 <= turn <= len(s.turns):
            raise KeyError(f"session {s.name} has no turn {turn} (it has {len(s.turns)})")
        t = s.turns[turn - 1]
        evs = read_events(svc.activity, s.id, run_id=t.run_id) if t.run_id else []
        _turn_span(x, None, s, turn, t, evs)
        return x.request(resource)
    grouped = _by_turn(read_events(svc.activity, s.id))
    started = [t for t in s.turns if t.started_at]
    root = x.span(f"{s.id}/session", "invoke_workflow hangar.session",
                  started[0].started_at if started else s.created_at,
                  max((t.finished_at or now() for t in started), default=s.last_active_at), {
                      "hangar.kind": "session", "gen_ai.operation.name": "invoke_workflow",
                      "gen_ai.workflow.name": "hangar.session", "hangar.environment": s.environment,
                      "hangar.harness": s.harness, "gen_ai.request.model": s.model,
                      **_usage_attrs(total_usage(s.turns))})
    for n, t in enumerate(s.turns, 1):
        if t.started_at:
            _turn_span(x, root["id"] if root else None, s, n, t, _events_of(grouped, n, t))
    return x.request(resource)


def job_export(jobs: JobService, job: Job, sessions: list[Session] | None = None,
               usage: Usage | None = None) -> dict[str, Any]:
    """A job: its sessions' turns (the main one, best-of-N attempts, the judge) under a `job` root, with hangar's
    check after each turn, the delivery (commits, push, PR), CI and the merge as events. `sessions` and `usage`:
    read beforehand on the event loop (export_job), so this can run in a thread."""
    svc = jobs.sessions
    if sessions is None:
        sessions = svc.of_job(job.id) if svc is not None else []
    if usage is None:
        usage = jobs.usage(job)
    x = Export(trace_id(job.id), jobs.broker.redact)
    last = max([e.ts for e in job.events] + [job.updated_at])
    root = x.span(f"{job.id}/job", "invoke_workflow hangar.job", job.created_at, last, {
        "hangar.kind": "job", "gen_ai.operation.name": "invoke_workflow", "gen_ai.workflow.name": "hangar.job",
        "hangar.job.id": job.id, "hangar.job.state": job.state.value, "hangar.job.reason": job.reason,
        "hangar.job.strategy": job.strategy, "hangar.environment": job.environment, "hangar.harness": job.harness,
        "gen_ai.request.model": job.model, "hangar.repo": job.repo, "hangar.diff.stat": job.result.diffstat,
        "hangar.pr.url": job.result.pr_url, **_usage_attrs(usage)},
        error=(job.reason or "") if job.state.value == "failed" else None)
    pid = root["id"] if root else None
    for s in sorted(sessions, key=lambda z: z.created_at):
        grouped = _by_turn(read_events(svc.activity, s.id))
        attempt = s.labels.get("hangar.attempt")
        extra = {"hangar.attempt": int(attempt)} if attempt and attempt.isdigit() else {}
        for n, t in enumerate(s.turns, 1):
            if t.started_at:
                _turn_span(x, pid, s, n, t, _events_of(grouped, n, t), extra)
    _job_events(x, root, job)
    try:  # the testresults work (build/testresults): each check's test.run / test.case events on the job
        from hangar import testresults
    except ImportError:
        testresults = None
    if testresults is not None and root is not None:
        testresults.add_to_export(x, root, jobs.store, job)
    main = next((z for z in sessions if z.id == job.session_id), None)
    return x.request({"hangar.export.scope": "job", "hangar.job.id": job.id, "hangar.environment": job.environment,
                      "hangar.session.id": main.id if main else None, "hangar.person": job.person})


async def export_session(svc: SessionService, s: Session, turn: int | None = None) -> dict[str, Any]:
    """session_export off the event loop (the event log can be tens of MB), from a copy of the session."""
    return await asyncio.to_thread(session_export, svc, s.model_copy(deep=True), turn)


async def export_job(jobs: JobService, job: Job) -> dict[str, Any]:
    """job_export off the event loop: the job and its sessions are copied here, the event logs read there."""
    sessions = [z.model_copy(deep=True) for z in (jobs.sessions.of_job(job.id) if jobs.sessions else [])]
    return await asyncio.to_thread(job_export, jobs, job.model_copy(deep=True), sessions, jobs.usage(job))


def _job_events(x: Export, root: dict[str, Any] | None, job: Job) -> None:
    r = job.result
    delivered = next((e.ts for e in reversed(job.events) if e.state == "delivering"), None) or job.updated_at
    verified = next((e.ts for e in reversed(job.events) if e.state == "verifying"), None)
    for e in job.events:
        x.event(root, "job.state", e.ts, {"hangar.kind": "job.state", "hangar.job.state": e.state,
                                          "hangar.text": e.message[:TEXT]})
    if r.verify_exit_code is not None:
        x.event(root, "verify", verified or delivered, {
            "hangar.kind": "verify", "hangar.verify.phase": "after_turn",
            "hangar.verify.passed": r.verify_exit_code == 0,
            "hangar.verify.exit_code": r.verify_exit_code, "hangar.verify.command": job.verify,
            "hangar.verify.new_failures": list(r.new_failures) if r.new_failures is not None else None})
    for c in reversed(r.commits):  # stored newest first
        sha, _, subject = c.partition(" ")
        x.event(root, "git.commit", delivered, {"hangar.kind": "git.commit", "vcs.ref.head.revision": sha,
                                                "hangar.git.subject": subject})
    if r.pushed or r.head_sha:
        x.event(root, "git.push", delivered, {"hangar.kind": "git.push", "vcs.ref.head.name": job.branch,
                                              "vcs.ref.head.revision": r.head_sha, "hangar.git.pushed": r.pushed})
    if r.pr_url:
        x.event(root, "pr.open", delivered, {"hangar.kind": "pr.open", "hangar.pr.number": r.pr_number,
                                             "hangar.pr.url": r.pr_url, "hangar.pr.draft": job.landing.draft})
    land = job.landing
    for name, conclusion in sorted(land.checks.items()):
        x.event(root, "ci.check", land.ci_green_at or job.updated_at, {
            "hangar.kind": "ci.check", "hangar.ci.name": name, "hangar.ci.conclusion": conclusion,
            "vcs.ref.head.revision": land.checks_sha})
    if land.merged_at:
        x.event(root, "pr.merge", land.merged_at, {"hangar.kind": "pr.merge", "hangar.pr.number": r.pr_number,
                                                   "hangar.pr.merged_by": land.merged_by})


# ====================================================================== live export (observe.py)
def live_children(sp: Any, svc: SessionService, s: Session, turn: SessionTurn) -> None:
    """HANGAR_TRACE_LIVE_TOOLS: the ending turn's tool calls, subagents and approvals as children of its live
    `invoke_agent` span, through hangar's own tracer (observe.py), with the attributes the file export gives them."""
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, Status, StatusCode

    from hangar import observe

    tracer = observe._tracer
    if tracer is None or sp is None or not turn.run_id:
        return
    evs = read_events(svc.activity, s.id, run_id=turn.run_id, limit=2000)
    x = Export(trace_id(s.id), svc.fleet.broker.redact, limit=2000)
    turn_children(x, None, turn.run_id, turn, turn.harness or s.harness, evs, None)
    ctx = trace.set_span_in_context(sp)
    for d in x.spans:
        child = tracer.start_span(d["name"], context=ctx, kind=SpanKind.INTERNAL, attributes=d["attrs"],
                                  start_time=int(d["start"] * 1e9))
        if d["error"] is not None:
            child.set_status(Status(StatusCode.ERROR, d["error"] or None))
        child.end(end_time=int(d["end"] * 1e9))
