"""Long work as MCP tasks, and agents' questions as elicitations (workstream mcp-modern).

Tasks follow the official tasks extension, `io.modelcontextprotocol/tasks` (SEP-2663, 2026-07-28). A client that
declares it on a `tools/call` of submit_job, follow_up_job, new_session, send_to_session, fork_session or run_agent
gets a task handle at once instead of the view; it polls `tasks/get` until the turn ends and then gets the tool's
result with the turn added (summary, PR, structured result, artifacts as resource links). When the agent asks a
question the task turns `input_required` with an elicitation, and `tasks/update` with the answer continues the same
conversation. Clients without the extension get the plain result, exactly as before. The Python SDK ships the
extension seams (an `Extension` with methods and a tools/call interceptor) but not the runtime, so it is here.

Task records are small JSON rows in the store's kv table, so a task survives a control-plane restart: its state is
always derived from the job or session it follows. A task belongs to the caller that created it; anyone else gets
"unknown task", so a task id is not a capability on its own.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, Field, ValidationError

from hangar import exchange
from hangar.fleet import FleetError
from hangar.jobs import JobError
from hangar.mcp_live import job_uri, links, session_uri
from hangar.models import JOB_TERMINAL, JobState
from hangar.providers.base import ProviderError
from hangar.sessions import SessionError

TASKS_EXT = "io.modelcontextprotocol/tasks"
TASK_TOOLS = frozenset({"submit_job", "follow_up_job", "new_session", "send_to_session", "fork_session", "run_agent"})
TASK_TTL_MS = 24 * 3600 * 1000
KV_PREFIX = "mcp-task:"
INVALID_PARAMS = -32602
MAX_ANSWER = 100_000
MAX_RESPONSES = 20
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "title": "Your answer",
                              "description": "Sent to the agent as its next message; it continues where it stopped."}},
    "required": ["answer"],
}


class Answer(BaseModel):
    """The form a client shows for an agent's question (handshake-era elicitation)."""

    answer: str = Field(title="Your answer", description=ANSWER_SCHEMA["properties"]["answer"]["description"])


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def supports_form_elicitation(caps: types.ClientCapabilities | None) -> bool:
    """A client that declared elicitation can be asked a form question (an empty object means form only)."""
    e = caps.elicitation if caps else None
    return e is not None and (e.form is not None or e.url is None)


def question_request(label: str, question: str) -> types.ElicitRequest:
    return types.ElicitRequest(params=types.ElicitRequestFormParams(
        message=f"{label} asks: {question}", requested_schema=ANSWER_SCHEMA))  # type: ignore[arg-type]


def answer(sessions: Any, jobs: Any, ref: str, turn_no: int, resp: types.ElicitResult, requester: str) -> str:
    """Feed the user's answer to an agent's question as the follow-up of that same turn.

    Returns answered | declined | cancelled | invalid | stale. `stale` means the question is no longer open (someone
    answered it already, or the job moved on), so nothing is sent twice when a client retries."""
    if resp.action != "accept":
        return "declined" if resp.action == "decline" else "cancelled"
    text = (resp.content or {}).get("answer")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_ANSWER:
        return "invalid"
    s, job = exchange._lookup(sessions, jobs, ref)  # noqa: SLF001 - shared with wait_for_turn
    try:
        if job is not None:
            if job.state != JobState.NEEDS_INPUT or len(job.turns) != turn_no:
                return "stale"
            jobs.follow_up(job.id, text, requester=requester)
        else:
            if s is None or not s.needs_input or len(s.turns) != turn_no:
                return "stale"
            sessions.send(s.name, text, requester=requester)
    except (JobError, SessionError):  # moved on between the check and the send (busy, archived...)
        return "stale"
    return "answered"


def open_question(sessions: Any, jobs: Any, ref: str) -> tuple[int, str, str] | None:
    """(turn number, question, label) when the job's or session's turn has ended on a question, else None."""
    s, job = exchange._lookup(sessions, jobs, ref)  # noqa: SLF001
    if not exchange._settled(sessions, jobs, s, job):  # noqa: SLF001
        return None
    if job is not None:
        q = job.result.question or (s.needs_input if s else None)
        return (len(job.turns), q, f"Job {job.id} ({job.title})") if job.state == JobState.NEEDS_INPUT and q else None
    if s is not None and s.needs_input:
        return len(s.turns), s.needs_input, f"Session {s.name}"
    return None


def tool_result(payload: dict[str, Any], turn: dict[str, Any] | None, refs: list[str],
                is_error: bool = False) -> types.CallToolResult:
    """A finished tool result: the payload as structured content and JSON text, plus resource links."""
    content: list[types.ContentBlock] = [types.TextContent(text=json.dumps(payload, indent=2, default=str))]
    content += links(turn, *refs)
    return types.CallToolResult(content=content, structured_content=payload, is_error=is_error)


# ====================================================================== task records
class TaskRecord(BaseModel):
    id: str
    owner: str
    tool: str
    kind: Literal["job", "session", "forks", "agent"]
    refs: list[str]
    base: dict[str, Any] = Field(default_factory=dict)  # the plain result of the call, which the task result extends
    created_at: float
    updated_at: float
    declined: list[int] = Field(default_factory=list)  # turns whose question the user declined to answer
    asked: dict[str, int] = Field(default_factory=dict)  # input request key -> turn (keys are never reused)
    cancelled: bool = False
    final: dict[str, Any] | None = None  # the terminal task state, once reached (it never changes after)


class TaskIdParams(types.RequestParams):
    task_id: str = Field(alias="taskId", min_length=1, max_length=100)


class TaskUpdateParams(TaskIdParams):
    input_responses: dict[str, Any] = Field(alias="inputResponses")


def _unknown(task_id: str) -> MCPError:
    return MCPError(INVALID_PARAMS, f"unknown task {task_id}")


class Tasks:
    """Create, observe, answer and cancel tasks over jobs, sessions and agent runs."""

    def __init__(self, store: Any, fleet: Any, sessions: Any, jobs: Any, actor: Callable[[], str]):
        self.store, self.fleet, self.sessions, self.jobs, self.actor = store, fleet, sessions, jobs, actor

    # --- storage
    def _load(self, task_id: str) -> TaskRecord:
        raw = self.store.kv_get(KV_PREFIX + task_id) if task_id.startswith("task-") else None
        rec = TaskRecord.model_validate_json(raw) if raw else None
        if rec is None or rec.owner != self.actor():
            raise _unknown(task_id)
        if time.time() * 1000 > rec.created_at * 1000 + TASK_TTL_MS:
            self.store.kv_delete(KV_PREFIX + task_id)
            raise _unknown(task_id)
        return rec

    def _save(self, rec: TaskRecord) -> None:
        rec.updated_at = time.time()
        self.store.kv_set(KV_PREFIX + rec.id, rec.model_dump_json())

    def _expire(self) -> None:
        cutoff = time.time() - TASK_TTL_MS / 1000
        for key in self.store.kv_keys(KV_PREFIX):
            raw = self.store.kv_get(key)
            try:
                if raw is None or TaskRecord.model_validate_json(raw).created_at < cutoff:
                    self.store.kv_delete(key)
            except ValidationError:
                self.store.kv_delete(key)

    # --- create
    def create(self, tool: str, result: types.CallToolResult, args: dict[str, Any]) -> dict[str, Any] | None:
        """A task for a call that started long work, or None when there is nothing to wait for."""
        data = result.structured_content
        if result.is_error or not isinstance(data, dict):
            return None
        if tool in ("submit_job", "follow_up_job"):
            kind, refs = "job", [data.get("id")]
        elif tool in ("new_session", "send_to_session"):
            if tool == "new_session" and not args.get("message"):
                return None  # the session was only created; no turn runs
            kind, refs = "session", [data.get("name")]
        elif tool == "fork_session":
            if not args.get("message"):
                return None
            kind, refs = "forks", [f.get("name") for f in data.get("forks", [])]
        elif tool == "run_agent":
            kind, refs = "agent", [data.get("sandbox"), data.get("run_id")]
        else:
            return None
        if not refs or not all(isinstance(r, str) for r in refs):
            return None
        self._expire()
        t = time.time()
        rec = TaskRecord(id=f"task-{secrets.token_hex(12)}", owner=self.actor(), tool=tool, kind=kind, refs=refs,
                         base=data, created_at=t, updated_at=t)
        self._save(rec)  # durable before the handle is returned (SEP-2663)
        return {"resultType": "task", **self._task(rec, "working", f"{tool} started")}

    def _task(self, rec: TaskRecord, status: str, message: str | None) -> dict[str, Any]:
        d = {"taskId": rec.id, "status": status, "createdAt": _iso(rec.created_at),
             "lastUpdatedAt": _iso(rec.updated_at), "ttlMs": TASK_TTL_MS,
             "pollIntervalMs": 5000 if rec.kind == "job" else 2000}
        if message:
            d["statusMessage"] = message[:500]
        return d

    # --- observe
    async def get(self, task_id: str, caps: types.ClientCapabilities | None) -> dict[str, Any]:
        rec = self._load(task_id)
        if rec.final is not None:
            return rec.final
        if rec.kind == "agent":
            return await self._agent(rec)
        if rec.kind == "forks":
            return self._forks(rec)
        ref = rec.refs[0]
        s, job = exchange._lookup(self.sessions, self.jobs, ref)  # noqa: SLF001
        if job is not None and job.state == JobState.CANCELLED and rec.cancelled:
            return self._finish(rec, {**self._task(rec, "cancelled", job.reason), "status": "cancelled"})
        if not exchange._settled(self.sessions, self.jobs, s, job):  # noqa: SLF001
            return self._task(rec, "working", self._activity(s, job))
        q = open_question(self.sessions, self.jobs, ref)
        if q is not None and q[0] not in rec.declined and supports_form_elicitation(caps):
            turn_no, question, label = q
            key = f"answer-{turn_no}"
            if rec.asked.get(key) != turn_no:
                rec.asked[key] = turn_no
                self._save(rec)
            req = question_request(label, question).model_dump(by_alias=True, mode="json", exclude_none=True)
            return {**self._task(rec, "input_required", question), "inputRequests": {key: req}}
        return self._complete(rec, s, job)

    def _activity(self, s: Any, job: Any) -> str | None:
        if job is not None and job.events:
            return f"{job.state.value}: {job.events[-1].message}"
        if s is not None:
            ev = self.sessions.events(s.id, tail=1)
            return f"{s.state.value}: {ev[0]['text']}" if ev and ev[0].get("text") else s.state.value
        return None

    def _complete(self, rec: TaskRecord, s: Any, job: Any) -> dict[str, Any]:
        turn = {**exchange.turn_view(s, job), "done": True}
        if job is not None:
            payload = {**self.jobs.view(job), "turn": turn}
            refs, err = [job_uri(job.id)], job.state in JOB_TERMINAL and job.state != JobState.SUCCEEDED
        else:
            payload = {**self.sessions.view(s), "turn": turn}
            refs, err = [session_uri(s.name)], turn.get("outcome") in ("error", "gone")
        if turn.get("turn") in rec.declined:
            payload["turn"]["elicitation"] = "declined"
        result = tool_result(payload, turn, refs, err)
        return self._finish(rec, {**self._task(rec, "completed", turn.get("summary") or turn.get("question")),
                                  "result": result.model_dump(by_alias=True, mode="json", exclude_none=True)})

    def _forks(self, rec: TaskRecord) -> dict[str, Any]:
        forks = [self.sessions.get(n) for n in rec.refs]
        busy = [s.name for s in forks if not exchange._settled(self.sessions, None, s, None)]  # noqa: SLF001
        if busy:
            return self._task(rec, "working", f"{len(forks) - len(busy)}/{len(forks)} forks done")
        views = [{**self.sessions.view(s), "turn": {**exchange.turn_view(s), "done": True}} for s in forks]
        payload = {**rec.base, "forks": views}
        content = tool_result(payload, None, [session_uri(s.name) for s in forks])
        return self._finish(rec, {**self._task(rec, "completed", f"{len(forks)} forks done"),
                                  "result": content.model_dump(by_alias=True, mode="json", exclude_none=True)})

    async def _agent(self, rec: TaskRecord) -> dict[str, Any]:
        sandbox, run_id = rec.refs
        try:
            st = await self.fleet.agent_status(sandbox, run_id, tail_lines=60)
        except (FleetError, ProviderError) as e:  # the sandbox went away: the run's outcome can't be read any more
            st = {"run_id": run_id, "state": "gone", "exit_code": None, "output_tail": None, "error": str(e)}
        if st["state"] == "running":
            tail = (st.get("output_tail") or "").strip().splitlines()
            return self._task(rec, "working", tail[-1] if tail else "running")
        payload = {**rec.base, **st}
        result = tool_result(payload, None, [], is_error=st["state"] == "gone")
        return self._finish(rec, {**self._task(rec, "completed", f"exit code {st.get('exit_code')}"),
                                  "result": result.model_dump(by_alias=True, mode="json", exclude_none=True)})

    def _finish(self, rec: TaskRecord, state: dict[str, Any]) -> dict[str, Any]:
        """Record the terminal state. If another request got there first (a cancel during a poll), theirs stands:
        a terminal state never changes."""
        raw = self.store.kv_get(KV_PREFIX + rec.id)
        if raw and (done := TaskRecord.model_validate_json(raw).final) is not None:
            return done
        rec.final = {**state, "lastUpdatedAt": _iso(time.time())}
        self._save(rec)
        return rec.final

    # --- answer and cancel
    def update(self, task_id: str, responses: dict[str, Any]) -> None:
        rec = self._load(task_id)
        if rec.final is not None or rec.kind not in ("job", "session"):
            return
        if len(responses) > MAX_RESPONSES:
            raise MCPError(INVALID_PARAMS, f"at most {MAX_RESPONSES} inputResponses per update")
        for key, raw in responses.items():
            turn_no = rec.asked.get(key)
            if turn_no is None:
                continue  # never issued (or for another task): ignored, as the extension asks
            try:
                resp = types.ElicitResult.model_validate(raw)
            except ValidationError as e:
                raise MCPError(INVALID_PARAMS, f"inputResponses[{key!r}] is not an elicitation result") from e
            outcome = answer(self.sessions, self.jobs, rec.refs[0], turn_no, resp, self.actor())
            if outcome in ("declined", "cancelled") and turn_no not in rec.declined:
                rec.declined.append(turn_no)
            elif outcome == "invalid":
                raise MCPError(INVALID_PARAMS, "the answer must be a non-empty string")
        self._save(rec)

    async def cancel(self, task_id: str) -> None:
        """Cancel the work where hangar can: a job is cancelled, an agent run is stopped. A session turn keeps
        running (suspend_session stops it); the task just stops following it."""
        rec = self._load(task_id)
        if rec.final is not None or rec.cancelled:
            return
        rec.cancelled = True
        self._save(rec)
        if rec.kind == "job":
            job = self.jobs.get(rec.refs[0])
            if job.state not in JOB_TERMINAL:
                self.jobs.cancel(job.id, requester=self.actor())
                return  # cancelled once the job says so (or completed, if it finished first)
        elif rec.kind == "agent":
            try:
                await self.fleet.terminal_kill(rec.refs[0], rec.refs[1])
            except Exception:  # noqa: BLE001 - already gone is as good as stopped
                pass
        self._finish(rec, self._task(rec, "cancelled", "cancelled by the client"))


class TasksExtension(Extension):
    """io.modelcontextprotocol/tasks for hangar's long-running tools."""

    identifier = TASKS_EXT

    def __init__(self, tasks: Tasks):
        self.tasks = tasks

    def methods(self) -> list[MethodBinding]:
        modern = frozenset(MODERN_PROTOCOL_VERSIONS)

        async def get(ctx: ServerRequestContext[Any], params: TaskIdParams) -> dict[str, Any]:
            return await self.tasks.get(params.task_id, ctx.session.client_capabilities)

        async def update(ctx: ServerRequestContext[Any], params: TaskUpdateParams) -> dict[str, Any]:
            self.tasks.update(params.task_id, params.input_responses)
            return {}

        async def cancel(ctx: ServerRequestContext[Any], params: TaskIdParams) -> dict[str, Any]:
            await self.tasks.cancel(params.task_id)
            return {}

        return [MethodBinding("tasks/get", TaskIdParams, get, modern),
                MethodBinding("tasks/update", TaskUpdateParams, update, modern),
                MethodBinding("tasks/cancel", TaskIdParams, cancel, modern)]

    async def intercept_tool_call(self, params: types.CallToolRequestParams, ctx: ServerRequestContext[Any],
                                  call_next: Any) -> Any:
        result = await call_next(ctx)
        if params.name not in TASK_TOOLS or ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            return result
        caps = ctx.session.client_capabilities
        if caps is None or not caps.extensions or TASKS_EXT not in caps.extensions:
            return result  # never a task for a client that did not ask on this request
        if not isinstance(result, types.CallToolResult):
            return result
        return self.tasks.create(params.name, result, params.arguments or {}) or result
