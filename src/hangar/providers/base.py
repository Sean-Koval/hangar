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


class Capabilities(BaseModel):
    full_snapshots: bool = False  # provider-native memory/filesystem snapshots
    warm_pools: bool = False
    pause: bool = False
    microvm: bool = False  # hardware-virtualized isolation available (Kata/Firecracker)
    local_build: bool = True  # can build env images itself


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

    async def list_refs(self) -> dict[str, SandboxState]:
        """provider_ref -> state for every sandbox this provider manages (for reconciliation)."""
        return {}

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
    async def read_file(self, sandbox: Sandbox, path: str) -> bytes | None: ...

    @abc.abstractmethod
    async def write_file(self, sandbox: Sandbox, path: str, data: bytes, mode: int = 0o644) -> None: ...

    @abc.abstractmethod
    async def open_terminal(self, sandbox: Sandbox, command: list[str], cols: int = 120, rows: int = 32) -> TerminalStream: ...

    # --- snapshots ----------------------------------------------------------
    async def snapshot(self, sandbox: Sandbox, snapshot_id: str) -> str:
        """Provider-native full snapshot. Returns a ref usable by `create` as `image`."""
        raise NotImplementedError

    async def close(self) -> None:
        pass
