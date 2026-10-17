"""The turn driver's per-edit tree hash (#165; docs/steering.md#trees, docs/api/traces.md).

    uv run python scripts/bench/tree_hash.py [--edits 50] [--repo PATH] [--out bench/results/<date>-tree-hash.json]

The bench repo is a local clone of this checkout (hangar's own source, about six hundred files), or `--repo`. For
each edit, one file is changed (alternately an existing file rewritten and a new file added), then `driver.Trees`
hashes the working tree as it does after an edit tool's result:
  tree_first          the turn's start: the scratch git directory made, the repo's index copied, `add -A`
  tree_per_edit       each edit's hash (what the driver adds to a tool call; the target is p50 < 50 ms)
  tree_unchanged      a hash after a read-only tool (nothing changed: the driver writes no line)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from _common import ROOT, Recorder, emit

from hangar import driver


class _Quiet:
    def __init__(self):
        self.lines: list[dict] = []

    def event(self, kind: str, **data: object) -> None:
        self.lines.append(data)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edits", type=int, default=50)
    ap.add_argument("--repo", help="a git checkout to use instead of a clone of this one")
    ap.add_argument("--out")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="tree-bench-"))
    try:
        repo = work / "repo"
        subprocess.run(["git", "clone", "-q", "--local", "--no-hardlinks", str(args.repo or ROOT), str(repo)],
                       check=True)
        files = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True).stdout.split()
        rec = Recorder("tree_hash")
        rec.note("repo_files", len(files))
        out = _Quiet()
        t = driver.Trees(out, str(repo), str(work / "ctl" / "tree"))
        with rec.time("tree_first"):
            sha, _ = t.hash()
        assert sha, "the bench repo isn't a git checkout"
        py = [f for f in files if f.endswith(".py")][: max(1, args.edits)]
        for i in range(args.edits):
            if i % 2:
                (repo / f"bench_new_{i}.py").write_text(f"x = {i}\n")
            else:
                p = repo / py[i % len(py)]
                p.write_text(p.read_text() + f"\n# edit {i}\n")
            with rec.time("tree_per_edit"):
                t.point(tool_use_id=f"t{i}")
            with rec.time("tree_unchanged"):
                t.point(tool_use_id=f"r{i}")
        rec.note("lines", len(out.lines))
        rec.note("skipped", t.off)
        emit(rec.result(edits=args.edits), args.out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
