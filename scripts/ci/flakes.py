#!/usr/bin/env python3
"""Which tests flake on main: the rerun reports (scripts/ci/flake_report.py) of main's recent CI runs, added up.

A test that failed, was rerun and passed is a flake; one that failed its rerun too is counted as a failure. Reads the
`flakes` artifact of each run through the GitHub REST API, with the `gh` CLI for auth (GH_TOKEN in Actions).

    scripts/ci/flakes.py                       # the last 30 runs of ci.yml
    scripts/ci/flakes.py --runs 100 --json     # more history, machine-readable
    scripts/ci/flakes.py --files flakes/*.json # one run's reports, already downloaded (ci.yml's report job)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

Fetch = Callable[[str], bytes]


def gh(path: str) -> bytes:
    """GET a REST path with gh's auth. Artifact zips come back as bytes (gh follows the redirect)."""
    return subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                          check=True, capture_output=True).stdout


def reports(fetch: Fetch, repo: str, workflow: str, runs: int, branch: str = "main") -> list[dict[str, Any]]:
    """Each finished run's flake reports, newest first; a run without the artifact (expired, or cancelled before
    the report job) is skipped."""
    found: list[dict[str, Any]] = []
    page, seen = 1, 0
    while seen < runs:
        per_page = min(100, runs - seen)
        listing = json.loads(fetch(f"repos/{repo}/actions/workflows/{workflow}/runs"
                                   f"?branch={branch}&status=completed&per_page={per_page}&page={page}"))
        batch = listing.get("workflow_runs", [])
        if not batch:
            break
        for run in batch:
            arts = json.loads(fetch(f"repos/{repo}/actions/runs/{run['id']}/artifacts?name=flakes"))["artifacts"]
            live = [a for a in arts if not a.get("expired")]
            if not live:
                continue
            with zipfile.ZipFile(io.BytesIO(fetch(f"repos/{repo}/actions/artifacts/{live[0]['id']}/zip"))) as z:
                for name in z.namelist():
                    if name.endswith(".json"):
                        r = json.loads(z.read(name))
                        r.update(run_id=run["id"], sha=run["head_sha"], url=run["html_url"],
                                 created_at=run["created_at"])
                        found.append(r)
        seen += len(batch)
        page += 1
    return found


def summarize(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per test: in how many runs it flaked (failed, then passed on rerun) or failed both times; most flaky first."""
    tests: dict[str, dict[str, Any]] = {}
    for r in found:  # newest first
        for t in r.get("tests", []):
            s = tests.setdefault(t["nodeid"], {"nodeid": t["nodeid"], "flaked": 0, "failed": 0,
                                               "last_url": r.get("url"), "last_sha": r.get("sha"),
                                               "last_error": (t.get("error") or "")[-500:]})
            s["flaked" if t.get("outcome") == "passed" else "failed"] += 1
    return sorted(tests.values(), key=lambda s: (-s["flaked"], -s["failed"], s["nodeid"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Sean-Koval/hangar"))
    ap.add_argument("--workflow", default="ci.yml", help="the post-merge workflow's file name")
    ap.add_argument("--runs", type=int, default=30, help="how many recent runs to read")
    ap.add_argument("--files", nargs="*", help="read these report files instead of the API")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.files is not None:
        found = []
        for path in args.files:
            r = json.loads(Path(path).read_text())
            found.append(r | {"run_id": r.get("run_id"), "url": r.get("job") or path})
    else:
        found = reports(gh, args.repo, args.workflow, args.runs)
    summary = summarize(found)
    runs = len({r["run_id"] for r in found})
    if args.json:
        print(json.dumps({"runs_with_reports": runs, "tests": summary}, indent=1))
        return 0
    print(f"{runs} run(s) with rerun reports ({len(found)} report(s)); {len(summary)} test(s) needed a rerun")
    for s in summary:
        print(f"  flaked {s['flaked']:3d}  failed twice {s['failed']:3d}  {s['nodeid']}\n      last: {s['last_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
