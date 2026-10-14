"""Requests an agent files with its owner: a missing secret, or a host the egress policy blocks.

`hangar-agent env request secret NAME --why ...` (or the in-sandbox MCP tool) writes a file under
`$HANGAR_IO/out/requests/`. While the turn runs, hangar picks it up (every `HANGAR_ENV_REQUEST_POLL_SECONDS`, and
when the turn ends), records an `EnvRequest`, and tells the owner: a session event, a job event, the audit log,
and an elicitation in `wait_for_turn` when an MCP client is following the work. Nothing is granted
automatically: a request stays pending until a person who may see the work (its owner or an admin) answers it
through `decide`, the one decision path (REST, MCP and the elicitation all call it). The answer reaches the agent
in its status file (`hangar-agent env requests`), and for egress, `egressapi.on_decided` applies it.

What the sandbox wrote is untrusted: names and hosts are validated, the reason is redacted and capped, and at most
`MAX_PER_TURN` requests per turn are recorded.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shlex
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from hangar import egressapi
from hangar.models import now

if TYPE_CHECKING:
    from hangar.models import Session, SessionTurn

log = logging.getLogger("hangar.envrequests")

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}"
                     r"[a-z0-9_])?)*$|^[0-9a-f:.]{2,45}$")
LOCAL_RE = re.compile(r"^[0-9]{1,12}-[0-9a-f]{8}\.json$")
MAX_WHY = 2000
MAX_NOTE = 2000
MAX_FILE = 8192
MAX_PER_TURN = 20
State = Literal["pending", "approved", "denied"]


class RequestError(ValueError):
    """A request that can't be filed or answered (shown to the caller as is)."""


class EnvRequest(BaseModel):
    id: str = Field(default_factory=lambda: "rq-" + secrets.token_hex(6))
    kind: Literal["secret", "egress"]
    environment: str
    name: str | None = None  # secret: the env var
    host: str | None = None  # egress
    port: int | None = None
    why: str
    requester: str  # "session:<id>" | "job:<id>"
    session_id: str
    session: str  # its name, for people
    job_id: str | None = None
    sandbox_id: str | None = None
    owner: str | None = None  # the owning person (people.py)
    turn: int = 0
    local: str = ""  # the sandbox's file name, so a re-read never files it twice
    state: State = "pending"
    scope: Literal["session", "environment"] = "session"
    created_at: float = Field(default_factory=now)
    decided_by: str | None = None
    decided_at: float | None = None
    note: str | None = None  # the person's note to the agent
    message: str | None = None  # what applying the decision said (egress)

    @property
    def target(self) -> str:
        return target(self.kind, self.name, self.host, self.port)

    def egress(self) -> egressapi.EgressRequest:
        return egressapi.EgressRequest(id=self.id, environment=self.environment, host=self.host or "",
                                       port=self.port, why=self.why, requester=self.requester, owner=self.owner,
                                       state=self.state, scope=self.scope, created_at=self.created_at,
                                       decided_by=self.decided_by, decided_at=self.decided_at, note=self.note)

    def agent_view(self) -> dict[str, Any]:
        """What the sandbox's status file shows the agent."""
        return self.model_dump(include={"id", "kind", "name", "host", "port", "state", "note", "message",
                                        "created_at", "decided_at"})


def target(kind: str, name: str | None, host: str | None, port: int | None) -> str:
    if kind == "secret":
        return name or ""
    return f"{host}:{port}" if port else host or ""


def parse(raw: bytes, redact: Callable[[Any], Any]) -> dict[str, Any]:
    """One request file from the sandbox, validated. Raises RequestError."""
    try:
        d = json.loads(raw[:MAX_FILE])
    except ValueError as e:
        raise RequestError("not JSON") from e
    if not isinstance(d, dict):
        raise RequestError("not a JSON object")
    why = " ".join(str(d.get("why") or "").split())[:MAX_WHY]
    if not why:
        raise RequestError("no reason given")
    kind = d.get("kind")
    if kind == "secret":
        name = d.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise RequestError("not an environment variable name")
        return {"kind": "secret", "name": name, "why": redact(why)}
    if kind == "egress":
        host, port = d.get("host"), d.get("port")
        if not isinstance(host, str):
            raise RequestError("no host")
        host = host.strip().lower().rstrip(".")
        if not HOST_RE.match(host):
            raise RequestError("not a host name or IP address")
        if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
            raise RequestError("not a port number")
        return {"kind": "egress", "host": host, "port": port, "why": redact(why)}
    raise RequestError("kind must be secret or egress")


class EnvRequests:
    """Requests across the fleet. One per control plane; the sessions service holds it (`sessions.env_requests`)."""

    def __init__(self, fleet: Any, sessions: Any, jobs: Any = None):
        self.fleet, self.sessions, self.jobs = fleet, sessions, jobs
        self.store = fleet.store
        self.settings = fleet.settings
        self._scanned: dict[str, float] = {}  # session id -> when its sandbox was last looked at
        # Called with each new request (an inbox, a Slack DM, ...); must not raise.
        self.listeners: list[Callable[[EnvRequest], None]] = []
        sessions.env_requests = self

    # ------------------------------------------------------------------ reading
    def get(self, rid: str) -> EnvRequest:
        from hangar import people

        r = self.store.get(EnvRequest, rid)
        if r is None or not people.visible(r.owner):
            raise KeyError(f"no request '{rid}'")
        return r

    def list(self, state: str | None = "pending", session_id: str | None = None,
             limit: int = 100) -> list[EnvRequest]:
        from hangar import people

        fields: dict[str, Any] = {}
        if state:
            fields["state"] = state
        if session_id:
            fields["session_id"] = session_id
        rows = self.store.list_where(EnvRequest, **fields) if fields else self.store.list(EnvRequest)
        rows = [r for r in rows if people.visible(r.owner)]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[: max(1, min(limit, 500))]

    def pending_for(self, s: Session) -> list[EnvRequest]:
        return sorted(self.store.list_where(EnvRequest, session_id=s.id, state="pending"), key=lambda r: r.created_at)

    def for_sandbox(self, s: Session) -> list[dict[str, Any]]:
        rows = sorted(self.store.list_where(EnvRequest, session_id=s.id), key=lambda r: r.created_at)[-30:]
        return [r.agent_view() for r in rows]

    # ------------------------------------------------------------------ picking requests up
    async def tick(self, s: Session, turn: SessionTurn) -> None:
        """While a turn runs: look for new requests every env_request_poll_seconds. Never raises."""
        every = self.settings.env_request_poll_seconds
        if every <= 0 or time.monotonic() - self._scanned.get(s.id, 0.0) < every:
            return
        self._scanned[s.id] = time.monotonic()
        await self.scan(s, turn)

    async def scan(self, s: Session, turn: SessionTurn) -> list[EnvRequest]:
        """Record the requests the agent wrote, tell the owner, and update the sandbox's status. Never raises."""
        try:
            return await self._scan(s, turn)
        except Exception:  # noqa: BLE001 - a request is retried at the next scan; the turn goes on
            log.warning("session %s: could not read environment requests", s.id, exc_info=True)
            return []

    async def _scan(self, s: Session, turn: SessionTurn) -> list[EnvRequest]:
        from hangar.exchange import io_dir

        if not s.sandbox_id or not self.sessions._alive(s.sandbox_id):
            return []
        sb = self.fleet.get(s.sandbox_id)
        d = f"{io_dir(self.settings, s)}/out/requests"
        res = await self.sessions._sh(sb, f"ls -1 {shlex.quote(d)} 2>/dev/null | head -n 100", timeout=30)
        names = [n for n in res.stdout.split() if LOCAL_RE.match(n)]
        if not names:
            return []
        _, prov = self.fleet._running(sb.id)
        known = {r.local for r in self.store.list_where(EnvRequest, session_id=s.id)}
        n_turn = next((i for i, t in enumerate(s.turns, 1) if t is turn), len(s.turns))
        this_turn = sum(1 for r in self.store.list_where(EnvRequest, session_id=s.id) if r.turn == n_turn)
        new: list[EnvRequest] = []
        for name in names:
            local = name.removesuffix(".json")
            if local in known:
                continue
            raw = await prov.read_file(sb, f"{d}/{name}", limit=MAX_FILE) or b""
            try:
                fields = parse(raw, self.fleet.broker.redact)
            except RequestError as e:
                turn.warnings.append(f"ignored an environment request the agent wrote ({name}): {e}")
                continue
            want = target(fields["kind"], fields.get("name"), fields.get("host"), fields.get("port"))
            dup = next((r for r in self.pending_for(s) if r.kind == fields["kind"] and r.target == want), None)
            if dup is not None or this_turn >= MAX_PER_TURN:
                if dup is None:
                    turn.warnings.append(f"more than {MAX_PER_TURN} environment requests this turn; ignored the rest")
                continue
            r = EnvRequest(environment=s.environment, requester=f"job:{s.job_id}" if s.job_id else f"session:{s.id}",
                           session_id=s.id, session=s.name, job_id=s.job_id, sandbox_id=sb.id, owner=s.person,
                           turn=n_turn, local=local, **fields)
            self.store.put(r)
            this_turn += 1
            new.append(r)
        # Picked up (or refused): the files go, so the next scan reads nothing twice. Rows remember them anyway.
        await self.sessions._sh(sb, f"cd {shlex.quote(d)} && rm -f -- " + " ".join(shlex.quote(n) for n in names),
                                timeout=30)
        for r in new:
            try:
                self._announce(s, r)
            except Exception:  # noqa: BLE001 - the row is recorded; one failed notice must not drop the others
                log.warning("could not announce request %s", r.id, exc_info=True)
        if new:
            from hangar import envdoctor

            await envdoctor.refresh(self.sessions, s, sb)
        return new

    def _announce(self, s: Session, r: EnvRequest) -> None:
        from hangar.events import RunEvent

        what = f"the secret {r.name}" if r.kind == "secret" else f"network access to {r.target}"
        text = f"the agent asks for {what}: {r.why}"
        self.fleet.audit(r.requester, f"env.request.{r.kind}", r.id, what=r.target, session=s.name)
        self.sessions.activity.append(s.id, [RunEvent(kind="progress", turn=r.turn, text=text[:500], data={
            "request": r.id, "request_kind": r.kind, "target": r.target, "state": "pending"})])
        if r.job_id and self.jobs is not None:
            try:
                job = self.jobs.get(r.job_id)
                self.jobs._event(job, f"request {r.id}: {text} (answer with answer_env_request or "
                                      f"POST /api/env-requests/{r.id})")
            except Exception:  # noqa: BLE001 - the session event and the row are what count
                log.debug("could not add a job event for %s", r.id, exc_info=True)
        for fn in self.listeners:
            try:
                fn(r)
            except Exception:  # noqa: BLE001
                log.exception("env request listener failed")

    # ------------------------------------------------------------------ answering
    async def decide(self, rid: str, approve: bool, actor: str, note: str | None = None,
                     scope: Literal["session", "environment"] | None = None) -> EnvRequest:
        """A person's answer. Only someone who may see the work (its owner, or an admin) answers it; widening an
        egress approval to the whole environment needs egressapi.may_widen."""
        from hangar import identity
        from hangar.events import RunEvent

        r = self.get(rid)  # KeyError for anything this caller may not see
        if r.state != "pending":
            raise RequestError(f"request {rid} was already {r.state} by {r.decided_by}")
        note = (note or "").strip()[:MAX_NOTE] or None
        scope = scope or "session"
        if scope == "environment":
            if r.kind != "egress":
                raise RequestError("only egress requests can be widened to the environment")
            spec = None
            try:
                spec = self.fleet.catalog.get(r.environment)
            except (KeyError, ValueError):
                pass
            if not approve or not egressapi.may_widen(identity.current.get(), spec):
                raise identity.Forbidden("approving for the whole environment needs an admin or the environment's "
                                         "owner")
        r.state, r.scope = ("approved" if approve else "denied"), scope
        r.decided_by, r.decided_at, r.note = actor, now(), note
        if r.kind == "egress":
            try:
                r.message = egressapi.on_decided(r.egress(), self.fleet)
            except Exception as e:  # noqa: BLE001 - the decision stands; say that applying it failed
                log.warning("egress decision on %s could not be applied", r.id, exc_info=True)
                r.message = f"the decision is recorded, but applying it failed: {e}"
        self.store.put(r)
        self.fleet.audit(actor, f"env.request.{r.state}", r.id, kind=r.kind, what=r.target, scope=scope)
        s = self._session(r)
        if s is not None:
            text = f"{actor} {r.state} the request for {r.target}" + (f": {note}" if note else "")
            self.sessions.activity.append(s.id, [RunEvent(kind="progress", turn=len(s.turns), text=text[:500],
                                                          data={"request": r.id, "state": r.state})])
            if s.sandbox_id and self.sessions._alive(s.sandbox_id):
                from hangar import envdoctor

                await envdoctor.refresh(self.sessions, s, self.fleet.get(s.sandbox_id))
        return r

    def _session(self, r: EnvRequest) -> Session | None:
        try:
            return self.sessions.get(r.session_id)
        except Exception:  # noqa: BLE001 - archived or deleted
            return None

    def view(self, r: EnvRequest) -> dict[str, Any]:
        return {**r.model_dump(mode="json", exclude={"local"}), "target": r.target}


class Answer(BaseModel):
    decision: Literal["approve", "deny"]
    note: str | None = Field(None, max_length=MAX_NOTE)
    scope: Literal["session", "environment"] | None = None


def mount(app: Any, h: Any, U: Any) -> None:
    """REST: GET /api/env-requests, GET/POST /api/env-requests/{id}."""
    from fastapi import HTTPException

    reqs: EnvRequests = h.env_requests

    @app.get("/api/env-requests")
    async def list_requests(state: str | None = "pending", session: str | None = None, limit: int = 100,
                            _: str = U):
        sid = h.sessions.get(session).id if session else None
        return [reqs.view(r) for r in reqs.list(state or None, sid, limit)]

    @app.get("/api/env-requests/{rid}")
    async def get_request(rid: str, _: str = U):
        return reqs.view(reqs.get(rid))

    @app.post("/api/env-requests/{rid}")
    async def answer_request(rid: str, body: Answer, who: str = U):
        try:
            r = await reqs.decide(rid, body.decision == "approve", who, body.note, body.scope)
        except RequestError as e:
            raise HTTPException(409, str(e)) from e
        return reqs.view(r)
