"""Credential vault: Fernet-encrypted secret blobs in the state store.

The key comes from HANGAR_SECRET_KEY (recommended in clusters: mount it from a Kubernetes
Secret / Secret Manager) or is generated once into <data_dir>/secret.key with mode 0600.
Secret values never appear in API responses, MCP tool results, logs or audit events.

--- durable: either can hold several keys, newest first: the newest encrypts, any key not yet retired decrypts
(keys.py), so the key can be rotated (`hangar admin rotate-key`).
"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import InvalidToken, MultiFernet

from hangar import keys
from hangar.store import Store


def load_or_create_key(explicit: str | None, data_dir: Path) -> bytes:
    """The newest key (keys.load_keys): what new signatures and derived keys use. Code that checks a signature
    should accept any of keys.verify_keys(settings), so a rotation doesn't break what was signed before it."""
    return keys.load_keys(explicit, data_dir)[0]


class Vault:
    def __init__(self, store: Store, key: bytes | keys.KeyRing):
        self._store = store
        self.ring = key if isinstance(key, keys.KeyRing) else keys.KeyRing.of(key)

    @property
    def _fernet(self) -> MultiFernet:
        return self.ring.fernet  # rebuilt when a key is added or retired

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

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt an arbitrary blob with the vault key (session checkpoints, spec 0010)."""
        return self._fernet.encrypt(data)

    def decrypt(self, blob: bytes) -> bytes:
        return self._fernet.decrypt(blob)

    def delete(self, name: str) -> None:
        self._store.delete_secret(name)
