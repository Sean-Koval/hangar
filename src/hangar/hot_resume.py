"""Hot resume (spec 0010 M4): a suspended session keeps its sandbox paused, memory and all, where the provider can
restore a paused sandbox in milliseconds (`hot_resume`: E2B and CubeSandbox microVM snapshots).

Suspend still checkpoints first, exactly as before; parking only replaces the destroy that would follow. Resume
tries the parked sandbox before booting a new one and restoring the checkpoint, and falls back to that whenever the
parked sandbox can't be trusted to be the session's latest state:
- a checkpoint was taken since it was parked (it resumed cold elsewhere, then suspended again);
- it is gone (the reaper took it after HANGAR_SESSION_HOT_RESUME_MINUTES, or the provider lost it);
- resuming it fails, or it doesn't answer a command afterwards.
Nothing here deletes a checkpoint, and a parked sandbox that isn't used is destroyed.

The record of a parked sandbox lives in the store (kv `session:parked:<session id>`), so it survives a restart.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from hangar.fleet import FleetError
from hangar.models import Sandbox, SandboxState, SessionState, now

if TYPE_CHECKING:
    from hangar.models import Session
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.sessions")

KEY = "session:parked:"


def _record(svc: SessionService, sid: str) -> dict | None:
    rec = svc.store.kv_get_json(KEY + sid)
    return rec if isinstance(rec, dict) and isinstance(rec.get("sandbox"), str) else None


def parked_ids(svc: SessionService) -> set[str]:
    """Sandboxes parked for their sessions: not orphans, whatever the session's sandbox_id says."""
    out = set()
    for k in svc.store.kv_keys(KEY):
        if rec := _record(svc, k.removeprefix(KEY)):
            out.add(rec["sandbox"])
    return out


async def park(svc: SessionService, s: Session, sandbox_id: str) -> bool:
    """Pause the session's sandbox instead of destroying it, after its suspend checkpoint. False: the caller destroys
    it as before (the provider can't hot-resume, hot resume is off, or the pause failed)."""
    minutes = svc.settings.session_hot_resume_minutes
    try:
        sb = svc.fleet.get(sandbox_id)
        prov = svc.fleet.provider(sb.provider)
    except FleetError:
        return False
    if minutes <= 0 or not prov.capabilities.hot_resume or sb.state != SandboxState.RUNNING:
        return False
    try:
        await svc.fleet.pause(sb.id, actor=f"session:{s.id}")
    except Exception as e:  # noqa: BLE001 - a failed pause is a cold resume later, never a failed suspend
        log.warning("session %s: could not pause %s for hot resume (%s); destroying it", s.id, sb.id, e)
        return False
    sb = svc.fleet.get(sb.id)
    sb.expires_at = now() + minutes * 60  # the reaper removes it if nobody comes back
    svc.store.put(sb)
    svc.store.kv_set_json(KEY + s.id, {"sandbox": sb.id, "checkpoint": s.checkpoints[-1].id if s.checkpoints
                                       else None, "at": now()})
    return True


async def discard(svc: SessionService, s: Session) -> None:
    """Forget the session's parked sandbox and destroy it (archive, delete, or a cold resume that superseded it)."""
    rec = _record(svc, s.id)
    svc.store.kv_delete(KEY + s.id)
    if rec:
        with contextlib.suppress(Exception):
            await svc.fleet.destroy(rec["sandbox"], actor=f"session:{s.id}")


async def wake(svc: SessionService, s: Session, stage: Callable[[str], None]) -> Sandbox | None:
    """Resume the session's parked sandbox and attach it, or None to boot a fresh one from the checkpoint. Caller holds
    the session lock."""
    rec = _record(svc, s.id)
    if rec is None:
        return None
    latest = s.checkpoints[-1].id if s.checkpoints else None
    try:
        sb = svc.fleet.get(rec["sandbox"])
    except FleetError:
        sb = None
    if (sb is None or sb.state != SandboxState.PAUSED or sb.labels.get("hangar.session") != s.id
            or rec.get("checkpoint") != latest):
        await discard(svc, s)
        return None
    stage("resuming the paused sandbox from its memory snapshot")
    try:
        sb = await svc.fleet.resume(sb.id, actor=f"session:{s.id}")
        res = await svc.fleet.provider(sb.provider).exec(sb, "true", timeout=30)
        if res.exit_code != 0:
            raise RuntimeError(f"a command exited {res.exit_code}")
    except Exception as e:  # noqa: BLE001 - the checkpoint is still there: restore it instead
        log.warning("session %s: hot resume of %s failed (%s); restoring the checkpoint", s.id, sb.id, e)
        await discard(svc, s)
        return None
    svc.store.kv_delete(KEY + s.id)
    ttl = max(s.idle_minutes * 3, 120) + svc.settings.session_turn_timeout_minutes
    sb.expires_at = now() + ttl * 60
    svc.store.put(sb)
    svc.fleet.gateway.extend(sb)  # a gateway grant lives as long as its sandbox
    with contextlib.suppress(Exception):
        await svc.fleet.provider(sb.provider).set_expiry(sb, sb.expires_at)
    s.sandbox_id = sb.id
    s.last_active_at = now()
    svc._set(s, SessionState.IDLE, "ready (resumed from memory)")
    return sb
