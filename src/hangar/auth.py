"""Bearer-token auth shared by the MCP endpoint, REST API, websocket terminal and dashboard.

Tokens are random 32-byte secrets shown once; only a SHA-256 hash is stored. Codex and
Claude Code send them via `bearer_token_env_var` / `--header "Authorization: Bearer ..."`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import NamedTuple

from hangar.models import ApiToken, now
from hangar.store import Store

PREFIX = "hgr_"
SCOPES = ("admin", "jobs")  # admin: everything; jobs: only /api/jobs* (service callers)
_RESERVED_NAMES = {"admin", "system"}


class Principal(NamedTuple):
    name: str  # display name (not unique)
    id: str  # token id: the ownership key
    scopes: list[str]
    callback_secret: str | None = None

    @property
    def admin(self) -> bool:
        return "admin" in self.scopes


def check_token_name(name: str) -> None:
    if not name or ":" in name or name.lower() in _RESERVED_NAMES or len(name) > 100:
        raise ValueError(f"token name '{name}' is not allowed (no ':', not 'admin' or 'system', ≤100 chars)")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenAuth:
    def __init__(self, store: Store, admin_token: str | None = None):
        self.store = store
        self._admin_hash = hash_token(admin_token) if admin_token else None
        self._tickets: dict[str, tuple[str, float]] = {}

    # Browsers can't set headers on WebSocket/EventSource, so the dashboard trades its token
    # for a single-use ticket that lives 60s. Tickets (unlike tokens) are safe to put in URLs/logs.
    def issue_ticket(self, actor: str, ttl: float = 60) -> str:
        t = time.time()
        self._tickets = {k: v for k, v in self._tickets.items() if v[1] > t}
        ticket = "tkt_" + secrets.token_urlsafe(24)
        self._tickets[ticket] = (actor, t + ttl)
        return ticket

    def redeem_ticket(self, ticket: str | None) -> str | None:
        if not ticket:
            return None
        actor, exp = self._tickets.pop(ticket, (None, 0.0))
        return actor if actor and exp > time.time() else None

    def create(self, name: str, scopes: list[str] | None = None,
               callback_secret: str | None = None) -> tuple[str, ApiToken]:
        check_token_name(name)
        raw = PREFIX + secrets.token_urlsafe(32)
        tok = ApiToken(name=name, token_hash=hash_token(raw), scopes=scopes or ["admin"],
                       callback_secret=callback_secret)
        self.store.put(tok)
        return raw, tok

    def list(self) -> list[ApiToken]:
        return sorted(self.store.list(ApiToken), key=lambda t: t.created_at)

    def revoke(self, token_id: str) -> None:
        self.store.delete(ApiToken, token_id)

    def identify(self, raw: str | None) -> tuple[str, str] | None:
        """(actor name, token id) for a valid *admin* token. The admin token's id is 'admin'.

        Used for OAuth consent: an MCP grant carries full tool access, so narrower tokens are refused."""
        p = self.principal(raw)
        return (p.name, p.id) if p and p.admin else None

    def principal(self, raw: str | None) -> Principal | None:
        if not raw:
            return None
        h = hash_token(raw)
        if self._admin_hash and hmac.compare_digest(h, self._admin_hash):
            return Principal("admin", "admin", ["admin"])
        for tok in self.store.list(ApiToken):
            if hmac.compare_digest(h, tok.token_hash):
                tok.last_used = now()
                self.store.put(tok)
                return Principal(tok.name, tok.id, list(tok.scopes), tok.callback_secret)
        return None

    def exists(self, token_id: str | None) -> bool:
        if token_id == "admin":
            return self._admin_hash is not None
        return bool(token_id) and self.store.get(ApiToken, token_id) is not None

    def verify_scopes(self, raw: str | None) -> tuple[str, list[str]] | None:
        """(actor name, scopes) for a valid token, else None."""
        p = self.principal(raw)
        return (p.name, p.scopes) if p else None

    def verify(self, raw: str | None, scope: str = "admin") -> str | None:
        """Return the actor name for a valid token that holds `admin` (or `scope`), else None."""
        res = self.verify_scopes(raw)
        if res is None:
            return None
        name, scopes = res
        return name if "admin" in scopes or scope in scopes else None

    def ensure_bootstrap(self) -> str | None:
        """If no admin token is configured and no tokens exist, mint one and return it (shown once)."""
        if self._admin_hash or self.store.list(ApiToken):
            return None
        raw, _ = self.create("bootstrap-admin")
        return raw


def bearer_from_headers(headers) -> str | None:
    auth = headers.get("authorization") or headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None
