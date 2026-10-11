"""Docker provider: sandboxes are containers on the local Docker engine.

Works on macOS (Docker Desktop / OrbStack / Colima), Linux and WSL2. On a Linux host you
can harden isolation by installing gVisor (`HANGAR_DOCKER_RUNTIME=runsc`) or Kata
(`HANGAR_DOCKER_RUNTIME=kata`, needs /dev/kvm) - the rest of hangar is unchanged.
"""

from __future__ import annotations

import asyncio
import io
import posixpath
import socket
import tarfile
import time

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from hangar.config import Settings
from hangar.envspec import BuildContext, EnvSpec
from hangar.models import ExecResult, Sandbox, SandboxState
from hangar.providers._build import docker_build
from hangar.providers.base import Capabilities, LogFn, Provider, ProviderError, TerminalStream

MAX_OUTPUT = 256 * 1024
LABEL = "dev.hangar.sandbox"
POOL_LABEL = "dev.hangar.pool"
SYSTEM_DIRS = ("/etc/", "/usr/", "/opt/", "/root/", "/var/", "/bin/", "/sbin/", "/lib")

_STATE = {
    "created": SandboxState.STARTING,
    "running": SandboxState.RUNNING,
    "paused": SandboxState.PAUSED,
    "restarting": SandboxState.STARTING,
    "removing": SandboxState.STOPPING,
    "exited": SandboxState.STOPPED,
    "dead": SandboxState.ERROR,
}


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) <= MAX_OUTPUT:
        return s, False
    return "[... truncated ...]\n" + s[-MAX_OUTPUT:], True


class DockerTerminal(TerminalStream):
    def __init__(self, client: docker.DockerClient, exec_id: str, sock):
        self._client = client
        self._exec_id = exec_id
        self._sock = getattr(sock, "_sock", sock)
        self._closed = False

    async def read(self) -> bytes:
        if self._closed:
            return b""
        try:
            return await asyncio.to_thread(self._sock.recv, 65536)
        except OSError:
            return b""

    async def write(self, data: bytes) -> None:
        if not self._closed:
            await asyncio.to_thread(self._sock.sendall, data)

    async def resize(self, cols: int, rows: int) -> None:
        try:
            await asyncio.to_thread(self._client.api.exec_resize, self._exec_id, height=rows, width=cols)
        except APIError:
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


class DockerProvider(Provider):
    name = "docker"

    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(
                "cannot reach the Docker engine. Start Docker Desktop/OrbStack, or set DOCKER_HOST."
            ) from e
        self._pool_lock = asyncio.Lock()

    @property
    def capabilities(self) -> Capabilities:
        rt = (self.settings.docker_runtime or "").lower()
        return Capabilities(full_snapshots=True, warm_pools=True, pause=True, microvm="kata" in rt)

    # ------------------------------------------------------------------ templates
    async def image_exists(self, tag: str) -> bool:
        try:
            await asyncio.to_thread(self.client.images.get, tag)
            return True
        except ImageNotFound:
            return False

    async def build_template(self, spec: EnvSpec, ctx: BuildContext, tag: str, log: LogFn | None = None) -> str:
        return await docker_build(ctx, tag, log=log)

    def _run_kwargs(self, name: str, image: str, spec_resources, labels: dict[str, str], hostname: str) -> dict:
        kw = dict(
            image=image,
            name=name,
            detach=True,
            init=True,
            labels=labels,
            hostname=hostname[:63],
            command=["sleep", "infinity"],
            user=self.settings.sandbox_user,
            working_dir=self.settings.workspace_dir,
            nano_cpus=int(spec_resources.cpus * 1e9),
            mem_limit=f"{int(spec_resources.memory_gb * 1024)}m",
        )
        if self.settings.docker_runtime:
            kw["runtime"] = self.settings.docker_runtime
        if self.settings.docker_network:
            kw["network"] = self.settings.docker_network
        return kw

    async def sync_warm_pool(self, spec: EnvSpec, image: str) -> None:
        async with self._pool_lock:
            pool = await asyncio.to_thread(
                self.client.containers.list, all=True,
                filters={"label": f"{POOL_LABEL}={image}", "name": "hangar-pool-"},
            )
            pool = [c for c in pool if c.name.startswith("hangar-pool-")]  # claimed ones were renamed
            # Pool members built from an older image of this environment are stale after a rebuild.
            stale = await asyncio.to_thread(
                self.client.containers.list, all=True,
                filters={"label": f"dev.hangar.env={spec.name}", "name": "hangar-pool-"},
            )
            for c in stale:
                if c.name.startswith("hangar-pool-") and c.labels.get(POOL_LABEL) != image:
                    await asyncio.to_thread(c.remove, force=True)
            alive = [c for c in pool if c.status in ("running", "created")]
            for c in pool:
                if c not in alive:
                    await asyncio.to_thread(c.remove, force=True)
            for _ in range(max(0, spec.warm_pool - len(alive))):
                name = f"hangar-pool-{spec.name}-{int(time.time() * 1000) % 10**8}"
                kw = self._run_kwargs(name, image, spec.resources, {POOL_LABEL: image, "dev.hangar.env": spec.name},
                                      hostname=spec.name)
                await asyncio.to_thread(self.client.containers.run, **kw)

    async def _claim_warm(self, image: str):
        async with self._pool_lock:
            pool = await asyncio.to_thread(
                self.client.containers.list,
                filters={"label": f"{POOL_LABEL}={image}", "status": "running", "name": "hangar-pool-"},
            )
            pool = [c for c in pool if c.name.startswith("hangar-pool-")]
            if not pool:
                return None
            # Rename inside the lock so two concurrent claims can never take the same container.
            claimed = pool[0]
            await asyncio.to_thread(claimed.rename, f"hangar-claiming-{claimed.id[:12]}")
            return claimed

    # ------------------------------------------------------------------ lifecycle
    async def create(self, sandbox: Sandbox, image: str, spec: EnvSpec | None = None) -> None:
        name = f"hangar-{sandbox.id}"
        warm = await self._claim_warm(image) if spec and spec.warm_pool else None
        if warm is not None:
            # Labels are immutable in Docker; pool membership is the name prefix + label, so a
            # rename takes the container out of the pool and the store becomes the source of truth.
            await asyncio.to_thread(warm.rename, name)
            sandbox.provider_ref = warm.id
            sandbox.info["warm"] = True
            sandbox.info["pool_image"] = image
            return
        labels = {LABEL: sandbox.id, "dev.hangar.env": sandbox.environment}
        kw = self._run_kwargs(name, image, sandbox.resources, labels, hostname=sandbox.name)
        try:
            c = await asyncio.to_thread(self.client.containers.run, **kw)
        except ImageNotFound as e:
            raise ProviderError(f"image {image} not found; build the environment first") from e
        sandbox.provider_ref = c.id
        sandbox.info["warm"] = False

    async def _container(self, sandbox: Sandbox):
        if not sandbox.provider_ref:
            raise ProviderError(f"sandbox {sandbox.id} has no container")
        try:
            return await asyncio.to_thread(self.client.containers.get, sandbox.provider_ref)
        except NotFound as e:
            raise ProviderError(f"container for sandbox {sandbox.id} no longer exists") from e

    async def status(self, sandbox: Sandbox) -> SandboxState:
        try:
            c = await self._container(sandbox)
        except ProviderError:
            return SandboxState.STOPPED
        return _STATE.get(c.status, SandboxState.ERROR)

    async def destroy(self, sandbox: Sandbox) -> None:
        try:
            c = await self._container(sandbox)
        except ProviderError:
            return
        await asyncio.to_thread(c.remove, force=True)

    async def pause(self, sandbox: Sandbox) -> None:
        await asyncio.to_thread((await self._container(sandbox)).pause)

    async def resume(self, sandbox: Sandbox) -> None:
        await asyncio.to_thread((await self._container(sandbox)).unpause)

    async def list_refs(self) -> dict[str, SandboxState]:
        cs = await asyncio.to_thread(self.client.containers.list, all=True, filters={"name": "hangar-sb-"})
        return {c.id: _STATE.get(c.status, SandboxState.ERROR) for c in cs}

    # ------------------------------------------------------------------ I/O
    async def exec(self, sandbox, command, timeout=120, cwd=None, user=None) -> ExecResult:
        c = await self._container(sandbox)
        argv = ["timeout", "--kill-after=5", str(int(timeout)), "bash", "-lc", command]
        t0 = time.monotonic()

        def _run():
            return c.exec_run(argv, user=user or self.settings.sandbox_user,
                              workdir=cwd or self.settings.workspace_dir, demux=True)

        try:
            res = await asyncio.to_thread(_run)
        except APIError as e:
            raise ProviderError(f"exec failed: {e.explanation or e}") from e
        out_b, err_b = res.output if res.output else (b"", b"")
        out, t1 = _truncate((out_b or b"").decode(errors="replace"))
        err, t2 = _truncate((err_b or b"").decode(errors="replace"))
        code = res.exit_code if res.exit_code is not None else -1
        if code == 124:
            err += f"\n[hangar] command timed out after {timeout}s"
        return ExecResult(exit_code=code, stdout=out, stderr=err,
                          duration_ms=int((time.monotonic() - t0) * 1000), truncated=t1 or t2)

    async def read_file(self, sandbox, path) -> bytes | None:
        c = await self._container(sandbox)
        try:
            stream, _ = await asyncio.to_thread(c.get_archive, path)
        except NotFound:
            return None
        data = b"".join(await asyncio.to_thread(list, stream))
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            member = next((m for m in tf.getmembers() if m.isfile()), None)
            if member is None:
                return None
            f = tf.extractfile(member)
            return f.read() if f else None

    async def write_file(self, sandbox, path, data, mode=0o644) -> None:
        c = await self._container(sandbox)
        parent = posixpath.dirname(path)
        user = self.settings.sandbox_user
        owned = not path.startswith(SYSTEM_DIRS)
        mk = await asyncio.to_thread(c.exec_run, ["mkdir", "-p", parent], user=user if owned else "root")
        if mk.exit_code != 0:
            raise ProviderError(f"cannot create {parent}: {mk.output.decode(errors='replace')}")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(posixpath.basename(path))
            info.size = len(data)
            info.mode = mode
            info.mtime = int(time.time())
            if owned:
                info.uid = info.gid = 1000
            tf.addfile(info, io.BytesIO(data))
        ok = await asyncio.to_thread(c.put_archive, parent, buf.getvalue())
        if not ok:
            raise ProviderError(f"failed to write {path}")

    async def open_terminal(self, sandbox, command, cols=120, rows=32) -> TerminalStream:
        c = await self._container(sandbox)
        api = self.client.api
        ex = await asyncio.to_thread(
            api.exec_create, c.id, command, stdin=True, tty=True, user=self.settings.sandbox_user,
            workdir=self.settings.workspace_dir, environment={"TERM": "xterm-256color", "COLUMNS": str(cols),
                                                              "LINES": str(rows)},
        )
        sock = await asyncio.to_thread(api.exec_start, ex["Id"], tty=True, socket=True)
        term = DockerTerminal(self.client, ex["Id"], sock)
        await asyncio.sleep(0.05)
        await term.resize(cols, rows)
        return term

    # ------------------------------------------------------------------ snapshots
    async def snapshot(self, sandbox, snapshot_id) -> str:
        c = await self._container(sandbox)
        repo = f"hangar-snap/{sandbox.environment}"
        await asyncio.to_thread(
            c.commit, repository=repo, tag=snapshot_id,
            conf={"Labels": {"dev.hangar.snapshot": snapshot_id, "dev.hangar.from": sandbox.id}},
        )
        return f"{repo}:{snapshot_id}"

    async def delete_image(self, ref: str) -> None:
        try:
            await asyncio.to_thread(self.client.images.remove, ref, force=True)
        except (ImageNotFound, APIError):
            pass

    async def close(self) -> None:
        self.client.close()
