"""Structured test results (--- testresults): the one report parser, collecting reports around every check, flaky
tests, exact red-base subtraction, and where the results show (job result, events, packet, REST, status, OTel)."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hangar import baseline, driver, testreports, testresults, verifyloop
from hangar.app import create_app
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, SubmitJob
from hangar.models import Baseline, ExecResult, Job, JobState, now
from hangar.repoenv import RepoEnvError, parse
from hangar.repopolicy import RepoPolicy, repo_key
from hangar.store import Store
from hangar.testresults import TestRun
from tests.conftest import FakeProvider
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures

FIX = Path(__file__).parent / "fixtures" / "testreports"


def _parse(name: str) -> testreports.Report:
    return testreports.parse((FIX / name).read_bytes(), name)


def _outcomes(r: testreports.Report) -> dict[str, str]:
    return {c.id: c.outcome for c in r.cases}


# ====================================================================== the parser, one fixture per format
def test_pytest_junitxml():
    r = _parse("pytest.xml")
    assert r.kind == "junit" and r.error is None
    assert _outcomes(r) == {"tests.test_math::test_add": "passed", "tests.test_math::test_div[0-1]": "failed",
                            "tests.test_math::test_skip": "skipped", "tests.test_db::test_query": "error",
                            "tests.test_db::test_ünïcode": "passed"}
    div = next(c for c in r.cases if c.name == "test_div[0-1]")
    assert div.message.startswith("ZeroDivisionError: division by zero\n") and "tests/test_math.py:9" in div.message
    assert div.suite == "pytest" and div.seconds == 0.002


def test_gotestsum_jest_vitest():
    go = _parse("gotestsum.xml")
    assert _outcomes(go) == {"example.com/app/parse::TestParse": "passed",
                             "example.com/app/parse::TestParse/empty": "passed",
                             "example.com/app/server::TestServe": "failed",
                             "example.com/app/server::TestServe/timeout": "failed"}
    assert "got 500, want 200" in next(c for c in go.cases if c.name == "TestServe").message
    jest = _parse("jest-junit.xml")
    assert _outcomes(jest) == {"math adds::math adds": "passed", "math subtracts::math subtracts": "failed",
                               "math divides::math divides": "skipped"}
    assert "Received: 2" in next(c for c in jest.cases if c.outcome == "failed").message
    vi = _parse("vitest-junit.xml")
    assert _outcomes(vi) == {"src/a.test.ts::math > adds": "passed", "src/a.test.ts::math > multiplies": "failed"}


def test_retried_tests_from_nextest_and_surefire():
    """A case the runner retried and finally passed is flaky; one that failed every time is a failure."""
    nx = _parse("nextest-junit.xml")
    assert {c.id: (c.outcome, c.flaky) for c in nx.cases} == {
        "app::bin/app::tests::it_works": ("passed", False), "app::bin/app::tests::sometimes": ("passed", True),
        "app::bin/app::tests::always_broken": ("failed", False)}
    sf = _parse("TEST-com.acme.FooTest.xml")
    assert {c.id: (c.outcome, c.flaky) for c in sf.cases} == {
        "com.acme.FooTest::adds": ("passed", False), "com.acme.FooTest::retries": ("passed", True),
        "com.acme.FooTest::divides": ("failed", False)}
    div = next(c for c in sf.cases if c.name == "divides")
    assert div.message.startswith("/ by zero\njava.lang.ArithmeticException") and "not the message" not in div.message
    gr = _parse("TEST-com.acme.GradleTest.xml")
    assert _outcomes(gr) == {"com.acme.GradleTest::parsesConfig()": "passed",
                             "com.acme.GradleTest::needsNetwork()": "skipped"}


def test_coverage_reports():
    cov = _parse("coverage.xml")  # coverage.py
    assert cov.kind == "cobertura" and cov.coverage.lines_valid == 1200 and cov.coverage.line_rate == 0.82
    ist = _parse("cobertura-coverage.xml")  # istanbul writes a DOCTYPE naming the DTD: allowed, never fetched
    assert ist.kind == "cobertura" and ist.error is None and ist.coverage.line_rate == 0.75
    assert ist.coverage.branches_valid == 40
    lc = _parse("lcov.info")
    assert lc.kind == "lcov" and (lc.coverage.lines_valid, lc.coverage.lines_covered) == (10, 9)
    assert (lc.coverage.branches_valid, lc.coverage.branches_covered) == (2, 1)
    both = testreports.merge([cov, lc])
    assert (both.coverage.lines_valid, both.coverage.lines_covered) == (1210, 993) and both.coverage.format == "mixed"


@pytest.mark.parametrize("name", ["xxe.xml", "billion-laughs.xml", "param-entity.xml"])
def test_untrusted_xml_never_expands_or_fetches_anything(name):
    r = _parse(name)
    assert r.error and "entity" in r.error and not r.cases
    assert "root:" not in json.dumps([c.id for c in r.cases])


def test_reports_are_capped(monkeypatch):
    big = b"<testsuite>" + b"".join(b'<testcase name="t%d"/>' % i for i in range(50)) + b"</testsuite>"
    monkeypatch.setattr(testreports, "MAX_CASES", 10)
    r = testreports.parse(big, "big.xml")
    assert r.truncated and len(r.cases) == 10 and "more than 10" in r.error
    assert not testreports.merge([r]).complete
    monkeypatch.setattr(testreports, "MAX_BYTES", 100)
    assert "over" in testreports.parse(big, "big.xml").error
    monkeypatch.setattr(testreports, "MAX_BYTES", 8 * 1024 * 1024)
    deep = b"<testsuite>" + b"<a>" * 100 + b"</a>" * 100 + b"</testsuite>"
    assert "deeper" in testreports.parse(deep, "deep.xml").error
    long = b'<testsuite><testcase name="x"><failure message="m">' + b"y" * 50_000 + b"</failure></testcase></testsuite>"
    assert len(testreports.parse(long, "long.xml").cases[0].message) == testreports.MESSAGE_MAX


def test_what_isnt_a_report():
    assert testreports.parse(b"<html><body/></html>", "x.xml").kind == "unknown"
    assert testreports.parse(b"plain text", "x.txt").error
    assert "not well-formed" in testreports.parse(b"<testsuite><testcase", "x.xml").error
    merged = testreports.merge([testreports.parse(b"<html/>", "page.xml"), _parse("pytest.xml")])
    # an html file is no report, not a JUnit report cut short
    assert merged.complete and merged.sources == ["pytest.xml"] and merged.errors
    # the same test in two reports counts once, at its worst
    a = testreports.parse(b'<testsuite><testcase classname="c" name="t"/></testsuite>', "a.xml")
    b = testreports.parse(b'<testsuite><testcase classname="c" name="t"><failure/></testcase></testsuite>', "b.xml")
    assert testreports.merge([a, b]).counts() == {"total": 1, "passed": 0, "failed": 1, "error": 0, "skipped": 0}


# ====================================================================== collecting around a check
def _local(cwd: Path):
    async def ex(cmd: str, timeout: int) -> ExecResult:
        p = await asyncio.create_subprocess_exec("bash", "-c", cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(), timeout)
        return ExecResult(exit_code=p.returncode, stdout=out.decode(), stderr=err.decode())

    async def rd(path: str, limit: int) -> bytes | None:
        p = Path(path)
        return p.read_bytes()[:limit + 1] if p.is_file() else None

    return ex, rd


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git = ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text("reports/\nout/\n__pycache__/\n")
    (path / "test_x.py").write_text("def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return path


async def test_pytest_gets_its_junitxml_and_the_check_is_unchanged(tmp_path):
    """The injected PYTEST_ADDOPTS makes a real pytest write its report; the check's exit code and output are its
    own, and the tree id names the working tree (uncommitted changes included)."""
    repo = _git_repo(tmp_path / "repo")
    ex, rd = _local(repo)
    cmd = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider test_x.py"
    res, got = await testresults.execute(ex, rd, cmd, d=str(tmp_path / "home/.hangar/tests/tr-1"), repo=str(repo),
                                         timeout=120, globs=[])
    assert res.exit_code == 1 and "1 failed, 1 passed" in res.stdout and "_ht" not in res.stdout
    assert got.files == ["pytest.xml"] and got.results.counts()["failed"] == 1
    assert {c.id for c in got.results.failing()} == {"test_x::test_bad"}
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    clean = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True)
    assert got.head == head.strip() and got.tree == clean.stdout.strip()
    (repo / "new.py").write_text("x = 1\n")  # untracked: part of the tree hangar compares runs by
    _, again = await testresults.execute(ex, rd, "true", d=str(tmp_path / "home/.hangar/tests/tr-2"),
                                         repo=str(repo), timeout=60, globs=[])
    assert again.tree and again.tree != got.tree and again.results is None
    assert not (tmp_path / "home/.hangar/tests/tr-1").exists()  # one check at a time: the last run's reports only
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                          text=True).stdout.strip() == "?? new.py"  # the real index is untouched


async def test_declared_reports_and_only_what_the_run_wrote(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    ex, rd = _local(repo)
    (repo / "reports").mkdir()
    stale = repo / "reports" / "junit.xml"  # an old report the check doesn't write this time
    stale.write_bytes((FIX / "pytest.xml").read_bytes())
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    (repo / "TEST-symlink.xml").symlink_to(FIX / "pytest.xml")  # a link, never followed
    write = ("mkdir -p out build/test-results/test && cp {f}/vitest-junit.xml out/custom.xml && "
             "cp {f}/TEST-com.acme.GradleTest.xml build/test-results/test/ && cp {f}/lcov.info out/x.lcov").format(
        f=shlex.quote(str(FIX)))
    res, got = await testresults.execute(ex, rd, write, d=str(tmp_path / "home/t"), repo=str(repo), timeout=60,
                                         globs=["out/*.xml"])
    assert res.exit_code == 0
    assert sorted(got.files) == ["build/test-results/test/TEST-com.acme.GradleTest.xml", "out/custom.xml",
                                 "out/x.lcov"]
    assert got.results.counts() == {"total": 4, "passed": 2, "failed": 1, "error": 0, "skipped": 1}
    assert got.results.coverage.lines_valid == 10
    _, none = await testresults.execute(ex, rd, write, d=str(tmp_path / "home/t2"), repo=str(repo), timeout=60,
                                        globs=[], inject=False)
    assert "out/custom.xml" not in (none.files or [])  # not declared, and no default path matches it


async def test_a_runner_run_twice_leaves_an_incomplete_report(tmp_path):
    """`pytest a; pytest b` overwrites the one report hangar named: what failed in `a` is missing from it."""
    repo = _git_repo(tmp_path / "repo")
    ex, rd = _local(repo)
    py = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"
    _, got = await testresults.execute(ex, rd, f"{py} test_x.py; {py} test_x.py -k ok", d=str(tmp_path / "h/tr-1"),
                                       repo=str(repo), timeout=120, globs=[])
    assert not got.results.complete and "more than once" in got.results.errors[-1]
    assert got.results.counts()["total"] == 1  # the second run's: only test_ok


def test_report_globs_are_checked():
    assert testresults.check_globs(["reports/*.xml", "**/TEST-*.xml"]) == ["reports/*.xml", "**/TEST-*.xml"]
    script = testresults.gather_script("/h/tr-1", "/w/my repo", ["out/*.xml; rm -rf ~"])
    assert "'./out/*.xml; rm -rf ~'" in script and "'/w/my repo'" in script  # quoted: a glob, never shell
    for bad in (["../x.xml"], ["/etc/x.xml"], ["-delete"], ["a\nb"], ["x"] * 17):
        with pytest.raises(ValueError):
            testresults.check_globs(bad)
    assert testresults._find_pattern("**/TEST-*.xml") == "*/TEST-*.xml"
    assert testresults._find_pattern("reports/*.xml") == "./reports/*.xml"
    cfg = parse("verify: pytest\nverify_reports: [reports/*.xml]\n")
    assert cfg.verify_reports == ["reports/*.xml"]
    with pytest.raises(RepoEnvError, match="relative"):
        parse("verify_reports: [../../etc/passwd]\n")
    assert RepoPolicy(repo="o/r", verify_reports=["x.xml"], rerun_flaky=True).rerun_flaky is True
    with pytest.raises(ValueError):
        RepoPolicy(repo="o/r", verify_reports=["/abs.xml"])


# ====================================================================== flaky tests
def _results(**outcomes: str) -> testreports.Results:
    r = testreports.Results()
    for cid, o in outcomes.items():
        r.cases[cid] = testreports.Case(id=cid, name=cid, outcome=o)  # type: ignore[arg-type]
    return r


def test_test_ids_are_redacted_consistently(tmp_path):
    store = Store(tmp_path / "db")
    red = lambda s: s.replace("hunter2", "[redacted]")  # noqa: E731
    r = testresults.record(store, results=_results(**{"t[hunter2]": "failed", "t[x]": "passed"}), kind="verify",
                           repo="o/r", redact=red)
    assert r.failing[0]["id"] == "t[[redacted]]" and set(testresults.decode(r)) == {"t[[redacted]]", "t[x]"}


def test_a_test_that_passes_and_fails_on_one_tree_is_flaky(tmp_path):
    store = Store(tmp_path / "db")
    kw = dict(repo="https://github.com/Acme/App.git", environment="py", tree="a" * 40)
    first = testresults.record(store, results=_results(t1="passed", t2="failed", t3="passed"), kind="verify", **kw)
    assert first.repo == "acme/app" and first.flaky == [] and testresults.flaky(store, "acme/app") == {}
    other_env = testresults.record(store, results=_results(t1="failed"), kind="verify",
                                   **{**kw, "environment": "node"})
    agent = testresults.record(store, results=_results(t3="failed"), kind="in_turn", **kw)  # the agent's: no
    assert other_env.flaky == [] and agent.flaky == [] and not agent.trusted and agent.cases is None
    second = testresults.record(store, results=_results(t1="failed", t2="passed", t3="passed"), kind="rerun", **kw)
    assert second.flaky == ["t1", "t2"]
    fl = testresults.flaky(store, "Acme/App")
    assert set(fl) == {"t1", "t2"} and fl["t1"]["how"] == "rerun" and fl["t1"]["n"] == 1
    runner = _parse("nextest-junit.xml")
    third = testresults.record(store, results=testreports.merge([runner]), kind="verify",
                               **{**kw, "tree": "b" * 40})
    assert third.flaky == ["app::bin/app::tests::sometimes"]
    assert testresults.flaky(store, "acme/app")["app::bin/app::tests::sometimes"]["how"] == "runner"


def test_old_entries_and_runs_go(tmp_path, monkeypatch):
    store = Store(tmp_path / "db")
    store.kv_set_json(testresults.KV_FLAKY + "o/r", {"old": {"n": 1, "last": now() - 40 * 86400},
                                                     "new": {"n": 2, "last": now()}})
    assert list(testresults.flaky(store, "o/r")) == ["new"]
    r = testresults.record(store, results=_results(a="passed"), kind="verify", repo="o/r")
    r.at = now() - 4 * 86400
    store.put(r)
    base = testresults.record(store, results=_results(a="failed"), kind="base", repo="o/r")
    base.at = now() - 4 * 86400
    store.put(base)
    gone = testresults.record(store, results=_results(a="failed"), kind="verify", repo="o/r")
    gone.at = now() - 15 * 86400
    store.put(gone)
    assert testresults.prune(store) == 1
    assert store.get(TestRun, r.id).cases is None and store.get(TestRun, base.id).cases is not None
    assert store.get(TestRun, gone.id) is None


# ====================================================================== red bases, exactly
def _job_on(store: Store, base: TestRun) -> Job:
    job = Job(title="t", instructions="x", environment="e", branch="hangar/j", verify="pytest", repo="o/r",
              verify_source="repo")
    job.result.base_sha = "c" * 40
    job.result.baseline = Baseline(sha="c" * 40, exit_code=1, tests=base.id, test_failures=base.n_failing)
    return job


def test_new_failures_by_test_id(tmp_path):
    store = Store(tmp_path / "db")
    many = {f"ok{i}": "passed" for i in range(300)}
    base = testresults.record(store, results=_results(old="failed", **many), kind="base", repo="o/r", exit_code=1)
    job = _job_on(store, base)
    after = testresults.record(store, results=_results(old="failed", new="failed", **many), kind="verify",
                               repo="o/r", exit_code=1)
    assert testresults.structured_new(store, job, after) == ["new"]
    same = testresults.record(store, results=_results(old="failed", **many), kind="verify", repo="o/r", exit_code=1)
    assert testresults.structured_new(store, job, same) == []
    svc = SimpleNamespace(store=store)
    # text that reads nothing (no ids), yet the reports say it exactly
    assert testresults.new_failures(svc, job, same, "boom", truncated=True) == []
    # a build error: it failed with no failing test, so the reports can't speak for it
    broken = testresults.record(store, results=_results(**many), kind="verify", repo="o/r", exit_code=2)
    assert testresults.structured_new(store, job, broken) is None
    # tests the base ran that didn't run here could be hiding failures
    fewer = testresults.record(store, results=_results(old="failed", **dict(list(many.items())[:200])),
                               kind="verify", repo="o/r", exit_code=1)
    assert testresults.structured_new(store, job, fewer) is None
    # a check that stops at its first failure can't vouch for the rest
    job.verify = "pytest -x"
    assert testresults.new_failures(svc, job, same, "boom", truncated=False) is None
    # a green base: nothing to subtract
    job.result.baseline = Baseline(sha="c" * 40, exit_code=0)
    assert testresults.structured_new(store, job, after) is None


# ====================================================================== in jobs
AGENT = r"""#!/usr/bin/env bash
mode=$(cat "$MODE_FILE" 2>/dev/null || echo ok)
case "$mode" in
  ok) echo fixed > fix.txt && git add fix.txt && git commit -qm fix ;;
  newfail)
    if grep -q "these failures are new: tests.test_new::test_new" "$HANGAR_PROMPT_FILE"; then
      git rm -q new.txt && git commit -qm "drop new.txt"
    else
      echo x > new.txt && git add -A && git commit -qm "break test_new"
    fi ;;
esac
echo "done" > "$HANGAR_JOB_DIR/RESULT.md"
"""

# The repo's check writes a JUnit report and prints nothing hangar can read test ids from: the base always fails
# test_old; new.txt breaks test_new; coverage drops a little with fix.txt
CHECK = r"""#!/usr/bin/env bash
mkdir -p reports
{
  echo '<testsuites><testsuite name="t">'
  echo '<testcase classname="tests.test_old" name="test_old"><failure message="old">AssertionError</failure></testcase>'
  echo '<testcase classname="tests.test_a" name="test_a"/>'
  [ -f new.txt ] && echo '<testcase classname="tests.test_new" name="test_new"><failure message="new"/></testcase>'
  echo '</testsuite></testsuites>'
} > reports/junit.xml
covered=90; [ -f fix.txt ] && covered=85
printf '<coverage lines-valid="100" lines-covered="%s" line-rate="0.9"/>' "$covered" > reports/coverage.xml
echo "something broke"
exit 1
"""

FLAKY = r"""#!/usr/bin/env bash
n=$(( $(cat "$COUNTER" 2>/dev/null || echo 0) + 1 )); echo $n > "$COUNTER"
mkdir -p reports
fail=""; [ $((n % 2)) -eq 1 ] && fail='<failure message="timing"/>'
flaky="<testcase classname=\"tests.test_f\" name=\"test_flaky\">$fail</testcase>"
echo "<testsuite>$flaky<testcase classname=\"tests.test_f\" name=\"test_ok\"/></testsuite>" > reports/junit.xml
[ $((n % 2)) -eq 0 ]
"""


@pytest.fixture
def jrepo(world, monkeypatch):  # noqa: F811
    root = world["root"]
    agent = root / "tr-agent.sh"
    agent.write_text(AGENT)
    monkeypatch.setitem(HARNESSES, "scripted", Harness(
        name="scripted", title="Scripted", run=f"MODE_FILE={world['mode']} bash {agent}"))
    seed = root / "seed"
    (seed / "check.sh").write_text(CHECK)
    (seed / "flaky.sh").write_text(FLAKY.replace('"$COUNTER"', str(root / "flaky-runs")))
    (seed / ".gitignore").write_text("reports/\n")
    git = ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "checks"], check=True)
    subprocess.run([*git, "push", "-q", str(world["remote"]), "HEAD:refs/heads/main"], check=True)
    (world["envs"] / "junitenv.yaml").write_text("name: junitenv\nharnesses: [scripted]\nverify: bash check.sh\n")
    (world["envs"] / "plainenv.yaml").write_text("name: plainenv\nharnesses: [scripted]\n")
    return world


async def _run(jobs, world, **kw) -> Job:  # noqa: F811
    jobs.fleet.catalog.reload()
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], **kw), Caller("svc", "svc"))
    return await _until(jobs, job.id, DONE, timeout=40)


def _messages(job: Job) -> list[str]:
    return [e.message for e in job.events]


def _box(jobs, job) -> Path:  # noqa: F811
    return jobs.fleet.providers["docker"].boxes[job.sandbox_id]


async def test_a_red_base_is_subtracted_by_test_id(jobs, jrepo):  # noqa: F811
    """The output names no test, so the text can't tell new failures from the base's (before: a draft, reason
    base_red); the reports can, exactly."""
    job = await _run(jobs, jrepo, environment="junitenv")
    assert job.state == JobState.SUCCEEDED and job.reason is None, _messages(job)
    b = job.result.baseline
    assert b.red and not b.parsed and b.tests and b.test_failures == 1
    assert any(m.startswith("base was red") and "1 failing test(s)" in m for m in _messages(job))
    assert job.result.new_failures == []
    t = job.result.tests
    assert (t.kind, t.passed, t.failed, t.new_failures, t.coverage, t.coverage_delta) == \
        ("verify", 1, 1, [], 85.0, -5.0)
    assert t.failing == ["tests.test_old::test_old"] and t.file.endswith("/TEST-RESULTS.json")
    assert any(m.startswith("the base's tests: 1 passed, 1 failed") for m in _messages(job))
    assert any(m.startswith("tests: 1 passed, 1 failed, 0 skipped; new against the base: 0") for m in _messages(job))
    doc = json.loads((_box(jobs, job) / t.file.lstrip("/")).read_text())
    assert doc["base_failing"] == ["tests.test_old::test_old"] and doc["new_failures"] == []
    assert doc["failing"][0]["message"].startswith("old")
    prompt = sorted((_box(jobs, job) / "home/agent/.hangar/runs").glob("*.prompt"))[0].read_text()
    assert "TEST-RESULTS.json" in prompt
    packet = jobs.landing.packet(job)
    assert "**Tests:** 1 passed, 1 failed, 0 skipped" in packet and "new failures against the base: none" in packet
    assert "line coverage 85.0%, -5.00 points against the base" in packet
    evs = jobs.events(job.id)
    tests = [e for e in evs if e["data"].get("action") == "tests"]
    assert tests and tests[-1]["data"]["failed"] == 1 and tests[-1]["kind"] == "progress"
    runs = testresults.runs_of(jobs.store, job_id=job.id)
    assert [r.kind for r in runs] == ["verify", "base"] and runs[0].tree and runs[0].sha


async def test_new_failures_by_id_go_back_to_the_agent(jobs, jrepo):  # noqa: F811
    jrepo["mode"].write_text("newfail")
    job = await _run(jobs, jrepo, environment="junitenv")
    assert job.state == JobState.SUCCEEDED, _messages(job)
    assert [t.trigger for t in job.turns] == [None, "verify"] and job.result.new_failures == []
    fix = sorted((_box(jobs, job) / "home/agent/.hangar/runs").glob("*.prompt"), key=lambda p: p.stat().st_mtime)[-1]
    text = fix.read_text()
    assert "these failures are new: tests.test_new::test_new" in text and "TEST-RESULTS.json" in text


async def test_a_failure_only_on_known_flaky_tests_runs_once_more(jobs, jrepo):  # noqa: F811
    key = repo_key(jrepo["url"])
    jobs.policies.put(RepoPolicy(repo=key, rerun_flaky=True))
    jobs.store.kv_set_json(testresults.KV_FLAKY + key,
                           {"tests.test_f::test_flaky": {"n": 3, "first": now(), "last": now(), "how": "same_tree"}})
    job = await _run(jobs, jrepo, environment="plainenv", verify="bash flaky.sh")
    assert job.state == JobState.SUCCEEDED and len(job.turns) == 1, _messages(job)
    assert any("running it once more (rerun_flaky)" in m for m in _messages(job))
    assert "the rerun passes" in _messages(job)
    assert job.result.tests.kind == "rerun" and job.result.verify_exit_code == 0
    entry = testresults.flaky(jobs.store, key)["tests.test_f::test_flaky"]
    assert entry["n"] == 4 and entry["how"] == "rerun" and entry["job"] == job.id  # passed after a rerun, no change


async def test_without_rerun_flaky_the_failure_stands(jobs, jrepo):  # noqa: F811
    key = repo_key(jrepo["url"])
    jobs.store.kv_set_json(testresults.KV_FLAKY + key,
                           {"tests.test_f::test_flaky": {"n": 3, "first": now(), "last": now(), "how": "same_tree"}})
    jrepo["mode"].write_text("none")  # the agent changes nothing: the verify-fix turn runs the same tree again
    job = await _run(jobs, jrepo, environment="plainenv", verify="bash flaky.sh", max_verify_rounds=1)
    assert not any("once more" in m for m in _messages(job))
    assert [t.trigger for t in job.turns] == [None, "verify"]
    # the second check ran on the same tree and passed it: flaky again, seen by comparing the two runs
    assert job.result.tests.flaky == ["tests.test_f::test_flaky"] and job.result.tests.failed == 0
    entry = testresults.flaky(jobs.store, key)["tests.test_f::test_flaky"]
    assert entry["how"] == "same_tree" and entry["n"] == 4


# ====================================================================== the in-turn gate
def test_the_stop_hook_keeps_each_checks_reports(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    (home / ".hangar/runs").mkdir(parents=True)
    repo.mkdir()
    verify = ('printf "<testsuite><testcase name=\\"a\\"><failure/></testcase></testsuite>" '
              '> "$HANGAR_TESTS_DIR/x.xml"; echo "PYTEST_ADDOPTS=$PYTEST_ADDOPTS"; exit 1')
    hook = verifyloop.stop_hook(verify, str(repo), 3, int(now()) + 600, testresults.prefix('"$HANGAR_TESTS_DIR"'))
    env = {"HOME": str(home), "HANGAR_RUN_ID": "r1", "PATH": os.environ["PATH"]}
    for n in (0, 1):
        p = subprocess.run(["bash", "-c", hook], env=env, capture_output=True, text=True)
        assert p.returncode == 2 and "round" in p.stderr
        d = home / f".hangar/runs/r1.tests/{n}"
        assert (d / ".start").exists() and (d / "x.xml").exists()
        assert f"--junitxml={d}/pytest.xml" in p.stderr
    assert (home / ".hangar/runs/r1.verify").read_text() == "fail 1\nfail 1\n"
    # without reports the hook is as it was
    assert "HANGAR_TESTS_DIR" not in verifyloop.stop_hook("pytest", "/w", 3, 100)


def test_the_drivers_gate_keeps_each_checks_reports(tmp_path, monkeypatch):
    home, repo = tmp_path / "home", tmp_path / "repo"
    (home / ".hangar/runs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HANGAR_RUN_ID", "r2")
    events: list[dict] = []
    out = SimpleNamespace(event=lambda kind, **kw: events.append(kw))
    spec = {"command": 'touch "$HANGAR_TESTS_DIR/ran"; exit 4', "cwd": str(repo), "rounds": 2,
            "deadline": int(now()) + 600, "timeout": 30, "prefix": testresults.prefix("@D@")}
    g = driver.Gate(spec, out)
    assert g.check() is not None and g.check() is not None
    for n in (0, 1):
        assert (home / f".hangar/runs/r2.tests/{n}/ran").exists()
    assert "@D@" not in json.dumps(events) and events[0]["exit_code"] == 4


class _Fleet:
    """Runs the sandbox's commands here, with /home/agent under `root`."""

    def __init__(self, root: Path):
        self.root = root

    def _m(self, s: str) -> str:
        return s.replace("/home/agent", str(self.root / "home/agent"))

    async def exec(self, sbid, cmd, timeout=120, actor="", quiet=False, cwd=None):
        p = subprocess.run(["bash", "-c", self._m(cmd)], capture_output=True, text=True, timeout=timeout)
        return ExecResult(exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr)

    async def read_file(self, sbid, path, limit=None):
        f = Path(self._m(path))
        return f.read_bytes()[: (limit or 10**9) + 1] if f.is_file() else None


async def test_the_gates_reports_are_collected_after_the_turn(tmp_path):
    from hangar.models import Turn

    runs = tmp_path / "home/agent/.hangar/runs"
    for n, case in enumerate(['<testcase name="a"><failure/></testcase>', '<testcase name="a"/>']):
        d = runs / f"r1.tests/{n}"
        d.mkdir(parents=True)
        (d / ".start").write_text("")
        os.utime(d / ".start", (time.time() - 60, time.time() - 60))
        (d / "pytest.xml").write_text(f"<testsuite>{case}</testsuite>")
    (runs / "r1.verify").write_text("fail 1\npass\n")
    store, events = Store(tmp_path / "db"), []
    svc = SimpleNamespace(settings=SimpleNamespace(test_reports="inject", sandbox_user="agent"),
                          fleet=_Fleet(tmp_path), store=store, broker=SimpleNamespace(redact=lambda s: s),
                          _event=lambda job, msg: events.append(msg), sessions=SimpleNamespace(activity=None))
    job = Job(title="t", instructions="x", environment="e", branch="hangar/j", verify="pytest", repo="o/r",
              sandbox_id="sb")
    await testresults.in_turn(svc, job, Turn(run_id="r1"))
    got = sorted(testresults.runs_of(store, job_id=job.id), key=lambda r: r.at)
    assert [(r.kind, r.trusted, r.exit_code, r.n_failing) for r in got] == [("in_turn", False, 1, 1),
                                                                          ("in_turn", False, 0, 0)]
    assert events == ["the agent's last check in the turn (2 run(s)): 1 passed, 0 failed, 0 skipped"]
    assert not (runs / "r1.tests").exists() and testresults.flaky(store, "o/r") == {}  # the agent's: advisory


def test_the_gate_env_carries_the_prefix(settings):
    job = Job(title="t", instructions="x", environment="e", branch="hangar/j", verify="pytest -q")
    env = verifyloop.turn_env(settings, job, "claude-code", "/w", 60, None)
    assert "HANGAR_TESTS_DIR" in env[verifyloop.CLAUDE_ENV]
    settings.test_reports = "off"
    assert "HANGAR_TESTS_DIR" not in verifyloop.turn_env(settings, job, "claude-code", "/w", 60, None)[
        verifyloop.CLAUDE_ENV]


# ====================================================================== where it shows
def test_trace_events_follow_the_trace_taxonomy_and_the_test_semantic_conventions(tmp_path):
    """docs/api/traces.md (hangar.trace/v1) reserves `test.run` and `test.case`, attributes under hangar.test.*;
    the OTel test conventions' names ride along."""
    store = Store(tmp_path / "db")
    r = testresults.record(store, results=testreports.merge([_parse("nextest-junit.xml")]), kind="verify",
                           repo="o/r", job_id="job-1", exit_code=1, command="cargo nextest run")
    run, *cases = testresults.trace_events(r)
    a = run["attributes"]
    assert run["name"] == a["hangar.kind"] == "test.run" and a["test.suite.name"] == "cargo nextest run"
    assert a["test.suite.run.status"] == "failure" and (a["hangar.test.failed"], a["hangar.test.total"]) == (1, 3)
    assert {e["attributes"]["test.case.name"]: e["attributes"]["test.case.result.status"] for e in cases} == {
        "app::bin/app::tests::always_broken": "fail", "app::bin/app::tests::sometimes": "pass"}
    assert all(e["name"] == e["attributes"]["hangar.kind"] == "test.case" for e in cases)
    assert all(k.startswith(("hangar.", "test.", "vcs.")) for e in [run, *cases] for k in e["attributes"])
    assert testresults._suite_status(124) == "timed_out" and testresults._suite_status(0) == "success"

    class X:  # the trace export's Export, as traces.job_export calls it
        def __init__(self):
            self.events = []

        def event(self, sp, name, ts, attrs):
            self.events.append((sp, name, attrs["hangar.test.run.id"]))

    job = Job(id="job-1", title="t", instructions="x", environment="e", branch="hangar/j")
    x = X()
    testresults.add_to_export(x, "root", store, job)
    assert [n for _, n, _ in x.events] == ["test.run", "test.case", "test.case"] and x.events[0][2] == r.id


async def test_live_traces_carry_the_runs_events(settings, tmp_path):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from hangar import observe

    exp = InMemorySpanExporter()
    await observe.start(settings, exporter=exp)
    try:
        r = testresults.record(Store(tmp_path / "db"), results=_results(a="failed", b="passed"), kind="verify",
                               repo="o/r", exit_code=1, command="pytest -q")
        testresults.export_otel(r)  # no span open: nothing, and no error
        with observe.span("job.verify", {"hangar.job.id": "job-1"}):
            testresults.export_otel(r)
    finally:
        await observe.stop()
    (sp,) = [s for s in exp.get_finished_spans() if s.name == "job.verify"]
    assert [e.name for e in sp.events] == ["test.run", "test.case"]
    assert sp.events[1].attributes["test.case.name"] == "a" and sp.events[0].attributes["hangar.test.failed"] == 1


def test_rest_and_status(settings):
    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        h = app.state.hangar
        h.fleet.providers["docker"] = FakeProvider()
        c.headers["Authorization"] = "Bearer t0ken"
        job = Job(title="t", instructions="x", environment="agent", branch="hangar/j", repo="acme/app")
        h.store.put(job)
        r1 = testresults.record(h.store, results=_results(a="passed", b="failed"), kind="verify", repo="acme/app",
                                job_id=job.id, tree="d" * 40, environment="agent", exit_code=1)
        testresults.record(h.store, results=_results(a="failed", b="failed"), kind="rerun", repo="acme/app",
                           job_id=job.id, tree="d" * 40, environment="agent", exit_code=1)
        got = c.get(f"/api/jobs/{job.id}/tests").json()
        assert [r["kind"] for r in got["runs"]] == ["rerun", "verify"] and got["latest"]["flaky"] == ["a"]
        assert "cases" not in got["latest"] and set(got["flaky"]) == {"a"}
        one = c.get(f"/api/jobs/{job.id}/tests", params={"run": r1.id, "cases": "true"}).json()
        assert one["cases"] == {"a": "passed", "b": "failed"}
        evs = c.get(f"/api/jobs/{job.id}/tests", params={"format": "otel"}).json()["events"]
        assert [e["name"] for e in evs] == ["test.run", "test.case", "test.run", "test.case", "test.case"]
        assert c.get("/api/jobs/job-nope/tests").status_code == 404
        assert c.get("/api/repos/tests/flaky", params={"repo": "Acme/App"}).json()["flaky"]["a"]["how"] == "rerun"
        st = c.get("/api/status", params={"section": "tests"}).json()["tests"]
        assert st["repos"][0]["repo"] == "acme/app" and st["repos"][0]["flaky"] == 1


def test_the_acceptance_checks_are_runs_on_the_deployed_commit(tmp_path):
    store = Store(tmp_path / "db")
    assert testresults.record_acceptance(store, {"result": "passed"}) is None
    testresults.record_acceptance(store, {"sha": "e" * 40, "checks": {"exec": "pass", "stream": "pass"}})
    r = testresults.record_acceptance(store, {"sha": "e" * 40, "checks": {"exec": "pass", "stream": "fail"}})
    assert r.kind == "acceptance" and r.exit_code == 1 and r.flaky == ["stream"]
    assert list(testresults.flaky(store, testresults.ACCEPTANCE_REPO)) == ["stream"]


def test_the_doctor_counts_only_flaky_failures_as_transient(tmp_path):
    store = Store(tmp_path / "db")
    store.kv_set_json(testresults.KV_FLAKY + "o/r", {"t1": {"n": 2, "last": now()}})
    testresults.record(store, results=_results(t1="failed", t2="passed"), kind="verify", repo="o/r", job_id="j1",
                       exit_code=1)
    testresults.record(store, results=_results(t1="failed", t2="failed"), kind="verify", repo="o/r", job_id="j2",
                       exit_code=1)
    assert testresults.only_flaky(store, ["j1"], "o/r")
    assert not testresults.only_flaky(store, ["j1", "j2"], "o/r")  # j2 also failed t2
    assert not testresults.only_flaky(store, ["j1", "j3"], "o/r")  # j3 left no reports
    assert not testresults.only_flaky(store, ["j1"], "x/y")


def test_canary_reads_reports_through_the_one_parser(tmp_path):
    from hangar import canary

    res = canary.junit_results([FIX / "pytest.xml", FIX / "xxe.xml"])
    assert (res["passed"], res["failed"], res["skipped"]) == (2, 3, 1)
    assert {"test": "xxe.xml", "message": "no readable test report"} in res["failures"]
    assert baseline.stops_early("pytest -x", "") is True  # the guard both kinds of subtraction share
