"""hangar.envd against a real hangar-envd daemon on this machine (and against a hostile fake one).

Needs the binary: `envd/build.sh` (or HANGAR_ENVD_DIR). The Docker end-to-end test is test_e2e_envd_docker.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hangar.config import Settings
from hangar.envd import MAX_OUTPUT, SKIP, Envd, EnvdError, find_binaries
from hangar.models import Sandbox
from hangar.providers.base import ProviderError

ARCH = os.uname().machine
BINARIES = find_binaries(Settings(envd_dir=os.environ.get("HANGAR_ENVD_DIR") or None))
needs_binary = pytest.mark.skipif(ARCH not in BINARIES, reason="no hangar-envd binary (run envd/build.sh)")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalEnvd(Envd):
    """The daemon runs on this machine, as this user: `install` starts it, `urls` points at it."""

    def __init__(self, settings: Settings, port: int, wrong_token: bool = False):
        super().__init__(SimpleNamespace(name="local"), settings)
        self.port, self.installs, self.wrong_token = port, 0, wrong_token

    def token(self, sb: Sandbox) -> str:
        return "not-the-token-0123456789" if self.wrong_token else super().token(sb)

    async def urls(self, sb: Sandbox) -> list[str]:
        return [f"http://127.0.0.1:{self.port}"]

    async def install(self, sb: Sandbox, token: str) -> None:
        self.installs += 1
        argv = [str(self.binaries[ARCH]), "--listen", "127.0.0.1", "--port", str(self.port), "--daemon",
                "--workdir", self.s.workspace_dir]
        p = await asyncio.to_thread(subprocess.run, argv, env={**os.environ, "HANGAR_ENVD_TOKEN": token},
                                    capture_output=True, text=True)
        if p.returncode != 0:
            raise EnvdError(p.stderr.strip())


def kill_daemon(port: int) -> None:
    subprocess.run(["pkill", "-f", f"hangar-envd.*--port {port}"], check=False)


@pytest.fixture
def env(tmp_path):
    port = free_port()
    s = Settings(data_dir=tmp_path / "data", workspace_dir=str(tmp_path), envd_dir=next(iter(BINARIES.values())).parent
                 if BINARIES else None)
    s.data_dir.mkdir()
    e = LocalEnvd(s, port)
    sb = Sandbox(name="t", environment="x", provider="local", provider_ref="local-1")
    yield e, sb, tmp_path
    kill_daemon(port)


@needs_binary
async def test_exec_and_files_go_through_the_daemon(env):
    e, sb, tmp = env
    assert await e.endpoint(sb) is None  # first call: starts it in the background and uses the provider
    assert await e.ready(sb) is not None
    assert sb.info["envd"]["url"].endswith(str(e.port))
    r = await e.exec(sb, "echo hi; echo err >&2; pwd; exit 3")
    assert (r.exit_code, r.stdout, r.stderr, r.truncated) == (3, f"hi\n{tmp}\n", "err\n", False)
    r = await e.exec(sb, "sleep 5", timeout=1)
    assert r.exit_code == 124 and "timed out after 1s" in r.stderr

    data = os.urandom(300_000)
    assert await e.write_file(sb, str(tmp / "d/f.bin"), data, 0o600) is True
    assert (tmp / "d/f.bin").read_bytes() == data
    assert stat.S_IMODE((tmp / "d/f.bin").stat().st_mode) == 0o600
    assert await e.read_file(sb, str(tmp / "d/f.bin")) == data
    assert await e.read_file(sb, str(tmp / "d/f.bin"), limit=1000) == data[:1001]
    assert await e.read_file(sb, str(tmp / "missing")) is None
    assert await e.read_file(sb, str(tmp)) is None  # a directory
    assert e.installs == 1


@needs_binary
async def test_output_is_capped(env):
    e, sb, _ = env
    await e.ready(sb)
    r = await e.exec(sb, "head -c 5000000 /dev/zero | tr '\\0' x; echo; echo tail-marker")
    assert r.exit_code == 0 and r.truncated
    assert len(r.stdout) <= MAX_OUTPUT + 100 and r.stdout.endswith("tail-marker\n")


@needs_binary
async def test_cwd_missing_or_root_user_uses_the_provider(env):
    e, sb, _ = env
    await e.ready(sb)
    assert await e.exec(sb, "true", cwd="/nonexistent/dir") is None  # refused before starting: provider decides
    assert await e.exec(sb, "true", user="root") is None
    assert await e.endpoint(sb) is not None  # neither counts against the daemon


async def test_unreachable_daemon_falls_back(tmp_path):
    s = Settings(data_dir=tmp_path, envd_dir=tmp_path)
    (tmp_path / f"hangar-envd-{ARCH}").write_bytes(b"#!/bin/sh\nexit 1\n")
    e = LocalEnvd(s, free_port())
    sb = Sandbox(name="t", environment="x", provider="local", provider_ref="local-1")
    assert await e.ready(sb) is None
    assert await e.exec(sb, "true") is None
    assert await e.read_file(sb, "/etc/hostname") is SKIP
    assert await e.write_file(sb, str(tmp_path / "x"), b"x") is False
    assert e.installs == 1  # and not again until RETRY_AFTER


@needs_binary
async def test_close_drops_the_pool_and_later_calls_use_the_provider(env):
    """integrate (#57 x #60): Fleet.stop closes each provider, and with it the daemon connections; a call that
    still comes afterwards neither reopens a pool nor starts a daemon."""
    e, sb, _ = env
    assert await e.ready(sb) is not None
    assert (await e.exec(sb, "true")).transport == "envd"
    pool = e.http
    await e.close()
    assert pool.is_closed and e._http is None
    other = Sandbox(name="u", environment="x", provider="local", provider_ref="local-2")
    assert await e.exec(sb, "true") is None and await e.read_file(sb, "/etc/hostname", limit=10) is SKIP
    assert await e.endpoint(other) is None and not e._starting and e._http is None


@needs_binary
async def test_wrong_token_is_refused(env):
    e, sb, _ = env
    assert await e.ready(sb) is not None
    bad = LocalEnvd(e.s, e.port, wrong_token=True)
    assert await bad.ready(sb) is None  # its probe is refused, and a second daemon can't take the port
    assert await bad.exec(sb, "true") is None


@needs_binary
async def test_a_dead_daemon_is_noticed_and_restarted(env):
    e, sb, _ = env
    await e.ready(sb)
    kill_daemon(e.port)
    await asyncio.sleep(0.1)
    assert await e.exec(sb, "true") is None  # connection refused before anything ran: the provider runs it
    assert await e.ready(sb) is not None
    assert (await e.exec(sb, "echo back")).stdout == "back\n"
    assert e.installs == 2


@needs_binary
async def test_terminal(env):
    e, sb, _ = env
    await e.ready(sb)
    term = await e.open_terminal(sb, ["sh"], 80, 24)
    assert term is not None
    await term.resize(100, 30)
    await term.write(b"stty size; echo $((6*7))\n")
    seen = b""
    while b"42" not in seen or b"30 100" not in seen:
        chunk = await asyncio.wait_for(term.read(), 5)
        assert chunk
        seen += chunk
    await term.close()
    assert await term.read() == b""


# ---------------------------------------------------------------------- a hostile daemon
def proof_for(token: str, head: bytes) -> str:
    """X-Hangar-Proof for a request (envd/src/auth.rs): what only a holder of the sandbox's token can make."""
    sig = next(x.split(b":", 1)[1].strip() for x in head.split(b"\r\n") if x.lower().startswith(b"x-hangar-sig"))
    mac = bytes.fromhex(sig.decode().rsplit(".", 1)[1])
    return hmac.new(token.encode(), b"proof\n" + mac, hashlib.sha256).hexdigest()


class FakeDaemon:
    """Answers every request with the same canned HTTP response; `/info` says it is a daemon. With `token` it is
    run by the sandbox's own user, who can read the daemon's token, and proves it; without, it is an impostor (say,
    another sandbox that got this one's old address). `respond`, if given, answers non-/info requests instead."""

    def __init__(self, body: bytes = b"", headers: str = "content-type: application/connect+json\r\n",
                 token: str | None = None, respond=None):
        self.body, self.headers, self.token, self.respond = body, headers, token, respond

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        n = next((int(x.split(b":")[1]) for x in head.split(b"\r\n") if x.lower().startswith(b"content-length")), 0)
        await r.readexactly(n)
        info = b" /info " in head.split(b"\r\n")[0]
        proof = f"x-hangar-proof: {proof_for(self.token, head)}\r\n" if self.token else ""
        if self.respond and not info:
            await self.respond(w, proof)
            return
        body, headers = (b'{"version":"fake"}', "") if info else (self.body, self.headers)
        w.write(f"HTTP/1.1 200 OK\r\nconnection: close\r\n{proof}{headers}content-length: {len(body)}\r\n\r\n".encode()
                + body)
        await w.drain()
        w.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self.server.close()


def frame(obj: dict | bytes, flags: int = 0, size: int | None = None) -> bytes:
    p = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return bytes([flags]) + (len(p) if size is None else size).to_bytes(4, "big") + p


async def hostile(tmp_path, body: bytes = b"", headers: str | None = None, respond=None):
    s = Settings(data_dir=tmp_path, envd_dir=tmp_path)
    (tmp_path / f"hangar-envd-{ARCH}").write_bytes(b"")
    sb = Sandbox(name="t", environment="x", provider="local", provider_ref="local-1")
    e = LocalEnvd(s, 0)
    fake = FakeDaemon(body, *([headers] if headers else []), token=e.token(sb), respond=respond)
    await fake.__aenter__()
    e.port = fake.port
    assert await e.ready(sb) is not None
    return e, sb, fake


async def test_oversized_frames_are_refused(tmp_path):
    e, sb, fake = await hostile(tmp_path, frame(b"x" * 10, size=64 << 20))
    try:
        # A garbled answer to a Start that arrived: whether the command started is unknown, so it isn't run again.
        with pytest.raises(ProviderError, match="over the limit"):
            await e.exec(sb, "true")
    finally:
        await fake.__aexit__()
    start = frame({"event": {"start": {"pid": 1}}})
    e, sb, fake = await hostile(tmp_path, start + frame(b"x", size=64 << 20))
    try:
        with pytest.raises(ProviderError, match="over the limit"):
            await e.exec(sb, "true")
    finally:
        await fake.__aexit__()


async def test_compressed_or_malformed_output_is_refused(tmp_path):
    start = frame({"event": {"start": {"pid": 1}}})
    for bad in (frame(b"\x1f\x8b", flags=1), frame(b"not json"), frame({"event": {"data": {"stdout": "%%%"}}})):
        e, sb, fake = await hostile(tmp_path, start + bad)
        try:
            with pytest.raises(ProviderError):
                await e.exec(sb, "true")
        finally:
            await fake.__aexit__()


async def test_file_reads_are_not_decompressed_and_stop_at_the_limit(tmp_path):
    import gzip

    bomb = gzip.compress(b"\0" * (20 << 20))
    e, sb, fake = await hostile(tmp_path, bomb, "content-encoding: gzip\r\n")
    try:
        # Never inflated, and not mistaken for the file's bytes either: the provider reads the file instead.
        assert await e.read_file(sb, "/x", limit=100) is SKIP
        assert await e.read_file(sb, "/x") is SKIP
    finally:
        await fake.__aexit__()
    e, sb, fake = await hostile(tmp_path, b"y" * 5000, "content-type: application/octet-stream\r\n")
    try:
        assert await e.read_file(sb, "/x", limit=100) == b"y" * 101
    finally:
        await fake.__aexit__()


async def test_unbounded_reads_are_capped(tmp_path, monkeypatch):
    # read_file(limit=None) trusts the file's size; a daemon has no file and can send forever. Past the cap the
    # provider reads the file (from disk, where it really is that big).
    import hangar.envd as envd_mod

    monkeypatch.setattr(envd_mod, "MAX_READ", 1 << 20, raising=False)

    async def endless(w, proof):
        w.write(f"HTTP/1.1 200 OK\r\n{proof}transfer-encoding: chunked\r\n\r\n".encode())
        try:
            for _ in range(10_000):  # 640 MB, if nobody stops reading
                w.write(b"10000\r\n" + b"z" * 65536 + b"\r\n")
                await w.drain()
        except ConnectionError:
            pass
        w.close()

    e, sb, fake = await hostile(tmp_path, respond=endless)
    try:
        assert await asyncio.wait_for(e.read_file(sb, "/x"), 30) is SKIP
    finally:
        await fake.__aexit__()
    e, sb, fake = await hostile(tmp_path, b"z" * (2 << 20), "content-type: application/octet-stream\r\n")
    try:
        assert await e.read_file(sb, "/x") is SKIP  # a declared length over the cap isn't even read
        assert await e.read_file(sb, "/x", limit=10) == b"z" * 11
    finally:
        await fake.__aexit__()


async def test_a_broken_stream_after_the_request_is_not_run_again(tmp_path):
    """The request reached the daemon and the answer broke off: the command may have run, so the provider must not
    run it a second time. Only a refusal (an error status, or an error before any event) falls back."""

    async def hang_up(w, proof):  # read the request, then close without a word
        w.close()

    async def headers_only(w, proof):  # accepted, then nothing
        w.write(f"HTTP/1.1 200 OK\r\n{proof}content-type: application/connect+json\r\n"
                "transfer-encoding: chunked\r\n\r\n".encode())
        await w.drain()
        w.close()

    for respond in (hang_up, headers_only):
        e, sb, fake = await hostile(tmp_path, respond=respond)
        try:
            with pytest.raises(ProviderError, match="may still be running|may have run"):
                await e.exec(sb, "touch /tmp/once")
        finally:
            await fake.__aexit__()

    async def refused(w, proof):
        body = json.dumps({"code": "resource_exhausted", "message": "512 processes"}).encode()
        w.write(f"HTTP/1.1 429 Too Many Requests\r\n{proof}content-length: {len(body)}\r\n\r\n".encode() + body)
        await w.drain()
        w.close()

    e, sb, fake = await hostile(tmp_path, respond=refused)
    try:
        assert await e.exec(sb, "true") is None  # refused: nothing ran, the provider runs it
    finally:
        await fake.__aexit__()
    e, sb, fake = await hostile(tmp_path, frame({"error": {"code": "invalid_argument", "message": "no cwd"}}, flags=2))
    try:
        assert await e.exec(sb, "true") is None
    finally:
        await fake.__aexit__()


async def test_exec_gives_up_on_a_daemon_that_never_finishes(tmp_path, monkeypatch):
    # The daemon kills the command at its timeout and ends the stream; one that keeps the stream open regardless
    # (keepalives forever) must not hold the caller forever.
    import hangar.envd as envd_mod

    monkeypatch.setattr(envd_mod, "EXEC_GRACE", 1.0, raising=False)

    async def forever(w, proof):
        w.write(f"HTTP/1.1 200 OK\r\n{proof}content-type: application/connect+json\r\n"
                "transfer-encoding: chunked\r\n\r\n".encode())
        try:
            for i in range(10_000):
                f = frame({"event": {"start": {"pid": 7}}} if i == 0 else {"event": {"keepalive": {}}})
                w.write(f"{len(f):x}\r\n".encode() + f + b"\r\n")
                await w.drain()
                await asyncio.sleep(0.1)
        except ConnectionError:
            pass
        w.close()

    e, sb, fake = await hostile(tmp_path, respond=forever)
    try:
        with pytest.raises(ProviderError, match="may still be running"):
            await asyncio.wait_for(e.exec(sb, "sleep 1000", timeout=1), 20)
    finally:
        await fake.__aexit__()


async def test_an_impostor_is_not_taken_for_the_daemon(tmp_path):
    """Whatever answers at a sandbox's address (an address reused by another sandbox, say) without that sandbox's
    token gets no traffic beyond the probe, and none of its answers are used."""
    s = Settings(data_dir=tmp_path, envd_dir=tmp_path)
    (tmp_path / f"hangar-envd-{ARCH}").write_bytes(b"")
    sb = Sandbox(name="t", environment="x", provider="local", provider_ref="local-1")
    start = frame({"event": {"start": {"pid": 1}}})
    end = frame({"event": {"end": {"exited": True, "status": "exit status 0"}}})
    fake = FakeDaemon(start + frame({"event": {"data": {"stdout": "bGllcwo="}}}) + end + frame({}, flags=2))
    await fake.__aenter__()
    try:
        e = LocalEnvd(s, fake.port)
        assert await e._probe(sb, await e.urls(sb), e.token(sb)) is None
        # Taken for the daemon anyway (it answered before the address changed hands): its answers are refused.
        from hangar.envd import Endpoint

        e._eps[sb.id] = Endpoint(url=f"http://127.0.0.1:{fake.port}", token=e.token(sb), ref=sb.provider_ref)
        with pytest.raises(ProviderError, match="not from this sandbox's daemon"):
            await e.exec(sb, "true")
        assert sb.id not in e._eps  # and the address is looked up again
        e._eps[sb.id] = Endpoint(url=f"http://127.0.0.1:{fake.port}", token=e.token(sb), ref=sb.provider_ref)
        assert await e.read_file(sb, "/x") is SKIP
        e._eps[sb.id] = Endpoint(url=f"http://127.0.0.1:{fake.port}", token=e.token(sb), ref=sb.provider_ref)
        assert await e.write_file(sb, "/x", b"data") is False
    finally:
        await fake.__aexit__()


def test_docker_start_script_output_is_capped(tmp_path):
    """The start script runs whatever sits at the binary's path in the sandbox; its output must not be held whole."""
    from hangar.envd import DockerEnvd

    class Api:
        def exec_create(self, *a, **kw):
            return {"Id": "e1"}

        def exec_start(self, eid, stream=False, demux=False, **kw):
            if not stream:
                return b"x" * (64 << 20)
            return ((b"x" * 65536, None) for _ in range(1024))

        def exec_inspect(self, eid):
            return {"ExitCode": 1}

    prov = SimpleNamespace(name="docker", client=SimpleNamespace(api=Api()))
    (tmp_path / f"hangar-envd-{ARCH}").write_bytes(b"bin")
    e = DockerEnvd(prov, Settings(data_dir=tmp_path, envd_dir=tmp_path))
    sb = Sandbox(name="t", environment="x", provider="docker", provider_ref="c1")
    code, out = asyncio.run(e.run_script(sb, "tok"))
    assert code == 1 and len(out) <= 65536


# ---------------------------------------------------------------------- Kubernetes
class FakePod:
    """What KubernetesEnvd uses of the provider: the pod's name and IP, exec and the streamed upload."""

    name = "kubernetes"
    ns = "hangar-sandboxes"

    def __init__(self, ip: str = "10.8.0.7"):
        self.ip, self.execs, self.uploads = ip, [], []
        self.core = SimpleNamespace(read_namespaced_pod=lambda pod, ns: SimpleNamespace(
            status=SimpleNamespace(pod_ip=self.ip)))

    async def _pod(self, sb):
        return "pod-1"

    def _exec_blocking(self, pod, argv, timeout, keep=None):
        self.execs.append(argv)
        p = subprocess.run(argv, capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        return p.returncode, p.stdout, p.stderr

    def _upload_stream_blocking(self, pod, path, b64, mode, timeout, size=None):
        self.uploads.append((path, mode, size))
        return 0, ""


def k8s_envd(tmp_path, arch: str = ARCH):
    """A KubernetesEnvd over FakePod whose start script runs for real, with the sandbox's /tmp moved to tmp_path (so
    the script's look for daemons can only ever match processes this test started)."""
    from hangar.envd import KubernetesEnvd

    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / f"hangar-envd-{arch}").write_bytes(b"binary")
    pod = FakePod()
    e = KubernetesEnvd(pod, Settings(data_dir=tmp_path, envd_dir=tmp_path / "bin"))
    e.remote_dir = str(tmp_path / "remote")
    (tmp_path / "remote").mkdir()
    sb = Sandbox(name="t", environment="x", provider="kubernetes", provider_ref="hangar-sb-1")
    return e, pod, sb


def test_kubernetes_addresses(tmp_path):
    e, pod, sb = k8s_envd(tmp_path)
    assert asyncio.run(e.urls(sb)) == ["http://10.8.0.7:49983"]
    pod.ip = "fd00::7"
    assert asyncio.run(e.urls(sb)) == ["http://[fd00::7]:49983"]
    pod.ip = "not an ip"
    assert asyncio.run(e.urls(sb)) == []


def test_kubernetes_install_uploads_once_then_starts(tmp_path, monkeypatch):
    # The script runs for real: the binary isn't in the pod, so it asks for an upload (exit 42), and the second
    # run finds the uploaded file and starts it. Here the "uploaded" file is a stub that records how it was run.
    e, pod, sb = k8s_envd(tmp_path)
    remote = e.remote_path(ARCH)
    marker = tmp_path / "ran"

    def upload(pod_, path, b64, mode, timeout, size=None):
        pod.uploads.append((path, mode, size))
        Path(path).write_text(f'#!/bin/sh\necho "$HANGAR_ENVD_TOKEN $*" > {marker}\n')
        os.chmod(path, 0o755)
        return 0, ""

    monkeypatch.setattr(pod, "_upload_stream_blocking", upload)
    asyncio.run(e.install(sb, "tok-123"))
    assert pod.uploads == [(remote, 0o755, len(b"binary"))]
    assert len(pod.execs) == 2
    assert marker.read_text().split() == ["tok-123", "--daemon", "--port", "49983", "--workdir", e.s.workspace_dir,
                                          "--log", "/tmp/hangar-envd.log"]
    asyncio.run(e.install(sb, "tok-123"))  # already there: one exec, no upload
    assert len(pod.uploads) == 1 and len(pod.execs) == 3


def test_kubernetes_install_replaces_other_versions_but_not_itself(tmp_path):
    e, pod, sb = k8s_envd(tmp_path)
    sleep = shutil.which("sleep")
    # A daemon of another hangar version (here: `sleep` copied under such a name) is stopped, and this one started.
    old = Path(e.remote_dir) / "hangar-envd-0123456789abcdef-x86_64"
    shutil.copy(sleep, old)
    stale = subprocess.Popen([str(old), "30"])
    Path(e.remote_path(ARCH)).write_text("#!/bin/sh\nexit 0\n")
    os.chmod(e.remote_path(ARCH), 0o755)
    try:
        asyncio.run(e.install(sb, "tok"))
        assert stale.wait(5) == -15
        # This version already running but unreachable: not started twice, and said so.
        shutil.copy(sleep, e.remote_path(ARCH))
        same = subprocess.Popen([e.remote_path(ARCH), "30"])
        try:
            with pytest.raises(EnvdError, match="already running.*can't reach it"):
                asyncio.run(e.install(sb, "tok"))
            assert same.poll() is None
        finally:
            same.kill()
    finally:
        stale.kill()


def test_kubernetes_install_refuses_unknown_cpus(tmp_path):
    other = "aarch64" if ARCH == "x86_64" else "x86_64"
    e, _, sb = k8s_envd(tmp_path, other)
    with pytest.raises(EnvdError, match="43"):
        asyncio.run(e.install(sb, "tok"))


def test_binaries_are_found_by_arch(tmp_path):
    (tmp_path / "hangar-envd-x86_64").write_bytes(b"a")
    (tmp_path / "hangar-envd-aarch64").write_bytes(b"b")
    (tmp_path / "hangar-envd-mips").write_bytes(b"c")
    assert set(find_binaries(Settings(envd_dir=tmp_path))) == {"x86_64", "aarch64"}
    assert find_binaries(Settings(envd_dir=Path(tmp_path / "none"))) == {}
    off = LocalEnvd(Settings(envd="off", envd_dir=tmp_path), 1)
    assert not off.enabled
