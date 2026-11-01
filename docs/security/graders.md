# Sealed graders

An experiment's trials must not see the answer, and its grader runs their code beside hidden tests that must not leave or be bent. Part of the [security model](README.md); the [threat model](threat-model.md) lists the rest.

## What a trial can reach

A task mined from a merged PR has its fix in the repo's later refs and on GitHub. So every experiment trial, the trials API's included, gets this (KAN-234):

- **A clone that stops at the base.** Once cloned, the checkout is on the pinned base. It has no remote, no fetch refspec, no other branch or tag, and no object past the base. `git log --all`, `git branch -a` and `git tag` show nothing later.
- **No git grant past the clone.** Its gateway grant then covers no repository, so the gateway refuses every fetch and GitHub API call. Without the gateway, the trial keeps the environment's own credential, and its manifest warns.
- **Closed egress by default.** An environment that sets no `egress.mode` gives an eval job `allowlist`: the model API and the package mirror. No GitHub host gets through, whatever a preset, the repo policy or an approval adds.

An environment that sets its own egress keeps it. When that reaches GitHub, the trial's manifest and the experiment's notes carry a warning: its results may not be valid.

## The grader's sandbox

The grader ([jobs/experiments.md](../jobs/experiments.md#grading)) runs in a sealed sandbox (`delivery.sealed`):

- **Nothing out.** Egress `none` with no host at all: no model API, MCP server, cloud or approved request. It is always on the proxy-only network. A clone without the git gateway reaches the repo's host only until the base and the patch are in; its git grant goes then too. `network: mirror` adds hangar's read-only package mirror.
- **Nothing to take.** No credential, `secrets:` or cloud token, no `mcp:`, `computer:` or services. No build caches, no GitHub API and never a warm pool sandbox.
- **Hidden tests from a pristine copy.** They sit outside the repo, root-owned on Docker. `hangar-grade pytest` runs them with `python -I`, `--noconftest`, `-c /dev/null` and no cache. The repo joins `sys.path` only after pytest loads its plugins. Files that could hook a test run are recorded as `tamper_signals`.
- **The grade is hangar's** where the sandbox has the uid split (Docker, `sudo: false`). A wrapper running as hangar's user starts the command as the agent, with no capabilities. It reads the exit code, `$HANGAR_GRADE_OUT` and the junit from pipes it made, and writes the grade where the agent can't (`trust: isolated`).
- **Detectors.** Hidden tests that pass on the base, or a reference patch that fails them, make the task `invalid_task`. A canary in every hidden test file, seen in a trial's output, makes it `contaminated`. `touched_tests` lists existing tests the trial changed.

Not covered: code under test runs inside the test runner's process, so it can bend the runner and lie on the pipe. The detectors and fresh hidden tests are the defence. Without the uid split (GKE, or `sudo: true`) the trial's code could write `$HANGAR_GRADE_OUT` (`trust: best_effort`). Non-Python graders get no safe runner (`trust: low`). Tests: `test_trialiso.py`, `test_sealed_grader.py`, `test_grade_forgery.py`, `test_e2e_sealed_docker.py`.
