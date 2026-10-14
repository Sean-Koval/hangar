#!/usr/bin/env python3
"""After a red post-merge run on main: hand the bisect and repair to a hangar job, or open an issue (docs/ci.md).

The failing tests come from the run's junit XML (final outcomes: a test that passed its rerun isn't failing), the
failed jobs from the REST API, and the range from the last green run of the same workflow on main. With HANGAR_URL
and HANGAR_TOKEN set, it submits a job (POST /api/jobs, docs/jobs.md) whose agent runs `git bisect run` over that
range with a check that reproduces the failure, then opens a fix or revert PR. Without them, or when hangar can't be
reached, it opens a GitHub issue with the same instructions, or comments on the open one.

    scripts/ci/bisect_hook.py --junit results/ --dry-run   # what it would send, without sending it
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
MAX_IDS = 40  # failing tests on the check's command line: one that reproduces is all a bisect needs
# How to repeat a failed job that isn't a pytest run, at any commit: as ci.yml runs it.
JOB_CHECKS = {
    "lint": "uv run --only-dev ruff check src tests scripts/ci",
    "envd": "ARCHES=x86_64 envd/build.sh",
    "rust": "(cd envd && cargo clippy --locked --all-targets -- -D warnings && cargo test --locked)",
    "image": "docker build -q .",
    "terraform": "for d in deploy/terraform/gke deploy/terraform/single-vm; do terraform -chdir=$d init "
                 "-backend=false -input=false >/dev/null && terraform -chdir=$d validate || exit 1; done "
                 "&& terraform fmt -check -recursive deploy/terraform",
}


def failing_tests(junit_dir: Path) -> tuple[list[str], list[str]]:
    """pytest node ids of the tests that ended failed or errored: (unit, Docker e2e), told apart by the junit file
    (main's unit shards write junit-unit-<n>.xml, each e2e job junit-<suite>.xml)."""
    unit, e2e = set(), set()
    for path in sorted(junit_dir.rglob("*.xml")):
        ids = unit if path.name.startswith("junit-unit-") else e2e
        for case in ET.parse(path).iter("testcase"):
            if case.find("failure") is None and case.find("error") is None:
                continue
            parts = (case.get("classname") or "").split(".")  # tests.test_x[.Class]
            if len(parts) >= 2 and parts[0] == "tests":
                ids.add("::".join([f"tests/{parts[1]}.py", *parts[2:], case.get("name") or ""]))
            elif not case.get("classname") and (case.get("name") or "").startswith("tests."):  # a collection error
                ids.add(case.get("name", "").replace(".", "/") + ".py")
    return sorted(unit), sorted(e2e)


def check_command(unit: list[str], e2e: list[str], jobs: list[str]) -> str | None:
    """One shell command that fails while main is broken the way this run was, or None if none is known."""
    def pytest(ids: list[str]) -> str:
        return "uv run pytest -x -q -p no:cacheprovider " + " ".join(shlex.quote(t) for t in ids[:MAX_IDS])

    parts = [JOB_CHECKS[j] for j in jobs if j in JOB_CHECKS]
    if unit:
        parts.append(pytest(unit))
    if e2e:
        parts.append("HANGAR_E2E=1 " + pytest(e2e))
    if unit or e2e or "lint" in jobs:
        parts.insert(0, "uv sync --all-extras --frozen -q")
    return " && ".join(parts) or None


def gh_json(path: str, *args: str) -> Any:
    out = subprocess.run(["gh", "api", path, *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else None


def last_green(repo: str, workflow: str, before_run: int) -> str | None:
    """head sha of the newest successful run of the workflow on main older than this one (run ids only grow: a
    rerun of an old red run must not bisect against a newer green commit)."""
    runs = gh_json(f"repos/{repo}/actions/workflows/{workflow}/runs?branch=main&status=success&per_page=30")
    for run in runs.get("workflow_runs", []):
        if run["id"] < before_run and run["event"] in ("push", "workflow_dispatch"):
            return run["head_sha"]
    return None


def failed_jobs(repo: str, run_id: int) -> list[str]:
    jobs = gh_json(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    return sorted(j["name"] for j in jobs.get("jobs", []) if j.get("conclusion") == "failure")


def instructions(bad: str, good: str | None, tests: list[str], jobs: list[str], check: str | None,
                 run_url: str) -> str:
    """What the agent is asked to do. Only facts from the run and fixed text: no commit messages or test output, which
    anyone who can push could have written."""
    lines = [f"Main's post-merge CI is red at {bad} ({run_url}).", ""]
    if jobs:
        lines.append("Failed jobs: " + ", ".join(jobs) + ".")
    if tests:
        lines += ["Failing tests:", *(f"- {t}" for t in tests[:100])]
        if len(tests) > 100:
            lines.append(f"- ... and {len(tests) - 100} more (see the run)")
    lines.append("")
    if check is None:
        lines.append(f"1. Reproduce the failed jobs at {bad} as the run's logs show them.")
    elif good:
        lines += [
            f"1. Find the culprit between the last green commit {good} and {bad}:",
            "   git fetch origin main",
            f"   git bisect start {bad} {good}",
            "   git bisect run sh -c " + shlex.quote(check),
            "   git bisect reset",
            "   If the check passes at the bad commit, the failure was a flake: say so, and stop.",
        ]
    else:
        lines.append(f"1. There is no green run to bisect from: reproduce at {bad} with `{check}`.")
    lines += [
        "2. If the cause is clear and the fix small, fix it with a regression test. Otherwise revert the culprit",
        "   commit (git revert --no-edit <sha>) and explain in the PR what it broke.",
        f"3. Open a PR against main. Name the culprit commit and link {run_url}.",
    ]
    return "\n".join(lines)


def submit_job(url: str, token: str, repo: str, text: str, check: str | None, run_id: int) -> str:
    body: dict[str, Any] = {
        "instructions": text,
        "repo": f"https://github.com/{repo}",
        "base_branch": "main",
        "deliver": "pr",
        "idempotency_key": f"ci-bisect-{run_id}",  # a rerun of the failed jobs doesn't start a second bisect
    }
    if check:
        body["verify"] = check
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

    unit, e2e = failing_tests(args.junit) if args.junit.is_dir() else ([], [])
    jobs = failed_jobs(repo, run_id)
    good = last_green(repo, args.workflow, run_id)
    check = check_command(unit, e2e, jobs)
    text = instructions(bad, good, unit + e2e, jobs, check, run_url)
    hangar_url, token = env.get("HANGAR_URL", ""), env.get("HANGAR_TOKEN", "")
    print(f"{len(unit) + len(e2e)} failing test(s), failed jobs {jobs or 'none'}, last green {good or 'none'}")
    if args.dry_run:
        print(("would submit a hangar job:\n" if hangar_url and token else "would open an issue:\n") + text)
        return 0
    if hangar_url and token:
        try:
            print(f"submitted hangar job {submit_job(hangar_url, token, repo, text, check, run_id)}")
            return 0
        except (urllib.error.URLError, TimeoutError, ValueError) as e:  # no secret in these messages
            print(f"could not submit the hangar job ({e}); opening an issue instead", file=sys.stderr)
    print(f"issue: {open_issue(repo, text, bad)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
