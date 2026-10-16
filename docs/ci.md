# CI: a fast check before the merge, everything after it

Pull requests get a fast check of what the change can break. Main gets the whole suite on every commit, tracks flaky
tests, and when it goes red, hands the bisect and the repair to a hangar job. Two workflows do this:

| Workflow | When | What | Wall time |
| --- | --- | --- | --- |
| `.github/workflows/pr.yml` | every pull request | ruff; the selected tests in 4 shards; the Docker smoke suite, Rust and terraform when their paths change | about 40 s for a one-module change, about 50 s for the whole suite (the required `test` check) |
| `.github/workflows/ci.yml` | every push to main; `workflow_dispatch` on any branch | ruff; every test in 4 shards; all 12 Docker e2e suites; envd's clippy and tests; the image build; terraform; flake report; bisect hook | about 2 min |
| `.github/workflows/harness-contract.yml` | every PR (the `contract` job is skipped, and passes, unless the PR touches harness, driver or isolation code); every push to main | the real agent CLIs at the locked versions, offline, and the recorded protocols ([harnesses.md](harnesses.md#the-contract-tests)) | about 1 min with the CLIs cached |
| `.github/workflows/harness-canary.yml` | nightly | the newest upstream CLIs against the same tests; a lock-bump PR or an issue ([harnesses.md](harnesses.md#the-canary)) | a few minutes |

Before this, one job ran the 734 unit tests one after another (5 min 24 s), one of the 12 Docker suites ran, and envd
wasn't built or tested at all. Both PRs and main took about 5.5 minutes.

## Pre-merge (`pr.yml`)

- **`lint`**: `ruff check src tests scripts/ci`.
- **`unit (1/4)` … `unit (4/4)`**: each shard runs `scripts/ci/select_tests.py --base HEAD^1 --shard I/4`, then
  `pytest -n 8` on its share. `HEAD^1` is the base branch the PR merges into, so the diff is exactly the PR's. When
  nothing is selected (a docs change), the shard stops before installing anything. If envd/ is unchanged, the shards
  restore main's hangar-envd binary from the Actions cache, so the tests that drive it run too.
- **`test`**: passes when `lint` and every shard passed. It is the required check.
- **`changes`**, then, only when needed:
  - **`e2e-docker`**: `tests/test_e2e_docker.py` when the change reaches any Docker e2e suite. Main runs all 12.
  - **`rust`**: when `envd/**` changes. Runs clippy (`-D warnings`), `cargo test --locked`, builds the binary, and
    runs the Python tests that drive it.
  - **`terraform`**: when `deploy/terraform/**` changes. Runs `init -backend=false`, `validate` and `fmt -check`.

  A job skipped by its path filter counts as passed, so `e2e-docker` and `terraform` stay usable as required checks.

There are no reruns before the merge: a test that fails, fails the PR.

## Test selection

`scripts/ci/select_tests.py` builds a static map from imports with `ast`. Nothing is imported or run.

- **A changed hangar module** selects the test files that import it, and those that import a module importing it
  (one hop). Imports inside functions count. hangar's modules import each other in one large cycle, so following
  imports all the way selects the whole suite for any change. One hop is the useful middle.
- **Test helper modules** count as part of the tests that import them. For example, many files import
  `tests/test_jobs.py` for its fixtures.
- **Tests that read every module's source** (they build `"src" / "hangar"`, like `test_sweep_caps.py`) run for any
  hangar change. That includes `test_scope_parity.py`, so a change to `mcp_server.py`, `app.py` or any module that
  mounts routes runs the REST/MCP scope check ([security.md](security.md#scopes)).
- **Non-Python paths** map to the tests that read them, through a table in the script: `envs/`, `envd/`,
  `scripts/acceptance/`, `deploy/` and `docs/gke.md`. Docs, `*.md`, the Dockerfile and terraform select no tests.
- **The whole suite runs** for any of these:
  - a core module: anything `tests/conftest.py` imports (`fleet`, `models`, `config`, `store`, …), plus the hubs listed
    in `CORE`;
  - `tests/conftest.py` or `tests/fixtures/`;
  - `pyproject.toml` or `uv.lock`;
  - `.github/workflows/` or `scripts/ci/`;
  - a path the table doesn't know;
  - a module no test reaches;
  - more than 12 changed modules.

  The hubs in `CORE` are the modules whose one-hop selection was already over 60% of the suite's time.

What it chose for some sample diffs:

| Change | Selected | Share of the suite's time |
| --- | --- | --- |
| `src/hangar/slack.py` | 6 files: slack, front door security, interactions, autofix, setup wizards, sweep caps | 22% |
| `src/hangar/providers/e2b.py` | 4 files | 6% |
| `src/hangar/sandbox_router.py` | 3 files | 8% |
| `src/hangar/triggers.py` | 4 files | 16% |
| `src/hangar/cli.py` | 12 files | 20% |
| `src/hangar/cache.py` | 9 files, 3 e2e suites | 30% |
| `tests/test_jobs.py` | 20 files: it, and the tests that import its fixtures | 60% |
| `envd/src/main.rs` | 7 files and 6 e2e suites, plus the `rust` job | 15% |
| `docs/…`, `README.md`, `deploy/terraform/…` | nothing (terraform has its own job) | 0 |
| `src/hangar/fleet.py`, `tiers.py` (no test reaches it), `tests/conftest.py`, `uv.lock` | everything | 100% |

On CI, the slack.py change took 41 s from push to a green `test` check. A change that selects everything took 50 s.

The selection is a bet, not a proof. A change can break a test that is two imports away. Main's full run catches
that within minutes of the merge, and the bisect hook finds and repairs it.

### Run the same thing locally

```bash
scripts/ci/select_tests.py --base origin/main                  # the files, one per line; why goes to stderr
uv run pytest -n 8 $(scripts/ci/select_tests.py --base origin/main)
scripts/ci/select_tests.py --base origin/main --shard 2/4      # CI's second shard
scripts/ci/select_tests.py --files src/hangar/slack.py         # for given paths instead of your diff
scripts/ci/select_tests.py --base origin/main --e2e            # the Docker suites your change reaches
```

`--base` compares against the merge base, and also counts uncommitted and untracked files.

## Shards

Shards split whole test files using recorded durations (`scripts/ci/durations.json`). Files go longest first into the
least-loaded shard. Inside a shard, xdist balances single tests across 8 workers. The workers mostly wait on git, tmux
and subprocesses, so 8 beat 4 on a 4-vCPU runner.

Why not `pytest-split`:

- Splitting by file keeps each module's fixtures and helpers in one shard.
- The shard's argument list is plain file paths, which the selection already produces.
- No longest file (about 57 s on CI, under xdist) comes near a shard's share (about 140 s), so balance stays within one
  file.

To rebalance, copy the `durations` artifact from a recent main run over `scripts/ci/durations.json`. A new test file
counts at the median duration until then.

## Post-merge (`ci.yml`)

- **`envd`** builds hangar-envd for x86_64 once (`envd/build.sh`) and uploads it as the `envd` artifact. The build is
  cached by the hash of `envd/**`: about 10 s when the cache hits, about 85 s when it misses.
- **`unit (I/4)`** runs every test, sharded, with the envd binary.
- **`e2e (<suite>)`** runs each Docker suite in its own job: the 11 `test_e2e_*_docker.py` files plus the e2e part of
  `test_acceptance_script.py`. The list comes from the selection script, so a new suite joins the matrix
  automatically. `test_e2e_kata.py` needs a KVM host with Kata, so it isn't included.
- **`rust`** runs `cargo clippy --locked --all-targets -- -D warnings` and `cargo test --locked`, with
  `Swatinem/rust-cache`.
- **`image`** builds the control-plane image with buildx and the GHA layer cache, and doesn't push it. A cold build is
  about 9 minutes (envd for two CPUs with fat LTO, gcloud). A warm one takes 20 s, plus the Python layer when `src/`
  changes.
- **`terraform`** validates both stacks.
- **`report`** merges the per-job reports, uploads the `flakes` and `durations` artifacts, and runs the bisect hook
  when anything failed.

One full run goes at a time, and none is cancelled. Pushes that land during a run wait, and only the newest of them
runs next. The bisect range from the last green run covers the commits in between. A full run uses up to 22 jobs at
once. On a plan with 20 concurrent jobs, a PR opened during one waits for runners. To leave room, set `max-parallel`
on the e2e matrix, at the cost of main's time.

`workflow_dispatch` runs the same on any branch. There, the bisect hook only prints what it would do.
`bisect_dry_run: true` runs it even when everything passes:

```bash
gh api -X POST repos/Sean-Koval/hangar/actions/workflows/ci.yml/dispatches -f ref=<branch> -F 'inputs[bisect_dry_run]=true'
```

## Flakes

Main runs pytest with `--reruns 1` (pytest-rerunfailures). `scripts/ci/flake_report.py`, a pytest plugin, writes each
job's reruns as JSON: the test, its first error, and whether the rerun passed (a flake) or failed again (a real
failure). The `report` job collects them into the `flakes` artifact and prints them. Across runs:

```bash
scripts/ci/flakes.py                  # the last 30 runs of ci.yml on main, most flaky first
scripts/ci/flakes.py --runs 100 --json
```

A flake still passes the run. Fix it, not the rerun count. Pull requests never rerun.

## Release gate

A P1 release needs a green main. Before tagging one, check both:

1. **The last 10 `ci.yml` runs on main passed.** A run still in progress doesn't count, either way:

   ```bash
   gh api 'repos/Sean-Koval/hangar/actions/workflows/ci.yml/runs?branch=main&status=completed&per_page=10' \
     --jq '[.workflow_runs[].conclusion] | (map(select(. == "success")) | length | tostring) + "/10 green"'
   ```

2. **No test "failed twice" in the last 30 runs.** `scripts/ci/flakes.py --runs 30` prints every rerun test with
   its `flaked` and `failed twice` counts. A test that failed twice turned a run red. Fix it before releasing (or
   show that the failure was in a commit that has since been reverted). A test that only flaked doesn't block the
   release, but open an issue for it.

If either check fails, fix the cause and wait for 10 green runs again. Don't rerun failed jobs to make the count.
Re-running only repeats the same commit, and it hides the flake from the next check.

The first gate found two flaky tests (#118, #120): the background-process tests in `tests/test_streams.py` attached
for one fixed second and expected the output to be there. On a loaded runner, the process's first output reached
hangar later than that, and the attach correctly returned nothing. The tests now attach again from `next` until the
output arrives, within 10 s.

## Bisect and repair

When any job on main fails, `scripts/ci/bisect_hook.py` collects three facts:

- the failing test ids, from the junit XML (final outcomes, so a test that passed its rerun doesn't count);
- the failed jobs;
- the last green commit, from the newest successful `ci.yml` run on main that is older than this one.

From them it builds one check command that fails the way the run did: `uv run pytest -x` on the failing unit tests,
the same with `HANGAR_E2E=1` for failing e2e tests, and each failed non-pytest job's own command (clippy and cargo
test, the envd build, `docker build`, terraform validate, ruff).

What happens next depends on the repo secrets:

- **`HANGAR_URL` and `HANGAR_TOKEN` are set** (an admin token, or a `jobs` token whose `HANGAR_GITHUB_REPOS` maps this
  repo): the hook submits a hangar job (`POST /api/jobs`, [jobs.md](jobs.md)) with `deliver: pr` and the failing
  tests as `verify`.
  - The agent runs `git bisect run` between the two commits with that check. It then fixes the culprit, with a
    regression test, or reverts it, and opens a PR. The check is also the job's `verify`.
  - The idempotency key is the run id, so re-running the failed jobs doesn't start a second bisect.
  - Optional: `HANGAR_ENVIRONMENT` picks the environment.
- **The secrets aren't set, or hangar can't be reached**: the hook opens a "CI: main is red" issue with the same
  instructions. If that issue is already open, it adds a comment instead.

The instructions are built from the run's facts and fixed text only: no commit messages or test output.

## Test isolation

These changes make the suite safe under xdist, and next to other runs on the same machine:

- **Scratch directory.** Each pytest process gets its own directory under `HANGAR_TEST_ROOT` (default
  `~/.cache/hangar-tests`), and removes it when the session ends. Concurrent runs used to collide in the shared one.
  Fake sandboxes, their tmux sockets (`TMUX_TMPDIR` per sandbox) and test git remotes all live in it.
- **Ports.** `tests.conftest.free_port()` hands out ports from a block of 256 per xdist worker. The blocks sit below
  the kernel's ephemeral range, so an outgoing connection, or another worker binding port 0, can't take a port before
  the test server binds it. The e2b code-interpreter test no longer needs the fixed port 49999.
- **Docker.** Unit tests never reach the machine's Docker. Without `HANGAR_E2E=1`, `DOCKER_HOST` points nowhere, so a
  test that builds a real app behaves as it does on a laptop without Docker. One such test built a real image on CI
  and took 46 s.

With these changes, the suite passed nine full runs on a 2-vCPU machine without a failure:

- five with `-n 8`;
- two with `-n auto`;
- two with `-n 4` at the same time, sharing one `HANGAR_TEST_ROOT`.

CI's first full runs found two more races, both fixed in the tests:

- an upload cut short by a 413 is cleaned up by envd only after the router answers;
- a pty can echo `^@` ahead of a command's output.
