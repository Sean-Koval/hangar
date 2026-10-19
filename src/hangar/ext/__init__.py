"""hangar's plugin surface, `hangar.plugin/v1` (docs/plugins.md).

A plugin is a Python package that declares one entry point in the `hangar.plugins` group. hangar reads the group
once at startup (pluginhost.py), validates each enabled plugin's settings from `HANGAR_PLUGINS`, and calls its
`register(host, settings)`, which adds doors, notifiers, trackers or deliverers through the host. Nothing outside
this package and the protocols it names is a stable interface.

    # pyproject.toml of a plugin package
    [project.entry-points."hangar.plugins"]
    acme-bump = "hangar_acme.bump:PLUGIN"

Installed is not enabled: an outside plugin loads only when `HANGAR_PLUGINS` names it in `enable`. A bad
configuration (an unknown plugin, settings that don't validate, another major version of this API) refuses to
start the server, as a bad `HANGAR_JIRA` does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from hangar.ext.doors import Door, Reply
from hangar.ext.notify import Action, Event, Notifier
from hangar.ext.trackers import Tracker

if TYPE_CHECKING:
    from hangar.broker import Broker
    from hangar.config import Settings
    from hangar.jobs import JobService
    from hangar.store import Store

API = "hangar.plugin/v1"
GROUP = "hangar.plugins"  # the one entry-point group

Check = dict[str, Any]  # a doctor check: {"name", "status": ok|warn|fail, "detail", "fix"} (doctor._check)


class NoSettings(BaseModel):
    """For a plugin that takes none: any key under its name in HANGAR_PLUGINS is refused."""

    model_config = {"extra": "forbid"}


@runtime_checkable
class Plugin(Protocol):
    name: str
    api: Literal["hangar.plugin/v1"]
    Settings: type[BaseModel]  # validated at startup; a bad config refuses to start

    def register(self, host: PluginHost, settings: BaseModel) -> None: ...


class PluginHost(Protocol):
    """What `register` is handed (pluginhost.Host implements it)."""

    jobs: JobService
    store: Store
    broker: Broker
    settings: Settings

    def add_door(self, d: Door) -> None: ...

    def add_notifier(self, n: Notifier) -> None: ...

    def add_tracker(self, scheme: str, t: Tracker) -> None: ...  # intake.TRACKERS[scheme]

    def add_deliverer(self, d: Any) -> None: ...  # ext.deliverers.Deliverer: keyed by its `kind`


__all__ = ["API", "GROUP", "Action", "Check", "Door", "Event", "NoSettings", "Notifier", "Plugin", "PluginHost",
           "Reply", "Tracker"]
