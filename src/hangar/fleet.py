"""Fleet manager: the single source of truth for sandbox lifecycle.

The MCP server, REST API, dashboard and CLI are thin layers over this class.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import shlex
import time
from collections.abc import AsyncIterator
from typing import Any

from hangar.broker import Broker, CredentialError
from hangar.config import Settings
from hangar.envspec import EnvCatalog, EnvSpec, image_tag, render, repo_dirname
from hangar.harnesses import HARNESSES, get_harness
from hangar.models import (
    AuditEvent,
    Credential,
    CredentialKind,
    ExecResult,
    Resources,
    Sandbox,
    SandboxState,
    Snapshot,
    SnapshotKind,
    new_id,
    now,
)
from hangar.providers import Provider, ProviderError, load_provider
from hangar.store import Store

log = logging.getLogger("hangar.fleet")

ACTIVE = {SandboxState.PENDING, SandboxState.STARTING, SandboxState.RUNNING, SandboxState.PAUSED}
_NAME_RE = re.compile(r"[^a-z0-9-]+")


class FleetError(RuntimeError):
    pass


def _slug(s: str) -> str:
    return _NAME_RE.sub("-", s.lower()).strip("-")[:40] or "sandbox"


class BuildJob:
    def __init__(self, env: str, provider: str):
        self.id = new_id("build")
        self.env = env
        self.provider = provider
        self.state = "running"
        self.image: str | None = None
        self.error: str | None = None
        self.started_at = now()
        self.finished_at: float | None = None
        self.lines: collections.deque[str] = collections.deque(maxlen=4000)
        self.total = 0  # monotonic count of emitted lines (the deque drops old ones)
        self._cond = asyncio.Condition()
        self.task: asyncio.Task | None = None

    async def emit(self, line: str) -> None:
        self.lines.append(line)
        self.total += 1
        async with self._cond:
            self._cond.notify_all()

    async def follow(self) -> AsyncIterator[str]:
        seen = 0
        while True:
            snapshot, total = list(self.lines), self.total
            first = total - len(snapshot)  # absolute index of snapshot[0]
            for line in snapshot[max(seen - first, 0):]:
                yield line
            seen = total
            if self.state != "running":
                return
            async with self._cond:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=2)
                except TimeoutError:
                    pass

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "environment": self.env, "provider": self.provider, "state": self.state,
            "image": self.image, "error": self.error, "started_at": self.started_at,
            "finished_at": self.finished_at, "log_tail": list(self.lines)[-15:],
        }


class Fleet:
    def __init__(self, settings: Settings, store: Store, broker: Broker, catalog: EnvCatalog):
        self.settings = settings
        self.store = store
        self.broker = broker
        self.catalog = catalog
        self.providers: dict[str, Provider] = {}
        self.provider_errors: dict[str, str] = {}
        self.builds: dict[str, BuildJob] = {}
        self._build_locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._reaper: asyncio.Task | None = None
        self.logins: dict[str, dict[str, Any]] = {}

    # ================================================================== setup
    async def start(self) -> None:
        for name in dict.fromkeys([self.settings.default_provider, *self.settings.enabled_providers]):
            try:
                self.providers[name] = load_provider(name, self.settings)
            except Exception as e:  # noqa: BLE001 - surface in UI, keep serving other providers
                self.provider_errors[name] = str(e)
                log.warning("provider %s unavailable: %s", name, e)
        await self.reconcile()
        self._reaper = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for p in self.providers.values():
            await p.close()

    def provider(self, name: str | None) -> Provider:
        name = name or self.settings.default_provider
        if name in self.providers:
            return self.providers[name]
        reason = self.provider_errors.get(name, "not enabled (add it to HANGAR_ENABLED_PROVIDERS)")
        raise FleetError(f"provider '{name}' unavailable: {reason}")

    def audit(self, actor: str, action: str, target: str | None = None, **detail: Any) -> None:
        # Commands, prompts and terminal input can contain pasted secrets; mask any stored credential value.
        detail = self.broker.redact(detail)
        self.store.audit(AuditEvent(actor=actor, action=action, target=target, detail=detail))

    # ================================================================== environments
    def environments(self) -> list[dict[str, Any]]:
        self.catalog.reload()
        out = []
        for name in self.catalog.names():
            try:
                spec = self.catalog.get(name)
            except Exception as e:  # noqa: BLE001
                out.append({"name": name, "error": str(e)})
                continue
            images = {p: self.store.kv_get(f"image:{p}:{name}") for p in self.providers}
            out.append({
                "name": name, "description": spec.description, "extends": spec.extends, "base": spec.base,
                "harnesses": spec.harnesses, "repo": spec.repo.url if spec.repo else None,
                "skills": len(spec.skills), "warm_pool": spec.warm_pool,
                "resources": spec.resources.model_dump(), "credentials": spec.credentials,
                "images": {k: v for k, v in images.items() if v},
            })
        return out

    def _extra_ca(self) -> bytes | None:
        p = self.settings.extra_ca_bundle
        return p.expanduser().read_bytes() if p else None

    def environment_detail(self, name: str) -> dict[str, Any]:
        spec = self.catalog.get(name)
        ctx = render(spec, self.settings.sandbox_user, self.settings.workspace_dir, self._extra_ca())
        return {"spec": spec.model_dump(mode="json"), "raw": self.catalog.raw(name), "dockerfile": ctx.dockerfile,
                "digest": ctx.digest}

    def save_environment(self, data: dict[str, Any], actor: str = "system") -> EnvSpec:
        spec = self.catalog.add(data)
        self.audit(actor, "environment.save", spec.name)
        return spec

    def _tag_for(self, spec: EnvSpec, provider: str) -> tuple[str, Any]:
        ctx = render(spec, self.settings.sandbox_user, self.settings.workspace_dir, self._extra_ca())
        registry = self.settings.registry if provider in ("kubernetes", "e2b") else None
        return image_tag(spec, ctx, self.settings.image_prefix, registry), ctx

    async def build(self, env: str, provider: str | None = None, force: bool = False, actor: str = "system",
                    wait: bool = True) -> BuildJob:
        pname = provider or self.settings.default_provider
        prov = self.provider(pname)
        spec = self.catalog.get(env)
        tag, ctx = self._tag_for(spec, pname)
        job = BuildJob(env, pname)
        self.builds[job.id] = job

        async def _run() -> None:
            async with self._build_locks[f"{pname}:{env}"]:
                try:
                    if not force and await prov.image_exists(tag):
                        await job.emit(f"image {tag} is up to date")
                        job.image = prov.image_ref(tag)
                    else:
                        await job.emit(f"building {env} -> {tag}")
                        job.image = await prov.build_template(spec, ctx, tag, log=job.emit)
                    self.store.kv_set(f"image:{pname}:{env}", job.image)
                    if spec.warm_pool:
                        await job.emit(f"syncing warm pool to {spec.warm_pool}")
                        await prov.sync_warm_pool(spec, job.image)
                    job.state = "succeeded"
                    self.audit(actor, "environment.build", env, provider=pname, image=job.image)
                except Exception as e:  # noqa: BLE001
                    job.state = "failed"
                    job.error = str(e)
                    await job.emit(f"ERROR: {e}")
                    log.exception("build failed")
                finally:
                    job.finished_at = now()

        job.task = asyncio.create_task(_run())
        if wait:
            await job.task
        return job

    def build_context(self, env: str, provider: str | None = None) -> tuple[str, bytes]:
        """Image tag + build-context tar, for building on another machine (laptop/CI) and registering after."""
        pname = provider or self.settings.default_provider
        spec = self.catalog.get(env)
        tag, ctx = self._tag_for(spec, pname)
        return tag, ctx.tar()

    async def register_image(self, env: str, image: str, provider: str | None = None,
                             actor: str = "system") -> dict[str, Any]:
        """Record an externally built image for an environment and sync its warm pool."""
        pname = provider or self.settings.default_provider
        prov = self.provider(pname)
        spec = self.catalog.get(env)
        ref = prov.image_ref(image)
        self.store.kv_set(f"image:{pname}:{env}", ref)
        if hasattr(prov, "_known_images"):
            prov._known_images.add(image)  # type: ignore[attr-defined]
        await prov.sync_warm_pool(spec, ref)
        self.audit(actor, "environment.register_image", env, provider=pname, image=ref)
        return {"environment": env, "provider": pname, "image": ref}

    async def _ensure_image(self, spec: EnvSpec, pname: str, actor: str) -> str:
        prov = self.provider(pname)
        tag, _ = self._tag_for(spec, pname)
        registered = self.store.kv_get(f"image:{pname}:{spec.name}")
        if registered and registered == prov.image_ref(tag):
            return registered
        if await prov.image_exists(tag):
            return prov.image_ref(tag)
        job = await self.build(spec.name, pname, actor=actor)
        if job.state != "succeeded" or not job.image:
            raise FleetError(f"build of {spec.name} failed: {job.error}")
        return job.image

    # ================================================================== sandboxes
    def get(self, ref: str) -> Sandbox:
        sb = self.store.get(Sandbox, ref)
        if sb is None:
            matches = [s for s in self.store.list(Sandbox) if s.name == ref and s.state in ACTIVE]
            if len(matches) == 1:
                sb = matches[0]
        if sb is None:
            raise FleetError(f"no sandbox '{ref}'")
        return sb

    def list(self, include_stopped: bool = False, label: str | None = None) -> list[Sandbox]:
        sbs = [s for s in self.store.list(Sandbox) if include_stopped or s.state in ACTIVE]
        if label:
            k, _, v = label.partition("=")
            sbs = [s for s in sbs if s.labels.get(k) == v or (not v and k in s.labels)]
        return sorted(sbs, key=lambda s: s.created_at, reverse=True)

    def _touch(self, sb: Sandbox) -> None:
        sb.last_activity = now()
        self.store.put(sb)

    async def create(
        self,
        environment: str,
        name: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        work_branch: str | None = None,
        credentials: list[str] | None = None,
        provider: str | None = None,
        ttl_minutes: int | None = None,
        labels: dict[str, str] | None = None,
        resources: Resources | None = None,
        actor: str = "system",
        _image: str | None = None,
        _parent: str | None = None,
        _restore_workspace: str | None = None,
        _skip_repo: bool = False,
    ) -> Sandbox:
        active = [s for s in self.store.list(Sandbox) if s.state in ACTIVE]
        if len(active) >= self.settings.max_sandboxes:
            raise FleetError(f"fleet is at max_sandboxes={self.settings.max_sandboxes}; destroy some first")
        spec = self.catalog.get(environment)
        pname = provider or self.settings.default_provider
        prov = self.provider(pname)
        cred_names = list(dict.fromkeys(credentials if credentials is not None else spec.credentials))
        for c in cred_names:
            self.broker.get(c)  # fail fast on typos
        ttl = ttl_minutes if ttl_minutes is not None else self.settings.default_ttl_minutes
        base_name = _slug(name or f"{environment}-{new_id('x')[2:]}")
        taken = {s.name for s in active}
        unique = base_name if base_name not in taken else f"{base_name}-{new_id('x')[2:6]}"
        sb = Sandbox(
            name=unique,
            environment=environment,
            provider=pname,
            repo=None if _skip_repo else repo or (spec.repo.url if spec.repo else None),
            branch=None if _skip_repo else branch or (spec.repo.branch if spec.repo else None),
            credentials=cred_names,
            labels=labels or {},
            resources=resources or spec.resources,
            parent=_parent,
            expires_at=now() + ttl * 60 if ttl else None,
            created_by=actor,
        )
        self.store.put(sb)
        self.audit(actor, "sandbox.create", sb.id, environment=environment, provider=pname, name=sb.name)
        try:
            creds = self.broker.acquire(cred_names, sb.id)  # fail before starting anything
            sb.state = SandboxState.STARTING
            self.store.put(sb)
            image = _image or await self._ensure_image(spec, pname, actor)
            sb.image = image
            await prov.create(sb, image, spec)  # image may be a snapshot ref (forks)
            self.store.put(sb)
            await self._wait_running(prov, sb)
            if _parent:
                await self._scrub_inherited_secrets(prov, sb)
            await self.broker.inject(sb, prov, creds)
            if _restore_workspace:
                await self._restore_workspace(prov, sb, _restore_workspace)
            elif sb.repo:
                await self._setup_repo(prov, sb, spec, work_branch)
            sb.state = SandboxState.RUNNING
            sb.error = None
            self._touch(sb)
            if spec.warm_pool and pname in self.providers and _image is None:
                asyncio.create_task(self._refill(spec, pname, image))
            return sb
        except BaseException as e:  # includes CancelledError (client cancelled / tool timeout)
            sb.state = SandboxState.ERROR
            sb.error = str(e) or type(e).__name__
            self.store.put(sb)
            log.warning("create failed for %s: %s", sb.id, sb.error)

            async def _cleanup() -> None:
                try:
                    await self.broker.release(sb, prov, write_back=False)
                    await prov.destroy(sb)
                except Exception:  # noqa: BLE001
                    log.exception("cleanup after failed create of %s", sb.id)

            await asyncio.shield(asyncio.ensure_future(_cleanup()))
            if not isinstance(e, Exception):
                raise
            raise FleetError(f"failed to create sandbox: {e}") from e

    async def _refill(self, spec: EnvSpec, pname: str, image: str) -> None:
        try:
            await self.providers[pname].sync_warm_pool(spec, image)
        except Exception:  # noqa: BLE001
            log.exception("warm pool refill failed")

    async def _wait_running(self, prov: Provider, sb: Sandbox, timeout: float = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = await prov.status(sb)
            if st == SandboxState.RUNNING:
                return
            if st in (SandboxState.ERROR, SandboxState.STOPPED):
                raise ProviderError(f"sandbox entered state {st}")
            await asyncio.sleep(0.5)
        raise ProviderError("timed out waiting for sandbox to start")

    async def _scrub_inherited_secrets(self, prov: Provider, sb: Sandbox) -> None:
        """A fork inherits the parent's filesystem; drop secrets so leases and env are re-issued cleanly."""
        home = f"/home/{self.settings.sandbox_user}"
        paths = [f"{home}/.hangar/env", f"{home}/.git-credentials"]
        for c in self.store.list(Credential):
            if c.file_path:
                paths.append(c.file_path if c.file_path.startswith("/") else f"{home}/{c.file_path}")
        res = await prov.exec(
            sb, "tmux kill-server 2>/dev/null; rm -f " + " ".join(map(shlex.quote, paths))
            + f" && touch {home}/.hangar/env && chmod 600 {home}/.hangar/env", timeout=30)
        if res.exit_code != 0:
            raise FleetError(f"could not scrub inherited secrets from fork: {res.stderr[-300:]}")

    async def _setup_repo(self, prov: Provider, sb: Sandbox, spec: EnvSpec, work_branch: str | None) -> None:
        ws = self.settings.workspace_dir
        dest = (spec.repo.path if spec.repo and spec.repo.path and spec.repo.url == sb.repo else None) or \
            f"{ws}/{repo_dirname(sb.repo)}"
        baked = bool(spec.repo and spec.repo.bake and spec.repo.url == sb.repo)
        q = shlex.quote
        if baked:
            script = f"cd {q(dest)} && git fetch --all --prune -q"
            if sb.branch:
                script += f" && git checkout -q {q(sb.branch)} && git pull -q --ff-only || true"
        else:
            script = f"git clone -q {'--branch ' + q(sb.branch) + ' ' if sb.branch else ''}-- {q(sb.repo)} {q(dest)}"
        if work_branch:
            script += f" && cd {q(dest)} && git checkout -q -b {q(work_branch)}"
        res = await prov.exec(sb, script, timeout=600)
        if res.exit_code != 0:
            raise FleetError(f"repo setup failed: {(res.stderr or res.stdout)[-800:]}")
        setup = spec.repo.setup if spec.repo and spec.repo.url == sb.repo and not baked else None
        if setup:
            res = await prov.exec(sb, setup, cwd=dest, timeout=1800)
            if res.exit_code != 0:
                raise FleetError(f"repo setup command failed: {(res.stderr or res.stdout)[-800:]}")
        sb.info["repo_path"] = dest
        if work_branch:
            sb.info["work_branch"] = work_branch

    async def destroy(self, ref: str, actor: str = "system") -> Sandbox:
        sb = self.get(ref)
        prov = self.providers.get(sb.provider)
        sb.state = SandboxState.STOPPING
        self.store.put(sb)
        if prov:
            await self.broker.release(sb, prov, write_back=True)
            try:
                await prov.destroy(sb)
            except Exception as e:  # noqa: BLE001
                log.warning("destroy %s: %s", sb.id, e)
        sb.state = SandboxState.STOPPED
        self.store.put(sb)
        self.audit(actor, "sandbox.destroy", sb.id, name=sb.name)
        return sb

    async def destroy_many(self, refs: list[str] | None = None, label: str | None = None,
                           actor: str = "system") -> list[str]:
        targets = [self.get(r) for r in refs] if refs else self.list(label=label)
        await asyncio.gather(*(self.destroy(s.id, actor) for s in targets), return_exceptions=True)
        return [s.id for s in targets]

    async def pause(self, ref: str, actor: str = "system") -> Sandbox:
        sb = self.get(ref)
        await self.provider(sb.provider).pause(sb)
        sb.state = SandboxState.PAUSED
        self.store.put(sb)
        self.audit(actor, "sandbox.pause", sb.id)
        return sb

    async def resume(self, ref: str, actor: str = "system") -> Sandbox:
        sb = self.get(ref)
        await self.provider(sb.provider).resume(sb)
        sb.state = SandboxState.RUNNING
        self._touch(sb)
        self.audit(actor, "sandbox.resume", sb.id)
        return sb

    async def extend(self, ref: str, minutes: int, actor: str = "system") -> Sandbox:
        sb = self.get(ref)
        sb.expires_at = max(sb.expires_at or now(), now()) + minutes * 60
        self.store.put(sb)
        prov = self.providers.get(sb.provider)
        if prov and sb.state in ACTIVE:
            await prov.set_expiry(sb, sb.expires_at)
        self.audit(actor, "sandbox.extend", sb.id, minutes=minutes)
        return sb

    def _running(self, ref: str) -> tuple[Sandbox, Provider]:
        sb = self.get(ref)
        if sb.state == SandboxState.PAUSED:
            raise FleetError(f"sandbox {sb.id} is paused; resume it first")
        if sb.state != SandboxState.RUNNING:
            raise FleetError(f"sandbox {sb.id} is {sb.state}")
        return sb, self.provider(sb.provider)

    # ================================================================== I/O
    async def exec(self, ref: str, command: str, cwd: str | None = None, timeout: int = 120,
                   actor: str = "system") -> ExecResult:
        sb, prov = self._running(ref)
        self._touch(sb)
        res = await prov.exec(sb, command, timeout=timeout, cwd=cwd or sb.info.get("repo_path"))
        self.audit(actor, "sandbox.exec", sb.id, command=command[:500], exit_code=res.exit_code)
        return res

    async def read_file(self, ref: str, path: str) -> bytes | None:
        sb, prov = self._running(ref)
        self._touch(sb)
        return await prov.read_file(sb, self._abs(sb, path))

    async def write_file(self, ref: str, path: str, data: bytes, actor: str = "system") -> str:
        sb, prov = self._running(ref)
        p = self._abs(sb, path)
        await prov.write_file(sb, p, data)
        self._touch(sb)
        self.audit(actor, "sandbox.write_file", sb.id, path=p, bytes=len(data))
        return p

    async def list_files(self, ref: str, path: str = ".", depth: int = 1) -> str:
        sb, prov = self._running(ref)
        p = self._abs(sb, path)
        res = await prov.exec(
            sb, f"find {shlex.quote(p)} -mindepth 1 -maxdepth {int(depth)} -not -path '*/.git/*' "
                f"-printf '%y %10s %P\\n' 2>/dev/null | sort -k3 | head -500", timeout=30)
        return res.stdout

    def _abs(self, sb: Sandbox, path: str) -> str:
        if path.startswith("/"):
            return path
        if path.startswith("~/"):
            return f"/home/{self.settings.sandbox_user}/{path[2:]}"
        base = sb.info.get("repo_path") or self.settings.workspace_dir
        return f"{base}/{path}" if path not in (".", "") else base

    # ================================================================== terminals (tmux)
    async def terminal_open(self, ref: str, session: str = "main", command: str | None = None,
                            cwd: str | None = None) -> dict[str, Any]:
        sb, prov = self._running(ref)
        s = shlex.quote(_slug(session))
        wd = shlex.quote(cwd or sb.info.get("repo_path") or self.settings.workspace_dir)
        cmd = f" {shlex.quote(command)}" if command else ""
        res = await prov.exec(
            sb, f"tmux has-session -t {s} 2>/dev/null || tmux new-session -d -s {s} -x 200 -y 50 -c {wd}{cmd}",
            timeout=20)
        if res.exit_code != 0:
            raise FleetError(f"tmux failed: {res.stderr}")
        self._touch(sb)
        return {"sandbox": sb.id, "session": _slug(session)}

    async def terminal_list(self, ref: str) -> list[str]:
        sb, prov = self._running(ref)
        res = await prov.exec(sb, "tmux list-sessions -F '#{session_name}' 2>/dev/null || true", timeout=15)
        return [s for s in res.stdout.split() if s]

    async def terminal_read(self, ref: str, session: str = "main", lines: int = 200) -> str:
        sb, prov = self._running(ref)
        res = await prov.exec(
            sb, f"tmux capture-pane -p -J -t {shlex.quote(_slug(session))} -S -{int(lines)}", timeout=15)
        if res.exit_code != 0:
            raise FleetError(f"no terminal session '{session}' in {sb.id} (open one with terminal_open)")
        return res.stdout.rstrip("\n")

    async def terminal_send(self, ref: str, session: str = "main", text: str = "", enter: bool = True,
                            keys: list[str] | None = None, wait_ms: int = 1500, wait_for: str | None = None,
                            lines: int = 60, actor: str = "system") -> str:
        sb, prov = self._running(ref)
        s = shlex.quote(_slug(session))
        parts = []
        if text:
            parts.append(f"tmux send-keys -t {s} -l -- {shlex.quote(text)}")
        for k in keys or []:
            parts.append(f"tmux send-keys -t {s} {shlex.quote(k)}")
        if enter:
            parts.append(f"tmux send-keys -t {s} Enter")
        if parts:
            res = await prov.exec(sb, " && ".join(parts), timeout=15)
            if res.exit_code != 0:
                raise FleetError(f"terminal_send failed: {res.stderr or 'no such session'}")
        self.audit(actor, "sandbox.terminal_send", sb.id, session=session, text=text[:300], keys=keys or [])
        deadline = time.monotonic() + max(wait_ms, 0) / 1000
        pattern = re.compile(wait_for, re.MULTILINE) if wait_for else None
        out = ""
        while True:
            await asyncio.sleep(0.3 if pattern else max(wait_ms, 0) / 1000)
            out = await self.terminal_read(sb.id, session, lines)
            if not pattern or pattern.search(out) or time.monotonic() >= deadline:
                break
        self._touch(sb)
        return out

    async def terminal_kill(self, ref: str, session: str) -> None:
        sb, prov = self._running(ref)
        await prov.exec(sb, f"tmux kill-session -t {shlex.quote(_slug(session))} 2>/dev/null || true", timeout=15)

    async def attach(self, ref: str, session: str | None = None, cols: int = 120, rows: int = 32):
        """Live PTY for the dashboard. Attaching to a tmux session lets you watch the same
        terminal an MCP client or agent is driving."""
        sb, prov = self._running(ref)
        if session:
            await self.terminal_open(sb.id, session)
            cmd = ["tmux", "attach-session", "-t", _slug(session)]
        else:
            wd = sb.info.get("repo_path") or self.settings.workspace_dir
            cmd = ["bash", "-lc", f"cd {shlex.quote(wd)} 2>/dev/null; exec bash -l"]
        self._touch(sb)
        return await prov.open_terminal(sb, cmd, cols, rows)

    # ================================================================== agent runs
    async def run_agent(self, ref: str, prompt: str, harness: str | None = None, cwd: str | None = None,
                        extra_args: str = "", actor: str = "system") -> dict[str, Any]:
        sb, prov = self._running(ref)
        spec = self.catalog.get(sb.environment)
        hname = harness or next((h for h in spec.harnesses if h != "git"), None)
        if not hname:
            raise FleetError(f"environment {sb.environment} has no agent harness installed")
        h = get_harness(hname)
        if hname not in spec.harnesses:
            raise FleetError(f"harness {hname} is not installed in environment {sb.environment}")
        run_id = new_id("run")
        home = f"/home/{self.settings.sandbox_user}"
        rdir = f"{home}/.hangar/runs"
        wd = cwd or sb.info.get("repo_path") or self.settings.workspace_dir
        await prov.write_file(sb, f"{rdir}/{run_id}.prompt", prompt.encode(), mode=0o600)
        script = (
            "#!/usr/bin/env bash\n"
            "set -a; [ -f ~/.hangar/env ] && . ~/.hangar/env; set +a\n"
            f"export HANGAR_PROMPT_FILE={rdir}/{run_id}.prompt HANGAR_RUN_ID={run_id}\n"
            f"cd {shlex.quote(wd)}\n"
            f"{{ {h.run} {extra_args}; }} 2>&1 | tee {rdir}/{run_id}.log\n"
            f"echo ${{PIPESTATUS[0]}} > {rdir}/{run_id}.exit\n"
        )
        await prov.write_file(sb, f"{rdir}/{run_id}.sh", script.encode(), mode=0o700)
        res = await prov.exec(sb, f"tmux new-session -d -s {run_id} -x 200 -y 50 -c {shlex.quote(wd)} "
                                  f"'bash {rdir}/{run_id}.sh; exec bash -l'", timeout=20)
        if res.exit_code != 0:
            raise FleetError(f"failed to start agent: {res.stderr}")
        runs = sb.info.setdefault("runs", [])
        runs.append({"id": run_id, "harness": hname, "started_at": now(), "prompt": prompt[:200]})
        self._touch(sb)
        self.audit(actor, "agent.run", sb.id, run=run_id, harness=hname, prompt=prompt[:300])
        return {"sandbox": sb.id, "run_id": run_id, "harness": hname, "terminal_session": run_id,
                "log": f"{rdir}/{run_id}.log"}

    async def agent_status(self, ref: str, run_id: str, tail_lines: int = 60) -> dict[str, Any]:
        sb, prov = self._running(ref)
        if not re.fullmatch(r"run-[0-9a-f]{8}", run_id):
            raise FleetError(f"invalid run id '{run_id}'")
        rdir = f"/home/{self.settings.sandbox_user}/.hangar/runs"
        res = await prov.exec(
            sb, f"cat {rdir}/{run_id}.exit 2>/dev/null || echo RUNNING; echo '---'; "
                f"tail -n {int(tail_lines)} {rdir}/{run_id}.log 2>/dev/null", timeout=20)
        head, _, tail = res.stdout.partition("---\n")
        status = head.strip()
        return {"run_id": run_id, "state": "running" if status == "RUNNING" else "finished",
                "exit_code": None if status == "RUNNING" else int(status or -1), "output_tail": tail}

    # ================================================================== snapshots / forks
    async def snapshot(self, ref: str, label: str | None = None, kind: str = "auto",
                       actor: str = "system") -> Snapshot:
        sb, prov = self._running(ref)
        snap_id = new_id("snap")
        use_full = kind == "full" or (kind == "auto" and prov.capabilities.full_snapshots)
        if use_full:
            try:
                ref_ = await prov.snapshot(sb, snap_id)
                snap = Snapshot(id=snap_id, sandbox_id=sb.id, environment=sb.environment, provider=sb.provider,
                                kind=SnapshotKind.FULL, label=label, ref=ref_)
            except NotImplementedError as e:
                if kind == "full":
                    raise FleetError(f"{sb.provider} provider has no full snapshots; use kind=workspace") from e
                use_full = False
        if not use_full:
            snap = await self._workspace_snapshot(prov, sb, snap_id, label)
        self.store.put(snap)
        self.audit(actor, "sandbox.snapshot", sb.id, snapshot=snap.id, kind=snap.kind.value)
        return snap

    async def _workspace_snapshot(self, prov: Provider, sb: Sandbox, snap_id: str, label: str | None) -> Snapshot:
        tmp = f"/tmp/{snap_id}.tgz"
        res = await prov.exec(sb, f"tar czf {tmp} -C {self.settings.workspace_dir} .", timeout=900)
        if res.exit_code not in (0, 1):  # 1 = files changed while reading, still usable
            raise FleetError(f"workspace snapshot failed: {res.stderr[-500:]}")
        data = await prov.read_file(sb, tmp)
        await prov.exec(sb, f"rm -f {tmp}", timeout=30)
        if data is None:
            raise FleetError("workspace snapshot produced no archive")
        path = self.settings.snapshot_dir / f"{snap_id}.tgz"
        path.write_bytes(data)
        return Snapshot(id=snap_id, sandbox_id=sb.id, environment=sb.environment, provider=sb.provider,
                        kind=SnapshotKind.WORKSPACE, label=label, ref=str(path), size_bytes=len(data))

    async def _restore_workspace(self, prov: Provider, sb: Sandbox, archive: str) -> None:
        from pathlib import Path

        data = Path(archive).read_bytes()
        tmp = f"/tmp/restore-{sb.id}.tgz"
        await prov.write_file(sb, tmp, data)
        res = await prov.exec(sb, f"tar xzf {tmp} -C {self.settings.workspace_dir} && rm -f {tmp}", timeout=900)
        if res.exit_code != 0:
            raise FleetError(f"workspace restore failed: {res.stderr[-500:]}")

    def snapshots(self, sandbox: str | None = None) -> list[Snapshot]:
        snaps = self.store.list(Snapshot)
        if sandbox:
            snaps = [s for s in snaps if s.sandbox_id == sandbox]
        return sorted(snaps, key=lambda s: s.created_at, reverse=True)

    async def delete_snapshot(self, snap_id: str, actor: str = "system") -> None:
        snap = self.store.get(Snapshot, snap_id)
        if not snap:
            raise FleetError(f"no snapshot {snap_id}")
        if snap.kind == SnapshotKind.WORKSPACE:
            from pathlib import Path

            Path(snap.ref).unlink(missing_ok=True)
        elif snap.provider in self.providers:
            await self.providers[snap.provider].delete_snapshot(snap.ref)
        self.store.delete(Snapshot, snap_id)
        self.audit(actor, "snapshot.delete", snap_id)

    async def fork(self, source: str, count: int = 1, name_prefix: str | None = None,
                   credentials: list[str] | None = None, work_branch_prefix: str | None = None,
                   provider: str | None = None, ttl_minutes: int | None = None,
                   labels: dict[str, str] | None = None, actor: str = "system") -> list[Sandbox]:
        """Clone a live sandbox (snapshotting it first) or an existing snapshot into N new sandboxes."""
        if count < 1 or count > 20:
            raise FleetError("count must be between 1 and 20")
        snap = self.store.get(Snapshot, source)
        if snap is None:
            snap = await self.snapshot(source, label="fork", actor=actor)
        parent = self.store.get(Sandbox, snap.sandbox_id)
        if credentials is None and parent:
            # Inherit everything except exclusive creds, which stay with the parent.
            credentials = [c for c in parent.credentials if not self.broker.get(c).exclusive]
        pname = provider or snap.provider
        prefix = _slug(name_prefix or (parent.name if parent else snap.environment))
        same_provider = pname == snap.provider and snap.kind == SnapshotKind.FULL

        async def _one(i: int) -> Sandbox:
            sb = await self.create(
                environment=snap.environment,
                name=f"{prefix}-f{i + 1}-{new_id('x')[2:6]}",
                repo=None if same_provider or snap.kind == SnapshotKind.WORKSPACE
                else (parent.repo if parent else None),
                credentials=credentials or [],
                provider=pname,
                ttl_minutes=ttl_minutes,
                labels={**(parent.labels if parent else {}), **(labels or {}), "fork-of": snap.sandbox_id},
                actor=actor,
                _image=snap.ref if same_provider else None,
                _parent=snap.id,
                _restore_workspace=snap.ref if snap.kind == SnapshotKind.WORKSPACE else None,
                _skip_repo=same_provider or snap.kind == SnapshotKind.WORKSPACE,
            )
            if parent and parent.info.get("repo_path"):
                sb.info["repo_path"] = parent.info["repo_path"]
                self.store.put(sb)
            if work_branch_prefix and sb.info.get("repo_path"):
                await self.exec(sb.id, f"git checkout -q -b {shlex.quote(f'{work_branch_prefix}-{i + 1}')}",
                                actor=actor)
                sb.info["work_branch"] = f"{work_branch_prefix}-{i + 1}"
                self.store.put(sb)
            return sb

        results = await asyncio.gather(*(_one(i) for i in range(count)), return_exceptions=True)
        ok = [r for r in results if isinstance(r, Sandbox)]
        errs = [str(r) for r in results if isinstance(r, BaseException)]
        if not ok:
            raise FleetError("all forks failed: " + "; ".join(errs))
        if errs:
            log.warning("some forks failed: %s", errs)
        return ok

    # ================================================================== logins
    async def start_login(self, harness: str, credential_name: str, environment: str | None = None,
                          provider: str | None = None, actor: str = "system") -> dict[str, Any]:
        h = get_harness(harness)
        if not h.logins:
            raise FleetError(f"{harness} has no interactive login flow; add an API key instead")
        flow = h.logins[0]
        env = environment or next(
            (e for e in self.catalog.names() if harness in self.catalog.get(e).harnesses), None)
        if not env:
            raise FleetError(f"no environment installs {harness}; create one (see envs/) to host the login")
        sb = await self.create(env, name=f"login-{harness}", credentials=[], provider=provider, ttl_minutes=30,
                               _skip_repo=True,
                               labels={"purpose": "login"}, actor=actor)
        await self.terminal_open(sb.id, "login", cwd=f"/home/{self.settings.sandbox_user}")
        await self.terminal_send(sb.id, "login", flow.command, wait_ms=500, actor=actor)
        login_id = new_id("login")
        self.logins[login_id] = {"id": login_id, "harness": harness, "credential": credential_name,
                                 "sandbox": sb.id, "session": "login", "flow": flow.model_dump(mode="json")}
        self.audit(actor, "credential.login_start", credential_name, harness=harness, sandbox=sb.id)
        return {**self.logins[login_id], "instructions": flow.notes or "Complete the login in the terminal."}

    async def complete_login(self, login_id: str, actor: str = "system") -> Credential:
        lg = self.logins.get(login_id)
        if not lg:
            raise FleetError(f"no login session {login_id}")
        h = get_harness(lg["harness"])
        flow = h.logins[0]
        value: str | bytes | None = None
        kind = flow.captures
        if flow.token_regex:
            out = await self.terminal_read(lg["sandbox"], lg["session"], lines=2000)
            m = re.findall(flow.token_regex + r"(?![0-9A-Za-z_\-])", out)
            value = m[-1] if m else None
        elif flow.capture_file:
            value = await self.read_file(lg["sandbox"], f"~/{flow.capture_file}")
        if not value:
            raise FleetError("login not finished yet: no token/credential file found. Finish the flow in the "
                             "terminal, then complete again.")
        cred = self.broker.put(lg["credential"], lg["harness"], kind, value,
                               description=f"{h.title} login captured {time.strftime('%Y-%m-%d')}")
        await self.destroy(lg["sandbox"], actor=actor)
        self.logins.pop(login_id, None)
        self.audit(actor, "credential.login_complete", cred.name, harness=lg["harness"])
        return cred

    # ================================================================== housekeeping
    def _mark_gone(self, sb: Sandbox, reason: str) -> None:
        sb.state = SandboxState.STOPPED
        sb.error = reason
        self.store.put(sb)
        for c in self.store.list(Credential):
            if c.leased_to == sb.id:  # the auth file is gone with the sandbox; keep the vault copy
                c.leased_to = None
                self.store.put(c)
        self.audit("reaper", "sandbox.gone", sb.id, reason=reason)

    async def reconcile(self) -> None:
        """Mark sandboxes whose backing container/pod/VM died or vanished as stopped."""
        for pname, prov in self.providers.items():
            try:
                refs = await prov.list_refs()
            except Exception:  # noqa: BLE001
                continue
            if refs is None:  # provider can't enumerate; nothing to compare against
                continue
            for sb in self.store.list(Sandbox):
                if sb.provider != pname or not sb.provider_ref:
                    continue
                if sb.state not in (SandboxState.RUNNING, SandboxState.PAUSED):
                    continue  # pending/starting sandboxes may not be listed yet
                reported = refs.get(sb.provider_ref)
                if reported is None:
                    self._mark_gone(sb, "disappeared from provider")
                elif reported in (SandboxState.STOPPED, SandboxState.ERROR):
                    self._mark_gone(sb, f"provider reports {reported.value}")

    async def _reaper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.settings.reaper_interval_seconds)
                t = now()
                for sb in self.list():
                    if sb.expires_at and sb.expires_at < t:
                        log.info("expiring %s", sb.id)
                        await self.destroy(sb.id, actor="reaper")
                await self.reconcile()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("reaper iteration failed")

    def overview(self) -> dict[str, Any]:
        sbs = self.list()
        return {
            "sandboxes": {st.value: sum(1 for s in sbs if s.state == st) for st in ACTIVE},
            "total_active": len(sbs),
            "max_sandboxes": self.settings.max_sandboxes,
            "providers": {
                name: {"ok": True, "capabilities": p.capabilities.model_dump()} for name, p in self.providers.items()
            } | {name: {"ok": False, "error": err} for name, err in self.provider_errors.items()},
            "default_provider": self.settings.default_provider,
            "environments": len(self.catalog.names()),
            "credentials": len(self.broker.list()),
            "harnesses": {k: v.title for k, v in HARNESSES.items()},
        }


__all__ = ["Fleet", "FleetError", "CredentialError", "CredentialKind"]
