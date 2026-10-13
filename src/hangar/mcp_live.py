"""What MCP clients get beyond plain tool calls (workstream mcp-modern): typed results, tool hints, slim tool
schemas, live resources with change notifications for both protocol eras, and workflow prompts.

`mcp_server.build_mcp` wires these in. Tasks and elicitation live in `mcp_tasks`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import weakref
from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.subscriptions import ResourcesListChanged, ResourceUpdated, SubscriptionBus
from mcp.shared.exceptions import MCPError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from hangar import exchange
from hangar.models import Job, Session

log = logging.getLogger("hangar.mcp")


# ====================================================================== typed results (outputSchema)
class _Out(BaseModel):
    """Results keep every field the view has; the declared ones are what clients can rely on."""

    model_config = ConfigDict(extra="allow")


class SandboxOut(_Out):
    id: str
    name: str | None = None
    state: str
    environment: str | None = None
    provider: str | None = None
    repo: str | None = None
    branch: str | None = None
    harness: str | None = None
    expires_at: float | None = None
    error: str | None = None


class ArtifactOut(_Out):
    path: str
    turn: int
    size: int
    media_type: str | None = None
    uri: str | None = None


class TurnOut(_Out):
    done: bool | None = None
    session: str | None = None
    job: str | None = None
    state: str | None = None
    turn: int | None = None
    outcome: str | None = None
    exit_code: int | None = None
    summary: str | None = None
    result: Any = None
    result_error: str | None = None
    question: str | None = None
    needs_input: str | None = None
    artifacts: list[ArtifactOut] = []
    pr_url: str | None = None
    branch: str | None = None
    elicitation: str | None = None  # what happened when hangar asked you the agent's question


class JobResultOut(_Out):
    pr_url: str | None = None
    commits: list[str] = []
    pushed: bool | None = None
    summary: str | None = None
    data: Any = None
    question: str | None = None
    verify_exit_code: int | None = None


class JobOut(_Out):
    id: str
    title: str | None = None
    state: str
    reason: str | None = None
    repo: str | None = None
    branch: str | None = None
    environment: str | None = None
    active: bool | None = None
    result: JobResultOut | None = None
    usage: dict[str, Any] | None = None
    turn: dict[str, Any] | None = None  # when it ran as a task: its last turn, as wait_for_turn returns it


class SessionOut(_Out):
    name: str
    state: str
    id: str | None = None
    environment: str | None = None
    harness: str | None = None
    repo: str | None = None
    branch: str | None = None
    needs_input: str | None = None
    last_summary: str | None = None
    sandbox_id: str | None = None
    turn: dict[str, Any] | None = None  # when it ran as a task: the turn, as wait_for_turn returns it


class ForkOut(_Out):
    parent: str
    forks: list[SessionOut]


class EventOut(_Out):
    seq: int
    kind: str
    text: str = ""
    turn: int | None = None
    ts: float | None = None


class EventsOut(_Out):
    events: list[EventOut]
    next: int


class ExecOut(_Out):
    exit_code: int
    stdout: str
    stderr: str = ""
    truncated: bool = False


class AgentRunOut(_Out):
    run_id: str
    sandbox: str | None = None
    harness: str | None = None
    state: str | None = None  # running | finished (agent_status, or the task result)
    exit_code: int | None = None
    output_tail: str | None = None


# ====================================================================== tool hints (annotations)
def _hint(read_only: bool = False, destructive: bool = False, idempotent: bool = False,
          open_world: bool = False) -> ToolAnnotations:
    if read_only:
        return ToolAnnotations(read_only_hint=True, open_world_hint=open_world)
    return ToolAnnotations(read_only_hint=False, destructive_hint=destructive, idempotent_hint=idempotent,
                           open_world_hint=open_world)


READ = _hint(read_only=True)
# Hints are by tool name, so a tool added in mcp_server.py without an entry here shows up in test_mcp_modern.
# open_world marks tools whose effects leave hangar: agents that push branches and open PRs, commands with network.
TOOL_HINTS: dict[str, ToolAnnotations] = {
    **{n: READ for n in (
        "fleet_overview", "list_environments", "describe_environment", "list_credentials", "list_sandboxes",
        "get_sandbox", "read_file", "list_files", "terminal_read", "terminal_list", "agent_status", "list_snapshots",
        "list_sessions", "get_session", "get_artifact", "get_session_patch", "get_job", "list_jobs", "get_events",
        "list_schedules")},
    "build_environment": _hint(idempotent=True, open_world=True),
    "create_environment": _hint(destructive=True, idempotent=True),  # replaces a saved environment of that name
    "create_sandbox": _hint(),
    "create_sandboxes": _hint(),
    "destroy_sandbox": _hint(destructive=True, idempotent=True),
    "pause_sandbox": _hint(idempotent=True),
    "resume_sandbox": _hint(idempotent=True),
    "extend_sandbox": _hint(),
    "exec": _hint(destructive=True, open_world=True),
    "write_file": _hint(destructive=True, idempotent=True),
    "terminal_open": _hint(idempotent=True, open_world=True),
    "terminal_send": _hint(destructive=True, open_world=True),
    "terminal_close": _hint(destructive=True, idempotent=True),
    "run_agent": _hint(open_world=True),
    "snapshot_sandbox": _hint(),
    "fork_sandbox": _hint(),
    "new_session": _hint(open_world=True),
    "open_session": _hint(idempotent=True),
    "send_to_session": _hint(open_world=True),
    "keep_session": _hint(idempotent=True),
    "suspend_session": _hint(idempotent=True),
    "fork_session": _hint(open_world=True),
    "wait_for_turn": _hint(),  # it only reads, unless you answer the agent's question when asked
    "submit_job": _hint(open_world=True),
    "follow_up_job": _hint(open_world=True),
    "cancel_job": _hint(destructive=True, idempotent=True),
    "approve_job": _hint(idempotent=True, open_world=True),
    "reject_job": _hint(destructive=True, idempotent=True),
    "create_schedule": _hint(open_world=True),
    "delete_schedule": _hint(destructive=True, idempotent=True),
    "run_schedule_now": _hint(open_world=True),
}


# ====================================================================== slim tool listings
def _slim(schema: Any, inputs: bool) -> Any:
    """Drop what pydantic generates that says nothing: titles that repeat property names and, for an optional
    value, the `anyOf [T, null]` wrapper.

    Clients pay for every byte of tools/list, and models see the input schemas on every turn. An optional
    argument is already optional by being absent from `required` (the tool's own model still accepts null). An
    output that may be null becomes `"type": [T, "null"]`, and output defaults go (they mean nothing there)."""
    if isinstance(schema, list):
        return [_slim(s, inputs) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if (k == "title" and isinstance(v, str)) or (k == "default" and not inputs):
            continue
        if k in ("properties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {name: _slim(s, inputs) for name, s in v.items()}
        elif k in ("items", "additionalProperties", "anyOf", "oneOf", "allOf", "not", "prefixItems"):
            out[k] = _slim(v, inputs)
        else:
            out[k] = v
    arms = out.get("anyOf")
    if isinstance(arms, list) and len(arms) == 2 and {"type": "null"} in arms:
        (arm,) = [a for a in arms if a != {"type": "null"}] or [None]
        rest = {k: v for k, v in out.items() if k != "anyOf"}
        if inputs and out.get("default", 0) is None and isinstance(arm, dict):
            out = {**{k: v for k, v in rest.items() if k != "default"}, **arm}
        elif not inputs and isinstance(arm, dict) and set(arm) == {"type"} and isinstance(arm["type"], str):
            out = {**rest, "type": [arm["type"], "null"]}
    return out


class HangarMCP(MCPServer):
    """MCPServer with compact tool listings (the order stays the registration order, so listings are stable)."""

    async def list_tools(self) -> list[types.Tool]:
        tools = await super().list_tools()
        return [t.model_copy(update={"input_schema": _slim(t.input_schema, True),
                                     "output_schema": _slim(t.output_schema, False) if t.output_schema else None})
                for t in tools]


# ====================================================================== resources and change notifications
SESSION_URI = "hangar://sessions/{name}"
JOB_URI = "hangar://jobs/{job_id}"
_URI_RE = re.compile(r"hangar://(sessions|jobs)/[A-Za-z0-9._-]+(/events|/patch|/artifacts/\d+/.+)?")
MAX_LEGACY_SUBSCRIPTIONS = 2000
INVALID_PARAMS = -32602


def session_uri(name: str) -> str:
    return f"hangar://sessions/{name}"


def job_uri(job_id: str) -> str:
    return f"hangar://jobs/{job_id}"


class ChangeFeed:
    """Turns store writes of jobs and sessions into resource-updated notifications, for both protocol eras.

    2026-07-28 clients follow URIs on a `subscriptions/listen` stream (the SDK's bus delivers only what each stream
    asked for); earlier clients `resources/subscribe` on their session and are told on it. Writes can come from any
    thread; each URI is announced at most once per `interval`, since a notification only says "read it again"."""

    def __init__(self, bus: SubscriptionBus, interval: float = 0.25):
        self.bus = bus
        self.interval = interval
        self.legacy: dict[str, weakref.WeakSet[Any]] = {}
        self._dirty: set[str] = set()
        self._list_changed = False
        self._known: set[str] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    # --- producers
    def on_put(self, obj: Any) -> None:
        """Store hook: called after every write."""
        if isinstance(obj, Session):
            key, uris = obj.id, [session_uri(obj.name), session_uri(obj.name) + "/events"]
            if obj.job_id:  # a job's progress is its session's
                uris += [job_uri(obj.job_id), job_uri(obj.job_id) + "/events"]
        elif isinstance(obj, Job):
            key, uris = obj.id, [job_uri(obj.id)]
        else:
            return
        self.mark(uris, new_key=key)

    def bind(self) -> None:
        """Deliver on the running loop (the server's); writes from other threads are handed to it."""
        self.mark([])

    def mark(self, uris: list[str], new_key: str | None = None) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        with self._lock:
            if running is not None and running is not self._loop:
                self._loop, self._task = running, None  # first write on this loop (or a new loop in tests)
            if not uris:
                return
            if self._loop is None or self._loop.is_closed():
                return  # nothing serves MCP yet, so nobody can be listening
            self._dirty.update(uris)
            if new_key is not None and new_key not in self._known:
                self._known.add(new_key)
                self._list_changed = True
            loop = self._loop
        if running is loop:
            self._schedule()
        else:
            loop.call_soon_threadsafe(self._schedule)

    def _schedule(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._flush())

    # --- delivery
    async def _flush(self) -> None:
        await asyncio.sleep(self.interval)
        with self._lock:
            uris, self._dirty = self._dirty, set()
            list_changed, self._list_changed = self._list_changed, False
        for uri in sorted(uris):
            await self.publish(uri)
        if list_changed:
            try:
                await self.bus.publish(ResourcesListChanged())
            except Exception:  # noqa: BLE001 - a notification is best effort
                log.debug("resources list-changed publish failed", exc_info=True)

    async def publish(self, uri: str) -> None:
        try:
            await self.bus.publish(ResourceUpdated(uri=uri))
        except Exception:  # noqa: BLE001
            log.debug("resource-updated publish failed for %s", uri, exc_info=True)
        for conn in list(self.legacy.get(uri, ())):
            try:
                await conn.send_resource_updated(uri)
            except Exception:  # noqa: BLE001 - the client went away: forget it
                self.legacy.get(uri, weakref.WeakSet()).discard(conn)

    # --- legacy resources/subscribe (pre-2026 sessions)
    def install_legacy(self, mcp: MCPServer) -> None:
        """Serve resources/subscribe and resources/unsubscribe for handshake-era sessions.

        Only useful when legacy clients keep a session (stateful streamable HTTP or stdio): a stateless request has
        no stream to notify on later. The SDK offers no public hook for either: MCPServer has no handler slot for
        these methods (they go on its low-level server), and a handler's `ctx.session` lives for one request, so
        the subscription holds (weakly) the connection behind it, which is what notifies on the session's own
        stream."""

        async def subscribe(ctx: ServerRequestContext[Any], params: types.SubscribeRequestParams) -> types.EmptyResult:
            uri = str(params.uri)
            if not _URI_RE.fullmatch(uri):
                raise MCPError(INVALID_PARAMS, f"not a hangar resource: {uri} (hangar://sessions/<name>"
                                               "[/events|/patch] or hangar://jobs/<id>[/events])")
            if sum(len(s) for s in self.legacy.values()) >= MAX_LEGACY_SUBSCRIPTIONS:
                raise MCPError(INVALID_PARAMS, "too many resource subscriptions on this server")
            self.bind()
            self.legacy.setdefault(uri, weakref.WeakSet()).add(_connection(ctx))
            return types.EmptyResult()

        async def unsubscribe(ctx: ServerRequestContext[Any],
                              params: types.UnsubscribeRequestParams) -> types.EmptyResult:
            subs = self.legacy.get(str(params.uri))
            if subs is not None:
                subs.discard(_connection(ctx))
                if not subs:
                    self.legacy.pop(str(params.uri), None)
            return types.EmptyResult()

        low = mcp._lowlevel_server  # noqa: SLF001 - see docstring
        low.add_request_handler("resources/subscribe", types.SubscribeRequestParams, subscribe)
        low.add_request_handler("resources/unsubscribe", types.UnsubscribeRequestParams, unsubscribe)


def _connection(ctx: ServerRequestContext[Any]) -> Any:
    return ctx.session._connection  # noqa: SLF001 - see install_legacy


def _json(d: Any) -> str:
    return json.dumps(d, indent=1, default=str)


def register_resources(mcp: MCPServer, sessions: Any, jobs: Any) -> None:
    """Sessions and jobs as resources, so a client can read and follow them (subscribe) instead of polling."""

    @mcp.resource(SESSION_URI, name="session", mime_type="application/json",
                  description="A session: state, turns, the agent's summaries and question, checkpoints.")
    async def session_resource(name: str) -> str:
        return _json(sessions.view(sessions.get(name)))

    @mcp.resource(SESSION_URI + "/events", name="session-events", mime_type="application/json",
                  description="The last 200 activity events of a session (messages, tool calls, commands...).")
    async def session_events(name: str) -> str:
        return _json({"events": sessions.events(name, tail=200)})

    @mcp.resource(SESSION_URI + "/patch", name="session-patch", mime_type="text/x-diff",
                  description="The session's work as one patch against its base branch, ready for git apply.")
    async def session_patch(name: str) -> str:
        p = await exchange.session_patch(sessions, sessions.get(name), 5_000_000)
        return p["patch"]

    if jobs is None:
        return

    @mcp.resource(JOB_URI, name="job", mime_type="application/json",
                  description="A job: state, timeline, result (commits, PR, verification, summary), attempts.")
    async def job_resource(job_id: str) -> str:
        return _json(jobs.view(jobs.get(job_id)))

    @mcp.resource(JOB_URI + "/events", name="job-events", mime_type="application/json",
                  description="The last 200 activity events of a job's agent.")
    async def job_events(job_id: str) -> str:
        return _json({"events": jobs.events(job_id, tail=200)})


def patch_link(name: str, patch: dict[str, Any]) -> types.ResourceLink:
    return types.ResourceLink(uri=session_uri(name) + "/patch", name=f"{name}.patch", mime_type="text/x-diff",
                              description="The whole patch" + (" (the inline one was cut)" if patch.get("truncated")
                                                               else ""))


def links(turn: dict[str, Any] | None, *refs: str) -> list[types.ResourceLink]:
    """resource_link blocks for a result: the job or session it is about and the files the agent handed back."""
    out = [types.ResourceLink(uri=r, name=r.removeprefix("hangar://"), mime_type="application/json") for r in refs]
    for a in (turn or {}).get("artifacts") or []:
        if a.get("uri"):
            out.append(types.ResourceLink(uri=a["uri"], name=a["path"], mime_type=a.get("media_type"),
                                          size=a.get("size")))
    return out


# ====================================================================== prompts
def register_prompts(mcp: MCPServer, sessions: Any) -> None:
    """Workflows worth knowing, as prompts a client can offer (slash commands in Claude Code and VS Code)."""

    @mcp.prompt(name="hand_off_to_cloud", title="Hand this branch to a cloud session",
                description="Continue the work in this checkout (unpushed commits and uncommitted changes) in a "
                            "hangar session, then follow it.")
    def hand_off(task: str, name: str = "") -> str:
        who = f"named {name!r}" if name else "with a short, descriptive name"
        return f"""Hand the work in this git checkout to a hangar cloud session {who} and follow it.

Task for the agent: {task}

1. If the `hangar` CLI is installed and logged in, run: hangar session new <name> "<task>" --from-local
   It bundles unpushed commits and uncommitted changes. Otherwise collect them yourself:
   - uncommitted changes: `git diff --binary HEAD` -> context item {{"name": "wip.patch", "kind": "patch",
     "content": <the diff>}}
   - unpushed commits: `git bundle create - <upstream>..HEAD | base64 -w0` -> {{"name": "local.bundle",
     "kind": "bundle", "content": <base64>, "encoding": "base64", "base_commit": <upstream sha>}}
   and call new_session(name, repo=<origin url>, base_branch=<upstream branch>, message=<task>, context=[...]).
2. Follow the turn: wait_for_turn(<name>) (it reports progress; call it again while `done` is false), or subscribe
   to hangar://sessions/<name>. If the agent asks something, answer with send_to_session.
3. When it is done, summarize what changed. Bring the work back with `hangar session pull <name>` or
   get_session_patch(<name>) and `git apply`."""

    @mcp.prompt(name="best_of_n", title="Best-of-N on an issue",
                description="Run N agents on the same issue in parallel and deliver the attempt that verifies best.")
    def best_of_n(issue: str, repo: str, n: str = "3", verify: str = "") -> str:
        v = f', verify="{verify}"' if verify else ""
        return f"""Run a best-of-{n} hangar job on this work and report the outcome.

Repository: {repo}
Issue or instructions: {issue}

1. submit_job(instructions=<the issue text, with any context you have>, repo="{repo}", strategy="best_of_n",
   n={n}{v}). Use judge=true when tests alone can't tell the attempts apart. It costs {n} sandboxes and agent runs.
2. Follow it with wait_for_turn(<job id>) until `done`, or let your client track the task.
3. Report the delivered PR, why that attempt won (get_job lists every attempt: verify result, diff size, score),
   and anything the agent asked or left unfinished."""

    @mcp.prompt(name="pick_up_session", title="Pick up a session",
                description="Catch up on a hangar session (what was asked, what the agent did, where it stopped) "
                            "and continue it.")
    def pick_up(session: str) -> str:
        s = sessions.get(session)
        turns = s.turns[-3:]
        recap = "\n".join(f"- turn {len(s.turns) - len(turns) + i}: asked {t.message[:300]!r} -> "
                          f"{t.outcome or 'running'}: {(t.summary or t.question or '')[:500]}"
                          for i, t in enumerate(turns, 1)) or "- no turns yet"
        q = f"\nThe agent is waiting for an answer: {s.needs_input}\n" if s.needs_input else ""
        return f"""Pick up hangar session {s.name!r} ({s.state.value}, repo {s.repo or '-'}, branch {s.branch}).

Recent turns:
{recap}
{q}
1. Read hangar://sessions/{s.name} (or get_session) for the full record and get_events(session=...) for detail.
2. Continue it with send_to_session("{s.name}", <message>); the agent resumes its own conversation and the
   session's commits and uncommitted work are restored first. Then wait_for_turn("{s.name}").
3. To work in its sandbox yourself instead, open_session("{s.name}"); to take the code locally,
   `hangar session pull {s.name}`."""
