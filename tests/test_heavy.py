"""--- heavy: the heavy tier (heavy.py): Docker inside a VM-isolated sandbox, resource profiles, per-person caps, warm
pools off by default, the Kubernetes and Docker providers' side, Compose stacks in the spec, and what the agent is told.
Providers are fakes: nothing here starts a VM or a dockerd (the Docker e2e suite does, test_e2e_heavy_docker.py)."""

from __future__ import annotations

import json

import pytest
import yaml

from hangar import envservices, heavy
from hangar.envspec import EnvSpec, render
from hangar.fairness import CapReached
from hangar.models import PROFILES, ExecResult, Resources, Sandbox
from hangar.repoenv import RepoEnvError, parse
from hangar.repopolicy import RepoPolicies, RepoPolicy

from .conftest import FakeProvider

REPO = "https://github.com/acme/app.git"


class VMProvider(FakeProvider):
    """A fake heavy-capable provider: answers hangar-docker's start like a sandbox whose dockerd came up."""

    def __init__(self, ok: bool = True):
        super().__init__()
        self.ok = ok
        self.started: list[str] = []
        self.specs: list[EnvSpec] = []

    async def create(self, sandbox, image, spec=None):
        self.specs.append(spec)
        await super().create(sandbox, image, spec)

    async def exec(self, sandbox, command, timeout=120, cwd=None, user=None):
        if "hangar-docker start" in command:
            self.started.append(command)
            out = {"ok": True, "version": "29.8.2", "driver": "overlayfs", "storage": "ext4 at /var/lib/docker"} \
                if self.ok else {"ok": False, "error": "dockerd didn't answer in 90s"}
            return ExecResult(exit_code=0 if self.ok else 1, stdout=json.dumps(out) + "\n", stderr="")
        return await super().exec(sandbox, command, timeout, cwd, user)


@pytest.fixture
def hv(fleet, settings, tmp_path):
    settings.tiers = {**settings.tiers, "heavy": ["kubernetes"]}
    vm = VMProvider()
    fleet.providers["kubernetes"] = vm
    d = tmp_path / "heavy-envs"
    d.mkdir()
    (d / "dind.yaml").write_text("name: dind\nheavy: true\nharnesses: [git]\n")
    (d / "big.yaml").write_text("name: big\ntier: heavy\nprofile: large\nharnesses: [git]\n")
    (d / "small.yaml").write_text("name: small\nharnesses: [git]\nwarm_pool: 2\n")
    fleet.catalog.dirs.insert(0, d)
    fleet.catalog.reload()
    fleet.vm = vm
    return fleet


# ====================================================================== the spec
def test_tier_heavy_is_heavy_and_a_profile_replaces_resources():
    assert EnvSpec(name="x", tier="heavy").heavy
    big = EnvSpec(name="x", heavy=True, profile="xlarge", resources=Resources(cpus=1, memory_gb=1))
    assert big.resources == PROFILES["xlarge"] and big.resources.disk_gb == 200
    assert EnvSpec(name="x", profile="large").resources.cpus == 8
    with pytest.raises(ValueError, match="heavy is its own tier"):
        EnvSpec(name="x", heavy=True, tier="fast")
    with pytest.raises(ValueError, match="needs sudo"):
        EnvSpec(name="x", heavy=True, sudo=False)


def test_a_heavy_image_gets_docker_from_the_pinned_image_and_a_plain_one_doesnt():
    plain = render(EnvSpec(name="x")).dockerfile
    assert "hangar-docker" not in plain and "iptables" not in plain
    ctx = render(EnvSpec(name="x", heavy=True), docker_image="docker:29.8.2-dind")
    df = ctx.dockerfile
    assert df.index("FROM docker:29.8.2-dind AS hangar-docker") < df.index("FROM ubuntu:24.04")
    assert "COPY --from=hangar-docker /usr/local/bin/ /opt/hangar/docker/bin/" in df
    assert "COPY --from=hangar-docker /usr/local/libexec/docker/cli-plugins/" in df
    steps = b"".join(ctx.files[k] for k in sorted(ctx.files) if k.startswith("steps/")).decode()
    assert "iptables e2fsprogs mount pigz" in steps or all(p in steps for p in ("iptables", "e2fsprogs"))
    assert "ln -sf /opt/hangar/docker/bin/$b /usr/local/bin/$b" in steps and "docker compose version" in steps
    other = render(EnvSpec(name="x", heavy=True), docker_image="docker:29.9.0-dind")
    assert other.digest != ctx.digest, "a new Docker is a new image"


def test_compose_services_short_form_and_validation():
    spec = EnvSpec(name="x", heavy=True, services={"deps": "docker-compose.yml",
                                                   "db": {"compose": "ops/compose.yml", "port": 5432},
                                                   "redis": {"image": "redis:7", "port": 6379}})
    assert spec.services["deps"].compose == "docker-compose.yml" and spec.services["db"].port == 5432
    assert set(envservices.sidecars(spec.services)) == {"redis"}
    for bad, why in (({"compose": "c.yml", "image": "redis"}, "takes compose"),
                     ({"compose": "../c.yml"}, "relative to the repo"),
                     ({"compose": "/etc/c.yml"}, "relative to the repo"),
                     ({"port": 5432}, "needs image and port")):
        with pytest.raises(ValueError, match=why):
            EnvSpec(name="x", services={"s": bad})


def test_the_repo_file_may_ask_for_heavy_and_a_profile_but_no_other_tier():
    cfg = parse(yaml.safe_dump({"heavy": True, "profile": "large", "services": {"deps": "compose.yml"}}))
    assert cfg.wants_heavy and cfg.profile == "large" and cfg.services["deps"].compose == "compose.yml"
    assert parse("tier: heavy\n").wants_heavy
    with pytest.raises(RepoEnvError, match="tier"):
        parse("tier: fast\n")
    with pytest.raises(RepoEnvError, match="resources"):
        parse("resources: {cpus: 64}\n")


# ====================================================================== deciding
def _seen(fleet, env: str, config: dict) -> None:
    from hangar.envbuilds import KV_SEEN, Seen
    from hangar.repopolicy import repo_key

    fleet.store.kv_set(f"{KV_SEEN}{env}:{repo_key(REPO)}", Seen(commit="c" * 40, present=True, digest="d",
                                                                config=config).model_dump_json())


def test_decide_from_the_environment_the_policy_and_the_repo(hv):
    f = hv
    small = f.catalog.get("small")
    assert heavy.decide(f, small, REPO) == heavy.Decision(False, None, "environment")
    assert heavy.decide(f, f.catalog.get("dind"), REPO).docker
    RepoPolicies(f.store).put(RepoPolicy(repo="acme/app", heavy=True))
    d = heavy.decide(f, small, REPO)
    assert d.docker and d.source == "repo policy" and d.vm
    RepoPolicies(f.store).put(RepoPolicy(repo="acme/app"))
    _seen(f, "small", {"heavy": True})
    assert heavy.decide(f, small, REPO).source == "repo config"
    RepoPolicies(f.store).put(RepoPolicy(repo="acme/app", heavy=False))
    d = heavy.decide(f, small, REPO)
    assert not d.docker and "heavy: false" in d.notes[0]
    RepoPolicies(f.store).put(RepoPolicy(repo="acme/app"))
    f.settings.tiers = {"fast": ["e2b"]}
    d = heavy.decide(f, small, REPO)
    assert not d.docker and "HANGAR_TIERS" in d.notes[0], "a repo can't ask for a tier this hangar doesn't have"


def test_profiles_go_up_only_as_far_as_the_policy_allows(hv):
    f = hv
    small = f.catalog.get("small")
    with pytest.raises(heavy.HeavyError, match="acme/app's policy allows up to standard.*--max-profile large"):
        heavy.decide(f, small, REPO, {heavy.LABEL: "large"})
    with pytest.raises(heavy.HeavyError, match="unknown profile"):
        heavy.decide(f, small, REPO, {heavy.LABEL: "huge"})
    RepoPolicies(f.store).put(RepoPolicy(repo="acme/app", max_profile="large"))
    d = heavy.decide(f, small, REPO, {heavy.LABEL: "large"})
    assert d == heavy.Decision(False, "large", "job") and d.vm, "large runs on the heavy tier, Docker or not"
    with pytest.raises(heavy.HeavyError):
        heavy.decide(f, small, REPO, {heavy.LABEL: "xlarge"})
    _seen(f, "small", {"profile": "xlarge"})
    d = heavy.decide(f, small, REPO)
    assert d.profile is None and "--max-profile xlarge" in d.notes[0]
    RepoPolicies(f.store).put(RepoPolicy(repo="acme/app", profile="large"))
    assert heavy.decide(f, small, REPO).profile == "large"
    assert heavy.decide(f, f.catalog.get("big"), None, {heavy.LABEL: "standard"}).profile == "standard", \
        "smaller is always allowed"


# ====================================================================== creating
async def test_a_heavy_environment_runs_on_the_heavy_tier_with_docker_started(hv):
    f = hv
    sb = await f.create("dind", actor="ann")
    assert sb.provider == "kubernetes"
    assert sb.info["heavy"]["docker"] and sb.info["heavy"]["vm"] and sb.info["heavy"]["status"]["ok"]
    assert f.vm.started and "sudo -n python3 /home/agent/.hangar/bin/hangar-docker start" in f.vm.started[0]
    box = f.vm.boxes[sb.id]
    conf = json.loads((box / "home/agent/.hangar/docker.json").read_text())
    assert conf["disk_gb"] == heavy.DISK_GB and conf["user"] == "agent" and "proxy" not in conf
    assert (box / "home/agent/.hangar/bin/hangar-docker").read_bytes() == heavy.SCRIPT
    assert f.vm.specs[-1].heavy and f.vm.specs[-1].warm_pool == 0


async def test_without_a_heavy_tier_a_heavy_environment_says_why(hv):
    from hangar.fleet import FleetError

    f = hv
    f.settings.tiers = {"fast": ["e2b"]}
    with pytest.raises(FleetError, match=r"heavy tier is available \(none configured in HANGAR_TIERS\)"):
        await f.create("dind")
    with pytest.raises(FleetError, match="docker isn't one of its providers"):
        f.settings.tiers = {"heavy": ["kubernetes"]}
        await f.create("dind", provider="docker")
    with pytest.raises(FleetError, match="isn't heavy"):
        await f.create("small", tier="heavy")
    f.settings.tiers = {"heavy": ["docker"]}
    with pytest.raises(FleetError, match="HANGAR_HEAVY_DOCKER_PRIVILEGED"):
        await f.create("dind")
    assert not f.list()


async def test_a_profile_label_sizes_the_sandbox_and_its_own_template(hv):
    f = hv
    sb = await f.create("small", labels={heavy.LABEL: "standard"}, actor="ann")
    assert sb.provider == "docker" and sb.resources == Resources(), "standard stays on the default tier"
    from hangar.fleet import FleetError

    with pytest.raises(FleetError, match="environment small is standard"):
        await f.create("small", labels={heavy.LABEL: "large"}, actor="ann")
    big = await f.create("big", actor="ann")
    assert big.provider == "kubernetes" and big.resources == PROFILES["large"]
    spec = f.vm.specs[-1]
    assert spec.name == "big" and spec.resources == PROFILES["large"], "the environment's own profile, own template"


async def test_profile_overrides_get_a_template_of_their_own(hv):
    f = hv
    base = f.catalog.get("small")
    sb = Sandbox(name="s", environment="small", provider="kubernetes")
    sb.info["heavy"] = {"docker": True, "profile": "large", "vm": True, "source": "job", "variant": "docker+large"}
    out = envservices.provider_spec(heavy.prepare(f, base, "kubernetes", heavy.Decision(True, "large", "job")), sb,
                                    None)
    assert out.name.startswith("small-s") and len(out.name.split("-s")[-1]) == 8 and out.warm_pool == 0
    from hangar.k8sgc import DERIVED

    assert DERIVED.match(f"hangar-{out.name}"), "k8sgc sweeps it once nothing runs from it"
    assert out.heavy and out.resources == PROFILES["large"]
    plain = Sandbox(name="p", environment="small", provider="kubernetes")
    assert envservices.provider_spec(base, plain, None) is base


async def test_a_person_holds_a_bounded_number_of_large_sandboxes(hv):
    f = hv
    f.settings.heavy_person_max = {"large": 2, "xlarge": 1}
    await f.create("big", actor="ann")
    await f.create("big", actor="mcp:ann")
    with pytest.raises(CapReached, match="ann already has 2 large or larger sandboxes"):
        await f.create("big", actor="ann")
    await f.create("big", actor="bob")
    await f.create("dind", actor="ann"), "Docker at the standard size isn't capped"
    f.settings.heavy_person_max = {"large": 0, "xlarge": 0}
    await f.create("big", actor="ann")


async def test_docker_that_doesnt_start_is_a_setup_failure_the_agent_sees(hv):
    f = hv
    f.vm.ok = False
    sb = await f.create("dind", actor="ann")
    assert sb.state.value == "running", "the sandbox works without Docker"
    fails = [x for x in sb.info.get("setup_failures") or [] if x["step"] == "docker"]
    assert fails and "dockerd didn't answer" in fails[0]["message"] and "hangar-docker start" in fails[0]["message"]
    assert not sb.info["heavy"]["status"]["ok"]


def test_instructions_and_skill():
    sb = Sandbox(name="s", environment="e", provider="kubernetes", resources=PROFILES["large"])
    sb.info["heavy"] = {"docker": True, "profile": "large", "status": {"ok": True}}
    text = heavy.instructions(sb)
    assert text.startswith(heavy.BLOCK_START) and text.rstrip().endswith(heavy.BLOCK_END)
    assert "profile large: 8 CPUs, 32 GiB, 100 GiB for Docker" in text and "--compose" in text
    assert "isn't running" not in text
    sb.info["heavy"]["status"] = {"ok": False, "error": "no dockerd"}
    assert "**Docker isn't running**: no dockerd" in heavy.instructions(sb)
    once = heavy.splice("# mine\n", heavy.instructions(sb))
    assert heavy.splice(once, heavy.instructions(sb)) == once and once.startswith("# mine")
    head = heavy.SKILL.split("---")[1]
    assert f"name: {heavy.SKILL_NAME}" in head and "description:" in head
    assert "hangar-agent env doctor" in heavy.SKILL and "compose: docker-compose.yml" in heavy.SKILL


# ====================================================================== the providers
def _k8s(**kw):
    pytest.importorskip("kubernetes")
    from tests.test_kubernetes import provider

    return provider(**kw)


def test_a_heavy_pod_is_privileged_in_its_vm_on_the_heavy_pool_and_its_sidecars_arent():
    p = _k8s()
    spec = EnvSpec(name="d", heavy=True, profile="large", services={"pg": {"image": "postgres:17", "port": 5432},
                                                                     "deps": "compose.yml"})
    pod = p._pod_template(spec, "reg/d:1")["spec"]
    assert pod["runtimeClassName"] == "kata-qemu-runtime-rs"
    assert pod["nodeSelector"] == {"hangar.dev/runtime": "heavy"}
    assert pod["tolerations"] == [{"key": "hangar.dev/runtime", "operator": "Equal", "value": "heavy",
                                   "effect": "NoSchedule"}]
    main, side = pod["containers"][0], pod["containers"][1]
    assert main["securityContext"] == {"privileged": True, "allowPrivilegeEscalation": True}
    assert side["name"] == "svc-pg" and side["securityContext"]["privileged"] is False
    assert [c["name"] for c in pod["containers"]] == ["sandbox", "svc-pg"], "a Compose stack isn't a sidecar"
    assert {"name": "hangar-docker", "emptyDir": {"sizeLimit": "102Gi"}} in pod["volumes"]
    assert {"name": "hangar-docker", "mountPath": "/var/lib/docker"} in main["volumeMounts"]
    assert main["resources"]["requests"]["ephemeral-storage"] == "100Gi"
    assert main["resources"]["limits"] == {"cpu": "8.0", "memory": "32768Mi"}
    assert pod["securityContext"]["runAsNonRoot"] is True, "the container still starts as the sandbox user"
    assert "serviceAccountName" not in pod


def test_a_large_profile_without_docker_is_a_vm_but_not_privileged():
    p = _k8s(k8s_pod_snapshots=False)
    pod = p._pod_template(EnvSpec(name="l", profile="large"), "i")["spec"]
    assert pod["runtimeClassName"] == "kata-qemu-runtime-rs" and pod["nodeSelector"] == {"hangar.dev/runtime": "heavy"}
    assert pod["containers"][0]["securityContext"]["privileged"] is False
    plain = p._pod_template(EnvSpec(name="p"), "i")["spec"]
    assert plain["runtimeClassName"] == "gvisor" and "hangar-docker" not in json.dumps(plain)
    free = _k8s(heavy_node_selector=False, heavy_runtime_class="kata-clh")._pod_template(EnvSpec(name="l", heavy=True),
                                                                                        "i")["spec"]
    assert free["runtimeClassName"] == "kata-clh" and "nodeSelector" not in free and "tolerations" not in free


async def test_heavy_warm_pools_stay_empty_unless_turned_on():
    p = _k8s()
    await p.sync_warm_pool(EnvSpec(name="d", heavy=True, warm_pool=3), "reg/d:1")
    assert p.crd.objects[("sandboxwarmpools", "hangar-d")]["spec"]["replicas"] == 0
    p.settings.heavy_warm_pools = True
    p._synced.clear()
    await p.sync_warm_pool(EnvSpec(name="d", heavy=True, warm_pool=3), "reg/d:1")
    assert p.crd.objects[("sandboxwarmpools", "hangar-d")]["spec"]["replicas"] == 3
    from hangar.config import Settings

    assert heavy.warm(Settings(), EnvSpec(name="s", warm_pool=2)) == 2


def test_docker_provider_kwargs():
    kw = {"mounts": [{"Type": "image", "Target": "/opt/x"}]}
    heavy.docker_kwargs(kw, EnvSpec(name="p"))
    assert "privileged" not in kw
    heavy.docker_kwargs(kw, EnvSpec(name="d", heavy=True))
    assert kw["privileged"] is True and kw["mounts"][-1] == {"Type": "volume", "Target": "/var/lib/docker"}
    heavy.docker_kwargs(kw, None)


async def test_docker_conf_carries_mirrors_and_the_proxy_never_stored(hv):
    f = hv
    f.settings.heavy_registry_mirrors = ["https://mirror.gcr.io"]
    f.settings.heavy_mirror = True
    f.settings.gateway_url = "http://gw.hangar.svc:8788"
    sb = Sandbox(name="s", environment="dind", provider="kubernetes")
    sb.info["egress"] = {"mode": "allowlist", "proxy": "hangar-gateway.hangar.svc:3128"}
    conf = heavy.docker_conf(f, sb, f.catalog.get("dind"))
    from hangar import egress, keys

    token = egress.token(keys.key_for(f.settings, sb.created_at), sb.id)
    assert conf["mirrors"] == [f"http://gw.hangar.svc:8788/registry/{sb.id}/{token}", "https://mirror.gcr.io"]
    assert conf["insecure"] == ["gw.hangar.svc:8788"]
    assert conf["proxy"] == f"http://{sb.id}:{token}@hangar-gateway.hangar.svc:3128"
    assert "gw.hangar.svc" in conf["no_proxy"] and "localhost" in conf["no_proxy"]
    assert token not in json.dumps(sb.info), "the credential is derived, never stored"


async def test_doctor_status_names_the_tier(hv):
    from hangar import envdoctor

    f = hv
    sb = await f.create("big", actor="ann")
    st = envdoctor.build_status(f.settings, sb, f.catalog.get("big"), fleet=f)
    assert st["tier"]["tier"] == "heavy" and st["tier"]["profile"] == "large" and st["tier"]["docker"]
    assert st["tier"]["resources"] == {"cpus": 8.0, "memory_gb": 32.0, "disk_gb": 100.0}
    assert st["runtime"] == "kata"
    plain = await f.create("small", actor="ann")
    assert envdoctor.build_status(f.settings, plain, f.catalog.get("small"), fleet=f)["tier"]["tier"] == "standard"


def test_sandbox_doctor_reports_the_tier_and_docker(monkeypatch):
    from hangar import sandbox_env as se

    st = {"tier": {"tier": "standard", "resources": {"cpus": 2, "memory_gb": 4}, "docker": False}, "runtime": "gvisor"}
    c = se.check_tier(st)
    assert c["status"] == "ok" and "tier standard" in c["message"] and "no Docker here" in c["message"]
    st = {"tier": {"tier": "heavy", "profile": "large", "resources": {"cpus": 8, "memory_gb": 32, "disk_gb": 100},
                   "docker": True, "docker_status": {"ok": False, "error": "no dockerd"}}, "runtime": "kata"}
    monkeypatch.setattr(se, "_docker_ping", lambda sock="/var/run/docker.sock": "No such file or directory")
    c = se.check_tier(st)
    assert c["status"] == "fail" and "profile large (8 CPUs, 32 GiB, 100 GiB disk, kata)" in c["message"]
    assert "no dockerd" in c["message"] and "hangar-docker start" in c["message"]
    monkeypatch.setattr(se, "_docker_ping", lambda sock="/var/run/docker.sock": None)
    assert se.check_tier(st)["status"] == "ok"


def test_the_docker_preset_is_docker_hub():
    from hangar.egress import PRESETS, EgressSpec

    assert "registry-1.docker.io" in PRESETS["docker"] and "auth.docker.io" in PRESETS["docker"]
    assert EgressSpec(mode="allowlist", presets=["docker"]).presets == ["docker"]


def test_repo_policy_fields(hv):
    from hangar.repo_cli import _line

    p = RepoPolicy(repo="acme/app", heavy=True, profile="large", max_profile="xlarge")
    assert "heavy: on, profile large, up to xlarge" in _line({**p.model_dump(), "effective_verify": None})
    with pytest.raises(ValueError):
        RepoPolicy(repo="acme/app", profile="huge")


async def test_a_heavy_pod_is_never_privileged_outside_kata(hv):
    from hangar.fleet import FleetError

    f = hv
    f.settings.heavy_runtime_class = "gvisor"
    with pytest.raises(FleetError, match="Kata runtime class"):
        await f.create("dind")
    with pytest.raises(ValueError, match="isn't a Kata runtime class"):
        heavy.k8s_pod({"containers": [{"securityContext": {}, "resources": {"requests": {}}}]},
                      EnvSpec(name="d", heavy=True), f.settings)


def test_the_cluster_refuses_privileged_sandbox_pods_without_kata():
    """deploy/k8s/heavy-policy.yaml: defence in depth at admission, in the sandbox namespace only."""
    from pathlib import Path

    k8s = Path(__file__).parents[1] / "deploy" / "k8s"
    policy, binding = list(yaml.safe_load_all((k8s / "heavy-policy.yaml").read_text()))
    assert policy["kind"] == "ValidatingAdmissionPolicy" and policy["spec"]["failurePolicy"] == "Fail"
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
    assert set(rule["resources"]) == {"pods", "pods/ephemeralcontainers"} and "CREATE" in rule["operations"]
    expr = policy["spec"]["validations"][0]["expression"]
    assert "startsWith('kata')" in expr and expr.count("privileged") >= 6
    for kind in ("containers", "initContainers", "ephemeralContainers"):
        assert f"object.spec.{kind}" in expr
    assert binding["spec"]["policyName"] == policy["metadata"]["name"]
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert binding["spec"]["matchResources"]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "hangar-sandboxes"}
    assert "heavy-policy.yaml" in yaml.safe_load((k8s / "kustomization.yaml").read_text())["resources"]
