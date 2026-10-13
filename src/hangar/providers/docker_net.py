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


def network(settings: Settings) -> str:
    """Where sandboxes go: HANGAR_DOCKER_NETWORK as it is, else hangar's own isolated network (`bridge` is Docker's
    default bridge, with no isolation between sandboxes)."""
    return settings.docker_network or NAME


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

    async def ensure(self) -> None:
        """Checked before every container start, not once: `docker network prune` removes the network whenever no
        sandbox is on it, and a remembered answer would fail every start until hangar restarts."""
        if network(self.settings) != NAME:
            return
        async with self._lock:
            await asyncio.to_thread(self._ensure)

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
             for c in client.containers.list(filters={"label": label}) if not joined(c.attrs, name)}
    if stray:
        return _check("docker sandbox network", None,
                      f"{len(stray)} running sandbox(es) started before are on another network, where sandboxes can "
                      f"reach each other: {', '.join(sorted(stray)[:5])}",
                      "destroy or suspend them; new sandboxes start on " + name)
    return _check("docker sandbox network", True, f"{name}: sandboxes can't reach each other")
