"""Image builds via the docker CLI (BuildKit), shared by all providers.

We shell out instead of using docker-py's build API because docker-py drives the legacy
builder, which lacks BuildKit features the rendered Dockerfiles use (RUN heredocs).
`docker build -` reads the whole context as a tar on stdin.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import tempfile
from pathlib import Path

from hangar.envspec import BuildContext
from hangar.providers.base import LogFn, ProviderError


async def _emit(log: LogFn | None, line: str) -> None:
    if log is None:
        return
    r = log(line)
    if inspect.isawaitable(r):
        await r


async def run_cli(args: list[str], stdin: bytes | None = None, log: LogFn | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=None,
    )
    if stdin is not None:
        assert proc.stdin
        proc.stdin.write(stdin)
        await proc.stdin.drain()
        proc.stdin.close()
    out: list[str] = []
    assert proc.stdout
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        out.append(line)
        await _emit(log, line)
    code = await proc.wait()
    if code != 0:
        tail = "\n".join(out[-40:])
        raise ProviderError(f"`{' '.join(args[:3])} ...` failed with exit code {code}:\n{tail}")
    return "\n".join(out)


_BUILDX: bool | None = None


async def _has_buildx() -> bool:
    global _BUILDX
    if _BUILDX is None:
        proc = await asyncio.create_subprocess_exec("docker", "buildx", "version", stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        _BUILDX = await proc.wait() == 0
    return _BUILDX


_DAEMON: bool | None = None


async def docker_available() -> bool:
    """True when the docker CLI is installed *and* can reach a daemon. The control-plane image ships the
    CLI (for single-VM installs) but has no daemon inside a cluster, where builds must use Cloud Build."""
    global _DAEMON
    if _DAEMON is None:
        if not shutil.which("docker"):
            return False
        quiet = asyncio.subprocess.DEVNULL
        proc = await asyncio.create_subprocess_exec("docker", "version", "--format", "{{.Server.Version}}",
                                                    stdout=quiet, stderr=quiet)
        try:
            _DAEMON = await asyncio.wait_for(proc.wait(), 15) == 0
        except TimeoutError:
            proc.kill()
            _DAEMON = False
    return _DAEMON


async def docker_build(ctx: BuildContext, tag: str, log: LogFn | None = None, platform: str | None = None,
                       push: bool = False) -> str:
    if not shutil.which("docker"):
        raise ProviderError("docker CLI not found; install Docker Desktop / OrbStack / docker-ce to build images")
    args = ["docker", "build", "-t", tag]
    if await _has_buildx():
        args.insert(2, "--progress=plain")  # BuildKit; the legacy builder prints plain logs already
    if platform:
        args += ["--platform", platform]
    args.append("-")
    await _emit(log, f"$ {' '.join(args)}")
    await run_cli(args, stdin=ctx.tar(), log=log)
    if push:
        await _emit(log, f"$ docker push {tag}")
        await run_cli(["docker", "push", tag], log=log)
    return tag


async def cloud_build(ctx: BuildContext, tag: str, log: LogFn | None = None, service_account: str | None = None,
                      project: str | None = None) -> str:
    """Build and push `tag` with Cloud Build (`gcloud builds submit`). --- images: shared by the Kubernetes provider
    and `hangar images build`."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "Dockerfile").write_text(ctx.dockerfile)
        for rel, data in ctx.files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        (root / "cloudbuild.yaml").write_text(
            "steps:\n- name: gcr.io/cloud-builders/docker\n  env: ['DOCKER_BUILDKIT=1']\n"
            f"  args: ['build', '-t', '{tag}', '.']\nimages: ['{tag}']\ntimeout: 3600s\n"
            # A user-specified build SA can't write to the default logs bucket.
            + ("options:\n  logging: CLOUD_LOGGING_ONLY\n" if service_account else "")
        )
        args = ["gcloud", "builds", "submit", "--config", str(root / "cloudbuild.yaml")]
        if service_account:
            args.append(f"--service-account=projects/-/serviceAccounts/{service_account}")
        if project:
            args.append(f"--project={project}")
        await run_cli([*args, str(root)], log=log)
    return tag
