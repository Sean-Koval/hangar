"""Sandbox isolation end to end on a real Docker engine (workstream isolation).

Network: two sandboxes on hangar's network can't reach each other's ports (an app port, hangar-envd's), while the
control plane still reaches both and a sandbox still reaches the host (where the git gateway listens).

Opt-in (needs Docker; hangar-envd from envd/build.sh or HANGAR_ENVD_DIR for the envd part):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_isolation_docker.py -v
"""

from __future__ import annotations

import asyncio
import os
import shlex
import socket
from pathlib import Path

import pytest

from hangar.envd import PORT as ENVD_PORT
from hangar.envd import find_binaries

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

ROOT = Path(__file__).parents[1]


async def _reach(fleet, ref: str, host: str, port: int) -> bool:
    """Whether a TCP connection from the sandbox to host:port opens within 3 s."""
    r = await fleet.exec(ref, f"timeout 3 bash -c 'exec 3<>/dev/tcp/{host}/{port}' && echo open || echo shut")
    return r.stdout.strip() == "open"


async def test_sandboxes_cant_reach_each_other_on_docker(tmp_path):
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.providers.docker_net import NAME, isolates

    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs"], reaper_interval_seconds=3600, envd_dir=envd_dir,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    prov = fleet.provider("docker")
    host_srv = None
    try:
        a = await fleet.create("minimal", name="e2e-iso-a", labels={"e2e-iso": "1"}, actor="test")
        b = await fleet.create("minimal", name="e2e-iso-b", labels={"e2e-iso": "1"}, actor="test")
        assert isolates(prov.client.api.inspect_network(NAME))
        ips = {}
        for sb in (a, b):
            attrs = prov.client.api.inspect_container(fleet.get(sb.id).provider_ref)
            assert list(attrs["NetworkSettings"]["Networks"]) == [NAME]
            ips[sb.id] = attrs["NetworkSettings"]["Networks"][NAME]["IPAddress"]
        await fleet.exec(a.id, "nohup python3 -m http.server 8000 >/dev/null 2>&1 &")
        for _ in range(50):  # the control plane (this host) reaches it: the server is up
            try:
                socket.create_connection((ips[a.id], 8000), timeout=1).close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("the app in sandbox a never answered the host")

        assert not await _reach(fleet, b.id, ips[a.id], 8000)
        assert await _reach(fleet, a.id, "127.0.0.1", 8000)  # its own app, from inside: fine

        if find_binaries(s):  # hangar-envd: reachable by hangar, not by the other sandbox
            assert await prov.envd.ready(fleet.get(a.id)) is not None
            assert not await _reach(fleet, b.id, ips[a.id], ENVD_PORT)

        # The host stays reachable (the git gateway listens there, on the default bridge's address).
        gw = prov.client.api.inspect_network("bridge")["IPAM"]["Config"][0]["Gateway"]
        host_srv = await asyncio.start_server(lambda r, w: w.close(), gw, 0)
        port = host_srv.sockets[0].getsockname()[1]
        assert await _reach(fleet, b.id, shlex.quote(gw), port)
    finally:
        if host_srv is not None:
            host_srv.close()
        await fleet.destroy_many(label="e2e-iso")
        await fleet.stop()
