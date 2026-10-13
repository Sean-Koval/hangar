"""Provider interface. A provider turns environment images into running sandboxes.

Everything above this layer (terminals, agent runs, credential injection, repo setup,
workspace snapshots) is built on `exec`, `read_file` and `write_file`, so a new backend
only has to implement this small surface.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from typing import ClassVar

from pydantic import BaseModel

from hangar.envspec import BuildContext, EnvSpec
from hangar.models import ExecResult, Sandbox, SandboxState

LogFn = Callable[[str], Awaitable[None] | None]


# --- overnight/sandbox-security review: the sandbox decides how much its commands print and how big its files are
class OutputTail[T: (str, bytes)]:
    """The last `keep` characters (or bytes) of a stream that may never end, so a command that prints without
    stopping can't fill the control plane's memory. `cut` says something was dropped."""

    def __init__(self, keep: int, empty: T) -> None:
        self.keep, self.cut, self._empty = keep, False, empty
        self._parts: list[T] = []
        self._n = 0

    def add(self, chunk: T) -> None:
        self._parts.append(chunk)
        self._n += len(chunk)
        if self._n > 2 * self.keep:
            last = self._empty.join(self._parts)[-self.keep:]
            self._parts, self._n, self.cut = [last], len(last), True

    def value(self) -> T:
        return self._empty.join(self._parts)


class Capabilities(BaseModel):
    full_snapshots: bool = False  # provider-native memory/filesystem snapshots
    warm_pools: bool = False
    pause: bool = False
    microvm: bool = False  # hardware-virtualized isolation available (Kata/Firecracker)
    local_build: bool = True  # can build env images itself
    image_volumes: bool = False  # can mount harness packs (read-only OCI images) into sandboxes


class TerminalStream(abc.ABC):
    """A bidirectional PTY into a sandbox, used by the dashboard's live terminal."""

    @abc.abstractmethod
    async def read(self) -> bytes:
        """Return the next chunk of output, or b'' when the stream is closed."""

    @abc.abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    async def resize(self, cols: int, rows: int) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class ProviderError(RuntimeError):
    pass


class Provider(abc.ABC):
    name: ClassVar[str]

    @property
    @abc.abstractmethod
    def capabilities(self) -> Capabilities: ...

    # --- templates ----------------------------------------------------------
    @abc.abstractmethod
    async def build_template(self, spec: EnvSpec, ctx: BuildContext, tag: str, log: LogFn | None = None) -> str:
        """Build (and publish if needed) the environment image. Returns the image reference."""

    async def image_exists(self, tag: str) -> bool:
        return False

    def image_ref(self, tag: str) -> str:
        """What `create` should receive for a built image tag (E2B maps tags to template names)."""
        return tag

    async def sync_warm_pool(self, spec: EnvSpec, image: str) -> None:
        """Bring the warm pool for this environment to spec.warm_pool ready sandboxes."""

    # --- lifecycle ----------------------------------------------------------
    @abc.abstractmethod
    async def create(self, sandbox: Sandbox, image: str, spec: EnvSpec | None = None) -> None:
        """Start the sandbox. Must set sandbox.provider_ref; may claim from a warm pool."""

    @abc.abstractmethod
    async def status(self, sandbox: Sandbox) -> SandboxState: ...

    @abc.abstractmethod
    async def destroy(self, sandbox: Sandbox) -> None: ...

    async def pause(self, sandbox: Sandbox) -> None:
        raise ProviderError(f"{self.name} provider does not support pause")

    async def resume(self, sandbox: Sandbox) -> None:
        raise ProviderError(f"{self.name} provider does not support resume")

    async def list_refs(self) -> dict[str, SandboxState] | None:
        """provider_ref -> state for every sandbox this provider manages (for reconciliation).
        Return None if the provider cannot enumerate its sandboxes."""
        return None

    async def set_expiry(self, sandbox: Sandbox, expires_at: float) -> None:
        """Propagate a TTL change to the backend's own timeout, if it has one."""

    async def explain_pending(self, sandbox: Sandbox) -> str | None:
        """Why a starting sandbox isn't running yet (waiting for a node, quota, pulling the image...), in plain
        words, if the backend can tell. Shown to people while they wait."""
        return None

    # --- I/O ----------------------------------------------------------------
    @abc.abstractmethod
    async def exec(
        self,
        sandbox: Sandbox,
        command: str,
        timeout: int = 120,
        cwd: str | None = None,
        user: str | None = None,
    ) -> ExecResult:
        """Run `command` with `bash -lc` (so ~/.hangar/env is sourced) and wait for it."""

    @abc.abstractmethod
    async def read_file(self, sandbox: Sandbox, path: str, limit: int | None = None) -> bytes | None:
        """The file's bytes, or None if it isn't there. With `limit`, no more than `limit` + 1 bytes are read, so
        a caller can tell a file is over its cap without holding all of it (the sandbox decides its size, and can
        change it between a size check and the read). Hangar's own reads of agent-written files pass one."""

    @abc.abstractmethod
    async def write_file(self, sandbox: Sandbox, path: str, data: bytes, mode: int = 0o644) -> None: ...

    @abc.abstractmethod
    async def open_terminal(
        self, sandbox: Sandbox, command: list[str], cols: int = 120, rows: int = 32
    ) -> TerminalStream: ...

    # --- snapshots ----------------------------------------------------------
    async def snapshot(self, sandbox: Sandbox, snapshot_id: str) -> str:
        """Provider-native full snapshot. Returns a ref usable by `create` as `image`."""
        raise NotImplementedError

    async def delete_snapshot(self, ref: str) -> None:
        """Delete a provider-native snapshot created by `snapshot`."""

    async def close(self) -> None:
        pass
