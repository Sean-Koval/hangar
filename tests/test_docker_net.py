"""hangar's Docker network for sandboxes (workstream isolation): created on first use with inter-container traffic
off, used for every sandbox and warm-pool container, left alone when the operator names one; doctor says when
sandboxes can reach each other. The real engine is exercised by tests/test_e2e_isolation_docker.py."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from docker.errors import APIError, NotFound

from hangar.config import Settings
from hangar.providers.base import ProviderError
from hangar.providers.docker_net import BRIDGE, ICC, NAME, OPTIONS, Network, doctor_check, network


class Api:
    def __init__(self, nets: dict | None = None, create_error: int | None = None):
        self.nets = dict(nets or {})
        self.created: list[tuple] = []
        self.create_error = create_error

    def inspect_network(self, name):
        if name not in self.nets:
            raise NotFound(f"network {name} not found")
        return self.nets[name]

    def create_network(self, name, driver=None, options=None, labels=None):
        self.created.append((name, driver, options, labels))
        if self.create_error:
            self.nets[name] = {"Driver": "bridge", "Options": OPTIONS}  # someone else's create won the race
            raise APIError("conflict", response=SimpleNamespace(status_code=self.create_error, reason="x"),
                           explanation="bridge name conflict")
        self.nets[name] = {"Driver": driver, "Options": options}


def client(api, containers=()):
    return SimpleNamespace(api=api, containers=SimpleNamespace(list=lambda filters=None: list(containers)))


def test_sandboxes_go_on_hangars_network_unless_one_is_named():
    assert network(Settings()) == NAME
    assert network(Settings(docker_network="bridge")) == "bridge"
    assert OPTIONS == {ICC: "false", "com.docker.network.bridge.name": BRIDGE}


async def test_created_once_with_icc_off():
    api = Api()
    n = Network(client(api), Settings())
    await n.ensure()
    await n.ensure()
    assert api.created == [(NAME, "bridge", OPTIONS, {"dev.hangar.network": "sandboxes"})]


async def test_a_race_with_another_control_plane_is_fine_and_other_errors_say_what_to_do():
    api = Api(create_error=409)
    await Network(client(api), Settings()).ensure()
    with pytest.raises(ProviderError, match="HANGAR_DOCKER_NETWORK"):
        await Network(client(Api(create_error=500)), Settings()).ensure()


async def test_an_operators_network_is_used_as_it_is(caplog):
    api = Api()
    await Network(client(api), Settings(docker_network="mynet")).ensure()
    assert api.created == []
    api = Api({NAME: {"Driver": "bridge", "Options": {}}})  # made by hand, without the option
    await Network(client(api), Settings()).ensure()
    assert api.created == [] and "lets sandboxes reach each other" in caplog.text


def test_the_provider_puts_every_container_on_it(monkeypatch):
    from hangar.providers.docker import DockerProvider

    prov = DockerProvider.__new__(DockerProvider)
    res = SimpleNamespace(cpus=1, memory_gb=1)
    for setting, want in ((None, NAME), ("bridge", "bridge"), ("host", "host")):
        prov.settings = Settings(docker_network=setting)
        kw = prov._run_kwargs("n", "img", res, {}, hostname="h")
        assert kw["network"] == want


async def test_warm_pool_members_on_another_network_are_replaced_and_never_claimed():
    from hangar.envspec import EnvSpec
    from hangar.providers.docker import POOL_LABEL, DockerProvider

    class C:
        def __init__(self, name, net):
            self.name, self.status, self.removed = name, "running", False
            self.labels = {POOL_LABEL: "img", "dev.hangar.env": "e"}
            self.attrs = {"NetworkSettings": {"Networks": {net: {}}}}

        def remove(self, force=False):
            self.removed = True

    old = C("hangar-pool-e-1", "bridge")
    runs: list[dict] = []
    prov = DockerProvider.__new__(DockerProvider)
    prov.settings = Settings()
    prov._pool_lock = asyncio.Lock()
    prov.network = Network(client(Api()), prov.settings)
    prov.client = SimpleNamespace(containers=SimpleNamespace(
        list=lambda all=False, filters=None: [old], run=lambda **kw: runs.append(kw)))
    assert await prov._claim_warm("img") is None  # an old member is not handed out
    await prov.sync_warm_pool(EnvSpec(name="e", warm_pool=1), "img")
    assert old.removed and [r["network"] for r in runs] == [NAME]


def test_doctor():
    s = Settings()
    on = SimpleNamespace(name="hangar-sb-1", attrs={"NetworkSettings": {"Networks": {NAME: {}}}})
    off = SimpleNamespace(name="hangar-sb-2", attrs={"NetworkSettings": {"Networks": {"bridge": {}}}})
    ok = {"Driver": "bridge", "Options": OPTIONS}
    assert doctor_check(client(Api()), s)["status"] == "ok"  # made with the first sandbox
    assert doctor_check(client(Api({NAME: ok}), [on]), s)["status"] == "ok"
    c = doctor_check(client(Api({NAME: ok}), [on, off]), s)
    assert c["status"] == "warn" and "hangar-sb-2" in c["detail"]
    c = doctor_check(client(Api({"bridge": {"Driver": "bridge", "Options": {}}})), Settings(docker_network="bridge"))
    assert c["status"] == "warn" and "reach each other" in c["detail"] and "unset HANGAR_DOCKER_NETWORK" in c["fix"]
    assert doctor_check(client(Api()), Settings(docker_network="nope"))["status"] == "fail"
    assert doctor_check(client(Api()), Settings(docker_network="host"))["status"] == "warn"
