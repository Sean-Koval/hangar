"""Credential vault: Fernet-encrypted secret blobs in the state store.

The key comes from HANGAR_SECRET_KEY (recommended in clusters: mount it from a Kubernetes
Secret / Secret Manager) or is generated once into <data_dir>/secret.key with mode 0600.
Secret values never appear in API responses, MCP tool results, logs or audit events.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from hangar.store import Store


def load_or_create_key(explicit: str | None, data_dir: Path) -> bytes:
    if explicit:
        return explicit.encode()
    key_file = data_dir / "secret.key"
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class Vault:
    def __init__(self, store: Store, key: bytes):
        self._store = store
        self._fernet = Fernet(key)

    def put(self, name: str, value: bytes | str) -> None:
        data = value.encode() if isinstance(value, str) else value
        self._store.put_secret(name, self._fernet.encrypt(data))

    def get(self, name: str) -> bytes | None:
        blob = self._store.get_secret(name)
        if blob is None:
            return None
        try:
            return self._fernet.decrypt(blob)
        except InvalidToken as e:
            raise RuntimeError(
                f"cannot decrypt secret '{name}': HANGAR_SECRET_KEY does not match the key it was stored with"
            ) from e

    def get_text(self, name: str) -> str | None:
        v = self.get(name)
        return v.decode() if v is not None else None

    def delete(self, name: str) -> None:
        self._store.delete_secret(name)
