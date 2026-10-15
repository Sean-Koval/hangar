"""Services beside a sandbox: Postgres, Redis, MinIO... declared as images (--- envbuilds; docs/environments.md).

Docker-in-gVisor is limited, so a service never runs inside the sandbox. It runs next to it, with the sandbox's own
isolation, and goes away with it:

* **Kubernetes**: a sidecar container in the sandbox's pod. It shares the pod's network, so the sandbox reaches it at
  127.0.0.1, and at its name through the pod's hostAliases. It runs as uid 1000 without capabilities, like the sandbox
  container (GKE Sandbox's admission policy wants that of every container), under the same runtime class.
* **Docker**: a container on a network of the sandbox's own (`hangar-svc-<sandbox>`). The network is internal, so
  services get no route out, and other sandboxes and their services aren't on it. hangar's own network keeps sandboxes
  from reaching each other (enable_icc=false), so it can't carry this. The sandbox is attached to the network before
  it starts (gVisor can't take an interface added later) and finds each service by name in /etc/hosts. Same runtime,
  uid 1000, no capabilities, no-new-privileges.

Which services a sandbox gets is decided before it starts: those of the repo build it starts from, else the repo's
config as its default branch was last seen, else the environment's. Images must match HANGAR_ENV_SERVICE_IMAGES.
`volumes` are empty writable directories (emptyDir; tmpfs on Docker), for a database's data.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from hangar import repoenv
from hangar.envspec import EnvSpec, Service
from hangar.models import Sandbox, now

if TYPE_CHECKING:
    from hangar.fleet import Fleet

log = logging.getLogger("hangar.envservices")

SUPPORTED = ("docker", "kubernetes")
NET_PREFIX = "hangar-svc-"
LABEL = "dev.hangar.svc-of"  # on Docker: the sandbox a service container or network belongs to
SWEEP_AFTER = 600  # seconds: a service whose sandbox is gone is removed after this (a create may still be running)


def specs(sb: Sandbox) -> dict[str, Service]:
    """The services the sandbox was created with (sb.info["service_specs"])."""
    out = {}
    for name, d in (sb.info.get("service_specs") or {}).items():
        with contextlib.suppress(ValueError):
            out[name] = Service.model_validate(d)
    return out


def digest(services: dict[str, Service]) -> str:
    raw = json.dumps({k: v.model_dump(mode="json") for k, v in sorted(services.items())}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def host(pname: str, name: str) -> str:
    """Where the sandbox reaches a service: its name on both providers (an /etc/hosts entry)."""
    return name


def prepare(fleet: Fleet, spec: EnvSpec, sb: Sandbox, build_config: dict[str, Any] | None) -> None:
    """Decide the sandbox's services before it starts: sb.info["service_specs"] (what the provider runs) and the
    `service` entries of sb.info["services"] (what the supervisor checks, the agent and get_sandbox see). Never fails
    the create: a problem is a setup failure the agent is told about."""
    from hangar import envbuilds

    try:
        _prepare(fleet, spec, sb, build_config)
    except Exception as e:  # noqa: BLE001
        log.warning("could not decide the services of %s: %s", sb.id, e, exc_info=True)
        envbuilds.of(fleet).failure(sb, "services", f"hangar couldn't decide the services: {e}")


def _prepare(fleet: Fleet, spec: EnvSpec, sb: Sandbox, build_config: dict[str, Any] | None) -> None:
    from hangar import envbuilds

    cfg = None
    if envbuilds.VALIDATE_LABEL in sb.labels:  # --- autoinstall: the proposed config hangar is validating, only
        from hangar import autoinstall

        cfg = autoinstall.validation_config(fleet, sb)
    elif build_config:
        with contextlib.suppress(ValueError):
            cfg = repoenv.RepoEnvironment.model_validate(build_config)
    elif fleet.settings.env_builds and sb.repo:
        cfg = envbuilds.of(fleet).seen_config(spec.name, sb.repo)
    wanted = cfg.services if cfg is not None and cfg.services is not None else spec.services
    if not wanted:
        return
    eb = envbuilds.of(fleet)
    if sb.provider not in SUPPORTED:
        eb.failure(sb, "services", f"{', '.join(wanted)}: the {sb.provider} provider doesn't run services yet; start "
                   "them yourself if the task needs them")
        return
    allowed: dict[str, Service] = {}
    st = fleet.settings
    cpus = mem = 0.0
    for name, svc in sorted(wanted.items()):
        if not repoenv.service_image_allowed(st, svc.image):
            eb.failure(sb, f"service:{name}", f"image {svc.image} isn't allowed here (HANGAR_ENV_SERVICE_IMAGES); "
                       "an admin can add it")
        elif cpus + svc.cpus > st.env_services_max_cpus or mem + svc.memory_gb > st.env_services_max_memory_gb:
            # --- review: the repo writes each service's resources; together they fit an admin's budget
            eb.failure(sb, f"service:{name}", f"{svc.cpus:g} CPU / {svc.memory_gb:g} GiB would take the sandbox's "
                       f"services past HANGAR_ENV_SERVICES_MAX_CPUS ({st.env_services_max_cpus:g}) or "
                       f"HANGAR_ENV_SERVICES_MAX_MEMORY_GB ({st.env_services_max_memory_gb:g}); ask for less, or an "
                       "admin can raise them")
        else:
            allowed[name] = svc
            cpus, mem = cpus + svc.cpus, mem + svc.memory_gb
    if not allowed:
        return
    sb.info["service_specs"] = {n: s.model_dump(mode="json") for n, s in allowed.items()}
    sb.info["services"] = [{"name": n, "kind": "service", "host": host(sb.provider, n), "port": s.port,
                            "check": s.ready, "image": s.image} for n, s in allowed.items()]


def provider_spec(spec: EnvSpec, sb: Sandbox, suffix: str | None) -> EnvSpec:
    """The spec the provider gets when the sandbox's services aren't the environment's own: its own name (on
    Kubernetes, a SandboxTemplate with those sidecars) and no warm pool (warm sandboxes have the environment's)."""
    svcs = specs(sb)
    if svcs == spec.services and not suffix:
        return spec
    tag = suffix or f"s{digest(svcs)}"
    return spec.model_copy(update={"name": f"{spec.name[:40]}-{tag}", "warm_pool": 0, "warm_pool_hours": None,
                                   "services": svcs})


# ====================================================================== Kubernetes: sidecars
def add_sidecars(pod: dict[str, Any], spec: EnvSpec, cpu_request_fraction: float) -> None:
    """Append the spec's services to a pod template as sidecar containers, with hostAliases for their names."""
    if not spec.services:
        return
    main = pod["containers"][0]
    volumes = pod.setdefault("volumes", [])
    for name, svc in sorted(spec.services.items()):
        c: dict[str, Any] = {
            "name": f"svc-{name}",
            "image": svc.image,
            "securityContext": dict(main["securityContext"]),
            "env": [{"name": k, "value": v} for k, v in sorted(svc.env.items())],
            "ports": [{"containerPort": svc.port, "name": f"p{svc.port}"[:15]}],
            "resources": {
                "requests": {"cpu": f"{max(50, round(svc.cpus * cpu_request_fraction * 1000))}m",
                             "memory": f"{int(svc.memory_gb * 1024)}Mi"},
                "limits": {"cpu": str(svc.cpus), "memory": f"{int(svc.memory_gb * 1024)}Mi"},
            },
        }
        if svc.command:
            c["command"] = list(svc.command)
        if svc.args:
            c["args"] = list(svc.args)
        if svc.volumes:
            c["volumeMounts"] = []
            for i, path in enumerate(svc.volumes):
                vol = f"svc-{name}-{i}"
                volumes.append({"name": vol, "emptyDir": {}})
                c["volumeMounts"].append({"name": vol, "mountPath": path})
        pod["containers"].append(c)
    pod["hostAliases"] = [{"ip": "127.0.0.1", "hostnames": sorted(spec.services)}]


# ====================================================================== Docker: a network of the sandbox's own
def network_name(sb: Sandbox) -> str:
    return f"{NET_PREFIX}{sb.id}"


async def docker_run(prov: Any, sb: Sandbox, kw: dict[str, Any]) -> Any:
    """Start the sandbox's services on its own network, then the sandbox, attached to that network before it starts,
    with an /etc/hosts entry per service. Anything left over from a failure goes with the sandbox (docker_teardown)."""
    svcs = specs(sb)
    client = prov.client
    net = network_name(sb)
    s = prov.settings

    def setup() -> dict[str, str]:
        try:
            client.networks.create(net, driver="bridge", internal=True, labels={LABEL: sb.id})
        except Exception as e:  # noqa: BLE001 - 409: made by an earlier try of this create
            if getattr(e, "status_code", None) != 409:
                raise
        hosts: dict[str, str] = {}
        for name, svc in sorted(svcs.items()):
            cname = f"{NET_PREFIX}{sb.id}-{name}"
            ckw: dict[str, Any] = dict(
                image=svc.image, name=cname, detach=True, labels={LABEL: sb.id, "dev.hangar.svc": name},
                environment=dict(svc.env), network=net, user="1000:1000", cap_drop=["ALL"],
                security_opt=["no-new-privileges"], nano_cpus=int(svc.cpus * 1e9),
                mem_limit=f"{int(svc.memory_gb * 1024)}m", hostname=name,
                tmpfs={p: "mode=1777" for p in svc.volumes},
            )
            if s.docker_runtime:
                ckw["runtime"] = s.docker_runtime
            if svc.command:
                ckw["entrypoint"] = list(svc.command)
            if svc.args:
                ckw["command"] = list(svc.args)
            try:
                c = client.containers.run(**ckw)  # pulls the image if it isn't here
                c.reload()
            except Exception as e:  # noqa: BLE001 - a bad image or pull: name the service, not the sandbox's image
                from hangar.providers.base import ProviderError

                raise ProviderError(f"service {name} ({svc.image}) didn't start: {e}") from e
            ip = ((c.attrs.get("NetworkSettings") or {}).get("Networks") or {}).get(net, {}).get("IPAddress")
            if not ip:
                raise RuntimeError(f"service {name} got no address on {net}")
            hosts[name] = ip
        return hosts

    hosts = await asyncio.to_thread(setup)
    kw = dict(kw)
    kw["extra_hosts"] = {**(kw.get("extra_hosts") or {}), **hosts}
    kw.pop("detach", None)

    def start() -> Any:
        c = client.containers.create(**kw)
        client.api.connect_container_to_network(c.id, net)
        c.start()
        return c

    return await asyncio.to_thread(start)


async def docker_teardown(prov: Any, sb: Sandbox) -> None:
    """Remove the sandbox's service containers and its network (no-op when it had none)."""
    await asyncio.to_thread(_remove_docker, prov.client, sb.id)


def _remove_docker(client: Any, sid: str) -> None:
    for c in client.containers.list(all=True, filters={"label": f"{LABEL}={sid}"}):
        with contextlib.suppress(Exception):
            c.remove(force=True)
    for n in client.networks.list(names=[f"{NET_PREFIX}{sid}"]):
        if n.name == f"{NET_PREFIX}{sid}":
            with contextlib.suppress(Exception):
                n.reload()  # the sandbox may still be attached (its container goes after its services)
                for cid in (n.attrs.get("Containers") or {}):
                    client.api.disconnect_container_from_network(cid, n.id, force=True)
            with contextlib.suppress(Exception):
                n.remove()


async def sweep(fleet: Fleet) -> int:
    """Docker: service containers and networks whose sandbox is no longer live (a crash between creating them and
    recording the sandbox, a control plane that went away mid-destroy). Called from the reaper."""
    prov = fleet.providers.get("docker")
    if prov is None or not hasattr(prov, "client"):
        return 0
    from hangar.fleet import ACTIVE

    def find() -> set[str]:
        owners = set()
        for c in prov.client.containers.list(all=True, filters={"label": LABEL}):
            owners.add(c.labels.get(LABEL, ""))
        for n in prov.client.networks.list(filters={"label": LABEL}):
            owners.add((n.attrs.get("Labels") or {}).get(LABEL, ""))
        return {o for o in owners if o}

    removed = 0
    for sid in await asyncio.to_thread(find):
        sb = fleet.store.get(Sandbox, sid)
        if sb is not None and (sb.state in ACTIVE or now() - (sb.last_activity or 0) < SWEEP_AFTER):
            continue  # live, or settled so recently that its destroy may still be removing them
        await asyncio.to_thread(_remove_docker, prov.client, sid)
        removed += 1
    if removed:
        log.info("removed the services of %d sandbox(es) that are gone", removed)
    return removed
