"""SQLite-backed state store.

Every record is a pydantic model stored as JSON in a per-kind table. This keeps the
control plane a single process with a single file of state, which is what you want on
a laptop or a single VM. On GKE the file lives on a PersistentVolume (one replica).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from hangar.models import ApiToken, AuditEvent, Credential, Job, Sandbox, Snapshot

T = TypeVar("T", bound=BaseModel)

_TABLES: dict[type[BaseModel], tuple[str, str]] = {
    Sandbox: ("sandboxes", "id"),
    Snapshot: ("snapshots", "id"),
    Credential: ("credentials", "name"),
    ApiToken: ("api_tokens", "id"),
    Job: ("jobs", "id"),
}


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        for table, _ in _TABLES.values():
            self._db.execute(f"CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, blob BLOB NOT NULL)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, data TEXT)"
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")

    # --- generic model CRUD -------------------------------------------------
    def put(self, obj: BaseModel) -> None:
        table, key_field = _TABLES[type(obj)]
        with self._lock:
            self._db.execute(
                f"INSERT INTO {table}(key, data) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (getattr(obj, key_field), obj.model_dump_json()),
            )

    def get(self, cls: type[T], key: str) -> T | None:
        table, _ = _TABLES[cls]
        with self._lock:
            row = self._db.execute(f"SELECT data FROM {table} WHERE key=?", (key,)).fetchone()
        return cls.model_validate_json(row[0]) if row else None

    def list(self, cls: type[T]) -> list[T]:
        table, _ = _TABLES[cls]
        with self._lock:
            rows = self._db.execute(f"SELECT data FROM {table}").fetchall()
        return [cls.model_validate_json(r[0]) for r in rows]

    def delete(self, cls: type[BaseModel], key: str) -> None:
        table, _ = _TABLES[cls]
        with self._lock:
            self._db.execute(f"DELETE FROM {table} WHERE key=?", (key,))

    # --- encrypted secret blobs (encryption handled by the vault) ----------
    def put_secret(self, name: str, blob: bytes) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO secrets(name, blob) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET blob=excluded.blob",
                (name, blob),
            )

    def get_secret(self, name: str) -> bytes | None:
        with self._lock:
            row = self._db.execute("SELECT blob FROM secrets WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def delete_secret(self, name: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM secrets WHERE name=?", (name,))

    # --- audit --------------------------------------------------------------
    def audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._db.execute("INSERT INTO audit(ts, data) VALUES(?, ?)", (event.ts, event.model_dump_json()))

    def audit_tail(self, limit: int = 200) -> list[AuditEvent]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [AuditEvent.model_validate_json(r[0]) for r in rows]

    # --- small kv -----------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def kv_delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key=?", (key,))

    def kv_keys(self, prefix: str) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT key FROM kv WHERE key LIKE ? ESCAPE '\\'",
                                    (prefix.replace("_", "\\_").replace("%", "\\%") + "%",)).fetchall()
        return [r[0] for r in rows]

    def kv_get_json(self, key: str, default=None):
        v = self.kv_get(key)
        return json.loads(v) if v is not None else default

    def kv_set_json(self, key: str, value) -> None:
        self.kv_set(key, json.dumps(value))
