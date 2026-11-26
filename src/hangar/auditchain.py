"""--- KAN-296: the audit chain's HMACs (store.py writes them, datahandling.verify_chain checks them). The key is in
the vault (datahandling.audit_key): whoever holds only the database can't rebuild the chain after an edit, nor confirm
a guessed name in a redacted row against the digest it kept."""

from __future__ import annotations

import hashlib
import hmac


def audit_digest(key: bytes, data: str) -> str:
    return hmac.new(key, data.encode(), hashlib.sha256).hexdigest()


def audit_link(key: bytes, prev: str, row_id: int, digest: str) -> str:
    """--- KAN-296: one row's link in the audit chain."""
    return hmac.new(key, f"{prev}\n{row_id}\n{digest}".encode(), hashlib.sha256).hexdigest()


REDACTION_V2 = "v2:"


def audit_redaction(key: bytes, row_id: int, digest: str, data: str) -> str:
    """--- KAN-296: what a purge wrote in place of a row's JSON, so a row marked redacted can't hide an edit.
    --- KAN-553: bound to the row's id and the digest of what it first held, so a redacted row's JSON and HMAC can't
    be copied over another row (erasing it) or swapped with another redacted row."""
    msg = f"redacted\n{row_id}\n{digest}\n".encode() + data.encode()
    return REDACTION_V2 + hmac.new(key, msg, hashlib.sha256).hexdigest()


def audit_redaction_v1(key: bytes, data: str) -> str:
    """The first form (KAN-296), bound to the JSON alone: only `Store.set_audit_key` reads it, to rebind such rows."""
    return hmac.new(key, b"redacted\n" + data.encode(), hashlib.sha256).hexdigest()
