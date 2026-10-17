"""Shadow runs (#183; docs/repo-policy.md#shadows): a candidate harness on a sampled real ticket, delivering nothing.

A repo's policy `shadow: {pin_set | harness: name@sha256:..., p, sources}` (default: none) samples its real jobs from
those intake sources (Jira, GitHub issues and Projects, Slack) with probability `p`. When a sampled job has started
(its sandbox is up and its base commit known), hangar starts one shadow trial through the trials API (#178):
`source: shadow`, `shadow_of: <job id>`, the job's task text and untrusted context at the job's own base commit, in
the eval queue class (#181), counted against the monthly eval budget (#182) under the harness owner (`owner`).

**No trace on real work.** A shadow trial is a job with `source: shadow` and `deliver: none`: it carries no
`intake.tracker` label (no tracker write-back), no `reply_to` and no `source_ref` (nothing posted to Slack, GitHub or
Jira), no callback and no PR. It runs with egress `none` (the posture floor label) and the team's credentials only (no
person, so never someone's login), and its untrusted text stays fenced (the real job's `untrusted_source` carries over).
It is in nobody's inbox, and job listings leave shadows out unless asked for (`?source=shadow`, admins).

**Gate.** Shadows double the cost of a sampled job and their grading waits on the real PR's review. So `p > 0` is
refused while the last 7 days' review-wait p50 (#168) isn't under 4 hours, and shadows pause by themselves (an audit
entry says so, once) when it rises above, and resume when it falls back.

The run capture (`run.json`, #163) says `source: shadow` and `shadow_of`; the real job's merge and close events let
flightdeck grade the shadow by what was merged.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Any

from hangar.models import now

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job
    from hangar.repopolicy import RepoPolicy

log = logging.getLogger("hangar.shadows")

PICK_LABEL = "hangar.shadow.pick"  # on a sampled real job: "1" until its shadow starts, then the shadow's experiment
SHADOW_OF = "hangar.shadow_of"  # on a shadow's experiment and trial jobs: the real job
REVIEW_WAIT_MAX_HOURS = 4.0
WINDOW_DAYS = 7
STATE_KEY = "shadows:paused"  # the last gate state, so a pause or resume is audited once
SOURCES = {"jira": "jira", "github": "github", "slack": "slack", "github-projects": "github-projects"}
rand = random.random  # tests replace it


def review_wait_p50(svc: JobService) -> float | None:
    """The last 7 days' review-wait p50 in hours (#168's weekly metrics), None without data."""
    from hangar import weekly

    hi = now()
    try:
        return weekly.compute(svc, hi - WINDOW_DAYS * 86400, hi)["review_wait"]["p50_hours"]
    except Exception:  # noqa: BLE001 - no metrics: treated as not known
        log.exception("could not read the review-wait p50")
        return None


def paused(svc: JobService) -> str | None:
    """Why shadows can't run now (the review-wait gate), or None."""
    p50 = review_wait_p50(svc)
    if p50 is None:
        return (f"the last {WINDOW_DAYS} days have no agent PR review to measure the wait by; shadows need a "
                f"review-wait p50 under {REVIEW_WAIT_MAX_HOURS:g} h")
    if p50 >= REVIEW_WAIT_MAX_HOURS:
        return (f"the last {WINDOW_DAYS} days' review-wait p50 is {p50:.1f} h, and shadows need it under "
                f"{REVIEW_WAIT_MAX_HOURS:g} h (each shadow's grade waits on a person's review)")
    return None


def _note_gate(svc: JobService, why: str | None) -> None:
    """Audit a pause or a resume once, when the gate's state changes."""
    was = svc.store.kv_get_json(STATE_KEY, None)
    now_paused = why is not None
    if was is None or bool(was.get("paused")) != now_paused:
        svc.store.kv_set_json(STATE_KEY, {"paused": now_paused, "why": why, "at": now()})
        if was is not None or now_paused:
            svc.fleet.audit("hangar", "shadow.paused" if now_paused else "shadow.resumed", None, why=why)


def policy_of(svc: JobService, job: Job) -> Any:
    p = svc.policies.get(job.repo) if job.repo else None
    return p.shadow if p is not None else None


def at_submit(svc: JobService, job: Job, policy: RepoPolicy | None, source: str) -> None:
    """A real job from an intake source of a repo with shadows on: sample it (probability `p`)."""
    sp = policy.shadow if policy is not None else None
    if sp is None or sp.p <= 0 or source not in {SOURCES.get(s) for s in sp.sources}:
        return
    if job.source in ("experiment", "review", "shadow", "env-setup") or experiments_label(job):
        return
    why = paused(svc)
    _note_gate(svc, why)
    if why is not None:
        return
    if rand() < sp.p:
        job.labels[PICK_LABEL] = "1"
        svc._save(job)


def experiments_label(job: Job) -> bool:
    from hangar.experiments import EXP_LABEL, KIND_LABEL

    return EXP_LABEL in job.labels or KIND_LABEL in job.labels


def after_provision(svc: JobService, job: Job) -> None:
    """A sampled real job has its sandbox and base commit: start its shadow (once). Never fails the real job."""
    if job.labels.get(PICK_LABEL) != "1":
        return
    try:
        out = start(svc, job)
    except Exception as e:  # noqa: BLE001 - a shadow is never the real job's problem
        log.warning("job %s: its shadow didn't start: %s", job.id, e)
        svc.fleet.audit("hangar", "shadow.failed", job.id, error=str(e)[:300])
        job.labels[PICK_LABEL] = "failed"
        svc._save(job)
        return
    job.labels[PICK_LABEL] = out["experiment"] or "cached"
    svc._save(job)
    svc.fleet.audit("hangar", "shadow.start", job.id, experiment=out["experiment"], trials=out["trial_ids"])


def start(svc: JobService, job: Job) -> dict[str, Any]:
    """The shadow trial of `job`: its pin set (the policy's harness at the job's base commit), then the trial."""
    from hangar import experiments, pinsets
    from hangar.jobs import Caller
    from hangar.models import JobBudget

    sp = policy_of(svc, job)
    if sp is None:
        raise ValueError("the repo's policy has no shadow any more")
    if not job.result.base_sha:
        raise ValueError("the job has no base commit (no repo checkout)")
    tid = "shadow"
    task = pinsets.TaskPin(repo=job.repo, base_branch=job.base_branch, base_sha=job.result.base_sha)
    if sp.pin_set:
        base = pinsets.get(svc.fleet, sp.pin_set)
        body = pinsets.PinSetBody.model_validate({**base.body(), "tasks": {tid: task.model_dump()}})
    else:
        body = _resolve_now(svc, sp.harness, job.environment, {tid: task})
    p = pinsets.put(svc.fleet, body, "hangar:shadow")
    context = [c.model_copy() for c in job.context if c.kind not in ("patch", "bundle")]
    from hangar import canaries

    plan = canaries.choose(sp, context)  # --- #184: an injection canary in the shadow's own copy, maybe
    req = pinsets.TrialsRequest(
        pin_set=p.id, n=1, source="shadow", shadow_of=job.id, author=sp.owner or "shadow",
        task=experiments.Task(id=tid, instructions=job.instructions, repo=job.repo, base_branch=job.base_branch,
                              base_sha=job.result.base_sha, verify=job.verify, context=context or None,
                              title=job.title[:200]),
        budget=JobBudget(max_cost_usd=sp.budget_usd), name=f"shadow of {job.id}")
    if plan is not None:
        req.reuse = None  # a canary shadow is never served from the trial cache
    # the team's credentials only: a shadow runs for no person, so never with someone's own login
    out = pinsets.start_trials(svc, req, Caller("hangar:shadow", "shadow"), untrusted_source=job.untrusted_source)
    if plan is not None and out.get("experiment"):
        canaries.remember(svc, out["experiment"], plan, job.id, p.body().get("harness_digest"))
    return out


def _resolve_now(svc: JobService, lock: str, environment: str, tasks: dict[str, Any]) -> Any:
    """pinsets.resolve without awaiting: every task has its base commit, so nothing asks GitHub."""
    from hangar import harness_thaw, pinsets

    lk = harness_thaw.pin(svc.fleet, lock, environment)
    return pinsets.PinSetBody(harness=lk["harness"], harness_version=lk["version"], harness_lock=lock,
                              harness_digest=lk["digest"], environment=environment,
                              environment_spec_digest=pinsets.environment_spec_digest(svc.fleet, environment),
                              model_id=lk["model"], approvals=lk["approvals"], tasks=tasks,
                              egress_digest=pinsets.egress_digest(svc.fleet, environment))
