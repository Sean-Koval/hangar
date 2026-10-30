"""The e2b provider in each flavour (cloud, selfhosted, cube), without KVM (docs/microvm.md).

`FakeE2B` stands in for E2B and CubeSandbox: a small control API with each flavour's quirks (CubeAPI's 405 on the v2
routes and its missing envd token, a CubeProxy-like traffic-token check), whose sandboxes each run a real hangar-envd
daemon. hangar-envd takes E2B's X-Access-Token, so commands and files cross the real envd wire protocol.

    uv run --extra e2b pytest tests/test_e2b_provider.py
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from hangar.config import Settings
from hangar.envd import MAX_OUTPUT
from hangar.models import Sandbox, SandboxState
from hangar.providers.base import ProviderError, Unavailable
from hangar.providers.e2b import E2BProvider
from tests.conftest import FakeProvider, scratch_root
from tests.test_envd import ARCH, BINARIES, free_port, kill_daemon

needs_binary = pytest.mark.skipif(ARCH not in BINARIES, reason="no hangar-envd binary (run envd/build.sh)")
USER = getpass.getuser()  # the daemons run as whoever runs the tests, and act only as that user


# ---------------------------------------------------------------------------- a fake E2B / CubeAPI
def _streamed(status: int, body: dict) -> httpx.Response:
    """An edge's own answer as a network one arrives: a stream not read yet (envd.py reads error bodies raw)."""
    return httpx.Response(status, headers={"content-type": "application/json"},
                          stream=httpx.ByteStream(json.dumps(body).encode()))


class FakeE2B:
    """E2B's control API as `flavor` serves it, plus the sandbox edge (`edge`, an httpx transport). Every sandbox is
    a directory with a real hangar-envd on 127.0.0.1. `token=False` answers like CubeAPI: no envdAccessToken, and the
    edge (like CubeProxy) checks the traffic token and authenticates to the daemon itself."""

    def __init__(self, flavor: str, token: bool = True, domain: str = "e2b.app", traffic: bool = True):
        self.flavor, self.token, self.domain, self.traffic = flavor, token, domain, traffic
        self.root = Path(tempfile.mkdtemp(prefix="e2bfake-", dir=scratch_root()))
        self.sbs: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.starts: list[dict] = []  # process.Process/Start bodies the edge saw
        self.edge_hosts: list[str] = []
        self.full = False
        self.real = httpx.AsyncClient(trust_env=False, timeout=60)
        self.app = self._app()

    # ---- control API
    def _app(self) -> FastAPI:
        app = FastAPI()
        cube = self.flavor == "cube"

        @app.middleware("http")
        async def record(request: Request, call_next):
            self.calls.append((request.method, request.url.path))
            if request.headers.get("x-api-key") != "e2b_test" and not cube:
                return JSONResponse({"code": 401, "message": "Invalid API key"}, status_code=401)
            return await call_next(request)

        def view(sid: str, traffic: bool = True) -> dict:
            sb = self.sbs[sid]
            return {"templateID": sb["template"], "sandboxID": sid, "clientID": "x", "envdVersion": "0.5.7",
                    "envdAccessToken": sb["token"] if self.token else None,
                    "trafficAccessToken": sb["traffic"] if traffic else None, "domain": None}

        async def create(request: Request):
            if self.full:
                return JSONResponse({"code": 429, "message": "concurrency limit"}, status_code=429)
            body = await request.json()
            assert body["network"] == {"allowPublicTraffic": False}
            sid = "i" + secrets.token_hex(8)
            box = self.root / sid
            box.mkdir()
            port, token = free_port(), secrets.token_hex(16)
            p = subprocess.run([str(BINARIES[ARCH]), "--listen", "127.0.0.1", "--port", str(port), "--daemon",
                                "--workdir", str(box)], env={**os.environ, "HANGAR_ENVD_TOKEN": token},
                               capture_output=True, text=True)
            assert p.returncode == 0, p.stderr
            self.sbs[sid] = {"template": body["templateID"], "metadata": body.get("metadata") or {}, "port": port,
                             "token": token, "traffic": secrets.token_hex(8) if self.traffic else None,
                             "state": "running", "box": box,
                             "timeout": body.get("timeout")}
            return JSONResponse(view(sid), status_code=201)

        @app.post("/v2/sandboxes")
        async def create_v2(request: Request):
            if cube:  # CubeAPI routes /v2/sandboxes as GET-only (spec 0013 §5.4)
                return Response(status_code=405)
            return await create(request)

        @app.post("/sandboxes")
        async def create_v1(request: Request):
            return await create(request)

        @app.get("/v2/sandboxes")
        async def list_v2(limit: int = 100, nextToken: str | None = None):  # noqa: N803 - E2B's name
            ids = sorted(self.sbs)
            start = ids.index(nextToken) if nextToken in ids else 0
            page = ids[start:start + limit]
            rows = [{"sandboxID": i, "templateID": self.sbs[i]["template"], "state": self.sbs[i]["state"],
                     "metadata": self.sbs[i]["metadata"]} for i in page]
            nxt = ids[start + limit] if start + limit < len(ids) else None
            return JSONResponse(rows, headers={"x-next-token": nxt} if nxt else None)

        def known(sid: str):
            if sid not in self.sbs:
                return JSONResponse({"code": 404, "message": f"sandbox {sid} not found"}, status_code=404)
            return None

        @app.get("/sandboxes/{sid}")
        async def get(sid: str):
            if r := known(sid):
                return r
            return {**view(sid, traffic=not cube), "state": self.sbs[sid]["state"]}

        @app.delete("/sandboxes/{sid}")
        async def kill(sid: str):
            if r := known(sid):
                return r
            kill_daemon(self.sbs.pop(sid)["port"])
            return Response(status_code=204)

        @app.post("/sandboxes/{sid}/timeout")
        async def timeout(sid: str, request: Request):
            if r := known(sid):
                return r
            self.sbs[sid]["timeout"] = (await request.json())["timeout"]
            return Response(status_code=204)

        @app.post("/sandboxes/{sid}/pause")
        async def pause(sid: str):
            if r := known(sid):
                return r
            if self.sbs[sid]["state"] != "running":
                return JSONResponse({"code": 409, "message": "not running"}, status_code=409)
            self.sbs[sid]["state"] = "paused"
            return Response(status_code=204)

        async def connect(sid: str):
            if r := known(sid):
                return r
            sb = self.sbs[sid]
            resumed = sb["state"] == "paused"
            if resumed:  # a restored microVM gets fresh tokens; the daemon's own stays (it is the same process)
                sb["state"] = "running"
                if not cube:  # CubeProxy keeps the one CubeMaster stored at create
                    sb["traffic"] = secrets.token_hex(8)
            # review(#64): CubeAPI's connect reads the sandbox back without its traffic token (services/sandboxes.rs)
            return JSONResponse(view(sid, traffic=not cube), status_code=201 if resumed and not cube else 200)

        @app.post("/v2/sandboxes/{sid}/connect")
        async def connect_v2(sid: str):
            if cube:
                return Response(status_code=405)
            return await connect(sid)

        @app.post("/sandboxes/{sid}/connect")
        async def connect_v1(sid: str):
            return await connect(sid)

        @app.post("/sandboxes/{sid}/snapshots")
        async def snapshot(sid: str, request: Request):
            if r := known(sid):
                return r
            return JSONResponse({"snapshotID": "snap-" + (await request.json())["name"], "names": []}, status_code=201)

        @app.get("/templates/aliases/{alias}")
        async def alias(alias: str):
            if alias.startswith("hangar-built"):
                return {"templateID": "tpl-" + alias[-6:], "public": False}
            return JSONResponse({"code": 404, "message": "no such alias"}, status_code=404)

        @app.post("/templates")
        async def build(request: Request):
            self.built = await request.json()
            return JSONResponse({"jobID": "job-1", "templateID": "tpl-new", "status": "accepted", "phase": "",
                                 "progress": 0}, status_code=202)

        @app.get("/templates/{tid}/builds/{bid}/status")
        async def build_status(tid: str, bid: str):
            assert (tid, bid) == ("tpl-new", "job-1")
            return {"buildID": bid, "templateID": tid, "status": "READY", "progress": 100, "message": "done"}

        return app

    # ---- the sandbox edge
    async def edge(self, request: httpx.Request) -> httpx.Response:
        """E2B's client proxy (route by E2b-Sandbox-Id on sandbox.<domain>, else by host label), or CubeProxy (the
        traffic token, then the daemon with a token the client never sees)."""
        self.edge_hosts.append(request.url.host)
        sid = request.headers.get("e2b-sandbox-id", "")
        if not request.url.host.startswith("sandbox."):
            assert request.url.host.split(".", 1)[0] == f"49983-{sid}", request.url.host
        sb = self.sbs.get(sid)
        if sb is None or sb["state"] != "running":
            return _streamed(502, {"message": "sandbox not running"})
        if request.headers.get("e2b-traffic-access-token") != sb["traffic"]:
            return _streamed(401, {"message": "traffic token mismatch"})
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "e2b-traffic-access-token", "content-length")}
        if not self.token:
            assert "x-access-token" not in headers
            headers["x-access-token"] = sb["token"]
        body = await request.aread()
        if request.url.path == "/process.Process/Start":
            self.starts.append(json.loads(body[5:]))
        up = self.real.build_request(request.method, f"http://127.0.0.1:{sb['port']}{request.url.raw_path.decode()}",
                                     headers=headers, content=body)
        resp = await self.real.send(up, stream=True)
        return httpx.Response(resp.status_code, headers=resp.headers, stream=resp.stream)

    def provider(self, **kw) -> E2BProvider:
        s = Settings(data_dir=self.root / "data", e2b_flavor=self.flavor, e2b_api_key="e2b_test",
                     e2b_domain=self.domain, sandbox_user=USER, workspace_dir=str(self.root),
                     e2b_api_url="http://cubeapi.test:3000" if self.flavor == "cube" else None, **kw)
        p = E2BProvider(s)
        p.api.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=p.api.base,
                                       headers=p.api.http.headers)
        p.dataplane._http = httpx.AsyncClient(transport=httpx.MockTransport(self.edge), timeout=60)
        return p

    async def close(self) -> None:
        for sb in self.sbs.values():
            kill_daemon(sb["port"])
        await self.real.aclose()
        subprocess.run(["rm", "-rf", str(self.root)], check=False)


@pytest.fixture
async def fake_e2b():
    made: list[FakeE2B] = []

    def make(flavor: str, **kw) -> FakeE2B:
        f = FakeE2B(flavor, **kw)
        made.append(f)
        return f

    yield make
    for f in made:
        await f.close()


def sandbox(env: str = "py") -> Sandbox:
    return Sandbox(name="t", environment=env, provider="e2b", state=SandboxState.STARTING)


# ---------------------------------------------------------------------------- configuration
def test_flavour_configuration_is_checked():
    with pytest.raises(ProviderError, match="HANGAR_E2B_API_URL"):
        E2BProvider(Settings(e2b_flavor="cube"))
    with pytest.raises(ProviderError, match="HANGAR_E2B_DOMAIN"):
        E2BProvider(Settings(e2b_flavor="selfhosted", e2b_api_key="k"))
    with pytest.raises(ProviderError, match="HANGAR_E2B_API_KEY"):
        E2BProvider(Settings(e2b_flavor="cloud"))
    cube = E2BProvider(Settings(e2b_flavor="cube", e2b_api_url="http://cubeapi:3000/"))  # Cube's auth is optional
    assert cube.api.base == "http://cubeapi:3000" and "x-api-key" not in cube.api.http.headers
    own = E2BProvider(Settings(e2b_flavor="selfhosted", e2b_api_key="k", e2b_domain="e2b.corp"))
    assert own.api.base == "https://api.e2b.corp" and own.api.http.headers["x-api-key"] == "k"
    assert own.capabilities.hot_resume and own.capabilities.pause
    assert not hasattr(own, "envd"), "hangar's E2B API and router must never take this provider for a hangar-envd one"


def test_envd_urls_per_flavour():
    sb = sandbox()
    sb.provider_ref = "abc123"
    d = {"envdAccessToken": "tok", "trafficAccessToken": "tr", "envdVersion": "0.5.7"}
    cloud = E2BProvider(Settings(e2b_api_key="k"))
    ep = cloud._endpoint(sb, d)
    assert ep.url == "https://sandbox.e2b.app"  # E2B's own domains: one host, routed by header
    assert ep.headers == {"E2b-Sandbox-Id": "abc123", "E2b-Sandbox-Port": "49983", "X-Access-Token": "tok",
                          "e2b-traffic-access-token": "tr"}
    own = E2BProvider(Settings(e2b_flavor="selfhosted", e2b_api_key="k", e2b_domain="e2b.corp"))
    assert own._endpoint(sb, d).url == "https://49983-abc123.e2b.corp"
    assert own._endpoint(sb, {**d, "domain": "eu.e2b.corp"}).url == "https://49983-abc123.eu.e2b.corp"
    tpl = E2BProvider(Settings(e2b_flavor="cube", e2b_api_url="http://c:3000",
                               e2b_sandbox_url="http://{port}-{sandbox_id}.cube.internal"))
    assert tpl._endpoint(sb, d).url == "http://49983-abc123.cube.internal"
    one = E2BProvider(Settings(e2b_flavor="selfhosted", e2b_api_key="k", e2b_api_url="http://h/e2b",
                               e2b_sandbox_url="http://h/e2b/sandbox/"))
    assert one._endpoint(sb, d).url == "http://h/e2b/sandbox"
    with pytest.raises(ProviderError, match="no envd access token"):
        tpl._endpoint(sb, {"trafficAccessToken": "tr"})
    for bad in ("evil.example/x", "user@evil.example", "a b", "evil.example:443/"):  # the tokens go to this host
        with pytest.raises(ProviderError, match="unexpected sandbox domain"):
            own._endpoint(sb, {**d, "domain": bad})


def test_templates_map_environments():
    p = E2BProvider(Settings(e2b_api_key="k", e2b_environment_templates={"py": "my-team/python-ml"}))
    assert p.image_ref("reg.example/hangar/py:0123abcd") == "my-team/python-ml"
    assert p.image_ref("reg.example/hangar/node:0123abcd") == "hangar-node:0123abcd"
    cube = E2BProvider(Settings(e2b_flavor="cube", e2b_api_url="http://c:3000"))
    assert cube.image_ref("reg.example/hangar/Node:0123456789abcdef0123") == "hangar-node-0123456789abcdef"


# ---------------------------------------------------------------------------- against the fake, per flavour
@needs_binary
@pytest.mark.parametrize("flavor,domain", [("cloud", "e2b.app"), ("selfhosted", "e2b.corp"), ("cube", "cube.test")])
async def test_lifecycle_exec_files_and_hot_resume(fake_e2b, flavor, domain):
    fake = fake_e2b(flavor, domain=domain, token=flavor != "cube")
    prov = fake.provider(e2b_allow_unauthenticated_envd=flavor == "cube")
    sb = sandbox()
    await prov.create(sb, "hangar-py:abc")
    created = ("POST", "/sandboxes") if flavor == "cube" else ("POST", "/v2/sandboxes")
    assert created in fake.calls and ("POST", "/v2/sandboxes") not in fake.calls[1:]
    assert fake.sbs[sb.provider_ref]["metadata"] == {"hangar_id": sb.id, "hangar_env": "py"}
    traffic = fake.sbs[sb.provider_ref]["traffic"]
    assert {k: v for k, v in sb.info["e2b"].items() if k != "traffic"} == {"flavor": flavor, "envd_version": "0.5.7"}
    assert traffic not in json.dumps(sb.info) and prov._traffic(sb) == traffic, "kept with the sandbox, sealed"
    assert await prov.status(sb) == SandboxState.RUNNING
    sb.state = SandboxState.RUNNING

    r = await prov.exec(sb, "echo hi; echo err >&2; exit 3")
    assert (r.exit_code, r.stdout, r.stderr, r.transport) == (3, "hi\n", "err\n", "envd")
    start = fake.starts[-1]["process"]
    assert start["cmd"] == "timeout" and start["args"][-3:] == ["bash", "-lc", "echo hi; echo err >&2; exit 3"], \
        "one login shell per exec, not `bash -lc` inside the SDK's own `bash -l -c`"
    expected_host = "sandbox.e2b.app" if flavor == "cloud" else f"49983-{sb.provider_ref}.{domain}"
    assert fake.edge_hosts[-1] == expected_host

    # #48's caps: a bounded tail of the output, reads stop at the limit
    r = await prov.exec(sb, "head -c 3000000 /dev/zero | tr '\\0' x")
    assert r.truncated and len(r.stdout) <= MAX_OUTPUT + 64
    await prov.write_file(sb, f"{fake.root}/{sb.provider_ref}/big.bin", b"y" * 200_000)
    assert len(await prov.read_file(sb, f"{fake.root}/{sb.provider_ref}/big.bin", limit=1000)) == 1001
    path = f"{fake.root}/{sb.provider_ref}/sub/a.sh"
    await prov.write_file(sb, path, b"#!/bin/sh\necho ok\n", mode=0o755)
    assert await prov.read_file(sb, path) == b"#!/bin/sh\necho ok\n"
    assert (await prov.exec(sb, f"{path}")).stdout == "ok\n"
    assert await prov.read_file(sb, f"{fake.root}/nope") is None

    # pause keeps the microVM (here: the daemon and its processes); resume reconnects with fresh tokens
    await prov.exec(sb, "nohup sleep 30 >/dev/null 2>&1 & echo $! > proc.pid")
    old_traffic = fake.sbs[sb.provider_ref]["traffic"]
    await prov.pause(sb)
    sb.state = SandboxState.PAUSED
    assert await prov.status(sb) == SandboxState.PAUSED
    with pytest.raises(ProviderError, match="paused"):  # nothing reconnects (and so resumes) a paused sandbox
        await prov.exec(sb, "true")
    await prov.pause(sb)  # already paused: fine
    await prov.resume(sb)
    sb.state = SandboxState.RUNNING
    assert (fake.sbs[sb.provider_ref]["traffic"] == old_traffic) == (flavor == "cube")
    connect = ("POST", f"/sandboxes/{sb.provider_ref}/connect") if flavor == "cube" else \
        ("POST", f"/v2/sandboxes/{sb.provider_ref}/connect")
    assert connect in fake.calls
    r = await prov.exec(sb, "kill -0 $(cat proc.pid) && echo alive")
    assert r.stdout == "alive\n", r.stderr

    # a control plane restart: the token is asked for again (connect on a running sandbox only extends it)
    again = fake.provider(e2b_allow_unauthenticated_envd=flavor == "cube")
    assert (await again.exec(sb, "echo again")).stdout == "again\n"
    # a token that stopped working (rotated elsewhere) is asked for again, once, for commands, reads and writes
    def stale() -> None:
        h = prov.dataplane._eps[sb.id].headers
        h.update({k: "stale" for k in ("X-Access-Token", "e2b-traffic-access-token") if k in h})

    stale()
    assert (await prov.exec(sb, "echo fresh")).stdout == "fresh\n"
    stale()
    assert await prov.read_file(sb, path, limit=100) == b"#!/bin/sh\necho ok\n"
    stale()
    await prov.write_file(sb, path, b"#!/bin/sh\necho ok\n")
    assert prov.dataplane._eps[sb.id].headers["e2b-traffic-access-token"] != "stale"
    if flavor == "cube":  # review(#64): connect never hands it out again; without the sealed copy, envd is unreachable
        sealed, sb.info["e2b"]["traffic"] = sb.info["e2b"]["traffic"], "garbage"
        lost = fake.provider(e2b_allow_unauthenticated_envd=True)
        with pytest.raises(ProviderError, match="neither an envd access token nor a traffic token"):
            await lost.lookup(sb)
        await lost.close()
        sb.info["e2b"]["traffic"] = sealed

    await prov.set_expiry(sb, sb.created_at + 7200)
    assert 300 < fake.sbs[sb.provider_ref]["timeout"] <= 7200
    assert sb.provider_ref in (await prov.list_refs() or {})
    assert await prov.snapshot(sb, "s1") == "snap-s1"
    await prov.destroy(sb)
    assert await prov.status(sb) == SandboxState.STOPPED
    await prov.destroy(sb)  # gone already: fine
    await prov.close()
    await again.close()


@needs_binary
async def test_cube_without_an_envd_token_is_refused_by_default(fake_e2b):
    fake = fake_e2b("cube", token=False, domain="cube.test")
    prov = fake.provider()
    sb = sandbox()
    with pytest.raises(ProviderError, match="HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD"):
        await prov.create(sb, "tpl-abc")
    assert not fake.sbs, "the refused sandbox is killed, not left running"
    assert ("POST", "/v2/sandboxes") not in fake.calls
    await prov.close()


@needs_binary
async def test_unauthenticated_envd_without_a_traffic_token_is_refused(fake_e2b):
    """review(#64): HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD rests on the edge wanting a token only hangar holds. A
    sandbox created without one (CubeMaster made it public) is refused and killed even with the flag on."""
    fake = fake_e2b("cube", token=False, domain="cube.test", traffic=False)
    prov = fake.provider(e2b_allow_unauthenticated_envd=True)
    with pytest.raises(ProviderError, match="neither an envd access token nor a traffic token"):
        await prov.create(sandbox(), "tpl-abc")
    assert not fake.sbs
    await prov.close()


async def test_control_api_answers_are_capped_and_have_a_deadline():
    """review(#64): an answer is read up to MAX_API_BODY, and a call has one deadline however slowly bytes come."""
    import hangar.providers.e2b as mod

    async def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/slow"):
            async def drip():
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    yield b" "
            return httpx.Response(200, content=drip())
        return httpx.Response(200, content=b"[" + b" " * (mod.MAX_API_BODY + 10) + b"]")

    p = E2BProvider(Settings(e2b_api_key="k", e2b_request_timeout_seconds=0.5))
    p.api.http = httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url=p.api.base)
    with pytest.raises(ProviderError, match="over 8 MiB"):
        await p.api.list()
    assert await p.list_refs() is None
    with pytest.raises(Unavailable, match="took over 0.5s"):
        await p.api.call("GET", "/slow")
    await p.close()


@needs_binary
async def test_terminal_through_envd(fake_e2b):
    fake = fake_e2b("selfhosted", domain="e2b.corp")
    prov = fake.provider()
    sb = sandbox()
    await prov.create(sb, "hangar-py:abc")
    sb.state = SandboxState.RUNNING
    t = await prov.open_terminal(sb, ["bash", "--norc", "-i"], cols=90, rows=20)
    await t.write(b"stty size; exit\n")
    out = b""
    async with asyncio.timeout(10):
        while chunk := await t.read():
            out += chunk
    assert b"20 90" in out
    await t.close()
    await prov.close()


async def test_unavailable_and_errors(fake_e2b):
    fake = fake_e2b("cloud")
    prov = fake.provider()
    fake.full = True
    with pytest.raises(Unavailable, match="429"):
        await prov.create(sandbox(), "hangar-py:abc")
    down = E2BProvider(Settings(e2b_api_key="k", e2b_api_url="http://127.0.0.1:9"))
    with pytest.raises(Unavailable, match="unreachable"):
        await down.create(sandbox(), "t")
    assert await down.list_refs() is None, "a failed list must not make reconcile mark everything gone"
    bad = fake.provider()
    bad.api.http.headers["x-api-key"] = "wrong"
    with pytest.raises(ProviderError, match="401 Invalid API key") as ei:
        await bad.create(sandbox(), "t")
    assert not isinstance(ei.value, Unavailable) and "wrong" not in str(ei.value)
    for p in (prov, down, bad):
        await p.close()


async def test_list_pages(fake_e2b):
    fake = fake_e2b("cloud")
    prov = fake.provider()
    for i in range(250):
        fake.sbs[f"s{i:03d}"] = {"template": "t", "metadata": {"hangar_id": f"sb-{i}"} if i % 2 else {},
                                 "state": "paused" if i % 3 == 0 else "running", "port": 0}
    refs = await prov.list_refs()
    assert len(refs) == 125 and refs["s003"] == SandboxState.PAUSED and refs["s001"] == SandboxState.RUNNING
    fake.sbs.clear()
    await prov.close()


async def test_templates_registered_and_built(fake_e2b, monkeypatch):
    fake = fake_e2b("cube", domain="cube.test")
    prov = fake.provider(registry="reg.example/hangar", e2b_environment_templates={"mapped": "tpl-out-of-band"})
    assert await prov.image_exists("reg.example/hangar/mapped:abc")
    assert await prov.image_exists("reg.example/hangar/built:abcdef")  # Cube knows the alias
    assert await prov.image_exists("reg.example/hangar/other:abcdef") is False  # alias lookup: 404
    import hangar.providers.e2b as mod

    pushed = []

    async def fake_build(ctx, tag, log=None, platform=None, push=False):
        pushed.append((tag, push))
        return tag

    monkeypatch.setattr(mod, "docker_build", fake_build)
    from hangar.envspec import EnvSpec
    from hangar.models import Resources

    spec = EnvSpec(name="py", resources=Resources(cpus=2, memory_gb=4, disk_gb=10))
    lines = []
    name = await prov.build_template(spec, None, "reg.example/hangar/py:0123456789abcdef9", log=lines.append)
    assert name == "hangar-py-0123456789abcdef" and pushed == [("reg.example/hangar/py:0123456789abcdef9", True)]
    assert fake.built == {"image": "reg.example/hangar/py:0123456789abcdef9", "name": name, "cpu": 2000,
                          "memory": 4096, "exposedPorts": [49983], "probePort": 49983, "probePath": "/health",
                          "writableLayerSize": "10G"}
    assert any("ready" in ln for ln in lines)
    assert await prov._template_id(name) == "tpl-new", "Cube creates from the template id the build returned"
    assert await prov._template_id("hangar-built-x0y1z2") == "tpl-x0y1z2"
    assert await prov.build_template(spec, None, "reg.example/hangar/mapped:abc") == "tpl-out-of-band"
    await prov.close()


# ---------------------------------------------------------------------------- tiers
class FullProvider(FakeProvider):
    """A fast tier that is at capacity."""

    def __init__(self):
        super().__init__()
        self.tries = 0

    async def create(self, sandbox, image, spec=None):
        self.tries += 1
        raise Unavailable("e2b (cloud) POST /v2/sandboxes: 429 concurrency limit")


@pytest.fixture
def tier_envs(fleet, tmp_path):
    d = tmp_path / "tier-envs"
    d.mkdir()
    (d / "fast.yaml").write_text("name: fast\ntier: fast\n")
    (d / "locked.yaml").write_text("name: locked\ntier: isolated\n")
    fleet.catalog.dirs.insert(0, d)
    fleet.catalog.reload()
    return fleet


async def test_tier_picks_the_fast_provider(tier_envs):
    fleet = tier_envs
    fast = FakeProvider()
    fleet.providers["e2b"] = fast
    sb = await fleet.create("fast")
    assert sb.provider == "e2b" and sb.id in fast.boxes
    assert (await fleet.create("fast", provider="docker")).provider == "docker", "a named provider wins"
    assert (await fleet.create("plain", tier="fast")).provider == "e2b", "a create can ask for a tier itself"
    assert (await fleet.create("fast", tier="default")).provider == "docker"


async def test_fast_falls_back_when_full_and_cools_down(tier_envs, store):
    fleet = tier_envs
    full = FullProvider()
    fleet.providers["e2b"] = full
    sb = await fleet.create("fast")
    assert sb.provider == "docker" and full.tries == 1
    assert (await fleet.create("fast")).provider == "docker" and full.tries == 1, "skipped while cooling down"
    assert any(e.action == "sandbox.tier_fallback" for e in store.audit_tail(50))
    fleet.tier_down.clear()
    await fleet.create("fast")
    assert full.tries == 2


async def test_fast_without_the_e2b_provider_uses_the_default(tier_envs):
    fleet = tier_envs
    fleet.provider_errors["e2b"] = "set HANGAR_E2B_API_KEY to use the e2b provider"
    assert (await fleet.create("fast")).provider == "docker"


async def test_isolated_never_falls_back(tier_envs):
    from hangar.fleet import FleetError

    fleet = tier_envs
    with pytest.raises(FleetError, match="isolated tier is available.*kubernetes: not enabled"):
        await fleet.create("locked")
    fleet.providers["kubernetes"] = FullProvider()
    with pytest.raises(FleetError, match="no provider in the isolated tier could create"):
        await fleet.create("locked")
    assert not [s for s in fleet.list() if s.provider == "docker"]


async def test_an_isolated_environment_is_never_put_on_a_weaker_provider(tier_envs):
    """review(#64): `isolated` is the environment's security choice. Naming a provider outside the tier, asking for
    another tier, or hangar's E2B API pinning the default provider must not move it off an isolated provider."""
    from hangar.fleet import FleetError

    fleet = tier_envs
    fleet.providers["kubernetes"] = FakeProvider()
    for kw in ({"provider": "docker"}, {"tier": "fast"}, {"tier": "default"}, {"provider": "docker", "tier": "fast"}):
        with pytest.raises(FleetError, match="isolated"):
            await fleet.create("locked", **kw)
    assert not [s for s in fleet.list() if s.provider == "docker"]
    assert (await fleet.create("locked", provider="kubernetes")).provider == "kubernetes"
    assert (await fleet.create("locked")).provider == "kubernetes"


async def test_other_errors_do_not_fall_back(tier_envs):
    from hangar.fleet import FleetError

    fleet = tier_envs

    class Broken(FakeProvider):
        async def create(self, sandbox, image, spec=None):
            raise ProviderError("template not found")

    fleet.providers["e2b"] = Broken()
    with pytest.raises(FleetError, match="template not found"):
        await fleet.create("fast")
    assert not fleet.tier_down
