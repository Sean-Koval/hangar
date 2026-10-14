"""REST routes for live turns (workstream steer; docs/steering.md): steer, interrupt and tool approvals, for a
session (admin tokens) and for a job (its submitter, or admin)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from hangar import cache  # --- overnight/cache
from hangar import steer as steering
from hangar.driver import MAX_TEXT
from hangar.models import Session, Steer


class SteerBody(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_TEXT)
    model: str | None = Field(None, max_length=200)  # --- overnight/cache: from the next turn on, with a warning


class InterruptBody(BaseModel):
    message: str | None = Field(None, max_length=MAX_TEXT)  # carry on with this instead of stopping


class ApprovalBody(BaseModel):
    decision: Literal["allow", "deny", "allow_always"]
    message: str | None = Field(None, max_length=steering.MAX_MESSAGE)  # sent to the agent with a denial


def _steer(s: Session, st: Steer) -> dict[str, Any]:
    return {"session": s.name, "turn": len(s.turns), **st.model_dump(mode="json", exclude={"text"})}


def mount(app: FastAPI, h: Any, admin: Any, job_caller: Any, job_session: Callable[[str, Any], Session]) -> None:
    """`admin` and `job_caller` are app.py's auth dependencies; `job_session(job_id, caller)` finds a job's
    session for that caller (restricted callers see only their own jobs)."""
    sessions = h.sessions

    async def steer(s: Session, body: SteerBody, who: str) -> dict[str, Any]:
        model = cache.check_model(body.model)  # --- overnight/cache: a bad one fails before anything is sent
        out = _steer(s, await steering.steer(sessions, s, body.message, who))
        if warning := cache.note_switch(sessions, s, model, who):  # --- overnight/cache
            out["warning"] = warning
        return out

    async def interrupt(s: Session, body: InterruptBody | None, who: str) -> dict[str, Any]:
        return _steer(s, await steering.interrupt(sessions, s, who, body.message if body else None))

    async def answer(s: Session, approval: str, body: ApprovalBody, who: str) -> dict[str, Any]:
        a = await steering.answer(sessions, s, approval, body.decision, who, body.message)
        return a.model_dump(mode="json")

    @app.post("/api/sessions/{ref}/steer")
    async def session_steer(ref: str, body: SteerBody, who: str = admin):
        """Fold a message into the running turn (Claude Code and Codex turns)."""
        return await steer(sessions.get(ref), body, who)

    @app.post("/api/sessions/{ref}/interrupt")
    async def session_interrupt(ref: str, body: InterruptBody | None = None, who: str = admin):
        """Stop the running turn, or with a message, have the agent carry on with that instead."""
        return await interrupt(sessions.get(ref), body, who)

    @app.get("/api/sessions/{ref}/approvals")
    async def session_approvals(ref: str, _: str = admin):
        return steering.view(sessions.get(ref))

    @app.post("/api/sessions/{ref}/approvals/{approval}")
    async def session_approve(ref: str, approval: str, body: ApprovalBody, who: str = admin):
        return await answer(sessions.get(ref), approval, body, who)

    @app.post("/api/jobs/{job_id}/steer")
    async def job_steer(job_id: str, body: SteerBody, caller: Any = job_caller):
        return await steer(job_session(job_id, caller), body, caller.requester)

    @app.post("/api/jobs/{job_id}/interrupt")
    async def job_interrupt(job_id: str, body: InterruptBody | None = None, caller: Any = job_caller):
        return await interrupt(job_session(job_id, caller), body, caller.requester)

    @app.get("/api/jobs/{job_id}/approvals")
    async def job_approvals(job_id: str, caller: Any = job_caller):
        return steering.view(job_session(job_id, caller))

    @app.post("/api/jobs/{job_id}/approvals/{approval}")
    async def job_approve(job_id: str, approval: str, body: ApprovalBody, caller: Any = job_caller):
        return await answer(job_session(job_id, caller), approval, body, caller.requester)
