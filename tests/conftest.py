"""Shared fixtures: an in-memory fake provider so fleet logic is tested without Docker."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest

from hangar.broker import Broker
from hangar.config import Settings
from hangar.envspec import BuildContext, EnvCatalog
from hangar.fleet import Fleet
from hangar.models import ExecResult, Sandbox, SandboxState
from hangar.providers.base import Capabilities, Provider, TerminalStream
from hangar.store import Store
from hangar.vault import Vault, load_or_create_key

ENVS = Path(__file__).parent / "fixtures" / "envs"


class FakeProvider(Provider):
    """Each sandbox is a temp dir; exec runs bash locally with HOME/workspace remapped into it."""

    name = "fake"

    def __init__(self):
        base = Path.home() / ".cache" / "hangar-tests"  # must not contain /tmp/ or /workspace (path remapping)
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="fake-", dir=base))
        self.boxes: dict[str, Path] = {}
        self.images: set[str] = set()
        self.snaps: dict[str, Path] = {}
        self.created: list[str] = []
        self.packs_mounted: list[dict[str, str]] = []
        self.image_volumes = False

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(full_snapshots=True, warm_pools=False, pause=True, image_volumes=self.image_volumes)

    def _map(self, sb: Sandbox, path: str) -> Path:
        box = self.boxes[sb.provider_ref]
        return box / path.lstrip("/")

    async def image_exists(self, tag):
        return tag in self.images

    async def build_template(self, spec, ctx: BuildContext, tag, log=None):
        if log:
            r = log(f"fake build {tag}")
            if asyncio.iscoroutine(r):
                await r
        self.images.add(tag)
        return tag

    async def create(self, sandbox, image, spec=None):
        box = self.root / sandbox.id
        if image in self.snaps:
            box.mkdir(parents=True)
            subprocess.run(["cp", "-a", f"{self.snaps[image]}/.", str(box)], check=True)
        else:
            for d in ("home/agent/.hangar", "workspace", "tmp"):
                (box / d).mkdir(parents=True, exist_ok=True)
            (box / "home/agent/.hangar/env").touch()
        sandbox.provider_ref = sandbox.id
        self.boxes[sandbox.id] = box
        self.created.append(image)
        self.packs_mounted.append(dict(spec.packs) if spec else {})

    async def status(self, sandbox):
        return SandboxState.RUNNING if sandbox.provider_ref in self.boxes else SandboxState.STOPPED

    async def destroy(self, sandbox):
        self.boxes.pop(sandbox.provider_ref or "", None)

    async def pause(self, sandbox):
        pass

    async def resume(self, sandbox):
        pass

    async def exec(self, sandbox, command, timeout=120, cwd=None, user=None):
        box = self.boxes[sandbox.provider_ref]
        # Remap absolute sandbox paths into the box for the handful of commands fleet issues.
        cmd = command.replace("/home/agent", f"{box}/home/agent").replace("/workspace", f"{box}/workspace") \
            .replace("/tmp/", f"{box}/tmp/")
        # TMUX_TMPDIR gives each fake sandbox a private tmux socket. Without it, fleet's
        # `tmux kill-server` (fork scrubbing) hits the real tmux server of whoever runs the tests,
        # e.g. the hangar agent session running this suite inside a sandbox.
        env = {"HOME": f"{box}/home/agent", "PATH": "/usr/bin:/bin:/usr/local/bin", "TMUX_TMPDIR": f"{box}/tmp"}
        p = subprocess.run(["bash", "-c", f"set -a; . {box}/home/agent/.hangar/env; set +a; {cmd}"],
                           capture_output=True, text=True, env=env,
                           cwd=str(self._map(sandbox, cwd)) if cwd and self._map(sandbox, cwd).exists() else str(box))
        return ExecResult(exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr)

    async def read_file(self, sandbox, path):
        p = self._map(sandbox, path)
        return p.read_bytes() if p.is_file() else None

    async def write_file(self, sandbox, path, data, mode=0o644):
        p = self._map(sandbox, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(mode)

    async def open_terminal(self, sandbox, command, cols=120, rows=32) -> TerminalStream:
        raise NotImplementedError

    async def snapshot(self, sandbox, snapshot_id):
        dest = self.root / f"snap-{snapshot_id}"
        subprocess.run(["cp", "-a", str(self.boxes[sandbox.provider_ref]), str(dest)], check=True)
        ref = f"fake-snap:{snapshot_id}"
        self.snaps[ref] = dest
        return ref


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=tmp_path, env_dirs=[ENVS], default_provider="docker", enabled_providers=[],
                 reaper_interval_seconds=3600)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


@pytest.fixture
def broker(settings, store):
    return Broker(store, Vault(store, load_or_create_key(None, settings.data_dir)))


@pytest.fixture
async def fleet(settings, store, broker):
    f = Fleet(settings, store, broker, EnvCatalog([ENVS]))
    fake = FakeProvider()
    f.providers["docker"] = fake  # registered under the default provider name
    yield f
