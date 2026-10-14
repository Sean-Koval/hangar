"""hangar-envd: a daemon inside each sandbox that runs commands and moves files over HTTP (docs/envd.md).

Every `exec`, `read_file` and `write_file` otherwise goes through `docker exec` or the Kubernetes exec websocket (via
the API server): tens of milliseconds per call on Docker and more on GKE. The daemon (the Rust crate in envd/) speaks
E2B envd's protocol, so the Docker and Kubernetes providers start it in a sandbox on first use and route I/O to it,
falling back to their own exec path whenever it can't be used: no binary for the sandbox's CPU, an unreachable
address, a request as root, or a call that fails before the command started.

The daemon runs as the sandbox user, in a sandbox that runs untrusted code, so nothing it returns is trusted: output
keeps only a bounded tail, reads stop at the caller's limit (or MAX_READ), frames are size-capped, compressed answers
are refused, and every call has a deadline. Its access token is HMAC(the vault key, sandbox id): unique per sandbox,
never stored, and the same after a control-plane restart, so an already running daemon is found again rather than
started twice. Requests are signed with the token (`sign`) rather than carrying it, and answers must prove the token
(X-Hangar-Proof), so nothing else answering at a sandbox's address (a reused IP) is taken for its daemon. The
sandbox's own user can read its daemon's token and so pass as it, which gains it nothing: it can already run
anything there as that user.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import secrets
import shlex
import tarfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from hangar.config import Settings
from hangar.models import ExecResult, Sandbox
from hangar.providers.base import OutputTail, Provider, ProviderError, TerminalStream
from hangar.providers.docker_net import network  # --- isolation

log = logging.getLogger("hangar.envd")

PORT = 49983  # E2B envd's port
MAX_FRAME = 4 << 20  # largest Connect message accepted from a daemon
MAX_OUTPUT = 256 * 1024  # what exec results keep per stream, as the providers do
RETRY_AFTER = 300.0  # seconds before trying a sandbox whose daemon couldn't be used again
START_TIMEOUT = 90.0  # install + start; a k8s upload of the binary is the slow part
LOG = "/tmp/hangar-envd.log"
SKIP = object()  # "use the provider's own path" from read_file
MAX_READ = 64 << 20  # read_file without a limit: past this, the provider reads the file (a daemon could send forever)
EXEC_GRACE = 30.0  # past a command's timeout, how long to wait for the daemon to report its end
UPLOAD_CHUNK = 256 << 10  # frame size of a signed upload (`chunked_body`); measured best of 256 KiB, 1 and 4 MiB
# Transport failures that happen before a request is sent: nothing reached the daemon.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class EnvdError(ProviderError):
    """The daemon refused or failed a call. `started` says whether the command may have run."""

    def __init__(self, msg: str, code: str = "unknown", started: bool = False):
        super().__init__(msg)
        self.code, self.started = code, started


def find_binaries(settings: Settings) -> dict[str, Path]:
    """`uname -m` -> hangar-envd binary for that CPU, from HANGAR_ENVD_DIR, the control-plane image
    (/usr/local/lib/hangar) or a checkout's envd/dist (envd/build.sh)."""
    dirs = [settings.envd_dir] if settings.envd_dir else [
        Path("/usr/local/lib/hangar"), Path(__file__).resolve().parents[2] / "envd" / "dist"]
    out: dict[str, Path] = {}
    for d in dirs:
        for arch in ("x86_64", "aarch64"):
            p = d / f"hangar-envd-{arch}"
            if arch not in out and p.is_file():
                out[arch] = p
    return out


def _frame(payload: bytes, flags: int = 0) -> bytes:
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


def _b64(s: str) -> bytes:
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError, TypeError) as e:
        raise EnvdError("malformed output from envd", started=True) from e


def _connect_error(body: bytes, status: int) -> tuple[str, str]:
    try:
        d = json.loads(body[:65536])
        err = d.get("error", d) if isinstance(d, dict) else {}
        return str(err.get("code") or status)[:64], str(err.get("message") or "")[:500]
    except (ValueError, AttributeError):
        return str(status), body[:200].decode(errors="replace")


def _text(t: OutputTail[bytes]) -> tuple[str, bool]:
    """What the Docker provider does with a stream's tail: decode, keep the last MAX_OUTPUT characters, say so."""
    s = t.value().decode(errors="replace")
    cut = t.cut or len(s) > MAX_OUTPUT
    if cut:
        s = "[... truncated ...]\n" + s[-MAX_OUTPUT:]
    return s, cut


@dataclass
class Endpoint:
    url: str
    token: str
    ref: str | None
    version: str = ""


def sign(token: str, method: str, raw_path: bytes, body: str) -> tuple[str, bytes]:
    """X-Hangar-Sig (envd/src/auth.rs) and its MAC: HMAC(token; ms, nonce, method, path?query, body), where `body` is
    the body's SHA-256 in hex, or "chunked" for an upload whose frames carry their own MACs (`chunked_body`). The token
    itself never leaves the control plane, so a request that reaches the wrong address is useless to whoever got it.
    The daemon checks the time against its own clock, within 5 minutes either way."""
    ts, nonce = int(time.time() * 1000), secrets.token_hex(16)
    msg = f"{ts}\n{nonce}\n{method}\n".encode() + raw_path + f"\n{body}".encode()
    mac = hmac.new(token.encode(), msg, hashlib.sha256).digest()
    return f"{ts}.{nonce}.{body}.{mac.hex()}", mac


def proof(token: str, mac: bytes) -> str:
    """The X-Hangar-Proof on the daemon's answer to a request signed with `mac`: only a token holder can make it."""
    return hmac.new(token.encode(), b"proof\n" + mac, hashlib.sha256).hexdigest()


def chunked_body(data: bytes, size: int) -> tuple[int, list[memoryview]]:
    """A signed upload's body is frames of `u32 length | data | HMAC(token; "chunk\\n", previous HMAC (the request's
    for the first), length, data)`, ending with an empty frame. The daemon checks each frame before writing it, and
    the sender signs each as it goes (`frames`), so a big write isn't hashed whole before its first byte is sent.
    Returns the body's length and its parts (slices, not copies)."""
    parts = [memoryview(data)[i:i + size] for i in range(0, len(data), size)] + [memoryview(b"")]
    return len(data) + 36 * len(parts), parts


def frames(token: str, mac: bytes, parts: list[memoryview]) -> Iterator[bytes]:
    prev = mac
    for part in parts:
        head = len(part).to_bytes(4, "big")
        h = hmac.new(token.encode(), b"chunk\n", hashlib.sha256)
        h.update(prev)
        h.update(head)
        h.update(part)
        prev = h.digest()
        yield b"".join((head, part, prev))


class Envd:
    """Per-provider router: finds or starts each sandbox's daemon and makes calls to it.

    Starting a daemon happens in the background: the call that finds none running uses the provider, and the
    sandbox's later calls use the daemon once it answers. A daemon that can't be started or reached isn't tried
    again for RETRY_AFTER seconds."""

    def __init__(self, provider: Provider, settings: Settings):
        self.p, self.s = provider, settings
        self.binaries = find_binaries(settings) if settings.envd != "off" else {}
        self.remote_dir = "/tmp"  # where the binary goes in a sandbox
        self._eps: dict[str, Endpoint] = {}
        self._down: dict[str, float] = {}
        self._starting: dict[str, asyncio.Task] = {}
        self._digests: dict[str, str] = {}
        self._http: httpx.AsyncClient | None = None
        self._closed = False
        if settings.envd != "off" and not self.binaries:
            log.info("envd: no hangar-envd binary found (set HANGAR_ENVD_DIR or run envd/build.sh); "
                     "using %s exec", provider.name)

    @property
    def enabled(self) -> bool:
        return bool(self.binaries)

    # ------------------------------------------------------------------ plumbing
    def token(self, sb: Sandbox) -> str:
        from hangar import keys  # --- durable: during a key rotation, a sandbox from before keeps its token

        return hmac.new(keys.key_for(self.s, sb.created_at), b"hangar-envd:" + sb.id.encode(),
                        hashlib.sha256).hexdigest()

    def digest(self, arch: str) -> str:
        if arch not in self._digests:
            self._digests[arch] = hashlib.sha256(self.binaries[arch].read_bytes()).hexdigest()[:16]
        return self._digests[arch]

    def remote_path(self, arch: str) -> str:
        return f"{self.remote_dir}/hangar-envd-{self.digest(arch)}-{arch}"

    def start_args(self) -> list[str]:
        return ["--daemon", "--port", str(PORT), "--workdir", self.s.workspace_dir, "--log", LOG]

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            # trust_env=False: a configured HTTP proxy must never see these requests.
            self._http = httpx.AsyncClient(
                trust_env=False, timeout=httpx.Timeout(30.0, connect=2.0),
                limits=httpx.Limits(max_connections=2048, max_keepalive_connections=256, keepalive_expiry=30))
        return self._http

    async def close(self) -> None:
        """At shutdown (Fleet.stop, after #57's drain): the pool's connections go, and a call still in flight
        afterwards uses the provider rather than opening a new pool or starting a daemon (integrate)."""
        self._closed = True
        for t in self._starting.values():
            t.cancel()
        self._starting.clear()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @staticmethod
    def instance(sb: Sandbox) -> str | None:
        """What a cached daemon address belongs to: the provider ref, and on Kubernetes the pod's UID too, since the
        claim name stays the same when a pod is replaced or a paused sandbox resumes on a new pod (--- reconciler)."""
        uid = sb.info.get("pod_uid")
        return f"{sb.provider_ref}#{uid}" if uid and sb.provider_ref else sb.provider_ref

    def forget(self, sb: Sandbox) -> None:
        """The sandbox is gone (destroy): drop what is known about its daemon."""
        self._eps.pop(sb.id, None)
        self._down.pop(sb.id, None)
        if t := self._starting.pop(sb.id, None):
            t.cancel()

    def _lost(self, sb: Sandbox, why: str) -> None:
        """A call failed in transport: the daemon died, or the sandbox moved (a resumed pod has a new IP). Look it
        up again on the next call rather than trusting the old address."""
        if self._eps.pop(sb.id, None) is not None:
            log.info("envd: lost the daemon in %s (%s)", sb.id, why[:200])

    def _mark_down(self, sb: Sandbox, why: str) -> None:
        log.info("envd: not using a daemon in %s for %ds (%s); %s exec instead", sb.id, RETRY_AFTER, why[:300],
                 self.p.name)
        self._eps.pop(sb.id, None)
        self._down[sb.id] = time.monotonic() + RETRY_AFTER

    def request(self, ep: Endpoint, method: str, path: str, *, params: dict[str, str] | None = None,
                content: bytes = b"", headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None, upload: bool = False) -> httpx.Request:
        """A signed request. `upload`: `content` goes as a chunked signed body (`chunked_body`). The answer's
        expected X-Hangar-Proof rides along in `req.extensions["hangar_proof"]` (see `check_proof`)."""
        url = httpx.URL(f"{ep.url}{path}", params=params)
        headers = {**(headers or {}), "Accept-Encoding": "identity"}
        if upload:
            sig, mac = sign(ep.token, method, url.raw_path, "chunked")
            size, parts = chunked_body(content, UPLOAD_CHUNK)
            headers["Content-Length"] = str(size)
            if len(parts) <= 2:
                body: Any = b"".join(frames(ep.token, mac, parts))
            else:
                async def body_iter():
                    # Signing a frame holds the loop for under a millisecond; the daemon checks the last meanwhile.
                    for f in frames(ep.token, mac, parts):
                        yield f

                body = body_iter()
        else:
            sig, mac = sign(ep.token, method, url.raw_path, hashlib.sha256(content).hexdigest())
            body = content
        headers["X-Hangar-Sig"] = sig
        req = self.http.build_request(method, url, content=body, headers=headers,
                                      timeout=timeout or httpx.Timeout(30.0, connect=2.0))
        req.extensions["hangar_proof"] = proof(ep.token, mac)
        return req

    @staticmethod
    def check_proof(req: httpx.Request, resp: httpx.Response, started: bool = False) -> None:
        """Every answer but a refused signature (401) must come from the sandbox's daemon: anything else at its
        address (another sandbox that got its old IP, say) can't prove the token."""
        if resp.status_code != 401 and not hmac.compare_digest(
                resp.headers.get("x-hangar-proof", ""), req.extensions.get("hangar_proof", "-")):
            raise EnvdError("envd: the answer is not from this sandbox's daemon (no valid X-Hangar-Proof)", "impostor",
                            started=started)

    async def send_capped(self, req: httpx.Request, cap: int) -> tuple[int, bytes]:
        """Status and raw body of a small response, refusing more than `cap` bytes (and any compression: httpx
        would otherwise inflate whatever the sandbox sent)."""
        resp = await self.http.send(req, stream=True)
        try:
            self.check_proof(req, resp)
            if resp.headers.get("content-encoding", "identity") != "identity":
                raise EnvdError("envd: compressed response, none was asked for")
            body = bytearray()
            async for chunk in resp.aiter_raw():
                body += chunk
                if len(body) > cap:
                    raise EnvdError(f"envd: response over {cap} bytes")
            return resp.status_code, bytes(body)
        finally:
            await resp.aclose()

    # ------------------------------------------------------------------ finding and starting the daemon
    async def endpoint(self, sb: Sandbox) -> Endpoint | None:
        """The sandbox's daemon if it is known to be up; otherwise None, starting it in the background."""
        if self._closed or not self.binaries or not sb.provider_ref:
            return None
        ep = self._eps.get(sb.id)
        if ep is not None and ep.ref == self.instance(sb):  # --- reconciler: by pod, not just by claim
            return ep
        if self._down.get(sb.id, 0.0) > time.monotonic():
            return None
        t = self._starting.get(sb.id)
        if t is None or t.done():
            self._starting[sb.id] = asyncio.create_task(self._bring_up(sb))
        return None

    async def ready(self, sb: Sandbox, timeout: float = START_TIMEOUT) -> Endpoint | None:
        """Start the daemon (or wait for the start under way) and return it: for callers that would rather wait
        than use the provider meanwhile, like tests and benchmarks."""
        if (ep := await self.endpoint(sb)) is not None:
            return ep
        if t := self._starting.get(sb.id):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(t), timeout)
        return self._eps.get(sb.id)

    async def _bring_up(self, sb: Sandbox) -> None:
        ref = self.instance(sb)  # --- reconciler
        try:
            ep = await asyncio.wait_for(self._connect(sb), START_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - any failure means "use the provider", never a failed call
            self._mark_down(sb, str(e) or type(e).__name__)
            return
        finally:
            if self._starting.get(sb.id) is asyncio.current_task():
                del self._starting[sb.id]
        if self.instance(sb) != ref:  # --- reconciler
            return  # replaced meanwhile
        self._eps[sb.id] = ep
        self._down.pop(sb.id, None)
        sb.info["envd"] = {"url": ep.url, "version": ep.version}

    async def _connect(self, sb: Sandbox) -> Endpoint:
        token = self.token(sb)
        urls = await self.urls(sb)
        if not urls:
            raise EnvdError("the sandbox has no address the control plane can reach")
        if ep := await self._probe(sb, urls, token):
            return ep
        await self.install(sb, token)
        for _ in range(10):  # the daemon listens before `--daemon` returns; this covers address lookups lagging
            if ep := await self._probe(sb, await self.urls(sb), token):
                return ep
            await asyncio.sleep(0.1)
        raise EnvdError(f"started, but not answering at {', '.join(urls)}")

    async def _probe(self, sb: Sandbox, urls: list[str], token: str) -> Endpoint | None:
        for url in urls:
            ep = Endpoint(url=url, token=token, ref=self.instance(sb))  # --- reconciler
            try:
                status, body = await self.send_capped(
                    self.request(ep, "GET", "/info", timeout=httpx.Timeout(2.0, connect=0.5)), 65536)
                info = json.loads(body) if status == 200 else None
            except (httpx.HTTPError, EnvdError, ValueError):
                continue
            if isinstance(info, dict):
                ep.version = str(info.get("version", ""))[:32]
                return ep
        return None

    async def urls(self, sb: Sandbox) -> list[str]:
        """Base URLs to try for the sandbox's daemon, best first."""
        raise NotImplementedError

    async def install(self, sb: Sandbox, token: str) -> None:
        """Put the binary in the sandbox (if it isn't there) and start it as the sandbox user."""
        raise NotImplementedError

    # ------------------------------------------------------------------ Connect calls
    @staticmethod
    def _connect_headers(streaming: bool, timeout: float | None) -> dict[str, str]:
        h = {"Content-Type": "application/connect+json" if streaming else "application/json",
             "Connect-Protocol-Version": "1"}
        if timeout:
            h["Connect-Timeout-Ms"] = str(int(timeout * 1000))
        return h

    async def unary(self, ep: Endpoint, method: str, body: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        req = self.request(ep, "POST", f"/{method}", content=json.dumps(body).encode(),
                           headers=self._connect_headers(False, timeout), timeout=httpx.Timeout(timeout + 5, connect=2))
        try:
            status, raw = await self.send_capped(req, MAX_FRAME)
        except httpx.HTTPError as e:
            raise EnvdError(f"envd {method}: {e}", "transport") from e
        if status != 200:
            code, msg = _connect_error(raw, status)
            raise EnvdError(f"envd {method}: {code}: {msg}", code)
        try:
            out = json.loads(raw) if raw else {}
        except ValueError as e:
            raise EnvdError(f"envd {method}: malformed response") from e
        return out if isinstance(out, dict) else {}

    async def stream(self, ep: Endpoint, method: str, body: dict[str, Any], timeout: float | None,
                     keepalive: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Messages of a server stream. EnvdError(started=False) means the daemon refused the call (an error status,
        or an error before any message) or never got it: nothing ran, so the caller may use the provider instead.
        Any other failure, once the request may have arrived, is started=True: the work may be under way."""
        headers = self._connect_headers(True, timeout)
        if keepalive:
            headers["Keepalive-Ping-Interval"] = str(keepalive)
        # Between messages: the call's own timeout, or for a long-lived stream a few missed keepalives.
        read = timeout + 30 if timeout is not None else (4 * keepalive if keepalive else None)
        req = self.request(ep, "POST", f"/{method}", content=_frame(json.dumps(body).encode()), headers=headers,
                           timeout=httpx.Timeout(read, connect=2.0))
        try:
            resp = await self.http.send(req, stream=True)
        except NOT_SENT as e:
            raise EnvdError(f"envd {method}: {e}", "transport") from e
        except httpx.HTTPError as e:
            raise EnvdError(f"envd {method}: {e or type(e).__name__}", "transport", started=True) from e
        started = True  # from here on, a failure that isn't the daemon's refusal may have followed a start
        try:
            self.check_proof(req, resp, started=True)  # the request reached something; it may have passed it on
            if resp.status_code != 200:
                raw = b""
                async for chunk in resp.aiter_raw():
                    raw += chunk
                    if len(raw) > 65536:
                        break
                code, msg = _connect_error(raw, resp.status_code)
                raise EnvdError(f"envd {method}: {code}: {msg}", code, started=False)
            first = True
            buf = bytearray()
            async for chunk in resp.aiter_raw():  # raw: a sandbox doesn't get to make us decompress anything
                buf += chunk
                while len(buf) >= 5:
                    flags, n = buf[0], int.from_bytes(buf[1:5], "big")
                    if n > MAX_FRAME:
                        raise EnvdError(f"envd {method}: a {n}-byte message is over the limit", started=started)
                    if len(buf) < 5 + n:
                        break
                    data = bytes(buf[5:5 + n])
                    del buf[:5 + n]
                    if flags & 1:
                        raise EnvdError(f"envd {method}: compressed message, none was asked for", started=started)
                    try:
                        msg = json.loads(data) if data else {}
                    except ValueError as e:
                        raise EnvdError(f"envd {method}: malformed message", started=started) from e
                    if flags & 2:  # end of stream: {} or {"error": {...}}
                        if isinstance(msg, dict) and msg.get("error"):
                            code, text = _connect_error(data, 200)
                            # An error before any event is the daemon refusing the call (no cwd, the process cap).
                            raise EnvdError(f"envd {method}: {code}: {text}", code, started=not first)
                        return
                    if not isinstance(msg, dict):
                        raise EnvdError(f"envd {method}: malformed message", started=started)
                    first = False
                    yield msg
            raise EnvdError(f"envd {method}: the stream ended without its end message", "transport", started=started)
        except httpx.HTTPError as e:
            raise EnvdError(f"envd {method}: {e or type(e).__name__}", "transport", started=started) from e
        finally:
            await resp.aclose()

    # ------------------------------------------------------------------ provider operations
    async def exec(self, sb: Sandbox, command: str, timeout: int = 120, cwd: str | None = None,
                   user: str | None = None) -> ExecResult | None:
        """The provider's `exec` through the daemon, with the same `timeout` wrapper, caps and result; None means
        "use the provider" (and that the command did not run)."""
        if user and user != self.s.sandbox_user:
            return None  # the daemon is unprivileged; root commands keep the provider's path
        ep = await self.endpoint(sb)
        if ep is None:
            return None
        body = {"process": {"cmd": "timeout", "args": ["--kill-after=5", str(int(timeout)), "bash", "-lc", command],
                            "cwd": cwd or self.s.workspace_dir}, "stdin": False}
        out, err = OutputTail(4 * MAX_OUTPUT, b""), OutputTail(4 * MAX_OUTPUT, b"")
        end: dict[str, Any] | None = None
        t0 = time.monotonic()
        try:
            # The daemon ends the stream once it has killed the command (Connect-Timeout-Ms below); one that doesn't
            # doesn't get to hold the caller past that.
            async with asyncio.timeout(timeout + 15 + EXEC_GRACE):
                # Read to the end-of-stream frame (it follows the exit event), so the connection goes back to the pool.
                async with contextlib.aclosing(self.stream(ep, "process.Process/Start", body, timeout + 15)) as events:
                    async for msg in events:
                        ev = msg.get("event") or {}
                        if (data := ev.get("data")) and isinstance(data, dict):
                            if "stdout" in data:
                                out.add(_b64(data["stdout"]))
                            elif "stderr" in data:
                                err.add(_b64(data["stderr"]))
                        elif (e := ev.get("end")) is not None:
                            end = e if isinstance(e, dict) else {}
        except TimeoutError as e:
            self._lost(sb, "a command's stream outlived its timeout")
            if end is None:
                raise ProviderError("exec failed: no exit status from the sandbox's daemon in time; the command may "
                                    "still be running") from e
        except EnvdError as e:
            if e.code in ("transport", "unauthenticated", "impostor"):
                self._lost(sb, str(e))
            if not e.started:
                return None  # refused before starting (no cwd, the process limit, a dead daemon): the provider runs it
            if e.code == "impostor":
                raise ProviderError(f"exec failed: {e}; the command may have run elsewhere") from e
            if end is None:
                raise ProviderError(f"exec failed: lost the command's output ({e}); it may still be running") from e
        if end is None:
            raise ProviderError("exec failed: the command's output stream ended without an exit status")
        exited = end.get("exited") is True
        code = end.get("exitCode", 0) if exited else -1
        code = code if isinstance(code, int) else -1
        o, t1 = _text(out)
        e_, t2 = _text(err)
        if code == 124:
            e_ += f"\n[hangar] command timed out after {timeout}s"
        return ExecResult(exit_code=code, stdout=o, stderr=e_, duration_ms=int((time.monotonic() - t0) * 1000),
                          truncated=t1 or t2, transport="envd")

    async def read_file(self, sb: Sandbox, path: str, limit: int | None = None) -> Any:
        """The file's bytes (no more than `limit` + 1), None if it isn't a readable regular file, or SKIP."""
        ep = await self.endpoint(sb)
        if ep is None:
            return SKIP
        req = self.request(ep, "GET", "/files", params={"path": path}, timeout=httpx.Timeout(300.0, connect=2.0))
        cap = MAX_READ if limit is None else limit + 1
        try:
            # A deadline for the whole read: a daemon trickling bytes can't hold the caller past what a big file takes.
            async with asyncio.timeout(300 + cap / (1 << 20)):
                resp = await self.http.send(req, stream=True)
                try:
                    self.check_proof(req, resp)
                    if resp.status_code in (400, 404):
                        return None
                    if resp.status_code != 200 or resp.headers.get("content-encoding", "identity") != "identity":
                        return SKIP  # e.g. 403: a file the sandbox user can't read; the provider may
                    declared = resp.headers.get("content-length", "")
                    if limit is None and declared.isdigit() and int(declared) >= cap:
                        return SKIP  # more than a daemon's answer may be: the provider reads the file
                    parts, n = [], 0
                    async for chunk in resp.aiter_raw():
                        parts.append(chunk)
                        n += len(chunk)
                        if n >= cap:
                            break
                    if limit is None and n >= cap:
                        return SKIP
                    return b"".join(parts)[:cap]
                finally:
                    await resp.aclose()
        except (httpx.HTTPError, EnvdError, TimeoutError) as e:
            self._lost(sb, str(e) or type(e).__name__)
            return SKIP

    async def write_file(self, sb: Sandbox, path: str, data: bytes, mode: int = 0o644) -> bool:
        """True once written through the daemon; False means "use the provider" (a whole-file write is safe to
        repeat, so any failure here falls back)."""
        ep = await self.endpoint(sb)
        if ep is None:
            return False
        # Signed frame by frame as it is sent (`chunked_body`): no pass over the whole body before the first byte.
        req = self.request(ep, "POST", "/files", params={"path": path, "mode": f"{mode & 0o7777:o}"}, content=data,
                           headers={"Content-Type": "application/octet-stream"}, upload=True,
                           timeout=httpx.Timeout(60.0, connect=2.0))
        try:
            async with asyncio.timeout(60 + len(data) / (1 << 20)):
                status, _ = await self.send_capped(req, 65536)
        except (httpx.HTTPError, TimeoutError) as e:
            self._lost(sb, str(e) or type(e).__name__)
            return False
        except EnvdError as e:
            if e.code == "impostor":
                self._lost(sb, str(e))
            return False
        if status != 200:
            if status != 403:
                log.warning("envd: write of %s in %s failed (%s); using %s", path, sb.id, status, self.p.name)
            return False
        return True

    async def open_terminal(self, sb: Sandbox, command: list[str], cols: int, rows: int) -> TerminalStream | None:
        ep = await self.endpoint(sb)
        if ep is None or not command:
            return None
        body = {"process": {"cmd": command[0], "args": command[1:], "cwd": self.s.workspace_dir,
                            "envs": {"TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)}},
                "pty": {"size": {"cols": cols, "rows": rows}}}
        events = self.stream(ep, "process.Process/Start", body, None, keepalive=30)
        try:
            first = await anext(events)
            pid = int(((first.get("event") or {}).get("start") or {}).get("pid") or 0)
        except (EnvdError, StopAsyncIteration, ValueError, TypeError) as e:
            await events.aclose()
            log.info("envd: terminal in %s failed to start (%s); using %s", sb.id, e, self.p.name)
            return None
        if pid <= 0:
            await events.aclose()
            return None
        return EnvdTerminal(self, ep, pid, events)


class EnvdTerminal(TerminalStream):
    """A PTY process in the daemon: output from its Start stream, input/resize/close as unary calls."""

    def __init__(self, envd: Envd, ep: Endpoint, pid: int, events: AsyncIterator[dict[str, Any]]):
        self._envd, self._ep, self.pid, self._events = envd, ep, pid, events
        self._closed = False

    async def read(self) -> bytes:
        while not self._closed:
            try:
                msg = await anext(self._events)
            except (StopAsyncIteration, EnvdError, ProviderError):
                return b""
            ev = msg.get("event") or {}
            if (data := ev.get("data")) and isinstance(data, dict) and "pty" in data:
                try:
                    return _b64(data["pty"])
                except EnvdError:
                    return b""
            if "end" in ev:
                return b""
        return b""

    async def write(self, data: bytes) -> None:
        if not self._closed:
            await self._envd.unary(self._ep, "process.Process/SendInput",
                                   {"process": {"pid": self.pid}, "input": {"pty": base64.b64encode(data).decode()}})

    async def resize(self, cols: int, rows: int) -> None:
        if not self._closed:
            try:
                await self._envd.unary(self._ep, "process.Process/Update",
                                       {"process": {"pid": self.pid}, "pty": {"size": {"cols": cols, "rows": rows}}})
            except EnvdError:
                pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._envd.unary(self._ep, "process.Process/SendSignal",
                                   {"process": {"pid": self.pid}, "signal": "SIGNAL_SIGKILL"}, timeout=5)
        except EnvdError:
            pass  # already gone
        aclose = getattr(self._events, "aclose", None)
        if aclose is not None:
            # A read still unwinding from its task's cancellation holds the stream; the killed process ends it then.
            with contextlib.suppress(RuntimeError):
                await aclose()


# ---------------------------------------------------------------------- providers
def _http_url(ip: str) -> str | None:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return f"http://[{a}]:{PORT}" if a.version == 6 else f"http://{a}:{PORT}"


class Installer(Envd):
    """Starting the daemon the same way on every provider: one script, run as the sandbox user, that picks the
    binary for the CPU, says if it needs uploading (exit 42), refuses to start a second copy of a daemon that is
    already running (44: it runs but the control plane can't reach it), stops daemons of other hangar versions, and
    starts this one. The providers differ only in how they run a script and copy a file in."""

    async def run_script(self, sb: Sandbox, token: str) -> tuple[int, str]:
        """Run the start script in the sandbox as the sandbox user, with the token; (exit code, output)."""
        raise NotImplementedError

    async def upload(self, sb: Sandbox, path: str, data: bytes) -> None:
        raise NotImplementedError

    def script(self, inline_token: str | None) -> str:
        case = " ".join(f"{a}) b={shlex.quote(self.remote_path(a))} ;;" for a in sorted(self.binaries))
        start = " ".join(shlex.quote(x) for x in self.start_args())
        env = f"HANGAR_ENVD_TOKEN={shlex.quote(inline_token)} " if inline_token else ""
        return (f'a=$(uname -m); case "$a" in {case} *) echo "no hangar-envd binary for $a"; exit 43 ;; esac; '
                f'[ -x "$b" ] || {{ echo "need $a"; exit 42; }}; '
                'for p in /proc/[0-9]*; do e=$(readlink "$p/exe" 2>/dev/null) || continue; case "$e" in '
                '"$b") echo "a daemon is already running (pid ${p#/proc/})"; exit 44 ;; '
                f'{shlex.quote(self.remote_dir)}/hangar-envd-*) kill "${{p#/proc/}}" 2>/dev/null ;; esac; done; '
                # A daemon of another version just stopped may hold the port for a moment (its shutdown grace).
                f'for i in 1 2 3 4 5 6 7 8 9 10; do {env}"$b" {start} && exit 0; sleep 0.3; done; exit 1')

    async def install(self, sb: Sandbox, token: str) -> None:
        code, out = await self.run_script(sb, token)
        if code == 42:
            arch = out.strip().rsplit(" ", 1)[-1]
            if arch not in self.binaries:
                raise EnvdError(f"no hangar-envd binary for {arch}")
            await self.upload(sb, self.remote_path(arch), self.binaries[arch].read_bytes())
            code, out = await self.run_script(sb, token)
        if code != 0:
            why = {43: "", 44: "; the control plane can't reach it"}.get(code, "")
            raise EnvdError(f"hangar-envd did not start ({code}): {out[-300:].strip()}{why}")


class DockerEnvd(Installer):
    """Docker: the binary is copied in with put_archive (root-owned, so the sandbox user can't swap it). Reached at the
    container's IP (Linux hosts, or a control plane on the same network as the sandboxes) or at the port published on
    127.0.0.1 (Docker Desktop, where container IPs aren't routable)."""

    async def urls(self, sb: Sandbox) -> list[str]:
        api = self.p.client.api  # type: ignore[attr-defined]
        attrs = await asyncio.to_thread(api.inspect_container, sb.provider_ref)
        ns = attrs.get("NetworkSettings") or {}
        nets: dict[str, dict] = ns.get("Networks") or {}
        order = sorted(nets, key=lambda n: n != network(self.s))  # --- isolation: hangar's network first
        out = [u for n in order if (u := _http_url((nets[n] or {}).get("IPAddress") or ""))]
        for b in (ns.get("Ports") or {}).get(f"{PORT}/tcp") or []:
            host = b.get("HostIp") or "127.0.0.1"
            host = "127.0.0.1" if host in ("0.0.0.0", "") else ("::1" if host == "::" else host)
            if (u := _http_url(host)) and str(b.get("HostPort", "")).isdigit():
                out.append(u.rsplit(":", 1)[0] + f":{int(b['HostPort'])}")
        return out

    async def run_script(self, sb: Sandbox, token: str) -> tuple[int, str]:
        api = self.p.client.api  # type: ignore[attr-defined]

        def _run() -> tuple[int, str]:
            # The token goes in the exec's environment, not its command line.
            eid = api.exec_create(sb.provider_ref, ["sh", "-c", self.script(None)], user=self.s.sandbox_user,
                                  environment={"HANGAR_ENVD_TOKEN": token})["Id"]
            # What runs is whatever sits at the binary's path in the sandbox: keep a tail of its output, and stop
            # reading once the start should long be over (a stream nothing writes to still blocks; the caller's
            # START_TIMEOUT gives up on it).
            out, deadline = OutputTail(65536, b""), time.monotonic() + START_TIMEOUT
            chunks = api.exec_start(eid, stream=True, demux=True)
            try:
                for o, e in chunks:
                    out.add((o or b"") + (e or b""))
                    if time.monotonic() > deadline:
                        break
            finally:
                close = getattr(chunks, "close", None)
                if close is not None:
                    close()
            code = api.exec_inspect(eid).get("ExitCode")
            return (code if isinstance(code, int) else -1), out.value()[-65536:].decode(errors="replace")

        return await asyncio.to_thread(_run)

    async def upload(self, sb: Sandbox, path: str, data: bytes) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(path.rsplit("/", 1)[1])
            info.size, info.mode, info.mtime = len(data), 0o755, int(time.time())
            tf.addfile(info, io.BytesIO(data))
        api = self.p.client.api  # type: ignore[attr-defined]
        if not await asyncio.to_thread(api.put_archive, sb.provider_ref, path.rsplit("/", 1)[0], buf.getvalue()):
            raise EnvdError("could not copy hangar-envd into the sandbox")


class KubernetesEnvd(Installer):
    """Kubernetes: reached at the pod IP (deploy/k8s/envd-network.yaml lets only hangar's pods in). The first start in
    a pod streams the binary in (~3 MB, one exec)."""

    async def urls(self, sb: Sandbox) -> list[str]:
        pod = await self.p._pod(sb)  # type: ignore[attr-defined]
        p = await asyncio.to_thread(self.p.core.read_namespaced_pod, pod, self.p.ns)  # type: ignore[attr-defined]
        if (want := sb.info.get("pod_uid")) and p is not None and p.metadata.uid != want:  # --- reconciler
            raise EnvdError(f"the pod of sandbox {sb.id} was replaced; not starting a daemon in a blank pod")
        ip = (p.status.pod_ip if p and p.status else None) or ""
        u = _http_url(ip)
        return [u] if u else []

    async def run_script(self, sb: Sandbox, token: str) -> tuple[int, str]:
        # pods/exec has no environment: the token is on the start script's command line, readable for a moment
        # only by the sandbox user (who can read the daemon's memory anyway).
        pod = await self.p._pod(sb)  # type: ignore[attr-defined]
        run: Callable[..., tuple[int, str, str]] = self.p._exec_blocking  # type: ignore[attr-defined]
        code, out, err = await asyncio.to_thread(run, pod, ["sh", "-c", self.script(token)], 60)
        return code, out + err

    async def upload(self, sb: Sandbox, path: str, data: bytes) -> None:
        pod = await self.p._pod(sb)  # type: ignore[attr-defined]
        upload = self.p._upload_stream_blocking  # type: ignore[attr-defined]
        code, err = await asyncio.to_thread(upload, pod, path, data, 0o755, 120, len(data))  # --- scale: raw bytes
        if code != 0:
            raise EnvdError(f"could not copy hangar-envd into the pod: {err[-300:]}")


class Off:
    """The router of a provider built without its __init__ (tests): every call goes to the provider."""

    enabled = False

    async def exec(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def read_file(self, *_a: Any, **_kw: Any) -> Any:
        return SKIP

    async def write_file(self, *_a: Any, **_kw: Any) -> bool:
        return False

    async def open_terminal(self, *_a: Any, **_kw: Any) -> None:
        return None

    def forget(self, _sb: Sandbox) -> None:
        pass

    async def close(self) -> None:
        pass


OFF = Off()
