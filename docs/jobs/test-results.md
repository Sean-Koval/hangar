# Test results

Structured results from every check: where the reports come from, what is kept, flaky tests, where they show. Part of [jobs](README.md).

Every check hangar runs leaves structured results, not just an exit code and the end of its output: the check on
the base commit, the check after each turn (and its [flaky rerun](#flaky-tests)), best-of-N attempts, agent-led
setup's validation, the checks the [in-turn gate](agent-loop.md#the-verify-gate-inside-the-turn) runs for the agent, and an
experiment's [grader](experiments.md#grading) (kind `grader`). The deploy acceptance checks are recorded the same way. The design is in `src/hangar/testresults.py`; the one report parser is
`src/hangar/testreports.py`.

## Where the reports come from

Around the check, in the same shell and the same exec, hangar:

1. makes a fresh report directory, `$HANGAR_TESTS_DIR` (`~/.hangar/tests/<run>/`), and marks the run's start;
2. sets the variables the common runners read, unless the environment already sets them:
   - **pytest:** `PYTEST_ADDOPTS=--junitxml=$HANGAR_TESTS_DIR/pytest.xml`, added to what `PYTEST_ADDOPTS` already holds,
     and not at all when it already names a JUnit file, when the check's command names `junitxml`, or when the
     repo's `pytest.ini`, `pyproject.toml`, `setup.cfg` or `tox.ini` does (PYTEST_ADDOPTS comes after the ini's
     `addopts`, so it would replace a report path the repo sets, and `-p no:junitxml` would make pytest stop with a
     usage error). Then the repo's own report is read where it puts it.
   - **gotestsum:** `GOTESTSUM_JUNITFILE` (gotestsum reads the variable as it reads `--junitfile`).
   - **jest-junit:** `JEST_JUNIT_OUTPUT_DIR`, when the repo uses the `jest-junit` reporter.
3. runs the check, unchanged: its output and exit code are its own;
4. reads the report files the run wrote (newer than its start, regular files, not links) from that directory, from the
   paths the repo declares, and from the usual places: Maven surefire and failsafe and Gradle (`TEST-*.xml`),
   cargo-nextest (`target/nextest/<profile>/junit.xml`), vitest and jest (`*junit*.xml`), `test-results/` and
   `test-reports/`, and coverage: `coverage.xml` (coverage.py, pytest-cov), `cobertura*.xml` (istanbul), `lcov.info`
   and `*.lcov`.

vitest's `junit` reporter, nextest's JUnit output and a `--junitxml` elsewhere aren't set by a variable. Name where
they go with `verify_reports`, in the repo's `.hangar/environment.yaml` (it comes first) or its
[policy](../repo-policy.md):

```yaml
verify: npx vitest run --reporter=junit --outputFile=reports/vitest.xml
verify_reports: [reports/*.xml, "**/coverage/lcov.info"]   # relative to the repo; **/ for any depth
```

`HANGAR_TEST_REPORTS=collect` reads reports without setting any variable; `off` does neither.

**Reports are untrusted.** The repo's code writes them. They are read as the sandbox's user, the same one that ran
the check, and parsed with defusedxml's rules: an entity declaration or an external entity reference stops the parse
(no XXE, no entity expansion), no DTD is fetched, and a report is capped at 8 MiB, 2 million elements, 64 levels deep
and 100,000 test cases (64 files and 32 MiB a run). Messages are clipped and pass through `broker.redact` before they
are kept.

## What is kept

A **test run** per run of a check: the counts (passed, failed, error, skipped), the first 200 failures with their
messages, every case's outcome keyed by test id (compressed, up to 20,000, kept 3 days, and a base check's 14), the
coverage totals, and the commit and tree it ran on. Runs are kept 14 days. A test id is `classname::name`, as the
runner wrote it (`tests.test_math::test_div[0-1]`, `example.com/app/server::TestServe/timeout`). Each run records the
agent run it checked (`agent_run_id`, `hangar.run.id`), so a session's trace puts it on that turn.

**Trusted and counted.** A run is `trusted` when hangar ran it where the agent can't forge it: the base check, the
check after each turn and its rerun, attempts, setup's validation, the acceptance checks, and a grader whose grade is
`trust: isolated`. The in-turn gate's runs are not (the agent's own sandbox wrote them while it worked), nor a grader
run below `isolated`. A run is `counted` when it is trusted, ran at least one test (`total > 0`: `pytest
--collect-only`, or a runner that found nothing, ran none) and every report was read (`complete`). Anything that
asks "did the tests run" (flightdeck's behavior predicates, a review) reads `counted`, never the exit code or
`trusted` alone ([traces.md](../api/traces.md#test-results)).

On a red base, the failures the change adds are the after-turn run's failing ids minus the base run's, exactly,
whenever both left complete reports ([repo-policy.md](../repo-policy.md#the-check-on-the-base-commit)). The text of the
output is the fallback. Reports cover only the runners that write one: when they show nothing new but the output
names a failure the base didn't have (a second runner without a JUnit reporter), hangar can't tell, and the PR goes
out as a draft that says so.

## Flaky tests

A test is **flaky** when, in one repo and environment:

- it passed and failed on the same tree: the working tree's content, uncommitted changes included (two base checks of
  one commit by two people, a verify-fix round that changed nothing, a rerun); or
- the runner retried it and it passed (surefire's and nextest's `flakyFailure`).

Each repo keeps a list of them, each test dropped 30 days after it was last seen flaky. The in-turn gate's runs never
add to it: the agent's own sandbox wrote them while the agent worked. With the repo policy's `rerun_flaky`, a check
whose every failure is on the list (on a red base: every new failure) runs once more, and the rerun decides. The
review packet marks the failing tests that are on the list, and the [Cloud Doctor](../doctor.md) counts a failed check
whose every failure is on it as transient.

The list is advisory. Even hangar's own checks run the repo's code, which the agent may have written, and that code
writes the reports (a `flakyFailure`, a test that passes once): so a job's failures are excused (rerun, "flaky" in its
packet, "transient" for the doctor) only by entries on the list before the job started, never by what its own checks
added. The tree a run is filed under is read in an exec of its own before the check, so the check can't change it.

## Where it shows

- **The job:** `result.tests` on `get_job` and `wait_for_turn`, and on each best-of-N attempt: counts, the first
  failing ids, `new_failures` against the base, `flaky`, line `coverage` and `coverage_delta` against the base.
- **Events:** a `progress` event with `data.action: "tests"` in the job's and the session's events, and a line in the
  job's timeline.
- **The agent:** `TEST-RESULTS.json` in the job's directory, rewritten after each of hangar's runs: every failure
  with its message (up to 2,000), what is new against the base, the base's own failures, the flaky ones and coverage.
  The prompt and each verify-fix message point at it, so the agent searches a file instead of scrolling output.
- **The review packet:** pass, fail and skip counts, new failures against the base, flaky tests, and the coverage
  change against the base.
- **REST:** `GET /api/jobs/{id}/tests` and `GET /api/sessions/{ref}/tests`: the summary, every run, and the latest in
  full. `?run=<id>` gives one run, `&cases=true` every case, and `?format=otel` the runs as spans (below).
  `GET /api/repos/tests/flaky?repo=` is a repo's list (admins). The resource `hangar://jobs/<id>/tests`.
- **`hangar status`:** the repos with the most flaky tests, with their worst.

## In traces

Each run is span events in the trace export's taxonomy (`hangar.trace/v1`, which reserves `test.run` and `test.case`
for them), with OpenTelemetry's
[test semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/test/) (stability:
development) alongside:

- `test.run`: `test.suite.name` (the command), `test.suite.run.status` (`success`, `failure`, `timed_out`,
  `aborted`), and the totals under `hangar.test.*` (`total`, `passed`, `failed`, `errors`, `skipped`, `flaky`,
  `complete`, `coverage.line_rate`, `run.id`, `run.kind`, `exit_code`), with `vcs.ref.head.revision`;
- `test.case`, one per failing or flaky test: `test.case.name`, `test.case.result.status` (`pass` or `fail`),
  `hangar.test.outcome` (`failed` or `error`), `hangar.test.flaky` and the start of `hangar.test.message`. Passing tests
  are counted, not listed.

With tracing on ([observability.md](../observability.md)) they go on the span that ran the check (`job.verify`, the
job's provisioning for the base). `GET /api/jobs/{id}/tests?format=otel` serves them as data,
`testresults.add_to_export` puts them on a job export's `job` span, and a session's export puts each turn's runs (the
in-turn gate's, the check after it, an attempt's) on that turn's span. Every `test.run` carries
`hangar.test.run.trusted` and `hangar.test.run.counted`.

