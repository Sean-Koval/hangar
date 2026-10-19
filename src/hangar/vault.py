"""Credential vault: Fernet-encrypted secret blobs in the state store.

The key comes from HANGAR_SECRET_KEY (recommended in clusters: mount it from a Kubernetes
Secret / Secret Manager) or is generated once into <data_dir>/secret.key with mode 0600.
Secret values never appear in API responses, MCP tool results, logs or audit events.

--- durable: either can hold several keys, newest first: the newest encrypts, any key not yet retired decrypts
(keys.py), so the key can be rotated (`hangar admin rotate-key`).

--- company-deploy: a credential's value may instead be a reference to a mounted file, so it comes from the company's
secret manager (External Secrets -> a Kubernetes Secret -> files under HANGAR_SECRETS_DIR, by default
/var/run/hangar-secrets) and is never typed into hangar or kept in its database. Store the value
`hangar-secret-ref:file:/var/run/hangar-secrets/<name>` (reference(), which `hangar apply` sends for
`source: file:...`): the vault seals the reference, and get() reads the file each time, so a rotation in the secret
manager reaches hangar without a restart. The file must be inside the secrets directory and be named after the
credential, so a credential can only ever reach the file meant for it. A missing file reads as no value.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import InvalidToken, MultiFernet

from hangar import keys
from hangar.store import Store

log = logging.getLogger(__name__)

REF_PREFIX = "hangar-secret-ref:"  # a stored value that starts with this is a reference, not a secret
DEFAULT_SECRETS_DIR = "/var/run/hangar-secrets"
MAX_MOUNTED_BYTES = 1 << 20  # a credential file is small; anything bigger is a mistake, not a secret


class VaultReferenceError(ValueError):
    """A reference hangar refuses to store (shown to the caller: it names the path, never a value)."""


def reference(source: str) -> str:
    """The credential value that stands for `source` (file:/var/run/hangar-secrets/<name>)."""
    return REF_PREFIX + source


def secrets_dir() -> Path:
    return Path(os.environ.get("HANGAR_SECRETS_DIR") or DEFAULT_SECRETS_DIR)


def load_or_create_key(explicit: str | None, data_dir: Path) -> bytes:
    """The newest key (keys.load_keys): what new signatures and derived keys use. Code that checks a signature
    should accept any of keys.verify_keys(settings), so a rotation doesn't break what was signed before it."""
    return keys.load_keys(explicit, data_dir)[0]


class Vault:
    def __init__(self, store: Store, key: bytes | keys.KeyRing, mounted: Path | None = None):
        self._store = store
        self.ring = key if isinstance(key, keys.KeyRing) else keys.KeyRing.of(key)
        self._mounted = mounted  # --- company-deploy: the secrets directory; None reads HANGAR_SECRETS_DIR

    @property
    def _fernet(self) -> MultiFernet:
        return self.ring.fernet  # rebuilt when a key is added or retired

    def put(self, name: str, value: bytes | str) -> None:
        data = value.encode() if isinstance(value, str) else value
        if data.startswith(REF_PREFIX.encode()):  # --- company-deploy: seal the reference, checked, not a value
            data = reference(f"file:{self._check_reference(name, data)}").encode()
        self._store.put_secret(name, self._fernet.encrypt(data))

    def _raw(self, name: str) -> bytes | None:
        blob = self._store.get_secret(name)
        if blob is None:
            return None
        try:
            return self._fernet.decrypt(blob)
        except InvalidToken as e:
            raise RuntimeError(
                f"cannot decrypt secret '{name}': HANGAR_SECRET_KEY does not match the key it was stored with"
            ) from e

    def get(self, name: str) -> bytes | None:
        data = self._raw(name)
        if data is not None and data.startswith(REF_PREFIX.encode()):  # --- company-deploy
            return self._read_mounted(name, data)
        return data

    # ------------------------------------------------------------------ --- company-deploy: mounted credentials
    @property
    def mounted_dir(self) -> Path:
        return self._mounted or secrets_dir()

    def source(self, name: str) -> str | None:
        """Where a stored value comes from: "file:<path>" for a reference, "vault" for a stored value, None for
        none. Reads no file and returns no value."""
        data = self._raw(name)
        if data is None:
            return None
        return data[len(REF_PREFIX):].decode() if data.startswith(REF_PREFIX.encode()) else "vault"

    def _check_reference(self, name: str, data: bytes) -> Path:
        """The file a reference names, after the checks: a credential's own (cred:<name>), a file: source, inside the
        secrets directory and named after the credential. It needn't exist yet (External Secrets may sync later)."""
        src = data[len(REF_PREFIX):].decode(errors="replace").strip()
        if not name.startswith("cred:"):
            raise VaultReferenceError("only credentials can reference a mounted file")
        cred = name.removeprefix("cred:")
        if not src.startswith("file:"):
            raise VaultReferenceError(f"credential '{cred}': a source is file:<path>, not '{src.split(':')[0]}:'")
        path = Path(src.removeprefix("file:"))
        root = self.mounted_dir
        if not path.is_absolute() or ".." in path.parts or path.parent != root:
            raise VaultReferenceError(f"credential '{cred}': the source must be {root}/{cred}, not {path}")
        if path.name != cred:
            raise VaultReferenceError(f"credential '{cred}': the mounted file must be named after the credential "
                                      f"({root}/{cred}), not {path.name}")
        return path

    def _read_mounted(self, name: str, data: bytes) -> bytes | None:
        """The referenced file's bytes, read now; None (logged by path, never by content) when it can't be read."""
        try:
            path = self._check_reference(name, data)
        except VaultReferenceError as e:  # the secrets directory changed since it was stored
            log.warning("%s", e)
            return None
        try:
            root = os.path.realpath(self.mounted_dir)
            real = os.path.realpath(path)  # Kubernetes Secret volumes: <key> -> ..data/<key>, inside the directory
            if os.path.commonpath([root, real]) != root:
                log.warning("credential %s: %s points outside %s; not read", name, path, root)
                return None
            with open(real, "rb") as f:
                value = f.read(MAX_MOUNTED_BYTES + 1)
        except FileNotFoundError:
            log.warning("credential %s: %s is not mounted", name.removeprefix("cred:"), path)
            return None
        except OSError as e:
            log.warning("credential %s: can't read %s (%s)", name.removeprefix("cred:"), path, e.strerror)
            return None
        if len(value) > MAX_MOUNTED_BYTES:
            log.warning("credential %s: %s is over %d bytes; not read", name.removeprefix("cred:"), path,
                        MAX_MOUNTED_BYTES)
            return None
        return value

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
