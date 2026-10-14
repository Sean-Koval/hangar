"""The vault key ring, and rotating the vault key (docs/security.md#rotating-the-vault-key).

One Fernet key encrypted the vault, every checkpoint and every backup, and nothing could replace it: a key that
leaked stayed good for everything ever stored. HANGAR_SECRET_KEY (or <data dir>/secret.key) now holds a list of
keys, newest first, separated by commas (or lines in the file). The first encrypts; every key hangar hasn't retired
decrypts (cryptography's MultiFernet). The keys hangar derives tokens from (envd, the egress proxy, previews, E2B
access tokens, signed media links, process tags) are signed with the newest and checked against all of them.

A rotation, `hangar admin rotate-key`:

1. **A new key goes first.** With HANGAR_SECRET_KEY the operator adds it (in Secret Manager, then the hangar
   Secret, then a restart); with a key file, the command writes it. From the first start that sees it, new data is
   encrypted with it, and the rotation is recorded as pending. Sandboxes started before keep the tokens derived from
   the old key (envd's, the egress proxy's), which their processes already hold.
2. **The vault is re-sealed at once**, with the other encrypted rows in the database (live turns' control keys, E2B
   sandboxes' envVars), in one go.
3. **A background pass** goes through the session store (checkpoint parts, artifacts, backups: every `.enc` object)
   and re-encrypts what the old key sealed, with its progress in the database, so a restart picks it up where it
   was. Meanwhile a checkpoint part that is copied into a new checkpoint is re-encrypted as it is copied
   (`Resealing`), instead of carrying the old key forward.
4. **The old key is retired** once the pass is done and no sandbox started before the rotation is running: hangar
   stops reading with it (and drops it from the key file). The operator then removes it from HANGAR_SECRET_KEY.

The state is one durable kv record, KV, with key ids only (`key_id`), never keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

if TYPE_CHECKING:
    from hangar.checkpoints import CheckpointStore
    from hangar.config import Settings
    from hangar.store import Store

log = logging.getLogger("hangar.keys")

KV = "vault-keys"
FILE = "secret.key"
PASS_SAVE_EVERY = 25  # objects between two progress records
RETIRE_CHECK = 60.0  # seconds between two looks at what still holds the old key
ACTIVE_ROTATION = ("pending", "sealed", "passing", "passed")


class RotationError(ValueError):
    """A rotation that can't start or go on, with what to do about it."""


def parse(raw: str | bytes | None) -> list[bytes]:
    """Keys from HANGAR_SECRET_KEY or the key file: newest first, separated by commas or lines; each must be a
    Fernet key (32 bytes, url-safe base64)."""
    text = raw.decode() if isinstance(raw, bytes) else (raw or "")
    out: list[bytes] = []
    for part in text.replace("\n", ",").split(","):
        k = part.strip().encode()
        if not k:
            continue
        try:
            Fernet(k)
        except ValueError as e:
            raise ValueError(f"vault key {len(out) + 1} isn't a Fernet key (32 bytes, url-safe base64)") from e
        if k not in out:
            out.append(k)
    return out


def key_id(key: bytes) -> str:
    """A key's fingerprint, for logs, status and the state record: the key can't be had from it."""
    return hashlib.sha256(b"hangar-vault-key\0" + key).hexdigest()[:12]


def _file(settings_or_dir: Any) -> Path:
    data_dir = getattr(settings_or_dir, "data_dir", settings_or_dir)
    return Path(data_dir) / FILE


def load_keys(explicit: str | None, data_dir: Path) -> list[bytes]:
    """HANGAR_SECRET_KEY's keys, else the key file's, made with one new key the first time."""
    if explicit:
        return parse(explicit)
    f = _file(data_dir)
    if f.exists():
        return parse(f.read_bytes())
    key = Fernet.generate_key()
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return [key]


def _write_file(data_dir: Path, keys: list[bytes]) -> None:
    """Replace the key file in one step (0600), newest key first."""
    f = _file(data_dir)
    tmp = f.with_name(f".{FILE}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"\n".join(keys) + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(f)


class KeyRing:
    """The configured keys, minus those retired, and the rotation in progress (if any)."""

    def __init__(self, keys: list[bytes], retired: list[str] | None = None, rotation: dict[str, Any] | None = None):
        if not keys:
            raise ValueError("no vault key")
        self.keys = keys
        self.retired = set(retired or ())
        self.rotation = rotation
        self._fernet: MultiFernet | None = None

    @classmethod
    def of(cls, key: bytes | str) -> KeyRing:
        return cls(parse(key))

    def set_keys(self, keys: list[bytes]) -> None:
        self.keys, self._fernet = keys, None

    def retire(self, kid: str) -> None:
        self.retired.add(kid)
        self._fernet = None

    @property
    def active(self) -> list[bytes]:
        """The keys hangar reads with, newest first. Never empty: the newest is never retired."""
        live = [k for k in self.keys if key_id(k) not in self.retired]
        return live or self.keys[:1]

    @property
    def primary(self) -> bytes:
        return self.active[0]

    @property
    def fernet(self) -> MultiFernet:
        if self._fernet is None:
            self._fernet = MultiFernet([Fernet(k) for k in self.active])
        return self._fernet

    def by_id(self, kid: str | None) -> bytes | None:
        return next((k for k in self.active if key_id(k) == kid), None)

    @property
    def rotating(self) -> bool:
        return bool(self.rotation) and self.rotation.get("phase") in ACTIVE_ROTATION

    def key_for(self, created_at: float | None = None) -> bytes:
        """The key a sandbox's derived tokens come from: during a rotation, one started before it keeps the old key
        (its envd and egress proxy already hold tokens of it); everything else, the newest."""
        r = self.rotation
        if created_at is not None and self.rotating and created_at < r.get("started_at", 0):
            return self.by_id(r.get("old")) or self.primary
        return self.primary

    def sealed_with_primary(self, blob: bytes) -> bool:
        try:
            Fernet(self.primary).decrypt(blob)
            return True
        except InvalidToken:
            return False


# ====================================================================== the process's key ring
_RINGS: dict[tuple[str | None, str], KeyRing] = {}


def ring(settings: Settings) -> KeyRing:
    """The key ring for these settings: the one the app loaded (`load`), or the configured keys alone."""
    k = (settings.secret_key, str(settings.data_dir))
    if (r := _RINGS.get(k)) is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        r = _RINGS[k] = KeyRing(load_keys(settings.secret_key, settings.data_dir))
    return r


def key_for(settings: Settings, created_at: float | None = None) -> bytes:
    """What to sign or derive with (KeyRing.key_for)."""
    return ring(settings).key_for(created_at)


def verify_keys(settings: Settings) -> list[bytes]:
    """What to check a signature against: every key hangar still reads with, newest first."""
    return ring(settings).active


def load(settings: Settings, store: Store) -> KeyRing:
    """At startup: the configured keys, what was retired, and a rotation in progress. A new key first in the
    configuration starts one (pending until `hangar admin rotate-key`), so sandboxes from before keep their tokens."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    state = store.kv_get_json(KV) or {}
    r = KeyRing(load_keys(settings.secret_key, settings.data_dir), state.get("retired"), state.get("rotation"))
    new = key_id(r.primary)
    current = state.get("current")
    if current is None:
        state["current"] = new  # the first start with a key ring: everything is sealed with this key
    elif current != new and not r.rotating and r.by_id(current) is not None:
        r.rotation = state["rotation"] = {"old": current, "new": new, "started_at": time.time(), "phase": "pending"}
        log.warning("a new vault key (%s) is first; key %s sealed what is stored: run `hangar admin rotate-key`",
                    new, current)
    elif r.rotating and r.rotation.get("new") != new:
        log.error("vault key %s is first, but the rotation to %s isn't finished; put %s back first until it is",
                  new, r.rotation.get("new"), r.rotation.get("new"))
    elif current != new and r.by_id(current) is None and not r.rotating:
        log.error("the vault key that sealed what is stored (%s) isn't in HANGAR_SECRET_KEY: credentials, "
                  "checkpoints and backups can't be decrypted until it is", current)
    store.kv_set(KV, json.dumps(state), durable=True)
    _RINGS[(settings.secret_key, str(settings.data_dir))] = r
    return r


# ====================================================================== copies re-encrypt during a rotation
class Resealing:
    """A checkpoint store whose copies re-encrypt what the old key sealed while a rotation runs (a checkpoint copies
    the parts that didn't change from the one before, which would otherwise carry the old key forward)."""

    def __init__(self, inner: CheckpointStore, keyring: KeyRing):
        self.inner = inner
        self.ring = keyring

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def copy(self, src: str, dst: str) -> bool:
        if not self.ring.rotating or not src.endswith(".enc"):
            return await self.inner.copy(src, dst)
        blob = await self.inner.get(src)
        if blob is None:
            return False
        if await asyncio.to_thread(self.ring.sealed_with_primary, blob):
            return await self.inner.copy(src, dst)
        await self.inner.put(dst, await asyncio.to_thread(self.ring.fernet.rotate, blob))
        return True


# ====================================================================== the rotation
class Rotation:
    """Starts, carries on (after a restart too) and finishes a rotation. One per app (`h.rotation`)."""

    def __init__(self, settings: Settings, store: Store, keyring: KeyRing,
                 stores: list[tuple[CheckpointStore, bool]], live_sandboxes: Callable[[], list[Any]],
                 audit: Callable[..., None]):
        self.settings = settings
        self.store = store
        self.ring = keyring
        # (store, may it refuse reads): the session store; and the backup store when it's another one, which hangar
        # may be allowed only to write to (docs/security.md#backups-and-the-vault-key)
        self.stores = stores
        self.live_sandboxes = live_sandboxes
        self.audit = audit
        # (store, ring) -> rows re-sealed: encrypted values kept in the database outside the vault
        self.resealers: list[Callable[[Store, KeyRing], int]] = []
        self._task: asyncio.Task | None = None
        self._starting = asyncio.Lock()  # two `rotate-key` at once would write two new keys

    # ---------------------------------------------------------------- state
    def _state(self) -> dict[str, Any]:
        return self.store.kv_get_json(KV) or {}

    def _save(self, **rotation: Any) -> None:
        st = self._state()
        st["rotation"] = {**(st.get("rotation") or {}), **rotation}
        self.ring.rotation = st["rotation"]
        self.store.kv_set(KV, json.dumps(st), durable=True)

    def status(self) -> dict[str, Any]:
        st = self._state()
        r = st.get("rotation") or {}
        out: dict[str, Any] = {
            "keys": [{"id": key_id(k), "first": i == 0, "retired": key_id(k) in self.ring.retired}
                     for i, k in enumerate(self.ring.keys)],
            "current": st.get("current"), "key_file": not self.settings.secret_key,
            "rotation": r or None, "running": self._task is not None and not self._task.done(),
        }
        if r.get("phase") in ACTIVE_ROTATION:
            out["blockers"] = self.blockers()
        out["next"] = self._next(r, out.get("blockers") or [])
        return out

    def _next(self, r: dict[str, Any], blockers: list[str]) -> str:
        phase = r.get("phase")
        if not phase:
            return ("hangar admin rotate-key (writes a new key first in the key file)" if not self.settings.secret_key
                    else "put a new key first in HANGAR_SECRET_KEY, restart hangar, then hangar admin rotate-key")
        if phase == "pending":
            return "hangar admin rotate-key: re-seal the vault and start the pass"
        if phase in ("sealed", "passing"):
            return "wait for the pass over the session store (hangar admin rotate-key --status)"
        if phase == "passed":
            return (f"waiting for {len(blockers)} sandbox(es) started before the rotation to stop; or "
                    "hangar admin rotate-key --retire --force" if blockers else "retiring the old key")
        if phase == "retired" and self.settings.secret_key and self.ring.by_id(r.get("old")) is None and \
                any(key_id(k) == r.get("old") for k in self.ring.keys):
            return f"remove key {r.get('old')} from HANGAR_SECRET_KEY (it is retired), then restart hangar"
        return "nothing: no rotation in progress"

    def blockers(self) -> list[str]:
        """Sandboxes started before the rotation that are still live: they hold tokens of the old key."""
        r = self.ring.rotation or {}
        t = r.get("started_at", 0)
        return sorted(f"{sb.name} ({sb.id})" for sb in self.live_sandboxes() if (sb.created_at or 0) < t)

    # ---------------------------------------------------------------- start
    async def start(self, who: str) -> dict[str, Any]:
        """Start a rotation, or carry on with the one in progress (idempotent)."""
        async with self._starting:
            return await self._start(who)

    async def _start(self, who: str) -> dict[str, Any]:
        r = self.ring.rotation or {}
        if r.get("phase") in ("sealed", "passing", "passed"):
            self.resume()
            return self.status()
        if r.get("phase") != "pending":
            if self.settings.secret_key:
                raise RotationError("no new key: put a new one first in HANGAR_SECRET_KEY (newest first, comma-"
                                "separated: <new>,<old>), restart hangar, then run this again (docs/security.md"
                                "#rotating-the-vault-key)")
            await asyncio.to_thread(self._new_file_key)
        r = self.ring.rotation or {}
        if self.ring.by_id(r.get("new")) is None or key_id(self.ring.primary) != r.get("new"):
            raise RotationError(f"key {r.get('new')} must be first in HANGAR_SECRET_KEY to rotate to it")
        n, bad = await asyncio.to_thread(self._reseal)
        self._save(phase="sealed", sealed_rows=n, unreadable_rows=bad, sealed_at=time.time(), passes={}, error=None)
        self.audit(who, "vault.rotate_key", r.get("new"), old=r.get("old"), sealed_rows=n, unreadable_rows=bad)
        log.warning("vault key rotation %s -> %s: %d rows re-sealed; the pass over the session store starts",
                    r.get("old"), r.get("new"), n)
        self.resume()
        return self.status()

    def _new_file_key(self) -> None:
        """Key file mode: a new key goes first in the file, and the rotation is recorded."""
        new = Fernet.generate_key()
        keys = [new, *self.ring.keys]
        _write_file(self.settings.data_dir, keys)
        self.ring.set_keys(keys)
        st = self._state()
        old = st.get("current") or key_id(keys[1])
        st["rotation"] = {"old": old, "new": key_id(new), "started_at": time.time(), "phase": "pending"}
        self.ring.rotation = st["rotation"]
        self.store.kv_set(KV, json.dumps(st), durable=True)

    def _reseal(self) -> tuple[int, int]:
        """The vault's rows and the other encrypted rows, under the new key, at once. A row no key reads (damaged
        before the rotation) stays as it is: it was unreadable already. Returns (re-sealed, unreadable)."""
        bad = 0

        def one(blob: bytes) -> bytes:
            nonlocal bad
            try:
                return self.ring.fernet.rotate(blob)
            except InvalidToken:
                bad += 1
                return blob

        n = self.store.reseal_secrets(one)
        for fn in self.resealers:
            n += fn(self.store, self.ring)
        return n - bad, bad

    # ---------------------------------------------------------------- the pass and retiring
    def resume(self) -> None:
        """Carry on with a rotation that was re-sealed (at startup, or after `start`)."""
        r = self.ring.rotation or {}
        if r.get("phase") in ("sealed", "passing", "passed") and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                r = self.ring.rotation or {}
                if r.get("phase") in ("sealed", "passing"):
                    await self._pass()
                    self._save(phase="passed", passed_at=time.time(), error=None)
                if (self.ring.rotation or {}).get("phase") == "passed" and not self.blockers():
                    self.retire("hangar")
                if (self.ring.rotation or {}).get("phase") not in ACTIVE_ROTATION:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the pass is idempotent and keeps its place: try again
                log.exception("vault key rotation: the pass failed; retrying")
                self._save(error=str(e)[:500])
            await asyncio.sleep(RETIRE_CHECK)

    async def _pass(self) -> None:
        """Every `.enc` object in each store, in key order, from where the last pass stopped."""
        self._save(phase="passing")
        for store, write_only in self.stores:
            name = store.description
            prog = ((self.ring.rotation or {}).get("passes") or {}).get(name) or {}
            if prog.get("finished"):
                continue
            try:
                keys = [k for k in await store.list("") if k.endswith(".enc")]
            except Exception as e:
                if not write_only:
                    raise
                self._progress(name, {**prog, "finished": True, "unreachable": str(e)[:300]})
                log.warning("vault key rotation: can't list %s (%s); what it holds keeps its key", name, e)
                continue
            after = prog.get("after", "")
            todo = [k for k in keys if k > after]
            stats = {"objects": len(keys), "checked": len(keys) - len(todo), "rotated": prog.get("rotated", 0),
                     "unreadable": prog.get("unreadable", 0), "after": after, "finished": False}
            last_save = time.monotonic()
            for i, k in enumerate(todo, 1):
                blob = await store.get(k)
                if blob is not None:
                    res = await asyncio.to_thread(self._reencrypt, blob)
                    if res is None:
                        stats["unreadable"] += 1
                    elif res is not blob:
                        await store.put(k, res)
                        stats["rotated"] += 1
                stats["checked"] += 1
                stats["after"] = k
                if i % PASS_SAVE_EVERY == 0 or time.monotonic() - last_save > 10:
                    self._progress(name, stats)
                    last_save = time.monotonic()
            self._progress(name, {**stats, "finished": True, "finished_at": time.time()})

    def _reencrypt(self, blob: bytes) -> bytes | None:
        """The blob under the new key: itself when it already is, None when no key reads it."""
        if self.ring.sealed_with_primary(blob):
            return blob
        try:
            return self.ring.fernet.rotate(blob)
        except InvalidToken:
            return None

    def _progress(self, name: str, stats: dict[str, Any]) -> None:
        passes = dict((self.ring.rotation or {}).get("passes") or {})
        passes[name] = dict(stats)
        self._save(passes=passes)

    def retire(self, who: str, force: bool = False) -> dict[str, Any]:
        """Stop reading with the old key. Only after the pass; with sandboxes from before it still running, only
        when forced (their envd falls back to exec, and a proxied one loses the egress proxy)."""
        r = self.ring.rotation or {}
        if r.get("phase") != "passed":
            raise RotationError(f"the rotation is {r.get('phase') or 'not started'}: the old key still seals data "
                            "until the pass over the session store is done")
        if (left := self.blockers()) and not force:
            raise RotationError(f"{len(left)} sandbox(es) started before the rotation still run with tokens of the old "
                            f"key: {', '.join(left[:10])}. Wait for them, or --force")
        old = r.get("old")
        st = self._state()
        st["retired"] = sorted({*st.get("retired", []), old})
        st["current"] = r.get("new")
        st["rotation"] = {**r, "phase": "retired", "retired_at": time.time(), "forced": bool(left)}
        self.store.kv_set(KV, json.dumps(st), durable=True)
        self.ring.rotation = st["rotation"]
        self.ring.retire(old)
        if not self.settings.secret_key:  # the key file: the old key leaves it
            _write_file(self.settings.data_dir, self.ring.active)
            self.ring.set_keys(self.ring.active)
        self.audit(who, "vault.retire_key", old, new=r.get("new"), forced=bool(left))
        log.warning("vault key %s retired; %s seals everything now", old, r.get("new"))
        return self.status()


# ====================================================================== doctor
def doctor_checks(h: Any) -> list[dict[str, Any]]:
    """The rotation's state, a retired key still configured, and a configuration without the sealing key."""
    from hangar.doctor import _check

    rot: Rotation | None = getattr(h, "rotation", None)
    if rot is None:
        return []
    st = rot.status()
    r = st.get("rotation") or {}
    configured = {k["id"] for k in st["keys"]}
    out = []
    if st.get("current") and st["current"] not in configured:
        out.append(_check("vault key", False, f"the key that sealed what is stored ({st['current']}) isn't "
                          "configured: credentials, checkpoints and backups can't be decrypted",
                          "put it back in HANGAR_SECRET_KEY (from Secret Manager), then restart hangar"))
    elif r.get("phase") in ACTIVE_ROTATION:
        done = sum(p.get("checked") or 0 for p in (r.get("passes") or {}).values())
        out.append(_check("vault key", None, f"rotating {r.get('old')} -> {r.get('new')}: {r.get('phase')}"
                          + (f", {done} objects checked" if r.get("passes") else "")
                          + (f"; last error: {r['error']}" if r.get("error") else ""), st["next"]))
    elif r.get("phase") == "retired" and r.get("old") in configured and rot.settings.secret_key:
        out.append(_check("vault key", None, f"key {r.get('old')} is retired but still in HANGAR_SECRET_KEY",
                          st["next"]))
    else:
        out.append(_check("vault key", True, f"{len(configured)} key(s) configured; {st.get('current')} seals "
                          "what is stored"))
    return out


# ====================================================================== REST
def mount(app: Any, h: Any, admin: Any) -> None:
    """Admin only: GET /api/admin/rotate-key (status), POST /api/admin/rotate-key (start or carry on),
    POST /api/admin/rotate-key/retire (?force=true: with sandboxes from before still running)."""
    from fastapi import HTTPException

    @app.get("/api/admin/rotate-key")
    async def rotate_status(_: str = admin):
        return h.rotation.status()

    @app.post("/api/admin/rotate-key")
    async def rotate_start(who: str = admin):
        try:
            return await h.rotation.start(who)
        except RotationError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/admin/rotate-key/retire")
    async def rotate_retire(force: bool = False, who: str = admin):
        try:
            return h.rotation.retire(who, force)  # on the loop, like the rotation's own retiring
        except RotationError as e:
            raise HTTPException(409, str(e)) from e
