"""Streaming over MCP and REST (workstream stream; docs/streaming.md).

MCP:
- `exec(stream=true)`: each chunk of output is a `notifications/progress` on the call (progress = bytes so far,
  message = the text), in order, on the request's own response stream; the result is exec's. The 2026-07-28
  revision has no other way to stream a tool's output: progress and log notifications are the request-scoped ones.
- `process`: background processes (start, attach, input, kill, list); attach streams like exec.
- `hangar://sandboxes/<sandbox>/watch?path=...`: a directory watch that runs while someone subscribes to that URI
  (`subscriptions/listen` or `resources/subscribe`); each change sends `notifications/resources/updated`.
- Events: a turn's events reach `hangar://.../events` subscribers as soon as they are parsed (the turn watcher
  follows the run log live), and the resource reads `?since=<seq>` for just the new ones.

REST (Server-Sent Events): `POST /api/sandboxes/{ref}/exec/stream`, background processes under
`/api/sandboxes/{ref}/processes`, `GET /api/sandboxes/{ref}/watch`, and `GET /api/sessions/{ref}/events/stream` /
`GET /api/jobs/{id}/events/stream` (resumable with `Last-Event-ID`).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import collections
import contextlib
import json
import logging
import re
import threading
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

import mcp_types as types
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer, ResourceSecurity
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field

from hangar import mcp_live as live
from hangar import sandbox_streams as ss
from hangar.auth import bearer_from_headers
from hangar.events import RunEvent
from hangar.fleet import FleetError
from hangar.streams import Stream, StreamClosed, StreamLimit, credential, hub, token_check

log = logging.getLogger("hangar.streams")

KEEPALIVE = 15.0  # seconds between SSE comments on a quiet stream, so proxies don't time it out
SUB_BACKLOG = 2000  # events queued for one slow SSE reader before it re-reads from the log instead
WATCH_URI = re.compile(r"^hangar://sandboxes/([A-Za-z0-9._-]{1,128})/watch\?(.{1,4200})$")
INVALID_PARAMS = -32602


# ====================================================================== live events
class Sub:
    """One reader's queue of new events. Past SUB_BACKLOG it only notes it fell behind (the reader goes back to
    the log), so a slow reader costs bounded memory."""

    def __init__(self) -> None:
        self.events: collections.deque[RunEvent] = collections.deque()
        self.lagged = False
        self.ready = asyncio.Event()

    def put(self, evs: list[RunEvent]) -> None:
        if len(self.events) + len(evs) > SUB_BACKLOG:
            self.events.clear()
            self.lagged = True
        else:
            self.events.extend(evs)
        self.ready.set()

    def take(self) -> tuple[list[RunEvent], bool]:
        evs, lagged = list(self.events), self.lagged
        self.events.clear()
        self.lagged = False
        self.ready.clear()
        return evs, lagged


class EventFeed:
    """Events as `Activity.append` records them, to the SSE readers of that session or job (any thread)."""

    def __init__(self) -> None:
        self.subs: dict[str, set[Sub]] = collections.defaultdict(set)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def publish(self, key: str, evs: list[RunEvent]) -> None:
        with self._lock:
            subs, loop = list(self.subs.get(key, ())), self.loop
        if not subs or loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for sub in subs:
            if running is loop:
                sub.put(evs)
            else:
                loop.call_soon_threadsafe(sub.put, evs)

    @contextlib.contextmanager
    def subscribe(self, key: str):
        sub = Sub()
        with self._lock:
            self.loop = asyncio.get_running_loop()
            self.subs[key].add(sub)
        try:
            yield sub
        finally:
            with self._lock:
                self.subs[key].discard(sub)
                if not self.subs[key]:
                    del self.subs[key]


_FEEDS: weakref.WeakKeyDictionary[Any, EventFeed] = weakref.WeakKeyDictionary()


def event_feed(sessions: Any) -> EventFeed:
    """The sessions service's live event feed, hooked into its Activity on first use."""
    f = _FEEDS.get(sessions)
    if f is None:
        f = _FEEDS[sessions] = EventFeed()
        sessions.activity.listeners.append(f.publish)
    return f


def _sse(data: Any, event: str | None = None, id: int | None = None) -> bytes:
    head = (f"id: {id}\n" if id is not None else "") + (f"event: {event}\n" if event else "")
    return f"{head}data: {json.dumps(data, separators=(',', ':'), default=str)}\n\n".encode()


async def _event_stream(st: Stream, feed: EventFeed, key: str, since: int,
                        read: Callable[[int], list[dict[str, Any]]]) -> AsyncIterator[bytes]:
    """Every event after `since` (from the log), then each new one as it is recorded, until the stream closes. A
    reader that falls behind goes back to the log; `seq` keeps each event sent once, in order."""
    cut = _sse({"reason": "over HANGAR_STREAM_MAX_BYTES; reconnect with Last-Event-ID", "code": "cap"}, "close")
    with feed.subscribe(key) as sub:  # before reading the log, so nothing recorded in between is missed
        last, from_log = since, True
        try:
            while True:
                if from_log:
                    batch = await asyncio.to_thread(read, last)  # a scan of the log file: off the loop
                    out = [(e["seq"], e) for e in batch]
                    from_log = len(batch) >= 500
                else:
                    evs, from_log = sub.take()
                    out = [(ev.seq, ev.model_dump(mode="json", exclude={"off"})) for ev in evs]
                    if not evs and not from_log:
                        try:
                            await st.race(asyncio.wait_for(sub.ready.wait(), KEEPALIVE))
                        except TimeoutError:
                            yield b": keepalive\n\n"
                        continue
                for seq, e in out:
                    if seq <= last:
                        continue
                    chunk = _sse(e, id=seq)
                    if not await st.send(len(chunk)):
                        yield cut
                        return
                    yield chunk
                    last = seq
        except StreamClosed as e:
            yield _sse({"reason": str(e), "code": e.code}, "close")


# ====================================================================== MCP
async def exec_stream(fleet: Any, ctx: Context, sandbox: str, command: str, cwd: str | None, timeout: int,
                      owner: str, actor: str) -> dict[str, Any]:
    """MCP exec(stream=true): output as progress notifications, then exec's result."""
    sent = 0
    decoders = {k: codecs.getincrementaldecoder("utf-8")(errors="replace") for k in ("stdout", "stderr")}

    async def emit(kind: str, data: bytes) -> None:
        nonlocal sent
        sent += len(data)
        text = decoders[kind].decode(data)
        if text:
            await ctx.report_progress(sent, None, text)

    sb = fleet.get(sandbox)
    async with hub(fleet).open(owner, "exec", sb.id, attrs={"hangar.transport": "mcp"}) as st:
        return await ss.run(fleet, sb.id, command, cwd=cwd, timeout=timeout, actor=actor, st=st, emit=emit)


def _parse_watch(uri: str) -> tuple[str, str, bool] | None:
    m = WATCH_URI.match(uri)
    if not m:
        return None
    q = parse_qs(m.group(2), keep_blank_values=True)
    path = (q.get("path") or [""])[0]
    if not path:
        return None
    return m.group(1), path, (q.get("recursive") or ["false"])[0].lower() in ("true", "1", "yes")


def register_streams(mcp: MCPServer, tool: Callable[[], Any], fleet: Any, sessions: Any, jobs: Any,
                     feed: live.ChangeFeed, owner: Callable[[], str]) -> None:
    """The `process` tool, the watch resource with its subscription hooks, and live event notifications."""
    procs = ss.processes(fleet)
    watches = ss.Watches(fleet, feed.mark)

    # --- events reach `.../events` subscribers as they are recorded, not when the session is next saved
    def on_events(key: str, _evs: list[RunEvent]) -> None:
        s = sessions._sessions.get(key)
        uris = [live.session_uri(s.name) + "/events"] if s is not None else []
        if jobs is not None and key in jobs._jobs:
            uris.append(live.job_uri(key) + "/events")
        if uris:
            feed.mark(uris)

    if sessions is not None:
        sessions.activity.listeners.append(on_events)

    @tool()
    async def process(sandbox: str, ctx: Context, action: str = "list", command: str | None = None,
                      process_id: str | None = None, input: str | None = None, eof: bool = False,
                      since: int | None = None, wait_seconds: int = 0, cwd: str | None = None) -> dict[str, Any]:
        """Background commands (dev servers, watchers). action: start (command, cwd) -> process_id | attach
        (process_id, since, wait_seconds<=600; output as progress; returns its tail and `next`) | input (input,
        eof) | kill | list. Yours only."""
        who = owner()
        if action == "list":
            return {"processes": await procs.list(who, sandbox)}
        if action == "start":
            p = await procs.start(who, sandbox, command or "", cwd, actor=f"mcp:{who}")
            return p.view()
        p = await procs.get(who, sandbox, process_id or "")
        if action == "attach":
            dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

            async def emit(nxt: int, data: bytes) -> None:
                if text := dec.decode(data):
                    await ctx.report_progress(nxt, None, text)

            wait = max(0, min(600, int(wait_seconds)))
            async with hub(fleet).open(who, "process_attach", p.sandbox_id) as st:
                return await procs.attach(p, since, wait, st, emit if wait else None)
        if action == "input":
            await procs.send_input(p, (input or "").encode(), eof)
            return p.view()
        if action == "kill":
            return await procs.kill(p)
        raise ValueError(f"unknown action '{action}' (start | attach | input | kill | list)")

    # --- file watches: a resource that is live while someone subscribes to it
    # `path` is a path in the sandbox, read by its daemon as the sandbox user, never one on this machine: absolute
    # paths are what it usually is (the SDK's path checks are for servers that serve their own files)
    @mcp.resource("hangar://sandboxes/{sandbox}/watch{?path,recursive,since}", name="sandbox-watch",
                  mime_type="application/json", security=ResourceSecurity(exempt_params={"path"}),
                  description="Changes under a directory of a sandbox (`path`: absolute or relative to its repo; "
                              "`recursive`: true|false), watched while you subscribe to this URI. Reading returns "
                              "the latest events; `since` (an earlier `next`) only the newer ones.")
    async def watch_resource(sandbox: str, path: str = "", recursive: str = "false", since: str = "0") -> str:
        w = watches.find(owner(), sandbox, path, recursive.lower() in ("true", "1", "yes"))
        if w is None:
            return live._json({"sandbox": sandbox, "path": path, "state": "not watching",
                               "hint": "subscribe to this URI to watch it"})
        return live._json(w.view(int(since) if since.isdigit() else 0))

    def hold(uri: str) -> ss.Watch:
        parsed = _parse_watch(uri)
        if parsed is None:
            raise MCPError(INVALID_PARAMS, f"not a watch URI: {uri} (hangar://sandboxes/<sandbox>/watch?path=<dir>"
                                           "[&recursive=true])")
        try:
            from hangar.people import visible  # --- identity: a member watches only their own sandboxes

            if not visible(fleet.get(parsed[0]).person):
                raise FleetError(f"no sandbox '{parsed[0]}'")
            return watches.acquire(owner(), parsed[0], parsed[1], parsed[2], uri)
        except (FleetError, ValueError) as e:
            raise MCPError(INVALID_PARAMS, str(e)) from e

    low = mcp._lowlevel_server  # noqa: SLF001 - the SDK has no hook for "a client subscribed to this URI"
    listen = low._request_handlers.get("subscriptions/listen")  # noqa: SLF001

    if listen is not None:
        inner = listen.handler

        async def listen_with_watches(ctx: ServerRequestContext[Any],
                                      params: types.SubscriptionsListenRequestParams) -> Any:
            """subscriptions/listen, holding a watch for each watch URI it asks for, for as long as it is open;
            and ended gracefully (its final result sent) when hangar shuts down."""
            held: list[tuple[ss.Watch, str]] = []
            try:
                for uri in (params.notifications.resource_subscriptions or [])[:64]:
                    if WATCH_URI.match(str(uri)):
                        held.append((hold(str(uri)), str(uri)))
            except MCPError:
                for w, u in held:
                    watches.release(w, u)
                raise
            closer = asyncio.ensure_future(_close_on_shutdown(fleet, inner))
            try:
                return await inner(ctx, params)
            finally:
                closer.cancel()
                for w, u in held:
                    watches.release(w, u)

        low.add_request_handler("subscriptions/listen", types.SubscriptionsListenRequestParams, listen_with_watches)

    sub_entry = low._request_handlers.get("resources/subscribe")  # noqa: SLF001
    unsub_entry = low._request_handlers.get("resources/unsubscribe")  # noqa: SLF001
    if sub_entry is not None and unsub_entry is not None:
        legacy: dict[tuple[str, int], tuple[ss.Watch, weakref.ref]] = {}

        def drop(key: tuple[str, int], ref: weakref.ref | None = None) -> None:
            """Release a connection's watch: on unsubscribe, or (`ref`) once that connection is gone, unless its
            id was reused by a newer connection's subscription meanwhile."""
            got = legacy.get(key)
            if got is not None and (ref is None or got[1] is ref):
                del legacy[key]
                watches.release(got[0], key[0])

        def gone(loop: asyncio.AbstractEventLoop, key: tuple[str, int], ref: weakref.ref) -> None:
            if not loop.is_closed():
                with contextlib.suppress(RuntimeError):  # the loop closed in between
                    loop.call_soon_threadsafe(drop, key, ref)

        async def subscribe(ctx: ServerRequestContext[Any], params: types.SubscribeRequestParams) -> Any:
            uri = str(params.uri)
            if not uri.startswith("hangar://sandboxes/"):
                return await sub_entry.handler(ctx, params)
            conn = live._connection(ctx)
            key = (uri, id(conn))
            held = legacy.get(key)
            if held is None or held[1]() is not conn:
                w = hold(uri)
                if held is not None:
                    drop(key)
                loop = asyncio.get_running_loop()
                # the connection going away (its session ended) releases the watch
                legacy[key] = (w, weakref.ref(conn, lambda r, k=key: gone(loop, k, r)))
            feed.bind()
            feed.legacy.setdefault(uri, weakref.WeakSet()).add(conn)
            return types.EmptyResult()

        async def unsubscribe(ctx: ServerRequestContext[Any], params: types.UnsubscribeRequestParams) -> Any:
            uri = str(params.uri)
            if uri.startswith("hangar://sandboxes/"):
                drop((uri, id(live._connection(ctx))))
            return await unsub_entry.handler(ctx, params)

        low.add_request_handler("resources/subscribe", types.SubscribeRequestParams, subscribe)
        low.add_request_handler("resources/unsubscribe", types.UnsubscribeRequestParams, unsubscribe)


async def _close_on_shutdown(fleet: Any, listen: Any) -> None:
    await fleet.closing.wait()
    with contextlib.suppress(Exception):
        listen.close()


# ====================================================================== REST
class ProcessBody(BaseModel):
    command: str = Field(min_length=1, max_length=100_000)
    cwd: str | None = Field(None, max_length=4096)


class InputBody(BaseModel):
    input: str = Field("", max_length=1 << 20)
    base64: bool = False
    eof: bool = False


class StreamExecBody(BaseModel):
    command: str = Field(min_length=1, max_length=100_000)
    cwd: str | None = Field(None, max_length=4096)
    timeout: int = Field(120, ge=1, le=24 * 3600)


def mount(app: FastAPI, h: Any, admin: Any, job_caller: Any, owner_filter: Callable[[Any], str | None]) -> None:
    """`admin` and `job_caller` are app.py's auth dependencies (admin tokens; a job's submitter or admin)."""
    fleet, sessions, jobs = h.fleet, h.sessions, h.jobs
    procs = ss.processes(fleet)
    feed = event_feed(sessions)
    the_hub = hub(fleet)
    watches = ss.Watches(fleet, lambda _uris: None)

    def check(request: Request, *scopes: str) -> Callable[[], Awaitable[bool]]:
        return token_check(h.auth, app.state.oauth, bearer_from_headers(request.headers), scopes)

    def who(actor: str) -> str:
        return actor.removeprefix("ui:")  # the same token's name, as MCP calls it

    def admit(owner: str) -> None:
        if fleet.closing.is_set():
            raise HTTPException(503, "hangar is restarting; reconnect in a moment", headers={"Retry-After": "2"})
        n = sum(1 for st in the_hub.streams.values() if st.owner == owner and st.lifetime)
        if n >= h.settings.stream_max_per_token:
            raise HTTPException(429, f"{owner} already has {n} open streams (HANGAR_STREAM_MAX_PER_TOKEN)")

    def respond(gen: AsyncIterator[bytes]) -> StreamingResponse:
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def opened(owner: str, kind: str, sandbox_id: str | None, chk: Callable[[], Awaitable[bool]],
                     body: Callable[[Stream], AsyncIterator[bytes]]) -> AsyncIterator[bytes]:
        try:
            async with the_hub.open(owner, kind, sandbox_id, check=chk) as st:
                async for chunk in body(st):
                    yield chunk
        except StreamLimit as e:
            yield _sse({"reason": str(e), "code": "limit"}, "close")

    # ------------------------------------------------------------------ exec
    @app.post("/api/sandboxes/{ref}/exec/stream")
    async def exec_stream_route(ref: str, body: StreamExecBody, request: Request, actor: str = admin):
        """Run a command and stream its output as Server-Sent Events: `stdout` and `stderr` events ({"data":
        text}), then one `result` event (exec's result). Closing the connection kills the command."""
        sb = fleet.get(ref)
        owner = who(actor)
        admit(owner)
        chk = check(request, "admin")

        async def run(st: Stream) -> AsyncIterator[bytes]:
            q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
            decoders = {k: codecs.getincrementaldecoder("utf-8")(errors="replace") for k in ("stdout", "stderr")}

            async def emit(kind: str, data: bytes) -> None:
                if text := decoders[kind].decode(data):
                    await q.put(_sse({"data": text}, kind))

            async def produce() -> None:
                try:
                    res = await ss.run(fleet, sb.id, body.command, cwd=body.cwd, timeout=body.timeout, actor=actor,
                                       st=st, emit=emit)
                    await q.put(_sse(res, "result"))
                except (FleetError, ValueError, StreamLimit) as e:
                    await q.put(_sse({"error": str(e)}, "error"))
                except Exception as e:  # noqa: BLE001 - reported on the stream; its headers are already sent
                    log.warning("streamed exec in %s failed: %s", sb.id, e)
                    await q.put(_sse({"error": h.fleet.broker.redact(str(e))[:500]}, "error"))
                finally:
                    await q.put(None)

            task = asyncio.create_task(produce())
            try:
                while (item := await q.get()) is not None:
                    yield item
            finally:
                if not task.done():
                    task.cancel()  # the client went away: the command is killed (ss.run)
                await asyncio.gather(task, return_exceptions=True)

        return respond(opened(owner, "exec", sb.id, chk, run))

    # ------------------------------------------------------------------ background processes
    @contextlib.contextmanager
    def as_caller(request: Request):
        """(review #68) A process this request starts, or finds again after a restart, is followed under its token,
        so revoking the token ends it, as for one started over MCP."""
        tok = credential.set(check(request, "admin"))
        try:
            yield
        finally:
            credential.reset(tok)

    @app.post("/api/sandboxes/{ref}/processes")
    async def process_start(ref: str, body: ProcessBody, request: Request, actor: str = admin):
        try:
            with as_caller(request):
                p = await procs.start(who(actor), ref, body.command, body.cwd, actor=actor)
        except StreamLimit as e:
            raise HTTPException(429, str(e)) from e
        return p.view()

    @app.get("/api/sandboxes/{ref}/processes")
    async def process_list(ref: str, request: Request, actor: str = admin):
        with as_caller(request):
            return await procs.list(who(actor), ref)

    @app.get("/api/sandboxes/{ref}/processes/{process_id}")
    async def process_get(ref: str, process_id: str, request: Request, since: int | None = None,
                          actor: str = admin):
        """The process and its buffered output from `since` (poll with the returned `next`)."""
        with as_caller(request):
            p = await procs.get(who(actor), ref, process_id)
        async with the_hub.open(who(actor), "process_attach", p.sandbox_id, capped=False) as st:
            return await procs.attach(p, since, 0, st, None)

    @app.get("/api/sandboxes/{ref}/processes/{process_id}/stream")
    async def process_stream(ref: str, process_id: str, request: Request, since: int | None = None,
                             actor: str = admin):
        """Attach: `output` events ({"data": text, "next": offset}, `id` = next) from `since` (or Last-Event-ID)
        on, then an `end` event with the process when it exits. Detaching (closing the connection) leaves it
        running."""
        owner = who(actor)
        with as_caller(request):
            p = await procs.get(owner, ref, process_id)
        last = request.headers.get("last-event-id", "")
        since = int(last) if last.isdigit() else since
        admit(owner)

        async def attach(st: Stream) -> AsyncIterator[bytes]:
            q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
            dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

            async def emit(nxt: int, data: bytes) -> None:
                await q.put(_sse({"data": dec.decode(data), "next": nxt}, "output", id=nxt))

            async def produce() -> None:
                try:
                    res = await procs.attach(p, since, None, st, emit)
                    res.pop("output", None)
                    await q.put(_sse(res, "end" if res["state"] != "running" else "close"))
                finally:
                    await q.put(None)

            task = asyncio.create_task(produce())
            try:
                while (item := await q.get()) is not None:
                    yield item
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return respond(opened(owner, "process_attach", p.sandbox_id, check(request, "admin"), attach))

    @app.post("/api/sandboxes/{ref}/processes/{process_id}/input")
    async def process_input(ref: str, process_id: str, body: InputBody, request: Request, actor: str = admin):
        with as_caller(request):
            p = await procs.get(who(actor), ref, process_id)
        try:
            data = base64.b64decode(body.input, validate=True) if body.base64 else body.input.encode()
        except (binascii.Error, ValueError) as e:
            raise HTTPException(400, "input is not valid base64") from e
        await procs.send_input(p, data, body.eof)
        return p.view()

    @app.delete("/api/sandboxes/{ref}/processes/{process_id}")
    async def process_kill(ref: str, process_id: str, request: Request, actor: str = admin):
        with as_caller(request):
            p = await procs.get(who(actor), ref, process_id)
        return await procs.kill(p)

    # ------------------------------------------------------------------ file watches
    @app.get("/api/sandboxes/{ref}/watch")
    async def watch_route(ref: str, path: str, request: Request, recursive: bool = False, actor: str = admin):
        """Changes under `path` as Server-Sent Events ({"seq", "ts", "name", "type"}), while connected."""
        owner = who(actor)
        sb = fleet.get(ref)
        admit(owner)
        uri = f"sse:{id(request)}"
        try:
            watches.check(owner, sb.id, path, recursive)
        except StreamLimit as e:
            raise HTTPException(429, str(e)) from e

        async def follow(st: Stream) -> AsyncIterator[bytes]:
            # (review #68) held only once the stream is open, and released by it: a stream refused by the hub, or
            # never started (the client went first), would otherwise hold the watch, and its quota, for good
            try:
                w = watches.acquire(owner, sb.id, path, recursive, uri)
            except StreamLimit as e:
                yield _sse({"reason": str(e), "code": "limit"}, "close")
                return
            last = 0
            try:
                while True:
                    tick = w.tick
                    v = w.view(last)
                    for e in v["events"]:
                        chunk = _sse(e, id=e["seq"])
                        if not await st.send(len(chunk)):
                            yield _sse({"reason": "over HANGAR_STREAM_MAX_BYTES"}, "close")
                            return
                        yield chunk
                    last = v["next"]
                    if w.state == "stopped":
                        yield _sse({"reason": w.error or "the watch ended"}, "close")
                        return
                    try:
                        await st.race(asyncio.wait_for(tick.wait(), KEEPALIVE))
                    except TimeoutError:
                        yield b": keepalive\n\n"
            except StreamClosed as e:
                yield _sse({"reason": str(e), "code": e.code}, "close")
            finally:
                watches.release(w, uri)

        return respond(opened(owner, "watch_sse", sb.id, check(request, "admin"), follow))

    # ------------------------------------------------------------------ events
    def since_of(request: Request, since: int) -> int:
        last = request.headers.get("last-event-id", "")
        return int(last) if last.isdigit() else max(0, since)

    @app.get("/api/sessions/{ref}/events/stream")
    async def session_events_stream(ref: str, request: Request, since: int = 0, actor: str = admin):
        """The session's events as Server-Sent Events (`id` = seq), from `since` (or Last-Event-ID) on, as the
        agent produces them."""
        s = sessions.get(ref)
        owner = who(actor)
        admit(owner)
        start = since_of(request, since)

        def read(after: int) -> list[dict[str, Any]]:
            return sessions.events(s.id, since=after, limit=500)

        return respond(opened(owner, "events", None, check(request, "admin"),
                              lambda st: _event_stream(st, feed, s.id, start, read)))

    @app.get("/api/jobs/{job_id}/events/stream")
    async def job_events_stream(job_id: str, request: Request, since: int = 0, caller: Any = job_caller):
        """The job's events as Server-Sent Events, as for a session's. A `jobs` token sees only its own jobs."""
        job = jobs.get(job_id, owner=owner_filter(caller))
        owner = caller.requester
        admit(owner)
        start = since_of(request, since)
        restricted = owner_filter(caller)

        def read(after: int) -> list[dict[str, Any]]:
            return jobs.events(job.id, owner=restricted, since=after, limit=500)

        return respond(opened(owner, "events", None, check(request, "admin", "jobs"),
                              lambda st: _event_stream(st, feed, job.id, start, read)))
