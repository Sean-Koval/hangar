"""How hangar's server writes its log: one record, one entry (KAN-556).

Cloud Logging makes each line of the container's output an entry of its own, and the audit sink
(deploy/terraform/*/audit.tf) keeps the entries that start like an audit record. A message or traceback that
carries text from a sandbox (a command's stderr, an exception quoting it) could hold a newline and then a whole
line of its own that looks like one. So every line after a record's first starts with `| `: nothing a record
carries can start a line, and the sink's filter is anchored at the start of the line.

The container runtime splits a line longer than 16 KiB into entries too, so a line is cut into continued pieces of
at most PIECE bytes first: padding can't put a forged record at the start of an entry that way either.
"""

from __future__ import annotations

import logging
import re
import sys
import threading

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
CONTINUED = "| "  # starts every line after a record's first; never a digit, so never the start of an audit line
# What the audit sink keeps (the filter in deploy/terraform/gke/audit.tf and modules/hangar-workload/audit.tf): a
# line this format wrote for the `hangar.audit` logger. tests/test_audit_export.py keeps the two in step.
PIECE = 16_000  # bytes of UTF-8 in a line, below the runtime's 16 KiB, with room for CONTINUED and the newline
AUDIT_LINE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} INFO hangar[.]audit: [{]")


class OneEntry(logging.Formatter):
    """FORMAT, with every line after the first marked as a continuation (CONTINUED)."""

    def __init__(self) -> None:
        super().__init__(FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        lines = super().format(record).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        pieces = [p for line in lines for p in _cut(line)]
        return "\n".join([pieces[0], *(CONTINUED + p for p in pieces[1:])])


def _size(text: str) -> int:
    """Bytes `text` takes on stderr, as Python writes it there: UTF-8, a lone surrogate as `\\udXXX` (6 bytes)."""
    return len(text.encode("utf-8", "backslashreplace"))


def _cut(line: str) -> list[str]:
    """`line` in pieces of at most PIECE bytes as written (never splitting a character)."""
    if len(line) * 6 <= PIECE or _size(line) <= PIECE:
        return [line]
    out, start, size = [], 0, 0
    for i, ch in enumerate(line):
        n = _size(ch)
        if size + n > PIECE:
            out.append(line[start:i])
            start, size = i, 0
        size += n
    return [*out, line[start:]]


def configure(level: int = logging.INFO) -> None:
    """The server's logging: the root logger's one handler with OneEntry. uvicorn's own loggers propagate to it
    (`uvicorn.run(log_config=None)`), so their lines are written the same way."""
    logging.basicConfig(level=level, force=True)
    for h in logging.getLogger().handlers:
        h.setFormatter(OneEntry())
    # What would reach stderr past the handler, written the same way: warnings, and uncaught exceptions
    logging.captureWarnings(True)
    sys.excepthook = lambda t, v, tb: logging.getLogger("hangar").critical("uncaught exception", exc_info=(t, v, tb))
    threading.excepthook = _thread_exception
    sys.unraisablehook = lambda u: logging.getLogger("hangar").error(
        "unraisable exception: %s %r", u.err_msg or "exception ignored in", u.object,
        exc_info=(u.exc_type, u.exc_value, u.exc_traceback) if u.exc_value is not None else None)


def _thread_exception(a: threading.ExceptHookArgs) -> None:
    if a.exc_type is SystemExit:
        return
    logging.getLogger("hangar").error("uncaught exception in thread %s", a.thread.name if a.thread else "?",
                                      exc_info=(a.exc_type, a.exc_value, a.exc_traceback))  # type: ignore[arg-type]
