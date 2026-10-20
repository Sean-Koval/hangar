"""What each live turn is doing right now, kept in memory for the Fleet (KAN-140; docs/console.md#fleet).

The turn watcher (events.TurnWatcher) sees every RunEvent of a running turn as it is recorded. `Live.observe` folds
each batch into one `LiveState` per session: the humanised latest event (`now`), when the agent was last active, the
stage it is in (plan, edit, test, PR) with the timeline of stages so far, the files it changed, the commits it made,
and the agent's own progress report. `GET /api/fleet` (fleet_api.py) reads these states: no log scan on the list
path, so 200 sandboxes are one in-memory pass.

Everything here is derived and rebuilt from the stream: a control-plane restart starts with empty states, which
fill again as the turns' logs are re-read from their saved cursors. A finished turn keeps its state until the
session is deleted or the table is full (`MAX_STATES`), so a settled sandbox still says what its agent last did.

Stage times (`StageStats`) feed the `stage_estimate` the fleet reports when the agent reported no progress of its
own: the median of the same stage's durations in this control plane's recent turns, per environment and harness.
"""

from __future__ import annotations

import collections
import re
import statistics
from typing import TYPE_CHECKING, Any

from hangar.models import now

if TYPE_CHECKING:
    from hangar.events import RunEvent
    from hangar.models import Session, SessionTurn

STAGES = ("plan", "edit", "test", "pr")
MAX_STATES = 5000  # sessions kept; the oldest finished ones go first
MAX_FILES = 500
NOW_MAX = 80  # characters of a `now` line
CMD_MAX = 60
STAGE_SAMPLES = 50  # durations kept per (environment, harness, stage)

_PUSH = re.compile(r"\bgit\s+push\b|\bgh\s+pr\b|\bgh\s+api\b.*\bpulls\b")
_COMMIT = re.compile(r"\bgit\s+commit\b")
_TEST = re.compile(r"\b(pytest|py\.test|npm\s+(run\s+)?test|pnpm\s+(run\s+)?test|yarn\s+(run\s+)?test|go\s+test|"
                   r"cargo\s+test|jest|vitest|mocha|tox|nox|make\s+(test|check|lint)|unittest|rspec|phpunit|"
                   r"ruff|eslint|mypy|pyright|tsc\b|flake8|pylint|shellcheck)\b")
_RESOLVED = {"allowed": "approved; carrying on", "allowed_always": "approved; carrying on",
             "denied": "denied", "cancelled": None}


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def humanise(ev: RunEvent) -> str | None:
    """One line a person reads on a tile: what the agent is doing, from one event. None: this event says nothing new
    about what the agent is doing (a tool's result, a usage line, a tree hash), so the previous line stands."""
    d = ev.data
    if ev.kind == "file_change":
        return _clip(f"editing {d.get('path') or ev.text}", NOW_MAX)
    if ev.kind == "command":
        return f"running {_clip(ev.text, CMD_MAX)}"
    if ev.kind == "progress":
        if d.get("action") == "tree":
            return None
        return _clip(ev.text, NOW_MAX) or None
    if ev.kind == "approval_request":
        if d.get("status", "pending") == "pending":
            return _clip(f"waiting on you: {d.get('tool') or 'a tool call'}", NOW_MAX)
        word = _RESOLVED.get(str(d.get("status")))
        if word is None:
            return None
        return _clip(f"{word}" + (f" by {d['by']}" if d.get("by") and word == "denied" else ""), NOW_MAX)
    if ev.kind == "tool_call":
        return _clip(ev.text, NOW_MAX) or None
    if ev.kind == "error":
        return _clip(f"error: {ev.text}", NOW_MAX) if ev.text else "error"
    if ev.kind == "message":
        first = ev.text.strip().splitlines()[0] if ev.text.strip() else ""
        return _clip(first, NOW_MAX) or None
    return None  # tool_result, usage


def stage_signal(ev: RunEvent) -> str | None:
    """The stage one event is evidence of, or None. A command names the stage when it is a test runner or a push;
    an edit is editing; a read, search or message early in a turn is planning."""
    if ev.kind == "file_change":
        return "edit"
    if ev.kind == "command":
        if _PUSH.search(ev.text):
            return "pr"
        if _TEST.search(ev.text):
            return "test"
        return None
    if ev.kind in ("tool_call", "message"):
        return "plan"
    return None


class LiveState:
    """One session's live picture (the fields `GET /api/fleet` serves, in `view`)."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.turn = 0
        self.run_id = ""
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.now: str | None = None
        self.now_kind: str | None = None
        self.last_activity_at: float | None = None
        self.stage: str | None = None
        self.stages: list[dict[str, Any]] = []
        self.files: dict[str, None] = {}  # ordered set of paths changed this turn
        self.commits = 0
        self.progress: dict[str, Any] | None = None  # the agent's own report: {message, pct?, ts}
        self.usage: dict[str, Any] | None = None  # the last usage event's totals for the turn
        self.denial: dict[str, Any] | None = None  # the latest policy denial of this turn
        self.last_command: str | None = None
        self.events = 0

    # ------------------------------------------------------------------ feeding
    def reset(self, turn: int, run_id: str, started_at: float | None) -> None:
        """A new turn: the picture starts over."""
        self.turn, self.run_id, self.started_at, self.finished_at = turn, run_id, started_at, None
        self.now = self.now_kind = self.stage = None
        self.stages, self.files, self.commits = [], {}, 0
        self.progress = self.usage = self.denial = self.last_command = None
        self.events = 0

    def observe(self, ev: RunEvent, stats: StageStats | None = None, key: tuple[str, str] | None = None) -> bool:
        """Fold one event in. True when the picture a tile shows changed (now, stage, files, commits, denial)."""
        changed = False
        if ev.kind != "usage":
            self.events += 1
            self.last_activity_at = ev.ts
            if (line := humanise(ev)) is not None and (line != self.now or ev.kind != self.now_kind):
                self.now, self.now_kind, changed = line, ev.kind, True
        if ev.kind == "file_change":
            p = ev.data.get("path") or ev.text
            if p and p not in self.files:
                if len(self.files) >= MAX_FILES:
                    self.files.pop(next(iter(self.files)))
                self.files[p] = None
                changed = True
        elif ev.kind == "command":
            self.last_command = ev.text
            if _COMMIT.search(ev.text):
                self.commits += 1
                changed = True
        elif ev.kind == "progress":
            if ev.data.get("source") != "hangar" and ev.data.get("action") is None:
                self.progress = {"message": ev.text, "ts": ev.ts, **({"pct": ev.data["pct"]} if "pct" in ev.data
                                                                      else {})}
                changed = True
        elif ev.kind == "approval_request":
            d = ev.data
            if d.get("status") == "denied" and d.get("by") == "policy":
                self.denial = {"id": d.get("id"), "message": d.get("message"), "at": ev.ts}
                changed = True
        elif ev.kind == "usage":
            self.usage = {k: v for k, v in ev.data.items() if k != "final"}
        if (st := stage_signal(ev)) is not None and self._enter(st, ev.ts, stats, key):
            changed = True
        return changed

    def _enter(self, stage: str, ts: float, stats: StageStats | None, key: tuple[str, str] | None) -> bool:
        """Move to `stage` unless already there. Plan only before anything else happened: a read after an edit is
        part of editing, not a new plan."""
        if stage == self.stage or (stage == "plan" and self.stage is not None):
            return False
        self._end(ts, stats, key)
        self.stage = stage
        self.stages.append({"stage": stage, "started_at": ts, "ended_at": None})
        return True

    def _end(self, ts: float, stats: StageStats | None, key: tuple[str, str] | None) -> None:
        if self.stages and self.stages[-1]["ended_at"] is None:
            last = self.stages[-1]
            last["ended_at"] = ts
            if stats is not None and key is not None:
                stats.add(key, last["stage"], max(0.0, ts - last["started_at"]))

    def finish(self, ts: float | None = None, stats: StageStats | None = None,
               key: tuple[str, str] | None = None) -> None:
        self.finished_at = ts or now()
        self._end(self.finished_at, stats, key)

    # ------------------------------------------------------------------ reading
    def stage_elapsed(self, at: float | None = None) -> float | None:
        if not self.stages or self.stages[-1]["ended_at"] is not None:
            return None
        return max(0.0, (at if at is not None else now()) - self.stages[-1]["started_at"])

    def view(self) -> dict[str, Any]:
        return {"turn": self.turn, "now": self.now, "last_activity_at": self.last_activity_at, "stage": self.stage,
                "stages": [dict(s) for s in self.stages], "files_changed": len(self.files),
                "files": list(self.files)[-20:], "commits": self.commits, "progress": self.progress,
                "usage": self.usage, "denial": self.denial, "finished": self.finished_at is not None}


class StageStats:
    """How long each stage typically takes here, per (environment, harness): the last STAGE_SAMPLES durations."""

    def __init__(self) -> None:
        self._d: dict[tuple[str, str, str], collections.deque[float]] = collections.defaultdict(
            lambda: collections.deque(maxlen=STAGE_SAMPLES))

    def add(self, key: tuple[str, str], stage: str, seconds: float) -> None:
        if seconds >= 1.0:  # a stage entered and left within a second is a mis-signal, not a sample
            self._d[(key[0], key[1], stage)].append(seconds)

    def typical(self, key: tuple[str, str], stage: str) -> tuple[float, int] | None:
        """(median seconds, samples) for the stage in this environment and harness; the harness alone, then any,
        when the exact pair has fewer than three samples."""
        for k in ((key[0], key[1], stage), ("", key[1], stage), ("", "", stage)):
            xs = self._d.get(k) if k[0] or k[1] else self._all(stage, k)
            if xs and len(xs) >= 3:
                return statistics.median(xs), len(xs)
        return None

    def _all(self, stage: str, k: tuple[str, str, str]) -> list[float]:
        out: list[float] = []
        for (_env, h, st), xs in self._d.items():
            if st == stage and (not k[1] or h == k[1]):
                out.extend(xs)
        return out


class Live:
    """The live states of one control plane (`Activity.live`), by session id."""

    def __init__(self) -> None:
        self.states: dict[str, LiveState] = {}
        self.stats = StageStats()
        self.listeners: list[Any] = []  # --- S5: fn(session_id, state) after a batch changed what a tile shows

    def get(self, session_id: str) -> LiveState | None:
        return self.states.get(session_id)

    def observe(self, s: Session, number: int, turn: SessionTurn, evs: list[RunEvent]) -> None:
        st = self.states.get(s.id)
        if st is None:
            self._room()
            st = self.states[s.id] = LiveState(s.id)
        key = (s.environment, turn.harness or s.harness or "")
        if st.turn != number or st.run_id != (turn.run_id or ""):
            st.reset(number, turn.run_id or "", turn.started_at)
        changed = False
        for ev in evs:
            changed = st.observe(ev, self.stats, key) or changed
        if changed:
            self._notify(s.id, st)

    def finished(self, s: Session, turn: SessionTurn) -> None:
        st = self.states.get(s.id)
        if st is not None and st.finished_at is None:
            st.finish(turn.finished_at or now(), self.stats, (s.environment, turn.harness or s.harness or ""))
            self._notify(s.id, st)

    def drop(self, session_id: str) -> None:
        self.states.pop(session_id, None)

    def _room(self) -> None:
        if len(self.states) < MAX_STATES:
            return
        done = sorted((k for k, v in self.states.items() if v.finished_at is not None),
                      key=lambda k: self.states[k].finished_at or 0)
        for k in done[: max(1, MAX_STATES // 10)] or list(self.states)[:1]:
            del self.states[k]

    def _notify(self, sid: str, st: LiveState) -> None:
        for fn in self.listeners:
            try:
                fn(sid, st)
            except Exception:  # noqa: BLE001 - a listener never costs the state
                import logging

                logging.getLogger("hangar.livestate").exception("live listener failed")
