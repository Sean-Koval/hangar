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
import verify_baseline as vb  # noqa: E402


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


# ---------------------------------------------------------------------- verify's cap (KAN-138)
def capped(graph, budget, durations, *changed):
    sel = st.select(list(changed), graph)
    files = sorted(t for t in sel.tests if graph.is_unit(t))
    kept, why = st.cap(sel, files, budget, durations)
    return [Path(t).stem for t in kept], why


def test_under_the_budget_verify_runs_what_ci_would(repo):
    assert capped(repo, 100, {}, "src/hangar/a.py") == (["test_a", "test_b", "test_helper_user", "test_scan"], "")


def test_over_the_budget_it_keeps_named_then_direct_then_cheapest(repo, monkeypatch):
    monkeypatch.setattr(st, "CONTRACT", ["tests/test_core.py"])
    durations = {"tests/test_a.py": 5, "tests/test_b.py": 1, "tests/test_helper_user.py": 1, "tests/test_scan.py": 9,
                 "tests/test_c.py": 2, "tests/test_core.py": 3}
    # a.py: test_a is its own (named), the others import it one hop away; 7s fits test_a, then the cheapest two
    kept, why = capped(repo, 7, durations, "src/hangar/a.py")
    assert kept == ["test_a", "test_b", "test_helper_user"] and "3 of 4 test files" in why and "--full" in why
    # a hub change keeps the contract tests, then its direct importers (test_core imports core) within the budget
    kept, _ = capped(repo, 4, durations, "src/hangar/core.py")
    assert kept == ["test_b", "test_core"]
    # what CI runs everything for keeps the contract tests however small the budget (and test_ci for scripts/ci/)
    kept, _ = capped(repo, 0, durations, "scripts/ci/verify.sh")
    assert kept == ["test_core"]


def test_the_real_cap_keeps_the_api_and_contract_tests_for_a_hub():
    graph = st.Graph()
    durations = json.loads(st.DURATIONS.read_text())
    for hub in ("src/hangar/app.py", "src/hangar/config.py", "src/hangar/models.py"):
        sel = st.select([hub], graph)
        files = sorted(t for t in sel.tests if graph.is_unit(t))
        kept, why = st.cap(sel, files, 240, durations)
        assert sel.full and set(st.CONTRACT) <= set(kept) and len(kept) < len(files) and "CI runs the rest" in why
    sel = st.select(["scripts/ci/verify_baseline.py"], graph)
    kept, _ = st.cap(sel, sorted(t for t in sel.tests if graph.is_unit(t)), 240, durations)
    assert "tests/test_ci.py" in kept


def test_the_cli_caps_only_when_asked(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/ci/select_tests.py"), *args],
                              capture_output=True, text=True)
    everything = run("--files", "src/hangar/app.py").stdout.split()
    some = run("--files", "src/hangar/app.py", "--budget", "120")
    assert set(st.CONTRACT) <= set(some.stdout.split()) < set(everything) and "capped at 120s" in some.stderr
    assert run("--all", "--budget", "60").returncode == 2  # CI's full run is never capped


def test_a_list_of_marks_still_opts_in():
    import ast
    tree = ast.parse('import os, pytest\npytestmark = [pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", '
                     'reason=""), pytest.mark.needs("docker")]\n')
    assert st.opt_ins(tree) == {"pytestmark": "HANGAR_E2E"}


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


# ====================================================================== verify's baseline (KAN-138)
def test_junit_names_become_node_ids(tmp_path):
    (tmp_path / "tests").mkdir()
    for f in ("test_a", "test_b", "test_c"):
        (tmp_path / "tests" / f"{f}.py").write_text("")
    (tmp_path / "j.xml").write_text(JUNIT)
    assert vb.outcomes(tmp_path / "j.xml", tmp_path) == {
        "tests/test_a.py::test_ok": "passed", "tests/test_a.py::test_bad[x-1]": "failed",
        "tests/test_b.py::TestK::test_err": "failed", "tests/test_c.py": "failed"}


def test_only_what_also_failed_on_the_base_is_pre_existing():
    base = {"t::a": "failed", "t::b": "passed", "t::c": "skipped", "t::d": "missing"}
    assert vb.compare(["t::a", "t::b", "t::c", "t::d", "t::e"], base) == (["t::a"], ["t::b", "t::c", "t::d", "t::e"])


BASE_TESTS = """
def test_always_broken():
    assert False

def test_fine():
    assert True
"""


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def two_commits(tmp_path, monkeypatch):
    """A repo whose base has one broken test, and a branch that breaks test_fine and adds a broken test."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_x.py").write_text(BASE_TESTS)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(vb, "ROOT", repo)
    return repo, base


def _run_head(repo):
    junit = repo.parent / "head.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml", str(junit),
                    "tests"], cwd=repo, capture_output=True)
    return junit


def test_verify_passes_when_every_failure_is_the_bases(two_commits, capsys):
    repo, base = two_commits
    junit = _run_head(repo)
    assert vb.main(["--base-sha", base, "--junit", str(junit), "--workers", "0"]) == 0
    out = capsys.readouterr().out
    assert "pre-existing: tests/test_x.py::test_always_broken" in out and "no new failures" in out
    cached = json.loads(vb.cache_file(base).read_text())
    assert cached == {"tests/test_x.py::test_always_broken": "failed"}
    assert vb.main(["--base-sha", base, "--junit", str(junit), "--workers", "0"]) == 0  # from the cache
    assert "running" not in capsys.readouterr().err
    assert "hangar-verify-base" not in subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True,
                                                      text=True).stdout  # the scratch worktree is gone


def test_verify_fails_on_a_new_failure_and_names_it(two_commits, capsys):
    repo, base = two_commits
    (repo / "tests" / "test_x.py").write_text(BASE_TESTS.replace("assert True", "assert 0"))
    (repo / "tests" / "test_y.py").write_text("def test_new():\n    assert False\n")
    junit = _run_head(repo)
    assert vb.main(["--base-sha", base, "--junit", str(junit), "--workers", "0"]) == 1
    out = capsys.readouterr().out
    assert "pre-existing: tests/test_x.py::test_always_broken" in out
    assert "new: tests/test_x.py::test_fine (base: passed)" in out
    assert "new: tests/test_y.py::test_new (base: missing)" in out


def test_verify_counts_a_test_the_base_lacks_inside_a_file_it_has(two_commits, capsys):
    repo, base = two_commits
    (repo / "tests" / "test_x.py").write_text(BASE_TESTS + "\ndef test_added():\n    assert False\n")
    junit = _run_head(repo)
    assert vb.main(["--base-sha", base, "--junit", str(junit), "--workers", "0"]) == 1
    assert "new: tests/test_x.py::test_added (base: missing)" in capsys.readouterr().out


# ====================================================================== capabilities (KAN-138)
def test_a_missing_capability_skips_with_its_reason_and_ci_fails_it(tmp_path):
    (tmp_path / "conftest.py").write_text("from tests.conftest import *  # noqa\nfrom tests import conftest as c\n"
                                          "c._missing['kvm'] = 'no usable /dev/kvm'\n")
    (tmp_path / "test_k.py").write_text("import pytest\n\n@pytest.mark.needs('kvm')\ndef test_k():\n    pass\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    env.pop("HANGAR_REQUIRE_CAPS", None)

    def run(**extra):
        return subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "-p",
                               "no:randomly", str(tmp_path)], capture_output=True, text=True, cwd=tmp_path,
                              env={**env, **extra})
    r = run()
    assert r.returncode == 0 and "capability: kvm: no usable /dev/kvm" in r.stdout and "1 skipped" in r.stdout
    r = run(HANGAR_REQUIRE_CAPS="1")
    assert r.returncode == 1 and "capability kvm is required here" in r.stdout


def test_an_unknown_capability_is_a_usage_error(tmp_path):
    (tmp_path / "conftest.py").write_text("from tests.conftest import *  # noqa\n")
    (tmp_path / "test_k.py").write_text("import pytest\n\n@pytest.mark.needs('gpu')\ndef test_k():\n    pass\n")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
                       capture_output=True, text=True, cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 4 and "unknown capability ['gpu']" in r.stdout + r.stderr


def test_ci_never_lets_a_capability_skip():
    for wf in ("ci.yml", "pr.yml", "harness-contract.yml"):
        assert 'HANGAR_REQUIRE_CAPS: "1"' in (ROOT / ".github/workflows" / wf).read_text(), wf
    docker = [p.name for p in (ROOT / "tests").glob("test_e2e_*docker.py")
              if 'pytest.mark.needs("docker")' not in p.read_text()]
    assert docker == []  # every Docker suite says so: CI fails it, rather than skips it, without Docker


def test_fake_sandboxes_remap_their_paths_but_not_the_hosts(monkeypatch):
    monkeypatch.setattr(conftest, "HOST_PATHS", ["/workspace/hangar/.venv", "/workspace/hangar",
                                                 "/home/agent/.local/python"])
    cmd = "/workspace/hangar/.venv/bin/python /workspace/hangar/tests/h.py --home /home/agent --repo /workspace/r"
    assert conftest.remap(cmd, "/box") == ("/workspace/hangar/.venv/bin/python /workspace/hangar/tests/h.py --home "
                                           "/box/home/agent --repo /box/workspace/r")
    assert conftest.remap("/home/agent/.local/python/bin/python3 /tmp/x", "/box") == \
        "/home/agent/.local/python/bin/python3 /box/tmp/x"
    assert conftest.remap("/tmp/x", "/box", tmp=False) == "/tmp/x"
