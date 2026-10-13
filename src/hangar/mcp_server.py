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

import base64
import contextvars
import functools
import inspect
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hangar import exchange
from hangar.broker import CredentialError
from hangar.fleet import Fleet, FleetError
from hangar.jobs import Caller, JobService, SubmitJob
from hangar.models import Budget, ContextItem, Sandbox
from hangar.providers.base import ProviderError
from hangar.sessions import NewSession, SessionService
from hangar.triggers import ScheduleSpec, Triggers

# Failures an agent can act on (bad name, quota, build error...). Anything else is a crash and stays generic.
EXPECTED = (FleetError, ProviderError, CredentialError, ValueError, KeyError, NotImplementedError)

current_actor: contextvars.ContextVar[str] = contextvars.ContextVar("hangar_actor", default="mcp")

INSTRUCTIONS = """\
Hangar manages a fleet of isolated coding sandboxes (containers or microVMs) built from
named environments that bundle tools, agent harnesses, dependencies, skills and a repo.

Typical flow:
1. list_environments -> pick one (or build_environment if it has no image yet).
2. create_sandbox(environment, repo=..., work_branch=...) -> sandbox id.
3. exec(...) for one-shot commands; terminal_open/terminal_send/terminal_read for
   long-running or interactive processes; read_file/write_file for files.
4. run_agent(sandbox, prompt) to hand a task to a coding agent inside the sandbox, then
   agent_status(...) to poll it.
5. fork_sandbox(sandbox, count=N) to clone a prepared sandbox for parallel attempts.
6. destroy_sandbox when done (sandboxes also expire after their TTL).
Relative paths resolve against the sandbox's repo checkout (or /workspace).

For an ongoing stream of work you'll come back to, use sessions: new_session(name, environment, message)
then send_to_session(name, message) later, even days later; the agent resumes its own conversation and the
repo state (commits, uncommitted changes) is restored. list_sessions shows what you can pick up.

Or hand off whole pieces of work: submit_job(instructions, repo=..., context=[...]) runs an agent in
a fresh sandbox, verifies, pushes a branch and opens a draft PR on its own. Check it with get_job,
continue it with follow_up_job, stop it with cancel_job.

Context in and out (jobs and sessions alike): `context` items can carry files, your uncommitted changes
({"kind": "patch", "content": <git diff --binary>}) and unpushed commits ({"kind": "bundle", "content":
<base64 git bundle>, "encoding": "base64", "base_commit": <sha on the remote>}). `output_schema` (JSON
Schema) asks the agent for a structured result. wait_for_turn(job or session) blocks with progress
notifications and returns the result, the agent's question (state needs_input: answer it with
follow_up_job / send_to_session) and its artifacts (get_artifact). get_session_patch gives the code.
"""


def _sb(s: Sandbox) -> dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "state": s.state.value, "environment": s.environment, "provider": s.provider,
        "repo": s.repo, "branch": s.info.get("work_branch") or s.branch, "repo_path": s.info.get("repo_path"),
        "harness": s.info.get("harness"), "credentials": s.credentials, "labels": s.labels, "parent": s.parent,
        "expires_at": s.expires_at, "error": s.error,
    }


def build_mcp(fleet: Fleet, jobs: JobService | None = None, sessions: SessionService | None = None,
              triggers: Triggers | None = None) -> MCPServer:
    mcp = MCPServer("hangar", instructions=INSTRUCTIONS, version="0.1.0")

    def tool():
        """mcp.tool() that reports expected failures to the client instead of a generic error."""

        def deco(fn):
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def wrapped(*a, **kw):
                    try:
                        return await fn(*a, **kw)
                    except EXPECTED as e:
                        raise ToolError(str(e).strip("'\"")) from e
            else:
                @functools.wraps(fn)
                def wrapped(*a, **kw):
                    try:
                        return fn(*a, **kw)
                    except EXPECTED as e:
                        raise ToolError(str(e).strip("'\"")) from e
            return mcp.tool()(wrapped)

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
        """Build (or rebuild with force=True) an environment's image and warm pool. Can take minutes."""
        job = await fleet.build(name, provider, force=force, actor=actor())
        return job.summary()

    @tool()
    def validate_environment(spec: dict[str, Any]) -> dict[str, Any]:
        """Check an environment spec without saving it: resolves `extends`, validates fields, lists warnings
        (missing credentials, deprecated harnesses) and returns the Dockerfile it would build.

        Spec fields: name, description, extends, base | image (prebuilt), tools {apt, mise, script},
        harnesses [claude-code, codex, pi, antigravity, git], default_harness, harness_mode (auto|pack|bake),
        model, dependencies {pip, npm, script}, skills [{path|git, name}], agent_config {instructions, files},
        mcp {name: {url, headers} | {command, args, env}}, secrets {ENV_VAR: credential}, env, repo {url,
        branch, setup, bake}, credentials, resources {cpus, memory_gb}, warm_pool, runtime_class."""
        return fleet.validate_environment(spec)

    @tool()
    def create_environment(spec: dict[str, Any]) -> dict[str, Any]:
        """Save a new or updated environment (same fields as validate_environment). Usually `extends` a
        default (bare, python, node, go, polyglot) or a team environment. Build it with build_environment."""
        s = fleet.save_environment(spec, actor=actor())
        return {"name": s.name, "harnesses": s.harnesses, "default_harness": s.pick_harness()}

    @tool()
    def clone_environment(source: str, name: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create environment `name` that extends `source`, applying `overrides` (lists and maps merge with
        the source's, scalars replace). E.g. overrides={"harnesses": ["codex"], "secrets": {...}}."""
        s = fleet.clone_environment(source, name, overrides, actor=actor())
        return {"name": s.name, "extends": source, "harnesses": s.harnesses, "default_harness": s.pick_harness()}

    @tool()
    def list_credentials() -> list[dict[str, Any]]:
        """Credentials available to attach to sandboxes (names and metadata only, never secrets)."""
        return [c.model_dump(include={"name", "harness", "kind", "exclusive", "leased_to", "description"})
                for c in fleet.broker.list()]

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
    ) -> dict[str, Any]:
        """Start a sandbox from an environment. Optionally clone `repo` (at `branch`) and check out a
        new `work_branch`. `harness` picks which of the environment's agents this sandbox is for (default:
        the environment's default); only that agent's credentials, config and MCP servers are applied.
        `credentials` overrides the environment's default credential list."""
        sb = await fleet.create(environment, name=name, repo=repo, branch=branch, work_branch=work_branch,
                                credentials=credentials, provider=provider, ttl_minutes=ttl_minutes,
                                labels=labels, harness=harness, actor=actor())
        return _sb(sb)

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
        """Start `count` (1-20) identical sandboxes concurrently, each on its own work branch."""
        import asyncio

        if not 1 <= count <= 20:
            raise ValueError("count must be 1..20")
        prefix = name_prefix or environment

        async def one(i: int):
            return await fleet.create(
                environment, name=f"{prefix}-{i + 1}", repo=repo, branch=branch,
                work_branch=f"{work_branch_prefix}-{i + 1}" if work_branch_prefix else None,
                credentials=credentials, provider=provider, ttl_minutes=ttl_minutes, labels=labels,
                harness=harness, actor=actor())

        res = await asyncio.gather(*(one(i) for i in range(count)), return_exceptions=True)
        return [_sb(r) if isinstance(r, Sandbox) else {"error": str(r)} for r in res]

    @tool()
    def list_sandboxes(include_stopped: bool = False, label: str | None = None) -> list[dict[str, Any]]:
        """List sandboxes. `label` filters by key or key=value."""
        return [_sb(s) for s in fleet.list(include_stopped=include_stopped, label=label)]

    @tool()
    def get_sandbox(sandbox: str) -> dict[str, Any]:
        """Details for one sandbox, including recent agent runs."""
        s = fleet.get(sandbox)
        return {**_sb(s), "runs": s.info.get("runs", [])[-10:], "created_at": s.created_at,
                "last_activity": s.last_activity}

    @tool()
    async def destroy_sandbox(sandbox: str) -> dict[str, Any]:
        """Stop and delete a sandbox. Exclusive credentials are written back and released."""
        return _sb(await fleet.destroy(sandbox, actor=actor()))

    @tool()
    async def destroy_sandboxes(sandboxes: list[str] | None = None, label: str | None = None) -> list[str]:
        """Destroy several sandboxes by id/name, or every active sandbox matching `label`."""
        if not sandboxes and not label:
            raise ValueError("pass sandboxes or label (refusing to destroy the whole fleet implicitly)")
        return await fleet.destroy_many(sandboxes, label=label, actor=actor())

    @tool()
    async def pause_sandbox(sandbox: str) -> dict[str, Any]:
        """Freeze a sandbox's processes to free CPU (state is kept)."""
        return _sb(await fleet.pause(sandbox, actor=actor()))

    @tool()
    async def resume_sandbox(sandbox: str) -> dict[str, Any]:
        """Resume a paused sandbox."""
        return _sb(await fleet.resume(sandbox, actor=actor()))

    @tool()
    async def extend_sandbox(sandbox: str, minutes: int = 60) -> dict[str, Any]:
        """Push back a sandbox's expiry."""
        return _sb(await fleet.extend(sandbox, minutes, actor=actor()))

    # ------------------------------------------------------------------ commands & files
    @tool()
    async def exec(sandbox: str, command: str, cwd: str | None = None, timeout: int = 120) -> dict[str, Any]:
        """Run a shell command (bash -lc) and wait for it. Returns exit_code, stdout, stderr."""
        r = await fleet.exec(sandbox, command, cwd=cwd, timeout=timeout, actor=actor())
        return r.model_dump()

    @tool()
    async def read_file(sandbox: str, path: str, binary: bool = False) -> dict[str, Any]:
        """Read a file. Text by default; binary=True returns base64."""
        data = await fleet.read_file(sandbox, path)
        if data is None:
            return {"path": path, "exists": False}
        if binary:
            return {"path": path, "exists": True, "base64": base64.b64encode(data).decode()}
        return {"path": path, "exists": True, "content": data.decode(errors="replace")}

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
        """Open (or reuse) a persistent terminal session, optionally running `command` in it."""
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
                        cwd: str | None = None, model: str | None = None) -> dict[str, Any]:
        """Start the sandbox's headless coding agent (claude-code | codex | pi | antigravity) on `prompt`.
        `model` overrides the environment's default model (harness-specific id, e.g. "sonnet", "gpt-5.5",
        "openrouter/qwen/qwen3-coder"). Returns a run_id; its terminal session has the same name. Poll with
        agent_status."""
        return await fleet.run_agent(sandbox, prompt, harness, cwd, model=model, actor=actor())

    @tool()
    async def agent_status(sandbox: str, run_id: str, tail_lines: int = 60) -> dict[str, Any]:
        """State (running/finished), exit code and output tail of an agent run."""
        return await fleet.agent_status(sandbox, run_id, tail_lines)

    # ------------------------------------------------------------------ clone
    @tool()
    async def snapshot_sandbox(sandbox: str, label: str | None = None, kind: str = "auto") -> dict[str, Any]:
        """Snapshot a sandbox. kind: auto | full (provider-native) | workspace (portable tarball)."""
        return (await fleet.snapshot(sandbox, label, kind, actor=actor())).model_dump(mode="json")

    @tool()
    def list_snapshots(sandbox: str | None = None) -> list[dict[str, Any]]:
        """List snapshots, optionally for one sandbox."""
        return [s.model_dump(mode="json") for s in fleet.snapshots(sandbox)]

    @tool()
    async def fork_sandbox(source: str, count: int = 1, name_prefix: str | None = None,
                           work_branch_prefix: str | None = None, credentials: list[str] | None = None,
                           provider: str | None = None, ttl_minutes: int | None = None) -> list[dict[str, Any]]:
        """Clone a live sandbox (snapshotted first) or a snapshot id into `count` new sandboxes that
        start with the same files, installed deps and repo state. Exclusive credentials are not copied."""
        sbs = await fleet.fork(source, count, name_prefix, credentials, work_branch_prefix, provider,
                               ttl_minutes, actor=actor())
        return [_sb(s) for s in sbs]

    # ------------------------------------------------------------------ sessions (spec 0010)
    if sessions is not None:
        @tool()
        async def new_session(name: str, environment: str | None = None, repo: str | None = None,
                              message: str | None = None, harness: str | None = None, model: str | None = None,
                              base_branch: str | None = None, idle_minutes: int | None = None,
                              budget: dict[str, Any] | None = None,
                              context: list[dict[str, Any]] | None = None,
                              output_schema: dict[str, Any] | None = None) -> dict[str, Any]:
            """Start a resumable stream of work (like a cloud Claude session): a sandbox of `environment` on branch
            hangar/session/<name>. With `message`, the agent starts on it right away. The conversation and repo
            state are checkpointed after every turn and when the session idles, so it can be picked up days later
            with send_to_session or open_session. `context`: attachments for the first turn, including your local
            work ({"kind": "patch"} uncommitted changes, {"kind": "bundle", "base_commit": ...} unpushed commits).
            `output_schema`: JSON Schema for a structured result. `budget` caps each agent turn:
            {"max_minutes": 30, "max_tokens": 2000000, "max_cost_usd": 5}. Then call wait_for_turn(name)."""
            who = current_actor.get()
            req = NewSession(name=name, environment=environment, repo=repo, message=message, harness=harness,
                             model=model, base_branch=base_branch, idle_minutes=idle_minutes,
                             budget=Budget.model_validate(budget) if budget else None,
                             context=[ContextItem.model_validate(c) for c in context or []],
                             output_schema=output_schema)
            return sessions.view(sessions.new(req, owner=who, requester=who))

        @tool()
        async def list_sessions(include_archived: bool = False, limit: int = 30) -> list[dict[str, Any]]:
            """Streams of work, most recently active first: name, state (idle | running | suspended ...), repo,
            branch and the agent's last summary."""
            keep = ("id", "name", "title", "state", "environment", "harness", "repo", "branch", "last_active_at",
                    "last_summary", "sandbox_id")
            return [{k: v for k, v in sessions.view(s).items() if k in keep}
                    for s in sessions.list(include_archived=include_archived, limit=limit)]

        @tool()
        async def get_session(session: str) -> dict[str, Any]:
            """A session's state, turns (messages and the agent's summaries), checkpoints, sandbox and the command
            to resume its conversation interactively in the sandbox's terminal."""
            return sessions.view(sessions.get(session))

        @tool()
        async def open_session(session: str, harness: str | None = None) -> dict[str, Any]:
            """Boot a sandbox for a suspended session and restore its last checkpoint (commits, uncommitted changes,
            untracked files and the agent's conversation). Use it to work in the sandbox yourself; to give the agent
            a task, use send_to_session (it opens the session as needed)."""
            return sessions.view(sessions.open(session, requester=current_actor.get(), harness=harness))

        @tool()
        async def send_to_session(session: str, message: str, context: list[dict[str, Any]] | None = None,
                                  output_schema: dict[str, Any] | None = None) -> dict[str, Any]:
            """Continue a session: the agent resumes its own conversation (full earlier context) in the session's
            sandbox, booting and restoring it first if the session is suspended. If the agent asked a question
            (needs_input), the message answers it. `context` and `output_schema` as for new_session. Then call
            wait_for_turn(session)."""
            return sessions.view(sessions.send(session, message, requester=current_actor.get(),
                                               context=[ContextItem.model_validate(c) for c in context or []],
                                               output_schema=output_schema))

        @tool()
        async def keep_session(session: str, idle_minutes: int) -> dict[str, Any]:
            """How long the session may sit idle before it checkpoints and releases its sandbox (0 = stay up
            until suspended by hand). Work through hangar, open terminals and running turns all count as
            activity, so a session you're using never suspends under you."""
            return sessions.view(sessions.set_idle(session, idle_minutes, requester=current_actor.get()))

        @tool()
        async def suspend_session(session: str) -> dict[str, Any]:
            """Checkpoint a session and release its sandbox now (idle sessions also suspend on their own)."""
            return sessions.view(sessions.suspend(session, requester=current_actor.get()))

    # ------------------------------------------------------------------ context exchange (workstream E, spec 0011)
    if sessions is not None:
        @tool()
        async def wait_for_turn(ref: str, ctx: Context, timeout_seconds: int = 300) -> dict[str, Any]:
            """Wait (up to `timeout_seconds`, max 600) for a job's or session's current agent turn to end, with
            progress notifications while it runs. Returns the turn: `done`, `outcome`, `summary`, `result` (the
            structured result), `result_error`, `question` (the agent needs input: answer with follow_up_job or
            send_to_session), `artifacts` (read with get_artifact). `ref`: a job id or a session name. If `done`
            is false, call it again."""

            async def report(progress: float, total: float | None, message: str) -> None:
                await ctx.report_progress(progress, total, message)

            return await exchange.wait_for_turn(sessions, jobs, ref, timeout_seconds, report)

        @tool()
        async def get_artifact(ref: str, turn: int, path: str, max_bytes: int | None = None) -> dict[str, Any]:
            """Read a file the agent handed back (listed on the turn's `artifacts`). `ref`: a job id or a session
            name. Text comes back inline, binary as base64, either capped at `max_bytes` (default and max 512 KB;
            `truncated` says if it was cut). The REST API serves whole files."""
            s, _ = exchange.resolve(sessions, jobs, ref)
            a, data = await exchange.read_artifact(sessions, s, turn, path)
            cap = sessions.settings.exchange_inline_artifact_kb * 1024
            return exchange.inline(a, data, max(1, min(max_bytes or cap, cap)))

        @tool()
        async def get_session_patch(ref: str, max_bytes: int = 200_000) -> dict[str, Any]:
            """The session's (or job's) work as one patch against its base branch: commits, uncommitted changes
            and new files, ready for `git apply`. Live from the sandbox if it is up, else from the last checkpoint.
            `files` lists what changed; `truncated` says if the patch was cut at `max_bytes`."""
            s, _ = exchange.resolve(sessions, jobs, ref)
            return await exchange.session_patch(sessions, s, max_bytes)

        @mcp.resource("hangar://sessions/{name}/artifacts/{turn}/{+path}", name="session-artifact",
                      description="A file an agent handed back in a session turn (see get_session).")
        async def artifact_resource(name: str, turn: str, path: str) -> str | bytes:
            if not turn.isdigit():
                raise ValueError("turn must be a number")
            a, data = await exchange.read_artifact(sessions, sessions.get(name), int(turn), path)
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return data

    # ------------------------------------------------------------------ jobs (spec 0009)
    if jobs is None:
        return mcp

    @tool()
    async def submit_job(instructions: str, repo: str | None = None, environment: str | None = None,
                   title: str | None = None, harness: str | None = None, model: str | None = None,
                   base_branch: str | None = None, verify: str | None = None, deliver: str = "draft_pr",
                   context: list[dict[str, Any]] | None = None, timeout_minutes: int | None = None,
                   callback_url: str | None = None, budget: dict[str, Any] | None = None,
                   output_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        """Hand a piece of development work to a cloud agent and get on with other things.

        Hangar queues the job, starts a sandbox from `environment` (default: the repo's mapped one), writes
        `context` next to the repo, runs the agent, runs `verify` (the repo's own check, e.g. "pytest -q"),
        commits anything left, pushes branch hangar/<job> and opens a PR (`deliver`: draft_pr | pr | branch |
        none). `context` items: {"name": "trace.txt", "content": "...", "kind": "log"},
        {"name": "spec", "kind": "url", "url": "https://..."}, or your local work: {"kind": "patch"} and
        {"kind": "bundle", "base_commit": ...}. `output_schema`: JSON Schema for a structured result
        (get_job's result.data). `budget` caps each agent turn ({"max_minutes": 30, "max_tokens": 2000000,
        "max_cost_usd": 5}); a turn that crosses it is stopped and what it did is delivered as a draft.
        Returns the job; then wait_for_turn(job id), or poll get_job (it shows usage) or get_events."""
        req = SubmitJob(instructions=instructions, repo=repo, environment=environment, title=title, harness=harness,
                        model=model, base_branch=base_branch, verify=verify, deliver=deliver,
                        context=[ContextItem.model_validate(c) for c in context or []],
                        timeout_minutes=timeout_minutes, callback_url=callback_url, output_schema=output_schema,
                        budget=Budget.model_validate(budget) if budget else None)
        who = current_actor.get()
        return jobs.view(jobs.submit(req, Caller(who, who), source="mcp"))

    @tool()
    async def get_job(job: str) -> dict[str, Any]:
        """A job's state, timeline, result (commits, PR, verification, the agent's summary) and its sandbox
        and terminal session for watching or taking over."""
        return jobs.view(jobs.get(job))

    @tool()
    async def list_jobs(state: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Recent jobs, newest first (state: queued | provisioning | running | verifying | delivering |
        needs_input | succeeded | failed | cancelled)."""
        return [{k: v for k, v in jobs.view(j).items() if k not in ("events", "instructions", "turns")}
                for j in jobs.list(state=state, limit=limit)]

    @tool()
    async def follow_up_job(job: str, message: str, context: list[dict[str, Any]] | None = None,
                            output_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        """Continue a finished job, or answer a job that needs input: the agent picks up `message` in its own
        conversation on the same branch, then hangar verifies and delivers again (the PR updates). `context` and
        `output_schema` as for submit_job (the job's schema applies when omitted)."""
        return jobs.view(jobs.follow_up(job, message, requester=current_actor.get(),
                                        context=[ContextItem.model_validate(c) for c in context or []],
                                        output_schema=output_schema))

    @tool()
    async def cancel_job(job: str) -> dict[str, Any]:
        """Stop a queued or running job (the agent is killed; work committed so far stays in the sandbox)."""
        return jobs.view(jobs.cancel(job, requester=current_actor.get()))

    @tool()
    async def get_events(job: str | None = None, session: str | None = None, since: int = 0,
                         limit: int = 100) -> dict[str, Any]:
        """What the agent is doing, as one event stream for every harness: message, tool_call, tool_result,
        command, file_change, error, usage and progress events, oldest first. Pass a job or a session; poll with
        `since` = the returned `next` to get only new events."""
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

        @tool()
        async def approve_job(job: str) -> dict[str, Any]:
            """Let a job in state awaiting_approval run. Such jobs were started by someone outside the trusted list
            (e.g. a non-collaborator's /hangar comment); read its instructions and context with get_job first."""
            return jobs.view(triggers.approve(job, approver=current_actor.get()))

        @tool()
        async def reject_job(job: str) -> dict[str, Any]:
            """Drop a job that is waiting for approval; it is cancelled without running."""
            return jobs.view(triggers.reject(job, approver=current_actor.get()))

        @tool()
        async def create_schedule(name: str, cron: str, instructions: str, repo: str | None = None,
                                  environment: str | None = None, timezone: str = "UTC", title: str | None = None,
                                  harness: str | None = None, model: str | None = None, verify: str | None = None,
                                  deliver: str = "draft_pr", context: list[dict[str, Any]] | None = None,
                                  allow_overlap: bool = False, enabled: bool = True) -> dict[str, Any]:
            """Submit the same job on a schedule. `cron` has 5 fields (minute hour day-of-month month day-of-week,
            e.g. "0 9 * * 1-5" for 9:00 on weekdays) in `timezone` (IANA, e.g. "Europe/Berlin"). The job fields are
            those of submit_job. A run is skipped while the previous one is still going unless allow_overlap."""
            job = {k: v for k, v in dict(instructions=instructions, repo=repo, environment=environment, title=title,
                                         harness=harness, model=model, verify=verify, deliver=deliver,
                                         context=context).items() if v is not None}
            who = current_actor.get()
            spec = ScheduleSpec(name=name, cron=cron, timezone=timezone, job=job, allow_overlap=allow_overlap,
                                enabled=enabled)
            return triggers.view_schedule(triggers.create_schedule(spec, owner=who, requester=who))

        @tool()
        async def list_schedules() -> list[dict[str, Any]]:
            """Scheduled jobs with their next and last run, and the last job each one started."""
            return [triggers.view_schedule(s) for s in triggers.list_schedules()]

        @tool()
        async def delete_schedule(schedule: str) -> dict[str, Any]:
            """Delete a schedule by id or name. Jobs it already started are not affected."""
            return {"deleted": triggers.delete_schedule(schedule, requester=current_actor.get()).id}

        @tool()
        async def run_schedule_now(schedule: str) -> dict[str, Any]:
            """Submit a schedule's job right away (its regular runs are unaffected). Returns the job."""
            return jobs.view(triggers.run_now(schedule, requester=current_actor.get()))

    if triggers is not None:
        _trigger_tools()

    return mcp
