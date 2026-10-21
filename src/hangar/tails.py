"""Tail numbers (KAN-114): the short ID a person reads, derived from the real one (src/hangar/ui/tails.js is the
console's copy; tests/test_tails.py holds the two equal).

Airframe's form: the product prefix, a kind segment, a four-character code, uppercase and hyphenated. A sandbox is
`HGR-7Q2K`, a job `HGR-JOB-4F7A`, a session `HGR-SES-…`, an approval `HGR-APR-…`, a snapshot `HGR-SNAP-…`, an
environment request `HGR-REQ-…`. The code is Crockford base32 of a 53-bit hash (cyrb53) of the whole real ID.

Display only: the API, the store, URLs, commands and trailers keep the real ID. Text the server writes for a person
(a Slack message, the PR's footer, the review packet) leads with the tail and keeps the real ID beside it, since a
message has no hover and a person may paste the ID into the CLI.
"""

from __future__ import annotations

import re

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PRODUCT = "HGR"
KINDS = {"sb": "", "job": "JOB", "ses": "SES", "apr": "APR", "ap": "APR", "snap": "SNAP", "rq": "REQ", "run": "RUN",
         "bld": "BLD", "tok": "TOK"}
MIN, MAX = 4, 10
_RAW = re.compile(r"^(sb|job|ses|apr|ap|snap|rq|run|bld|tok)-[0-9a-f]{4,32}$")
_M32 = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    return (a * b) & _M32


def hash53(s: str) -> int:
    """cyrb53 over the string's UTF-16 code units, as JavaScript's charCodeAt reads them."""
    h1, h2 = 0xDEADBEEF, 0x41C6CE57
    units = s.encode("utf-16-le")
    for i in range(0, len(units), 2):
        c = units[i] | units[i + 1] << 8
        h1 = _imul(h1 ^ c, 2654435761)
        h2 = _imul(h2 ^ c, 1597334677)
    h1 = _imul(h1 ^ (h1 >> 16), 2246822507) ^ _imul(h2 ^ (h2 >> 13), 3266489909)
    h2 = _imul(h2 ^ (h2 >> 16), 2246822507) ^ _imul(h1 ^ (h1 >> 13), 3266489909)
    return 4294967296 * (2097151 & h2) + (h1 & _M32)


def code(identifier: str, n: int = MIN) -> str:
    h = hash53(identifier)
    return "".join(ALPHABET[(h >> (45 - 5 * i)) & 31] for i in range(min(n, MAX)))


def is_raw(identifier: str | None) -> bool:
    return bool(identifier and _RAW.match(identifier))


def tail(identifier: str | None) -> str:
    """sb-8e7f50c8 -> HGR-8AS5, job-e0123d68 -> HGR-JOB-VDTR; anything else comes back as it was."""
    m = _RAW.match(identifier or "")
    if not m:
        return identifier or ""
    kind = KINDS[m.group(1)]
    return f"{PRODUCT}-{kind + '-' if kind else ''}{code(identifier or '')}"


def label(identifier: str | None) -> str:
    """For Markdown and Slack text: the tail, then the real ID in code (`HGR-JOB-VDTR (`job-e0123d68`)`)."""
    return f"{tail(identifier)} (`{identifier}`)" if is_raw(identifier) else f"`{identifier or ''}`"
