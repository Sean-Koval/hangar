"""Experiment jobs: task × variant × trial under pinned, isolated conditions (--- interfaces; spec 0014, 0007 §5.4).

The slice of spec 0007's experiments hangar has to own, because it needs sandboxes, credentials and pinning; the rest
(grading rubrics, effect sizes, leaderboards) is the sister tool's. An experiment is a job labelled
`hangar.kind: experiment`. It runs no agent itself: a background runner (no job slot, which its trials need) starts
one ordinary job per trial and records, per trial, its effective harness manifest, its trace export and its
verification, plus an optional external grader's output. The result is an index of trials (`hangar.experiment/v1`),
not an analysis.

**Variants.** An environment plus an overlay: `harness` (optionally `harness@version`, which must be the locked one),
`model` (a policy is resolved once, when the experiment starts, and every trial gets that concrete id), `approvals`
(`auto` or `deny-risky`: nobody is there to answer `ask`), `skills` (the full set the agent gets: skills of the
environment by name, or directories next to the environment's spec by path; `[]` for none; the same replace
semantics an environment's `skills` has under `extends`), `skills_remove` (names dropped from that set, else from the
environment's) and `instructions` (replace the environment's global instructions) or `instructions_append`. The
variant is the only overlay on an environment and gains no new fields: it shrinks to a harness lock reference (#135).

**Isolation (0007 finding 3).** Each trial's sandbox mounts only its own harness's pack (`hangar.packs: selected`; on
Kubernetes a template of its own, never a warm sandbox of the environment, which mounts them all), gets exactly the
skills and instructions its variant declares (written when its sandbox is created, every time it is, so a resumed
trial has them too), and nothing else of the experiment. Egress is fixed per experiment: every variant's environment
must have the same egress policy, or the experiment is refused.

**Pinning.** The harness version (the lock at the start; a lock change mid-way stops new trials), the model id, the
base commit of each task (resolved from GitHub at the start, else from the first trial's clone, and checked out in
every trial's sandbox), the approvals policy. Trials of one task run interleaved across variants, so provider latency
and rate limits hit every variant alike. The manifests' `config_digest` shows whether that held: trials of one variant
that differ are flagged (`drift`).

**Budgets and fairness.** Trials are jobs: each has the per-trial budget (#85), the daily budgets apply, and they
queue in the fair job queue under the experiment's person (#89). The experiment's own `budget` stops it starting
trials once its trials together have spent it; `parallel` caps how many run at once.

**Grading.** `grader.command` runs outside the agent's sandbox: in a clean sandbox of the same environment, on the
pinned base with the trial's patch applied, after the trial ended. It runs the trial's agent-written code beside the
hidden tests, so its sandbox is sealed (delivery.sealed, --- sealed #126): no credential, no `secrets:`, no cloud
token, a fetch-only grant, none of the environment's `mcp:`, `computer:` or services, and egress `none` with no host
at all (`grader.network: mirror` adds hangar's read-only package mirror), so the code can't send the tests anywhere.
The grader's files are written outside the repo (`files_mode: pristine`, the default; `in_repo` keeps the old
behaviour, recorded as `trust: low`), and `hangar-grade pytest` runs them so that nothing the patch adds
(`conftest.py`, pytest settings, `sitecustomize.py`, a plugin) loads first. Files the patch adds or changes that could
hook a test run are listed in the grade (`tamper_signals`).

**Grade anti-forgery (#127).** Where the sandbox has the uid split (Docker, isolation.split), a wrapper running as
hangar's user starts the grader command as the agent (no capabilities, no_new_privs) and owns everything the grade is
made of: the exit code, the output, `$HANGAR_GRADE_OUT` and the hidden tests' junit reach it through pipes it
created, and it writes grade.json itself, in a 0700 directory of its own. Each grade says how far it can be trusted:
`trust: isolated` (the uid split, the pristine copy and `hangar-grade`), `best_effort` (no uid split: GKE) or `low`
(`files_mode: in_repo`, or a command that isn't `hangar-grade`). Code under test still runs inside the test runner's
process and can lie through it; the detectors are a base-must-fail check per task (hidden tests that pass on the
pinned base make the task `invalid_task`, as does a `reference_patch` that fails them), a canary string in every
hidden test file (seen in a trial's patch, RESULT.md, trace or egress: `contaminated`), and `touched_tests`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hangar import people
from hangar.models import JOB_TERMINAL, Budget, ContextItem, Job, JobBudget, JobState, now

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.jobs import JobService
    from hangar.models import Sandbox, Session

log = logging.getLogger("hangar.experiments")

SCHEMA = "hangar.experiment/v1"
KIND_LABEL = "hangar.kind"
KIND = "experiment"
EXP_LABEL = "hangar.experiment"  # on a trial's job, session and sandboxes: the experiment's job id
TRIAL_LABEL = "hangar.experiment.trial"  # the trial's key: <task>/<variant>/<n>
PIN_CHECKED_LABEL = "hangar.experiment.pin-checked"  # --- #178: its turn 1 was checked against its pin set
VARIANT_LABEL = "hangar.experiment.variant"
GRADER_LABEL = "hangar.experiment.grader"  # on a grader's sandbox
PACKS_LABEL = "hangar.packs"  # "selected": mount only the sandbox's own harness's pack
KV_OVERLAY = "experiment-overlay:"  # + <experiment>:<variant>: what its sandboxes get
KV_BASE = "experiment-base:"  # + <experiment>:<task>: the pinned base commit
KV_TRIAL = "experiment-trial:"  # + <trial job id> -> <experiment>: labels anyone can set; this only the runner
NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"
MAX_SKILL_BYTES = 1024 * 1024  # a variant's skills, all files together
MAX_GRADER_FILES = 1024 * 1024
GRADE_OUT = "/tmp/hangar-grade/out.json"
GRADE_WORK = "/tmp/hangar-grade"
# --- sealed (#126): where a grader's hidden tests and `hangar-grade` go, outside the repo: root-owned on Docker (whose
# exec picks the uid; hangar's own user's with the uid split), else under /tmp (a provider whose sandbox runs as one
# user can't keep them from it: the trial's code could change them, which the grade's trust says)
GRADER_ROOT = "/var/lib/hangar-grader"
GRADER_ROOT_SHARED = "/tmp/hangar-grader"
# --- anti-forgery (#127): the grade's own directory with the uid split, hangar's user's and 0700
GRADE_DIR = "/var/lib/hangar/grade"
GRADE_FD, JUNIT_FD = 198, 199  # the pipes $HANGAR_GRADE_OUT and $HANGAR_GRADE_JUNIT name (/proc/self/fd/N)
MAX_JUNIT = 8 * 1024 * 1024
MAX_REFERENCE_PATCH = 1024 * 1024
CANARY_PREFIX = "HANGAR-CANARY-"
CANARY_MASK = "HANGAR-CANARY-[hidden]"
TAIL = 4000


# ====================================================================== the spec
class SkillRef(BaseModel):
    """One skill a variant's agent gets: one of the environment's (`name`), or a directory next to the
    environment's spec (`path`, as an environment's own `skills:` entries)."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    path: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _one(self) -> SkillRef:
        if bool(self.name) == bool(self.path):
            raise ValueError("a skill is either `name` (one of the environment's) or `path`")
        return self


# --- #135: the overlay fields, deprecated now that a variant can name a harness lock. Removal is scheduled for the
# release after flightdeck v0 ships its first report (then SkillRef, these fields, _overlay and KV_OVERLAY go).
OVERLAY_FIELDS = ("harness", "model", "approvals", "skills", "skills_remove", "instructions", "instructions_append")
LOCK_REF = r"^[a-z0-9][a-z0-9._-]{0,62}@sha256:[0-9a-f]{64}$"


class Variant(BaseModel):
    """An environment plus a harness: `{harness_lock: name@sha256:..., environment}` (#135), or the deprecated overlay.

    `harness_lock` names a lock in the harness registry by its digest (a channel or revision could move between two
    trials); every trial is claimed from it (#134) and its turn 1 checked against it. It excludes the overlay fields:
    the lock already fixes the harness, its CLI version, the concrete model, approvals, skills and instructions.

    The overlay (deprecated, spec 0014 §4) is the experiment's (then the environment's) unset fields plus: `skills`
    with the semantics an environment's `skills` has under `extends` (#132), the whole set, `[]` for none;
    `skills_remove` drops named ones. `hangar experiment freeze-variants EXP` turns an overlay experiment into locks."""

    model_config = ConfigDict(extra="forbid")
    environment: str | None = None
    harness_lock: str | None = Field(None, max_length=200, pattern=LOCK_REF)  # --- #135: name@sha256:<digest>
    harness: str | None = Field(None, max_length=80)  # "codex" or "codex@0.162.1" (must be the locked version)
    model: str | None = Field(None, max_length=200)  # an id or a policy (frontier, balanced)
    approvals: Literal["auto", "deny-risky"] | None = None
    skills: list[SkillRef] | None = Field(None, max_length=50)  # the whole set; None: the environment's
    # drop these by name from the set above (else from the environment's), as an environment's own skills_remove
    skills_remove: list[str] | None = Field(None, max_length=50)
    instructions: str | None = Field(None, max_length=64_000)  # replaces the environment's global instructions
    instructions_append: str | None = Field(None, max_length=64_000)

    @model_validator(mode="after")
    def _lock_or_overlay(self) -> Variant:
        if self.harness_lock and (mixed := self.overlay_fields()):
            raise ValueError(f"a variant with `harness_lock` takes no overlay fields ({', '.join(mixed)}): the lock "
                             "fixes the harness, model, approvals, skills and instructions. Keep `harness_lock` and "
                             "`environment`, or freeze a new lock with the change (`hangar harness freeze`)")
        return self

    def overlay_fields(self) -> list[str]:
        """The deprecated overlay fields this variant sets."""
        return [f for f in OVERLAY_FIELDS if getattr(self, f) is not None]


class Task(BaseModel):
    """A prompt with its repo, or a GitHub issue (`owner/repo#N`, whose text goes to the agent as untrusted
    context)."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=NAME)
    instructions: str | None = Field(None, max_length=64_000)
    issue: str | None = Field(None, pattern=r"^[\w.-]+/[\w.-]+#\d+$")
    repo: str | None = Field(None, max_length=500)
    base_branch: str | None = Field(None, max_length=200)
    base_sha: str | None = Field(None, pattern=r"^[0-9a-f]{40}$")
    verify: str | None = Field(None, max_length=2000)
    title: str | None = Field(None, max_length=200)
    # --- anti-forgery (#127): a known-good patch for the base check: the hidden tests must pass with it
    reference_patch: str | None = Field(None, max_length=MAX_REFERENCE_PATCH)

    @model_validator(mode="after")
    def _what(self) -> Task:
        if not self.instructions and not self.issue:
            raise ValueError(f"task {self.id}: give `instructions` or an `issue`")
        return self


class Grader(BaseModel):
    """A command run outside the agent's sandbox after each trial: in a clean, sealed sandbox, on the trial's
    patch. `files` (the hidden tests) go to `$HANGAR_HIDDEN_TESTS`, outside the repo (`files_mode: pristine`), or
    into the repo as before (`in_repo`, recorded as `trust: low`). `network`: `none` (nothing), or `mirror` (hangar's
    read-only package mirror, when it is on, for a grader that installs packages)."""

    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=4000)
    timeout_seconds: int = Field(900, ge=30, le=3600)
    files: dict[str, str] = Field(default_factory=dict, max_length=64)  # relative path -> content (hidden tests)
    files_mode: Literal["pristine", "in_repo"] = "pristine"
    network: Literal["none", "mirror"] = "none"
    # --- anti-forgery (#127): written as a comment into every hidden test file; generated when the experiment
    # starts if unset (HANGAR-CANARY-<uuid4>). Never reuse one across experiments.
    canary: str | None = Field(None, min_length=16, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")

    @field_validator("files")
    @classmethod
    def _files(cls, v: dict[str, str]) -> dict[str, str]:
        if sum(len(x.encode()) for x in v.values()) > MAX_GRADER_FILES:
            raise ValueError("grader files are limited to 1 MB in all")
        for p in v:
            if p.startswith("/") or any(x in ("..", ".", "") for x in p.split("/")) or "\0" in p or not p.strip():
                raise ValueError(f"grader file '{p[:60]}' must be a relative path, without '..'")
        return v


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    environment: str
    harness: str | None = None
    model: str | None = None
    provider: str | None = None
    tasks: list[Task] = Field(min_length=1, max_length=100)
    variants: dict[str, Variant] = Field(min_length=1, max_length=8)
    trials: int = Field(1, ge=1, le=20)
    parallel: int | None = Field(None, ge=1, le=64)
    trial_budget: Budget | None = None  # each agent turn of a trial
    trial_job_budget: JobBudget | None = None  # each trial, all its turns
    budget: JobBudget | None = None  # the whole experiment: no new trial starts past it
    timeout_minutes: int | None = Field(None, ge=1, le=720)
    verify_in_loop: bool = False  # the in-turn gate differs by harness; off unless asked for
    keep_minutes: int = Field(30, ge=0, le=1440)
    grader: Grader | None = None
    # --- #178: a variant's pin set (POST /api/trials makes one variant per pin set); unset, _begin records one
    pin_sets: dict[str, str] | None = None
    source: Literal["experiment", "shadow"] = "experiment"  # --- #178: who asked for the trials (#183: shadow)
    # --- #180: the trial cache. Flightdeck's task digests (else computed), and whether finished trials fill it
    # (a sentinel's don't: it compares against what the cache holds)
    task_digests: dict[str, str] | None = None
    cache_record: bool = True

    @field_validator("variants")
    @classmethod
    def _names(cls, v: dict[str, Variant]) -> dict[str, Variant]:
        for name in v:
            if not re.match(NAME, name):
                raise ValueError(f"variant name '{name[:40]}': letters, digits, '.', '_', '-'")
        return v

    @model_validator(mode="after")
    def _unique(self) -> ExperimentSpec:
        ids = [t.id for t in self.tasks]
        if dup := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"task ids must be unique: {', '.join(dup)}")
        return self


# ====================================================================== the index (job.result.data)
class Trial(BaseModel):
    key: str
    task: str
    variant: str
    trial: int
    job: str | None = None
    state: Literal["pending", "running", "grading", "done", "cancelled", "skipped"] = "pending"
    outcome: str | None = None  # the trial job's state: succeeded | failed | cancelled
    reason: str | None = None
    session: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    turns: int = 0
    manifests: list[str | None] = Field(default_factory=list)  # each turn's manifest digest
    config_digest: str | None = None
    harness_digest: str | None = None  # the first turn's manifest's two halves of config_digest (None: older)
    environment_digest: str | None = None
    verify: dict[str, Any] | None = None
    tests: Any = None  # the trial job's test results, when hangar parsed them
    usage: dict[str, Any] | None = None
    grade: dict[str, Any] | None = None
    stored: list[str] = Field(default_factory=list)  # files kept for it: manifest.json, trace.json
    drift: list[str] = Field(default_factory=list)
    # --- anti-forgery (#127)
    touched_tests: list[str] = Field(default_factory=list)  # existing test files its work changed
    contaminated: bool = False  # the grader's canary was seen in what the trial produced
    contamination: list[str] = Field(default_factory=list)  # where: patch, result, trace, egress
    # --- #166: agent | infra_error | cancelled | human (None: unknown, from before). An infra_error trial is
    # `excluded` from statistics and runs again as a fresh trial with the same key, the next `attempt`, and
    # `retry_of` naming the excluded trial's job
    outcome_class: str | None = None
    infra_reason: str | None = None
    excluded: bool = False
    attempt: int = 1
    retry_of: str | None = None
    pin_set: str | None = None  # --- #178: what it ran against (pinsets.py)
    cached: bool = False  # --- #180: it is in the trial cache (trialcache.py)

    @property
    def ref(self) -> str:
        """The trial's own name: its key, and the attempt after the first (`look/a/1~2`)."""
        return self.key if self.attempt <= 1 else f"{self.key}~{self.attempt}"


class State(BaseModel):
    schema_: str = Field(SCHEMA, alias="schema")
    stage: Literal["start", "running", "done"] = "start"
    spec: dict[str, Any] = Field(default_factory=dict)
    pins: dict[str, Any] = Field(default_factory=dict)  # variants: {...}, tasks: {...}
    trials: list[Trial] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    finished_at: float | None = None
    task_checks: dict[str, Any] = Field(default_factory=dict)  # --- anti-forgery (#127): the base check, per task
    infra_stopped: str | None = None  # --- #166: why no further trial starts: more infra errors than the cap

    model_config = ConfigDict(populate_by_name=True)


def is_experiment(job: Job) -> bool:
    return job.labels.get(KIND_LABEL) == KIND


def load(job: Job) -> State:
    try:
        return State.model_validate(job.result.data) if isinstance(job.result.data, dict) else State()
    except ValidationError:
        log.warning("experiment %s has a state hangar can't read", job.id)
        return State(stage="done", notes=["the experiment's state could not be read"])


def _save(jobs: JobService, job: Job, st: State) -> None:
    job.result.data = st.model_dump(mode="json", by_alias=True)
    jobs._save(job)


def _dir(settings: Any, exp: str, key: str | None = None) -> Path:
    d = settings.data_dir / "experiments" / re.sub(r"[^A-Za-z0-9._-]", "_", exp)
    return d / key.replace("/", "__") if key else d


# ====================================================================== submitting
def submit(jobs: JobService, spec: ExperimentSpec, caller: Any, source: str = "api") -> Job:
    """The experiment's job (`hangar.kind: experiment`), checked now so a bad spec fails here, not in the runner."""
    from hangar.jobs import JobError, SubmitJob

    n = len(spec.tasks) * len(spec.variants) * spec.trials
    if n > jobs.settings.experiment_max_trials:
        raise JobError(f"{n} trials (tasks x variants x trials) is over HANGAR_EXPERIMENT_MAX_TRIALS "
                       f"({jobs.settings.experiment_max_trials})")
    try:
        egress = _egress(jobs.fleet, spec)
    except KeyError as ex:
        raise JobError(str(ex).strip("'\"")) from ex
    if len(set(egress.values())) > 1:
        raise JobError("egress is fixed per experiment, and these environments differ in it: "
                       + "; ".join(f"{k}: {v}" for k, v in egress.items()))
    for vname, v in spec.variants.items():
        if v.harness_lock:  # --- #135: a lock that can't be claimed in the variant's environment fails now
            _variant_lock(jobs.fleet, spec, vname, v)
            continue
        harness, _ = _variant_harness(jobs.fleet, spec, vname, v)  # an unknown or unlocked harness version fails now
        _variant_model(jobs.fleet, spec, vname, v, harness, record=False)  # #167: a floating alias fails now
    req = SubmitJob(instructions=f"Experiment {spec.name}: {len(spec.tasks)} task(s) x {len(spec.variants)} "
                                 f"variant(s) x {spec.trials} trial(s).", title=f"experiment: {spec.name}"[:200],
                    environment=spec.environment, deliver="none", labels={KIND_LABEL: KIND},
                    job_budget=spec.budget, verify="", autofix="off")
    job = jobs.submit(req, caller, source=source)
    if job.result.data is None:  # not an idempotent repeat; nothing awaits between submit and here
        st = State(spec=spec.model_dump(mode="json", exclude_none=True), notes=deprecation_notes(spec))
        _save(jobs, job, st)
    return job


def deprecation_notes(spec: ExperimentSpec) -> list[str]:
    """--- #135: a note per variant that uses the deprecated overlay instead of a harness lock."""
    return [f"variant {vname}: the overlay fields ({', '.join(fields)}) are deprecated and will be removed in the "
            "release after flightdeck v0's first report; pin a harness lock instead (`harness_lock: name@sha256:...`; "
            f"`hangar experiment freeze-variants` prints one from a finished trial)"
            for vname, v in spec.variants.items() if (fields := v.overlay_fields())]


def _variant_lock(fleet: Fleet, spec: ExperimentSpec, vname: str, v: Variant) -> dict[str, Any]:
    """--- #135: the variant's harness lock, claimed in its environment (harness_thaw.pin: the CLI version must be the
    one hangar runs, the environment must bind its MCP servers). The experiment's own `harness`/`model` defaults
    don't apply to a lock variant: the lock fixes both."""
    from hangar import harness_thaw
    from hangar.jobs import JobError

    try:
        return harness_thaw.pin(fleet, v.harness_lock or "", v.environment or spec.environment)
    except harness_thaw.ThawError as e:
        raise JobError(f"variant {vname}: {e}") from e


def _egress(fleet: Fleet, spec: ExperimentSpec) -> dict[str, str]:
    """Each of the experiment's environments' egress policy, canonical JSON (KeyError: one is gone)."""
    envs = {v.environment or spec.environment for v in spec.variants.values()} | {spec.environment}
    out: dict[str, str] = {}
    for name in sorted(envs):
        e = fleet.catalog.get(name)
        out[name] = json.dumps(e.egress.model_dump(mode="json") if e.egress else None, sort_keys=True)
    return out


def _variant_harness(fleet: Fleet, spec: ExperimentSpec, vname: str, v: Variant) -> tuple[str, str | None]:
    from hangar.harnesses import get_harness
    from hangar.jobs import JobError

    name, _, version = (v.harness or spec.harness or "").partition("@")
    env = fleet.catalog.get(v.environment or spec.environment)
    try:
        harness = env.pick_harness(name or None)
    except ValueError as e:
        raise JobError(f"variant {vname}: {e}") from e
    if harness is None:
        raise JobError(f"variant {vname}: environment {env.name} has no agent harness")
    h = get_harness(harness)
    locked = h.version if h.install else None
    if version and version != locked:
        raise JobError(f"variant {vname}: {harness}@{version} isn't what hangar runs: its lock pins "
                       f"{locked or 'no version'} (harnesses.lock.yaml); trials run the locked version only")
    return harness, locked


def _variant_model(fleet: Fleet, spec: ExperimentSpec, vname: str, v: Variant, harness: str,
                   record: bool = True) -> tuple[Any, dict[str, Any]]:
    """#167: the variant's model, resolved (a policy to the id it names now), and refused unless that id is concrete:
    a floating alias (`claude-opus-latest`, `opus`, `gpt-5`, OpenRouter's `~…`) can move between two trials. Returns
    the resolution and what the pins record about it (`model_concrete`, `model_concrete_basis`, a note). A harness
    with no model list (nothing to pin: it runs its own default) passes, recorded as `model_concrete: null`."""
    from hangar import modelpolicy, pins
    from hangar.jobs import JobError

    env = fleet.catalog.get(v.environment or spec.environment)
    value = v.model or spec.model or env.model_for(harness) or pins.JOB_DEFAULT
    resolver = modelpolicy.of(fleet)
    r = resolver.resolve(harness, value, record=record)
    if r.model is None and harness not in modelpolicy.PROVIDER and modelpolicy.is_policy(value):
        return r, {"model_concrete": None, "model_concrete_basis": "none",
                   "note": f"variant {vname}: {harness} has no model list, so no model id is pinned (its own default)"}
    ok, basis, note = resolver.concreteness(harness, r.model)
    if not ok:
        got = resolver.suggest(harness)
        hint = "; ".join(f"`{p}` resolves to `{m}` now" for p, m in got.items())
        what = {"alias": "a floating alias the provider can move between two trials",
                "none": "no model id (the harness's own default, which can change)",
                "unknown": "not a model id hangar can tell is concrete (not in the provider's model list, nor of a "
                           "known versioned shape)"}.get(basis, "not a concrete id")
        raise JobError(f"variant {vname}: model `{r.model or value}` is {what}: an experiment pins concrete, dated "
                       "model ids so every trial runs the same model. Pin one"
                       + (f" ({hint}), or name the policy itself, which the experiment resolves once at its start"
                          if hint else " (such as `claude-opus-5-5`)") + (f". {note}" if note else ""))
    return r, {"model_concrete": True, "model_concrete_basis": basis, "note": note}


# ====================================================================== the runner
def _runners(jobs: JobService) -> dict[str, asyncio.Task]:
    return jobs.__dict__.setdefault("_experiment_runners", {})


async def process(jobs: JobService, job: Job) -> None:
    """From JobService._process: start (or keep) the experiment's runner and return; it holds no job slot."""
    runners = _runners(jobs)
    if (t := runners.get(job.id)) is not None and not t.done():
        return
    task = asyncio.create_task(_run(jobs, job.id))
    runners[job.id] = task
    jobs._background.add(task)
    task.add_done_callback(jobs._background.discard)
    task.add_done_callback(lambda _t, jid=job.id: runners.pop(jid, None) if runners.get(jid) is _t else None)


def restore(jobs: JobService, job: Job) -> bool:
    """At startup: an unfinished experiment goes back to its runner. True when it was handled here."""
    if not is_experiment(job) or job.state in JOB_TERMINAL or job.state == JobState.AWAITING_APPROVAL:
        return False
    jobs._queue.put_nowait(job.id)
    return True


class _Stop(Exception):  # noqa: N818 - the experiment ends with this reason
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


async def _run(jobs: JobService, job_id: str) -> None:
    job = jobs.get(job_id)
    st = load(job)
    graders: dict[str, asyncio.Task] = {}
    try:
        if not st.spec:
            raise _Stop("error", "an experiment job needs its spec: submit it with POST /api/experiments or "
                                 "`hangar experiment run`")
        spec = ExperimentSpec.model_validate(st.spec)
        if spec.grader is not None and not spec.grader.canary:  # --- anti-forgery (#127): one per experiment
            spec.grader.canary = new_canary()
            st.spec.setdefault("grader", {})["canary"] = spec.grader.canary
            _save(jobs, job, st)
        if st.stage == "start":
            await _begin(jobs, job, st, spec)
        await _loop(jobs, job, st, spec, graders)
    except asyncio.CancelledError:
        for t in graders.values():
            t.cancel()
        raise  # hangar is stopping: the state is saved; the runner resumes after the restart
    except _Stop as e:
        st.stage, st.finished_at = "done", now()
        _save(jobs, job, st)
        jobs._event(job, f"failed ({e.reason}): {str(e)[:1500]}", JobState.FAILED, reason=e.reason)
    except Exception as e:  # noqa: BLE001 - one experiment must not take the job service down
        log.exception("experiment %s failed", job.id)
        st.stage, st.finished_at = "done", now()
        _save(jobs, job, st)
        jobs._event(job, f"error: {e}", JobState.FAILED, reason="error")


async def _begin(jobs: JobService, job: Job, st: State, spec: ExperimentSpec) -> None:
    """Pin what every trial runs with, and lay the trials out interleaved: for each round of trials, each task's
    variants side by side."""
    from hangar.jobs import JobError

    fleet = jobs.fleet
    variants: dict[str, Any] = {}
    overlays: dict[str, Any] = {}  # --- #178: an overlay variant's, for its pin set's overlay_digest
    for vname, v in spec.variants.items():
        if v.harness_lock:  # --- #135: everything the variant runs with comes from the lock
            try:
                lk = _variant_lock(fleet, spec, vname, v)
            except JobError as e:
                raise _Stop("harness_lock", str(e)) from e
            env = fleet.catalog.get(v.environment or spec.environment)
            # no overlay: the claim mounts the lock's instructions, skills and plugins (harness_thaw.mount_files)
            fleet.store.kv_set(f"{KV_OVERLAY}{job.id}:{vname}", json.dumps({"skills": None, "instructions": None}))
            variants[vname] = {"environment": env.name, "harness": lk["harness"], "harness_version": lk["version"],
                               "model": lk["model"], "model_policy": None, "model_source": "harness_lock",
                               "model_concrete": True if lk["model"] else None,
                               "model_concrete_basis": "harness_lock" if lk["model"] else "none",
                               "approvals": lk["approvals"] or env.approvals or jobs.settings.approvals,
                               "skills": None, "instructions": None,
                               "harness_lock": v.harness_lock, "harness_revision": lk["revision"],
                               "harness_digest": lk["digest"]}
            continue
        harness, locked = _variant_harness(fleet, spec, vname, v)
        env = fleet.catalog.get(v.environment or spec.environment)
        try:  # #167: checked at submit too; the lists may have moved since
            r, concrete = _variant_model(fleet, spec, vname, v, harness)
        except JobError as e:
            raise _Stop("model_not_pinned", str(e)) from e
        if note := concrete.pop("note", None):
            st.notes.append(note)
        overlay = overlays[vname] = _overlay(env, v)
        fleet.store.kv_set(f"{KV_OVERLAY}{job.id}:{vname}", json.dumps(overlay))
        variants[vname] = {"environment": env.name, "harness": harness, "harness_version": locked,
                           "model": r.model, "model_policy": r.policy, "model_source": r.source, **concrete,
                           "approvals": v.approvals or env.approvals or jobs.settings.approvals,
                           "skills": [{"name": s["name"], "digest": s.get("digest"), "source": s["source"]}
                                      for s in overlay["skills"]] if overlay["skills"] is not None else None,
                           "instructions": overlay.get("instructions_digest"),
                           "harness_digest": None}  # --- #135: an overlay's, from its first trial's turn 1
    tasks: dict[str, Any] = {}
    for t in spec.tasks:
        env = fleet.catalog.get(spec.environment)
        repo = t.repo or (f"https://github.com/{t.issue.split('#')[0]}.git" if t.issue else None) or (
            env.repo.url if env.repo else None)
        pinned = t.base_sha or await _github_sha(jobs, repo, t.base_branch)
        if pinned:
            fleet.store.kv_set(f"{KV_BASE}{job.id}:{t.id}", pinned)
        issue_text = await _issue_text(jobs, t.issue) if t.issue else None
        from hangar import trialcache  # --- #180: the task's identity for the trial cache

        tasks[t.id] = {"repo": repo, "base_branch": t.base_branch, "base_sha": pinned,
                       "issue": t.issue, "issue_text": issue_text,
                       "task_digest": (spec.task_digests or {}).get(t.id)
                       or trialcache.task_digest(t, spec.environment, spec.grader)}
    # --- review(#114): egress too: an approved egress request widens an environment mid-experiment
    _pin_sets(jobs, job, st, spec, variants, tasks, overlays)  # --- #178
    st.pins = {"variants": variants, "tasks": tasks, "egress": _egress(fleet, spec), "pinned_at": now()}
    st.trials = [Trial(key=f"{t.id}/{vname}/{i}", task=t.id, variant=vname, trial=i)
                 for i in range(1, spec.trials + 1) for t in spec.tasks for vname in spec.variants]
    st.stage = "running"
    _save(jobs, job, st)
    jobs._event(job, f"experiment {spec.name}: {len(st.trials)} trial(s), at most "
                     f"{spec.parallel or jobs.settings.experiment_parallel} at once; pinned "
                     + "; ".join(f"{k}: {v['harness']}@{v['harness_version'] or '?'} on {v['model'] or 'its default'}"
                                 for k, v in variants.items()), JobState.RUNNING, reason=None)


def _pin_sets(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, variants: dict[str, Any],
              tasks: dict[str, Any], overlays: dict[str, Any]) -> None:
    """--- #178: each variant's pin set. A trials request names it (`spec.pin_sets`): what the lock claims now must be
    what it pinned. Otherwise the variant's pins become one (the same content gives the same pin set)."""
    from hangar import pinsets

    for vname, vp in variants.items():
        want = (spec.pin_sets or {}).get(vname)
        if want:
            try:
                p = pinsets.get(jobs.fleet, want)
            except KeyError as e:
                raise _Stop("pin_set", str(e).strip("'\"")) from e
            if p.harness_digest and vp.get("harness_digest") != p.harness_digest:
                raise _Stop("pin_set", f"variant {vname}: the lock claims {vp.get('harness_digest')}, but pin set "
                                       f"{p.id} pinned {p.harness_digest}")
            if p.harness_version != vp.get("harness_version"):
                raise _Stop("pin_set", f"pin set {p.id} ran {p.harness} {p.harness_version or '(no version)'}, and "
                                       f"this hangar runs {vp.get('harness_version') or '(none)'}: its pack can't be "
                                       "claimed any more; resolve a new pin set on what hangar runs now")
            for tid, t in tasks.items():
                pinned = p.tasks.get(tid)
                if pinned is not None and pinned.base_sha and t.get("base_sha") != pinned.base_sha:
                    raise _Stop("pin_set", f"task {tid}: base {t.get('base_sha')} isn't pin set {p.id}'s "
                                           f"{pinned.base_sha}")
        else:
            env = vp.get("environment") or spec.environment
            p = pinsets.experiment_pin(jobs, job, vname, vp, tasks, env, spec.grader, overlays.get(vname))
        vp["pin_set"] = p.id


def _overlay(env: EnvSpec, v: Variant) -> dict[str, Any]:
    """What a variant's sandboxes get on top of the environment's image: skills (their files, read now, so the
    experiment keeps the version it started with) and instructions."""
    from hangar.envspec import _contained_path, remove_skills, skill_name
    from hangar.jobs import JobError
    from hangar.manifest import _dir_digest

    skills: list[dict[str, Any]] | None = None
    known = {skill_name(s) for s in env.skills}
    refs = v.skills
    if v.skills_remove is not None:
        base = refs if refs is not None else [SkillRef(name=n) for n in (skill_name(s) for s in env.skills) if n]
        try:
            refs = remove_skills([r.model_dump() for r in base], v.skills_remove, "variant")
        except ValueError as e:
            raise JobError(str(e)) from e
        refs = [SkillRef.model_validate(r) for r in refs]
    if refs is not None:
        skills = []
        total = 0
        for ref in refs:
            if ref.name:
                if ref.name not in known:
                    raise JobError(f"skill '{ref.name}' isn't one of environment {env.name}'s "
                                   f"({', '.join(sorted(x for x in known if x)) or 'none'})")
                skills.append({"name": ref.name, "source": "environment"})
                continue
            try:
                src = _contained_path(env.source_dir or Path("."), ref.path or "")
            except (ValueError, FileNotFoundError) as e:
                raise JobError(f"skill {ref.path}: {e}") from e
            files: dict[str, str] = {}
            for f in sorted(p for p in src.rglob("*") if p.is_file() and not p.is_symlink()):
                data = f.read_bytes()
                total += len(data)
                if total > MAX_SKILL_BYTES:
                    raise JobError("a variant's skills are limited to 1 MB of files in all")
                files[f.relative_to(src).as_posix()] = base64.b64encode(data).decode()
            digest, _ = _dir_digest(str(src.resolve()), src.stat().st_mtime_ns)
            skills.append({"name": src.name, "source": f"path:{ref.path}", "digest": digest, "files": files})
    out: dict[str, Any] = {"skills": skills, "instructions": None}
    if v.instructions is not None or v.instructions_append:
        base = v.instructions if v.instructions is not None else (env.agent_config.instructions or "")
        text = base + (("\n\n" if base else "") + v.instructions_append if v.instructions_append else "")
        import hashlib

        out["instructions"] = text
        out["instructions_digest"] = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    return out


async def _github_sha(jobs: JobService, repo: str | None, branch: str | None) -> str | None:
    """The head of the task's base branch on GitHub, when hangar can ask; else the first trial's clone pins it."""
    from hangar.github import parse_repo

    name = parse_repo(repo)
    gh = jobs._github() if name else None
    if gh is None or name is None:
        return None
    try:
        return await gh.branch_sha(name, branch or await gh.default_branch(name))
    except Exception as e:  # noqa: BLE001
        log.info("could not read %s's base from GitHub: %s", name, e)
        return None
    finally:
        await gh.close()


async def _issue_text(jobs: JobService, issue: str) -> str:
    repo, _, number = issue.partition("#")
    gh = jobs._github()
    if gh is None:
        raise _Stop("issue", f"task issue {issue}: hangar has no GitHub token (HANGAR_GITHUB_TOKEN_CREDENTIAL); "
                             "give the task's `instructions` instead")
    try:
        d = await gh._req("GET", f"/repos/{repo}/issues/{int(number)}")
    except Exception as e:  # noqa: BLE001
        raise _Stop("issue", f"could not read issue {issue}: {e}") from e
    finally:
        await gh.close()
    return jobs.broker.redact(f"# {d.get('title') or ''}\n\n{d.get('body') or ''}")[:60_000]


async def _loop(jobs: JobService, job: Job, st: State, spec: ExperimentSpec,
                graders: dict[str, asyncio.Task]) -> None:
    poll = max(0.05, min(5.0, jobs.settings.job_poll_seconds))
    parallel = spec.parallel or jobs.settings.experiment_parallel
    shown: set[str] = set()
    for t in st.trials:  # a restart while grading: grade again
        if t.state == "grading":
            graders[t.ref] = asyncio.create_task(_grade(jobs, job, st, spec, t))
    while True:
        if job.cancel_requested:
            await _cancel(jobs, job, st, graders)
            return
        changed = False
        for t in st.trials:
            if t.state != "running" or not t.job:
                continue
            child = jobs._jobs.get(t.job)
            if child is None:
                t.state, t.reason, changed = "done", "its job is gone", True
                continue
            if child.state == JobState.AWAITING_APPROVAL and child.id not in shown:
                shown.add(child.id)
                jobs._event(job, f"trial {t.key} (job {child.id}) waits for approval: "
                                 f"{child.approval.reason if child.approval else 'see the job'}")
            if child.state == JobState.NEEDS_INPUT and child.id not in jobs._active:
                jobs.cancel(child.id, f"experiment:{job.id}")  # nobody answers an experiment's agents
            if child.state not in JOB_TERMINAL and not child.cancel_requested and t.pin_set:
                _check_pin(jobs, job, t, child)  # --- #178: turn 1 against the pin set; drift ends it
            if child.state not in JOB_TERMINAL and not child.cancel_requested and _infra_turn(child) is not None:
                # --- #166: a restart or the power-down cut its turn off and the job would resume from a checkpoint:
                # not the conditions the other trials ran under. It ends here and runs again as a fresh trial.
                with contextlib.suppress(Exception):
                    jobs.cancel(child.id, f"experiment:{job.id}")
            if child.state in JOB_TERMINAL and child.id not in jobs._active:
                await _finish_trial(jobs, job, st, spec, t, child)
                if spec.grader and t.state == "grading":
                    graders[t.ref] = asyncio.create_task(_grade(jobs, job, st, spec, t))
                changed = True
        for key, task in list(graders.items()):
            if task.done():
                graders.pop(key)
                changed = True
        running = sum(1 for t in st.trials if t.state in ("running", "grading"))
        pending = [t for t in st.trials if t.state == "pending"]
        if pending and running < parallel:
            if why := _stop_starting(jobs, job, st, spec):
                for t in pending:
                    t.state, t.reason = "skipped", why
                st.notes.append(why)
                jobs._event(job, f"no more trials start: {why}")
            else:
                for t in pending[: parallel - running]:
                    _start_trial(jobs, job, st, spec, t)
            changed = True
        if changed:
            _save(jobs, job, st)
        if not any(t.state in ("pending", "running", "grading") for t in st.trials) and not graders:
            break
        await asyncio.sleep(poll)
    st.stage, st.finished_at = "done", now()
    counts: dict[str, int] = {}
    for t in st.trials:
        what = "excluded (infra_error)" if t.excluded else t.outcome or t.state  # --- #166
        counts[what] = counts.get(what, 0) + 1
    job.result.summary = (f"Experiment {spec.name}: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
                          + ". The trials' manifests, traces and results are in the index (GET "
                          f"/api/experiments/{job.id}).")
    _save(jobs, job, st)
    jobs._event(job, job.result.summary, JobState.SUCCEEDED, reason=None)


def _stop_starting(jobs: JobService, job: Job, st: State, spec: ExperimentSpec) -> str | None:
    """Why no further trial may start: the experiment's budget is spent, or the harness lock changed under it."""
    from hangar.harnesses import HARNESSES

    if st.infra_stopped:  # --- #166: more infra errors than the retry cap: a provider outage, usually
        return st.infra_stopped
    for vname, v in st.pins.get("variants", {}).items():
        h = HARNESSES.get(v["harness"])
        now_v = h.version if h is not None and h.install else None
        if now_v != v.get("harness_version"):
            return (f"the harness lock changed mid-experiment ({v['harness']} {v.get('harness_version')} -> "
                    f"{now_v}, variant {vname}): the trials left would not run under the same conditions")
    if (pinned := st.pins.get("egress")) is not None:  # --- review(#114)
        try:
            current = _egress(jobs.fleet, spec)
        except KeyError as e:
            return f"an environment of the experiment is gone ({str(e).strip(chr(39))[:200]})"
        if changed := sorted(k for k in pinned if current.get(k) != pinned[k]):
            return (f"the egress policy of {', '.join(changed)} changed mid-experiment: the trials left would not "
                    "run under the same conditions")
    b = spec.budget
    if b is None:
        return None
    cost, tokens, turns = 0.0, 0, 0
    for t in st.trials:
        if t.job and (child := jobs._jobs.get(t.job)) is not None:
            u = jobs.usage(child)
            cost += (u.cost_usd or 0.0) if u else 0.0
            tokens += u.tokens if u else 0
            turns += sum(1 for x in child.turns if x.run_id)
    if b.max_cost_usd is not None and cost >= b.max_cost_usd:
        return f"its budget is spent (${cost:.2f} of ${b.max_cost_usd:.2f})"
    if b.max_tokens is not None and tokens >= b.max_tokens:
        return f"its budget is spent ({tokens:,} of {b.max_tokens:,} tokens)"
    if b.max_turns is not None and turns >= b.max_turns:
        return f"its budget is spent ({turns} of {b.max_turns} agent turns)"
    return None


def _start_trial(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, t: Trial) -> None:
    from hangar.jobs import Caller, JobError, SubmitJob

    task = next(x for x in spec.tasks if x.id == t.task)
    v = spec.variants[t.variant]
    pin = st.pins["variants"][t.variant]
    tp = st.pins["tasks"][t.task]
    context = []
    instructions = task.instructions or f"Resolve GitHub issue {task.issue}: its text is in the context."
    if tp.get("issue_text"):
        context.append(ContextItem(name=f"issue-{task.issue.replace('/', '-').replace('#', '-')}.md",
                                   kind="issue", untrusted=True, content=tp["issue_text"]))
    req = SubmitJob(
        instructions=instructions, title=f"{spec.name}: {t.ref}"[:200], repo=tp.get("repo"),
        base_branch=task.base_branch, environment=v.environment or spec.environment, harness=pin["harness"],
        model=pin["model"], harness_lock=pin.get("harness_lock"),  # --- #135: claimed from the variant's lock
        provider=spec.provider, verify=task.verify, deliver="none", context=context,
        keep_minutes=spec.keep_minutes, timeout_minutes=spec.timeout_minutes, budget=spec.trial_budget,
        job_budget=spec.trial_job_budget, idempotency_key=f"experiment:{job.id}:{t.ref}",
        verify_in_loop=spec.verify_in_loop, autofix="off", queue_class="eval",  # --- #181
        labels={EXP_LABEL: job.id, TRIAL_LABEL: t.key, VARIANT_LABEL: t.variant, PACKS_LABEL: "selected"})
    try:
        child = jobs._submit(req, Caller(job.requester, job.owner, person=job.person), source="experiment",
                             untrusted_source=job.untrusted_source)
    except JobError as e:
        t.state, t.outcome, t.reason, t.finished_at = "done", "failed", f"could not start: {e}", now()
        return
    jobs.fleet.store.kv_set(KV_TRIAL + child.id, job.id)  # before it can start: nothing awaits in between
    t.job, t.state, t.started_at = child.id, "running", now()
    t.pin_set = pin.get("pin_set")  # --- #178


async def _finish_trial(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, t: Trial, child: Job) -> None:
    """Record what the trial ran with and what came of it, and keep its manifest and trace export."""
    from hangar import manifest, traces

    t.outcome, t.reason, t.finished_at = child.state.value, child.reason, now()
    t.outcome_class, t.infra_reason = trial_class(child)  # --- #166
    r = child.result
    t.verify = {"command": child.verify, "exit_code": r.verify_exit_code, "passed": (
        r.verify_exit_code == 0 if r.verify_exit_code is not None else None), "new_failures": r.new_failures,
        "baseline_red": r.baseline.red if r.baseline else None, "tail": (r.verify_tail or "")[-TAIL:] or None}
    t.touched_tests = list(r.touched_tests or [])[:100]  # --- anti-forgery (#127): the job's own check of its tests
    tests = getattr(r, "tests", None)  # the testresults work's TestSummary, when hangar parsed the check's reports
    t.tests = tests.model_dump(mode="json") if isinstance(tests, BaseModel) else tests
    if (u := jobs.usage(child)) is not None:
        t.usage = {"cost_usd": u.cost_usd, "tokens": u.tokens, "input_tokens": u.input_tokens,
                   "output_tokens": u.output_tokens, "cache_read_tokens": u.cache_read_tokens, "model": u.model}
    s = jobs._session(child)
    d = _dir(jobs.settings, job.id, t.ref)
    if s is not None:
        t.session, t.turns = s.name, sum(1 for x in s.turns if x.run_id)
        t.manifests = [x.manifest for x in s.turns if x.run_id]
        first = next((n for n, x in enumerate(s.turns, 1) if x.manifest), None)
        m = manifest.store(jobs.settings).get(s.id, first) if first else None
        try:
            export = await traces.export_job(jobs, child)
            await asyncio.to_thread(_write, d, {"trace.json": export, **({"manifest.json": m} if m else {})})
            t.stored = ["trace.json", *(["manifest.json"] if m else [])]
        except Exception as e:  # noqa: BLE001 - the index still has the rest
            log.warning("experiment %s: could not store trial %s's files: %s", job.id, t.key, e)
            st.notes.append(f"trial {t.key}: its trace and manifest could not be stored ({e})")
        if m:
            t.config_digest = m.get("config_digest")
            t.harness_digest, t.environment_digest = m.get("harness_digest"), m.get("environment_digest")
            same = [x for x in st.trials if x.variant == t.variant and x.config_digest and x is not t]
            if same and same[0].config_digest != t.config_digest:
                t.drift.append(drift_message(t, same[0]))
            vp = st.pins.get("variants", {}).get(t.variant) or {}
            if vp.get("harness_lock"):  # --- #135: turn 1 must have run exactly the variant's lock
                if t.harness_digest != vp.get("harness_digest"):
                    t.drift.append(f"harness drift: turn 1's harness_digest {t.harness_digest} isn't the variant's "
                                   f"lock {vp.get('harness_lock')}")
            elif t.harness_digest and not vp.get("harness_digest") and vp:
                vp["harness_digest"] = t.harness_digest  # an overlay variant's digest, as its first trial ran it
    t.state = "grading" if spec.grader and child.state != JobState.CANCELLED and s is not None else "done"
    retry = None
    _cache(jobs, job, st, spec, t)  # --- #180: a counted trial of a pinned key fills the cache
    if t.outcome_class == "infra_error" and t.infra_reason == "drift":  # --- #178: not what was pinned; not again
        t.excluded, t.state = True, "done"
        st.notes.append(f"trial {t.ref}: its turn 1 didn't run pin set {t.pin_set} (drift): excluded, not retried")
    elif t.outcome_class == "infra_error":  # --- #166: not the agent's result: left out, and run again
        t.excluded, t.state = True, "done"
        retry = _retry(jobs, st, spec, t)
    jobs._event(job, f"trial {t.ref}: {t.outcome}" + (f" ({t.reason})" if t.reason else "")
                + (f", infra_error/{t.infra_reason}: excluded" if t.excluded else "")
                + (f", retried as {retry.ref}" if t.excluded and retry is not None else "")
                + (", grading" if t.state == "grading" else ""))


def trial_id(job: Job, st: State, t: Trial) -> str:
    """--- #180: a trial's id for GET /api/trials: `<experiment>.<n>` when the experiment has one task and one variant
    (a trials request), else `<experiment>~<task>~<variant>~<n>`."""
    if len({x.task for x in st.trials}) == 1 and len({x.variant for x in st.trials}) == 1:
        return f"{job.id}.{t.trial}"
    return f"{job.id}~{t.task}~{t.variant}~{t.trial}"


def _cache(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, t: Trial) -> None:
    from hangar import pinsets, trialcache

    if not t.pin_set or not spec.cache_record:
        return
    try:
        p = pinsets.get(jobs.fleet, t.pin_set)
    except KeyError:
        return
    td = (st.pins.get("tasks", {}).get(t.task) or {}).get("task_digest")
    if td and trialcache.record(jobs.store, trialcache.parts_for(p, t.task, td), trial_id(job, st, t), t.job,
                                t.outcome, t.outcome_class):
        t.cached = True


def _check_pin(jobs: JobService, job: Job, t: Trial, child: Job) -> None:
    """--- #178: once the trial's turn 1 has its manifest, its digests must be the pin set's. A trial that drifted is
    cancelled as `infra_error` / `drift`: not run further, and not retried."""
    from hangar import manifest, pinsets

    s = jobs._session(child)
    first = next((x for x in s.turns if x.run_id), None) if s is not None else None
    if first is None or not first.harness_digest or child.labels.get(PIN_CHECKED_LABEL):
        return
    child.labels[PIN_CHECKED_LABEL] = "1"
    try:
        p = pinsets.get(jobs.fleet, t.pin_set or "")
    except KeyError:
        return
    m = manifest.store(jobs.settings).get(s.id, s.turns.index(first) + 1) or {}
    found = pinsets.drift(p, first.harness_digest, first.environment_digest,
                          (m.get("environment") or {}).get("spec_digest"))
    if not found:
        jobs._save(child)
        return
    t.drift.extend(f"pin set {p.id}: {d}" for d in found)
    child.infra_cut = pinsets.DRIFT
    jobs._event(child, "drift from pin set " + p.id + ": " + "; ".join(found))
    with contextlib.suppress(Exception):
        jobs.cancel(child.id, f"experiment:{job.id}")


def _infra_turn(child: Job) -> Any:
    """The child job's first agent turn infrastructure ended (#166), if any."""
    return next((x for x in child.turns if x.run_id and x.outcome_class == "infra_error"), None)


def trial_class(child: Job) -> tuple[str | None, str | None]:
    """--- #166: a trial's outcome class. Any of its turns cut off by infrastructure makes the whole trial
    `infra_error`, even if the job resumed and finished: a trial resumed from a checkpoint didn't run under the
    conditions the others did. Else the job's own class (outcomes.job_class)."""
    from hangar import outcomes

    if child.infra_cut:  # --- #181: hangar ended it on purpose (the power-down)
        return "infra_error", child.infra_cut
    if (cut := _infra_turn(child)) is not None:
        return "infra_error", cut.infra_reason
    if child.result.outcome_class == "infra_error":
        return "infra_error", child.result.infra_reason
    return outcomes.job_class(child)


def infra_retry_cap(st: State) -> int:
    """--- #166: how many infra-error retries the experiment makes in all: 10% of its trials (at least one)."""
    first = sum(1 for t in st.trials if t.attempt <= 1)
    return max(1, first // 10)


def _retry(jobs: JobService, st: State, spec: ExperimentSpec, t: Trial) -> Trial | None:
    """--- #166: a fresh trial in place of an infra_error one (same key, the next attempt), at most
    HANGAR_TRIAL_INFRA_RETRIES times per trial and `infra_retry_cap` times per experiment; past the latter no
    further trial starts, with a note (many infra errors at once is usually a provider outage)."""
    if t.attempt > max(0, jobs.settings.trial_infra_retries):
        st.notes.append(f"trial {t.key}: infra_error ({t.infra_reason}) on all {t.attempt} attempts "
                        "(HANGAR_TRIAL_INFRA_RETRIES); it has no counted result")
        return None
    # --- #181: the scheduled power-down isn't an outage: its retries don't count towards the experiment's cap
    cut = {x.job for x in st.trials if x.infra_reason == "power_down" and x.job}
    used = sum(1 for x in st.trials if x.attempt > 1 and x.retry_of not in cut)
    if t.infra_reason != "power_down" and used >= infra_retry_cap(st):
        n = sum(1 for x in st.trials if x.outcome_class == "infra_error")
        why = (f"{n} trials ended in infra_error (the latest {t.ref}: {t.infra_reason}), and the experiment has used "
               f"the {infra_retry_cap(st)} retries it gets (10% of its trials): probably a provider or cluster "
               "outage. No further trial starts; run the experiment again once it is over")
        if not st.infra_stopped:
            st.infra_stopped = why
            st.notes.append(why)
        return None
    retry = Trial(key=t.key, task=t.task, variant=t.variant, trial=t.trial, attempt=t.attempt + 1, retry_of=t.job)
    st.trials.append(retry)
    return retry


def drift_message(t: Trial, other: Trial) -> str:
    """Why two trials of one variant differ, naming the half that drifted: the harness (instructions, skills, MCP
    servers, hooks, model, approvals) or the environment (image, build, egress, resources, isolation)."""
    halves = [name for name, a, b in (("harness", t.harness_digest, other.harness_digest),
                                      ("environment", t.environment_digest, other.environment_digest))
              if a and b and a != b]
    if not halves and not (t.harness_digest and other.harness_digest and t.environment_digest
                           and other.environment_digest):
        return (f"its harness configuration ({t.config_digest}) differs from trial {other.key}'s "
                f"({other.config_digest}): compare their manifests")
    what = " and ".join(f"{h} drift" for h in halves) or "configuration drift"  # neither half: it's in the rest
    detail = ", ".join(f"{h}_digest {getattr(t, h + '_digest')} vs {getattr(other, h + '_digest')}" for h in halves)
    return (f"{what}: its configuration differs from trial {other.key}'s"
            + (f" ({detail})" if detail else f" ({t.config_digest} vs {other.config_digest})")
            + ": compare their manifests")


def _write(d: Path, files: dict[str, Any]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (d / name).write_text(json.dumps(content, default=str))


async def _cancel(jobs: JobService, job: Job, st: State, graders: dict[str, asyncio.Task]) -> None:
    for t in graders.values():
        t.cancel()
    await asyncio.gather(*graders.values(), return_exceptions=True)
    for t in st.trials:
        if t.state == "pending":
            t.state, t.reason = "cancelled", "the experiment was cancelled"
        elif t.state in ("running", "grading"):
            if t.job and (child := jobs._jobs.get(t.job)) is not None and child.state not in JOB_TERMINAL:
                with contextlib.suppress(Exception):
                    jobs.cancel(child.id, f"experiment:{job.id}")
            t.state, t.reason = "cancelled", t.reason or "the experiment was cancelled"
    st.stage, st.finished_at = "done", now()
    _save(jobs, job, st)
    if job.state != JobState.CANCELLED:
        jobs._event(job, "cancelled; its running trials were stopped", JobState.CANCELLED, reason="cancelled")


# ====================================================================== grading
# --- sealed (#126): `hangar-grade`, written into each grader's sandbox (outside the repo, where the trial's code can't
# change it on Docker). The repo's directory joins sys.path only once pytest has started, after its plugins are
# loaded, so a plugin the patch declares (a *.dist-info entry point) never loads; the hidden tests' own conftest.py is
# registered then too (its fixtures and collection hooks work; an option it adds doesn't).
GRADE_RUNNER = r'''import importlib.util, os, sys
repo, tests, junit = sys.argv[1:4]
import pytest


class HangarGrade:
    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session):
        sys.path.insert(0, repo)
        conftest = os.path.join(tests, "conftest.py")
        if os.path.isfile(conftest):
            spec = importlib.util.spec_from_file_location("hangar_hidden_conftest", conftest)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            session.config.pluginmanager.register(mod, "hangar-hidden-conftest")


raise SystemExit(pytest.main(["--noconftest", "-p", "no:cacheprovider", "-c", os.devnull, "--rootdir", tests,
                              "-o", "addopts=", "--junitxml=" + junit, *sys.argv[4:], tests],
                             plugins=[HangarGrade()]))
'''
GRADE_HELPER = r"""#!/bin/sh
# hangar-grade: run an experiment's hidden tests from their pristine copy (written by hangar; docs/jobs.md#grading).
#   hangar-grade pytest [PYTEST ARGS...]   the tests in $HANGAR_HIDDEN_TESTS against the code in $HANGAR_REPO
# Nothing the repo carries loads first: no conftest.py (--noconftest), no pytest settings (-c /dev/null, addopts
# cleared), no sitecustomize.py, usercustomize.py or user site (python -I), no plugin it declares, no cache, no
# bytecode. Junit goes to $HANGAR_GRADE_JUNIT, which hangar reads back. Only Python (pytest) in this version.
set -u
case "${1:-}" in
  pytest) shift ;;
  *) echo "usage: hangar-grade pytest [pytest args...]" >&2; exit 2 ;;
esac
[ -d "${HANGAR_HIDDEN_TESTS:-}" ] || { echo "hangar-grade: \$HANGAR_HIDDEN_TESTS is not a directory" >&2; exit 2; }
[ -d "${HANGAR_REPO:-}" ] || { echo "hangar-grade: \$HANGAR_REPO is not a directory" >&2; exit 2; }
py=${HANGAR_GRADE_PYTHON:-}
[ -n "$py" ] || py=$(command -v python3 || command -v python) || { echo "hangar-grade: no python" >&2; exit 2; }
junit=${HANGAR_GRADE_JUNIT:-/tmp/hangar-grade/junit.xml}
case "$junit" in
  /proc/self/fd/*|/dev/fd/*) ;;  # hangar's pipe (the uid split): hangar reads it, nothing to clear
  *) mkdir -p "$(dirname "$junit")" && rm -f "$junit" ;;
esac
cd "$HANGAR_HIDDEN_TESTS" || exit 2
unset PYTEST_ADDOPTS PYTEST_PLUGINS PYTHONSTARTUP PYTHONPATH PYTHONHOME PYTHONSAFEPATH
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
exec "$py" -I -B -c "$(cat <<'HANGAR_PY'
@RUNNER@HANGAR_PY
)" "$HANGAR_REPO" "$HANGAR_HIDDEN_TESTS" "$junit" "$@"
""".replace("@RUNNER@", GRADE_RUNNER)
# Files that can hook a Python test run before (or around) the tests: by name, and config files by their section
TAMPER_NAMES = {"conftest.py": "conftest", "sitecustomize.py": "sitecustomize", "usercustomize.py": "usercustomize",
                "pytest.ini": "pytest-config", ".pytest.ini": "pytest-config", "pytest.toml": "pytest-config"}
TAMPER_SECTIONS = {"tox.ini": re.compile(r"^\[(pytest|tool:pytest)\]", re.M),
                   "setup.cfg": re.compile(r"^\[tool:pytest\]", re.M),
                   "pyproject.toml": re.compile(r"^\[tool\.pytest(\.ini_options)?\]", re.M)}
_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)


def patch_files(patch: str) -> list[str]:
    """The paths a patch adds or changes (not those it deletes), in order."""
    out: list[str] = []
    for block in re.split(r"(?m)^(?=diff --git )", patch):
        m = _DIFF_FILE.match(block)
        if not m or re.search(r"(?m)^deleted file mode ", block.split("\n@@", 1)[0]):
            continue
        path = m.group(2).strip().strip('"')
        if path not in out:
            out.append(path)
    return out


def tamper_candidates(patch: str) -> tuple[list[dict[str, str]], list[str]]:
    """The patch's files that can hook a test run by their name alone, and the config files whose content decides
    it (tox.ini, setup.cfg, pyproject.toml: only with a pytest section)."""
    hits: list[dict[str, str]] = []
    configs: list[str] = []
    for path in patch_files(patch):
        name = path.rsplit("/", 1)[-1]
        if name in TAMPER_NAMES:
            hits.append({"path": path, "kind": TAMPER_NAMES[name]})
        elif name.endswith(".pth"):
            hits.append({"path": path, "kind": "pth"})
        elif name in TAMPER_SECTIONS:
            configs.append(path)
    return hits, configs


def tamper_config(path: str, content: str) -> dict[str, str] | None:
    pat = TAMPER_SECTIONS.get(path.rsplit("/", 1)[-1])
    return {"path": path, "kind": "pytest-config"} if pat is not None and pat.search(content) else None


async def _tamper_signals(prov: Any, sb: Any, repo: str, patch: str) -> list[dict[str, str]]:
    """Signals, not refusals: the patch's added or changed files that can load before, or around, the tests."""
    hits, configs = tamper_candidates(patch)
    for path in configs:
        raw = await prov.read_file(sb, f"{repo}/{path}", limit=256 * 1024)
        if raw and (hit := tamper_config(path, raw[:256 * 1024].decode(errors="replace"))):
            hits.append(hit)
    return hits


# --- anti-forgery (#127): run as hangar's own user (the uid split), it starts the grader's command as the agent (no
# capabilities, no_new_privs: driver.as_agent) and keeps everything the grade is made of out of the agent's reach: the
# command's output, its exit code, and $HANGAR_GRADE_OUT and $HANGAR_GRADE_JUNIT, which are pipes it created (owned by
# the agent's uid, so the command reopens them through /proc/self/fd, and only there). It writes grade.json, out
# and junit.xml into its own 0700 directory. Standard library only: it runs on the sandbox's python3, isolated (-I).
GRADE_WRAPPER = r'''import ctypes, json, os, pwd, selectors, signal, subprocess, sys, time
user, home, envf, cwd, timeout, outdir, launch, command = sys.argv[1:9]
timeout = int(timeout)
GRADE_FD, JUNIT_FD, TAIL, OUT_MAX, JUNIT_MAX = @CONSTS@
libc = ctypes.CDLL(None, use_errno=True)
pw = pwd.getpwnam(user)
libc.setfsuid(pw.pw_uid)
libc.setfsgid(pw.pw_gid)
reads = {}
try:
    for name, fd in (("out", GRADE_FD), ("junit", JUNIT_FD)):
        r, w = os.pipe()
        if w != fd:
            os.dup2(w, fd)
            os.close(w)
        os.set_inheritable(fd, True)
        reads[name] = r
finally:
    libc.setfsuid(os.getuid())
    libc.setfsgid(os.getgid())
owner_ok = os.fstat(GRADE_FD).st_uid == pw.pw_uid
argv = ["setpriv", "--reuid=%d" % pw.pw_uid, "--regid=%d" % pw.pw_gid, "--init-groups", "--inh-caps=-all",
        "--ambient-caps=-all", "--no-new-privs", "--", "env", "HOME=" + home, "USER=" + user, "LOGNAME=" + user,
        "bash", "-c", launch, "hangar-grade", envf, cwd, "bash", "-c", command]
p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     pass_fds=(GRADE_FD, JUNIT_FD), start_new_session=True)
os.close(GRADE_FD)
os.close(JUNIT_FD)
caps = {"stdout": TAIL, "stderr": TAIL, "out": OUT_MAX + 1, "junit": JUNIT_MAX + 1}
bufs = {k: bytearray() for k in caps}
total = {k: 0 for k in caps}
sel = selectors.DefaultSelector()
for name, f in (("stdout", p.stdout.fileno()), ("stderr", p.stderr.fileno()), *reads.items()):
    os.set_blocking(f, False)
    sel.register(f, selectors.EVENT_READ, name)
deadline, timed_out, grace = time.monotonic() + timeout, False, None


def kill():
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


while sel.get_map():
    now = time.monotonic()
    if not timed_out and now > deadline:
        timed_out = True
        kill()
    if grace is None and p.poll() is not None:
        grace = now + 3  # what it started keeps a pipe open: a few seconds more, then it goes
    if grace is not None and now > grace:
        kill()
        break
    for key, _ in sel.select(0.2):
        name = key.data
        try:
            chunk = os.read(key.fd, 65536)
        except BlockingIOError:
            continue
        if not chunk:
            sel.unregister(key.fd)
            continue
        total[name] += len(chunk)
        b = bufs[name]
        if name in ("stdout", "stderr"):
            b += chunk
            del b[:-caps[name]]
        elif len(b) < caps[name]:
            b += chunk[: caps[name] - len(b)]
try:
    code = p.wait(timeout=5)
except subprocess.TimeoutExpired:
    kill()
    code = p.wait()
if timed_out:
    code = 124
for name, fname in (("out", "out"), ("junit", "junit.xml")):
    with open(os.path.join(outdir, fname), "wb") as f:
        f.write(bytes(bufs[name]))
status = {"exit_code": code, "timed_out": timed_out, "pipe_owner_ok": owner_ok,
          "stdout": bufs["stdout"].decode(errors="replace"), "stderr": bufs["stderr"].decode(errors="replace"),
          "out_bytes": total["out"], "junit_bytes": total["junit"]}
with open(os.path.join(outdir, "grade.json"), "w") as f:
    json.dump(status, f)
print("hangar-grade-wrapper: done", code)
'''.replace("@CONSTS@", f"{GRADE_FD}, {JUNIT_FD}, {TAIL * 4}, 64_000, {MAX_JUNIT}")
# Where a canary comment goes: appended to a hidden test file, in the comment syntax of its language
_HASH = (".py", ".sh", ".bash", ".rb", ".pl", ".r", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".tf", ".ex", ".exs",
         ".jl", ".nim", ".cr", ".mk", ".cmake", ".ps1", ".coffee", ".el")
_SLASH = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".swift",
          ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".dart", ".php", ".groovy", ".zig", ".v", ".sol", ".proto")
_DASH = (".sql", ".lua", ".hs", ".elm", ".ada")
_TAG = (".md", ".html", ".htm", ".xml", ".svg", ".vue")


def with_canary(rel: str, content: str, canary: str | None) -> tuple[str, bool]:
    """The hidden test file with the canary as a comment line at its end, and whether it could carry one (a file
    without comments, JSON, or one of unknown kind is written as it is)."""
    if not canary:
        return content, False
    low = rel.lower()
    if low.endswith(_HASH) or low.rsplit("/", 1)[-1] in ("makefile", "dockerfile"):
        line = f"# {canary}"
    elif low.endswith(_SLASH):
        line = f"// {canary}"
    elif low.endswith(_DASH):
        line = f"-- {canary}"
    elif low.endswith(_TAG):
        line = f"<!-- {canary} -->"
    else:
        return content, False
    return content + ("" if not content or content.endswith("\n") else "\n") + line + "\n", True


def new_canary() -> str:
    import uuid

    return f"{CANARY_PREFIX}{uuid.uuid4()}"


def mask_canary(value: Any, canary: str | None) -> Any:
    """The value with the canary replaced (case-insensitively) wherever it occurs: never in an export."""
    if not canary:
        return value
    pat = re.compile(re.escape(canary), re.I)
    if isinstance(value, str):
        return pat.sub(CANARY_MASK, value)
    if isinstance(value, dict):
        return {k: mask_canary(v, canary) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_canary(v, canary) for v in value]
    return value


def _runner(command: str) -> str:
    """`hangar-grade` when the command is the safe runner itself (after variable assignments), else `command`."""
    try:
        words = shlex.split(command)
    except ValueError:
        return "command"
    first = next((w for w in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)), "")
    return "hangar-grade" if first == "hangar-grade" else "command"


def trust_of(split: bool, files_mode: str, runner: str) -> str:
    """How far a grade can be trusted: `isolated` with the uid split, the pristine copy and `hangar-grade`; `low`
    with the hidden tests in the repo or a command of the author's own; `best_effort` without the uid split."""
    if files_mode == "in_repo" or runner != "hangar-grade":
        return "low"
    return "isolated" if split else "best_effort"


def passed(grade: dict[str, Any] | None) -> bool | None:
    """Whether a grade passed: its hidden tests all passed (when it has them), else its exit code was 0; None when
    it couldn't tell (an error, no exit code)."""
    if not grade or grade.get("error") or grade.get("exit_code") is None:
        return None
    h = grade.get("hidden_tests")
    if isinstance(h, dict) and isinstance(h.get("total"), int) and h["total"] > 0:
        return grade["exit_code"] == 0 and not h.get("failed") and not h.get("error")
    return grade["exit_code"] == 0


def grader_root(prov: Any) -> str:
    """Where a grader's hidden tests and `hangar-grade` go. On Docker hangar writes them as root (its exec and
    archive uploads pick the uid), so the sandbox's user reads them and can't change them; elsewhere the sandbox runs
    as one user, and they go to /tmp."""
    return GRADER_ROOT if getattr(prov, "name", "") == "docker" else GRADER_ROOT_SHARED


async def _grade(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, t: Trial) -> None:
    """The experiment's grader on this trial's work (see _grade_patch), after the task's base check and a scan of
    what the trial produced for the grader's canary (#127)."""
    from hangar import exchange

    g = spec.grader
    assert g is not None
    grade: dict[str, Any] = {"command": g.command, "at": now()}
    try:
        child = jobs.get(t.job or "")
        s = jobs._session(child)
        if s is None:
            raise RuntimeError("the trial has no session")
        try:
            patch = await exchange.session_patch(jobs.sessions, s)
            text, base = patch["patch"], patch.get("base") or s.base_sha
        except exchange.ExchangeError as e:
            text, base = "", s.base_sha
            grade["patch_error"] = str(e)[:300]
        _touched(spec, t, child, text)
        await _canary_scan(jobs, job, st, spec, t, child, text)
        check = await _task_check(jobs, job, st, spec, t.task, t.variant, base)
        grade = await _grade_patch(jobs, job, st, spec, t.task, t.variant, t.key, base, text, grade, trial=child)
        if check.get("invalid"):
            grade["task_invalid"] = True
    except asyncio.CancelledError:
        grade["error"] = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001 - the trial keeps everything else
        grade["error"] = jobs.broker.redact(str(e))[:1000]
    finally:
        t.grade, t.state = mask_canary(grade, g.canary), "done"
        with contextlib.suppress(Exception):
            _save(jobs, job, st)
        if grade.get("test_run") and "trace.json" in t.stored:  # --- #164: the stored trace gets the grader's run
            with contextlib.suppress(Exception):
                from hangar import traces

                export = mask_canary(await traces.export_job(jobs, jobs.get(t.job or "")), g.canary)
                await asyncio.to_thread(_write, _dir(jobs.settings, job.id, t.ref), {"trace.json": export})
        from hangar import outbox  # --- outbox (#163): grade.json under the trial's last run

        outbox.of(jobs.fleet).trial_graded(jobs, job, t)


def _touched(spec: ExperimentSpec, t: Trial, child: Job, patch: str) -> None:
    """--- anti-forgery (#127): the trial's `touched_tests`: the job's own (with the in-loop check), else the existing
    test files its patch changes, renames or deletes (strategies.touched_tests, with the task's verify command)."""
    from hangar.strategies import touched_tests

    if t.touched_tests:
        return
    task = next((x for x in spec.tasks if x.id == t.task), None)
    name_status = []
    for block in re.split(r"(?m)^(?=diff --git )", patch):
        m = _DIFF_FILE.match(block)
        if not m:
            continue
        head = block.split("\n@@", 1)[0]
        if re.search(r"(?m)^new file mode ", head):
            continue
        kind = "D" if re.search(r"(?m)^deleted file mode ", head) else "R" if re.search(r"(?m)^rename from ", head) \
            else "M"
        name_status.append(f"{kind}\t{m.group(1).strip()}")
    t.touched_tests = touched_tests(task.verify if task else None, name_status) or \
        (touched_tests("pytest", name_status) if not (task and task.verify) else [])


async def _canary_scan(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, t: Trial, child: Job,
                       patch: str) -> None:
    """--- anti-forgery (#127): look for the grader's canary in what the trial produced: its patch, its RESULT.md (each
    turn's summary), its trace export, and the egress proxy's records of its sandbox. A hit means the agent had the
    hidden tests (a leaked copy in its context, say): the trial is `contaminated`, the experiment gets a note, and the
    audit log a `grade.canary_seen` entry. The canary itself is never written anywhere."""
    g = spec.grader
    canary = g.canary if g else None
    if not canary:
        return
    needle = canary.lower()
    where: list[str] = []
    if needle in patch.lower():
        where.append("patch")
    summaries = [x.summary for x in child.turns if x.summary] + ([child.result.summary] if child.result.summary
                                                                  else [])
    if any(needle in x.lower() for x in summaries):
        where.append("result")
    trace = _dir(jobs.settings, job.id, t.ref) / "trace.json"
    with contextlib.suppress(OSError):
        if "trace.json" in t.stored and await asyncio.to_thread(_file_has, trace, needle):
            where.append("trace")
    # the egress proxy records host names (it doesn't terminate TLS): a canary sent out in a name shows there
    sandboxes = {child.sandbox_id} | {x.id for x in jobs.fleet.list(include_stopped=True,
                                                                     label=f"hangar.job={child.id}")}
    since = (t.started_at or child.created_at) - 1
    for sid in sorted(x for x in sandboxes if x):
        events = jobs.fleet.store.audit_since(since, ("egress.allow", "egress.deny"), target=sid, limit=2000)
        if any(needle in json.dumps(e.detail, default=str).lower() for e in events):
            where.append("egress")
            break
    if not where:
        return
    t.contaminated, t.contamination = True, where
    st.notes.append(f"trial {t.key} is contaminated: the grader's canary was seen in its {', '.join(where)} (the "
                    "agent had the hidden tests); leave it out of comparisons")
    jobs.fleet.audit(f"experiment:{job.id}", "grade.canary_seen", job.id, trial=t.key, job=child.id, where=where)


def _file_has(path: Path, needle: str) -> bool:
    """Whether a stored file holds `needle` (lower case), read a piece at a time."""
    tail = ""
    with path.open(encoding="utf-8", errors="replace") as f:
        while chunk := f.read(1 << 20):
            text = tail + chunk.lower()
            if needle in text:
                return True
            tail = text[-len(needle):]
    return False


def _checks(jobs: JobService) -> dict[tuple[str, str], asyncio.Future]:
    return jobs.__dict__.setdefault("_experiment_task_checks", {})


async def _task_check(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, task: str, variant: str,
                      base: str | None) -> dict[str, Any]:
    """--- anti-forgery (#127): once per task per experiment, the hidden tests on the pinned base without any patch:
    if they pass there, the task can't tell a fix from no fix (`invalid_task`), and every grade of it says
    `task_invalid`. With the task's `reference_patch`, the hidden tests run on it too, and must pass. Trials of a task
    grading at once wait for one check; it is kept in the index, so a restart doesn't run it again."""
    if (done := st.task_checks.get(task)) and done.get("state") == "done":
        return done
    futs = _checks(jobs)
    k = (job.id, task)
    if (f := futs.get(k)) is not None:
        return await asyncio.shield(f)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    futs[k] = fut
    try:
        out = await _run_task_check(jobs, job, st, spec, task, variant, base)
        fut.set_result(out)
        return out
    except BaseException as e:
        futs.pop(k, None)
        if not fut.done():
            fut.set_exception(e if isinstance(e, Exception) else RuntimeError("cancelled"))
            fut.exception()  # retrieved: a waiter that went away doesn't make it "never retrieved"
        raise


async def _run_task_check(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, task: str, variant: str,
                          base: str | None) -> dict[str, Any]:
    g = spec.grader
    assert g is not None
    out: dict[str, Any] = {"state": "done", "base_sha": base, "at": now(), "invalid": False}
    keep = ("exit_code", "hidden_tests", "error", "trust", "seconds", "applied", "apply_error")

    async def run(key: str, patch: str) -> dict[str, Any]:
        try:
            return await _grade_patch(jobs, job, st, spec, task, variant, key, base, patch, tamper=False)
        except Exception as e:  # noqa: BLE001 - the trials are graded all the same; the check says it didn't run
            return {"error": jobs.broker.redact(str(e))[:500]}

    on_base = await run(f"{task}/base/0", "")
    out["base"] = {k: on_base[k] for k in keep if k in on_base}
    reasons = []
    if passed(on_base) is True:
        reasons.append("hidden tests pass on base")
    t = next(x for x in spec.tasks if x.id == task)
    if t.reference_patch:
        ref = await run(f"{task}/reference/0", t.reference_patch)
        out["reference"] = {k: ref[k] for k in keep if k in ref}
        ok = passed(ref)
        if ok is False or ref.get("applied") is False:
            reasons.append("the reference patch fails the hidden tests" if ref.get("applied") is not False
                           else "the reference patch doesn't apply to the base")
    if reasons:
        out["invalid"], out["reason"] = True, "; ".join(reasons)
        st.notes.append(f"invalid_task {task}: {out['reason']}; its grades say task_invalid")
    elif passed(on_base) is None:
        out["reason"] = "the base check couldn't run: " + str(on_base.get("error") or "no result")[:300]
    st.task_checks[task] = mask_canary(out, g.canary)
    with contextlib.suppress(Exception):
        _save(jobs, job, st)
    return st.task_checks[task]


async def _grade_patch(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, task: str, variant: str,
                       key: str, base: str | None, text: str, grade: dict[str, Any] | None = None,
                       tamper: bool = True, trial: Job | None = None) -> dict[str, Any]:
    """The experiment's grader on one patch (a trial's; none, for the base check; the task's reference patch), in a
    clean, sealed sandbox: the pinned base, the patch, the grader's own files (outside the repo unless
    `files_mode: in_repo`), each with the canary. It runs agent-written code beside the hidden tests, so it holds
    nothing and reaches nothing (GRADER_LABEL: delivery.builds_repo_code, delivery.sealed): the environment's git
    credentials only tell the gateway which one to fetch with. With the uid split the grade is hangar's own
    (_run_split, #127); without, the command's output and $HANGAR_GRADE_OUT are the sandbox user's."""
    from hangar import delivery, isolation
    from hangar.autoinstall import validation_credentials

    fleet = jobs.fleet
    g = spec.grader
    assert g is not None
    t0 = now()
    grade = grade if grade is not None else {"command": g.command, "at": t0}
    grade["patch_bytes"] = len(text.encode())
    sb = None
    try:
        tp = st.pins["tasks"][task]
        env_name = spec.variants[variant].environment or spec.environment
        env = fleet.catalog.get(env_name)
        labels = {GRADER_LABEL: job.id, TRIAL_LABEL: key}
        if g.network == "mirror":
            labels[delivery.NETWORK_LABEL] = "mirror"
        sb = await fleet.create(env_name, repo=tp.get("repo"), branch=next(
            (x.base_branch for x in spec.tasks if x.id == task), None),
            credentials=validation_credentials(fleet, env), provider=spec.provider,
            ttl_minutes=max(10, g.timeout_seconds // 60 + 15), person=job.person, actor=f"experiment:{job.id}",
            labels=labels, purpose="automated")  # --- #125: and strict
        _, prov = fleet._running(sb.id)
        sb = fleet.get(sb.id)
        eg = sb.info.get("egress") or {}
        grade["network"] = "mirror" if eg.get("mirrors") else "none"  # what the sandbox got (sealed: none)
        q = shlex.quote
        repo = sb.info.get("repo_path")
        work = GRADE_WORK
        await fleet.exec(sb.id, f"mkdir -p {work}", actor=f"experiment:{job.id}", quiet=True)
        if repo and base:
            res = await fleet.exec(sb.id, f"(git cat-file -e {q(base)}^{{commit}} 2>/dev/null || git fetch -q origin "
                                          f"{q(base)}) && git checkout -q --detach {q(base)}", cwd=repo, timeout=300,
                                   actor=f"experiment:{job.id}", quiet=True)
            if res.exit_code != 0:
                raise RuntimeError(f"could not check out the base {base[:12]}: {(res.stderr or res.stdout)[-300:]}")
        if repo and text.strip():
            await prov.write_file(sb, f"{work}/trial.patch", text.encode(), mode=0o600)
            res = await fleet.exec(sb.id, f"git apply --binary --whitespace=nowarn {work}/trial.patch", cwd=repo,
                                   timeout=120, actor=f"experiment:{job.id}", quiet=True)
            grade["applied"] = res.exit_code == 0
            if res.exit_code != 0:
                grade["apply_error"] = jobs.broker.redact((res.stderr or res.stdout)[-1000:])
            elif tamper and (signals := await _tamper_signals(prov, sb, repo, text)):  # --- sealed (#126)
                grade["tamper_signals"] = signals
            await fleet.exec(sb.id, f"rm -f {work}/trial.patch", actor=f"experiment:{job.id}", quiet=True)
        # --- sealed (#126): the base and the patch are in; from here the trial's code runs: no host at all (not even
        # the setup's git host), and the sandbox's git grant (its one way to the gateway's upstream) goes
        sb = delivery.lock_sealed(fleet, sb)
        # the hidden tests outside the repo, beside `hangar-grade`; or in the repo (in_repo)
        root = f"{grader_root(prov)}/{re.sub(r'[^A-Za-z0-9._-]', '_', key.replace('/', '__'))}"
        grade["files_mode"] = g.files_mode
        if g.files_mode == "in_repo":
            hidden = repo or fleet.settings.workspace_dir
        else:
            hidden = f"{root}/tests"
            await prov.write_file(sb, f"{root}/bin/hangar-grade", GRADE_HELPER.encode(), mode=0o755)
            if not g.files:  # the directory exists even without files: a command can count on it
                await prov.write_file(sb, f"{hidden}/.hangar-hidden-tests", b"", mode=0o644)
        marked = 0
        for rel, content in g.files.items():  # --- anti-forgery (#127): each with the canary, where it takes one
            body, ok = with_canary(rel, content, g.canary)
            marked += ok
            await prov.write_file(sb, f"{hidden}/{rel}", body.encode(), mode=0o644)
        if g.files:
            grade["canary_files"] = marked
        grade["runner"] = "hangar-grade" if g.files_mode == "pristine" and _runner(g.command) == "hangar-grade" \
            else "command"
        env_vars = {"HANGAR_TRIAL": key, "HANGAR_TASK": task, "HANGAR_VARIANT": variant, "HANGAR_EXPERIMENT": job.id,
                    "HANGAR_HIDDEN_TESTS": hidden, "HANGAR_REPO": repo or fleet.settings.workspace_dir}
        path = f"{root}/bin" if g.files_mode == "pristine" else None
        # --- anti-forgery (#127): with the uid split, hangar's user runs the command as the agent and owns the grade
        split = isolation.supported(prov) and await isolation.ensure(fleet, prov, sb)
        ran = await _run_split(jobs, prov, sb, g, key, repo, env_vars, path) if split else None
        if ran is None:
            if split:
                grade.setdefault("notes", []).append("the uid split's grade wrapper couldn't run (python3 in the "
                                                     "image?): graded as the sandbox's user")
            split = False
            ran = await _run_shared(jobs, prov, sb, g, repo, env_vars, path, f"experiment:{job.id}")
        grade["trust"] = trust_of(split, g.files_mode, grade["runner"])
        grade["isolation"] = "split" if split else (sb.info.get("isolation") or {}).get("why") or "shared"
        exit_code, tail, raw, junit = ran
        grade["exit_code"] = exit_code
        grade["tail"] = jobs.broker.redact(tail[-TAIL:])
        if (hidden_tests := _junit_counts(junit)) is not None:
            grade["hidden_tests"] = hidden_tests
        if exit_code == 124 and grade["network"] == "none":
            grade.setdefault("notes", []).append("the grader timed out with no network: if it fetches a dependency, "
                                                 "that isn't in the image (grader.network: mirror, or a repo build)")
        if raw:
            try:
                grade["data"] = jobs.broker.redact(json.loads(raw[:64_000]))
            except ValueError:
                grade["data_error"] = "$HANGAR_GRADE_OUT isn't JSON (or is over 64 KB)"
        if trial is not None:  # --- #164: the hidden tests' results as a `grader` TestRun of the trial's job
            with contextlib.suppress(Exception):
                grade["test_run"] = record_grader_run(jobs, trial, g, grade, junit).id
    finally:
        grade["seconds"] = round(now() - t0, 1)
        if sb is not None:
            with contextlib.suppress(Exception):
                await fleet.destroy(sb.id, actor=f"experiment:{job.id}")
    return grade


async def _run_shared(jobs: JobService, prov: Any, sb: Any, g: Grader, repo: str | None, env_vars: dict[str, str],
                      path: str | None, actor: str) -> tuple[int, str, bytes | None, bytes | None]:
    """The grader's command as the sandbox's user (no uid split): its output and files are that user's, as is the
    code it grades (`trust: best_effort` at most)."""
    q = shlex.quote
    junit = f"{GRADE_WORK}/hidden-junit.xml"
    env = {**env_vars, "HANGAR_GRADE_OUT": GRADE_OUT, "HANGAR_GRADE_JUNIT": junit}
    cmd = (f"PATH={q(path)}:\"$PATH\" " if path else "") + " ".join(f"{k}={q(v)}" for k, v in env.items()) + \
        f" timeout {g.timeout_seconds} bash -c " + q(g.command)
    res = await jobs.fleet.exec(sb.id, cmd, cwd=repo, timeout=g.timeout_seconds + 30, actor=actor)
    raw = await prov.read_file(sb, GRADE_OUT, limit=64_000)
    xml = await prov.read_file(sb, junit, limit=MAX_JUNIT)
    return res.exit_code, (res.stdout or "") + (res.stderr or ""), raw, xml


async def _run_split(jobs: JobService, prov: Any, sb: Any, g: Grader, key: str, repo: str | None,
                     env_vars: dict[str, str], path: str | None) -> tuple[int, str, bytes | None, bytes | None] | None:
    """--- anti-forgery (#127): the grader's command under GRADE_WRAPPER, as hangar's user, in a 0700 directory of
    its own (GRADE_DIR/<trial>): the command runs as the agent, and what makes the grade reaches the wrapper through
    pipes it created. None when the wrapper couldn't run at all (then the caller grades without it)."""
    from hangar import isolation
    from hangar.driver import LAUNCH
    from hangar.providers.docker import SYSTEM_PATH

    q = shlex.quote
    fleet = jobs.fleet
    user = fleet.settings.sandbox_user
    d = f"{GRADE_DIR}/{re.sub(r'[^A-Za-z0-9._-]', '_', key.replace('/', '__'))}-{sb.id}"
    res = await isolation.sh(prov, sb, f"umask 077 && mkdir -p {q(GRADE_DIR)} && chmod 0700 {q(GRADE_DIR)} && "
                                       f"rm -rf {q(d)} && mkdir -m 0700 {q(d)}")
    if res.exit_code != 0:
        log.warning("grade %s: no grade directory: %s", key, (res.stderr or res.stdout)[-300:])
        return None
    env = {**env_vars, "HANGAR_GRADE_OUT": f"/proc/self/fd/{GRADE_FD}",
           "HANGAR_GRADE_JUNIT": f"/proc/self/fd/{JUNIT_FD}"}
    exports = "".join(f"export {k}={q(v)}\n" for k, v in env.items())
    if path:
        exports += f"export PATH={q(path)}:\"$PATH\"\n"
    await isolation.put(prov, sb, d, {"wrapper.py": (GRADE_WRAPPER.encode(), 0o600)})
    try:  # the agent's PATH is the image's (its tools); hangar's side never resolves through it
        container = await prov._container(sb)
        image_path = next((e[5:] for e in (container.attrs.get("Config") or {}).get("Env") or []
                           if e.startswith("PATH=")), SYSTEM_PATH)
    except Exception:  # noqa: BLE001
        image_path = SYSTEM_PATH
    # agent.env must be the agent's to read: the wrapper's directory is 0700, so it goes beside the hidden tests
    shared = f"{GRADER_ROOT}/{d.rsplit('/', 1)[-1]}.env"
    await prov.write_file(sb, shared, exports.encode(), mode=0o644)
    argv = [user, f"/home/{user}", shared, repo or fleet.settings.workspace_dir, str(g.timeout_seconds), d, LAUNCH,
            g.command]
    caps = isolation.CAPS
    cmd = (f"cd {q(isolation.HOME)} && setpriv --reuid={isolation.USER} --regid={isolation.USER} --init-groups "
           f"--inh-caps={caps} --ambient-caps={caps} -- env HOME={isolation.HOME} USER={isolation.USER} "
           f"LOGNAME={isolation.USER} PATH={SYSTEM_PATH} HANGAR_AGENT_PATH={q(image_path)} "
           f"python3 -I {q(d + '/wrapper.py')} " + " ".join(q(a) for a in argv))
    res = await prov.exec(sb, cmd, timeout=g.timeout_seconds + 60, user="root", cwd="/")
    raw = await prov.read_file(sb, f"{d}/grade.json", limit=1024 * 1024)
    if not raw or "hangar-grade-wrapper: done" not in (res.stdout or ""):
        log.warning("grade %s: the wrapper didn't finish (exit %s): %s", key, res.exit_code,
                    ((res.stderr or "") + (res.stdout or ""))[-300:])
        return None
    try:
        st = json.loads(raw)
    except ValueError:
        return None
    out = await prov.read_file(sb, f"{d}/out", limit=64_000)
    junit = await prov.read_file(sb, f"{d}/junit.xml", limit=MAX_JUNIT)
    tail = str(st.get("stdout") or "") + str(st.get("stderr") or "")
    if not st.get("pipe_owner_ok", True):
        tail += "\n[hangar] the grade's pipes weren't the agent's to reopen"
    return int(st.get("exit_code", -1)), tail, out or None, junit or None


def record_grader_run(jobs: JobService, trial: Job, g: Grader, grade: dict[str, Any], junit: bytes | None) -> Any:
    """--- #164: a trial's grade as a `grader` TestRun (testresults.record, the store the verify runs use): the junit
    `$HANGAR_GRADE_JUNIT` received (what `hangar-grade` writes), read through testreports' size caps; with no report,
    a run from the exit code alone, `complete: false`. Trusted only with `trust: isolated`: below it, the code under
    test could have written the report. Its tests are the hidden tests: their ids and messages pass through the broker
    and the canary mask, and never reach the repo's flaky list. On the trial's job, its last agent turn."""
    from hangar import testreports, testresults

    results = testreports.Results()
    if junit:
        rep = testreports.parse(junit[:MAX_JUNIT], "hidden-junit.xml")
        results = testreports.merge([rep])
    else:
        results.complete = False
        results.errors.append("the grader wrote no report to $HANGAR_GRADE_JUNIT: its exit code only")

    def redact(text: str) -> str:
        return mask_canary(jobs.broker.redact(text), g.canary)

    last = next((x.run_id for x in reversed(trial.turns) if x.run_id), None)
    return testresults.record(jobs.fleet.store, results=results, kind="grader", redact=redact, repo=trial.repo,
                              environment=trial.environment, job_id=trial.id, session_id=trial.session_id,
                              turn=len(trial.turns) or None, person=trial.person, command=g.command,
                              exit_code=grade.get("exit_code"), agent_run_id=last,
                              trusted=grade.get("trust") == "isolated")


def _junit_counts(raw: bytes | None) -> dict[str, Any] | None:
    """The hidden tests' results from the junit `hangar-grade` wrote: counts per outcome."""
    from hangar import testreports

    if not raw:
        return None
    r = testreports.parse_junit(raw[:MAX_JUNIT], "hidden-junit.xml")
    if r.error:
        return {"error": r.error[:300]}
    counts = {k: 0 for k in ("passed", "failed", "error", "skipped")}
    for c in r.cases:
        counts[c.outcome] += 1
    return {**counts, "total": len(r.cases), "truncated": r.truncated}


# ====================================================================== a trial's sandbox (fleet.create)
def selected_packs(sb: Sandbox) -> bool:
    """A trial's sandbox: one pack, never from (nor refilling) the environment's warm pool."""
    return sb.labels.get(PACKS_LABEL) == "selected"


def select_packs(spec: EnvSpec, sb: Sandbox, harness: str | None) -> None:
    """`hangar.packs: selected`: only the sandbox's own harness's pack is mounted."""
    if selected_packs(sb):
        spec.packs = {k: v for k, v in spec.packs.items() if k == harness}


def provider_spec(spec: EnvSpec, sb: Sandbox) -> EnvSpec:
    """A sandbox with selected packs doesn't come from the environment's warm pool (whose sandboxes mount every
    pack): on Kubernetes it has a template of its own, per harness."""
    if sb.labels.get(PACKS_LABEL) != "selected":
        return spec
    harness = sb.info.get("harness") or "none"
    return spec.model_copy(update={"name": f"{spec.name[:40]}-p{harness}"[:63], "warm_pool": 0,
                                   "warm_pool_hours": None})


def overlay_for(fleet: Fleet, sb: Sandbox) -> dict[str, Any] | None:
    """The overlay a trial's sandbox gets (None: not a trial's). A trial whose overlay is gone fails closed."""
    exp, var = sb.labels.get(EXP_LABEL), sb.labels.get(VARIANT_LABEL)
    if not exp or not var or fleet.store.kv_get(KV_TRIAL + sb.labels.get("hangar.job", "")) != exp:
        return None  # not a trial the experiment started (a job or session can carry any labels)
    raw = fleet.store.kv_get(f"{KV_OVERLAY}{exp}:{var}")
    if raw is None:
        from hangar.fleet import FleetError

        raise FleetError(f"experiment {exp}'s overlay for variant {var} is missing; the trial can't run as declared")
    return json.loads(raw)


async def apply_overlay(fleet: Fleet, prov: Any, sb: Sandbox, spec: EnvSpec, harness: str | None) -> None:
    """Write the variant's skills and instructions into a trial's sandbox: exactly the skills it declares (the
    environment's others are removed; hangar's own `verify-ui` stays with a computer), and its instructions.
    After this, a repo with a `.hangar/environment.yaml` or a build gets hangar's environment section spliced into
    the instructions and its `hangar-environment` skill (svc_control.tell_agent), alike for every variant on the
    environment; the manifest's effective skills and instructions digest show them."""
    from hangar.harnesses import HARNESSES

    o = overlay_for(fleet, sb)
    h = HARNESSES.get(harness or "")
    if o is None or h is None:
        return
    home = f"/home/{fleet.settings.sandbox_user}"
    q = shlex.quote
    if o.get("skills") is not None and h.skills_dir:
        root = f"{home}/{h.skills_dir}"
        keep = {s["name"] for s in o["skills"]} | ({"verify-ui"} if spec.computer else set())
        res = await prov.exec(sb, f"mkdir -p {q(root)} && cd {q(root)} && for d in */; do "
                                  "[ -d \"$d\" ] && printf '%s\\0' \"${d%/}\"; done; true", timeout=60)
        if res.exit_code != 0:
            raise RuntimeError(f"could not set the variant's skills: {res.stderr[-300:]}")
        if drop := sorted(d for d in res.stdout.split("\0") if d and d not in keep):
            res = await prov.exec(sb, f"cd {q(root)} && rm -rf -- {' '.join(q(d) for d in drop)}", timeout=60)
            if res.exit_code != 0:
                raise RuntimeError(f"could not remove the environment's other skills: {res.stderr[-300:]}")
        for s in o["skills"]:
            files = s.get("files")
            if files is None:
                continue
            await prov.exec(sb, f"rm -rf -- {q(root + '/' + s['name'])}", timeout=60)
            for rel, b64 in files.items():
                await prov.write_file(sb, f"{root}/{s['name']}/{rel}", base64.b64decode(b64), mode=0o644)
    if o.get("instructions") is not None and h.instructions_file:
        await prov.write_file(sb, f"{home}/{h.instructions_file}", o["instructions"].encode(), mode=0o644)
    sb.info["experiment"] = {"id": sb.labels.get(EXP_LABEL), "variant": sb.labels.get(VARIANT_LABEL),
                             "skills": [s["name"] for s in o["skills"]] if o.get("skills") is not None else None,
                             "instructions": o.get("instructions_digest")}


def effective_spec(fleet: Fleet, sb: Sandbox, spec: EnvSpec) -> EnvSpec:
    """The environment as a trial's variant sees it, for code that reads instructions or skills from the spec rather
    than the sandbox (harden.py's hangar-owned settings for a turn that asks; the manifest's declared skills and
    instructions, so a variant's `skills` and `skills_remove` show as what it declares)."""
    from hangar.envspec import Skill, skill_name

    try:
        o = overlay_for(fleet, sb)
    except Exception:  # noqa: BLE001
        return spec
    if o is None:
        return spec
    update: dict[str, Any] = {}
    if o.get("instructions") is not None:
        update["agent_config"] = spec.agent_config.model_copy(update={"instructions": o["instructions"]})
    if o.get("skills") is not None:
        by_name = {skill_name(x): x for x in spec.skills}
        skills = []
        for x in o["skills"]:
            if x.get("source") == "environment" and x["name"] in by_name:
                skills.append(by_name[x["name"]])
            elif str(x.get("source") or "").startswith("path:"):
                skills.append(Skill(path=x["source"][5:], name=x["name"]))
        update["skills"] = skills
    return spec.model_copy(update=update) if update else spec


async def prepare_trial(jobs: JobService, job: Job, s: Session) -> None:
    """From JobService._provision, before the trial's first turn: its variant's approvals, and its task's pinned
    base commit (the first trial to clone pins it when GitHub couldn't say)."""
    from hangar.jobs import JobError

    exp, key = job.labels.get(EXP_LABEL), job.labels.get(TRIAL_LABEL)
    if not exp or not key or job.source != "experiment" or jobs.fleet.store.kv_get(KV_TRIAL + job.id) != exp:
        return
    parent = jobs._jobs.get(exp)
    st = load(parent) if parent is not None else None
    task, variant = key.split("/")[0], job.labels.get(VARIANT_LABEL, "")
    if st is not None and (vp := st.pins.get("variants", {}).get(variant)):
        s.approvals = vp.get("approvals")
    sb = jobs.fleet.get(job.sandbox_id or "")
    repo = sb.info.get("repo_path")
    if not repo:
        return
    kv = f"{KV_BASE}{exp}:{task}"
    pin = jobs.fleet.store.kv_get(kv)
    if pin is None:
        if s.base_sha:
            jobs.fleet.store.kv_set(kv, s.base_sha)
        return
    if pin == s.base_sha:
        return
    q = shlex.quote
    res = await jobs._sh(job, f"(git cat-file -e {q(pin)}^{{commit}} 2>/dev/null || git fetch -q origin {q(pin)}) && "
                              f"git checkout -q -B {q(s.branch)} {q(pin)}", cwd=repo, timeout=300)
    if res.exit_code != 0:
        raise JobError(f"could not check out the experiment's pinned base {pin[:12]}: "
                       f"{(res.stderr or res.stdout)[-300:]}")
    s.base_sha = job.result.base_sha = pin


# ====================================================================== reading
def view(jobs: JobService, job: Job) -> dict[str, Any]:
    """The index: what was pinned, and per trial its job, manifests, verification, grade and stored files."""
    st = load(job)
    canary = (st.spec.get("grader") or {}).get("canary")
    trials = []
    for t in st.trials:
        d = t.model_dump(mode="json")
        # --- anti-forgery (#127): what flightdeck filters on, at the trial's top level
        d["trust"] = (t.grade or {}).get("trust")
        d["task_invalid"] = bool((st.task_checks.get(t.task) or {}).get("invalid"))
        base = f"/api/experiments/{job.id}/trials/{t.key}"
        q = f"?attempt={t.attempt}" if t.attempt > 1 or t.excluded else ""  # --- #166: one of a retry chain
        d["links"] = {"job": f"/api/jobs/{t.job}" if t.job else None,
                      "manifest": f"{base}/manifest{q}" if "manifest.json" in t.stored else None,
                      "trace": f"{base}/trace{q}" if "trace.json" in t.stored else None}
        trials.append(d)
    counts: dict[str, int] = {}
    for t in st.trials:
        counts[t.state] = counts.get(t.state, 0) + 1
    out = {"schema": SCHEMA, "id": job.id, "name": st.spec.get("name"), "state": job.state.value,
           "reason": job.reason, "requester": job.requester, "person": job.person, "created_at": job.created_at,
           "finished_at": st.finished_at, "spec": jobs.broker.redact(st.spec), "pins": jobs.broker.redact(st.pins),
           "counts": counts, "trials": jobs.broker.redact(trials), "notes": jobs.broker.redact(st.notes),
           "task_checks": jobs.broker.redact(st.task_checks)}
    return mask_canary(out, canary)  # --- anti-forgery (#127): the canary never leaves in an export


# ====================================================================== #135: overlays -> locks
def lock_name(experiment: str, variant: str) -> str:
    """The harness name `freeze-variants` gives a variant's lock: `<experiment>-<variant>`, as a lock name allows."""
    n = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9._-]+", "-", f"{experiment}-{variant}".lower())).strip("-._")
    return (n or "variant")[:63].rstrip("-._")


def freeze_source(index: dict[str, Any], variant: str) -> dict[str, Any] | None:
    """The trial a variant's lock is frozen from: a completed one (succeeded, counted, with a captured turn 1),
    else any finished trial with a captured turn 1; None when the variant has none yet."""
    done = [t for t in index.get("trials") or [] if t.get("variant") == variant and t.get("job")
            and t.get("state") in ("done", "grading") and not t.get("excluded") and (t.get("manifests") or [None])[0]]
    return next((t for t in done if t.get("outcome") == "succeeded"), done[0] if done else None)


def locked_spec(spec: dict[str, Any], locks: dict[str, str]) -> dict[str, Any]:
    """The experiment spec with each variant in `locks` as `{harness_lock, environment}`: what `freeze-variants`
    prints. The experiment-level `harness`/`model` defaults go once every variant is locked (a lock fixes both);
    the grader's canary goes too (a new experiment gets a new one)."""
    out = json.loads(json.dumps(spec))
    for vname, ref in locks.items():
        env = (out.get("variants", {}).get(vname) or {}).get("environment")
        out["variants"][vname] = {"harness_lock": ref, **({"environment": env} if env else {})}
    if all((v or {}).get("harness_lock") for v in out.get("variants", {}).values()):
        out.pop("harness", None)
        out.pop("model", None)
    if isinstance(out.get("grader"), dict):
        out["grader"].pop("canary", None)
    return out


def trial_file(jobs: JobService, job: Job, key: str, name: str, attempt: int | None = None) -> dict[str, Any]:
    """A trial's stored file. A retried trial (#166) has several attempts under one key: `attempt` picks one, else
    the latest (the one that counts)."""
    st = load(job)
    same = [x for x in st.trials if x.key == key and (attempt is None or x.attempt == attempt)]
    t = max(same, key=lambda x: x.attempt) if same else None
    if t is None:
        raise KeyError(f"experiment {job.id} has no trial '{key}'" + (f" attempt {attempt}" if attempt else ""))
    if name not in t.stored:
        raise KeyError(f"trial {key} has no {name} (it hasn't finished, or it couldn't be stored)")
    # --- review(#114): redacted again on the way out (a secret stored since the trial ended is masked too)
    # --- anti-forgery (#127): and the grader's canary, which a contaminated trial's trace holds
    return mask_canary(jobs.broker.redact(json.loads((_dir(jobs.settings, job.id, t.ref) / name).read_text())),
                       (st.spec.get("grader") or {}).get("canary"))


def experiment(jobs: JobService, exp_id: str, owner: str | None = None) -> Job:
    job = jobs.get(exp_id, owner=owner)
    if not is_experiment(job) or not people.visible(job.person):
        raise KeyError(f"no experiment '{exp_id}'")
    return job


# ====================================================================== REST, MCP
def mount(app: Any, h: Any, J: Any, owner_filter: Any) -> None:
    """POST /api/experiments (the spec), GET /api/experiments/{id} (the index), POST /api/experiments/{id}/cancel,
    GET /api/experiments/{id}/trials/{task}/{variant}/{n}/manifest and .../trace."""

    @app.post("/api/experiments")
    async def experiment_submit(body: ExperimentSpec, caller: Any = J):
        return view(h.jobs, submit(h.jobs, body, caller))

    @app.get("/api/experiments/{exp_id}")
    async def experiment_get(exp_id: str, caller: Any = J):
        return view(h.jobs, experiment(h.jobs, exp_id, owner_filter(caller)))

    @app.post("/api/experiments/{exp_id}/cancel")
    async def experiment_cancel(exp_id: str, caller: Any = J):
        job = experiment(h.jobs, exp_id, owner_filter(caller))
        h.jobs.cancel(job.id, caller.requester, owner=owner_filter(caller))
        return view(h.jobs, job)

    @app.get("/api/experiments/{exp_id}/trials/{key:path}/manifest")
    async def experiment_trial_manifest(exp_id: str, key: str, attempt: int | None = None, caller: Any = J):
        return trial_file(h.jobs, experiment(h.jobs, exp_id, owner_filter(caller)), key, "manifest.json", attempt)

    @app.get("/api/experiments/{exp_id}/trials/{key:path}/trace")
    async def experiment_trial_trace(exp_id: str, key: str, attempt: int | None = None, caller: Any = J):
        return trial_file(h.jobs, experiment(h.jobs, exp_id, owner_filter(caller)), key, "trace.json", attempt)


def register(mcp: Any, jobs: Any, guard: Any) -> None:
    """hangar://experiments/<id>: the index, as a resource (no new tools: the tool list is at its cap)."""
    if jobs is None:
        return
    from mcp.server.mcpserver.exceptions import ResourceError

    @mcp.resource("hangar://experiments/{job_id}", name="experiment", mime_type="application/json",
                  description="An experiment (hangar.experiment/v1): what each variant was pinned to, and per trial "
                              "its job, manifest digests, verification, grade and links to its manifest and trace.")
    async def experiment_resource(job_id: str) -> str:
        try:
            guard.check("job", job_id)
            return json.dumps(view(jobs, experiment(jobs, job_id)), indent=1, default=str)
        except (KeyError, PermissionError) as e:
            raise ResourceError(str(e).strip("'\"")) from None
