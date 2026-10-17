"""Outcome classes (#166): whether an outcome is the agent's, or infrastructure's.

A spot preemption, the 01:00 power-down, rate-limit exhaustion and a provider's 5xx are not agent failures. Counted
as failures they corrupt yield metrics and paired experiments, so every turn, attempt, trial and job carries an
`outcome_class` beside its outcome:

* `agent`: the agent's own result, success or failure. What statistics count.
* `infra_error`: infrastructure ended it; `infra_reason` says what (`preempted`, `power_down`, `rate_limited`,
  `provider_5xx`, `sandbox_create`, `checkpoint`, `control_plane_restart`). An experiment retries such a trial as a
  fresh one and leaves it out (`excluded`); jobs aren't retried beyond today's restart and power-down resume, but
  the class is recorded and weekly yield leaves them out.
* `cancelled`: someone (or the experiment) stopped it.
* `human`: a person stepped in: an interrupt, or the agent asked a question.

None means unknown (a record from before #166) and is never read as `agent`.

Misclassification goes towards `agent`: a sandbox that died with no node-level cause on record (a container the
agent's fork bomb or memory took down, say) stays `agent`; only a recorded node event (the reconciler's
`pod_replaced`: node repair, scale-down, preemption), the power-down, a control-plane restart, or the harness's own
report of a provider error (HTTP 429 after its retries, a 5xx, `overloaded_error`) makes it `infra_error`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hangar.models import Job, Sandbox

CLASSES = ("agent", "infra_error", "cancelled", "human")
INFRA_REASONS = ("preempted", "power_down", "rate_limited", "provider_5xx", "sandbox_create", "checkpoint",
                 "control_plane_restart", "drift")  # --- #178: drift: a trial's turn 1 didn't run its pin set
# How a harness reports a provider error it gave up on: Claude Code's result (`API Error: 529 {..."overloaded_error"
# ...}`), Codex's `turn.failed` (`exceeded retry limit, last status: 429 Too Many Requests`), pi's and agy's errors.
_RATE = re.compile(r"\b429\b|rate[_ -]?limit|too many requests|resource[_ ]exhausted", re.I)
_5XX = re.compile(r"overloaded|\b5(?:0[0-4]|29)\b|internal server error|service unavailable|bad gateway|"
                  r"gateway time-?out|\bapi_error\b|server_error|upstream connect error", re.I)
LOG_TAIL = 256 * 1024  # the end of the run log is where the harness says why it stopped
LAST_ERRORS = 3


def provider_error(raw: str | bytes, harness: str | None = None) -> str | None:
    """`rate_limited` or `provider_5xx` when the harness's last errors in its run log are a provider's, else None.
    Only the harness's own error events count (a result marked as an error, `turn.failed`, an error item, a retry
    notice), never a tool's output: an agent that cats a log full of 503s is still the agent."""
    from hangar.events import parse_log

    data = raw.encode(errors="replace") if isinstance(raw, str) else raw
    if not data:
        return None
    data = data[-LOG_TAIL:]
    if (nl := data.find(b"\n")) > 0 and len(data) == LOG_TAIL:
        data = data[nl + 1:]  # the first line is cut
    try:
        evs, _ = parse_log(data, harness)
    except Exception:  # noqa: BLE001 - a log hangar can't read says nothing about the provider
        return None
    errors = [e.text for e in evs if e.kind == "error" and e.text][-LAST_ERRORS:]
    for text in reversed(errors):
        if _RATE.search(text):
            return "rate_limited"
        if _5XX.search(text):
            return "provider_5xx"
    return None


def classify_turn(outcome: str | None, exit_code: int | None = None, *, raw: str | bytes | None = None,
                  harness: str | None = None, cause: str | None = None, powered_down: bool = False,
                  infra: str | None = None) -> tuple[str | None, str | None]:
    """(outcome_class, infra_reason) for an agent turn that ended with `outcome`.

    `infra`: a reason the caller knows (a control-plane restart or the power-down cut the turn off). `cause`: the
    sandbox's flight-record cause when it went away (reconciler: `pod_replaced`, `gone`, `orphaned_by_restart`).
    `powered_down`: the sandbox pools were off when it went. `raw`: the run log, for a provider error."""
    if outcome is None:
        return None, None
    if infra:
        return "infra_error", infra
    if outcome == "cancelled":
        return "cancelled", None
    if outcome in ("interrupted", "needs_input"):
        return "human", None
    if outcome in ("gone", "error"):
        if powered_down:
            return "infra_error", "power_down"
        if cause == "pod_replaced":  # node repair, scale-down, preemption: the node went, not the agent
            return "infra_error", "preempted"
        if cause == "orphaned_by_restart":
            return "infra_error", "control_plane_restart"
    failed = outcome != "exited" or (exit_code or 0) != 0
    if failed and raw and outcome != "over_budget" and (why := provider_error(raw, harness)):
        return "infra_error", why
    return "agent", None


def gone_context(fleet: Any, sb: Sandbox | None) -> dict[str, Any]:
    """What `classify_turn` needs to know about a sandbox that went away: its recorded cause, and whether the pools
    were powered down (then the power-down took it)."""
    if sb is None:
        return {"cause": None, "powered_down": False}
    power = getattr(fleet, "power", None)
    off = bool(power is not None and getattr(power, "off", False) and power.affects(sb.provider))
    return {"cause": sb.info.get("cause"), "powered_down": off}


def mark(obj: Any, cls: str | None, reason: str | None = None) -> None:
    """Set `outcome_class` and `infra_reason` on a turn, attempt, trial or job result."""
    obj.outcome_class = cls
    obj.infra_reason = reason if cls == "infra_error" else None


def job_class(job: Job) -> tuple[str | None, str | None]:
    """The class of a finished job's outcome: one set on the way (a sandbox create or a checkpoint restore that
    failed, a control-plane restart with no resumes left), else its last agent turn's. A job that never ran a turn
    and failed for its own reasons (a bad branch, a bad spec) is `agent`: nothing infrastructural is on record."""
    from hangar.models import JobState

    r = job.result
    if r.outcome_class:
        return r.outcome_class, r.infra_reason
    if getattr(job, "infra_cut", None):  # --- #181: hangar ended it on purpose (the power-down cancels trials)
        return "infra_error", job.infra_cut
    if job.state == JobState.CANCELLED:
        return "cancelled", None
    if job.reason == "interrupted":  # restarts: the control plane restarted and the job couldn't resume
        return "infra_error", "control_plane_restart"
    if job.strategy == "best_of_n" and job.attempts:
        ok = [a for a in job.attempts if a.outcome_class == "agent"]
        if ok or job.state == JobState.SUCCEEDED:
            return "agent", None
        infra = [a for a in job.attempts if a.outcome_class == "infra_error"]
        if infra and len(infra) == len(job.attempts):
            return "infra_error", infra[0].infra_reason
    last = next((t for t in reversed(job.turns) if t.run_id), None)
    if last is not None and last.outcome_class:
        if last.outcome_class == "human" and job.state in (JobState.SUCCEEDED, JobState.FAILED):
            return "agent", None  # a person answered or redirected it, and the agent finished the job
        return last.outcome_class, last.infra_reason
    if last is None and job.state in (JobState.SUCCEEDED, JobState.FAILED):
        return "agent", None
    return None, None


def run_fields(obj: Any) -> dict[str, Any]:
    """The two fields as a run export (`run.json`, #163) carries them; None: unknown."""
    return {"outcome_class": getattr(obj, "outcome_class", None), "infra_reason": getattr(obj, "infra_reason", None)}
