#!/usr/bin/env python3
"""verify.sh's baseline: which of a run's failing tests also fail on the base commit (docs/ci.md).

A test that fails on the branch counts only when it passes on the base. So the failing ids from the branch's junit
XML are run again, alone, in a scratch worktree of the base commit, and compared by test id:

    scripts/ci/verify_baseline.py --base-sha SHA --junit head.xml   # exit 0: every failure is pre-existing

The base's outcomes are cached per commit in the git directory (`hangar-verify/<sha>.json`), so the next verify on
the same base only runs ids it hasn't seen. A test that doesn't exist on the base, is skipped there or passes there
is new. hangar's job baseline (src/hangar/baseline.py) can't do this for this repo: on the base commit, verify.sh
selects nothing and passes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAILED, PASSED, SKIPPED, MISSING = "failed", "passed", "skipped", "missing"
_NOT_FOUND = re.compile(r"ERROR: (?:not found|file or directory not found): (\S+)")
MAX_RERUN = 400  # past this many failures the branch is broken, not the base: no comparison


def node_id(classname: str, name: str, root: Path) -> str:
    """pytest's junit names back to a node id: ("tests.test_x.TestY", "t[a]") -> tests/test_x.py::TestY::t[a]. A
    collection error has no classname and names the module: it becomes the file."""
    if not classname:
        return name.replace(".", "/") + ".py"
    parts = classname.split(".")
    for i in range(len(parts), 0, -1):
        path = "/".join(parts[:i]) + ".py"
        if (root / path).is_file():
            return "::".join([path, *parts[i:], name])
    return "::".join(["/".join(parts) + ".py", name])


def outcomes(junit: Path, root: Path) -> dict[str, str]:
    """Each test's outcome in a junit XML report; a test both failing and erroring (a teardown error) failed."""
    out: dict[str, str] = {}
    for case in ET.parse(junit).iter("testcase"):
        ident = node_id(case.get("classname") or "", case.get("name") or "", root)
        if case.find("failure") is not None or case.find("error") is not None:
            out[ident] = FAILED
        elif case.find("skipped") is not None:
            out.setdefault(ident, SKIPPED)
        else:
            out.setdefault(ident, PASSED)
    return out


def failing(junit: Path, root: Path | None = None) -> list[str]:
    return sorted(i for i, o in outcomes(junit, root or ROOT).items() if o == FAILED)


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd or ROOT, check=True, capture_output=True, text=True).stdout.strip()


def cache_file(sha: str) -> Path:
    return Path(git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "hangar-verify" / f"{sha}.json"


def load_cache(sha: str) -> dict[str, str]:
    try:
        return json.loads(cache_file(sha).read_text())
    except (OSError, ValueError):
        return {}


def save_cache(sha: str, seen: dict[str, str]) -> None:
    path = cache_file(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=0, sort_keys=True))
    tmp.replace(path)


def deps_changed(sha: str) -> bool:
    return bool(git("diff", "--name-only", sha, "--", "pyproject.toml", "uv.lock"))


def pytest_cmd(tree: Path, sha: str) -> tuple[list[str], dict[str, str]]:
    """This venv with the base's sources first on the path; or, when the dependencies differ, the base's own."""
    if deps_changed(sha):
        return ["uv", "run", "--frozen", "--directory", str(tree), "pytest"], dict(os.environ)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(tree / "src"), os.environ.get("PYTHONPATH")]))}
    return [sys.executable, "-m", "pytest"], env


def run_on_base(sha: str, ids: list[str], workers: str) -> dict[str, str]:
    """Run `ids` on a scratch worktree of `sha`; ids the base doesn't have are MISSING."""
    tree = Path(tempfile.mkdtemp(prefix="hangar-verify-base-"))
    shutil.rmtree(tree)
    git("worktree", "add", "--detach", "--force", str(tree), sha)
    try:
        envd = ROOT / "envd" / "dist"  # a built hangar-envd: its tests would only skip on the base without it
        if envd.is_dir() and not (tree / "envd" / "dist").exists():
            (tree / "envd" / "dist").symlink_to(envd)
        seen: dict[str, str] = {}
        todo = [i for i in ids if (tree / i.split("::")[0]).is_file()]
        seen.update((i, MISSING) for i in ids if i not in todo)
        cmd, env = pytest_cmd(tree, sha)
        junit = tree / ".verify-base.xml"
        while todo:
            p = subprocess.run([*cmd, "-q", "-p", "no:cacheprovider", "-n", workers, "--junitxml", str(junit), *todo],
                               cwd=tree, env=env, capture_output=True, text=True)
            gone = set(_NOT_FOUND.findall(p.stdout + p.stderr))
            if p.returncode == 4 and gone & set(todo):  # an id the base doesn't have stops the whole run
                seen.update((i, MISSING) for i in gone)
                todo = [i for i in todo if i not in gone]
                continue
            got = outcomes(junit, tree) if junit.is_file() else {}
            seen.update((i, got.get(i, MISSING)) for i in todo)
            break
        return seen
    finally:
        git("worktree", "remove", "--force", str(tree))


def compare(failures: list[str], base: dict[str, str]) -> tuple[list[str], list[str]]:
    """(pre-existing, new): a failure is pre-existing only when it failed on the base too."""
    old = [f for f in failures if base.get(f) == FAILED]
    return old, [f for f in failures if base.get(f) != FAILED]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-sha", required=True, help="the commit the branch left its base at (the merge base)")
    ap.add_argument("--junit", required=True, type=Path, help="the branch's junit XML")
    ap.add_argument("--workers", default="4")
    args = ap.parse_args(argv)

    failures = failing(args.junit)
    if not failures:
        print("verify: pytest failed but its report names no failing test: nothing to compare", file=sys.stderr)
        return 1
    if len(failures) > MAX_RERUN:
        print(f"verify: {len(failures)} failures, too many to compare with the base", file=sys.stderr)
        return 1
    sha = git("rev-parse", args.base_sha)
    seen = load_cache(sha)
    todo = [f for f in failures if f not in seen]
    if todo:
        print(f"verify: running {len(todo)} failing test(s) on the base {sha[:12]}", file=sys.stderr)
        seen.update(run_on_base(sha, todo, args.workers))
        save_cache(sha, seen)
    old, new = compare(failures, seen)
    if old:
        print(f"verify: {len(old)} failure(s) also fail on the base {sha[:12]} (pre-existing, not counted):")
        print("".join(f"  pre-existing: {f}\n" for f in old), end="")
    if new:
        print(f"verify: {len(new)} new failure(s), passing or absent on the base {sha[:12]}:")
        print("".join(f"  new: {f} (base: {seen.get(f, MISSING)})\n" for f in new), end="")
        return 1
    print("verify: no new failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
