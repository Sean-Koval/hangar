"""The Docker network sandboxes run on (workstream isolation; docs/security.md).

On Docker's default bridge, and on any bridge network created without options, containers reach every port of each
other: hangar-envd (49983), dev servers, whatever an agent listens on. Hangar puts sandboxes on a network of its own,
created with inter-container connectivity off (`com.docker.network.bridge.enable_icc=false`). Checked on Docker
29.8: one sandbox can't reach another's ports; the host still reaches a sandbox at its IP and at a port published on
127.0.0.1 (how hangar reaches envd); sandboxes still reach the internet and the host (the git gateway at
host.docker.internal). A container *attached* to that network can't reach the sandboxes either, so a control plane
running in a container can't join it to reach envd: it uses `docker exec` instead (docs/envd.md).

The bridge has a fixed name, `hangar0`, so a host firewall rule can drop what sandboxes send to link-local addresses
(the cloud metadata server), which Docker has no option for: `iptables -I DOCKER-USER -i hangar0 -d 169.254.0.0/16
-p tcp ! --dport 53 -j DROP` (the single-VM install adds it). A network by hangar's name that isn't isolating, or isn't
on that bridge, is refused rather than used.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from docker.errors import APIError, NotFound

from hangar.config import Settings
from hangar.providers.base import ProviderError

log = logging.getLogger("hangar.docker")

NAME = "hangar-sandboxes"
BRIDGE = "hangar0"
ICC = "com.docker.network.bridge.enable_icc"
BRIDGE_NAME = "com.docker.network.bridge.name"
OPTIONS = {ICC: "false", BRIDGE_NAME: BRIDGE}
LABEL = "dev.hangar.network"
# --- egress: sandboxes of an `allowlist`/`none` environment. Internal (Docker sets up no route out of it; its docs:
# "communication with the gateway IP address (and thus appropriately configured host services) is possible"), no
# traffic between sandboxes, a bridge name of its own. Hangar's egress proxy, and a relay to the git gateway, listen
# on its gateway address; DNS on an internal network answers container names only (Docker 26+, CVE-2024-29018).
EGRESS_NAME = "hangar-egress"
EGRESS_BRIDGE = "hangar1"
EGRESS_OPTIONS = {ICC: "false", BRIDGE_NAME: EGRESS_BRIDGE}


def network(settings: Settings) -> str:
    """Where sandboxes go: HANGAR_DOCKER_NETWORK as it is, else hangar's own isolated network (`bridge` is Docker's
    default bridge, with no isolation between sandboxes)."""
    return settings.docker_network or NAME


def network_for(settings: Settings, spec: Any) -> str:
    """--- egress: the network for an environment's sandboxes: the egress network when its mode is proxied."""
    from hangar import egress

    return EGRESS_NAME if spec is not None and egress.proxied(settings, spec) else network(settings)


def _egress_ok(attrs: dict[str, Any]) -> bool:
    return isolates(attrs) and bool(attrs.get("Internal")) and \
        (attrs.get("Options") or {}).get(BRIDGE_NAME) == EGRESS_BRIDGE


def _check_internal_dns(api: Any) -> None:
    """--- review(egress): refuse an engine whose internal networks forward DNS to the host's resolver
    (CVE-2024-29018, fixed in Moby 26.0.0, 25.0.4 and 23.0.11): a sandbox there could send data out in the names it
    looks up, whatever the proxy allows."""
    v = ""
    try:
        v = str(api.version().get("Version") or "")
        major, minor, patch = (int(x) for x in v.split("-")[0].split(".")[:3])
    except Exception:  # noqa: BLE001 - unreadable: don't assume it is fixed
        major = minor = patch = -1
    if major >= 26 or (major, minor) == (25, 0) and patch >= 4 or (major, minor) == (23, 0) and patch >= 11:
        return
    raise ProviderError(f"Docker {v or '(unknown version)'} forwards DNS from internal networks to the internet "
                        "(CVE-2024-29018), so egress allowlists can't be enforced on it; upgrade the engine to 26 or "
                        "later (or 25.0.4, 23.0.11)")


def gateway_ip(attrs: dict[str, Any]) -> str | None:
    """A network's gateway address (its IPv4 one when it has several), from `docker network inspect`."""
    cfgs = (attrs.get("IPAM") or {}).get("Config") or []
    gws = [c.get("Gateway") for c in cfgs if c.get("Gateway")]
    return next((g for g in gws if ":" not in g), None) or next(iter(gws), None)


def isolates(attrs: dict[str, Any]) -> bool:
    """Whether a network (`docker network inspect`) keeps its containers from reaching each other."""
    return attrs.get("Driver") == "bridge" and (attrs.get("Options") or {}).get(ICC) == "false"


def joined(attrs: dict[str, Any], name: str) -> bool:
    """Whether a container (`docker ps`/inspect attributes) is on network `name`."""
    return name in ((attrs.get("NetworkSettings") or {}).get("Networks") or {})


def _hangars(attrs: dict[str, Any]) -> bool:
    """Whether `hangar-sandboxes` is as hangar makes it: isolating, on the bridge the host's metadata rule names."""
    return isolates(attrs) and (attrs.get("Options") or {}).get(BRIDGE_NAME) == BRIDGE


class Network:
    """Creates hangar's network the first time a sandbox needs it; any other network named in HANGAR_DOCKER_NETWORK
    is the operator's and is used as it is."""

    def __init__(self, client: Any, settings: Settings) -> None:
        self.client, self.settings = client, settings
        self._lock = asyncio.Lock()

    async def ensure(self, name: str | None = None) -> None:
        """Checked before every container start, not once: `docker network prune` removes the network whenever no
        sandbox is on it, and a remembered answer would fail every start until hangar restarts."""
        if name == EGRESS_NAME:  # --- egress
            async with self._lock:
                await asyncio.to_thread(self._ensure_egress)
            return
        if network(self.settings) != NAME:
            return
        async with self._lock:
            await asyncio.to_thread(self._ensure)

    # --- egress
    def _ensure_egress(self) -> dict[str, Any]:
        api = self.client.api
        _check_internal_dns(api)  # --- review(egress)
        try:
            attrs = api.inspect_network(EGRESS_NAME)
        except NotFound:
            try:
                api.create_network(EGRESS_NAME, driver="bridge", internal=True, options=EGRESS_OPTIONS,
                                   labels={LABEL: "egress"})
                log.info("created Docker network %s (bridge %s, internal: sandboxes reach only hangar's egress proxy)",
                         EGRESS_NAME, EGRESS_BRIDGE)
            except APIError as e:
                if e.status_code != 409:
                    raise ProviderError(f"could not create the Docker network {EGRESS_NAME} for egress-restricted "
                                        f"sandboxes: {e.explanation or e}. Free the bridge name {EGRESS_BRIDGE}") from e
            attrs = api.inspect_network(EGRESS_NAME)
        if not _egress_ok(attrs):
            raise ProviderError(
                f"the Docker network {EGRESS_NAME} wasn't made by hangar: it isn't internal, lets sandboxes reach each "
                f"other or isn't on the bridge {EGRESS_BRIDGE}. Remove it (`docker network rm {EGRESS_NAME}` once no "
                "container uses it) and hangar makes it again")
        return attrs

    async def egress_gateway(self, create: bool = True) -> str | None:
        """The egress network's gateway address, where its sandboxes reach the proxy; None when it doesn't exist
        (and `create` is off)."""

        def get() -> dict[str, Any] | None:
            if create:
                return self._ensure_egress()
            try:
                attrs = self.client.api.inspect_network(EGRESS_NAME)
            except NotFound:
                return None
            return attrs if _egress_ok(attrs) else None

        async with self._lock:
            attrs = await asyncio.to_thread(get)
        return gateway_ip(attrs) if attrs else None

    def _ensure(self) -> None:
        api = self.client.api
        try:
            attrs = api.inspect_network(NAME)
        except NotFound:
            try:
                api.create_network(NAME, driver="bridge", options=OPTIONS, labels={LABEL: "sandboxes"})
                log.info("created Docker network %s (bridge %s, no traffic between sandboxes)", NAME, BRIDGE)
                return
            except APIError as e:
                if e.status_code != 409:  # 409: another control plane created it meanwhile
                    raise ProviderError(
                        f"could not create the Docker network {NAME} for sandboxes: {e.explanation or e}. Free the "
                        f"bridge name {BRIDGE}, or set HANGAR_DOCKER_NETWORK to a network of your own") from e
            attrs = api.inspect_network(NAME)
        if not _hangars(attrs):  # made by hand: deny rather than start sandboxes where they reach each other
            raise ProviderError(
                f"the Docker network {NAME} wasn't made by hangar: it lets sandboxes reach each other ({ICC} isn't "
                f"false) or isn't on the bridge {BRIDGE}, which the host's metadata rule names. Remove it (`docker "
                f"network rm {NAME}` once no container uses it) and hangar makes it again, or set "
                f"HANGAR_DOCKER_NETWORK to a network of your own")


def doctor_check(client: Any, settings: Settings) -> dict[str, Any]:
    """`hangar doctor`: are sandboxes kept apart on the network, and which still run where they aren't."""
    from hangar.doctor import _check

    name = network(settings)
    fix = (f"unset HANGAR_DOCKER_NETWORK to use hangar's own network ({NAME}), or recreate {name} with "
           f"`-o {ICC}=false`; sandboxes move when they are next created")
    if name in ("host", "none"):
        return _check("docker sandbox network", None, f"sandboxes use the {name} network"
                      + (": they share the host's, and reach every service on it" if name == "host" else ""), fix)
    try:
        attrs = client.api.inspect_network(name)
    except NotFound:
        return _check("docker sandbox network", True if name == NAME else False,
                      f"{name} is created with the first sandbox" if name == NAME else f"network {name} doesn't exist",
                      None if name == NAME else f"docker network create -o {ICC}=false {name}")
    except APIError as e:
        return _check("docker sandbox network", None, f"could not inspect {name}: {e.explanation or e}")
    if not isolates(attrs):
        return _check("docker sandbox network", None,
                      f"sandboxes on {name} can reach each other's ports (hangar-envd, dev servers)", fix)
    if name == NAME and not _hangars(attrs):
        return _check("docker sandbox network", None,
                      f"{name} isn't on the bridge {BRIDGE}, so the host's metadata rule doesn't cover it; hangar "
                      "refuses to start sandboxes on it", f"docker network rm {name} (hangar makes it again)")
    # Sandboxes, and warm-pool members (claimed ones keep only the pool's label: labels can't change).
    stray = {c.name for label in ("dev.hangar.sandbox", "dev.hangar.pool")
             for c in client.containers.list(filters={"label": label})
             if not joined(c.attrs, name) and not joined(c.attrs, EGRESS_NAME)}  # --- egress: that one is hangar's too
    if stray:
        return _check("docker sandbox network", None,
                      f"{len(stray)} running sandbox(es) started before are on another network, where sandboxes can "
                      f"reach each other: {', '.join(sorted(stray)[:5])}",
                      "destroy or suspend them; new sandboxes start on " + name)
    return _check("docker sandbox network", True, f"{name}: sandboxes can't reach each other")
