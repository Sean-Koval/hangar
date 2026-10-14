"""REST and MCP for the agent's computer and its requests (docs/computer-use.md).

REST:
* `POST /api/screen/tickets {"ref", "control"}`: a one-time ticket (60 s) for the sandbox's screen, for its owner or
  an admin. `control` lets the viewer type and click; without it hangar drops the viewer's input.
* `WS /api/screen?ticket=`: RFB (VNC) over a websocket, as noVNC speaks it. The dashboard page `/ui/vnc.html`
  is a viewer.
* `GET /api/media/{token}`: one artifact, behind a signed, expiring link (for PR bodies).

MCP: `open_screen`, `list_env_requests`, `answer_env_request`; and the helpers `wait_for_turn` uses to put an
agent's pending request to the person following the work (an elicitation).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from typing import Any, Literal

import mcp_types as types
from fastapi import Query, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel, Field

from hangar import computer, exchange
from hangar.envrequests import MAX_NOTE, EnvRequest, EnvRequests, RequestError
from hangar.fleet import FleetError

log = logging.getLogger("hangar.computer")

_SCREENS: weakref.WeakKeyDictionary[Any, computer.Screens] = weakref.WeakKeyDictionary()


def screens(fleet: Any) -> computer.Screens:
    """The tickets and viewers of one control plane (REST and MCP share them)."""
    if fleet not in _SCREENS:
        _SCREENS[fleet] = computer.Screens()
    return _SCREENS[fleet]


def resolve(fleet: Any, sessions: Any, jobs: Any, ref: str) -> tuple[Any, Any]:
    """(sandbox, session or None) for a job id, a session name or id, or a sandbox id or name. A record the
    caller may not see is reported as missing (people.visible)."""
    from hangar import people
    from hangar.fleet import FleetError

    s = None
    with contextlib.suppress(KeyError, LookupError, ValueError):
        s, _ = exchange.resolve(sessions, jobs, ref)
    if s is not None:
        if not people.visible(s.person):
            raise KeyError(f"no session or job '{ref}'")
        if not s.sandbox_id or not sessions._alive(s.sandbox_id):
            raise KeyError(f"{ref} has no running sandbox (send it a message to resume it)")
        return fleet.get(s.sandbox_id), s
    try:
        sb = fleet.get(ref)
    except FleetError as e:
        raise KeyError(f"no session, job or sandbox '{ref}'") from e
    if not people.visible(sb.person):
        raise KeyError(f"no session, job or sandbox '{ref}'")
    return sb, None


def issue(fleet: Any, sessions: Any, jobs: Any, ref: str, control: bool, actor: str) -> dict[str, Any]:
    from hangar import identity
    from hangar.models import SandboxState

    sb, s = resolve(fleet, sessions, jobs, ref)
    if sb.state != SandboxState.RUNNING:
        raise KeyError(f"sandbox {sb.id} is {sb.state.value}, not running")
    spec = fleet.catalog.get(sb.environment)
    if spec.computer is None:
        raise ValueError(f"environment '{spec.name}' has no computer: set `computer: true` in its spec")
    p = identity.current.get()
    t = computer.ScreenTicket(sandbox_id=sb.id, actor=actor, person=p.person if p else None, control=control,
                              expires=time.time() + computer.Screens.TTL, session=s.name if s else None)
    raw = screens(fleet).issue(t)
    fleet.audit(actor, "screen.ticket", sb.id, control=control, session=t.session)
    base = fleet.settings.public_url.rstrip("/")
    return {"sandbox": sb.id, "session": t.session, "control": control, "expires_in": int(computer.Screens.TTL),
            "ticket": raw, "url": f"{base}/ui/vnc.html#ticket={raw}{'&control=1' if control else ''}",
            "websocket": f"{base.replace('http', 'ws', 1)}/api/screen?ticket={raw}"}


async def proxy(ws: Any, fleet: Any, sessions: Any, t: computer.ScreenTicket) -> None:
    """One viewer: the websocket <-> `hangar-computer bridge` in the sandbox. Ends when either side does."""
    from hangar.events import RunEvent

    reg = screens(fleet)
    sid = t.sandbox_id
    if reg.viewers.get(sid, 0) >= computer.Screens.MAX_VIEWERS:
        await ws.close(code=1013, reason="too many viewers on this screen")
        return
    reg.viewers[sid] = reg.viewers.get(sid, 0) + 1
    term = None
    try:
        sb, prov = fleet._running(sid)
        term = await prov.open_terminal(sb, computer.bridge_cmd(f"/home/{fleet.settings.sandbox_user}"), 80, 24)
        bridge = computer.Bridge(term)
        first = await bridge.ready()
        verb = "took control of" if t.control else "is watching"
        fleet.audit(t.actor, "screen.control" if t.control else "screen.view", sid, session=t.session)
        if t.session:
            with contextlib.suppress(Exception):
                s = sessions.get(t.session)
                sessions.activity.append(s.id, [RunEvent(kind="progress", turn=len(s.turns),
                                                         text=f"{t.actor} {verb} the screen",
                                                         data={"screen": "control" if t.control else "view"})])

        async def down() -> None:
            if first:
                await ws.send_bytes(first)
            while (data := await bridge.read()) is not None:
                if data:
                    await ws.send_bytes(data)

        async def up() -> None:
            filt = None if t.control else computer.RfbFilter()
            last = 0.0
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                data = msg.get("bytes") or (msg.get("text") or "").encode("latin-1", errors="ignore")
                if filt is not None:
                    data = filt.feed(data)
                if data:
                    await bridge.write(data)
                if time.monotonic() - last > 30:  # someone is using the screen: keep the session awake
                    last = time.monotonic()
                    fleet.touch(sid)

        tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for x in pending:
            x.cancel()
        for x in done:
            e = x.exception()
            if isinstance(e, computer.RfbFilter.Closed):
                with contextlib.suppress(Exception):
                    await ws.close(code=1008, reason=str(e)[:120])
            elif e is not None:
                log.info("screen %s ended: %s", sid, e)
    except (TimeoutError, ConnectionError, KeyError, NotImplementedError, OSError, FleetError) as e:
        with contextlib.suppress(Exception):
            await ws.close(code=1011, reason=str(e)[:120] or "the screen is not available")
    except Exception as e:  # noqa: BLE001 - a viewer must never take anything else down
        log.warning("screen %s failed", sid, exc_info=True)
        with contextlib.suppress(Exception):
            await ws.close(code=1011, reason=f"the screen failed: {type(e).__name__}")
    finally:
        reg.viewers[sid] = max(0, reg.viewers.get(sid, 1) - 1)
        if term is not None:
            with contextlib.suppress(Exception):
                await term.close()
        with contextlib.suppress(Exception):
            await ws.close()


class TicketBody(BaseModel):
    ref: str = Field(..., max_length=200)
    control: bool = False


def mount(app: Any, h: Any, U: Any) -> None:
    from hangar import envrequests

    @app.post("/api/screen/tickets")
    async def screen_ticket(body: TicketBody, who: str = U):
        return issue(h.fleet, h.sessions, h.jobs, body.ref, body.control, who)

    @app.websocket("/api/screen")
    async def screen(ws: WebSocket, ticket: str = Query("")):
        t = screens(h.fleet).redeem(ticket)
        if t is None:
            await ws.close(code=4401)
            return
        offered = ws.scope.get("subprotocols") or []
        await ws.accept(subprotocol="binary" if "binary" in offered else None)
        await proxy(ws, h.fleet, h.sessions, t)

    @app.get("/api/media/{token}")
    async def media(token: str):
        """A screenshot or demo linked from a PR body: the link is the credential (signed, expiring, one file)."""
        try:
            sid, turn, path = computer.read_media_token(h.settings, token)
            s = h.sessions.get(sid)
            a, data = await exchange.read_artifact(h.sessions, s, turn, path)
        except (KeyError, LookupError, ValueError) as e:
            return Response(str(e).strip("'\""), status_code=404, media_type="text/plain")
        media_type, headers = exchange.download_headers(a)
        return Response(data, media_type=media_type, headers={**headers, "Cache-Control": "private, max-age=3600"})

    envrequests.mount(app, h, U)


# ====================================================================== MCP
DECISION_HELP = "approve or deny. Approving tells the agent it's handled; it never sets a secret's value."
REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "deny"], "title": "Decision", "description": DECISION_HELP},
        "note": {"type": "string", "title": "Note to the agent", "description": "Optional. Never a secret value."},
    },
    "required": ["decision"],
}


class RequestForm(BaseModel):
    """The form a handshake-era client shows for an agent's environment request."""

    decision: Literal["approve", "deny"] = Field(title="Decision", description=DECISION_HELP)
    note: str = Field("", title="Note to the agent", description="Optional. Never a secret value.")


def first_pending(sessions: Any, jobs: Any, ref: str, skip: list[str] | tuple[str, ...] = ()
                  ) -> tuple[str, EnvRequest] | None:
    """(label, request) for the oldest pending request of the job's or session's agent the client hasn't skipped."""
    reqs: EnvRequests | None = getattr(sessions, "env_requests", None)
    if reqs is None:
        return None
    try:
        s, job = exchange._lookup(sessions, jobs, ref)  # noqa: SLF001 - shared with wait_for_turn
    except (KeyError, LookupError):
        return None
    if s is None:
        return None
    r = next((r for r in reqs.pending_for(s) if r.id not in skip), None)
    if r is None:
        return None
    return (f"Job {job.id} ({job.title})" if job is not None else f"Session {s.name}"), r


def message(label: str, r: EnvRequest) -> str:
    what = f"the secret {r.name} (set it in the environment yourself; never paste its value here)" \
        if r.kind == "secret" else f"network access to {r.target}"
    return f"{label}: the agent asks for {what}. Why: {r.why}"


def elicit_request(label: str, r: EnvRequest) -> types.ElicitRequest:
    return types.ElicitRequest(params=types.ElicitRequestFormParams(
        message=message(label, r), requested_schema=REQUEST_SCHEMA))  # type: ignore[arg-type]


async def answer_elicited(sessions: Any, rid: str, resp: types.ElicitResult, actor: str) -> str:
    """answered | declined | cancelled | invalid | stale (answered elsewhere, or gone)."""
    if resp.action != "accept":
        return "declined" if resp.action == "decline" else "cancelled"
    content = resp.content or {}
    decision, note = content.get("decision"), content.get("note")
    if decision not in ("approve", "deny") or not isinstance(note, str | None):
        return "invalid"
    try:
        await sessions.env_requests.decide(rid, decision == "approve", actor, (note or "")[:MAX_NOTE] or None)
    except (RequestError, KeyError, PermissionError):
        return "stale"
    return "answered"


def register(tool: Any, fleet: Any, sessions: Any, jobs: Any, caller: Any) -> None:
    """The tools; `tool` is build_mcp's decorator, `caller()` the current caller's name."""

    @tool()
    def list_env_requests(ref: str | None = None, state: str = "pending") -> list[dict[str, Any]]:
        """Agents' requests for a secret (by name) or a blocked host. `ref`: job or session (default: all yours);
        `state`: pending | approved | denied | all."""
        reqs: EnvRequests = sessions.env_requests
        sid = exchange.resolve(sessions, jobs, ref)[0].id if ref else None
        return [reqs.view(r) for r in reqs.list(None if state == "all" else state, sid)]

    @tool()
    async def answer_env_request(request: str, decision: str, note: str | None = None,
                                 scope: str = "session") -> dict[str, Any]:
        """Answer an agent's request: approve | deny, a `note` for the agent (never a secret value). Egress `scope`:
        session, or environment (admins)."""
        if decision not in ("approve", "deny"):
            raise ValueError("decision must be approve or deny")
        if scope not in ("session", "environment"):
            raise ValueError("scope must be session or environment")
        reqs: EnvRequests = sessions.env_requests
        r = await reqs.decide(request, decision == "approve", f"mcp:{caller()}", note,
                              scope)  # type: ignore[arg-type]
        return reqs.view(r)

    @tool()
    def open_screen(ref: str, control: bool = False) -> dict[str, Any]:
        """A one-time link (60 s) to watch a job's or session's screen; `control` lets you type and click."""
        return issue(fleet, sessions, jobs, ref, control, f"mcp:{caller()}")
