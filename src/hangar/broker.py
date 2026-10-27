"""Credential broker: registers credentials and materializes them inside sandboxes.

Materialization is provider-agnostic: it only needs `exec` and `write_file`, so the same
code path works for Docker, Agent Sandbox pods and E2B VMs.

* Env-style secrets (API keys, OAuth tokens) are written to ~/.hangar/env (0600), which
  every login/interactive shell and tmux session sources. They are never set as container
  env, so warm-pool sandboxes can be claimed without a restart.
* File-style secrets (e.g. ~/.codex/auth.json) are written to their target path.
* Exclusive credentials (rotating refresh tokens) are leased to one sandbox at a time and
  written back to the vault when the sandbox releases them, so the newest refresh token
  is never lost and two sandboxes never race on the same token. A team credential's file is never written back, and
  a person's own only when it signs in to the same account as the stored one (KAN-168, authident.py); a refused file
  is audited.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
from typing import TYPE_CHECKING, Any

import hangar.identity as identity  # --- identity
from hangar.harnesses import HARNESSES, slot_for
from hangar.models import AuditEvent, Credential, CredentialKind, now
from hangar.store import Store
from hangar.vault import REF_PREFIX, Vault

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.models import Sandbox
    from hangar.providers.base import Provider


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# --- overnight/sweep: the sandbox decides how big these files are, so hangar reads no more than this of them
AUTH_FILE_LIMIT = 1024 * 1024  # a harness login file (~/.codex/auth.json and the like are a few KB)
ENV_FILE_LIMIT = 1024 * 1024  # ~/.hangar/env: hangar's exports and PATH lines
log = logging.getLogger("hangar.broker")


def _is_reference(value: str | bytes) -> bool:
    """--- company-deploy: the value is a reference to a mounted file (the vault seals it as one), not a secret."""
    return (value.encode() if isinstance(value, str) else value).startswith(REF_PREFIX.encode())


class CredentialError(RuntimeError):
    pass


class Broker:
    def __init__(self, store: Store, vault: Vault, user: str = "agent"):
        self.store = store
        self.vault = vault
        self.user = user
        self._lock = threading.Lock()
        self._secret_cache: list[str] | None = None
        self._redactor: Any = None  # --- egress: redaction.Redactor over _secret_cache, rebuilt when it changes
        self.settings: Settings | None = None  # --- identity: names the control plane's own credentials (Fleet sets it)

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
                if c.kind == CredentialKind.GITHUB_APP:  # workstream F: the App's private key, escaped and raw
                    import json

                    vals += re.findall(r'"private_key"\s*:\s*"([^"]{12,})"', text)
                    try:
                        vals.append(str(json.loads(text)["private_key"]).strip())
                    except (ValueError, KeyError, TypeError):
                        pass
            self._secret_cache = sorted({x for x in vals if len(x) >= 8}, key=len, reverse=True)
        return self._secret_cache

    def redact(self, obj: Any) -> Any:
        secrets_ = self._secrets()
        if not secrets_:
            return obj
        if isinstance(obj, str):  # --- egress: verbatim, and common encodings of long values (redaction.py)
            if self._redactor is None or self._redactor.src is not secrets_:
                from hangar.redaction import Redactor

                self._redactor = Redactor(secrets_)
            return self._redactor.text(obj)
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
        exposure: str | None = None,  # --- identity: "sandbox" | "control_plane"; unset keeps the stored choice
        person: str | None = None,  # --- identity: whose own credential it is; unset keeps the stored owner
        team: bool = False,  # --- teamcreds: a team credential (no person), whoever owned it before
        reference_ok: bool = False,  # --- company-deploy: an admin may store a reference to a mounted file
    ) -> Credential:
        if not NAME_RE.match(name):
            raise CredentialError(f"invalid credential name '{name}': letters, digits, '.', '_', '-'")
        # --- company-deploy: a value naming a mounted file (vault.REF_PREFIX) makes a file-backed credential. Only an
        # admin creates one (the API passes reference_ok for admins): otherwise a member could point a credential of
        # their own at a mounted file meant for something else, before an admin declares that name
        is_ref = _is_reference(value)
        if is_ref and not reference_ok:
            raise identity.Forbidden(f"credential '{name}': only an admin can make a credential that reads a mounted "
                                     "file (hangar apply with an admin token)")
        if exposure not in (None, "sandbox", "control_plane"):  # --- identity
            raise CredentialError("exposure must be sandbox or control_plane")
        slot = slot_for(harness, kind)
        # --- workstream F: a GitHub App key is used by the git gateway only and never goes into a sandbox.
        if kind == CredentialKind.GITHUB_APP:
            from hangar.gateway import GatewayError, parse_app_credential

            if env_var or file_path:
                raise CredentialError("github_app credentials can't be mapped into sandboxes")
            try:
                # --- company-deploy: a mounted App key may not be synced yet; it is parsed when used, and `hangar
                # doctor` checks it (Gateway.doctor), like any other App key
                if not is_ref:
                    parse_app_credential(value)
            except GatewayError as e:
                raise CredentialError(str(e)) from None
        # `webhook` credentials are signing keys the control plane uses (job callbacks); never put in sandboxes.
        elif harness == "webhook" and (env_var or file_path):  # --- identity
            raise CredentialError("webhook credentials are control-plane only and can't be mapped into sandboxes")
        elif slot is None and not (env_var or file_path) and harness != "webhook":
            known = ", ".join(f"{s.kind.value}" for s in HARNESSES[harness].credentials) if harness in HARNESSES else ""
            raise CredentialError(
                f"harness '{harness}' has no slot for kind '{kind.value}'"
                + (f" (supports: {known})" if known else "")
                + "; pass env_var or file_path to define a custom mapping"
            )
        existing = self.store.get(Credential, name)
        leased = existing.leased_to if existing else None
        if leased:  # --- KAN-178: a new value while a sandbox holds the old one (see _drop_lease)
            leased = self._drop_lease(name, leased, value)
        cred = Credential(
            name=name,
            harness=harness,
            kind=kind,
            description=description,
            exclusive=bool(slot and slot.exclusive),
            env_var=env_var or (slot.env_var if slot else None),
            file_path=file_path or (slot.file_path if slot else None),
            created_at=existing.created_at if existing else now(),
            leased_to=leased,
            exposure=exposure or (existing.exposure if existing else None),  # --- identity
            person=None if team else person or (existing.person if existing else None),  # --- identity, teamcreds
        )
        self._check_exposure_choice(cred)  # --- identity
        self.vault.put(f"cred:{name}", value)
        self.store.put(cred)
        self._secret_cache = None
        return cred

    def _drop_lease(self, name: str, sandbox_id: str, value: str | bytes) -> str | None:
        """--- KAN-178: storing a credential anew while a sandbox holds its lease ends that lease, so the sandbox's
        release never writes its old login back over the new one. Otherwise a fresh `codex login`, stored to
        replace a dead one while a job's sandbox kept for follow-ups still holds the dead file, is overwritten by
        that file when the sandbox ends (same account, new access token: nothing refuses it). The sandbox keeps
        running with what it has; the next one gets the new value. The same value stored again (an owner or
        exposure change) keeps the lease."""
        data = value.encode() if isinstance(value, str) else value
        if self.vault.get(f"cred:{name}") == data:
            return sandbox_id
        log.warning("credential %s stored anew while sandbox %s held it: its lease ends and it won't be written "
                    "back", name, sandbox_id)
        self.store.audit(AuditEvent(actor="system", action="credential.lease_dropped", target=name,
                                    detail={"sandbox": sandbox_id, "reason": "stored anew while leased"}))
        return None

    def list(self) -> list[Credential]:
        return sorted(self.store.list(Credential), key=lambda c: c.name)

    def get(self, name: str) -> Credential:
        cred = self.store.get(Credential, name)
        if not cred:
            raise CredentialError(f"unknown credential '{name}'")
        return cred

    # --- identity: credential exposure (identity.exposure)
    def _check_exposure_choice(self, cred: Credential) -> None:
        if cred.exposure not in (None, "sandbox", "control_plane"):
            raise CredentialError("exposure must be sandbox or control_plane")
        if cred.exposure == "sandbox" and identity.locked(cred, self.settings):
            raise CredentialError(f"credential '{cred.name}' is a signing key, bot token or GitHub App key; it stays "
                                  "control-plane only")

    def set_exposure(self, name: str, exposure: str | None) -> Credential:
        """Mark a credential `sandbox` or `control_plane`; None goes back to the default for its kind and name."""
        cred = self.get(name)
        cred.exposure = exposure  # type: ignore[assignment]
        self._check_exposure_choice(cred)
        self.store.put(cred)
        return cred

    def check_sandbox(self, cred: Credential | str) -> Credential:
        """The credential, if it may go into a sandbox; CredentialError (saying why) if it is control-plane only."""
        c = self.get(cred) if isinstance(cred, str) else cred
        if identity.exposure(c, self.settings) == "control_plane":
            raise CredentialError(identity.refusal(c, self.settings))
        return c

    def sandbox_secret(self, name: str) -> str:
        """secret_text for a `secrets:` mapping into a sandbox: refused for control-plane credentials."""
        self.check_sandbox(name)
        return self.secret_text(name)

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
                    data = await provider.read_file(sandbox, self._abs(c.file_path), limit=AUTH_FILE_LIMIT)
                except Exception:  # sandbox may already be gone; keep the vault copy
                    data = None
                if data is not None and len(data) > AUTH_FILE_LIMIT:  # --- overnight/sweep: not an auth file
                    log.warning("not writing back credential %s: %s in sandbox %s is over %d bytes", c.name,
                                c.file_path, sandbox.id, AUTH_FILE_LIMIT)
                    data = None
            with self._lock:
                # Re-read after the await: the lease may have been force-released and re-issued meanwhile.
                cur = self.store.get(Credential, c.name)
                if cur is None or cur.leased_to != sandbox.id:
                    continue
                if data and _is_reference(data):  # --- company-deploy: a sandbox never writes a reference back
                    log.warning("not writing back credential %s: %s in sandbox %s names a mounted file", c.name,
                                c.file_path, sandbox.id)
                    data = None
                stored = self.vault.get(f"cred:{c.name}")
                if data and data != stored and (why := self._writeback_refusal(cur, stored, data)):
                    # --- KAN-168: keep the stored login; the audit names the credential and sandbox, never content
                    log.warning("not writing back credential %s from sandbox %s: %s", c.name, sandbox.id, why)
                    self.store.audit(AuditEvent(actor=f"sandbox:{sandbox.id}", action="credential.writeback_refused",
                                                target=c.name, detail={"sandbox": sandbox.id, "file": c.file_path,
                                                                       "team": cur.person is None, "reason": why}))
                    data = None
                if data and data != stored:
                    self.vault.put(f"cred:{c.name}", data)
                    cur.updated_at = now()
                    self._secret_cache = None
                cur.leased_to = None
                self.store.put(cur)

    @staticmethod
    def _writeback_refusal(cred: Credential, stored: bytes | None, data: bytes) -> str | None:
        """--- KAN-168: why a changed login file read back from a sandbox isn't stored, or None to store it.

        The sandbox decides what the file holds, and the identity it names is unverified: a sandbox can copy the
        stored file's id and access tokens and swap in another account's refresh token. So:

        * **A team credential** (no person) is never written back. Members without a login of their own fall back to
          it, so a swapped file would sign the next of them in to another account. Subscription logins can't be team
          credentials (teamcreds.check_team), so no team login needs its rotation kept; an admin re-puts one.
        * **A person's own login** is written back so a rotated refresh token survives, only when it names the same
          account as the stored one (authident.same) and isn't the stored tokens with a new refresh token
          (authident.swapped). What stays open is in docs/security/review-1.0.md."""
        from hangar.authident import account, same, swapped

        if cred.person is None:
            return "team credential: hangar doesn't store a login file a sandbox returns (an admin re-puts it)"
        verdict = same(account(stored), account(data))
        if verdict is False:
            return "it signs in to a different account than the stored login"
        if swapped(stored, data):
            return "its refresh token changed but its access and id tokens are the stored ones, which no refresh does"
        return None

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
            if c.kind == CredentialKind.GITHUB_APP:  # workstream F: gateway-only, never materialized
                continue
            self.check_sandbox(c)  # --- identity: control-plane credentials never reach a sandbox (B4)
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
            existing = await provider.read_file(sandbox, f"{self.home}/.hangar/env", limit=ENV_FILE_LIMIT) or b""
            if len(existing) > ENV_FILE_LIMIT:  # --- overnight/sweep: grown by the agent; start it over
                log.warning("sandbox %s: ~/.hangar/env is over %d bytes; rewriting it", sandbox.id, ENV_FILE_LIMIT)
                existing = b""
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
