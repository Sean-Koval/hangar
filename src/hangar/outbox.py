"""Run capture: the outbox (--- outbox #163, #189; docs/api/outbox.md, spec 0014 §7).

Every finished agent run (a session turn, a job turn after its check and delivery, a best-of-N attempt after its
check, an experiment trial's turns) is written once, as immutable objects, to `HANGAR_OUTBOX_URL` (a GCS bucket, or
a local directory for dev and tests), so the sister tool reads yesterday's runs while the sandbox pools are off and
after hangar's own retention has let them go:

    v1/runs/{date}/{session_id}.{run_id}/run.json         hangar.run/v1 (docs/api/run.schema.json), written last
                                         manifest.json    hangar.manifest/v1, as captured when the turn started
                                         trace.otlp.json  hangar.trace/v1 of this run only
                                         tests.json       hangar.tests/v1: the turn's TestRuns, with every case
                                         grade.json       experiment trials, when the grader has run
                                         patch.diff       the turn's diff against the commit it started on
    v1/jobs/{date}/{job_id}/events/{ts}-{kind}.json       hangar.job_event/v1: pr.open, review.first, pr.merge, ...

Nothing here is on a turn's path. A turn's end only starts a task: the patch is read from the sandbox while it is
still up, then the objects are built, redacted (`broker.redact` and secretscan's masking, on top of what the
manifest and trace already did; a payload that still holds a secret is `failed` and never uploaded), gzipped and
staged on the data volume with one durable queue record each (`OutboxItem`). A drain loop uploads them write-once
(`ifGenerationMatch=0`: a 412 means it is already there, which counts as done), retrying with exponential backoff
from 1 minute to 6 hours for as long as it takes. The control plane stays up while the pools are off (01:00-10:00
ET), and GCS doesn't depend on the cluster, so the queue keeps draining overnight.

Retention (#189): `exported()` says whether every object of a run is uploaded (always true with the outbox off). A
session deleted before its runs are exported keeps its event log and manifests under `<data>/pending-delete/` until
they are, or until HANGAR_EXPORT_GRACE_DAYS (audited `retention.unexported_deleted`); test runs of failed or
abandoned jobs outlive testresults' windows until exported.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from hangar.models import JobState, now

if TYPE_CHECKING:
    from hangar.fleet import Fleet
    from hangar.jobs import JobService
    from hangar.models import Attempt, Job, Session, SessionTurn, Turn
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.outbox")

SCHEMA = "hangar.run/v1"
EVENT_SCHEMA = "hangar.job_event/v1"
TESTS_SCHEMA = "hangar.tests/v1"
SCHEMA_FILE = "docs/api/run.schema.json"
PREFIX = "v1"
SOURCES = ("job", "session", "experiment", "shadow")
EVENT_KINDS = ("pr.open", "pr.ready", "review.first", "pr.merge", "pr.close", "revert", "abandon", "label")
BACKOFF_MIN = 60.0
BACKOFF_MAX = 6 * 3600.0
WARN_PENDING = 1000
WARN_AGE = 86400.0
DRAIN_EVERY = 5.0  # seconds between passes when nothing new arrives
SCAN_EVERY = 60.0  # jobs' lifecycle facts (PR opened, merged, ...)
SWEEP_EVERY = 600.0  # pending-delete
BATCH = 50
UPLOADS = 4  # at once
PATCH_SECONDS = 120
PATCH_WAIT = PATCH_SECONDS + 30
DIFF_FILES = 1000
KV_SCAN = "outbox:jobs-scanned-at"
KV_EVENT = "outbox:job-event:"  # + <job id>:<kind>[:<when>] -> the object key
KV_SINCE = "outbox:enabled-at"
PENDING_DELETE = "pending-delete"
TYPES = {".json": "application/json", ".diff": "text/x-diff"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")

# --- #189: testresults.prune asks this before dropping a run or its cases (None: the outbox is off)
RETAIN: Callable[[Any], bool] | None = None


class OutboxItem(BaseModel):
    """One object on its way to the outbox. Kept after upload (`done`): exported() reads them."""

    key: str  # the object's name under HANGAR_OUTBOX_URL
    kind: str  # run.json | manifest.json | trace.otlp.json | tests.json | grade.json | patch.diff | event
    run: str  # "<session id>.<run id>", or "job:<job id>" for a job event
    session_id: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    state: Literal["pending", "done", "failed"] = "pending"
    attempts: int = 0
    next_at: float = Field(default_factory=now)
    error: str | None = None
    bytes: int = 0  # gzipped, as staged and uploaded
    after: list[str] = Field(default_factory=list)  # keys uploaded first (run.json waits for the rest of its run)
    created: bool | None = None  # True: this upload made it; False: it was already there (412)
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    done_at: float | None = None


class Unredacted(ValueError):
    """A payload still held a secret after redaction: it is never uploaded."""


def run_key(session_id: str, run_id: str) -> str:
    return f"{session_id}.{run_id}"


def day(ts: float | None) -> str:
    """The UTC date a run (or event) is filed under."""
    return datetime.fromtimestamp(ts or now(), UTC).strftime("%Y-%m-%d")


def run_prefix(session_id: str, run_id: str, ts: float | None) -> str:
    return f"{PREFIX}/runs/{day(ts)}/{_SAFE.sub('_', run_key(session_id, run_id))}/"


# ====================================================================== sinks
class LocalSink:
    """A directory (dev, tests): the objects as a GCS reader gets them (decompressed), created exclusively."""

    def __init__(self, root: Path):
        self.root = root
        self.description = f"local directory {root}"

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve()) + os.sep):
            raise ValueError(f"bad outbox key {key!r}")
        return p

    async def put_once(self, key: str, gz: bytes, content_type: str) -> bool:
        p = self._path(key)

        def write() -> bool:
            if p.exists():
                return False
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.parent / f".{p.name}.{secrets.token_hex(4)}.part"
            tmp.write_bytes(gzip.decompress(gz))
            try:
                os.link(tmp, p)  # fails when it exists: never an overwrite, even racing another writer
            except FileExistsError:
                return False
            finally:
                tmp.unlink(missing_ok=True)
            return True

        return await asyncio.to_thread(write)

    async def close(self) -> None:
        return None


class GCSSink:
    """A bucket, through checkpoints.GCSStore (Workload Identity, its retries): gzip objects with
    `Content-Encoding: gzip`, created with ifGenerationMatch=0 by a service account that can't delete or overwrite."""

    def __init__(self, url: str, transport: Any = None, token: str | None = None):
        from hangar.checkpoints import GCSStore

        self.store = GCSStore(url, transport=transport, token=token)
        self.store.ATTEMPTS = 2  # the outbox has its own backoff; don't hold a drain slot for long
        self.description = self.store.description

    async def put_once(self, key: str, gz: bytes, content_type: str) -> bool:
        return await self.store.put_once(key, gz, content_type=content_type, content_encoding="gzip")

    async def close(self) -> None:
        await self.store.close()


def make_sink(url: str | None) -> LocalSink | GCSSink | None:
    if not url:
        return None
    if url.startswith("gs://"):
        return GCSSink(url)
    return LocalSink(Path(url.removeprefix("file://")).expanduser())


# ====================================================================== the outbox
class Outbox:
    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self.settings = fleet.settings
        self.store = fleet.store
        self.sink: LocalSink | GCSSink | None = make_sink(self.settings.outbox_url)
        self.root = self.settings.data_dir / "outbox"
        self.sessions: SessionService | None = None
        self.jobs: JobService | None = None
        self._deferred: set[str] = set()  # run ids a job or attempt captures itself, after its check
        self._patches: dict[str, asyncio.Task] = {}
        self._extra: dict[str, dict[str, Any]] = {}  # run key -> what only the turn's end knew (traceparent)
        self._capturing: set[str] = set()  # run keys being built: not exported yet
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._wake_loop: asyncio.AbstractEventLoop | None = None

    @property
    def enabled(self) -> bool:
        return self.sink is not None

    @property
    def description(self) -> str:
        return self.sink.description if self.sink is not None else "off"

    # ------------------------------------------------------------------ lifecycle
    def start(self, sessions: Any = None, jobs: Any = None) -> None:
        global RETAIN
        self.sessions, self.jobs = sessions or self.sessions, jobs or self.jobs
        if not self.enabled:
            return
        if self.store.kv_get(KV_SINCE) is None:
            self.store.kv_set(KV_SINCE, str(now()), durable=True)
        RETAIN = self.retain_test_run
        if self._loop is None or self._loop.done():
            self._loop = asyncio.create_task(self._run())

    async def stop(self) -> None:
        global RETAIN
        if RETAIN == self.retain_test_run:
            RETAIN = None
        tasks = [*self._tasks, *self._patches.values(), *([self._loop] if self._loop else [])]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        self._tasks.clear()
        self._patches.clear()
        self._loop = None
        if self.sink is not None:
            with contextlib.suppress(Exception):
                await self.sink.close()

    def _spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _kick(self) -> None:
        """Wake the drain loop now (from any thread: job scans and captures stage from worker threads)."""
        if self._wake is None or self._wake_loop is None or self._wake_loop.is_closed():
            return
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is self._wake_loop:
            self._wake.set()
        else:
            self._wake_loop.call_soon_threadsafe(self._wake.set)

    # ------------------------------------------------------------------ hooks on the turn's path (never raise)
    def wanted(self, s: Session) -> bool:
        return self.enabled and (self.settings.outbox_sessions != "jobs_only" or bool(s.job_id))

    def defer(self, run_id: str | None) -> None:
        """A job (or best-of-N attempt) captures this run itself, once it is checked and delivered."""
        if run_id and self.enabled:
            self._deferred.add(run_id)

    def turn_ended(self, svc: SessionService, s: Session, turn: SessionTurn, outcome: str) -> None:
        """SessionService.end_turn, as soon as the agent is done: read the turn's patch while the sandbox is up (a
        task of its own; the turn goes on), and what only the turn's end knows."""
        try:
            if not turn.run_id or not self.wanted(s):
                return
            key = run_key(s.id, turn.run_id)
            self._capturing.add(key)
            from hangar import observe

            self._extra[key] = {"traceparent": observe.current_traceparent()}
            if outcome != "gone" and s.sandbox_id and svc._alive(s.sandbox_id):
                n = _index(s, turn)
                t = asyncio.create_task(self._patch(svc, s.id, s.sandbox_id, n, turn.run_id, s.base_sha))
                self._patches[key] = t
        except Exception:  # noqa: BLE001
            log.warning("outbox: could not start capturing a turn of %s", s.id, exc_info=True)

    def session_turn(self, svc: SessionService, s: Session, turn: SessionTurn) -> None:
        """SessionService.end_turn, as it returns: a turn nothing else captures (a session's, a judge's)."""
        try:
            if not turn.run_id or not self.wanted(s) or turn.run_id in self._deferred:
                return
            self._spawn(self._capture(svc, s.model_copy(deep=True), _index(s, turn)))
        except Exception:  # noqa: BLE001
            log.warning("outbox: could not capture a turn of %s", s.id, exc_info=True)

    def job_turn(self, jobs: JobService, job: Job, turn: Turn, s: Session | None) -> None:
        """JobService._finish_turn, after the check and the delivery: `tests.json` holds the trusted check."""
        try:
            self._deferred.discard(turn.run_id or "")
            if not turn.run_id or s is None or not self.wanted(s):
                return
            if turn.outcome in ("gone", "error") and job.state.value not in _TERMINAL:
                self._forget(run_key(s.id, turn.run_id))  # cut off (power-down, restart): it resumes later
                return
            n = next((i for i, t in enumerate(s.turns, 1) if t.run_id == turn.run_id), None)
            if n is None:
                self._forget(run_key(s.id, turn.run_id))
                return
            self._spawn(self._capture(jobs.sessions, s.model_copy(deep=True), n, job=job.model_copy(deep=True),
                                      job_turn=next(i for i, t in enumerate(job.turns, 1) if t is turn
                                                    or t.run_id == turn.run_id)))
        except Exception:  # noqa: BLE001
            log.warning("outbox: could not capture job %s's turn", job.id, exc_info=True)

    def attempt_done(self, jobs: JobService, job: Job, a: Attempt) -> None:
        """strategies._run_attempt, once the attempt is checked (or failed)."""
        try:
            self._deferred.discard(a.run_id or "")
            if not a.run_id or not a.session_id or a.state == "waiting" or jobs.sessions is None:
                if a.run_id and a.session_id:
                    self._forget(run_key(a.session_id, a.run_id))
                return
            s = jobs.sessions.get(a.session_id)
            if not self.wanted(s):
                return
            n = next((i for i, t in enumerate(s.turns, 1) if t.run_id == a.run_id), None)
            if n is not None:
                self._spawn(self._capture(jobs.sessions, s.model_copy(deep=True), n, job=job.model_copy(deep=True),
                                          attempt=a.model_copy(deep=True)))
        except Exception:  # noqa: BLE001
            log.warning("outbox: could not capture an attempt of job %s", job.id, exc_info=True)

    def trial_graded(self, jobs: JobService, experiment: Job, t: Any) -> None:
        """experiments._grade: the trial's grade, under its job's last run (it may arrive after run.json)."""
        from hangar import testresults, traces

        try:
            if not self.enabled or not t.job or t.grade is None:
                return
            child = jobs.store.get(type(experiment), t.job)
            run = next((x for x in reversed(child.turns) if x.run_id), None) if child else None
            if child is None or run is None or not child.session_id:
                return
            s = jobs.sessions.get(child.session_id) if jobs.sessions else None
            st = next((x for x in s.turns if x.run_id == run.run_id), None) if s else None
            prefix = run_prefix(child.session_id, run.run_id, (st.finished_at or st.started_at) if st else None)
            grade = {"schema": "hangar.grade/v1", "experiment_id": experiment.id, "trial_key": t.key,
                     "job_id": child.id, "session_id": child.session_id, "run_id": run.run_id, **t.grade}
            if (tr := self.store.get(testresults.TestRun, t.grade.get("test_run") or "")) is not None:
                # --- #164: the grader's TestRun, as tests.json carries a turn's (`counted` included)
                grade["tests"] = {"runs": [testresults.view(tr, cases=True)],
                                  "events": [{"name": e["name"], "timeUnixNano": traces._ns(e["time"]),
                                              "attributes": traces._attrs(e["attributes"])}
                                             for e in testresults.trace_events(tr, limit=testresults.FAILING_KEPT)]}
            self._stage(run_key(child.session_id, run.run_id), prefix, {"grade.json": grade},
                        session_id=child.session_id, run_id=run.run_id, job_id=child.id)
            self._kick()
        except Exception:  # noqa: BLE001
            log.warning("outbox: could not stage a grade of %s", experiment.id, exc_info=True)

    def _forget(self, key: str) -> None:
        self._capturing.discard(key)
        self._extra.pop(key, None)
        if (p := self._patches.pop(key, None)) is not None:
            p.cancel()

    # ------------------------------------------------------------------ capture
    async def _patch(self, svc: SessionService, sid: str, sbid: str, n: int, run_id: str,
                     base_sha: str | None) -> dict[str, Any] | None:
        """The turn's work against the commit it started on (the manifest's `instructions.repo.commit`, else the
        session's base): committed, uncommitted and untracked, capped at HANGAR_EXCHANGE_MAX_PATCH_MB."""
        import shlex

        from hangar import exchange, manifest

        try:
            sb = svc.fleet.get(sbid)
            repo = sb.info.get("repo_path")
            if not repo:
                return None
            m = manifest.store(self.settings).get(sid, n) or {}
            start = ((m.get("instructions") or {}).get("repo") or {}).get("commit") or base_sha
            cap = self.settings.exchange_max_patch_mb * exchange.MB
            tmp = f"/tmp/hangar-outbox-{_SAFE.sub('_', run_id)}"
            base = shlex.quote(start) if start else "HEAD"
            script = (f"export GIT_OPTIONAL_LOCKS=0; mkdir -p {tmp} && "
                      + exchange.patch_script(base, f"{tmp}/full.patch", self.settings.session_max_untracked_mb
                                              * exchange.MB)
                      + f" && head -c {cap + 1} {tmp}/full.patch > {tmp}/out.patch && rm -f {tmp}/full.patch"
                      "; git rev-parse HEAD")
            res = await svc._sh(sb, script, cwd=repo, timeout=PATCH_SECONDS)
            _, prov = svc.fleet._running(sb.id)
            data = await prov.read_file(sb, f"{tmp}/out.patch", limit=cap + 1)
            with contextlib.suppress(Exception):
                await svc._sh(sb, f"rm -rf {tmp}", timeout=30)
            if data is None:
                return None
            head = (res.stdout.split() or [None])[-1]
            return {"text": data[:cap].decode(errors="replace"), "base": start, "head": head,
                    "truncated": len(data) > cap}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the run is captured without its patch
            log.info("outbox: no patch for %s/%s: %s", sid, run_id, e)
            return None

    async def _capture(self, svc: SessionService, s: Session, n: int, *, job: Job | None = None,
                       job_turn: int | None = None, attempt: Attempt | None = None) -> None:
        turn = s.turns[n - 1]
        key = run_key(s.id, turn.run_id or "")
        self._capturing.add(key)
        try:
            patch = None
            if (t := self._patches.pop(key, None)) is not None:
                with contextlib.suppress(Exception):
                    patch = await asyncio.wait_for(t, PATCH_WAIT)
            extra = self._extra.pop(key, {})
            files, record = await asyncio.to_thread(self.build, svc, s, n, job=job, job_turn=job_turn,
                                                    attempt=attempt, patch=patch, extra=extra)
            prefix = run_prefix(s.id, turn.run_id or "", turn.finished_at or turn.started_at)
            await asyncio.to_thread(self._stage, key, prefix, files, record=record, session_id=s.id,
                                    run_id=turn.run_id, job_id=job.id if job else s.job_id)
            self._kick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - backfill can capture it again from what's on disk
            log.warning("outbox: capturing %s failed", key, exc_info=True)
        finally:
            self._capturing.discard(key)

    def build(self, svc: SessionService, s: Session, n: int, *, job: Job | None = None, job_turn: int | None = None,
              attempt: Attempt | None = None, patch: dict[str, Any] | None = None,
              extra: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """The run's objects (name -> JSON-able or text) and its run.json, from what hangar has on disk."""
        from hangar import manifest, testresults, traces
        from hangar.exchange import diff_stat

        turn = s.turns[n - 1]
        files: dict[str, Any] = {}
        m = manifest.store(self.settings).get(s.id, n)
        if m is not None:
            files["manifest.json"] = m
        trace = traces.session_export(svc, s, turn=n)
        files["trace.otlp.json"] = trace
        runs = self._test_runs(s, n, job, job_turn, attempt)
        if runs:
            events = []
            for r in sorted(runs, key=lambda r: r.at):
                for e in testresults.trace_events(r, limit=testresults.FAILING_KEPT):
                    events.append({"name": e["name"], "timeUnixNano": traces._ns(e["time"]),
                                   "attributes": traces._attrs(e["attributes"])})
            files["tests.json"] = {"schema": TESTS_SCHEMA, "run_id": turn.run_id, "session_id": s.id,
                                   "job_id": job.id if job else s.job_id, "turn": n,
                                   "runs": [testresults.view(r, cases=True) for r in runs], "events": events}
        if patch is not None:
            files["patch.diff"] = patch["text"]
        rec = self.run_record(s, n, job=job, job_turn=job_turn, attempt=attempt, manifest_doc=m, runs=runs,
                              patch=patch, extra=extra or {}, trace=trace)
        if patch is not None and not rec.get("diff_files"):
            rec["diff_files"] = [f["path"] for f in diff_stat(patch["text"])][:DIFF_FILES]
        return files, rec

    def _test_runs(self, s: Session, n: int, job: Job | None, job_turn: int | None,
                   attempt: Attempt | None) -> list[Any]:
        from hangar import testresults

        if attempt is not None:
            return [r for r in testresults.runs_of(self.store, session_id=s.id) if r.attempt == attempt.index]
        if job is not None and job_turn is not None:
            return [r for r in testresults.runs_of(self.store, job_id=job.id)
                    if r.turn == job_turn and r.attempt is None and r.kind in ("verify", "rerun", "in_turn")]
        return testresults.turn_runs(self.store, s, n)  # --- #164: by the run's id (older runs: the turn's number)

    def run_record(self, s: Session, n: int, *, job: Job | None, job_turn: int | None, attempt: Attempt | None,
                   manifest_doc: dict[str, Any] | None, runs: list[Any], patch: dict[str, Any] | None,
                   extra: dict[str, Any], trace: dict[str, Any] | None = None) -> dict[str, Any]:
        """`hangar.run/v1` (docs/api/run.schema.json)."""
        from hangar.events import total_usage
        from hangar.traces import hangar_version

        turn = s.turns[n - 1]
        m = manifest_doc or {}
        exp = _experiment(job, self.store)
        source = "experiment" if exp else ("job" if (job is not None or s.job_id) else "session")
        u = total_usage([turn])
        jt = job.turns[job_turn - 1] if job is not None and job_turn else None
        last = job is not None and jt is not None and job_turn == len(job.turns)
        verify: dict[str, Any] | None = None
        if attempt is not None:
            verify = {"command": job.verify if job else None, "exit_code": attempt.verify_exit_code,
                      "passed": attempt.verify_exit_code == 0 if attempt.verify_exit_code is not None else None,
                      "new_failures": attempt.new_failures, "rounds": None, "trusted": True}
        elif job is not None and job.verify:
            code = job.result.verify_exit_code if last else None
            if code is None:  # a turn a verify-fix turn followed: its own check's record
                ran = [r for r in runs if r.kind in ("verify", "rerun")]
                code = max(ran, key=lambda r: r.at).exit_code if ran else None
            verify = {"command": job.verify, "exit_code": code, "passed": code == 0 if code is not None else None,
                      "new_failures": job.result.new_failures if last else None,
                      "rounds": jt.verify_rounds if jt else None, "trusted": True}
        by_class: dict[str, int] = {}
        for x in turn.steers:
            if c := getattr(x, "classification", None) or getattr(x, "steer_class", None):  # #162
                by_class[c] = by_class.get(c, 0) + 1
        labels = {**s.labels, **(job.labels if job else {})}
        truncated = False
        if trace:
            for rs in trace.get("resourceSpans", []):
                for a in rs.get("resource", {}).get("attributes", []):
                    if a.get("key") == "hangar.export.truncated":
                        truncated = bool(a.get("value", {}).get("boolValue"))
        rec: dict[str, Any] = {
            "schema": SCHEMA,
            "source": source,
            "run_id": turn.run_id,
            "session_id": s.id,
            "session_name": s.name,
            "turn": n,
            "job_id": job.id if job else s.job_id,
            "job_source": job.source if job else None,
            "job_turn": job_turn,
            "attempt": attempt.index if attempt else None,
            "person": s.person or (job.person if job else None),
            "repo": (job.repo if job else None) or s.repo,
            "environment": s.environment,
            "base_sha": s.base_sha,
            "start_sha": (patch or {}).get("base") or ((m.get("instructions") or {}).get("repo") or {}).get("commit"),
            "head_sha": (patch or {}).get("head") or (job.result.head_sha if last and job else None)
            or (attempt.head_sha if attempt else None),
            "harness": turn.harness or s.harness,
            "harness_version": turn.harness_version,
            "model": (u.model if u and u.model else None) or s.model,
            "harness_digest": turn.harness_digest or m.get("harness_digest"),
            "environment_digest": turn.environment_digest or m.get("environment_digest"),
            "config_digest": m.get("config_digest"),
            "manifest_digest": turn.manifest or m.get("digest"),
            "pinned": exp is not None,
            "pin_set": f"{exp['id']}/{exp['variant']}" if exp and exp.get("variant") else None,
            "experiment": exp,
            "outcome": (jt.outcome if jt else None) or turn.outcome,
            "outcome_reason": (jt.outcome_reason if jt else None) or turn.outcome_reason,
            "outcome_class": getattr(jt, "outcome_class", None) or getattr(turn, "outcome_class", None),  # #166
            "infra_reason": getattr(jt, "infra_reason", None) or getattr(turn, "infra_reason", None),
            "exit_code": turn.exit_code,
            "job_state": job.state.value if job else None,
            "job_reason": job.reason if job else None,
            "started_at": turn.started_at,
            "finished_at": turn.finished_at,
            "usage": u.model_dump(mode="json", include={"input_tokens", "output_tokens", "cache_read_tokens",
                                                        "cache_write_tokens", "cost_usd", "cost_source", "model"})
            if u else None,
            "verify": verify,
            "tests": [r.id for r in runs],
            "steers": {"n": len(turn.steers), "by_class": by_class},
            "approvals": len(turn.approvals),
            "questions": 1 if turn.question else 0,
            "labels": labels,
            "traceparent": extra.get("traceparent"),
            "untrusted": bool(job.untrusted_source) if job else False,
            "tainted": bool((turn.taint or {}).get("tainted")),
            "shadow_of": None,  # #183
            "pr": {"url": job.result.pr_url, "number": job.result.pr_number} if job and job.result.pr_url else None,
            "diff_files": list(job.result.files)[:DIFF_FILES] if last and job and job.result.files else None,
            "patch": {"base": patch["base"], "bytes": len(patch["text"].encode()), "truncated": patch["truncated"]}
            if patch else None,
            "truncated": truncated,
            "expects": ["grade.json"] if exp and exp.get("graded") else [],
            "hangar_version": hangar_version(),
            "captured_at": now(),
        }
        return rec

    # ------------------------------------------------------------------ staging
    def _scrub(self, obj: Any) -> Any:
        from hangar import secretscan

        broker = self.fleet.broker
        if isinstance(obj, str):
            return broker.redact(secretscan.mask(obj, broker)[0]) if obj else obj
        if isinstance(obj, dict):
            return {k: self._scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._scrub(v) for v in obj]
        return obj

    def encode(self, payload: Any) -> bytes:
        """Redacted bytes of one object, or Unredacted: stored credentials (and their encodings) and the token
        formats secretscan knows, out of every string, then the whole text checked again."""
        from hangar import secretscan

        clean = self._scrub(payload)
        text = clean if isinstance(clean, str) else json.dumps(clean, ensure_ascii=False, default=str)
        if left := secretscan.find(text, self.fleet.broker):
            raise Unredacted(f"still holds a secret after redaction ({', '.join(sorted({k for *_, k in left}))})")
        return text.encode()

    def _blob(self, key: str) -> Path:
        return self.root / "blobs" / (hashlib.sha256(key.encode()).hexdigest()[:40] + ".gz")

    def _stage(self, run: str, prefix: str, files: dict[str, Any], record: dict[str, Any] | None = None, *,
               session_id: str | None = None, run_id: str | None = None, job_id: str | None = None) -> list[str]:
        """Redact, gzip and queue each object; run.json last, waiting for the rest. An object already queued is
        left alone (write-once). Returns the keys queued now."""
        queued: list[str] = []
        withheld: list[str] = []
        pending: list[str] = []
        for name, payload in files.items():
            key = prefix + name
            if self.store.get(OutboxItem, key) is not None:
                continue
            item = OutboxItem(key=key, kind=name, run=run, session_id=session_id, run_id=run_id, job_id=job_id)
            try:
                data = gzip.compress(self.encode(payload), mtime=0)
            except Exception as e:  # noqa: BLE001 - never uploaded unredacted
                item.state, item.error = "failed", f"redaction: {str(e)[:300]}"
                withheld.append(name)
                log.warning("outbox: %s withheld: %s", key, item.error)
            else:
                self._write(key, data)
                item.bytes = len(data)
                pending.append(key)
            self.store.put(item)
            queued.append(key)
        if record is not None and self.store.get(OutboxItem, prefix + "run.json") is None:
            record["objects"] = sorted(n for n in files if n not in withheld)
            record["withheld"] = sorted(withheld)
            key = prefix + "run.json"
            item = OutboxItem(key=key, kind="run.json", run=run, session_id=session_id, run_id=run_id,
                              job_id=job_id, after=pending)
            try:
                data = gzip.compress(self.encode(record), mtime=0)
            except Exception as e:  # noqa: BLE001
                item.state, item.error = "failed", f"redaction: {str(e)[:300]}"
            else:
                self._write(key, data)
                item.bytes = len(data)
            self.store.put(item)
            queued.append(key)
        return queued

    def _write(self, key: str, data: bytes) -> None:
        p = self._blob(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    # ------------------------------------------------------------------ job events
    def job_event(self, job: Job, kind: str, ts: float | None = None, data: dict[str, Any] | None = None,
                  once: str | None = None) -> str | None:
        """An immutable event object for a fact about a job that arrives after its runs (a PR opened, its first
        review, merged, closed, reverted, an abandon, a failure label). `once`: emitted at most once per job for
        this mark. Returns the key, None when off or already emitted."""
        if not self.enabled or kind not in EVENT_KINDS:
            return None
        mark = f"{KV_EVENT}{job.id}:{once or kind}"
        if self.store.kv_get(mark) is not None:
            return None
        ts = ts or now()
        key = f"{PREFIX}/jobs/{day(ts)}/{_SAFE.sub('_', job.id)}/events/{int(ts * 1000)}-{kind}.json"
        ev = {"schema": EVENT_SCHEMA, "kind": kind, "ts": ts, "job_id": job.id, "job_source": job.source,
              "source": "experiment" if _experiment(job) else "job", "repo": job.repo, "person": job.person,
              "environment": job.environment, "session_id": job.session_id,
              "run_ids": [t.run_id for t in job.turns if t.run_id],
              "pr": {"url": job.result.pr_url, "number": job.result.pr_number} if job.result.pr_url else None,
              "untrusted": bool(job.untrusted_source), "data": data or {}}
        if self.store.get(OutboxItem, key) is None:
            item = OutboxItem(key=key, kind="event", run=f"job:{job.id}", session_id=None, job_id=job.id)
            try:
                data = gzip.compress(self.encode(ev), mtime=0)
            except Exception as e:  # noqa: BLE001 - never uploaded unredacted
                item.state, item.error = "failed", f"redaction: {str(e)[:300]}"
            else:
                self._write(key, data)
                item.bytes = len(data)
            self.store.put(item)
        self.store.kv_set(mark, key, durable=True)
        self._kick()
        return key

    def lifecycle(self, job: Job) -> list[tuple[str, float | None, dict[str, Any], str]]:
        """(kind, when, data, once-mark) of the facts a job shows now."""
        land, r = job.landing, job.result
        out: list[tuple[str, float | None, dict[str, Any], str]] = []
        if r.pr_url:
            opened = next((e.ts for e in job.events if r.pr_url in (e.message or "")), None)
            out.append(("pr.open", opened, {"draft": land.draft}, "pr.open"))
        if land.ready_at:  # --- #168: review wait starts here
            out.append(("pr.ready", land.ready_at, {}, "pr.ready"))
        if land.approved_at:
            out.append(("review.first", land.approved_at, {"by": land.approved_by, "sha": land.approved_sha,
                                                           # --- #168: the first human review, any state
                                                           "first_review_at": land.first_review_at,
                                                           "first_review_by": land.first_review_by},
                        "review.first"))
        if land.merged_at:
            out.append(("pr.merge", land.merged_at, {"by": land.merged_by,
                                                     "green_to_merge_seconds": land.green_to_merge_seconds},
                        "pr.merge"))
        if land.pr_state == "closed" and land.closed_at:
            out.append(("pr.close", land.closed_at, {}, f"pr.close:{int(land.closed_at)}"))
        if job.state == JobState.CANCELLED:
            out.append(("abandon", job.updated_at, {"reason": job.reason or "cancelled"}, "abandon"))
        return out

    def scan_jobs(self) -> int:
        """Jobs changed since the last scan: an event object for each new lifecycle fact. Idempotent."""
        from hangar.models import Job
        from hangar.store import Ge

        if not self.enabled:
            return 0
        since = float(self.store.kv_get(KV_SCAN) or self.store.kv_get(KV_SINCE) or now())
        t = now()
        n = 0
        for job in self.store.list_where(Job, updated_at=Ge(since - 120)):
            for kind, ts, data, once in self.lifecycle(job):
                if self.job_event(job, kind, ts, data, once=once):
                    n += 1
        self.store.kv_set(KV_SCAN, str(t))
        return n

    # ------------------------------------------------------------------ draining
    async def drain_once(self) -> dict[str, int]:
        """Upload what is due; returns {"done", "retry", "failed"}."""
        out = {"done": 0, "retry": 0, "failed": 0}
        if self.sink is None:
            return out
        t = now()
        rows = await asyncio.to_thread(self.store.list_where, OutboxItem, state="pending")
        due = sorted((i for i in rows if i.next_at <= t), key=lambda i: (i.kind == "run.json", i.created_at))
        ready: list[OutboxItem] = []
        for i in due:
            if i.after:
                deps = [self.store.get(OutboxItem, k) for k in i.after]
                if any(d is not None and d.state == "pending" for d in deps):
                    continue
            ready.append(i)
            if len(ready) >= BATCH:
                break
        sem = asyncio.Semaphore(UPLOADS)

        async def one(item: OutboxItem) -> None:
            async with sem:
                out[await self._upload(item)] += 1

        await asyncio.gather(*(one(i) for i in ready))
        return out

    async def _upload(self, item: OutboxItem) -> str:
        assert self.sink is not None
        p = self._blob(item.key)
        item.attempts += 1
        item.updated_at = now()
        try:
            data = await asyncio.to_thread(p.read_bytes)
        except OSError:
            item.state, item.error = "failed", "its staged payload is gone"
            self.store.put(item)
            return "failed"
        ctype = TYPES.get(os.path.splitext(item.key)[1], "application/octet-stream")
        try:
            item.created = await self.sink.put_once(item.key, data, ctype)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - try again later, for as long as it takes
            item.next_at = now() + backoff(item.attempts)
            item.error = str(e)[:300] or type(e).__name__
            self.store.put(item)
            return "retry"
        item.state, item.error, item.done_at = "done", None, now()
        self.store.put(item)
        with contextlib.suppress(OSError):
            p.unlink()
        return "done"

    async def _run(self) -> None:
        self._wake, self._wake_loop = asyncio.Event(), asyncio.get_running_loop()
        last_scan = last_sweep = 0.0
        while True:
            full = False
            try:
                got = await self.drain_once()
                full = sum(got.values()) >= BATCH
                if time.monotonic() - last_scan >= SCAN_EVERY:
                    last_scan = time.monotonic()
                    await asyncio.to_thread(self.scan_jobs)
                if time.monotonic() - last_sweep >= SWEEP_EVERY:
                    last_sweep = time.monotonic()
                    await asyncio.to_thread(self.sweep_pending_delete)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - next time round
                log.exception("outbox pass failed")
            if full:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), DRAIN_EVERY)
            self._wake.clear()

    # ------------------------------------------------------------------ what is left, and retrying it
    def status(self) -> dict[str, Any]:
        rows = self.store.list_where(OutboxItem, state=("pending", "failed"))
        pend = [i for i in rows if i.state == "pending"]
        failed = [i for i in rows if i.state == "failed"]
        counts = {str(k[0]): v for k, v in self.store.count_by(OutboxItem, "state").items()}
        oldest = min((i.created_at for i in pend), default=None)
        return {"enabled": self.enabled, "sink": self.description, "counts": counts, "pending": len(pend),
                "pending_bytes": sum(i.bytes for i in pend), "oldest_pending_at": oldest,
                "oldest_pending_age_seconds": (now() - oldest) if oldest else None,
                "next_retry_at": min((i.next_at for i in pend), default=None),
                "last_error": next((i.error for i in sorted(pend, key=lambda i: -i.updated_at) if i.error), None),
                "failed": [{"key": i.key, "error": i.error} for i in failed[:20]], "failed_count": len(failed),
                "pending_delete": self.pending_delete_status()}

    def retry(self) -> int:
        """Every pending item due now (after an outage, without waiting out the backoff)."""
        n = 0
        for i in self.store.list_where(OutboxItem, state="pending"):
            i.next_at, i.updated_at = now(), now()
            self.store.put(i)
            n += 1
        self._kick()
        return n

    async def backfill(self, since_seconds: float) -> dict[str, int]:
        """Runs finished in the window with nothing queued, captured again from what is still on disk (manifests,
        event logs, test runs; no patch unless the sandbox is up)."""
        from hangar.models import Job, Session
        from hangar.store import Ge

        out = {"runs": 0, "skipped": 0}
        if not self.enabled or self.sessions is None:
            return out
        lo = now() - since_seconds
        svc = self.sessions
        for s in self.store.list_where(Session, archived=True, updated_at=Ge(lo)):
            if not self.wanted(s):
                continue
            job = self.store.get(Job, s.job_id) if s.job_id else None
            for n, t in enumerate(s.turns, 1):
                if not t.run_id or not t.finished_at or t.finished_at < lo:
                    continue
                key = run_key(s.id, t.run_id)
                if key in self._capturing or self.store.list_where(OutboxItem, run=key):
                    out["skipped"] += 1
                    continue
                jt = next((i for i, x in enumerate(job.turns, 1) if x.run_id == t.run_id), None) if job else None
                att = next((a for a in job.attempts if a.run_id == t.run_id), None) if job else None
                await self._capture(svc, s.model_copy(deep=True), n, job=job if (jt or att) else None,
                                    job_turn=jt, attempt=att)
                out["runs"] += 1
        return out

    # ------------------------------------------------------------------ #189: exported, pending delete
    def exported(self, session_id: str, run_id: str | None = None) -> bool:
        """Whether every object of the run (of every run of the session, without `run_id`) is uploaded: nothing
        being built, nothing pending or failed. True when the outbox is off, and for a run with nothing queued (it
        ran before the outbox was on, or `jobs_only` leaves it out: nothing will ever export it)."""
        if not self.enabled:
            return True
        if run_id is None:
            if any(k.startswith(session_id + ".") for k in self._capturing):
                return False
            return all(i.state == "done" for i in self.store.list_where(OutboxItem, session_id=session_id))
        key = run_key(session_id, run_id)
        return key not in self._capturing and all(i.state == "done"
                                                  for i in self.store.list_where(OutboxItem, run=key))

    def session_exported(self, s: Session) -> bool:
        return all(self.exported(s.id, t.run_id) for t in s.turns if t.run_id and t.started_at)

    def retire_session(self, s: Session, svc: SessionService, requester: str, force_now: bool = False) -> bool:
        """SessionService.delete: its manifests and event log go now when its runs are exported (or the outbox is
        off, or an admin forces it); else they move to `<data>/pending-delete/<session>/` until they are. Returns
        whether they were deleted now."""
        from hangar import manifest

        mstore = manifest.store(self.settings)
        if force_now or self.session_exported(s):
            mstore.drop(s.id)
            svc.activity.log.drop(s.id)
            if force_now and not self.session_exported(s):
                self.fleet.audit(requester, "retention.unexported_deleted", s.id, forced=True,
                                 runs=[t.run_id for t in s.turns if t.run_id])
            return True
        d = self.settings.data_dir / PENDING_DELETE / _SAFE.sub("_", s.id)
        (d / "events").mkdir(parents=True, exist_ok=True)
        src = mstore._dir(s.id)
        if src.exists():
            shutil.rmtree(d / "manifests", ignore_errors=True)
            shutil.move(str(src), str(d / "manifests"))
        for p in svc.activity.log._paths(s.id):
            if p.exists():
                shutil.move(str(p), str(d / "events" / p.name))
        svc.activity.log._last.pop(s.id, None)
        (d / "marker.json").write_text(json.dumps({"session_id": s.id, "name": s.name, "deleted_at": now(),
                                                   "requester": requester,
                                                   "runs": [t.run_id for t in s.turns if t.run_id and t.started_at]}))
        log.info("session %s deleted; its event log and manifests wait for the outbox", s.id)
        return False

    def sweep_pending_delete(self) -> dict[str, int]:
        """Deleted sessions' files: gone once exported, or after HANGAR_EXPORT_GRACE_DAYS (audited)."""
        out = {"deleted": 0, "expired": 0, "held": 0}
        root = self.settings.data_dir / PENDING_DELETE
        if not root.is_dir():
            return out
        grace = self.settings.export_grace_days * 86400
        for d in sorted(root.iterdir()):
            try:
                mk = json.loads((d / "marker.json").read_text())
            except (OSError, ValueError):
                continue
            sid, runs = mk.get("session_id") or d.name, mk.get("runs") or []
            done = all(self.exported(sid, r) for r in runs)
            if done:
                shutil.rmtree(d, ignore_errors=True)
                out["deleted"] += 1
            elif now() - float(mk.get("deleted_at") or 0) > grace:
                size = _du(d)
                shutil.rmtree(d, ignore_errors=True)
                self.fleet.audit("retention", "retention.unexported_deleted", sid, bytes=size,
                                 grace_days=self.settings.export_grace_days,
                                 runs=[r for r in runs if not self.exported(sid, r)])
                log.warning("session %s: deleted its event log and manifests unexported after %s days", sid,
                            self.settings.export_grace_days)
                out["expired"] += 1
            else:
                out["held"] += 1
        return out

    def pending_delete_status(self) -> dict[str, Any]:
        root = self.settings.data_dir / PENDING_DELETE
        dirs = [d for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
        return {"sessions": len(dirs), "bytes": sum(_du(d) for d in dirs)}

    def retain_test_run(self, r: Any) -> bool:
        """testresults.prune: keep a run of a failed or abandoned job until its runs are exported."""
        if not r.job_id:
            return False
        from hangar.models import Job

        job = self.store.get(Job, r.job_id)
        if job is None or not failure(job):
            return False
        sid = r.session_id or job.session_id
        return not (sid and self.exported(sid))


def failure(job: Job) -> bool:
    """A job whose runs retention keeps until exported: failed, cancelled (abandoned), its PR closed unmerged, or
    labelled in the harness review (#169, `job.failure_label`)."""
    return (job.state in (JobState.FAILED, JobState.CANCELLED) or job.landing.pr_state == "closed"
            or job.failure_label is not None)


_TERMINAL = {"succeeded", "failed", "cancelled"}


def backoff(attempts: int) -> float:
    """1 minute, doubling, at most 6 hours."""
    return min(BACKOFF_MAX, BACKOFF_MIN * 2 ** max(0, attempts - 1))


def _index(s: Session, turn: SessionTurn) -> int:
    return next((i for i, t in enumerate(s.turns, 1) if t is turn or (turn.run_id and t.run_id == turn.run_id)),
                len(s.turns))


def _experiment(job: Job | None, store: Any = None) -> dict[str, Any] | None:
    """An experiment trial's identity, from its job's labels (experiments._start_trial); None for other jobs.
    `graded`: the experiment has a grader, so a grade.json follows."""
    if job is None:
        return None
    from hangar import experiments

    exp = job.labels.get(experiments.EXP_LABEL)
    if not exp and job.source != "experiment":
        return None
    key = job.labels.get(experiments.TRIAL_LABEL) or ""
    task, _, rest = key.partition("/")
    variant, _, trial = rest.partition("/")
    graded = False
    if exp and store is not None:
        with contextlib.suppress(Exception):
            from hangar.models import Job as JobModel

            parent = store.get(JobModel, exp)
            graded = bool(parent is not None and (experiments.load(parent).spec or {}).get("grader"))
    return {"id": exp, "trial_key": key or None, "task": task or None,
            "variant": job.labels.get(experiments.VARIANT_LABEL) or variant or None,
            "trial": int(trial) if trial.isdigit() else None, "graded": graded}


def _du(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        with contextlib.suppress(OSError):
            if p.is_file():
                total += p.stat().st_size
    return total


def parse_since(text: str) -> float:
    """`7d`, `12h`, `30m` or seconds, as seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", text or "")
    if m is None:
        raise ValueError(f"'{text}' isn't a duration (7d, 12h, 30m)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def doctor_checks(h: Any) -> list[dict[str, Any]]:
    """`hangar doctor`: past WARN_PENDING items or WARN_AGE of age, failed (withheld) objects, and the bytes
    deleted sessions hold only because their export is pending (#189)."""
    from hangar.doctor import _check

    o = of(h.fleet)
    if not o.enabled:
        return []
    st = o.status()
    out = []
    age = st["oldest_pending_age_seconds"] or 0
    slow = st["pending"] > WARN_PENDING or age > WARN_AGE
    out.append(_check("run capture (outbox)", None if slow else True,
                      f"{st['pending']} object(s) pending to {st['sink']}"
                      + (f", oldest {round(age / 3600, 1)} h" if age else "")
                      + (f"; last error: {st['last_error']}" if st["last_error"] and slow else ""),
                      "check the bucket and hangar's service account (roles/storage.objectCreator on it, "
                      "deploy/terraform/gke), then `hangar outbox retry`"))
    if st["failed_count"]:
        out.append(_check("run capture: withheld objects", None,
                          f"{st['failed_count']} object(s) held back because they still held a secret after "
                          f"redaction (first: {st['failed'][0]['key']})",
                          "nothing to fix in the outbox: they are never uploaded; `hangar outbox status` lists them"))
    pd = st["pending_delete"]
    if pd["sessions"]:
        out.append(_check("retention held for export", None,
                          f"{pd['sessions']} deleted session(s) keep {round(pd['bytes'] / 2**20, 1)} MiB of event "
                          f"logs and manifests until the outbox exports them (at most "
                          f"{h.settings.export_grace_days:g} days)",
                          "let the outbox drain (`hangar outbox status`), or `hangar session delete --force-now`"))
    return out


def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/admin/outbox; POST /api/admin/outbox/retry; POST /api/admin/outbox/backfill {"since": "7d"}."""
    from fastapi import Body, HTTPException

    @app.get("/api/admin/outbox")
    async def outbox_status(_: str = admin):
        return await asyncio.to_thread(of(h.fleet).status)

    @app.post("/api/admin/outbox/retry")
    async def outbox_retry(who: str = admin):
        n = of(h.fleet).retry()
        h.fleet.audit(who, "outbox.retry", None, items=n)
        return {"retried": n}

    @app.post("/api/admin/outbox/backfill")
    async def outbox_backfill(body: dict[str, Any] = Body(default={}), who: str = admin):
        o = of(h.fleet)
        if not o.enabled:
            raise HTTPException(409, "the outbox is off (set HANGAR_OUTBOX_URL)")
        try:
            since = parse_since(str(body.get("since") or "7d"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        got = await o.backfill(since)
        h.fleet.audit(who, "outbox.backfill", None, since_seconds=since, **got)
        return got


def of(fleet: Fleet) -> Outbox:
    """The fleet's outbox (made on first use, so Fleet itself needs no change)."""
    o = fleet.__dict__.get("_outbox")
    if o is None:
        o = fleet.__dict__["_outbox"] = Outbox(fleet)
    return o
