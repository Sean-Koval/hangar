"""The jobs and sessions held in memory (docs/team-scale.md).

The job and session services used to hold every record ever, about 30 KB for a finished job, so the control plane's
memory grew with a team's history. Now each holds what is live, and what finished or was used in the last
HANGAR_RESIDENT_MINUTES; the rest stays in the store and is read back by key, or found through the store's indexed
queries (`Store.list_where`, `Store.page`), when something asks for it.

A record read back stays in memory while it is in use, so everyone who asks for it gets the same object: the services
change records in place and save them, and two copies of one record would overwrite each other's changes. Iterating
the map gives only what is in memory: whatever must see every record asks the store.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Collection, Iterator, MutableMapping
from typing import Any

from pydantic import BaseModel

from hangar.store import _TABLES, NOT_NULL, Ge, Not, Store


class Resident[T: BaseModel](MutableMapping[str, T]):
    """Records of one kind by key: those in memory, then the store's (archive included) on a miss.

    `pinned(record)` says a record must stay in memory whatever its age: it is live, or something drives it. Any
    other record leaves at the next `evict` once neither saved nor asked for in `window()` seconds."""

    def __init__(self, store: Store, cls: type[T], pinned: Callable[[T], bool], window: Callable[[], float]):
        self.store = store
        self.cls = cls
        self.pinned = pinned
        self.window = window
        self._mem: dict[str, T] = {}
        self._seen: dict[str, float] = {}  # key -> when it was last asked for

    # ---------------------------------------------------------------- the mapping
    def __getitem__(self, key: str) -> T:
        rec = self.get(key)
        if rec is None:
            raise KeyError(key)
        return rec

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        """The record in memory, else the store's, which then stays in memory while it's in use."""
        rec = self._mem.get(key)
        if rec is None and key:
            rec = self.store.get(self.cls, key)
            if rec is not None:
                self._mem[key] = rec
        if rec is None:
            return default
        self._seen[key] = time.time()
        return rec

    def peek(self, key: str) -> T | None:
        """The record if it's in memory; never reads the store."""
        return self._mem.get(key)

    def adopt(self, records: list[T]) -> list[T]:
        """Records the caller read from the store, swapped for the copies in memory where there are some, and kept
        in memory otherwise: whoever changes one changes the only copy."""
        out = []
        t = time.time()
        for rec in records:
            key = self._key(rec)
            mine = self._mem.setdefault(key, rec)
            self._seen[key] = t
            out.append(mine)
        return out

    def where(self, memory: bool = True, **fields: Any) -> list[T]:
        """Records whose fields match (Store.list_where's values: Ge, Not, NOT_NULL; dotted names for nested
        fields), from the store's indexes, archive included. A record in memory is judged by its copy here, which
        has the last word: changed since it was saved, or not saved yet. `memory=False` leaves out the records that
        match only in memory (no pass over them), for lookups by a field that is saved as soon as it is set."""
        found = [r for r in self.adopt(self.store.list_where(self.cls, archived=True, **fields)) if match(r, fields)]
        return found + self._unsaved(found, fields) if memory else found

    def read(self, **fields: Any) -> list[T]:
        """`where`, to read only: records that aren't in memory are returned without being kept there."""
        found = [self._mem.get(self._key(r)) or r for r in self.store.list_where(self.cls, archived=True, **fields)]
        found = [r for r in found if match(r, fields)]
        return found + self._unsaved(found, fields)

    def _unsaved(self, found: list[T], fields: dict[str, Any]) -> list[T]:
        keys = {self._key(r) for r in found}
        return [r for k, r in self._mem.items() if k not in keys and match(r, fields)]

    def __setitem__(self, key: str, rec: T) -> None:
        self._mem[key] = rec
        self._seen[key] = time.time()

    def __delitem__(self, key: str) -> None:
        del self._mem[key]
        self._seen.pop(key, None)

    def __contains__(self, key: object) -> bool:
        """In memory: a record that isn't may still be in the store (get finds it)."""
        return key in self._mem

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._mem))

    def __len__(self) -> int:
        return len(self._mem)

    def values(self) -> list[T]:  # type: ignore[override]
        """What is in memory (a copy: callers change the map while they go through it). Not a use of each."""
        return list(self._mem.values())

    def items(self) -> list[tuple[str, T]]:  # type: ignore[override]
        return list(self._mem.items())

    # ---------------------------------------------------------------- memory
    def evict(self, keep: Callable[[str], bool] = lambda _k: False) -> list[str]:
        """Drop from memory what is neither pinned, nor kept by `keep(key)`, nor saved or asked for within the
        window; returns their keys. Everything here was saved when it last changed, so nothing is lost."""
        horizon = time.time() - self.window()
        out = []
        for key, rec in list(self._mem.items()):
            last = max(getattr(rec, "updated_at", 0.0) or 0.0, self._seen.get(key, 0.0))
            if last >= horizon or self.pinned(rec) or keep(key):
                continue
            del self._mem[key]
            self._seen.pop(key, None)
            out.append(key)
        return out

    def _key(self, rec: T) -> str:
        return getattr(rec, _TABLES[self.cls][1])


def match(rec: BaseModel, fields: dict[str, Any]) -> bool:
    """Store._where's test, on a record in memory."""
    for name, want in fields.items():
        v: Any = rec
        for part in name.split("."):
            v = getattr(v, part, None)
        if want is NOT_NULL:
            ok = v is not None
        elif isinstance(want, Ge):
            ok = v is not None and v >= want.value
        elif isinstance(want, Not):
            ok = v != want.value
        elif isinstance(want, str | bytes) or not isinstance(want, Collection):
            ok = v == want
        else:
            ok = v in want
        if not ok:
            return False
    return True


# ====================================================================== cursors
def cursor(order_value: float, key: str) -> str:
    """Where a listing stands after a record: opaque to callers, who pass it back as `cursor` for the next page."""
    return base64.urlsafe_b64encode(json.dumps([order_value, key]).encode()).decode().rstrip("=")


def after(value: str | None) -> tuple[float, str] | None:
    """A cursor back into (order value, key), or a ValueError a caller can show as is."""
    if not value:
        return None
    try:
        v = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int | float) and isinstance(v[1], str):
            return float(v[0]), v[1]
    except (ValueError, TypeError):
        pass
    raise ValueError("cursor: pass back the `cursor` of the last item of the page before")
