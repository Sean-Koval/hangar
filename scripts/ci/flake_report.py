"""pytest plugin: the tests pytest-rerunfailures had to rerun, and how each ended, as JSON (docs/ci.md).

Main's CI loads it next to `--reruns 1`; scripts/ci/flakes.py adds the reports up across runs. Under xdist only the
controller writes the file: it sees every worker's reports.

    PYTHONPATH=scripts/ci pytest -p flake_report --reruns 1 --flake-report flakes.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--flake-report", metavar="PATH", help="write the tests that were rerun, and their outcome, here")


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("flake_report")
    if path and not hasattr(config, "workerinput"):  # xdist workers report to the controller
        config.pluginmanager.register(Recorder(Path(path)), "flake-recorder")


class Recorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tests: dict[str, dict[str, Any]] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.outcome == "rerun":
            t = self.tests.setdefault(report.nodeid, {"nodeid": report.nodeid, "reruns": 0, "outcome": None,
                                                      "when": report.when, "error": ""})
            t["reruns"] += 1
            t["error"] = t["error"] or str(report.longrepr)[-4000:]
            return
        t = self.tests.get(report.nodeid)
        if t is None:
            return
        if report.failed:
            t["outcome"] = "failed"  # failed again: a real failure, or a flake that lost twice
        elif report.when == "call" and t["outcome"] is None:
            t["outcome"] = report.outcome

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        for t in self.tests.values():
            t["outcome"] = t["outcome"] or "passed"
        env = os.environ
        self.path.write_text(json.dumps({
            "sha": env.get("GITHUB_SHA"), "run_id": env.get("GITHUB_RUN_ID"), "job": env.get("FLAKE_JOB"),
            "tests": sorted(self.tests.values(), key=lambda t: t["nodeid"]),
        }, indent=1) + "\n")
