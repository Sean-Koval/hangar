"""Server identity and stale tool lists (tooling P3).

`serverInfo.version` is the package version plus a hash of the tool surface (`0.1.0+tools.3f9c0a1b2d4e`), so a
client, a deploy check or a person can tell which tools a server offers.

The claude.ai apps cache `tools/list` for as long as a connector exists and ignore list-changed notifications
(anthropics/claude-code#38324, closed as not planned), so hangar cannot refresh them. It can notice and say so: it
records the surface each caller last listed, and when a caller is clearly working from an older one (it lacks tools
that exist now, the tool it calls has gained arguments, or it calls a retired name), the result carries one plain
line asking to re-add the connector, at most once an hour per caller. A tool that doesn't exist at all gets the
same advice in its error.

Callers are hangar's MCP actors (a token name, or an OAuth client and subject), so clients sharing one token share
a record; the note is advisory. A caller with no record listed before this server started keeping them: it is
compared with the first surface recorded, which its list can be no newer than.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any

import mcp_types as types

NOTE_EVERY_SECONDS = 3600
# Old names still callable but no longer listed (mcp_live.HIDDEN_TOOLS): only a list from before the merge has them.
RETIRED = {"validate_environment": "create_environment(dry_run=true)", "clone_environment": "create_environment",
           "destroy_sandboxes": "destroy_sandbox"}
REFRESH = "To refresh it, remove the hangar connector and add it again."
_SURFACE, _FIRST, _LISTED = "mcp:surface:", "mcp:surface-first", "mcp:listed:"


def surface_hash(tools: list[types.Tool]) -> str:
    """A short hash of everything a client caches from tools/list."""
    raw = json.dumps([t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools], sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def server_version(tools_hash: str) -> str:
    try:
        pkg = package_version("hangar")
    except PackageNotFoundError:
        pkg = "0.0.0"
    return f"{pkg}+tools.{tools_hash}"


def _shape(tools: list[types.Tool]) -> dict[str, list[str]]:
    """What a stale list can lack: tool names and each one's argument names (descriptions don't count)."""
    return {t.name: sorted((t.input_schema or {}).get("properties") or {}) for t in tools}


class Freshness:
    """Per-caller record of the tool surface last listed, and the note for callers on an older one."""

    def __init__(self, store: Any, tools: list[types.Tool], retired: dict[str, str], caller: Callable[[], str]):
        self.store = store
        self.caller = caller
        self.retired = retired  # still-callable old name -> the tool that replaced it
        self.hash = surface_hash(tools)
        self.version = server_version(self.hash)
        self.shape = _shape(tools)
        rec = store.kv_get_json(_SURFACE + self.hash)
        if rec is None:
            rec = {"first_seen": time.time(), "tools": self.shape}
            store.kv_set_json(_SURFACE + self.hash, rec)
        self.since = float(rec["first_seen"])
        self.first = store.kv_get(_FIRST)
        if self.first is None:
            store.kv_set(_FIRST, self.hash)
            self.first = self.hash
        self._surfaces: dict[str, dict[str, list[str]] | None] = {self.hash: self.shape}
        self._listed: dict[str, str | None] = {}  # caller -> hash last listed (None: no record)
        self._noted: dict[str, float] = {}  # caller -> monotonic time of its last note

    def listed(self) -> None:
        """tools/list: this caller now has the current surface."""
        who = self.caller()
        if self._last(who) != self.hash:
            self.store.kv_set_json(_LISTED + who, {"hash": self.hash, "at": time.time()})
        self._listed[who] = self.hash

    def unknown(self, name: str) -> str:
        return (f"Hangar has no tool named '{name}'. If this client lists it, its list of hangar tools is out of "
                "date. " + REFRESH)

    def note(self, name: str) -> str | None:
        """The stale-list line for a call of `name`, or None (fresh, unknown, or already told this hour)."""
        who, t = self.caller(), time.monotonic()
        if t - self._noted.get(who, -NOTE_EVERY_SECONDS) < NOTE_EVERY_SECONDS:
            return None
        why = self._why(who, name)
        if why is None:
            return None
        self._noted[who] = t
        day = datetime.fromtimestamp(self.since, UTC).strftime("%Y-%m-%d")
        return f"Note: hangar's tools changed on {day} and this client's list of them is older ({why}). " + REFRESH

    def _last(self, who: str) -> str | None:
        if who not in self._listed:
            rec = self.store.kv_get_json(_LISTED + who)
            self._listed[who] = rec.get("hash") if isinstance(rec, dict) else None
        return self._listed[who]

    def _why(self, who: str, name: str) -> str | None:
        if name in self.retired:
            return f"{name} is an old name for {self.retired[name]}"
        last = self._last(who) or self.first
        if last == self.hash:
            return None
        if last not in self._surfaces:
            rec = self.store.kv_get_json(_SURFACE + last)
            self._surfaces[last] = rec.get("tools") if isinstance(rec, dict) else None
        had = self._surfaces[last]
        if not had:
            return None  # a surface we have no record of: nothing to compare with
        new = [n for n in self.shape if n not in had]
        if new:
            return f"it lacks {len(new)} tool{'s' if len(new) > 1 else ''}, e.g. {', '.join(new[:3])}"
        gained = [a for a in self.shape.get(name, []) if name in had and a not in had[name]]
        if gained:
            return f"{name} has new arguments: {', '.join(gained[:3])}"
        return None
