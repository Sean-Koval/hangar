"""The analysis boundary (#147, spec 0014 §1): hangar emits run data and makes runtime decisions; it computes no
metric across trials. Statistics libraries and estimator-shaped functions belong to the sister tool (flightdeck).

The guard reads every module's source with `ast` (nothing is imported). Counting for a runtime decision (a queue
length, `awaiting_capacity`, a flaky list) is allowed: the guard targets statistics libraries and estimator names,
not counting."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "hangar"
SPEC = "docs/specs/0014-sister-tool-interfaces.md §1 (What stays in hangar and why)"

# Libraries for analysis over runs. A hangar module importing one is computing something flightdeck should.
BANNED_IMPORTS = {"scipy", "numpy", "pandas", "statistics", "duckdb", "pyarrow"}

# Function names shaped like estimators: confidence intervals, pass@k, effect sizes, resampling.
BANNED_NAMES = re.compile(r"bootstrap|wilson|pass_at|pass_hat|effect_size|confidence", re.IGNORECASE)

# module (relative to src/hangar, posix) -> library: allowed, with the reason. Keep each entry justified.
ALLOWED_IMPORTS: dict[str, set[str]] = {
    # Placeholder (part B, #153): if the outbox's GCS -> Parquet compactor ends up living in hangar rather than in
    # flightdeck (an open coordinator decision; today it is flightdeck#5), its module may import pyarrow to write
    # Parquet. Add it here as e.g. "outbox_compact.py": {"pyarrow"}, and nothing else.
    # KAN-140: the Fleet tile's `stage_estimate` is a runtime display guess for one live turn (how far its current
    # stage probably is), from the median of recent stage and turn durations held in memory; a median, no metric
    # reported across runs.
    "livestate.py": {"statistics"},
    "fleet_api.py": {"statistics"},
}

# "module:function" -> reason. Names that match BANNED_NAMES but aren't estimators.
ALLOWED_NAMES: dict[str, str] = {
    "auth.py:ensure_bootstrap": "the first admin's bootstrap login token, not a statistical bootstrap",
}


def _modules() -> list[tuple[str, ast.Module]]:
    return [(p.relative_to(SRC).as_posix(), ast.parse(p.read_text(), str(p))) for p in sorted(SRC.rglob("*.py"))]


def _imported_roots(tree: ast.Module) -> set[str]:
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_statistics_libraries():
    found = []
    for rel, tree in _modules():
        for lib in sorted(_imported_roots(tree) & BANNED_IMPORTS):
            if lib not in ALLOWED_IMPORTS.get(rel, set()):
                found.append(f"src/hangar/{rel} imports {lib}")
    assert not found, (
        "hangar computes no metric across runs; analysis belongs to flightdeck, which reads the outbox and traces. "
        f"See {SPEC}. Found: {found}. If this is a runtime decision, not analysis, add a justified ALLOWED_IMPORTS "
        "entry in tests/test_boundary.py and a row to the spec's table.")


def test_no_estimator_functions():
    found = []
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and BANNED_NAMES.search(node.name):
                if f"{rel}:{node.name}" not in ALLOWED_NAMES:
                    found.append(f"src/hangar/{rel}:{node.lineno} {node.name}")
    assert not found, (
        "functions named like estimators (bootstrap, Wilson intervals, pass@k, effect sizes, confidence) belong to "
        f"flightdeck, not hangar. See {SPEC}. Found: {found}. If the name means something else, add it to "
        "ALLOWED_NAMES in tests/test_boundary.py with the reason.")


def test_the_guard_catches_what_it_should():
    """The guard itself: a module importing numpy and defining `wilson_interval` is caught; counting isn't."""
    bad = ast.parse("import numpy as np\nfrom scipy.stats import norm\ndef wilson_interval(k, n): ...\n"
                    "async def pass_at_k(rows): ...\n")
    ok = ast.parse("import collections\ndef queued_count(jobs): return len(jobs)\n")
    assert _imported_roots(bad) & BANNED_IMPORTS == {"numpy", "scipy"}
    names = [n.name for n in ast.walk(bad) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert all(BANNED_NAMES.search(n) for n in names)
    assert not _imported_roots(ok) & BANNED_IMPORTS
    assert not any(BANNED_NAMES.search(n.name) for n in ast.walk(ok) if isinstance(n, ast.FunctionDef))


def test_allowlists_point_at_real_code():
    """A stale allowlist entry would hide a future violation under an old excuse."""
    names = {f"{rel}:{n.name}" for rel, tree in _modules() for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert set(ALLOWED_NAMES) <= names, set(ALLOWED_NAMES) - names
    assert all((SRC / rel).exists() for rel in ALLOWED_IMPORTS)
