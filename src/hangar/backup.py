"""Database backups (overnight/sweep): hangar's SQLite file, copied consistently into the session store.

Everything hangar knows lives in one SQLite file on the control plane's volume: sessions and their checkpoint
lists, jobs, schedules, tokens, the vault, stored settings. A backup is
- consistent: SQLite's online backup API on a connection of its own, in one step, so it is one read transaction
  (WAL mode: hangar keeps writing meanwhile, and the copy holds none of a write half done);
- encrypted with the vault key and gzipped, so the bucket holds nothing readable without HANGAR_SECRET_KEY;
- written to the session store (`_backups/` in the GCS bucket on GKE, else <data_dir>/sessions/_backups), and only
  the newest HANGAR_BACKUP_KEEP are kept.

A restore is staged: `hangar admin restore` decrypts a backup, checks it with `PRAGMA integrity_check` and writes
<data_dir>/restore-pending.db. The next start moves the current database aside (hangar.db.before-restore-<time>) and
puts the backup in its place before anything opens it. docs/gke.md has the procedure.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import io
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hangar import metrics as metrics  # --- reconciler: loop heartbeat
from hangar import observe  # integrate (#57 + #55): spans and /metrics

if TYPE_CHECKING:
    from hangar.checkpoints import CheckpointStore
    from hangar.config import Settings
    from hangar.vault import Vault

log = logging.getLogger("hangar.backup")

PREFIX = "_backups/"
PENDING = "restore-pending.db"
_NAME = re.compile(r"hangar-(\d{8}T\d{6}Z)\.db\.gz\.enc")  # review(sweep): fullmatch; `$` also matches before a "\n"
_FIRST_DELAY = 300.0  # the first scheduled backup after a start with none recent: not in the startup rush
_RETRY = 3600.0  # after a failed scheduled backup


class BackupError(RuntimeError):
    pass


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y%m%dT%H%M%SZ")


def taken_at(name: str) -> float | None:
    """When a backup was taken, from its name; None for anything that isn't one."""
    m = _NAME.fullmatch(name.rsplit("/", 1)[-1])
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp() if m else None


def snapshot(db_path: Path, dest: Path) -> None:
    """A consistent copy of the database into `dest`, a new file (mode 600): on disk, not in memory, since the
    audit log makes the database grow for as long as hangar runs."""
    dest.unlink(missing_ok=True)
    os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)  # pages=-1: the whole database in one step, so one read transaction
    finally:
        dst.close()
        src.close()


def _gzip(path: Path) -> bytes:
    buf = io.BytesIO()
    with path.open("rb") as f, gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        shutil.copyfileobj(f, gz, 1 << 20)
    return buf.getvalue()


def check(data: bytes) -> None:
    """Refuse anything that isn't an intact SQLite database with hangar's tables."""
    db = sqlite3.connect(":memory:")
    head = bytearray(data[:100])
    if len(head) == 100 and head[18:20] == b"\x02\x02":  # a WAL-mode header: memory databases can't open one
        head[18:20] = b"\x01\x01"  # (sqlite.org/fileformat2.html: bytes 18-19 are 1 for rollback, 2 for WAL)
    try:
        db.deserialize(bytes(head) + data[100:])
        ok = db.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as e:
        raise BackupError(f"not a SQLite database: {e}") from None
    finally:
        db.close()
    if ok != "ok":
        raise BackupError(f"the backup fails SQLite's integrity check: {ok}")
    if not {"jobs", "sessions", "secrets"} <= tables:
        raise BackupError("the backup is not a hangar database")


def apply_pending(settings: Settings) -> str | None:
    """At startup, before the database is opened: swap in a staged restore. Returns where the old one went."""
    pending = settings.data_dir / PENDING
    if not pending.is_file():
        return None
    db = settings.db_path
    aside = db.with_name(f"{db.name}.before-restore-{_stamp(time.time())}")
    for suffix in ("", "-wal", "-shm"):  # the WAL holds commits the main file doesn't have yet: it goes along
        old = Path(f"{db}{suffix}")
        if old.exists():
            old.rename(f"{aside}{suffix}")
    pending.rename(db)
    log.warning("restored the database from a backup; the previous one is %s", aside)
    return str(aside)


def backup_store(settings: Settings, session_store: CheckpointStore) -> CheckpointStore:
    """--- durable: HANGAR_BACKUP_STORE, else the session store."""
    from hangar.checkpoints import make_store

    loc = (settings.backup_store or "").strip()
    if not loc or loc.rstrip("/") == (settings.session_store or "").rstrip("/"):
        return session_store
    return make_store(loc, settings.data_dir)


def _refused(e: Exception) -> bool:
    """--- durable: the store said no (a bucket hangar may only write to), not that it failed."""
    import httpx

    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 403


class Backups:
    def __init__(self, settings: Settings, vault: Vault, blobs: CheckpointStore):
        self.settings = settings
        self.vault = vault
        self.blobs = blobs
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ backup
    @observe.traced("db.backup")
    async def run(self) -> dict[str, Any]:
        """Take a backup now, then delete all but the newest HANGAR_BACKUP_KEEP."""
        async with self._lock:  # a scheduled one and `hangar admin backup` at once would race on pruning
            t = time.time()
            key = f"{PREFIX}hangar-{_stamp(t)}.db.gz.enc"

            def build() -> tuple[int, bytes]:
                for old in self.settings.data_dir.glob("backup-*.db.part"):  # left by a crash mid-backup
                    old.unlink(missing_ok=True)
                tmp = self.settings.data_dir / f"backup-{_stamp(t)}.db.part"
                try:
                    snapshot(self.settings.db_path, tmp)
                    return tmp.stat().st_size, self.vault.encrypt(_gzip(tmp))  # memory: the compressed size
                finally:
                    tmp.unlink(missing_ok=True)

            size, blob = await asyncio.to_thread(build)
            await self.blobs.put(key, blob)
            pruned = await self.prune()
            log.info("database backup %s: %d bytes (%d stored)", key, size, len(blob))
            return {"name": key.removeprefix(PREFIX), "at": t, "db_bytes": size, "stored_bytes": len(blob),
                    "where": self.blobs.description, "pruned": pruned}

    async def list(self) -> list[dict[str, Any]]:
        """Backups in the store, newest first."""
        out = [{"name": k.removeprefix(PREFIX), "at": at} for k in await self.blobs.list(PREFIX)
               if (at := taken_at(k)) is not None]
        return sorted(out, key=lambda b: b["at"], reverse=True)

    async def prune(self) -> list[str]:
        try:
            found = await self.list()
        except Exception as e:
            if not _refused(e):
                raise
            # --- durable: a bucket hangar may only write to keeps its backups by its own lifecycle rule
            log.info("backups in %s can't be listed by hangar; the bucket's lifecycle rule keeps them",
                     self.blobs.description)
            return []
        old = found[max(1, self.settings.backup_keep):]
        for b in old:
            await self.blobs.delete(PREFIX + b["name"])
        return [b["name"] for b in old]

    # ------------------------------------------------------------------ restore
    @observe.traced("db.restore_stage")
    async def stage(self, name: str) -> dict[str, Any]:
        """Fetch, decrypt and check a backup ("latest" for the newest), and stage it for the next start."""
        if name == "latest":
            found = await self.list()
            if not found:
                raise BackupError(f"no backups in {self.blobs.description}")
            name = found[0]["name"]
        if taken_at(name) is None or "/" in name:
            raise BackupError(f"'{name}' is not a backup name (hangar-<time>.db.gz.enc; see hangar admin backups)")
        blob = await self.blobs.get(PREFIX + name)
        if blob is None:
            raise BackupError(f"no backup '{name}' in {self.blobs.description}")

        def unpack() -> int:
            from cryptography.fernet import InvalidToken

            try:
                data = gzip.decompress(self.vault.decrypt(blob))
            except InvalidToken:
                raise BackupError("the backup can't be decrypted with this hangar's key (HANGAR_SECRET_KEY)") from None
            check(data)
            part = self.settings.data_dir / f"{PENDING}.part"
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            part.replace(self.settings.data_dir / PENDING)
            return len(data)

        size = await asyncio.to_thread(unpack)
        return {"name": name, "at": taken_at(name), "db_bytes": size, "staged": str(self.settings.data_dir / PENDING)}

    # ------------------------------------------------------------------ schedule
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        """Every HANGAR_BACKUP_HOURS, counted from the newest backup in the store, so restarts don't add any."""
        wait = _FIRST_DELAY
        with contextlib.suppress(Exception):
            if (found := await self.list()) and (hours := self.settings.backup_interval_hours):
                wait = max(_FIRST_DELAY, found[0]["at"] + hours * 3600 - time.time())
        while True:
            await asyncio.sleep(wait)
            hours = self.settings.backup_interval_hours  # read each time: `hangar config set` applies without restart
            if not hours:
                wait = _RETRY
                continue
            try:
                await self.run()
                wait = hours * 3600
                metrics.loop_tick("backups")  # --- reconciler: heartbeat
            except Exception as e:  # noqa: BLE001 - the next attempt may work; doctor reports the age
                log.error("scheduled database backup failed: %s", e)
                metrics.loop_failed("backups")  # --- reconciler
                wait = min(hours * 3600, _RETRY)


def mount(app, h, admin) -> None:
    """REST routes, admin tokens only: POST /api/admin/backup, GET /api/admin/backups, POST /api/admin/restore."""
    from fastapi import Body, HTTPException

    @app.post("/api/admin/backup")
    async def backup_now(who: str = admin):
        res = await h.backups.run()
        h.fleet.audit(who, "admin.backup", res["name"])
        return res

    @app.get("/api/admin/backups")
    async def backup_list(_: str = admin):
        return {"where": h.backups.blobs.description, "every_hours": h.settings.backup_interval_hours,
                "keep": h.settings.backup_keep, "backups": await h.backups.list()}

    @app.post("/api/admin/restore")
    async def restore(name: str = Body(..., embed=True), who: str = admin):
        try:
            res = await h.backups.stage(name)
        except BackupError as e:
            raise HTTPException(400, str(e)) from e
        h.fleet.audit(who, "admin.restore_staged", res["name"])
        return res


def _bucket(location: str | None) -> str | None:
    return location[5:].split("/", 1)[0] if location and location.startswith("gs://") else None


async def isolation_check(backups: Backups, settings: Settings) -> dict[str, Any] | None:
    """--- durable (hardening review): backups beside the checkpoints, under the same service account. Whoever can
    read the bucket with that account's token reads every checkpoint and every backup (and with it the vault), and
    one key decrypts them all. OK: a bucket of their own that hangar may only write to."""
    from hangar.doctor import _check

    ses = _bucket(settings.session_store)
    bak = _bucket(settings.backup_store) or (ses if not settings.backup_store else None)
    if ses is None or (settings.backup_store and bak is None):
        return None  # no bucket for the checkpoints, or backups on a disk: PD snapshots are the advice there
    fix = ("set HANGAR_BACKUP_STORE to a bucket of its own where hangar's service account has only "
           "roles/storage.objectCreator, with a lifecycle rule for how long backups are kept "
           "(docs/security/vault-key.md#backups-and-the-vault-key)")
    if bak == ses:
        return _check("backup isolation", None, f"backups share gs://{ses} and hangar's service account with the "
                      "checkpoints: one token or one grant on the bucket reads both", fix)
    try:
        await backups.blobs.list(PREFIX)
    except Exception as e:  # noqa: BLE001
        if _refused(e):
            return _check("backup isolation", True, f"backups go to gs://{bak}, which hangar may only write to")
        return _check("backup isolation", None, f"can't tell whether hangar may read gs://{bak}: {e}", fix)
    return _check("backup isolation", None, f"backups have a bucket of their own (gs://{bak}), but hangar's service "
                  "account can read it, so its token still reads both", fix)


async def doctor_check(backups: Backups, settings: Settings, on_kubernetes: bool) -> dict[str, Any] | None:
    """The age of the newest backup against the schedule (doctor.py's check shape)."""
    from hangar.doctor import _check

    hours = settings.backup_interval_hours
    if not hours:
        if not on_kubernetes:
            return None
        return _check("database backups", None, "no scheduled backups of hangar's database",
                      "hangar config set HANGAR_BACKUP_HOURS 24 (or point HANGAR_SESSION_STORE at a bucket), or "
                      "schedule PD snapshots of the hangar-data volume (docs/gke.md)")
    try:
        found = await backups.list()
    except Exception as e:  # noqa: BLE001
        if _refused(e) and settings.backup_store:  # --- durable: write-only for hangar, as advised
            return _check("database backups", None, f"{backups.blobs.description} is write-only for hangar, so it "
                          "can't see how old the newest backup is", "gcloud storage ls the bucket; a failed "
                          "backup shows in the control plane's log as 'scheduled database backup failed'")
        return _check("database backups", False, f"can't list backups in {backups.blobs.description}: {e}",
                      "check HANGAR_SESSION_STORE and the bucket's IAM")
    if not found:
        return _check("database backups", None, f"none yet in {backups.blobs.description}; the first is taken "
                      "a few minutes after startup", "hangar admin backup")
    age = (time.time() - found[0]["at"]) / 3600
    ok = age <= 2 * hours
    return _check("database backups", ok, f"newest is {age:.1f} h old ({found[0]['name']}, {len(found)} kept; "
                  f"every {hours:g} h)", "see the control plane's log for 'database backup failed'; hangar admin "
                  "backup takes one now")
