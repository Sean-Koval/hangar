"""Steer classification (#162): was a person's steer a `correction`, a `redirect` or `new_info`?

Steer-avoidance counts the interventions a better harness would have made unnecessary. Many steers aren't the
agent's fault: the requester changed the scope, or remembered a constraint the ticket never stated. Counting those
rewards a harness that ignores people, so each steer can say what it was, and flightdeck counts only corrections.

* **Where.** A job's follow-up (`Turn`) and a steer or interrupt into a running turn (`Steer`) carry `steer_class`
  (default `unclassified`), `classified_by` and `classified_at`. Classifying records a `steer.classified` progress
  event in the session's activity, and the trace export puts `hangar.steer.class` on steer events and follow-up
  turn spans (traces.py), which the outbox carries.
* **How.** In Slack, buttons under hangar's "Continuing…" acknowledgement of a human follow-up (only its sender may
  click them: slack.py's interactions route); in Slack and Jira, a reply starting `fix:` / `correction:`,
  `redirect:` or `info:` (classified on arrival, the prefix stripped); in Jira, `/hangar class correction|redirect|
  info` classifies the sender's last unclassified follow-up; over REST, `POST /api/jobs/{id}/turns/{n}/class` and
  `POST /api/sessions/{ref}/steers/{id}/class`. No MCP tool: the tool list is at its cap.
* **Defaults.** An answer to the agent's question (`needs_input`) is `new_info`: the agent asked. Steers and
  interrupts over REST or MCP, and follow-ups nobody classified, stay `unclassified`; the weekly metrics show that
  share. Classification is self-reported and optional.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, get_args

from fastapi import HTTPException
from pydantic import BaseModel, Field

from hangar.events import RunEvent
from hangar.models import Job, Session, SteerClass, Turn, now

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Steer
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.steer_class")

CLASSES: tuple[str, ...] = get_args(SteerClass)
ACTION = "steer.classified"
_WORDS = {"correction": "correction", "fix": "correction", "redirect": "redirect", "info": "new_info",
          "new_info": "new_info", "new-info": "new_info", "newinfo": "new_info"}
_PREFIX = re.compile(r"^\s*(fix|correction|redirect|info)\s*:\s*", re.I)
LABELS = {"correction": "Correction", "redirect": "Redirect", "new_info": "New info"}
HINT = "Start a reply with `fix:`, `redirect:` or `info:` to say whether it corrects the agent, changes the task " \
       "or adds something it didn't know."


class ClassifyError(ValueError):
    pass


def parse(word: str | None) -> str | None:
    """`correction`, `fix`, `redirect`, `info`, `new_info` -> the class; anything else None."""
    return _WORDS.get((word or "").strip().lower())


def split_prefix(text: str) -> tuple[str | None, str]:
    """A reply's class prefix, and the text without it: `fix: use the v2 client` -> (correction, use the v2 client)."""
    m = _PREFIX.match(text or "")
    if not m:
        return None, text
    return _WORDS[m.group(1).lower()], text[m.end():].strip()


def _check(cls: str) -> str:
    c = parse(cls) or (cls if cls in CLASSES else None)
    if c is None:
        raise ClassifyError(f"class must be one of correction, redirect, new_info (or unclassified), not '{cls}'")
    return c


def record(sessions: SessionService | None, s: Session | None, cls: str, ref: str | int, by: str,
           job: str | None = None) -> RunEvent | None:
    """The `steer.classified` event in the session's activity."""
    if sessions is None or s is None:
        return None
    t = s.turns[-1] if s.turns else None
    try:
        [ev] = sessions.activity.append(s.id, [RunEvent(
            kind="progress", turn=len(s.turns), run_id=(t.run_id or "") if t else "",
            text=f"steer classified: {cls} (by {by})",
            data={"source": "hangar", "action": ACTION, "class": cls, "ref": ref, "by": by, "job": job})])
        return ev
    except Exception:  # noqa: BLE001 - the class is on the record either way
        log.warning("could not record a steer classification on session %s", s.id, exc_info=True)
        return None


def mark(x: Turn | Steer, cls: str, by: str) -> None:
    x.steer_class, x.classified_by, x.classified_at = cls, by, now()  # type: ignore[assignment]


# ====================================================================== jobs: follow-up turns
def follow_up_turn(job: Job, n: int) -> Turn:
    if not 1 <= n <= len(job.turns):
        raise KeyError(f"job {job.id} has no turn {n} (it has {len(job.turns)})")
    t = job.turns[n - 1]
    if t.kind != "follow_up" or t.trigger:
        raise ClassifyError(f"turn {n} of {job.id} isn't a person's follow-up, so it has no steer class")
    return t


def classify_turn(svc: JobService, job: Job, n: int, cls: str, by: str, sender: str | None = None) -> Turn:
    """Set follow-up turn `n`'s class. `sender`: only the person who sent it may (a front door's check)."""
    cls = _check(cls)
    t = follow_up_turn(job, n)
    if sender is not None and t.requester != sender:
        raise ClassifyError("only the person who sent that follow-up can classify it")
    mark(t, cls, by)
    svc._save(job)
    record(svc.sessions, svc._session(job) if svc.sessions is not None else None, cls, n, by, job=job.id)
    return t


def last_unclassified(job: Job, requester: str) -> int | None:
    """The 1-based index of `requester`'s last follow-up nobody classified."""
    for n in range(len(job.turns), 0, -1):
        t = job.turns[n - 1]
        if t.kind == "follow_up" and not t.trigger and t.requester == requester and t.steer_class == "unclassified":
            return n
    return None


# ====================================================================== sessions: steers and interrupts
def classify_steer(sessions: SessionService, s: Session, steer_id: str, cls: str, by: str) -> Steer:
    cls = _check(cls)
    st = next((x for t in reversed(s.turns) for x in t.steers if x.id == steer_id), None)
    if st is None:
        raise KeyError(f"session {s.name} has no steer {steer_id}")
    mark(st, cls, by)
    sessions._save(s)
    record(sessions, s, cls, steer_id, by, job=s.job_id)
    return st


# ====================================================================== REST
class ClassBody(BaseModel):
    steer_class: str = Field(alias="class", max_length=20)

    model_config = {"populate_by_name": True}


def _view(x: Turn | Steer) -> dict[str, Any]:
    return {"class": x.steer_class, "classified_by": x.classified_by, "classified_at": x.classified_at}


def mount(app: Any, h: Any, admin: Any, job_caller: Any, job_session: Any) -> None:
    """POST /api/jobs/{id}/turns/{n}/class (the job's submitter, or admin) and POST /api/sessions/{ref}/steers/{id}
    /class (admin), beside the steer routes they classify."""

    @app.post("/api/jobs/{job_id}/turns/{n}/class")
    async def job_turn_class(job_id: str, n: int, body: ClassBody, caller: Any = job_caller):
        s = job_session(job_id, caller)  # the caller may see the job (restricted callers: their own)
        try:
            t = classify_turn(h.jobs, h.jobs.get(s.job_id or job_id), n, body.steer_class, caller.requester)
        except ClassifyError as e:
            raise HTTPException(400, str(e)) from e
        return {"job": job_id, "turn": n, **_view(t)}

    @app.post("/api/sessions/{ref}/steers/{steer_id}/class")
    async def session_steer_class(ref: str, steer_id: str, body: ClassBody, who: str = admin):
        s = h.sessions.get(ref)
        try:
            st = classify_steer(h.sessions, s, steer_id, body.steer_class, who)
        except ClassifyError as e:
            raise HTTPException(400, str(e)) from e
        return {"session": s.name, "steer": steer_id, **_view(st)}


# ====================================================================== traces
def turn_attrs(t: Turn | None) -> dict[str, Any]:
    """`hangar.steer.class` (and who said so) for a job's follow-up turn span; nothing for other turns."""
    if t is None or t.kind != "follow_up" or t.trigger:
        return {}
    return {"hangar.steer.class": t.steer_class, "hangar.steer.classified_by": t.classified_by}
