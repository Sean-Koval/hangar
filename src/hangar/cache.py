"""Prompt caching for agent turns (spec 0012 item 1; docs/sessions.md#prompt-caching).

Every model call of a turn re-sends the whole conversation; the provider's prompt cache bills the unchanged prefix
at a tenth of the input price or less. Hangar works against that cache in three places, which this module fixes:

* **TTL.** Claude Code with an API key writes 5-minute cache entries, but sessions idle up to 30 minutes and wait
  on people (`needs_input`, `ask` approvals). Each turn now runs with a cache policy (`auto | 5m | 1h | off`, per
  session, else per environment, else HANGAR_CACHE); `auto` asks for the 1-hour cache when the session is
  expected to wait longer than 5 minutes for its next message. The harness profile says how to ask
  (`Harness.cache_env`).
* **One model per session.** Each model has its own cache, so a switch re-reads the whole conversation uncached.
  The model is pinned when the session is created; asking for another one is allowed, but warned about.
* **Siblings.** Best-of-N attempts and forks started with a message begin with the same prompt prefix. Started
  together, each writes it; the provider only serves an entry once the first response has begun. The first
  sibling to start goes ahead and the rest wait until it has answered (or HANGAR_CACHE_STAGGER_SECONDS), so they
  read the prefix it wrote.

What caching saved is computed per model call from the price table in events.py (`Usage.saved_usd`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hangar.events import RunEvent
from hangar.harnesses import Harness
from hangar.models import Session

if TYPE_CHECKING:
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.cache")

POLICIES = ("auto", "5m", "1h", "off")
DEFAULT_TTL_MINUTES = 5  # what providers keep an entry for unless asked otherwise (Anthropic: 5 min)
# Claude Code's model aliases (`--model opus`); any id naming a Claude model also qualifies.
_CLAUDE_ALIASES = {"opus", "sonnet", "haiku", "fable", "mythos", "opusplan", "default", "best"}


# ====================================================================== policy
def policy(svc: SessionService, s: Session) -> str:
    """The session's cache policy, else its environment's, else HANGAR_CACHE."""
    if s.cache:
        return s.cache
    with contextlib.suppress(KeyError, ValueError):
        if env := svc.fleet.catalog.get(s.environment).cache:
            return env
    return svc.settings.cache


def supports_1h(h: Harness, model: str | None) -> bool:
    """Whether the turn's model can be asked for 1-hour entries. Claude Code: every current Claude model on the
    Claude API (the price table has a 1-hour write price for each); a model id that names no Claude model (a
    gateway alias, say) gets 5 minutes. pi checks the model's own compat flags and ignores `long` where it has no
    meaning, so any model qualifies there."""
    if h.name != "claude-code" or not model:
        return True
    m = model.lower().rsplit("/", 1)[-1]
    return "claude" in m or m.split("[", 1)[0] in _CLAUDE_ALIASES


def choose(svc: SessionService, s: Session, h: Harness) -> tuple[str | None, str]:
    """(TTL, why) for the session's next turn. The TTL is "5m", "1h", "off", or None when the harness has no
    control for it (Codex and Antigravity cache automatically; pi can't turn caching off)."""
    pol = policy(svc, s)
    if pol == "auto":
        ttl, why = _auto(svc, s, h)
    else:
        ttl, why = pol, f"cache policy {pol}"
    if ttl is None or ttl in h.cache_env:
        return ttl, why
    if ttl == "1h" and "5m" in h.cache_env:
        return "5m", f"{why}, but {h.name} has no 1-hour cache"
    return None, f"{why}, but {h.name} has no setting for it: the provider's own caching applies"


def _auto(svc: SessionService, s: Session, h: Harness) -> tuple[str | None, str]:
    if "1h" not in h.cache_env:
        return (("5m", f"{h.name} has a 5-minute cache") if "5m" in h.cache_env
                else (None, f"{h.name} caches automatically"))
    if not supports_1h(h, s.model):
        return "5m", f"model {s.model} has no 1-hour cache"
    from hangar.steer import policy as approvals  # import cycle: steer imports sessions

    waits = []
    if s.idle_minutes == 0 or s.idle_minutes > DEFAULT_TTL_MINUTES:
        waits.append(f"it waits {'indefinitely' if s.idle_minutes == 0 else f'{s.idle_minutes} min'} "
                     "for the next message")
    if approvals(svc, s) in ("ask", "governed") and svc.settings.approval_timeout_seconds > DEFAULT_TTL_MINUTES * 60:
        waits.append("people answer its tool approvals")
    if s.needs_input or any(t.outcome == "needs_input" for t in s.turns):
        waits.append("its agent asks people questions")  # and the turn that asks must have written 1h entries
    if waits:
        return "1h", "auto: " + "; ".join(waits)
    return "5m", f"auto: it waits at most {s.idle_minutes} min between messages"


def turn_env(svc: SessionService, s: Session, h: Harness) -> tuple[dict[str, str], str | None, str]:
    """The env that gives the turn its TTL, the TTL, and why."""
    ttl, why = choose(svc, s, h)
    return (dict(h.cache_env.get(ttl, {})) if ttl else {}), ttl, why


# ====================================================================== one model per session
def pin_model(svc: SessionService, s: Session) -> None:
    """Fix the session's model at creation (the environment's default for its harness, when none was asked for),
    so a later change to the environment doesn't switch a running conversation to another model's cold cache."""
    if s.model is None and s.harness:
        with contextlib.suppress(KeyError, ValueError):
            s.model = svc.fleet.catalog.get(s.environment).model_for(s.harness)


def check_model(model: str | None) -> str | None:
    """A model id from a caller, stripped (None: keep the session's). Raises ValueError for a malformed one."""
    model = (model or "").strip() or None
    if model is not None and (len(model) > 200 or not model.isprintable()):
        raise ValueError("model: at most 200 printable characters")
    return model


def switch_model(svc: SessionService, s: Session, model: str | None, requester: str) -> str | None:
    """A send or steer that names a model. Another model than the session's is allowed from the next turn on, but
    its first turn reads the whole conversation uncached: returns that warning (also an event and an audit
    entry), or None when nothing changes."""
    model = check_model(model)
    if model is None or model == s.model:
        return None
    old, s.model = s.model, model
    warning = (f"model switched from {old or 'the default'} to {model}: each model has its own prompt cache, so the "
               "next turn re-reads the whole conversation uncached")
    svc.activity.append(s.id, [RunEvent(kind="progress", turn=len(s.turns), text=warning,
                                        data={"source": "hangar", "warning": "model_switch", "from": old or "",
                                              "to": model})])
    svc.fleet.audit(requester, "session.model", s.id, old=old, new=model)
    svc._save(s)
    log.info("session %s: %s", s.id, warning)
    return warning


def note_switch(svc: SessionService, s: Session, model: str | None, requester: str) -> str | None:
    """A steer that names a model: the running turn keeps its model, the next one switches; the warning goes on
    the running turn too."""
    warning = switch_model(svc, s, model, requester)
    if warning and s.turns:
        s.turns[-1].warnings.append(warning)
        svc._save(s)
    return warning


# ====================================================================== siblings share one cache write
@dataclass
class _Group:
    members: set[str]
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    leader: str | None = None
    led_at: float = 0.0
    made_at: float = field(default_factory=time.monotonic)


STALE_SECONDS = 3600  # a sibling that never started a turn (its sandbox failed to boot) is forgotten after this


class Stagger:
    """Holds the first turns of sibling sessions until the first of them to start has had a model response. In
    memory only: after a control-plane restart the siblings start without waiting (they lose a cache read, not
    work)."""

    def __init__(self) -> None:
        self._of: dict[str, _Group] = {}  # session id -> its group, until its first turn has started

    def group(self, session_ids: list[str]) -> None:
        """Register sessions whose next turns share a prompt prefix."""
        cutoff = time.monotonic() - STALE_SECONDS
        for sid in [sid for sid, g in self._of.items() if g.made_at < cutoff]:
            del self._of[sid]
        if len(session_ids) < 2:
            return
        g = _Group(members=set(session_ids))
        for sid in session_ids:
            self._of[sid] = g

    async def wait(self, sid: str, seconds: float) -> float:
        """Called as a turn begins. The group's first caller leads and goes ahead; the others wait until the leader
        has responded or `seconds` after it started. Returns how long this caller waited."""
        g = self._of.pop(sid, None)
        if g is None or seconds <= 0:
            return 0.0
        g.members.discard(sid)
        if g.leader == sid:  # the leader's next turn: its first one has ended, whatever the others saw
            g.ready.set()
            return 0.0
        if g.leader is None:
            g.leader, g.led_at = sid, time.monotonic()
            if g.members:
                self._of[sid] = g  # until it responds: responded() finds the group through it
            return 0.0
        started = time.monotonic()
        left = g.led_at + seconds - started
        if left <= 0 or g.ready.is_set():
            return 0.0
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(g.ready.wait(), left)
        return time.monotonic() - started

    def responded(self, sid: str) -> None:
        """The leader's model call has begun answering (or its turn ended or failed): release the others."""
        g = self._of.get(sid)
        if g is not None and g.leader == sid:
            del self._of[sid]
            g.ready.set()


STAGGER = Stagger()
RESPONSE_KINDS = frozenset({"message", "tool_call", "command", "usage", "error"})


def watched(sid: str, events: list[RunEvent]) -> None:
    """TurnWatcher hook: the first sign of a model response (a message, a tool call or usage) releases siblings."""
    if any(ev.kind in RESPONSE_KINDS for ev in events):
        STAGGER.responded(sid)
