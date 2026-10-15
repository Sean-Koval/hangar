"""The check on the base commit (--- frontdoor; product review round 2, section 6).

With a repo or environment `verify` on every front door, a red base branch would fail every job's check: the agent
spends its verify-fix rounds on failures it didn't cause (and widens its diff doing so), best-of-N ranks every
attempt as failing, and each PR goes out as a draft that says the change broke something.

So before the agent starts, hangar runs the job's standing check once on the base commit:

* once per (repo, base commit, command, environment, owner), shared: the result is kept in the store's kv table,
  and jobs that start on the same base while it runs wait for that one run instead of starting their own (60
  sessions of 6 people on one `main` cost at most one check per person and environment per merge, not 60);
* only for a standing check, the one from the repo's policy or the environment. A `verify` the job brought is its
  acceptance test, written for the change, and expected to fail before it;
* a red base is reported in the job's events, its prompt and the PR body. The in-turn gate is off for the job (it
  can't subtract failures), and the check after the turn counts only *new* failures: the test ids it can read from
  the output (pytest, go test, cargo test, jest and vitest), minus the base's. When it can't read them on both
  sides it can't tell what's new: no verify-fix turn, and the PR goes out as a draft that says why.
* --- testresults: when the check writes test reports (testresults.py), the base's are kept as a TestRun, and a
  check after the turn that left complete reports too is compared by test id, exactly; the text is the fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from hangar.models import Baseline, Job, now
from hangar.repopolicy import repo_key

if TYPE_CHECKING:
    from hangar.jobs import JobService

log = logging.getLogger("hangar.baseline")

KV_PREFIX = "baseline:"
KEEP_SECONDS = 14 * 86400  # a base commit nobody starts from for two weeks has long been merged over
PRUNE_EVERY = 600
MAX_IDS = 500
PROMPT_IDS = 20
_NOT_CACHED = {124, 137, -1}  # timed out or killed: maybe slow, maybe flaky; the next job tries again

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_DURATION = re.compile(r"\s+\(?\d+(?:\.\d+)?\s*m?s\)?$")
_PATTERNS = [
    # pytest's short test summary (-rfE, its default); a parametrized id may hold spaces ("test_x[a b]")
    re.compile(r"^(?:FAILED|ERROR) (\S.*?)(?: - .*)?$"),
    re.compile(r"^\s*--- FAIL: (\S+)"),  # go test, subtests included
    re.compile(r"^test (\S+) \.\.\. FAILED$"),  # cargo test
    re.compile(r"^\s*FAIL\s+(\S.*?)\s*$"),  # jest (a file), vitest (file > suite > test)
    re.compile(r"^\s*[✕×]\s+(\S.*?)\s*$"),  # jest and vitest, one failing test
]


# A runner that stops at the first failures never runs the rest, so what it doesn't name may still fail: on a red
# base the base's own failure would stop it before the change's. pytest says so in its output (also when -x comes
# from addopts); cargo and nextest stop after the first failing test binary unless told not to; the others by flag.
_STOPPED = re.compile(r"stopping after \d+ failures")
_STOP_FLAGS = re.compile(r"(?:^|\s)(?:-x|--exitfirst|--maxfail(?:=\S+|\s+\S+)|--?failfast|--bail(?:=\S+)?"
                         r"|--stepwise|--sw)(?=\s|$|[;&|)])")
_CARGO = re.compile(r"\bcargo\s+(?:\+\S+\s+)?(?:test|nextest)\b")


def failures(output: str) -> list[str] | None:
    """The failing test ids in a check's output, or None when it names none it can read. All of them: a capped
    list could drop exactly the failure a change added."""
    ids: set[str] = set()
    for line in _ANSI.sub("", output).splitlines():
        for rx in _PATTERNS:
            if m := rx.match(line):
                ident = _DURATION.sub("", m.group(1)).strip()
                if ident:
                    ids.add(ident[:300])
                break
    return sorted(ids) if ids else None


def stops_early(verify: str | None, output: str) -> bool:
    """Might the check have stopped before running every test (so a failure it doesn't name may still be there)?"""
    cmd = verify or ""
    return bool(_STOPPED.search(output) or _STOP_FLAGS.search(cmd)
                or (_CARGO.search(cmd) and "--no-fail-fast" not in cmd))


def read_base(output: str, exit_code: int | None, verify: str | None, truncated: bool) -> tuple[list[str], bool]:
    """The base's failing ids, and whether they are all of them (else nothing can be subtracted): not when the
    output was cut short, the run stopped at its first failures, or there are more than hangar keeps."""
    if exit_code == 0:
        return [], True
    ids = failures(output)
    if ids is None:
        return [], False
    whole = not truncated and not stops_early(verify, output) and len(ids) <= MAX_IDS
    return ids[:MAX_IDS], whole


def applies(job: Job) -> bool:
    return bool(job.verify and job.verify_source in ("repo", "environment") and job.repo and job.result.base_sha)


def current(job: Job) -> Baseline | None:
    """The job's baseline, if it was taken on the commit the job builds on (a bundle may have moved that)."""
    b = job.result.baseline
    return b if b is not None and b.sha == job.result.base_sha else None


def red(job: Job) -> bool:
    b = current(job)
    return bool(b and b.red)


Verdict = Literal["pass", "fail", "unknown"]


def verdict(job: Job, exit_code: int | None, new: list[str] | None) -> Verdict:
    """Did the check pass, counting only what the job added to a red base?"""
    if not job.verify or exit_code == 0:
        return "pass"
    if not red(job):
        return "fail"
    if new is None:
        return "unknown"
    return "fail" if new else "pass"


def new_failures(job: Job, output: str, truncated: bool = False) -> list[str] | None:
    """Failures in `output` that the base didn't have; None when the base was green or they can't be told apart
    (the output was cut short, or the check stopped before running everything)."""
    b = current(job)
    if b is None or not b.red or not b.parsed or truncated or stops_early(job.verify, output):
        return None
    after = failures(output)
    if after is None:
        return None
    return sorted(set(after) - set(b.failures))


def finish_line(job: Job, ctxdir: str) -> str:
    """The prompt's instruction about the check."""
    b = current(job)
    if b is None or not b.red:
        return f"Before finishing, run `{job.verify}` and make it pass."
    known = ""
    if b.parsed:
        more = f" and {len(b.failures) - PROMPT_IDS} more" if len(b.failures) > PROMPT_IDS else ""
        known = f": {', '.join(b.failures[:PROMPT_IDS])}{more}"
    return (f"Before finishing, run `{job.verify}`. It already fails on the base commit `{b.sha[:12]}`, before any "
            f"change (exit {b.exit_code}){known}; its output is in `{ctxdir}/BASELINE.txt`. Those failures are not "
            "yours: leave them unless the task is about them, and make sure your change adds no new ones.")


def after_line(job: Job) -> str:
    """The job event for the check after a turn, on a red base."""
    r = job.result
    if r.verify_exit_code == 0:
        return "the check passes now (it failed on the base)"
    if r.new_failures == []:
        return "the check fails only where the base already did: no new failures"
    if r.new_failures:
        return "the check adds failures to the base's: " + ", ".join(r.new_failures[:10])
    return ("the check fails, as it did on the base, and hangar can't tell whether this change added failures: no "
            "verify-fix turn, and the PR is a draft")


def pr_line(job: Job) -> str | None:
    b = current(job)
    if b is None or not b.red:
        return None
    ids = f" ({len(b.failures)} failing: " + ", ".join(f"`{x}`" for x in b.failures[:5]) \
        + (", …" if len(b.failures) > 5 else "") + ")" if b.parsed else ""
    if job.result.verify_exit_code == 0:
        tail = "it passes now."
    elif job.result.new_failures == []:
        tail = "this change adds no new failures."
    elif job.result.new_failures:
        tail = "this change adds: " + ", ".join(f"`{x}`" for x in job.result.new_failures[:10]) + "."
    else:
        tail = "hangar couldn't tell which failures are new, so this PR is a draft."
    return f"**Base was red:** `{job.verify}` already failed on `{b.sha[:12]}` (exit {b.exit_code}){ids}; {tail}"


class Baselines:
    """The cache and the single flight per key. One control plane runs jobs (one replica), so an in-process map of
    running checks is enough to stop parallel jobs on one base from each running it."""

    def __init__(self, svc: JobService):
        self.svc = svc
        self._flights: dict[str, asyncio.Task[Baseline | None]] = {}
        self._pruned = 0.0

    def key(self, job: Job) -> str:
        """The repo, base commit and command, and what else decides the result: the environment as it is now (its
        image, setup, env and resources; a check that is red for a missing dependency in one is no excuse in
        another) and whose sandbox ran it (one member's sandbox can't decide which failures another's jobs may
        subtract)."""
        try:
            spec = self.svc.fleet.catalog.get(job.environment).model_dump(mode="json",
                                                                          exclude={"verify", "description"})
        except KeyError:
            spec = None
        try:
            image = self.svc.fleet.get(job.sandbox_id or "").image
        except KeyError:
            image = None
        raw = json.dumps([repo_key(job.repo), job.result.base_sha, job.verify, job.owner, job.environment, spec,
                          image], sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def cached(self, k: str) -> Baseline | None:
        raw = self.svc.store.kv_get(KV_PREFIX + k)
        if raw is None:
            return None
        try:
            return Baseline.model_validate(json.loads(raw)["baseline"])
        except (ValueError, KeyError, TypeError, ValidationError):
            return None

    async def ensure(self, job: Job) -> None:
        """Give the job its baseline: from the cache, from a run another job started, or by running it here."""
        if not applies(job):
            return
        svc, k = self.svc, self.key(job)
        b = self.cached(k)
        if b is None:
            task = self._flights.get(k)
            if task is None:
                svc._event(job, f"checking the base: running `{job.verify}` on {job.result.base_sha[:12]} "
                                "(once per base commit)")
                task = asyncio.create_task(self._run(job, k))
                self._flights[k] = task
                svc._background.add(task)  # cancelled with the service, not with the job that started it
                task.add_done_callback(svc._background.discard)
                task.add_done_callback(lambda _t: self._flights.pop(k, None))
            else:
                svc._event(job, f"waiting for another job's check of the base {job.result.base_sha[:12]}")
            try:
                b = await asyncio.shield(task)  # this job going away must not cancel the others' check
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - no baseline is today's behaviour, not a failure
                log.warning("job %s: baseline check failed: %s", job.id, e)
                b = None
        if b is None:
            svc._event(job, "couldn't check the base; every failure of the check counts")
            return
        job.result.baseline = b
        svc._save(job)
        if b.tests:  # --- testresults: the base's failures by test id, in the job's events and the agent's file
            from hangar import testresults

            with contextlib.suppress(Exception):
                await testresults.base_ready(svc, job, b.tests)
        if b.red:
            n = f"; {len(b.failures)} failing test(s)" if b.parsed else f"; {b.test_failures} failing test(s)" \
                if b.test_failures else "; hangar can't read which tests fail"  # --- testresults: or from reports
            svc._event(job, f"base was red: `{job.verify}` fails on {b.sha[:12]} before any change (exit "
                            f"{b.exit_code}){n}. The agent is told; those failures don't count against the job")
            with contextlib.suppress(Exception):  # the prompt points at it; the check itself still works without
                await svc.fleet.write_file(job.sandbox_id, f"{svc._ctxdir(job)}/BASELINE.txt",
                                           (b.tail or "").encode(), actor=f"job:{job.id}")
        else:
            svc._event(job, f"the base is green: `{job.verify}` passes on {b.sha[:12]}")

    async def _run(self, job: Job, k: str) -> Baseline | None:
        from hangar import testresults  # --- testresults
        from hangar.fleet import _clip_tail  # fleet imports verifyloop, which imports this module

        svc = self.svc
        try:
            res, tests = await testresults.run_check(svc, job, "base")  # --- testresults: and its reports
        except Exception as e:  # noqa: BLE001 - the sandbox went away: the next job on this base tries again
            log.warning("job %s: baseline check could not run: %s", job.id, e)
            return None
        out = (res.stdout or "") + (res.stderr or "")
        ids, whole = read_base(out, res.exit_code, job.verify, res.truncated)
        b = Baseline(sha=job.result.base_sha or "", exit_code=res.exit_code, failures=ids, parsed=whole,
                     job_id=job.id, tail=svc.broker.redact(_clip_tail(out)[-4000:]))
        if tests is not None and not stops_early(job.verify, out) and testresults.failing_ids(tests) is not None \
                and (res.exit_code == 0 or tests.n_failing):  # --- testresults: all of its failures, by test id
            b.tests, b.test_failures = tests.id, tests.n_failing
        if res.exit_code not in _NOT_CACHED:
            svc.store.kv_set(KV_PREFIX + k, json.dumps({"repo": repo_key(job.repo), "verify": job.verify,
                                                        "baseline": b.model_dump(mode="json")}))
            self._prune()
        return b

    def _prune(self) -> None:
        t = now()
        if t - self._pruned < PRUNE_EVERY:
            return
        self._pruned = t
        for k in self.svc.store.kv_keys(KV_PREFIX):
            b = self.cached(k[len(KV_PREFIX):])
            if b is None or b.at < t - KEEP_SECONDS:
                self.svc.store.kv_delete(k)
