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
-j DROP` (the single-VM install adds it).
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
OPTIONS = {ICC: "false", "com.docker.network.bridge.name": BRIDGE}
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


class Network:
    """Creates hangar's network the first time a sandbox needs it; any other network named in HANGAR_DOCKER_NETWORK
    is the operator's and is used as it is."""

    def __init__(self, client: Any, settings: Settings) -> None:
        self.client, self.settings = client, settings
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        if self._ready or network(self.settings) != NAME:
            return
        async with self._lock:
            if not self._ready:
                await asyncio.to_thread(self._ensure)
                self._ready = True

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
        if not isolates(attrs):
            log.warning("Docker network %s lets sandboxes reach each other (it was not created by hangar with "
                        "%s=false); remove it and hangar recreates it", NAME, ICC)


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
    # Sandboxes, and warm-pool members (claimed ones keep only the pool's label: labels can't change).
    stray = {c.name for label in ("dev.hangar.sandbox", "dev.hangar.pool")
             for c in client.containers.list(filters={"label": label}) if not joined(c.attrs, name)}
    if stray:
        return _check("docker sandbox network", None,
                      f"{len(stray)} running sandbox(es) started before are on another network, where sandboxes can "
                      f"reach each other: {', '.join(sorted(stray)[:5])}",
                      "destroy or suspend them; new sandboxes start on " + name)
    return _check("docker sandbox network", True, f"{name}: sandboxes can't reach each other")
