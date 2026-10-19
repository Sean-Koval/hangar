"""Deliverers (`hangar.plugin/v1`): what happens to a change set's deploy role once the repos before it have merged
(changesets.py, docs/changesets.md).

A profile's `deploy:` block names two of them by `kind`:

    deploy:
      role: deploy
      after: [database, infra, code]
      ready: {kind: image-ready, image: "registry.example.com/payments-bot", tag: "{code.merge_sha}"}
      apply: {kind: command, run: "./bump.sh dev {code.merge_sha}"}

* `ready(ctx)` is polled by the control plane with no sandbox (`ctx.exec` is None): it answers `Ready` when the step
  may run (the image the code repo's CI builds exists, a check passed), or None for "not yet".
* `apply(ctx, ready)` runs once, in a fresh sandbox holding the deploy repo's checkout on a new branch: it edits
  `ctx.checkout` through `ctx.exec` only. Hangar commits what it changed, pushes it with a delivery grant pinned to
  that branch and commit, and opens the PR under the deploy repo's own policy. It never runs an agent.

Built-ins (`hangar.deliverers`): `command` (ready at once; apply runs `run` in the checkout) and `image-ready` (ready
when `image:tag` is in its registry). A company plugin adds its own kind (an Argo CD bumper, say) with
a plugin's `register` (`host.add_deliverer`, which writes `DELIVERERS`, the store change sets look kinds up in);
`add_deliverer` here registers one without the plugin host.
Templates in a step's settings take `{<role>.merge_sha}`, `{<role>.pr}`, `{<role>.repo}`, `{changeset}`,
`{ticket}`, `{ticket_key}`, `{environment}` and, in `apply`, `{ready.<key>}` (`ctx.render`).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from hangar.models import ChangeSet, ExecResult

API = "hangar.plugin/v1"
_KIND = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_PLACEHOLDER = re.compile(r"\{([a-z0-9_-]+)(?:\.([a-z0-9_]+))?\}")


class DeliverError(RuntimeError):
    """A step that can't be run as configured, or that failed; the message is shown on the change set."""


@dataclass
class Merged:
    """A role's merged PR."""

    repo: str
    pr: int | None
    merge_sha: str | None


@dataclass
class Ready:
    """A ready step: `detail` is passed on to `apply` (and to its templates as `{ready.<key>}`)."""

    detail: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass
class DeliverCtx:
    changeset: ChangeSet
    role: str  # the deploy role
    merged: dict[str, Merged]  # every changed role, merged
    environment: str | None  # the profile's deploy environment (`environments: [dev]`)
    spec: dict[str, Any]  # this step's block from the profile (`kind` and its own settings)
    checkout: str | None = None  # apply only: the deploy repo's working copy
    exec: Callable[[str], Awaitable[ExecResult]] | None = None  # apply only: runs in `checkout`, in the sandbox
    audit: Callable[..., None] = lambda action, **detail: None
    note: Callable[[str], None] = lambda text: None  # a line on the change set's parent job

    def values(self, ready: Ready | None = None) -> dict[str, str]:
        cs = self.changeset
        out = {"changeset": cs.id, "ticket": cs.ticket or "", "ticket_key": ticket_key(cs.ticket) or "",
               "environment": self.environment or ""}
        for role, m in self.merged.items():
            out[f"{role}.merge_sha"] = m.merge_sha or ""
            out[f"{role}.pr"] = str(m.pr or "")
            out[f"{role}.repo"] = m.repo
        for k, v in (ready.detail if ready else {}).items():
            if isinstance(v, str | int):
                out[f"ready.{k}"] = str(v)
        return out

    def render(self, template: str, ready: Ready | None = None, quote: bool = False) -> str:
        """`template` with its placeholders filled; `quote` shell-quotes each value (commands). An unknown
        placeholder, or one with no value yet, is an error: a step never runs with a hole in it."""
        values = self.values(ready)

        def sub(m: re.Match[str]) -> str:
            key = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
            v = values.get(key)
            if not v:
                raise DeliverError(f"{{{key}}} has no value here (known: {', '.join(sorted(values))})")
            return shlex.quote(v) if quote else v

        return _PLACEHOLDER.sub(sub, template)


@runtime_checkable
class Deliverer(Protocol):
    kind: str

    async def ready(self, ctx: DeliverCtx) -> Ready | None: ...

    async def apply(self, ctx: DeliverCtx, ready: Ready) -> None: ...


DELIVERERS: dict[str, Deliverer] = {}


def add_deliverer(d: Deliverer) -> None:
    """Register a deliverer by its kind (a plugin's `register`, or the built-ins). A kind taken by another object is
    refused: a plugin can't replace a step another profile relies on by accident."""
    kind = getattr(d, "kind", None)
    if not isinstance(kind, str) or not _KIND.match(kind):
        raise ValueError(f"a deliverer's kind must match {_KIND.pattern}: {kind!r}")
    if not isinstance(d, Deliverer):
        raise TypeError(f"deliverer {kind} needs async ready(ctx) and apply(ctx, ready)")
    if (old := DELIVERERS.get(kind)) is not None and old is not d:
        raise ValueError(f"a deliverer of kind {kind!r} is already registered ({type(old).__name__})")
    DELIVERERS[kind] = d


def get(kind: str) -> Deliverer:
    from hangar import deliverers  # noqa: F401 - the built-ins register on import

    try:
        return DELIVERERS[kind]
    except KeyError:
        raise DeliverError(f"no deliverer of kind {kind!r} (registered: {', '.join(sorted(DELIVERERS))})") from None


def ticket_key(ticket: str | None) -> str | None:
    """`jira:PAY-412` -> `PAY-412`."""
    if not ticket:
        return None
    return ticket.split(":", 1)[1] if ":" in ticket else ticket
