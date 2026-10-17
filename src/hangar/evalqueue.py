"""The eval queue class (#181): trials wait for the evening, never ahead of a person, and under a token budget.

Trials share the providers' rate limits (tokens per minute) with people's interactive work, and the 01:00
power-down kills whatever is in flight. So every job has a `queue_class`:

* `interactive` (the default; AI reviews too): what people wait on. Always picked first.
* `eval`: experiment trials (and jobs submitted with `queue_class: eval`). One starts only
  - inside the eval window (HANGAR_EVAL_WINDOW, `20:00-00:15` in HANGAR_EVAL_TZ, `America/New_York`): no new trial
    after 00:15, so most finish before the power-down; an admin's `priority: now` lifts the window for one job;
  - while fewer than HANGAR_EVAL_MAX_RUNNING eval jobs run (0: half the job slots), so interactive work always
    finds a slot;
  - while the provider's observed token rate plus a typical trial's (the median of recent eval turns, else
    HANGAR_EVAL_TPM_PER_TRIAL) stays within HANGAR_EVAL_TPM_BUDGET for that provider.
  A held job stays `queued` in its place with a reason (`eval_window`, `eval_slots`, `eval_tpm`) and an event.

**Observed rate.** A rolling 60 seconds per provider (the harness's vendor: claude-code -> anthropic, codex ->
openai): turns still running count their live usage spread over the time they have run, finished turns the part of
their usage that falls in the last minute, and exact counts (`Meter.note` with start == end, from the model gateway
once #177 lands) count in full. Turn-end usage lags; the gateway's counts don't.

**Power-down.** On a drain (POST /api/admin/drain, `power.sh down`) running trials are cancelled, not checkpointed:
a trial resumed the next morning would not have run under the conditions the others did. Each is marked
`infra_error` / `power_down` (#166), so the experiment leaves it out and queues a fresh attempt, which, like every
queued eval job, waits for the next window rather than starting at power-up.
"""

from __future__ import annotations

import collections
import datetime as dt
import re
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from hangar.models import JobState

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.fleet import Fleet
    from hangar.jobs import JobService
    from hangar.models import Job

CLASSES = ("interactive", "eval")
EVAL = "eval"
NOW = "now"
RATE_WINDOW = 60.0  # seconds: the rolling window of the observed token rate
TRIAL_SAMPLES = 50  # eval turns per provider the per-trial median is taken over
WAIT_WINDOW, WAIT_SLOTS, WAIT_TPM = "eval_window", "eval_slots", "eval_tpm"
REASONS = (WAIT_WINDOW, WAIT_SLOTS, WAIT_TPM)
POWER_DOWN = "power_down"
RECHECK_SECONDS = 15.0  # the queue looks again this often: the window opens and the rate drops with no event


def clock() -> float:
    """Now, in epoch seconds (tests replace it with a fake clock)."""
    return time.time()


def provider_of(harness: str | None) -> str:
    """The vendor whose rate limit a harness's turns spend."""
    from hangar.modelpolicy import PROVIDER

    return PROVIDER.get(harness or "", harness or "unknown")


# ====================================================================== the window
_SPEC = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


class Window:
    """`HH:MM-HH:MM` in a time zone; the end may be past midnight. `always` (or an empty spec) is no window."""

    def __init__(self, spec: str | None, tz: str = "UTC"):
        self.spec = (spec or "").strip()
        self.tz = ZoneInfo(tz or "UTC")
        self.span: tuple[int, int] | None = None
        if self.spec.lower() in ("", "always", "off", "none"):
            return
        m = _SPEC.match(self.spec)
        if not m:
            raise ValueError(f"eval window '{self.spec}' is not HH:MM-HH:MM (or 'always')")
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
            raise ValueError(f"eval window '{self.spec}' has an invalid time")
        a, b = h1 * 60 + m1, (h2 * 60 + m2) % 1440
        if a != b:
            self.span = (a, b)

    @property
    def always(self) -> bool:
        return self.span is None

    def _minute(self, t: float) -> int:
        local = dt.datetime.fromtimestamp(t, self.tz)
        return local.hour * 60 + local.minute

    def open(self, t: float) -> bool:
        if self.span is None:
            return True
        a, b = self.span
        m = self._minute(t)
        return a <= m < b if a < b else (m >= a or m < b)

    def next_open(self, t: float) -> float:
        """When the window next opens (t itself while it is open)."""
        if self.open(t) or self.span is None:
            return t
        local = dt.datetime.fromtimestamp(t, self.tz)
        start = local.replace(hour=self.span[0] // 60, minute=self.span[0] % 60, second=0, microsecond=0)
        if start <= local:
            start = (start.replace(tzinfo=None) + dt.timedelta(days=1)).replace(tzinfo=self.tz)
        return start.timestamp()

    def describe(self) -> str:
        return "always" if self.span is None else f"{self.spec} {self.tz.key}"


def budgets(settings: Settings) -> dict[str, int]:
    """HANGAR_EVAL_TPM_BUDGET: `400000` (every provider, as `*`) or `anthropic=400000,openai=300000`."""
    raw = (settings.eval_tpm_budget or "").strip()
    out: dict[str, int] = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, n = part.rpartition("=")
        try:
            out[name.strip() or "*"] = max(0, int(float(n)))
        except ValueError as e:
            raise ValueError(f"HANGAR_EVAL_TPM_BUDGET: '{part}' is not provider=tokens") from e
    return out


# ====================================================================== the observed rate
class Meter:
    """Tokens per provider over the last RATE_WINDOW seconds, and what one eval turn spends per minute."""

    def __init__(self) -> None:
        self._done: dict[str, collections.deque[tuple[float, float, int]]] = collections.defaultdict(
            collections.deque)
        self._trial: dict[str, collections.deque[float]] = collections.defaultdict(
            lambda: collections.deque(maxlen=TRIAL_SAMPLES))

    def note(self, provider: str, tokens: int, start: float, end: float) -> None:
        """`tokens` spent between `start` and `end` (start == end: an exact count at that moment)."""
        if tokens <= 0:
            return
        q = self._done[provider]
        q.append((min(start, end), end, int(tokens)))
        cutoff = clock() - RATE_WINDOW
        while q and q[0][1] < cutoff:
            q.popleft()

    def note_trial(self, provider: str, tokens: int, seconds: float) -> None:
        """An eval turn finished: its tokens per minute, for the per-trial estimate."""
        if tokens > 0 and seconds > 0:
            self._trial[provider].append(tokens * 60.0 / max(seconds, RATE_WINDOW))

    def p50(self, provider: str) -> float | None:
        """The median eval turn's tokens per minute (an admission estimate, a runtime decision: no analysis)."""
        q = sorted(self._trial.get(provider) or ())
        if not q:
            return None
        mid = len(q) // 2
        return q[mid] if len(q) % 2 else (q[mid - 1] + q[mid]) / 2

    @staticmethod
    def _part(tokens: int, start: float, end: float, at: float) -> float:
        lo = at - RATE_WINDOW
        if end - start < 1.0:
            return float(tokens) if lo <= end <= at else 0.0
        overlap = max(0.0, min(end, at) - max(start, lo))
        return tokens * overlap / (end - start)

    def rate(self, provider: str, at: float, live: Iterable[tuple[int, float]] = ()) -> float:
        """Tokens in the last minute: finished spans' share, plus each live turn's (tokens so far, started at)."""
        total = sum(self._part(n, s, e, at) for s, e, n in self._done.get(provider, ()))
        total += sum(self._part(n, s, at, at) for n, s in live)
        return total

    def providers(self) -> set[str]:
        return set(self._done) | set(self._trial)


def meter(fleet: Fleet) -> Meter:
    """The fleet's meter (made on first use): fed by every turn's end, read by the queue."""
    m = fleet.__dict__.get("_eval_meter")
    if m is None:
        m = fleet.__dict__["_eval_meter"] = Meter()
    return m


def turn_ended(fleet: Fleet, s: Any, turn: Any, job: Job | None) -> None:
    """A session's turn ended: its usage counts towards its provider's rate, and an eval job's towards the
    per-trial estimate. Never raises: accounting must not fail a turn."""
    try:
        u = turn.usage
        if u is None or not u.tokens or not turn.started_at:
            return
        end = turn.finished_at or clock()
        provider = provider_of(turn.harness or s.harness)
        m = meter(fleet)
        m.note(provider, u.tokens, turn.started_at, end)
        if job is not None and job.queue_class == EVAL:
            m.note_trial(provider, u.tokens, end - turn.started_at)
    except Exception:  # noqa: BLE001
        return


# ====================================================================== the queue's side
def is_eval(job: Job | None) -> bool:
    return job is not None and job.queue_class == EVAL


def slots(settings: Settings) -> int:
    """Eval jobs running at once: HANGAR_EVAL_MAX_RUNNING, else half the job slots (at least one)."""
    from hangar.fairness import job_slots

    if settings.eval_max_running > 0:
        return settings.eval_max_running
    return max(1, job_slots(settings) // 2)


class EvalQueue:
    """What the job queue asks about each waiting job: its rank (interactive first) and whether it may start."""

    def __init__(self, svc: JobService):
        self.svc = svc

    @property
    def window(self) -> Window:
        s = self.svc.settings
        return Window(s.eval_window, s.eval_tz)

    def rank(self, job_id: str) -> int:
        return 1 if is_eval(self.svc._jobs.get(job_id)) else 0

    def running(self) -> list[Job]:
        jobs = (self.svc._jobs.get(j) for j in list(self.svc._active))
        return [j for j in jobs if is_eval(j)]

    def live(self, provider: str) -> list[tuple[int, float]]:
        """(tokens so far, started at) of the turns running now on `provider`."""
        sessions = getattr(self.svc, "sessions", None)
        out = []
        for s in list(sessions._sessions.values()) if sessions is not None else []:
            t = s.turns[-1] if s.turns else None
            if t is None or t.finished_at or not t.started_at or t.usage is None or not t.usage.tokens:
                continue
            if provider_of(t.harness or s.harness) == provider:
                out.append((t.usage.tokens, t.started_at))
        return out

    def observed(self, provider: str, at: float | None = None) -> float:
        at = clock() if at is None else at
        return meter(self.svc.fleet).rate(provider, at, self.live(provider))

    def per_trial(self, provider: str) -> float:
        p = meter(self.svc.fleet).p50(provider)
        return p if p is not None else float(self.svc.settings.eval_tpm_per_trial)

    def budget(self, provider: str) -> int | None:
        b = budgets(self.svc.settings)
        return b.get(provider, b.get("*"))

    def why_wait(self, job: Job, at: float | None = None) -> tuple[str, str] | None:
        """(reason, sentence) when an eval job must keep waiting; None when it may start."""
        at = clock() if at is None else at
        w = self.window
        if job.priority != NOW and not w.open(at):
            nxt = dt.datetime.fromtimestamp(w.next_open(at), w.tz).strftime("%H:%M %Z")
            return WAIT_WINDOW, f"eval jobs start inside the eval window ({w.describe()}); next at {nxt}"
        cap = slots(self.svc.settings)
        if len(self.running()) >= cap:
            return WAIT_SLOTS, f"{cap} eval jobs are running (HANGAR_EVAL_MAX_RUNNING)"
        provider = provider_of(job.harness)
        budget = self.budget(provider)
        if budget is not None:
            seen, one = self.observed(provider, at), self.per_trial(provider)
            if seen + one > budget:
                return WAIT_TPM, (f"{provider} is at {seen:,.0f} tokens/min; one more trial (~{one:,.0f}) would pass "
                                  f"the eval budget of {budget:,} (HANGAR_EVAL_TPM_BUDGET)")
        return None

    def admit(self, job_id: str) -> bool:
        """FairJobQueue's `admit` for the eval class: interactive jobs always; an eval job when `why_wait` is
        None. A held job keeps its place and `queued` state; its reason says why, with an event when that changes."""
        job = self.svc._jobs.get(job_id)
        if not is_eval(job) or job.cancel_requested or job.state != JobState.QUEUED:
            return True
        held = self.why_wait(job)
        if held is None:
            return True
        reason, why = held
        if job.reason != reason:
            self.svc._event(job, f"waiting: {why}", reason=reason)
        return False

    def admitted(self, job_id: str) -> None:
        """A worker took the job: an eval job that waited says it starts."""
        job = self.svc._jobs.get(job_id)
        if job is not None and job.reason in REASONS and job.state == JobState.QUEUED:
            self.svc._event(job, "eval queue: starting", reason=None)

    def power_down(self, actor: str = "power-down") -> list[str]:
        """A drain began: cancel the running experiment trials as `infra_error` / `power_down`; the experiment
        queues a fresh attempt for the next window. Other eval jobs drain like any job."""
        from hangar import experiments

        cut = []
        for job in self.running():
            if experiments.EXP_LABEL not in job.labels or job.cancel_requested:
                continue
            job.infra_cut = POWER_DOWN
            try:
                self.svc.cancel(job.id, actor)
            except Exception:  # noqa: BLE001 - it ended meanwhile
                continue
            self.svc._event(job, "power-down: trial cancelled (infra_error/power_down); the experiment runs it "
                                 "again in the next eval window")
            cut.append(job.id)
        return cut

    def status(self, at: float | None = None) -> dict[str, Any]:
        at = clock() if at is None else at
        w = self.window
        waiting = [self.svc._jobs.get(j) for j in self.svc._queue.waiting()]
        held = [j for j in waiting if is_eval(j)]
        providers = set(budgets(self.svc.settings)) - {"*"} | meter(self.svc.fleet).providers()
        return {
            "window": w.describe(), "open": w.open(at), "next_open": w.next_open(at),
            "max_running": slots(self.svc.settings), "running": [j.id for j in self.running()],
            "waiting": [{"job": j.id, "reason": j.reason, "title": j.title, "priority": j.priority}
                        for j in held[:200] if j is not None],
            "tpm": {p: {"observed": round(self.observed(p, at)), "per_trial": round(self.per_trial(p)),
                        "budget": self.budget(p)} for p in sorted(providers)},
        }

    def prioritize(self, job_id: str, actor: str) -> Job:
        """An admin lets one queued eval job start outside the window (`priority: now`)."""
        from hangar.jobs import JobError

        job = self.svc.get(job_id)
        if job.state != JobState.QUEUED:
            raise JobError(f"job {job.id} is {job.state}; only a queued job is prioritized")
        job.priority = NOW
        self.svc._event(job, f"priority: now (by {actor}): starts outside the eval window",
                        reason=None if job.reason == WAIT_WINDOW else False)
        self.svc.fleet.audit(actor, "job.priority", job.id, priority=NOW)
        self.svc._queue.wake()
        return job


def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/admin/eval-queue (admin): the window, eval jobs running and waiting, and the token rates.
    POST /api/jobs/{id}/priority (admin; body {"priority": "now"}): one eval job starts outside the window."""
    from fastapi import Body, HTTPException

    from hangar.jobs import JobError, JobNotFound

    svc: JobService = h.jobs

    @app.get("/api/admin/eval-queue")
    async def eval_queue(_: str = admin):
        return svc.evalq.status()

    @app.post("/api/jobs/{job_id}/priority")
    async def job_priority(job_id: str, body: dict[str, Any] = Body(default_factory=dict), who: str = admin):
        if body.get("priority", NOW) != NOW:
            raise HTTPException(422, "priority must be 'now'")
        try:
            return svc.view(svc.evalq.prioritize(job_id, who))
        except JobNotFound as e:
            raise HTTPException(404, str(e).strip("'\"")) from e
        except JobError as e:
            raise HTTPException(409, str(e)) from e
