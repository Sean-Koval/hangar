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
        self.budget_stops = Counter("hangar_budget_stops", "Agent turns stopped over their budget",
                                    ["harness", "environment"], registry=r)
        self.ckpt_seconds = Histogram("hangar_checkpoint_seconds", "Checkpoint capture and restore duration",
                                      ["op", "result"], buckets=_SLOW, registry=r)
        self.ckpt_bytes = Histogram("hangar_checkpoint_bytes", "Plaintext bytes a stored checkpoint holds",
                                    ["reason"], buckets=_BYTES, registry=r)
        self.pushes = Counter("hangar_gateway_pushes", "Pushes the git gateway forwarded", ["result"], registry=r)
        self.denials = Counter("hangar_gateway_denials", "Requests the git gateway refused", ["reason"], registry=r)
        self.mcp_calls = Counter("hangar_mcp_calls", "MCP tool calls", ["tool", "status"], registry=r)
        self.mcp_seconds = Histogram("hangar_mcp_call_seconds", "MCP tool call duration", ["tool"], buckets=_SECONDS,
                                     registry=r)
        self.http = Histogram("hangar_http_request_seconds", "REST, dashboard and MCP HTTP requests",
                              ["method", "route", "status"], buckets=_SECONDS, registry=r)
        self.lag = Histogram("hangar_event_loop_lag_seconds", f"How late a {LAG_INTERVAL}s sleep on the event loop "
                             "woke up", buckets=_LAG, registry=r)
        self.lag_last = Gauge("hangar_event_loop_lag_last_seconds", "The latest event-loop lag sample", registry=r)
        self.io_queue = Gauge("hangar_io_pool_queue_depth", "Blocking calls waiting for an I/O worker thread",
                              registry=r)
        self.io_threads = Gauge("hangar_io_pool_threads", "I/O worker threads started", registry=r)
        self.io_max = Gauge("hangar_io_pool_max_threads", "I/O worker thread limit (HANGAR_IO_THREADS)", registry=r)
        self._lag_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ scrape-time gauges
    def collect_state(self, h: Any) -> None:
        """Set the state gauges from the fleet, sessions and jobs, just before the registry is rendered. On the
        loop, which owns the session and job maps; sandboxes are one GROUP BY in SQLite, like any store call. Not
        on the I/O pool: a scrape must answer exactly when that pool is saturated."""
        from hangar.models import Sandbox

        counts = h.store.count_by(Sandbox, "state", "provider")
        self.sandboxes.clear()
        for (state, provider), n in counts.items():
            self.sandboxes.labels(str(state), str(provider)).set(n)
        self.sessions.clear()
        for state, n in collections.Counter(s.state.value for s in h.sessions._sessions.values()).items():
            self.sessions.labels(state).set(n)
        self.jobs.clear()
        for (state, reason), n in collections.Counter((j.state.value, j.reason or "")
                                                      for j in h.jobs._jobs.values()).items():
            self.jobs.labels(state, self.label("reason", reason) if reason else "").set(n)
        pool = getattr(asyncio.get_running_loop(), "_default_executor", None)
        if pool is not None and hasattr(pool, "_work_queue"):
            self.io_queue.set(pool._work_queue.qsize())
            self.io_threads.set(len(pool._threads))
            self.io_max.set(pool._max_workers)

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
                  outcome: str, seconds: float | None, usage: Any) -> None:
    if M is None:
        return
    lb = M.label
    harness_l = lb("harness", harness)
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


def checkpoint(op: str, result: str, seconds: float, reason: str | None = None, size: int | None = None) -> None:
    if M is None:
        return
    M.ckpt_seconds.labels(op, result).observe(seconds)
    if size is not None:
        M.ckpt_bytes.labels(M.label("reason", reason)).observe(size)


def gateway_push(result: str) -> None:
    if M is not None:
        M.pushes.labels(result).inc()


def gateway_denied(reason: str) -> None:
    if M is not None:
        M.denials.labels(reason).inc()


def mcp_call(tool: str, status: str, seconds: float) -> None:
    if M is None:
        return
    t = M.label("tool", tool)
    M.mcp_calls.labels(t, status).inc()
    M.mcp_seconds.labels(t).observe(seconds)


def http_request(method: str, route: str, status: int, seconds: float) -> None:
    if M is not None:
        M.http.labels(method, M.label("route", route), str(status)).observe(seconds)
