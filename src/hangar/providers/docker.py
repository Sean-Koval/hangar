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

from hangar import (
    envservices,  # --- envbuilds
    heavy,  # --- heavy
)
from hangar.config import Settings
from hangar.envd import OFF, SKIP, DockerEnvd, Off  # --- envd
from hangar.envd import PORT as ENVD_PORT  # --- envd
from hangar.envspec import BuildContext, EnvSpec
from hangar.harnesses import PACK_ROOT
from hangar.models import ExecResult, Sandbox, SandboxState
from hangar.providers._build import docker_build
from hangar.providers.base import Capabilities, LogFn, OutputTail, Provider, ProviderError, TerminalStream
from hangar.providers.docker_net import EGRESS_NAME, Network, joined, network, network_for  # --- isolation, egress

MAX_OUTPUT = 256 * 1024
LABEL = "dev.hangar.sandbox"
POOL_LABEL = "dev.hangar.pool"
SYSTEM_DIRS = ("/etc/", "/usr/", "/opt/", "/root/", "/var/", "/bin/", "/sbin/", "/lib")
# --- isolation: the PATH of a command run as anyone but the sandbox user. Docker resolves a command with the image's
# PATH, which starts with directories the sandbox user owns (~/.local/bin): a `timeout` planted there would run as root.
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

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


# --- overnight/sandbox-security review: bounded reads from the sandbox
def _text(t: OutputTail[bytes]) -> tuple[str, bool]:
    s, cut = _truncate(t.value().decode(errors="replace"))
    if t.cut and not cut:
        return "[... truncated ...]\n" + s, True
    return s, cut


class _Chunks(io.RawIOBase):
    """A file over get_archive's chunk generator, so tarfile reads the stream as it needs it."""

    def __init__(self, chunks):
        self._it, self._buf = iter(chunks), b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self._buf:
            self._buf = next(self._it, b"")
            if not self._buf:
                return 0
        n = min(len(b), len(self._buf))
        b[:n], self._buf = self._buf[:n], self._buf[n:]
        return n


def _first_file(stream, limit: int | None) -> bytes | None:
    """The first regular file in a get_archive stream, reading no more of it than `limit` + 1 bytes."""
    try:
        with tarfile.open(fileobj=_Chunks(stream), mode="r|") as tf:
            for m in tf:
                if m.isfile():
                    f = tf.extractfile(m)
                    if f is None:
                        return None
                    return f.read() if limit is None else f.read(limit + 1)
        return None
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()


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


def _supports_image_mounts(client) -> bool:
    """`--mount type=image` needs Engine API 1.48+ (Docker 28) and the containerd image store."""
    try:
        api = tuple(int(x) for x in client.version().get("ApiVersion", "0.0").split("."))
        status = dict((k, v) for k, v in (client.info().get("DriverStatus") or []) if isinstance(k, str))
        return api >= (1, 48) and "containerd.snapshotter" in status.get("driver-type", "")
    except Exception:  # noqa: BLE001 - fall back to baking harnesses into images
        return False


class DockerProvider(Provider):
    name = "docker"
    envd: DockerEnvd | Off = OFF  # --- envd: set in __init__
    network: Network | None = None  # --- isolation: set in __init__

    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            # perf: one connection per concurrent call (turns waiting to end hold one each); the default pool of 10
            # made every extra call open, and throw away, a new connection.
            self.client = docker.from_env(max_pool_size=max(10, settings.io_pool))
            self.client.ping()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(
                "cannot reach the Docker engine. Start Docker Desktop/OrbStack, or set DOCKER_HOST."
            ) from e
        self._pool_lock = asyncio.Lock()
        self._image_volumes = settings.docker_image_volumes
        if self._image_volumes is None:
            self._image_volumes = _supports_image_mounts(self.client)
        self.envd = DockerEnvd(self, settings)  # --- envd: exec, files and terminals through the in-sandbox daemon
        self.network = Network(self.client, settings)  # --- isolation: sandboxes can't reach each other

    @property
    def capabilities(self) -> Capabilities:
        rt = (self.settings.docker_runtime or "").lower()
        return Capabilities(full_snapshots=True, warm_pools=True, pause=True, microvm="kata" in rt,
                            image_volumes=bool(self._image_volumes))

    # ------------------------------------------------------------------ templates
    async def image_exists(self, tag: str) -> bool:
        try:
            await asyncio.to_thread(self.client.images.get, tag)
            return True
        except ImageNotFound:
            return False

    async def build_template(self, spec: EnvSpec, ctx: BuildContext, tag: str, log: LogFn | None = None) -> str:
        return await docker_build(ctx, tag, log=log)

    def _run_kwargs(self, name: str, image: str, spec_resources, labels: dict[str, str], hostname: str,
                    packs: dict[str, str] | None = None, net: str | None = None,
                    egress_ip: str | None = None) -> dict:
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
        kw["network"] = net or network(self.settings)  # --- isolation: hangar's own network unless one is named
        # workstream F: on Linux, host.docker.internal (the git gateway on the host) needs an explicit mapping
        if "host.docker.internal" in (self.settings.gateway_url or ""):
            kw["extra_hosts"] = {"host.docker.internal": "host-gateway"}
        egress_net = kw["network"] == EGRESS_NAME
        if egress_net:  # --- egress: no route out; hangar relays the git gateway on the network's gateway address
            kw["extra_hosts"] = {"host.docker.internal": egress_ip} if egress_ip else {}
        # --- envd: the daemon's port on loopback too, for hosts that can't route to container IPs (Docker Desktop).
        # Not on the egress network: it publishes nothing (internal), and the host reaches the container's IP.
        if self.envd.enabled and kw["network"] not in ("host", "none") and not egress_net:
            kw["ports"] = {f"{ENVD_PORT}/tcp": ("127.0.0.1", None)}
        if packs:
            kw["mounts"] = [{"Type": "image", "Source": ref, "Target": f"{PACK_ROOT}/{n}", "ReadOnly": True}
                            for n, ref in sorted(packs.items())]
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
            # --- isolation: members started on another network (before hangar had its own) are replaced too
            net = network_for(self.settings, spec)  # --- egress: or since the environment's egress mode changed
            alive = [c for c in pool if c.status in ("running", "created") and joined(c.attrs, net)]
            for c in pool:
                if c not in alive:
                    await asyncio.to_thread(c.remove, force=True)
            want = heavy.warm(self.settings, spec)  # --- heavy: none for a heavy environment unless asked
            for c in alive[want:]:  # scaled down (e.g. a schedule's hours ended)
                await asyncio.to_thread(c.remove, force=True)
            ip = None
            if self.network and want > len(alive):
                ip = await self._ensure_net(net)  # --- isolation, egress
            for _ in range(max(0, want - len(alive))):
                name = f"hangar-pool-{spec.name}-{int(time.time() * 1000) % 10**8}"
                kw = self._run_kwargs(name, image, spec.resources, {POOL_LABEL: image, "dev.hangar.env": spec.name},
                                      hostname=spec.name, packs=spec.packs, net=net, egress_ip=ip)
                heavy.docker_kwargs(kw, spec)  # --- heavy
                await asyncio.to_thread(self.client.containers.run, **kw)

    async def _ensure_net(self, net: str) -> str | None:
        """--- egress: make sure the network exists; for the egress network, its gateway address (for the git
        gateway's host name in the sandbox)."""
        if self.network is None:
            return None
        if net == EGRESS_NAME:
            return await self.network.egress_gateway()
        await self.network.ensure()
        return None

    async def _claim_warm(self, image: str, net: str | None = None):
        async with self._pool_lock:
            pool = await asyncio.to_thread(
                self.client.containers.list,
                filters={"label": f"{POOL_LABEL}={image}", "status": "running", "name": "hangar-pool-"},
            )
            pool = [c for c in pool if c.name.startswith("hangar-pool-")
                    and joined(c.attrs, net or network(self.settings))]  # --- isolation, egress
            if not pool:
                return None
            # Rename inside the lock so two concurrent claims can never take the same container.
            claimed = pool[0]
            await asyncio.to_thread(claimed.rename, f"hangar-claiming-{claimed.id[:12]}")
            return claimed

    # ------------------------------------------------------------------ lifecycle
    async def create(self, sandbox: Sandbox, image: str, spec: EnvSpec | None = None) -> None:
        name = f"hangar-{sandbox.id}"
        net = network_for(self.settings, spec)  # --- egress
        # --- envbuilds: a sandbox with services starts cold, attached to their network before it starts
        warm = await self._claim_warm(image, net) if spec and spec.warm_pool and not envservices.specs(sandbox) \
            else None
        if warm is not None:
            # Labels are immutable in Docker; pool membership is the name prefix + label, so a
            # rename takes the container out of the pool and the store becomes the source of truth.
            try:
                await asyncio.to_thread(warm.rename, name)
            except APIError:
                try:
                    await asyncio.to_thread(warm.remove, force=True)  # don't orphan a half-claimed container
                except APIError:
                    pass
                warm = None
        if warm is not None:
            sandbox.provider_ref = warm.id
            sandbox.info["warm"] = True
            sandbox.info["pool_image"] = image
            return
        labels = {LABEL: sandbox.id, "dev.hangar.env": sandbox.environment}
        ip = await self._ensure_net(net)  # --- isolation, egress
        kw = self._run_kwargs(name, image, sandbox.resources, labels, hostname=sandbox.name,
                              packs=spec.packs if spec else None, net=net, egress_ip=ip)
        heavy.docker_kwargs(kw, spec)  # --- heavy: privileged, /var/lib/docker on a volume of its own
        try:
            if envservices.specs(sandbox):  # --- envbuilds
                c = await envservices.docker_run(self, sandbox, kw)
            else:
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
        self.envd.forget(sandbox)  # --- envd
        if sandbox.info.get("service_specs"):  # --- envbuilds: its services and their network go with it
            await envservices.docker_teardown(self, sandbox)
        try:
            c = await self._container(sandbox)
        except ProviderError:
            return
        await asyncio.to_thread(c.remove, force=True, v=bool(sandbox.info.get("heavy")))  # --- heavy: its volume

    async def pause(self, sandbox: Sandbox) -> None:
        await asyncio.to_thread((await self._container(sandbox)).pause)

    async def resume(self, sandbox: Sandbox) -> None:
        await asyncio.to_thread((await self._container(sandbox)).unpause)

    async def orphans(self, known_refs: set[str]) -> list[dict[str, str]]:
        """Containers hangar created that no active sandbox owns (crashed creates, other state dirs)."""
        import datetime as dt

        out = []
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
        for prefix in ("hangar-sb-", "hangar-claiming-"):
            cs = await asyncio.to_thread(self.client.containers.list, all=True, filters={"name": prefix})
            for c in cs:
                if not c.name.startswith(prefix) or c.id in known_refs or f"name:{c.name}" in known_refs:
                    continue
                created = dt.datetime.fromisoformat(c.attrs.get("Created", "1970-01-01T00:00:00Z")[:19] + "+00:00")
                if created > cutoff:
                    continue  # may belong to a create that is still in flight
                out.append({"ref": c.id, "name": c.name, "status": c.status})
        return out

    async def remove_ref(self, ref: str) -> None:
        try:
            c = await asyncio.to_thread(self.client.containers.get, ref)
            await asyncio.to_thread(c.remove, force=True)
        except NotFound:
            pass

    async def list_refs(self) -> dict[str, SandboxState]:
        cs = await asyncio.to_thread(self.client.containers.list, all=True, filters={"name": "hangar-sb-"})
        return {c.id: _STATE.get(c.status, SandboxState.ERROR) for c in cs}

    async def find_ref(self, sandbox: Sandbox) -> str | None:  # --- reconciler
        """Its own container carries the sandbox label; a claimed warm one only its name."""
        for filters in ({"label": f"{LABEL}={sandbox.id}"}, {"name": f"hangar-{sandbox.id}"}):
            cs = await asyncio.to_thread(self.client.containers.list, all=True, filters=filters)
            for c in cs:
                if c.labels.get(LABEL) == sandbox.id or c.name == f"hangar-{sandbox.id}":
                    return c.id
        return None

    # ------------------------------------------------------------------ I/O
    async def exec(self, sandbox, command, timeout=120, cwd=None, user=None) -> ExecResult:
        if (res := await self.envd.exec(sandbox, command, timeout, cwd, user)) is not None:  # --- envd
            return res
        c = await self._container(sandbox)
        argv = ["timeout", "--kill-after=5", str(int(timeout)), "bash", "-lc", command]
        t0 = time.monotonic()

        def _run() -> tuple[int | None, OutputTail[bytes], OutputTail[bytes]]:
            # Streamed, keeping only the tail (exec_run without a stream holds everything the command prints, and
            # the sandbox decides how much that is). Same calls exec_run makes.
            api = self.client.api
            other = user and user != self.settings.sandbox_user  # --- isolation: root, hangar's user
            # --- harden: Docker starts an exec with umask 0000 (verified, Docker 29.8): what root or hangar's user
            # created without an explicit mode would be the agent's to change
            run = [*argv[:-1], "umask 022; " + command] if other else argv
            eid = api.exec_create(c.id, run, user=user or self.settings.sandbox_user,
                                  workdir=cwd or self.settings.workspace_dir,
                                  environment={"PATH": SYSTEM_PATH} if other else None)["Id"]
            out, err = OutputTail(4 * MAX_OUTPUT, b""), OutputTail(4 * MAX_OUTPUT, b"")
            frames = api.exec_start(eid, stream=True, demux=True)
            try:
                for o, e in frames:
                    if o:
                        out.add(o)
                    if e:
                        err.add(e)
            finally:
                frames.close()
            return api.exec_inspect(eid).get("ExitCode"), out, err

        try:
            exit_code, out_t, err_t = await asyncio.to_thread(_run)
        except APIError as e:
            raise ProviderError(f"exec failed: {e.explanation or e}") from e
        out, t1 = _text(out_t)
        err, t2 = _text(err_t)
        code = exit_code if exit_code is not None else -1
        if code == 124:
            err += f"\n[hangar] command timed out after {timeout}s"
        return ExecResult(exit_code=code, stdout=out, stderr=err,
                          duration_ms=int((time.monotonic() - t0) * 1000), truncated=t1 or t2)

    async def read_file(self, sandbox, path, limit=None) -> bytes | None:
        if (got := await self.envd.read_file(sandbox, path, limit=limit)) is not SKIP:  # --- envd
            return got
        c = await self._container(sandbox)
        try:
            stream, _ = await asyncio.to_thread(c.get_archive, path)
        except NotFound:
            return None
        return await asyncio.to_thread(_first_file, stream, limit)

    async def write_file(self, sandbox, path, data, mode=0o644) -> None:
        # --- envd: system paths keep this provider's root-owned writes
        if not path.startswith(SYSTEM_DIRS) and await self.envd.write_file(sandbox, path, data, mode):
            return
        c = await self._container(sandbox)
        parent = posixpath.dirname(path)
        user = self.settings.sandbox_user
        owned = not path.startswith(SYSTEM_DIRS)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(posixpath.basename(path))
            info.size = len(data)
            info.mode = mode
            info.mtime = int(time.time())
            if owned:
                info.uid = info.gid = 1000
            tf.addfile(info, io.BytesIO(data))
        # perf: the directory usually exists, so try the upload first; a `mkdir` exec costs ~70 ms, the upload ~5.
        try:
            try:
                ok = await asyncio.to_thread(c.put_archive, parent, buf.getvalue())
            except NotFound:
                root = {} if owned else {"environment": {"PATH": SYSTEM_PATH}}  # --- isolation
                mkdir = ["mkdir", "-p", parent] if owned else ["sh", "-c", 'umask 022 && exec mkdir -p "$0"', parent]
                mk = await asyncio.to_thread(c.exec_run, mkdir, user=user if owned else "root", **root)  # harden
                if mk.exit_code != 0:
                    raise ProviderError(f"cannot create {parent}: {mk.output.decode(errors='replace')}") from None
                ok = await asyncio.to_thread(c.put_archive, parent, buf.getvalue())
        except APIError as e:
            raise ProviderError(f"failed to write {path}: {e.explanation or e}") from e
        if not ok:
            raise ProviderError(f"failed to write {path}")

    async def open_terminal(self, sandbox, command, cols=120, rows=32) -> TerminalStream:
        if (term := await self.envd.open_terminal(sandbox, command, cols, rows)) is not None:  # --- envd
            return term
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

    async def delete_snapshot(self, ref: str) -> None:
        await self.delete_image(ref)

    async def delete_image(self, ref: str) -> None:
        try:
            await asyncio.to_thread(self.client.images.remove, ref, force=True)
        except (ImageNotFound, APIError):
            pass

    async def close(self) -> None:
        await self.envd.close()  # --- envd
        self.client.close()
