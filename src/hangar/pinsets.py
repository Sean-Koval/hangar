"""Pin sets and trials (#178; docs/api/trials.md): the hangar <-> flightdeck seam is a trial against an immutable pin
set; flightdeck owns the sampling policy (how many trials, of what, when to stop).

**Pin sets.** What a trial runs with, recorded once and never changed: the harness lock (`name@sha256:...`, #133/#134:
it fixes the harness, its CLI version, the concrete model, approvals, instructions, skills and plugins) and its
`harness_digest`, the environment (`environment_spec_digest`, and optionally the `environment_digest` a turn's
manifest carries), the concrete `model_id` (#167), each task's repo and `base_sha`, the grader's digest, the egress
policy's digest, and an optional knowledge snapshot id. The id is content-addressed, `pin-<sha256 of the canonical
body>[:16]`, so creating the same pin set twice returns the same one; the store refuses to change or delete a row
(SQLite triggers), and there is no PUT. A pin set from a month ago re-runs the same base commit, lock and model; when
hangar no longer runs the lock's CLI version (harnesses.lock.yaml moved on), the claim is refused with what to do.

**Trials.** `POST /api/trials {pin_set, task, n, source, budget}` starts n trials of one task against one pin set. They
run as experiment trials do (one ordinary job each, `deliver: none`, the grader in a clean sealed sandbox, the eval
queue class, #181), by an experiment job hangar makes for the request, whose variant is the pin set. On turn 1 each
trial's manifest must carry the pin set's digests (`harness_digest`, `environment.spec_digest`, and
`environment_digest` when pinned); a mismatch ends the trial as `infra_error` / `drift` (#166), not run further and
not retried. A trial's id is `<experiment job id>.<n>`; `GET /api/trials/{id}` reads it.

`hangar.experiment/v1` keeps working on top: its `_begin` records a pin set per variant (a lock variant's carries the
lock's digest; a deprecated overlay variant's has no `harness_digest`, since only its first trial knows it), and the
index lists each variant's and trial's `pin_set`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hangar.models import JobBudget, now

if TYPE_CHECKING:
    from hangar.fleet import Fleet
    from hangar.jobs import JobService
    from hangar.models import Job

SCHEMA = "hangar.pinset/v1"
ID = re.compile(r"^pin-[0-9a-f]{16}$")
# `<experiment>.<n>` for a trials request (one task, one variant); `<experiment>~<task>~<variant>~<n>` for any trial
TRIAL_ID = re.compile(r"^(?P<exp>job-[0-9a-f]{8})"
                      r"(?:\.(?P<n>\d{1,3})|~(?P<task>[^~]+)~(?P<variant>[^~]+)~(?P<k>\d{1,3}))$")
DRIFT = "drift"
SHA = r"^[0-9a-f]{40}$"


class PinSetError(ValueError):
    """A pin set can't be made or used as asked (a 4xx at every front door)."""


class TaskPin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str | None = Field(None, max_length=500)
    base_branch: str | None = Field(None, max_length=200)
    base_sha: str | None = Field(None, pattern=SHA)


class PinSetBody(BaseModel):
    """What a pin set pins; its id is the hash of this, canonical."""

    model_config = ConfigDict(extra="forbid")
    harness: str = Field(max_length=80)
    harness_version: str | None = Field(None, max_length=80)
    harness_lock: str | None = Field(None, max_length=200)  # name@sha256:<digest>
    harness_digest: str | None = Field(None, max_length=100)
    environment: str = Field(max_length=100)
    environment_spec_digest: str = Field(max_length=100)
    environment_digest: str | None = Field(None, max_length=100)
    model_id: str | None = Field(None, max_length=200)
    approvals: str | None = Field(None, max_length=20)
    overlay_digest: str | None = Field(None, max_length=100)  # a deprecated overlay variant's (experiments only)
    tasks: dict[str, TaskPin] = Field(default_factory=dict, max_length=100)
    grader_digest: str | None = Field(None, max_length=100)
    knowledge_snapshot: str | None = Field(None, max_length=200)
    egress_digest: str = Field(max_length=100)

    @field_validator("tasks")
    @classmethod
    def _names(cls, v: dict[str, TaskPin]) -> dict[str, TaskPin]:
        for k in v:
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$", k):
                raise ValueError(f"task id '{k[:40]}': letters, digits, '.', '_', '-'")
        return v


class PinSet(PinSetBody):
    """A stored pin set (table `pin_sets`, immutable)."""

    id: str
    schema_: str = Field(SCHEMA, alias="schema")
    created_by: str = "system"
    created_at: float = Field(default_factory=now)

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", include=set(PinSetBody.model_fields))

    def view(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj).encode()).hexdigest()


def pin_id(body: PinSetBody) -> str:
    return "pin-" + hashlib.sha256(canonical(body.model_dump(mode="json")).encode()).hexdigest()[:16]


def grader_digest(grader: Any) -> str | None:
    """A grader's identity: its command, files, mode, network and timeout (the canary is per experiment)."""
    if grader is None:
        return None
    d = grader.model_dump(mode="json", exclude={"canary"}) if hasattr(grader, "model_dump") else dict(grader)
    d.pop("canary", None)
    return digest(d)


def egress_digest(fleet: Fleet, environment: str) -> str:
    e = fleet.catalog.get(environment)
    return digest(e.egress.model_dump(mode="json") if e.egress else None)


def environment_spec_digest(fleet: Fleet, environment: str) -> str:
    """The environment's spec as a turn's manifest records it (`environment.spec_digest`)."""
    from hangar import manifest

    return manifest._spec_digest(fleet.catalog.get(environment))


# ====================================================================== the store
def get(fleet: Fleet, pin: str) -> PinSet:
    p = fleet.store.get(PinSet, pin) if ID.match(pin or "") else None
    if p is None:
        raise KeyError(f"no pin set '{pin}'")
    return p


def put(fleet: Fleet, body: PinSetBody, actor: str) -> PinSet:
    """Store `body` as a pin set, or return the one with the same content (created_by/at stay the first's)."""
    pid = pin_id(body)
    if (old := fleet.store.get(PinSet, pid)) is not None:
        return old
    p = PinSet(id=pid, created_by=actor, **body.model_dump())
    fleet.store.put(p)
    fleet.audit(actor, "pinset.create", pid, harness_lock=p.harness_lock, model=p.model_id,
                environment=p.environment)
    return p


def check(fleet: Fleet, body: PinSetBody) -> None:
    """What `POST /api/pinsets` refuses: a harness without a lock digest, a floating model alias (#167), a task without
    a base commit, a lock that doesn't name what the body says."""
    from hangar import harness_thaw, modelpolicy

    if not body.harness_lock or not body.harness_digest:
        raise PinSetError("a pin set needs a harness lock and its digest (`harness_lock: name@sha256:...`); freeze one "
                          "with `hangar harness freeze` (or POST /api/pinsets/resolve with the lock)")
    try:
        lk = harness_thaw.pin(fleet, body.harness_lock, body.environment)
    except harness_thaw.ThawError as e:
        raise PinSetError(str(e)) from e
    if lk["digest"] != body.harness_digest or lk["harness"] != body.harness:
        raise PinSetError(f"{body.harness_lock} is {lk['harness']} with harness_digest {lk['digest']}, not "
                          f"{body.harness} {body.harness_digest}")
    if lk["model"] and body.model_id and lk["model"] != body.model_id:
        raise PinSetError(f"{body.harness_lock} pins model {lk['model']}, not {body.model_id}")
    model = body.model_id or lk["model"]
    if model is not None:
        ok, basis, note = modelpolicy.of(fleet).concreteness(body.harness, model)
        if not ok:
            raise PinSetError(f"model `{model}` is not a concrete, dated model id ({basis}): a pin set pins one so "
                              "every trial runs the same model" + (f". {note}" if note else ""))
    if not body.tasks:
        raise PinSetError("a pin set pins at least one task's base commit (`tasks: {id: {repo, base_sha}}`)")
    if missing := sorted(k for k, t in body.tasks.items() if not t.base_sha):
        raise PinSetError(f"task(s) {', '.join(missing)} have no base_sha: a pin set pins each task's commit")
    try:
        fleet.catalog.get(body.environment)
    except KeyError as e:
        raise PinSetError(f"no environment '{body.environment}'") from e


async def resolve(jobs: JobService, harness_lock: str, environment: str, tasks: dict[str, TaskPin],
                  grader: Any = None, knowledge_snapshot: str | None = None) -> PinSetBody:
    """A proposed pin set from what hangar runs now (what an experiment's start resolves): the lock's digest, model and
    approvals, the environment's digests, each task's base commit (the task's own, else its branch head on GitHub)."""
    from hangar import experiments, harness_thaw

    try:
        lk = harness_thaw.pin(jobs.fleet, harness_lock, environment)
    except harness_thaw.ThawError as e:
        raise PinSetError(str(e)) from e
    except KeyError as e:
        raise PinSetError(f"no environment '{environment}'") from e
    pinned: dict[str, TaskPin] = {}
    for tid, t in tasks.items():
        sha = t.base_sha or await experiments._github_sha(jobs, t.repo, t.base_branch)
        pinned[tid] = TaskPin(repo=t.repo, base_branch=t.base_branch, base_sha=sha)
    return PinSetBody(harness=lk["harness"], harness_version=lk["version"], harness_lock=harness_lock,
                      harness_digest=lk["digest"], environment=environment,
                      environment_spec_digest=environment_spec_digest(jobs.fleet, environment),
                      model_id=lk["model"], approvals=lk["approvals"], tasks=pinned,
                      grader_digest=grader_digest(grader), knowledge_snapshot=knowledge_snapshot,
                      egress_digest=egress_digest(jobs.fleet, environment))


def drift(p: PinSet, harness_digest: str | None, environment_digest: str | None,
          spec_digest: str | None) -> list[str]:
    """How a trial's turn-1 manifest differs from its pin set (empty: it ran what was pinned)."""
    out = []
    if p.harness_digest and harness_digest != p.harness_digest:
        out.append(f"harness_digest {harness_digest} isn't the pin set's {p.harness_digest}")
    if p.environment_spec_digest and spec_digest and spec_digest != p.environment_spec_digest:
        out.append(f"environment {p.environment} changed: spec_digest {spec_digest} isn't the pin set's "
                   f"{p.environment_spec_digest}")
    if p.environment_digest and environment_digest != p.environment_digest:
        out.append(f"environment_digest {environment_digest} isn't the pin set's {p.environment_digest}")
    return out


# ====================================================================== trials
class Reuse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_trials: int = Field(1, ge=1, le=20)


class TrialsRequest(BaseModel):
    """`POST /api/trials`: n trials of one task against one pin set."""

    model_config = ConfigDict(extra="forbid")
    pin_set: str = Field(pattern=ID.pattern)
    task: Any  # experiments.Task (its id must be one of the pin set's tasks)
    variant: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")  # a label; default the pin's id
    n: int = Field(1, ge=1, le=20)
    source: Literal["experiment", "shadow"] = "experiment"
    budget: JobBudget  # each trial, all its turns: required (the team's eval budget is #182)
    turn_budget: Any = None  # models.Budget: each agent turn
    grader: Any = None  # experiments.Grader; its digest must be the pin set's
    timeout_minutes: int | None = Field(None, ge=1, le=720)
    name: str | None = Field(None, max_length=100)
    # --- #180: reuse cached trials of the same key when it has at least min_trials counted ones
    reuse: Reuse | None = None
    task_digest: str | None = Field(None, max_length=100)  # flightdeck's own; default computed (trialcache)
    # --- #183: a shadow's real job, and the eval author (the harness owner) its trials count against
    shadow_of: str | None = Field(None, max_length=40)
    author: str | None = Field(None, max_length=100)

    @field_validator("task")
    @classmethod
    def _task(cls, v: Any) -> Any:
        from hangar.experiments import Task

        return Task.model_validate(v)

    @field_validator("grader")
    @classmethod
    def _grader(cls, v: Any) -> Any:
        from hangar.experiments import Grader

        return Grader.model_validate(v) if v is not None else None

    @field_validator("turn_budget")
    @classmethod
    def _turn_budget(cls, v: Any) -> Any:
        from hangar.models import Budget

        return Budget.model_validate(v) if v is not None else None


def start_trials(jobs: JobService, req: TrialsRequest, caller: Any, sentinel: bool = False,
                 untrusted_source: str | None = None) -> dict[str, Any]:
    """The trials of `req`, as an experiment job whose one variant is the pin set (it runs, grades, retries and drift-
    checks them). Returns {"trial_ids", "cached", "experiment", "pin_set", "cache_parts"}. --- #180: with `reuse`,
    cached trials of the same key stand in for new ones; `sentinel` runs bypass the cache both ways."""
    from hangar import experiments, trialcache
    from hangar.jobs import JobError

    try:
        p = get(jobs.fleet, req.pin_set)
    except KeyError as e:
        raise JobError(str(e).strip("'\"")) from e
    task = req.task
    tp = p.tasks.get(task.id)
    if tp is None:
        raise JobError(f"task {task.id} isn't one of pin set {p.id}'s ({', '.join(sorted(p.tasks)) or 'none'})")
    if task.base_sha and task.base_sha != tp.base_sha:
        raise JobError(f"task {task.id}: base_sha {task.base_sha} isn't the pin set's {tp.base_sha}")
    if task.repo and tp.repo and task.repo != tp.repo:
        raise JobError(f"task {task.id}: repo {task.repo} isn't the pin set's {tp.repo}")
    if grader_digest(req.grader) != p.grader_digest:
        raise JobError(f"the grader isn't the pin set's (digest {grader_digest(req.grader)} vs {p.grader_digest})")
    if not p.harness_lock:
        raise JobError(f"pin set {p.id} has no harness lock (an overlay experiment's): trials need a locked harness")
    vname = req.variant or p.id
    from pydantic import ValidationError

    td = req.task_digest or trialcache.task_digest(task, p.environment, req.grader)
    parts = trialcache.parts_for(p, task.id, td)
    cached = []
    if req.reuse is not None and not sentinel:
        have = trialcache.lookup(jobs.store, parts)
        if len(have) >= req.reuse.min_trials:
            cached = [x["trial_id"] for x in have[:req.n]]
    need = req.n - len(cached)
    out = {"experiment": None, "pin_set": p.id, "trial_ids": list(cached), "cached": list(cached),
           "cache_key": trialcache.key_of(parts), "cache_parts": parts}
    if need <= 0:
        return out
    try:
        spec = _trials_spec(experiments, req, p, task, tp, vname).model_copy(update={
            "trials": need, "task_digests": {task.id: td}, "cache_record": not sentinel,
            "shadow_of": req.shadow_of, "author": req.author})
    except ValidationError as e:
        raise JobError(str(e)) from e
    job = experiments.submit(jobs, spec, caller, source="shadow" if req.source == "shadow" else "api",
                             untrusted_source=untrusted_source)
    out["experiment"] = job.id
    out["trial_ids"] += [f"{job.id}.{i}" for i in range(1, need + 1)]
    return out


def _trials_spec(experiments: Any, req: TrialsRequest, p: PinSet, task: Any, tp: TaskPin, vname: str) -> Any:
    return experiments.ExperimentSpec(
        name=req.name or f"trials {p.id} {task.id}", environment=p.environment,
        tasks=[task.model_copy(update={"repo": task.repo or tp.repo, "base_branch": task.base_branch or tp.base_branch,
                                       "base_sha": tp.base_sha})],
        variants={vname: experiments.Variant(harness_lock=p.harness_lock)}, trials=req.n,
        trial_budget=req.turn_budget, trial_job_budget=req.budget, timeout_minutes=req.timeout_minutes,
        grader=req.grader, pin_sets={vname: p.id}, source=req.source)


def trial_view(jobs: JobService, trial_id: str, owner: str | None = None) -> dict[str, Any]:
    """`GET /api/trials/{id}`: the trial that counts (the last attempt of its key), its pin set, outcome and class, the
    manifest digests and where its runs are in the outbox."""
    from hangar import experiments, outbox

    m = TRIAL_ID.match(trial_id or "")
    if m is None:
        raise KeyError(f"no trial '{trial_id}'")
    job = experiments.experiment(jobs, m["exp"], owner)
    st = experiments.load(job)
    if m["n"]:
        mine = [t for t in st.trials if t.trial == int(m["n"])]
    else:
        mine = [t for t in st.trials if (t.task, t.variant, t.trial) == (m["task"], m["variant"], int(m["k"]))]
    if not mine:
        raise KeyError(f"no trial '{trial_id}'")
    t = max(mine, key=lambda x: x.attempt)
    runs: list[dict[str, Any]] = []
    child = jobs._jobs.get(t.job) if t.job else None
    s = jobs._session(child) if child is not None else None
    if s is not None:
        for x in s.turns:
            if x.run_id:
                runs.append({"run_id": x.run_id, "outbox": outbox.run_prefix(s.id, x.run_id, x.finished_at)})
    return {
        "id": trial_id, "experiment": job.id, "pin_set": t.pin_set, "key": t.key, "attempt": t.attempt,
        "attempts": len(mine), "source": st.spec.get("source", "experiment"), "state": t.state, "outcome": t.outcome,
        "reason": t.reason, "outcome_class": t.outcome_class, "infra_reason": t.infra_reason, "excluded": t.excluded,
        "job": t.job, "session": t.session, "manifests": t.manifests, "harness_digest": t.harness_digest,
        "environment_digest": t.environment_digest, "config_digest": t.config_digest, "drift": t.drift,
        "verify": t.verify, "grade": experiments.mask_canary(t.grade, (st.spec.get("grader") or {}).get("canary")),
        "usage": t.usage, "started_at": t.started_at, "finished_at": t.finished_at, "runs": runs,
        # --- #179: a fork's trial
        "fork_of": t.fork_of, "replay": t.replay, "tainted": t.tainted, "aa": t.aa,
    }


# ====================================================================== REST
def mount(app: Any, h: Any, J: Any, owner_filter: Any) -> None:
    """POST /api/pinsets, GET /api/pinsets/{id}, POST /api/pinsets/resolve, POST /api/trials, GET /api/trials/{id}.
    No PUT or DELETE: a pin set never changes. No MCP tools: flightdeck talks REST."""
    from fastapi import HTTPException

    from hangar.jobs import JobError, JobNotFound

    class ResolveBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        harness_lock: str = Field(max_length=200)
        environment: str = Field(max_length=100)
        tasks: dict[str, TaskPin] = Field(min_length=1, max_length=100)
        grader: Any = None
        knowledge_snapshot: str | None = Field(None, max_length=200)

    @app.post("/api/pinsets")
    async def pinset_create(body: PinSetBody, caller: Any = J):
        try:
            check(h.fleet, body)
        except PinSetError as e:
            raise HTTPException(422, str(e)) from e
        return put(h.fleet, body, caller.requester).view()

    @app.post("/api/pinsets/resolve")
    async def pinset_resolve(body: ResolveBody, caller: Any = J):
        from hangar.experiments import Grader

        try:
            g = Grader.model_validate(body.grader) if body.grader is not None else None
            return (await resolve(h.jobs, body.harness_lock, body.environment, body.tasks, g,
                                  body.knowledge_snapshot)).model_dump(mode="json")
        except (PinSetError, ValueError) as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/pinsets/{pin}")
    async def pinset_get(pin: str, caller: Any = J):
        try:
            return get(h.fleet, pin).view()
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\"")) from e

    @app.post("/api/trials")
    async def trials_start(body: TrialsRequest, caller: Any = J):
        try:
            return start_trials(h.jobs, body, caller)
        except JobError as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/trials/{trial_id}")
    async def trial_get(trial_id: str, caller: Any = J):
        try:
            return trial_view(h.jobs, trial_id, owner_filter(caller))
        except (KeyError, JobNotFound) as e:
            raise HTTPException(404, str(e).strip("'\"")) from e


def experiment_pin(jobs: JobService, job: Job, vname: str, vp: dict[str, Any], tasks: dict[str, Any],
                   env: str, grader: Any, overlay: dict[str, Any] | None) -> PinSet:
    """--- the experiment wrapper: the pin set of one variant as `_begin` pinned it. A lock variant's carries the lock's
    digest; an overlay variant's has no harness_digest (only its first trial knows it) and its overlay's digest."""
    body = PinSetBody(
        harness=vp["harness"], harness_version=vp.get("harness_version"), harness_lock=vp.get("harness_lock"),
        harness_digest=vp.get("harness_digest"), environment=env,
        environment_spec_digest=environment_spec_digest(jobs.fleet, env), model_id=vp.get("model"),
        approvals=vp.get("approvals"),
        overlay_digest=digest(overlay) if overlay and (overlay.get("skills") is not None
                                                      or overlay.get("instructions") is not None) else None,
        tasks={tid: TaskPin(repo=t.get("repo"), base_branch=t.get("base_branch"), base_sha=t.get("base_sha"))
               for tid, t in tasks.items()},
        grader_digest=grader_digest(grader), egress_digest=egress_digest(jobs.fleet, env))
    return put(jobs.fleet, body, job.requester)
