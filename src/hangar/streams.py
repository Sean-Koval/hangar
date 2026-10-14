"""Streams to MCP and REST clients (workstream stream; docs/streaming.md): command output, a session's or job's
events and file changes as they happen, instead of polled.

Every stream hangar holds open for a client is registered here, so they are bounded and closed the same way: at most
`stream_max_per_token` per token (and `stream_max_total` in all), each paced to `stream_bytes_per_second` and cut at
`stream_max_bytes`, and every one ends when its sandbox is destroyed or paused, when its token is revoked (checked
every `stream_recheck_seconds`), after `stream_max_hours`, and at shutdown (#57's Fleet.closing). A producer waits
through `Stream.race`, which raises StreamClosed the moment any of those happens, so it can answer with what it has.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from hangar import metrics, observe
from hangar.models import Sandbox, SandboxState

log = logging.getLogger("hangar.streams")

# How the request that opens a stream proves, later, that its token is still valid (set by the MCP auth middleware
# and the REST routes). None: nothing to re-check (hangar's own streams).
credential: ContextVar[Callable[[], Awaitable[bool]] | None] = ContextVar("hangar_stream_credential", default=None)

# Why a stream ended: a short code for metrics, and what the client is told.
REASONS = {
    "done": "finished",
    "client": "the client went away",
    "shutdown": "hangar is restarting; reconnect in a moment",
    "destroyed": "the sandbox was destroyed",
    "paused": "the sandbox was paused",
    "revoked": "the token that opened this stream was revoked",
    "lifetime": "the stream was open longer than HANGAR_STREAM_MAX_HOURS",
    "error": "the stream failed",
}
_GONE = {SandboxState.STOPPING: "destroyed", SandboxState.STOPPED: "destroyed", SandboxState.ERROR: "destroyed",
         SandboxState.PAUSED: "paused"}
# What each message costs the rate beyond its payload (review #68): its framing (a progress notification's JSON, an
# SSE event) is far bigger than a byte, so a command printing a byte at a time can't send a flood of messages.
MESSAGE_COST = 64


class StreamLimit(ValueError):
    """Too many streams for this token or this control plane, or hangar is shutting down: nothing was opened."""


class StreamClosed(Exception):
    def __init__(self, code: str):
        super().__init__(REASONS.get(code, code))
        self.code = code


class Stream:
    """One open stream: its owner, what it follows, and the bytes it sent."""

    def __init__(self, hub: Hub, owner: str, kind: str, sandbox_id: str | None,
                 check: Callable[[], Awaitable[bool]] | None, lifetime: bool):
        self.hub, self.owner, self.kind, self.sandbox_id, self.check = hub, owner, kind, sandbox_id, check
        self.id = secrets.token_hex(6)
        self.lifetime = lifetime
        self.opened = time.monotonic()
        self.closed = asyncio.Event()
        self.code: str | None = None
        self.bytes = 0
        self.capped = False  # over stream_max_bytes: nothing more is sent
        self._allowance = float(hub.settings.stream_bytes_per_second)
        self._at = self.opened
        self._waiter: asyncio.Future | None = None

    @property
    def reason(self) -> str:
        return REASONS.get(self.code or "done", self.code or "")

    def close(self, code: str) -> None:
        if not self.closed.is_set():
            self.code = code
            self.closed.set()

    async def race[T](self, aw: Awaitable[T]) -> T:
        """`aw`'s result, or StreamClosed as soon as the stream is closed (`aw` is then cancelled)."""
        task = asyncio.ensure_future(aw)
        if self.closed.is_set():
            await asyncio.sleep(0)  # let it start, so the coroutines it was handed are awaited, then stop it
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise StreamClosed(self.code or "done")
        if self._waiter is None:
            self._waiter = asyncio.ensure_future(self.closed.wait())
        try:
            await asyncio.wait({task, self._waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task.done():
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise StreamClosed(self.code or "done")

    async def send(self, n: int) -> bool:
        """Account for `n` bytes about to be sent: waits while the stream is over its rate, and says False (and
        sends nothing more) once it is over its total."""
        if self.capped:
            return False
        if self.bytes + n > self.hub.settings.stream_max_bytes:
            self.capped = True
            return False
        rate = self.hub.settings.stream_bytes_per_second
        now = time.monotonic()
        self._allowance = min(float(rate), self._allowance + (now - self._at) * rate) - n - MESSAGE_COST
        self._at = now
        if self._allowance < 0:
            await self.race(asyncio.sleep(-self._allowance / rate))
        metrics.stream_sent(self.kind, n, time.monotonic() - self.opened if not self.bytes else None)
        self.bytes += n
        return True

    def _end(self) -> None:
        if self._waiter is not None:
            self._waiter.cancel()


class Hub:
    """The open streams of one control plane (one per Fleet: `hub(fleet)`)."""

    def __init__(self, fleet: Any):
        self.fleet = fleet
        self.settings = fleet.settings
        self.streams: dict[str, Stream] = {}
        self._guard: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.on_close_sandbox: list[Callable[[str, str], None]] = []  # background processes, watches
        fleet.store.on_put.append(self._on_put)

    def count(self, owner: str, kind: str | None = None) -> int:
        return sum(1 for st in self.streams.values() if st.owner == owner and (kind is None or st.kind == kind))

    def check_total(self) -> None:
        """StreamLimit if another stream can't open now (also asked before starting what a stream will follow)."""
        if len(self.streams) >= self.settings.stream_max_total:
            raise StreamLimit(f"hangar already holds {len(self.streams)} open streams (HANGAR_STREAM_MAX_TOTAL); "
                              "try again later")

    @contextlib.asynccontextmanager
    async def open(self, owner: str, kind: str, sandbox_id: str | None = None, *, capped: bool = True,
                   check: Callable[[], Awaitable[bool]] | None = None, system: bool = False,
                   attrs: dict[str, Any] | None = None) -> AsyncIterator[Stream]:
        """Register a stream for its whole life. `capped`: it counts against the per-token cap (hangar's own
        followers, like background process pumps, are capped by their own setting instead). `check` re-checks the
        token; by default the one of the request that opens it (`credential`), unless it is hangar's own
        (`system`: a turn log followed for whoever started the turn)."""
        if self.fleet.closing.is_set():
            raise StreamLimit("hangar is restarting; try again in a moment")
        if capped:
            mine = sum(1 for st in self.streams.values() if st.owner == owner and st.lifetime)
            if mine >= self.settings.stream_max_per_token:
                raise StreamLimit(f"{owner} already has {mine} open streams (HANGAR_STREAM_MAX_PER_TOKEN); close "
                                  "one first")
        self.check_total()
        st = Stream(self, owner, kind, sandbox_id, check if check is not None or system else credential.get(), capped)
        self.streams[st.id] = st
        self._loop = asyncio.get_running_loop()
        metrics.stream_opened(kind)
        self._start_guard()
        err: BaseException | None = None
        with observe.span(f"stream {kind}", {"hangar.sandbox.id": sandbox_id, "hangar.stream.kind": kind,
                                             **(attrs or {})}) as sp:
            try:
                yield st
            except BaseException as e:
                err = e
                raise
            finally:
                if st.code is None:
                    st.code = "client" if isinstance(err, asyncio.CancelledError | GeneratorExit) else \
                        "error" if err is not None and not isinstance(err, StreamClosed) else "done"
                self.streams.pop(st.id, None)
                st._end()
                metrics.stream_closed(kind, st.code)
                observe.set_attrs(sp, {"hangar.stream.bytes": st.bytes, "hangar.stream.end": st.code})

    # ------------------------------------------------------------------ closing
    def close_sandbox(self, sandbox_id: str, code: str) -> None:
        for st in list(self.streams.values()):
            if st.sandbox_id == sandbox_id:
                st.close(code)
        for fn in list(self.on_close_sandbox):
            try:
                fn(sandbox_id, code)
            except Exception:  # noqa: BLE001 - one listener must not keep the others from closing
                log.exception("closing streams of %s failed", sandbox_id)

    def close_all(self, code: str) -> None:
        for st in list(self.streams.values()):
            st.close(code)

    def _on_put(self, obj: Any) -> None:
        """Store hook (any thread): a sandbox that is going away or paused ends its streams."""
        if not isinstance(obj, Sandbox) or obj.state not in _GONE:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return  # nothing was ever streamed
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.close_sandbox(obj.id, _GONE[obj.state])
        else:
            loop.call_soon_threadsafe(self.close_sandbox, obj.id, _GONE[obj.state])

    def _start_guard(self) -> None:
        if self._guard is None or self._guard.done():
            self._guard = asyncio.get_running_loop().create_task(self._guarding())

    async def _guarding(self) -> None:
        """While streams are open: close them all at shutdown, and each one whose token was revoked or that has
        been open too long."""
        closing = self.fleet.closing
        while self.streams:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(closing.wait(), self.settings.stream_recheck_seconds)
            if closing.is_set():
                self.close_all("shutdown")
                return
            limit = self.settings.stream_max_hours * 3600
            now = time.monotonic()
            checked: dict[Any, bool] = {}
            for st in list(self.streams.values()):
                if st.lifetime and now - st.opened > limit:
                    st.close("lifetime")
                    continue
                if st.check is None or st.closed.is_set():
                    continue
                if st.check not in checked:
                    try:
                        checked[st.check] = bool(await st.check())
                    except Exception:  # noqa: BLE001 - a failing check (the database, say) is not a revocation
                        log.warning("could not re-check a stream's token", exc_info=True)
                        checked[st.check] = True
                if not checked[st.check]:
                    st.close("revoked")


_HUBS: weakref.WeakKeyDictionary[Any, Hub] = weakref.WeakKeyDictionary()


def hub(fleet: Any) -> Hub:
    """The fleet's stream hub, made on first use (no edit to Fleet itself)."""
    h = _HUBS.get(fleet)
    if h is None:
        h = _HUBS[fleet] = Hub(fleet)
    return h


def token_check(auth: Any, oauth: Any, raw: str | None, scopes: tuple[str, ...]) -> Callable[[], Awaitable[bool]]:
    """A re-check of the bearer token `raw`: still known, and still holding one of `scopes` (or admin). OAuth access
    tokens are asked of the OAuth server. The check holds the token itself only in memory, for the stream's life."""

    async def check() -> bool:
        have = auth.scopes_of(raw)
        if have is not None:
            return "admin" in have or any(s in have for s in scopes)
        if oauth is not None and raw:
            return await oauth.verify(raw) is not None
        return False

    return check
