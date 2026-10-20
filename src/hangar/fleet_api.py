"""`GET /api/fleet`: the Fleet page's one read (KAN-140, the Decision deck; docs/console.md#fleet).

The console used to fetch sandboxes, jobs and sessions and join them in the browser (`buildUnits` in fleet.js).
The join now happens here, once per read, from what is already in memory: live sandboxes from the store's state
index, resident jobs and sessions, each session's `LiveState` (livestate.py: what the agent is doing right now, its
stage, files and commits), the pending approvals, and the caller's inbox. One unit per sandbox, with the ids the
console had (sandbox, job, session), so links and actions keep working.

Visibility is `/api/jobs`'s: an admin sees everything, a member their own work (people.visible). Text that came from
an agent or a person passes through the broker's redaction as every view does. `?owner=<person>` keeps one person's
units (`none`: those nobody owns); `?include_stopped=true` adds settled sandboxes.

Per unit, beyond the join: `now` (the humanised latest event of the live turn), `last_activity_at`, `stage` (plan,
edit, test or pr; only with a real signal), `stage_pct` (only when the agent reported progress itself:
`hangar-agent progress --pct`), `stage_estimate` (elapsed in the stage against a typical time, clearly a guess:
the UI styles it differently), `stages[]` with started and ended times, `files_changed`, `commits`, `usage`,
`tokens_1h` (12 five-minute buckets of new tokens, oldest first; kept in memory, so empty after a restart), the
status role, label and reason, and the inbox items that wait on this unit (`inbox`).
"""

from __future__ import annotations

import re
import statistics
import time
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from hangar import identity, inbox, people, steer
from hangar.events import total_usage
from hangar.livestate import STAGES, TOKEN_BUCKETS, LiveState
from hangar.models import JobState, Person, SandboxState, SessionState, now
from hangar.runtime import FastJSON

if TYPE_CHECKING:
    from hangar.models import Job, Sandbox, Session

# Attention first everywhere: what needs a person, then what failed, then what policy stopped, then the quiet states.
ATTENTION = ("approval", "failed", "denied", "running", "queued", "off")
_RANK = {r: i for i, r in enumerate(ATTENTION)}
SB_ROLE = {"pending": "queued", "starting": "queued", "running": "running", "paused": "off", "stopping": "off",
           "stopped": "off", "error": "failed"}
SB_WORD = {"pending": "Starting", "starting": "Starting", "running": "Running", "paused": "Paused",
           "stopping": "Stopping", "stopped": "Spun down", "error": "Failed"}
LIVE_STATES = {SandboxState.RUNNING, SandboxState.PAUSED, SandboxState.STARTING, SandboxState.PENDING}
_PUSH = re.compile(r"\bgit\s+push\b|\bgh\s+pr\b")
TASK_MAX = 300
# A stage's share of a turn, for the estimate's fallback when this control plane has no stage samples yet
STAGE_SHARE = {"plan": 0.10, "edit": 0.50, "test": 0.25, "pr": 0.15}
DEFAULT_TURN_SECONDS = 1500.0
TYPICAL_TTL = 300.0  # how long a (environment, harness)'s typical turn time from the store is kept
RECENT_JOBS = 50


def _word(s: str) -> str:
    return (s or "").replace("_", " ").capitalize()


def _latest(records: list[Any], key: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for r in records:
        k = getattr(r, key, None)
        if k and (k not in out or r.created_at > out[k].created_at):
            out[k] = r
    return out


# ====================================================================== stage
def stage_of(job: Job | None, ses: Session | None, live: LiveState | None, held: str | None,
             busy: bool) -> str | None:
    """The stage of the work: the job's phase when hangar drives it (verifying = test, delivering = PR), else what
    the live turn's events show (livestate.stage_signal). No signal, no stage: a queued or idle unit has none."""
    if job is not None:
        if job.state == JobState.VERIFYING:
            return "test"
        if job.state in (JobState.DELIVERING, JobState.SUCCEEDED):
            return "pr"
        if job.state in (JobState.RUNNING, JobState.NEEDS_INPUT):
            if live is not None and live.stage:
                return live.stage
            return "pr" if held and _PUSH.search(held) else None
        return None
    if ses is not None and (ses.state == SessionState.RUNNING or busy):
        return live.stage if live is not None else None
    return None


def _job_stages(job: Job) -> list[dict[str, Any]]:
    """The phases hangar itself drove, from the job's timeline: verifying (test) and delivering (pr)."""
    out: list[dict[str, Any]] = []
    for ev in job.events:
        st = {"verifying": "test", "delivering": "pr"}.get(ev.state)
        if st is None:
            continue
        if out and out[-1]["stage"] == st:
            continue
        if out:
            out[-1]["ended_at"] = ev.ts
        out.append({"stage": st, "started_at": ev.ts, "ended_at": None})
    if out and job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        out[-1]["ended_at"] = out[-1]["ended_at"] or job.updated_at
    return out


class Typical:
    """A typical turn's length per (environment, harness), from recent finished jobs in the store, cached."""

    def __init__(self, jobs: Any) -> None:
        self.jobs = jobs
        self._cache: dict[tuple[str, str], tuple[float, float | None, int]] = {}

    def turn_seconds(self, env: str, harness: str) -> tuple[float | None, int]:
        key = (env, harness)
        hit = self._cache.get(key)
        t = time.monotonic()
        if hit is not None and t - hit[0] < TYPICAL_TTL:
            return hit[1], hit[2]
        secs: list[float] = []
        try:
            recent = self.jobs.where(state=JobState.SUCCEEDED, environment=env)
        except Exception:  # noqa: BLE001 - the estimate is a nicety; never a failure of the fleet read
            recent = []
        recent = sorted((j for j in recent if not harness or (j.harness or "") == harness),
                        key=lambda j: j.updated_at, reverse=True)[:RECENT_JOBS]
        for j in recent:
            d = sum((tn.finished_at or 0) - (tn.started_at or 0) for tn in j.turns if tn.started_at and tn.finished_at)
            if d > 0:
                secs.append(d)
        med = statistics.median(secs) if secs else None
        self._cache[key] = (t, med, len(secs))
        return med, len(secs)


def stage_estimate(live: LiveState | None, stage: str | None, env: str, harness: str, stats: Any,
                   typical: Typical, at: float) -> dict[str, Any] | None:
    """How far along the stage probably is, from time alone: elapsed in it against the median of the same stage in
    this control plane's recent turns (livestate.StageStats), else a share of the median turn of recent finished
    jobs for the environment and harness, else a default. Always marked with its `basis`: it is a guess, which the
    UI shows as one; `stage_pct` is the agent's own word."""
    if live is None or stage is None or live.stage != stage:
        return None
    elapsed = live.stage_elapsed(at)
    if elapsed is None:
        return None
    typ = stats.typical((env, harness), stage)
    if typ is not None:
        secs, n, basis = typ[0], typ[1], f"median of {typ[1]} recent {stage} stages"
    else:
        med, n = typical.turn_seconds(env, harness)
        if med is not None:
            secs, basis = med * STAGE_SHARE[stage], f"share of the median turn of {n} recent jobs"
        else:
            secs, basis = DEFAULT_TURN_SECONDS * STAGE_SHARE[stage], "default"
    pct = min(99, int(100 * elapsed / secs)) if secs > 0 else 0
    return {"pct": pct, "elapsed_s": int(elapsed), "typical_s": int(secs), "basis": basis, "samples": n}


# ====================================================================== the join
class FleetRead:
    """One read of the fleet for one caller. Built per request; everything it touches is in memory already except
    the sandbox list (the store's state index) and the people it names (by key)."""

    def __init__(self, h: Any) -> None:
        self.h = h
        self.fleet, self.sessions, self.jobs = h.fleet, h.sessions, h.jobs
        self.red = h.fleet.broker.redact
        self.typical = Typical(h.jobs)

    def units(self, p: Any, include_stopped: bool = False, owner: str | None = None) -> dict[str, Any]:
        at = now()
        sbs: list[Sandbox] = self.fleet.list(include_stopped)
        jobs = [j for j in self.jobs._jobs.values() if j.source != "shadow"]  # --- #183: never on the fleet
        sess = list(self.sessions._sessions.values())
        job_by_id = {j.id: j for j in jobs}
        ses_by_id = {s.id: s for s in sess}
        job_by_sb, ses_by_sb = _latest(jobs, "sandbox_id"), _latest(sess, "sandbox_id")
        items = inbox.items(self.jobs, p)
        by_job: dict[str, list[dict[str, Any]]] = {}
        by_ses: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            if it.get("job"):
                by_job.setdefault(it["job"], []).append(it)
            if it.get("session"):
                by_ses.setdefault(it["session"], []).append(it)
        live = self.sessions.activity.live
        units: list[dict[str, Any]] = []
        names: dict[str, dict[str, Any]] = {}
        for sb in sbs:
            made = sb.created_by or ""
            ses = ses_by_sb.get(sb.id) or ses_by_id.get(made.removeprefix("session:"))
            job = job_by_sb.get(sb.id) or job_by_id.get(made.removeprefix("job:")) or \
                (job_by_id.get(ses.job_id) if ses is not None and ses.job_id else None)
            if job is not None and ses is None and job.session_id:
                ses = ses_by_id.get(job.session_id)
            person = job.person if job is not None else ses.person if ses is not None else sb.person
            if not people.visible(person):
                continue
            u = self._unit(sb, job, ses, live.get(ses.id) if ses is not None else None, live.stats,
                           (by_job.get(job.id, []) if job is not None else []) + (by_ses.get(ses.name, [])
                                                                                  if ses is not None else []), at)
            if owner is not None and (u["owner"] or "none") != owner:
                continue
            if u["owner"] and u["owner"] not in names:
                names[u["owner"]] = self._person(u["owner"])
            units.append(u)
        units.sort(key=lambda u: (_RANK[u["role"]], u["created_at"], u["id"]))
        counts: dict[str, int] = {r: 0 for r in ATTENTION}
        for u in units:
            counts[u["role"]] += 1
        owners: dict[str, int] = {}
        for u in units:
            owners[u["owner"] or "none"] = owners.get(u["owner"] or "none", 0) + 1
        return {"at": at, "person": p.person if p is not None else None, "units": units, "counts": counts,
                "owners": [{"id": k, "name": names.get(k, {}).get("name") or (k if k != "none" else None),
                            "count": n} for k, n in sorted(owners.items(), key=lambda kv: (-kv[1], kv[0]))],
                "inbox": {"counts": inbox.summary(items), "items": items},
                "slots": {"used": sum(1 for s in sbs if s.state in LIVE_STATES),
                          "max": self.h.settings.max_sandboxes}}

    def _person(self, pid: str) -> dict[str, Any]:
        try:
            pr = self.h.store.get(Person, pid)
        except Exception:  # noqa: BLE001
            pr = None
        return {"name": pr.name or pid, "role": pr.role} if pr is not None else {"name": pid, "role": None}

    def _unit(self, sb: Sandbox, job: Job | None, ses: Session | None, live: LiveState | None, stats: Any,
              waiting: list[dict[str, Any]], at: float) -> dict[str, Any]:
        from hangar.fairness import person_of

        kind = "job" if job is not None else "session" if ses is not None else "sandbox"
        actor = job.requester if job is not None else ses.owner if ses is not None else (sb.created_by or "")
        owner = person_of(job.requester, job.person) if job is not None else \
            person_of(ses.owner, ses.person) if ses is not None else person_of(sb.created_by, sb.person)
        pending = steer.pending(ses)
        turn = ses.turns[-1] if ses is not None and ses.turns else None
        live_turn = live is not None and turn is not None and not turn.finished_at and live.turn == len(ses.turns)
        denial = None
        if turn is not None and not turn.finished_at:
            denial = next((a for a in reversed(turn.approvals) if a.status == "denied" and a.by == "policy"), None)
        question = (job.result.question if job is not None else None) or (ses.needs_input if ses is not None
                                                                           else None) or ""
        is_live = sb.state in LIVE_STATES
        role, label = SB_ROLE.get(sb.state.value, "off"), SB_WORD.get(sb.state.value, _word(sb.state.value))
        reason: dict[str, str] | None = None
        ask: dict[str, Any] | None = None
        ask_expires: float | None = None  # when the wait ends by itself (a tool approval times out, a held job expires)
        asks = [w for w in waiting if w["kind"] in ("approval", "question")]
        if sb.state == SandboxState.ERROR:
            reason = {"title": "Error", "text": sb.error or "The sandbox stopped on an error."}
        elif job is not None and job.state == JobState.AWAITING_APPROVAL:
            role, label = "approval", "Needs approval"
            ask = {"kind": "job", "job": job.id}
            ask_expires = job.approval.expires_at if job.approval else None
            reason = {"title": "Held", "text": (job.approval.reason if job.approval else "") or
                      "Started from outside the team: waits for a trusted person to approve it."}
        elif pending and is_live:
            a = pending[0]
            role, label = "approval", "Needs approval"
            ask = {"kind": "tool", "approval": a.id, "job": job.id if job else None,
                   "session": ses.id if ses else None}
            ask_expires = a.expires_at
            reason = {"title": "Held", "text": f"{a.tool or 'A request'}" + (f": {a.detail}" if a.detail else "")
                      + (f" ({a.taint})" if a.taint else "")}
        elif question and is_live and ((job is not None and job.state == JobState.NEEDS_INPUT) or
                                       (ses is not None and job is None and ses.state != SessionState.RUNNING
                                        and ses.needs_input)):
            role, label = "approval", "Needs input"
            ask = {"kind": "question", "job": job.id if job else None, "session": ses.id if ses else None}
            reason = {"title": "Question", "text": question}
        elif asks and is_live:  # a request another module raised (a secret, a host): the inbox says so
            w = asks[0]
            role, label = "approval", "Needs input" if w["kind"] == "question" else "Needs approval"
            ask = {"kind": "inbox", "item": w["id"], "request": w.get("request")}
            reason = {"title": "Held", "text": w["reason"]}
        elif denial is not None and is_live:
            role, label = "denied", "Denied"
            reason = {"title": "Policy", "text": f"{denial.tool or 'A tool'}" + (f": {denial.detail}" if denial.detail
                                                                                else "") + " denied"
                      + (f". {denial.message}" if denial.message else "")}
        elif job is not None and job.state == JobState.FAILED and is_live:
            role, label = "failed", "Failed"
            r = job.result
            reason = {"title": "Failed", "text": r.result_error or (f"The check failed (exit {r.verify_exit_code})."
                                                                    if r.verify_exit_code else
                                                                    f"{_word(job.reason or 'failed')}.")}
        busy = ses is not None and self.sessions.busy(ses.id)
        stage = None if role in ("off", "queued") else stage_of(job, ses, live if live_turn else None,
                                                                 reason["text"] if reason else None, busy)
        env, harness = sb.environment, (job.harness if job else None) or (ses.harness if ses else None) or \
            str(sb.info.get("harness") or "")
        # stages: the live turn's, then what hangar drove (verify, deliver)
        stages = [dict(s) for s in live.stages] if live is not None and live_turn else []
        if job is not None:
            stages += _job_stages(job)
        # the agent's own progress, this turn only; a guess from time otherwise
        pct = None
        if live_turn and live is not None and live.progress and "pct" in live.progress:
            pct = live.progress["pct"]
        elif turn is not None and not turn.finished_at and turn.progress and "pct" in turn.progress:
            pct = turn.progress["pct"]
        est = None if pct is not None else stage_estimate(live if live_turn else None, stage, env, harness, stats,
                                                          self.typical, at)
        now_line = live.now if live is not None and live_turn else None
        if now_line is None and job is not None and job.state in (JobState.VERIFYING, JobState.DELIVERING) \
                and job.events:
            now_line = job.events[-1].message
        last_at = live.last_activity_at if live is not None and live.last_activity_at else sb.last_activity
        usage = total_usage(ses.turns) if ses is not None and ses.turns else (total_usage(job.turns) if job else None)
        r = job.result if job is not None else None
        work = (job.title if job else None) or (ses.title or ses.name if ses else None) or sb.name
        task = (job.instructions if job else None) or (turn.message if turn else None) or ""
        u = {
            "id": sb.id, "tail": sb.id[-4:], "name": sb.name, "environment": env, "provider": sb.provider,
            "state": sb.state.value, "kind": kind, "job": job.id if job else None,
            "job_state": job.state.value if job else None,
            "session": {"id": ses.id, "name": ses.name, "state": ses.state.value} if ses else None,
            "owner": owner, "actor": actor, "harness": harness, "model": (job.model if job else None) or
            (ses.model if ses else None), "repo": (job.repo if job else None) or (ses.repo if ses else None)
            or sb.repo, "branch": (job.branch if job else None) or (ses.branch if ses else None) or sb.branch,
            "role": role, "label": label, "reason": reason, "ask": ask, "live": is_live,
            "work": work, "task": task[:TASK_MAX], "question": question or None,
            "now": now_line, "last_activity_at": last_at,
            "stage": stage, "stage_pct": pct, "stage_estimate": est, "stages": stages,
            "files_changed": (len(r.files) if r is not None and r.files else 0) or (len(live.files) if live else 0),
            "commits": (len(r.commits) if r is not None and r.commits else 0) or (live.commits if live else 0),
            "usage": usage.model_dump(mode="json") if usage is not None else None,
            # S2: new tokens per five minutes over the last hour, oldest first (in memory: empty after a restart)
            "tokens_1h": live.tokens_1h(at) if live is not None else [0] * TOKEN_BUCKETS,
            "pr": r.pr_url if r is not None else None,
            "created_at": sb.created_at, "expires_at": sb.expires_at, "turn": len(ses.turns) if ses else 0,
            "inbox": [w["id"] for w in waiting],
            "ask_expires_at": ask_expires or next((w["expires_at"] for w in waiting if w.get("expires_at")), None),
        }
        return self.red(u)


# ====================================================================== front door
def mount(app: Any, h: Any, person_actor: Any) -> None:
    """GET /api/fleet: an admin's token (everything), or a person's own `user` token (their work)."""
    reader = FleetRead(h)
    h.fleet_read = reader

    @app.get("/api/fleet")
    async def fleet_route(include_stopped: bool = False, owner: str | None = None, _: str = person_actor):
        if owner is not None and (not owner or len(owner) > 100):
            raise HTTPException(400, "owner is a person id, or none")
        return FastJSON(reader.units(identity.principal(), include_stopped, owner))


__all__ = ["ATTENTION", "STAGES", "FleetRead", "mount", "stage_estimate", "stage_of"]
