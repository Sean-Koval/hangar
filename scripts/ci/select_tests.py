#!/usr/bin/env python3
"""Pick the tests a change can break, for the pre-merge CI job, and split them into balanced shards (docs/ci.md).

The map is static, built from imports with `ast` (nothing is imported or run). A changed hangar module selects the
test files that import it, and those that import a module importing it (one hop: hangar's modules import each other
in one big cycle, so the full transitive closure is the whole suite for any change). Test helper modules count as
the tests that import them. Core modules, which every test runs through conftest's fixtures or which most of hangar
imports, aren't hopped through; a change to one runs the whole suite, as does a change to conftest, test fixtures,
the dependencies or CI itself, an unknown path, a module no test reaches, or a large diff. Main runs the whole suite
after the merge, so this only has to catch most breakage, fast.

    scripts/ci/select_tests.py --base origin/main               # test files for this branch, one per line
    scripts/ci/select_tests.py --base origin/main --shard 2/4   # this shard's share of them
    scripts/ci/select_tests.py --files src/hangar/slack.py      # for given paths instead of a git diff
    scripts/ci/select_tests.py --base origin/main --e2e         # the Docker e2e suites to run, as a JSON list
    scripts/ci/select_tests.py --base origin/main --budget 600  # verify.sh's: capped to 600 s of CI time
    scripts/ci/select_tests.py --record-durations junit/*.xml > scripts/ci/durations.json   # from a full run

Why it chose what it did goes to stderr; stdout is only the answer.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DURATIONS = Path(__file__).with_name("durations.json")

# Core, beyond what tests/conftest.py imports (added automatically): the hubs whose one-hop selection was already
# over 60% of the suite's time when this was written. A change to one runs the whole suite.
CORE = {"hangar", "hangar.harnesses", "hangar.exchange", "hangar.events", "hangar.sessions", "hangar.isolation",
        "hangar.steer", "hangar.jobs", "hangar.mcp_server", "hangar.auth", "hangar.app"}

# Changed paths that select every test: shared fixtures, dependencies, and CI (the selection itself included).
FULL = ["tests/conftest.py", "tests/__init__.py", "tests/fixtures/*", "pyproject.toml", "uv.lock",
        ".github/workflows/*", "scripts/ci/*"]

# Non-Python paths, matched in order: the tests that read them, or a key of READS to find those by their source.
PATHS: list[tuple[str, list[str] | str]] = [
    # the front page's security posture and feature table (#151), and the editorial rule (tests/test_editorial.py)
    ("README.md", ["tests/test_front_page.py", "tests/test_editorial.py"]),
    ("docs/index.md", ["tests/test_front_page.py", "tests/test_editorial.py"]),
    ("docs/gke.md", ["tests/test_sweep_durability.py", "tests/test_editorial.py"]),
    ("docs/*", ["tests/test_editorial.py"]),  # the editorial rule: every page, the nav
    ("*.md", []),
    ("LICENSE", []),
    ("bench/*", []),
    ("Dockerfile", ["tests/test_image_supply_chain.py", "tests/test_apt_versions.py"]),  # and the build, on main
    (".dockerignore", []),
    (".gitignore", []),
    ("deploy/terraform/gke/outbox.tf", ["tests/test_outbox.py"]),  # --- outbox (#163): its write-once IAM
    ("deploy/terraform/*", []),  # the terraform job
    ("deploy/*", ["tests/test_k8s_network.py", "tests/test_sweep_durability.py", "tests/test_up_gke.py",
                  "tests/test_deploy_kustomize.py",  # --- company-deploy: deploy/kustomize and its deploy/k8s copies
                  "tests/test_egress.py",  # --- egress: reads the NetworkPolicies too
                  "tests/test_setup_cicd.py"]),  # --- cicd: deploy/gke/setup-cicd.sh
    ("scripts/acceptance/*", ["tests/test_acceptance_script.py", "tests/test_acceptance_team.py",  # --- acceptance
                              "tests/test_e2e_acceptance_docker.py", "tests/test_e2e_heavy_docker.py"]),  # --- heavy
    ("src/hangar/ui/*", ["tests/test_app.py", "tests/test_ui_airframe.py"]),  # the console on Airframe
    ("scripts/sync-airframe.sh", ["tests/test_ui_airframe.py"]),
    ("envs/*", "envs"),
    ("envd/*", "envd"),
]

LARGE_DIFF = 12  # changed hangar modules past which selection isn't worth its risk

# --- KAN-138: verify.sh's cap (--budget). A hangar job's check has minutes, not CI's 32 workers: past the budget it
# keeps the API and contract tests (for a hub change, or when CI would run everything), the tests a changed path
# names, then the tests importing a changed module directly, then the rest, cheapest first. CI keeps running it all.
CONTRACT = ["tests/test_app.py", "tests/test_mcp_contract.py", "tests/test_scope_parity.py",
            "tests/test_fleet_api.py", "tests/test_steer_api.py"]
CAPPED_PATHS = [("scripts/ci/*", ["tests/test_ci.py"]), (".github/workflows/*", ["tests/test_ci.py"])]
READS = {
    "envs": re.compile(r"""(parents\[1\]|ROOT)\s*/\s*["']envs["']"""),  # the repo's environments
    "envd": re.compile(r"""BINARIES|envd_dir|HANGAR_ENVD_DIR"""),  # the hangar-envd binary
}


@dataclass
class Selection:
    full: bool = False
    tests: set[str] = field(default_factory=set)  # repo-relative test files
    why: list[str] = field(default_factory=list)
    named: set[str] = field(default_factory=set)  # changed test files, tests a changed path names, a module's test_<it>
    direct: set[str] = field(default_factory=set)  # tests importing a changed module (or a changed helper) directly
    hub: bool = False  # a core module changed

    def everything(self, reason: str) -> None:
        self.full = True
        self.why.append(f"full suite: {reason}")


def module_name(path: str) -> str | None:
    """src/hangar/providers/docker.py -> hangar.providers.docker; tests/test_x.py -> tests.test_x."""
    p = Path(path)
    if p.suffix != ".py" or not p.parts or p.parts[0] not in ("src", "tests"):
        return None
    parts = list(p.with_suffix("").parts[1:] if p.parts[0] == "src" else p.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports(tree: ast.Module, name: str, package: bool) -> set[str]:
    """Every hangar or tests module the file imports, at module level or inside functions: `from a import b` counts
    as both a and a.b, since b may be a module."""
    pkg = name if package else name.rpartition(".")[0]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = pkg
                for _ in range(node.level - 1):
                    anchor = anchor.rpartition(".")[0]
                base = f"{anchor}.{base}" if base else anchor
            out.add(base)
            out.update(f"{base}.{a.name}" for a in node.names)
    return {m for m in out if m.split(".")[0] in ("hangar", "tests")}


def scans_sources(tree: ast.Module) -> bool:
    """Whether the file builds the path `... / "src" / "hangar"`: a test that reads every module's source (its ast)
    depends on all of them."""
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) and isinstance(n.left, ast.BinOp)
               and isinstance(n.right, ast.Constant) and n.right.value == "hangar"
               and isinstance(n.left.right, ast.Constant) and n.left.right.value == "src" for n in ast.walk(tree))


def opt_ins(tree: ast.Module) -> dict[str, str]:
    """Module-level markers that skip unless an env var opts in, e.g. `pytestmark = pytest.mark.skipif(
    os.environ.get("HANGAR_E2E") != "1", ...)` -> {"pytestmark": "HANGAR_E2E"}."""
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        # one mark, or a list of them (`[skipif(...), pytest.mark.needs("docker")]`, KAN-138)
        marks = node.value.elts if isinstance(node.value, ast.List) else [node.value]
        for mark in marks:
            if isinstance(mark, ast.Call) and ast.unparse(mark.func).endswith("skipif"):
                env = [c.value for c in ast.walk(mark) if isinstance(c, ast.Constant)
                       and isinstance(c.value, str) and c.value.startswith("HANGAR_")]
                calls = {ast.unparse(c.func) for c in ast.walk(mark) if isinstance(c, ast.Call)}
                if env and calls & {"os.environ.get", "os.getenv"}:
                    found.update((t.id, env[0]) for t in node.targets if isinstance(t, ast.Name))
    return found


class Graph:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.files: dict[str, str] = {}  # module -> repo-relative path
        self.text: dict[str, str] = {}
        self.imports: dict[str, set[str]] = {}
        self.opt_ins: dict[str, dict[str, str]] = {}
        self.scanners: set[str] = set()
        for p in [*sorted((root / "src").rglob("*.py")), *sorted((root / "tests").rglob("*.py"))]:
            rel = p.relative_to(root).as_posix()
            name = module_name(rel)
            if name is None:
                continue
            self.files[name], self.text[name] = rel, p.read_text()
            tree = ast.parse(self.text[name], rel)
            self.imports[name] = imports(tree, name, p.name == "__init__.py")
            self.opt_ins[name] = opt_ins(tree)
            if scans_sources(tree):
                self.scanners.add(rel)
        self.importers: dict[str, set[str]] = defaultdict(set)
        for name, deps in self.imports.items():
            for d in deps:
                self.importers[d].add(name)
        self.core = CORE | {m for m in self.imports.get("tests.conftest", ()) if m.startswith("hangar")}
        self.tests = {n: f for n, f in self.files.items() if re.fullmatch(r"tests/test_[^/]*\.py", f)}

    def dependents(self, module: str, hops: int = 1) -> set[str]:
        """The module, the hangar modules importing it up to `hops` away (not through core ones), and every test
        module importing any of those, through any chain of test helper modules."""
        found, frontier = {module}, {module}
        for _ in range(hops):
            frontier = {i for m in frontier for i in self.importers.get(m, ())
                        if i.startswith("hangar") and i not in self.core} - found
            found |= frontier
        todo = list(found)
        while todo:
            for importer in self.importers.get(todo.pop(), ()):
                if importer.startswith("tests") and importer not in found:
                    found.add(importer)
                    todo.append(importer)
        return found

    def test_files(self, modules: set[str]) -> set[str]:
        return {self.tests[m] for m in modules if m in self.tests}

    def reading(self, what: str) -> set[str]:
        return {f for n, f in self.tests.items() if READS[what].search(self.text[n])}

    def is_e2e(self, test_file: str) -> bool:
        """A Docker e2e suite: it has tests that run only with HANGAR_E2E=1 (kata's HANGAR_E2E_KATA isn't one: it
        needs a KVM host)."""
        return "HANGAR_E2E" in self.opt_ins.get(module_name(test_file) or "", {}).values()

    def is_unit(self, test_file: str) -> bool:
        """Has tests that run without an opt-in: the module as a whole isn't skipped by one."""
        name = module_name(test_file) or ""
        return name in self.text and "pytestmark" not in self.opt_ins[name]


def select(changed: list[str], graph: Graph) -> Selection:
    sel = Selection()
    src_modules = []
    for path in changed:
        if any(fnmatch.fnmatch(path, pat) for pat in FULL):
            sel.everything(f"{path} is shared by every test or is CI itself")
            sel.named |= {t for pat, ts in CAPPED_PATHS if fnmatch.fnmatch(path, pat) for t in ts}
            continue
        name = module_name(path)
        if name and name.startswith("hangar"):
            if name in graph.core:
                sel.everything(f"{path} is a core module (CORE, or imported by tests/conftest.py)")
                sel.hub = True
            src_modules.append(name)
            sel.named |= {f"tests/test_{name.rpartition('.')[2] or name}.py"}
            sel.direct |= graph.test_files(graph.dependents(name, hops=0))
            continue
        if name and name.startswith("tests"):
            hit = graph.test_files(graph.dependents(name))
            sel.tests |= hit
            sel.named.add(path)
            sel.direct |= hit
            sel.why.append(f"{path}: {len(hit)} test file(s) (it and the tests importing it)")
            continue
        for pattern, targets in PATHS:
            if fnmatch.fnmatch(path, pattern):
                hit = graph.reading(targets) if isinstance(targets, str) else set(targets)
                sel.tests |= hit
                sel.named |= hit
                sel.why.append(f"{path}: {len(hit)} test file(s) read it" if hit else f"{path}: no tests")
                break
        else:
            sel.everything(f"{path} isn't mapped to tests (add it to PATHS in {Path(__file__).name})")
    if len(src_modules) > LARGE_DIFF:
        sel.everything(f"{len(src_modules)} hangar modules changed (more than {LARGE_DIFF})")
    for name in src_modules:
        hit = graph.test_files(graph.dependents(name))
        if not hit and name not in graph.core:
            sel.everything(f"no test imports {name} or a module importing it")
        sel.tests |= hit
        sel.why.append(f"{graph.files.get(name, name)}: {len(hit)} test file(s) import it or a module that does")
    if src_modules:
        scanners = graph.scanners & set(graph.tests.values())
        sel.tests |= scanners
        if scanners:
            sel.why.append(f"hangar source changed: {len(scanners)} test file(s) scan every module")
    if sel.full:
        sel.tests = set(graph.tests.values())
    sel.tests = {t for t in sel.tests if (graph.root / t).is_file()}  # a deleted test file has nothing to run
    return sel


def changed_files(base: str) -> list[str]:
    """What this branch changes since it left `base`: commits, uncommitted edits and new files."""
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if line]

    merge_base = git("merge-base", base, "HEAD")[0]
    return sorted(set(git("diff", "--name-only", "--no-renames", merge_base))
                  | set(git("ls-files", "--others", "--exclude-standard")))


def shard(files: list[str], index: int, count: int, durations: dict[str, float]) -> list[str]:
    """Longest-first into the least loaded shard, by each file's recorded duration (unknown files: the median).
    Whole files, not single tests: their fixtures and helpers stay together, and xdist balances tests inside a
    shard. Deterministic, so every shard job computes the same split."""
    known = sorted(durations.values())
    default = known[len(known) // 2] if known else 1.0
    loads = [0.0] * count
    shards: list[list[str]] = [[] for _ in range(count)]
    for f in sorted(files, key=lambda f: (-durations.get(f, default), f)):
        i = loads.index(min(loads))
        loads[i] += durations.get(f, default)
        shards[i].append(f)
    return sorted(shards[index - 1])


def cap(sel: Selection, files: list[str], budget: float, durations: dict[str, float]) -> tuple[list[str], str]:
    """--- KAN-138: verify's selection within `budget` seconds of recorded (CI) time, and why. Under it, everything
    selected; over it, the API and contract tests when a hub changed or CI would run everything, and the tests a
    change names, always; then direct importers of a changed module, then the rest, each cheapest first."""
    known = sorted(durations.values())
    default = known[len(known) // 2] if known else 1.0
    cost = {f: durations.get(f, default) for f in files}
    if sum(cost.values()) <= budget:
        return sorted(files), ""
    keep = (sel.named | (set(CONTRACT) if sel.hub or sel.full else set())) & set(files)
    total = sum(cost[f] for f in keep)
    for tier in (sel.direct, set(files)):
        for f in sorted(tier & set(files) - keep, key=lambda f: (cost[f], f)):
            if total + cost[f] <= budget:
                keep.add(f)
                total += cost[f]
    return sorted(keep), (f"capped at {budget:.0f}s of CI time: {len(keep)} of {len(files)} test files, about "
                          f"{total:.0f}s; CI runs the rest (--full runs everything)")


def record_durations(junit_files: list[str]) -> dict[str, float]:
    """Seconds per test file from pytest's junit XML (`--junitxml`), summed over its tests."""
    total: dict[str, float] = defaultdict(float)
    for path in junit_files:
        for case in ET.parse(path).iter("testcase"):
            parts = (case.get("classname") or "").split(".")
            if len(parts) >= 2 and parts[0] == "tests":
                total[f"tests/{parts[1]}.py"] += float(case.get("time") or 0)
    return {f: round(t, 1) for f, t in sorted(total.items())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--base", help="git ref the branch is compared with, e.g. origin/main")
    src.add_argument("--files", nargs="*", help="changed paths, relative to the repo root")
    src.add_argument("--all", action="store_true", help="every test (main's full run), still sharded by --shard")
    src.add_argument("--record-durations", nargs="+", metavar="JUNIT_XML", help=f"print a new {DURATIONS.name}")
    ap.add_argument("--shard", help="I/N: only shard I (1-based) of N")
    ap.add_argument("--e2e", action="store_true", help="print the selected Docker e2e suites as a JSON list")
    ap.add_argument("--budget", type=float, metavar="SECONDS",
                    help="cap the selection at this much recorded CI time (verify.sh); CI never caps")
    args = ap.parse_args(argv)
    if args.budget is not None and (args.shard or args.all):
        ap.error("--budget is for verify.sh's selection, not with --shard or --all")

    if args.record_durations:
        print(json.dumps(record_durations(args.record_durations), indent=0))
        return 0
    graph = Graph()
    if args.all:
        sel = Selection(full=True, tests=set(graph.tests.values()), why=["full suite: --all"])
        changed = []
    else:
        changed = changed_files(args.base) if args.base else [f.removeprefix("./") for f in args.files]
        sel = select(changed, graph)
    print(f"{len(changed)} changed file(s); " + ("; ".join(sel.why) or "nothing to test"), file=sys.stderr)

    if args.e2e:
        print(json.dumps(sorted(t for t in sel.tests if graph.is_e2e(t))))
        return 0
    files = sorted(t for t in sel.tests if graph.is_unit(t))
    if args.budget is not None:
        files, why = cap(sel, files, args.budget, json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {})
        if why:
            print(why, file=sys.stderr)
    if args.shard:
        index, _, count = args.shard.partition("/")
        if not (index.isdigit() and count.isdigit() and 1 <= int(index) <= int(count)):
            ap.error(f"--shard {args.shard}: want I/N with 1 <= I <= N")
        durations = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
        files = shard(files, int(index), int(count), durations)
    print(f"{len(files)} of {len(graph.tests)} test files selected", file=sys.stderr)
    if files:  # --- KAN-138: nothing, not an empty line, which `mapfile` reads as one empty path (the whole suite)
        print("\n".join(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
