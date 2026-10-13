"""Shared helpers for the benchmarks: timing, percentiles, and the result format.

A result file is one JSON object: {"meta": {...}, "metrics": {name: {"n", "p50_ms", "p95_ms", "mean_ms",
"min_ms", "max_ms", "unit"?}}, "notes": {...}}. `report.py` compares two of them.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # `tests.conftest` helpers (scratch_root) and `scripts.bench.*`


def pct(xs: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100). With few samples p95 is the max, which is the honest answer."""
    s = sorted(xs)
    if not s:
        return float("nan")
    k = max(0, min(len(s) - 1, int(round(q / 100 * len(s) + 0.5)) - 1))
    return s[k]


def summarize(samples_s: list[float]) -> dict[str, Any]:
    ms = [x * 1000 for x in samples_s]
    return {"n": len(ms), "p50_ms": round(pct(ms, 50), 2), "p95_ms": round(pct(ms, 95), 2),
            "mean_ms": round(statistics.fmean(ms), 2) if ms else None,
            "min_ms": round(min(ms), 2) if ms else None, "max_ms": round(max(ms), 2) if ms else None}


class Recorder:
    """Collects samples per metric; `with rec.time("x"):` adds one."""

    def __init__(self, name: str):
        self.name = name
        self.samples: dict[str, list[float]] = {}
        self.values: dict[str, Any] = {}
        self.t0 = time.time()

    @contextlib.contextmanager
    def time(self, metric: str) -> Iterator[None]:
        t = time.perf_counter()
        yield
        self.add(metric, time.perf_counter() - t)

    def add(self, metric: str, seconds: float) -> None:
        self.samples.setdefault(metric, []).append(seconds)

    def note(self, key: str, value: Any) -> None:
        self.values[key] = value

    def result(self, **meta: Any) -> dict[str, Any]:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True).stdout.strip()
        return {
            "meta": {"bench": self.name, "git": sha, "started": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                                               time.localtime(self.t0)),
                     "seconds": round(time.time() - self.t0, 1), "host": platform.node(),
                     "cpus": os.cpu_count(), "python": platform.python_version(), **meta},
            "metrics": {k: summarize(v) for k, v in self.samples.items()},
            "notes": self.values,
        }


def emit(result: dict[str, Any], out: str | None) -> None:
    text = json.dumps(result, indent=2)
    print(text)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n")
