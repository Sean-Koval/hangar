"""Single sign-on for the console and the CLI, and deprovisioning driven by the identity provider (KAN-294).

Off by default (`HANGAR_SSO_MODE=off`): people sign in with a pasted token, as before. Two modes turn it on:

* **oidc**: the console signs in through an OpenID Connect provider (Google Workspace, Okta, Entra) with the
  authorization code flow and PKCE. hangar verifies the id token (signature against the provider's JWKS, issuer,
  audience, expiry, nonce), then sets a short-lived session cookie: `__Host-`, HttpOnly, Secure, SameSite=Strict.
  The cookie holds a hangar token that expires with the session; the console never sees a token.
* **iap**: Google IAP sits in front of the console. hangar checks the `x-goog-iap-jwt-assertion` header (ES256,
  Google's IAP keys, the backend service's audience) at sign-in and on every request the session cookie makes.
  Webhooks, `/mcp` and the CLI's token routes go through a backend without IAP and keep bearer tokens.

**Who gets in.** The provider's verified email maps to the hangar person linked to it (`hangar person link <id>
email <address>`). An email linked to nobody is refused, or (`HANGAR_SSO_DEFAULT_ROLE=member`, which needs
`HANGAR_SSO_ALLOWED_DOMAINS`) becomes a new member. `HANGAR_SSO_REQUIRED_GROUP` admits only members of that group.
A person's first sign-in needs an email the provider vouches for, and binds them to the provider's subject. Later
sign-ins find the person by that subject, not the email; a different subject with the same email is refused until an
admin clears the binding. A Google issuer also needs the account's Workspace domain (`hd`) to be an allowed one.

**Deprovisioning.** Every person who signed in through SSO is checked again at the provider every
`HANGAR_SSO_RECHECK_MINUTES` (oidc: a refresh-token grant, then the domain, group and email checks again). A provider
that refuses the refresh, or a person no longer in the group, disables the binding and revokes their tokens, without
an admin. A person hangar hasn't confirmed for `HANGAR_SSO_MAX_STALE_MINUTES` (the provider unreachable, no refresh
token, IAP with no console visit) can't use their tokens until it does: the check fails closed. Their service
tokens (jobs, metrics) ride out an outage for `HANGAR_SSO_SERVICE_STALE_HOURS`, then stop too. A recheck that would
drop too many people at once drops no one, and suspends them instead. `HANGAR_SSO_REQUIRED` (on whenever a mode is)
also stops the tokens of people who never signed in through SSO. Every other front door (the relay, Tower
passports, Slack, GitHub, Jira) asks `people.deprovisioned` too.

**Break-glass.** The server's admin token and the bootstrap token (by its recorded id) are never gated, so an admin
can still get in when the provider is down. Each use is recorded, and `hangar doctor` warns while one is in use.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from hangar.models import ApiToken, now

if TYPE_CHECKING:
    from hangar.app import Hangar

log = logging.getLogger("hangar.sso")

MODES = ("off", "oidc", "iap")
SESSION_COOKIE = "__Host-hangar_session"  # __Host-: Secure, Path=/, no Domain: no subdomain can set or read it
PENDING_COOKIE = "__Host-hangar_sso"  # ties a sign-in to the browser that started it (login CSRF)
CONSOLE_HEADER = "x-requested-with"  # the console sends it on every call; a cross-site form can't (no CORS here)
IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_JWKS = "https://www.gstatic.com/iap/verify/public_key-jwk"
SESSION_NAME = "sso-session"
PENDING_TTL = 600
HANDOFF_TTL = 120
CONFIRM_COOKIE = "__Host-hangar_sso_cli"  # ties a CLI hand-off's confirmation to the browser that signed in
INTERACTIVE = frozenset({"admin", "user", "mcp"})  # scopes a person uses by hand; staleness stops these only
BREAKER = 0.2  # a recheck pass that would drop more than this share of people (and more than one) drops no one
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
LEEWAY = 60
JWKS_REFETCH = 60
DISCOVERY_TTL = 3600
TIMEOUT = 10
_KV = "sso:"
_PORT_RE = re.compile(r"^[0-9]{1,5}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_IAP_AUD_RE = re.compile(r"^/projects/[0-9]+/(global/backendServices/[0-9]+|apps/[a-z][a-z0-9-]*)$")
REFUSED_AUDITS_PER_MINUTE = 30  # beyond this, a refused sign-in is only logged: an unauthenticated route


class Refused(Exception):
    """A sign-in hangar won't accept. The text is safe to show the person."""


class Revoked(Exception):
    """The provider definitively refused a person's refresh token (removed, suspended, consent withdrawn)."""


class Unreachable(Exception):
    """The provider couldn't be asked. Never a reason to disable anyone: staleness covers it."""


# ---------------------------------------------------------------------------- JWTs
def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def s256(verifier: str) -> str:
    """PKCE's S256 challenge of a verifier."""
    return _b64e(hashlib.sha256(verifier.encode()).digest())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _jwk_key(k: dict[str, Any]) -> tuple[str, Any] | None:
    """(family, public key) for an RSA or P-256 JWK; None for any other."""
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    try:
        if k.get("kty") == "RSA":
            n, e = int.from_bytes(_b64d(k["n"]), "big"), int.from_bytes(_b64d(k["e"]), "big")
            return "RSA", rsa.RSAPublicNumbers(e, n).public_key()
        if k.get("kty") == "EC" and k.get("crv") == "P-256":
            x, y = int.from_bytes(_b64d(k["x"]), "big"), int.from_bytes(_b64d(k["y"]), "big")
            return "EC", ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except (KeyError, ValueError, TypeError):
        return None
    return None


_ALG_FAMILY = {"RS256": "RSA", "ES256": "EC"}


def _signature_ok(alg: str, key: Any, data: bytes, sig: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        if alg == "RS256":
            key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
        elif alg == "ES256":
            if len(sig) != 64:
                return False
            der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
            key.verify(der, data, ec.ECDSA(hashes.SHA256()))
        else:
            return False
    except InvalidSignature:
        return False
    return True


class JWKS:
    """A provider's signing keys, by key id. An unknown key id refetches at most once a minute."""

    def __init__(self, url: str, client: Callable[[], httpx.AsyncClient], clock: Callable[[], float] = time.time):
        self.url, self._client, self.clock = url, client, clock
        self._keys: dict[str, tuple[str, Any]] = {}
        self._fetched = float("-inf")

    async def key(self, kid: str) -> tuple[str, Any] | None:
        """The key, None when a fresh fetch doesn't have it, or Unreachable: a fetch failed, or a key id appeared
        within a minute of the last fetch (a rotation, not a forgery, until the keys can be read again)."""
        if kid not in self._keys:
            if self.clock() - self._fetched < JWKS_REFETCH:
                raise Unreachable("the provider's keys were read under a minute ago; try again shortly")
            try:
                async with self._client() as c:
                    r = await c.get(self.url)
                keys = r.json().get("keys") if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError, AttributeError) as e:
                raise Unreachable(f"the provider's keys couldn't be read ({type(e).__name__})") from None
            if not isinstance(keys, list):
                raise Unreachable(f"the provider's keys answered HTTP {r.status_code}")
            self._fetched = self.clock()  # only a fetch that worked counts toward the once-a-minute limit
            self._keys = {str(k["kid"]): fk for k in keys
                          if isinstance(k, dict) and k.get("kid") and k.get("use") in (None, "sig")
                          and (fk := _jwk_key(k)) is not None}
        return self._keys.get(kid)


async def verify_jwt(token: str, jwks: JWKS, *, issuer: str | tuple[str, ...], audience: str, algs: tuple[str, ...],
                     nonce: str | None = None, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """The claims of a JWT the provider signed for `audience`, or Refused (Unreachable when its keys can't be read)."""
    try:
        head_b, body_b, sig_b = token.split(".")
        header, claims = json.loads(_b64d(head_b)), json.loads(_b64d(body_b))
        sig = _b64d(sig_b)
    except (ValueError, json.JSONDecodeError, AttributeError):
        raise Refused("the sign-in token isn't a JWT") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise Refused("the sign-in token isn't a JWT")
    alg = header.get("alg")
    if alg not in algs:
        raise Refused(f"the sign-in token's algorithm {alg!r} isn't one of {', '.join(algs)}")
    found = await jwks.key(str(header.get("kid") or ""))
    if found is None or found[0] != _ALG_FAMILY[alg]:
        raise Refused("the sign-in token was signed with a key the provider doesn't publish")
    if not _signature_ok(alg, found[1], f"{head_b}.{body_b}".encode(), sig):
        raise Refused("the sign-in token's signature doesn't verify")
    issuers = (issuer,) if isinstance(issuer, str) else issuer
    if str(claims.get("iss") or "").rstrip("/") not in [i.rstrip("/") for i in issuers]:
        raise Refused("the sign-in token comes from another issuer")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if audience not in auds:
        raise Refused("the sign-in token was issued for another application")
    if len(auds) > 1 and claims.get("azp") not in (None, audience):
        raise Refused("the sign-in token was issued to another party")
    t = clock()
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or exp + LEEWAY < t:
        raise Refused("the sign-in token has expired")
    for k in ("nbf", "iat"):
        v = claims.get(k)
        if v is not None and (not isinstance(v, int | float) or v - LEEWAY > t):
            raise Refused("the sign-in token isn't valid yet")
    if nonce is not None and not (isinstance(claims.get("nonce"), str)
                                  and secrets.compare_digest(claims["nonce"], nonce)):
        raise Refused("the sign-in token doesn't belong to this sign-in")
    return claims


# ---------------------------------------------------------------------------- settings
def domains(settings) -> list[str]:
    return [d.strip().lower().lstrip("@") for d in (settings.sso_allowed_domains or "").split(",") if d.strip()]


def _local_url(url: str) -> bool:
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")


def google(settings) -> bool:
    return (settings.sso_issuer or "").rstrip("/") in GOOGLE_ISSUERS


def required(settings) -> bool:
    """HANGAR_SSO_REQUIRED: unset means on whenever a mode is; false is an explicit opt-out."""
    return settings.sso_mode != "off" if settings.sso_required is None else bool(settings.sso_required)


def exempt(settings) -> set[str]:
    return {p.strip() for p in (settings.sso_exempt_people or "").split(",") if p.strip()}


def _email(raw: Any) -> str:
    """An email as hangar compares it: stripped and lowercased, ASCII only. Folding other characters can make two
    addresses the provider keeps apart equal (the Kelvin sign lowercases, and NFKC-normalizes, to k), so anything
    else is refused rather than folded."""
    email = str(raw or "").strip()
    if not email.isascii():
        raise Refused("the provider sent an email address with non-ASCII characters")
    return email.lower()


def problems(settings) -> list[str]:
    """What stops this SSO configuration from starting (none when SSO is off)."""
    s, out = settings, []
    if s.sso_mode == "off":
        return out
    if not (s.public_url.startswith("https://") or _local_url(s.public_url)):
        out.append("HANGAR_SSO_MODE needs an https HANGAR_PUBLIC_URL: the session cookie is Secure")
    if s.sso_mode == "oidc":
        if not s.sso_issuer or not (s.sso_issuer.startswith("https://") or _local_url(s.sso_issuer)):
            out.append("HANGAR_SSO_MODE=oidc needs the provider's https issuer URL in HANGAR_SSO_ISSUER")
        if not s.sso_client_id:
            out.append("HANGAR_SSO_MODE=oidc needs HANGAR_SSO_CLIENT_ID")
    if s.sso_mode == "oidc" and google(s) and not domains(s):
        out.append("a Google issuer needs HANGAR_SSO_ALLOWED_DOMAINS (the Workspace domains): otherwise any Google "
                   "account, a personal one included, can claim a person")
    if s.sso_mode == "iap" and not _IAP_AUD_RE.match(s.sso_iap_audience or ""):
        out.append("HANGAR_SSO_MODE=iap needs HANGAR_SSO_IAP_AUDIENCE "
                   "(/projects/<number>/global/backendServices/<id> or /projects/<number>/apps/<project id>)")
    if s.sso_mode == "iap" and s.sso_required_group:
        out.append("HANGAR_SSO_REQUIRED_GROUP needs HANGAR_SSO_MODE=oidc: IAP's header carries no groups (limit the "
                   "IAP resource's access to the group instead)")
    if s.sso_default_role != "none" and not domains(s):
        out.append("HANGAR_SSO_DEFAULT_ROLE=member needs HANGAR_SSO_ALLOWED_DOMAINS: otherwise any account the "
                   "provider knows becomes a member")
    if s.sso_mode == "oidc" and not s.sso_require_email_verified and not domains(s):
        out.append("HANGAR_SSO_REQUIRE_EMAIL_VERIFIED=false needs HANGAR_SSO_ALLOWED_DOMAINS")
    if s.sso_max_stale_minutes < s.sso_recheck_minutes:
        out.append("HANGAR_SSO_MAX_STALE_MINUTES must be at least HANGAR_SSO_RECHECK_MINUTES")
    if s.sso_service_stale_hours * 60 < s.sso_max_stale_minutes:
        out.append("HANGAR_SSO_SERVICE_STALE_HOURS must be at least HANGAR_SSO_MAX_STALE_MINUTES")
    return out


# ---------------------------------------------------------------------------- bindings
def binding(store, person: str) -> dict[str, Any] | None:
    return store.kv_get_json(f"{_KV}binding:{person}")


def bindings(store) -> list[dict[str, Any]]:
    return [b for k in sorted(store.kv_keys(f"{_KV}binding:")) if (b := store.kv_get_json(k))]


def _put_binding(store, b: dict[str, Any]) -> None:
    from hangar.people import SSO_BINDING_KV

    store.kv_set(SSO_BINDING_KV + b["person"], json.dumps(b), durable=True)  # a removal must not roll back


def bound_to(store, issuer: str, sub: str) -> str | None:
    """The person bound to this provider account, by its immutable subject, whatever its email now says."""
    if not sub:
        return None
    for b in bindings(store):
        if (str(b.get("issuer") or "").rstrip("/"), b.get("sub")) == (issuer.rstrip("/"), sub):
            return b["person"]
    return None


def vouched(claims: dict[str, Any]) -> bool:
    """Whether the provider vouches for the email: email_verified, or Entra's xms_edov (verified domain owner)."""
    return claims.get("email_verified") in (True, "true") or claims.get("xms_edov") in (True, "true")


def break_glass(tok: ApiToken | None, store) -> bool:
    """The server's admin token (None) or the bootstrap token, by the id `ensure_bootstrap` recorded (a token merely
    named like it is ordinary): never gated, so a provider outage can't lock out."""
    from hangar.auth import break_glass_token_id

    return tok is None or (tok.person is None and tok.id == break_glass_token_id(store))


class Gate:
    """`TokenAuth.gate`: whether a person's token works, given what the provider last said about them."""

    def __init__(self, sso: SSO):
        self.sso = sso

    def __call__(self, person: str, tok: ApiToken | None) -> bool:
        s, store = self.sso.settings, self.sso.store
        if break_glass(tok, store):
            self.sso.note_break_glass(tok)
            return True
        b = binding(store, person)
        if b is None:
            return person in exempt(s) or not required(s)
        if b.get("disabled_at") or b.get("suspended_at"):
            return False
        age = now() - float(b.get("verified_at") or 0)
        if age <= s.sso_max_stale_minutes * 60:
            return True
        # unconfirmed for too long: a person's own tokens stop (fail closed); their service tokens (jobs, metrics)
        # ride out a provider outage for a bounded grace, then stop too
        if age > s.sso_service_stale_hours * 3600:
            return False
        return tok is not None and not (set(tok.scopes) & INTERACTIVE)


# ---------------------------------------------------------------------------- the service
class SSO:
    """The configured mode's sign-in, the session cookie, the CLI hand-off and the recheck loop."""

    def __init__(self, h: Hangar):
        self.h, self.settings, self.store = h, h.settings, h.store
        self.mode = self.settings.sso_mode
        self.transport: httpx.AsyncBaseTransport | None = None  # tests inject the provider
        self.clock: Callable[[], float] = time.time
        self._meta: dict[str, Any] | None = None
        self._meta_at = float("-inf")
        self._jwks: JWKS | None = None
        self._iap_jwks = JWKS(IAP_JWKS, self.client, lambda: self.clock())
        self._iap_seen: dict[str, tuple[dict[str, Any], float]] = {}
        self._break_glass_noted = 0.0
        self._locks: dict[str, asyncio.Lock] = {}
        self._session_key: bytes | None = None
        self._task: asyncio.Task | None = None
        from hangar.people import SSO_POLICY_KV

        # what the other front doors enforce (people.deprovisioned): with SSO off, bindings left behind stop no one
        self.store.kv_set(SSO_POLICY_KV, json.dumps({"on": self.on, "required": self.on and required(self.settings),
                                                     "exempt": sorted(exempt(self.settings))}), durable=True)
        if self.mode != "off":
            if bad := problems(self.settings):
                raise ValueError("; ".join(bad))
            h.auth.gate = Gate(self)

    @property
    def on(self) -> bool:
        return self.mode != "off"

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT)

    def audit(self, actor: str, action: str, target: str | None = None, **kw: Any) -> None:
        self.h.fleet.audit(actor, action, target, **kw)

    def note_break_glass(self, tok: ApiToken | None) -> None:
        """Record a break-glass token's use (at most once a minute; audited at most once an hour)."""
        t = now()
        if t - self._break_glass_noted < 60:
            return
        last = float(self.store.kv_get(f"{_KV}break_glass") or 0)
        self._break_glass_noted = t
        self.store.kv_set(f"{_KV}break_glass", str(t))
        if t - last >= 3600:
            self.audit("admin", "sso.break_glass", "admin" if tok is None else tok.id,
                       why="the admin or bootstrap token was used while SSO is on")

    def break_glass_last(self) -> float | None:
        v = self.store.kv_get(f"{_KV}break_glass")
        return float(v) if v else None

    # ------------------------------------------------------------------ provider (oidc)
    async def metadata(self) -> dict[str, Any]:
        if self._meta is not None and self.clock() - self._meta_at < DISCOVERY_TTL:
            return self._meta
        issuer = (self.settings.sso_issuer or "").rstrip("/")
        try:
            async with self.client() as c:
                r = await c.get(f"{issuer}/.well-known/openid-configuration")
            meta = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as e:
            raise Unreachable(f"the provider's discovery document couldn't be read ({type(e).__name__})") from None
        if not isinstance(meta, dict):
            raise Unreachable(f"the provider's discovery document answered HTTP {r.status_code}")
        if str(meta.get("issuer") or "").rstrip("/") != issuer:
            raise Unreachable("the provider's discovery document names another issuer")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not str(meta.get(k) or "").startswith(("https://", "http://localhost", "http://127.0.0.1")):
                raise Unreachable(f"the provider's discovery document has no usable {k}")
        if self._jwks is None or self._jwks.url != meta["jwks_uri"]:
            self._jwks = JWKS(meta["jwks_uri"], self.client, lambda: self.clock())
        self._meta, self._meta_at = meta, self.clock()
        return meta

    def redirect_uri(self) -> str:
        return self.settings.public_url.rstrip("/") + "/auth/sso/callback"

    def _client_auth(self) -> dict[str, str]:
        out = {"client_id": self.settings.sso_client_id or ""}
        if name := self.settings.sso_client_secret_credential:
            secret = self.h.vault.get_text(name)
            if not secret:
                raise Unreachable(f"the client secret credential '{name}' isn't in the vault")
            out["client_secret"] = secret.strip()
        return out

    async def _token_call(self, form: dict[str, str]) -> dict[str, Any]:
        meta = await self.metadata()
        try:
            async with self.client() as c:
                r = await c.post(meta["token_endpoint"], data={**form, **self._client_auth()},
                                 headers={"Accept": "application/json"})
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise Unreachable(f"the provider's token endpoint couldn't be reached ({type(e).__name__})") from None
        # only invalid_grant speaks for the person; anything else (unauthorized_client, invalid_client) is about
        # hangar's own registration, and must not drop everyone
        if r.status_code == 400 and isinstance(body, dict) and body.get("error") == "invalid_grant":
            raise Revoked("invalid_grant")
        if r.status_code != 200 or not isinstance(body, dict):
            raise Unreachable(f"the provider's token endpoint answered HTTP {r.status_code}")
        return body

    async def id_claims(self, id_token: str, nonce: str | None) -> dict[str, Any]:
        await self.metadata()
        assert self._jwks is not None
        issuer = self.settings.sso_issuer or ""
        issuers = GOOGLE_ISSUERS if issuer.rstrip("/") == GOOGLE_ISSUERS[0] else (issuer,)
        return await verify_jwt(id_token, self._jwks, issuer=issuers,
                                audience=self.settings.sso_client_id or "", algs=("RS256", "ES256"),
                                nonce=nonce, clock=self.clock)

    async def _userinfo(self, access_token: str | None, sub: str) -> dict[str, Any] | None:
        meta = await self.metadata()
        url = meta.get("userinfo_endpoint")
        if not access_token or not url:
            return None
        try:
            async with self.client() as c:
                r = await c.get(url, headers={"Authorization": f"Bearer {access_token}"})
            info = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        return info if isinstance(info, dict) and info.get("sub") == sub else None

    # ------------------------------------------------------------------ who gets in
    def admit(self, claims: dict[str, Any], *, create: bool = True, bound: str | None = None):
        """The hangar person the provider's claims name, or Refused. `bound`: the person already bound to this
        account's subject, who it stays, whatever the email now says. Without one, the email names the person, so
        the provider must vouch for it (`email_verified`, or Entra's `xms_edov`). `create`: an unknown email may
        become a member (HANGAR_SSO_DEFAULT_ROLE); the recheck passes False."""
        from hangar import people

        s = self.settings
        email = _email(claims.get("email"))
        if not email or "@" not in email:
            raise Refused("the provider sent no email address")
        if claims.get("xms_edov") in (False, "false"):
            raise Refused(f"the provider says the domain of {email} isn't verified")
        if self.mode == "oidc" and (bound is None or s.sso_require_email_verified) and not vouched(claims):
            raise Refused(f"the provider doesn't vouch for {email} (no email_verified, or Entra's xms_edov), so it "
                          "can't name a hangar person")
        if (allowed := domains(s)) and email.rsplit("@", 1)[1] not in allowed:
            raise Refused(f"{email} isn't in an allowed domain")
        if allowed and (google(s) if self.mode == "oidc" else self.mode == "iap") \
                and str(claims.get("hd") or "").lower() not in allowed:
            raise Refused(f"{email} isn't a Workspace account of an allowed domain (hd)")
        if group := s.sso_required_group:
            groups = claims.get(s.sso_group_claim)
            if not isinstance(groups, list):
                raise Refused(f"the provider sent no '{s.sso_group_claim}' claim, so membership of {group} can't be "
                              "checked")
            if group not in [str(g) for g in groups]:
                raise Refused(f"{email} isn't in the group {group}")
        if bound is not None:
            person = people.get(self.store, bound)
            if person is None:
                raise Refused(f"{bound} no longer exists")
            # the subject names the person, but an admin's links still decide: an email moved to someone else, or
            # every email unlinked from them, ends their sign-in
            other = people.resolve_external(self.store, "email", email, include_deprovisioned=True)
            if other is not None and other.id != bound:
                raise Refused(f"{email} is now linked to {other.id}, not {bound}")
            if not any(k.startswith("email:") for k in person.links):
                raise Refused(f"{bound} has no email linked any more")
            return person
        person = people.resolve_external(self.store, "email", email, include_deprovisioned=True)
        if person is not None:
            return person
        if not create or s.sso_default_role == "none":
            raise Refused(f"{email} isn't linked to a hangar person; an admin links it with "
                          f"`hangar person link <person> email {email}`")
        pid = _new_person_id(self.store, email)
        person = people.add(self.store, pid, name=str(claims.get("name") or email)[:100], role=s.sso_default_role)
        people.link(self.store, pid, "email", email)
        self.audit("sso", "person.add", pid, role=s.sso_default_role, via="sso", email=email)
        return person

    def bind(self, person, issuer: str, sub: str, email: str, refresh: str | None, *, verified: bool = True) -> None:
        """Record a successful sign-in. A person bound to another subject is refused (a reused email). A first
        sign-in needs an email the provider vouches for: otherwise whoever first claims the address (Entra's `email`
        is editable) would become that person. A sign-in lifts a suspension: the provider just vouched for them."""
        email = _email(email)
        b = binding(self.store, person.id)
        if b is None and not verified:
            raise Refused(f"the provider doesn't vouch for {email}, so it can't be a first sign-in; turn on the "
                          "provider's verified-email claim (Entra: the xms_edov optional claim)")
        if not sub:
            raise Refused("the provider sent no subject for the account")
        person = person.id
        if b and (b.get("issuer"), b.get("sub")) != (issuer, sub):
            raise Refused(f"{email} signed in as a different account at the provider than before; an admin clears "
                          f"the old binding with DELETE /api/sso/bindings/{person}")
        b = {**(b or {}), "person": person, "issuer": issuer, "sub": sub, "email": email, "verified_at": now(),
             "disabled_at": None, "disabled_why": None, "suspended_at": None, "suspended_why": None,
             "mode": self.mode}
        if refresh:
            b["refresh"] = base64.b64encode(self.h.vault.encrypt(refresh.encode())).decode()
        _put_binding(self.store, b)

    def disable(self, b: dict[str, Any], why: str) -> list[str]:
        """The provider dropped this person: their binding is disabled and every token of theirs revoked."""
        b.update(disabled_at=now(), disabled_why=why[:300])
        b.pop("refresh", None)
        _put_binding(self.store, b)
        revoked = [t.id for t in self.store.list(ApiToken) if (t.person or "owner") == b["person"]
                   and not break_glass(t, self.store)]
        for tid in revoked:
            self.store.delete(ApiToken, tid)
        self.audit("sso", "sso.deprovisioned", b["person"], why=why[:300], revoked=len(revoked))
        log.warning("sso: %s deprovisioned: %s (%d tokens revoked)", b["person"], why, len(revoked))
        return revoked

    # ------------------------------------------------------------------ recheck loop (oidc)
    def _lock(self, person: str) -> asyncio.Lock:
        return self._locks.setdefault(person, asyncio.Lock())

    def _update(self, person: str, sub: Any, **fields: Any) -> None:
        """Write fields onto the binding as it is now: an admin's unbind or a newer sign-in in the meantime wins."""
        b = binding(self.store, person)
        if b is not None and b.get("sub") == sub and not b.get("disabled_at"):
            _put_binding(self.store, {**b, **fields})

    def _seal(self, refresh: str) -> str:
        return base64.b64encode(self.h.vault.encrypt(refresh.encode())).decode()

    async def check(self, person: str) -> tuple[str, str | None]:
        """Ask the provider about one bound person, without disabling anyone: ('ok' | 'stale' | 'unreachable' |
        'drop', why). Serialised per person, so two rechecks never spend one refresh token."""
        async with self._lock(person):
            b = binding(self.store, person)
            if b is None or b.get("disabled_at"):
                return "gone", None
            sealed, sub = b.get("refresh"), b.get("sub")
            if not sealed or self.mode != "oidc":
                return "stale", None
            try:
                refresh = self.h.vault.decrypt(base64.b64decode(sealed)).decode()
                tokens = await self._token_call({"grant_type": "refresh_token", "refresh_token": refresh})
                if new := tokens.get("refresh_token"):  # rotated: keep it now, whatever the checks below hit
                    self._update(person, sub, refresh=self._seal(str(new)))
                claims: dict[str, Any] | None = None
                if tokens.get("id_token"):
                    claims = await self.id_claims(str(tokens["id_token"]), None)
                else:
                    claims = await self._userinfo(tokens.get("access_token"), str(sub))
                if claims is None:
                    return "unreachable", None
                if claims.get("sub") != sub:
                    raise Refused("the provider answered for another account")
                # a refreshed id token or userinfo may leave out what sign-in verified; the same subject keeps it
                same = str(claims.get("email") or b.get("email")).lower() == b.get("email")
                if "email_verified" not in claims and same:
                    claims = {**claims, "email": b["email"], "email_verified": True}
                self.admit(claims, create=False, bound=person)
            except Revoked as e:
                return "drop", f"the provider refused the refresh ({e}): removed, suspended or signed out everywhere"
            except Refused as e:
                return "drop", str(e)
            except (Unreachable, ValueError) as e:
                log.info("sso: couldn't recheck %s: %s", person, e)
                return "unreachable", None
            self._update(person, sub, verified_at=now(), suspended_at=None, suspended_why=None)
            return "ok", None

    async def recheck(self, b: dict[str, Any]) -> str:
        """One person: 'ok', 'disabled', 'stale' (no refresh token) or 'unreachable'."""
        verdict, why = await self.check(b["person"])
        if verdict == "drop":
            self._drop(b["person"], why or "dropped by the provider", b.get("sub"))
            return "disabled"
        return "disabled" if verdict == "gone" else verdict

    def _drop(self, person: str, why: str, sub: Any = None) -> None:
        """Disable, unless the binding changed since it was checked (an unbind and a new sign-in in the meantime)."""
        b = binding(self.store, person)
        if b is not None and not b.get("disabled_at") and (sub is None or b.get("sub") == sub):
            self.disable(b, why)

    async def recheck_all(self) -> dict[str, int]:
        """Every bound person. A pass that would drop more than BREAKER of them (and more than one) drops no one:
        that is a misconfigured provider, not a team leaving at once. `hangar doctor` fails until a pass is clean."""
        out: dict[str, int] = {}
        drops: list[tuple[str, str]] = []
        subs = {b["person"]: b.get("sub") for b in bindings(self.store) if not b.get("disabled_at")}
        active = list(subs)
        for person in active:
            verdict, why = await self.check(person)
            if verdict == "drop":
                drops.append((person, why or "dropped by the provider"))
            elif verdict != "gone":
                out[verdict] = out.get(verdict, 0) + 1
        # not confirmed for longer than the service grace (IAP, no refresh token, a long outage): suspended, so the
        # other front doors stop them too, as the gate already does; a sign-in or a clean check lifts it
        grace = self.settings.sso_service_stale_hours * 3600
        for b in bindings(self.store):
            if not b.get("disabled_at") and not b.get("suspended_at") and b.get("sub") == subs.get(b["person"]) \
                    and now() - float(b.get("verified_at") or 0) > grace:
                _put_binding(self.store, {**b, "suspended_at": now(),
                                          "suspended_why": "not confirmed by the provider within "
                                                           "HANGAR_SSO_SERVICE_STALE_HOURS"})
        if len(drops) > 1 and len(drops) > BREAKER * len(active):
            # too many at once to be people leaving: revoke nothing, but suspend each of them (every token stops)
            # until a clean recheck, a sign-in or an admin's unbind; break-glass still works
            self.store.kv_set_json(f"{_KV}halted", {"at": now(), "drops": len(drops), "of": len(active),
                                                    "why": drops[0][1][:300]})
            for person, why in drops:
                b = binding(self.store, person)
                if b is not None and not b.get("disabled_at") and b.get("sub") == subs.get(person):
                    _put_binding(self.store, {**b, "suspended_at": b.get("suspended_at") or now(),
                                              "suspended_why": why[:300]})
            self.audit("sso", "sso.recheck_halted", None, drops=len(drops), of=len(active), why=drops[0][1][:300],
                       suspended=[p for p, _ in drops])
            log.error("sso: recheck would drop %d of %d people (%r); revoked nothing and suspended them",
                      len(drops), len(active), drops[0][1])
            out["halted"] = len(drops)
        else:
            self.store.kv_delete(f"{_KV}halted")
            for person, why in drops:
                self._drop(person, why, subs.get(person))
            if drops:
                out["disabled"] = len(drops)
        self._prune()
        return out

    def _prune(self) -> None:
        """Expired session tokens, sign-ins never finished and CLI hand-offs never redeemed."""
        t = now()
        for tok in self.store.list(ApiToken):
            if tok.expires_at is not None and tok.expires_at <= t:
                self.store.delete(ApiToken, tok.id)
        for prefix in ("pending:", "handoff:", "confirm:"):
            for k in self.store.kv_keys(_KV + prefix):
                v = self.store.kv_get_json(k)
                if not v or float(v.get("expires_at") or 0) <= t:
                    self.store.kv_delete(k)

    def start(self) -> None:
        if self.on and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.recheck_all()
            except Exception:  # noqa: BLE001 - the loop must survive anything one pass hits
                log.exception("sso recheck failed")
            await asyncio.sleep(self.settings.sso_recheck_minutes * 60)

    # ------------------------------------------------------------------ IAP
    async def iap_claims(self, assertion: str | None) -> dict[str, Any]:
        if not assertion:
            raise Refused("the request didn't come through IAP")
        seen = self._iap_seen.get(assertion)
        if seen is not None and seen[1] > self.clock():
            return seen[0]
        claims = await verify_jwt(assertion, self._iap_jwks, issuer=IAP_ISSUER,
                                  audience=self.settings.sso_iap_audience or "", algs=("ES256",), clock=self.clock)
        if len(self._iap_seen) > 1000:
            self._iap_seen.clear()
        self._iap_seen[assertion] = (claims, min(float(claims["exp"]), self.clock() + 60))
        return claims

    # ------------------------------------------------------------------ tokens
    def session_bearer(self, cookie: str) -> str:
        """The bearer a session cookie stands for. Only the server holds the key, so the cookie's value is no token:
        sent as `Authorization: Bearer` it is refused, and only SessionMiddleware (with its checks) can use it."""
        if self._session_key is None:
            sealed = self.store.kv_get(f"{_KV}session_key")
            if sealed:
                key = self.h.vault.decrypt(base64.b64decode(sealed))
            else:
                key = secrets.token_bytes(32)
                self.store.kv_set(f"{_KV}session_key", base64.b64encode(self.h.vault.encrypt(key)).decode())
            self._session_key = key
        return "hgs_" + hmac.new(self._session_key, cookie.encode(), hashlib.sha256).hexdigest()

    def mint_session(self, person) -> str:
        """A console session: the cookie's value, whose token is stored under its derived bearer."""
        from hangar.auth import hash_token

        scopes = ["admin"] if person.role == "admin" else ["user", "jobs"]
        cookie = secrets.token_urlsafe(32)
        tok = ApiToken(name=SESSION_NAME, token_hash=hash_token(self.session_bearer(cookie)), scopes=scopes,
                       person=person.id, expires_at=now() + self.settings.sso_session_minutes * 60)
        self.store.put(tok)
        return cookie

    def mint_cli(self, person, name: str) -> tuple[str, ApiToken]:
        scopes = ["admin"] if person.role == "admin" else ["user", "mcp", "jobs"]
        return self.h.auth.create(name, scopes, person=person.id)


def _new_person_id(store, email: str) -> str:
    from hangar import people

    base = re.sub(r"[^a-z0-9._-]+", "-", email.split("@", 1)[0].lower()).strip("-._")[:34] or "person"
    if not base[0].isalnum():
        base = "p" + base
    pid, n = base, 1
    while people.get(store, pid) is not None or pid in people.RESERVED_IDS:  # --- KAN-552
        n += 1
        pid = f"{base}-{n}"
    return pid


# ---------------------------------------------------------------------------- ASGI: the session cookie
def _cookies(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in headers.get("cookie", "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep:
            out.setdefault(k, v)
    return out


class SessionMiddleware:
    """A console request with the session cookie and no Authorization header becomes a bearer request on `/api/`.

    Requests that change something must also carry the console's `X-Requested-With` header and must not be
    cross-site (`Sec-Fetch-Site`), on top of SameSite=Strict. In IAP mode the IAP assertion must be valid and name
    the session's person. Bearer requests (the CLI, MCP clients, webhooks) pass through untouched."""

    def __init__(self, app, hangar):
        self.app, self.h = app, hangar

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        raw = _cookies(headers).get(SESSION_COOKIE)
        if "authorization" in headers or not raw:
            return await self.app(scope, receive, send)
        if scope.get("method", "GET") not in ("GET", "HEAD", "OPTIONS"):
            if headers.get(CONSOLE_HEADER, "").lower() != "hangar" or \
                    headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                return await _deny(send, 403, "a console request must come from the console")
        sso: SSO = self.h.sso
        bearer = sso.session_bearer(raw)
        if sso.mode == "iap":
            p = self.h.auth.principal(bearer)
            try:
                claims = await sso.iap_claims(headers.get(IAP_HEADER))
            except (Refused, Unreachable) as e:
                return await _deny(send, 401, f"IAP: {e}")
            b = binding(self.h.store, p.person) if p and p.person else None
            if b is None or str(claims.get("email") or "").lower() != b.get("email"):
                return await _deny(send, 401, "the session and the IAP sign-in are different people")
            if now() - float(b.get("verified_at") or 0) >= 60:
                b["verified_at"] = now()
                _put_binding(self.h.store, b)
        scope = dict(scope)
        scope["headers"] = [*scope.get("headers", []), (b"authorization", f"Bearer {bearer}".encode())]
        return await self.app(scope, receive, send)


async def _deny(send, status: int, msg: str) -> None:
    body = json.dumps({"error": msg}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------- routes
class CliToken(BaseModel):
    """`POST /auth/sso/cli/token`: the hand-off code the browser got, and the verifier only the CLI holds."""

    code: str = Field(max_length=200)
    verifier: str = Field(min_length=43, max_length=128)
    name: str = Field("cli-sso", max_length=100)


def _cookie(name: str, value: str, max_age: int, samesite: str) -> str:
    return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; Secure; SameSite={samesite}"


def _page(title: str, body: str, refresh: str | None = None, status: int = 200):
    from hangar.uiheaders import page_headers

    meta = f'<meta http-equiv="refresh" content="0;url={html.escape(refresh)}">' if refresh else ""
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}<title>{html.escape(title)}</title>'
           f'<link rel="stylesheet" href="/ui/airframe/airframe.css"></head><body class="af-page">'
           f'<main class="af-card af-card--airy"><h1>{html.escape(title)}</h1>{body}</main></body></html>')
    return HTMLResponse(doc, status_code=status, headers=page_headers(None))


def mount(app, h: Hangar) -> None:
    """The SSO routes and the session middleware. With SSO off only `/auth/sso/config` answers (mode off)."""
    sso: SSO = h.sso

    @app.get("/auth/sso/config")
    async def sso_config():
        return {"mode": sso.mode, "login": "/auth/sso/login" if sso.on else None}

    if not sso.on:
        return

    audited: list[float] = []

    def refused(e: Exception, who: str | None = None, *, status: int = 403):
        """The person sees a reference, not the reason: the reason (the provider's answer, an email, a claim) goes
        to the audit log and the server log under that reference, where an admin looks it up. The reason can carry
        an unauthenticated caller's text, so it is logged quoted, and audited at most REFUSED_AUDITS_PER_MINUTE."""
        ref = "sso_" + secrets.token_hex(6)
        why = repr(str(e))[1:-1][:300]  # escaped: no newline or control character reaches a log
        t = now()
        audited[:] = [a for a in audited if t - a < 60]
        if len(audited) < REFUSED_AUDITS_PER_MINUTE:
            audited.append(t)
            sso.audit(who or "sso", "sso.refused" if status == 403 else "sso.unavailable", None, ref=ref, why=why)
        log.warning("sso: sign-in %s (%s): %r", "refused" if status == 403 else "unavailable", ref, str(e))
        title = "Sign-in refused" if status == 403 else "Sign-in unavailable"
        body = (f"<p>hangar couldn't sign you in. Try again, or give an admin this reference: <code>{ref}</code>"
                '</p><p><a href="/">Back to hangar</a></p>')
        return _page(title, body, status=status)

    def cli_params(request: Request) -> dict[str, Any] | None:
        q = request.query_params
        port, challenge, state = q.get("cli_port"), q.get("cli_challenge"), q.get("cli_state")
        if port is None and challenge is None:
            return None
        if not (port and _PORT_RE.match(port) and int(port) <= 65535 and challenge and _CHALLENGE_RE.match(challenge)):
            raise HTTPException(400, "cli_port must be a port (0: show the code) and cli_challenge an S256 challenge")
        if state is not None and not _STATE_RE.match(state):
            raise HTTPException(400, "cli_state must be 16 to 128 URL-safe characters")
        return {"port": int(port), "challenge": challenge, "state": state}

    def confirm_page(person, cli: dict[str, Any]):
        """A CLI sign-in stops here: a link someone else crafted would otherwise hand them a token for you."""
        c = secrets.token_urlsafe(32)
        h.store.kv_set_json(f"{_KV}confirm:{_sha(c)}", {"person": person.id, "cli": cli,
                                                        "expires_at": now() + PENDING_TTL})
        where = "this machine's terminal" if cli["port"] else "a terminal where you'll paste a code"
        body = (f"<p>A hangar command line on {where} asked for a token for <strong>{html.escape(person.id)}"
                "</strong>.</p><p>Continue only if you just ran <code>hangar login --sso</code> yourself.</p>"
                '<form method="post" action="/auth/sso/cli/confirm">'
                f'<input type="hidden" name="c" value="{html.escape(c)}">'
                '<button class="af-btn af-btn--primary" type="submit">Give the command line a token</button></form>'
                '<p><a href="/">Cancel</a></p>')
        resp = _page("Sign in the command line?", body)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers.append("set-cookie", _cookie(CONFIRM_COOKIE, c, PENDING_TTL, "Strict"))
        resp.headers.append("set-cookie", _cookie(PENDING_COOKIE, "", 0, "Lax"))
        return resp

    async def finish(person, cli: dict[str, Any] | None, request: Request, *, confirmed: bool = False):
        if cli is not None and not confirmed:
            return confirm_page(person, cli)
        if cli is not None:
            code = "hgc_" + secrets.token_urlsafe(24)
            h.store.kv_set_json(f"{_KV}handoff:{_sha(code)}", {"person": person.id, "challenge": cli["challenge"],
                                                               "expires_at": now() + HANDOFF_TTL})
            sso.audit(person.id, "sso.signin", person.id, via="cli")
            if cli["port"]:  # a page, not a redirect: form-action 'self' would block a POST's redirect off-site
                back = {"code": code} | ({"state": cli["state"]} if cli.get("state") else {})
                resp = _page("Finish signing in", "<p>Handing the token to your command line…</p>",
                             refresh=f"http://127.0.0.1:{cli['port']}/sso?{urlencode(back)}")
            else:
                resp = _page("Finish signing in", "<p>Paste this code into <code>hangar login --sso</code>. It "
                             f"works once, within two minutes.</p><p><code>{html.escape(code)}</code></p>")
            resp.headers["Cache-Control"] = "no-store"
            resp.headers.append("set-cookie", _cookie(CONFIRM_COOKIE, "", 0, "Strict"))
            return resp
        raw = sso.mint_session(person)
        sso.audit(person.id, "sso.signin", person.id, via="console")
        resp = _page("Signed in", '<p>Opening hangar…</p><p><a href="/">Continue</a></p>', refresh="/")
        resp.headers.append("set-cookie", _cookie(SESSION_COOKIE, raw, sso.settings.sso_session_minutes * 60,
                                                  "Strict"))
        resp.headers.append("set-cookie", _cookie(PENDING_COOKIE, "", 0, "Lax"))
        return resp

    @app.get("/auth/sso/login")
    async def sso_login(request: Request):
        cli = cli_params(request)
        if sso.mode == "iap":
            try:
                claims = await sso.iap_claims(request.headers.get(IAP_HEADER))
                sub = str(claims.get("sub") or "")
                person = sso.admit(claims, bound=bound_to(h.store, IAP_ISSUER, sub))
                sso.bind(person, IAP_ISSUER, sub, str(claims["email"]), None)
            except (Refused, Unreachable) as e:
                return refused(e)
            return await finish(person, cli, request)
        try:
            meta = await sso.metadata()
        except Unreachable as e:
            return refused(e, status=503)
        state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
        h.store.kv_set_json(f"{_KV}pending:{_sha(state)}", {
            "nonce": nonce, "verifier": verifier, "browser": _sha(browser), "cli": cli,
            "expires_at": now() + PENDING_TTL})
        q = {"response_type": "code", "client_id": sso.settings.sso_client_id, "redirect_uri": sso.redirect_uri(),
             "scope": sso.settings.sso_scopes, "state": state, "nonce": nonce, "code_challenge": s256(verifier),
             "code_challenge_method": "S256"}
        extra = dict(p.split("=", 1) for p in sso.settings.sso_auth_params.split("&") if "=" in p)
        q.update({k: v for k, v in extra.items() if k not in q})
        sep = "&" if "?" in meta["authorization_endpoint"] else "?"
        resp = RedirectResponse(f"{meta['authorization_endpoint']}{sep}{urlencode(q)}", 302,
                                headers={"Cache-Control": "no-store"})
        resp.headers.append("set-cookie", _cookie(PENDING_COOKIE, browser, PENDING_TTL, "Lax"))
        return resp

    @app.get("/auth/sso/callback")
    async def sso_callback(request: Request):
        if sso.mode != "oidc":
            raise HTTPException(404)
        qp = request.query_params
        if qp.get("error"):
            return refused(Refused(f"the provider refused the sign-in ({qp.get('error')})"))
        state, code = qp.get("state") or "", qp.get("code") or ""
        key = f"{_KV}pending:{_sha(state)}"
        pending = h.store.kv_get_json(key) if state else None
        if pending:
            h.store.kv_delete(key)  # one use, whatever happens next
        browser = _cookies({"cookie": request.headers.get("cookie", "")}).get(PENDING_COOKIE, "")
        if not pending or float(pending.get("expires_at") or 0) <= now() or not code \
                or not secrets.compare_digest(pending.get("browser", ""), _sha(browser)):
            return refused(Refused("this sign-in expired or was started in another browser; start again"))
        try:
            tokens = await sso._token_call({"grant_type": "authorization_code", "code": code,
                                            "redirect_uri": sso.redirect_uri(), "code_verifier": pending["verifier"]})
            if not tokens.get("id_token"):
                raise Refused("the provider sent no id token")
            claims = await sso.id_claims(str(tokens["id_token"]), pending["nonce"])
            issuer, sub = str(claims["iss"]).rstrip("/"), str(claims.get("sub") or "")
            issuer = GOOGLE_ISSUERS[0] if issuer in GOOGLE_ISSUERS else issuer  # Google sends either form
            person = sso.admit(claims, bound=bound_to(h.store, issuer, sub))
            sso.bind(person, issuer, sub, str(claims["email"]), tokens.get("refresh_token"), verified=vouched(claims))
        except (Refused, Revoked, Unreachable) as e:
            return refused(e)
        return await finish(person, pending.get("cli"), request)

    @app.post("/auth/sso/cli/confirm")
    async def sso_cli_confirm(request: Request):
        from hangar import people

        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise HTTPException(403, "the confirmation must come from hangar's own page")
        form = parse_qs((await request.body())[:2000].decode("latin-1"))
        c = (form.get("c") or [""])[0]
        cookie = _cookies({"cookie": request.headers.get("cookie", "")}).get(CONFIRM_COOKIE, "")
        key = f"{_KV}confirm:{_sha(c)}"
        pending = h.store.kv_get_json(key) if c else None
        if pending:
            h.store.kv_delete(key)
        if not pending or float(pending.get("expires_at") or 0) <= now() or not cookie \
                or not secrets.compare_digest(cookie, c):
            return refused(Refused("this confirmation expired or came from another browser; run hangar login --sso "
                                   "again"))
        person = people.get(h.store, pending["person"])
        if person is None:
            return refused(Refused("that person no longer exists"))
        return await finish(person, pending["cli"], request, confirmed=True)

    @app.post("/auth/sso/logout")
    async def sso_logout(request: Request):
        if request.headers.get(CONSOLE_HEADER, "").lower() != "hangar":
            raise HTTPException(403, "a console request must come from the console")
        raw = _cookies({"cookie": request.headers.get("cookie", "")}).get(SESSION_COOKIE)
        p = h.auth.principal(sso.session_bearer(raw)) if raw else None
        if p is not None and p.name == SESSION_NAME:
            h.auth.revoke(p.id)
            sso.audit(p.person or "sso", "sso.signout", p.person)
        resp = JSONResponse({"signed_out": True})
        resp.headers.append("set-cookie", _cookie(SESSION_COOKIE, "", 0, "Strict"))
        return resp

    @app.post("/auth/sso/cli/token")
    async def sso_cli_token(body: CliToken):
        from hangar import people

        key = f"{_KV}handoff:{_sha(body.code)}"
        hand = h.store.kv_get_json(key)
        if hand:
            h.store.kv_delete(key)
        if not hand or float(hand.get("expires_at") or 0) <= now() \
                or not secrets.compare_digest(hand.get("challenge", ""), s256(body.verifier)):
            raise HTTPException(400, "that code expired, was used already or wasn't for this login; sign in again")
        person = people.get(h.store, hand["person"])
        if person is None:
            raise HTTPException(400, "that person no longer exists")
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", body.name).strip("-") or "cli-sso"
        raw, tok = sso.mint_cli(person, name)
        sso.audit(person.id, "token.create", tok.id, name=name, scopes=tok.scopes, via="sso")
        return {"token": raw, "person": person.id, "role": person.role, "id": tok.id}

    async def admin_only(request: Request) -> str:
        from hangar.auth import bearer_from_headers

        p = h.auth.principal(bearer_from_headers(request.headers))
        if p is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if not p.admin:
            raise HTTPException(403, "only an admin manages SSO bindings")
        return f"ui:{p.name}"

    @app.get("/api/sso/bindings")
    async def sso_bindings(request: Request):
        await admin_only(request)
        return [{k: v for k, v in b.items() if k != "refresh"} | {"refresh": bool(b.get("refresh"))}
                for b in bindings(h.store)]

    @app.delete("/api/sso/bindings/{person}")
    async def sso_unbind(person: str, request: Request):
        who = await admin_only(request)
        if binding(h.store, person) is None:
            raise HTTPException(404, f"{person} has no SSO binding")
        h.store.kv_delete(f"{_KV}binding:{person}")
        sso.audit(who, "sso.unbind", person)
        return {"unbound": person}

    @app.post("/api/sso/recheck")
    async def sso_recheck(request: Request):
        await admin_only(request)
        return await sso.recheck_all()


# ---------------------------------------------------------------------------- doctor
async def doctor_checks(h) -> list[dict[str, Any]]:
    """`hangar doctor`: SSO's own checks, when it is on."""
    from hangar.doctor import _check

    sso: SSO | None = getattr(h, "sso", None)
    if sso is None or not sso.on:
        return []
    out = []
    if sso.mode == "oidc":
        try:
            await sso.metadata()
            out.append(_check("sso provider", True, f"{sso.settings.sso_issuer} answers discovery"))
        except Unreachable as e:
            out.append(_check("sso provider", False, str(e), "check HANGAR_SSO_ISSUER and hangar's egress to it"))
        no_refresh = [b["person"] for b in bindings(h.store) if not b.get("disabled_at") and not b.get("refresh")]
        if no_refresh:
            out.append(_check("sso recheck", False, f"{len(no_refresh)} people have no refresh token, so hangar can't "
                              "ask the provider whether they left; their tokens stop after "
                              "HANGAR_SSO_MAX_STALE_MINUTES (service tokens after HANGAR_SSO_SERVICE_STALE_HOURS) "
                              "until they sign in again",
                              "ask for offline access: add offline_access to HANGAR_SSO_SCOPES (Okta, Entra) or "
                              "access_type=offline&prompt=consent to HANGAR_SSO_AUTH_PARAMS (Google)"))
        if halted := h.store.kv_get_json(f"{_KV}halted"):
            out.append(_check("sso recheck", False, f"the last recheck would have dropped {halted.get('drops')} of "
                              f"{halted.get('of')} people at once, so it revoked nothing and suspended them: "
                              f"{halted.get('why')}",
                              "check the client registration and HANGAR_SSO_REQUIRED_GROUP; a clean recheck or a "
                              "sign-in lifts a suspension, and DELETE /api/sso/bindings/<person> clears one"))
    suspended = [b["person"] for b in bindings(h.store) if b.get("suspended_at") and not b.get("disabled_at")]
    if suspended:
        out.append(_check("sso suspended", None, f"{len(suspended)} people are suspended until the provider confirms "
                          f"them: {', '.join(suspended[:10])}", "GET /api/sso/bindings shows why"))
    last = sso.break_glass_last()
    if last is not None and now() - last < 7 * 86400:
        out.append(_check("sso break-glass", None, f"the admin or bootstrap token was used "
                          f"{int((now() - last) // 60)} min ago while SSO is on",
                          "sign in through SSO; keep HANGAR_ADMIN_TOKEN and the bootstrap token for outages, and "
                          "rotate one that was used"))
    else:
        out.append(_check("sso break-glass", True, "the admin and bootstrap tokens haven't been used this week"))
    if ex := sorted(exempt(sso.settings)):
        from hangar import people as _people

        admins = [p for p in ex if (q := _people.get(h.store, p)) is not None and q.role == "admin"]
        out.append(_check("sso exempt", None if admins else True,
                          f"never gated: {', '.join(ex)}" + (f"; admins among them: {', '.join(admins)}" if admins
                                                              else ""),
                          "exempt only service people with no provider account, never an admin" if admins else None))
    if sso.mode == "iap":
        out.append(_check("sso recheck", True, "IAP gives no refresh token: a person's CLI tokens stop "
                          "HANGAR_SSO_MAX_STALE_MINUTES after their last console visit, and their jobs tokens "
                          "HANGAR_SSO_SERVICE_STALE_HOURS after it"))
    if not required(sso.settings):
        out.append(_check("sso required", None, "HANGAR_SSO_REQUIRED=false: people who never signed in through SSO "
                          "keep their pasted tokens, and leaving the provider doesn't stop them",
                          "unset HANGAR_SSO_REQUIRED once everyone has signed in once; list service people (bots, "
                          "CI) in HANGAR_SSO_EXEMPT_PEOPLE"))
    return out

