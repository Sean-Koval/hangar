"""The plugin host: discover, configure and register `hangar.plugins` entry points (docs/plugins.md).

Not plugins.py, which is about Claude Code plugins seeded into sandbox images.

* **Discovery.** `importlib.metadata.entry_points(group="hangar.plugins")`, once, when the app is built.
* **Built-ins.** hangar declares its own (BUILTINS) in its pyproject and they are always loaded: the Slack, Jira
  and GitHub doors still answer "not configured" until their settings are there, as before, and settings changed
  at runtime (`hangar setup jira`) keep working without a restart. The `webhook` notifier loads when
  `HANGAR_PLUGINS` configures it.
* **Outside plugins.** Installed is not enabled: one loads only when `HANGAR_PLUGINS.enable` names it.
* **Settings.** `HANGAR_PLUGINS` (JSON) holds each plugin's settings under its name, in the section that says
  what it adds (`doors`, `notifiers`, `trackers`, `deliverers`) or in `settings`. Secrets are vault credential
  names, never values. A plugin that isn't installed, settings that don't validate, an unknown section or
  another major API version raise PluginConfigError, and the server refuses to start.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from importlib import metadata
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from hangar.ext import API, GROUP, Door, NoSettings, Notifier, Tracker

if TYPE_CHECKING:
    from hangar.app import Hangar

log = logging.getLogger("hangar.pluginhost")

# name -> entry point value. Also declared in pyproject.toml; listed here so a source checkout without installed
# metadata loads the same set. A built-in whose module isn't there (yet) is skipped and shown by the doctor.
BUILTINS: dict[str, str] = {
    "github": "hangar.app:GITHUB_PLUGIN",
    "slack": "hangar.slack:PLUGIN",
    "jira": "hangar.jira:PLUGIN",
    "webhook": "hangar.events_out:PLUGIN",
    "command": "hangar.deliverers.command:PLUGIN",
    "image-ready": "hangar.deliverers.image_ready:PLUGIN",
}
OPTIONAL_BUILTINS = {"webhook"}  # load only when HANGAR_PLUGINS configures them
SECTIONS = ("doors", "notifiers", "trackers", "deliverers", "settings")
KEYS = ("enable", *SECTIONS)


class PluginConfigError(ValueError):
    """HANGAR_PLUGINS or a plugin is unusable: the server doesn't start."""


@dataclass
class Loaded:
    name: str
    version: str | None
    builtin: bool
    plugin: Any = None
    adds: list[str] = field(default_factory=list)  # "door:slack", "tracker:jira", "notifier:webhook"
    error: str | None = None  # a built-in that couldn't be imported

    def view(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "builtin": self.builtin, "api": API,
                "adds": self.adds, **({"error": self.error} if self.error else {})}


def entry_points() -> list[metadata.EntryPoint]:
    """The installed `hangar.plugins` entry points (tests replace this)."""
    return list(metadata.entry_points(group=GROUP))


def _version(ep: metadata.EntryPoint) -> str | None:
    dist = getattr(ep, "dist", None)
    return getattr(dist, "version", None) if dist is not None else None


def _major(api: Any) -> str | None:
    s = str(api or "")
    return s.rsplit("/", 1)[-1].split(".", 1)[0] if s.startswith("hangar.plugin/") else None


class Host:
    """Implements ext.PluginHost over the existing registries (jobs.NOTIFIERS, intake.TRACKERS)."""

    def __init__(self, h: Hangar):
        self.h = h
        self.jobs = h.jobs
        self.store = h.store
        self.broker = h.broker
        self.settings = h.settings
        self.doors: list[Door] = []
        self.notifiers: list[Notifier] = []
        self.deliverers: dict[str, Any] = {}
        self.trackers: dict[str, Tracker] = {}
        self.loaded: list[Loaded] = []
        self._current: Loaded | None = None

    # ------------------------------------------------------------------ PluginHost
    def _added(self, what: str) -> None:
        if self._current is not None:
            self._current.adds.append(what)

    def add_door(self, d: Door) -> None:
        from hangar.jobs import NOTIFIERS

        name = str(getattr(d, "name", "") or "")
        if not name or not callable(getattr(d, "mount", None)):
            raise PluginConfigError(f"plugin {self._who()}: a door needs a name and mount(app)")
        if any(x.name == name for x in self.doors):
            raise PluginConfigError(f"plugin {self._who()}: there is already a door named '{name}'")
        reply = getattr(d, "reply", None)
        if reply is not None:
            if name == "github":
                raise PluginConfigError("the 'github' reply scheme is hangar's own (PR and issue comments)")
            if NOTIFIERS.get(name) not in (None, reply):
                raise PluginConfigError(f"plugin {self._who()}: '{name}' threads already reply through another door")
            NOTIFIERS[name] = reply
        self.doors.append(d)
        self._added(f"door:{name}")

    def add_notifier(self, n: Notifier) -> None:
        name = str(getattr(n, "name", "") or "")
        if not name or not callable(getattr(n, "send", None)) or not callable(getattr(n, "wants", None)):
            raise PluginConfigError(f"plugin {self._who()}: a notifier needs a name, wants() and send()")
        if any(x.name == name for x in self.notifiers):
            raise PluginConfigError(f"plugin {self._who()}: there is already a notifier named '{name}'")
        self.notifiers.append(n)
        self._added(f"notifier:{name}")

    def add_tracker(self, scheme: str, t: Tracker) -> None:
        from hangar import intake

        if not scheme or ":" in scheme or not callable(t):
            raise PluginConfigError(f"plugin {self._who()}: a tracker needs a scheme (no ':') and a callable")
        if self.trackers.get(scheme) not in (None, t) or intake.TRACKERS.get(scheme) not in (None, t):
            raise PluginConfigError(f"plugin {self._who()}: there is already a '{scheme}' tracker")
        intake.TRACKERS[scheme] = self.trackers[scheme] = t
        self._added(f"tracker:{scheme}")

    def add_deliverer(self, d: Any) -> None:
        kind = str(getattr(d, "kind", "") or "")
        if not kind or not callable(getattr(d, "ready", None)) or not callable(getattr(d, "apply", None)):
            raise PluginConfigError(f"plugin {self._who()}: a deliverer needs a kind, ready() and apply()")
        if kind in self.deliverers and self.deliverers[kind] is not d:
            raise PluginConfigError(f"plugin {self._who()}: there is already a '{kind}' deliverer")
        self.deliverers[kind] = d
        from hangar.ext import deliverers as ext_deliverers  # --- change sets look a profile's kinds up there

        ext_deliverers.DELIVERERS[kind] = d
        self._added(f"deliverer:{kind}")

    def _who(self) -> str:
        return self._current.name if self._current is not None else "?"

    # ------------------------------------------------------------------ loading
    def load(self, config: dict[str, Any] | None = None, eps: list[metadata.EntryPoint] | None = None) -> Host:
        cfg = dict(config if config is not None else (self.settings.plugins or {}))
        unknown = sorted(set(cfg) - set(KEYS))
        if unknown:
            raise PluginConfigError(f"HANGAR_PLUGINS: unknown key(s) {', '.join(unknown)} "
                                    f"(expected {', '.join(KEYS)})")
        enable = cfg.get("enable") or []
        if not isinstance(enable, list) or not all(isinstance(x, str) for x in enable):
            raise PluginConfigError("HANGAR_PLUGINS.enable must be a list of plugin names")
        settings: dict[str, dict[str, Any]] = {}
        for section in SECTIONS:
            block = cfg.get(section) or {}
            if not isinstance(block, dict):
                raise PluginConfigError(f"HANGAR_PLUGINS.{section} must be an object of plugin name -> settings")
            for name, value in block.items():
                if name in settings:
                    raise PluginConfigError(f"HANGAR_PLUGINS: '{name}' is configured in two sections")
                if not isinstance(value, dict):
                    raise PluginConfigError(f"HANGAR_PLUGINS.{section}.{name} must be an object")
                settings[name] = value

        found: dict[str, metadata.EntryPoint] = {}
        for ep in (eps if eps is not None else entry_points()):
            if ep.name in found and found[ep.name].value != ep.value:
                raise PluginConfigError(f"two installed plugins are named '{ep.name}' ({found[ep.name].value}, "
                                        f"{ep.value}); uninstall one")
            found.setdefault(ep.name, ep)
        for name, value in BUILTINS.items():  # a source checkout without installed metadata
            found.setdefault(name, metadata.EntryPoint(name=name, value=value, group=GROUP))

        for name in [*enable, *settings]:
            if name not in found:
                raise PluginConfigError(f"HANGAR_PLUGINS names plugin '{name}', which is not installed "
                                        f"(installed: {', '.join(sorted(found))})")
        for name in settings:
            if name not in BUILTINS and name not in enable:
                raise PluginConfigError(f"HANGAR_PLUGINS configures '{name}' but doesn't enable it "
                                        "(add it to `enable`: installed is not enabled)")

        order = [n for n in BUILTINS if n not in OPTIONAL_BUILTINS or n in settings or n in enable]
        order += [n for n in enable if n not in order]
        for name in order:
            ep = found[name]
            builtin = name in BUILTINS and ep.value == BUILTINS[name]
            rec = Loaded(name, _version(ep), builtin)
            try:
                obj = ep.load()
            except ModuleNotFoundError as e:
                if builtin and e.name and BUILTINS[name].split(":", 1)[0].startswith(e.name):
                    rec.error = f"not available in this build ({e.name})"
                    self.loaded.append(rec)
                    continue
                raise PluginConfigError(f"plugin '{name}' could not be imported: {e}") from e
            except Exception as e:  # noqa: BLE001 - any import failure refuses to start
                raise PluginConfigError(f"plugin '{name}' could not be imported: {e}") from e
            plugin = obj() if inspect.isclass(obj) else obj
            self._register(rec, plugin, settings.get(name, {}))
        return self

    def _register(self, rec: Loaded, plugin: Any, raw: dict[str, Any]) -> None:
        name = rec.name
        if not callable(getattr(plugin, "register", None)):
            raise PluginConfigError(f"plugin '{name}' has no register(host, settings)")
        major = _major(getattr(plugin, "api", None))
        if major != _major(API):
            raise PluginConfigError(f"plugin '{name}' targets {getattr(plugin, 'api', None)!r}; this hangar "
                                    f"speaks {API}")
        model = getattr(plugin, "Settings", None) or NoSettings
        if not (inspect.isclass(model) and issubclass(model, BaseModel)):
            raise PluginConfigError(f"plugin '{name}': Settings must be a pydantic model")
        try:
            settings = model.model_validate(raw)
        except ValidationError as e:
            errs = "; ".join(f"{'.'.join(str(p) for p in x['loc']) or '(top)'}: {x['msg']}" for x in e.errors())
            raise PluginConfigError(f"HANGAR_PLUGINS settings for '{name}' are invalid: {errs}") from None
        rec.plugin = plugin
        self._current = rec
        try:
            plugin.register(self, settings)
        except PluginConfigError:
            raise
        except Exception as e:  # noqa: BLE001 - a plugin that can't register refuses to start
            raise PluginConfigError(f"plugin '{name}' failed to register: {e}") from e
        finally:
            self._current = None
        self.loaded.append(rec)

    # ------------------------------------------------------------------ the app and the doctor
    def mount(self, app: Any) -> None:
        for d in self.doors:
            d.mount(app)

    def views(self) -> list[dict[str, Any]]:
        return [x.view() for x in self.loaded]

    async def doctor_checks(self) -> list[dict[str, Any]]:
        from hangar.doctor import _check

        out = []
        for rec in self.loaded:
            if rec.error:
                out.append(_check(f"plugin {rec.name}", None, rec.error))
                continue
            what = ", ".join(rec.adds) or "nothing"
            ver = f" {rec.version}" if rec.version else ""
            out.append(_check(f"plugin {rec.name}", True, f"{'built-in' if rec.builtin else 'plugin'}{ver}: {what}"))
        for d in [*self.doors, *self.notifiers]:
            kind = "door" if d in self.doors else "notifier"
            if kind == "door" and any(r.builtin and f"door:{d.name}" in r.adds for r in self.loaded):
                continue  # the built-in doors' checks are hangar doctor's own (slack.py, jira.py)
            if not callable(getattr(d, "doctor", None)):
                continue
            try:
                res = d.doctor()
                if inspect.isawaitable(res):
                    res = await res
                out += list(res or [])
            except Exception as e:  # noqa: BLE001 - a door's doctor must not break the doctor
                out.append(_check(f"{kind} {d.name}", False, f"its doctor failed: {e}"))
        return out


def of(h: Hangar) -> Host:
    """The Hangar's plugin host, loaded on first use (create_app loads it first, so a bad config fails there)."""
    host = h.__dict__.get("_plugin_host")
    if host is None:
        host = Host(h).load()
        h.__dict__["_plugin_host"] = host
    return host


async def doctor_checks(h: Hangar) -> list[dict[str, Any]]:
    from hangar.doctor import _check

    try:
        return await of(h).doctor_checks()
    except PluginConfigError as e:
        return [_check("plugins", False, str(e), "fix HANGAR_PLUGINS (docs/plugins.md)")]
