"""Who created a branch through hangar (KAN-174, known gap 8).

A session may name its own branch. Outside `hangar/session/` that name is predictable, so another person's sandbox
could create it first and plant commits a new session would then continue. The git gateway records, for each branch
a sandbox creates through it (an update from the zero id), the person its sandbox belongs to; a job's delivery push
records its job's person. `Sessions.new` reads the record: a branch another person's sandbox created is refused,
unless an admin takes it on purpose. A branch hangar never saw created (a person pushed it from their machine, or it
predates this record) is continued as before.

Records live in the store's kv as `gw.created:<owner/repo>:<ref>` with a JSON value.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hangar.models import Sandbox, now

if TYPE_CHECKING:
    from hangar.store import Store

PREFIX = "gw.created:"


def _key(repo: str, ref: str) -> str:
    return f"{PREFIX}{repo.lower()}:{ref if ref.startswith('refs/') else 'refs/heads/' + ref}"


def record(store: Store, repo: str, ref: str, sb: Sandbox | None, via: str, replace: bool = True) -> None:
    """Note that `sb`'s person created `ref` on `repo` ("owner/repo"). `replace=False` keeps an earlier record
    (a delivery push may update a branch it didn't create). A sandbox with no person (a GitHub or webhook job, one
    made before people) is recorded as nobody's, which matches no one."""
    key = _key(repo, ref)
    if not replace and store.kv_get(key) is not None:
        return
    labels = sb.labels if sb else {}
    store.kv_set(key, json.dumps({
        "person": sb.person if sb else None, "sandbox": sb.id if sb else None,
        "session": labels.get("hangar.session"), "job": labels.get("hangar.job"), "via": via, "at": now()}),
        durable=True)


def creator(store: Store, repo: str, branch: str) -> dict[str, Any] | None:
    """The record for `branch` on `repo`, or None when no sandbox created it through hangar."""
    raw = store.kv_get(_key(repo, branch))
    if raw is None:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return {"person": None}
    return rec if isinstance(rec, dict) else {"person": None}


def foreign(rec: dict[str, Any] | None, person: str | None) -> bool:
    """Whether a branch with this record belongs to someone other than `person`: recorded, and not recorded as
    theirs. A record with no person (nobody's sandbox) is foreign to everyone."""
    return rec is not None and (rec.get("person") is None or rec.get("person") != person)
