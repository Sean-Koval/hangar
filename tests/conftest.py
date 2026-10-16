"""Shared fixtures: an in-memory fake provider so fleet logic is tested without Docker."""

from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import socket
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


@pytest.fixture(autouse=True)
def _isolated_cli_config(tmp_path, monkeypatch):
    """Never read or write the developer's real ~/.hangar/cli.json (hangar login) in tests."""
    monkeypatch.setenv("HANGAR_CLI_CONFIG", str(tmp_path / "cli.json"))


@pytest.fixture(autouse=True)
def _no_host_docker(monkeypatch):
    """Only the Docker e2e suites (HANGAR_E2E=1) reach the machine's Docker. A unit test that builds a real app
    (whose default provider is docker) otherwise builds real images wherever Docker runs, as on CI: one such test
    took 46 s there and 1 s on a laptop, and xdist workers (and other runs) share the daemon."""
    if os.environ.get("HANGAR_E2E") != "1":
        monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/hangar-unit-tests-use-no-docker.sock")


@pytest.fixture(autouse=True)
def _intake_posture_off(monkeypatch):
    """--- intake posture (#123): enforce, the default, needs the git gateway and an egress proxy, which the fake
    provider and most tests (and e2e suites) don't set up. Tests about the posture pass `intake_posture` to Settings
    themselves (test_posture.py, test_e2e_egress_docker.py)."""
    monkeypatch.setenv("HANGAR_INTAKE_POSTURE", "off")


_run_root: Path | None = None


def scratch_root() -> Path:
    """Where fake sandboxes and test git remotes live. The fake provider rewrites /home/agent, /workspace and
    /tmp/ inside commands, so this path must contain none of them. Inside a hangar sandbox $HOME *is*
    /home/agent, so fall back to /dev/shm there (override with HANGAR_TEST_ROOT).

    Each pytest process gets its own directory under that base: xdist workers, and other people's runs on the same
    machine, share the base and once collided in it. Kept short: tmux sockets live a few levels below it, and a unix
    socket path can't pass 107 bytes."""
    global _run_root
    if _run_root is None:
        if os.environ.get("HANGAR_TEST_ROOT"):
            base = Path(os.environ["HANGAR_TEST_ROOT"])
        else:
            base = Path.home() / ".cache" / "hangar-tests"
            if any(x in str(base) for x in ("/home/agent", "/workspace", "/tmp/")):
                base = Path("/dev/shm/hangar-tests")
        base.mkdir(parents=True, exist_ok=True)
        _run_root = Path(tempfile.mkdtemp(prefix=f"{os.environ.get('PYTEST_XDIST_WORKER', 'run')}-", dir=base))
        _run_root.chmod(0o755)  # as the base is: tests that drop to another uid (test_driver) reach in
    return _run_root


@pytest.fixture(autouse=True, scope="session")
def _scratch_root_removed():
    """This process's scratch directory goes when its session ends (each test removes what it made inside)."""
    yield
    if _run_root is not None:
        shutil.rmtree(_run_root, ignore_errors=True)


_ports = itertools.count()


def free_port() -> int:
    """A port for a test server that nothing else will take before the server binds it: below the kernel's ephemeral
    range (where an outgoing connection, or another worker's bind to port 0, could grab it in the meantime), in a
    block of its own per xdist worker, and checked free on this machine (another run may be using the block)."""
    try:
        low = int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        low = 32768
    worker = int(os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw") or 0)
    size = 256
    first = max(1024, low - size * (worker + 1))  # worker blocks stack down from just under the range
    start = (os.getpid() * 7) % size  # two runs on one machine start at different places in the block
    for _ in range(size):
        port = first + (start + next(_ports)) % size
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {first}-{first + size - 1}")


class FakeProvider(Provider):
    """Each sandbox is a temp dir; exec runs bash locally with HOME/workspace remapped into it."""

    name = "fake"
    made: list[FakeProvider] = []  # this test's providers, cleaned up after it (_fake_sandboxes_cleaned_up)

    def __init__(self):
        base = scratch_root()
        self.root = Path(tempfile.mkdtemp(prefix="fake-", dir=base))
        FakeProvider.made.append(self)
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
        # Asynchronous, like a real provider's exec (a worker thread): a command that waits (a turn's end, a
        # clone) must not freeze the event loop and everything else under test.
        p = await asyncio.create_subprocess_exec(
            "bash", "-c", f"set -a; . {box}/home/agent/.hangar/env; set +a; {cmd}", env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(self._map(sandbox, cwd)) if cwd and self._map(sandbox, cwd).exists() else str(box))
        try:
            out, err = await p.communicate()
        except BaseException:  # cancelled (a test ending mid-turn): don't leave the process to a closed loop
            if p.returncode is None:
                p.kill()
            await asyncio.shield(p.wait())
            raise
        return ExecResult(exit_code=p.returncode, stdout=out.decode(errors="replace"),
                          stderr=err.decode(errors="replace"))

    async def read_file(self, sandbox, path, limit=None):
        p = self._map(sandbox, path)
        if not p.is_file():
            return None
        with p.open("rb") as f:
            return f.read() if limit is None else f.read(limit + 1)

    async def write_file(self, sandbox, path, data, mode=0o644):
        p = self._map(sandbox, path)
        if path.endswith(".sh"):  # scripts fleet writes (agent runs) name sandbox paths; remap like exec does
            box = str(self.boxes[sandbox.provider_ref]).encode()
            data = data.replace(b"/home/agent", box + b"/home/agent").replace(b"/workspace", box + b"/workspace")
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

    def cleanup(self) -> None:
        """Kill the tmux servers agent runs started (one per box, its socket under the box's tmp/, destroyed boxes
        and snapshot copies included), then remove the boxes. Left running, they outlived the test run, one per
        session, and once filled a shared machine's memory."""
        for tmp in self.root.glob("*/tmp"):
            subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=10,
                           env={"TMUX_TMPDIR": str(tmp), "PATH": "/usr/bin:/bin:/usr/local/bin"})
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _fake_sandboxes_cleaned_up():
    """Every FakeProvider a test made, the `fleet` fixture's and those tests build themselves, once it ends."""
    yield
    while FakeProvider.made:
        FakeProvider.made.pop().cleanup()


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=tmp_path, env_dirs=[ENVS], default_provider="docker", enabled_providers=[],
                 reaper_interval_seconds=3600)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    # --- overnight/cache: siblings start together, as before, unless a test is about holding them back (the
    # scripted agents print nothing until they exit, so a held sibling would wait for the first one to finish)
    s.cache_stagger_seconds = 0
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
