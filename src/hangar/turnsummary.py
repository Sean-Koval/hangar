"""The summary of a turn that ended without the agent's own (KAN-137).

A turn stopped over its budget, timed out or cancelled has no final assistant message and, for a job, no
`RESULT.md`. Its summary used to be the run log's tail: the last few KB of the harness's JSON stream (tool calls,
usage lines), which then became the draft PR's body. Now it is built from the turn's normalized events: why it
stopped, what the agent said last, the files it changed and the last check's result. Raw harness output never goes
into a summary; the run log stays where it was (`get_events`, the run's log tail).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hangar.events import FORMATS, parse_log

STOPPED = ("over_budget", "timed_out", "cancelled", "interrupted", "failed", "error", "gone")
MESSAGES = 3  # the agent's last few messages
MESSAGE_MAX = 1200
TAIL_MAX = 2000


def _took(started_at: float | None, finished_at: float | None) -> str | None:
    if not started_at or not finished_at or finished_at < started_at:
        return None
    secs = finished_at - started_at
    return f"{max(1, round(secs))} s" if secs < 60 else f"{round(secs / 60)} min"


def stop_line(outcome: str | None, reason: str | None = None, started_at: float | None = None,
              finished_at: float | None = None) -> str | None:
    """Why the turn stopped, as a sentence ("Stopped over budget after 45 min (used ...)."), or None if it didn't."""
    if outcome not in STOPPED:
        return None
    took = _took(started_at, finished_at)
    after = f" after {took}" if took else ""
    what = {"over_budget": "Stopped over budget", "timed_out": "Timed out", "cancelled": "Cancelled",
            "interrupted": "Interrupted", "failed": "The turn failed", "error": "The turn stopped on an error",
            "gone": "The sandbox went away"}[outcome]
    return f"{what}{after}" + (f" ({reason.strip()})" if reason and reason.strip() else "") + "."


def messages(events: Iterable[Any], n: int = MESSAGES) -> list[str]:
    """The agent's last `n` messages, from RunEvents or their JSON dicts."""
    out: list[str] = []
    for e in events:
        kind = e.get("kind") if isinstance(e, dict) else getattr(e, "kind", None)
        data = (e.get("data") if isinstance(e, dict) else getattr(e, "data", None)) or {}
        text = (e.get("text") if isinstance(e, dict) else getattr(e, "text", "")) or ""
        if kind == "message" and data.get("role", "assistant") == "assistant" and text.strip():
            out.append(text.strip())
    return out[-n:]


def plain_tail(text: str, limit: int = TAIL_MAX) -> str:
    """The log's last plain-text lines: everything but JSON lines (a harness's stream), which never belong in a
    summary."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith(("{", "["))]
    return "\n".join(lines).strip()[-limit:].strip()


def _quote(text: str) -> str:
    text = text if len(text) <= MESSAGE_MAX else text[:MESSAGE_MAX].rstrip() + " …"
    return "\n".join(f"> {ln}" if ln.strip() else ">" for ln in text.splitlines())


def build(*, outcome: str | None, reason: str | None = None, started_at: float | None = None,
          finished_at: float | None = None, said: list[str] | None = None, diffstat: str | None = None,
          files_known: bool = False, verify: str | None = None, verify_exit_code: int | None = None,
          verify_ran: bool = False, fallback: str | None = None) -> str | None:
    """A plain-text summary: the stop reason, the agent's last messages, the diffstat (once the delivered commits
    are known: `files_known`) and the last check."""
    parts: list[str] = []
    if stop := stop_line(outcome, reason, started_at, finished_at):
        parts.append(f"{stop} The agent did not write a summary; this one is built from its activity.")
    if said:
        parts.append("What the agent said last:\n\n" + "\n>\n".join(_quote(m) for m in said))
    elif fallback:
        parts.append(fallback)
    elif stop:
        parts.append("The agent sent no message before it stopped.")
    if diffstat or files_known:
        parts.append("Files changed:\n\n" + "\n".join(f"    {ln}" for ln in diffstat.splitlines())
                     if diffstat else "Files changed: none committed.")
    if verify:
        if not verify_ran:
            parts.append(f"Check `{verify}`: not run.")
        else:
            ok = "passed" if verify_exit_code == 0 else f"failed (exit {verify_exit_code})"
            parts.append(f"Last check `{verify}`: {ok}.")
    return "\n\n".join(parts) or None


def from_log(harness: str | None, log_text: str, *, outcome: str | None = None, reason: str | None = None,
             started_at: float | None = None, finished_at: float | None = None) -> str | None:
    """A session turn's summary when the harness left no final message: from the events its log parses into."""
    evs, _ = parse_log(log_text.encode(errors="replace"), harness if harness in FORMATS else None)
    said = messages(evs)
    tail = None if evs else (plain_tail(log_text) or None)  # a harness without a JSON stream: its plain output
    if not said and tail is None and outcome not in STOPPED:
        return None
    return build(outcome=outcome, reason=reason, started_at=started_at, finished_at=finished_at, said=said,
                 fallback=tail)
