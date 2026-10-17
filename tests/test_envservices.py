"""--- envbuilds: services beside a sandbox (envservices.py): which a sandbox gets, Kubernetes sidecars, Docker's
network of the sandbox's own, teardown and the sweep."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hangar import envbuilds, envservices, repoenv, svc_control
from hangar.config import Settings
from hangar.envbuilds import KV_SEEN, Seen
from hangar.envspec import EnvSpec, Service
from hangar.models import Resources, Sandbox, SandboxState
from hangar.repopolicy import repo_key

PG = {"image": "postgres:17", "port": 5432, "env": {"POSTGRES_PASSWORD": "dev", "PGDATA": "/var/lib/pg/data/x"},
      "volumes": ["/var/lib/pg/data"], "ready": "pg_isready -h postgres"}


def _sb(provider: str = "docker", **info) -> Sandbox:
    return Sandbox(name="s", environment="e", provider=provider, repo="https://github.com/o/r.git", info=info)


def test_prepare_takes_the_environments_services(fleet):
    spec = EnvSpec(name="e", services={"redis": {"image": "redis:7", "port": 6379}})
    sb = _sb()
    envservices.prepare(fleet, spec, sb, None)
    assert sb.info["services"] == [{"name": "redis", "kind": "service", "host": "redis", "port": 6379, "check": None,
                                    "image": "redis:7"}]
    assert envservices.specs(sb) == spec.services


def test_prepare_prefers_the_build_then_the_repo_file(fleet):
    spec = EnvSpec(name="e", services={"redis": {"image": "redis:7", "port": 6379}})
    fleet.store.kv_set(f"{KV_SEEN}e:{repo_key('https://github.com/o/r.git')}", Seen(config=repoenv.parse(
        "services: {db: {image: 'postgres:17', port: 5432}}\n").model_dump(mode="json")).model_dump_json())
    sb = _sb()
    envservices.prepare(fleet, spec, sb, None)
    assert set(envservices.specs(sb)) == {"db"}
    sb = _sb()
    envservices.prepare(fleet, spec, sb, {"services": {"pg": PG}})
    assert set(envservices.specs(sb)) == {"pg"}
    sb = _sb()
    envservices.prepare(fleet, spec, sb, {"install": "x"})  # a build whose config names none: the environment's
    assert set(envservices.specs(sb)) == {"redis"}


def test_images_off_the_allowlist_and_unsupported_providers_are_reported(fleet):
    spec = EnvSpec(name="e", services={"db": {"image": "evil.example/pg:1", "port": 5432},
                                       "redis": {"image": "redis", "port": 6379}})
    sb = _sb()
    envservices.prepare(fleet, spec, sb, None)
    assert set(envservices.specs(sb)) == {"redis"}
    assert sb.info["setup_failures"][0]["step"] == "service:db" and "isn't allowed" in sb.info["setup_failures"][0][
        "message"]
    sb = _sb("e2b")
    envservices.prepare(fleet, spec, sb, None)
    assert "service_specs" not in sb.info and "doesn't run services yet" in sb.info["setup_failures"][0]["message"]


def test_services_fit_the_sandboxs_service_budget(fleet):
    """Review: `resources` is an admin's, but each service carries its own cpus and memory_gb, which the repo writes:
    8 services of 4 CPUs and 8 GiB would ask a node for 64 GiB on top of the sandbox (docs/team-scale.md packs 14
    sandboxes of 4 GiB on a node). Their sum must fit HANGAR_ENV_SERVICES_MAX_CPUS and _MEMORY_GB; the rest stay out."""
    fleet.settings.env_services_max_cpus = 1.0
    fleet.settings.env_services_max_memory_gb = 1.0
    spec = EnvSpec(name="e", services={"a": {"image": "redis", "port": 6379},
                                       "b": {"image": "postgres:17", "port": 5432},
                                       "c": {"image": "redis", "port": 6380, "cpus": 4, "memory_gb": 8}})
    sb = _sb()
    envservices.prepare(fleet, spec, sb, None)
    assert set(envservices.specs(sb)) == {"a", "b"}
    fails = [f for f in sb.info["setup_failures"] if f["step"] == "service:c"]
    assert fails and "HANGAR_ENV_SERVICES_MAX_MEMORY_GB" in fails[0]["message"]


def test_services_never_run_hangars_own_images(fleet):
    """Review: hangar's own images (environments, and repo builds that hold other repos' checkouts) are never a
    service, whatever HANGAR_ENV_SERVICE_IMAGES allows."""
    fleet.settings.env_service_images = ["**"]
    fleet.settings.registry = "us-docker.pkg.dev/acme/hangar"
    spec = EnvSpec(name="e", services={
        "x": {"image": "us-docker.pkg.dev/acme/hangar/appenv-repo:abc-0a1b2c3d", "port": 1},
        "y": {"image": f"{fleet.settings.image_prefix}/appenv-repo:abc", "port": 2},
        "z": {"image": "us-docker.pkg.dev/acme/services/pg:17", "port": 3}})
    sb = _sb()
    envservices.prepare(fleet, spec, sb, None)
    assert set(envservices.specs(sb)) == {"z"}
    assert not repoenv.service_image_allowed(fleet.settings, "us-docker.pkg.dev/acme/hangar/appenv:x")


def test_prepare_never_fails_a_create(fleet):
    fleet.store.kv_set(f"{KV_SEEN}e:{repo_key('https://github.com/o/r.git')}", "{broken")
    sb = _sb()
    envservices.prepare(fleet, EnvSpec(name="e"), sb, None)
    assert "couldn't decide the services" in sb.info["setup_failures"][0]["message"]


def test_provider_spec_names_a_template_per_set_of_services_or_build():
    own = EnvSpec(name="e", warm_pool=2, services={"redis": {"image": "redis", "port": 6379}})
    sb = _sb(service_specs={"redis": {"image": "redis", "port": 6379}})
    assert envbuilds.EnvBuilds.provider_spec(own, sb) is own  # the environment's own: its template, warm pool
    sb = _sb(service_specs={"db": PG})
    out = envbuilds.EnvBuilds.provider_spec(own, sb)
    assert out.name.startswith("e-s") and out.warm_pool == 0 and set(out.services) == {"db"}
    assert envbuilds.EnvBuilds.provider_spec(own, _sb(service_specs={"db": PG})).name == out.name  # stable
    sb = _sb(service_specs={"db": PG}, env_build={"id": "bld-0a1b2c3d", "image": "x", "fallback": False})
    out = envbuilds.EnvBuilds.provider_spec(own, sb)
    assert out.name == "e-0a1b2c3d" and set(out.services) == {"db"}


def test_kubernetes_sidecars_share_the_sandboxes_hardening():
    pytest.importorskip("kubernetes")
    from tests.test_kubernetes import provider

    spec = EnvSpec(name="py", resources=Resources(cpus=2, memory_gb=4),
                   services={"postgres": PG, "redis": {"image": "redis:7", "port": 6379, "args": ["--save", ""]}})
    pod = provider(k8s_runtime_class="gvisor")._pod_template(spec, "reg/py:abc")["spec"]
    main, *sidecars = pod["containers"]
    assert [c["name"] for c in sidecars] == ["svc-postgres", "svc-redis"]
    pg = sidecars[0]
    assert pg["securityContext"] == main["securityContext"]  # no capabilities, no privilege escalation
    assert pg["securityContext"] is not main["securityContext"]
    assert {"name": "PGDATA", "value": "/var/lib/pg/data/x"} in pg["env"]
    assert pg["volumeMounts"] == [{"name": "svc-postgres-0", "mountPath": "/var/lib/pg/data"}]
    assert {"name": "svc-postgres-0", "emptyDir": {}} in pod["volumes"]
    assert pg["resources"]["limits"] == {"cpu": "0.5", "memory": "512Mi"}
    assert sidecars[1]["args"] == ["--save", ""] and "command" not in sidecars[1]
    assert pod["hostAliases"] == [{"ip": "127.0.0.1", "hostnames": ["postgres", "redis"]}]
    assert pod["runtimeClassName"] == "gvisor" and pod["securityContext"]["runAsNonRoot"]  # pod-wide: uid 1000
    plain = provider()._pod_template(EnvSpec(name="py"), "i")["spec"]
    assert len(plain["containers"]) == 1 and "hostAliases" not in plain
    # integration (#97 + #99): a proxied pod has no resolver, so its services' names and hangar's own both stay
    # in /etc/hosts
    dns = {"dnsPolicy": "None", "dnsConfig": {"nameservers": ["127.0.0.1"]},
           "hostAliases": [{"ip": "10.0.0.5", "hostnames": ["hangar-gateway.hangar.svc"]}]}
    proxied = EnvSpec(**{**spec.model_dump(exclude={"source_dir", "packs"}), "egress": {"mode": "allowlist"}})
    pod = provider()._pod_template(proxied, "i", dns)["spec"]
    assert pod["dnsPolicy"] == "None" and pod["hostAliases"] == [
        {"ip": "127.0.0.1", "hostnames": ["postgres", "redis"]},
        {"ip": "10.0.0.5", "hostnames": ["hangar-gateway.hangar.svc"]}]


# ---------------------------------------------------------------------- Docker
class FakeDocker:
    """Just enough of docker-py for envservices' Docker side, recording the calls in order."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.containers_ = {}
        self.networks_ = {}
        outer = self

        class C:
            def __init__(self, cid, kw):
                self.id, self.kw, self.labels = cid, kw, kw.get("labels") or {}
                self.attrs = {"NetworkSettings": {"Networks": {kw.get("network"): {"IPAddress": f"10.9.0.{cid}"}}}}

            def reload(self):
                pass

            def start(self):
                outer.calls.append(("start", self.id))

            def remove(self, force=False):
                outer.calls.append(("remove", self.id))
                outer.containers_.pop(self.id, None)

        class Containers:
            def run(self, **kw):
                c = C(str(len(outer.containers_) + 2), kw)
                outer.containers_[c.id] = c
                outer.calls.append(("run", kw["name"]))
                return c

            def create(self, **kw):
                assert "detach" not in kw
                c = C("sandbox", kw)
                outer.containers_[c.id] = c
                outer.calls.append(("create", kw["name"]))
                return c

            def list(self, all=False, filters=None):
                k, _, v = (filters or {}).get("label", "").partition("=")
                return [c for c in outer.containers_.values() if k in c.labels and (not v or c.labels[k] == v)]

        class N:
            def __init__(self, name, labels):
                self.name, self.id, self.attrs = name, f"id-{name}", {"Labels": labels, "Containers": {}}

            def reload(self):
                pass

            def remove(self):
                if self.attrs["Containers"]:
                    raise RuntimeError("network has active endpoints")  # what Docker says
                outer.calls.append(("netrm", self.name))
                outer.networks_.pop(self.name, None)

        class Networks:
            def create(self, name, **kw):
                outer.calls.append(("net", name, kw["internal"]))
                outer.networks_[name] = N(name, kw["labels"])

            def list(self, names=None, filters=None):
                return [n for n in outer.networks_.values() if (not names or n.name in names)]

        class Api:
            def connect_container_to_network(self, cid, net):
                outer.calls.append(("connect", cid, net))
                outer.networks_[net].attrs["Containers"][cid] = {}

            def disconnect_container_from_network(self, cid, net_id, force=False):
                outer.calls.append(("disconnect", cid))
                next(n for n in outer.networks_.values() if n.id == net_id).attrs["Containers"].pop(cid)

        self.containers, self.networks, self.api = Containers(), Networks(), Api()


async def test_docker_services_on_a_network_of_the_sandboxes_own():
    client = FakeDocker()
    prov = SimpleNamespace(client=client, settings=Settings(docker_runtime="runsc"))
    sb = _sb(service_specs={"postgres": PG})
    kw = {"image": "img", "name": "hangar-sb-1", "detach": True, "network": "hangar-sandboxes",
          "extra_hosts": {"host.docker.internal": "host-gateway"}}
    c = await envservices.docker_run(prov, sb, kw)
    net = f"hangar-svc-{sb.id}"
    assert client.calls[0] == ("net", net, True)  # internal: no route out, no metadata server
    svc = client.containers_["2"].kw
    assert svc["user"] == "1000:1000" and svc["cap_drop"] == ["ALL"] and svc["security_opt"] == ["no-new-privileges"]
    assert svc["runtime"] == "runsc" and svc["network"] == net and svc["tmpfs"] == {"/var/lib/pg/data": "mode=1777"}
    assert svc["labels"] == {envservices.LABEL: sb.id, "dev.hangar.svc": "postgres"}
    assert c.kw["extra_hosts"] == {"host.docker.internal": "host-gateway", "postgres": "10.9.0.2"}
    assert client.calls[-3:] == [("create", "hangar-sb-1"), ("connect", "sandbox", net), ("start", "sandbox")]
    await envservices.docker_teardown(prov, sb)  # the sandbox is still on the network: it's let go first
    assert ("remove", "2") in client.calls and ("disconnect", "sandbox") in client.calls
    assert ("netrm", net) in client.calls


async def test_the_sweep_removes_services_whose_sandbox_is_gone(fleet):
    client = FakeDocker()
    fleet.providers["docker"] = SimpleNamespace(client=client)
    gone, live = _sb(service_specs={"db": PG}), _sb(service_specs={"db": PG})
    for s in (gone, live):
        client.networks.create(f"hangar-svc-{s.id}", internal=True, labels={envservices.LABEL: s.id})
        client.containers.run(name=f"hangar-svc-{s.id}-db", labels={envservices.LABEL: s.id}, network="n")
    gone.state, gone.last_activity = SandboxState.STOPPED, 0
    live.state = SandboxState.RUNNING
    fleet.store.put(gone)
    fleet.store.put(live)
    assert await envservices.sweep(fleet) == 1
    assert {c.labels[envservices.LABEL] for c in client.containers_.values()} == {live.id}
    assert set(client.networks_) == {f"hangar-svc-{live.id}"}


def test_the_supervisor_and_the_agent_learn_the_services():
    eff = EnvSpec(name="e", services={"postgres": PG})
    sb = _sb(services=[{"name": "postgres", "kind": "service", "host": "postgres", "port": 5432,
                        "check": "pg_isready -h postgres"}])
    m = svc_control.manifest(sb, eff, "/workspace/r")
    assert m["services"] == [{"name": "postgres", "host": "postgres", "port": 5432, "ready": "pg_isready -h postgres"}]
    text = svc_control.instructions(sb, eff, "/workspace/r")
    assert "`postgres` at `postgres:5432`" in text
    assert Service.model_validate(PG).volumes == ["/var/lib/pg/data"]
