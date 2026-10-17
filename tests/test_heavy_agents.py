"""--- heavy: the in-sandbox halves of the heavy tier, run for real without Docker: the supervisor's Compose stacks
(svc_agent.py, against a fake `docker` that plays `compose up`, `config` and `ps`), and hangar-docker's decisions
(docker_agent.py: storage, daemon.json, the proxy for containers, the socket probe)."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from hangar import docker_agent as da
from hangar import svc_agent, svc_control
from hangar.envspec import EnvSpec
from hangar.models import Sandbox

from .test_svc_control import home, manifest, prog, status, svc, until  # noqa: F401 - the `home` fixture

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""A stand-in for `docker compose`: up (ready after a moment, stopped by SIGTERM), config --services, ps."""
import json, os, re, signal, sys, time
a = sys.argv[1:]
assert a[0] == "compose", a
name, f = a[a.index("-p") + 1], a[a.index("-f") + 1]
state = os.path.join(os.environ["FAKE_DOCKER_STATE"], name + ".json")
svcs = re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", open(f).read(), re.M)
if "config" in a:
    print("\n".join(svcs))
elif "ps" in a:
    try:
        rows = json.load(open(state))
    except (OSError, ValueError):
        rows = []
    for r in rows:
        print(json.dumps(r))
elif "up" in a:
    def write(st, health=""):
        json.dump([{"Service": s, "State": st, "Health": health, "ExitCode": 0} for s in svcs], open(state, "w"))
    def stop(*_):
        write("exited")
        open(state + ".stopped", "w").write("SIGTERM")
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    print("creating " + ", ".join(svcs), flush=True)
    write("running", "starting")
    time.sleep(1.5)
    write("running", "healthy")
    print("all healthy", flush=True)
    while True:
        time.sleep(0.2)
'''


@pytest.fixture
def docker_env(home, tmp_path):  # noqa: F811
    bin_ = tmp_path / "fake-docker"
    bin_.write_text(FAKE_DOCKER)
    bin_.chmod(0o755)
    state = tmp_path / "docker-state"
    state.mkdir()
    (home / "repo" / "compose.yml").write_text("services:\n  db:\n    image: postgres:17\n  cache:\n    image: redis\n")
    return {"HANGAR_SVC_DOCKER": str(bin_), "FAKE_DOCKER_STATE": str(state)}, state


def test_a_compose_stack_comes_up_before_start_and_stops_with_sigterm(home, docker_env):  # noqa: F811
    env, state = docker_env
    manifest(home, start="date +%s.%N > started.txt", compose=[{"name": "deps", "file": "compose.yml"}])
    r = svc(home, "up", **env)
    assert r.returncode == 0, r.stderr
    r = svc(home, "wait", "--timeout", "20", **env)
    assert r.returncode == 0, r.stdout + r.stderr
    st = status(home)
    deps = prog(st, "deps")
    assert deps["kind"] == "compose" and deps["ready"] and deps["state"] == "running"
    assert deps["command"].endswith("compose -p deps -f compose.yml up --remove-orphans --quiet-pull --no-color")
    healthy = json.loads((state / "deps.json").read_text())
    assert {x["Health"] for x in healthy} == {"healthy"}
    started = until(lambda: (home / "repo/started.txt").exists() and float((home / "repo/started.txt").read_text()))
    assert started > (state / "deps.json").stat().st_mtime - 0.5, "start waited for the stack to be healthy"
    assert "all healthy" in (home / ".hangar/svc/logs/deps.log").read_text()
    r = svc(home, "stop", "deps", **env)
    assert r.returncode == 0, r.stderr
    until(lambda: (state / "deps.json.stopped").exists())


def test_svc_add_compose_and_its_usage(home, docker_env):  # noqa: F811
    env, state = docker_env
    manifest(home)
    assert svc(home, "up", **env).returncode == 0
    r = svc(home, "add", "extra", "--compose", "compose.yml", **env)
    assert r.returncode == 0, r.stderr
    r = svc(home, "wait", "extra", "--timeout", "20", **env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (state / "extra.json").exists()
    bad = svc(home, "add", "x", "--compose", "c.yml", "--", "echo", **env)
    assert bad.returncode == 2 and "--compose FILE" in bad.stderr
    assert svc(home, "rm", "extra", **env).returncode == 0
    until(lambda: (state / "extra.json.stopped").exists())


def test_stack_readiness():
    up = [{"Service": "db", "State": "running", "Health": "healthy"}, {"Service": "migrate", "State": "exited",
                                                                       "ExitCode": 0}]
    assert svc_agent.stack_ready(["db", "migrate"], up)
    assert not svc_agent.stack_ready(["db", "migrate", "web"], up), "a service with no container yet"
    assert not svc_agent.stack_ready(["db"], [{"Service": "db", "State": "running", "Health": "starting"}])
    assert not svc_agent.stack_ready(["m"], [{"Service": "m", "State": "exited", "ExitCode": 1}])
    assert not svc_agent.stack_ready([], [])
    lines = "\n".join(json.dumps(x) for x in up)
    assert svc_agent._compose_json(lines) == up == svc_agent._compose_json(json.dumps(up))
    spec = svc_agent.compose_spec({"name": "db", "compose": "a b.yml", "port": 5432})
    assert "-f 'a b.yml' up" in spec["command"] and "ready" not in spec
    assert "compose-ready db" in svc_agent.compose_spec({"name": "db", "compose": "c.yml"})["ready"]


def test_the_manifest_carries_compose_only_where_there_is_docker():
    eff = EnvSpec(name="e", heavy=True, services={"deps": "compose.yml", "pg": {"image": "postgres:17", "port": 1}})
    sb = Sandbox(name="s", environment="e", provider="kubernetes")
    assert svc_control.manifest(sb, eff, "/w")["compose"] == []
    sb.info["heavy"] = {"docker": True}
    m = svc_control.manifest(sb, eff, "/w")
    assert m["compose"] == [{"name": "deps", "file": "compose.yml", "port": None, "ready": None, "env": {}}]
    kinds = {x["name"]: x["kind"] for x in svc_control.programs_info(eff, sb)}
    assert kinds == {"deps": "compose"}
    text = svc_control.instructions(sb, eff, "/w")
    assert "`deps` (`docker compose -p deps -f compose.yml`)" in text


async def test_compose_without_docker_is_a_setup_failure():
    class Prov:
        async def write_file(self, *a, **k):
            pass

        async def exec(self, *a, **k):
            from hangar.models import ExecResult

            return ExecResult(exit_code=0, stdout="", stderr="")

    from hangar.config import Settings

    eff = EnvSpec(name="e", services={"deps": "compose.yml"})
    sb = Sandbox(name="s", environment="e", provider="kubernetes")
    assert await svc_control.start(Prov(), sb, Settings(), eff, "/w") is None
    assert "Compose stacks need Docker" in sb.info["setup_failures"][0]["message"]


# ====================================================================== hangar-docker
def test_storage_plan_loop_mounts_only_on_a_shared_filesystem():
    gib = 1 << 30
    assert da.storage_plan("ext4", 100, 500 * gib) == ("direct", None)
    assert da.storage_plan("tmpfs", 100, 500 * gib) == ("direct", None)
    assert da.storage_plan("virtiofs", 100, 500 * gib) == ("loop", 100 * gib)
    assert da.storage_plan("fuse.kataShared", 100, 500 * gib) == ("loop", 100 * gib)
    assert da.storage_plan("virtiofs", 100, 21 * gib) == ("loop", 20 * gib), "no bigger than the volume has room"
    assert da.storage_plan("virtiofs", 100, gib // 2) == ("loop", gib)


def test_fstype_is_the_longest_mount_prefix(tmp_path):
    m = tmp_path / "mounts"
    m.write_text("overlay / overlay rw 0 0\nkataShared /var/lib/docker virtiofs rw 0 0\n"
                 "tmpfs /var/lib/dockerish tmpfs rw 0 0\n")
    assert da.fstype("/var/lib/docker", str(m)) == "virtiofs"
    assert da.fstype("/var/lib/docker/x", str(m)) == "virtiofs"
    assert da.fstype("/var/lib", str(m)) == "overlay"


def test_daemon_config():
    conf = {"user": "agent", "mirrors": ["http://gw:8788/registry/sb-1/tok", "https://mirror.gcr.io", "ftp://x"],
            "insecure": ["gw:8788"], "proxy": "http://sb-1:tok@proxy:3128", "no_proxy": ["localhost", "gw"]}
    out = da.daemon_config(conf, "/var/lib/docker", 1460)
    assert out["registry-mirrors"] == ["http://gw:8788/registry/sb-1/tok", "https://mirror.gcr.io"]
    assert out["insecure-registries"] == ["gw:8788"] and out["group"] == "agent"
    assert out["hosts"] == ["unix:///var/run/docker.sock"], "nothing listens on TCP"
    assert out["proxies"] == {"http-proxy": conf["proxy"], "https-proxy": conf["proxy"], "no-proxy": "localhost,gw"}
    assert out["mtu"] == 1460 and out["default-network-opts"]["bridge"]["com.docker.network.driver.mtu"] == "1460"
    plain = da.daemon_config({"user": "agent"}, "/d", 1500)
    assert "proxies" not in plain and "mtu" not in plain and "registry-mirrors" not in plain


def test_containers_get_the_proxy_by_address():
    assert da.container_proxy("http://sb-1:tok@proxy.hangar.svc:3128", lambda h: "10.1.2.3") == \
        "http://sb-1:tok@10.1.2.3:3128"
    assert da.container_proxy("http://proxy:3128", lambda h: "10.1.2.3") == "http://10.1.2.3:3128"

    def fail(h):
        raise OSError("no")

    assert da.container_proxy("http://proxy:3128", fail) == "http://proxy:3128"
    assert da.container_proxy(None) is None


def test_ping_and_api_over_a_unix_socket(tmp_path):
    path = str(tmp_path / "d.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)

    def serve():
        for _ in range(2):
            c, _ = srv.accept()
            req = c.recv(4096)
            body = b"OK" if b"/_ping" in req else json.dumps({"ServerVersion": "29.8.2"}).encode()
            c.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + body)
            c.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert da.ping(path)
    assert da.api("/info", path) == {"ServerVersion": "29.8.2"}
    t.join(5)
    srv.close()
    assert not da.ping(path), "nothing listening"


def test_start_needs_root_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert da.main(["start", str(tmp_path / "c.json")]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert not out["ok"] and "sudo" in out["error"]
    assert da.main(["bogus"]) == 2


def test_status_without_a_daemon(monkeypatch, capsys):
    monkeypatch.setattr(da, "ping", lambda *a, **k: False)
    assert da.main(["status"]) == 1
    assert "hangar-docker start" in json.loads(capsys.readouterr().out)["error"]


def test_cgroup_v1_needs_nothing(tmp_path):
    assert da.cgroup_nesting(str(tmp_path)) == "v1"


def test_the_script_is_standalone():
    """It runs in the sandbox with nothing from hangar: the standard library only, Python 3.8 syntax."""
    import ast

    src = Path(da.__file__).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("hangar"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("hangar") for a in node.names)
