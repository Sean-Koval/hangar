"""Credential broker: registers credentials and materializes them inside sandboxes.

Materialization is provider-agnostic: it only needs `exec` and `write_file`, so the same
code path works for Docker, Agent Sandbox pods and E2B VMs.

* Env-style secrets (API keys, OAuth tokens) are written to ~/.hangar/env (0600), which
  every login/interactive shell and tmux session sources. They are never set as container
  env, so warm-pool sandboxes can be claimed without a restart.
* File-style secrets (e.g. ~/.codex/auth.json) are written to their target path.
* Exclusive credentials (rotating refresh tokens) are leased to one sandbox at a time and
  written back to the vault when the sandbox releases them, so the newest refresh token
  is never lost and two sandboxes never race on the same token.
"""

from __future__ import annotations

import re
import shlex
import threading
from typing import TYPE_CHECKING, Any

from hangar.harnesses import HARNESSES, slot_for
from hangar.models import Credential, CredentialKind, now
from hangar.store import Store
from hangar.vault import Vault

if TYPE_CHECKING:
    from hangar.models import Sandbox
    from hangar.providers.base import Provider


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CredentialError(RuntimeError):
    pass


class Broker:
    def __init__(self, store: Store, vault: Vault, user: str = "agent"):
        self.store = store
        self.vault = vault
        self.user = user
        self._lock = threading.Lock()
        self._secret_cache: list[str] | None = None

    # ------------------------------------------------------------------ redaction
    def _secrets(self) -> list[str]:
        if self._secret_cache is None:
            vals: list[str] = []
            for c in self.store.list(Credential):
                v = self.vault.get(f"cred:{c.name}")
                if not v:
                    continue
                text = v.decode(errors="ignore").strip()
                vals.append(text)
                if c.kind == CredentialKind.AUTH_FILE:  # also mask the tokens inside JSON auth files
                    vals += re.findall(r'"[^"]*token[^"]*"\s*:\s*"([^"]{12,})"', text)
            self._secret_cache = sorted({x for x in vals if len(x) >= 8}, key=len, reverse=True)
        return self._secret_cache

    def redact(self, obj: Any) -> Any:
        secrets_ = self._secrets()
        if not secrets_:
            return obj
        if isinstance(obj, str):
            for sec in secrets_:
                obj = obj.replace(sec, "***")
            return obj
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        return obj

    @property
    def home(self) -> str:
        return f"/home/{self.user}"

    # ------------------------------------------------------------------ registry
    def put(
        self,
        name: str,
        harness: str,
        kind: CredentialKind,
        value: str | bytes,
        description: str | None = None,
        env_var: str | None = None,
        file_path: str | None = None,
    ) -> Credential:
        if not NAME_RE.match(name):
            raise CredentialError(f"invalid credential name '{name}': letters, digits, '.', '_', '-'")
        slot = slot_for(harness, kind)
        # `webhook` credentials are signing keys the control plane uses (job callbacks); never put in sandboxes.
        if slot is None and not (env_var or file_path) and harness != "webhook":
            known = ", ".join(f"{s.kind.value}" for s in HARNESSES[harness].credentials) if harness in HARNESSES else ""
            raise CredentialError(
                f"harness '{harness}' has no slot for kind '{kind.value}'"
                + (f" (supports: {known})" if known else "")
                + "; pass env_var or file_path to define a custom mapping"
            )
        existing = self.store.get(Credential, name)
        cred = Credential(
            name=name,
            harness=harness,
            kind=kind,
            description=description,
            exclusive=bool(slot and slot.exclusive),
            env_var=env_var or (slot.env_var if slot else None),
            file_path=file_path or (slot.file_path if slot else None),
            created_at=existing.created_at if existing else now(),
            leased_to=existing.leased_to if existing else None,
        )
        self.vault.put(f"cred:{name}", value)
        self.store.put(cred)
        self._secret_cache = None
        return cred

    def list(self) -> list[Credential]:
        return sorted(self.store.list(Credential), key=lambda c: c.name)

    def get(self, name: str) -> Credential:
        cred = self.store.get(Credential, name)
        if not cred:
            raise CredentialError(f"unknown credential '{name}'")
        return cred

    def delete(self, name: str) -> None:
        self.store.delete(Credential, name)
        self.vault.delete(f"cred:{name}")
        self._secret_cache = None

    # ------------------------------------------------------------------ leasing
    def acquire(self, names: list[str], sandbox_id: str) -> list[Credential]:
        creds = [self.get(n) for n in names]
        with self._lock:
            for c in creds:
                if c.exclusive and c.leased_to and c.leased_to != sandbox_id:
                    raise CredentialError(
                        f"credential '{c.name}' is exclusive (rotating refresh token) and is in use by "
                        f"sandbox {c.leased_to}. Release it there first, or log in a second account."
                    )
            for c in creds:
                if c.exclusive:
                    c.leased_to = sandbox_id
                    self.store.put(c)
        return creds

    async def release(self, sandbox: Sandbox, provider: Provider, write_back: bool = True) -> None:
        """Return exclusive leases, copying rotated auth files back into the vault first."""
        for c in self.store.list(Credential):
            if c.leased_to != sandbox.id:
                continue
            data = None
            if write_back and c.kind == CredentialKind.AUTH_FILE and c.file_path:
                try:
                    data = await provider.read_file(sandbox, self._abs(c.file_path))
                except Exception:  # sandbox may already be gone; keep the vault copy
                    data = None
            with self._lock:
                # Re-read after the await: the lease may have been force-released and re-issued meanwhile.
                cur = self.store.get(Credential, c.name)
                if cur is None or cur.leased_to != sandbox.id:
                    continue
                if data and data != self.vault.get(f"cred:{c.name}"):
                    self.vault.put(f"cred:{c.name}", data)
                    cur.updated_at = now()
                    self._secret_cache = None
                cur.leased_to = None
                self.store.put(cur)

    def force_release(self, name: str) -> Credential:
        c = self.get(name)
        c.leased_to = None
        self.store.put(c)
        return c

    # ------------------------------------------------------------------ materialize
    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else f"{self.home}/{path}"

    def secret_text(self, name: str) -> str:
        """A credential's value as text, for `secrets:` env mappings and MCP config resolution."""
        self.get(name)
        value = self.vault.get(f"cred:{name}")
        if value is None:
            raise CredentialError(f"credential '{name}' has no stored secret")
        return value.decode().strip()

    async def inject(self, sandbox: Sandbox, provider: Provider, creds: list[Credential],
                     extra_env: dict[str, str] | None = None, prelude: list[str] | None = None) -> dict[str, str]:
        """Materialize credentials. `extra_env` adds plain exports (harness env, `secrets:` mappings) and
        `prelude` raw shell lines (PATH) to ~/.hangar/env. Returns every env var set, for config rendering."""
        env: dict[str, str] = dict(extra_env or {})
        post: list[str] = []
        for c in creds:
            value = self.vault.get(f"cred:{c.name}")
            if value is None:
                raise CredentialError(f"credential '{c.name}' has no stored secret")
            slot = slot_for(c.harness, c.kind)
            if c.env_var:
                env[c.env_var] = value.decode().strip()
            if c.file_path:
                await provider.write_file(sandbox, self._abs(c.file_path), value, mode=0o600)
            if slot and slot.post_inject:
                post.append(slot.post_inject)
        if env or prelude:
            existing = await provider.read_file(sandbox, f"{self.home}/.hangar/env") or b""
            kept = [ln for ln in existing.decode().splitlines()
                    if ln not in (prelude or []) and not any(ln.startswith(f"export {v}=") for v in env)]
            lines = [*(prelude or []), *kept, *(f"export {k}={shlex.quote(v)}" for k, v in env.items())]
            content = "\n".join(lines) + "\n"
            await provider.write_file(sandbox, f"{self.home}/.hangar/env", content.encode(), mode=0o600)
        for script in post:
            res = await provider.exec(sandbox, script, timeout=60)
            if res.exit_code != 0:
                raise CredentialError(f"post-inject step failed ({res.exit_code}): {res.stderr[-500:]}")
        return env
