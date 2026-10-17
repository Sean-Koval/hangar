"""The heavy tier (--- heavy; docs/environments.md#the-heavy-tier): Docker inside a VM-isolated sandbox, and bigger
resource profiles, for repos that test with `docker compose` or Testcontainers and build with more than 2 CPUs.

Docker runs natively in the sandbox: dockerd is started as root inside the sandbox's own VM (docker_agent.py), so
`docker build`, `docker compose up` and Testcontainers work as on a laptop, and the containers they start stay in
that VM. The VM is the boundary, so a heavy sandbox only ever runs where there is one: the providers HANGAR_TIERS lists
for `heavy`, in order, and nothing else (like `isolated`, it never falls back):

* kubernetes: a Kata pod (HANGAR_HEAVY_RUNTIME_CLASS) on deploy/terraform/gke's `sandboxes-heavy` pool. The sandbox
  container is privileged, which under Kata is privileged inside the guest: kata-deploy sets containerd's
  `privileged_without_host_devices`, so no host device is passed into the VM. /var/lib/docker is a volume of its own.
* e2b: an E2B or CubeSandbox microVM; dockerd runs on the guest kernel.
* docker: only with HANGAR_HEAVY_DOCKER_PRIVILEGED, for CI and throwaway machines: privileged runc is root on the host.

gVisor stays the default tier. Its Docker-in-gVisor mode wants added capabilities that GKE's sandbox policy refuses,
/var/lib/docker on tmpfs (memory) and dockerd without iptables, so published ports and Compose's networks don't work
(docs/environments.md#why-kata-and-not-gvisor).

What decides, before a sandbox starts (`decide`):
* **Docker** is deny by default (the owner's decision): the environment (`heavy: true` or `tier: heavy`, an admin's;
  its image then has Docker), else the repo's policy (`heavy: on`), else the repo's .hangar/environment.yaml asking
  for it, honored only when the policy says `heavy: allow` and a heavy tier is configured. A request that isn't
  allowed runs on the default tier, and the agent's instructions and `env doctor` say how an admin enables it. A heavy
  repo on a plain environment gets the environment's heavy variant: the same spec with Docker in its image.
* **Profile** (models.PROFILES: standard, large 8 vCPU / 32 GiB / 100 GiB, xlarge 16 / 64 / 200): a job's label
  `hangar.profile`, then the repo's file, each up to the policy's `max_profile` (else what the environment or policy
  gives); then the policy's `profile`; then the environment's. `large` and `xlarge` run on the heavy tier's VMs too
  (the gVisor pool's nodes can't hold them), with or without Docker.
* **Caps**: one person holds at most HANGAR_HEAVY_PERSON_MAX sandboxes of a profile at once (fairness.CapReached);
  work no person owns shares one bucket with the same caps, and repo builds are outside them.
* **Warm pools** of heavy environments stay at 0 unless HANGAR_HEAVY_WARM_POOLS: a warm heavy node costs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urlsplit

from hangar.models import PROFILES, Sandbox, now

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.providers.base import Provider
    from hangar.repoenv import RepoEnvironment

log = logging.getLogger("hangar.heavy")

SCRIPT = (Path(__file__).parent / "docker_agent.py").read_bytes()
LABEL = "hangar.profile"  # a job's (or a create's) profile
ORDER = ("standard", "large", "xlarge")
BIG = ("large", "xlarge")  # profiles only the heavy tier's nodes hold
POOL_LABEL = "hangar.dev/runtime"  # deploy/terraform/gke's heavy pool: hangar.dev/runtime=heavy, tainted the same
VOLUME = "hangar-docker"
DATA_ROOT = "/var/lib/docker"
DISK_GB = 30  # /var/lib/docker of a heavy sandbox whose resources name no disk
BLOCK_START = "<!-- hangar:docker -->"
BLOCK_END = "<!-- /hangar:docker -->"
SKILL_NAME = "hangar-docker"


class HeavyError(ValueError):
    """A heavy request hangar can't honor; the message says what to change."""


def rank(profile: str | None) -> int:
    return ORDER.index(profile) if profile in ORDER else 0


def vm(spec: EnvSpec) -> bool:
    """The spec runs on the heavy tier's VMs: it has Docker, or a profile only they hold."""
    return bool(spec.heavy) or spec.profile in BIG


def kata(runtime_class: str | None) -> bool:
    """A Kata runtime class: the only one a privileged heavy pod may run under (deploy/k8s/heavy-policy.yaml)."""
    return (runtime_class or "").startswith("kata")


def providers(settings: Settings) -> list[str]:
    """The heavy tier's providers, in order (HANGAR_TIERS). None configured: the tier is off."""
    return list((settings.tiers or {}).get("heavy") or [])


def unsupported(fleet: Fleet, pname: str) -> str | None:
    """Why `pname` can't run heavy sandboxes, or None."""
    if pname == "kubernetes":
        return None if kata(fleet.settings.heavy_runtime_class) else (
            f"HANGAR_HEAVY_RUNTIME_CLASS is {fleet.settings.heavy_runtime_class!r}: heavy sandboxes run only under a "
            "Kata runtime class (a privileged container under runc or gVisor isn't inside a VM)")
    if pname == "e2b":
        return None
    if pname == "docker":
        if fleet.settings.heavy_docker_privileged:
            return None
        return ("the Docker provider runs heavy sandboxes only with HANGAR_HEAVY_DOCKER_PRIVILEGED=true: privileged "
                "runc is root on the host, for CI and throwaway machines")
    return f"the {pname} provider can't run heavy sandboxes"


# ====================================================================== deciding
class Decision(NamedTuple):
    docker: bool  # Docker inside: the image has it, the sandbox is privileged in its VM
    profile: str | None  # a named size; None: the environment's own resources
    source: str  # what decided it: environment | repo policy | repo config | job
    notes: tuple[str, ...] = ()
    denied: str | None = None  # the repo's file asked for Docker and its policy doesn't allow it: what to tell people

    @property
    def vm(self) -> bool:
        return self.docker or self.profile in BIG


NONE = Decision(False, None, "environment")


def _config(fleet: Fleet, spec: EnvSpec, repo: str | None) -> RepoEnvironment | None:
    """The repo's .hangar/environment.yaml as its default branch was last seen (what a decision before any clone has;
    envbuilds.seen_config)."""
    if not repo or not fleet.settings.env_builds:
        return None
    from hangar import envbuilds

    with contextlib.suppress(Exception):
        return envbuilds.of(fleet).seen_config(spec.name, repo)
    return None


def decide(fleet: Fleet, spec: EnvSpec, repo: str | None, labels: dict[str, str] | None = None) -> Decision:
    """Docker and the profile for a sandbox of `spec` (the catalog's) for `repo`, with a create's `labels`. Raises
    HeavyError for a profile asked for past what the repo allows."""
    from hangar.repopolicy import RepoPolicies, repo_key

    policy = RepoPolicies(fleet.store).get(repo) if repo else None
    cfg = _config(fleet, spec, repo)
    name = repo_key(repo) or repo  # how people name it (owner/repo), in what they're told
    notes: list[str] = []
    denied = None
    mode = policy.heavy if policy is not None else None
    docker, source = bool(spec.heavy), "environment"
    if not docker and mode == "on":
        docker, source = True, "repo policy"
    elif not docker and cfg is not None and cfg.wants_heavy:
        if mode != "allow":  # --- integration(wave 4): the owner's decision, deny by default
            why = "its repo policy says heavy: deny" if mode == "deny" else \
                "a repo's own file only requests it, and an admin hasn't allowed that for this repo"
            denied = (f"Docker isn't enabled for {name}: its .hangar/environment.yaml asks for the heavy tier, and "
                      f"{why}, so this sandbox runs on the default tier, without Docker. An admin enables it: "
                      f"hangar repo set {name} --heavy allow")
            notes.append(denied)
        elif not providers(fleet.settings):
            notes.append(f"{name}'s .hangar/environment.yaml asks for the heavy tier, which this hangar doesn't have "
                         "(HANGAR_TIERS lists no providers for heavy), so this sandbox has no Docker")
        else:
            docker, source = True, "repo config"
    given = (policy.profile if policy is not None and policy.profile else None) or spec.profile
    ceiling = max(rank(given), rank(policy.max_profile if policy is not None else None))
    profile = given
    if given and policy is not None and policy.profile and policy.profile != spec.profile:
        source = "repo policy" if source == "environment" else source
    if cfg is not None and cfg.profile and cfg.profile != profile:
        if cfg.profile in BIG and not providers(fleet.settings):
            notes.append(f"{name}'s .hangar/environment.yaml asks for profile {cfg.profile}, which runs on the heavy "
                         "tier this hangar doesn't have (HANGAR_TIERS)")
        elif rank(cfg.profile) <= ceiling:
            profile, source = cfg.profile, "repo config"
        else:
            notes.append(f"{name}'s .hangar/environment.yaml asks for profile {cfg.profile}; its policy allows up to "
                         f"{ORDER[ceiling]} (an admin raises it: hangar repo set {name} --max-profile {cfg.profile})")
    asked = (labels or {}).get(LABEL)
    if asked:
        if asked not in PROFILES:
            raise HeavyError(f"unknown profile '{asked[:40]}' ({', '.join(ORDER)})")
        if rank(asked) > ceiling:
            where = f"{name}'s policy allows up to {ORDER[ceiling]}" if repo else \
                f"environment {spec.name} is {given or 'standard'}"
            hint = f"hangar repo set {name} --max-profile {asked}" if repo else "an environment with that profile"
            raise HeavyError(f"profile {asked} is more than this sandbox may ask for ({where}); an admin allows it "
                             f"({hint})")
        profile, source = asked, "job"
    return Decision(docker, profile, source, tuple(notes), denied)


def prepare(fleet: Fleet, spec: EnvSpec, pname: str, d: Decision) -> EnvSpec:
    """The spec a heavy sandbox is created from (the image, the provider): Docker in its image for a heavy repo on a
    plain environment, the profile's resources. Raises HeavyError where it can't run."""
    if not d.docker and d.profile is None:
        return spec
    if d.vm:
        allowed = providers(fleet.settings)
        if not allowed:
            raise HeavyError(f"{'Docker' if d.docker else 'profile ' + str(d.profile)} ({d.source}) runs on the heavy "
                             "tier, and HANGAR_TIERS lists no providers for it (docs/environments.md#the-heavy-tier)")
        if pname not in allowed:
            raise HeavyError(f"this sandbox runs on the heavy tier ({d.source}); {pname} isn't one of its providers "
                             f"({', '.join(allowed)})")
        if why := unsupported(fleet, pname):
            raise HeavyError(why)
    if d.docker and not spec.sudo and "sudo" in spec.model_fields_set:  # --- #124: unset is the default, not a no
        raise HeavyError(f"environment {spec.name} sets sudo: false, and dockerd runs as root inside the sandbox's VM")
    update: dict[str, Any] = {}
    if d.docker and not spec.heavy:
        update["heavy"] = True
    if d.docker and not spec.sudo:
        update["sudo"] = True
    if d.profile is not None and d.profile != spec.profile:
        if pname == "e2b":  # its templates are sized when they're built: one per environment
            raise HeavyError(f"on the e2b provider a sandbox has its environment's size; set profile: {d.profile} on "
                             f"environment {spec.name} instead")
        update["profile"] = d.profile
        update["resources"] = PROFILES[d.profile].model_copy()
    return spec.model_copy(update=update) if update else spec


def build_spec(fleet: Fleet, spec: EnvSpec, repo: str | None) -> EnvSpec:
    """The spec a repo's environment build starts from: Docker in it when the repo's sandboxes have it, so the build
    and the sandboxes share a base (envbuilds)."""
    if spec.heavy:
        return spec
    try:
        d = decide(fleet, spec, repo)
    except HeavyError:
        return spec
    ok = spec.sudo or "sudo" not in spec.model_fields_set  # --- #124: only an explicit sudo: false says no
    return spec.model_copy(update={"heavy": True, "sudo": True}) if d.docker and ok else spec


def variant(fleet: Fleet, spec: EnvSpec) -> bool:
    """`spec` is a heavy variant of a plain environment (its image is built on its own, not as the environment's)."""
    if not spec.heavy:
        return False
    try:
        return not fleet.catalog.get(spec.name).heavy
    except KeyError:
        return False


UNATTRIBUTED = "~unattributed"  # the team's bucket for HANGAR_HEAVY_PERSON_MAX (no person can be named this)
_BUILD_ACTOR = "hangar:build:"  # a repo build's sandbox (envbuilds): one per repo and environment at a time


def _bucket(person: str | None, actor: str | None) -> str | None:
    """Whose heavy caps a sandbox counts against: its person; else, for work no person owns (an unlinked sender's
    job, a webhook's), one shared team bucket, so those can't take the heavy pool past the cap either (the #117
    review). Repo builds are hangar's own and never refused here."""
    from hangar import fairness

    if who := person or fairness.person_of(actor):
        return who
    return None if (actor or "").startswith(_BUILD_ACTOR) else UNATTRIBUTED


def admit(fleet: Fleet, sb: Sandbox, d: Decision, base: EnvSpec, actor: str, person: str | None) -> None:
    """Refuse a sandbox past its person's cap for its profile, and record the decision (sb.info["heavy"]). Called
    right before the sandbox is first stored, with nothing awaited in between, so two creates can't both slip under
    a cap."""
    for n in d.notes:  # what the repo asked for and didn't get, whether or not anything is heavy
        sb.info.setdefault("warnings", []).append(n)
    if d.denied:  # --- integration(wave 4): the agent's instructions and `env doctor` say so (start, status)
        sb.info["heavy_denied"] = d.denied
    if not d.docker and d.profile is None:
        return
    from hangar import fairness

    who = _bucket(person, actor)
    caps = fleet.settings.heavy_person_max or {}
    if who and d.profile in BIG:
        from hangar.fleet import ACTIVE

        active = fleet.store.list_where(Sandbox, state=ACTIVE)
        for level in BIG:
            cap = int(caps.get(level) or 0)
            if cap <= 0 or rank(d.profile) < rank(level):
                continue
            held = sum(1 for s in active if rank((s.info.get("heavy") or {}).get("profile")) >= rank(level)
                       and _bucket(s.person, s.created_by) == who)
            if held >= cap:
                team = who == UNATTRIBUTED
                whose = ("unattributed work (jobs and sessions no person owns: unlinked GitHub, Slack or Jira "
                         "senders, webhooks)" if team else who)
                raise fairness.CapReached(
                    f"{whose} already has {held} {level}{' or larger' if level == 'large' else ''} sandboxes running "
                    f"(the limit {'for it together' if team else 'per person'} is {cap}); destroy or suspend one "
                    "first, or ask the operator to raise HANGAR_HEAVY_PERSON_MAX")
    marks = [m for m in ("docker" if d.docker and not base.heavy else None,
                         d.profile if d.profile and d.profile != base.profile else None) if m]
    sb.info["heavy"] = {"docker": d.docker, "profile": d.profile, "vm": d.vm, "source": d.source,
                        "variant": "+".join(marks) or None}


def provider_tag(sb: Sandbox, tag: str) -> str:
    """A template name suffix for a sandbox whose Docker or profile isn't its environment's own: of the shape
    k8sgc sweeps (`s` and 8 hex digits), so an unused one goes like a services one."""
    mark = (sb.info.get("heavy") or {}).get("variant")
    return "s" + hashlib.sha256(f"{tag}:{mark}".encode()).hexdigest()[:8] if mark else tag


def warm(settings: Settings, spec: EnvSpec, n: int | None = None) -> int:
    """The warm pool size a provider keeps: 0 for a heavy spec unless HANGAR_HEAVY_WARM_POOLS."""
    n = max(0, spec.warm_pool if n is None else n)
    return n if not vm(spec) or settings.heavy_warm_pools else 0


def disk_gb(spec: EnvSpec) -> int:
    return int(spec.resources.disk_gb or DISK_GB)


# ====================================================================== providers
def k8s_pod(pod: dict[str, Any], spec: EnvSpec, settings: Settings) -> str | None:
    """A heavy spec's pod: the VM runtime class on the heavy pool and, with Docker, a privileged sandbox container
    (privileged inside the guest only) and a volume for /var/lib/docker. Sidecars keep their own security context.
    Returns the runtime class, or None for a spec that isn't heavy."""
    if not vm(spec):
        return None
    rc = settings.heavy_runtime_class
    if not kata(rc):  # unsupported() refuses the create first; never a privileged pod outside a VM
        raise ValueError(f"HANGAR_HEAVY_RUNTIME_CLASS {rc!r} isn't a Kata runtime class")
    pod["runtimeClassName"] = rc
    pod.pop("nodeSelector", None)
    pod.pop("tolerations", None)
    if settings.heavy_node_selector:
        pod["nodeSelector"] = {POOL_LABEL: "heavy"}
        pod["tolerations"] = [{"key": POOL_LABEL, "operator": "Equal", "value": "heavy", "effect": "NoSchedule"}]
    if spec.heavy:
        main = pod["containers"][0]
        main["securityContext"] = {"privileged": True, "allowPrivilegeEscalation": True}
        disk = disk_gb(spec)
        pod.setdefault("volumes", []).append({"name": VOLUME, "emptyDir": {"sizeLimit": f"{disk + 2}Gi"}})
        main.setdefault("volumeMounts", []).append({"name": VOLUME, "mountPath": DATA_ROOT})
        main["resources"]["requests"]["ephemeral-storage"] = f"{disk}Gi"
    return rc


def docker_kwargs(kw: dict[str, Any], spec: EnvSpec | None) -> None:
    """Docker provider (HANGAR_HEAVY_DOCKER_PRIVILEGED): privileged, with an anonymous volume for /var/lib/docker that
    goes with the container."""
    if spec is None or not spec.heavy:
        return
    kw["privileged"] = True
    kw["mounts"] = [*(kw.get("mounts") or []), {"Type": "volume", "Target": DATA_ROOT}]


# ====================================================================== Docker in the sandbox
def docker_conf(fleet: Fleet, sb: Sandbox, spec: EnvSpec) -> dict[str, Any]:
    """What hangar-docker starts dockerd with: the disk, registry mirrors and, in a proxied sandbox, the egress proxy
    with the sandbox's own credential (never stored: derived like the proxy's, worthless once the sandbox stops)."""
    from hangar import egress, keys

    s = fleet.settings
    home = f"/home/{s.sandbox_user}"
    conf: dict[str, Any] = {"v": 1, "user": s.sandbox_user, "group": s.sandbox_user, "home": home,
                            "disk_gb": disk_gb(spec), "mirrors": list(s.heavy_registry_mirrors)}
    password = None
    if s.heavy_mirror and s.gateway_url:
        from hangar import regmirror

        password = egress.token(keys.key_for(s, sb.created_at), sb.id)
        conf["mirrors"].insert(0, regmirror.mirror_url(s, sb.id, password))
        u = urlsplit(s.gateway_url)
        if u.scheme == "http":
            conf["insecure"] = [u.netloc]
    eg = sb.info.get("egress") or {}
    if eg.get("proxy"):
        password = password or egress.token(keys.key_for(s, sb.created_at), sb.id)
        conf["proxy"] = f"http://{sb.id}:{password}@{eg['proxy']}"
        conf["no_proxy"] = list(dict.fromkeys(["localhost", "127.0.0.1", "::1", *egress._direct_hosts(s)]))
    return conf


async def start(fleet: Fleet, prov: Provider, sb: Sandbox, spec: EnvSpec) -> None:
    """Start Docker in a heavy sandbox and tell the agent about it. Never fails the create: a problem is a setup
    failure the agent and `hangar-agent env doctor` show, with the command that starts it again."""
    hv = sb.info.get("heavy") or {}
    if not hv.get("docker"):
        if sb.info.get("heavy_denied"):  # the repo asked and isn't allowed: say so, not a silently weaker sandbox
            with contextlib.suppress(Exception):
                await tell_agent(prov, sb, fleet.settings, sb.info.get("harness"))
        return
    home = f"/home/{fleet.settings.sandbox_user}"
    status: dict[str, Any]
    try:
        await prov.write_file(sb, f"{home}/.hangar/bin/hangar-docker", SCRIPT, mode=0o755)
        await prov.write_file(sb, f"{home}/.hangar/docker.json",
                              json.dumps(docker_conf(fleet, sb, spec), indent=1).encode(), mode=0o600)
        res = await prov.exec(sb, f"sudo -n python3 {home}/.hangar/bin/hangar-docker start {home}/.hangar/docker.json",
                              timeout=180)
        status = _last_json(res.stdout) or {"ok": False, "error": (res.stderr or res.stdout).strip()[-500:]
                                                                   or f"hangar-docker exited {res.exit_code}"}
    except Exception as e:  # noqa: BLE001 - the sandbox works without Docker; say why
        status = {"ok": False, "error": f"hangar couldn't start Docker: {e}"}
    status = {k: v for k, v in status.items() if k in ("ok", "error", "version", "driver", "data_root", "cgroup",
                                                       "storage", "iptables", "mtu", "already")}
    status["at"] = now()
    hv["status"] = fleet.broker.redact(status)
    sb.info["heavy"] = hv
    if not status.get("ok"):
        from hangar import envbuilds

        envbuilds.of(fleet).failure(sb, "docker", f"Docker didn't start: {status.get('error') or 'unknown'}. Start "
                                    f"it again with `sudo {home}/.hangar/bin/hangar-docker start "
                                    f"{home}/.hangar/docker.json`")
    with contextlib.suppress(Exception):
        await tell_agent(prov, sb, fleet.settings, sb.info.get("harness"))


def _last_json(out: str) -> dict[str, Any] | None:
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                v = json.loads(line)
                return v if isinstance(v, dict) else None
    return None


def status(sb: Sandbox, spec: EnvSpec) -> dict[str, Any]:
    """The tier for the environment doctor (`hangar-agent env doctor`): what the sandbox runs on and with."""
    hv = sb.info.get("heavy") or {}
    res = sb.resources
    return {"tier": "heavy" if hv.get("vm") or vm(spec) else "standard", "profile": hv.get("profile") or spec.profile,
            "resources": {"cpus": res.cpus, "memory_gb": res.memory_gb, "disk_gb": res.disk_gb},
            "docker": bool(hv.get("docker") or spec.heavy), "docker_status": hv.get("status"),
            "source": hv.get("source"), "docker_denied": sb.info.get("heavy_denied")}


# ====================================================================== telling the agent
def instructions(sb: Sandbox) -> str:
    hv = sb.info.get("heavy") or {}
    if not hv.get("docker") and (denied := sb.info.get("heavy_denied")):
        return "\n".join([
            BLOCK_START, "## Docker isn't enabled for this repo", "",
            f"{denied}.",
            "- There is no `docker` here: use the environment's `services` (sidecar images such as Postgres) and "
            "tests that don't need Docker.",
            "- If the task needs Docker (`docker compose`, Testcontainers, building images), say so plainly in your "
            "summary, with the admin command above, and finish everything else.",
            "- `hangar-agent env doctor` shows this on its `tier` line.",
            BLOCK_END]) + "\n"
    st = hv.get("status") or {}
    res = sb.resources
    size = f"{res.cpus:g} CPUs, {res.memory_gb:g} GiB" + (f", {res.disk_gb:g} GiB for Docker" if res.disk_gb else "")
    if hv.get("profile"):
        size = f"profile {hv['profile']}: {size}"
    lines = [BLOCK_START, "## Docker (hangar heavy tier)", "",
             f"This sandbox is a VM of its own on hangar's heavy tier ({size}), with Docker inside. `docker build`, "
             "`docker compose` and Testcontainers work as on a laptop, and the containers you start stay inside this "
             "VM."]
    if not st.get("ok"):
        lines.append(f"**Docker isn't running**: {str(st.get('error') or 'not started')[:300]}. Start it with "
                     "`sudo ~/.hangar/bin/hangar-docker start ~/.hangar/docker.json`.")
    lines += [
        "- Stacks you need while you work (a database, a queue): declare them in the repo's .hangar/environment.yaml "
        "(`services: {db: {compose: docker-compose.yml}}`) or run `hangar-agent svc add NAME --compose FILE`, so the "
        "supervisor brings them up, restarts them and keeps their logs (`hangar-agent svc logs NAME`). Don't leave "
        "`docker compose up -d` stacks nobody watches.",
        "- Testcontainers needs nothing extra: it finds Docker at /var/run/docker.sock.",
        "- `hangar-agent env doctor` shows the tier, the profile and whether Docker answers. Pulled images and volumes "
        "don't survive the sandbox: a resumed session starts with an empty Docker.",
        BLOCK_END]
    return "\n".join(lines) + "\n"


SKILL_DESCRIPTION = (
    "Use when a build or test needs Docker: `docker compose` stacks, Testcontainers, building or running images, or "
    "when `docker` fails or isn't found. Covers the heavy tier (Docker inside the sandbox's VM), running Compose "
    "stacks under the supervisor, and checking the tier with `hangar-agent env doctor`.")
SKILL = f"---\nname: {SKILL_NAME}\ndescription: {json.dumps(SKILL_DESCRIPTION)}\n---\n" + """
# Docker in a hangar sandbox

Docker is there only on hangar's **heavy tier**: a sandbox that is a VM of its own, with dockerd running inside it.
Check first: `hangar-agent env doctor` prints a `tier` line (heavy or standard), the profile (CPUs, memory, disk) and
whether Docker answers. On the standard tier there is no Docker: use the environment's `services` (sidecar images like
Postgres) and say in your summary that the task wants the heavy tier (`heavy: true` in .hangar/environment.yaml; an
admin decides whether it is honored).

## Compose stacks under the supervisor

Run long-lived stacks through the supervisor, not `docker compose up -d` in your shell:

- In the repo, for every run: `.hangar/environment.yaml`
  ```yaml
  services:
    deps: {compose: docker-compose.yml}            # `docker compose -f docker-compose.yml up`, supervised
    db: {compose: compose.test.yml, port: 5432}    # ready once 5432 answers (default: every container is
                                                   # running and healthy, or exited 0)
  ```
- For this run only: `hangar-agent svc add deps --compose docker-compose.yml` (`--port P` or `--ready CMD` to say
  when it is ready).
- Then `hangar-agent svc wait deps --timeout 300`, `hangar-agent svc logs deps -n 200`, `hangar-agent svc restart deps`.
  `start` and the terminals wait for the stacks, as they do for services.

The stack runs as Compose project `<name>`, so `docker compose -p <name> ps` and `docker compose -p <name> exec ...`
reach it.

## Testcontainers

Works as is: Docker is at /var/run/docker.sock and published ports answer on localhost. Ryuk (its reaper) is fine.

## When Docker fails

1. `hangar-agent env doctor`: is this the heavy tier, does Docker answer?
2. Not answering: `sudo ~/.hangar/bin/hangar-docker start ~/.hangar/docker.json` (prints why it can't), and
   `sudo tail -n 100 /var/log/hangar-dockerd.log`.
3. A pull is refused or times out: the sandbox's egress policy decides which registries it reaches
   (`hangar-agent env doctor --host registry-1.docker.io`); file the host with `hangar-agent env request egress HOST
   --why "..."` and keep going with what you have.
4. Out of disk: `docker system df`, then `docker system prune -f`. The profile sets the disk; a bigger one is a job
   label (`hangar.profile: large`) an admin allows.
"""


async def tell_agent(prov: Provider, sb: Sandbox, settings: Settings, harness: str | None) -> None:
    """The Docker section in the harness's instructions file and the hangar-docker skill (svc_control.tell_agent does
    the same for the environment section)."""
    from hangar.harnesses import HARNESSES
    from hangar.svc_control import INSTRUCTIONS_MAX

    h = HARNESSES.get(harness or "")
    if h is None:
        return
    home = f"/home/{settings.sandbox_user}"
    if h.instructions_file:
        path = f"{home}/{h.instructions_file}"
        cur = await prov.read_file(sb, path, limit=INSTRUCTIONS_MAX)
        if cur is None or len(cur) <= INSTRUCTIONS_MAX:
            await prov.write_file(sb, path, splice((cur or b"").decode(errors="replace"), instructions(sb)).encode(),
                                  mode=0o644)
    if h.skills_dir:
        await prov.write_file(sb, f"{home}/{h.skills_dir}/{SKILL_NAME}/SKILL.md", SKILL.encode(), mode=0o644)


def splice(current: str, block: str) -> str:
    a, b = current.find(BLOCK_START), current.find(BLOCK_END)
    if a != -1 and b > a:
        return current[:a] + block + current[b + len(BLOCK_END):].lstrip("\n")
    return current + ("\n\n" if current and not current.endswith("\n\n") else "") + block


def compose_unavailable(sb: Sandbox, names: list[str]) -> None:
    """A repo declared Compose stacks for a sandbox without Docker: the agent is told why they aren't running."""
    xs = sb.info.setdefault("setup_failures", [])
    xs.append({"ts": now(), "step": "services", "message": (
        f"{', '.join(names)}: Compose stacks need Docker, which only the heavy tier has (heavy: true in "
        ".hangar/environment.yaml, honored where an admin allows it); use image services instead")})
    del xs[:-20]
