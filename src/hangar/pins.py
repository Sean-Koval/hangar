"""What a session is pinned to when it is created (--- canary, docs/harnesses.md).

* **Its model**, as a concrete id: a policy (`frontier`, `balanced`) is resolved once, from the provider's model list
  (modelpolicy.py), so the conversation never changes model when the list does (spec 0012: each model has its own
  prompt cache). Jobs default to `balanced` when nothing else names a model (the owner's call: Opus on Claude Code;
  `frontier` is chosen per environment, repo policy or job).
* **Its harness's CLI version**, the one the lock pinned at creation. A new lock (the canary's bump) applies to
  sessions created after it. A follow-up that would resume an older session's conversation on a sandbox running a
  newer CLI is handled as a harness switch: the conversation is handed off (a summary in a new conversation), never
  resumed by another version than the one that wrote it.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from hangar import modelpolicy
from hangar.events import RunEvent
from hangar.harnesses import HARNESSES
from hangar.models import Sandbox, Session, SessionTurn

if TYPE_CHECKING:
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.pins")

JOB_DEFAULT = "balanced"  # --- least-privilege: the owner's default, was `frontier`


def _event(svc: SessionService, s: Session, text: str, warning: str, **data: str | None) -> None:
    svc.activity.append(s.id, [RunEvent(kind="progress", turn=len(s.turns), text=text,
                                        data={"source": "hangar", "warning": warning,
                                              **{k: v for k, v in data.items() if v is not None}})])
    log.info("session %s: %s", s.id, text)


def pin(svc: SessionService, s: Session) -> modelpolicy.Resolution | None:
    """At creation, and when the session switches harness: the concrete model and the CLI version."""
    if not s.harness:
        return None
    h = HARNESSES.get(s.harness)
    s.harness_version = h.version if h and h.install else None
    value = s.model
    if value is None:
        with contextlib.suppress(KeyError, ValueError):
            value = svc.fleet.catalog.get(s.environment).model_for(s.harness)
    if value is None and s.job_id:
        value = JOB_DEFAULT
    r = modelpolicy.of(svc.fleet).resolve(s.harness, value)
    s.model, s.model_policy = r.model, r.policy
    if r.note:
        _event(svc, s, f"model {r.policy}: {r.model or 'the harness default'} ({r.note})", "model_resolution",
               policy=r.policy, model=r.model, source=r.source)
    return r


def concrete(svc: SessionService, s: Session, model: str | None) -> str | None:
    """A model a caller names for the next turns (send_to_session's `model`): a policy becomes its id now."""
    if not modelpolicy.is_policy(model):
        return model
    r = modelpolicy.of(svc.fleet).resolve(s.harness, model)
    if r.note:
        _event(svc, s, f"model {r.policy}: {r.model or 'the harness default'} ({r.note})", "model_resolution",
               policy=r.policy, model=r.model, source=r.source)
    return r.model


def before_turn(svc: SessionService, s: Session, sb: Sandbox, turn: SessionTurn) -> None:
    """A turn on a sandbox whose CLI isn't the version the session's conversation was written with starts a new
    conversation from a summary (a handoff), as a harness switch does. Sandboxes record the locked version their
    pack was built for (`harness_lock`); one that doesn't (baked images, older records) changes nothing."""
    have = sb.info.get("harness_lock") if sb.info.get("harness") == s.harness else None
    if not have:
        return
    if not s.harness_version:  # a session from before versions were pinned: it starts here
        s.harness_version = have
        return
    if have == s.harness_version:
        return
    old, s.harness_version = s.harness_version, have
    if not (s.harness_session or s.fork_conversation):
        return  # nothing to resume: the new version simply applies from here on
    s.harness_session = s.harness_session_for = s.fork_conversation = None
    warning = (f"{s.harness} {old} -> {have}: this sandbox runs a newer CLI than the one that wrote the session's "
               "conversation, so this turn hands off (a summary in a new conversation) instead of resuming it")
    turn.warnings.append(warning)
    _event(svc, s, warning, "harness_version", old=old, new=have)
    svc.fleet.audit("hangar", "session.harness_version", s.id, harness=s.harness, old=old, new=have)
