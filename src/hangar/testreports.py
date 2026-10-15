"""Test and coverage reports, parsed (--- testresults; docs/jobs.md, "Test results").

The one parser for what test runners write: JUnit XML (pytest `--junitxml`, gotestsum, jest-junit and vitest's
`junit` reporter, cargo-nextest, Maven surefire and failsafe, Gradle), Cobertura XML and LCOV coverage. Every
report comes out of a sandbox, written by the repo's code, so it is untrusted input:

* XML goes through expat with defusedxml's rules: an entity declaration (internal or external, parsed or not) or
  an external entity reference stops the parse, so there is no XXE and no entity expansion, and no external DTD or
  parameter entity is ever read. A DOCTYPE alone is allowed (Cobertura writers emit one); nothing is fetched;
* the reader streams it and keeps no tree: a report is capped in bytes (MAX_BYTES), elements (MAX_ELEMENTS), depth
  (MAX_DEPTH) and test cases (MAX_CASES), and each message is clipped (MESSAGE_MAX) while it is read;
* text that isn't a report gives a Report with `error` set, never an exception.

Outcomes: `passed`, `failed`, `error` and `skipped`. A case the runner itself retried (surefire's and nextest's
`flakyFailure`/`flakyError` on a case that finally passed) is `passed` with `flaky` set; a failing case keeps its
first failure and ignores its `rerunFailure`s.

A case's id is `classname::name` (just `name` without a classname): stable across runs of one runner, which is all
the comparisons in testresults.py need. The same id twice in one run (a report written twice, two reports of one
suite) counts once, at its worst outcome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from xml.parsers import expat

MAX_BYTES = 8 * 1024 * 1024  # one report
MAX_ELEMENTS = 2_000_000
MAX_DEPTH = 64
MAX_CASES = 100_000
MESSAGE_MAX = 2000  # characters of a failure's message and text, each
ID_MAX = 500

Outcome = Literal["passed", "failed", "error", "skipped"]
_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
_SUITES = ("testsuites", "testsuite")
_FAILS = {"failure": "failed", "error": "error"}
_FLAKY = ("flakyFailure", "flakyError")
_LCOV = re.compile(rb"^(LF|LH|BRF|BRH):(\d+)\s*$", re.M)


class ReportError(ValueError):
    pass


@dataclass(slots=True)
class Case:
    id: str
    name: str
    classname: str = ""
    suite: str = ""
    outcome: Outcome = "passed"
    message: str = ""
    seconds: float | None = None
    flaky: bool = False  # the runner retried it and it passed


@dataclass(slots=True)
class Coverage:
    format: str  # cobertura | lcov
    lines_valid: int | None = None
    lines_covered: int | None = None
    branches_valid: int | None = None
    branches_covered: int | None = None
    rate: float | None = None  # line rate, 0..1, as the report gave it when it has no counts

    @property
    def line_rate(self) -> float | None:
        if self.lines_valid:
            return (self.lines_covered or 0) / self.lines_valid
        return self.rate


@dataclass(slots=True)
class Report:
    source: str
    kind: Literal["junit", "cobertura", "lcov", "unknown"] = "unknown"
    cases: list[Case] = field(default_factory=list)
    coverage: Coverage | None = None
    error: str | None = None
    truncated: bool = False  # over MAX_CASES: the cases are not all of them


# ====================================================================== safe XML
def _parser(start, end, text) -> expat.XMLParserType:  # noqa: ANN001 - expat's handler signatures
    p = expat.ParserCreate()
    p.buffer_text = True

    def no_entity(*_a: object) -> None:
        raise ReportError("the report declares an entity, which hangar doesn't read")

    def no_external(*_a: object) -> int:
        raise ReportError("the report references an external entity, which hangar doesn't read")

    p.EntityDeclHandler = no_entity
    p.UnparsedEntityDeclHandler = no_entity
    p.ExternalEntityRefHandler = no_external
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = start, end, text
    return p


def _feed(p: expat.XMLParserType, data: bytes) -> str | None:
    """Parse `data`; the reason it stopped early, if it did (a cap the handlers raised, or malformed XML)."""
    try:
        p.Parse(data, True)
    except ReportError as e:
        return str(e)
    except expat.ExpatError as e:
        return f"not well-formed XML ({expat.ErrorString(e.code)}, line {e.lineno})"
    except ValueError as e:  # --- review(#116): pyexpat refuses some declared encodings (UTF-7, Shift_JIS) this way
        return f"unreadable XML ({str(e)[:100]})"
    return None


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _int(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


# ====================================================================== JUnit
class _JUnit:
    """Streaming handlers: the cases of every <testcase>, wherever the <testsuite>s nest."""

    def __init__(self, report: Report):
        self.r = report
        self.depth = 0
        self.elements = 0
        self.root: str | None = None
        self.suites: list[str] = []
        self.case: Case | None = None
        self.case_depth = 0
        self.capture: list[str] | None = None  # text of the failure being read
        self.captured = 0
        self.rank = 0

    def start(self, name: str, attrs: dict[str, str]) -> None:
        self.depth += 1
        self.elements += 1
        if self.depth > MAX_DEPTH:
            raise ReportError(f"nested deeper than {MAX_DEPTH} elements")
        if self.elements > MAX_ELEMENTS:
            raise ReportError(f"more than {MAX_ELEMENTS:,} elements")
        if self.root is None:
            self.root = name
            if name not in _SUITES:
                raise ReportError(f"<{name[:40]}> is not a JUnit report")
        if name == "testsuite":
            self.suites.append(attrs.get("name", "")[:ID_MAX])
        elif name == "testcase" and self.case is None:
            if len(self.r.cases) >= MAX_CASES:
                self.r.truncated = True
                raise ReportError(f"more than {MAX_CASES:,} test cases; the rest weren't read")
            cls, nm = attrs.get("classname", "")[:ID_MAX], attrs.get("name", "")[:ID_MAX]
            self.case = Case(id=f"{cls}::{nm}" if cls else nm, name=nm, classname=cls,
                             suite=self.suites[-1] if self.suites else "", seconds=_float(attrs.get("time")))
            self.case_depth, self.rank = self.depth, 0
        elif self.case is not None and self.depth == self.case_depth + 1:
            if name in _FAILS and _RANK[_FAILS[name]] > self.rank:
                self.case.outcome = _FAILS[name]  # type: ignore[assignment]
                self.rank = _RANK[self.case.outcome]
                self.case.message = attrs.get("message", "")[:MESSAGE_MAX]
                self.capture, self.captured = [], 0
            elif name == "skipped" and self.rank == 0:
                self.case.outcome, self.rank = "skipped", 1
                self.case.message = attrs.get("message", "")[:MESSAGE_MAX]
            elif name in _FLAKY:
                self.case.flaky = True

    def text(self, data: str) -> None:
        if self.capture is not None and self.captured < MESSAGE_MAX:
            self.capture.append(data[: MESSAGE_MAX - self.captured])
            self.captured += len(data)

    def end(self, name: str) -> None:
        if self.case is not None and self.depth == self.case_depth + 1 and self.capture is not None \
                and name in _FAILS:
            body = "".join(self.capture).strip()
            if body:
                self.case.message = (self.case.message + "\n" + body).strip()[:MESSAGE_MAX]
            self.capture = None
        elif name == "testcase" and self.case is not None and self.depth == self.case_depth:
            if self.case.outcome != "passed":
                self.case.flaky = False  # it didn't pass in the end: a failure, not a flake
            self.r.cases.append(self.case)
            self.case = None
        elif name == "testsuite" and self.suites:
            self.suites.pop()
        self.depth -= 1


def parse_junit(data: bytes, source: str = "") -> Report:
    r = Report(source=source, kind="junit")
    if len(data) > MAX_BYTES:
        r.error = f"over {MAX_BYTES // (1024 * 1024)} MiB"
        return r
    h = _JUnit(r)
    if why := _feed(_parser(h.start, h.end, h.text), data):
        r.error = why
    if h.root not in _SUITES:
        r.kind = "unknown"
    return r


# ====================================================================== coverage
def parse_cobertura(data: bytes, source: str = "") -> Report:
    """The totals on Cobertura's root element (coverage.py's `coverage xml`, pytest-cov, jest's and istanbul's
    cobertura reporter): only the root is read."""
    r = Report(source=source, kind="cobertura")
    seen: dict[str, str] = {}

    def start(name: str, attrs: dict[str, str]) -> None:
        if name != "coverage":
            raise ReportError(f"<{name[:40]}> is not a Cobertura report")
        seen.update(attrs)
        raise _Done

    p = _parser(start, lambda _n: None, lambda _t: None)
    try:
        why = _feed(p, data[:MAX_BYTES])
    except _Done:
        why = None
    if why or not seen:
        r.kind, r.error = "unknown", why or "empty"
        return r
    r.coverage = Coverage(format="cobertura", lines_valid=_int(seen.get("lines-valid")),
                          lines_covered=_int(seen.get("lines-covered")),
                          branches_valid=_int(seen.get("branches-valid")),
                          branches_covered=_int(seen.get("branches-covered")), rate=_float(seen.get("line-rate")))
    return r


class _Done(Exception):
    pass


def parse_lcov(data: bytes, source: str = "") -> Report:
    """LCOV's per-file LF/LH (lines found and hit) and BRF/BRH, summed."""
    r = Report(source=source, kind="lcov")
    tot = {"LF": 0, "LH": 0, "BRF": 0, "BRH": 0}
    n = 0
    for m in _LCOV.finditer(data[:MAX_BYTES]):
        tot[m.group(1).decode()] += int(m.group(2))
        n += 1
    if not n:
        r.kind, r.error = "unknown", "no LF/LH records"
        return r
    r.coverage = Coverage(format="lcov", lines_valid=tot["LF"], lines_covered=tot["LH"],
                          branches_valid=tot["BRF"] or None, branches_covered=tot["BRH"] if tot["BRF"] else None)
    return r


# ====================================================================== any report
def parse(data: bytes, source: str = "") -> Report:
    """Whatever `data` is: LCOV by its records, else XML by its root element (JUnit or Cobertura)."""
    head = data[:4096].lstrip()
    if not head.startswith(b"<"):
        if b"end_of_record" in data[:MAX_BYTES] or _LCOV.search(data[:65536]):
            return parse_lcov(data, source)
        return Report(source=source, error="not a JUnit, Cobertura or LCOV report")
    if re.search(rb"<coverage[\s>]", head) and not re.search(rb"<testsuites?[\s>/]", head):
        return parse_cobertura(data, source)
    return parse_junit(data, source)


@dataclass(slots=True)
class Results:
    """Every report of one run of a check, merged."""

    cases: dict[str, Case] = field(default_factory=dict)
    coverage: Coverage | None = None
    sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    complete: bool = True  # every case of every report was read

    def counts(self) -> dict[str, int]:
        c = {"total": len(self.cases), "passed": 0, "failed": 0, "error": 0, "skipped": 0}
        for x in self.cases.values():
            c[x.outcome] += 1
        return c

    def failing(self) -> list[Case]:
        return [c for c in self.cases.values() if c.outcome in ("failed", "error")]


def merge(reports: list[Report]) -> Results:
    out = Results()
    covs: list[Coverage] = []
    for r in reports:
        if r.error:
            out.errors.append(f"{r.source or 'a report'}: {r.error}"[:300])
        if r.truncated or (r.error and r.kind == "junit"):
            out.complete = False
        if r.kind == "unknown":
            continue
        out.sources.append(r.source)
        for c in r.cases:
            have = out.cases.get(c.id)
            if have is None or _RANK[c.outcome] > _RANK[have.outcome]:
                if have is not None:
                    c.flaky = c.flaky or have.flaky
                out.cases[c.id] = c
            elif c.flaky:
                have.flaky = True
        if r.coverage:
            covs.append(r.coverage)
    if covs:
        out.coverage = _sum(covs)
    return out


def _sum(covs: list[Coverage]) -> Coverage:
    """Several coverage reports of one run (a monorepo's packages): their counts added. Without counts on every
    one, the first report's rate stands for all of them."""
    if all(c.lines_valid is not None for c in covs):
        def total(attr: str) -> int | None:
            vals = [getattr(c, attr) for c in covs]
            return sum(v for v in vals if v is not None) if any(v is not None for v in vals) else None

        fmt = covs[0].format if len({c.format for c in covs}) == 1 else "mixed"
        return Coverage(format=fmt, lines_valid=total("lines_valid"), lines_covered=total("lines_covered"),
                        branches_valid=total("branches_valid"), branches_covered=total("branches_covered"))
    return covs[0]
