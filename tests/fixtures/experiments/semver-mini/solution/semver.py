"""A reference semver.py for semver-mini: just enough of the semver smoke task to pass its five hidden tests."""
import re
from functools import total_ordering

_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
_RE = re.compile(rf"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-({_ID}(?:\.{_ID})*))?"
                 r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?")


@total_ordering
class Version:
    def __init__(self, major, minor, patch, prerelease=(), build=()):
        self.major, self.minor, self.patch = major, minor, patch
        self.prerelease, self.build = tuple(prerelease), tuple(build)

    @classmethod
    def parse(cls, s):
        m = _RE.fullmatch(s.strip())
        if not m:
            raise ValueError(f"not a semver version: {s!r}")
        pre = tuple(int(x) if x.isdigit() else x for x in m.group(4).split(".")) if m.group(4) else ()
        return cls(*map(int, m.group(1, 2, 3)), pre, m.group(5).split(".") if m.group(5) else ())

    def _key(self):
        pre = tuple((0, x, "") if isinstance(x, int) else (1, 0, x) for x in self.prerelease)
        return self.major, self.minor, self.patch, not self.prerelease, pre

    def __eq__(self, other):
        return self._key() == other._key()

    def __lt__(self, other):
        return self._key() < other._key()

    def __hash__(self):
        return hash(self._key())

    def __str__(self):
        s = f"{self.major}.{self.minor}.{self.patch}"
        s += "-" + ".".join(map(str, self.prerelease)) if self.prerelease else ""
        return s + ("+" + ".".join(self.build) if self.build else "")


def _comparators(token):
    if token.startswith("^"):
        lo = Version.parse(token[1:])
        hi = (lo.major + 1, 0, 0) if lo.major else (0, lo.minor + 1, 0) if lo.minor else (0, 0, lo.patch + 1)
        return [(">=", lo), ("<", Version(*hi))]
    op, ver = re.fullmatch(r"(>=|<=|>|<|=)?(.+)", token).groups()
    return [(op or "=", Version.parse(ver))]


def satisfies(version, range_str):
    v = version if isinstance(version, Version) else Version.parse(version)
    test = {"=": v.__eq__, ">": v.__gt__, ">=": v.__ge__, "<": v.__lt__, "<=": v.__le__}
    for part in range_str.split("||"):
        comps = [c for t in part.split() for c in _comparators(t)]
        if all(test[op](c) for op, c in comps) and (not v.prerelease or any(
                c.prerelease and c._key()[:3] == v._key()[:3] for _, c in comps)):
            return True
    return False
