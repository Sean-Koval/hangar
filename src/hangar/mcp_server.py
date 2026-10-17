"""MCP server: exposes the fleet to any MCP client (Codex, Claude Code, ChatGPT dev mode, ...).

Tool design notes
* Every tool takes a sandbox id *or* name, so a client can say `exec("api-refactor", ...)`.
* Outputs are compact JSON-able dicts; long outputs are tail-truncated by the providers.
* `terminal_*` tools give persistent tmux sessions for long-running or interactive work
  (dev servers, REPLs, other agents); `exec` is for one-shot commands with an exit code.
* `run_agent` starts a headless coding agent (Claude Code / Codex / agy / Gemini) inside a
  sandbox, so one orchestrator session can fan work out across a fleet.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import functools
import inspect
import json
import logging
import time
from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.subscriptions import InMemorySubscriptionBus
from mcp.shared.exceptions import NoBackChannelError
from mcp_types import CallToolResult, ElicitResult, Icon, InputRequiredResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

import hangar.identity as identity  # --- identity
import hangar.people as people  # --- identity
from hangar import approvals as approvals  # --- approvals (#121): who may release a held job
from hangar import computer_api as computer_api  # --- computer
from hangar import exchange, mcp_steer, mcp_tasks, observe  # mcp_tasks: elicitation; mcp_steer: live turns; --- observe
from hangar import mcp_deadline as contract  # --- deadlines: the client-deadline contract
from hangar import mcp_fresh as fresh  # --- deadlines: server version, stale tool lists
from hangar import mcp_live as live  # workstream mcp-modern: typed results, hints, resources, prompts
from hangar.broker import CredentialError
from hangar.envrequests import EnvRequests  # --- computer
from hangar.fleet import Fleet, FleetError, _slug
from hangar.jobs import Caller, JobService, SubmitJob
from hangar.models import Budget, ContextItem, Sandbox, new_id
from hangar.providers.base import ProviderError
from hangar.sessions import NewSession, SessionService
from hangar.strategies import fork_session as _fork_session  # workstream D: forks/best-of-N
from hangar.stream_api import exec_stream, register_streams  # --- stream: exec(stream=true), process, watches
from hangar.triggers import ScheduleSpec, Triggers

# Failures an agent can act on (bad name, quota, build error...). Anything else is a crash and stays generic.
EXPECTED = (FleetError, ProviderError, CredentialError, ValueError, KeyError, NotImplementedError)
EXPECTED += (identity.Forbidden,)  # --- identity: a scope refusal is the client's to read

current_actor: contextvars.ContextVar[str] = contextvars.ContextVar("hangar_actor", default="mcp")
log = logging.getLogger("hangar.mcp")
ELICIT_WAIT_SECONDS = 900  # how long an agent's question put to you by wait_for_turn waits for your answer


async def _elicit(ctx: Context, closing: Any, wait: float, message: str, schema: type) -> Any:
    """ctx.elicit, given up with TimeoutError after `wait` seconds or when a shutdown starts (integrate: #57's
    long-polls answer on SIGTERM; a question or an approval put to a client would hold the pod for up to 15 min)."""
    async def cut() -> None:
        await closing.wait()
        scope.cancel()

    with anyio.fail_after(wait), anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tg:
            tg.start_soon(cut)
            res = await ctx.elicit(message, schema)
            tg.cancel_scope.cancel()
            return res
    raise TimeoutError("the control plane is shutting down")
READ_FILE_LIMIT = 10 * 1024 * 1024  # --- overnight/sweep: read_file answers with at most this much of a file

INSTRUCTIONS = """\
Hangar runs coding agents (Claude Code, Codex, pi, Antigravity) in cloud sandboxes. Pick the level you need:

- submit_job(instructions, repo=..., context=[...]): hand off a piece of work. An agent runs in a fresh sandbox,
  hangar verifies it, pushes a branch and opens a draft PR. follow_up_job continues it; strategy="best_of_n"
  runs n attempts and delivers the best.
- new_session(name, environment, message) / send_to_session(name, message): a stream of work you come back to,
  even days later; the agent resumes its own conversation and the repo state is restored. fork_session branches it.
- create_sandbox + exec / terminal_* / read_file / write_file / run_agent: drive a sandbox yourself.

Long work: wait_for_turn(job id or session name) waits with progress and returns the turn (summary, PR, structured
result, artifacts). If the agent asks a question, hangar asks you (elicitation) when your client supports it and
continues with your answer; otherwise answer with follow_up_job / send_to_session. Clients with the MCP tasks
extension get a task handle from the long tools instead. Jobs and sessions are also resources
(hangar://jobs/<id>, hangar://sessions/<name>, .../events) you can subscribe to instead of polling.

Context in: `context` items carry files, your uncommitted changes ({"kind": "patch", "content": <git diff
--binary>}) and unpushed commits ({"kind": "bundle", "content": <base64 git bundle>, "encoding": "base64",
"base_commit": <sha on the remote>}). `output_schema` (JSON Schema) asks for a structured result. Code out:
get_session_patch. The prompts hand_off_to_cloud, best_of_n and pick_up_session walk through common workflows.
"""


# --- #140: the second server's own instructions
SANDBOX_INSTRUCTIONS = """\
Hangar's sandbox tools: drive cloud sandboxes yourself. create_sandbox (or fork_sandbox from a snapshot), then exec,
process (background commands), read_file / write_file / list_files, terminal_* for interactive programs, and
run_agent / agent_status to start a coding agent inside one. snapshot_sandbox keeps its state; destroy_sandbox ends
it. create_environment and build_environment define and build what sandboxes start from.

To hand work off instead (an agent runs, hangar verifies it and opens a PR), use the `hangar` server's submit_job,
new_session and wait_for_turn: the work tools live there, not here.
"""
# What the `hangar` server lists, for hangar-sandbox's "that tool is on the other server" error
WORK_TOOL_NAMES = frozenset({
    "fleet_overview", "list_environments", "describe_environment", "list_credentials", "new_session", "list_sessions",
    "get_session", "send_to_session", "keep_session", "suspend_session", "fork_session", "wait_for_turn",
    "get_artifact", "get_session_patch", "steer_turn", "answer_approval", "submit_job", "get_job", "list_jobs",
    "follow_up_job", "cancel_job", "get_events", "approve_job", "reject_job", "create_schedule", "list_schedules",
    "delete_schedule", "run_schedule_now"})


def _sb(s: Sandbox) -> dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "state": s.state.value, "environment": s.environment, "provider": s.provider,
        "repo": s.repo, "branch": s.info.get("work_branch") or s.branch, "repo_path": s.info.get("repo_path"),
        "harness": s.info.get("harness"), "credentials": s.credentials, "labels": s.labels, "parent": s.parent,
        "expires_at": s.expires_at, "error": s.error,
        "stage": s.info.get("stage") if s.state.value in ("starting", "pending") else None,
        "env_build": s.info.get("env_build"),  # --- envbuilds: the repo build it runs, or why the base image
        "model": s.info.get("model"),  # --- canary: the concrete model its runs use (a policy, resolved at create)
    }


_BACKGROUND: set[asyncio.Task] = set()


def _detach(coro, what: str) -> asyncio.Task:
    """Run long work outside the MCP request. A client that times out or disconnects cancels its request, and
    the work must not die with it (a half-created sandbox, a build that never finishes and restarts on every try)."""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)

    def done(t: asyncio.Task) -> None:
        _BACKGROUND.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("%s failed in the background: %s", what, t.exception())

    task.add_done_callback(done)
    return task


async def _within(task: asyncio.Task, seconds: float):
    """The task's result if it finishes within `seconds`, else None (it keeps running). Cancelling the caller
    leaves the task alone."""
    try:
        return await asyncio.wait_for(asyncio.shield(task), seconds)
    except TimeoutError:
        return None


ICON = Icon(src="data:image/svg+xml;base64," + base64.b64encode(
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" '
    b'fill="#1f2937"/><path d="M4 24 16 8l12 16z" fill="none" stroke="#f59e0b" stroke-width="3" '
    b'stroke-linejoin="round"/><path d="M11 24h10" stroke="#f59e0b" stroke-width="3"/></svg>').decode(),
    mime_type="image/svg+xml", sizes=["any"])


def build_mcp(fleet: Fleet, jobs: JobService | None = None, sessions: SessionService | None = None,
              triggers: Triggers | None = None, legacy_sessions: bool = True, surface: str = "work") -> MCPServer:
    """`legacy_sessions`: pre-2026 clients keep a session (stateful streamable HTTP), so hangar can ask them
    questions mid-call and tell them about resources they subscribed to. `surface` (#140): `work` builds `hangar`
    (/mcp: jobs, sessions, schedules, discovery; the sandbox tools callable but hidden for one release), `sandbox`
    builds `hangar-sandbox` (/mcp/sandbox: the raw drive tools, mcp_live.SANDBOX_TOOLS)."""
    if surface not in ("work", "sandbox"):
        raise ValueError(f"surface must be work or sandbox, not {surface!r}")
    drive = surface == "sandbox"
    # --- workstream mcp-modern: tasks extension, change notifications, compact listings
    bus = InMemorySubscriptionBus()
    feed = live.ChangeFeed(bus)
    # --- #140: on both servers: jobs' and sessions' long tools on `hangar`, run_agent on `hangar-sandbox`
    tasks = mcp_tasks.Tasks(fleet.store, fleet, sessions, jobs, current_actor.get) if sessions is not None else None
    mcp = live.HangarMCP(live.SANDBOX if drive else live.WORK, title="Hangar sandboxes" if drive else "Hangar",
                         instructions=SANDBOX_INSTRUCTIONS if drive else INSTRUCTIONS, version="", icons=[ICON],
                         website_url="https://github.com/Sean-Koval/hangar", subscriptions=bus,
                         extensions=[mcp_tasks.TasksExtension(tasks)] if tasks else None,
                         # a question put to you in wait_for_turn may wait this long for your answer
                         request_state_security=RequestStateSecurity.ephemeral(ttl=ELICIT_WAIT_SECONDS))

    guard = people.Guard(fleet, sessions, jobs, triggers)  # --- identity: members act on their own work only

    def tool(scope: str | None = None, rule: str | None = None):
        """mcp.tool() that reports expected failures to the client instead of a generic error. The description is
        the docstring without its indentation; hints come from mcp_live.TOOL_HINTS. `scope` (e.g. "admin"): only
        tokens holding it may call the tool (--- identity). `rule` names a check the tool makes itself instead
        (e.g. "approvals.can_decide"). Both are recorded on the registered function as `__hangar_scope__` (default
        "mcp": any token the MCP endpoint accepts) and `__hangar_guarded__`, which tests/test_scope_parity.py
        checks against tests/fixtures/scope_matrix.yaml (#122)."""

        def deco(fn):
            home = live.server_of(fn.__name__)  # --- #140: which server lists it
            if drive and home != live.SANDBOX:
                return fn  # a work tool: not on hangar-sandbox at all
            if not drive and home == live.SANDBOX:
                mcp.moved = mcp.moved | {fn.__name__}  # on hangar for one release: callable, hidden, with a note
            if scope:  # --- identity
                fn = identity.scoped(fn, scope)
            fn = people.guarded(fn, guard)  # --- identity
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def wrapped(*a, **kw):
                    # --- deadlines: waits lowered to the budget; a call that would outlive it is cut off (mcp_deadline)
                    budget = fleet.settings.mcp_tool_wait_seconds
                    kw = contract.clamp(kw, budget)
                    bounded = fn.__name__ not in contract.DETACHED | contract.SELF_TIMED
                    try:
                        async with asyncio.timeout(contract.limit(budget) if bounded else None) as cm:
                            return await fn(*a, **kw)
                    except EXPECTED as e:
                        raise ToolError(str(e).strip("'\"")) from e
                    except TimeoutError as e:
                        if not cm.expired():
                            raise
                        raise ToolError(contract.cut_off_message(fn.__name__, budget)) from e
            else:
                @functools.wraps(fn)
                def wrapped(*a, **kw):
                    try:
                        return fn(*a, **kw)
                    except EXPECTED as e:
                        raise ToolError(str(e).strip("'\"")) from e
            wrapped = observe.mcp_tool(fn.__name__, wrapped)  # --- observe: a span and /metrics per call
            wrapped.__hangar_scope__ = rule or scope or "mcp"  # --- scope parity (#122)
            wrapped.__hangar_guarded__ = True  # people.guarded above: a member's calls reach their own records only
            return mcp.tool(description=inspect.cleandoc(fn.__doc__ or ""),
                            annotations=live.TOOL_HINTS.get(fn.__name__))(wrapped)

        return deco

    def actor() -> str:
        return f"mcp:{current_actor.get()}"

    # ------------------------------------------------------------------ discovery
    @tool()
    def fleet_overview() -> dict[str, Any]:
        """Counts of sandboxes by state, available providers and their capabilities."""
        return fleet.overview()

    @tool()
    def list_environments() -> list[dict[str, Any]]:
        """List environment templates (tools, harnesses, deps, skills, repo) and their built images."""
        return fleet.environments()

    @tool()
    def describe_environment(name: str) -> dict[str, Any]:
        """Full spec and the rendered Dockerfile for an environment."""
        d = fleet.environment_detail(name)
        return {"spec": d["spec"], "dockerfile": d["dockerfile"]}

    @tool()
    async def build_environment(name: str, provider: str | None = None, force: bool = False) -> dict[str, Any]:
        """Build (or rebuild with force=True) an environment's image and warm pool. Can take minutes; calling again
        while it runs returns that build."""
        job = await fleet.build(name, provider, force=force, actor=actor(), wait=False)
        # The build runs on its own task, so a client that gives up or disconnects doesn't cancel it.
        await _within(job.task, fleet.settings.mcp_tool_wait_seconds)
        return job.summary()  # "running" if it is still going; asking again returns this job (fleet.build, B17)

    @tool()
    def create_environment(spec: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        """Save a new or updated environment. dry_run=True only checks it: resolves `extends`, validates,
        lists warnings (missing credentials, deprecated harnesses) and returns the Dockerfile. To clone one,
        extend it: {"name": "api-codex", "extends": "api", "harnesses": ["codex"]} (maps merge, lists replace
        except tools.apt and dependencies.pip/npm; skills_remove drops a skill). Defaults to extend: bare, python,
        node, go, polyglot. Then build_environment.

        Spec fields: name, description, extends, base | image (prebuilt), tools {apt, mise, script},
        harnesses [claude-code, codex, pi, antigravity, git], default_harness, harness_mode (auto|pack|bake),
        model, dependencies {pip, npm, script}, skills [{path|git, name}], agent_config {instructions, files},
        mcp {name: {url, headers} | {command, args, env}}, secrets {ENV_VAR: credential}, env, repo {url,
        branch, setup, bake}, credentials, resources {cpus, memory_gb}, warm_pool, runtime_class."""
        if dry_run:
            return fleet.validate_environment(spec)
        s = fleet.save_environment(spec, actor=actor())
        return {"name": s.name, "extends": s.extends, "harnesses": s.harnesses, "default_harness": s.pick_harness()}

    @tool()
    def list_credentials() -> list[dict[str, Any]]:
        """Credentials available to attach to sandboxes (names and metadata only, never secrets)."""
        return [c.model_dump(include={"name", "harness", "kind", "exclusive", "leased_to", "description", "person",
                                      "expires_at", "last_ok", "health"})  # --- clouddoctor: credhealth.py
                for c in people.usable(fleet.broker.list())]  # --- identity: shared and the caller's own

    # ------------------------------------------------------------------ lifecycle
    @tool()
    async def create_sandbox(
        environment: str,
        name: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        work_branch: str | None = None,
        credentials: list[str] | None = None,
        provider: str | None = None,
        ttl_minutes: int | None = None,
        labels: dict[str, str] | None = None,
        harness: str | None = None,
    ) -> live.SandboxOut:
        """Start a sandbox from an environment. Optionally clone `repo` (at `branch`) and check out a
        new `work_branch`. `harness` picks which of the environment's agents this sandbox is for (default:
        the environment's default); only that agent's credentials, config and MCP servers are applied.
        `credentials` overrides the environment's default credential list."""
        started = time.time()
        # A name of our own, so a create still running past the wait can be found again (and no one else's is).
        name = name or f"{environment}-{new_id('x')[2:]}"
        task = _detach(fleet.create(environment, name=name, repo=repo, branch=branch, work_branch=work_branch,
                                    credentials=credentials, provider=provider, ttl_minutes=ttl_minutes,
                                    labels=labels, harness=harness, actor=actor(),
                                    person=identity.person()), "create_sandbox")  # --- identity
        sb = await _within(task, fleet.settings.mcp_tool_wait_seconds)
        if sb is not None:
            return _sb(sb)
        # Still starting (a cold node, an image build): hand back the sandbox as it stands; get_sandbox shows when
        # it is running, and `stage` what it is waiting on.
        want = _slug(name)
        mine = max((s for s in people.mine(fleet.list(include_stopped=True)) if s.environment == environment
                    and s.created_at >= started - 1 and (s.name == want or s.name.startswith(want + "-"))),
                   key=lambda s: s.created_at, default=None)
        if mine is None:
            raise FleetError("the sandbox is still being set up; check list_sandboxes in a minute")
        return _sb(mine)

    @tool()
    async def create_sandboxes(
        environment: str,
        count: int,
        name_prefix: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        work_branch_prefix: str | None = None,
        credentials: list[str] | None = None,
        provider: str | None = None,
        ttl_minutes: int | None = None,
        labels: dict[str, str] | None = None,
        harness: str | None = None,
    ) -> list[dict[str, Any]]:
        """Start `count` (1-20) identical sandboxes concurrently, each on its own work branch. To copy one you
        already prepared (deps installed, files changed), use fork_sandbox."""
        import asyncio

        if not 1 <= count <= 20:
            raise ValueError("count must be 1..20")
        prefix = name_prefix or environment

        async def one(i: int):
            return await fleet.create(
                environment, name=f"{prefix}-{i + 1}", repo=repo, branch=branch,
                work_branch=f"{work_branch_prefix}-{i + 1}" if work_branch_prefix else None,
                credentials=credentials, provider=provider, ttl_minutes=ttl_minutes, labels=labels,
                harness=harness, actor=actor(), person=identity.person())  # --- identity

        started = time.time()
        async def every() -> list[Any]:  # --- deadlines: create_task takes a coroutine, not gather's future
            return await asyncio.gather(*(one(i) for i in range(count)), return_exceptions=True)

        task = _detach(every(), "create_sandboxes")
        res = await _within(task, fleet.settings.mcp_tool_wait_seconds)
        if res is None:  # still starting: report what exists so far; they keep coming up in the background
            return [_sb(s) for s in people.mine(fleet.list()) if s.environment == environment  # --- identity
                    and s.created_at >= started and s.name.startswith(_slug(prefix))]
        return [_sb(r) if isinstance(r, Sandbox) else {"error": str(r)} for r in res]

    @tool()
    def list_sandboxes(include_stopped: bool = False, label: str | None = None) -> list[live.SandboxOut]:
        """List sandboxes. `label` filters by key or key=value."""
        return [_sb(s) for s in people.mine(fleet.list(include_stopped=include_stopped, label=label))]  # identity

    @tool()
    def get_sandbox(sandbox: str) -> live.SandboxOut:
        """Details for one sandbox, including recent agent runs."""
        s = fleet.get(sandbox)
        return {**_sb(s), "runs": s.info.get("runs", [])[-10:], "created_at": s.created_at,
                "last_activity": s.last_activity}

    async def _destroy(refs: list[str], label: str | None) -> list[str]:
        """--- deadlines: a destroy checkpoints sessions first and can outlast the client; it finishes regardless
        (an error comes back if it fails within the wait)."""
        # --- identity: named refs passed the guard; a member's label reaches only their own sandboxes
        targets = [fleet.get(r) for r in refs] if refs else people.mine(fleet.list(label=label))
        ids = [s.id for s in targets]
        if not ids:  # a label that matches nothing (destroy_many(None) would mean every sandbox)
            return []
        work = fleet.destroy(ids[0], actor=actor()) if refs and len(ids) == 1 else \
            fleet.destroy_many(ids, actor=actor())
        await _within(_detach(work, "destroy_sandbox"), fleet.settings.mcp_tool_wait_seconds)
        return ids

    @tool()
    async def destroy_sandbox(sandbox: str | None = None, sandboxes: list[str] | None = None,
                              label: str | None = None) -> list[str]:
        """Stop and delete one sandbox, several (`sandboxes`: ids or names) or every active one matching `label`
        (key or key=value). Exclusive credentials are written back and released. Returns the destroyed ids."""
        refs = ([sandbox] if sandbox else []) + (sandboxes or [])
        if not refs and not label:
            raise ValueError("pass sandbox, sandboxes or label (refusing to destroy the whole fleet implicitly)")
        if refs and label:
            raise ValueError("pass sandboxes or a label, not both")
        return await _destroy(refs, label)

    # --- review: the tools merged above, kept callable under their old names and signatures (not listed:
    # mcp_live.HIDDEN_TOOLS), so existing scripts and client workflows don't break.
    @tool()
    def validate_environment(spec: dict[str, Any]) -> dict[str, Any]:
        """Deprecated: create_environment(spec, dry_run=True)."""
        return fleet.validate_environment(spec)

    @tool()
    def clone_environment(source: str, name: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deprecated: create_environment({"name": ..., "extends": source, ...})."""
        s = fleet.clone_environment(source, name, overrides, actor=actor())
        return {"name": s.name, "extends": source, "harnesses": s.harnesses, "default_harness": s.pick_harness()}

    @tool()
    async def destroy_sandboxes(sandboxes: list[str] | None = None, label: str | None = None) -> list[str]:
        """Deprecated: destroy_sandbox(sandboxes=... | label=...)."""
        if not sandboxes and not label:
            raise ValueError("pass sandboxes or label (refusing to destroy the whole fleet implicitly)")
        return await _destroy(sandboxes or [], None if sandboxes else label)  # --- deadlines

    async def _lifecycle(work, sandbox: str, what: str) -> dict[str, Any]:
        """--- deadlines: a pause, resume or expiry change on its own task (a cancel half-way leaves hangar's record
        and the provider disagreeing); past the wait, the sandbox as it stands."""
        sb = await _within(_detach(work, f"{what} {sandbox}"), fleet.settings.mcp_tool_wait_seconds)
        if sb is not None:
            return _sb(sb)
        return {**_sb(fleet.get(sandbox)), "note": f"still {what}; get_sandbox shows when it is done"}

    @tool()
    async def pause_sandbox(sandbox: str) -> live.SandboxOut:
        """Freeze a sandbox's processes to free CPU (state is kept)."""
        return await _lifecycle(fleet.pause(sandbox, actor=actor()), sandbox, "pausing")

    @tool()
    async def resume_sandbox(sandbox: str) -> live.SandboxOut:
        """Resume a paused sandbox."""
        return await _lifecycle(fleet.resume(sandbox, actor=actor()), sandbox, "resuming")

    @tool()
    async def extend_sandbox(sandbox: str, minutes: int = 60) -> live.SandboxOut:
        """Push back a sandbox's expiry."""
        return await _lifecycle(fleet.extend(sandbox, minutes, actor=actor()), sandbox, "extending")

    # ------------------------------------------------------------------ commands & files
    @tool()
    async def exec(sandbox: str, command: str, ctx: Context, cwd: str | None = None, timeout: int = 120,
                   stream: bool = False) -> live.ExecOut:
        """Run a shell command (bash -lc) in a sandbox and wait: exit_code, stdout, stderr (stream=true: also as
        progress while it runs). Max 150 s; longer work and servers: process; interactive: terminal_open."""
        if stream:  # --- stream
            out = await exec_stream(fleet, ctx, sandbox, command, cwd, timeout, current_actor.get(), actor())
        else:
            out = (await fleet.exec(sandbox, command, cwd=cwd, timeout=timeout, actor=actor())).model_dump()
        if out.get("exit_code") == 124 and timeout >= int(fleet.settings.mcp_tool_wait_seconds):  # --- deadlines
            out["note"] = (f"exec waits at most {fleet.settings.mcp_tool_wait_seconds:g} s; run longer commands "
                           "with process(action='start') and follow them with process(action='attach')")
        return out

    @tool()
    async def read_file(sandbox: str, path: str, binary: bool = False) -> dict[str, Any]:
        """Read a file (its first 10 MB). Text by default; binary=True returns base64."""
        data = await fleet.read_file(sandbox, path, limit=READ_FILE_LIMIT)  # --- overnight/sweep: the sandbox's size
        if data is None:
            return {"path": path, "exists": False}
        cut = {"truncated": True, "note": f"only the first {READ_FILE_LIMIT // 1024 // 1024} MB; read the rest with "
               "exec (tail -c, split)"} if len(data) > READ_FILE_LIMIT else {}
        data = data[:READ_FILE_LIMIT]
        if binary:
            return {"path": path, "exists": True, "base64": base64.b64encode(data).decode(), **cut}
        return {"path": path, "exists": True, "content": data.decode(errors="replace"), **cut}

    @tool()
    async def write_file(sandbox: str, path: str, content: str, base64_encoded: bool = False) -> dict[str, Any]:
        """Write a file (parents are created). Set base64_encoded for binary content."""
        data = base64.b64decode(content) if base64_encoded else content.encode()
        p = await fleet.write_file(sandbox, path, data, actor=actor())
        return {"path": p, "bytes": len(data)}

    @tool()
    async def list_files(sandbox: str, path: str = ".", depth: int = 2) -> str:
        """List files under a directory (type, size, path), skipping .git."""
        return await fleet.list_files(sandbox, path, depth)

    # ------------------------------------------------------------------ terminals
    @tool()
    async def terminal_open(sandbox: str, session: str = "main", command: str | None = None,
                            cwd: str | None = None) -> dict[str, Any]:
        """Open (or reuse) a persistent tmux terminal in a sandbox, optionally running `command` in it: for dev
        servers, REPLs, watchers and interactive programs. Drive it with terminal_send / terminal_read."""
        return await fleet.terminal_open(sandbox, session, command, cwd)

    @tool()
    async def terminal_send(sandbox: str, session: str = "main", text: str = "", enter: bool = True,
                            keys: list[str] | None = None, wait_ms: int = 1500,
                            wait_for: str | None = None, lines: int = 60) -> str:
        """Type into a terminal session and return its screen afterwards. `keys` sends tmux key names
        (e.g. ["C-c"], ["Up"]); `wait_for` is a regex to wait for (up to wait_ms)."""
        return await fleet.terminal_send(sandbox, session, text, enter, keys, wait_ms, wait_for, lines,
                                         actor=actor())

    @tool()
    async def terminal_read(sandbox: str, session: str = "main", lines: int = 200) -> str:
        """Return the last `lines` lines of a terminal session's scrollback."""
        return await fleet.terminal_read(sandbox, session, lines)

    @tool()
    async def terminal_list(sandbox: str) -> list[str]:
        """List terminal sessions (including agent runs) in a sandbox."""
        return await fleet.terminal_list(sandbox)

    @tool()
    async def terminal_close(sandbox: str, session: str) -> dict[str, str]:
        """Kill a terminal session."""
        await fleet.terminal_kill(sandbox, session)
        return {"closed": session}

    # ------------------------------------------------------------------ agents
    @tool()
    async def run_agent(sandbox: str, prompt: str, harness: str | None = None,
                        cwd: str | None = None, model: str | None = None) -> live.AgentRunOut:
        """Start the sandbox's headless coding agent (claude-code | codex | pi | antigravity) on `prompt`.
        `model` overrides the environment's default model (harness-specific id, e.g. "sonnet", "gpt-5.5").
        Returns a run_id; its terminal session has the same name. Poll with
        agent_status (a client with the MCP tasks extension gets a task that ends with the run). For work you
        want verified and delivered as a PR, submit_job is simpler."""
        # --- deadlines: started on its own task, so a cut-off can't leave an agent running that nobody recorded
        rid = new_id("run")
        task = _detach(fleet.run_agent(sandbox, prompt, harness, cwd, model=model, actor=actor(), run_id=rid),
                       "run_agent")
        run = await _within(task, fleet.settings.mcp_tool_wait_seconds)
        if run is not None:
            return run
        return {"sandbox": fleet.get(sandbox).id, "run_id": rid, "terminal_session": rid, "state": "starting",
                "note": "the sandbox is slow to start the agent; agent_status shows the run once it is up"}

    @tool()
    async def agent_status(sandbox: str, run_id: str, tail_lines: int = 60,
                           wait_seconds: int = 0) -> live.AgentRunOut:
        """State (running/finished), exit code and output tail of a run_agent run. With wait_seconds (up to 25) it
        answers as soon as the run finishes, or after that long: use it instead of polling quickly."""
        return await fleet.agent_status(sandbox, run_id, tail_lines,  # perf: long-poll
                                        wait_seconds=max(0, min(25, wait_seconds)))

    # ------------------------------------------------------------------ clone
    @tool()
    async def snapshot_sandbox(sandbox: str, label: str | None = None, kind: str = "auto") -> dict[str, Any]:
        """Snapshot a sandbox. kind: auto | full (provider-native) | workspace (portable tarball)."""
        # --- deadlines: a workspace snapshot can tar for minutes; it lands in list_snapshots either way
        task = _detach(fleet.snapshot(sandbox, label, kind, actor=actor()), "snapshot_sandbox")
        snap = await _within(task, fleet.settings.mcp_tool_wait_seconds)
        if snap is not None:
            return snap.model_dump(mode="json")
        sid = fleet.get(sandbox).id
        return {"sandbox_id": sid, "state": "running",
                "note": f"still taking the snapshot; list_snapshots(sandbox='{sid}') shows it when done"}

    @tool()
    def list_snapshots(sandbox: str | None = None) -> list[dict[str, Any]]:
        """List snapshots, optionally for one sandbox."""
        return [s.model_dump(mode="json") for s in people.mine(fleet.snapshots(sandbox))]  # --- identity

    @tool()
    async def fork_sandbox(source: str, count: int = 1, name_prefix: str | None = None,
                           work_branch_prefix: str | None = None, credentials: list[str] | None = None,
                           provider: str | None = None, ttl_minutes: int | None = None) -> list[live.SandboxOut]:
        """Clone a live sandbox (snapshotted first) or a snapshot id into `count` new sandboxes that
        start with the same files, installed deps and repo state. Exclusive credentials are not copied."""
        started = time.time()
        task = _detach(fleet.fork(source, count, name_prefix, credentials, work_branch_prefix, provider,
                                  ttl_minutes, actor=actor()), "fork_sandbox")
        sbs = await _within(task, fleet.settings.mcp_tool_wait_seconds)
        if sbs is None:
            return [_sb(s) for s in people.mine(fleet.list(include_stopped=True))  # --- identity
                    if s.created_at >= started and s.parent]
        return [_sb(s) for s in sbs]

    # ------------------------------------------------------------------ sessions (spec 0010)
    if sessions is not None:
        @tool()
        async def new_session(name: str, environment: str | None = None, repo: str | None = None,
                              message: str | None = None, harness: str | None = None, model: str | None = None,
                              base_branch: str | None = None, idle_minutes: int | None = None,
                              budget: dict[str, Any] | None = None,
                              context: list[dict[str, Any]] | None = None,
                              output_schema: dict[str, Any] | None = None,
                              approvals: str | None = None, cache: str | None = None) -> live.SessionOut:
            """Start a resumable stream of work: a sandbox of `environment` on branch hangar/session/<name>; with
            `message` the agent starts at once. Checkpointed after every turn and when idle, so send_to_session
            picks it up days later. `context`, `output_schema`, `budget`: as for submit_job. `approvals`: ask |
            deny-risky | auto. `cache`: auto | 5m | 1h | off. Then wait_for_turn(name)."""
            who = current_actor.get()
            req = NewSession(name=name, environment=environment, repo=repo, message=message, harness=harness,
                             model=model, base_branch=base_branch, idle_minutes=idle_minutes,
                             budget=Budget.model_validate(budget) if budget else None,
                             context=[ContextItem.model_validate(c) for c in context or []],
                             output_schema=output_schema, approvals=approvals, cache=cache)
            return sessions.view(sessions.new(req, owner=who, requester=who, person=identity.person()))  # identity

        @tool()
        async def list_sessions(include_archived: bool = False, limit: int = 30,
                                cursor: str | None = None) -> list[live.SessionOut]:
            """Sessions, last active first: name, state, repo, branch, last summary. More: cursor=the last one's."""
            keep = ("id", "name", "title", "state", "environment", "harness", "repo", "branch", "last_active_at",
                    "last_summary", "sandbox_id", "needs_input")
            # --- identity; --- durable: the caller's own, filtered in the store, a page at a time
            page, _ = sessions.page(include_archived=include_archived, limit=limit, person=people.scope(),
                                    cursor=cursor)
            return [{**{k: v for k, v in sessions.view(s).items() if k in keep}, "cursor": c} for s, c in page]

        @tool()
        async def get_session(session: str) -> live.SessionOut:
            """A session's state, turns (messages and the agent's summaries), checkpoints, sandbox and the command
            to resume its conversation interactively in the sandbox's terminal."""
            return sessions.view(sessions.get(session))

        @tool()
        async def open_session(session: str, harness: str | None = None) -> live.SessionOut:
            """Boot a sandbox for a suspended session and restore its last checkpoint (commits, uncommitted changes,
            untracked files and the agent's conversation). Use it to work in the sandbox yourself; to give the agent
            a task, use send_to_session (it opens the session as needed)."""
            return sessions.view(sessions.open(session, requester=current_actor.get(), harness=harness))

        @tool()
        async def send_to_session(session: str, message: str, context: list[dict[str, Any]] | None = None,
                                  output_schema: dict[str, Any] | None = None,
                                  model: str | None = None) -> live.SessionOut:
            """Continue a session: the agent resumes its conversation in its sandbox (restored if suspended); the
            message answers its question if it asked one. `context`, `output_schema` as for new_session. `model`:
            switch model (one uncached turn). Then wait_for_turn(session)."""
            return sessions.view(sessions.send(session, message, requester=current_actor.get(),
                                               context=[ContextItem.model_validate(c) for c in context or []],
                                               output_schema=output_schema, model=model))

        @tool()
        async def keep_session(session: str, idle_minutes: int) -> live.SessionOut:
            """How long the session may idle before it checkpoints and releases its sandbox (0: until suspended
            by hand). Running turns, open terminals and hangar calls count as activity."""
            return sessions.view(sessions.set_idle(session, idle_minutes, requester=current_actor.get()))

        @tool()
        async def suspend_session(session: str) -> live.SessionOut:
            """Checkpoint a session and release its sandbox now (idle sessions also suspend on their own)."""
            return sessions.view(sessions.suspend(session, requester=current_actor.get()))

        @tool()
        async def fork_session(session: str, n: int = 2, message: str | None = None, harness: str | None = None,
                               names: list[str] | None = None) -> live.ForkOut:
            """Branch a session into `n` sessions from its latest checkpoint: each gets its work, its own branch
            (<branch>-fork-<i>) and sandbox, and the conversation so far. With `message` every fork starts on it;
            else send_to_session each."""
            # --- deadlines: the parent's checkpoint and the copies run on their own task; a cancel half-way would
            # leave forks without their checkpoint
            task = _detach(_fork_session(sessions, session, n, requester=current_actor.get(), harness=harness,
                                         names=names, message=message), "fork_session")
            children = await _within(task, fleet.settings.mcp_tool_wait_seconds)
            parent = sessions.get(session).name
            if children is None:
                return {"parent": parent, "forks": [],
                        "note": f"still forking {parent}; list_sessions shows the forks as they appear"}
            return {"parent": parent, "forks": [sessions.view(c) for c in children]}

    # ------------------------------------------------------------------ context exchange (workstream E, spec 0011)
    if sessions is not None:
        @tool()
        async def wait_for_turn(ref: str, ctx: Context,
                                timeout_seconds: int = 150) -> live.TurnOut | InputRequiredResult:
            """Wait (up to `timeout_seconds`, max 150) for a job's or session's turn (`ref`: job id or session name)
            to end, with progress. Returns `done`, `outcome`, `summary`, `result`, `result_error`, `artifacts`
            (get_artifact) and the agent's `question`, which hangar asks you as an elicitation if it can (else
            answer with follow_up_job or send_to_session). If `done` is false, call it again."""
            return await _wait_for_turn(ref, ctx, timeout_seconds)

        async def _wait_for_turn(ref: str, ctx: Context, timeout_seconds: int) -> CallToolResult | InputRequiredResult:
            """wait_for_turn with the agent's questions put to the user (workstream mcp-modern).

            2026-07-28 clients get the question back as an `input_required` result and retry the call with the
            answer (multi round-trip; the SDK seals `request_state`). Earlier clients get an elicitation request
            on their session, when it has one. Either way the answer goes to the agent as the follow-up of the
            turn that asked, at most once."""

            async def report(progress: float, total: float | None, message: str) -> None:
                await ctx.report_progress(progress, total, message)

            # --- deadlines: timeout_seconds arrives clamped to mcp_tool_wait_seconds (mcp_deadline)
            deadline = time.monotonic() + max(1, min(600, int(timeout_seconds)))
            modern = ctx.protocol_version in MODERN_PROTOCOL_VERSIONS
            note = approved = request = None  # --- computer: request
            skip: list[str] = []  # --- workstream steer: approvals the user declined to answer here
            if ctx.input_responses and ctx.request_state:  # a 2026-07-28 retry carrying the user's answer
                st = json.loads(ctx.request_state)  # minted below; the SDK verified its seal
                resp = ctx.input_responses.get(st.get("key", ""))
                skip = [str(x) for x in st.get("skip") or []][-50:]
                if st.get("ref") == ref and isinstance(resp, ElicitResult) and st.get("env_request"):  # --- computer
                    request = await computer_api.answer_elicited(sessions, str(st["env_request"]), resp,
                                                                 current_actor.get())
                    skip += [str(st["env_request"])]
                elif st.get("ref") == ref and isinstance(resp, ElicitResult) and st.get("approval"):
                    approved = await mcp_steer.answer_elicited(sessions, jobs, ref, str(st["approval"]), resp,
                                                               current_actor.get())
                    skip += [str(st["approval"])] if approved in ("declined", "cancelled", "stale") else []
                elif st.get("ref") == ref and isinstance(resp, ElicitResult):
                    note = mcp_tasks.answer(sessions, jobs, ref, int(st["turn"]), resp, current_actor.get())
            asks = mcp_tasks.supports_form_elicitation(ctx.client_capabilities)

            def approval_waits() -> bool:
                return (mcp_steer.first_pending(sessions, jobs, ref, skip) is not None
                        or computer_api.first_pending(sessions, jobs, ref, skip) is not None)  # --- computer

            closing = fleet.closing  # integrate (#57): on shutdown, answer with the turn as it stands
            while True:
                left = max(1, int(deadline - time.monotonic()))
                turn = await exchange.wait_for_turn(sessions, jobs, ref, left, report,
                                                    until=approval_waits if asks else None)
                if closing.is_set():
                    break
                # --- workstream steer: the agent waits on a tool approval; ask the user
                ap = mcp_steer.first_pending(sessions, jobs, ref, skip) if asks and not turn["done"] else None
                if ap is not None and time.monotonic() < deadline:
                    label, a = ap
                    key = f"approve-{a.id}"
                    if modern:
                        state = json.dumps({"ref": ref, "approval": a.id, "key": key, "skip": skip})
                        return InputRequiredResult(input_requests={key: mcp_steer.approval_request(label, a)},
                                                   request_state=state)
                    try:
                        wait = min(ELICIT_WAIT_SECONDS, max(1.0, (a.expires_at or 0) - time.time()))
                        form = await _elicit(ctx, closing, wait, mcp_steer.message(label, a), mcp_steer.ApprovalForm)
                    except (NoBackChannelError, TimeoutError, ValueError) as e:
                        log.info("could not ask the client about %s's approval: %s", ref, e)
                        asks = False
                        continue
                    approved = await mcp_steer.answer_elicited(sessions, jobs, ref, a.id, ElicitResult(
                        action=form.action, content=form.data.model_dump() if form.action == "accept" else None),
                        current_actor.get())
                    skip += [a.id] if approved in ("declined", "cancelled", "stale") else []
                    continue
                # --- computer: the agent asks its owner for a secret or a host; put it to the person following
                er = computer_api.first_pending(sessions, jobs, ref, skip) if asks else None
                if er is not None and time.monotonic() < deadline:
                    label, r = er
                    key = f"request-{r.id}"
                    if modern:
                        state = json.dumps({"ref": ref, "env_request": r.id, "key": key, "skip": skip})
                        return InputRequiredResult(input_requests={key: computer_api.elicit_request(label, r)},
                                                   request_state=state)
                    try:
                        form = await _elicit(ctx, closing, ELICIT_WAIT_SECONDS, computer_api.message(label, r),
                                             computer_api.RequestForm)
                    except (NoBackChannelError, TimeoutError, ValueError) as e:
                        log.info("could not ask the client about %s's request: %s", ref, e)
                        asks = False
                        continue
                    request = await computer_api.answer_elicited(sessions, r.id, ElicitResult(
                        action=form.action, content=form.data.model_dump() if form.action == "accept" else None),
                        current_actor.get())
                    skip += [r.id]
                    continue
                if not turn["done"] and time.monotonic() < deadline:  # the approval was answered elsewhere
                    continue
                q = mcp_tasks.open_question(sessions, jobs, ref) if turn["done"] else None
                if q is None or note in ("declined", "cancelled") or time.monotonic() >= deadline \
                        or not mcp_tasks.supports_form_elicitation(ctx.client_capabilities):
                    break
                turn_no, question, label = q
                key = f"answer-{turn_no}"
                if modern:
                    state = json.dumps({"ref": ref, "turn": turn_no, "key": key})
                    return InputRequiredResult(input_requests={key: mcp_tasks.question_request(label, question)},
                                               request_state=state)
                try:
                    resp = await _elicit(ctx, closing, ELICIT_WAIT_SECONDS, f"{label} asks: {question}",
                                         mcp_tasks.Answer)
                except (NoBackChannelError, TimeoutError, ValueError) as e:
                    log.info("could not ask the client %s's question: %s", ref, e)
                    break
                note = mcp_tasks.answer(sessions, jobs, ref, turn_no, ElicitResult(
                    action=resp.action, content=resp.data.model_dump() if resp.action == "accept" else None),
                    current_actor.get())
                if note != "answered":
                    break
            out = {**turn, "elicitation": note} if note else turn
            if approved:  # workstream steer
                out = {**out, "approval": approved}
            if request:  # --- computer
                out = {**out, "env_request": request}
            if extra := computer_api.pending_view(sessions, jobs, ref):  # --- computer: answer_approval answers them
                out = {**out, "pending_approvals": [*out.get("pending_approvals", []), *extra]}
            uri = live.job_uri(turn["job"]) if turn.get("job") else live.session_uri(turn.get("session") or ref)
            return mcp_tasks.tool_result(out, out, [uri])

        @tool()
        async def get_artifact(ref: str, turn: int, path: str, max_bytes: int | None = None) -> dict[str, Any]:
            """Read a file the agent handed back (the turn's `artifacts`; `ref`: job id or session name): text
            inline, binary as base64, at most `max_bytes` (512 KB; `truncated` if cut)."""
            s, _ = exchange.resolve(sessions, jobs, ref)
            a, data = await exchange.read_artifact(sessions, s, turn, path)
            cap = sessions.settings.exchange_inline_artifact_kb * 1024
            return exchange.inline(a, data, max(1, min(max_bytes or cap, cap)))

        @tool()
        async def get_session_patch(ref: str, max_bytes: int = 200_000) -> dict[str, Any]:
            """A session's (or job's) work as one patch against its base branch, for `git apply`: live from the
            sandbox, else the last checkpoint. `files` lists what changed; `truncated`: cut at `max_bytes` (the
            whole patch: hangar://sessions/<name>/patch)."""
            s, _ = exchange.resolve(sessions, jobs, ref)
            p = await exchange.session_patch(sessions, s, max_bytes)
            res = mcp_tasks.tool_result(p, None, [])  # workstream mcp-modern: link the full patch
            res.content.append(live.patch_link(s.name, p))
            return res

        @(_noop if drive else mcp.resource(  # --- #140: resources stay on the server whose tools they serve
            "hangar://sessions/{name}/artifacts/{turn}/{+path}", name="session-artifact",
            description="A file an agent handed back in a session turn (see get_session)."))
        async def artifact_resource(name: str, turn: str, path: str) -> str | bytes:
            if not turn.isdigit():
                raise ValueError("turn must be a number")
            guard.check("session", name)  # --- identity
            a, data = await exchange.read_artifact(sessions, sessions.get(name), int(turn), path)
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return data

        mcp_steer.register(mcp, tool, sessions, jobs, current_actor.get)  # workstream steer: steer_turn, approvals
        if sessions.env_requests is None:  # --- computer: a server built without the app (tests, the CLI)
            EnvRequests(fleet, sessions, jobs)

        # --- workstream mcp-modern: jobs and sessions as resources clients can follow, and workflow prompts
        # --- #140: on `hangar` only: they serve its tools
        if not drive:
            _work_resources(mcp, fleet, sessions, jobs, guard, feed, legacy_sessions)

    # --- stream: background processes, file watches, events to subscribers as they are recorded
    register_streams(mcp, tool, fleet, sessions, jobs, feed, current_actor.get)

    # ------------------------------------------------------------------ jobs (spec 0009)
    if jobs is None:
        return _identify(mcp, fleet)

    @tool()
    async def submit_job(instructions: str, repo: str | None = None, environment: str | None = None,
                   title: str | None = None, harness: str | None = None, model: str | None = None,
                   base_branch: str | None = None, verify: str | None = None, deliver: str = "draft_pr",
                   context: list[dict[str, Any]] | None = None, timeout_minutes: int | None = None,
                   callback_url: str | None = None, budget: dict[str, Any] | None = None,
                   output_schema: dict[str, Any] | None = None, strategy: str = "single", n: int | None = None,
                   judge: bool = False, verify_in_loop: bool | None = None,
                   autofix: str | None = None, harness_lock: str | None = None) -> live.JobOut:
        """Hand development work to a cloud agent. Hangar starts a sandbox of `environment` (default: the repo's),
        runs the agent, then `verify` (e.g. "pytest -q"), pushes hangar/<job> and opens a PR (`deliver`: draft_pr |
        pr | branch | none). `context` items: {"name", "content", "kind": "log"}, {"kind": "url", "url"}, your
        local work {"kind": "patch"} or {"kind": "bundle", "base_commit"}. `output_schema`: JSON Schema for
        result.data. `budget` caps each turn ({"max_minutes", "max_tokens", "max_cost_usd"}); over it, the work
        is delivered as a draft. Then wait_for_turn(job id).
        `strategy="best_of_n"`, `n` 2-8: parallel attempts, the best delivered (`judge=True`: an agent compares).
        `verify_in_loop` (default on): failures go back to the agent in the turn. `autofix` (ci,reviews | ci |
        reviews | off): PR CI failures and trusted reviews start follow-ups. `harness_lock`: name@rN."""
        req = SubmitJob(instructions=instructions, repo=repo, environment=environment, title=title, harness=harness,
                        model=model, base_branch=base_branch, verify=verify, deliver=deliver,
                        context=[ContextItem.model_validate(c) for c in context or []],
                        timeout_minutes=timeout_minutes, callback_url=callback_url, output_schema=output_schema,
                        budget=Budget.model_validate(budget) if budget else None, strategy=strategy, n=n,
                        judge=judge, verify_in_loop=verify_in_loop, autofix=autofix,  # --- overnight/autofix
                        harness_lock=harness_lock)  # --- harness locks (#134)
        who = current_actor.get()
        return jobs.view(jobs.submit(req, Caller(who, who, person=identity.person()), source="mcp"))  # identity

    @tool()
    async def get_job(job: str) -> live.JobOut:
        """Check on a job: state, timeline, result (commits, PR, verification, the agent's summary), attempts,
        usage, and its sandbox and terminal session for watching or taking over. To wait for it, wait_for_turn."""
        return jobs.view(jobs.get(job))

    @tool()
    async def list_jobs(state: str | None = None, limit: int = 20, cursor: str | None = None) -> list[live.JobOut]:
        """Jobs, newest first; state: queued|running|needs_input|succeeded|failed... More: cursor=the last one's."""
        page, _ = jobs.page(state=state, limit=limit, person=people.scope(), cursor=cursor)  # identity
        return [{**{k: v for k, v in jobs.view(j).items() if k not in ("events", "instructions", "turns")},
                 "cursor": c} for j, c in page]

    @tool()
    async def follow_up_job(job: str, message: str, context: list[dict[str, Any]] | None = None,
                            output_schema: dict[str, Any] | None = None) -> live.JobOut:
        """Continue a finished job, or answer one that needs input: the agent takes `message` in its conversation
        on the same branch, then hangar verifies and delivers again (the PR updates). `context`, `output_schema`
        as for submit_job."""
        return jobs.view(jobs.follow_up(job, message, requester=current_actor.get(),
                                        context=[ContextItem.model_validate(c) for c in context or []],
                                        output_schema=output_schema))

    @tool()
    async def cancel_job(job: str) -> live.JobOut:
        """Stop a queued or running job (the agent is killed; work committed so far stays in the sandbox)."""
        return jobs.view(jobs.cancel(job, requester=current_actor.get()))

    @tool()
    async def get_events(job: str | None = None, session: str | None = None, since: int = 0,
                         limit: int = 100) -> live.EventsOut:
        """A job's or session's agent events, oldest first, one shape for every harness (message, tool_call,
        tool_result, command, file_change, error, usage, progress). Poll with `since` = the returned `next`, or
        subscribe to hangar://sessions/<name>/events."""
        if bool(job) == bool(session):
            raise ValueError("pass exactly one of job or session")
        if job:
            evs = jobs.events(job, since=since, limit=limit)
        elif sessions is not None:
            evs = sessions.events(session or "", since=since, limit=limit)
        else:
            raise ValueError("sessions are not enabled")
        return {"events": evs, "next": evs[-1]["seq"] if evs else since}

    # ------------------------------------------------------------------ triggers (workstream A)
    def _trigger_tools() -> None:
        """Registered only when triggers are enabled; a function rather than an early return, so tools added
        after this section still register."""

        # --- approvals (#121): who may decide is approvals.can_decide's, the same rule as REST's twins
        @tool(rule="approvals.can_decide")
        async def approve_job(job: str) -> live.JobOut:
            """Let a job in state awaiting_approval run; read it with get_job first. Admins and the repo's policy
            approvers only, with a login token, never on their own job."""
            return jobs.view(await approvals.decide(jobs, job, approvals.from_principal(identity.current.get()),
                                                    "approve"))

        @tool(rule="approvals.can_decide")
        async def reject_job(job: str) -> live.JobOut:
            """Drop a job that is waiting for approval; it is cancelled without running."""
            return jobs.view(await approvals.decide(jobs, job, approvals.from_principal(identity.current.get()),
                                                    "reject"))

        @tool()
        async def create_schedule(name: str, cron: str, instructions: str, repo: str | None = None,
                                  environment: str | None = None, timezone: str = "UTC", title: str | None = None,
                                  harness: str | None = None, model: str | None = None, verify: str | None = None,
                                  deliver: str = "draft_pr", context: list[dict[str, Any]] | None = None,
                                  allow_overlap: bool = False, enabled: bool = True) -> dict[str, Any]:
            """Submit the same job on a schedule: `cron` (5 fields, e.g. "0 9 * * 1-5") in `timezone` (IANA). Job
            fields as for submit_job. A run is skipped while the last one runs unless allow_overlap."""
            job = {k: v for k, v in dict(instructions=instructions, repo=repo, environment=environment, title=title,
                                         harness=harness, model=model, verify=verify, deliver=deliver,
                                         context=context).items() if v is not None}
            who = current_actor.get()
            spec = ScheduleSpec(name=name, cron=cron, timezone=timezone, job=job, allow_overlap=allow_overlap,
                                enabled=enabled)
            return triggers.view_schedule(triggers.create_schedule(spec, owner=who, requester=who,
                                                                   person=identity.person()))  # --- identity

        @tool()
        async def list_schedules() -> list[dict[str, Any]]:
            """Scheduled jobs with their next and last run, and the last job each one started."""
            return [triggers.view_schedule(s) for s in people.mine(triggers.list_schedules())]  # --- identity

        @tool()
        async def delete_schedule(schedule: str) -> dict[str, Any]:
            """Delete a schedule by id or name. Jobs it already started are not affected."""
            return {"deleted": triggers.delete_schedule(schedule, requester=current_actor.get()).id}

        @tool()
        async def run_schedule_now(schedule: str) -> live.JobOut:
            """Submit a schedule's job right away (its regular runs are unaffected). Returns the job."""
            return jobs.view(triggers.run_now(schedule, requester=current_actor.get()))

    if triggers is not None:
        _trigger_tools()

    return _identify(mcp, fleet)


def _noop(fn):
    return fn


def _work_resources(mcp: live.HangarMCP, fleet: Fleet, sessions: SessionService, jobs: JobService | None,
                    guard: Any, feed: Any, legacy_sessions: bool) -> None:
    """Jobs and sessions as resources clients can follow, the workflow prompts and change notifications: on the
    `hangar` server, whose tools they serve (#140)."""
    live.register_resources(mcp, sessions, jobs, guard)  # --- identity: guard
    from hangar import inbox  # --- landing: hangar://inbox, what needs the caller (a resource, not a tool)

    inbox.register(mcp, jobs)
    from hangar import flightrec, opstatus  # --- clouddoctor: hangar://status (admins), job flight records

    opstatus.register(mcp, fleet, sessions, jobs)
    flightrec.register(mcp, jobs, guard)
    from hangar import experiments, manifest  # --- interfaces: manifests, traces, experiments as resources

    manifest.register(mcp, sessions, jobs, guard)
    experiments.register(mcp, jobs, guard)
    from hangar import testresults  # --- testresults: hangar://jobs/<id>/tests (a resource, not a tool)

    testresults.register(mcp, jobs, guard)
    mcp.recent = lambda: live.recent_resources(sessions, jobs)
    live.register_prompts(mcp, sessions)
    fleet.store.on_put.append(feed.on_put)
    if legacy_sessions:
        feed.install_legacy(mcp)


def _identify(mcp: live.HangarMCP, fleet: Fleet) -> live.HangarMCP:
    """--- deadlines: once every tool is registered, serverInfo.version names the package and the tool surface
    (`0.1.0+tools.<hash>`), and calls from clients on an older surface get told to refresh (mcp_fresh). --- #140:
    each server hashes its own listing; `hangar` notes calls of tools that moved, and `hangar-sandbox` says where a
    work tool is."""
    drive = mcp.name == live.SANDBOX
    if drive:
        names = {t.name for t in mcp._tool_manager.list_tools()}  # noqa: SLF001
        retired = {k: v for k, v in fresh.RETIRED.items() if k in names}
        mcp.fresh = fresh.Freshness(fleet.store, mcp.listing(), retired, current_actor.get, ns="sandbox",
                                    elsewhere={n: live.WORK for n in WORK_TOOL_NAMES})
    else:
        retired = {k: v for k, v in fresh.RETIRED.items() if k not in mcp.moved}
        mcp.fresh = fresh.Freshness(fleet.store, mcp.listing(), retired, current_actor.get,
                                    moved={n: live.SANDBOX for n in mcp.moved})
    mcp._lowlevel_server.version = mcp.fresh.version  # noqa: SLF001 - the SDK takes it only in the constructor
    return mcp
