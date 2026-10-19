"""`command`: a deterministic step in the deploy repo's checkout (no agent).

    apply: {kind: command, run: "./bump.sh dev {code.merge_sha}"}

As a `ready` step it is ready at once. As `apply` it runs `run` in the checkout, each placeholder shell-quoted; a
non-zero exit fails the step with the command's last output."""

from __future__ import annotations

from typing import Any

from hangar.ext.deliverers import DeliverCtx, DeliverError, Ready

MAX_RUN = 2000


class Command:
    kind = "command"

    async def ready(self, ctx: DeliverCtx) -> Ready | None:
        return Ready(note="no ready check")

    async def apply(self, ctx: DeliverCtx, ready: Ready) -> None:
        run = ctx.spec.get("run")
        if not isinstance(run, str) or not run.strip() or len(run) > MAX_RUN or "\n" in run.strip():
            raise DeliverError(f"command: `run` must be one command line of at most {MAX_RUN} characters")
        if ctx.exec is None:
            raise DeliverError("command: apply needs the deploy repo's checkout")
        cmd = ctx.render(run.strip(), ready, quote=True)
        res = await ctx.exec(cmd)
        if res.exit_code != 0:
            tail = ((res.stdout or "") + (res.stderr or ""))[-600:].strip()
            raise DeliverError(f"command `{cmd[:200]}` exited {res.exit_code}: {tail}")


class _Plugin:
    """Built in (pyproject `hangar.plugins`, pluginhost.BUILTINS): registers the `command` deliverer."""

    name = "command"
    api = "hangar.plugin/v1"

    def __init__(self, d: Any):
        from hangar.ext import NoSettings

        self.Settings = NoSettings
        self.d = d

    def register(self, host: Any, settings: Any) -> None:
        host.add_deliverer(self.d)


INSTANCE = Command()
PLUGIN = _Plugin(INSTANCE)
