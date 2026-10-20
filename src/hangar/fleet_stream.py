"""`GET /api/fleet/stream`: the Fleet page's live feed (KAN-140 S5; docs/fleet-api.md#the-fleet-stream).

Instead of re-reading `GET /api/fleet` on a timer, the console opens one Server-Sent Events stream. It starts with a
`snapshot` (the whole fleet read), then sends what changed:

* `agent`: one unit (as in the fleet read) whose picture changed: at most one per agent per `COALESCE_S` (2 s), so a
  turn writing a hundred events a second costs the browser one update every two seconds;
* `gone`: `{"id"}` of a unit that left the fleet (destroyed, or no longer visible);
* `inbox`: the caller's inbox (`{"counts", "items"}`) when it changed, the deck's one source as on the read;
* `summary`: `{"counts", "owners", "slots"}` when they changed;
* `close`: `{"reason", "code"}` before the server ends the stream (a limit, a revoked token, a restart).

What wakes a stream is a `Changes` record: the turn watcher's LiveState listeners (what an agent is doing) and store
writes of sandboxes, jobs, sessions and environment requests (state, approvals, questions), plus inbox acks. Each
change has a sequence number; every event's `id` is `<epoch>.<seq>`, the change it reflects. A client that
reconnects with `Last-Event-ID` gets only the units changed since then (and the inbox); one whose id is from another
epoch (a restart) or older than the `RING` most recent changes gets a fresh `snapshot`.

Auth, as the build stream's: browsers can't set headers on an EventSource, so the console trades its token for a
one-time ticket (`POST /api/fleet/tickets`, 60 s, bound to the caller: a member's ticket sees that member's fleet).
An admin ticket from `POST /api/tickets` or a bearer token (`user` or admin) works too. The stream counts against the
stream limits (HANGAR_STREAM_MAX_PER_TOKEN, _TOTAL, _BYTES_PER_SECOND, _MAX_BYTES, _MAX_HOURS: streams.py) and ends
when the token behind it is revoked.
"""

from __future__ import annotations

import asyncio
import collections
import json
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from hangar import identity, people
from hangar.auth import Principal, bearer_from_headers
from hangar.streams import Stream, StreamClosed, StreamLimit, hub, token_check

COALESCE_S = 2.0  # at most one update per agent per this many seconds
KEEPALIVE = 15.0  # an SSE comment on a quiet stream, so proxies keep it open
RING = 4096  # changes remembered for Last-Event-ID resume
TICKET_TTL = 60.0
_UNSTABLE = ("stage_estimate",)  # moves with the clock alone: sent with a unit, never the reason to send one


def _sse(data: Any, event: str, id: str | None = None) -> bytes:
    head = (f"id: {id}\n" if id is not None else "") + f"event: {event}\n"
    return f"{head}data: {json.dumps(data, separators=(',', ':'), default=str)}\n\n".encode()


def _key(u: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in u.items() if k not in _UNSTABLE}, sort_keys=True, default=str)


class Changes:
    """Every change a fleet stream may need to send, numbered, for one control plane. Writers are on any thread
    (store hooks); each open stream registers an asyncio.Event on its loop and is woken thread-safely."""

    def __init__(self, h: Any) -> None:
        self.h = h
        self.epoch = secrets.token_hex(4)
        self.seq = 0
        self.ring: collections.deque[tuple[int, str | None]] = collections.deque(maxlen=RING)
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        h.store.on_put.append(self._on_put)
        h.sessions.activity.live.listeners.append(self._on_live)
        h.jobs.inbox_listeners = [*getattr(h.jobs, "inbox_listeners", []), lambda: self.mark(None)]  # acks

    # ------------------------------------------------------------------ writers
    def mark(self, unit: str | None) -> None:
        """A change to `unit` (a sandbox id), or to the inbox or something not tied to one unit (None)."""
        with self._lock:
            self.seq += 1
            self.ring.append((self.seq, unit))
            waiters = list(self._waiters)
        for loop, ev in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(ev.set)

    def _on_put(self, obj: Any) -> None:
        from hangar.envrequests import EnvRequest
        from hangar.models import Job, Sandbox, Session

        if isinstance(obj, Sandbox):
            self.mark(obj.id)
        elif isinstance(obj, Job | Session):
            self.mark(obj.sandbox_id)
        elif isinstance(obj, EnvRequest):
            self.mark(None)

    def _on_live(self, session_id: str, _state: Any) -> None:
        s = self.h.sessions._sessions.get(session_id)
        self.mark(s.sandbox_id if s is not None else None)

    # ------------------------------------------------------------------ readers
    def id(self, seq: int) -> str:
        return f"{self.epoch}.{seq}"

    def since(self, last_id: str) -> set[str | None] | None:
        """The units changed after the event `last_id` (None among them: the inbox or the summary), or None when
        that can't be told: another epoch, a malformed id, or older than the ring remembers."""
        epoch, _, n = last_id.partition(".")
        if epoch != self.epoch or not n.isdigit():
            return None
        seq = int(n)
        with self._lock:
            if seq > self.seq:
                return None
            if seq < self.seq and (not self.ring or self.ring[0][0] > seq + 1):
                return None
            return {u for s, u in self.ring if s > seq}

    def waiter(self) -> asyncio.Event:
        ev = asyncio.Event()
        with self._lock:
            self._waiters.add((asyncio.get_running_loop(), ev))
        return ev

    def unwait(self, ev: asyncio.Event) -> None:
        with self._lock:
            self._waiters = {w for w in self._waiters if w[1] is not ev}


class Tickets:
    """One-time fleet tickets: each is bound to the principal that asked for it, and to a re-check of its token."""

    def __init__(self) -> None:
        self._t: dict[str, tuple[Principal, Callable[[], Awaitable[bool]] | None, float]] = {}
        self._lock = threading.Lock()

    def issue(self, p: Principal, check: Callable[[], Awaitable[bool]] | None, ttl: float = TICKET_TTL) -> str:
        t = time.time()
        raw = "ftk_" + secrets.token_urlsafe(24)
        with self._lock:
            self._t = {k: v for k, v in self._t.items() if v[2] > t}
            self._t[raw] = (p, check, t + ttl)
        return raw

    def redeem(self, raw: str | None) -> tuple[Principal, Callable[[], Awaitable[bool]] | None] | None:
        if not raw:
            return None
        with self._lock:
            hit = self._t.pop(raw, None)
        return (hit[0], hit[1]) if hit is not None and hit[2] > time.time() else None


# ====================================================================== the stream
async def follow(st: Stream, reader: Any, changes: Changes, p: Principal | None, owner: str | None,
                 last_id: str | None) -> AsyncIterator[bytes]:
    """The events of one fleet stream, for one caller, until the stream closes."""

    def read() -> dict[str, Any]:
        tok = identity.current.set(p)  # the caller's visibility, whatever task runs this generator
        try:
            return reader.units(p, False, owner)
        finally:
            identity.current.reset(tok)

    async def emit(chunk: bytes) -> bool:
        return await st.send(len(chunk))

    def theirs(sandbox_id: str) -> bool:
        """A unit no longer on this caller's fleet that they may have had: never tell a member someone else's ids."""
        from hangar.models import Sandbox

        sb = reader.h.store.get(Sandbox, sandbox_id)
        if sb is None:
            return False
        tok = identity.current.set(p)
        try:
            return people.visible(sb.person) and (owner is None or (sb.person or "none") == owner)
        finally:
            identity.current.reset(tok)

    def summary(body: dict[str, Any]) -> dict[str, Any]:
        return {"counts": body["counts"], "owners": body["owners"], "slots": body["slots"]}

    wake = changes.waiter()
    cut = _sse({"reason": "over HANGAR_STREAM_MAX_BYTES; reconnect with Last-Event-ID", "code": "cap"}, "close")
    try:
        seq = changes.seq
        body = read()
        sent = {u["id"]: _key(u) for u in body["units"]}
        sent_inbox, sent_summary = json.dumps(body["inbox"], sort_keys=True), json.dumps(summary(body),
                                                                                         sort_keys=True)
        dirty = changes.since(last_id) if last_id else None
        if dirty is None:
            chunk = _sse(body, "snapshot", changes.id(seq))
            if not await emit(chunk):
                yield cut
                return
            yield chunk
        else:  # resume: what changed since the client's last event, as it is now
            here = {u["id"]: u for u in body["units"]}
            out = [_sse(here[u], "agent", changes.id(seq)) if u in here else _sse({"id": u}, "gone", changes.id(seq))
                   for u in sorted(x for x in dirty if x is not None) if u in here or theirs(u)]
            out += [_sse(body["inbox"], "inbox", changes.id(seq)), _sse(summary(body), "summary", changes.id(seq))]
            for chunk in out:
                if not await emit(chunk):
                    yield cut
                    return
                yield chunk
        last = time.monotonic()
        while True:
            try:
                await st.race(asyncio.wait_for(wake.wait(), KEEPALIVE))
            except TimeoutError:
                yield b": keepalive\n\n"
                continue
            if (gap := COALESCE_S - (time.monotonic() - last)) > 0:
                await st.race(asyncio.sleep(gap))  # coalesce: everything that changed meanwhile goes in one pass
            wake.clear()
            seq = changes.seq
            body = read()
            last = time.monotonic()
            eid = changes.id(seq)
            out = []
            here = set()
            for u in body["units"]:
                here.add(u["id"])
                if sent.get(u["id"]) != (k := _key(u)):
                    sent[u["id"]] = k
                    out.append(_sse(u, "agent", eid))
            for gone in [i for i in sent if i not in here]:
                del sent[gone]
                out.append(_sse({"id": gone}, "gone", eid))
            if (ib := json.dumps(body["inbox"], sort_keys=True)) != sent_inbox:
                sent_inbox = ib
                out.append(_sse(body["inbox"], "inbox", eid))
            if (sm := json.dumps(summary(body), sort_keys=True)) != sent_summary:
                sent_summary = sm
                out.append(_sse(summary(body), "summary", eid))
            for chunk in out:
                if not await emit(chunk):
                    yield cut
                    return
                yield chunk
    except StreamClosed as e:
        yield _sse({"reason": str(e), "code": e.code}, "close")
    finally:
        changes.unwait(wake)


# ====================================================================== front door
def mount(app: Any, h: Any, person_actor: Any, reader: Any) -> Changes:
    changes = Changes(h)
    tickets = Tickets()
    the_hub = hub(h.fleet)
    h.fleet_changes, h.fleet_tickets = changes, tickets

    def check_of(request: Request, raw: str | None) -> Callable[[], Awaitable[bool]]:
        return token_check(h.auth, getattr(request.app.state, "oauth", None), raw, ("user",))

    @app.post("/api/fleet/tickets")
    async def fleet_ticket(request: Request, _: str = person_actor):
        """A one-time ticket (60 s) for `GET /api/fleet/stream?ticket=`, bound to the caller."""
        p = identity.principal()
        assert p is not None
        raw = tickets.issue(p, check_of(request, bearer_from_headers(request.headers)))
        return {"ticket": raw, "ttl_seconds": int(TICKET_TTL), "stream": f"/api/fleet/stream?ticket={raw}"}

    @app.get("/api/fleet/stream")
    async def fleet_stream(request: Request, ticket: str | None = None, owner: str | None = None):
        if owner is not None and (not owner or len(owner) > 100):
            raise HTTPException(400, "owner is a person id, or none")
        p: Principal | None
        chk: Callable[[], Awaitable[bool]] | None
        if (hit := tickets.redeem(ticket)) is not None:
            p, chk = hit
        elif ticket and (actor := h.auth.redeem_ticket(ticket)):  # an admin's POST /api/tickets ticket
            p, chk = Principal(actor, f"ticket:{actor}", ["admin"], role="admin"), None
        else:
            raw = bearer_from_headers(request.headers)
            p = h.auth.principal(raw)
            if p is None:
                raise HTTPException(401, "unauthorized: a ticket (POST /api/fleet/tickets) or a token",
                                    headers={"WWW-Authenticate": "Bearer"})
            if not (p.admin or "user" in p.scopes):
                raise HTTPException(403, f"token scopes {p.scopes} do not allow this route")
            chk = check_of(request, raw)
        if h.fleet.closing.is_set():
            raise HTTPException(503, "hangar is restarting; reconnect in a moment", headers={"Retry-After": "2"})
        n = sum(1 for st in the_hub.streams.values() if st.owner == p.name and st.lifetime)
        if n >= h.settings.stream_max_per_token:
            raise HTTPException(429, f"{p.name} already has {n} open streams (HANGAR_STREAM_MAX_PER_TOKEN)")
        last_id = request.headers.get("last-event-id") or None

        async def gen() -> AsyncIterator[bytes]:
            try:
                async with the_hub.open(p.name, "fleet", None, check=chk, system=chk is None) as st:
                    async for chunk in follow(st, reader, changes, p, owner, last_id):
                        yield chunk
            except StreamLimit as e:
                yield _sse({"reason": str(e), "code": "limit"}, "close")

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return changes


__all__ = ["COALESCE_S", "Changes", "Tickets", "follow", "mount"]
