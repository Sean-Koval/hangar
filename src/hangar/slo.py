"""SLO samples for the operator console (--- clouddoctor; docs/observability.md#slos).

`hangar status` answers "are creates, turn starts and resumes as fast as they should be" without a Prometheus
server: each create, turn start and session resume records how long it took, here, in memory, and the ops loop
saves them to the store every minute so a restart keeps the last day. Percentiles are over the last 24 hours.

The targets are the observability review's (team/observability.md §3a), measured on GKE:

| What | Path | Target |
| --- | --- | --- |
| create | warm (a pod claimed from the warm pool) | p50 <= 1.5 s, p95 <= 2.5 s |
| create | cold (a new pod, the image cached) | p95 <= 30 s |
| create | build (an image or pack built on the way) | none: exceptional by design |
| turn start | from the request to the agent running, sandbox up | p50 <= 5 s, p95 <= 15 s |
| resume | hot (a parked sandbox woken) / cold (restored from a checkpoint) | p95 <= 5 s / <= 60 s |

/metrics carries the same creates as `hangar_sandbox_create_seconds` for people who scrape it.
"""

from __future__ import annotations

import collections
import time
from typing import Any

WINDOW = 86400.0
MAX_SAMPLES = 2000  # per metric: a team of six creates a few hundred sandboxes a day
KV = "slo:samples"
# (metric, path) -> (p50 target, p95 target), None where there is none
TARGETS: dict[tuple[str, str], tuple[float | None, float | None]] = {
    ("create", "warm"): (1.5, 2.5),
    ("create", "cold"): (None, 30.0),
    ("turn_start", "any"): (5.0, 15.0),
    ("resume", "hot"): (None, 5.0),
    ("resume", "cold"): (None, 60.0),
}


class Samples:
    def __init__(self) -> None:
        self.by: dict[str, collections.deque[tuple[float, float, str]]] = {}
        self.dirty = False

    def record(self, metric: str, seconds: float, path: str = "any", at: float | None = None) -> None:
        if seconds < 0:
            return
        q = self.by.setdefault(metric, collections.deque(maxlen=MAX_SAMPLES))
        q.append((at if at is not None else time.time(), round(seconds, 3), path))
        self.dirty = True

    def summary(self, t: float | None = None) -> dict[str, dict[str, dict[str, Any]]]:
        """{metric: {path: {n, p50, p95, target_p50, target_p95, ok}}} over the last WINDOW seconds. `ok` is None
        with no target or no samples."""
        t = t if t is not None else time.time()
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for metric, q in sorted(self.by.items()):
            paths: dict[str, list[float]] = {}
            for at, secs, path in q:
                if t - at <= WINDOW:
                    paths.setdefault(path, []).append(secs)
            for path, xs in sorted(paths.items()):
                p50, p95 = _pct(xs, 50), _pct(xs, 95)
                want50, want95 = TARGETS.get((metric, path), (None, None))
                checks = [v <= w for v, w in ((p50, want50), (p95, want95)) if w is not None]
                out.setdefault(metric, {})[path] = {"n": len(xs), "p50": p50, "p95": p95, "target_p50": want50,
                                                    "target_p95": want95, "ok": all(checks) if checks else None}
        return out

    # ------------------------------------------------------------------ persistence (the ops loop)
    def save(self, store: Any) -> None:
        if not self.dirty:
            return
        t = time.time()
        store.kv_set_json(KV, {m: [list(s) for s in q if t - s[0] <= WINDOW] for m, q in self.by.items()})
        self.dirty = False

    def load(self, store: Any) -> None:
        """Samples a previous process saved, before any of this one's (a restart keeps the day)."""
        got = store.kv_get_json(KV, None)
        if not isinstance(got, dict):
            return
        for metric, rows in got.items():
            q = self.by.setdefault(str(metric), collections.deque(maxlen=MAX_SAMPLES))
            old = [(float(r[0]), float(r[1]), str(r[2])) for r in rows if isinstance(r, list) and len(r) == 3]
            mine = list(q)
            q.clear()
            q.extend(sorted(old + mine))


def _pct(xs: list[float], p: int) -> float:
    """Nearest-rank percentile: a sample that happened, never an interpolation between two."""
    s = sorted(xs)
    k = max(0, min(len(s) - 1, -(-p * len(s) // 100) - 1))
    return s[k]


SAMPLES = Samples()  # process-wide, like metrics.TICKS


def record(metric: str, seconds: float, path: str = "any") -> None:
    SAMPLES.record(metric, seconds, path)


def created(sb: Any, fork: bool = False) -> None:
    """A create that reached running: `warm` when it claimed a warm pod, `build` when it built an image or pack on
    the way (its stages say so), else `cold`. Forks restore a snapshot: not a create SLO."""
    if fork:
        return
    stages = sb.info.get("stages") or []
    building = any("building" in str(s[1]) for s in stages if isinstance(s, list) and len(s) == 2)
    path = "warm" if sb.info.get("warm") else "build" if building else "cold"
    record("create", time.time() - sb.created_at, path)
