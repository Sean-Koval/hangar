"""E2B provider: sandboxes are Firecracker microVMs on E2B Cloud, E2B BYOC, or a self-hosted E2B.

Environment images are built with docker, pushed to HANGAR_REGISTRY, then registered as an
E2B template via `Template().from_image(...)`. Point at a self-hosted E2B with
HANGAR_E2B_DOMAIN (or E2B_DOMAIN). Status: implemented against the e2b 2.x SDK; exercise it
against your account before relying on it for production fleets.
"""

from __future__ import annotations

import asyncio
import shlex
import time

from hangar.config import Settings
from hangar.envspec import BuildContext, EnvSpec
from hangar.models import ExecResult, Sandbox, SandboxState
from hangar.providers._build import docker_build
from hangar.providers.base import Capabilities, LogFn, Provider, ProviderError, TerminalStream

try:
    import e2b
    from e2b import Sandbox as E2BSandbox
    from e2b import Template
    from e2b.sandbox.commands.command_handle import CommandExitException, PtySize
except ImportError:  # pragma: no cover
    e2b = None  # type: ignore[assignment]

MAX_OUTPUT = 256 * 1024
MAX_TIMEOUT = 24 * 3600


class E2BTerminal(TerminalStream):
    def __init__(self, sbx, loop: asyncio.AbstractEventLoop):
        self.sbx = sbx
        self.loop = loop
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.handle = None
        self._closed = False

    def on_data(self, data: bytes) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, bytes(data))

    async def read(self) -> bytes:
        return b"" if self._closed else await self.queue.get()

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self.sbx.pty.send_stdin, self.handle.pid, data)

    async def resize(self, cols: int, rows: int) -> None:
        await asyncio.to_thread(self.sbx.pty.resize, self.handle.pid, PtySize(rows=rows, cols=cols))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.queue.put_nowait(b"")
        try:
            await asyncio.to_thread(self.sbx.pty.kill, self.handle.pid)
        except Exception:  # noqa: BLE001
            pass


class E2BProvider(Provider):
    name = "e2b"

    def __init__(self, settings: Settings):
        if e2b is None:
            raise ProviderError("e2b extra not installed: pip install 'hangar[e2b]'")
        if not settings.e2b_api_key:
            raise ProviderError("set HANGAR_E2B_API_KEY to use the e2b provider")
        self.settings = settings
        self.opts = {"api_key": settings.e2b_api_key}
        if settings.e2b_domain:
            self.opts["domain"] = settings.e2b_domain
        self._handles: dict[str, object] = {}

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(full_snapshots=True, warm_pools=False, pause=True, microvm=True)

    # ------------------------------------------------------------------ templates
    async def image_exists(self, tag: str) -> bool:
        # `tag` is the registry image; the E2B template is named after it.
        name = self._from_tag(tag)
        try:
            return await asyncio.to_thread(Template.exists, name, **self.opts)
        except Exception:  # noqa: BLE001
            return False

    def _base(self, tag: str):
        """Start an E2B template from our pushed image, with whatever registry auth is configured."""
        s = self.settings
        if s.e2b_gcp_service_account_json:
            return Template().from_gcp_registry(tag, s.e2b_gcp_service_account_json)
        if s.e2b_aws_access_key_id and s.e2b_aws_secret_access_key:
            return Template().from_aws_registry(tag, access_key_id=s.e2b_aws_access_key_id,
                                                secret_access_key=s.e2b_aws_secret_access_key,
                                                region=s.e2b_aws_region or "us-east-1")
        if s.e2b_registry_username and s.e2b_registry_password:
            return Template().from_image(tag, username=s.e2b_registry_username, password=s.e2b_registry_password)
        if any(h in tag for h in (".pkg.dev/", "gcr.io/", ".amazonaws.com/")):
            raise ProviderError(
                f"{tag.split('/')[0]} is a private registry: set HANGAR_E2B_GCP_SERVICE_ACCOUNT_JSON (Artifact "
                "Registry/GCR) or HANGAR_E2B_AWS_* (ECR) so E2B can pull the image")
        return Template().from_image(tag)

    def image_ref(self, tag: str) -> str:
        return self._from_tag(tag)

    @staticmethod
    def _from_tag(tag: str) -> str:
        repo, _, digest = tag.rpartition(":")
        return f"hangar-{repo.rsplit('/', 1)[-1]}:{digest}"

    async def build_template(self, spec: EnvSpec, ctx: BuildContext, tag: str, log: LogFn | None = None) -> str:
        if not self.settings.registry:
            raise ProviderError("set HANGAR_REGISTRY: E2B builds templates from an image in a registry it can pull")
        await docker_build(ctx, tag, log=log, platform="linux/amd64", push=True)
        name = self._from_tag(tag)
        tmpl = self._base(tag).set_user(self.settings.sandbox_user).set_workdir(self.settings.workspace_dir)
        loop = asyncio.get_running_loop()

        def on_log(entry) -> None:
            if log:
                r = log(f"[e2b] {getattr(entry, 'message', entry)}")
                if asyncio.iscoroutine(r):
                    asyncio.run_coroutine_threadsafe(r, loop)

        await asyncio.to_thread(Template.build, tmpl, name, cpu_count=max(1, int(spec.resources.cpus)),
                                memory_mb=int(spec.resources.memory_gb * 1024), on_build_logs=on_log, **self.opts)
        return name

    # ------------------------------------------------------------------ lifecycle
    async def _sbx(self, sandbox: Sandbox):
        ref = sandbox.provider_ref
        if not ref:
            raise ProviderError(f"sandbox {sandbox.id} has no E2B id")
        if ref not in self._handles:
            self._handles[ref] = await asyncio.to_thread(E2BSandbox.connect, ref, **self.opts)
        return self._handles[ref]

    async def create(self, sandbox: Sandbox, image: str, spec: EnvSpec | None = None) -> None:
        timeout = int(min(MAX_TIMEOUT, (sandbox.expires_at - time.time()) if sandbox.expires_at else 3600))
        sbx = await asyncio.to_thread(
            E2BSandbox.create, image, timeout=max(timeout, 300), metadata={"hangar_id": sandbox.id,
                                                                          "hangar_env": sandbox.environment},
            envs={"TERM": "xterm-256color"}, **self.opts)
        sandbox.provider_ref = sbx.sandbox_id
        self._handles[sbx.sandbox_id] = sbx

    async def status(self, sandbox: Sandbox) -> SandboxState:
        # get_info, not connect(): connect() auto-resumes paused sandboxes.
        try:
            info = await asyncio.to_thread(E2BSandbox.get_info, sandbox.provider_ref, **self.opts)
        except Exception:  # noqa: BLE001
            return SandboxState.STOPPED
        return SandboxState.PAUSED if str(getattr(info, "state", "running")) == "paused" else SandboxState.RUNNING

    async def list_refs(self) -> dict[str, SandboxState] | None:
        def _list() -> dict[str, SandboxState]:
            out: dict[str, SandboxState] = {}
            pager = E2BSandbox.list(**self.opts)
            while pager.has_next:
                for info in pager.next_items():
                    if "hangar_id" in (info.metadata or {}):
                        out[info.sandbox_id] = (SandboxState.PAUSED if str(info.state) == "paused"
                                                else SandboxState.RUNNING)
            return out

        try:
            return await asyncio.to_thread(_list)
        except Exception:  # noqa: BLE001
            return None

    async def set_expiry(self, sandbox: Sandbox, expires_at: float) -> None:
        secs = int(min(MAX_TIMEOUT, max(60, expires_at - time.time())))
        await asyncio.to_thread(E2BSandbox.set_timeout, sandbox.provider_ref, secs, **self.opts)

    async def destroy(self, sandbox: Sandbox) -> None:
        if sandbox.provider_ref:
            try:
                await asyncio.to_thread(E2BSandbox.kill, sandbox.provider_ref, **self.opts)
            except Exception:  # noqa: BLE001
                pass
            self._handles.pop(sandbox.provider_ref, None)

    async def pause(self, sandbox: Sandbox) -> None:
        sbx = await self._sbx(sandbox)
        await asyncio.to_thread(sbx.pause)

    async def resume(self, sandbox: Sandbox) -> None:
        self._handles.pop(sandbox.provider_ref or "", None)
        await self._sbx(sandbox)  # connect() auto-resumes

    # ------------------------------------------------------------------ I/O
    async def exec(self, sandbox, command, timeout=120, cwd=None, user=None) -> ExecResult:
        sbx = await self._sbx(sandbox)
        t0 = time.monotonic()

        def _run():
            try:
                r = sbx.commands.run(f"bash -lc {shlex.quote(command)}", user=user or self.settings.sandbox_user,
                                     cwd=cwd or self.settings.workspace_dir, timeout=timeout)
                return r.exit_code, r.stdout, r.stderr
            except CommandExitException as e:
                return e.exit_code, e.stdout, e.stderr

        code, out, err = await asyncio.to_thread(_run)
        return ExecResult(exit_code=code, stdout=out[-MAX_OUTPUT:], stderr=err[-MAX_OUTPUT:],
                          duration_ms=int((time.monotonic() - t0) * 1000),
                          truncated=len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT)

    async def read_file(self, sandbox, path) -> bytes | None:
        sbx = await self._sbx(sandbox)
        try:
            data = await asyncio.to_thread(sbx.files.read, path, format="bytes", user=self.settings.sandbox_user)
        except Exception:  # noqa: BLE001
            return None
        return bytes(data)

    async def write_file(self, sandbox, path, data, mode=0o644) -> None:
        sbx = await self._sbx(sandbox)
        await asyncio.to_thread(sbx.files.write, path, data, user=self.settings.sandbox_user)
        if mode != 0o644:
            await self.exec(sandbox, f"chmod {mode:o} {shlex.quote(path)}", timeout=30)

    async def open_terminal(self, sandbox, command, cols=120, rows=32) -> TerminalStream:
        sbx = await self._sbx(sandbox)
        term = E2BTerminal(sbx, asyncio.get_running_loop())

        def _open():
            h = sbx.pty.create(PtySize(rows=rows, cols=cols), user=self.settings.sandbox_user,
                               cwd=self.settings.workspace_dir, timeout=0)
            return h

        term.handle = await asyncio.to_thread(_open)
        loop = asyncio.get_running_loop()

        def _pump() -> None:
            try:
                term.handle.wait(on_pty=term.on_data)
            except Exception:  # noqa: BLE001 - shell exited with non-zero status
                pass
            loop.call_soon_threadsafe(term.queue.put_nowait, b"")  # EOF

        loop.run_in_executor(None, _pump)
        await term.write(("exec " + " ".join(shlex.quote(c) for c in command) + "\n").encode())
        return term

    # ------------------------------------------------------------------ snapshots
    async def snapshot(self, sandbox, snapshot_id) -> str:
        sbx = await self._sbx(sandbox)
        info = await asyncio.to_thread(sbx.create_snapshot, snapshot_id)
        return getattr(info, "snapshot_id", None) or getattr(info, "id", snapshot_id)
