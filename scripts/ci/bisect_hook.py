#!/usr/bin/env python3
"""After a red post-merge run on main: hand the bisect and repair to a hangar job, or open an issue (docs/ci.md).

The failing tests come from the run's junit XML (final outcomes: a test that passed its rerun isn't failing), the
range from the last green run of the same workflow on main. With HANGAR_URL and HANGAR_TOKEN set, it submits a job
(POST /api/jobs, docs/jobs.md) whose agent runs `git bisect run` over that range with those tests and opens a fix or
revert PR. Without them, or when hangar can't be reached, it opens a GitHub issue with the same facts, or comments on
the open one.

    scripts/ci/bisect_hook.py --junit junit/ --dry-run   # what it would send, without sending it
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ISSUE_TITLE = "CI: main is red"
MAX_IDS = 40  # node ids put on one pytest command line; past that, the whole shard's files


def failing_tests(junit_dir: Path) -> list[str]:
    """pytest node ids of the tests that ended failed or errored, from every junit XML file under junit_dir."""
    ids = set()
    for path in sorted(junit_dir.rglob("*.xml")):
        for case in ET.parse(path).iter("testcase"):
            if case.find("failure") is None and case.find("error") is None:
                continue
            parts = (case.get("classname") or "").split(".")  # tests.test_x[.Class]
            if len(parts) >= 2 and parts[0] == "tests":
                ids.add("::".join([f"tests/{parts[1]}.py", *parts[2:], case.get("name") or ""]))
            elif not case.get("classname") and (case.get("name") or "").startswith("tests."):  # a collection error
                ids.add(case.get("name", "").replace(".", "/") + ".py")
    return sorted(ids)


def gh_json(path: str, *args: str) -> Any:
    out = subprocess.run(["gh", "api", path, *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else None


def last_green(repo: str, workflow: str, before_run: int) -> str | None:
    """head sha of the newest successful run of the workflow on main before this one."""
    runs = gh_json(f"repos/{repo}/actions/workflows/{workflow}/runs?branch=main&status=success&per_page=20")
    for run in runs.get("workflow_runs", []):
        if run["id"] != before_run and run["event"] in ("push", "workflow_dispatch"):
            return run["head_sha"]
    return None


def failed_jobs(repo: str, run_id: int) -> list[str]:
    jobs = gh_json(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    return sorted(j["name"] for j in jobs.get("jobs", []) if j.get("conclusion") == "failure")


def instructions(repo: str, bad: str, good: str | None, tests: list[str], jobs: list[str], run_url: str) -> str:
    """What the agent is asked to do. Only facts from the run and fixed text: no commit messages or test output, which
    anyone who can push could have written."""
    picked = tests[:MAX_IDS]
    pytest_cmd = ("uv run pytest -x -q -p no:cacheprovider " + " ".join(shlex.quote(t) for t in picked)) if picked \
        else "uv run pytest -x -q -p no:cacheprovider"
    lines = [f"Main's post-merge CI is red at {bad} ({run_url}).", ""]
    if jobs:
        lines += ["Failed jobs: " + ", ".join(jobs) + "."]
    if tests:
        lines += ["Failing tests:", *(f"- {t}" for t in tests[:100])]
        if len(tests) > 100:
            lines.append(f"- ... and {len(tests) - 100} more (see the run)")
    lines.append("")
    if good:
        lines += [
            f"1. Find the culprit between the last green commit {good} and {bad}:",
            "   git fetch origin main",
            f"   git bisect start {bad} {good}",
            "   git bisect run sh -c " + shlex.quote(f"uv sync --all-extras --frozen -q && {pytest_cmd}"),
            "   git bisect reset",
            "   If the failure doesn't reproduce at the bad commit, it is a flake: say so and stop.",
        ]
    else:
        lines.append(f"1. There is no green run to bisect from: reproduce at {bad} with `{pytest_cmd}`.")
    lines += [
        "2. If the cause is clear and the fix small, fix it with a regression test. Otherwise revert the culprit",
        "   commit (git revert --no-edit <sha>) and explain in the PR what it broke.",
        f"3. Open a PR against main. Name the culprit commit and link {run_url}.",
    ]
    return "\n".join(lines)


def submit_job(url: str, token: str, repo: str, text: str, tests: list[str], run_id: int) -> str:
    body = {
        "instructions": text,
        "repo": f"https://github.com/{repo}",
        "base_branch": "main",
        "deliver": "pr",
        "idempotency_key": f"ci-bisect-{run_id}",  # a rerun of the failed jobs doesn't start a second bisect
    }
    if tests:
        body["verify"] = "uv run pytest -q -p no:cacheprovider " + " ".join(shlex.quote(t) for t in tests[:MAX_IDS])
    if os.environ.get("HANGAR_ENVIRONMENT"):
        body["environment"] = os.environ["HANGAR_ENVIRONMENT"]
    req = urllib.request.Request(url.rstrip("/") + "/api/jobs", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        job = json.loads(r.read())
    return str(job.get("id"))


def open_issue(repo: str, text: str, bad: str) -> str:
    """One open issue at a time: a later red run comments on it."""
    issues = gh_json(f"repos/{repo}/issues?state=open&per_page=100") or []
    for issue in issues:
        if issue["title"].startswith(ISSUE_TITLE) and "pull_request" not in issue:
            gh_json(f"repos/{repo}/issues/{issue['number']}/comments", "-f", f"body={text}")
            return issue["html_url"]
    created = gh_json(f"repos/{repo}/issues", "-f", f"title={ISSUE_TITLE} at {bad[:12]}", "-f", f"body={text}")
    return created["html_url"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--junit", type=Path, required=True, help="directory with the run's junit XML files")
    ap.add_argument("--workflow", default="ci.yml")
    ap.add_argument("--dry-run", action="store_true", help="print the job or issue instead of creating it")
    args = ap.parse_args(argv)
    env = os.environ
    repo, bad, run_id = env["GITHUB_REPOSITORY"], env["GITHUB_SHA"], int(env["GITHUB_RUN_ID"])
    run_url = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{run_id}"

    tests = failing_tests(args.junit) if args.junit.is_dir() else []
    jobs = failed_jobs(repo, run_id)
    good = last_green(repo, args.workflow, run_id)
    text = instructions(repo, bad, good, tests, jobs, run_url)
    hangar_url, token = env.get("HANGAR_URL", ""), env.get("HANGAR_TOKEN", "")
    print(f"{len(tests)} failing test(s), failed jobs {jobs or 'none'}, last green {good or 'none'}")
    if args.dry_run:
        print(("would submit a hangar job:\n" if hangar_url and token else "would open an issue:\n") + text)
        return 0
    if hangar_url and token:
        try:
            print(f"submitted hangar job {submit_job(hangar_url, token, repo, text, tests, run_id)}")
            return 0
        except (urllib.error.URLError, TimeoutError, ValueError) as e:  # no secret in these messages
            print(f"could not submit the hangar job ({e}); opening an issue instead", file=sys.stderr)
    print(f"issue: {open_issue(repo, text, bad)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
