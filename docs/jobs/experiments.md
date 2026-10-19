# Experiments

Trials, grading, shadows, the monthly eval budget and the eval queue. Part of [jobs](README.md).

An experiment runs the same tasks under several configurations, each several times, so a tool outside hangar can
compare them. It is a job of kind `experiment`; each trial is an ordinary job of its own (`source: experiment`,
`deliver: none`).

**Trials and pin sets (#178).** The unit underneath is the trial against an immutable **pin set**: the harness lock and
its digest, the environment's digests, the concrete model, each task's base commit, the grader's digest and the egress
policy, content-addressed (`pin-<hash>`) and never changed. `POST /api/trials {pin_set, task, n, budget}` runs n trials
of one task against one pin set, and a trial whose turn 1 didn't run what was pinned ends as `infra_error` / `drift`
(not retried). Flightdeck decides how many trials to run; the batch experiment below is a convenience on top
(`trials` and `parallel` are its wrapper fields), and it records a pin set per variant (`pins.variants.<name>.pin_set`,
and on each trial). [api/trials.md](../api/trials.md) has the API.

```yaml
# skills-ab.yaml
name: skills-ab
environment: hangar
tasks:
  - {id: fix-retry, instructions: "Make the reaper retry a failed destroy.",
     repo: https://github.com/Sean-Koval/hangar.git, verify: "uv run pytest -q tests/test_fleet.py"}
variants:
  team: {harness_lock: "team-default@sha256:5f0c…"}     # a harness lock, by digest (#135)
  team-codex: {harness_lock: "team-codex@sha256:9a41…", environment: hangar-codex}
trials: 3
trial_job_budget: {max_cost_usd: 3}
budget: {max_cost_usd: 40}
```

```bash
hangar experiment run skills-ab.yaml --wait   # or POST /api/experiments
hangar experiment get job-1a2b3c4d             # the index: pins, and per trial its job, verify, grade
hangar experiment export job-1a2b3c4d -o out/  # index.json and each trial's manifest.json and trace.json
```

**Variants are harness locks.** A variant is `{harness_lock: name@sha256:…, environment}`: a lock in the harness
registry ([harness-lock.md](../harness-lock.md)), named by digest so nothing can move it between two trials. Every trial
is claimed from it as `hangar harness thaw` claims a job: the lock's CLI version (refused at submit when
`harnesses.lock.yaml` pins another), its concrete model and approvals, and its instructions, skills and plugins
mounted read-only. `pins.variants.<name>.harness_digest` is the lock's digest, and each trial's turn-1
`harness_digest` must equal it: a mismatch is **harness drift** on the trial. A variant with `harness_lock` takes no
overlay field (`harness`, `model`, `approvals`, `skills`, `skills_remove`, `instructions`, `instructions_append`):
mixing them is a 422. The experiment's own `harness` and `model` defaults don't apply to it.

**The overlay is deprecated.** Variants written as an overlay still run (`{}`, `{skills: []}`,
`{skills_remove: [repo-conventions]}`, `{harness: codex, model: balanced}`, `instructions_append`, ...), with a note
per variant in the experiment's `notes`; the fields are marked `deprecated` in
[experiment.schema.json](../api/experiment.schema.json) and go in the release after flightdeck v0 ships its first
report. Their `pins.variants.<name>.harness_digest` is the first finished trial's. To move an experiment over:

```bash
hangar experiment freeze-variants job-1a2b3c4d --out ~/team-harness/harnesses -o locked.yaml
# freezes each overlay variant from a completed trial's turn 1 into harnesses/<experiment>-<variant>/
# (as `hangar harness freeze`), and writes the same spec with harness_lock variants; commit the directories
# to the harness registry (HANGAR_HARNESS_REPO), then `hangar experiment run locked.yaml`
```

**Concrete model ids only.** A variant's model must be a concrete, dated id once its policy is resolved: a floating
alias (`claude-opus-latest`, `opus`, `sonnet`, `gpt-5`, OpenRouter's `~…-latest`) can change what it means between two
trials, so the experiment is refused at submit (and again when it starts), naming the alias and the concrete ids the
policies resolve to now. An environment's own alias counts too. `model: balanced` (or `frontier`) is fine: it is
resolved once and every trial gets that id, recorded in `pins.variants.<name>.model` with `model_concrete` and
`model_concrete_basis` (`list`: in the provider's cached model list; `pattern`: no list was cached, so its versioned
shape decided, and the experiment's notes say so). Ordinary jobs still accept aliases; their manifests say
`model.concrete: false` ([harnesses.md](../harnesses.md#concrete-ids-and-floating-aliases)).

hangar pins the harness version, the model id and each task's base commit, mounts only the trial's harness, gives it
exactly the variant's skills and instructions (the lock's, or the overlay's), and keeps egress the same for every
variant. It records what each
trial ran with (its [manifest](../api/manifest.schema.json)), what it did (its [trace](../api/traces.md)) and its
verification, and runs an optional `grader.command` in a clean sandbox on each trial's patch. Two trials of one
variant should share a configuration; when they don't, the trial's `drift` says which half moved: **harness drift**
(its manifest's `harness_digest`: instructions, skills, MCP servers, hooks, model, approvals) or **environment drift**
(`environment_digest`: image, build, egress, resources, isolation). It doesn't compare the
variants: that is the sister tool's job. The details are in [spec 0014](../specs/0014-sister-tool-interfaces.md#4-experiment-jobs-hangarexperimentv1).

## Grading

```yaml
grader:
  command: hangar-grade pytest -q       # the hidden tests, from their pristine copy
  files: {"test_retry.py": "..."}       # written to $HANGAR_HIDDEN_TESTS, outside the repo
  network: none                         # or mirror: hangar's read-only package mirror, for a grader that installs
  # files_mode: in_repo                 # the old layout (files in the repo); the grade says trust: low
  # canary: HANGAR-CANARY-...           # generated per experiment if unset; never reuse one
tasks:
  - {id: fix-retry, ..., reference_patch: "diff --git ..."}   # optional: a known fix the hidden tests must pass
```

The grader runs agent-written code beside the hidden tests, so its sandbox is sealed: no credential, `secrets:` or
cloud token, a fetch-only git grant that is revoked before the command runs, none of the environment's `mcp:`,
`computer:` or services, and egress `none` with no host at all, not even the harness's model API, on the proxy-only
network whatever the environment's egress says ([security](../security/graders.md)). A dependency that isn't in the
image (the environment's, or its repo build's) can't be fetched: use `network: mirror` or a repo build.

The command runs in the repo with the trial's patch applied, and gets `HANGAR_HIDDEN_TESTS` (the grader's `files`,
outside the repo; on Docker root-owned, so the trial's code can read them and not change them), `HANGAR_REPO`,
`HANGAR_GRADE_OUT`, `HANGAR_TRIAL`, `HANGAR_TASK`, `HANGAR_VARIANT` and `HANGAR_EXPERIMENT`.

`hangar-grade pytest [args]` runs the hidden tests so that nothing the patch added loads first: `python -I` (no
`sitecustomize.py`, `usercustomize.py`, user site or `PYTHON*` variables), `--noconftest`, `-c /dev/null` and
`addopts` cleared (no repo `conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg` or `pyproject` settings), no cache, no
bytecode, and the repo on `sys.path` only once pytest has started (a plugin the patch declares never loads). A
`conftest.py` among the hidden tests still works (its fixtures and hooks; not options it adds). Its junit counts are
stored as `grade.hidden_tests`.

**The grader's test run** (#164). The junit `$HANGAR_GRADE_JUNIT` receives (what `hangar-grade` writes; another
command can write JUnit XML there too) is recorded as a [test run](test-results.md) of kind `grader` on the trial's
job, through the same parser and size caps as the check after a turn: it shows in `GET /api/jobs/{trial job}/tests`,
the trial's stored trace (`GET /api/experiments/{id}/trials/{key}/trace`, rewritten once graded) and the outbox's
`grade.json`, and `grade.test_run` names it. A grader that writes no report gets a run from its exit code alone,
`complete: false`. It is `trusted` only when the grade is `trust: isolated`; below that the code under test could
have written the report. Hidden test ids and messages pass the broker's redaction and the canary mask, and never
reach the repo's flaky list (which the agent reads in `TEST-RESULTS.json`).

Other ecosystems get no safe runner yet; run them from a copy you control and treat the grade as best effort: `jest
--config "$HANGAR_HIDDEN_TESTS/jest.config.js" --rootDir "$HANGAR_REPO"` with a config of your own, or copy the
hidden `_test.go` files into a fresh copy of the package before `go test`.

Each grade records `files_mode`, `network`, `runner` (`hangar-grade` or `command`) and `tamper_signals`: files the
patch adds or changes that can hook a test run (`conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth`,
`pytest.ini`, a pytest section in `tox.ini`, `setup.cfg` or `pyproject.toml`). They are signals, not refusals.

**Who writes the grade.** On Docker, where the sandbox has the [uid split](../steering.md#the-uid-split), a wrapper
running as hangar's own user starts the command as the agent's user (no capabilities, `no_new_privs`) and writes the
grade itself, in a 0700 directory the agent can't reach: the exit code, the output, `$HANGAR_GRADE_OUT` and the junit
`hangar-grade` writes come to it through pipes it created (`$HANGAR_GRADE_OUT` is `/proc/self/fd/198`: write it, don't
look for a file). A sandbox where the agent has `sudo` (the environment's default) can't be split.

**Trust.** Each grade, and each trial in the index, says how far it can be trusted:

| `trust` | When |
| --- | --- |
| `isolated` | The uid split, the pristine copy and `hangar-grade` |
| `best_effort` | No uid split (GKE today, or an environment with `sudo`): the trial's code could write `$HANGAR_GRADE_OUT` |
| `low` | `files_mode: in_repo`, or a command that isn't `hangar-grade` itself |

Code under test runs inside the test runner's process, so it can still bend the runner while it runs (patch pytest's
reporting from the module under test). Isolation stops writes to the grade, not lies through the runner. These catch
what it doesn't:

- **Base must fail.** Once per task per experiment, the hidden tests run on the pinned base without a patch. If they
  pass there the task can't tell a fix from no fix: `task_checks.<task>.invalid` with `hidden tests pass on base`, and
  every grade of the task says `task_invalid`. With `reference_patch` they also run on it, and must pass. One grader
  run per task, not per trial.
- **Canaries.** hangar writes the grader's canary as a comment line into every hidden test file that takes comments
  (`canary_files` counts them). After each trial it looks for it in the trial's patch, its `RESULT.md`, its trace
  export and the egress proxy's records of its sandbox. A hit makes the trial `contaminated` (with `contamination`
  saying where), adds a note to the experiment and a `grade.canary_seen` audit entry. The canary is kept only in the
  experiment's spec, and is masked in every export.
- **Touched tests.** `touched_tests`: existing test files the trial changed (the job's own check, else from its
  patch).

## Shadows

A repo policy's `shadow` (#183, [repo-policy.md](../repo-policy.md#shadows)) samples real intake jobs and runs a candidate
harness on each at the same base commit: a `source: shadow` trial (`shadow_of` names the real job) that delivers
nothing, writes back nowhere and runs with egress `none`, for flightdeck to grade against what was merged.

## The monthly eval budget

Trials count against the team's monthly eval budget and their author's cap (`HANGAR_EVAL_BUDGET_USD_MONTH`, 1500;
`HANGAR_EVAL_AUTHOR_CAP_USD_MONTH`, 400). An experiment projected over what is left is refused at submit with the
numbers. Once a budget is spent, running trials finish but no new trial starts. `hangar eval budget` shows the month
([repo-policy.md](../repo-policy.md#the-eval-budget)).

## Infrastructure errors are retried and left out

A trial that infrastructure ended (its node was preempted, the 01:00 power-down took its sandbox, the provider
rate-limited it past its harness's retries or answered 5xx, its sandbox couldn't be created, a control-plane restart
cut its turn off) is `outcome_class: infra_error` with an `infra_reason` ([outcome classes](reference.md#outcome-classes)). It is
`excluded`, and runs again as a fresh trial with the same `key`, the next `attempt` and `retry_of` naming the excluded
trial's job, at most `HANGAR_TRIAL_INFRA_RETRIES` (2) times. A trial whose job would resume from a checkpoint (after a
restart or at power-up) is ended instead: a resumed trial didn't run under the same conditions as the others. The
retries of one experiment stop at 10% of its trials (at least one); past that no further trial starts, with a note,
since that many at once usually means a provider or cluster outage. In the index, the highest `attempt` of a key is
the one that counts; `GET /api/experiments/{id}/trials/{key}/manifest?attempt=N` (and `.../trace`) reads another one.

## The eval queue

Trials are `queue_class: eval` jobs ([team-scale.md](../team-scale.md#the-eval-queue-class)): they start only in the eval
window (`HANGAR_EVAL_WINDOW`, `20:00-00:15` New York time), behind every interactive job, in at most half the job slots,
and under the provider's token budget (`HANGAR_EVAL_TPM_BUDGET`). An experiment submitted at 14:00 starts its trials at
20:00; meanwhile they show `reason: eval_window`. `parallel` still caps the trials one experiment has submitted at once.
At the power-down drain, running trials are cancelled as `infra_error` / `power_down` and retried in the next window
(above); those retries don't count towards the 10% cap, since the power-down is scheduled, not an outage.

