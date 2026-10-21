"""CI's own scripts (docs/ci.md): test selection and sharding, the rerun report, the flake summary and the bisect hook,
plus the test isolation xdist relies on (per-process scratch roots, per-worker ports)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import pytest

from tests import conftest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import bisect_hook as ci_bisect  # noqa: E402
import flakes  # noqa: E402
import select_tests as st  # noqa: E402


# ====================================================================== selection
@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A small hangar: `core` is what conftest imports; a <- b <- c is an import chain; lone has no tests."""
    files = {
        "src/hangar/__init__.py": "",
        "src/hangar/core.py": "",
        "src/hangar/a.py": "from hangar import core\n",
        "src/hangar/b.py": "from . import a\n",
        "src/hangar/c.py": "def f():\n    from hangar.b import x\n",
        "src/hangar/lone.py": "",
        "src/hangar/ui/app.js": "",
        "tests/__init__.py": "",
        "tests/conftest.py": "from hangar.core import thing\n",
        "tests/test_a.py": "import hangar.a\n",
        "tests/test_b.py": "from hangar import b\n",
        "tests/test_c.py": "from hangar.c import f\n",
        "tests/test_helper_user.py": "from tests.test_b import fixture\n",
        "tests/test_core.py": "from hangar import core\n",
        "tests/test_scan.py": 'SRC = ROOT / "src" / "hangar"\n',
        "tests/test_e2e_thing_docker.py": 'import os\nimport pytest\nimport hangar.a\n'
                                          'pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", '
                                          'reason="")\n',
    }
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    monkeypatch.setattr(st, "CORE", {"hangar"})
    return st.Graph(tmp_path)


def picked(graph, *changed):
    sel = st.select(list(changed), graph)
    return sel.full, sorted(Path(t).stem for t in sel.tests)


def test_a_module_selects_its_tests_and_those_one_import_away(repo):
    # b imports a; c imports b, two hops from a: not selected. test_scan reads every module's source.
    assert picked(repo, "src/hangar/a.py") == (False, ["test_a", "test_b", "test_e2e_thing_docker",
                                                       "test_helper_user", "test_scan"])
    assert picked(repo, "src/hangar/c.py") == (False, ["test_c", "test_scan"])  # a function-level import counts


def test_test_helpers_select_the_tests_importing_them(repo):
    assert picked(repo, "tests/test_b.py") == (False, ["test_b", "test_helper_user"])


@pytest.mark.parametrize("path", ["src/hangar/core.py", "tests/conftest.py", "uv.lock", ".github/workflows/x.yml",
                                  "scripts/ci/select_tests.py", "Makefile", "src/hangar/lone.py"])
def test_what_runs_everything(repo, path):
    full, tests = picked(repo, path)
    assert full and len(tests) == 7, path


def test_paths_without_tests_and_large_diffs(repo, monkeypatch):
    assert picked(repo, "docs/x.md", "README.md", "deploy/terraform/gke/main.tf") == (False, [])
    assert picked(repo, "src/hangar/ui/app.js") == (False, [])  # tests/test_app.py isn't in this repo
    monkeypatch.setattr(st, "LARGE_DIFF", 1)
    assert picked(repo, "src/hangar/a.py", "src/hangar/b.py")[0]


def test_unit_and_e2e_files(repo):
    assert repo.is_e2e("tests/test_e2e_thing_docker.py") and not repo.is_unit("tests/test_e2e_thing_docker.py")
    assert repo.is_unit("tests/test_a.py") and not repo.is_e2e("tests/test_a.py")


def test_the_real_suite_shards_into_disjoint_complete_sets():
    graph = st.Graph()
    units = sorted(t for t in graph.tests.values() if graph.is_unit(t))
    durations = json.loads(st.DURATIONS.read_text())
    shards = [st.shard(units, i, 4, durations) for i in range(1, 5)]
    assert sorted(f for s in shards for f in s) == units
    loads = [sum(durations.get(f, 0) for f in s) for s in shards]
    assert max(loads) - min(loads) <= max(durations.values())  # longest-first is within one file of even
    assert shards == [st.shard(list(reversed(units)), i, 4, durations) for i in range(1, 5)]  # order-independent


def test_the_real_e2e_suites():
    graph = st.Graph()
    e2e = {t for t in graph.tests.values() if graph.is_e2e(t)}
    assert {str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("test_e2e_*_docker.py")} <= e2e
    assert "tests/test_e2e_kata.py" not in e2e and "tests/test_acceptance_script.py" in e2e
    # this file's fixture repo spells out an opt-in marker and a source scan in strings: neither counts
    assert "tests/test_ci.py" not in e2e | graph.scanners and graph.is_unit("tests/test_ci.py")


def test_every_hangar_module_maps_to_tests_or_the_full_suite():
    graph = st.Graph()
    for name, path in graph.files.items():
        if name.startswith("hangar"):
            sel = st.select([path], graph)
            assert sel.full or sel.tests, path


def test_cli_with_files_and_shards(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/ci/select_tests.py"), *args],
                              capture_output=True, text=True, check=True)
    r = run("--files", "src/hangar/providers/e2b.py")
    assert "tests/test_e2b_provider.py" in r.stdout.split() and "test file(s)" in r.stderr
    assert json.loads(run("--files", "docs/ci.md", "--e2e").stdout) == []
    assert run("--files", "docs/ci.md").stdout.strip() == "tests/test_editorial.py"  # the editorial rule


def test_durations_from_junit(tmp_path):
    (tmp_path / "j.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_a" name="t1" time="1.25"/>'
        '<testcase classname="tests.test_a.TestX" name="t2" time="2"/>'
        '<testcase classname="tests.test_b" name="t[1]" time="0.5"/></testsuite></testsuites>')
    assert st.record_durations([str(tmp_path / "j.xml")]) == {"tests/test_a.py": 3.2, "tests/test_b.py": 0.5}


# ====================================================================== rerun report and flake summary
FLAKY = """
import os, pathlib
def test_flaky(tmp_path_factory):
    marker = pathlib.Path(os.environ["MARKER"])
    if not marker.exists():
        marker.write_text("x")
        assert False, "first try"
def test_broken():
    assert 1 == 2
def test_fine():
    pass
"""


@pytest.mark.parametrize("workers", [[], ["-n", "2"]])
def test_rerun_report(tmp_path, workers):
    (tmp_path / "test_x.py").write_text(FLAKY)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts" / "ci"), "MARKER": str(tmp_path / "marker"),
           "GITHUB_SHA": "abc", "FLAKE_JOB": "unit-1"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "flake_report",
                        "--reruns", "1", "--flake-report", str(tmp_path / "out.json"), *workers, "test_x.py"],
                       cwd=tmp_path, env=env, capture_output=True, text=True)
    assert r.returncode == 1, r.stdout + r.stderr  # test_broken
    report = json.loads((tmp_path / "out.json").read_text())
    assert report["sha"] == "abc" and report["job"] == "unit-1"
    got = {t["nodeid"]: (t["reruns"], t["outcome"]) for t in report["tests"]}
    assert got == {"test_x.py::test_flaky": (1, "passed"), "test_x.py::test_broken": (1, "failed")}
    assert "first try" in next(t["error"] for t in report["tests"] if t["nodeid"].endswith("flaky"))


def _zip(report: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("flakes-unit-1.json", json.dumps(report))
    return buf.getvalue()


def test_flakes_across_runs():
    runs = [{"id": 2, "head_sha": "s2", "html_url": "u2", "created_at": "t2"},
            {"id": 1, "head_sha": "s1", "html_url": "u1", "created_at": "t1"},
            {"id": 0, "head_sha": "s0", "html_url": "u0", "created_at": "t0"}]
    reps = {2: {"tests": [{"nodeid": "t::a", "outcome": "passed", "error": "boom"}]},
            1: {"tests": [{"nodeid": "t::a", "outcome": "passed"}, {"nodeid": "t::b", "outcome": "failed"}]}}
    asked = []

    def fetch(path: str) -> bytes:
        asked.append(path)
        if "/workflows/" in path:
            return json.dumps({"workflow_runs": runs if "page=1" in path else []}).encode()
        if path.endswith("artifacts?name=flakes"):
            run = int(path.split("/runs/")[1].split("/")[0])
            arts = [{"id": 100 + run, "expired": False}] if run in reps else [{"id": 9, "expired": True}]
            return json.dumps({"artifacts": arts}).encode()
        return _zip(reps[int(path.split("/artifacts/")[1].split("/")[0]) - 100])

    found = flakes.reports(fetch, "o/r", "ci.yml", 3)
    summary = flakes.summarize(found)
    assert [(s["nodeid"], s["flaked"], s["failed"], s["last_url"]) for s in summary] == [
        ("t::a", 2, 0, "u2"), ("t::b", 0, 1, "u1")]
    assert summary[0]["last_error"] == "boom"
    assert not any("/artifacts/9/" in p for p in asked)  # expired artifacts aren't downloaded


# ====================================================================== bisect hook
JUNIT = """<testsuites><testsuite>
<testcase classname="tests.test_a" name="test_ok" time="1"/>
<testcase classname="tests.test_a" name="test_bad[x-1]" time="1"><failure message="boom"/></testcase>
<testcase classname="tests.test_b.TestK" name="test_err" time="1"><error message="fixture"/></testcase>
<testcase classname="" name="tests.test_c" time="0"><error message="collection failure"/></testcase>
</testsuite></testsuites>"""


def test_failing_tests_from_junit(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "junit-unit-1.xml").write_text(JUNIT)
    (tmp_path / "junit-test_e2e_x_docker.xml").write_text(
        '<testsuite><testcase classname="tests.test_e2e_x_docker" name="test_y"><failure/></testcase></testsuite>')
    assert ci_bisect.failing_tests(tmp_path) == (
        ["tests/test_a.py::test_bad[x-1]", "tests/test_b.py::TestK::test_err", "tests/test_c.py"],
        ["tests/test_e2e_x_docker.py::test_y"])


def test_the_check_repeats_what_failed():
    check = ci_bisect.check_command(["tests/test_a.py::test_bad[x-1]"], ["tests/test_e2e_x.py::t"], ["rust"])
    assert check.startswith("uv sync --all-extras --frozen -q && (cd envd && cargo clippy")
    assert "uv run pytest -x -q -p no:cacheprovider 'tests/test_a.py::test_bad[x-1]'" in check
    assert check.endswith("HANGAR_E2E=1 uv run pytest -x -q -p no:cacheprovider tests/test_e2e_x.py::t")
    assert ci_bisect.check_command([], [], ["terraform"]).startswith("for d in deploy/terraform/gke")
    assert ci_bisect.check_command([], [], ["unit (1/4)"]) is None  # a shard died without a junit file


def test_bisect_instructions():
    check = ci_bisect.check_command(["tests/test_a.py::test_bad[x-1]"], [], ["unit (1/4)"])
    text = ci_bisect.instructions("b" * 40, "a" * 40, ["tests/test_a.py::test_bad[x-1]"], ["unit (1/4)"], check,
                                  "https://x/run")
    assert f"git bisect start {'b' * 40} {'a' * 40}" in text and "Failed jobs: unit (1/4)." in text
    assert "git bisect run sh -c 'uv sync" in text and "test_bad[x-1]" in text
    assert "no green run" in ci_bisect.instructions("b" * 40, None, [], [], check, "https://x/run")
    assert "as the run's logs show" in ci_bisect.instructions("b" * 40, "a" * 40, [], ["unit (1/4)"], None, "u")


def test_bisect_submits_a_hangar_job(tmp_path, monkeypatch, capsys):
    (tmp_path / "junit-unit-2.xml").write_text(JUNIT)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "77")
    monkeypatch.setenv("HANGAR_URL", "https://hangar.test/")
    monkeypatch.setenv("HANGAR_TOKEN", "tok-secret")

    def gh_json(path, *args):
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": [{"name": "unit (2/4)", "conclusion": "failure"},
                             {"name": "lint", "conclusion": "success"}]}
        if "status=success" in path:  # newest first: a later green run (78) and this run itself aren't the base
            return {"workflow_runs": [{"id": 78, "event": "push", "head_sha": "d" * 40},
                                      {"id": 77, "event": "push", "head_sha": "c" * 40},
                                      {"id": 70, "event": "push", "head_sha": "a" * 40}]}
        raise AssertionError(path)

    sent = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req: urllib.request.Request, timeout: float):
        sent.append(req)
        return Resp(b'{"id": "job-1"}')

    monkeypatch.setattr(ci_bisect, "gh_json", gh_json)
    monkeypatch.setattr(ci_bisect.urllib.request, "urlopen", urlopen)
    assert ci_bisect.main(["--junit", str(tmp_path)]) == 0
    req = sent[0]
    body = json.loads(req.data)
    assert req.full_url == "https://hangar.test/api/jobs" and req.get_header("Authorization") == "Bearer tok-secret"
    assert body["idempotency_key"] == "ci-bisect-77" and body["deliver"] == "pr" and body["base_branch"] == "main"
    assert f"git bisect start {'b' * 40} {'a' * 40}" in body["instructions"]
    assert "'tests/test_a.py::test_bad[x-1]'" in body["verify"] and "HANGAR_E2E" not in body["verify"]
    out = capsys.readouterr().out
    assert "job-1" in out and "tok-secret" not in out


def test_bisect_falls_back_to_one_issue(tmp_path, monkeypatch):
    for k, v in {"GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "b" * 40, "GITHUB_RUN_ID": "78"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("HANGAR_URL", raising=False)
    calls = []

    def gh_json(path, *args):
        calls.append((path, args))
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": [{"name": "terraform", "conclusion": "failure"}]}
        if "status=success" in path:
            return {"workflow_runs": []}
        if path.startswith("repos/o/r/issues?"):
            return [{"number": 5, "title": "CI: main is red at 1234", "html_url": "https://x/5"}]
        return {"html_url": "https://x/5#c"}

    monkeypatch.setattr(ci_bisect, "gh_json", gh_json)
    assert ci_bisect.main(["--junit", str(tmp_path / "none")]) == 0
    assert calls[-1][0] == "repos/o/r/issues/5/comments" and "no green run" in calls[-1][1][1]


# ====================================================================== isolation for xdist
def test_scratch_root_is_this_process_own():
    root = conftest.scratch_root()
    assert root == conftest.scratch_root() and root.name.startswith(os.environ.get("PYTEST_XDIST_WORKER", "run"))
    assert root.stat().st_mode & 0o777 == 0o755


def test_ports_come_from_this_worker_block_below_the_ephemeral_range(monkeypatch):
    low = int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    ports = [conftest.free_port() for _ in range(5)]
    assert len(set(ports)) == 5 and all(low - 4 * 256 <= p < low - 3 * 256 for p in ports)
