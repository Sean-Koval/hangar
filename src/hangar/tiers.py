"""Sandbox tiers: an environment (or a create) asks for `fast`, `isolated` or `default`, not for a provider.

- fast: microVMs that restore from a snapshot in tens of milliseconds (the e2b provider on E2B or CubeSandbox).
  A speed preference, so when every fast provider is down or full it falls back to HANGAR_DEFAULT_PROVIDER.
- isolated: hardware or user-space kernel isolation (GKE gVisor or Kata). A security choice, so it only ever uses
  the providers listed for it.
- default: HANGAR_DEFAULT_PROVIDER.

HANGAR_TIERS maps each tier to its providers in order. A provider that answered "full" or "unreachable" at create
(providers.base.Unavailable) is skipped for HANGAR_TIER_COOLDOWN_SECONDS, so a fast tier that is down costs one
failed create, not one per sandbox.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from hangar.providers.base import Unavailable

if TYPE_CHECKING:
    from hangar.fleet import Fleet
    from hangar.models import Sandbox

log = logging.getLogger("hangar.tiers")

TIERS = ("fast", "isolated", "default")


def candidates(fleet: Fleet, tier: str) -> list[str]:
    """The tier's providers, in order, that are loaded and not cooling down after a failure."""
    from hangar.fleet import FleetError

    s = fleet.settings
    if tier not in TIERS:
        raise FleetError(f"unknown tier {tier!r} (fast | isolated | default)")
    names = [s.default_provider] if tier == "default" else list(s.tiers.get(tier) or [])
    if tier == "fast":
        names.append(s.default_provider)
    t = time.monotonic()
    out = [n for n in dict.fromkeys(names) if n in fleet.providers and fleet.tier_down.get(n, 0.0) <= t]
    if not out:
        def why(n: str) -> str:
            if n in fleet.providers:
                return "full or down a moment ago; tried again soon"
            return fleet.provider_errors.get(n) or "not enabled"

        reasons = "; ".join(f"{n}: {why(n)}" for n in dict.fromkeys(names)) or "none configured in HANGAR_TIERS"
        raise FleetError(f"no provider for the {tier} tier is available ({reasons})")
    return out


async def create(fleet: Fleet, tier: str, call: dict[str, Any]) -> Sandbox:
    """fleet.create on the tier's first provider that has room, falling back past one that is full or down."""
    from hangar.fleet import FleetError

    last: FleetError | None = None
    for name in candidates(fleet, tier):
        try:
            return await fleet.create(**{**call, "provider": name})
        except FleetError as e:
            if not isinstance(e.__cause__, Unavailable):
                raise
            fleet.tier_down[name] = time.monotonic() + fleet.settings.tier_cooldown_seconds
            log.warning("tier %s: %s is unavailable (%s); trying the next provider", tier, name, e.__cause__)
            fleet.audit(call.get("actor") or "system", "sandbox.tier_fallback", None, tier=tier, provider=name,
                        error=str(e.__cause__)[:300])
            last = e
    raise FleetError(f"no provider in the {tier} tier could create the sandbox: {last}")
