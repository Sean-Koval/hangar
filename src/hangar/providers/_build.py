"""Image builds via the docker CLI (BuildKit), shared by all providers.

We shell out instead of using docker-py's build API because docker-py drives the legacy
builder, which lacks BuildKit features the rendered Dockerfiles use (RUN heredocs).
`docker build -` reads the whole context as a tar on stdin.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil

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


async def docker_build(ctx: BuildContext, tag: str, log: LogFn | None = None, platform: str | None = None,
                       push: bool = False) -> str:
    if not shutil.which("docker"):
        raise ProviderError("docker CLI not found; install Docker Desktop / OrbStack / docker-ce to build images")
    args = ["docker", "build", "--progress=plain", "-t", tag]
    if platform:
        args += ["--platform", platform]
    args.append("-")
    await _emit(log, f"$ {' '.join(args)}")
    await run_cli(args, stdin=ctx.tar(), log=log)
    if push:
        await _emit(log, f"$ docker push {tag}")
        await run_cli(["docker", "push", tag], log=log)
    return tag
