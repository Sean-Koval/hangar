"""Prometheus metrics for operators: where time and money go, and when things break (docs/observability.md).

Counters and histograms are recorded where the work happens (through `observe`); gauges for things that already
have a state (sandboxes, sessions, jobs, the I/O pool) are read when /metrics is scraped, so nothing has to keep
them in step. Labels are bounded: never a job, session or sandbox id, and free-form values such as requesters
and models fold into "other" past a cap. `prometheus_client` is imported only when metrics are enabled.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import time  # --- ops
from typing import Any

# A requester, model or environment beyond this many distinct values is reported as "other".
LABEL_CAP = 200
LAG_INTERVAL = 0.5  # seconds between event-loop lag samples

_SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
_SLOW = (1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 900, 1800)  # sandbox starts and checkpoint work
_TURN = (10, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200)
_BYTES = (1 << 10, 1 << 14, 1 << 17, 1 << 20, 1 << 22, 1 << 24, 1 << 26, 1 << 28)
_LAG = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)


class _Bounded:
    """Label values seen so far, per label name; new ones past the cap become "other"."""

    def __init__(self, cap: int = LABEL_CAP):
        self.cap = cap
        self.seen: dict[str, set[str]] = collections.defaultdict(set)

    def __call__(self, name: str, value: Any) -> str:
        v = str(value)[:100] if value not in (None, "") else "unknown"
        seen = self.seen[name]
        if v in seen:
            return v
        if len(seen) >= self.cap:
            return "other"
        seen.add(v)
        return v


class Metrics:
    def __init__(self) -> None:
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

        self.registry = r = CollectorRegistry()
        self.label = _Bounded()
        self.sandboxes = Gauge("hangar_sandboxes", "Sandboxes by state and provider", ["state", "provider"],
                               registry=r)
        self.sandbox_create = Histogram("hangar_sandbox_create_seconds", "Time to a running sandbox (image build, "
                                        "start, setup and clone included)", ["provider", "environment", "result"],
                                        buckets=_SLOW, registry=r)
        self.warm = Counter("hangar_warm_pool_claims", "Sandbox starts for environments with a warm pool",
                            ["provider", "environment", "result"], registry=r)
        self.sessions = Gauge("hangar_sessions", "Sessions by state", ["state"], registry=r)
        self.jobs = Gauge("hangar_jobs", "Jobs by state and outcome (reason)", ["state", "reason"], registry=r)
        self.turn = Histogram("hangar_turn_seconds", "Agent turn duration", ["harness", "outcome"], buckets=_TURN,
                              registry=r)
        self.tokens = Counter("hangar_tokens", "Tokens agent turns used", ["kind", "model", "harness", "environment",
                                                                            "requester"], registry=r)
        self.cost = Counter("hangar_cost_usd", "Money agent turns used, in USD (turns with an unknown price add 0)",
                            ["model", "harness", "environment", "requester"], registry=r)
        self.unpriced = Counter("hangar_unpriced_turns", "Turns that used tokens of a model with no known price",
                                ["model", "harness"], registry=r)
        # --- overnight/cache: the cache hit ratio is hangar_tokens{kind="cache_read"} over the input, cache_read
        # and cache_write kinds; savings are hangar_cost_uncached_usd - hangar_cost_usd (docs/observability.md)
        self.cost_uncached = Counter("hangar_cost_uncached_usd", "What agent turns would have cost with no prompt "
                                     "cache, in USD (priced turns only)", ["model", "harness", "environment"],
                                     registry=r)
        self.write_1h = Counter("hangar_cache_write_1h_tokens", "Cache-write tokens written with a 1-hour TTL (part "
                                "of hangar_tokens{kind=\"cache_write\"}, priced at 2x input)", ["model", "harness"],
                                registry=r)
        self.cache_turns = Counter("hangar_cache_ttl_turns", "Agent turns by the prompt-cache TTL they asked for "
                                   "(none: the harness has no setting)", ["ttl", "harness"], registry=r)
        self.budget_stops = Counter("hangar_budget_stops", "Agent turns stopped over their budget",
                                    ["harness", "environment"], registry=r)
        self.ckpt_seconds = Histogram("hangar_checkpoint_seconds", "Checkpoint capture and restore duration",
                                      ["op", "result"], buckets=_SLOW, registry=r)
        self.ckpt_bytes = Histogram("hangar_checkpoint_bytes", "Plaintext bytes a stored checkpoint holds",
                                    ["reason"], buckets=_BYTES, registry=r)
        self.pushes = Counter("hangar_gateway_pushes", "Pushes the git gateway forwarded", ["result"], registry=r)
        self.denials = Counter("hangar_gateway_denials", "Requests the git gateway refused", ["reason"], registry=r)
        # --- egress: connections through the egress proxy (egress_proxy.py), by decision and a bounded reason
        self.egress = Counter("hangar_egress_connections", "Connections the egress proxy allowed or denied",
                              ["decision", "reason", "mode"], registry=r)
        self.egress_bytes = Counter("hangar_egress_bytes", "Bytes through the egress proxy (out: to the internet)",
                                    ["direction"], registry=r)
        self.egress_open = Gauge("hangar_egress_open_connections", "Tunnels open through the egress proxy",
                                 registry=r)
        self.mcp_calls = Counter("hangar_mcp_calls", "MCP tool calls", ["tool", "status"], registry=r)
        # integrate (#60): how commands reach sandboxes: hangar-envd, the provider's exec, or neither (error)
        self.execs = Histogram("hangar_sandbox_exec_seconds", "Sandbox commands hangar ran, by how they ran (envd, "
                               "exec, or error)", ["transport"], buckets=_SECONDS, registry=r)
        self.mcp_seconds = Histogram("hangar_mcp_call_seconds", "MCP tool call duration", ["tool"], buckets=_SECONDS,
                                     registry=r)
        self.http = Histogram("hangar_http_request_seconds", "REST, dashboard and MCP HTTP requests",
                              ["method", "route", "status"], buckets=_SECONDS, registry=r)
        # integrate (#57): database backups, so an alert can fire on a stale one
        self.backups = Counter("hangar_db_backups", "Database backups taken", ["result"], registry=r)
        self.backup_seconds = Histogram("hangar_db_backup_seconds", "Time to take and store a database backup",
                                        buckets=_SLOW, registry=r)
        self.backup_last = Gauge("hangar_db_backup_last_success_timestamp_seconds", "When this process last stored "
                                 "a database backup (Unix time)", registry=r)
        self.lag = Histogram("hangar_event_loop_lag_seconds", f"How late a {LAG_INTERVAL}s sleep on the event loop "
                             "woke up", buckets=_LAG, registry=r)
        self.lag_last = Gauge("hangar_event_loop_lag_last_seconds", "The latest event-loop lag sample", registry=r)
        self.io_queue = Gauge("hangar_io_pool_queue_depth", "Blocking calls waiting for an I/O worker thread",
                              registry=r)
        self.io_threads = Gauge("hangar_io_pool_threads", "I/O worker threads started", registry=r)
        self.io_max = Gauge("hangar_io_pool_max_threads", "I/O worker thread limit (HANGAR_IO_THREADS)", registry=r)
        # --- stream: streams to MCP and REST clients, and the turn logs and background processes hangar follows
        self.retries = Counter("hangar_job_retries", "Jobs retried from the deck (KAN-140): same (the canned "
                               "follow-up) or fresh (started over)", ["mode"], registry=r)
        self.streams_open = Gauge("hangar_streams_open", "Open streams by kind", ["kind"], registry=r)
        self.streams = Counter("hangar_streams", "Streams closed, by kind and why", ["kind", "reason"], registry=r)
        self.stream_bytes = Counter("hangar_stream_bytes", "Bytes streams sent", ["kind"], registry=r)
        self.stream_first = Histogram("hangar_stream_first_output_seconds", "From a stream's start to its first "
                                      "output", ["kind"], buckets=_SECONDS, registry=r)
        # --- reconciler: background loops (reaper, reconciler, sessions, schedules, backups), so an alert can fire on
        # one that stopped ticking or fails every pass (a pass that raises only logs)
        self.loop_tick = Gauge("hangar_loop_last_tick_timestamp", "When each background loop last finished a pass "
                               "(Unix time)", ["loop"], registry=r)
        self.loop_failures = Counter("hangar_loop_failures", "Background loop passes that raised", ["loop"],
                                     registry=r)
        # --- outbox (#163): run capture objects not uploaded yet (an outage shows as a growing number)
        self.outbox_pending = Gauge("hangar_outbox_pending", "Outbox objects waiting to be uploaded", registry=r)
        # --- review capacity (#159): new jobs held while their person has too many agent PRs awaiting review (no
        # person label: labels stay bounded)
        self.awaiting_capacity = Gauge("hangar_jobs_awaiting_capacity", "Queued jobs held for review capacity",
                                       registry=r)
        # --- KAN-144: the SLO signals (slolog.py, docs/slos.md)
        self.job_starts = Counter("hangar_job_starts", "Job starts: queued or provisioning to running (ok) or failed "
                                  "(error; rejected for the requester's own mistakes)", ["result"], registry=r)
        self.job_queue = Histogram("hangar_job_queue_seconds", "From queued to running, for jobs that started",
                                   buckets=_SLOW, registry=r)
        self.fail_closed = Counter("hangar_governance_fail_closed", "Decisions hangar answered itself because Tower "
                                   "didn't (never an allow)", ["kind", "failure"], registry=r)
        self.jobs_stuck = Gauge("hangar_jobs_stuck", "Live jobs that look stuck (slolog.stuck_jobs)", registry=r)
        # --- #181: the eval queue: each provider's observed token rate and the eval budget; eval jobs held, by why
        self.eval_tpm = Gauge("hangar_eval_observed_tpm", "Tokens per minute observed per provider (rolling 60 s)",
                              ["provider"], registry=r)
        self.eval_budget = Gauge("hangar_eval_tpm_budget", "HANGAR_EVAL_TPM_BUDGET per provider", ["provider"],
                                 registry=r)
        self.eval_waiting = Gauge("hangar_eval_jobs_waiting", "Queued eval jobs held by the eval queue, by reason",
                                  ["reason"], registry=r)
        self.eval_window = Gauge("hangar_eval_window_open", "1 while the eval window is open", registry=r)
        self._lag_task: asyncio.Task | None = None
        # --- durable: jobs and sessions by state, counted by the store in a worker thread (state_counts)
        self._counts: dict[str, dict[tuple, int]] = {}
        self._counting: asyncio.Future | None = None
        self._counted_at = -60.0

    # ------------------------------------------------------------------ scrape-time gauges
    def state_counts(self, store: Any) -> dict[str, dict[tuple, int]]:
        """--- durable: sessions and jobs by state (and jobs by reason), up to a minute old; archived ones aren't
        counted, as for sandboxes. SQLite reads every row to group them (tens of ms at a month of history), so a
        worker thread counts on a connection of its own while scrapes take the last count (none on the very first)."""
        from hangar.models import Job, Session

        async def count() -> dict[str, dict[tuple, int]]:
            return {"sessions": await asyncio.to_thread(store.count_by, Session, "state", own_connection=True),
                    "jobs": await asyncio.to_thread(store.count_by, Job, "state", "reason", own_connection=True)}

        try:
            asyncio.get_running_loop()
        except RuntimeError:  # called from a thread or a script: count here
            return {"sessions": store.count_by(Session, "state"), "jobs": store.count_by(Job, "state", "reason")}
        t = self._counting
        if t is not None and t.done():
            if not t.cancelled() and t.exception() is None:
                self._counts = t.result()
            self._counting = t = None
        if t is None and time.monotonic() - self._counted_at > 60:
            self._counted_at = time.monotonic()
            self._counting = asyncio.ensure_future(count())
        return self._counts
    def collect_state(self, h: Any) -> None:
        """Set the state gauges from the fleet, sessions and jobs, just before the registry is rendered. On the
        loop, which owns the session and job maps; sandboxes are one GROUP BY in SQLite, like any store call. Not
        on the I/O pool: a scrape must answer exactly when that pool is saturated."""
        from hangar.models import Sandbox

        counts = h.store.count_by(Sandbox, "state", "provider")
        self.sandboxes.clear()
        for (state, provider), n in counts.items():
            self.sandboxes.labels(str(state), str(provider)).set(n)
        counts = self.state_counts(h.store)  # --- durable: not all of them are in memory any more
        self.sessions.clear()
        for (state,), n in counts.get("sessions", {}).items():
            self.sessions.labels(str(state)).set(n)
        self.jobs.clear()
        for (state, reason), n in counts.get("jobs", {}).items():
            self.jobs.labels(str(state), self.label("reason", reason) if reason else "").set(n)
        from hangar.outbox import OutboxItem  # --- outbox (#163): one indexed count

        self.outbox_pending.set(h.store.count_where(OutboxItem, state="pending"))
        if (jobs := getattr(h, "jobs", None)) is not None:  # --- review capacity (#159): in memory, no query
            from hangar.fairness import awaiting

            self.awaiting_capacity.set(len(awaiting(jobs)))
            if (evalq := getattr(jobs, "evalq", None)) is not None:  # --- #181
                self._eval(evalq)
        pool = getattr(asyncio.get_running_loop(), "_default_executor", None)
        if pool is not None and hasattr(pool, "_work_queue"):
            self.io_queue.set(pool._work_queue.qsize())
            self.io_threads.set(len(pool._threads))
            self.io_max.set(pool._max_workers)

    def _eval(self, evalq: Any) -> None:
        st = evalq.status()
        self.eval_window.set(1 if st["open"] else 0)
        self.eval_tpm.clear()
        self.eval_budget.clear()
        for p, v in st["tpm"].items():
            self.eval_tpm.labels(self.label("provider", p)).set(v["observed"])
            if v["budget"] is not None:
                self.eval_budget.labels(self.label("provider", p)).set(v["budget"])
        self.eval_waiting.clear()
        for reason in ("eval_window", "eval_slots", "eval_tpm"):
            self.eval_waiting.labels(reason).set(sum(1 for w in st["waiting"] if w["reason"] == reason))

    def render(self) -> bytes:
        from prometheus_client import generate_latest

        return generate_latest(self.registry)

    # ------------------------------------------------------------------ event-loop lag
    def start_lag_monitor(self) -> None:
        if self._lag_task is None or self._lag_task.done():
            self._lag_task = asyncio.get_running_loop().create_task(self._lag_loop())

    async def stop_lag_monitor(self) -> None:
        if self._lag_task:
            self._lag_task.cancel()
            with contextlib.suppress(BaseException):
                await self._lag_task
            self._lag_task = None

    async def _lag_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            t = loop.time()
            await asyncio.sleep(LAG_INTERVAL)
            lag = max(0.0, loop.time() - t - LAG_INTERVAL)
            self.lag.observe(lag)
            self.lag_last.set(lag)


M: Metrics | None = None  # set by setup() when HANGAR_METRICS_ENABLED (the default)


def setup(enabled: bool) -> Metrics | None:
    """A fresh registry per app (tests build several in one process)."""
    global M
    M = Metrics() if enabled else None
    return M


def mount(app: Any, h: Any) -> None:
    """GET /metrics for an admin token or a `metrics`-scoped one (a scraper's token can do nothing else)."""
    from fastapi import Header, HTTPException, Response

    from hangar.auth import bearer_from_headers

    @app.get("/metrics", include_in_schema=False)
    async def metrics_route(authorization: str | None = Header(None)):
        if M is None:
            raise HTTPException(404, "metrics are off (HANGAR_METRICS_ENABLED=false)")
        res = h.auth.verify_scopes(bearer_from_headers({"authorization": authorization}))
        if res is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if "admin" not in res[1] and "metrics" not in res[1]:
            raise HTTPException(403, f"token scopes {res[1]} do not allow /metrics")
        from prometheus_client import CONTENT_TYPE_LATEST

        M.collect_state(h)
        return Response(M.render(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------- recording (no-ops when off)
def sandbox_created(provider: str, environment: str, seconds: float, ok: bool, warm: bool | None) -> None:
    if M is None:
        return
    env = M.label("environment", environment)
    M.sandbox_create.labels(provider, env, "ok" if ok else "error").observe(seconds)
    if warm is not None:
        M.warm.labels(provider, env, "hit" if warm else "miss").inc()


def turn_finished(*, harness: str | None, environment: str | None, model: str | None, requester: str | None,
                  outcome: str, seconds: float | None, usage: Any, cache_ttl: str | None = None) -> None:
    if M is None:
        return
    lb = M.label
    harness_l = lb("harness", harness)
    M.cache_turns.labels(cache_ttl or "none", harness_l).inc()  # --- overnight/cache
    if seconds is not None:
        M.turn.labels(harness_l, lb("outcome", outcome)).observe(max(0.0, seconds))
    env_l = lb("environment", environment)
    if outcome == "over_budget":
        M.budget_stops.labels(harness_l, env_l).inc()
    if usage is None:
        return
    model_l = lb("model", usage.model or model)
    req_l = lb("requester", requester)
    for kind, n in (("input", usage.input_tokens), ("output", usage.output_tokens),
                    ("cache_read", usage.cache_read_tokens), ("cache_write", usage.cache_write_tokens)):
        if n:
            M.tokens.labels(kind, model_l, harness_l, env_l, req_l).inc(n)
    if usage.cost_usd is not None:
        M.cost.labels(model_l, harness_l, env_l, req_l).inc(max(0.0, usage.cost_usd))
    elif usage.tokens or usage.cache_read_tokens:
        M.unpriced.labels(model_l, harness_l).inc()
    # --- overnight/cache
    if usage.cost_usd is not None and (usage.saved_usd is not None
                                       or not usage.cache_read_tokens + usage.cache_write_tokens):
        M.cost_uncached.labels(model_l, harness_l, env_l).inc(max(0.0, usage.cost_usd + (usage.saved_usd or 0.0)))
    if usage.cache_write_1h_tokens:
        M.write_1h.labels(model_l, harness_l).inc(usage.cache_write_1h_tokens)


def checkpoint(op: str, result: str, seconds: float, reason: str | None = None, size: int | None = None) -> None:
    if M is None:
        return
    M.ckpt_seconds.labels(op, result).observe(seconds)
    if size is not None:
        M.ckpt_bytes.labels(M.label("reason", reason)).observe(size)


def db_backup(ok: bool, seconds: float) -> None:  # integrate (#57)
    if M is None:
        return
    M.backups.labels("ok" if ok else "error").inc()
    if ok:
        M.backup_seconds.observe(seconds)
        M.backup_last.set_to_current_time()


TICKS: dict[str, float] = {}  # --- ops: also kept with metrics off, for doctor and the overview (power.loops)
FAILS: collections.Counter[str] = collections.Counter()


def loop_tick(loop: str) -> None:  # --- reconciler
    TICKS[loop] = time.time()  # --- ops
    if M is not None:
        M.loop_tick.labels(loop).set_to_current_time()


def loop_failed(loop: str) -> None:  # --- reconciler
    FAILS[loop] += 1  # --- ops
    if M is not None:
        M.loop_failures.labels(loop).inc()


def gateway_push(result: str) -> None:
    if M is not None:
        M.pushes.labels(result).inc()


def gateway_denied(reason: str) -> None:
    if M is not None:
        M.denials.labels(reason).inc()


def egress_connection(decision: str, reason: str, mode: str) -> None:  # --- egress
    if M is not None:
        M.egress.labels(decision, reason, mode).inc()


def egress_bytes(out: int, in_: int) -> None:  # --- egress
    if M is not None:
        M.egress_bytes.labels("out").inc(out)
        M.egress_bytes.labels("in").inc(in_)


def egress_open(delta: int) -> None:  # --- egress
    if M is not None:
        M.egress_open.inc(delta)


def sandbox_exec(transport: str, seconds: float) -> None:  # integrate (#60)
    if M is not None:
        M.execs.labels(transport).observe(seconds)


def mcp_call(tool: str, status: str, seconds: float) -> None:
    if M is None:
        return
    t = M.label("tool", tool)
    M.mcp_calls.labels(t, status).inc()
    M.mcp_seconds.labels(t).observe(seconds)


def http_request(method: str, route: str, status: int, seconds: float) -> None:
    if M is not None:
        M.http.labels(method, M.label("route", route), str(status)).observe(seconds)


# --- stream
def job_retry(mode: str) -> None:  # KAN-140 S6
    if M is not None:
        M.retries.labels(mode).inc()


def stream_opened(kind: str) -> None:
    if M is not None:
        M.streams_open.labels(kind).inc()


def stream_closed(kind: str, reason: str) -> None:
    if M is not None:
        M.streams_open.labels(kind).dec()
        M.streams.labels(kind, reason).inc()


def stream_sent(kind: str, n: int, first_after: float | None = None) -> None:
    if M is None:
        return
    M.stream_bytes.labels(kind).inc(n)
    if first_after is not None:
        M.stream_first.labels(kind).observe(first_after)
