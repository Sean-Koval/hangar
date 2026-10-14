"""The check on the base commit (--- frontdoor; product review round 2, section 6).

With a repo or environment `verify` on every front door, a red base branch would fail every job's check: the agent
spends its verify-fix rounds on failures it didn't cause (and widens its diff doing so), best-of-N ranks every
attempt as failing, and each PR goes out as a draft that says the change broke something.

So before the agent starts, hangar runs the job's standing check once on the base commit:

* once per (repo, base commit, command), shared: the result is kept in the store's kv table, and jobs that start on
  the same base while it runs wait for that one run instead of starting their own (60 sessions on one `main` cost
  one check per merge, not 60);
* only for a standing check, the one from the repo's policy or the environment. A `verify` the job brought is its
  acceptance test, written for the change, and expected to fail before it;
* a red base is reported in the job's events, its prompt and the PR body. The in-turn gate is off for the job (it
  can't subtract failures), and the check after the turn counts only *new* failures: the test ids it can read from
  the output (pytest, go test, cargo test, jest and vitest), minus the base's. When it can't read them on both
  sides it can't tell what's new: no verify-fix turn, and the PR goes out as a draft that says why.
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
    re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$"),  # pytest's short test summary (-rfE, its default)
    re.compile(r"^\s*--- FAIL: (\S+)"),  # go test, subtests included
    re.compile(r"^test (\S+) \.\.\. FAILED$"),  # cargo test
    re.compile(r"^\s*FAIL\s+(\S.*?)\s*$"),  # jest (a file), vitest (file > suite > test)
    re.compile(r"^\s*[✕×]\s+(\S.*?)\s*$"),  # jest and vitest, one failing test
]


def failures(output: str) -> list[str] | None:
    """The failing test ids in a check's output, or None when it names none it can read."""
    ids: set[str] = set()
    for line in _ANSI.sub("", output).splitlines():
        for rx in _PATTERNS:
            if m := rx.match(line):
                ident = _DURATION.sub("", m.group(1)).strip()
                if ident:
                    ids.add(ident[:300])
                break
    return sorted(ids)[:MAX_IDS] if ids else None


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


def new_failures(job: Job, output: str) -> list[str] | None:
    """Failures in `output` that the base didn't have; None when the base was green or they can't be told apart."""
    b = current(job)
    if b is None or not b.red or not b.parsed:
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


def key(job: Job) -> str:
    raw = f"{repo_key(job.repo)}\0{job.result.base_sha}\0{job.verify}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


class Baselines:
    """The cache and the single flight per key. One control plane runs jobs (one replica), so an in-process map of
    running checks is enough to stop parallel jobs on one base from each running it."""

    def __init__(self, svc: JobService):
        self.svc = svc
        self._flights: dict[str, asyncio.Task[Baseline | None]] = {}
        self._pruned = 0.0

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
        svc, k = self.svc, key(job)
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
        if b.red:
            n = f"; {len(b.failures)} failing test(s)" if b.parsed else "; hangar can't read which tests fail"
            svc._event(job, f"base was red: `{job.verify}` fails on {b.sha[:12]} before any change (exit "
                            f"{b.exit_code}){n}. The agent is told; those failures don't count against the job")
            with contextlib.suppress(Exception):  # the prompt points at it; the check itself still works without
                await svc.fleet.write_file(job.sandbox_id, f"{svc._ctxdir(job)}/BASELINE.txt",
                                           (b.tail or "").encode(), actor=f"job:{job.id}")
        else:
            svc._event(job, f"the base is green: `{job.verify}` passes on {b.sha[:12]}")

    async def _run(self, job: Job, k: str) -> Baseline | None:
        from hangar.fleet import _clip_tail  # fleet imports verifyloop, which imports this module

        svc = self.svc
        try:
            res = await svc._sh(job, job.verify, timeout=svc.settings.job_verify_timeout_seconds)
        except Exception as e:  # noqa: BLE001 - the sandbox went away: the next job on this base tries again
            log.warning("job %s: baseline check could not run: %s", job.id, e)
            return None
        out = (res.stdout or "") + (res.stderr or "")
        ids = failures(out) if res.exit_code != 0 else []
        b = Baseline(sha=job.result.base_sha or "", exit_code=res.exit_code, failures=ids or [],
                     parsed=res.exit_code == 0 or ids is not None, job_id=job.id,
                     tail=svc.broker.redact(_clip_tail(out)[-4000:]))
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
