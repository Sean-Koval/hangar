# Run capture: the outbox (`hangar.run/v1`)

Hangar writes every finished agent run, once, as immutable objects to a bucket. The sister tool (flightdeck) reads
them into its own tables (`flightdeck.parquet/v1`) without asking hangar anything, so it works while the sandbox pools
are off (01:00-10:00 ET) and after hangar's own retention has let a run go. Hangar pushes; the sister tool owns the
tables and the compactor that builds them. This page is the contract between the two. It is the fourth of hangar's
interfaces, next to the [manifest](manifest.schema.json), the [trace export](traces.md) and
[experiments](experiment.schema.json) ([spec 0014](../specs/0014-sister-tool-interfaces.md#4a-run-capture-the-outbox-hangarrunv1)).

Versioned like the others: within `v1` hangar only adds optional fields, objects and event kinds; anything removed or
renamed is `v2`, under a new top-level prefix.

## Turning it on

| Setting | Default | Meaning |
| --- | --- | --- |
| `HANGAR_OUTBOX_URL` | unset (off) | `gs://bucket[/prefix]`, or a local directory (dev, tests: `file:///path` or a plain path) |
| `HANGAR_OUTBOX_SESSIONS` | `all` | `jobs_only` leaves out sessions not attached to a job (interactive personal sessions) |
| `HANGAR_EXPORT_GRACE_DAYS` | `30` | How long a deleted session's files wait for their export (below) |

On GKE, terraform creates the bucket (`terraform output run_bucket`, [gke.md](../gke.md#run-capture-bucket)). A
local directory gets the same layout and the same bytes a GCS reader gets (objects are stored gzipped in GCS with
`Content-Encoding: gzip`, which GCS decompresses on download unless the reader asks for the raw bytes).

## Layout

```
v1/runs/{date}/{session_id}.{run_id}/run.json          hangar.run/v1, written last
                                     manifest.json     hangar.manifest/v1, as captured when the turn started
                                     trace.otlp.json   hangar.trace/v1 of this run only (the turn and its children)
                                     tests.json        hangar.tests/v1: the turn's test runs (when tests ran)
                                     grade.json        experiment trials, once the grader has run
                                     patch.diff        the turn's diff against the commit it started on
v1/jobs/{date}/{job_id}/events/{ts_ms}-{kind}.json     hangar.job_event/v1
v1/tapes/{date}/{session_id}.{run_id}/tape/{seq}.json  hangar.tape/v1 entries (#177), expired after 30 days
                                          tape/index.json
v1/determinism/{date}/{repo}.json                      hangar.determinism/v1: the nightly determinism report (#179)
```

- `{date}` is the UTC date (`YYYY-MM-DD`) the run finished, or the event happened.
- The run key is `{session_id}.{run_id}`: a `run-[0-9a-f]{8}` id alone isn't unique across months.
  `run_id` is `hangar.run.id` in the trace and `subject.run_id` in the manifest; flightdeck's `runs.run_id`.
- **Complete runs.** `run.json` is uploaded only after every other object of its run is, so its presence is the
  completion marker: a run directory without it is still arriving. Its `objects` lists what is there, `withheld`
  what never will be (below), and `expects` what arrives later: `grade.json` for a trial of a graded experiment,
  written when the grader finishes, possibly after `run.json`.
- **Ordering.** Nothing may rely on arrival order. Runs and events carry their own timestamps and ids; a late
  object for an old date lands under that old date. A compactor looks back a few days (flightdeck: 3).

### What is captured, and when

| Run | Captured | `source` | `tests.json` |
| --- | --- | --- | --- |
| A session turn (no job) | as the turn ends | `session` | runs of the session's turn, if any |
| A job turn | after hangar's check and the delivery (`JobService._finish_turn`) | `job` | the trusted check after the turn, its flaky rerun, the in-turn gate's runs |
| A best-of-N attempt | after the attempt's check | `job` | the attempt's check |
| The best-of-N judge | as its turn ends | `job` | none |
| An experiment trial's turn | as a job turn | `experiment` | as a job turn; `grade.json` follows |
| Shadow runs | reserved (#183) | `shadow` | |

A job turn cut off by the power-down or a restart (it resumes later) is captured when it really ends. Capturing the
same run again uploads nothing: every object is write-once.

## `run.json` (`hangar.run/v1`)

Schema: [run.schema.json](run.schema.json) (validated in CI against real runs). The fields a reader most needs:

| Field | Meaning |
| --- | --- |
| `source` | `job`, `session`, `experiment` (`shadow` with #183) |
| `run_id`, `session_id`, `turn` | the run, its session, its number in the session (`hangar.turn.number`) |
| `job_id`, `job_source`, `job_turn`, `attempt` | the job, where it came from (`api`, `github`, `slack`, `jira`, ...), the turn's number in the job, the best-of-N attempt |
| `person`, `repo`, `environment` | |
| `base_sha`, `start_sha`, `head_sha` | the session's base, HEAD when the turn started, HEAD when it ended |
| `harness`, `harness_version`, `model` | |
| `harness_digest`, `environment_digest`, `config_digest`, `manifest_digest` | from the manifest ([spec 0014 §2](../specs/0014-sister-tool-interfaces.md#2-the-effective-harness-manifest-hangarmanifestv1)) |
| `pinned`, `pin_set`, `experiment` | an experiment trial: pinned, `<experiment>/<variant>`, `{id, trial_key, task, variant, trial, graded}`. Sessions and jobs: `pinned: false` |
| `outcome`, `outcome_reason`, `outcome_class`, `infra_reason` | the turn's outcome; the class (`agent`, `infra_error`, `cancelled`, `human`) is null until hangar classifies outcomes (#166). Null is unknown, never `agent` |
| `job_state`, `job_reason` | the job's state when the run was captured (the final state comes from later runs and events) |
| `started_at`, `finished_at` | Unix seconds |
| `usage` | tokens (`input_tokens` excludes cache reads and writes), `cost_usd`, `cost_source` |
| `verify` | hangar's check after the turn: `command`, `exit_code`, `passed`, `new_failures`, `rounds` (the in-turn gate's), `trusted`. Null for session turns |
| `tests` | ids of the test runs in `tests.json` |
| `steers` | `{n, by_class}`; `by_class` counts `correction`/`redirect`/`new_info` once steers are classified (#162) |
| `approvals`, `questions` | how many approvals the turn asked for; whether the agent asked a question |
| `labels` | the session's and job's labels |
| `traceparent` | the W3C traceparent of the turn's live `invoke_agent` span, when tracing was on ([observability.md](../observability.md)) |
| `untrusted` | the job's task text came from someone outside the trusted list (`Job.untrusted_source`): shadow and canary analysis stratify by it |
| `tainted` | the turn read untrusted content (#129) |
| `pr` | `{url, number}` of the job's PR, when it had one by then |
| `diff_files` | repo-relative paths: the delivered change's for a job's last turn, else `patch.diff`'s; at most 1,000 |
| `patch` | `{base, bytes, truncated}`: `patch.diff` stops at `HANGAR_EXCHANGE_MAX_PATCH_MB` |
| `truncated` | the trace hit the export's 20,000-item cap ([traces.md](traces.md#limits)) |
| `objects`, `withheld`, `expects` | above |

PR review data is not in `run.json`. Flightdeck reads the share of agent-authored PRs from GitHub into its own `prs`
table (Sean-Koval/flightdeck#6). The lifecycle facts hangar knows are job events: the PR ready for review, its first
human review, reverts and abandon reasons (#168, [metrics.md](../metrics.md)).

## The other objects

- **`manifest.json`**: the turn's stored `hangar.manifest/v1`, as captured when the turn started (its digests
  included), redacted again.
- **`trace.otlp.json`**: `hangar.trace/v1` for this run only, as `GET /api/sessions/{name}/trace?turn=N` returns it:
  the `turn` span with its tool calls, subagents, approvals and events.
- **`tests.json`** (`hangar.tests/v1`, `$defs.tests` in the schema): `runs` are the turn's `TestRun` records
  ([jobs.md](../jobs.md#8-test-results)) with `cases`, every case's outcome by test id (not only the failures);
  `events` are their `test.run` and `test.case` span events in OTLP/JSON span-event form (`name`, `timeUnixNano`,
  `attributes` as OTLP key-values), exactly what `GET /api/jobs/{id}/tests?format=otel` describes, so a trace
  flattener reads them unchanged. Each run has `counted` ([traces.md](traces.md#test-results): the field predicates
  use) and `agent_run_id`; a session turn's runs are those recorded with its run id (#164).
- **`grade.json`** (`hangar.grade/v1`): the trial's grade as the experiment index has it (`command`, `exit_code`,
  `applied`, `seconds`, `trust`, `hidden_tests`, `data` from `$HANGAR_GRADE_OUT`, ...) with `experiment_id`,
  `trial_key`, `job_id`, `session_id` and `run_id`. `test_run` names the grader's `grader` TestRun and `tests` holds
  it in `tests.json`'s shape (`runs` with every case, `events`), trusted only for `trust: isolated` (#164).
- **`patch.diff`**: the run's work against the commit the turn started on (`start_sha`): commits, uncommitted
  changes and untracked files (as `get_session_patch` computes it), read from the sandbox as the turn ends. Changes
  left uncommitted by an earlier turn appear again. Absent when the sandbox was gone.

## Tapes

Runs in a `model_gateway: record` environment ([model-gateway.md](../model-gateway.md)) also leave a tape of their
model calls, staged when the turn ends under `v1/tapes/` (a lifecycle rule can only match a name's prefix, so tapes
live apart from their run and the bucket deletes them after `run_tape_days`, 30). `{date}` is the day the turn
finished; the run key is the run's.

- **`tape/{seq:05d}.json`**, one per model call, in order: `seq` (from 1), `req_hash`, `req_delta`, `resp`,
  `resp_hash`, `prev_hash`, `at` (epoch seconds) and `usage` (the provider's, from the final events). `req_delta` is
  `{"full": request}` or `{"base": seq, "append": [...], "set": {...}, "unset": [...], "outer": {...}}`: the
  messages (or input items) the request adds to entry `base`. A request is `{method, path, headers, body}`; `resp`
  is `{status, headers, sse: [{event, data}]}` for a stream or `{status, headers, body}`, plus `truncated: true`
  when it wasn't kept whole. Both are normalized and redacted before hashing.
- **`tape/index.json`**: `schema` (`hangar.tape/v1`), `run`, `session_id`, `run_id`, `sandbox_id`, `provider`,
  `model`, `harness`, `manifest` (the turn's manifest digest), `harness_digest`, `entries`, `genesis`, `head`,
  `started_at`, `finished_at`, `untrusted` and `untrusted_reasons`.
- **Checking one**: hashes are SHA-256 over canonical JSON (sorted keys, `,` and `:` separators, UTF-8). Rebuild each
  request from its delta, check `req_hash` and `resp_hash`, check that `prev_hash` is the previous entry's hash
  (`genesis` for the first, `sha256("hangar.tape/v1\n" + run)`), and that the last entry's hash is `head`
  (`hangar.modelgw.validate` does it).

## Job events (`hangar.job_event/v1`)

Facts about a job that arrive after its runs, each its own immutable object, never a rewrite of `run.json`.
Schema: `$defs.job_event`. Every event has `kind`, `ts`, `job_id`, `job_source`, `source`, `repo`, `person`,
`environment`, `session_id`, `run_ids`, `pr`, `untrusted` and a `data` object:

| `kind` | When | `data` |
| --- | --- | --- |
| `pr.open` | the job's PR exists | `{draft}` |
| `review.first` | the first approval that counts (landing) | `{by, sha, first_review_at, first_review_by}`: the last two are the first human review in any state (#168, added within v1) |
| `pr.merge` | merged | `{by, green_to_merge_seconds}` |
| `pr.close` | closed without merging (each close) | `{}` |
| `abandon` | the job was cancelled; or an abandon reason was set on its closed PR (#168) | `{reason}`; for a PR's reason `{reason, by, via, pr: "closed"}` |
| `pr.ready` | the PR was opened non-draft or marked ready (#168): review wait starts here | `{}` |
| `revert` | the nightly pass found a commit on the default branch reverting the PR's merge commit (#168, [metrics.md](../metrics.md)) | `{sha}` |
| `label` | a failure label from the Friday harness review, and each relabel (#169, [runbook](../runbooks/harness-review.md)) | the `FailureLabel` (`id, job_id, session_id, turn, seq, category, fix, note, candidate_task, labelled_by, at`) and `base_sha, head_sha, repo, ticket_ref, title, ticket` (redacted, at most 4,000 characters), `ticket_untrusted, verify, pr_state, abandon_reason` |

Hangar scans jobs changed in the last minute for new facts and writes each once.

## Guarantees

- **Write-once.** Every upload is a create with `ifGenerationMatch=0`; a `412` means the object is already there and
  counts as done. Hangar's service account has `roles/storage.objectCreator` on the bucket and nothing else there,
  and an IAM deny policy takes `objects.delete` and `objects.update` away from it (its project-wide `storage.admin`
  is for Cloud Build): it can't delete or overwrite a run. Readers get `roles/storage.objectViewer`.
- **Redacted.** Every object passes the broker's redaction (stored credentials, verbatim and in common encodings) and
  secretscan's masking (token formats: GitHub, cloud keys, ...) before it is staged, on top of the redaction the
  manifest and trace already apply. The redacted text is then scanned again; an object that still holds a secret is
  marked `failed`, listed in `run.json`'s `withheld`, and never uploaded. A test seeds a stored secret, its base64
  form and a token-shaped string in the agent's output and work, and checks no object holds any of them.
- **Never on a turn's path.** A turn's end only starts a task. The objects are built, redacted, gzipped and staged on
  the data volume (`<data>/outbox/`) with one queue record each (`OutboxItem`: `key, kind, run, state:
  pending|done|failed, attempts, next_at, error`); a drain loop uploads them. A test holds every upload for a minute
  and checks the job finishes as fast as without the outbox.
- **Retried until it lands.** A failed upload is retried with exponential backoff, 1 minute doubling to 6 hours, for as
  long as it stays pending. The queue and the staged payloads survive restarts. The control plane stays up while
  the pools are off, and GCS doesn't depend on the cluster, so the queue drains overnight too.

## Operating it

- `hangar outbox status`: pending, done and withheld objects, the oldest pending one, the last error, and retention
  held for the export (`GET /api/admin/outbox`). Admins.
- `hangar outbox retry`: everything pending, now, instead of waiting out the backoff (after an outage).
- `hangar outbox backfill --since 7d`: runs finished in the window with nothing queued, captured again from what
  hangar still has on disk (manifests, event logs, test runs; the patch only while the sandbox is up).
- `/metrics`: `hangar_outbox_pending`. `hangar doctor` warns past 1,000 pending objects or 24 hours of age, lists
  withheld objects, and reports the bytes deleted sessions hold only because their export is pending.

## Retention and the export (#189)

Hangar's retention never deletes what the outbox hasn't exported yet:

- A session's **event log and manifests** go with the session record today. Now the record goes as before, but its
  files are deleted only once every run of it is exported; until then they wait in `<data>/pending-delete/<session>/`
  and a pass every 10 minutes deletes them once the export is done, or after `HANGAR_EXPORT_GRACE_DAYS` (30) with an
  audit event `retention.unexported_deleted` (a GCS outage longer than that loses those runs, and the event says so).
- **Test runs and cases** of a job that failed, was cancelled (abandoned), had its PR closed unmerged, or carries a
  failure label (#169, `job.failure_label`) outlive testresults' windows (cases 3 days, runs 14) until the job's runs are exported.
- Best-of-N attempt sessions are archived, never deleted, so their event logs stay.
- With the outbox off, retention is exactly as before.

A run is *exported* when nothing of it is being built and every object of it is uploaded. A run with nothing queued
(it ran before the outbox was turned on, or `jobs_only` leaves its session out) counts as exported: nothing will ever
upload it.

**Deletion requests.** Objects are immutable. To delete a run (personal data): delete its objects with an admin
credential (`gcloud storage rm -r gs://<bucket>/v1/runs/<date>/<session_id>.<run_id>/`, and its job's event objects),
then have the sister tool recompact the affected dates (flightdeck: `flightdeck compact --date D --rebuild`). Locally,
`hangar session delete <session> --force-now` deletes its event log and manifests without waiting for the export
(audited `session.delete_now` and `retention.unexported_deleted`).

## Out of scope

The GCS → Parquet compactor (flightdeck, Sean-Koval/flightdeck#5); replaying tapes from the bucket (replays read the
control plane's local copy, [model-gateway.md](../model-gateway.md#replay)); exporting anywhere other
than GCS or a local directory.
