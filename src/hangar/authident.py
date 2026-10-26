"""Whose account a harness login file signs in to (KAN-168, security review M-3).

On release, an exclusive `auth_file` lease is read back from the sandbox and stored (broker.release), so a rotated
refresh token isn't lost. The sandbox decides what that file holds, so before storing it hangar checks that it signs
in to the same account as the stored one. `account(data)` reads the identity a login file names:

* **Codex** (`~/.codex/auth.json`): `tokens.account_id`, and the claims of `tokens.id_token` and
  `tokens.access_token`: issuer, subject, and the ChatGPT account and user ids under `https://api.openai.com/auth`.
* **Gemini** (`~/.gemini/oauth_creds.json`) and any other file with a JWT `id_token` (or `idToken`) anywhere in it:
  that token's issuer and subject (Google's stable user id), and its `email`.
* Plain `account_id` / `accountId` / `user_id` / `userId` fields anywhere in the file.

JWT claims are read **without verifying the signature**: this compares identities, it doesn't trust them. Anything
else (pi's opaque Anthropic tokens, an API key, a file that isn't JSON) names no account, and `account` returns None.
"""

from __future__ import annotations

import base64
import json
from typing import Any

OPENAI_AUTH = "https://api.openai.com/auth"
_ID_FIELDS = ("account_id", "accountId", "user_id", "userId")
_JWT_FIELDS = ("id_token", "idToken", "access_token", "accessToken")
_MAX_DEPTH = 6
CONFLICT = "\0conflict"  # one file naming two values for a field (an id token and an access token that disagree)


def _set(ident: dict[str, str], key: str, value: str) -> None:
    ident[key] = value if ident.get(key, value) == value else CONFLICT


def _claims(token: Any) -> dict[str, Any] | None:
    """A JWT's payload, unverified; None if `token` isn't one."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    try:
        part = token.split(".")[1]
        out = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, TypeError):
        return None
    return out if isinstance(out, dict) else None


def _from_claims(c: dict[str, Any], ident: dict[str, str]) -> None:
    iss, sub = c.get("iss"), c.get("sub")
    if isinstance(iss, str) and isinstance(sub, str) and sub:
        _set(ident, f"sub@{iss}", sub)
    if isinstance(c.get("email"), str) and c["email"]:
        _set(ident, "email", c["email"].lower())
    oa = c.get(OPENAI_AUTH)
    if isinstance(oa, dict):
        for k in ("chatgpt_account_id", "chatgpt_user_id", "user_id"):
            if isinstance(oa.get(k), str) and oa[k]:
                _set(ident, f"openai.{k}", oa[k])


def _walk(obj: Any, ident: dict[str, str], path: str, depth: int) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{path}.{k}" if path else str(k)
            if k in _JWT_FIELDS and (c := _claims(v)) is not None:
                _from_claims(c, ident)
            elif k in _ID_FIELDS and isinstance(v, (str, int)) and str(v):
                _set(ident, key, str(v))
            else:
                _walk(v, ident, key, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, ident, f"{path}[{i}]", depth + 1)


def account(data: bytes | str | None) -> dict[str, str] | None:
    """The identity fields a login file names, or None when it names none (or isn't JSON)."""
    if not data:
        return None
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    ident: dict[str, str] = {}
    _walk(obj, ident, "", 0)
    return ident or None


def same(stored: dict[str, str] | None, new: dict[str, str] | None) -> bool | None:
    """True when `new` names the stored login's account, False when it names another, None when that can't be told.

    `new` must carry every identity field the stored login has, with the same value: a refresh may add a claim, but
    a file that drops one (keeps `account_id`, loses the id token) could be anyone's. A file that disagrees with
    itself is never the same account."""
    if not stored or not new:
        return None
    if CONFLICT in stored.values() or CONFLICT in new.values():
        return False
    if any(new.get(k, v) != v for k, v in stored.items()):
        return False
    return all(k in new for k in stored)


_ACCESS_FIELDS = ("id_token", "idToken", "access_token", "accessToken", "access")
_REFRESH_FIELDS = ("refresh_token", "refreshToken", "refresh")


def _values(obj: Any, fields: tuple[str, ...], out: dict[str, str], path: str = "", depth: int = 0) -> dict[str, str]:
    if depth > _MAX_DEPTH:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{path}.{k}" if path else str(k)
            if k in fields and isinstance(v, str) and v:
                out[key] = v
            else:
                _values(v, fields, out, key, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _values(v, fields, out, f"{path}[{i}]", depth + 1)
    return out


def swapped(stored: bytes | str | None, new: bytes | str | None) -> bool:
    """True when `new` keeps every access and id token of `stored` byte for byte but carries a different refresh
    token (KAN-168). A real refresh issues a new access token with the new refresh token; a file that only swapped
    the refresh token is the copy-and-swap a sandbox would return to move the login to another account. This catches
    that copy, not a sandbox that refreshes the stored login first: see docs/security/review-1.0.md."""
    try:
        a, b = json.loads(stored or ""), json.loads(new or "")
    except (ValueError, UnicodeDecodeError, TypeError):
        return False
    access_a, access_b = _values(a, _ACCESS_FIELDS, {}), _values(b, _ACCESS_FIELDS, {})
    refresh_a, refresh_b = _values(a, _REFRESH_FIELDS, {}), _values(b, _REFRESH_FIELDS, {})
    return bool(access_a) and access_a == access_b and bool(refresh_b) and refresh_a != refresh_b
