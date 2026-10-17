# Trials and pin sets (`hangar.pinset/v1`)

The seam between hangar and flightdeck is the **trial against an immutable pin set**. Flightdeck owns the sampling
policy (how many trials of which task, against which pin sets, when to stop); hangar runs each trial exactly as pinned
and says when it couldn't. A baseline pinned today re-runs identically next month: the same base commit, harness lock
and model.

## Pin sets

A pin set records what a trial runs with. Once created it never changes: its id is the hash of its content, the store
refuses to update or delete a row (SQLite triggers), and there is no `PUT` or `DELETE`.

| Field | Meaning |
| --- | --- |
| `id` | `pin-<sha256 of the canonical body>[:16]`: the same content always gives the same id |
| `harness`, `harness_version` | The harness and the CLI version its lock pins (`harnesses.lock.yaml`) |
| `harness_lock`, `harness_digest` | The lock (`name@sha256:...`, [harness-lock.md](../harness-lock.md)) and its digest. It fixes the CLI version, the concrete model, approvals, instructions, skills and plugins |
| `environment`, `environment_spec_digest` | The environment and its spec's digest, as a turn's manifest records it (`environment.spec_digest`) |
| `environment_digest` | Optional: a turn manifest's `environment_digest` (image, build, effective egress, resources, isolation), for a stricter pin |
| `model_id` | The concrete model id (#167); null when the harness runs its own default |
| `approvals` | The approval policy the lock binds |
| `tasks` | `{task_id: {repo, base_branch, base_sha}}`: each task's base commit |
| `grader_digest` | The grader's identity: command, files, mode, network and timeout (not the per-experiment canary) |
| `egress_digest` | The environment's egress policy |
| `knowledge_snapshot` | Optional: a retrieval snapshot id, once a retrieval tool exists |
| `overlay_digest` | Only on pin sets an overlay experiment records ([jobs.md](../jobs.md#9-experiments)) |
| `created_by`, `created_at` | The first creator; not part of the id |

```bash
# a proposed pin set from what hangar runs now: the lock's digest, model and approvals, the environment's digests,
# each task's base commit (its own, else its branch head on GitHub)
curl -s -X POST $HANGAR/api/pinsets/resolve -H "Authorization: Bearer $TOKEN" -d '{
  "harness_lock": "team@sha256:5f0c...", "environment": "hangar",
  "tasks": {"fix-retry": {"repo": "https://github.com/Sean-Koval/hangar.git"}}}'
# store it (create, or return the existing one with that content)
curl -s -X POST $HANGAR/api/pinsets -H "Authorization: Bearer $TOKEN" -d @pinset.json
curl -s $HANGAR/api/pinsets/pin-3f9c0a1b2d4e5f60 -H "Authorization: Bearer $TOKEN"
```

`POST /api/pinsets` refuses (422):

- a pin set without a harness lock and its digest;
- a lock whose digest, harness or model doesn't match the body, or that can't be claimed in the environment (the CLI
  version hangar runs has moved on, or the environment lacks an MCP server the lock binds);
- a floating model alias (#167);
- a task without a `base_sha`.

## Trials

```bash
curl -s -X POST $HANGAR/api/trials -H "Authorization: Bearer $TOKEN" -d '{
  "pin_set": "pin-3f9c0a1b2d4e5f60",
  "task": {"id": "fix-retry", "instructions": "Make the reaper retry a failed destroy.",
           "verify": "uv run pytest -q tests/test_fleet.py"},
  "n": 3, "source": "experiment", "budget": {"max_cost_usd": 3}}'
# {"experiment": "job-1a2b3c4d", "pin_set": "pin-3f9c0a1b2d4e5f60",
#  "trial_ids": ["job-1a2b3c4d.1", "job-1a2b3c4d.2", "job-1a2b3c4d.3"]}
curl -s $HANGAR/api/trials/job-1a2b3c4d.2 -H "Authorization: Bearer $TOKEN"
```

| Request field | Meaning |
| --- | --- |
| `pin_set` | The pin set to run against |
| `task` | The task, as an experiment's task. Its `id` must be one of the pin set's tasks; `repo`, `base_branch` and `base_sha` default to, and must equal, the pin set's |
| `n` | Trials (1-20) |
| `source` | `experiment` (the default) or `shadow` (#183) |
| `budget` | Required: each trial's job budget (`max_cost_usd`, `max_tokens`, `max_turns`). The team's monthly eval budget is #182 |
| `turn_budget`, `timeout_minutes` | Optional: each agent turn's budget, and its timeout |
| `grader` | Optional, as an experiment's grader. Its digest must be the pin set's `grader_digest` |
| `variant`, `name` | Labels: the variant's name in the index (default: the pin set's id), the experiment's name |

**How they run.** Hangar makes one experiment job for the request, whose only variant is the pin set
(`{harness_lock}`). Its trials run as experiment trials do:

- each is an ordinary job (`deliver: none`) claimed from the lock, checked out at the pinned base commit;
- each is in the eval queue class: the evening window, behind interactive work, under the token budget
  ([team-scale.md](../team-scale.md#the-eval-queue-class));
- graded, when there is a grader, in a clean sealed sandbox;
- an infrastructure error is excluded and retried (#166).

**Drift.** When a trial's turn 1 has its manifest, its `harness_digest` and `environment.spec_digest` (and
`environment_digest`, when the pin set has one) must be the pin set's. A trial that drifted is cancelled as
`outcome_class: infra_error`, `infra_reason: drift`, `excluded`, and is **not** retried: running it again wouldn't run
what was pinned either. Resolve a new pin set on what hangar runs now.

**Re-running an old pin set.** The pinned base commit is checked out even after the branch moved on, and the lock is
claimed by digest from the registry's history. If hangar no longer runs the lock's CLI version (its
`harnesses.lock.yaml` moved on and the old pack can't be claimed), the request is refused with that reason.

`GET /api/trials/{id}` returns the trial that counts (the last attempt under its key):

- `pin_set`, `state`, `outcome`, `reason`, `outcome_class`, `infra_reason`, `excluded`, `attempt`, `attempts`;
- its `job` and `session`, the turn manifests' digests (`manifests`, `harness_digest`, `environment_digest`,
  `config_digest`) and `drift`;
- `verify`, `grade` and `usage`;
- `runs`: each turn's run id and its outbox prefix (`v1/runs/{date}/{session}.{run}/`, [outbox.md](outbox.md)).

The experiment index (`GET /api/experiments/{experiment}`) has the rest, with the pin set id on the variant's pins and
on every trial.

## Experiments on top

`hangar.experiment/v1` keeps working. When an experiment starts, it records a pin set per variant:

- a lock variant's pin set carries the lock's digest;
- an overlay variant's has no `harness_digest`, since only its first trial knows it, and has the overlay's digest.

The index lists them: `pins.variants.<name>.pin_set` and each trial's `pin_set`. `trials` and `parallel` stay as
conveniences of the batch wrapper.

## Access

REST only, with `jobs`-scoped tokens (admins too): flightdeck talks REST, and once the MCP server is split (#140) it
gets its own. Trials are owned like experiments (a service token sees its own); pin sets are content-addressed and
readable by any such caller.
