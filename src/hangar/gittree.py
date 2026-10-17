"""Git tree ids of a turn's working tree (--- #165; docs/api/traces.md, `hangar.git.tree*`).

A live turn's driver (driver.Trees) writes the tree after each edit or shell tool result and at the turn's start and
end. A one-shot turn (a harness without a structured mode, or no python3 in the sandbox) has no driver: hangar runs
`script()` in the sandbox at the turn's start and end instead, and records the two as `progress` events with action
`tree`, as the event parser records the driver's. Either way the hash goes through a git directory, index and
object store of its own, never the agent's `.git` (whose config could name filters or an fsmonitor to run).

A tree recorded where the agent could have written it (a driver without the uid split, or an exec as the sandbox's
user) is `trusted: false`; #179 re-verifies trees when it replays a prefix anyway.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shlex
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hangar.models import Sandbox, Session, SessionTurn
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.gittree")

SLOW = 2.0  # seconds, as driver.TREE_SLOW
_SHA = re.compile(r"^[0-9a-f]{40,64}$")


def script(repo: str) -> str:
    """The working tree's id on stdout (nothing when it isn't a git checkout or git fails): `git add -A` and
    `git write-tree` with GIT_DIR, the index and new objects in a fresh temporary directory, the repo's objects read
    as alternates and its index copied for the stat cache. The agent's `.git` is only read."""
    q = shlex.quote
    git = ("env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null "
           "GIT_OPTIONAL_LOCKS=0 GIT_TERMINAL_PROMPT=0 GIT_DIR=\"$_hd/git\" GIT_WORK_TREE=\"$_top\" "
           "GIT_INDEX_FILE=\"$_hd/index\" GIT_OBJECT_DIRECTORY=\"$_hd/git/objects\" "
           "GIT_ALTERNATE_OBJECT_DIRECTORIES=\"$_common/objects\" "
           "git -c safe.directory='*' -c core.fsmonitor=false -c core.hooksPath=/dev/null")
    return (f'cd {q(repo)} 2>/dev/null || exit 0; _top=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0; '
            '_common=$(cd "$_top" && cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd) || exit 0; '
            '_hd=$(mktemp -d) || exit 0; '
            'GIT_DIR="$_hd/git" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git init -q >/dev/null 2>&1; '
            'cp "$_common/index" "$_hd/index" 2>/dev/null; '
            f'{{ {git} add -A >/dev/null 2>&1 && {git} write-tree 2>/dev/null; }}; rm -rf "$_hd"; true')


async def oneshot(svc: SessionService, s: Session, turn: SessionTurn, sb: Sandbox, run_id: str | None,
                  phase: str, n: int | None = None) -> str | None:
    """A one-shot turn's tree at its `start` or `end`, recorded as an event. Never raises; None when it couldn't
    tell (no repo, the sandbox gone, slower than SLOW: then the end isn't hashed either)."""
    from hangar.events import tree_events

    try:
        if turn.live or not run_id:
            return None
        if phase == "end" and skipped(svc, run_id):
            svc.__dict__["_tree_off"].discard(run_id)
            return None
        repo = sb.info.get("repo_path")
        if not repo:
            return None
        t0 = time.monotonic()
        res = await svc.fleet.exec(sb.id, script(repo), timeout=30, actor=f"session:{s.id}", quiet=True)
        ms = int((time.monotonic() - t0) * 1000)
        sha = (res.stdout or "").strip().splitlines()[-1:] or [""]
        tree = sha[0] if _SHA.match(sha[0]) else None
        if ms > SLOW * 1000:
            evs = tree_events(None, phase=phase, ms=ms, skipped="slow")
            if phase == "start":
                svc.__dict__.setdefault("_tree_off", set()).add(run_id)
            tree = None
        elif tree is None:
            return None
        else:
            evs = tree_events(tree, phase=phase, ms=ms)
        number = n or next((i for i, t in enumerate(s.turns, 1) if t is turn), len(s.turns))
        for ev in evs:
            ev.turn, ev.run_id = number, run_id
            ev.data["trusted"] = False  # an exec as the sandbox's user: what it runs is the agent's to change
        with contextlib.suppress(OSError):
            svc.activity.append(s.id, evs)
        return tree
    except Exception as e:  # noqa: BLE001 - a turn never waits on, or fails over, its trees
        log.info("session %s: no %s tree for %s: %s", s.id, phase, run_id, e)
        return None


def skipped(svc: Any, run_id: str | None) -> bool:
    """The start hash was slow: the end isn't hashed either."""
    return bool(run_id) and run_id in svc.__dict__.get("_tree_off", set())
