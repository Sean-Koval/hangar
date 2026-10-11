"""Bearer-token auth shared by the MCP endpoint, REST API, websocket terminal and dashboard.

Tokens are random 32-byte secrets shown once; only a SHA-256 hash is stored. Codex and
Claude Code send them via `bearer_token_env_var` / `--header "Authorization: Bearer ..."`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from hangar.models import ApiToken, now
from hangar.store import Store

PREFIX = "hgr_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenAuth:
    def __init__(self, store: Store, admin_token: str | None = None):
        self.store = store
        self._admin_hash = hash_token(admin_token) if admin_token else None

    def create(self, name: str, scopes: list[str] | None = None) -> tuple[str, ApiToken]:
        raw = PREFIX + secrets.token_urlsafe(32)
        tok = ApiToken(name=name, token_hash=hash_token(raw), scopes=scopes or ["admin"])
        self.store.put(tok)
        return raw, tok

    def list(self) -> list[ApiToken]:
        return sorted(self.store.list(ApiToken), key=lambda t: t.created_at)

    def revoke(self, token_id: str) -> None:
        self.store.delete(ApiToken, token_id)

    def verify(self, raw: str | None) -> str | None:
        """Return the actor name for a valid token, else None."""
        if not raw:
            return None
        h = hash_token(raw)
        if self._admin_hash and hmac.compare_digest(h, self._admin_hash):
            return "admin"
        for tok in self.store.list(ApiToken):
            if hmac.compare_digest(h, tok.token_hash):
                tok.last_used = now()
                self.store.put(tok)
                return tok.name
        return None

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
