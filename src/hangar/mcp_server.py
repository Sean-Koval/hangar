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

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hangar.broker import CredentialError
from hangar.fleet import Fleet, FleetError
from hangar.models import Sandbox
from hangar.providers.base import ProviderError

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
"""


def _sb(s: Sandbox) -> dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "state": s.state.value, "environment": s.environment, "provider": s.provider,
        "repo": s.repo, "branch": s.info.get("work_branch") or s.branch, "repo_path": s.info.get("repo_path"),
        "harness": s.info.get("harness"), "credentials": s.credentials, "labels": s.labels, "parent": s.parent,
        "expires_at": s.expires_at, "error": s.error,
    }


def build_mcp(fleet: Fleet) -> MCPServer:
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

    return mcp
