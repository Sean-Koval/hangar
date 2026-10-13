"""Event-loop plumbing for a responsive control plane (spec 0004 M1).

Provider SDKs block, so every exec, file transfer and status call runs in a worker thread, and a thread is held
for as long as the call takes (a clone, a verify, a turn waiting to end). The loop's default pool is far too small
for that, and FastAPI's `jsonable_encoder` walks every value of a big list response in Python on the event loop.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from hangar.config import Settings


def install_io_executor(settings: Settings) -> ThreadPoolExecutor:
    """Give the running loop a worker pool sized for blocking provider I/O (HANGAR_IO_THREADS)."""
    pool = ThreadPoolExecutor(max_workers=max(8, settings.io_pool), thread_name_prefix="hangar-io")
    asyncio.get_running_loop().set_default_executor(pool)
    return pool


class FastJSON(JSONResponse):
    """A JSON response that skips `jsonable_encoder` for content that is already JSON (model_dump(mode="json")
    views): about 10x less event-loop time for list routes. Anything json can't encode still goes through it."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
                          default=jsonable_encoder).encode("utf-8")
