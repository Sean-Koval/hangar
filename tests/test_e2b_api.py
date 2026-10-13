"""The E2B-compatible API and sandbox router (docs/e2b.md), driven by the stock e2b Python SDK.

Hangar runs on uvicorn with a fake provider whose sandboxes are local directories, each with a real hangar-envd
daemon on its own port (the binary from envd/build.sh), so every SDK call crosses the same code as on Docker or
GKE: the control API, the router's auth and re-signing, and the daemon. The Docker version of this is
tests/test_e2e_e2b_docker.py.

    uv run --extra e2b pytest tests/test_e2b_api.py
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import json
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import httpx
import pytest

from hangar.e2b_api import ENVD_VERSION
from hangar.envd import Envd, EnvdError
from hangar.models import Sandbox
from hangar.sandbox_router import SandboxRouter, _with_env, file_signature
from tests.conftest import FakeProvider
from tests.test_envd import ARCH, BINARIES, free_port, kill_daemon
from tests.test_mcp_modern import AppThread

needs_binary = pytest.mark.skipif(ARCH not in BINARIES, reason="no hangar-envd binary (run envd/build.sh)")
ADMIN = "admin-token-for-e2b-tests"
DOMAIN = "e2b.test"


# ---------------------------------------------------------------------------- pieces that need nothing running
def test_file_signature_matches_the_sdk():
    pytest.importorskip("e2b")
    from e2b.sandbox.signature import get_signature

    sdk = get_signature("/tmp/a.txt", "read", None, "tok", None)["signature"]
    assert file_signature("/tmp/a.txt", "read", "", "tok", None) == sdk
    exp = int(time.time()) + 60
    sdk = get_signature("/x", "write", "user", "tok", 60)
    assert file_signature("/x", "write", "user", "tok", str(sdk["expiration"])) == sdk["signature"]
    assert abs(sdk["expiration"] - exp) <= 1


def test_env_vars_go_under_the_requests_own():
    msg = {"process": {"cmd": "/bin/bash", "envs": {"A": "mine"}}, "stdin": False}
    raw = json.dumps(msg).encode()
    framed = b"\0" + len(raw).to_bytes(4, "big") + raw
    out = _with_env(framed, "application/connect+json", {"A": "default", "B": "b"})
    assert json.loads(out[5:])["process"]["envs"] == {"A": "mine", "B": "b"}
    assert int.from_bytes(out[1:5], "big") == len(out) - 5
    assert _with_env(b"\x01" + framed[1:], "application/connect+json", {"B": "b"}) == b"\x01" + framed[1:]
    assert _with_env(b"junk", "application/connect+json", {"B": "b"}) == b"junk"
    assert _with_env(raw, "application/proto", {"B": "b"}) == raw


def test_routes():
    svc = SimpleNamespace(settings=SimpleNamespace(e2b_api_path="/e2b"), domain=lambda: DOMAIN)
    r = SandboxRouter(None, svc)

    def route(host: str, path: str = "/x", **headers: str):
        hs = [(b"host", host.encode())] + [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
        return r._route({"type": "http", "path": path, "raw_path": path.encode(), "headers": hs})

    assert route(f"49983-sb-1a2b3c4d.{DOMAIN}") == ("sb", ("sb-1a2b3c4d", "49983", b"/x"))
    assert route(f"3000-sb-1a2b3c4d.{DOMAIN}:443", "/a/b") == ("sb", ("sb-1a2b3c4d", "3000", b"/a/b"))
    assert route(f"api.{DOMAIN}", "/v2/sandboxes") == ("api", "/e2b/v2/sandboxes")
    assert route(f"api.{DOMAIN}", "/e2b/v2/sandboxes") is None
    assert route(f"sandbox.{DOMAIN}", "/files", e2b_sandbox_id="sb-1", e2b_sandbox_port="3000") == \
        ("sb", ("sb-1", "3000", b"/files"))
    assert route("127.0.0.1:8787", "/e2b/sandbox/files", e2b_sandbox_id="sb-1") == ("sb", ("sb-1", "49983", b"/files"))
    assert route("127.0.0.1:8787", "/e2b/sandbox/files")[0] == "bad"
    assert route("127.0.0.1:8787", "/e2b/sandboxes") is None  # the control API
    assert route("127.0.0.1:8787", "/api/sandboxes") is None
    assert route(f"a.b.{DOMAIN}") is None  # nested labels: not a sandbox host


# ---------------------------------------------------------------------------- a hangar with local daemons
class LocalEnvd(Envd):
    """Each fake sandbox's daemon runs on this machine, on a port of its own, in the sandbox's directory."""

    async def urls(self, sb: Sandbox) -> list[str]:
        return [f"http://127.0.0.1:{self.p.ports[sb.provider_ref]}"]

    async def install(self, sb: Sandbox, token: str) -> None:
        port, box = self.p.ports[sb.provider_ref], self.p.boxes[sb.provider_ref]
        argv = [str(self.binaries[ARCH]), "--listen", "127.0.0.1", "--port", str(port), "--daemon",
                "--workdir", str(box / "workspace")]
        p = await asyncio.to_thread(subprocess.run, argv, env={**os.environ, "HANGAR_ENVD_TOKEN": token},
                                    capture_output=True, text=True)
        if p.returncode != 0:
            raise EnvdError(p.stderr.strip())


class EnvdFake(FakeProvider):
    def __init__(self, settings):
        super().__init__()
        self.ports: dict[str, int] = {}
        self.envd = LocalEnvd(self, settings)

    async def create(self, sandbox, image, spec=None):
        await super().create(sandbox, image, spec)
        self.ports[sandbox.provider_ref] = free_port()

    async def destroy(self, sandbox):
        self.envd.forget(sandbox)
        if port := self.ports.pop(sandbox.provider_ref or "", None):
            kill_daemon(port)
        await super().destroy(sandbox)

    def stop(self) -> None:
        for port in self.ports.values():
            kill_daemon(port)
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def hangar(settings):
    from hangar.app import create_app

    settings.admin_token = ADMIN
    settings.e2b_api_domain = DOMAIN
    settings.e2b_templates = {"base": "plain"}
    app = create_app(settings)
    srv = AppThread(app)
    h = app.state.hangar
    prov = EnvdFake(settings)
    h.fleet.providers["docker"] = prov
    h.fleet.provider_errors.pop("docker", None)
    url = f"http://127.0.0.1:{srv.port}"

    def opts(key: str) -> dict:
        """What a client sets: E2B_API_KEY, E2B_API_URL and E2B_SANDBOX_URL (no wildcard DNS needed)."""
        return {"api_key": key, "api_url": f"{url}/e2b", "sandbox_url": f"{url}/e2b/sandbox", "retries": 0,
                "request_timeout": 30}

    yield SimpleNamespace(h=h, app=app, prov=prov, url=url, svc=app.state.e2b, opts=opts)
    srv.stop()
    prov.stop()


def token(hg, name: str, scopes=("sandboxes",)) -> str:
    raw, _ = hg.h.auth.create(name, list(scopes))
    return raw


# ---------------------------------------------------------------------------- the SDK, end to end
@needs_binary
async def test_sdk_sandbox_lifecycle(hangar):
    from e2b import AsyncSandbox, CommandExitException, FilesystemEventType, PtySize, SandboxQuery

    key = token(hangar, "ci")
    opts = hangar.opts(key)
    sb = await AsyncSandbox.create(template="plain", timeout=120, envs={"GREETING": "hello"},
                                   metadata={"purpose": "test"}, **opts)
    assert sb.sandbox_id.startswith("sb-") and str(sb._envd_version) == ENVD_VERSION
    # The tokens are E2B's, not the daemon's.
    daemon_token = hangar.prov.envd.token(hangar.h.fleet.get(sb.sandbox_id))
    assert sb._envd_access_token and sb._envd_access_token != daemon_token

    # commands: envVars become the default env; the request's own env and cwd win
    r = await sb.commands.run("echo $GREETING; echo $((6*7)) >&2")
    assert (r.exit_code, r.stdout, r.stderr) == (0, "hello\n", "42\n")
    r = await sb.commands.run("echo $GREETING; pwd", envs={"GREETING": "own"}, cwd="/tmp")
    assert r.stdout == "own\n/tmp\n"
    with pytest.raises(CommandExitException) as ei:
        await sb.commands.run("exit 3")
    assert ei.value.exit_code == 3
    bg = await sb.commands.run("sleep 60", background=True)
    assert bg.pid in [p.pid for p in await sb.commands.list()]
    assert await sb.commands.kill(bg.pid)
    h = await sb.commands.run("read x; echo got $x", background=True, stdin=True)
    await sb.commands.send_stdin(h.pid, "abc\n")
    assert (await h.wait()).stdout == "got abc\n"

    # files
    box = hangar.prov.boxes[sb.sandbox_id]
    d = str(box / "workspace" / "sdk")
    info = await sb.files.write(f"{d}/a.txt", "written by the sdk")
    assert info.path == f"{d}/a.txt"
    assert await sb.files.read(f"{d}/a.txt") == "written by the sdk"
    big = os.urandom(3 << 20)
    await sb.files.write(f"{d}/big.bin", big)
    assert bytes(await sb.files.read(f"{d}/big.bin", format="bytes")) == big
    assert sorted(e.name for e in await sb.files.list(d)) == ["a.txt", "big.bin"]
    assert await sb.files.exists(f"{d}/a.txt") and not await sb.files.exists(f"{d}/nope")
    assert await sb.files.make_dir(f"{d}/sub")
    assert (await sb.files.rename(f"{d}/a.txt", f"{d}/sub/b.txt")).name == "b.txt"
    await sb.files.remove(f"{d}/sub")
    assert not await sb.files.exists(f"{d}/sub")
    events = []
    w = await sb.files.watch_dir(d, on_event=events.append)
    await sb.files.write(f"{d}/w.txt", "x")
    await asyncio.sleep(0.3)
    await w.stop()
    assert ("w.txt", FilesystemEventType.CREATE) in [(e.name, e.type) for e in events]

    # a terminal
    out = []
    t = await sb.pty.create(size=PtySize(rows=24, cols=80), on_data=out.append)
    await sb.pty.resize(t.pid, PtySize(rows=30, cols=100))
    await sb.pty.send_stdin(t.pid, b"stty size; exit\n")
    await t.wait()
    assert b"30 100" in b"".join(out)

    # timeout, info, list, connect by id
    before = (await sb.get_info()).end_at
    await sb.set_timeout(600)
    info = await sb.get_info()
    assert info.end_at > before and info.metadata == {"purpose": "test"} and info.template_id == "plain"
    assert (await sb.get_info()).state.value == "running"
    listed = await AsyncSandbox.list(query=SandboxQuery(metadata={"purpose": "test"}), **opts).next_items()
    assert [s.sandbox_id for s in listed] == [sb.sandbox_id]
    again = await AsyncSandbox.connect(sb.sandbox_id, **opts)
    assert (await again.commands.run("echo again")).stdout == "again\n"
    assert await sb.is_running()

    await sb.kill()
    assert not await AsyncSandbox.kill(sb.sandbox_id, **opts)  # already gone
    assert hangar.h.fleet.get(sb.sandbox_id).state.value == "stopped"
    with pytest.raises(Exception):  # noqa: B017 - the SDK maps the router's 502 to its "not running" errors
        await sb.commands.run("true")


@needs_binary
async def test_sdk_pause_and_connect(hangar):
    from e2b import AsyncSandbox, SandboxState

    opts = hangar.opts(token(hangar, "ci"))
    sb = await AsyncSandbox.create(template="base", timeout=60, **opts)  # "base" maps to an environment
    assert (await sb.get_info()).template_id == "plain"
    await sb.pause()
    info = await sb.get_info()
    assert info.state == SandboxState.PAUSED
    assert not await sb.pause()  # already paused: 409, which the SDK reports as False
    r = await httpx.AsyncClient().post(
        f"{hangar.url}/e2b/sandbox/process.Process/List", json={},
        headers={"E2b-Sandbox-Id": sb.sandbox_id, "X-Access-Token": sb._envd_access_token,
                 "Connect-Protocol-Version": "1"})
    assert r.status_code == 502 and "paused" in r.json()["message"]
    resumed = await AsyncSandbox.connect(sb.sandbox_id, timeout=120, **opts)
    assert (await resumed.commands.run("echo back")).stdout == "back\n"
    assert (await resumed.get_info()).state == SandboxState.RUNNING
    await resumed.kill()


@needs_binary
async def test_auth_and_ownership(hangar):
    from e2b import AsyncSandbox, AuthenticationException, NotFoundException, SandboxException
    from packaging.version import Version

    alice, bob = token(hangar, "alice"), token(hangar, "bob")
    sb = await AsyncSandbox.create(template="plain", **hangar.opts(alice))
    with pytest.raises(AuthenticationException):
        await AsyncSandbox.create(template="plain", **hangar.opts("hgr_wrong"))
    jobs = token(hangar, "jobs-only", ["jobs"])
    with pytest.raises(SandboxException, match="sandboxes scope"):
        await AsyncSandbox.create(template="plain", **hangar.opts(jobs))
    with pytest.raises(SandboxException, match="template 'nope' not found"):
        await AsyncSandbox.create(template="nope", **hangar.opts(alice))

    # Bob can't see, kill, connect to or reach Alice's sandbox; the admin can see it.
    assert [s.sandbox_id for s in await AsyncSandbox.list(**hangar.opts(bob)).next_items()] == []
    with pytest.raises(NotFoundException):
        await AsyncSandbox.get_info(sb.sandbox_id, **hangar.opts(bob))
    assert not await AsyncSandbox.kill(sb.sandbox_id, **hangar.opts(bob))
    with pytest.raises(NotFoundException):
        await AsyncSandbox.connect(sb.sandbox_id, **hangar.opts(bob))
    assert sb.sandbox_id in [s.sandbox_id for s in await AsyncSandbox.list(**hangar.opts(ADMIN)).next_items()]

    # The data plane wants this sandbox's access token: not another's, not the daemon's, not a hangar token.
    from e2b.connection_config import ConnectionConfig

    bobs = await AsyncSandbox.create(template="plain", **hangar.opts(bob))
    for wrong in (bobs._envd_access_token, hangar.prov.envd.token(hangar.h.fleet.get(sb.sandbox_id)), alice):
        cfg = ConnectionConfig(sandbox_url=f"{hangar.url}/e2b/sandbox", api_key=alice,
                               extra_sandbox_headers={"X-Access-Token": wrong, "E2b-Sandbox-Id": sb.sandbox_id})
        bad = AsyncSandbox(sandbox_id=sb.sandbox_id, sandbox_domain=None, envd_version=Version(ENVD_VERSION),
                           envd_access_token=wrong, traffic_access_token=None, connection_config=cfg)
        with pytest.raises(AuthenticationException):
            await bad.commands.run("true")
        with pytest.raises(AuthenticationException):
            await bad.files.read("/etc/hostname")
    # Refusals are audited.
    assert any(e.action == "e2b.envd.denied" for e in hangar.h.store.audit_tail(50))
    await sb.kill()
    await bobs.kill()


@needs_binary
async def test_limits(hangar):
    from e2b import AsyncSandbox, RateLimitException

    s = hangar.h.settings
    s.e2b_max_sandboxes_per_owner = 1
    key = token(hangar, "capped")
    sb = await AsyncSandbox.create(template="plain", **hangar.opts(key))
    with pytest.raises(RateLimitException, match="maximum number of concurrent sandboxes"):
        await AsyncSandbox.create(template="plain", **hangar.opts(key))
    await sb.kill()
    s.e2b_max_sandboxes_per_owner = 0
    s.e2b_create_burst, s.e2b_create_per_minute = 1, 0.01
    sb = await AsyncSandbox.create(template="plain", **hangar.opts(key))
    with pytest.raises(RateLimitException, match="too many sandbox creations"):
        await AsyncSandbox.create(template="plain", **hangar.opts(key))
    await AsyncSandbox.create(template="plain", **hangar.opts(ADMIN))  # admin tokens aren't rate limited
    s.e2b_rate_burst, s.e2b_rate_per_minute = 1, 0.01
    other = token(hangar, "chatty")
    async with httpx.AsyncClient(base_url=f"{hangar.url}/e2b", headers={"X-API-Key": other}) as c:
        assert (await c.get("/v2/sandboxes")).status_code == 200
        r = await c.get("/v2/sandboxes")
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["code"] == 429
    # A token's max_budget caps its sandboxes' timeouts.
    from hangar.models import Budget

    raw, tok = hangar.h.auth.create("budgeted", ["sandboxes"], max_budget=Budget(max_minutes=5))
    async with httpx.AsyncClient(base_url=f"{hangar.url}/e2b", headers={"X-API-Key": raw}) as c:
        r = await c.post("/v2/sandboxes", json={"templateID": "plain", "timeout": 3600})
        assert r.status_code == 400 and "Timeout" in r.json()["message"]
    await sb.kill()


# ---------------------------------------------------------------------------- the wire, without the SDK
@needs_binary
async def test_host_routing_signed_urls_and_unsupported(hangar):
    from e2b import AsyncSandbox

    key = token(hangar, "wire")
    sb = await AsyncSandbox.create(template="plain", **hangar.opts(key))
    sid, tok = sb.sandbox_id, sb._envd_access_token
    path = str(hangar.prov.boxes[sid] / "workspace" / "s.txt")
    async with httpx.AsyncClient(base_url=hangar.url) as c:
        # api.<domain> is the control API; <port>-<id>.<domain> the sandbox
        r = await c.get("/v2/sandboxes", headers={"Host": f"api.{DOMAIN}", "X-API-Key": key})
        assert [x["sandboxID"] for x in r.json()] == [sid]
        host = {"Host": f"49983-{sid}.{DOMAIN}"}
        stat = {"Content-Type": "application/json", "Connect-Protocol-Version": "1", **host}
        r = await c.post("/filesystem.Filesystem/Stat", json={"path": "/"}, headers=stat)
        assert r.status_code == 401
        r = await c.post("/filesystem.Filesystem/Stat", json={"path": "/"}, headers={**stat, "X-Access-Token": tok})
        assert r.status_code == 200 and r.json()["entry"]["type"] == "FILE_TYPE_DIRECTORY"
        r = await c.post("/filesystem.Filesystem/Stat", json={"path": "/"},
                         headers={**stat, "Host": f"49983-sb-00000000.{DOMAIN}", "X-Access-Token": tok})
        assert r.status_code == 502 and r.json()["message"] == "The sandbox was not found"

        # pre-signed /files URLs (made by the SDK with the access token), gzip uploads, E2B's default user
        up = sb.upload_url(path, use_signature_expiration=60)
        up = httpx.URL(up).copy_with(scheme="http", host="127.0.0.1", port=int(hangar.url.rsplit(":", 1)[1]))
        r = await c.post(str(up), files={"file": ("s.txt", b"signed upload")}, headers=host)
        assert r.status_code == 200, r.text
        down = httpx.URL(sb.download_url(path)).copy_with(scheme="http", host="127.0.0.1", port=up.port)
        assert (await c.get(str(down), headers=host)).content == b"signed upload"
        assert (await c.get(str(down).replace("signature=", "signature=x"), headers=host)).status_code == 401
        # a signed URL is good for /files only
        other = down.copy_with(path="/filesystem.Filesystem/Stat")
        assert (await c.post(str(other), json={"path": path}, headers=host)).status_code == 401
        expired = up.copy_merge_params({"signature_expiration": str(int(time.time()) - 1)})
        assert (await c.post(str(expired), files={"file": ("s.txt", b"x")}, headers=host)).status_code == 401
        r = await c.post("/files", params={"path": path, "username": "user"}, content=gzip.compress(b"z" * 100000),
                         headers={**host, "X-Access-Token": tok, "Content-Type": "application/octet-stream",
                                  "Content-Encoding": "gzip"})
        assert r.status_code == 200, r.text
        as_user = "Basic " + base64.b64encode(b"user:").decode()  # E2B's default user is the sandbox's user
        r = await c.get("/files", params={"path": path}, headers={**host, "X-Access-Token": tok,
                                                                  "Authorization": as_user})
        assert r.content == b"z" * 100000
        hangar.h.settings.max_request_mb = 1
        bomb = gzip.compress(b"\0" * ((1 << 20) + 1))
        r = await c.post("/files", params={"path": path}, content=bomb,
                         headers={**host, "X-Access-Token": tok, "Content-Type": "application/octet-stream",
                                  "Content-Encoding": "gzip"})
        assert r.status_code == 413

        # what hangar doesn't do is said, in E2B's shape
        r = await c.post(f"/e2b/sandboxes/{sid}/fork", json={}, headers={"X-API-Key": key})
        assert r.status_code == 501 and r.json()["code"] == 501
        r = await c.post("/e2b/v2/sandboxes", json={"templateID": "plain", "allow_internet_access": False},
                         headers={"X-API-Key": key})
        assert r.status_code == 400 and "allow_internet_access" in r.json()["message"]
        r = await c.post("/e2b/v2/sandboxes", json={"templateID": "plain", "metadata": {"hangar.session": "x"}},
                         headers={"X-API-Key": key})
        assert r.status_code == 400
        r = await c.get("/e2b/v2/templates", headers={"X-API-Key": key})
        assert {"templateID": "plain", "envdVersion": ENVD_VERSION}.items() <= \
            next(t for t in r.json() if t["templateID"] == "plain").items()
        assert "base" in next(t for t in r.json() if t["templateID"] == "plain")["aliases"]
        logs = (await c.get(f"/e2b/v2/sandboxes/{sid}/logs", headers={"X-API-Key": key})).json()["logs"]
        assert logs[0]["message"].startswith("sandbox.create by e2b:wire")
        r = await c.get(f"/e2b/v2/sandboxes/{sid}/logs?cursor=x", headers={"X-API-Key": key})
        assert r.status_code == 400 and r.json()["code"] == 400  # E2B's error shape, not FastAPI's
    await sb.kill()


@needs_binary
async def test_answers_must_prove_the_daemon_and_cut_uploads_change_nothing(hangar):
    """Uploads go to the daemon as signed frames; one the router stops (over the cap) leaves the file as it was.
    Anything at the sandbox's address that can't prove the daemon's token (a reused IP) gets a 502, not belief."""
    from e2b import AsyncSandbox

    key = token(hangar, "proofs")
    sb = await AsyncSandbox.create(template="plain", **hangar.opts(key))
    sid, tok = sb.sandbox_id, sb._envd_access_token
    path = hangar.prov.boxes[sid] / "workspace" / "kept.txt"
    host = {"Host": f"49983-{sid}.{DOMAIN}", "X-Access-Token": tok}
    async with httpx.AsyncClient(base_url=hangar.url, timeout=30) as c:
        up = {**host, "Content-Type": "application/octet-stream"}
        r = await c.post("/files", params={"path": str(path)}, content=b"k" * (600 << 10), headers=up)
        assert r.status_code == 200 and path.read_bytes() == b"k" * (600 << 10)  # three frames
        hangar.h.settings.max_request_mb = 1
        r = await c.post("/files", params={"path": str(path)}, content=b"x" * (3 << 20), headers=up)
        assert r.status_code == 413
        assert path.read_bytes() == b"k" * (600 << 10)
        assert not [p for p in path.parent.iterdir() if p.name != "kept.txt"]  # no temporary file left behind

        impostor_port = free_port()
        impostor = subprocess.Popen(["python3", "-m", "http.server", str(impostor_port), "--bind", "127.0.0.1"],
                                    cwd=hangar.prov.boxes[sid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                with contextlib.suppress(httpx.HTTPError):
                    if (await c.get(f"http://127.0.0.1:{impostor_port}/workspace/kept.txt")).status_code == 200:
                        break
                await asyncio.sleep(0.1)
            envd = hangar.prov.envd
            envd._eps[sid].url = f"http://127.0.0.1:{impostor_port}"
            r = await c.get("/files", params={"path": "/workspace/kept.txt"}, headers=host)
            assert r.status_code == 502 and "answered at its address" in r.json()["message"]
            assert sid not in envd._eps  # looked up again on the next call, which finds the real daemon
            assert (await sb.files.read(str(path))) == "k" * (600 << 10)
        finally:
            impostor.kill()
    await sb.kill()


@needs_binary
async def test_idempotent_create_and_trusted_environments(hangar):
    key = token(hangar, "idem")
    async with httpx.AsyncClient(base_url=f"{hangar.url}/e2b", headers={"X-API-Key": key}, timeout=60) as c:
        body = {"templateID": "plain", "timeout": 60}
        a = await c.post("/v2/sandboxes", json=body, headers={"Idempotency-Key": "k1"})
        b = await c.post("/v2/sandboxes", json=body, headers={"Idempotency-Key": "k1"})
        assert a.status_code == b.status_code == 201 and a.json() == b.json()
        r = await c.post("/v2/sandboxes", json={**body, "timeout": 61}, headers={"Idempotency-Key": "k1"})
        assert r.status_code == 422
        # an environment that hands its sandboxes secrets is the admin's (or trusted) only
        r = await c.post("/v2/sandboxes", json={"templateID": "agents"})
        assert r.status_code == 403 and "TRUSTED" in r.json()["message"]
        assert (await c.delete(f"/sandboxes/{a.json()['sandboxID']}")).status_code == 204
        assert (await c.delete(f"/sandboxes/{a.json()['sandboxID']}")).status_code == 404


@needs_binary
async def test_a_sandbox_whose_daemon_never_answers_is_removed(hangar, monkeypatch):
    async def never(sb, timeout=0):
        return None

    monkeypatch.setattr(hangar.prov.envd, "ready", never)
    key = token(hangar, "doomed")
    async with httpx.AsyncClient(base_url=f"{hangar.url}/e2b", headers={"X-API-Key": key}, timeout=60) as c:
        r = await c.post("/v2/sandboxes", json={"templateID": "plain"})
        assert r.status_code == 500 and "hangar-envd did not start" in r.json()["message"]
    assert [s for s in hangar.h.fleet.list() if s.environment == "plain"] == []
    assert hangar.h.store.kv_keys("e2b:sb:") == []


@needs_binary
async def test_other_ports_http_and_websocket(hangar, monkeypatch):
    """A server on a sandbox port, reached with the traffic token; the fake sandbox's address is this machine."""
    import websockets
    from e2b import AsyncSandbox

    async def local(self, sb):
        return "127.0.0.1"

    monkeypatch.setattr(SandboxRouter, "_host", local)
    sb = await AsyncSandbox.create(template="plain", **hangar.opts(token(hangar, "ports")))
    port = free_port()

    async def echo(ws):
        async for m in ws:
            await ws.send(f"echo:{m}")

    httpd = subprocess.Popen(["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                             cwd=hangar.prov.boxes[sb.sandbox_id], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        (hangar.prov.boxes[sb.sandbox_id] / "hello.txt").write_text("from the sandbox")
        host = {"Host": f"{port}-{sb.sandbox_id}.{DOMAIN}"}
        async with httpx.AsyncClient(base_url=hangar.url) as c:
            assert (await c.get("/hello.txt", headers=host)).status_code == 403
            for _ in range(50):
                r = await c.get("/hello.txt", headers={**host, "e2b-traffic-access-token": sb.traffic_access_token})
                if r.status_code != 502:
                    break
                await asyncio.sleep(0.1)
            assert r.text == "from the sandbox"
        wport = free_port()
        async with websockets.serve(echo, "127.0.0.1", wport):
            url = f"ws://127.0.0.1:{hangar.url.rsplit(':', 1)[1]}/"
            hdrs = {"Host": f"{wport}-{sb.sandbox_id}.{DOMAIN}", "e2b-traffic-access-token": sb.traffic_access_token}
            async with websockets.connect(url, additional_headers=hdrs) as ws:
                await ws.send("hi")
                assert await ws.recv() == "echo:hi"
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(url, additional_headers={"Host": hdrs["Host"]}):
                    pass
    finally:
        httpd.kill()
        await sb.kill()


@needs_binary
async def test_code_interpreter_sdk_reaches_port_49999(hangar, monkeypatch):
    """e2b-code-interpreter talks to the Jupyter-backed server E2B's template runs on port 49999, through the same
    router (X-Access-Token, a streamed NDJSON answer). A stand-in for that server shows hangar's side works; the real
    one needs an environment that runs E2B's code-interpreter server (docs/e2b.md)."""
    import socket
    import threading

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    ci = pytest.importorskip("e2b_code_interpreter")
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", 49999))
        except OSError:
            pytest.skip("port 49999 is taken on this machine")

    async def execute(request):
        body = await request.json()
        assert "x-access-token" not in request.headers  # the router keeps E2B's tokens to itself

        async def lines():
            yield json.dumps({"type": "stdout", "text": f"ran {body['code']}\n"}) + "\n"
            await asyncio.sleep(0.05)
            yield json.dumps({"type": "result", "text": "2", "is_main_result": True}) + "\n"
            yield json.dumps({"type": "end_of_execution"}) + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/execute", execute, methods=["POST"])]),
                                           host="127.0.0.1", port=49999, log_level="warning", loop="asyncio"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    async def local(self, sb):
        return "127.0.0.1"

    monkeypatch.setattr(SandboxRouter, "_host", local)
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        sb = await ci.AsyncSandbox.create(template="plain", **hangar.opts(token(hangar, "ci")))
        ex = await sb.run_code("1 + 1")
        assert ex.logs.stdout == ["ran 1 + 1\n"] and ex.results[0].text == "2"
        await sb.kill()
    finally:
        server.should_exit = True
        t.join(10)
