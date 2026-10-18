"""Fork a recorded run at a step (#179; docs/sessions.md#fork-at-a-step, docs/api/trials.md#forks).

`POST /api/runs/{session}.{run}/fork {at_seq, continuations: k, pin_set?, mode: "replay-prefix"}` rebuilds the state
of a recorded run at step N by re-execution (replay.py) and runs k live continuations from there. It is an experiment
job (experiments.py) whose task is the run's own prompt at its base commit and whose k trials each:

1. start a fresh sandbox at the session's base commit, with the run's harness and model (or a pin set's harness lock
   and environment), egress `none`, no credentials, fetch-only git (replay.isolate);
2. replay steps 1..N from the run's tape, re-executing every tool call and checking each step's request (and so every
   tool output) against the tape, then the request after step N;
3. go live from N: new trial ids (`<experiment>.<n>`), `source: "experiment"`, `fork_of: <run>`.

After a trial ends its working trees are checked against the run's (#165): the tree after each edit or command of the
prefix must be the one the run recorded. The fork's `divergence` is the earliest of its trials': `{seq, kind:
model|tool|network|tree}`, or null when every prefix was clean. A tainted tape (#177, `untrusted`) can be forked; its
trials are flagged `tainted: true`.

**A/A.** A trial whose prefix was clean and which ran the run's own harness (no pin set, or one with the run's
`harness_digest`) records whether it reproduced the run's outcome (`aa`): flightdeck aggregates them.

Only a session's first run can be forked: a later turn resumes a conversation a fresh sandbox doesn't have.
`continuations: 0` replays the prefix strictly and stops there (a check that the run reproduces up to N).
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hangar import replay
from hangar.models import JobBudget, JobState, Session

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job

RUN = re.compile(r"^(?P<session>ses-[0-9a-f]{8})\.(?P<run>run-[0-9a-f]{8})$")
VARIANT = "fork"
PENDING = ("pending", "replaying")


class ForkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at_seq: int = Field(ge=0, le=100_000)
    continuations: int = Field(1, ge=0, le=10)
    pin_set: str | None = Field(None, pattern=r"^pin-[0-9a-f]{16}$")
    mode: Literal["replay-prefix"] = "replay-prefix"
    on_divergence: Literal["stop", "live"] = "stop"
    budget: JobBudget = Field(default_factory=lambda: JobBudget(max_cost_usd=5.0))  # each continuation
    timeout_minutes: int | None = Field(None, ge=1, le=720)
    wait_seconds: int = Field(0, ge=0, le=600)  # wait for the prefixes before answering


def session_of(jobs: JobService, run: str, owner: str | None = None) -> tuple[Session, Any]:
    """The session and turn of `{session}.{run}`; KeyError when there is none (or it isn't the caller's)."""
    m = RUN.match(run or "")
    s = jobs.store.get(Session, m["session"]) if m else None
    if s is None or (owner is not None and s.owner != owner):
        raise KeyError(f"no run '{run}'")
    turn = next((t for t in s.turns if t.run_id == m["run"]), None)
    if turn is None:
        raise KeyError(f"no run '{run}'")
    return s, turn


def check(jobs: JobService, run: str, s: Session, turn: Any) -> None:
    """What a fork of `run` needs beyond its tape: it is the session's first run, its base commit is known, and the
    git gateway serves its repo (a replay sandbox fetches through it and holds no git credential)."""
    from hangar import posture

    first = next((t for t in s.turns if t.run_id), None)
    if first is not turn:
        raise replay.ReplayError(f"run {run} is turn {s.turns.index(turn) + 1} of its session: only a session's first "
                                 "run can be forked (a later turn resumes a conversation a fresh sandbox doesn't have)")
    if s.repo and not s.base_sha:
        raise replay.ReplayError(f"run {run}'s base commit isn't known (its session has no base_sha)")
    if not jobs.settings.gateway_url:
        raise replay.ReplayError("forks replay through the model gateway: HANGAR_GATEWAY_URL is not set "
                                 "(docs/model-gateway.md)")
    if s.repo and not posture.gateway_serves(jobs.settings, s.repo):
        raise replay.ReplayError(f"the git gateway doesn't serve {s.repo}: a replay sandbox fetches through it, "
                                 "with no git credential of its own (HANGAR_GATEWAY_UPSTREAMS)")


def start(jobs: JobService, run: str, req: ForkRequest, caller: Any, owner: str | None = None,
          purpose: str = "fork", tolerate: str | None = None) -> Job:
    """The fork's experiment job (its trials start as the experiment runner gets to them)."""
    from hangar import experiments, pinsets
    from hangar.jobs import JobError

    s, turn = session_of(jobs, run, owner)
    check(jobs, run, s, turn)
    index, entries = replay.read_tape(jobs.settings, run)
    if req.at_seq > len(entries):
        raise replay.ReplayError(f"at_seq {req.at_seq} is outside run {run}'s tape (steps 1..{len(entries)})")
    original = jobs._jobs.get(s.job_id) if s.job_id else None
    if req.pin_set:
        try:
            p = pinsets.get(jobs.fleet, req.pin_set)
        except KeyError as e:
            raise replay.ReplayError(str(e).strip("'\"")) from e
        if not p.harness_lock:
            raise replay.ReplayError(f"pin set {p.id} has no harness lock")
        env, variant = p.environment, experiments.Variant(harness_lock=p.harness_lock)
    else:
        env = s.environment
        variant = experiments.Variant(harness=turn.harness or s.harness, model=s.model or index.get("model"))
    task = experiments.Task(
        id="fork", instructions=(turn.message or "replay")[:64_000], repo=s.repo, base_branch=s.base_branch,
        base_sha=s.base_sha, verify=original.verify if original is not None else None,
        title=f"fork of {run} at step {req.at_seq}")
    fork = experiments.ForkSpec(
        run=run, at_seq=req.at_seq, mode=req.mode if req.continuations else "replay-strict",
        on_divergence=req.on_divergence, continuations=req.continuations, tainted=bool(index.get("untrusted")),
        harness_digest=index.get("harness_digest") or turn.harness_digest, pin_set=req.pin_set, purpose=purpose,
        tolerate=tolerate)
    try:
        spec = experiments.ExperimentSpec(
            name=f"{purpose} {run} @{req.at_seq}"[:100], environment=env, tasks=[task], variants={VARIANT: variant},
            trials=max(1, req.continuations), trial_job_budget=req.budget, timeout_minutes=req.timeout_minutes,
            verify_in_loop=bool(original.verify_in_loop) if original is not None else False, fork=fork)
    except ValueError as e:
        raise replay.ReplayError(str(e)) from e
    try:
        job = experiments.submit(jobs, spec, caller, source="api",
                                 untrusted_source=original.untrusted_source if original is not None else None)
    except JobError as e:
        raise replay.ReplayError(str(e)) from e
    jobs.fleet.audit(caller.requester, "run.fork", job.id, run=run, at_seq=req.at_seq,
                     continuations=req.continuations, pin_set=req.pin_set, purpose=purpose)
    return job


# ====================================================================== a trial's end
def finish_trial(jobs: JobService, job: Job, st: Any, t: Any, child: Job) -> None:
    """--- experiments._finish_trial: the trial's trees against the run's (a `tree` divergence when a prefix tree
    differs), and the A/A record for a clean prefix on the run's own harness."""
    fork = (st.spec or {}).get("fork") or {}
    if not t.replay or not fork:
        return
    try:
        p = replay.load_plan(jobs.settings, t.replay)
    except KeyError:
        return
    try:
        _trees(jobs, fork, p, child)
    except Exception as e:  # noqa: BLE001 - a tree check never fails the trial
        p.setdefault("notes", []).append(f"trees not checked: {type(e).__name__}: {str(e)[:200]}")
    if p.get("sandbox_id") is None:  # its harness never went through the gateway: nothing was replayed
        p["status"] = "unreplayed"
        p.setdefault("notes", []).append("the trial's harness has no model gateway path (claude-code and codex do)")
    elif p["status"] in PENDING:  # a strict replay that served every step is done; else it stopped short
        p["status"] = "done" if len(p.get("served") or []) >= p["at_seq"] else "stopped"
    replay.save_plan(jobs.settings, p)
    t.aa = _aa(jobs, fork, p, t, child)


def _trees(jobs: JobService, fork: dict[str, Any], p: dict[str, Any], child: Job) -> None:
    from hangar import traces

    s_orig, turn_orig = session_of(jobs, fork["run"])
    s_rep = jobs._session(child)
    run_rep = next((x.run_id for x in s_rep.turns if x.run_id), None) if s_rep is not None else None
    if run_rep is None:
        return
    index, entries = replay.read_tape(jobs.settings, fork["run"])
    if not index.get("tool_ids"):
        p["trees"] = "unverified: the tape predates tool ids"
        return
    upto = min(p.get("verified_through") or 0, p["at_seq"] + 1)
    d = p.get("divergence")
    if d is not None:
        upto = min(upto, int(d["seq"]) - 1)
    act = jobs.sessions.activity
    found = replay.tree_divergence(traces.read_events(act, s_orig.id, turn_orig.run_id, limit=20_000),
                                   traces.read_events(act, s_rep.id, run_rep, limit=20_000),
                                   index["tool_ids"], p.get("ids") or {}, entries, upto)
    p["trees"] = "diverged" if found else f"verified through step {upto}"
    if found is not None and (d is None or found["seq"] < int(d["seq"])):
        found["message"] = f"the working tree after step {found['seq'] - 1}'s tool isn't the run's"
        p["divergence"] = found


def _passed(job: Job | None) -> bool | None:
    if job is None:
        return None
    if job.verify and job.result.verify_exit_code is not None:
        return job.result.verify_exit_code == 0
    return job.state == JobState.SUCCEEDED if job.state in (JobState.SUCCEEDED, JobState.FAILED) else None


def _aa(jobs: JobService, fork: dict[str, Any], p: dict[str, Any], t: Any, child: Job) -> dict[str, Any] | None:
    if p.get("divergence") is not None or not fork.get("continuations"):
        return None
    production = not fork.get("pin_set") or (t.harness_digest is not None
                                             and t.harness_digest == fork.get("harness_digest"))
    if not production:
        return None
    s, _ = session_of(jobs, fork["run"])
    original = jobs._jobs.get(s.job_id) if s.job_id else None
    a, b = _passed(original), _passed(child)
    return {"production_harness": True, "original_job": original.id if original is not None else None,
            "original_passed": a, "fork_passed": b, "reproduced": None if a is None or b is None else a == b}


# ====================================================================== reading
def view(jobs: JobService, fork_id: str, owner: str | None = None) -> dict[str, Any]:
    from hangar import experiments

    job = experiments.experiment(jobs, fork_id, owner)
    st = experiments.load(job)
    fork = (st.spec or {}).get("fork")
    if not fork:
        raise KeyError(f"no fork '{fork_id}'")
    trials, statuses, divs = [], [], []
    for t in st.trials:
        p: dict[str, Any] = {}
        if t.replay:
            try:
                p = replay.load_plan(jobs.settings, t.replay)
            except KeyError:
                p = {}
        statuses.append(p.get("status", "pending"))
        if p.get("divergence"):
            divs.append(p["divergence"])
        trials.append({
            "id": experiments.trial_id(job, st, t), "job": t.job, "state": t.state, "outcome": t.outcome,
            "replay": t.replay, "status": p.get("status", "pending"), "divergence": p.get("divergence"),
            "served": len(p.get("served") or []), "verified_through": p.get("verified_through"),
            "live_since": p.get("live_since"), "trees": p.get("trees"), "mismatches": p.get("mismatches") or [],
            "source": "experiment", "fork_of": t.fork_of, "tainted": t.tainted, "aa": t.aa, "excluded": t.excluded})
    if not trials or any(x in PENDING for x in statuses):
        status = "replaying"
    elif "unreplayed" in statuses:
        status = "unreplayed"
    elif divs:
        status = "diverged"
    else:
        status = "forked" if fork.get("continuations") else "verified"
    first = min(divs, key=lambda d: int(d.get("seq") or 0)) if divs else None
    ids = [x["id"] for x in trials] or [f"{job.id}.{n}" for n in range(1, max(1, fork.get("continuations") or 0) + 1)]
    return {"fork": job.id, "run": fork["run"], "at_seq": fork["at_seq"], "mode": fork["mode"],
            "continuations": fork.get("continuations"), "pin_set": fork.get("pin_set"), "status": status,
            "divergence": first, "tainted": bool(fork.get("tainted")), "state": job.state.value,
            "trial_ids": ids, "trials": trials}


async def wait(jobs: JobService, fork_id: str, seconds: int, owner: str | None = None) -> dict[str, Any]:
    """The fork's view once its prefixes have settled (every trial live, diverged or stopped), or after `seconds`."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while True:
        v = view(jobs, fork_id, owner)
        if v["status"] != "replaying" or loop.time() >= end:
            return v
        await asyncio.sleep(1.0)


# ====================================================================== REST
def mount(app: Any, h: Any, J: Any, owner_filter: Any) -> None:
    """POST /api/runs/{session}.{run}/fork and GET /api/forks/{id} (jobs tokens: flightdeck talks REST)."""
    from fastapi import HTTPException

    @app.post("/api/runs/{run}/fork")
    async def run_fork(run: str, body: ForkRequest, caller: Any = J):
        owner = owner_filter(caller)
        try:
            job = start(h.jobs, run, body, caller, owner)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\"")) from e
        except replay.ReplayError as e:
            raise HTTPException(422, str(e)) from e
        return await wait(h.jobs, job.id, body.wait_seconds, owner)

    @app.get("/api/forks/{fork_id}")
    async def fork_get(fork_id: str, caller: Any = J):
        try:
            return view(h.jobs, fork_id, owner_filter(caller))
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\"")) from e
