"""MCP surfaces for live turns (workstream steer; docs/steering.md).

Two tools, `steer_turn` and `answer_approval`, and the approval questions hangar puts to a client while it follows
a turn: `wait_for_turn` returns early with an elicitation (an `input_required` result for 2026-07-28 clients, an
`elicitation/create` request on handshake-era sessions), and a task turns `input_required`. Pending approvals are
also on get_session / get_job (`pending_approvals`) for clients that can't be asked.
"""

from __future__ import annotations

from typing import Any, Literal

import mcp_types as types
from pydantic import BaseModel, Field

from hangar import cache, exchange  # --- overnight/cache
from hangar import steer as steering
from hangar.models import Approval, Session

DECISION_HELP = "allow, deny, or allow_always (also allow calls like it for the rest of this turn)"
APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "deny", "allow_always"], "title": "Decision",
                     "description": DECISION_HELP},
        "message": {"type": "string", "title": "Note to the agent", "description": "Optional; sent with a denial."},
    },
    "required": ["decision"],
}


class ApprovalForm(BaseModel):
    """The form a handshake-era client shows for a tool approval."""

    decision: Literal["allow", "deny", "allow_always"] = Field(title="Decision", description=DECISION_HELP)
    message: str = Field("", title="Note to the agent", description="Optional; sent with a denial.")


def _session(sessions: Any, jobs: Any, ref: str) -> tuple[Session | None, str]:
    s, job = exchange._lookup(sessions, jobs, ref)  # noqa: SLF001 - shared with wait_for_turn
    label = f"Job {job.id} ({job.title})" if job is not None else f"Session {s.name}" if s is not None else ref
    return s, label


def first_pending(sessions: Any, jobs: Any, ref: str, skip: list[str] | tuple[str, ...] = ()
                  ) -> tuple[str, Approval] | None:
    """(label, approval) for the oldest tool approval the agent waits on that the client hasn't declined."""
    try:
        s, label = _session(sessions, jobs, ref)
    except KeyError:
        return None
    a = next((a for a in steering.pending(s) if a.id not in skip), None)
    return (label, a) if a is not None else None


def message(label: str, a: Approval) -> str:
    return f"{label}: the agent asks to use {a.tool or 'a tool'}: {a.detail}"


def approval_request(label: str, a: Approval) -> types.ElicitRequest:
    return types.ElicitRequest(params=types.ElicitRequestFormParams(
        message=message(label, a), requested_schema=APPROVAL_SCHEMA))  # type: ignore[arg-type]


async def answer_elicited(sessions: Any, jobs: Any, ref: str, approval_id: str, resp: types.ElicitResult,
                          requester: str) -> str:
    """Pass the user's answer on to the agent. Returns answered | declined | cancelled | invalid | stale (the
    approval was answered elsewhere, timed out, or the turn ended)."""
    if resp.action != "accept":
        return "declined" if resp.action == "decline" else "cancelled"
    content = resp.content or {}
    decision, note = content.get("decision"), content.get("message")
    if decision not in ("allow", "deny", "allow_always") or not isinstance(note, str | None):
        return "invalid"
    try:
        s, _ = _session(sessions, jobs, ref)
        if s is None:
            return "stale"
        await steering.answer(sessions, s, approval_id, decision, requester, note or None)
    except (steering.SteerError, KeyError):
        return "stale"
    return "answered"


def register(mcp: Any, tool: Any, sessions: Any, jobs: Any, caller: Any) -> None:
    """Add the tools; `tool` is build_mcp's decorator, `caller()` the current caller's name."""

    @tool()
    async def steer_turn(ref: str, message: str | None = None, interrupt: bool = False,
                         model: str | None = None) -> dict[str, Any]:
        """Steer a running turn (`ref`: job id or session name). A `message` is folded into it; the agent reads it
        at its next step. `interrupt=true` stops the turn, or with a `message`, has the agent switch to that. pi
        and agy turns take interrupts only. `model`: switch model from the next turn (costs one uncached turn)."""
        s, _ = exchange.resolve(sessions, jobs, ref)
        model = cache.check_model(model)  # --- overnight/cache: a bad one fails before anything is sent
        if interrupt:
            st = await steering.interrupt(sessions, s, caller(), message)
        else:
            st = await steering.steer(sessions, s, message or "", caller())
        out = {"session": s.name, "turn": len(s.turns), "kind": st.kind, "status": st.status}
        if warning := cache.note_switch(sessions, s, model, caller()):  # --- overnight/cache
            out["warning"] = warning
        return out

    @tool()
    async def answer_approval(ref: str, approval: str, decision: str, message: str | None = None) -> dict[str, Any]:
        """Answer what the agent waits on (`pending_approvals` on get_session/get_job): `decision` allow | deny |
        allow_always (rest of the turn; not for `rq-` requests). `message` goes to the agent."""
        s, _ = exchange.resolve(sessions, jobs, ref)
        if approval.startswith("rq-"):  # --- computer: envrequests, its one decision path (never a value)
            return await sessions.env_requests.answer(s, approval, decision, f"mcp:{caller()}", message)
        a = await steering.answer(sessions, s, approval, decision, caller(), message)
        return a.model_dump(mode="json")
