"""Preview ports: a sandbox's declared `ports`, opened by its owner through hangar's authenticated proxy (--- envbuilds;
docs/environments.md#preview-ports).

Each port of each sandbox is an origin of its own, `<scheme>://<port>-<sandbox id>.<HANGAR_PREVIEW_DOMAIN>`, so what
an agent's dev server serves (HTML and scripts it wrote) never runs on hangar's own origin, where the dashboard's
token lives, nor on another sandbox's. Nothing is public:

* **Deny by default.** Only ports the sandbox declared (`ports:` in its environment or its repo's file) are reachable;
  hangar-envd's port never is. The sandbox must be running and still its owner's.
* **Owner-scoped (#88).** `GET /api/sandboxes/{ref}/previews` (the owner's `user` token, or an admin's) mints a
  two-minute link per port. Opening it sets a cookie for that origin only (`__Host-` on https), HMAC-signed and bound to
  the sandbox, the port and the person, valid HANGAR_PREVIEW_SESSION_HOURS. Every request checks it again, with the
  sandbox's owner as it is then. A hangar bearer token works too (`curl -H "Authorization: Bearer ..."`).
* **Nothing of hangar's reaches the app.** The proxy drops the Authorization header and its own cookie before
  forwarding, and reaches the port at the address the sandbox's hangar-envd proved (sandbox_router, as for E2B's
  sandbox URLs): never loopback, link-local or another sandbox's.

Needs hangar-envd (Docker and Kubernetes sandboxes) and wildcard DNS for the domain pointing at hangar (on a laptop,
`HANGAR_PREVIEW_DOMAIN=localhost`: browsers resolve `*.localhost` to 127.0.0.1).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import Counter
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from hangar import observe, people
from hangar.fleet import ACTIVE
from hangar.models import Sandbox, SandboxState
from hangar.sandbox_router import _SID, Refused, SandboxRouter, _gone, _Label, _send_json

if TYPE_CHECKING:
    from hangar.app import Hangar

HANDSHAKE = "/__hangar/preview"
TICKET_SECONDS = 120
COOKIE = "hangar_preview"


class Previews:
    """Links, tickets and sessions for preview ports."""

    def __init__(self, h: Hangar):
        self.h, self.settings, self.fleet, self.store = h, h.settings, h.fleet, h.store
        self._used: dict[str, float] = {}  # --- review: tickets already opened -> when they expire

    @property
    def domain(self) -> str | None:
        return (self.settings.preview_domain or "").strip(".").lower() or None

    def _mac(self, data: str, key: bytes | None = None) -> str:
        from hangar import keys  # --- durable: the newest vault key signs; any not yet retired checks (a rotation)

        return hmac.new(key or keys.key_for(self.settings), b"hangar-preview\0" + data.encode(),
                        hashlib.sha256).hexdigest()

    def token(self, kind: str, sid: str, port: int, person: str | None, seconds: float) -> str:
        nonce = secrets.token_urlsafe(9)  # --- review: no two tickets alike, so each one can be spent once
        data = f"{kind}|{sid}|{port}|{person or ''}|{int(time.time() + seconds)}|{nonce}"
        return base64.urlsafe_b64encode(data.encode()).decode().rstrip("=") + "." + self._mac(data)

    def check(self, token: str, kind: str, sid: str, port: int) -> str | None:
        """The person a valid token of `kind` for this sandbox and port belongs to ('' for an admin token without a
        person), or None."""
        raw, _, mac = token.partition(".")
        try:
            data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        from hangar import keys

        if not any(hmac.compare_digest(mac.encode(), self._mac(data, k).encode())
                   for k in keys.verify_keys(self.settings)):
            return None
        parts = data.split("|")
        if len(parts) != 6 or parts[0] != kind or parts[1] != sid or parts[2] != str(port):
            return None
        if not parts[4].isdigit() or int(parts[4]) < time.time():
            return None
        return parts[3]

    def spend(self, ticket: str) -> bool:
        """A valid ticket, opened for the first time: False when it was opened before. A ticket rides in the link's
        URL (browser history, a proxy's access log), so it signs a browser in once. One control plane holds the
        state; a restart forgets it, and the ticket's two minutes bound that."""
        t = time.time()
        for k in [k for k, exp in self._used.items() if exp < t]:
            del self._used[k]
        mac = ticket.rpartition(".")[2]
        if mac in self._used:
            return False
        self._used[mac] = t + TICKET_SECONDS + 5
        return True

    @staticmethod
    def ports(sb: Sandbox) -> list[dict[str, Any]]:
        return [s for s in sb.info.get("services") or [] if s.get("kind") == "port" and s.get("port")]

    def allowed(self, person: str | None, sb: Sandbox) -> bool:
        """Still the sandbox's owner, or an admin ('' is the bootstrap admin token)."""
        if person == "":
            return True
        p = people.get(self.store, person)
        return p is not None and (p.role == "admin" or (sb.person is not None and sb.person == p.id))

    def origin(self, sb: Sandbox, port: int) -> str:
        u = urlsplit(self.settings.public_url)
        return f"{u.scheme}://{port}-{sb.id}.{self.domain}" + (f":{u.port}" if u.port else "")

    def links(self, sb: Sandbox, person: str | None) -> list[dict[str, Any]]:
        if not self.domain:
            raise ValueError("previews are off: set HANGAR_PREVIEW_DOMAIN (a wildcard DNS name pointing at hangar; "
                             "`localhost` on a laptop)")
        out = []
        for p in self.ports(sb):
            t = self.token("t", sb.id, p["port"], person, TICKET_SECONDS)
            out.append({"port": p["port"], "name": p.get("name"), "origin": self.origin(sb, p["port"]),
                        "url": f"{self.origin(sb, p['port'])}{HANDSHAKE}?t={t}", "expires_in": TICKET_SECONDS})
        return out


class PreviewRouter(SandboxRouter):
    """ASGI middleware: `<port>-<sandbox>.<HANGAR_PREVIEW_DOMAIN>` is a sandbox's preview; anything else goes on."""

    def __init__(self, app, h: Hangar):  # noqa: D107 - SandboxRouter's own needs an E2BService
        self.app, self.h = app, h
        self.previews = Previews(h)
        self.svc = _Svc(h)
        self.domain = self.previews.domain
        self._inflight: Counter[str] = Counter()
        self._addr = {}
        self._observed = observe.HTTPMiddleware(self._sandbox)
        self.secure = urlsplit(h.settings.public_url).scheme == "https"
        self.cookie = ("__Host-" if self.secure else "") + COOKIE

    def _route(self, scope) -> tuple[str, Any] | None:
        if not self.domain:
            return None
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1").lower()
        host = host.rsplit(":", 1)[0] if host and not host.endswith("]") else host
        if not host.endswith("." + self.domain):
            return None
        label = host[: -len(self.domain) - 1]
        port, _, sid = label.partition("-")
        if "." in label or not port.isdigit() or not _SID.match(sid):
            return "bad", Refused(404, {"message": "no such preview"})
        return "sb", (sid, port, scope.get("raw_path") or scope["path"].encode())

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        route = self._route(scope)
        if route is None:
            return await self.app(scope, receive, send)
        if route[0] == "sb" and scope["type"] == "http" and scope["path"] == HANDSHAKE:
            return await self._handshake(scope, send, *route[1][:2])
        await self._observed(dict(scope, route=_Label("/preview"), **{"hangar.e2b": route}), receive, send)

    async def _handshake(self, scope, send, sid: str, port_s: str) -> None:
        """A link from `GET /api/sandboxes/{ref}/previews`: its ticket for a session cookie on this origin."""
        t = (parse_qs(scope.get("query_string", b"").decode("latin-1")).get("t") or [""])[0]
        sb = self.svc.store.get(Sandbox, sid)
        person = self.previews.check(t, "t", sid, int(port_s)) if port_s.isdigit() else None
        if person is None or sb is None or not self.previews.allowed(person, sb) or not self.previews.spend(t):
            self._refused(scope, sid, "preview.denied")
            return await _send_json(send, 401, {"message": "this preview link expired or isn't yours; open the "
                                                           "preview from hangar again"})
        hours = self.h.settings.preview_session_hours
        session = self.previews.token("s", sid, int(port_s), person, hours * 3600)
        attrs = f"Path=/; HttpOnly; SameSite=Lax; Max-Age={int(hours * 3600)}" + ("; Secure" if self.secure else "")
        await send({"type": "http.response.start", "status": 303, "headers": [
            (b"location", b"/"), (b"set-cookie", f"{self.cookie}={session}; {attrs}".encode()),
            (b"cache-control", b"no-store"), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    def _authorize(self, scope, sid: str, port: int, raw_path: bytes) -> tuple[Sandbox, dict[str, Any], int, bool]:
        """Deny by default: the sandbox's owner's cookie or bearer token first (so nobody else learns whether a sandbox
        or a port exists), then a live sandbox and a port it declared. The credentials are then taken out of the
        request, so the app never sees them."""
        sb = self.svc.store.get(Sandbox, sid)
        person = self._person(scope, sid, port)
        if sb is None or person is None or not self.previews.allowed(person, sb):
            self._refused(scope, sid, "preview.denied")
            raise Refused(401, {"message": "open this preview from hangar (`hangar sb preview`, or the dashboard): "
                                           "previews are for the sandbox's owner"})
        if sb.state not in ACTIVE:
            self._addr.pop(sid, None)
            raise _gone(sid, "no such preview")
        if not any(p["port"] == port for p in self.previews.ports(sb)):
            self._refused(scope, sid, "preview.port.denied")
            raise Refused(404, {"message": f"port {port} isn't one of this sandbox's preview ports"})
        if sb.state != SandboxState.RUNNING:
            raise _gone(sid, f"the sandbox is {sb.state.value}")
        scope["headers"] = self._strip(scope.get("headers", []))
        return sb, {}, port, False

    def _person(self, scope, sid: str, port: int) -> str | None:
        for k, v in scope.get("headers", []):
            name = k.lower()
            if name == b"authorization":
                kind, _, raw = v.decode("latin-1").partition(" ")
                p = self.h.auth.principal(raw.strip()) if kind.lower() == "bearer" else None
                if p is not None and (p.admin or "user" in p.scopes):  # a person's own token, as for the REST API
                    return p.person or ("" if p.admin else None)
            elif name == b"cookie":
                for part in v.decode("latin-1").split(";"):
                    n, _, val = part.strip().partition("=")
                    if n == self.cookie and (who := self.previews.check(val, "s", sid, port)) is not None:
                        return who
        return None

    def _strip(self, headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        out = []
        for k, v in headers:
            name = k.lower()
            if name == b"authorization":
                continue
            if name == b"cookie":
                kept = [p for p in v.decode("latin-1").split(";")
                        if p.strip().partition("=")[0] not in (self.cookie, COOKIE, "__Host-" + COOKIE)]
                if not kept:
                    continue
                v = ";".join(kept).strip().encode("latin-1")
            out.append((k, v))
        return out

    def _refused(self, scope, sid: str, action: str) -> None:
        client = (scope.get("client") or ("?", 0))[0]
        try:
            self.h.fleet.audit(f"ip:{client}", action, sid, path=scope.get("path", "")[:200])
        except Exception:  # noqa: BLE001,S110 - auditing never breaks the answer
            pass


class _Svc:
    """What SandboxRouter's proxying needs from its service (E2BService has it for E2B's URLs)."""

    def __init__(self, h: Hangar):
        self.h, self.settings, self.fleet, self.store = h, h.settings, h.fleet, h.store
        self._ports_http = None

    @property
    def ports_http(self):
        import httpx

        if self._ports_http is None:
            self._ports_http = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(None, connect=5.0),
                                                 limits=httpx.Limits(max_connections=512, max_keepalive_connections=32))
        return self._ports_http


def mount(app: Any, h: Hangar, member: Any) -> None:
    """GET /api/sandboxes/{ref}/previews: two-minute links to the sandbox's preview ports, for its owner (or an
    admin). The links are the only way in from a browser; each opening is audited."""
    from fastapi import HTTPException

    from hangar import identity
    from hangar.fleet import FleetError

    previews = Previews(h)

    @app.get("/api/sandboxes/{ref}/previews")
    async def sandbox_previews(ref: str, who: str = member):
        try:
            sb = h.fleet.get(ref)
        except FleetError as e:
            raise HTTPException(404, f"no sandbox '{ref}'") from e
        if not people.visible(sb.person):
            raise HTTPException(404, f"no sandbox '{ref}'")
        p = identity.principal()
        person = (p.person or ("" if p.admin else None)) if p is not None else ""
        try:
            links = previews.links(sb, person)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        h.fleet.audit(who, "preview.link", sb.id, ports=[x["port"] for x in links])
        return {"sandbox": sb.id, "previews": links}
