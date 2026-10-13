"""Compare two benchmark results: a Markdown table of p50/p95 before and after, per metric.

    uv run python scripts/bench/report.py BEFORE.json AFTER.json [--fail-over 1.25]

With --fail-over X, exits 1 when any metric's p50 got more than X times slower (a regression gate for CI or a
nightly run). Metrics only in one file are listed with a dash on the other side.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def fmt(ms: float | None) -> str:
    if ms is None:
        return "–"
    return f"{ms / 1000:.2f} s" if ms >= 1000 else f"{ms:.1f} ms"


def change(a: float | None, b: float | None) -> str:
    if not a or b is None:
        return ""
    r = b / a
    return f"{1 / r:.1f}× faster" if r < 0.95 else (f"{r:.1f}× slower" if r > 1.05 else "same")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--fail-over", type=float)
    a = p.parse_args()
    before, after = (json.loads(Path(x).read_text()) for x in (a.before, a.after))
    mb, ma = before["metrics"], after["metrics"]
    print("| metric | p50 before | p50 after | p95 before | p95 after | p50 change |")
    print("| --- | --- | --- | --- | --- | --- |")
    worse = []
    for k in list(mb) + [k for k in ma if k not in mb]:
        b, x = mb.get(k, {}), ma.get(k, {})
        print(f"| {k} | {fmt(b.get('p50_ms'))} | {fmt(x.get('p50_ms'))} | {fmt(b.get('p95_ms'))} | "
              f"{fmt(x.get('p95_ms'))} | {change(b.get('p50_ms'), x.get('p50_ms'))} |")
        if a.fail_over and b.get("p50_ms") and x.get("p50_ms") and x["p50_ms"] > b["p50_ms"] * a.fail_over:
            worse.append(k)
    if worse:
        print(f"\nregressed past {a.fail_over}×: {', '.join(worse)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
