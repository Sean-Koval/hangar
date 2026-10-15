"""Redaction of stored secret values in what hangar shows or keeps (--- egress: hardening B4).

`broker.redact` used to replace each stored value verbatim, so `printf %s "$X" | rev` (or `| base64`) printed a
secret straight past it. This also masks cheap, common encodings of each value that is long enough for an encoded
match to mean something (MIN_ENCODED characters):

* reversed;
* hex, lower and upper case (`xxd -p`, `od`);
* base64, standard and URL-safe, at each of the three alignments a value can have inside a longer encoded string
  (only the characters that depend on the value alone, so `base64 <<< "X=$SECRET"` is caught too);
* percent-encoded, when that differs.

Line-wrapped output (`base64` wraps at 76 columns, `xxd -p` at 60) is matched across the line breaks.

What it can't stop: any transformation that isn't on this list (gzip, an XOR, base32, splitting the value across
two commands, printing every other character, a second layer of encoding) and values hangar never stored. An agent
that can read a secret can always get it out in some form redaction doesn't know. Redaction keeps secrets out of
logs, transcripts and results by accident; it isn't a wall against a determined agent. Keeping the secret out of
the sandbox (the git gateway, short-lived cloud credentials, build secrets) is.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import quote

MIN_ENCODED = 12  # shorter values aren't worth the false positives their encodings would bring
MASK = "***"


def variants(secret: str) -> list[str]:
    """Encoded forms of `secret` that are long enough to be specific (at least MIN_ENCODED characters: review, a
    16-character floor dropped the reversed form of 12-15 character values and two base64 alignments of 12-13)."""
    b = secret.encode()
    out = [secret[::-1], b.hex(), b.hex().upper()]
    for k in range(3):  # the value starting k bytes into a 3-byte base64 group
        for enc in (base64.b64encode, base64.urlsafe_b64encode):
            e = enc(b"\0" * k + b).decode()
            first, last = -(-k // 3), (k + len(b)) // 3  # whole groups made of the value's bytes only
            out.append(e[4 * first:4 * last])
    for pct in (quote(secret, safe=""), quote(secret)):  # encodeURIComponent and quote()'s default (keeps "/")
        out += [pct, re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), pct)]  # and lower-case escapes
    return [v for v in dict.fromkeys(out) if len(v) >= MIN_ENCODED and v != secret]


class Redactor:
    """Masks a fixed set of secret values, verbatim and encoded, in text."""

    def __init__(self, secrets: list[str]):
        self.src = secrets  # the list it was built from (Broker rebuilds when its cache is replaced)
        self.plain = sorted(set(secrets), key=len, reverse=True)
        enc = {v for s in self.plain if len(s) >= MIN_ENCODED for v in variants(s)}
        self.encoded = sorted(enc - set(self.plain), key=len, reverse=True)

    def text(self, s: str) -> str:
        for sec in self.plain:
            if sec in s:
                s = s.replace(sec, MASK)
        for v in self.encoded:
            if v in s:
                s = s.replace(v, MASK)
        if "\n" in s and self.encoded:
            s = self._wrapped(s)
        return s

    def _wrapped(self, s: str) -> str:
        """Encoded values broken across lines: found in the text without its line breaks, masked in the original."""
        flat = s.replace("\r", "").replace("\n", "")
        found = [v for v in self.encoded if v in flat]
        if not found:
            return s
        pos = [i for i, c in enumerate(s) if c not in "\r\n"]  # flat index -> original index
        spans = []
        for v in found:
            i = flat.find(v)
            while i != -1:
                spans.append((pos[i], pos[i + len(v) - 1] + 1))
                i = flat.find(v, i + len(v))
        out, cur = [], 0
        for start, end in sorted(spans):
            if end <= cur:
                continue
            out += [s[cur:max(cur, start)], MASK]
            cur = end
        out.append(s[cur:])
        return "".join(out)
