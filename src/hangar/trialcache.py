"""The trial-result cache and its drift sentinel (#180; docs/api/trials.md#the-cache).

Tokens cost about 500x a trial's compute, so a nightly suite on an unchanged harness should cost only a small
sentinel. A trial's result is determined by its inputs: the harness, the environment, the task, the model, the base
commit and the grader. The cache keys on exactly those,

    key = sha256(harness_digest, environment, task_digest, model_id, base_sha, grader_digest)

where `environment` is the pin set's `environment_digest`, else its `environment_spec_digest`, and `task_digest` is the
sha256 of the task's canonical definition (its instructions or issue, verify command, repo, reference patch, the
grader's hidden tests by digest, and the environment); flightdeck may supply its own. Each entry lists the counted
trials that ran with that key (`outcome_class: agent`); infra errors, cancelled trials and drift never count.

`POST /api/trials` with `reuse: {min_trials: k}` returns up to n cached trials (`cached: true`) when the key has at
least k, and starts only the ones still missing. Cached trials cost nothing against the eval budget (#182).

**Sentinel.** Provider-side changes behind a dated model id are invisible to the key, so a sentinel re-runs a few
configured tasks (`HANGAR_TRIAL_SENTINEL`, at most 5, 2 trials each by default) on a schedule
(`HANGAR_TRIAL_SENTINEL_SCHEDULE`, 20:00 in HANGAR_EVAL_TZ: the eval window) *bypassing* the cache, and writes what
they did beside what the cache holds for the same key to `v1/sentinel/{date}.json` in the outbox (and
`<data>/sentinel/`). Flightdeck decides whether a change is significant and drops stale entries with
`POST /api/trials/cache/invalidate {model_id | harness_digest}`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from hangar.models import now

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.pinsets import PinSet

log = logging.getLogger("hangar.trialcache")

KEY_PARTS = ("harness_digest", "environment", "task_digest", "model_id", "base_sha", "grader_digest")
MAX_SENTINEL_TASKS = 5
SLOT_KEY = "trials:sentinel:slot"
RUN_KEY = "trials:sentinel:run"  # the sentinel run under way: {"date", "slot", "entries": [...]}
SCHEMA = "hangar.sentinel/v1"


class CacheEntry(BaseModel):
    """The counted trials of one key (table `trial_cache`)."""

    model_config = ConfigDict(extra="forbid")
    key: str
    parts: dict[str, str | None]
    trials: list[dict[str, Any]] = Field(default_factory=list)  # {trial_id, job, outcome, outcome_class, at}
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def task_digest(task: Any, environment: str, grader: Any = None) -> str:
    """A task's identity for the cache: what decides its result, not its name."""
    t = task.model_dump(mode="json") if hasattr(task, "model_dump") else dict(task)
    hidden = {}
    if grader is not None:
        files = grader.files if hasattr(grader, "files") else (grader.get("files") or {})
        hidden = {p: "sha256:" + hashlib.sha256(c.encode()).hexdigest() for p, c in sorted(files.items())}
    body = {"instructions": t.get("instructions"), "issue": t.get("issue"), "verify": t.get("verify"),
            "repo": t.get("repo"), "base_branch": t.get("base_branch"),
            "reference_patch": "sha256:" + hashlib.sha256(t["reference_patch"].encode()).hexdigest()
            if t.get("reference_patch") else None, "hidden_tests": hidden, "environment": environment}
    return "sha256:" + hashlib.sha256(_canon(body).encode()).hexdigest()


def parts_for(pin: PinSet, task_id: str, digest: str) -> dict[str, str | None]:
    tp = pin.tasks.get(task_id)
    return {"harness_digest": pin.harness_digest, "environment": pin.environment_digest or pin.environment_spec_digest,
            "task_digest": digest, "model_id": pin.model_id, "base_sha": tp.base_sha if tp else None,
            "grader_digest": pin.grader_digest}


def key_of(parts: dict[str, str | None]) -> str:
    return hashlib.sha256(_canon({k: parts.get(k) for k in KEY_PARTS}).encode()).hexdigest()


def cacheable(parts: dict[str, str | None]) -> bool:
    """Only a fully pinned key caches: a harness digest (a lock) and a base commit; an overlay variant's has neither
    until its first trial runs."""
    return bool(parts.get("harness_digest") and parts.get("base_sha") and parts.get("task_digest"))


def lookup(store: Any, parts: dict[str, str | None]) -> list[dict[str, Any]]:
    """The counted trials cached for `parts` (oldest first)."""
    if not cacheable(parts):
        return []
    e = store.get(CacheEntry, key_of(parts))
    return list(e.trials) if e is not None else []


def record(store: Any, parts: dict[str, str | None], trial_id: str, job: str | None, outcome: str | None,
           outcome_class: str | None) -> bool:
    """A finished trial, cached when it counts (`outcome_class: agent`). True when it was added."""
    if outcome_class != "agent" or not cacheable(parts):
        return False
    k = key_of(parts)
    e = store.get(CacheEntry, k) or CacheEntry(key=k, parts={p: parts.get(p) for p in KEY_PARTS})
    if any(x.get("trial_id") == trial_id for x in e.trials):
        return False
    e.trials.append({"trial_id": trial_id, "job": job, "outcome": outcome, "outcome_class": outcome_class,
                     "at": now()})
    e.updated_at = now()
    store.put(e)
    return True


def invalidate(store: Any, model_id: str | None = None, harness_digest: str | None = None) -> int:
    """Drop the entries for a model id or a harness digest (flightdeck saw the sentinel change). Returns how many."""
    if not model_id and not harness_digest:
        raise ValueError("give model_id or harness_digest")
    n = 0
    for e in store.list(CacheEntry):
        if (model_id and e.parts.get("model_id") == model_id) or \
                (harness_digest and e.parts.get("harness_digest") == harness_digest):
            store.delete(CacheEntry, e.key)
            n += 1
    return n


# ====================================================================== the sentinel
class SentinelTask(BaseModel):
    """One configured sentinel task: a pin set, its task (as POST /api/trials takes it), trials per run."""

    model_config = ConfigDict(extra="forbid")
    pin_set: str
    task: dict[str, Any]
    n: int = Field(2, ge=1, le=5)
    grader: dict[str, Any] | None = None
    budget: dict[str, Any] = Field(default_factory=lambda: {"max_cost_usd": 5.0})


def configured(settings: Any) -> list[SentinelTask]:
    return [SentinelTask.model_validate(x) for x in (settings.trial_sentinel or [])][:MAX_SENTINEL_TASKS]


def _cron(settings: Any) -> Any:
    from hangar.triggers import Cron

    expr = (settings.trial_sentinel_schedule or "").strip()
    if expr.lower() in ("", "off", "none") or not settings.trial_sentinel:
        return None
    return Cron(expr, settings.eval_tz or "UTC")


def start(jobs: JobService, caller: Any = None, date: str | None = None) -> dict[str, Any]:
    """Start a sentinel run now: each configured task's trials, bypassing the cache (and not filling it)."""
    from hangar import pinsets
    from hangar.jobs import Caller

    caller = caller or Caller("hangar:sentinel", "admin")
    date = date or datetime.fromtimestamp(now(), UTC).strftime("%Y-%m-%d")
    entries = []
    for st in configured(jobs.settings):
        req = pinsets.TrialsRequest(pin_set=st.pin_set, task=st.task, n=st.n, grader=st.grader, budget=st.budget,
                                    name=f"sentinel {date} {st.task.get('id')}")
        try:
            out = pinsets.start_trials(jobs, req, caller, sentinel=True)
        except Exception as e:  # noqa: BLE001 - one bad entry doesn't stop the others
            entries.append({"pin_set": st.pin_set, "task": st.task.get("id"), "error": str(e)[:500]})
            continue
        entries.append({"pin_set": st.pin_set, "task": st.task.get("id"), "experiment": out["experiment"],
                        "trial_ids": out["trial_ids"], "parts": out["cache_parts"]})
    run = {"date": date, "started_at": now(), "entries": entries}
    jobs.store.kv_set_json(RUN_KEY, run)
    jobs.fleet.audit(caller.requester, "trials.sentinel", None, date=date, tasks=len(entries))
    return run


def report(jobs: JobService, run: dict[str, Any]) -> dict[str, Any] | None:
    """The sentinel's result once its trials have all finished, else None: per task, what the sentinel's trials did
    and what the cache holds for the same key."""
    from hangar import experiments
    from hangar.models import JOB_TERMINAL

    out = []
    for e in run.get("entries") or []:
        if "experiment" not in e:
            out.append(e)
            continue
        exp = jobs._jobs.get(e["experiment"])
        if exp is None or exp.state not in JOB_TERMINAL:
            return None
        st = experiments.load(exp)
        counted = [t for t in st.trials if not t.excluded and t.state in ("done", "grading")]
        cached = lookup(jobs.store, e["parts"])
        out.append({**{k: e[k] for k in ("pin_set", "task", "experiment", "trial_ids")}, "key": key_of(e["parts"]),
                    "parts": e["parts"],
                    "sentinel": {"trials": len(counted), "outcomes": _counts(t.outcome for t in counted),
                                 "passed": sum(1 for t in counted if (t.verify or {}).get("passed")),
                                 "excluded": sum(1 for t in st.trials if t.excluded)},
                    "cached": {"trials": len(cached), "outcomes": _counts(x.get("outcome") for x in cached)}})
    return {"schema": SCHEMA, "date": run["date"], "started_at": run.get("started_at"), "finished_at": now(),
            "tasks": out}


def _counts(xs: Any) -> dict[str, int]:
    c: dict[str, int] = {}
    for x in xs:
        c[str(x)] = c.get(str(x), 0) + 1
    return c


def write(jobs: JobService, rep: dict[str, Any]) -> Path:
    """`<data>/sentinel/{date}.json`, and `v1/sentinel/{date}.json` in the outbox (write-once)."""
    from hangar import outbox

    d = Path(jobs.settings.data_dir) / "sentinel"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{rep['date']}.json"
    p.write_text(json.dumps(rep, indent=1, sort_keys=True))
    ob = outbox.of(jobs.fleet)
    if ob.enabled:
        ob._stage(f"sentinel:{rep['date']}", f"{outbox.PREFIX}/sentinel/", {f"{rep['date']}.json": rep})
        ob._kick()
    return p


async def minutely(h: Any) -> None:
    """The ops loop's step (opstatus.MINUTELY): start a run at its slot, write the report once its trials finish."""
    jobs = getattr(h, "jobs", None)
    if jobs is None:
        return
    run = jobs.store.kv_get_json(RUN_KEY, None)
    if run and not run.get("written"):
        rep = report(jobs, run)
        if rep is not None:
            write(jobs, rep)
            run["written"] = time.time()
            jobs.store.kv_set_json(RUN_KEY, run)
        return
    cron = _cron(jobs.settings)
    if cron is None:
        return
    slot = cron.latest_at_or_before(now())
    last = jobs.store.kv_get_json(SLOT_KEY, None)
    if slot is None or (last is not None and slot <= float(last)):
        return
    jobs.store.kv_set_json(SLOT_KEY, slot)
    if last is None:
        return  # the first look only remembers the slot: a deploy never runs it on the spot
    start(jobs, date=datetime.fromtimestamp(slot, UTC).strftime("%Y-%m-%d"))


def install() -> None:
    from hangar import opstatus

    if minutely not in opstatus.MINUTELY:
        opstatus.MINUTELY.append(minutely)


def mount(app: Any, h: Any, J: Any, admin: Any) -> None:
    """POST /api/trials/cache/invalidate (jobs tokens: flightdeck) and POST /api/trials/sentinel/run (admin)."""
    from fastapi import Body, HTTPException

    install()

    @app.post("/api/trials/cache/invalidate")
    async def cache_invalidate(body: dict[str, Any] = Body(default_factory=dict), caller: Any = J):
        try:
            n = invalidate(h.store, body.get("model_id"), body.get("harness_digest"))
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        h.fleet.audit(caller.requester, "trials.cache_invalidate", None, model_id=body.get("model_id"),
                      harness_digest=body.get("harness_digest"), dropped=n)
        return {"dropped": n}

    @app.post("/api/trials/sentinel/run")
    async def sentinel_run(who: str = admin):
        from hangar.jobs import Caller

        if not configured(h.settings):
            raise HTTPException(409, "no sentinel tasks configured (HANGAR_TRIAL_SENTINEL)")
        return start(h.jobs, Caller(who, "admin"))
