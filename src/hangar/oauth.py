"""OAuth 2.1 authorization server for MCP clients that can't use static bearer tokens (ChatGPT
connectors, claude.ai custom connectors, MCP Inspector).

Flow (MCP authorization spec): the client hits /mcp, gets 401 with `resource_metadata`, discovers
/.well-known/oauth-protected-resource/mcp and /.well-known/oauth-authorization-server, registers
itself (RFC 7591), and sends the user to /authorize with PKCE. Hangar has no user accounts, so
the consent page asks for a hangar API token: approving a client means vouching for it with a
token you hold. The client then receives short-lived access tokens (+ refresh tokens) that act
with the approving token's identity, and can be revoked from the dashboard.

Only SHA-256 hashes of codes/tokens are stored. Static `hgr_` tokens keep working everywhere.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from hangar.auth import TokenAuth
from hangar.store import Store

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
REQUEST_TTL = 600
SCOPE = "hangar"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
log = logging.getLogger("hangar.oauth")
# --- review of front-door-auth: never a redirect target, whatever host they name
_BROWSER_SCHEMES = {"javascript", "data", "file", "vbscript", "blob", "about", "filesystem", "ftp", "ws", "wss"}


def _private_use(scheme: str) -> bool:
    """A reverse-domain private-use scheme (RFC 8252 §7.1: com.example.app), which names no host. Review of
    overnight/sweep: a "scheme://host" entry can't express it, so such an app could not be allowed at all."""
    return "." in scheme and scheme not in _BROWSER_SCHEMES


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class HangarOAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider on the hangar store."""

    def __init__(self, store: Store, auth: TokenAuth, public_url: str, redirect_hosts: list[str] | None = None,
                 max_clients: int = 200, grant_days: int = 90):
        self.store = store
        self.auth = auth
        self.public_url = public_url.rstrip("/")
        # front-door-auth: an empty list allows no host (it used to mean "any"); "*" must be asked for.
        self.redirect_hosts = ["*"] if redirect_hosts is None else redirect_hosts
        self.max_clients = max_clients
        self.grant_seconds = grant_days * 86400

    # ------------------------------------------------------------------ storage helpers
    def _put(self, kind: str, key: str, value: dict[str, Any]) -> None:
        self.store.kv_set_json(f"oauth:{kind}:{key}", value)

    def _get(self, kind: str, key: str) -> dict[str, Any] | None:
        v = self.store.kv_get_json(f"oauth:{kind}:{key}")
        if v and v.get("expires_at") and v["expires_at"] < time.time():
            self._del(kind, key)
            return None
        return v

    def _del(self, kind: str, key: str) -> None:
        self.store.kv_delete(f"oauth:{kind}:{key}")

    # ------------------------------------------------------------------ clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        v = self.store.kv_get_json(f"oauth:client:{client_id}")
        return OAuthClientInformationFull.model_validate(v) if v else None

    def _host_allowed(self, uri: str) -> bool:
        u = urlparse(uri)
        host = (u.hostname or "").lower()
        # front-door-auth: the code travels in this URL, so it must be https, except to the user's own machine
        # (native clients such as Claude Code and Codex listen on a loopback port), or a native app's own scheme
        # (RFC 8252 §7.1: Cursor registers cursor://anysphere.cursor-mcp/...), which the OS hands to that app.
        # Schemes a browser acts on itself are never allowed.
        scheme = u.scheme.lower()
        if not host and _private_use(scheme):
            # review(sweep): RFC 8252 §7.1's own form, a reverse-domain scheme with no host
            # (com.example.app:/oauth2redirect), allowed by an exact "scheme:" entry
            return "*" in self.redirect_hosts or f"{scheme}:" in {e.lower() for e in self.redirect_hosts}
        if not host or scheme in _BROWSER_SCHEMES or (scheme == "http" and host not in _LOOPBACK):
            return False
        if "*" in self.redirect_hosts:
            return True
        # --- overnight/sweep: a bare host covers https (and loopback http) and its subdomains. Any local app can
        # claim a custom scheme, so one is allowed only by an exact "scheme://host" entry: a bare "claude.ai" no
        # longer lets an app register evil://claude.ai, whose host the consent page would show.
        if f"{scheme}://{host}" in {e.lower().rstrip("/") for e in self.redirect_hosts if "://" in e}:
            return True
        return scheme in ("http", "https") and any(host == h or host.endswith("." + h)
                                                   for h in self.redirect_hosts if "://" not in h)

    @staticmethod
    def _entry_for(uri: str) -> str | None:
        """The HANGAR_OAUTH_REDIRECT_HOSTS entry that would allow `uri`, or None when nothing can."""
        u = urlparse(uri)
        host, scheme = (u.hostname or "").lower(), u.scheme.lower()
        if not host and _private_use(scheme):
            return f"{scheme}:"  # review(sweep): RFC 8252 §7.1 form
        if not host or scheme in _BROWSER_SCHEMES or (scheme == "http" and host not in _LOOPBACK):
            return None
        return host if scheme in ("http", "https") else f"{scheme}://{host}"

    def _audience_ok(self, resource: str | None) -> bool:
        """RFC 8707: a client may only ask for a token for hangar's own MCP endpoint (or hangar itself)."""
        if not resource:
            return True
        # --- #140: the sandbox server shares the authorization server, so a grant for either resource works on both
        return resource.rstrip("/") in (self.public_url, f"{self.public_url}/mcp", f"{self.public_url}/mcp/sandbox")

    def sweep(self) -> None:
        """Drop expired requests/codes/tokens and never-approved clients older than a day."""
        now_ = time.time()
        for prefix in ("oauth:req:", "oauth:code:", "oauth:at:", "oauth:rt:"):
            for key in self.store.kv_keys(prefix):
                v = self.store.kv_get_json(key)
                if not v or (v.get("expires_at") and v["expires_at"] < now_):
                    self.store.kv_delete(key)
        for key in self.store.kv_keys("oauth:client:"):
            cid = key.removeprefix("oauth:client:")
            raw = self.store.kv_get_json(key) or {}
            if not self.store.kv_get_json(f"oauth:clientmeta:{cid}") and \
                    raw.get("client_id_issued_at", now_) < now_ - 86400:
                self.store.kv_delete(key)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = list(client_info.redirect_uris or [])
        ok = [u for u in uris if self._host_allowed(str(u))]
        if uris and not ok:
            entry = self._entry_for(str(uris[0]))
            log.warning("OAuth registration refused: redirect URI %s is not allowed%s", uris[0],
                        f"; to allow it: hangar config set HANGAR_OAUTH_REDIRECT_HOSTS "
                        f"'{json.dumps([*self.redirect_hosts, entry])}' and restart hangar" if entry else "")
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description=f"redirect URI {uris[0]} is not allowed by this hangar: "
                + (f"its admin can add \"{entry}\" to HANGAR_OAUTH_REDIRECT_HOSTS" if entry else
                   "use https, http to localhost, or an app's own scheme"))
        if len(ok) < len(uris):
            # --- overnight/sweep: Cursor registers its app scheme, a cursor.com callback and a loopback port in one
            # request. Register the allowed ones instead of refusing the client (RFC 7591 §3.2.1 lets the server
            # replace requested metadata; the response says what was registered). /authorize checks the rest.
            client_info.redirect_uris = ok
        self.sweep()
        if len(self.store.kv_keys("oauth:client:")) >= self.max_clients:
            raise RegistrationError(error="invalid_client_metadata", error_description="too many registered clients")
        self.store.kv_set(f"oauth:client:{client_info.client_id}", client_info.model_dump_json())

    # ------------------------------------------------------------------ authorize -> consent page
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self._audience_ok(params.resource):  # front-door-auth
            raise AuthorizeError(error="invalid_request",
                                 error_description=f"this server only issues tokens for {self.public_url}/mcp")
        req = secrets.token_urlsafe(24)
        self._put("req", req, {
            "client_id": client.client_id,
            "params": json.loads(params.model_dump_json()),
            "expires_at": time.time() + REQUEST_TTL,
        })
        return f"{self.public_url}/oauth/consent?req={req}"

    def pending(self, req: str) -> tuple[dict[str, Any], OAuthClientInformationFull] | None:
        v = self._get("req", req)
        if not v:
            return None
        raw = self.store.kv_get_json(f"oauth:client:{v['client_id']}")
        return (v, OAuthClientInformationFull.model_validate(raw)) if raw else None

    def approve(self, req: str, actor: str, token_id: str | None = None) -> str:
        """Issue an authorization code for a pending request; returns the client redirect URL."""
        found = self.pending(req)
        if not found:
            raise AuthorizeError(error="invalid_request", error_description="authorization request expired")
        v, client = found
        self._del("req", req)
        p = AuthorizationParams.model_validate(v["params"])
        code = secrets.token_urlsafe(32)
        self._put("code", _h(code), json.loads(AuthorizationCode(
            code=_h(code), scopes=p.scopes or [SCOPE], expires_at=time.time() + CODE_TTL,
            client_id=client.client_id, code_challenge=p.code_challenge, redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly, resource=p.resource,
            subject=actor,
        ).model_dump_json()) | {"approver_id": token_id, "grant_expires_at": time.time() + self.grant_seconds})
        from hangar.models import AuditEvent

        self.store.audit(AuditEvent(actor=f"ui:{actor}", action="oauth.approve", target=client.client_id,
                                    detail={"client_name": client.client_name or client.client_id,
                                            "redirect_host": urlparse(str(p.redirect_uri)).netloc}))
        return construct_redirect_uri(str(p.redirect_uri), code=code, state=p.state)

    def deny(self, req: str) -> str | None:
        found = self.pending(req)
        if not found:
            return None
        v, _ = found
        self._del("req", req)
        p = AuthorizationParams.model_validate(v["params"])
        return construct_redirect_uri(str(p.redirect_uri), error="access_denied", state=p.state)

    # ------------------------------------------------------------------ codes & tokens
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        v = self._get("code", _h(authorization_code))
        if not v or v["client_id"] != client.client_id or not self.auth.exists(v.get("approver_id")):
            return None
        return AuthorizationCode.model_validate(v)

    def _issue(self, client_id: str, scopes: list[str], subject: str | None, resource: str | None,
               approver_id: str | None, grant_expires_at: float, family: str | None = None) -> OAuthToken:
        access, refresh = "hoa_" + secrets.token_urlsafe(32), "hor_" + secrets.token_urlsafe(32)
        now_ = int(time.time())
        family = family or secrets.token_hex(8)
        base = {"client_id": client_id, "scopes": scopes, "subject": subject, "resource": resource,
                "approver_id": approver_id, "grant_expires_at": grant_expires_at, "family": family}
        self._put("at", _h(access), {**base, "token": _h(access), "expires_at": min(now_ + ACCESS_TTL, grant_expires_at)})
        self._put("rt", _h(refresh), {**base, "token": _h(refresh), "expires_at": min(now_ + REFRESH_TTL, grant_expires_at)})
        grants = self.store.kv_get_json(f"oauth:grants:{client_id}", []) or []
        grants = [g for g in grants if g.get("expires_at", 0) > now_][-20:]
        grants += [{"kind": "at", "key": _h(access), "expires_at": now_ + ACCESS_TTL, "family": family},
                   {"kind": "rt", "key": _h(refresh), "expires_at": now_ + REFRESH_TTL, "family": family}]
        self.store.kv_set_json(f"oauth:grants:{client_id}", grants)
        self.store.kv_set_json(f"oauth:clientmeta:{client_id}", {"subject": subject, "last_issued": now_})
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if self._get("code", authorization_code.code) is None:
            raise TokenError(error="invalid_grant", error_description="authorization code already used")
        raw = self._get("code", authorization_code.code) or {}
        self._del("code", authorization_code.code)  # single use
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject,
                           authorization_code.resource, raw.get("approver_id"),
                           raw.get("grant_expires_at") or time.time() + self.grant_seconds)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        key = _h(refresh_token)
        v = self._get("rt", key)
        if not v:
            used = self.store.kv_get_json(f"oauth:used-rt:{key}")
            if used:  # a rotated-out refresh token came back: assume theft, kill the whole grant
                self._revoke_family(used["client_id"], used["family"])
            return None
        if v["client_id"] != client.client_id or not self.auth.exists(v.get("approver_id")):
            return None
        return RefreshToken.model_validate(v)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        raw = self._get("rt", refresh_token.token)
        if raw is None:  # concurrent refresh already consumed it
            raise TokenError(error="invalid_grant", error_description="refresh token already used")
        self._del("rt", refresh_token.token)  # rotate
        self.store.kv_set_json(f"oauth:used-rt:{refresh_token.token}",
                               {"client_id": client.client_id, "family": raw.get("family"),
                                "expires_at": time.time() + REFRESH_TTL})
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject,
                           refresh_token.resource, raw.get("approver_id"),
                           raw.get("grant_expires_at") or time.time() + self.grant_seconds, raw.get("family"))

    def _revoke_family(self, client_id: str, family: str | None) -> None:
        for g in self.store.kv_get_json(f"oauth:grants:{client_id}", []) or []:
            if family is None or g.get("family") == family:
                self._del(g["kind"], g["key"])

    async def load_access_token(self, token: str) -> AccessToken | None:
        v = self._get("at", _h(token))
        if not v or not self.auth.exists(v.get("approver_id")) or \
                not self.store.kv_get_json(f"oauth:client:{v['client_id']}"):
            return None  # approving hangar token revoked, or the app was disconnected
        return AccessToken.model_validate(v)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._del("at", token.token)
        self._del("rt", token.token)

    # ------------------------------------------------------------------ dashboard helpers
    def clients(self) -> list[dict[str, Any]]:
        out = []
        for key in self.store.kv_keys("oauth:client:"):
            raw = self.store.kv_get_json(key)
            if not raw:
                continue
            cid = raw["client_id"]
            meta = self.store.kv_get_json(f"oauth:clientmeta:{cid}") or {}
            live = [g for g in (self.store.kv_get_json(f"oauth:grants:{cid}", []) or [])
                    if g["kind"] == "rt" and self._get("rt", g["key"])]
            out.append({"client_id": cid, "client_name": raw.get("client_name") or cid,
                        "redirect_uris": raw.get("redirect_uris") or [], "approved_by": meta.get("subject"),
                        "last_issued": meta.get("last_issued"), "active": bool(live)})
        return sorted(out, key=lambda c: c.get("last_issued") or 0, reverse=True)

    def revoke_client(self, client_id: str) -> None:
        for g in self.store.kv_get_json(f"oauth:grants:{client_id}", []) or []:
            self._del(g["kind"], g["key"])
        for key in (f"oauth:grants:{client_id}", f"oauth:client:{client_id}", f"oauth:clientmeta:{client_id}"):
            self.store.kv_delete(key)

    # ------------------------------------------------------------------ verification for /mcp
    async def principal(self, token: str | None):
        """--- identity: a grant acts as the hangar token that approved it, with that token's scopes, under the
        grant's own name (what `verify` returns)."""
        if not token:
            return None
        at = await self.load_access_token(token)
        approver = self.auth.by_id((self._get("at", _h(token)) or {}).get("approver_id")) if at else None
        if at is None or approver is None:
            return None
        return approver._replace(name=f"oauth:{at.client_id}({at.subject or '?'})")

    async def verify(self, token: str | None) -> str | None:
        if not token:
            return None
        at = await self.load_access_token(token)
        if not at:
            return None
        return f"oauth:{at.client_id}({at.subject or '?'})"


# ---------------------------------------------------------------------------- consent page
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Authorize · Hangar</title>
<style>
:root{{--floor:#1e2833;--panel:#26323f;--line:#3a4857;--ink:#e8ecef;--ink2:#a9b4be;--y:#f2c230;--err:#e5684f;color-scheme:dark}}
@media (prefers-color-scheme: light){{:root{{--floor:#e6e9eb;--panel:#f6f7f8;--line:#cdd4da;--ink:#1f2a36;--ink2:#4b5967;--y:#e8b419;color-scheme:light}}}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--floor);
color:var(--ink);font:15px/1.5 system-ui,sans-serif;padding:16px}}
.card{{width:min(460px,100%);background:var(--panel);border-top:6px solid var(--y);border-radius:8px;padding:26px;
display:flex;flex-direction:column;gap:14px}}h1{{margin:0;font-size:26px}}p{{margin:0;color:var(--ink2)}}
code{{font-size:13px}}label{{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--ink2)}}
input{{font:inherit;color:inherit;background:var(--floor);border:1px solid var(--line);border-radius:4px;padding:8px}}
.row{{display:flex;gap:10px;justify-content:flex-end}}button{{font:inherit;border-radius:4px;padding:7px 14px;
border:1px solid var(--line);background:transparent;color:inherit;cursor:pointer}}
button.p{{background:var(--y);border-color:var(--y);color:#1e2833;font-weight:600}}.e{{color:var(--err)}}
</style></head><body><form class="card" method="post" action="/oauth/consent">
<h1>Connect {name} to Hangar?</h1>
<p><b>{name}</b> wants to manage your sandboxes: create and destroy them, run commands and agents,
and read and write their files. It will act as the token you approve with.</p>
<p>After approval you'll return to <code>{host}</code>.</p>
{error}
<input type="hidden" name="req" value="{req}">
<label>Hangar API token<input name="token" type="password" autocomplete="off" required autofocus placeholder="hgr_…"></label>
<div class="row"><button name="action" value="deny" formnovalidate>Deny</button>
<button class="p" name="action" value="approve">Approve</button></div>
</form></body></html>"""

_HEADERS = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _page(req: str, client: OAuthClientInformationFull, params: dict[str, Any], error: str = "") -> HTMLResponse:
    u = urlparse(str(params.get("redirect_uri", "")))
    # review(sweep): an app's own scheme is shown with its host, so a custom scheme can't pass for that host's site
    host = (u.netloc if u.scheme in ("http", "https") else f"{u.scheme}:{'//' + u.netloc if u.netloc else ''}") \
        if u.scheme else "the app"
    body = _PAGE.format(name=html.escape(client.client_name or client.client_id), host=html.escape(host),
                        req=html.escape(req), error=f'<p class="e">{html.escape(error)}</p>' if error else "")
    return HTMLResponse(body, headers=_HEADERS)


async def consent_get(request: Request, provider: HangarOAuthProvider) -> Response:
    req = request.query_params.get("req", "")
    found = provider.pending(req)
    if not found:
        return HTMLResponse("<p>This authorization request expired. Start the connection again from the app.</p>",
                            status_code=400, headers=_HEADERS)
    v, client = found
    return _page(req, client, v["params"])


async def consent_post(request: Request, provider: HangarOAuthProvider) -> Response:
    form = await request.form()
    req = str(form.get("req", ""))
    expired = HTMLResponse("<p>This authorization request expired or was already used. Start again from the app.</p>",
                           status_code=400, headers=_HEADERS)
    if form.get("action") == "deny":
        url = provider.deny(req)
        return RedirectResponse(url, status_code=302, headers=_HEADERS) if url else expired
    found = provider.pending(req)
    if not found:
        return expired
    ident = provider.auth.identify(str(form.get("token", "")).strip())
    if not ident:
        v, client = found
        return _page(req, client, v["params"], error="That token was not accepted.")
    try:
        return RedirectResponse(provider.approve(req, ident[0], ident[1]), status_code=302, headers=_HEADERS)
    except AuthorizeError:
        return expired
