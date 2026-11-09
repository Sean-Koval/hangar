# The operator console, flight records and run capture

This page is part of [Observability: traces, metrics, cost](../observability.md).

## The operator console: `hangar status`

One view of what you'd otherwise need kubectl for, admins only (`GET /api/status`, the MCP resource `hangar://status`, `hangar status`). It is a resource, not a tool: the MCP tool list is at its cap, and claude.ai caches tool lists. A member's token gets "needs a token with the 'admin' scope".

```
$ hangar status
sandboxes: on
last deploy: 3h ago, 0.1.0+tools.4f1c: passed
loops: ops 21s, reaper 34s, reconciler 12s, schedules 3s, sessions 40s
harnesses: antigravity 1.3.3, claude-code 2.1.296, codex 0.162.1 (pack behind on kubernetes), pi 1.1.0
builds: pack codex, repo acme/api python v4 (installing)
failures (24h): 7, 3 transient
  missing_tool      4             last 2h ago (fr-9a1b…)  python
  model_api         3  transient  last 20m ago (fr-77c2…)  python, node
egress denies (24h): 12
  registry.npmjs.org                           12  3 sandbox(es)  node
requests waiting: 1, oldest 14m
SLOs (24h):
  create     warm  n=41   p50 1.2s p95 2.1s  (p50<=1.5s p95<=2.5s) ok
  create     cold  n=9    p50 18.0s p95 41.0s  (p95<=30.0s) BREACHED
  turn_start any   n=120  p50 2.4s p95 6.0s  (p50<=5.0s p95<=15.0s) ok
  resume     cold  n=6    p50 22.0s p95 38.0s  (p95<=60.0s) ok
credentials: 5 ok, 1 warn
  ! claude-alice (alice): expires on 2026-10-27, in 13 day(s). Run `claude setup-token` and store the new token: …
cloud doctor: 2 open finding(s); last run 9h ago (done)
```

| Section | What it holds | Source |
| --- | --- | --- |
| `power`, `last_deploy` | the sandbox pools' state and when they're back; the last deploy.yml result | [power.py](https://github.com/Sean-Koval/hangar/blob/main/src/hangar/power.py) |
| `loops` | each background loop's last pass, interval and failures (`ops` is the console's own) | `hangar_loop_last_tick_timestamp` |
| `harnesses` | each agent CLI's locked version and the version each provider's pack installed; `behind` names providers whose pack differs | `harnesses.lock.yaml`, pack builds |
| `builds` | environment images, harness packs and repo builds running now, with how long | the fleet, [envbuilds](../environments.md) |
| `failures` | the last 24 hours' [flight records](#flight-records) by cause, transient or not, and the 10 newest | flight records |
| `egress_denies` | the last 24 hours' denies by host: count (suppressed ones included), sandboxes, environments | the audit log |
| `requests` | secret and egress requests agents filed that wait for an answer | [env requests](../computer-use.md) |
| `slo` | p50/p95 over 24 hours against the targets below | in memory, saved every minute |
| `credentials` | [credential health](#credential-health): counts, what needs renewing, pools | [credhealth.py](https://github.com/Sean-Koval/hangar/blob/main/src/hangar/credhealth.py) |
| `models` | what jobs run on by default (`balanced`, the id it resolves to and its list price), the job budget and daily cap that bound it, and the repos whose policy caps them; `NO job budget or daily cap` until one is set, which `hangar doctor` also warns about (`model spend`) | [modelpolicy.py](https://github.com/Sean-Koval/hangar/blob/main/src/hangar/modelpolicy.py) |
| `nightly`, `doctor` | the nightly pass and the [Cloud Doctor](../doctor.md)'s last run and open findings | |
| `deploy_lag` | how far production is behind main ([a deploy waiting too long](../runbooks/deploy.md)) | GitHub, with hangar's token |
| `tests`, `caches` | test runs' results and flaky tests; the build caches | [test results](../jobs/test-results.md), [caches](../caches.md) |
| `log` | the last 50 warnings and errors hangar logged | in memory |

`?section=slo,failures` (or `--section`) returns only those. `--check-credentials` checks every credential now; otherwise status rechecks only results older than an hour, spending at most 15 s.

**The log section** keeps the last 200 records at WARNING and above from hangar's own loggers (`hangar.*`). Uvicorn's loggers never reach it: `uvicorn.access` lines carry request paths and query strings. Each line goes through `broker.redact` before it is shown, so a stored credential value in an exception message comes out masked.

`fleet_overview` (MCP) and `GET /api/overview` carry two of these as fields: `failures_24h` (count by cause) and `credential_health` (counts, and how many need attention). `list_credentials` carries each credential's `health`, `last_ok` and `expires_at`. `get_job` carries `flight_record` on a finished job that has one.

### SLOs

| What | Path | Target |
| --- | --- | --- |
| create | `warm`: a pod claimed from the warm pool | p50 ≤ 1.5 s, p95 ≤ 2.5 s |
| create | `cold`: a new pod, image cached | p95 ≤ 30 s |
| create | `build`: an image or pack was built on the way | none (exceptional by design) |
| turn start | from `begin_turn` to the agent running, the sandbox up (time spent waiting for a best-of-N sibling's cache excluded) | p50 ≤ 5 s, p95 ≤ 15 s |
| resume | `hot`: a parked sandbox woken / `cold`: a new sandbox restored from a checkpoint | p95 ≤ 5 s / ≤ 60 s |

These are the observability review's targets for GKE; forks aren't creates here. Samples are saved every minute, so a restart keeps the day. The rate SLOs and their alerts are on [their own page](../slos.md). `hangar_sandbox_create_seconds` in `/metrics` carries the creates for people who scrape.

## Flight records

A record is written for each job that fails or times out, each session turn outside a job that fails or times out, and each job or turn that ran slow against its environment's norm: more than 3× the median of its environment's last 50 clean runs, and at least 2 minutes over it, once there are 8 to compare with. Cancelled jobs and jobs waiting for an answer get none.

| Field | What |
| --- | --- |
| `cause`, `detail`, `transient` | decided without a model, most specific first: `power_down`, `pod_replaced`, `restart`, `model_api` (a 429/5xx/529 or "overloaded" from the model API), `provision` / `image_build`, `install` (setup failures, or a repo build that fell back to the base image), `missing_tool` (`command not found`, `No module named`, `Cannot find module`…; `detail` names it), `egress` (`detail`: the most denied host), `secret`, `blocked` (a request nobody answered), then the job's reason (`verify_failed`, `timed_out`, `over_budget`, `agent_failed`, `push_failed`…), `slow`. Transient: `model_api`, `power_down`, `restart`, `pod_replaced`, `sandbox_gone`, `network`. |
| `signature` | cause, detail (numbers dropped) and repo, or environment: what the Cloud Doctor groups by |
| `stages`, `failed_stage`, `sandbox_cause` | the sandbox create's stage history, and where and why it stopped |
| `env_doctor` | what `hangar-agent env doctor` reports: build and fallback, setup failures, warnings, services, expected secret names, egress mode |
| `egress_denies` | the sandbox's denies from the audit log (host, port, reason), at most 20 |
| `requests` | the agent's secret and egress requests still pending or denied |
| `versions`, `model`, `harness_version` | hangar, the harness CLI the turn ran, the model, the image and packs |
| `cost_usd`, `tokens`, `seconds`, `norm` | what it used, how long it took, and the environment's median |
| `verify`, `events`, `summary` | the check's command, exit code and tail; the job's last 20 events |
| `log` | where the end of the run log is stored, its size and SHA-256 |

**The run log** is the last 256 KB of the turn's log. It is read when the session goes idle (a job's waits in memory, at most 64 of them and an hour, until the job is reported), then on a task of its own it is masked (stored credential values and their encodings, and the token formats secret scanning knows), gzipped, encrypted with the vault key like a checkpoint part, and stored in the session store under `records/<id>/`. Every free-form field of the record goes through the same masking. Records and logs are deleted after 14 days, hourly. Nothing about a record is ever on the turn's or the job's path: a failure to write one is logged and nothing else.

```bash
hangar admin records ls --hours 48 [--cause missing_tool]
hangar admin records show fr-9a1b2c3d --log
```

REST: `GET /api/records?hours=24&cause=…` (admins), `GET /api/records/{id}` and `GET /api/records/{id}/log` (admins, or the record's person), `GET /api/jobs/{id}/record`. MCP: the resource `hangar://jobs/<id>/record`, for whoever may see the job.

## Run capture

Traces and metrics show the fleet now; run capture keeps every run. With `HANGAR_OUTBOX_URL` set (a GCS
bucket on GKE, `terraform output run_bucket`; a local directory in dev), every finished run is written once as
immutable objects: `run.json` (`hangar.run/v1`: who, what, the harness and environment digests, outcome, usage,
verify, steers, the untrusted flag), the turn's manifest, its trace export, its test results and its patch, under
`v1/runs/{date}/{session_id}.{run_id}/`, plus [job events](../api/outbox.md#job-events-hangarjob_eventv1) (a PR opened, reviewed, merged, reverted or abandoned) under
`v1/jobs/`. The sister tool reads them into its own tables while the sandbox pools sleep. The contract (layout,
schema, redaction, write-once IAM, deletion requests) is [docs/api/outbox.md](../api/outbox.md).

Nothing of it is on a turn's path: the objects are built and redacted on a task after the turn and staged on the data
volume with a queue record each, and a drain loop uploads them, retrying with backoff (1 minute to 6 hours) through an
outage and across restarts. The control plane stays up overnight, so the queue drains while the pools are off.

| Where | What |
| --- | --- |
| `hangar outbox status` (`GET /api/admin/outbox`) | pending, done and withheld objects, the oldest pending one, the last error |
| `hangar outbox retry` / `backfill --since 7d` | upload now after an outage; capture runs that have nothing queued |
| `hangar_outbox_pending` | objects waiting to be uploaded: alert when it keeps growing |
| `hangar doctor` | warns past 1,000 pending objects or a day of age, lists withheld objects (they still held a secret after redaction, so they are never uploaded), and the bytes deleted sessions hold until their export |

Retention waits for the export: a deleted session's event log and manifests, and a failed job's test results, stay
until the outbox has uploaded its runs (at most `HANGAR_EXPORT_GRACE_DAYS`, 30; [outbox.md](../api/outbox.md#retention-and-the-export-189)).

## The determinism report

Each night in the eval window, hangar strictly replays the previous day's recorded turns of each repo
([model-gateway.md](../model-gateway.md#replay), `replay-strict` with `tolerate: tool`): the taped answers are served
again in fresh replay sandboxes and every tool call re-executed, so a step whose output doesn't reproduce on an
identical tree (a flaky test, a build that reaches the network, a command that reads the clock) shows up. It costs
sandbox time only: no model call leaves hangar.

- **Which turns.** Tapes whose turn finished on the previous UTC day, of a session's first run, not of a trial, shadow
  or replay: up to `HANGAR_DETERMINISM_RUNS_PER_REPO` (50) per repo, sampled deterministically, for at most
  `HANGAR_DETERMINISM_MAX_REPOS` (10) repos, the busiest first. Each replay is a fork with no continuation
  ([sessions.md](../sessions/forks.md#fork-at-a-step)) in the eval queue class, with `HANGAR_DETERMINISM_REPLAY_BUDGET_USD` (1)
  as its job budget. `POST /api/determinism/run {"date"?}` (admins) starts a day's now.
- **What it writes**, once every replay has ended: per repo, `v1/determinism/{date}/{repo}.json`
  (`hangar.determinism/v1`) in the outbox and `<data>/determinism/{date}/{repo}.json`. `steps` lists the steps whose
  outputs didn't reproduce, ranked by how often (`count`, and in how many `runs`), each with its `kind` (`tool`,
  `network`; `model` means the replay stopped there because the harness or its system prompt changed), the `tool`, its
  `command` or `file`, and `evidence` (run, step, replay, and excerpts of both outputs, redacted as the tape is). Also
  `replays`, `reproduced`, `diverged`, `unreplayed` and `errors`.
- **Flaky tests.** A test on the repo's flaky list ([jobs/test-results.md](../jobs/test-results.md#flaky-tests)) whose id shows in a mismatched
  output gets the step as `evidence` on its list entry (the last 5), with a link to the report.

## Credential health

Each agent credential is checked without calling a model:

| Credential | Check | Expiry |
| --- | --- | --- |
| Claude Code `oauth_token` (`claude setup-token`) | the token's age: the command makes "a one-year OAuth token" ([Claude Code authentication](https://code.claude.com/docs/en/authentication)) | a year after it was stored |
| Claude Code and Codex `api_key` | the key's format (a gateway's own key is a warning, not a failure) | none |
| Codex `auth_file` (ChatGPT login) | `codex login status`, which "exits with 0 when credentials are present" ([Codex CLI](https://learn.chatgpt.com/docs/developer-commands)), in the login's sandbox when it is captured; elsewhere the same question answered from the file: an API key, or ChatGPT tokens with a refresh token | none: Codex refreshes its tokens while it is used; a run whose refresh OpenAI refuses marks it failed (KAN-178) |
| Google service-account keys (Vertex AI for pi and Antigravity) | in a gcloud config directory of its own: `gcloud auth activate-service-account --key-file`, then `gcloud auth print-access-token` (Google refuses a deleted or disabled key; the token is discarded), then `gcloud iam service-accounts keys list` where the key may list its own account's keys | the key's `validBeforeTime`, when it has one |

Each check writes `health` (`ok`, `warn`, `fail` or `unknown`, with a detail and when), `last_ok` and `expires_at` onto the credential. Storing a new value clears them. Checks run when a login is captured, every night (the [nightly pass](../observability.md#the-nightly-pass)), from `hangar status`, and with `hangar cred check [NAME…]` (`POST /api/credentials/check`).

A credential that fails, or expires within 14 days, shows in `hangar doctor` (`credential health`; marked `deploy_gate: false`, so `deploy.yml`'s acceptance gate reports it rather than rolling back, since a rollback can't renew a login), in `hangar status`, and in the [inbox](../landing.md) of whoever renews it: its person for their own logins, admins for the team's. Pools (several logins one sandbox holds at a time, such as a person's Codex ChatGPT logins) are summed up; a pool with no working login fails the doctor even when each login alone would only warn.
