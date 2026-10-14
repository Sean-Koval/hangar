"""Git credential gateway (spec 0003 M2-M4): sandboxes push and fetch through the control plane.

A sandbox in `credential_mode: gateway` never holds a GitHub token. It gets a grant token (`hgs_…`), good only
here, only for its own repo, and only while the sandbox is alive. Its git is configured with
`url.<gateway>/git/github.com/.insteadOf https://github.com/`, so clones, fetches and pushes reach the
smart-HTTP proxy below. The proxy checks the grant, reads the `git-receive-pack` command list before any of
the pack is forwarded, refuses refs outside the grant (other branches, deletes, tags), and only then streams
the request to GitHub with the real credential: a PAT from the vault, or a GitHub App installation token
minted for that one repo.

Protocol references (checked against git-scm.com/docs): gitprotocol-http (smart discovery, content types),
gitprotocol-pack (update-requests, report-status), gitprotocol-capabilities (side-band, push-options),
gitprotocol-common (pkt-line). Installation tokens: docs.github.com "Create an installation access token for
an app" and "Generating a JWT for a GitHub App".
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import shlex
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx
from pydantic import BaseModel, field_validator
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from hangar import observe  # --- observe
from hangar.auth import hash_token
from hangar.models import Credential, CredentialKind, Sandbox, SandboxGrant, SandboxState, Session, Snapshot, now

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.providers.base import Provider

log = logging.getLogger("hangar.gateway")

PREFIX = "hgs_"
GIT_KINDS = {CredentialKind.GIT_TOKEN, CredentialKind.GITHUB_APP, CredentialKind.SSH_KEY}
MAX_COMMAND_BYTES = 64 * 1024  # the receive-pack command list; a normal push is a few hundred bytes
MAX_COMMANDS = 8
MAX_FETCH_REQUEST = 64 * 1024 * 1024  # an upload-pack request lists wants and haves; big repos need a few MB
SERVICES = ("git-upload-pack", "git-receive-pack")
# Installation tokens live an hour; take a new one when less than this is left.
TOKEN_REFRESH_SECONDS = 600
# --- overnight/sandbox-security: limits a sandbox can't talk its way past (docs/security.md, threat model)
MAX_INFLIGHT_PER_GRANT = 8  # concurrent requests per grant; git uses one or two at a time
AUTH_FAILURES = 30  # failed grant checks per source address...
AUTH_WINDOW = 60.0  # ...per this many seconds, before that address gets 429 without a lookup
# Session branches are hangar/session/<name>: predictable, so only a sandbox granted one exactly may push there.
SESSION_REFS = "refs/heads/hangar/session/"

_PATH = re.compile(r"^/git/(?P<host>[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?)/(?P<owner>[A-Za-z0-9_.-]{1,100})/"
                   r"(?P<repo>[A-Za-z0-9_.-]{1,100})/(?P<svc>info/refs|git-upload-pack|git-receive-pack)$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REF_CHARS = re.compile(r"^refs/[A-Za-z0-9._/+@-]+$")


class GatewayError(RuntimeError):
    """A configuration problem the caller can fix (message is shown as is)."""


class ProtocolError(ValueError):
    """A malformed or unsupported receive-pack request."""


# ============================================================================ refs and remotes
def valid_ref(ref: str) -> bool:
    """Stricter than git check-ref-format: ASCII names only, which is all hangar's branches use."""
    return bool(_REF_CHARS.match(ref)) and len(ref) <= 255 and ".." not in ref and "//" not in ref \
        and "@{" not in ref and not ref.endswith(("/", ".lock", ".")) and "/." not in ref


def ref_allowed(patterns: list[str], ref: str) -> bool:
    for p in patterns:
        if p.endswith("/**"):
            if ref.startswith(p[:-2]) and len(ref) > len(p) - 2:
                return True
        elif ref == p:
            return True
    return False


def _allowed_text(patterns: list[str]) -> str:
    out = []
    for p in patterns:
        name = p.removeprefix("refs/heads/")
        out.append(f"branches under {name[:-2]}" if p.endswith("/**") else name)
    return " or ".join(out) if out else "nothing (no branch was granted)"


def parse_remote(url: str | None, hosts: list[str]) -> tuple[str, str] | None:
    """(host, "owner/repo") for a clone URL on one of `hosts` (https, scp-style or ssh), else None."""
    if not url:
        return None
    u = url.strip()
    for rx in (r"^https?://(?P<host>[^/@]+)/(?P<o>[\w.-]+)/(?P<r>[\w.-]+?)(?:\.git)?/?$",
               r"^git@(?P<host>[^:/]+):(?P<o>[\w.-]+)/(?P<r>[\w.-]+?)(?:\.git)?$",
               r"^ssh://git@(?P<host>[^/]+)/(?P<o>[\w.-]+)/(?P<r>[\w.-]+?)(?:\.git)?/?$"):
        m = re.match(rx, u)
        if m and m["host"].lower() in hosts and m["o"] not in (".", "..") and m["r"] not in (".", ".."):
            return m["host"].lower(), f"{m['o']}/{m['r']}".lower()
    return None


def _has_userinfo(url: str | None) -> bool:
    return bool(url and re.match(r"^[a-z+]+://[^/]*@", url.strip()) and not url.strip().startswith("ssh://git@"))


# ============================================================================ pkt-line
def pkt(data: bytes) -> bytes:
    return b"%04x" % (len(data) + 4) + data


FLUSH = b"0000"


def iter_pkts(data: bytes) -> list[bytes]:
    """Best-effort split of a complete pkt-line stream (flushes dropped); stops at the first bad header."""
    out, pos = [], 0
    while pos + 4 <= len(data):
        try:
            n = int(data[pos:pos + 4], 16)
        except ValueError:
            break
        if n == 0:
            pos += 4
            continue
        if n < 4 or pos + n > len(data):
            break
        out.append(data[pos + 4:pos + n])
        pos += n
    return out


class Command(NamedTuple):
    old: str
    new: str
    ref: str


class Commands(NamedTuple):
    commands: list[Command]
    capabilities: list[str]


def _is_zero(oid: str) -> bool:
    return set(oid) == {"0"}


def parse_commands(buf: bytes) -> tuple[Commands, int] | None:
    """Parse the command list at the start of a receive-pack request body (gitprotocol-pack: update-requests).

    Returns the commands and the offset just past the flush-pkt, or None when more bytes are needed. Signed
    pushes (push-cert) are refused rather than parsed: their commands live inside the certificate."""
    pos, cmds, caps = 0, [], []
    while True:
        if pos > MAX_COMMAND_BYTES:
            raise ProtocolError("the push command list is too long")
        if len(buf) - pos < 4:
            return None
        head = buf[pos:pos + 4]
        if not re.fullmatch(rb"[0-9a-fA-F]{4}", head):
            raise ProtocolError("malformed pkt-line")
        n = int(head, 16)
        if n == 0:
            return Commands(cmds, caps), pos + 4
        if n < 5:
            raise ProtocolError("unexpected special pkt-line in the command list")
        if len(buf) - pos < n:
            return None
        line = buf[pos + 4:pos + n]
        pos += n
        if line.endswith(b"\n"):
            line = line[:-1]
        if line.startswith(b"push-cert"):
            raise ProtocolError("signed pushes (git push --signed) are not supported by the hangar gateway")
        if line.startswith(b"shallow "):
            if cmds or not _OID.match(line[8:].decode("ascii", "replace")):
                raise ProtocolError("malformed shallow line")
            continue
        if not cmds and b"\0" in line:
            line, _, capstr = line.partition(b"\0")
            caps = capstr.decode("ascii", "replace").split()
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError as e:
            raise ProtocolError("non-ASCII ref update") from e
        parts = text.split(" ")
        if len(parts) != 3 or not _OID.match(parts[0]) or not _OID.match(parts[1]) or \
                len(parts[0]) != len(parts[1]) or not parts[2].isprintable():
            raise ProtocolError("malformed ref update")
        cmds.append(Command(*parts))
        if len(cmds) > MAX_COMMANDS:
            raise ProtocolError(f"a push may update at most {MAX_COMMANDS} refs through the hangar gateway")


class CommandScanner:
    """Finds the end of the command list as the request body arrives, then parses it once.

    Re-parsing the whole buffer per chunk was quadratic: a sandbox sending one byte per TCP segment could stall
    the control plane's event loop, which the gateway shares."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self._pos = 0

    def feed(self, chunk: bytes) -> tuple[Commands, int] | None:
        """(commands, end offset) once the flush-pkt has arrived, else None. Raises ProtocolError."""
        self.buf += chunk
        while len(self.buf) - self._pos >= 4:
            head = bytes(self.buf[self._pos:self._pos + 4])
            n = int(head, 16) if re.fullmatch(rb"[0-9a-fA-F]{4}", head) else -1
            if n < 5 or self._pos > MAX_COMMAND_BYTES:  # flush, special or malformed pkt, or too long: decide now
                return parse_commands(bytes(self.buf))
            if len(self.buf) - self._pos < n:
                return None
            self._pos += n
        return None


# --- overnight/sandbox-security
def has_cap(caps: list[str], name: str) -> bool:
    """git-receive-pack turns a capability on for `name` and for `name=<value>` (connect.c parse_feature_value),
    so a check for the bare name alone can be sidestepped."""
    return any(c == name or c.startswith(name + "=") for c in caps)


def _conflict(ref: str, reserved: dict[str, str]) -> str | None:
    """Why creating `ref` would take or block a branch someone else needs (`reserved`: ref -> reason). Git keeps
    refs as paths, so `a/b` blocks `a/b/c` and the other way round, and deletes (the only cleanup) are refused."""
    if ref.startswith(SESSION_REFS) or SESSION_REFS.startswith(ref + "/"):
        return "hangar: branches under hangar/session/ are reserved for sessions"
    for r, why in reserved.items():
        if ref == r or ref.startswith(r + "/") or r.startswith(ref + "/"):
            return why
    return None


def check_push(grant: SandboxGrant, cmds: Commands, allow_tags: bool, owned_elsewhere: set[str],
               session_refs: dict[str, str] | None = None) -> dict[str, str]:
    """{ref: reason} for every command the grant does not allow; empty means the push may go upstream.

    `owned_elsewhere`: exact refs other live sandboxes may push. `session_refs`: {ref: owner} ("session x",
    "job job-…") for the branches of sessions and unfinished jobs other than the pusher's own."""
    bad: dict[str, str] = {}
    push_options = has_cap(cmds.capabilities, "push-options")
    reserved = {r: f"hangar: that branch belongs to {who}" for r, who in (session_refs or {}).items()}
    reserved.update({r: "hangar: that branch belongs to another sandbox" for r in owned_elsewhere})
    for c in cmds.commands:
        if push_options:
            bad[c.ref] = "hangar: push options are not allowed"
        elif not valid_ref(c.ref):
            bad[c.ref] = "hangar: invalid ref name"
        elif _is_zero(c.new):
            bad[c.ref] = "hangar: deleting refs is not allowed"
        elif c.ref.startswith("refs/tags/"):
            if not allow_tags:
                bad[c.ref] = "hangar: pushing tags is not allowed"
            elif not _is_zero(c.old):
                bad[c.ref] = "hangar: tags can be created, not moved"
        elif not ref_allowed(grant.push_refs, c.ref):
            bad[c.ref] = f"hangar: this sandbox may push only to {_allowed_text(grant.push_refs)}"
        elif c.ref not in grant.push_refs and c.ref in owned_elsewhere:
            bad[c.ref] = "hangar: that branch belongs to another sandbox"
        elif c.ref not in grant.push_refs and not _is_zero(c.old):
            # A pattern lets a sandbox create branches; once created they are its own (see record_created).
            # Updating one it didn't create could overwrite another job's pushed work.
            bad[c.ref] = "hangar: this sandbox may create new branches there, not update existing ones"
        elif c.ref not in grant.push_refs and (why := _conflict(c.ref, reserved)):
            bad[c.ref] = why
    return bad


def rejection(cmds: Commands, bad: dict[str, str]) -> bytes:
    """A receive-pack result that makes git print `! [remote rejected] <ref> (hangar: …)` for each ref.

    Nothing is forwarded, so every command fails: the allowed ones are reported as not pushed."""
    report = pkt(b"unpack ok\n")
    for c in cmds.commands:
        why = bad.get(c.ref) or "hangar: not pushed, another ref in this push was rejected"
        report += pkt(f"ng {c.ref} {why}\n".encode())
    report += FLUSH
    caps = cmds.capabilities
    if not has_cap(caps, "side-band-64k") and not has_cap(caps, "side-band"):
        return report
    size = 65515 if has_cap(caps, "side-band-64k") else 995
    first = next(iter(bad.values()), "hangar: push rejected")
    out = pkt(b"\x02" + f"{first}\n".encode())  # band 2: shown as "remote: hangar: …"
    for i in range(0, len(report), size):
        out += pkt(b"\x01" + report[i:i + size])
    return out + FLUSH


def parse_report(data: bytes, sideband: bool) -> list[str]:
    """Status lines ("unpack ok", "ok <ref>", "ng <ref> <why>") from a receive-pack result, best effort."""
    lines = iter_pkts(data)
    if sideband:
        lines = iter_pkts(b"".join(x[1:] for x in lines if x[:1] == b"\x01"))
    return [x.decode("utf-8", "replace").rstrip("\n") for x in lines][:50]


# ============================================================================ GitHub App
class AppCredential(BaseModel):
    app_id: str
    installation_id: int | None = None
    private_key: str

    @field_validator("app_id", mode="before")
    @classmethod
    def _str_id(cls, v: Any) -> str:
        return str(v)


def parse_app_credential(value: str | bytes) -> AppCredential:
    """Validate a `github_app` credential. Errors never echo the value."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    try:
        data = json.loads(value)
        cred = AppCredential.model_validate(data)
    except Exception as e:  # noqa: BLE001
        raise GatewayError("a github_app credential is JSON: {\"app_id\": ..., \"installation_id\": ... (optional), "
                           "\"private_key\": \"-----BEGIN RSA PRIVATE KEY-----...\"}") from e
    try:
        key = load_pem_private_key(cred.private_key.encode(), password=None)
    except Exception as e:  # noqa: BLE001
        raise GatewayError("the github_app private_key is not a PEM private key (download it from the App's "
                           "settings page, 'Generate a private key')") from e
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GatewayError("the github_app private_key must be an RSA key")
    return cred


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, private_key: str, at: float | None = None) -> str:
    """RS256 JWT for the GitHub App API: issued 60s in the past for clock drift, valid 9 minutes (max 10)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    t = int(at if at is not None else time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iat": t - 60, "exp": t + 540, "iss": app_id}, separators=(",", ":")).encode())
    key = load_pem_private_key(private_key.encode(), password=None)
    sig = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    return f"{header}.{claims}.{_b64url(sig)}"


def _gh_message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("message", ""))[:200]
    except ValueError:
        return ""


async def mint_installation_token(client: httpx.AsyncClient, api_url: str, app: AppCredential, repo: str | None,
                                  permissions: dict[str, str]) -> tuple[str, float]:
    """(token, expires_at) from POST /app/installations/{id}/access_tokens, limited to `repo` when given."""
    api = api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {app_jwt(app.app_id, app.private_key)}",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    inst = app.installation_id
    if inst is None:
        if not repo:
            raise GatewayError("the github_app credential has no installation_id and there is no repo to look it up by")
        r = await client.get(f"{api}/repos/{repo}/installation", headers=headers)
        if r.status_code != 200:
            raise GatewayError(f"GitHub App is not installed on {repo} ({r.status_code} {_gh_message(r)})")
        inst = int(r.json()["id"])
    body: dict[str, Any] = {"permissions": permissions}
    if repo:
        body["repositories"] = [repo.split("/", 1)[1]]  # names, not owner/name
    r = await client.post(f"{api}/app/installations/{inst}/access_tokens", headers=headers, json=body)
    if r.status_code != 201:
        raise GatewayError(f"GitHub refused an installation token ({r.status_code} {_gh_message(r)})")
    data = r.json()
    exp = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00")).timestamp()
    return str(data["token"]), exp


class AppTokens:
    """Installation tokens per (credential value, repo), cached in memory only until shortly before they expire."""

    def __init__(self, api_url: str, client_factory):
        self.api_url = api_url
        self._client = client_factory
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = collections.defaultdict(asyncio.Lock)

    async def token(self, value: str, repo: str) -> str:
        key = (hashlib.sha256(value.encode()).hexdigest(), repo)
        hit = self._cache.get(key)
        if hit and hit[1] - time.time() > TOKEN_REFRESH_SECONDS:
            return hit[0]
        async with self._locks[key]:
            hit = self._cache.get(key)
            if hit and hit[1] - time.time() > TOKEN_REFRESH_SECONDS:
                return hit[0]
            tok, exp = await mint_installation_token(self._client(), self.api_url, parse_app_credential(value), repo,
                                                     {"contents": "write", "pull_requests": "write"})
            self._cache[key] = (tok, exp)
            return tok


# ============================================================================ the gateway
class _Server:
    """A second uvicorn listener in the control plane's event loop. The main server owns the signals."""

    def __init__(self, app, host: str, port: int):
        import uvicorn

        class Quiet(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):
                yield

        self.server = Quiet(uvicorn.Config(app, host=host, port=port, lifespan="off", log_level="warning",
                                           access_log=False))
        # No limit_concurrency: uvicorn counts idle connections against it (there is no header timeout) and then
        # answers everyone 503, so one sandbox holding sockets open would lock all the others out.
        self.task: asyncio.Task | None = None

    async def _run(self) -> None:
        try:
            await self.server.serve()
        except (SystemExit, Exception) as e:  # uvicorn exits on bind errors; keep the control plane up
            log.error("git gateway listener stopped: %s", e)

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.server.should_exit = True
        if self.task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.task, timeout=10)


# --- overnight/sandbox-security
class _AuthFailures:
    """Failed grant checks per source address in a sliding window. Grants are 256-bit, so this isn't about
    guessing one; it keeps a sandbox from turning the gateway into a busy loop over the grant table."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, collections.deque[float]] = {}

    def _recent(self, src: str) -> collections.deque[float] | None:
        q = self._hits.get(src)
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        if q is not None and not q:
            del self._hits[src]
            return None
        return q

    def blocked(self, src: str) -> bool:
        q = self._recent(src)
        return q is not None and len(q) >= self.limit

    def add(self, src: str) -> bool:
        """Record a failure; True when it is the one that blocks the source (worth one audit entry)."""
        if len(self._hits) > 10_000:  # many sources at once: forget the stalest rather than grow without bound
            for k in sorted(self._hits, key=lambda k: self._hits[k][-1] if self._hits[k] else 0)[:5_000]:
                del self._hits[k]
        q = self._recent(src)
        if q is None:
            q = self._hits[src] = collections.deque()
        q.append(time.monotonic())
        return len(q) == self.limit


class _InflightLimit:
    """ASGI wrapper around /git: at most MAX_INFLIGHT_PER_GRANT requests per grant at once, counted until the
    response has been streamed (so a relay that is still running counts)."""

    def __init__(self, app, gateway: Gateway):
        self.app, self.gateway = app, gateway

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The first Authorization header, as Request.headers (and so handle()) reads it: a later, made-up one
        # must not get a count of its own.
        token = Gateway._token_from_header(Headers(scope=scope).get("authorization", ""))
        key = hash_token(token) if token and token.startswith(PREFIX) and len(token) <= 128 else None
        n = self.gateway._inflight
        if key is not None and n.get(key, 0) >= MAX_INFLIGHT_PER_GRANT:
            await PlainTextResponse(f"hangar: more than {MAX_INFLIGHT_PER_GRANT} requests at once with this grant\n",
                                    status_code=429, headers={"Retry-After": "5"})(scope, receive, send)
            return
        if key is not None:
            n[key] = n.get(key, 0) + 1
        try:
            await self.app(scope, receive, send)
        finally:
            if key is not None:
                n[key] -= 1
                if n[key] <= 0:
                    n.pop(key, None)


class Gateway:
    def __init__(self, fleet: Fleet, transport: httpx.AsyncBaseTransport | None = None):
        self.fleet = fleet
        self.settings = fleet.settings
        self.store = fleet.store
        self.broker = fleet.broker
        self.transport = transport
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self.app_tokens = AppTokens(self.settings.github_api_url, self._http)
        self._server: _Server | None = None
        self._failures = _AuthFailures(AUTH_FAILURES, AUTH_WINDOW)  # overnight/sandbox-security
        self._inflight: dict[str, int] = {}  # token hash -> requests in flight (_InflightLimit)

    # ------------------------------------------------------------------ plumbing
    @property
    def hosts(self) -> list[str]:
        return [h.lower() for h in self.settings.gateway_upstreams]

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            t = self.settings.gateway_timeout_seconds
            self._client = httpx.AsyncClient(transport=self.transport, follow_redirects=False,
                                             timeout=httpx.Timeout(t, connect=15.0, pool=15.0))
            self._client_loop = loop
            self.app_tokens = AppTokens(self.settings.github_api_url, self._http)
        return self._client

    def asgi_app(self) -> Starlette:
        """The sandbox-facing app: /git only, no admin routes."""
        return Starlette(routes=[Route("/healthz", self.healthz, methods=["GET"]), *self.routes()],
                         middleware=[Middleware(observe.HTTPMiddleware)])  # --- observe: spans, /metrics

    def routes(self) -> list[Route]:
        return [Route("/git/{rest:path}", self.handle, methods=["GET", "POST"],
                      middleware=[Middleware(_InflightLimit, gateway=self)])]

    async def healthz(self, request: Request) -> Response:
        return JSONResponse({"ok": True, "service": "hangar-gateway"})

    async def start(self) -> None:
        s = self.settings
        if s.gateway_url and s.gateway_port:
            self._server = _Server(self.asgi_app(), s.gateway_host or s.host, s.gateway_port)
            self._server.start()

    async def stop(self) -> None:
        if self._server:
            await self._server.stop()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()

    # ------------------------------------------------------------------ grants
    def mint(self, sb: Sandbox, host: str, repos: list[str], push_refs: list[str], credential: str) -> str:
        """A new grant for the sandbox (replacing any earlier one). Returns the token; only its hash is stored."""
        self.revoke(sb.id, audit=False)
        raw = PREFIX + secrets.token_urlsafe(32)
        self.store.put(SandboxGrant(sandbox_id=sb.id, token_hash=hash_token(raw), host=host, repos=repos,
                                    push_refs=push_refs, credential=credential, expires_at=sb.expires_at))
        return raw

    def grants(self, sandbox_id: str | None = None) -> list[SandboxGrant]:
        return [g for g in self.store.list(SandboxGrant) if sandbox_id is None or g.sandbox_id == sandbox_id]

    def revoke(self, sandbox_id: str, audit: bool = True) -> None:
        gone = self.grants(sandbox_id)
        for g in gone:
            self.store.delete(SandboxGrant, g.id)
        if gone and audit:
            self.fleet.audit("gateway", "gateway.revoke", sandbox_id)

    def extend(self, sb: Sandbox) -> None:
        for g in self.grants(sb.id):
            g.expires_at = sb.expires_at
            self.store.put(g)

    def verify(self, raw: str | None) -> SandboxGrant | None:
        """The live grant for a token: unknown, expired, revoked or belonging to a sandbox that is not running
        all come back None."""
        if not raw or not raw.startswith(PREFIX) or len(raw) > 128:
            return None
        h = hash_token(raw)
        grant = next((g for g in self.store.list(SandboxGrant) if hmac.compare_digest(h, g.token_hash)), None)
        if grant is None:
            return None
        if grant.expires_at is not None and grant.expires_at < now():
            self.store.delete(SandboxGrant, grant.id)
            return None
        sb = self.store.get(Sandbox, grant.sandbox_id)
        if sb is None or sb.state not in (SandboxState.STARTING, SandboxState.RUNNING):
            return None
        return grant

    # ------------------------------------------------------------------ sandbox wiring
    def mode_for(self, spec: EnvSpec, sb: Sandbox, parent: Sandbox | None) -> str:
        if spec.credential_mode:
            return spec.credential_mode
        if parent is not None and (parent.info.get("git") or {}).get("mode") == "gateway":
            return "gateway"
        if self.settings.gateway_url and self.settings.gateway_for_jobs and "hangar.session" in sb.labels:
            return "gateway"  # sessions label their sandboxes; jobs run in sessions
        return "direct"

    async def attach(self, prov: Provider, sb: Sandbox, spec: EnvSpec, creds: list[Credential],
                     branch: str | None, parent_snapshot: str | None) -> list[Credential]:
        """Decide the sandbox's credential mode. In gateway mode: mint its grant, point its git at the gateway, and
        return the credentials minus every git secret, so none of them is written into the sandbox."""
        parent = None
        if parent_snapshot:
            snap = self.store.get(Snapshot, parent_snapshot)
            parent = self.store.get(Sandbox, snap.sandbox_id) if snap else None
        mode = self.mode_for(spec, sb, parent)
        mapped = {var: c.kind for var, name in spec.secrets.items()
                  if (c := self.store.get(Credential, name)) is not None
                  and (c.kind in GIT_KINDS if mode == "gateway" else c.kind == CredentialKind.GITHUB_APP)}
        if mapped:
            raise GatewayError(f"environment {spec.name} maps git credentials into the sandbox through secrets: "
                               f"({', '.join(mapped)}); remove them, the gateway pushes for the sandbox")
        if mode != "gateway":
            sb.info["git"] = {"mode": "direct"}
            return creds
        if not self.settings.gateway_url:
            raise GatewayError(f"environment {spec.name} uses credential_mode: gateway but HANGAR_GATEWAY_URL is not "
                               "set (docs/security.md)")
        git = [c for c in creds if c.kind in GIT_KINDS]
        if any(c.kind == CredentialKind.SSH_KEY for c in git):
            sb.info.setdefault("warnings", []).append("SSH keys are not attached in credential_mode: gateway")
        repo_url = sb.repo or (parent.repo if parent else None)
        if _has_userinfo(repo_url):
            raise GatewayError("the repo URL carries credentials; use a plain https URL with credential_mode: gateway")
        upstream = next((c.name for c in git if c.kind != CredentialKind.SSH_KEY), None) or \
            self.settings.gateway_credential
        remote = parse_remote(repo_url, self.hosts)
        push_refs = list(self.settings.gateway_push_refs)
        if branch:
            ref = f"refs/heads/{branch}"
            if not valid_ref(ref):
                raise GatewayError(f"invalid branch name '{branch}'")
            push_refs.insert(0, ref)
        token = None
        if remote:
            if not upstream:
                raise GatewayError("no git credential for the gateway: attach a git_token or github_app credential to "
                                   "the environment, or set HANGAR_GATEWAY_CREDENTIAL")
            cred = self.broker.get(upstream)
            if cred.kind not in (CredentialKind.GIT_TOKEN, CredentialKind.GITHUB_APP):
                raise GatewayError(f"credential '{upstream}' is a {cred.kind.value}; the gateway needs a git_token or "
                                   "github_app credential")
            token = self.mint(sb, remote[0], [remote[1]], push_refs, upstream)
            self.fleet.audit(sb.created_by or "system", "gateway.grant", sb.id, repo=remote[1], push_refs=push_refs,
                             credential=upstream)
        await self._configure(prov, sb, token)
        sb.info["git"] = {"mode": "gateway", "repo": remote[1] if remote else None,
                          "push_refs": push_refs if token else [], "credential": upstream if token else None}
        self.store.put(sb)
        return [c for c in creds if c.kind not in GIT_KINDS]

    async def _configure(self, prov: Provider, sb: Sandbox, token: str | None) -> None:
        """Point the sandbox's git at the gateway. The token goes into ~/.git-credentials (0600) with write_file,
        never into a command line. The helper answers `get` only, so a 401 can't make git erase the file."""
        gw = self.settings.gateway_url.rstrip("/")  # type: ignore[union-attr]
        q = shlex.quote
        lines = ["set -e"]
        for host in self.hosts:
            key = f"url.{gw}/git/{host}/.insteadOf"
            lines.append(f"git config --global --unset-all {q(key)} || true")
            for src in (f"https://{host}/", f"git@{host}:", f"ssh://git@{host}/"):
                lines.append(f"git config --global --add {q(key)} {q(src)}")
        helper = '!f() { test "$1" = get && git credential-store --file ~/.git-credentials get; }; f'
        lines += [f"git config --global --unset-all {q(f'credential.{gw}.helper')} || true",
                  f"git config --global --add {q(f'credential.{gw}.helper')} ''",
                  f"git config --global --add {q(f'credential.{gw}.helper')} {q(helper)}",
                  "git config --global user.email >/dev/null || git config --global user.email hangar@localhost",
                  "git config --global user.name >/dev/null || git config --global user.name hangar"]
        res = await prov.exec(sb, "\n".join(lines), timeout=60)
        if res.exit_code != 0:
            raise GatewayError(f"could not configure git for the gateway: {(res.stderr or res.stdout)[-300:]}")
        home = f"/home/{self.settings.sandbox_user}"
        scheme, _, rest = gw.partition("://")
        body = f"{scheme}://x-access-token:{token}@{rest.split('/', 1)[0]}\n" if token else ""
        await prov.write_file(sb, f"{home}/.git-credentials", body.encode(), mode=0o600)

    # ------------------------------------------------------------------ upstream
    async def upstream_auth(self, grant: SandboxGrant, repo: str) -> str:
        cred = self.broker.get(grant.credential)
        value = self.broker.secret_text(cred.name)
        if cred.kind == CredentialKind.GITHUB_APP:
            self._http()  # binds the client and the token cache to the running loop
            value = await self.app_tokens.token(value, repo)
        elif cred.kind != CredentialKind.GIT_TOKEN:
            raise GatewayError(f"credential '{cred.name}' can't be used upstream")
        return "Basic " + base64.b64encode(f"x-access-token:{value}".encode()).decode()

    # ------------------------------------------------------------------ smart HTTP
    @staticmethod
    def _client_token(request: Request) -> str | None:
        return Gateway._token_from_header(request.headers.get("authorization", ""))

    @staticmethod
    def _token_from_header(header: str) -> str | None:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer":
            return value.strip() or None
        if scheme.lower() == "basic":
            try:
                user, _, password = base64.b64decode(value.strip(), validate=True).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return None
            return password or user or None
        return None

    def _session_refs(self, grant: SandboxGrant) -> dict[str, str]:
        """{ref: owner} for branches of sessions (jobs run in sessions) on this repo that the pusher must not
        create or block: everyone's but those of its own session. A best-of-N winner's session takes the job
        branch before delivery, so the job's own session doesn't count against it. Unfinished jobs count too: a
        queued one has no session yet, but its branch is decided (and shown on its issue), and a job refuses to
        start on a branch that already exists."""
        from hangar.models import JOB_TERMINAL, Job

        sb = self.store.get(Sandbox, grant.sandbox_id)
        own_id = sb.labels.get("hangar.session") if sb else None
        sessions = self.store.list(Session)
        own = {s.branch for s in sessions if s.id == own_id}
        owners = [(s.branch, s.repo, f"session {s.name}") for s in sessions if s.id != own_id] + \
            [(j.branch, j.repo, f"job {j.id}") for j in self.store.list_active(Job)  # --- scale: indexed
             if j.state not in JOB_TERMINAL]
        out: dict[str, str] = {}
        for branch, repo, who in owners:
            if not branch or branch in own:
                continue
            remote = parse_remote(repo, self.hosts) if repo else None
            if repo and (remote is None or remote[1] not in grant.repos):
                continue  # another repo, or not one the gateway serves
            out.setdefault(f"refs/heads/{branch}", who)
        return out

    def _deny(self, grant: SandboxGrant, repo: str, reason: str, refs: list[Command] | None = None) -> None:
        observe.gateway_denied(reason)  # --- observe
        self.fleet.audit(f"sandbox:{grant.sandbox_id}", "gw.git.deny", grant.sandbox_id, repo=repo, reason=reason,
                         refs=[{"ref": c.ref, "old": c.old[:12], "new": c.new[:12]} for c in refs or []])

    async def handle(self, request: Request) -> Response:
        raw_path = request.scope.get("raw_path") or request.url.path.encode()
        m = _PATH.match(raw_path.decode("latin-1"))
        if not m or m["owner"] in (".", "..") or m["repo"] in (".", "..", ".git"):
            return PlainTextResponse("hangar: not found\n", status_code=404)
        token = self._client_token(request)
        # overnight/sandbox-security: git's first request carries no credentials; only presented grants count.
        src = request.client.host if request.client else "unknown"
        if token is not None and self._failures.blocked(src):
            return PlainTextResponse("hangar: too many requests with an invalid grant from this address; wait a "
                                     "minute\n", status_code=429, headers={"Retry-After": str(int(AUTH_WINDOW))})
        grant = self.verify(token)
        if grant is None:
            if token is not None and self._failures.add(src):
                log.warning("gateway: %s sent %d invalid grants in %ds; refusing it for now", src, AUTH_FAILURES,
                            int(AUTH_WINDOW))
                self.fleet.audit("gateway", "gw.auth.blocked", None, source=src)
            return PlainTextResponse("hangar: a valid sandbox grant is required\n", status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="hangar git gateway"'})
        host, svc = m["host"].lower(), m["svc"]
        repo = f"{m['owner']}/{m['repo'].removesuffix('.git')}"
        if host != grant.host or host not in self.hosts or repo.lower() not in grant.repos:
            self._deny(grant, f"{host}/{repo}", "repo not granted")
            return PlainTextResponse(f"hangar: repository {repo} not found\n", status_code=404)
        if svc == "info/refs":
            service = request.query_params.get("service")
            if request.method != "GET" or service not in SERVICES:
                return PlainTextResponse("hangar: only git's smart HTTP protocol is supported\n", status_code=403)
        elif request.method != "POST":
            return PlainTextResponse("hangar: method not allowed\n", status_code=405)
        try:
            auth = await self.upstream_auth(grant, repo.lower())
        except Exception as e:  # noqa: BLE001 - shown to git, never includes a secret
            log.warning("gateway: no upstream credential for %s: %s", grant.sandbox_id, e)
            return PlainTextResponse(f"hangar: the gateway could not get a credential for {repo}: "
                                     f"{e if isinstance(e, GatewayError) else type(e).__name__}\n", status_code=502)
        base = self.settings.gateway_upstreams.get(host) or next(
            v for k, v in self.settings.gateway_upstreams.items() if k.lower() == host)
        url = f"{base.rstrip('/')}/{repo}.git/{svc}"
        headers = {"Authorization": auth, "User-Agent": request.headers.get("user-agent", "hangar-gateway")[:200]}
        for h in ("content-type", "accept", "git-protocol"):
            if h in request.headers:
                headers[h] = request.headers[h][:1000]
        if svc == "info/refs":
            return await self._forward(request, "GET", url, headers, params={"service": service}, grant=grant,
                                       repo=repo)
        if svc == "git-upload-pack":
            for h in ("content-encoding", "accept-encoding", "content-length"):
                if h in request.headers:
                    headers[h] = request.headers[h][:200]
            return await self._forward(request, "POST", url, headers, body=self._passthrough(request),
                                       grant=grant, repo=repo)
        return await self._receive_pack(request, url, headers, grant, repo)

    def _cap(self) -> int:
        return self.settings.gateway_max_push_mb * 1024 * 1024

    async def _passthrough(self, request: Request) -> AsyncIterator[bytes]:
        """A fetch request body (wants and haves): small, but still capped, and a stalled one doesn't hold its
        upstream connection forever."""
        total, it = 0, request.stream().__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(it.__anext__(), timeout=self.settings.gateway_timeout_seconds)
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise GatewayError("timed out reading the fetch request") from None
            total += len(chunk)
            if total > MAX_FETCH_REQUEST:
                raise GatewayError("fetch request too large")
            yield chunk

    async def _receive_pack(self, request: Request, url: str, headers: dict[str, str], grant: SandboxGrant,
                            repo: str) -> Response:
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return PlainTextResponse("hangar: compressed push requests are not supported\n", status_code=415)
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isdigit():
                return PlainTextResponse("hangar: bad Content-Length\n", status_code=400)
            if int(length) > self._cap():
                self._deny(grant, repo, "push too large")
                return PlainTextResponse(f"hangar: push is larger than the gateway's limit "
                                         f"({self.settings.gateway_max_push_mb} MB)\n", status_code=413)
            headers["content-length"] = length
        it = request.stream().__aiter__()
        timeout = self.settings.gateway_timeout_seconds
        # overnight/sandbox-security: one deadline for the whole command list (git sends it at once), not one per
        # chunk, which a trickle of bytes could renew forever; and one pass over the bytes (CommandScanner).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        scan = CommandScanner()
        try:
            parsed = scan.feed(b"")
            while parsed is None:
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), timeout=max(0.0, deadline - loop.time()))
                except StopAsyncIteration:
                    raise ProtocolError("the push request ended before its command list") from None
                parsed = scan.feed(chunk)
        except ProtocolError as e:
            self._deny(grant, repo, str(e))
            return PlainTextResponse(f"hangar: {e}\n", status_code=400)
        except (TimeoutError, ClientDisconnect):
            return PlainTextResponse("hangar: timed out reading the push\n", status_code=408)
        buf = bytes(scan.buf)
        cmds, end = parsed
        if not cmds.commands:  # git's probe before a chunked upload: forward the flush and nothing after it
            headers["content-length"] = str(end)

            async def probe() -> AsyncIterator[bytes]:
                yield buf[:end]

            return await self._forward(request, "POST", url, headers, body=probe(), grant=grant, repo=repo)
        owned = {r for g in self.grants() if g.sandbox_id != grant.sandbox_id and set(g.repos) & set(grant.repos)
                 for r in g.push_refs if not r.endswith("/**")}
        creates = any(c.ref not in grant.push_refs for c in cmds.commands)
        bad = check_push(grant, cmds, self.settings.gateway_allow_tags, owned,
                         self._session_refs(grant) if creates else None)
        if bad:
            self._deny(grant, repo, "; ".join(sorted(set(bad.values()))), cmds.commands)
            with contextlib.suppress(Exception):  # drain the pack so git reads our report, not a broken pipe
                await asyncio.wait_for(self._drain(it, len(buf)), timeout=timeout)
            return Response(rejection(cmds, bad), media_type="application/x-git-receive-pack-result",
                            headers={"Cache-Control": "no-cache"})
        cap = self._cap()

        async def body() -> AsyncIterator[bytes]:
            total = len(buf)
            yield buf
            async for chunk in it:
                total += len(chunk)
                if total > cap:
                    raise GatewayError("push is larger than the gateway's limit")
                yield chunk

        headers["accept-encoding"] = "identity"  # so the report can be read for the audit log
        sideband = has_cap(cmds.capabilities, "side-band-64k") or has_cap(cmds.capabilities, "side-band")
        return await self._forward(request, "POST", url, headers, body=body(), grant=grant, repo=repo,
                                   push=(cmds, sideband))

    @staticmethod
    async def _drain(it, seen: int) -> None:
        cap = 1 << 34  # bounded by the timeout; just don't loop forever
        async for chunk in it:
            seen += len(chunk)
            if seen > cap:
                return

    async def _forward(self, request: Request, method: str, url: str, headers: dict[str, str], *,
                       grant: SandboxGrant, repo: str, params: dict[str, str] | None = None,
                       body: AsyncIterator[bytes] | None = None,
                       push: tuple[Commands, bool] | None = None) -> Response:
        client = self._http()
        req = client.build_request(method, url, headers=headers, params=params, content=body)
        try:
            resp = await client.send(req, stream=True)
        except (httpx.HTTPError, GatewayError, ClientDisconnect) as e:
            log.warning("gateway: upstream %s %s failed: %s", method, url.split("?")[0], type(e).__name__)
            if push:
                self._audit_push(grant, repo, push[0], 502, [f"upstream error: {type(e).__name__}"], 0)
            return PlainTextResponse(f"hangar: upstream request failed ({type(e).__name__})\n", status_code=502)
        if resp.status_code == 401:
            await resp.aclose()
            if push:
                self._audit_push(grant, repo, push[0], 401, ["upstream refused the credential"], 0)
            return PlainTextResponse("hangar: GitHub refused the gateway's credential for this repo\n",
                                     status_code=502)
        out_headers = {k: v for k, v in resp.headers.items()
                       if k.lower() in ("content-type", "cache-control", "expires", "pragma", "content-encoding")}

        async def relay() -> AsyncIterator[bytes]:
            seen, size = bytearray(), 0
            try:
                async for chunk in resp.aiter_raw():
                    size += len(chunk)
                    if push and len(seen) < MAX_COMMAND_BYTES:
                        seen += chunk[:MAX_COMMAND_BYTES - len(seen)]
                    yield chunk
            finally:
                await resp.aclose()
                if push:
                    self._audit_push(grant, repo, push[0], resp.status_code, parse_report(bytes(seen), push[1]),
                                     size)

        return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers)

    def _audit_push(self, grant: SandboxGrant, repo: str, cmds: Commands, status: int, report: list[str],
                    size: int) -> None:
        ok = status == 200 and bool(report) and report[0] == "unpack ok" and \
            all(not x.startswith("ng ") for x in report)
        self._record_created(grant, [x[3:] for x in report if status == 200 and x.startswith("ok ")])
        observe.gateway_push("ok" if ok or (status == 200 and not cmds.commands) else "failed",  # --- observe
                             **{"hangar.sandbox.id": grant.sandbox_id, "hangar.gateway.upstream_status": status})
        self.fleet.audit(f"sandbox:{grant.sandbox_id}", "gw.git.push", grant.sandbox_id, repo=repo,
                         result="ok" if ok or (status == 200 and not cmds.commands) else "failed", status=status,
                         refs=[{"ref": c.ref, "old": c.old[:12], "new": c.new[:12]} for c in cmds.commands],
                         report=report[:20], response_bytes=size)

    def _record_created(self, grant: SandboxGrant, refs: list[str]) -> None:
        """Branches a sandbox created through a pattern become its own: it may update them, others may not."""
        new = [r for r in refs if r not in grant.push_refs and ref_allowed(grant.push_refs, r) and valid_ref(r)]
        cur = self.store.get(SandboxGrant, grant.id) if new else None
        if cur is None:
            return
        cur.push_refs += [r for r in new if r not in cur.push_refs][:max(0, 100 - len(cur.push_refs))]
        self.store.put(cur)

    # ------------------------------------------------------------------ doctor
    async def doctor(self, transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
        from hangar.doctor import _check

        s, out = self.settings, []
        wants = []
        for name in self.fleet.catalog.names():
            with contextlib.suppress(Exception):
                if self.fleet.catalog.get(name).credential_mode == "gateway":
                    wants.append(name)
        if not s.gateway_url:
            if wants:
                out.append(_check("git gateway", False, f"environments {', '.join(wants)} use credential_mode: gateway "
                                  "but HANGAR_GATEWAY_URL is not set", "set HANGAR_GATEWAY_URL (docs/security.md)"))
            return out
        async with httpx.AsyncClient(transport=transport, timeout=5) as c:
            try:
                r = await c.get(f"{s.gateway_url.rstrip('/')}/healthz")
                ok = r.status_code == 200 and r.json().get("service") == "hangar-gateway"
                out.append(_check("git gateway", ok or None, f"{s.gateway_url} answers" if ok else
                                  f"{s.gateway_url} answered {r.status_code}, not the gateway",
                                  "check HANGAR_GATEWAY_URL, HANGAR_GATEWAY_PORT and the hangar-gateway Service"))
            except (httpx.HTTPError, ValueError) as e:
                out.append(_check("git gateway", None, f"the control plane can't reach {s.gateway_url} "
                                  f"({type(e).__name__}); sandboxes may still",
                                  "from inside a sandbox: curl $HANGAR_GATEWAY_URL/healthz"))
            plain = [h for h, base in s.gateway_upstreams.items() if not base.startswith("https://")]
            if plain:
                out.append(_check("gateway upstreams", None, f"not https: {', '.join(plain)}; the real credential "
                                  "would cross the network in clear text", "use https:// in HANGAR_GATEWAY_UPSTREAMS"))
            if s.gateway_credential and self.store.get(Credential, s.gateway_credential) is None:
                out.append(_check("gateway credential", False, f"credential '{s.gateway_credential}' is missing",
                                  f"hangar cred add {s.gateway_credential} --harness git --kind git_token --file ..."))
            repo = next((r for r in s.github_repos), None)
            for cred in self.store.list(Credential):
                if cred.kind != CredentialKind.GITHUB_APP:
                    continue
                try:
                    app = parse_app_credential(self.broker.secret_text(cred.name))
                    if app.installation_id is None and not repo:
                        out.append(_check(f"github app {cred.name}", None, "no installation_id and no repo in "
                                          "HANGAR_GITHUB_REPOS to look one up; not checked"))
                        continue
                    await mint_installation_token(c, s.github_api_url, app, None if app.installation_id else repo,
                                                  {"metadata": "read"})
                    out.append(_check(f"github app {cred.name}", True, "installation token minted"))
                except Exception as e:  # noqa: BLE001
                    out.append(_check(f"github app {cred.name}", False, f"can't mint a token: {e}",
                                      "check app_id, installation_id and the private key (docs/security.md)"))
        return out
