"""Flight records (--- clouddoctor): what a failed or slow job or turn leaves behind (docs/observability.md).

After a failure the evidence was scattered or gone: job events are truncated, the sandbox is kept two hours, and the
run log dies with it. So one record is written for each job that fails or times out, each session turn outside a job
that does, and each job or turn that ran slow against its environment's norm (SLOW_FACTOR times the median of its
last clean runs). A record holds:

* the sandbox's create stages (reconciler.note_stage), with the stage and cause it failed at;
* the end of the turn's run log, redacted (stored credentials and their encodings, and the token formats secretscan
  knows), sealed with the vault key like a checkpoint part, and deleted after RETENTION_DAYS. It is read, redacted
  and written only after the session is idle, on a task of its own: a record never holds up a turn or a job;
* what `hangar-agent env doctor` sees (envdoctor.build_status): the build and whether it is a fallback, setup
  failures, services, the secret names the environment expects, the egress mode;
* the sandbox's egress denies (the audit log) and the requests its agent filed that nobody answered;
* versions (hangar, the harness CLI the turn ran, the model, the image and packs), cost, and a cause: one of
  CAUSES, decided without a model, and whether that cause looks transient.

Admins list them (`hangar admin records`, `GET /api/records`); a job's own person reads its record too
(`GET /api/jobs/{id}/record`, `hangar://jobs/<id>/record`). The log is a separate read (`GET /api/records/{id}/log`).
The Cloud Doctor (clouddoctor.py) reads them every night.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import gzip
import hashlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from hangar.models import JobState, new_id, now

if TYPE_CHECKING:
    from hangar.fleet import Fleet
    from hangar.jobs import JobService
    from hangar.models import Job, Sandbox, Session, SessionTurn
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.flightrec")

RETENTION_DAYS = 14
LOG_TAIL = 256 * 1024  # characters of the run log's end a record keeps
PENDING_MAX = 64  # job turns whose log tail waits for its job to end (at most PENDING_MAX * LOG_TAIL in memory)
PENDING_TTL = 3600.0
NORM_KEEP = 50  # clean runs per environment the norm is taken from
NORM_MIN = 8  # fewer than this and nothing counts as slow yet
SLOW_FACTOR = 3.0
SLOW_FLOOR = 120.0  # and at least this many seconds over the median
MAX_DENIES = 20
MAX_EVENTS = 20
BLOB_PREFIX = "records/"

# Causes, decided without a model, most specific first. Transient ones are infrastructure blips a retry fixes; the
# Cloud Doctor counts them and looks into them only when one keeps coming back.
TRANSIENT = frozenset({"model_api", "power_down", "restart", "pod_replaced", "sandbox_gone", "network"})
CAUSES = ("model_api", "power_down", "restart", "pod_replaced", "sandbox_gone", "network", "provision",
          "image_build", "install", "missing_tool", "egress", "secret", "blocked", "verify_failed", "timed_out",
          "over_budget", "login_rejected", "agent_failed", "push_failed", "secret_found", "bad_request", "slow",
          "error")  # --- KAN-178: login_rejected: the vendor refused the turn's login

_API = re.compile(
    r"overloaded_error|rate_limit_error|\"type\"\s*:\s*\"api_error\"|API Error:? (?:429|5\d\d)\b|"
    r"api_error_status\"?\s*:\s*(?:429|5\d\d)\b|exceeded retry limit|stream disconnected before completion|"
    r"(?:529|503) (?:Service Unavailable|Overloaded)", re.I)
_NETWORK = re.compile(r"Could not resolve host|Connection (?:refused|reset|timed out)|Temporary failure in name "
                      r"resolution|early EOF|The remote end hung up unexpectedly", re.I)
# A command or module the agent's tools needed and the sandbox didn't have
_MISSING = (
    re.compile(r"(?:^|[\s:])(?:/bin/|/usr/bin/)?(?:ba|z)?sh(?:: line \d+)?: (?:\d+: )?([A-Za-z0-9_.+-]{1,64}): "
               r"(?:command )?not found", re.M),
    re.compile(r"\b([A-Za-z0-9_.+-]{1,64}): command not found"),
    re.compile(r"env: '?([A-Za-z0-9_.+-]{1,64})'?: No such file or directory"),
    re.compile(r"No module named '([A-Za-z0-9_.]{1,100})'"),
    re.compile(r"Cannot find module '([^'\s]{1,100})'"),
    re.compile(r"exec: \"([A-Za-z0-9_.+-]{1,64})\": executable file not found"),
)


class FlightRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("fr"))
    kind: Literal["job", "turn"]
    at: float = Field(default_factory=now)
    trigger: Literal["failed", "timed_out", "slow"]
    cause: str
    detail: str | None = None  # the tool, host, secret, stage or check the cause is about
    transient: bool = False
    signature: str = ""  # cause, detail and repo (or environment): the Cloud Doctor groups records by it
    environment: str
    repo: str | None = None
    harness: str | None = None
    harness_version: str | None = None
    model: str | None = None
    person: str | None = None
    job_id: str | None = None
    session_id: str | None = None
    session: str | None = None
    sandbox_id: str | None = None
    run_ids: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)  # the job's (the Cloud Doctor skips its own jobs)
    outcome: str | None = None  # the turn's outcome, or the job's state
    reason: str | None = None  # the job's reason (verify_failed, timed_out, ...)
    exit_code: int | None = None
    seconds: float | None = None
    norm: dict[str, Any] | None = None  # {p50, samples}: what this environment usually takes
    stages: list[Any] = Field(default_factory=list)
    failed_stage: str | None = None
    sandbox_cause: str | None = None
    env_doctor: dict[str, Any] | None = None
    egress_denies: list[dict[str, Any]] = Field(default_factory=list)
    requests: list[dict[str, Any]] = Field(default_factory=list)  # pending or denied, from the agent
    versions: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float | None = None
    tokens: int | None = None
    verify: dict[str, Any] | None = None  # {command, exit_code, tail}
    events: list[str] = Field(default_factory=list)
    summary: str | None = None
    log: dict[str, Any] | None = None  # {key, bytes, sha256}: the sealed run-log tail
    log_note: str | None = None  # why there is no log


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("hangar")
    except PackageNotFoundError:
        return "unknown"


# ====================================================================== the cause
def classify(*, texts: list[str], reason: str | None, outcome: str | None, sandbox_cause: str | None,
             failed_stage: str | None, env_doctor: dict[str, Any] | None, denies: list[dict[str, Any]],
             requests: list[dict[str, Any]], powered_down: bool, restarted: bool, slow: bool,
             verify_tail: str | None) -> tuple[str, str | None]:
    """(cause, detail) for a record, from what is known without a model. Infrastructure first (a pod replaced at
    02:00 explains the failure better than the "command not found" its retry printed), then the environment, then
    what the job's reason says."""
    blob = "\n".join(t for t in texts if t)
    if powered_down:
        return "power_down", None
    if sandbox_cause == "pod_replaced":
        return "pod_replaced", failed_stage
    if restarted or sandbox_cause == "orphaned_by_restart":
        return "restart", failed_stage
    if m := _API.search(blob):
        return "model_api", m.group(0)[:80]
    if reason == "error" and "failed to create sandbox" in blob:
        stage = failed_stage or ""
        return ("image_build" if "build" in stage else "provision"), stage[:200] or None
    build = (env_doctor or {}).get("build") or {}
    fails = (env_doctor or {}).get("setup_failures") or []
    if fails:
        f = fails[-1]
        return "install", str(f.get("step") or f.get("name") or f.get("command") or f.get("message") or "")[:200]
    if isinstance(build, dict) and build.get("fallback"):
        return "install", str(build.get("reason") or "the repo's build failed; the agent ran on the base image")[:200]
    for text in (verify_tail or "", blob):
        for rx in _MISSING:
            if m := rx.search(text):
                return "missing_tool", m.group(1)
    if denies:
        hosts = collections.Counter(d.get("host") for d in denies if d.get("host"))
        if hosts:
            return "egress", hosts.most_common(1)[0][0]
    asked = [r for r in requests if r.get("kind") == "secret"]
    if asked:
        return "secret", asked[0].get("target")
    if any(r.get("state") == "pending" for r in requests):
        return "blocked", next(r.get("target") for r in requests if r.get("state") == "pending")
    if reason == "sandbox_gone" or outcome == "gone":
        return "sandbox_gone", failed_stage
    if reason == "push_failed" and _NETWORK.search(blob):
        return "network", None
    if reason in ("verify_failed", "timed_out", "over_budget", "login_rejected", "agent_failed", "push_failed",
                  "secret_found"):
        return reason, None
    if reason == "bad_branch":
        return "bad_request", None
    if outcome in ("timed_out", "over_budget"):
        return outcome, None
    if slow:
        return "slow", None
    return "error", None


def signature(cause: str, detail: str | None, repo: str | None, environment: str) -> str:
    """What makes two failures the same one: the cause, its detail without numbers that change between runs (ports,
    line numbers, durations), and the repo (or the environment when there's no repo)."""
    d = re.sub(r"\d+", "#", (detail or "").strip().lower())[:120]
    return f"{cause}:{d}@{repo or 'env:' + environment}"


# ====================================================================== the recorder
class Recorder:
    """One per fleet (`of`). Turns hand over their run log as they end; jobs, their outcome when they're reported."""

    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self.store = fleet.store
        self.settings = fleet.settings
        # run id -> (when, the raw end of its log): a job turn's log waits here for the job's outcome
        self._pending: collections.OrderedDict[str, tuple[float, str]] = collections.OrderedDict()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ hooks
    def turn_ended(self, svc: SessionService, s: Session, turn: SessionTurn, outcome: str, raw: str) -> None:
        """SessionService.end_turn, once the session is idle. Never raises."""
        try:
            self._turn_ended(svc, s, turn, outcome, raw)
        except Exception:  # noqa: BLE001 - a record must never fail the turn it records
            log.warning("flight record for turn %s failed", getattr(turn, "run_id", None), exc_info=True)

    def _turn_ended(self, svc: SessionService, s: Session, turn: SessionTurn, outcome: str, raw: str) -> None:
        secs = (turn.finished_at - turn.started_at) if turn.finished_at and turn.started_at else None
        clean = outcome == "exited" and turn.exit_code == 0
        if s.job_id:  # the job decides once it knows its outcome (a clean turn can still fail the job's check)
            if raw and turn.run_id:
                self._pending[turn.run_id] = (time.monotonic(), raw[-LOG_TAIL:])
                self._pending.move_to_end(turn.run_id)
                self._trim()
            return
        slow = self._slow("turn", s.environment, secs) if clean else None
        if clean and secs is not None:
            self._note_norm("turn", s.environment, secs)
        failed = outcome in ("timed_out", "error", "over_budget", "gone") or (
            outcome == "exited" and turn.exit_code not in (0, None))
        if not failed and slow is None:
            return
        trigger = "slow" if not failed else "timed_out" if outcome == "timed_out" else "failed"
        self._spawn(self._write_turn(svc, s, turn, outcome, raw[-LOG_TAIL:], trigger, secs, slow))

    def job_done(self, svc: JobService, job: Job) -> None:
        """JobService._report: a failed job gets a record, and so does a slow one. Never raises."""
        try:
            self._job_done(svc, job)
        except Exception:  # noqa: BLE001
            log.warning("flight record for job %s failed", getattr(job, "id", None), exc_info=True)

    def _job_done(self, svc: JobService, job: Job) -> None:
        tails = [self._pending.pop(t.run_id)[1] for t in job.turns if t.run_id and t.run_id in self._pending]
        secs = self._job_seconds(job)
        if job.state == JobState.SUCCEEDED and secs is not None:
            slow = self._slow("job", job.environment, secs)
            self._note_norm("job", job.environment, secs)
            if slow is not None:
                self._spawn(self._write_job(svc, job, tails[-1] if tails else "", "slow", secs, slow))
            return
        if job.state != JobState.FAILED:
            return
        trigger = "timed_out" if job.reason == "timed_out" else "failed"
        self._spawn(self._write_job(svc, job, tails[-1] if tails else "", trigger, secs, None))

    # ------------------------------------------------------------------ norms
    def _norm_key(self, kind: str, env: str) -> str:
        return f"flight:norm:{kind}:{env}"

    def _note_norm(self, kind: str, env: str, secs: float) -> None:
        xs = self.store.kv_get_json(self._norm_key(kind, env), []) or []
        self.store.kv_set_json(self._norm_key(kind, env), [*xs, round(secs, 1)][-NORM_KEEP:])

    def norm(self, kind: str, env: str) -> dict[str, Any] | None:
        xs = sorted(self.store.kv_get_json(self._norm_key(kind, env), []) or [])
        if len(xs) < NORM_MIN:
            return None
        return {"p50": xs[len(xs) // 2], "samples": len(xs)}

    def _slow(self, kind: str, env: str, secs: float | None) -> dict[str, Any] | None:
        n = self.norm(kind, env)
        if n is None or secs is None:
            return None
        return n if secs > max(SLOW_FACTOR * n["p50"], n["p50"] + SLOW_FLOOR) else None

    @staticmethod
    def _job_seconds(job: Job) -> float | None:
        """From the first provisioning to the end: time a job waited for approval or for power isn't the
        environment's."""
        start = next((e.ts for e in job.events if e.state == JobState.PROVISIONING.value), None)
        return round(job.updated_at - start, 1) if start is not None else None

    # ------------------------------------------------------------------ building records
    def _trim(self) -> None:
        cutoff = time.monotonic() - PENDING_TTL
        while self._pending and (len(self._pending) > PENDING_MAX or next(iter(self._pending.values()))[0] < cutoff):
            self._pending.popitem(last=False)

    def _spawn(self, coro: Any) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def settle(self, timeout: float = 10.0) -> None:
        """Wait for records being written (tests, shutdown)."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    def _sandbox(self, sandbox_id: str | None, session_id: str | None) -> Sandbox | None:
        """The sandbox the work ran in, or, when the create failed, the one its session tried to start."""
        from hangar.models import Sandbox, SandboxState

        if sandbox_id:
            with contextlib.suppress(Exception):
                return self.store.get(Sandbox, sandbox_id)
        if session_id:
            tried = [sb for sb in self.store.list_where(Sandbox, state=SandboxState.ERROR)
                     if sb.labels.get("hangar.session") == session_id]
            if tried:
                return max(tried, key=lambda sb: sb.created_at)
        return None

    def _facts(self, rec: FlightRecord, sb: Sandbox | None, s: Session | None) -> None:
        """Stages, the environment doctor's view, egress denies and the agent's requests, onto `rec`."""
        from hangar import envdoctor
        from hangar.envrequests import EnvRequest

        if sb is not None:
            rec.sandbox_id = sb.id
            rec.stages = list(sb.info.get("stages") or [])[-30:]
            rec.failed_stage = sb.info.get("failed_stage") or (sb.info.get("stage") if sb.error else None)
            rec.sandbox_cause = sb.info.get("cause")
            rec.versions.update({k: v for k, v in (("image", sb.image), ("packs", sb.info.get("packs")),
                                                   ("harness_version", sb.info.get("harness_version"))) if v})
            try:
                spec = self.fleet.catalog.get(sb.environment)
                st = envdoctor.build_status(self.settings, sb, spec, s, fleet=self.fleet)
                rec.env_doctor = {k: st.get(k) for k in ("runtime", "build", "setup_failures", "warnings", "services",
                                                          "secrets")}
                rec.env_doctor["egress"] = {k: (st.get("egress") or {}).get(k) for k in ("mode", "enforced")}
            except Exception:  # noqa: BLE001 - an environment deleted since: the record keeps the rest
                rec.env_doctor = None
            since = sb.created_at
            denies = self.store.audit_since(since, ("egress.deny",), target=sb.id, limit=MAX_DENIES)
            rec.egress_denies = [{"at": e.ts, "host": e.detail.get("host"), "port": e.detail.get("port"),
                                  "reason": e.detail.get("reason")} for e in denies]
        if s is not None:
            rows = self.store.list_where(EnvRequest, session_id=s.id)
            rec.requests = [{"id": r.id, "kind": r.kind, "target": r.target, "state": r.state, "at": r.created_at}
                            for r in rows if r.state in ("pending", "denied")][-10:]

    async def _write_turn(self, svc: SessionService, s: Session, turn: SessionTurn, outcome: str, tail: str,
                          trigger: str, secs: float | None, slow: dict[str, Any] | None) -> None:
        from hangar.events import total_usage

        u = total_usage([turn])
        rec = FlightRecord(kind="turn", trigger=trigger, cause="error", environment=s.environment, repo=s.repo,
                           harness=turn.harness or s.harness, harness_version=turn.harness_version,
                           model=(u.model if u else None) or s.model, person=s.person, session_id=s.id,
                           session=s.name, run_ids=[turn.run_id] if turn.run_id else [], labels=dict(s.labels),
                           outcome=outcome, exit_code=turn.exit_code, seconds=secs, norm=slow,
                           cost_usd=u.cost_usd if u else None, tokens=u.tokens if u else None,
                           summary=turn.summary)
        sb = self._sandbox(s.sandbox_id, s.id)
        self._facts(rec, sb, s)
        rec.versions.update({"hangar": _version(), "harness_version": turn.harness_version, "model": rec.model})
        rec.cause, rec.detail = classify(
            texts=[tail[-20000:], turn.summary or "", turn.outcome_reason or ""], reason=None, outcome=outcome,
            sandbox_cause=rec.sandbox_cause, failed_stage=rec.failed_stage, env_doctor=rec.env_doctor,
            denies=rec.egress_denies, requests=rec.requests, powered_down=False, restarted=False,
            slow=trigger == "slow", verify_tail=None)
        await self._finish(svc, rec, tail)

    async def _write_job(self, svc: JobService, job: Job, tail: str, trigger: str, secs: float | None,
                         slow: dict[str, Any] | None) -> None:
        u = svc.usage(job)
        last = job.turns[-1] if job.turns else None
        s = svc._session(job)
        events = [e.message for e in job.events][-MAX_EVENTS:]
        rec = FlightRecord(kind="job", trigger=trigger, cause="error", environment=job.environment, repo=job.repo,
                           harness=job.harness, harness_version=last.harness_version if last else None,
                           model=(u.model if u else None) or job.model, person=job.person, job_id=job.id,
                           session_id=s.id if s else job.session_id, session=s.name if s else None,
                           run_ids=[t.run_id for t in job.turns if t.run_id], labels=dict(job.labels),
                           outcome=job.state.value, reason=job.reason, exit_code=last.exit_code if last else None,
                           seconds=secs, norm=slow, cost_usd=u.cost_usd if u else None,
                           tokens=u.tokens if u else None, events=events, summary=job.result.summary)
        if job.verify:
            rec.verify = {"command": job.verify, "exit_code": job.result.verify_exit_code,
                          "tail": (job.result.verify_tail or "")[-2000:]}
        sb = self._sandbox(job.sandbox_id or (s.sandbox_id if s else None), rec.session_id)
        self._facts(rec, sb, s)
        rec.versions.update({"hangar": _version(), "harness_version": rec.harness_version, "model": rec.model})
        joined = "\n".join(events)
        rec.cause, rec.detail = classify(
            texts=[tail[-20000:], joined, job.result.summary or "", last.outcome_reason if last else ""],
            reason=job.reason, outcome=last.outcome if last else None, sandbox_cause=rec.sandbox_cause,
            failed_stage=rec.failed_stage, env_doctor=rec.env_doctor, denies=rec.egress_denies,
            requests=rec.requests, powered_down="sandbox pools powered down" in joined,
            restarted="control plane restarted" in joined, slow=trigger == "slow",
            verify_tail=job.result.verify_tail)
        await self._finish(svc, rec, tail)

    async def _finish(self, svc: Any, rec: FlightRecord, tail: str) -> None:
        rec.transient = rec.cause in TRANSIENT
        rec.signature = signature(rec.cause, rec.detail, rec.repo, rec.environment)
        broker = self.fleet.broker
        sessions = svc if hasattr(svc, "blobs") else svc.sessions
        if tail:
            try:
                rec.log = await self._seal(sessions, rec.id, tail)
            except Exception as e:  # noqa: BLE001 - the record without its log beats no record
                log.warning("could not store the run log of %s: %s", rec.id, e)
                rec.log_note = f"the run log could not be stored: {type(e).__name__}"
        else:
            rec.log_note = ("no run log: the sandbox went away first, or the control plane restarted before the "
                            "job ended") if rec.run_ids else "no agent turn ran"
        # every free-form field passes through redaction, like the log: summaries and events quote the agent
        clean = FlightRecord.model_validate(await asyncio.to_thread(_scrub, broker, rec.model_dump(mode="json")))
        self.store.put(clean)
        log.info("flight record %s: %s %s (%s%s)", clean.id, clean.kind, clean.job_id or clean.session,
                 clean.cause, ", transient" if clean.transient else "")

    async def _seal(self, sessions: Any, rid: str, tail: str) -> dict[str, Any]:
        from hangar import ckpt, secretscan

        broker = self.fleet.broker

        def prepare() -> tuple[bytes, str, bytes]:
            masked, _ = secretscan.mask(tail, broker)
            plain = broker.redact(masked).encode()
            digest, blob = ckpt.seal(sessions.vault, gzip.compress(plain, compresslevel=6))
            return plain, digest, blob

        plain, digest, blob = await asyncio.to_thread(prepare)
        key = f"{BLOB_PREFIX}{rid}/run.log.gz"
        await sessions.blobs.put(key, blob)
        return {"key": key, "bytes": len(plain), "sha256": hashlib.sha256(plain).hexdigest()}

    # ------------------------------------------------------------------ reading
    def list(self, since: float = 0.0, limit: int = 500) -> list[FlightRecord]:
        return self.store.list_range(FlightRecord, "at", since, limit=limit)

    def get(self, rid: str) -> FlightRecord:
        r = self.store.get(FlightRecord, rid)
        if r is None:
            raise KeyError(f"no flight record '{rid}'")
        return r

    def for_job(self, job_id: str) -> FlightRecord | None:
        rows = self.store.list_where(FlightRecord, job_id=job_id)
        return max(rows, key=lambda r: r.at) if rows else None

    async def read_log(self, sessions: Any, rec: FlightRecord) -> str | None:
        if not rec.log:
            return None
        blob = await sessions.blobs.get(rec.log["key"])
        if blob is None:
            return None
        plain = await asyncio.to_thread(lambda: gzip.decompress(sessions.vault.decrypt(blob)))
        return plain.decode(errors="replace")

    async def prune(self, sessions: Any) -> int:
        """Records older than RETENTION_DAYS go, with their logs."""
        old = self.store.list_range(FlightRecord, "at", 0.0, hi=now() - RETENTION_DAYS * 86400, limit=2000)
        for r in old:
            if r.log:
                with contextlib.suppress(Exception):
                    await sessions.blobs.delete(r.log["key"])
            self.store.delete(FlightRecord, r.id)
        return len(old)


def _scrub(broker: Any, obj: Any) -> Any:
    """Stored values (and their encodings) and the token formats secretscan knows, out of every string in `obj`."""
    from hangar import secretscan

    if isinstance(obj, str):
        return broker.redact(secretscan.mask(obj, broker)[0]) if obj else obj
    if isinstance(obj, dict):
        return {k: _scrub(broker, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(broker, v) for v in obj]
    return obj


def of(fleet: Fleet) -> Recorder:
    """The fleet's recorder (made on first use, so Fleet itself needs no change)."""
    r = fleet.__dict__.get("_flightrec")
    if r is None:
        r = fleet.__dict__["_flightrec"] = Recorder(fleet)
    return r


def view(rec: FlightRecord) -> dict[str, Any]:
    return rec.model_dump(mode="json")


def brief(rec: FlightRecord) -> dict[str, Any]:
    return rec.model_dump(mode="json", include={"id", "kind", "at", "trigger", "cause", "detail", "transient",
                                                "environment", "repo", "job_id", "session", "person", "seconds",
                                                "cost_usd"})


def by_cause(records: list[FlightRecord]) -> dict[str, dict[str, Any]]:
    """{cause: {count, transient, last_at, last, environments}}, most frequent first."""
    out: dict[str, dict[str, Any]] = {}
    for r in sorted(records, key=lambda r: r.at):
        c = out.setdefault(r.cause, {"count": 0, "transient": r.transient, "last_at": None, "last": None,
                                     "environments": []})
        c["count"] += 1
        c["last_at"], c["last"] = r.at, r.id
        if r.environment not in c["environments"]:
            c["environments"].append(r.environment)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["count"]))


# ====================================================================== REST
def mount(app: Any, h: Any, admin: Any, person: Any) -> None:
    """GET /api/records (admins), GET /api/records/{id} and /log (admins, or the record's person), and
    GET /api/jobs/{id}/record."""
    from fastapi import HTTPException
    from fastapi.responses import PlainTextResponse

    from hangar import people

    rec = of(h.fleet)

    def visible(r: FlightRecord) -> FlightRecord:
        if not people.visible(r.person):
            raise KeyError(f"no flight record '{r.id}'")
        return r

    @app.get("/api/records")
    async def records_list(hours: float = 24, cause: str | None = None, limit: int = 200, _: str = admin):
        rows = rec.list(now() - max(0.0, hours) * 3600, max(1, min(limit, 2000)))
        rows = [r for r in rows if cause is None or r.cause == cause]
        return {"records": [brief(r) for r in rows], "by_cause": by_cause(rows)}

    @app.get("/api/records/{rid}")
    async def records_get(rid: str, _: str = person):
        return view(visible(rec.get(rid)))

    @app.get("/api/records/{rid}/log")
    async def records_log(rid: str, _: str = person):
        text = await rec.read_log(h.sessions, visible(rec.get(rid)))
        if text is None:
            raise HTTPException(404, "this record has no run log (see its log_note), or it was deleted")
        return PlainTextResponse(text)

    @app.get("/api/jobs/{job_id}/record")
    async def job_record(job_id: str, _: str = person):
        job = h.jobs.get(job_id)
        if not people.visible(job.person):
            raise KeyError(f"no job '{job_id}'")
        r = rec.for_job(job.id)
        if r is None:
            raise HTTPException(404, "no flight record: the job didn't fail, time out or run slow (or it is being "
                                     "written: try again in a few seconds)")
        return view(r)


def register(mcp: Any, jobs: Any, guard: Any) -> None:
    """`hangar://jobs/<id>/record`: a resource, not a tool."""
    if jobs is None:
        return
    import json

    @mcp.resource("hangar://jobs/{job_id}/record", name="job-record", mime_type="application/json",
                  description="A failed, timed-out or slow job's flight record: stages, cause, the environment "
                              "doctor's view, egress denies, versions and cost.")
    async def job_record_resource(job_id: str) -> str:
        from mcp.server.mcpserver.exceptions import ResourceError

        try:
            guard.check("job", job_id)
        except (KeyError, PermissionError) as e:
            raise ResourceError(str(e).strip("'\"")) from None
        r = of(jobs.fleet).for_job(job_id)
        return json.dumps(view(r) if r else {"job": job_id, "record": None,
                                             "note": "no record: the job didn't fail, time out or run slow"},
                          indent=1, default=str)
