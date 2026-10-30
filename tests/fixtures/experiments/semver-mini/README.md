# semver-mini

A minimal graded experiment task for hangar's own tests: five hidden cases from the semver smoke task, which lives in
flightdeck (`tasks/smoke/semver/`, 116 cases). hangar keeps this copy so `experiments.py` has a realistic grader to
test against without depending on flightdeck.

| File | What it is |
| --- | --- |
| `task.md` | The task's instructions |
| `hidden_test_semver.py` | Five unittest cases; expected values are node-semver 7.8.5's |
| `grade.sh` | The grader: copies only the agent's `semver.py` beside the hidden tests in a scratch directory, runs them, writes `{"passed", "total", "failed"}` to `$HANGAR_GRADE_OUT`, and exits 0 only when all five pass |
| `solution/semver.py` | A reference `semver.py` that passes all five |

As an experiment's grader: `files` holds `grade.sh` and `hidden_test_semver.py` (they land in `$HANGAR_HIDDEN_TESTS`),
and `command` is `bash "$HANGAR_HIDDEN_TESTS/grade.sh"`. `tests/test_interfaces.py` runs it end to end on the fake
provider: a solving and a breaking variant, and the base check, where the tests fail because there is no `semver.py`.
