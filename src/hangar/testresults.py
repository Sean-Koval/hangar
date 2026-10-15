"""Structured results of every check hangar runs (--- testresults; docs/jobs.md, "Test results").

`verify` used to come back as an exit code and the end of its output. Now every run of it that hangar makes, or
that the in-turn gate makes for the agent, leaves its test reports (testreports.py parses them) as a TestRun:

* **Where the reports come from.** Around the check, hangar sets the reporter variables the common runners read
  (`PYTEST_ADDOPTS=--junitxml=…`, `GOTESTSUM_JUNITFILE`, `JEST_JUNIT_OUTPUT_DIR`) to a fresh directory, then reads
  what the run wrote there, what the repo declares (`verify_reports:` in `.hangar/environment.yaml`, else in the
  repo policy) and the usual report paths (surefire and failsafe, Gradle, nextest, `*junit*.xml`, Cobertura and
  LCOV coverage). Only files the run wrote count: older than the run's start, a report is ignored.
  HANGAR_TEST_REPORTS=collect reads without setting the variables; off does neither. The check itself, its
  output and its exit code are unchanged: the variables are prepended to the same shell, in the same exec.
* **Which runs.** The check on the base commit (baseline.py), the check after each turn (and its flaky rerun),
  best-of-N attempts, agent-led setup's validation (autoinstall.py), the in-turn gate (advisory: the agent's own
  sandbox wrote it while the agent ran, so it never feeds the flaky list), and the deploy acceptance checks
  (`POST /api/admin/deploys`).
* **What is kept.** One TestRun per run: counts, the first FAILING_KEPT failures with their messages (redacted),
  every case's outcome keyed by test id (compressed; dropped after CASES_DAYS, a base's after RETENTION_DAYS), the
  coverage totals and the commit and tree it ran on. Runs go after RETENTION_DAYS.
* **Red bases, exactly.** When the base check and the check after the turn both left complete reports, the new
  failures are the failing test ids minus the base's, not a guess from the output's text (baseline.py keeps that
  for checks without reports). A run that failed with no failing test (a build error), or from which tests the base
  ran have gone missing, can't be compared that way.
* **Flaky tests.** A test is flaky when it passed and failed on the same tree (the working tree's content, so
  uncommitted changes count) in the same repo and environment, or the runner itself passed it on a retry
  (surefire's and nextest's `flakyFailure`). Each repo keeps a list (FLAKY_DAYS since last seen). With the repo
  policy's `rerun_flaky`, a check whose every failure (on a red base: every new failure) is on that list runs once
  more, and the rerun decides.

Where it shows: `result.tests` on the job (get_job, wait_for_turn) and on each attempt, a `progress` event in the
job's and session's events, the review packet, `GET /api/jobs/{id}/tests` and `/api/sessions/{ref}/tests`
(`?format=otel`: the `test.run` and `test.case` span events of the trace export, docs/api/traces.md, with OTel's
`test.case.name`/`test.case.result.status`), `hangar://jobs/<id>/tests`,
`hangar status` (the repos with the most flaky tests), and a file in the job's directory the agent reads
(`TEST-RESULTS.json`) instead of scrolling the output. The Cloud Doctor counts a failed check whose every failure
is on the flaky list as transient.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import secrets
import shlex
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from hangar import testreports
from hangar.models import ExecResult, TestSummary, now

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job, Turn

log = logging.getLogger("hangar.testresults")

RETENTION_DAYS = 14
CASES_DAYS = 3  # a run's every case: what the same-tree comparison reads, mostly within hours
FLAKY_DAYS = 30
FLAKY_MAX = 500  # tests on one repo's list
PRUNE_EVERY = 600.0
FAILING_KEPT = 200
MESSAGE_KEPT = 1000
CASES_KEPT = 20_000
PRIOR_RUNS = 5  # earlier runs on the same tree a run is compared with
FILES_MAX = 64
TOTAL_BYTES = 32 * 1024 * 1024
AGENT_FAILURES = 2000
OTEL_CASES = 100
GLOBS_MAX = 16
IN_TURN_MAX = 10
KV_FLAKY = "testresults:flaky:"
FILE = "TEST-RESULTS.json"
ACCEPTANCE_REPO = "hangar:acceptance"  # the deploy checks' runs: not a repo agents work in
TRUSTED = frozenset({"base", "verify", "rerun", "attempt", "autoinstall", "acceptance"})
# The usual report paths, as `find -path` patterns (where * also crosses /), relative to the checkout
DEFAULT_GLOBS = ("*/TEST-*.xml",  # Maven surefire and failsafe, Gradle, Ant
                 "*/*junit*.xml",  # nextest (target/nextest/<profile>/junit.xml), jest-junit, vitest, gotestsum
                 "*/test-results/*.xml", "*/test-reports/*.xml",
                 "*/coverage.xml", "*/cobertura*.xml", "*/lcov.info", "*/*.lcov")  # coverage.py, istanbul, c8
_PRUNE_DIRS = (".git", "node_modules", ".venv", "venv", ".tox", "__pycache__", ".hangar", ".gradle", ".m2",
               ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache", "vendor", "bower_components")
_PRUNE_PATHS = ("*/target/debug", "*/target/release", "*/target/doc")  # cargo's build output: many files, no reports
# One summary line per run of each runner whose report hangar names: two of them, and the file holds the last only
_SUMMARIES = {"pytest.xml": re.compile(r"^=*\s*(?:\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)"
                                       r"|no tests ran)\b.* in \d+(?:\.\d+)?s\b", re.M),
              "gotestsum.xml": re.compile(r"^DONE \d+ tests?\b", re.M),
              "junit.xml": re.compile(r"^Tests:\s+\d+", re.M)}
_CODE = {"passed": "p", "failed": "f", "error": "e", "skipped": "s"}
_OUTCOME = {v: k for k, v in _CODE.items()} | {"r": "passed"}  # r: passed on the runner's own retry
_BAD_GLOB = re.compile(r"[\x00-\x1f]")


def run_id() -> str:
    """Longer than new_id's: a busy team makes thousands of runs a day, kept RETENTION_DAYS."""
    return f"tr-{secrets.token_hex(8)}"


class TestRun(BaseModel):
    """One run of a check, as its reports had it."""

    __test__ = False  # not a pytest class
    id: str = Field(default_factory=run_id)
    at: float = Field(default_factory=now)
    kind: str  # base | verify | rerun | attempt | in_turn | autoinstall | acceptance
    trusted: bool = True  # hangar ran it; in_turn: the agent's sandbox did, while the agent worked
    repo: str | None = None  # repo_key
    environment: str | None = None
    job_id: str | None = None
    session_id: str | None = None
    turn: int | None = None
    attempt: int | None = None
    person: str | None = None
    sha: str | None = None  # HEAD when it ran
    tree: str | None = None  # the working tree's content (uncommitted changes included)
    command: str | None = None
    exit_code: int | None = None
    complete: bool = True  # every case of every report was read
    counts: dict[str, int] = Field(default_factory=dict)  # total, passed, failed, error, skipped
    n_failing: int = 0
    failing: list[dict[str, str]] = Field(default_factory=list)  # [{id, outcome, message}], the first FAILING_KEPT
    flaky: list[str] = Field(default_factory=list)  # tests this run showed to be flaky
    cases: str | None = None  # every case's outcome by id: zlib'd JSON, base64
    cases_complete: bool = False  # `cases` holds every case
    coverage: dict[str, Any] | None = None  # {format, line_rate, lines_valid, lines_covered, ...}
    reports: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)  # reports that couldn't be read, and why


# ====================================================================== in the sandbox
def check_globs(v: list[str] | None) -> list[str] | None:
    """`verify_reports`: paths or globs relative to the checkout (`**/` for any depth), checked like lockfiles."""
    if v is None:
        return None
    if len(v) > GLOBS_MAX:
        raise ValueError(f"at most {GLOBS_MAX} report paths")
    for p in v:
        if not p or len(p) > 200 or p.startswith(("/", "-", "~")) or ".." in p.split("/") or _BAD_GLOB.search(p):
            raise ValueError(f"report path '{p[:40]}' must be a path or glob relative to the repo, without '..'")
    return list(dict.fromkeys(v))


def _find_pattern(glob: str) -> str:
    """A declared glob as a `find -path` pattern: `**/x` any depth (the root too), else from the checkout's root."""
    g = re.sub(r"\*\*+", "*", glob.removeprefix("./"))
    return g if g.startswith("*/") else "./" + g


def prefix(dir_expr: str, repo: str | None = None, inject: bool = True, alone: bool = False) -> str:
    """Shell that goes before the check, in the same shell: a fresh report directory (`dir_expr`, a shell word),
    its start mark, the tree id (with `repo`: `git write-tree` of the working tree through a scratch index, so the
    real index is untouched) and, with `inject`, the reporter variables. With `alone`, the earlier runs' directories
    next to it go (hangar runs one check at a time in a sandbox). Nothing it does reaches the output."""
    q = shlex.quote
    parts = [f'_ht={dir_expr}']
    if alone:
        parts.append('for _hx in "${_ht%/*}"/tr-*; do [ "$_hx" = "$_ht" ] || rm -rf "$_hx"; done 2>/dev/null')
    parts.append('mkdir -p "$_ht" && : > "$_ht/.start"')
    if repo:
        parts.append(f'(cd {q(repo)} && _hi="$_ht/.idx" && {{ cp "$(git rev-parse --git-path index)" "$_hi"; '
                     'GIT_INDEX_FILE="$_hi" git add -A && GIT_INDEX_FILE="$_hi" git write-tree > "$_ht/.tree"; '
                     'rm -f "$_hi"; }) > /dev/null 2>&1')
    parts.append('export HANGAR_TESTS_DIR="$_ht"')
    if inject:
        parts.append('case "${PYTEST_ADDOPTS:-}" in *junit*) ;; '
                     '*) export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:+$PYTEST_ADDOPTS }--junitxml=$_ht/pytest.xml";; esac; '
                     'export GOTESTSUM_JUNITFILE="${GOTESTSUM_JUNITFILE:-$_ht/gotestsum.xml}" '
                     'JEST_JUNIT_OUTPUT_DIR="${JEST_JUNIT_OUTPUT_DIR:-$_ht}"')
    parts.append("unset _ht _hi _hx")
    return "; ".join(parts) + "\n"


def gather_script(d: str, repo: str | None, globs: list[str], discover: bool = True) -> str:
    """One exec, as the sandbox's user: the tree id and HEAD, then the report files the run wrote (newer than its
    start mark; regular files, not links): `d:./name` in its directory and, with `discover`, `r:./path` in the
    checkout. Relative, so hangar joins them to the paths it knows."""
    q = shlex.quote
    lines = [f'_ht={q(d)}; [ -f "$_ht/.start" ] || exit 3',
             'printf "tree:%s\\n" "$(head -c 80 "$_ht/.tree" 2>/dev/null)"',
             (f'printf "head:%s\\n" "$(cd {q(repo)} 2>/dev/null && git rev-parse HEAD 2>/dev/null)"' if repo
              else 'echo head:'),
             f'(cd "$_ht" && find . -maxdepth 1 -type f -name "*.xml" -newer .start 2>/dev/null | head -n {FILES_MAX}'
             ') | sed "s/^/d:/"']
    if discover and repo:
        pats = [*DEFAULT_GLOBS, *(_find_pattern(g) for g in globs)]
        prune = " -o ".join([*(f"-name {q(n)}" for n in _PRUNE_DIRS), *(f"-path {q(x)}" for x in _PRUNE_PATHS)])
        match = " -o ".join(f"-path {q(p)}" for p in dict.fromkeys(pats))
        lines.append(f"(cd {q(repo)} 2>/dev/null && find . -maxdepth 8 \\( {prune} \\) -prune -o -type f "
                     f'-newer "$_ht/.start" \\( {match} \\) -print 2>/dev/null | head -n {FILES_MAX}) | sed "s/^/r:/"')
    return "\n".join(lines) + "\n"


@dataclass
class Collected:
    tree: str | None = None
    head: str | None = None
    results: testreports.Results | None = None
    files: list[str] | None = None


Exec = Callable[[str, int], Awaitable[ExecResult]]
Read = Callable[[str, int], Awaitable[bytes | None]]


async def gather(exec_fn: Exec, read_fn: Read, d: str, repo: str | None, globs: list[str],
                 discover: bool = True) -> Collected:
    res = await exec_fn(gather_script(d, repo, globs, discover), 60)
    if res.exit_code != 0:
        return Collected()
    out = Collected()
    paths: list[str] = []
    for ln in (res.stdout or "").splitlines():
        if ln.startswith("tree:"):
            out.tree = ln[5:].strip() if re.fullmatch(r"[0-9a-f]{40,64}", ln[5:].strip()) else None
        elif ln.startswith("head:"):
            out.head = ln[5:].strip() if re.fullmatch(r"[0-9a-f]{40,64}", ln[5:].strip()) else None
        elif ln.startswith(("d:./", "r:./")) and not _BAD_GLOB.search(ln) and "/../" not in ln:
            if ln[0] == "r" and not repo:
                continue
            p = f"{d if ln[0] == 'd' else repo}/{ln[4:]}"
            if p not in paths:
                paths.append(p)
    read: list[tuple[str, bytes]] = []
    unreadable: list[testreports.Report] = []
    total = 0
    for p in paths[:FILES_MAX]:
        if total >= TOTAL_BYTES:
            unreadable.append(testreports.Report(source=p, error="over the reports' total size; not read"))
            continue
        name = p[len(d) + 1:] if p.startswith(d + "/") else \
            p[len(repo) + 1:] if repo and p.startswith(repo + "/") else p
        try:
            data = await read_fn(p, testreports.MAX_BYTES + 1)
        except Exception as e:  # noqa: BLE001 - one unreadable report leaves the others
            unreadable.append(testreports.Report(source=name, error=f"couldn't be read: {type(e).__name__}"))
            continue
        if data is not None:
            total += len(data)
            read.append((name, data))
    out.files = [n for n, _ in read]
    if read or unreadable:
        def parse() -> testreports.Results:
            return testreports.merge([testreports.parse(data, name) for name, data in read] + unreadable)

        out.results = await asyncio.to_thread(parse)
    return out


async def execute(exec_fn: Exec, read_fn: Read, command: str, *, d: str, repo: str | None, timeout: int,
                  globs: list[str], inject: bool = True, discover: bool = True) -> tuple[ExecResult, Collected]:
    """Run `command` with the report directory `d` set up, then collect. The run's own failure propagates (as an
    exec always did); collecting never fails the check."""
    res = await exec_fn(prefix(shlex.quote(d), repo, inject, alone=True) + command, timeout)
    try:
        got = await gather(exec_fn, read_fn, d, repo, globs, discover)
    except Exception as e:  # noqa: BLE001 - the check ran; its reports are a bonus
        log.info("collecting test reports from %s failed: %s", d, e)
        got = Collected()
    if got.results is not None:
        _overwritten(got, (res.stdout or "") + (res.stderr or ""))
    return res, got


def _overwritten(got: Collected, output: str) -> None:
    """A check that runs pytest twice (`pytest a; pytest b`) leaves one report, the last run's: whatever failed in
    the first is missing from it. Two summary lines from a runner whose report file hangar named, and the results
    aren't complete (no exact subtraction, no flaky verdicts from them)."""
    assert got.results is not None
    for name, rx in _SUMMARIES.items():
        if name in (got.files or []) and len(rx.findall(output)) > 1:  # the file hangar named, in its directory
            got.results.complete = False
            got.results.errors.append(f"{name}: the check ran its runner more than once, and the report holds the "
                                      "last run only; write one report per run (verify_reports)")


# ====================================================================== recording
def _encode(results: testreports.Results) -> tuple[str, bool]:
    """Failing first, so a capped run still holds every failure it can."""
    order = sorted(results.cases.values(), key=lambda c: (c.outcome == "passed", c.outcome == "skipped", c.id))
    kept = order[:CASES_KEPT]
    raw = json.dumps({c.id: ("r" if c.flaky and c.outcome == "passed" else _CODE[c.outcome]) for c in kept},
                     separators=(",", ":"), sort_keys=True)
    return base64.b64encode(zlib.compress(raw.encode(), 6)).decode(), len(kept) == len(order)


def decode(run: TestRun) -> dict[str, str] | None:
    """{test id: outcome} of every case kept, or None when they were dropped."""
    if not run.cases:
        return None
    try:
        raw = json.loads(zlib.decompress(base64.b64decode(run.cases)))
    except (ValueError, zlib.error):
        return None
    return {k: _OUTCOME.get(v, "passed") for k, v in raw.items()}


def failing_ids(run: TestRun) -> set[str] | None:
    """Every failing test id of the run, or None when it doesn't have all of them."""
    if not run.complete:
        return None
    if run.n_failing <= len(run.failing):
        return {f["id"] for f in run.failing}
    cases = decode(run)
    if cases is None:
        return None
    ids = {k for k, v in cases.items() if v in ("failed", "error")}
    return ids if len(ids) == run.n_failing else None


def record(store: Any, *, results: testreports.Results, kind: str, redact: Callable[[str], str] = lambda s: s,
           tree: str | None = None, sha: str | None = None, repo: str | None = None, environment: str | None = None,
           job_id: str | None = None, session_id: str | None = None, turn: int | None = None,
           attempt: int | None = None, person: str | None = None, command: str | None = None,
           exit_code: int | None = None, files: list[str] | None = None, run_id: str | None = None) -> TestRun:
    from hangar.repopolicy import repo_key

    _redact_ids(results, redact)
    counts = results.counts()
    failing = sorted(results.failing(), key=lambda c: c.id)
    run = TestRun(kind=kind, trusted=kind in TRUSTED, repo=repo if repo == ACCEPTANCE_REPO else repo_key(repo),
                  environment=environment, job_id=job_id, session_id=session_id, turn=turn, attempt=attempt,
                  person=person, sha=sha, tree=tree, command=redact(command)[:500] if command else None,
                  exit_code=exit_code, complete=results.complete, counts=counts, n_failing=len(failing),
                  failing=[{"id": c.id, "outcome": c.outcome, "message": redact(c.message)[:MESSAGE_KEPT]}
                           for c in failing[:FAILING_KEPT]],
                  coverage=_coverage(results.coverage), reports=(files or results.sources)[:FILES_MAX],
                  errors=results.errors[:20])
    if run_id:
        run.id = run_id
    if run.trusted:
        run.cases, run.cases_complete = _encode(results)
    found = _detect_flaky(store, run, results) if run.trusted else {}
    run.flaky = sorted(found)[:FAILING_KEPT]
    store.put(run)
    if found and run.repo:
        _note_flaky(store, run, found)
    _maybe_prune(store)
    return run


def _redact_ids(results: testreports.Results, redact: Callable[[str], str]) -> None:
    """Test ids pass through the broker too (a parametrized id can carry a value), all of them in one call, so a
    run's ids stay comparable with every other run's."""
    ids = list(results.cases)
    if not ids:
        return
    red = redact("\n".join(ids)).split("\n")
    if len(red) != len(ids) or red == ids:
        return
    cases: dict[str, testreports.Case] = {}
    for new, c in zip(red, results.cases.values(), strict=True):
        c.id = new
        cases[new] = c
    results.cases = cases


def _coverage(c: testreports.Coverage | None) -> dict[str, Any] | None:
    if c is None:
        return None
    rate = c.line_rate
    return {"format": c.format, "line_rate": round(rate, 6) if rate is not None else None,
            "lines_valid": c.lines_valid, "lines_covered": c.lines_covered, "branches_valid": c.branches_valid,
            "branches_covered": c.branches_covered}


def _detect_flaky(store: Any, run: TestRun, results: testreports.Results) -> dict[str, str]:
    """{test id: how}: passed on the runner's retry here, or passed here and failed on the same tree before (or
    the other way round), in the same repo and environment."""
    found = {c.id: "runner" for c in results.cases.values() if c.flaky and c.outcome == "passed"}
    if not run.tree or not run.repo:
        return found
    prior = [p for p in store.list_where(TestRun, tree=run.tree) if p.repo == run.repo and p.trusted
             and p.environment == run.environment and p.id != run.id and p.cases]
    how = "rerun" if run.kind == "rerun" else "same_tree"
    for p in sorted(prior, key=lambda p: p.at)[-PRIOR_RUNS:]:
        before = decode(p) or {}
        for cid, c in results.cases.items():
            o = before.get(cid)
            if o is None or o == "skipped" or c.outcome == "skipped":
                continue
            if (o == "passed") != (c.outcome == "passed"):
                found.setdefault(cid, how)
    return found


def _note_flaky(store: Any, run: TestRun, found: dict[str, str]) -> None:
    """Into the repo's list. One read-modify-write with no await in between: the event loop serializes them."""
    key = KV_FLAKY + (run.repo or "")
    data = _flaky_data(store, run.repo)
    t = now()
    for cid, how in found.items():
        e = data.get(cid) or {"first": t, "n": 0}
        e.update(n=int(e.get("n", 0)) + 1, last=t, how=how, job=run.job_id, environment=run.environment,
                 run=run.id)
        data[cid] = e
    if len(data) > FLAKY_MAX:
        data = dict(sorted(data.items(), key=lambda kv: -kv[1].get("last", 0))[:FLAKY_MAX])
    store.kv_set_json(key, data)


def _flaky_data(store: Any, repo: str | None) -> dict[str, dict[str, Any]]:
    if not repo:
        return {}
    got = store.kv_get_json(KV_FLAKY + repo, None)
    if not isinstance(got, dict):
        return {}
    cut = now() - FLAKY_DAYS * 86400
    return {k: v for k, v in got.items() if isinstance(v, dict) and float(v.get("last", 0)) > cut}


def flaky(store: Any, repo: str | None) -> dict[str, dict[str, Any]]:
    """The repo's flaky list: {test id: {n, first, last, how, job, environment, run}}."""
    from hangar.repopolicy import repo_key

    return _flaky_data(store, repo_key(repo) if repo and repo != ACCEPTANCE_REPO else repo)


_pruned = 0.0


def _maybe_prune(store: Any) -> None:
    global _pruned
    t = now()
    if t - _pruned < PRUNE_EVERY:
        return
    _pruned = t
    prune(store, t)


def prune(store: Any, t: float | None = None) -> int:
    """Runs past RETENTION_DAYS go; past CASES_DAYS they keep their counts and failures, not every case (a red
    base's stay whole: jobs on it subtract from them while its baseline is cached)."""
    t = t or now()
    old = store.list_range(TestRun, "at", 0.0, hi=t - RETENTION_DAYS * 86400, limit=2000)
    for r in old:
        store.delete(TestRun, r.id)
    for r in store.list_range(TestRun, "at", 0.0, hi=t - CASES_DAYS * 86400, limit=2000):
        if r.cases and r.kind != "base":
            r.cases = None
            store.put(r)
    return len(old)


# ====================================================================== jobs
def mode(settings: Any) -> str:
    return getattr(settings, "test_reports", "inject")


def declared(svc: JobService, job: Job) -> list[str]:
    """The repo's `verify_reports`: its .hangar/environment.yaml as the job's environment last saw it, else its
    policy."""
    from hangar import envbuilds

    with contextlib.suppress(Exception):
        cfg = envbuilds.of(svc.fleet).seen_config(job.environment, job.repo) if svc.settings.env_builds else None
        if cfg is not None and cfg.verify_reports:
            return list(cfg.verify_reports)
    p = svc.policies.get(job.repo) if job.repo else None
    return list(p.verify_reports or []) if p is not None else []


def _dir(settings: Any, rid: str) -> str:
    return f"/home/{settings.sandbox_user}/.hangar/tests/{rid}"


async def run_check(svc: JobService, job: Job, kind: str, *, sandbox_id: str | None = None,
                    session_id: str | None = None, turn: int | None = None,
                    attempt: int | None = None) -> tuple[ExecResult, TestRun | None]:
    """The job's `verify` in its sandbox (or an attempt's), as hangar has always run it, and its results."""
    sbid = sandbox_id or job.sandbox_id or ""
    timeout = svc.settings.job_verify_timeout_seconds
    actor = f"job:{job.id}"
    m = mode(svc.settings)
    if m == "off":
        return await svc.fleet.exec(sbid, job.verify or "", timeout=timeout, actor=actor), None
    repo = svc.fleet.get(sbid).info.get("repo_path")
    rid = run_id()

    async def ex(cmd: str, t: int) -> ExecResult:
        return await svc.fleet.exec(sbid, cmd, timeout=t, actor=actor)

    async def rd(path: str, limit: int) -> bytes | None:
        return await svc.fleet.read_file(sbid, path, limit=limit)

    res, got = await execute(ex, rd, job.verify or "", d=_dir(svc.settings, rid), repo=repo, timeout=timeout,
                             globs=declared(svc, job), inject=m == "inject")
    if got.results is None or not (got.results.cases or got.results.coverage):
        if got.results is not None and got.results.errors:
            svc._event(job, "the check's reports couldn't be read: " + "; ".join(got.results.errors[:3]))
        return res, None
    run = record(svc.store, results=got.results, kind=kind, redact=svc.broker.redact, tree=got.tree, sha=got.head,
                 repo=job.repo, environment=job.environment, job_id=job.id,
                 session_id=session_id or job.session_id, turn=turn, attempt=attempt, person=job.person,
                 command=job.verify, exit_code=res.exit_code, files=got.files, run_id=rid)
    export_otel(run)
    return res, run


def structured_new(store: Any, job: Job, run: TestRun | None) -> list[str] | None:
    """The failures `run` adds to a red base, by test id; None when the base was green or the two can't be
    compared that way (either side without complete reports, a failure outside the tests, tests gone missing)."""
    from hangar import baseline

    b = baseline.current(job)
    if run is None or b is None or not b.red or not b.tests or b.tests == run.id:
        return None
    base = store.get(TestRun, b.tests)
    if base is None or (base.exit_code not in (0, None) and not base.n_failing) or \
            (run.exit_code not in (0, None) and not run.n_failing):
        return None
    after, before = failing_ids(run), failing_ids(base)
    if after is None or before is None:
        return None
    was, now_ = decode(base), decode(run)
    if was is not None and now_ is not None and base.cases_complete and run.cases_complete:
        gone = len(set(was) - set(now_))
        if gone > max(5, len(was) // 50):  # a broken build can hide failures by not running their tests
            return None
    return sorted(after - before)


def new_failures(svc: JobService, job: Job, run: TestRun | None, output: str, truncated: bool) -> list[str] | None:
    """What JobResult.new_failures holds: by test id where both sides have reports, else from the output's text."""
    from hangar import baseline

    if run is not None and not baseline.stops_early(job.verify, output):
        new = structured_new(svc.store, job, run)
        if new is not None:
            return new
    return baseline.new_failures(job, output, truncated)


def summary(store: Any, run: TestRun, job: Job | None = None, file: str | None = None) -> TestSummary:
    from hangar import baseline

    c = run.counts
    on_list = set(flaky(store, run.repo)) if run.repo else set()
    ids = [f["id"] for f in run.failing]
    fl = sorted(set(run.flaky) | {i for i in ids if i in on_list})
    cov = (run.coverage or {}).get("line_rate")
    delta = None
    if job is not None and cov is not None and (b := baseline.current(job)) is not None and b.tests \
            and b.tests != run.id and (base := store.get(TestRun, b.tests)) is not None:
        was = (base.coverage or {}).get("line_rate")
        delta = round((cov - was) * 100, 2) if was is not None else None
    new = structured_new(store, job, run) if job is not None else None
    return TestSummary(run=run.id, kind=run.kind, at=run.at, exit_code=run.exit_code, total=c.get("total", 0),
                       passed=c.get("passed", 0), failed=c.get("failed", 0), errors=c.get("error", 0),
                       skipped=c.get("skipped", 0), complete=run.complete, failing=ids[:20],
                       new_failures=new[:50] if new is not None else None, flaky=fl[:20],
                       coverage=round(cov * 100, 2) if cov is not None else None, coverage_delta=delta,
                       reports=len(run.reports), file=file)


def _line(t: TestSummary) -> str:
    bits = [f"{t.passed} passed", f"{t.failed + t.errors} failed", f"{t.skipped} skipped"]
    out = ", ".join(bits)
    if t.new_failures is not None:
        out += f"; new against the base: {len(t.new_failures)}"
    if t.flaky:
        out += f"; flaky: {', '.join(t.flaky[:3])}" + (" …" if len(t.flaky) > 3 else "")
    if t.coverage is not None:
        out += f"; coverage {t.coverage:.1f}%" + (f" ({t.coverage_delta:+.1f})" if t.coverage_delta is not None
                                                     else "")
    return out + ("" if t.complete else " (not every report could be read)")


async def announce(svc: JobService, job: Job, run: TestRun, t: TestSummary, what: str,
                   session_id: str | None = None) -> None:
    """The job's event, and a `progress` event in its session's and its own event streams."""
    from hangar.events import RunEvent

    text = f"{what}: {_line(t)}"
    svc._event(job, text)
    data = {"source": "hangar", "action": "tests", "run": run.id, "kind": run.kind, "exit_code": run.exit_code,
            "passed": t.passed, "failed": t.failed, "errors": t.errors, "skipped": t.skipped, "total": t.total,
            "new_failures": len(t.new_failures) if t.new_failures is not None else None, "flaky": len(t.flaky),
            "coverage": t.coverage, "file": t.file}
    sid = session_id or job.session_id
    act = getattr(svc.sessions, "activity", None)
    if act is None:
        return
    with contextlib.suppress(Exception):  # events are a view; losing one loses nothing else
        s = svc.sessions.get(sid) if sid else None
        ev = RunEvent(kind="progress", turn=len(s.turns) if s else 0, text=text[:500],
                      data={k: v for k, v in data.items() if v is not None})
        if s is not None:
            act.append(s.id, [ev])
        if act.log.last_seq(job.id):  # the job's own stream (interactions), when it has one: never start one here
            act.append(job.id, [ev.model_copy()])


async def write_file(svc: JobService, job: Job, run: TestRun, t: TestSummary,
                     sandbox_id: str | None = None) -> str | None:
    """TEST-RESULTS.json in the job's directory: every failure (to AGENT_FAILURES) with its message, what is new
    against the base, the base's own failures, the flaky ones, coverage. The agent reads or greps it."""
    from hangar import baseline

    path = f"{svc._ctxdir(job)}/{FILE}"
    failing = run.failing
    if run.n_failing > len(failing) and (cases := decode(run)) is not None:
        shown = {f["id"] for f in failing}
        failing = failing + [{"id": k, "outcome": v} for k, v in sorted(cases.items())
                             if v in ("failed", "error") and k not in shown]
    base_failing = None
    if (b := baseline.current(job)) is not None and b.red and b.tests and b.tests != run.id:
        if (base := svc.store.get(TestRun, b.tests)) is not None:
            base_failing = sorted(failing_ids(base) or {f["id"] for f in base.failing})[:AGENT_FAILURES]
    doc = {"check": job.verify, "kind": run.kind, "run": run.id, "exit_code": run.exit_code, "counts": run.counts,
           "complete": run.complete, "failing": failing[:AGENT_FAILURES], "new_failures": t.new_failures,
           "base_failing": base_failing, "flaky": t.flaky, "flaky_list": sorted(flaky(svc.store, run.repo))[:200],
           "coverage": run.coverage, "reports": run.reports, "errors": run.errors}
    try:
        await svc.fleet.write_file(sandbox_id or job.sandbox_id or "", path,
                                   json.dumps(doc, indent=1).encode(), actor=f"job:{job.id}")
    except Exception as e:  # noqa: BLE001 - the summary on the job still says it all, in short
        log.info("job %s: couldn't write %s: %s", job.id, FILE, e)
        return None
    return path


async def finish(svc: JobService, job: Job, run: TestRun | None, what: str, *, sandbox_id: str | None = None,
                 session_id: str | None = None, agent_file: bool = True) -> TestSummary | None:
    """The summary of a run, its file for the agent and its events."""
    if run is None:
        return None
    t = summary(svc.store, run, job)
    if agent_file:
        t.file = await write_file(svc, job, run, t, sandbox_id)
    await announce(svc, job, run, t, what, session_id)
    return t


async def base_ready(svc: JobService, job: Job, run_id: str) -> None:
    """The base check's results, once the job has its baseline (run by it, or by another job on the same base)."""
    run = svc.store.get(TestRun, run_id)
    if run is not None:
        await finish(svc, job, run, "the base's tests")


def _rerun_wanted(svc: JobService, job: Job, run: TestRun) -> list[str]:
    """The failures that would justify running the check once more: all of them on the repo's flaky list."""
    from hangar import baseline

    p = svc.policies.get(job.repo) if job.repo else None
    if p is None or not p.rerun_flaky or not run.n_failing:
        return []
    ids = failing_ids(run)
    if baseline.red(job):
        new = structured_new(svc.store, job, run)
        ids = set(new) if new is not None else None
    if not ids:
        return []
    on_list = set(flaky(svc.store, run.repo))
    return sorted(ids) if ids <= on_list else []


async def verify(svc: JobService, job: Job) -> tuple[ExecResult, TestRun | None]:
    """The check after a turn: run, collect, and with the repo's `rerun_flaky`, once more when only known-flaky
    tests failed (the rerun decides). The job's `result.tests` is set."""
    turn = len(job.turns) or None
    res, run = await run_check(svc, job, "verify", turn=turn)
    if res.exit_code != 0 and run is not None and (again := _rerun_wanted(svc, job, run)):
        svc._event(job, f"the check failed only on tests on the repo's flaky list ({', '.join(again[:5])}"
                        + (" …" if len(again) > 5 else "") + "); running it once more (rerun_flaky)")
        res, run2 = await run_check(svc, job, "rerun", turn=turn)
        if res.exit_code == 0:
            svc._event(job, "the rerun passes")
        elif structured_new(svc.store, job, run2) == []:
            svc._event(job, "the rerun fails only where the base already did")
        else:
            svc._event(job, f"the rerun fails too (exit {res.exit_code})")
        run = run2
    job.result.tests = await finish(svc, job, run, "tests") if run is not None else None
    return res, run


async def in_turn(svc: JobService, job: Job, turn: Turn) -> None:
    """The reports of the checks the in-turn gate ran (`~/.hangar/runs/<run>.tests/<n>/`), as advisory runs."""
    if mode(svc.settings) == "off" or not turn.run_id or not job.sandbox_id or not job.verify:
        return
    sbid, home = job.sandbox_id, f"/home/{svc.settings.sandbox_user}"
    root = f"{home}/.hangar/runs/{turn.run_id}.tests"
    try:
        ls = await svc.fleet.exec(sbid, f"ls -1 {shlex.quote(root)} 2>/dev/null | head -n 50", timeout=30,
                                  actor=f"job:{job.id}", quiet=True)
        raw = await svc.fleet.read_file(sbid, f"{home}/.hangar/runs/{turn.run_id}.verify", limit=64_000)
    except Exception:  # noqa: BLE001 - the sandbox went away; the check after the turn reports what matters
        return
    lines = (raw or b"").decode(errors="replace").splitlines()
    dirs = sorted(int(x) for x in ls.stdout.split() if x.isdigit())[-IN_TURN_MAX:]

    async def ex(cmd: str, t: int) -> ExecResult:
        return await svc.fleet.exec(sbid, cmd, timeout=t, actor=f"job:{job.id}", quiet=True)

    async def rd(path: str, limit: int) -> bytes | None:
        return await svc.fleet.read_file(sbid, path, limit=limit)

    last: tuple[TestRun, int] | None = None
    for n in dirs:
        try:
            got = await gather(ex, rd, f"{root}/{n}", None, [], discover=False)
        except Exception:  # noqa: BLE001
            continue
        if got.results is None or not got.results.cases:
            continue
        code = None
        if n < len(lines):
            m = re.match(r"^(pass|fail (-?\d+))", lines[n])
            code = 0 if m and m.group(1) == "pass" else int(m.group(2)) if m and m.group(2) else None
        run = record(svc.store, results=got.results, kind="in_turn", redact=svc.broker.redact, repo=job.repo,
                     environment=job.environment, job_id=job.id, session_id=job.session_id, turn=len(job.turns),
                     person=job.person, command=job.verify, exit_code=code, files=got.files)
        last = (run, n)
    with contextlib.suppress(Exception):  # read: the next turn's checks start counting from 0 again
        await svc.fleet.exec(sbid, f"rm -rf {shlex.quote(root)}", timeout=30, actor=f"job:{job.id}", quiet=True)
    if last is not None:
        t = summary(svc.store, last[0])
        await announce(svc, job, last[0], t, f"the agent's last check in the turn ({len(dirs)} run(s))")


# ====================================================================== what people read
def packet_lines(svc: JobService, job: Job) -> list[str]:
    """For the review packet (landing.py), under the verification line."""
    if not job.verify or job.result.verify_exit_code is None or mode(svc.settings) == "off":
        return []
    t = job.result.tests
    if t is None:
        return ["  - **Tests:** no structured results: the check wrote no report hangar could read "
                "(`verify_reports` in .hangar/environment.yaml or the repo policy names them)"]
    out = [f"  - **Tests:** {t.passed} passed, {t.failed + t.errors} failed, {t.skipped} skipped"
           + ("" if t.complete else " (not every report could be read)")]
    if t.new_failures is not None:
        out.append("    - new failures against the base: "
                   + (", ".join(f"`{x}`" for x in t.new_failures[:10]) + (" …" if len(t.new_failures) > 10 else "")
                      if t.new_failures else "none"))
    elif t.failing:
        out.append("    - failing: " + ", ".join(f"`{x}`" for x in t.failing[:10])
                   + (" …" if t.failed + t.errors > 10 else ""))
    if t.flaky:
        out.append("    - flaky (on the repo's flaky list): " + ", ".join(f"`{x}`" for x in t.flaky[:10]))
    if t.coverage is not None:
        out.append(f"    - line coverage {t.coverage:.1f}%" + (f", {t.coverage_delta:+.2f} points against the base"
                                                                if t.coverage_delta is not None else ""))
    return out


def prompt_line(svc: JobService, job: Job, ctx: str) -> str | None:
    if mode(svc.settings) == "off":
        return None
    return (f"When `{job.verify}` writes test reports (JUnit, coverage), hangar puts the results of each of its own "
            f"runs of it in `{ctx}/{FILE}`: every failing test with its message, what is new against the base "
            "commit, and the tests known to be flaky. Read or grep that file rather than the whole output.")


# ====================================================================== traces (hangar.trace/v1)
def _suite_status(code: int | None) -> str:
    return {0: "success", None: "aborted", 124: "timed_out", 137: "timed_out"}.get(code, "failure")  # type: ignore


def trace_events(run: TestRun, limit: int = OTEL_CASES) -> list[dict[str, Any]]:
    """The run as span events in the trace export's taxonomy (docs/api/traces.md reserves `test.run` and
    `test.case`, on `job` or `turn`, attributes under `hangar.test.*`; every event's `hangar.kind` is its name),
    with OpenTelemetry's test semantic conventions alongside (registry `test.*`, Development stability:
    `test.suite.name`, `test.suite.run.status`, `test.case.name`, `test.case.result.status`). One `test.run` with
    the totals, then a `test.case` for each failing or flaky test, up to `limit`: passing tests are counted, not
    listed. [{"name", "time", "attributes"}]."""
    cov = (run.coverage or {}).get("line_rate")
    out = [{"name": "test.run", "time": run.at, "attributes": _present({
        "hangar.kind": "test.run", "test.suite.name": run.command or run.kind,
        "test.suite.run.status": _suite_status(run.exit_code), "hangar.test.run.id": run.id,
        "hangar.test.run.kind": run.kind, "hangar.test.run.trusted": run.trusted,
        "hangar.test.exit_code": run.exit_code, "hangar.test.total": run.counts.get("total", 0),
        "hangar.test.passed": run.counts.get("passed", 0), "hangar.test.failed": run.counts.get("failed", 0),
        "hangar.test.errors": run.counts.get("error", 0), "hangar.test.skipped": run.counts.get("skipped", 0),
        "hangar.test.complete": run.complete, "hangar.test.flaky": len(run.flaky),
        "hangar.test.coverage.line_rate": cov, "hangar.test.attempt": run.attempt, "vcs.ref.head.revision": run.sha,
        "hangar.environment": run.environment})}]
    flaky_ids = set(run.flaky)
    failing = {f["id"] for f in run.failing}
    for f in run.failing[:limit]:
        out.append({"name": "test.case", "time": run.at, "attributes": _present({
            "hangar.kind": "test.case", "test.case.name": f["id"], "test.case.result.status": "fail",
            "hangar.test.outcome": f["outcome"], "hangar.test.flaky": f["id"] in flaky_ids,
            "hangar.test.message": (f.get("message") or "")[:500], "hangar.test.run.id": run.id})})
    for cid in [c for c in run.flaky if c not in failing][:max(0, limit - len(out) + 1)]:
        out.append({"name": "test.case", "time": run.at, "attributes": {
            "hangar.kind": "test.case", "test.case.name": cid, "test.case.result.status": "pass",
            "hangar.test.outcome": "passed", "hangar.test.flaky": True, "hangar.test.run.id": run.id}})
    return out


def _present(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != ""}


def add_to_export(x: Any, sp: Any, store: Any, job: Job) -> None:
    """For the trace export (traces.job_export): each of the job's runs (the base check another job ran included) as
    events on the `job` span, through the export's own `event(span, name, ts, attrs)`, which redacts and counts
    them against its limit."""
    runs = runs_of(store, job_id=job.id)
    b = job.result.baseline
    if b is not None and b.tests and all(r.id != b.tests for r in runs) and (base := store.get(TestRun, b.tests)):
        runs.append(base)
    for r in sorted(runs, key=lambda r: r.at):
        for e in trace_events(r):
            x.event(sp, e["name"], e["time"], e["attributes"])


def export_otel(run: TestRun) -> None:
    """Live: the run's events on whatever span is open (job.verify, the job's provisioning), with tracing on."""
    from hangar import observe

    if not observe.enabled():
        return
    with contextlib.suppress(Exception):  # telemetry never breaks the check it describes
        from opentelemetry import trace

        sp = trace.get_current_span()
        if sp.is_recording():
            for e in trace_events(run):
                sp.add_event(e["name"], attributes={k: v if isinstance(v, str | bool | int | float) else str(v)
                                                    for k, v in e["attributes"].items()})


# ====================================================================== the acceptance checks and the doctor
def record_acceptance(store: Any, rec: dict[str, Any]) -> TestRun | None:
    """A deploy's acceptance checks (power.record_deploy's record) as a run on the deployed commit: a check that
    passes and fails on one commit is flaky."""
    checks = rec.get("checks")
    if not isinstance(checks, dict) or not checks:
        return None
    res = testreports.Results()
    for name, status in checks.items():
        outcome = {"pass": "passed", "ok": "passed", "fail": "failed"}.get(str(status), "skipped")
        res.cases[str(name)] = testreports.Case(id=str(name), name=str(name), suite="acceptance",
                                                outcome=outcome)  # type: ignore[arg-type]
    failed = any(c.outcome == "failed" for c in res.cases.values())
    return record(store, results=res, kind="acceptance", repo=ACCEPTANCE_REPO, environment="deploy",
                  tree=rec.get("sha"), sha=rec.get("sha"), command="acceptance", exit_code=1 if failed else 0)


def only_flaky(store: Any, job_ids: list[str], repo: str | None) -> bool:
    """For the Cloud Doctor: did each of these jobs' failing checks fail only on tests on the repo's flaky list?
    Without reports for every one of them, no."""
    if not job_ids or not repo:
        return False
    on_list = set(flaky(store, repo))
    if not on_list:
        return False
    for jid in job_ids:
        runs = [r for r in store.list_where(TestRun, job_id=jid)  # the job's own checks, not the base it started on
                if r.kind in ("verify", "rerun", "attempt") and r.exit_code not in (0, None)]
        if not runs:
            return False
        ids = failing_ids(max(runs, key=lambda r: r.at))
        if not ids or not ids <= on_list:
            return False
    return True


def status_section(fleet: Any, sessions: Any, jobs: Any) -> dict[str, Any]:
    """`hangar status`: the repos with the most flaky tests."""
    rows = []
    for key in fleet.store.kv_keys(KV_FLAKY):
        repo = key[len(KV_FLAKY):]
        data = _flaky_data(fleet.store, repo)
        if data:
            top = sorted(data.items(), key=lambda kv: (-int(kv[1].get("n", 0)), -kv[1].get("last", 0)))
            rows.append({"repo": repo, "flaky": len(data), "last_at": max(v.get("last", 0) for v in data.values()),
                         "top": [{"test": k, "times": v.get("n"), "how": v.get("how")} for k, v in top[:5]]})
    rows.sort(key=lambda r: (-r["flaky"], -r["last_at"]))
    return {"days": FLAKY_DAYS, "repos": rows[:10]}


# ====================================================================== REST and the resource
def view(run: TestRun, cases: bool = False) -> dict[str, Any]:
    d = run.model_dump(mode="json", exclude={"cases"})
    d["cases_kept"] = run.cases is not None
    if cases:
        d["cases"] = decode(run)
    return d


def brief(run: TestRun) -> dict[str, Any]:
    return run.model_dump(mode="json", include={"id", "at", "kind", "trusted", "turn", "attempt", "exit_code",
                                                "complete", "counts", "n_failing", "flaky", "coverage", "sha"})


def runs_of(store: Any, *, job_id: str | None = None, session_id: str | None = None) -> list[TestRun]:
    rows = store.list_where(TestRun, job_id=job_id) if job_id else store.list_where(TestRun, session_id=session_id)
    return sorted(rows, key=lambda r: r.at, reverse=True)


def _answer(rows: list[TestRun], run: str | None, fmt: str | None, cases: bool, extra: dict[str, Any]) -> Any:
    if run is not None:
        r = next((x for x in rows if x.id == run), None)
        if r is None:
            raise KeyError(f"no test run '{run}' here")
        return {"events": trace_events(r, limit=FAILING_KEPT)} if fmt == "otel" else view(r, cases)
    if fmt == "otel":
        return {**extra, "events": [e for r in sorted(rows[:50], key=lambda r: r.at) for e in trace_events(r)]}
    return {**extra, "runs": [brief(r) for r in rows[:200]], "latest": view(rows[0], cases) if rows else None}


def mount(app: Any, h: Any, job_caller: Any, admin: Any) -> None:
    """GET /api/jobs/{id}/tests and /api/sessions/{ref}/tests (`run=` one run whole, `cases=true` with every case,
    `format=otel` as spans), GET /api/repos/tests/flaky; and the `tests` section of `hangar status`."""
    from hangar import opstatus, people

    opstatus.EXTRA["tests"] = status_section

    @app.get("/api/jobs/{job_id}/tests")
    async def job_tests(job_id: str, run: str | None = None, format: str | None = None,  # noqa: A002
                        cases: bool = False, caller: Any = job_caller):
        job = h.jobs.get(job_id, owner=caller.owner if caller.restricted else None)
        if not people.visible(job.person):
            raise KeyError(f"no job '{job_id}'")
        rows = runs_of(h.store, job_id=job.id)
        b = job.result.baseline
        if b is not None and b.tests and all(r.id != b.tests for r in rows):  # the base check another job ran
            if (base := h.store.get(TestRun, b.tests)) is not None:
                rows.append(base)
        t = job.result.tests.model_dump(mode="json") if job.result.tests else None
        return _answer(rows, run, format, cases, {"job": job.id, "summary": t,
                                                  "flaky": flaky(h.store, job.repo) if job.repo else {}})

    @app.get("/api/sessions/{ref}/tests")
    async def session_tests(ref: str, run: str | None = None, format: str | None = None,  # noqa: A002
                            cases: bool = False, _: str = admin):
        s = h.sessions.get(ref)
        if not people.visible(s.person):
            raise KeyError(f"no session '{ref}'")
        return _answer(runs_of(h.store, session_id=s.id), run, format, cases, {"session": s.name})

    @app.get("/api/repos/tests/flaky")
    async def repo_flaky(repo: str, _: str = admin):
        return {"repo": repo, "days": FLAKY_DAYS, "flaky": flaky(h.store, repo)}


def register(mcp: Any, jobs: Any, guard: Any) -> None:
    """`hangar://jobs/<id>/tests`: a resource, not a tool (the tool list is at its cap)."""
    if jobs is None:
        return

    @mcp.resource("hangar://jobs/{job_id}/tests", name="job-tests", mime_type="application/json",
                  description="A job's test results: each run of its check (the base, after each turn, reruns, "
                              "attempts) with counts, failures, flaky tests and coverage.")
    async def job_tests_resource(job_id: str) -> str:
        from mcp.server.mcpserver.exceptions import ResourceError

        try:
            guard.check("job", job_id)
        except (KeyError, PermissionError) as e:
            raise ResourceError(str(e).strip("'\"")) from None
        job = jobs.get(job_id)
        rows = runs_of(jobs.store, job_id=job_id)
        return json.dumps({"job": job_id, "summary": job.result.tests.model_dump(mode="json")
                           if job.result.tests else None, "runs": [brief(r) for r in rows[:50]],
                           "latest": view(rows[0]) if rows else None}, indent=1, default=str)
