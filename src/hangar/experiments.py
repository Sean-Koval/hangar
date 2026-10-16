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
hook a test run are listed in the grade (`tamper_signals`). The code still runs beside the grader, as the sandbox's
user: it could write $HANGAR_GRADE_OUT too (#127 takes that file from it). Its exit code, the end of its output,
the hidden tests' junit counts and `$HANGAR_GRADE_OUT` (JSON) are stored with the trial.
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


class Variant(BaseModel):
    """An environment plus an overlay. Unset fields are the experiment's (then the environment's).

    The only overlay on an environment (spec 0014 §4). `skills` has the semantics an environment's `skills` has under
    `extends` (#132): the whole set, `[]` for none; `skills_remove` drops named ones. It gains no new fields: once
    harness locks exist (#133) a variant shrinks to `{harness: name@sha256, environment}` (#135)."""

    model_config = ConfigDict(extra="forbid")
    environment: str | None = None
    harness: str | None = Field(None, max_length=80)  # "codex" or "codex@0.162.1" (must be the locked version)
    model: str | None = Field(None, max_length=200)  # an id or a policy (frontier, balanced)
    approvals: Literal["auto", "deny-risky"] | None = None
    skills: list[SkillRef] | None = Field(None, max_length=50)  # the whole set; None: the environment's
    # drop these by name from the set above (else from the environment's), as an environment's own skills_remove
    skills_remove: list[str] | None = Field(None, max_length=50)
    instructions: str | None = Field(None, max_length=64_000)  # replaces the environment's global instructions
    instructions_append: str | None = Field(None, max_length=64_000)


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


class State(BaseModel):
    schema_: str = Field(SCHEMA, alias="schema")
    stage: Literal["start", "running", "done"] = "start"
    spec: dict[str, Any] = Field(default_factory=dict)
    pins: dict[str, Any] = Field(default_factory=dict)  # variants: {...}, tasks: {...}
    trials: list[Trial] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    finished_at: float | None = None

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
        _variant_harness(jobs.fleet, spec, vname, v)  # an unknown or unlocked harness version fails now
    req = SubmitJob(instructions=f"Experiment {spec.name}: {len(spec.tasks)} task(s) x {len(spec.variants)} "
                                 f"variant(s) x {spec.trials} trial(s).", title=f"experiment: {spec.name}"[:200],
                    environment=spec.environment, deliver="none", labels={KIND_LABEL: KIND},
                    job_budget=spec.budget, verify="", autofix="off")
    job = jobs.submit(req, caller, source=source)
    if job.result.data is None:  # not an idempotent repeat; nothing awaits between submit and here
        st = State(spec=spec.model_dump(mode="json", exclude_none=True))
        _save(jobs, job, st)
    return job


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
    from hangar import modelpolicy, pins

    fleet = jobs.fleet
    variants: dict[str, Any] = {}
    for vname, v in spec.variants.items():
        harness, locked = _variant_harness(fleet, spec, vname, v)
        env = fleet.catalog.get(v.environment or spec.environment)
        value = v.model or spec.model or env.model_for(harness) or pins.JOB_DEFAULT
        r = modelpolicy.of(fleet).resolve(harness, value)
        overlay = _overlay(env, v)
        fleet.store.kv_set(f"{KV_OVERLAY}{job.id}:{vname}", json.dumps(overlay))
        variants[vname] = {"environment": env.name, "harness": harness, "harness_version": locked,
                           "model": r.model, "model_policy": r.policy, "model_source": r.source,
                           "approvals": v.approvals or env.approvals or jobs.settings.approvals,
                           "skills": [{"name": s["name"], "digest": s.get("digest"), "source": s["source"]}
                                      for s in overlay["skills"]] if overlay["skills"] is not None else None,
                           "instructions": overlay.get("instructions_digest")}
    tasks: dict[str, Any] = {}
    for t in spec.tasks:
        env = fleet.catalog.get(spec.environment)
        repo = t.repo or (f"https://github.com/{t.issue.split('#')[0]}.git" if t.issue else None) or (
            env.repo.url if env.repo else None)
        pinned = t.base_sha or await _github_sha(jobs, repo, t.base_branch)
        if pinned:
            fleet.store.kv_set(f"{KV_BASE}{job.id}:{t.id}", pinned)
        issue_text = await _issue_text(jobs, t.issue) if t.issue else None
        tasks[t.id] = {"repo": repo, "base_branch": t.base_branch, "base_sha": pinned,
                       "issue": t.issue, "issue_text": issue_text}
    # --- review(#114): egress too: an approved egress request widens an environment mid-experiment
    st.pins = {"variants": variants, "tasks": tasks, "egress": _egress(fleet, spec), "pinned_at": now()}
    st.trials = [Trial(key=f"{t.id}/{vname}/{i}", task=t.id, variant=vname, trial=i)
                 for i in range(1, spec.trials + 1) for t in spec.tasks for vname in spec.variants]
    st.stage = "running"
    _save(jobs, job, st)
    jobs._event(job, f"experiment {spec.name}: {len(st.trials)} trial(s), at most "
                     f"{spec.parallel or jobs.settings.experiment_parallel} at once; pinned "
                     + "; ".join(f"{k}: {v['harness']}@{v['harness_version'] or '?'} on {v['model'] or 'its default'}"
                                 for k, v in variants.items()), JobState.RUNNING, reason=None)


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
            graders[t.key] = asyncio.create_task(_grade(jobs, job, st, spec, t))
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
            if child.state in JOB_TERMINAL and child.id not in jobs._active:
                await _finish_trial(jobs, job, st, spec, t, child)
                if spec.grader and t.state == "grading":
                    graders[t.key] = asyncio.create_task(_grade(jobs, job, st, spec, t))
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
        counts[t.outcome or t.state] = counts.get(t.outcome or t.state, 0) + 1
    job.result.summary = (f"Experiment {spec.name}: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
                          + ". The trials' manifests, traces and results are in the index (GET "
                          f"/api/experiments/{job.id}).")
    _save(jobs, job, st)
    jobs._event(job, job.result.summary, JobState.SUCCEEDED, reason=None)


def _stop_starting(jobs: JobService, job: Job, st: State, spec: ExperimentSpec) -> str | None:
    """Why no further trial may start: the experiment's budget is spent, or the harness lock changed under it."""
    from hangar.harnesses import HARNESSES

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
        instructions=instructions, title=f"{spec.name}: {t.key}"[:200], repo=tp.get("repo"),
        base_branch=task.base_branch, environment=v.environment or spec.environment, harness=pin["harness"],
        model=pin["model"], provider=spec.provider, verify=task.verify, deliver="none", context=context,
        keep_minutes=spec.keep_minutes, timeout_minutes=spec.timeout_minutes, budget=spec.trial_budget,
        job_budget=spec.trial_job_budget, idempotency_key=f"experiment:{job.id}:{t.key}",
        verify_in_loop=spec.verify_in_loop, autofix="off",
        labels={EXP_LABEL: job.id, TRIAL_LABEL: t.key, VARIANT_LABEL: t.variant, PACKS_LABEL: "selected"})
    try:
        child = jobs._submit(req, Caller(job.requester, job.owner, person=job.person), source="experiment",
                             untrusted_source=job.untrusted_source)
    except JobError as e:
        t.state, t.outcome, t.reason, t.finished_at = "done", "failed", f"could not start: {e}", now()
        return
    jobs.fleet.store.kv_set(KV_TRIAL + child.id, job.id)  # before it can start: nothing awaits in between
    t.job, t.state, t.started_at = child.id, "running", now()


async def _finish_trial(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, t: Trial, child: Job) -> None:
    """Record what the trial ran with and what came of it, and keep its manifest and trace export."""
    from hangar import manifest, traces

    t.outcome, t.reason, t.finished_at = child.state.value, child.reason, now()
    r = child.result
    t.verify = {"command": child.verify, "exit_code": r.verify_exit_code, "passed": (
        r.verify_exit_code == 0 if r.verify_exit_code is not None else None), "new_failures": r.new_failures,
        "baseline_red": r.baseline.red if r.baseline else None, "tail": (r.verify_tail or "")[-TAIL:] or None}
    tests = getattr(r, "tests", None)  # the testresults work's TestSummary, when hangar parsed the check's reports
    t.tests = tests.model_dump(mode="json") if isinstance(tests, BaseModel) else tests
    if (u := jobs.usage(child)) is not None:
        t.usage = {"cost_usd": u.cost_usd, "tokens": u.tokens, "input_tokens": u.input_tokens,
                   "output_tokens": u.output_tokens, "cache_read_tokens": u.cache_read_tokens, "model": u.model}
    s = jobs._session(child)
    d = _dir(jobs.settings, job.id, t.key)
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
    t.state = "grading" if spec.grader and child.state != JobState.CANCELLED and s is not None else "done"
    jobs._event(job, f"trial {t.key}: {t.outcome}" + (f" ({t.reason})" if t.reason else "")
                + (", grading" if t.state == "grading" else ""))


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
mkdir -p "$(dirname "$junit")" && rm -f "$junit"
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


def grader_root(prov: Any) -> str:
    """Where a grader's hidden tests and `hangar-grade` go. On Docker hangar writes them as root (its exec and
    archive uploads pick the uid), so the sandbox's user reads them and can't change them; elsewhere the sandbox runs
    as one user, and they go to /tmp."""
    return GRADER_ROOT if getattr(prov, "name", "") == "docker" else GRADER_ROOT_SHARED


async def _grade(jobs: JobService, job: Job, st: State, spec: ExperimentSpec, t: Trial) -> None:
    """The experiment's grader on this trial's work, in a clean, sealed sandbox: the pinned base, the trial's patch,
    the grader's own files (outside the repo unless `files_mode: in_repo`). It runs agent-written code beside the
    hidden tests, so it holds nothing and reaches nothing (GRADER_LABEL: delivery.builds_repo_code, delivery.sealed):
    the environment's git credentials only tell the gateway which one to fetch with."""
    from hangar import delivery, exchange
    from hangar.autoinstall import validation_credentials

    fleet = jobs.fleet
    g = spec.grader
    assert g is not None
    t0 = now()
    grade: dict[str, Any] = {"command": g.command, "at": t0}
    sb = None
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
        grade["patch_bytes"] = len(text.encode())
        tp = st.pins["tasks"][t.task]
        env_name = spec.variants[t.variant].environment or spec.environment
        env = fleet.catalog.get(env_name)
        labels = {GRADER_LABEL: job.id, TRIAL_LABEL: t.key}
        if g.network == "mirror":
            labels[delivery.NETWORK_LABEL] = "mirror"
        sb = await fleet.create(env_name, repo=tp.get("repo"), branch=next(
            (x.base_branch for x in spec.tasks if x.id == t.task), None),
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
            elif signals := await _tamper_signals(prov, sb, repo, text):  # --- sealed (#126)
                grade["tamper_signals"] = signals
        # --- sealed (#126): the base and the patch are in; from here the trial's code runs: no host at all (not even
        # the setup's git host), and the sandbox's git grant (its one way to the gateway's upstream) goes
        sb = delivery.lock_sealed(fleet, sb)
        # the hidden tests outside the repo, beside `hangar-grade`; or in the repo (in_repo)
        root = f"{grader_root(prov)}/{re.sub(r'[^A-Za-z0-9._-]', '_', t.key.replace('/', '__'))}"
        grade["files_mode"] = g.files_mode
        if g.files_mode == "in_repo":
            hidden = repo or fleet.settings.workspace_dir
        else:
            hidden = f"{root}/tests"
            await prov.write_file(sb, f"{root}/bin/hangar-grade", GRADE_HELPER.encode(), mode=0o755)
            if not g.files:  # the directory exists even without files: a command can count on it
                await prov.write_file(sb, f"{hidden}/.hangar-hidden-tests", b"", mode=0o644)
        for rel, content in g.files.items():
            await prov.write_file(sb, f"{hidden}/{rel}", content.encode(), mode=0o644)
        junit = f"{work}/hidden-junit.xml"
        env_vars = {"HANGAR_GRADE_OUT": GRADE_OUT, "HANGAR_TRIAL": t.key, "HANGAR_TASK": t.task,
                    "HANGAR_VARIANT": t.variant, "HANGAR_EXPERIMENT": job.id, "HANGAR_HIDDEN_TESTS": hidden,
                    "HANGAR_REPO": repo or fleet.settings.workspace_dir, "HANGAR_GRADE_JUNIT": junit}
        path = f"PATH={q(root + '/bin')}:\"$PATH\" " if g.files_mode == "pristine" else ""
        cmd = path + " ".join(f"{k}={q(v)}" for k, v in env_vars.items()) + \
            f" timeout {g.timeout_seconds} bash -c " + q(g.command)
        # how far the grade can be trusted (#127 adds `isolated`): the hidden tests in the repo, where the patch can
        # change them, are `low`; anything else is `best_effort` until the grade's output is hangar's own
        grade["trust"] = "low" if g.files_mode == "in_repo" else "best_effort"
        grade["runner"] = "hangar-grade" if g.files_mode == "pristine" and "hangar-grade" in g.command else "command"
        res = await fleet.exec(sb.id, cmd, cwd=repo, timeout=g.timeout_seconds + 30, actor=f"experiment:{job.id}")
        grade["exit_code"] = res.exit_code
        grade["tail"] = jobs.broker.redact(((res.stdout or "") + (res.stderr or ""))[-TAIL:])
        if (hidden_tests := await _junit_counts(prov, sb, junit)) is not None:
            grade["hidden_tests"] = hidden_tests
        if res.exit_code == 124 and grade["network"] == "none":
            grade.setdefault("notes", []).append("the grader timed out with no network: if it fetches a dependency, "
                                                 "that isn't in the image (grader.network: mirror, or a repo build)")
        raw = await prov.read_file(sb, GRADE_OUT, limit=64_000)
        if raw:
            try:
                grade["data"] = jobs.broker.redact(json.loads(raw[:64_000]))
            except ValueError:
                grade["data_error"] = "$HANGAR_GRADE_OUT isn't JSON (or is over 64 KB)"
    except asyncio.CancelledError:
        grade["error"] = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001 - the trial keeps everything else
        grade["error"] = jobs.broker.redact(str(e))[:1000]
    finally:
        grade["seconds"] = round(now() - t0, 1)
        t.grade, t.state = grade, "done"
        if sb is not None:
            with contextlib.suppress(Exception):
                await fleet.destroy(sb.id, actor=f"experiment:{job.id}")
        with contextlib.suppress(Exception):
            _save(jobs, job, st)


async def _junit_counts(prov: Any, sb: Any, path: str) -> dict[str, Any] | None:
    """The hidden tests' results from the junit `hangar-grade` wrote: counts per outcome."""
    from hangar import testreports

    raw = await prov.read_file(sb, path, limit=8 * 1024 * 1024)
    if not raw:
        return None
    r = testreports.parse_junit(raw[:8 * 1024 * 1024], "hidden-junit.xml")
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
    the instructions and its `hangar-environment` skill (supervisor.tell_agent), alike for every variant on the
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
    trials = []
    for t in st.trials:
        d = t.model_dump(mode="json")
        base = f"/api/experiments/{job.id}/trials/{t.key}"
        d["links"] = {"job": f"/api/jobs/{t.job}" if t.job else None,
                      "manifest": f"{base}/manifest" if "manifest.json" in t.stored else None,
                      "trace": f"{base}/trace" if "trace.json" in t.stored else None}
        trials.append(d)
    counts: dict[str, int] = {}
    for t in st.trials:
        counts[t.state] = counts.get(t.state, 0) + 1
    return {"schema": SCHEMA, "id": job.id, "name": st.spec.get("name"), "state": job.state.value,
            "reason": job.reason, "requester": job.requester, "person": job.person, "created_at": job.created_at,
            "finished_at": st.finished_at, "spec": jobs.broker.redact(st.spec), "pins": jobs.broker.redact(st.pins),
            "counts": counts, "trials": jobs.broker.redact(trials), "notes": jobs.broker.redact(st.notes)}


def trial_file(jobs: JobService, job: Job, key: str, name: str) -> dict[str, Any]:
    st = load(job)
    t = next((x for x in st.trials if x.key == key), None)
    if t is None:
        raise KeyError(f"experiment {job.id} has no trial '{key}'")
    if name not in t.stored:
        raise KeyError(f"trial {key} has no {name} (it hasn't finished, or it couldn't be stored)")
    # --- review(#114): redacted again on the way out (a secret stored since the trial ended is masked too)
    return jobs.broker.redact(json.loads((_dir(jobs.settings, job.id, key) / name).read_text()))


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
    async def experiment_trial_manifest(exp_id: str, key: str, caller: Any = J):
        return trial_file(h.jobs, experiment(h.jobs, exp_id, owner_filter(caller)), key, "manifest.json")

    @app.get("/api/experiments/{exp_id}/trials/{key:path}/trace")
    async def experiment_trial_trace(exp_id: str, key: str, caller: Any = J):
        return trial_file(h.jobs, experiment(h.jobs, exp_id, owner_filter(caller)), key, "trace.json")


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
