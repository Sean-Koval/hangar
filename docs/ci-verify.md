# Verify in a job: hangar's own check, fast and green in a sandbox

A hangar job on this repo runs `bash scripts/ci/verify.sh`, the `verify:` in `.hangar/environment.yaml`. It has to
finish in a few minutes on a 2-vCPU sandbox, with no Docker daemon, and fail only for what the change broke. CI runs
everything after it ([ci.md](ci.md)).

| Step | What it does |
| --- | --- |
| Lint | `ruff` on the Python files the branch changed. |
| Select | `select_tests.py --base origin/main --budget 600`: the tests the change can break, capped. |
| Run | `pytest -n 4` on them. A test whose capability is missing skips. |
| Compare | Failures run again on the base commit. Only new ones fail verify. |

`bash scripts/ci/verify.sh --full` runs every test, with no selection and no cap. `VERIFY_BUDGET` and
`VERIFY_WORKERS` change the cap and the workers.

## The cap

The selection is the one pre-merge CI makes, then held to a budget. The budget is seconds of recorded CI time, from
`scripts/ci/durations.json`. In a 2-vCPU sandbox, 600 s of CI time runs in about 3 minutes.

Under the budget, verify runs everything selected. Over it, it keeps, in order:

1. the API and contract tests (`CONTRACT`), when a hub module changed or CI would run everything;
2. the tests a change names: a changed test file, `tests/test_<module>.py`, `tests/test_ci.py` for `scripts/ci/`;
3. the tests that import a changed module directly, cheapest first, while they fit;
4. the rest of the selection, cheapest first, while they fit.

A change to `app.py`, `config.py` or `models.py` used to select the whole suite: about 2,900 tests and 30 minutes in
a sandbox. Capped, it runs about 1,300 tests in under 3 minutes. The rest run on the pull request and on main.

## Capabilities

Some tests need what a sandbox lacks. They say so with a marker, and `tests/conftest.py` checks the machine once:

| Marker | Present when | Gates |
| --- | --- | --- |
| `needs("docker")` | a Docker daemon answers on `DOCKER_HOST` | the Docker e2e suites, which also need `HANGAR_E2E=1` |
| `needs("kvm")` | `/dev/kvm` is usable | `test_e2e_kata.py`, which also needs `HANGAR_E2E_KATA=1` |
| `needs("uid_split")` | not root, or root with `setpriv` and a scratch root other users can reach | `test_driver`'s run-key test |

A missing capability skips the test, with a reason that starts `capability:`. CI sets `HANGAR_REQUIRE_CAPS=1`, which
fails the test instead. So a test CI should run can't quietly start skipping there.

The run-key test drops to uid 65534 when it runs as root. Under `/root`, a 0700 home, that uid can't reach the
scratch root. Set `HANGAR_TEST_ROOT=/dev/shm/hangar-tests` to run it as root.

## Fake sandboxes in a real one

The fake provider maps `/home/agent`, `/workspace` and `/tmp/` into a temporary box. In a hangar sandbox the checkout
is `/workspace/hangar` and Python lives under `/home/agent`. Mapping those too broke the scripted harnesses, about 40
tests. The provider now leaves the checkout, its venv and the interpreter where they are (`HOST_PATHS`).

## The baseline

When tests fail, `scripts/ci/verify_baseline.py` reads their ids from the run's junit XML. It runs them again in a
scratch worktree of the merge base, with the same venv and the base's sources first on the path.

- A failure that also fails on the base is pre-existing: verify prints it and passes.
- A failure that passes, skips or doesn't exist on the base is new: verify fails and names it.
- The base's outcomes are cached per commit, in `.git/hangar-verify/<sha>.json`.

hangar's own job baseline can't do this here. It runs the check on the base commit before the agent starts. On the
base, verify.sh selects nothing and passes, so there is nothing to subtract.
