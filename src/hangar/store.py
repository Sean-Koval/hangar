"""SQLite-backed state store.

Every record is a pydantic model stored as JSON in a per-kind table. This keeps the
control plane a single process with a single file of state, which is what you want on
a laptop or a single VM. On GKE the file lives on a PersistentVolume (one replica).
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Collection, Iterator
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

from pydantic import BaseModel

from hangar.envrequests import EnvRequest  # --- computer: requests agents file with their owners
from hangar.models import (
    JOB_TERMINAL,
    SESSION_LIVE,
    ApiToken,
    AuditEvent,
    Credential,
    Job,
    JobState,
    Person,
    Sandbox,
    SandboxGrant,
    SandboxState,
    ScheduledJob,
    Session,
    Snapshot,
)

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger("hangar.store")

_TABLES: dict[type[BaseModel], tuple[str, str]] = {
    Sandbox: ("sandboxes", "id"),
    Snapshot: ("snapshots", "id"),
    Credential: ("credentials", "name"),
    ApiToken: ("api_tokens", "id"),
    Job: ("jobs", "id"),
    Session: ("sessions", "id"),
    ScheduledJob: ("schedules", "id"),  # workstream A
    SandboxGrant: ("sandbox_grants", "id"),  # workstream F: git gateway
    Person: ("people", "id"),  # --- identity
    EnvRequest: ("env_requests", "id"),  # --- computer
}
# --- review(perf): access control must not roll back on a node crash (a revoked token coming back), so these
# commits, and the secrets table's, sync the WAL like synchronous=FULL; everything else stays on NORMAL.
_DURABLE: frozenset[type[BaseModel]] = frozenset({ApiToken, Credential, SandboxGrant, Person})  # --- identity: Person
# --- scale: top-level fields records are looked up by, each with an index on its JSON value (an expression index:
# no new column, no rewrite of existing rows). list_where/count_where on these never scan the table.
_INDEXED: dict[type[BaseModel], tuple[str, ...]] = {
    Sandbox: ("state", "name"),
    Job: ("state",),
    Session: ("state",),
    EnvRequest: ("state", "session_id"),  # --- computer
}
# The states in which a record holds, or is about to hold, resources (list_active). Sandbox: fleet.ACTIVE.
_ACTIVE: dict[type[BaseModel], frozenset[str]] = {
    Sandbox: frozenset({SandboxState.PENDING, SandboxState.STARTING, SandboxState.RUNNING, SandboxState.PAUSED}),
    Job: frozenset(JobState) - JOB_TERMINAL,
    Session: frozenset(SESSION_LIVE),
}
# Kinds whose settled records `archive` moves to <table>_archive: out of every listing, still found by get().
_ARCHIVABLE: frozenset[type[BaseModel]] = frozenset({Sandbox})
# --- review: (table, field) of records that point at an archivable one by key. A record something points at is
# neither archived nor deleted: a fork of a snapshot inherits from the snapshot's sandbox (credentials, harness,
# labels, the git gateway's mode), and an exclusive credential's lease names its sandbox. Kept out of the archive
# too, so a control plane rolled back to a version that never reads the archive still finds them.
_REFERENCED_BY: dict[type[BaseModel], tuple[tuple[str, str], ...]] = {
    Sandbox: (("snapshots", "sandbox_id"), ("credentials", "leased_to")),
}
# --- durable: jobs and sessions aren't all held in memory any more (resident.py). What they are looked up and
# listed by is indexed, in the archive too, which listings and lookups by key read along with the main table.
_INDEXED[Job] = (*_INDEXED[Job], "person", "owner", "source_ref", "idempotency_key", "session_id", "branch",
                 "updated_at", "result.pr_number", "result.head_sha", "landing.pr_state", "autofix_state.due_at")
_INDEXED[Session] = (*_INDEXED[Session], "name", "job_id", "parent_id", "harness_session", "branch", "updated_at",
                     "needs_input")
# kind -> (the field listings sort by, newest first; the filters a listing combines with it, each indexed with it)
_ORDERED: dict[type[BaseModel], tuple[str, tuple[str, ...]]] = {
    Job: ("created_at", ("person", "owner", "state")),
    Session: ("last_active_at", ("person",)),
}
_ARCHIVABLE = _ARCHIVABLE | {Job, Session}
# A finished job is archived once nothing needs it in the main table: not while its PR is open or approved, or is
# merged or closed with its session not yet ended (landing.py), nor with auto-fix triggers waiting (autofix.py), nor
# while a schedule's next run updates its PR (landing.open_pr_job). A session with checkpoints can always be resumed,
# so it is never archived, and nor is one a job in the main table points at: the job goes first.
_KEPT: dict[type[BaseModel], str] = {
    Job: "coalesce(json_extract(data, '$.landing.pr_state'), '') IN ('open', 'approved') "
         "OR (coalesce(json_extract(data, '$.landing.pr_state'), '') IN ('merged', 'closed') "
         "AND coalesce(json_extract(data, '$.landing.ended'), 0) = 0) "
         "OR coalesce(json_array_length(data, '$.autofix_state.pending'), 0) > 0",
    Session: "coalesce(json_array_length(data, '$.checkpoints'), 0) > 0",
}
_REFERENCED_BY[Job] = (("schedules", "last_job_id"),)
_REFERENCED_BY[Session] = (("jobs", "session_id"),)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        # perf: in WAL mode NORMAL syncs at checkpoints, not on every commit; a crash of hangar loses nothing and
        # the file stays consistent (only a power loss or OS crash can roll back the last commits). Writes run on
        # the event loop, so an fsync per state change was loop stall on network disks. Verified against
        # sqlite.org/pragma.html#pragma_synchronous and sqlite.org/wal.html. Access-control writes: _DURABLE.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        for table, _ in _TABLES.values():
            self._db.execute(f"CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, blob BLOB NOT NULL)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, data TEXT)"
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        for cls, fields in _INDEXED.items():  # --- scale
            table = _TABLES[cls][0]
            for f in fields:
                self._db.execute(f"CREATE INDEX IF NOT EXISTS {table}_by_{_ix(f)} ON {table}({_field(f)})")  # durable
        for cls in _ARCHIVABLE:
            self._db.execute(f"CREATE TABLE IF NOT EXISTS {_TABLES[cls][0]}_archive "
                             "(key TEXT PRIMARY KEY, data TEXT NOT NULL, archived_at REAL NOT NULL)")
            # --- review: a record is in one table. One in both was handed back by a rollback (docs/upgrading.md),
            # and the previous version wrote only the main table's copy since: the archive's is stale.
            t = _TABLES[cls][0]
            self._db.execute(f"DELETE FROM {t}_archive WHERE key IN (SELECT key FROM {t})")
        for cls, (order, by) in _ORDERED.items():  # --- durable: the same indexes on the archive, and ordered ones
            table = _TABLES[cls][0]
            for t in (table, f"{table}_archive"):
                if t != table:
                    for f in _INDEXED[cls]:
                        self._db.execute(f"CREATE INDEX IF NOT EXISTS {t}_by_{_ix(f)} ON {t}({_field(f)})")
                self._db.execute(f"CREATE INDEX IF NOT EXISTS {t}_by_{order} ON {t}({_field(order)})")
                for f in by:
                    self._db.execute(f"CREATE INDEX IF NOT EXISTS {t}_by_{f}_{order} "
                                     f"ON {t}({_field(f)}, {_field(order)})")
        # --- workstream mcp-modern: called after every put (MCP resource-updated notifications); must not raise
        self.on_put: list[Callable[[BaseModel], None]] = []

    @contextlib.contextmanager
    def _synced(self, durable: bool) -> Iterator[None]:
        """Commit what runs inside with a WAL sync (review: see _DURABLE). Caller holds the lock."""
        if not durable:
            yield
            return
        self._db.execute("PRAGMA synchronous=FULL")
        try:
            yield
        finally:
            self._db.execute("PRAGMA synchronous=NORMAL")

    @contextlib.contextmanager
    def _atomic(self, on: bool) -> Iterator[None]:
        """--- durable: what runs inside commits as one transaction. Caller holds the lock."""
        if not on:
            yield
            return
        self._db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # --- generic model CRUD -------------------------------------------------
    def put(self, obj: BaseModel) -> None:
        table, key_field = _TABLES[type(obj)]
        with self._lock, self._synced(type(obj) in _DURABLE), self._atomic(type(obj) in _ARCHIVABLE):
            self._db.execute(
                f"INSERT INTO {table}(key, data) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (getattr(obj, key_field), obj.model_dump_json()),
            )
            if type(obj) in _ARCHIVABLE:  # --- durable: back in the main table, it is in one place only
                self._db.execute(f"DELETE FROM {table}_archive WHERE key=?", (getattr(obj, key_field),))
        for fn in self.on_put:  # workstream mcp-modern
            try:
                fn(obj)
            except Exception:  # noqa: BLE001 - a listener must never fail a write
                log.exception("store listener failed")

    def get(self, cls: type[T], key: str) -> T | None:
        table, _ = _TABLES[cls]
        with self._lock:
            row = self._db.execute(f"SELECT data FROM {table} WHERE key=?", (key,)).fetchone()
            if row is None and cls in _ARCHIVABLE:  # --- scale: an archived record is still itself by key
                row = self._db.execute(f"SELECT data FROM {table}_archive WHERE key=?", (key,)).fetchone()
        return cls.model_validate_json(row[0]) if row else None

    def list(self, cls: type[T]) -> list[T]:
        table, _ = _TABLES[cls]
        with self._lock:
            rows = self._db.execute(f"SELECT data FROM {table}").fetchall()
        return [cls.model_validate_json(r[0]) for r in rows]

    # --- scale: indexed lookups. Whatever runs per request or per reaper pass uses these, not list().
    def list_where(self, cls: type[T], archived: bool = False, **fields: Any) -> list[T]:
        """Records whose top-level `fields` equal the given values (a collection: any of them), decoding only
        those. Fields in _INDEXED are answered from their index; any other field works but scans the table.
        --- durable: `archived` reads the kind's archive too; Ge, Not and NOT_NULL compare instead of matching."""
        where, params = _where(fields)
        table, _ = _TABLES[cls]
        sql = f"SELECT data FROM {table} WHERE {where}"
        if archived and cls in _ARCHIVABLE:
            sql, params = f"{sql} UNION ALL SELECT data FROM {table}_archive WHERE {where}", params * 2
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [cls.model_validate_json(r[0]) for r in rows]

    def page(self, cls: type[T], limit: int, after: tuple[Any, str] | None = None, archived: bool = False,
             **fields: Any) -> list[tuple[T, Any]]:
        """--- durable: up to `limit` records matching `fields`, newest first by the kind's _ORDERED field, from
        just after `after` (the order value and key of the last record of the page before). A keyset page: what
        any page costs doesn't grow with history, and records added meanwhile don't shift the pages after them.
        Filtering on one of the kind's _ORDERED filters walks that filter's index in order and stops at `limit`.
        --- review: each record comes with its order value as SQLite reads it, which is what `after` must carry:
        SQLite's reading of a JSON number can be a ulp away from Python's, and a cursor holding Python's skipped
        records made at the same instant."""
        order, _ = _ORDERED[cls]
        table, _ = _TABLES[cls]
        o = _field(order)
        where, params = _where(fields) if fields else ("1", [])
        if after is not None:  # a range on the index, then the ties at its edge by key
            where += f" AND {o} <= ? AND NOT ({o} = ? AND key >= ?)"
            params = [*params, after[0], after[0], after[1]]
        sql = f"SELECT key, data, {o} FROM {table} WHERE {where}"
        if archived and cls in _ARCHIVABLE:
            sql, params = f"{sql} UNION ALL SELECT key, data, {o} FROM {table}_archive WHERE {where}", params * 2
        with self._lock:
            rows = self._db.execute(f"{sql} ORDER BY 3 DESC, 1 DESC LIMIT ?", [*params, max(0, limit)]).fetchall()
        return [(cls.model_validate_json(r[1]), r[2]) for r in rows]

    def count_where(self, cls: type[BaseModel], **fields: Any) -> int:
        where, params = _where(fields)
        table, _ = _TABLES[cls]
        with self._lock:
            return self._db.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0]

    def list_active(self, cls: type[T]) -> list[T]:
        """Records in a state that holds resources (_ACTIVE): live sandboxes, unfinished jobs, live sessions."""
        return self.list_where(cls, state=_ACTIVE[cls])

    def archive(self, cls: type[BaseModel], states: Collection[str], before: float, field: str = "last_activity",
                batch: int = 500) -> int:
        """Move up to `batch` records in `states` whose timestamp `field` is older than `before` to the kind's
        archive table, in one transaction, and return how many moved. Call again until it returns less than `batch`:
        small batches keep the lock short for everyone else."""
        if cls not in _ARCHIVABLE:
            raise ValueError(f"{cls.__name__} records are not archived")
        where, params = _where({"state": states})
        table, _ = _TABLES[cls]
        with self._lock:
            kept = f" AND NOT ({_KEPT[cls]})" if cls in _KEPT else ""  # --- durable
            keys = [r[0] for r in self._db.execute(
                f"SELECT key FROM {table} WHERE {where} AND {_field(field)} < ?{_unreferenced(cls)}{kept} LIMIT ?",
                [*params, before, batch]).fetchall()]
            if not keys:
                return 0
            marks = ",".join("?" * len(keys))
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(f"INSERT OR REPLACE INTO {table}_archive(key, data, archived_at) "
                                 f"SELECT key, data, ? FROM {table} WHERE key IN ({marks})", [time.time(), *keys])
                self._db.execute(f"DELETE FROM {table} WHERE key IN ({marks})", keys)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return len(keys)

    def purge_archive(self, cls: type[BaseModel], before: float) -> int:
        """Delete records archived before `before`, except those something still points at; returns how many."""
        if cls not in _ARCHIVABLE:
            raise ValueError(f"{cls.__name__} records are not archived")
        table, _ = _TABLES[cls]
        with self._lock:
            return self._db.execute(f"DELETE FROM {table}_archive WHERE archived_at < ?{_unreferenced(cls)}",
                                    (before,)).rowcount

    def count_archived(self, cls: type[BaseModel]) -> int:
        table, _ = _TABLES[cls]
        with self._lock:
            return self._db.execute(f"SELECT count(*) FROM {table}_archive").fetchone()[0]

    # --- observe: /metrics gauges
    def count_by(self, cls: type[BaseModel], *fields: str, own_connection: bool = False) -> dict[tuple, int]:
        """Records per combination of top-level fields, counted by SQLite without decoding a model each.
        --- durable: `own_connection` counts on a read-only connection of its own, for a worker thread: SQLite still
        reads every row for a GROUP BY (tens of ms for a month of jobs), and WAL lets a reader run beside the
        store's writes instead of holding its lock meanwhile."""
        table, _ = _TABLES[cls]
        if not fields or not all(f.isidentifier() for f in fields):
            raise ValueError(f"bad fields {fields}")
        cols = ", ".join(f"json_extract(data, '$.{f}')" for f in fields)
        sql = f"SELECT {cols}, count(*) FROM {table} GROUP BY {cols}"
        if own_connection and self.path != ":memory:":
            con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                rows = con.execute(sql).fetchall()
            finally:
                con.close()
        else:
            with self._lock:
                rows = self._db.execute(sql).fetchall()
        return {tuple(r[:-1]): r[-1] for r in rows}

    def delete(self, cls: type[BaseModel], key: str) -> None:
        table, _ = _TABLES[cls]
        with self._lock, self._synced(cls in _DURABLE):
            self._db.execute(f"DELETE FROM {table} WHERE key=?", (key,))
            if cls in _ARCHIVABLE:  # --- scale
                self._db.execute(f"DELETE FROM {table}_archive WHERE key=?", (key,))

    # --- encrypted secret blobs (encryption handled by the vault) ----------
    def put_secret(self, name: str, blob: bytes) -> None:
        with self._lock, self._synced(True):
            self._db.execute(
                "INSERT INTO secrets(name, blob) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET blob=excluded.blob",
                (name, blob),
            )

    def get_secret(self, name: str) -> bytes | None:
        with self._lock:
            row = self._db.execute("SELECT blob FROM secrets WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def delete_secret(self, name: str) -> None:
        with self._lock, self._synced(True):
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

    def kv_set(self, key: str, value: str, durable: bool = False) -> None:
        """`durable`: commit with a WAL sync, like _DURABLE models (integrate: #51's stored settings decide who is
        trusted and what is allowed, so a change must not roll back after a power loss)."""
        with self._lock, self._synced(durable):
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def kv_delete(self, key: str, durable: bool = False) -> None:
        with self._lock, self._synced(durable):
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


# --- scale
def _field(name: str) -> str:
    """The SQL for a top-level JSON field: exactly the expression its index was built on, so SQLite uses it.
    --- durable: or a nested one, `result.pr_number`."""
    if not all(p.isidentifier() for p in name.split(".")):
        raise ValueError(f"bad field {name!r}")
    return f"json_extract(data, '$.{name}')"


def _ix(name: str) -> str:
    """--- durable: a field's part of its index's name."""
    return name.replace(".", "_")


class Ge(NamedTuple):
    """--- durable: a list_where/page value matching records whose field is at least this."""

    value: Any


class Not(NamedTuple):
    """--- durable: a list_where/page value matching records whose field is anything but this (or missing)."""

    value: Any


NOT_NULL = object()  # --- durable: a list_where/page value matching records that have the field set


def _unreferenced(cls: type[BaseModel]) -> str:
    """--- review: an `AND` clause leaving out the keys _REFERENCED_BY records point at. NULLs are filtered out of
    each subquery: `key NOT IN (…, NULL)` is never true, which would keep every record."""
    return "".join(f" AND key NOT IN (SELECT {_field(f)} FROM {t} WHERE {_field(f)} IS NOT NULL)"
                   for t, f in _REFERENCED_BY.get(cls, ()))


def _sql_value(v: Any) -> Any:
    return v.value if isinstance(v, Enum) else v


def _where(fields: dict[str, Any]) -> tuple[str, list[Any]]:
    if not fields:
        raise ValueError("name at least one field; list() returns everything")
    parts: list[str] = []
    params: list[Any] = []
    for name, want in fields.items():
        if want is NOT_NULL:  # --- durable
            parts.append(f"{_field(name)} IS NOT NULL")
        elif isinstance(want, Ge):
            parts.append(f"{_field(name)} >= ?")
            params.append(_sql_value(want.value))
        elif isinstance(want, Not):
            parts.append(f"{_field(name)} IS NOT ?")
            params.append(_sql_value(want.value))
        elif isinstance(want, str | bytes) or not isinstance(want, Collection):
            parts.append(f"{_field(name)} IS ?")
            params.append(_sql_value(want))
        else:
            vals = sorted({_sql_value(v) for v in want}, key=str)
            if not vals:
                return "0", []
            parts.append(f"{_field(name)} IN ({','.join('?' * len(vals))})")
            params.extend(vals)
    return " AND ".join(parts), params
