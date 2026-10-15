"""Where build caches live (--- caches; docs/caches.md): encrypted, content-addressed chunks and an index.

buildcache.py decides who may read and write what; this module keeps the bytes and answers lookups.

* **Objects** are split into CHUNK-sized pieces, each named by the SHA-256 of its plaintext and sealed with the vault
  key (Fernet, as checkpoints are) before it reaches the blob store: the session bucket's `caches/` prefix, a bucket
  of its own (HANGAR_BUILD_CACHE_STORE), or the data volume. Chunks live under their repo's own prefix, so equal
  content in two repos is two objects, and nothing a repo can ask for names another repo's data.
* **Integrity.** A read decrypts each chunk (Fernet checks it was sealed with a vault key), checks its SHA-256 against
  its name, and the whole object's SHA-256 against the entry. Anything that fails is a miss, and the entry goes.
* **The index** is a SQLite file of its own (`caches.db` on the data volume): compile caches write thousands of small
  entries per build, which don't belong in hangar's state database and its backups. Losing it loses caches only; the
  nightly sweep deletes chunks no entry points at.
* **Entries** are (repo, layer, namespace, key) -> object. Lookups take the layers in order, an exact key first, then
  each restore prefix, newest first (GitHub Actions' `restore-keys`).
* **Retention.** Totals per repo and overall are kept under HANGAR_BUILD_CACHE_REPO_MAX_GB and _MAX_GB, the least
  recently used entries going first; entries unused for HANGAR_BUILD_CACHE_RETENTION_DAYS (private layers:
  _PRIVATE_DAYS) go in the nightly pass.

A vault key that is retired makes what it sealed unreadable: those entries turn into misses and are dropped. Caches
are rebuilt by the next trusted build, so a key rotation doesn't re-encrypt them (their objects aren't `.enc`).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
import time
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken

from hangar.membudget import MemoryBudget

if TYPE_CHECKING:
    from hangar.checkpoints import CheckpointStore
    from hangar.vault import Vault

log = logging.getLogger("hangar.caches")

CHUNK = 8 * 1024 * 1024
TOUCH_EVERY = 60.0  # seconds between `used` updates of one entry: a hot entry isn't a write per hit
SWEEP_GRACE = 3600.0  # an object younger than this may belong to an upload still running
INFLATE_STEP = 4 * 1024 * 1024  # bytes inflated at a time while a trusted upload is checked for build secrets
# review(#115): an upload's hold on the memory budget, in multiples of what it buffers (up to a chunk): the buffer, the
# chunk's copy, the sealed token and its base64
UPLOAD_HOLD = 4
IDLE_SECONDS = 60.0  # an upload that sends nothing this long is cut off (and lets go of its memory)
TRUSTED = "trusted:"  # + the environment: a repo's shared layer, per environment its builds run in
PRIVATE = "private:"  # + the session (or sandbox) and branch


def shared(environment: str) -> str:
    """The shared layer of a repo's builds in one environment."""
    return f"{TRUSTED}{environment}"


class CacheError(RuntimeError):
    """An object hangar won't store (too big, a build secret in it, a digest that doesn't match its name)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Entry:
    repo: str
    layer: str
    ns: str
    key: str
    digest: str  # sha256 of the whole object
    size: int
    chunks: list[str]
    created: float
    used: float
    writer: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def repo_id(repo: str) -> str:
    """A repo's prefix in the blob store: fixed length, no characters a store could read as a path."""
    return hashlib.sha256(repo.encode()).hexdigest()[:24]


def object_key(repo: str, digest: str) -> str:
    return f"{repo_id(repo)}/{digest[:2]}/{digest}.blk"


def location(settings: Any) -> str | None:
    """HANGAR_BUILD_CACHE_STORE, else the session bucket's `caches/` prefix, else None (the data volume)."""
    if settings.build_cache_store:
        return settings.build_cache_store
    if (settings.session_store or "").startswith("gs://"):
        return settings.session_store.rstrip("/") + "/caches"
    return None


def total_cap_bytes(settings: Any) -> int:
    """HANGAR_BUILD_CACHE_MAX_GB, else 100 GB in a bucket and 10 GB on the data volume (which the database shares)."""
    gb = settings.build_cache_max_gb
    if gb is None:
        gb = 100.0 if (location(settings) or "").startswith("gs://") else 10.0
    return int(gb * (1 << 30))


class Index:
    """The entries and chunk reference counts, in data_dir/caches.db. Every method is short and synchronous; callers
    on the event loop keep them that way (no scan of the whole table outside the nightly pass)."""

    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("CREATE TABLE IF NOT EXISTS entries (repo TEXT, layer TEXT, ns TEXT, key TEXT, digest TEXT, "
                         "size INTEGER, chunks TEXT, created REAL, used REAL, writer TEXT, meta TEXT, "
                         "PRIMARY KEY (repo, layer, ns, key))")
        self._db.execute("CREATE INDEX IF NOT EXISTS entries_used ON entries(used)")
        self._db.execute("CREATE INDEX IF NOT EXISTS entries_repo_used ON entries(repo, used)")
        self._db.execute("CREATE TABLE IF NOT EXISTS chunks (repo TEXT, digest TEXT, size INTEGER, refs INTEGER, "
                         "created REAL, PRIMARY KEY (repo, digest))")
        self._db.execute("CREATE INDEX IF NOT EXISTS chunks_refs ON chunks(refs)")
        # review(#115): bytes of chunk rows per repo, kept as rows come and go, so the caps a write checks aren't a
        # GROUP BY over every chunk (a build writes thousands of compile-cache entries, each a check)
        fresh = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='totals'").fetchone() is None
        self._db.execute("CREATE TABLE IF NOT EXISTS totals (repo TEXT PRIMARY KEY, bytes INTEGER)")
        if fresh:  # an index from before the table: count once
            self._db.execute("INSERT OR REPLACE INTO totals SELECT repo, sum(size) FROM chunks GROUP BY repo")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextlib.contextmanager
    def _tx(self) -> Any:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    @staticmethod
    def _entry(row: tuple) -> Entry:
        return Entry(repo=row[0], layer=row[1], ns=row[2], key=row[3], digest=row[4], size=row[5],
                     chunks=json.loads(row[6]), created=row[7], used=row[8], writer=row[9] or "",
                     meta=json.loads(row[10] or "{}"))

    _COLS = "repo, layer, ns, key, digest, size, chunks, created, used, writer, meta"

    def get(self, repo: str, layer: str, ns: str, key: str) -> Entry | None:
        with self._lock:
            row = self._db.execute(f"SELECT {self._COLS} FROM entries WHERE repo=? AND layer=? AND ns=? AND key=?",
                                   (repo, layer, ns, key)).fetchone()
        return self._entry(row) if row else None

    def lookup(self, repo: str, layers: list[str], ns: str, key: str,
               prefixes: list[str] | None = None) -> Entry | None:
        """The exact key in the first layer that has it; else, for each prefix in turn, the newest entry under it,
        in the first layer that has one."""
        for layer in layers:
            if e := self.get(repo, layer, ns, key):
                return e
        for p in prefixes or []:
            if not p:
                continue
            upper = p[:-1] + chr(ord(p[-1]) + 1)  # keys under the prefix, as a range the primary key serves
            for layer in layers:
                with self._lock:
                    row = self._db.execute(
                        f"SELECT {self._COLS} FROM entries WHERE repo=? AND layer=? AND ns=? AND key >= ? AND key < ? "
                        "ORDER BY created DESC LIMIT 1", (repo, layer, ns, p, upper)).fetchone()
                if row:
                    return self._entry(row)
        return None

    def has_chunk(self, repo: str, digest: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM chunks WHERE repo=? AND digest=?", (repo, digest)).fetchone() \
                is not None

    def commit(self, e: Entry, sizes: dict[str, int]) -> list[tuple[str, int]]:
        """Add (or replace) an entry and count its chunks; the chunks the replaced entry no longer needs come back
        (refs 0) for the caller to free."""
        with self._tx() as db:
            old = db.execute("SELECT chunks FROM entries WHERE repo=? AND layer=? AND ns=? AND key=?",
                             (e.repo, e.layer, e.ns, e.key)).fetchone()
            for d in e.chunks:
                if db.execute("INSERT INTO chunks(repo, digest, size, refs, created) VALUES(?, ?, ?, 1, ?) "
                              "ON CONFLICT(repo, digest) DO NOTHING", (e.repo, d, sizes[d], e.created)).rowcount:
                    db.execute("INSERT INTO totals(repo, bytes) VALUES(?, ?) "
                               "ON CONFLICT(repo) DO UPDATE SET bytes=bytes+excluded.bytes", (e.repo, sizes[d]))
                else:
                    db.execute("UPDATE chunks SET refs=refs+1 WHERE repo=? AND digest=?", (e.repo, d))
            db.execute(f"INSERT OR REPLACE INTO entries({self._COLS}) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (e.repo, e.layer, e.ns, e.key, e.digest, e.size, json.dumps(e.chunks), e.created, e.used,
                        e.writer, json.dumps(e.meta)))
            return self._release(db, e.repo, json.loads(old[0])) if old else []

    @staticmethod
    def _release(db: sqlite3.Connection, repo: str, chunks: list[str]) -> list[tuple[str, int]]:
        """Drop one reference per chunk; (digest, size) of those nothing references any more."""
        freed = []
        for d in chunks:
            db.execute("UPDATE chunks SET refs=refs-1 WHERE repo=? AND digest=?", (repo, d))
            row = db.execute("SELECT refs, size FROM chunks WHERE repo=? AND digest=?", (repo, d)).fetchone()
            if row and row[0] <= 0 and (d, row[1]) not in freed:
                freed.append((d, int(row[1])))
        return freed

    def touch(self, e: Entry, t: float) -> None:
        if t - e.used < TOUCH_EVERY:
            return
        with self._lock:
            self._db.execute("UPDATE entries SET used=? WHERE repo=? AND layer=? AND ns=? AND key=?",
                             (t, e.repo, e.layer, e.ns, e.key))

    def delete(self, e: Entry) -> list[tuple[str, int]]:
        with self._tx() as db:
            row = db.execute("SELECT chunks FROM entries WHERE repo=? AND layer=? AND ns=? AND key=?",
                             (e.repo, e.layer, e.ns, e.key)).fetchone()
            if not row:
                return []
            db.execute("DELETE FROM entries WHERE repo=? AND layer=? AND ns=? AND key=?",
                       (e.repo, e.layer, e.ns, e.key))
            return self._release(db, e.repo, json.loads(row[0]))

    def drop_chunk(self, repo: str, digest: str) -> bool:
        """Forget a chunk nothing points at. False when an entry took it again meanwhile."""
        with self._tx() as db:
            row = db.execute("SELECT refs, size FROM chunks WHERE repo=? AND digest=?", (repo, digest)).fetchone()
            if row and row[0] > 0:
                return False
            if row:
                db.execute("DELETE FROM chunks WHERE repo=? AND digest=?", (repo, digest))
                db.execute("UPDATE totals SET bytes=bytes-? WHERE repo=?", (int(row[1]), repo))
                db.execute("DELETE FROM totals WHERE repo=? AND bytes<=0", (repo,))
            return True

    def where(self, sql: str, args: tuple = (), limit: int = 1000) -> list[Entry]:
        with self._lock:
            rows = self._db.execute(f"SELECT {self._COLS} FROM entries WHERE {sql} LIMIT {int(limit)}",
                                    args).fetchall()
        return [self._entry(r) for r in rows]

    def stored(self, repo: str | None = None) -> int:
        """Bytes of chunks (plaintext) held, for one repo or all: what the caps count."""
        with self._lock:
            if repo is None:
                row = self._db.execute("SELECT coalesce(sum(bytes), 0) FROM totals").fetchone()
            else:
                row = self._db.execute("SELECT coalesce(sum(bytes), 0) FROM totals WHERE repo=?", (repo,)).fetchone()
        return int(row[0])

    def repos_over(self, cap: int) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT repo FROM totals WHERE bytes > ?", (cap,)).fetchall()]

    def usage(self) -> list[dict[str, Any]]:
        """Per repo: bytes held, entries, bytes of entries in the trusted and private layers, last use."""
        with self._lock:
            held = dict(self._db.execute("SELECT repo, bytes FROM totals").fetchall())
            rows = self._db.execute(
                "SELECT repo, count(*), sum(CASE WHEN substr(layer, 1, ?)=? THEN size ELSE 0 END), "
                "sum(CASE WHEN substr(layer, 1, ?)=? THEN 0 ELSE size END), max(used) FROM entries GROUP BY repo",
                (len(TRUSTED), TRUSTED, len(TRUSTED), TRUSTED)).fetchall()
        out = [{"repo": r, "bytes": int(held.get(r) or 0), "entries": n, "trusted_bytes": int(t or 0),
                "private_bytes": int(p or 0), "last_used": u} for r, n, t, p, u in rows]
        return sorted(out, key=lambda x: -x["bytes"])

    def unreferenced(self, limit: int = 1000) -> list[tuple[str, str]]:
        with self._lock:
            return self._db.execute("SELECT repo, digest FROM chunks WHERE refs<=0 LIMIT ?", (limit,)).fetchall()

    def chunk_names(self) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT repo, digest FROM chunks").fetchall()
        return {object_key(r, d) for r, d in rows}


class CacheStore:
    """The blob store and the index together: ingest, read, evict, purge."""

    def __init__(self, blobs: CheckpointStore, index: Index, vault: Vault, settings: Any):
        self.blobs, self.index, self.vault, self.settings = blobs, index, vault, settings
        self.memory = MemoryBudget(settings.build_cache_transfer_mb * 1024 * 1024)
        self._pinned: collections.Counter[tuple[str, str]] = collections.Counter()  # chunks of uploads in flight
        self._evicting: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._lockers: collections.Counter[tuple[str, str]] = collections.Counter()
        self.stats: collections.Counter[str] = collections.Counter()

    @property
    def description(self) -> str:
        return self.blobs.description

    # ------------------------------------------------------------------ writing
    async def ingest(self, repo: str, stream: AsyncIterator[bytes], max_bytes: int, *,
                     expect: str | None = None, needles: list[bytes] | None = None,
                     size_hint: int | None = None) -> tuple[str, int, list[str], dict[str, int]]:
        """Store a stream as chunks: (sha256, size, chunk digests, chunk sizes). Nothing is indexed here; `commit`
        does that. `expect`: the object's sha256 must be this (Bazel's CAS). `needles`: build secret values that
        must not be in the object (gzip is looked into). `size_hint`: the body's Content-Length, when it has one.

        review(#115): what the upload holds counts against the memory budget for as long as it runs: its buffer (up
        to a chunk), the chunk's copy, and the sealed token with its base64. A body with a length holds that much;
        one without (chunked), a whole chunk's worth. A client that stops sending for IDLE_SECONDS is cut off, so
        nobody keeps budget by trickling."""
        whole = hashlib.sha256()
        size = 0
        digests: list[str] = []
        sizes: dict[str, int] = {}
        put: list[str] = []
        pins: list[tuple[str, str]] = []
        scan = _Scanner(needles) if needles else None
        buf = bytearray()
        reserve = UPLOAD_HOLD * min(CHUNK, max(0, size_hint) if size_hint is not None else CHUNK)
        try:
            async with self.memory.hold(reserve):
                pieces = stream.__aiter__()
                while True:
                    try:
                        piece = await asyncio.wait_for(pieces.__anext__(), IDLE_SECONDS)
                    except StopAsyncIteration:
                        break
                    except TimeoutError as e:
                        raise CacheError(408, f"the upload sent nothing for {IDLE_SECONDS:g}s") from e
                    size += len(piece)
                    if size > max_bytes:
                        raise CacheError(413, f"over HANGAR_BUILD_CACHE_OBJECT_MAX_MB ({max_bytes >> 20} MB)")
                    whole.update(piece)
                    if scan is not None:
                        scan.feed(piece)
                    buf += piece
                    while len(buf) >= CHUNK:
                        await self._chunk(repo, bytes(buf[:CHUNK]), digests, sizes, put, pins)
                        del buf[:CHUNK]
                if buf or not digests:
                    await self._chunk(repo, bytes(buf), digests, sizes, put, pins)
            if scan is not None:
                scan.end()
            digest = whole.hexdigest()
            if expect is not None and digest != expect:
                raise CacheError(400, "the body's sha256 isn't the digest it was stored under")
            return digest, size, digests, sizes
        except BaseException:
            await self._unput(repo, put)
            raise
        finally:
            for p in pins:
                self._unpin(p)

    def _unpin(self, p: tuple[str, str]) -> None:
        self._pinned[p] -= 1
        if self._pinned[p] <= 0:
            del self._pinned[p]

    async def _chunk(self, repo: str, data: bytes, digests: list[str], sizes: dict[str, int], put: list[str],
                     pins: list[tuple[str, str]]) -> None:
        d = hashlib.sha256(data).hexdigest()
        digests.append(d)
        sizes[d] = len(data)
        if (repo, d) not in pins:
            pins.append((repo, d))
            self._pinned[(repo, d)] += 1  # eviction leaves it alone until this upload commits or fails
        if d in put:
            return
        async with self._guard((repo, d)):
            if self.index.has_chunk(repo, d):
                return
            blob = await asyncio.to_thread(self.vault.encrypt, data)  # the memory: ingest's hold covers it
            await self.blobs.put(object_key(repo, d), blob)
        put.append(d)

    async def _unput(self, repo: str, put: list[str]) -> None:
        """An upload that failed: the objects it stored that nothing else has taken."""
        for d in put:
            async with self._guard((repo, d)):
                if not self.index.has_chunk(repo, d) and self._pinned[(repo, d)] <= 1:
                    with contextlib.suppress(Exception):
                        await self.blobs.delete(object_key(repo, d))

    def commit(self, repo: str, layer: str, ns: str, key: str, digest: str, size: int, chunks: list[str],
               sizes: dict[str, int], writer: str, meta: dict[str, Any] | None = None) -> Entry:
        t = time.time()
        e = Entry(repo=repo, layer=layer, ns=ns, key=key, digest=digest, size=size, chunks=chunks, created=t, used=t,
                  writer=writer, meta=meta or {})
        freed = self.index.commit(e, sizes)
        self.stats["writes"] += 1
        self._free(repo, freed)
        self.maybe_evict()
        return e

    # ------------------------------------------------------------------ reading
    async def first(self, e: Entry) -> bytes | None:
        """The entry's first chunk, checked: read before a response starts, so a damaged entry is a plain miss."""
        return await self._read(e, 0)

    async def _read(self, e: Entry, i: int) -> bytes | None:
        d = e.chunks[i]
        data = blob = None
        async with self.memory.hold(min(e.size, CHUNK) * 3):  # the token as read, its base64 decoded, the plaintext
            try:
                blob = await self.blobs.get(object_key(e.repo, d))
            except Exception as ex:  # noqa: BLE001 - the store is down: a miss now, nothing deleted
                log.warning("build cache: can't read %s of %s: %s", d[:12], e.repo, type(ex).__name__)
                self.stats["read_errors"] += 1
                return None
            if blob is not None:
                with contextlib.suppress(InvalidToken):
                    data = await asyncio.to_thread(self.vault.decrypt, blob)
        if data is None or hashlib.sha256(data).hexdigest() != d:
            self.damaged(e, "missing" if blob is None else "failed its integrity check")
            return None
        return data

    async def stream(self, e: Entry, first: bytes) -> AsyncIterator[bytes]:
        """The entry's bytes after `first`. A chunk that fails its check ends the stream short (the client sees a
        truncated body, which every cache client treats as a failed fetch) and the entry goes."""
        whole = hashlib.sha256(first)
        async with self.memory.hold(len(first)):  # review(#115): a chunk the client hasn't taken yet is still held
            yield first
        for i in range(1, len(e.chunks)):
            data = await self._read(e, i)
            if data is None:
                raise CacheError(502, "a chunk failed its integrity check")
            whole.update(data)
            async with self.memory.hold(len(data)):
                yield data
        if whole.hexdigest() != e.digest:
            self.damaged(e, "didn't match its digest")
            raise CacheError(502, "the object didn't match its digest")

    async def read_all(self, e: Entry, limit: int) -> bytes | None:
        """A small entry whole (and checked), or None."""
        if e.size > limit:
            return None
        first = await self.first(e)
        if first is None:
            return None
        out = bytearray()
        try:
            async for piece in self.stream(e, first):
                out += piece
        except CacheError:
            return None
        return bytes(out)

    def damaged(self, e: Entry, why: str) -> None:
        log.warning("build cache: %s %s/%s in %s %s; dropping the entry", e.repo, e.ns, e.key[:80], e.layer, why)
        self.stats["integrity_failures"] += 1
        self._free(e.repo, self.index.delete(e))

    # ------------------------------------------------------------------ freeing
    def _free(self, repo: str, freed: list[tuple[str, int]]) -> None:
        """Delete, in the background, the objects of chunks nothing references (a pinned one waits for the sweep)."""
        todo = [d for d, _ in freed if (repo, d) not in self._pinned]
        if todo:
            t = asyncio.get_running_loop().create_task(self._delete_chunks(repo, todo))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _delete_chunks(self, repo: str, digests: list[str]) -> None:
        for d in digests:
            async with self._guard((repo, d)):  # an upload of the same chunk waits, and puts it back after
                if (repo, d) in self._pinned or not self.index.drop_chunk(repo, d):
                    continue
                try:
                    await self.blobs.delete(object_key(repo, d))
                except Exception:  # noqa: BLE001 - the nightly sweep deletes what's left over
                    log.info("build cache: couldn't delete chunk %s of %s", d[:12], repo, exc_info=True)

    @contextlib.asynccontextmanager
    async def _guard(self, k: tuple[str, str]) -> AsyncIterator[None]:
        """One chunk's put and delete, one at a time: a delete that started before an upload of the same bytes must
        not remove the object the upload stored after it."""
        lock = self._locks.get(k)
        if lock is None:
            lock = self._locks[k] = asyncio.Lock()
        self._lockers[k] += 1
        try:
            async with lock:
                yield
        finally:
            self._lockers[k] -= 1
            if self._lockers[k] <= 0:
                del self._lockers[k]
                self._locks.pop(k, None)

    async def drain(self) -> None:
        """Wait for the deletes in flight (tests, shutdown). Only unfinished tasks are awaited: a finished one can still
        be listed until its discard callback runs, and gather() over finished futures doesn't yield, so waiting on
        those would spin without ever letting that callback run."""
        while pending := [t for t in (*self._tasks, self._evicting) if t is not None and not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._evicting is not None and self._evicting.done():
            self._evicting = None

    def maybe_evict(self) -> None:
        if self._evicting is None or self._evicting.done():
            self._evicting = asyncio.get_running_loop().create_task(self.evict())

    async def evict(self) -> int:
        """Down to 90% of each cap, the repo's then the total: private layers' entries first (a session can't push
        the repo's shared caches out), least recently used first. Returns how many entries went."""
        total_cap = total_cap_bytes(self.settings)
        repo_cap = int(self.settings.build_cache_repo_max_gb * (1 << 30))
        n = 0
        for repo in self.index.repos_over(repo_cap) if repo_cap else []:
            n += await self._evict_down(self.index.stored(repo), int(repo_cap * 0.9), "repo=?", (repo,))
        if total_cap and (held := self.index.stored()) > total_cap:
            n += await self._evict_down(held, int(total_cap * 0.9), "1=1", ())
        if n:
            self.stats["evicted"] += n
            log.info("build cache: evicted %d entries (caps %s GB per repo, %.1f GB total)", n,
                     self.settings.build_cache_repo_max_gb, total_cap / (1 << 30))
        return n

    async def _evict_down(self, held: int, target: int, sql: str, args: tuple) -> int:
        """Delete entries, private layers' first, then least recently used first, until `held` bytes (counted down
        as chunks are freed) reach `target`."""
        n = 0
        while held > target:
            batch = self.index.where(f"{sql} ORDER BY substr(layer, 1, {len(TRUSTED)}) = '{TRUSTED}', used ASC",
                                     args, limit=200)
            if not batch:
                break
            for e in batch:
                freed = self.index.delete(e)
                held -= sum(size for _, size in freed)
                self._free(e.repo, freed)
                n += 1
                if held <= target:
                    break
            await asyncio.sleep(0)
        return n

    async def expire(self, t: float | None = None) -> int:
        """Entries unused for the retention period (private layers: their shorter one)."""
        t = t or time.time()
        n = 0
        for sql, args in (("used < ?", (t - self.settings.build_cache_retention_days * 86400,)),
                          ("layer LIKE ? AND used < ?", (PRIVATE + "%", t - self.settings.build_cache_private_days
                                                         * 86400))):
            while batch := self.index.where(sql, args, limit=500):
                for e in batch:
                    self._free(e.repo, self.index.delete(e))
                    n += 1
                await asyncio.sleep(0)
        return n

    async def purge(self, repo: str | None, layer: str | None = None) -> int:
        """Every entry of a repo (or of every repo), or only of one layer: `trusted` or `private` for every shared or
        private one."""
        sql, args = [], []
        if repo:
            sql.append("repo=?")
            args.append(repo)
        if layer in ("private", "trusted"):
            prefix = PRIVATE if layer == "private" else TRUSTED
            sql.append("substr(layer, 1, ?)=?")
            args += [len(prefix), prefix]
        elif layer:
            sql.append("layer=?")
            args.append(layer)
        where = " AND ".join(sql) or "1=1"
        n = 0
        while batch := self.index.where(where, tuple(args), limit=500):
            for e in batch:
                self._free(e.repo, self.index.delete(e))
                n += 1
            await asyncio.sleep(0)
        return n

    async def sweep(self) -> int:
        """Objects in the store no chunk row names (an index that was lost, an upload cut short by a restart), and
        rows nothing references any more. Objects younger than SWEEP_GRACE are left: they may be an upload's."""
        n = 0
        for repo, d in self.index.unreferenced():
            if (repo, d) not in self._pinned and self.index.drop_chunk(repo, d):
                with contextlib.suppress(Exception):
                    await self.blobs.delete(object_key(repo, d))
                n += 1
        known = self.index.chunk_names()
        pinned = {object_key(r, d) for r, d in self._pinned}
        try:
            names = await self.blobs.list("")
        except Exception:  # noqa: BLE001 - next night
            log.warning("build cache: can't list %s for the sweep", self.description, exc_info=True)
            return n
        for name in names:
            if not name.endswith(".blk") or name in known or name in pinned:
                continue
            if not await self._old(name):
                continue
            with contextlib.suppress(Exception):
                await self.blobs.delete(name)
                n += 1
        return n

    async def _old(self, name: str) -> bool:
        """Whether an object is older than SWEEP_GRACE (always, for stores that can't tell)."""
        root = getattr(self.blobs, "root", None)
        if root is None:
            return True
        with contextlib.suppress(OSError):
            return time.time() - (Path(root) / name).stat().st_mtime > SWEEP_GRACE
        return False


# review(#115): formats a build-secret scan can't see into (sccache's entries are zip archives of zstd blobs, turbo's
# artifacts tar.zst): with build secrets around, uploads in them are refused. Magic numbers from each format's spec.
OPAQUE = (b"\x28\xb5\x2f\xfd", b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\xfd7zXZ\x00", b"BZh",
          b"7z\xbc\xaf\x27\x1c", b"\x04\x22\x4d\x18")
_SNIFF = b"\x00" * 6  # enough of the start to tell them apart


class _Scanner:
    """Build secret values in a stream, looked for in the gzip-decompressed bytes when the stream is gzip (as
    dependency archives are) and in the raw bytes otherwise, across chunk boundaries. Raises CacheError on a hit."""

    def __init__(self, needles: list[bytes]):
        self.needles = [n for n in needles if len(n) >= 8]
        self.keep = max((len(n) for n in self.needles), default=1) - 1
        self.tail_raw = b""
        self.tail_plain = b""
        self.inflate: Any = None
        self.started = False
        self.head = b""  # the first bytes, until there are enough to tell the format

    def _look(self, buf: bytes) -> None:
        if any(n in buf for n in self.needles):
            raise CacheError(422, "a build secret is in the cache archive; nothing was stored")

    def feed(self, piece: bytes) -> None:
        if not self.needles:
            return
        if not self.started and not self.inflate:
            if len(self.head) + len(piece) < len(_SNIFF):
                self.head += piece
                return
            piece, self.head = self.head + piece, b""
            self._sniff(piece)
        self._feed(piece)

    def _sniff(self, first: bytes) -> None:
        self.started = True
        if first[:2] == b"\x1f\x8b":
            self.inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif first.startswith(OPAQUE):  # review(#115): a secret in there is invisible to a scan of the raw bytes
            raise CacheError(422, "this build has build secrets, and hangar can't look into this upload's format "
                                  "(zstd, zip, xz, bzip2, 7z or lz4) to check it holds none; nothing was stored")

    def _feed(self, piece: bytes) -> None:
        buf = self.tail_raw + piece
        self._look(buf)
        self.tail_raw = buf[-self.keep:] if self.keep else b""
        data = piece
        while self.inflate is not None and data:
            try:  # bounded: a small archive can inflate a thousandfold
                plain = self.inflate.decompress(data, INFLATE_STEP)
            except zlib.error as e:  # review(#115): what isn't inflated isn't checked
                raise CacheError(422, "a damaged gzip stream from a build with build secrets; nothing was "
                                      "stored") from e
            data = self.inflate.unconsumed_tail
            buf = self.tail_plain + plain
            self._look(buf)
            self.tail_plain = buf[-self.keep:] if self.keep else b""
            if self.inflate.eof:  # gzip members after the first (pigz, concatenated streams): a new inflater
                data = self.inflate.unused_data + data
                if data[:2] != b"\x1f\x8b" and len(data) >= 2:  # review(#115): not gzip, so not looked into
                    raise CacheError(422, "data after the gzip stream from a build with build secrets; nothing "
                                          "was stored")
                self.inflate = zlib.decompressobj(16 + zlib.MAX_WBITS) if data else None
                self.started = bool(data)  # nothing left of this piece: the next one may start a member

    def end(self) -> None:
        if self.head:  # an object shorter than a format's magic: its raw bytes are all there is
            piece, self.head = self.head, b""
            self._sniff(piece)
            self._feed(piece)
        if self.inflate is not None:
            with contextlib.suppress(zlib.error):
                self._look(self.tail_plain + self.inflate.flush())
