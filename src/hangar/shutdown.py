"""Graceful shutdown (overnight/sweep): long-polls answer as soon as a shutdown signal arrives.

uvicorn drains open requests before it runs the app's shutdown, so a request that waits (MCP `agent_status` with
`wait_seconds`, `wait_for_turn`) held the pod's shutdown for as long as it waited. Hangar chains
its own handler in front of uvicorn's for SIGTERM and SIGINT: it sets `Fleet.closing`, every waiter returns what it
has now, and the client asks again (the new pod answers). Streams and terminals are cut after
HANGAR_SHUTDOWN_GRACE_SECONDS (uvicorn's timeout_graceful_shutdown, set by `hangar serve`).
"""

from __future__ import annotations

import asyncio
import signal
import threading
from collections.abc import Callable
from types import FrameType

SIGNALS = (signal.SIGTERM, signal.SIGINT)


def on_signal(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> Callable[[], None]:
    """Run `callback` on `loop` when a shutdown signal arrives, then whatever handled it before (uvicorn's
    handle_exit). Returns a function that puts the earlier handlers back. Signal handlers can only be set from the
    main thread; elsewhere (a test client's portal thread) this does nothing."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    before = {sig: signal.getsignal(sig) for sig in SIGNALS}

    def handler(sig: int, frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(callback)
        prev = before.get(sig)
        if callable(prev):
            prev(sig, frame)
        elif prev == signal.SIG_DFL:  # nothing else was listening: do what the signal would have done
            signal.signal(sig, signal.SIG_DFL)
            signal.raise_signal(sig)

    for sig in SIGNALS:
        signal.signal(sig, handler)

    def restore() -> None:
        for sig, prev in before.items():
            if signal.getsignal(sig) is handler and prev is not None:
                signal.signal(sig, prev)

    return restore
