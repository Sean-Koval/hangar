# Team scale

One control plane for a team: **6 engineers, each with up to ~10 sessions at once (~60 sandboxes), plus jobs.** This
page covers what keeps that responsive and fair, how to size GKE for it, and what it costs.
To onboard the team itself (people, credentials, and each repo's App, branch protection, policy and doors), follow
[the onboarding runbook](runbooks/onboarding.md).

## What holds it up

| Problem at 60 sessions | What hangar does now | Where |
| --- | --- | --- |
| Every create, lookup by name and listing parsed **every sandbox record ever made**, on the event loop (250–935 ms at 20k records; records were never deleted) | Indexed lookups: an expression index on `state` and `name` (and on jobs' and sessions' `state`), `Store.list_where` / `count_where` / `list_active` | `store.py` |
| History grows forever | Stopped and failed sandbox records idle for 7 days move to `sandboxes_archive`, in small batches off the event loop; still found by id; deleted after 180 days | `retention.py` |
| One person's batch could take the whole fleet | Caps per person on live sessions, sandboxes and running jobs | `fairness.py` |
| Jobs ran first come, first served, 4 at a time | Job slots sized from capacity (8 on GKE at `max_sandboxes=120` by default, 24 once job budgets are set; see the follow-ups); a free slot goes to whoever has the fewest jobs running | `fairness.py` (`FairJobQueue`, `job_slots`) |
| A fork could exhaust the control plane's 2 GiB (B12: ~5 children of a 400 MB workspace, 3 on the pods/exec fallback) | Workspace archives in flight share a memory budget (1 GiB, each counted at 2–3× its size); an oversized archive is refused before it is read; Kubernetes uploads encode base64 a frame at a time; archive disk I/O runs in a thread | `membudget.py`, `fleet.py`, `providers/kubernetes.py` |
| Each sandbox reserved its whole CPU limit | Sandbox pods request half their CPU and all their memory (Burstable) | `providers/kubernetes.py` |

### Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_MAX_SANDBOXES` | 120 (was 50) | The whole fleet: 60 sessions, job slots (8, or up to 24 set by hand), best-of-N attempts, forks, logins |
| `HANGAR_JOB_CONCURRENCY` | 0 = auto (was 4) | 4 on Docker; elsewhere `max_sandboxes / 5`, between 4 and 8. Set 24 by hand once job budgets and daily spend caps are set ([repo-policy.md](repo-policy/budgets.md#budgets)) |
| `HANGAR_PERSON_MAX_SESSIONS` | 15 | Live sessions per person (each holds a sandbox); new, resumed (`send`/`open`) and forked ones count. Suspended ones don't |
| `HANGAR_PERSON_MAX_SANDBOXES` | 20 | Sandboxes a person starts directly (MCP, REST). Sessions' and jobs' own sandboxes are capped by the two caps around this one |
| `HANGAR_PERSON_MAX_RUNNING_JOBS` | 0 = half the job slots, at least 4 | Jobs one person runs at once. Queued jobs past it wait while others' run |
| `HANGAR_PERSON_MAX_OPEN_AGENT_PRS` | 5 (0 = off) | Open agent PRs awaiting a person before their new jobs wait as `awaiting_capacity` ([review capacity](#review-capacity)). A repo policy's `max_open_agent_prs` overrides it for that repo's jobs; `HANGAR_PERSON_MAX_OPEN_AGENT_PRS_OVERRIDES` (`{"alice": 8}`) raises or lowers it for one person |
| `HANGAR_SANDBOX_RETENTION_DAYS` / `HANGAR_SANDBOX_ARCHIVE_DAYS` | 7 / 180 | When settled records move to the archive, and when archived ones are deleted (0 = never) |
| `HANGAR_WORKSPACE_TRANSFER_MB` | 1024 | Workspace archives in the control plane's memory at once (read at startup) |
| `HANGAR_K8S_CPU_REQUEST_FRACTION` | 0.5 | Share of an environment's `cpus` a sandbox pod requests; the limit stays `cpus`. 1 = Guaranteed QoS, as before |

`GET /api/overview` reports the per-person sandbox and session caps as `person_caps`; the web UI's **Fleet → Owners**
view shows each person's use of them, with their sandboxes by environment and what they spent today.

A cap reached answers 429 over REST, and an error naming the cap over MCP. Every cap is per
**person**, the person hangar records on each token and on the work it starts ([security/people.md](security/people.md#people)):
all of a person's tokens, MCP and REST calls, and the OAuth connectors they approved count together, and the
admin's existing tokens all count as `owner`. `fairness.person_of` is the one place that maps who acted to a person;
records from before people fall back to the token's name. hangar's own actors (sessions' and jobs' sandboxes, the
reaper, schedules) are not people. For the caps to be per engineer, **add each engineer as a person**
(`hangar person add <name>` prints their login token). Jobs from GitHub, Slack or Jira count for the
[linked](team/linked-accounts.md) person. An unlinked sender is capped per GitHub or Slack account.

The caps sit beside the per-caller limits that already exist: `HANGAR_JOB_MAX_ACTIVE_PER_OWNER` (unfinished jobs per
caller, 10) and the job rate limits.

## Review capacity

With ~60 sessions, review is the bottleneck, not compute: every Jira-door job is tainted (its ticket text is
untrusted), so its PR needs a second human ([landing.md](landing/how-a-pr-lands.md#tainted-prs)). Past
`HANGAR_PERSON_MAX_OPEN_AGENT_PRS` (5) open agent PRs awaiting a person, that person's **new** jobs wait in the queue:
their state stays `queued` (clients keep working) and their `reason` is `awaiting_capacity`, with an event saying why
("waiting: alice has 5 agent PRs awaiting review (the cap is 5); …"). Everyone else's jobs start meanwhile.

- **Who a PR waits on**: the job's person (they review their own delegated PR). When the
  [self-approval rule](landing/how-a-pr-lands.md#self-approval-who-may-be-the-only-approver) needs a second reviewer (a tainted PR, a
  normal- or high-risk one, `self_approval: never`), the PR also counts for the people linked to the repo policy's
  `approvers`. Team jobs (no person) count against `team:<owner/repo>`, capped at N × the number of policy approvers.
- **What counts**: agent PRs that are `open` or `approved` and not ended. Approved PRs that can't land (a failed
  check, `land: manual`) still need the person, so they count. The counts follow every landing event and are counted
  again at startup; held jobs keep their reason across a restart.
- **What never waits**: follow-ups, auto-fix and verify-fix turns on existing jobs (they shrink the queue), experiment
  trials, [AI review](landing/review.md#6a-ai-first-pass-review) jobs, and a job an admin admitted
  (`hangar job admit <id>`, `POST /api/jobs/{id}/admit`).
- **Waking**: a PR merging or closing wakes the queue at once, so the held job starts within one queue tick.
- **Seeing it**: `hangar job list --reason awaiting_capacity` (`GET /api/jobs?reason=awaiting_capacity`);
  `GET /api/review-capacity` (admin) lists open agent PRs per reviewer and the held jobs; the gauge
  `hangar_jobs_awaiting_capacity` (no person label: labels stay bounded); and the GitHub, Slack and Jira "queued"
  replies say the job waits on review capacity and link the person's review queue on GitHub. `fairness.capacity_view`
  gives each person's [digest](team/linked-accounts.md#inbox-delivery-dms-and-the-digest) the counts.
- **Power-down**: a job held for capacity stays in the queue; one held by the power-down shows `powered_down`, and
  capacity is checked again at power-up.

Someone who never reviews blocks only their own new work; an admin can raise their cap
(`HANGAR_PERSON_MAX_OPEN_AGENT_PRS_OVERRIDES`) or admit one job. Attention-priced scheduling (#185) may later replace
the fixed cap.

## The eval queue class

Experiment trials spend the same provider rate limits (tokens per minute) as people's interactive work, and the
01:00 power-down kills whatever is in flight, so evals run in the evening and never ahead of a person. Every job has a
`queue_class`: `interactive` (the default; AI reviews too) or `eval` (experiment trials, and any job submitted with
`queue_class: eval`). An eval job starts only when all of these hold:

- **The eval window is open**: `HANGAR_EVAL_WINDOW` (`20:00-00:15`) in `HANGAR_EVAL_TZ` (`America/New_York`); the end
  may be past midnight, `always` turns the window off. No new trial starts after 00:15, so most finish before the
  power-down. An admin lifts the window for one job with `POST /api/jobs/{id}/priority` (`{"priority": "now"}`).
- **No interactive job is waiting**: the queue always picks interactive jobs first, then the fairest eval job.
- **There is an eval slot**: at most `HANGAR_EVAL_MAX_RUNNING` eval jobs run at once (0, the default: half the job
  slots), so the rest are always there for people.
- **The provider has room**: with `HANGAR_EVAL_TPM_BUDGET` set (`400000` for every provider, or
  `anthropic=400000,openai=300000`), a trial starts only while the provider's observed rate plus a typical trial's
  stays within it. The observed rate is a rolling 60 seconds per provider (the harness's vendor): running turns count
  their live usage spread over the time they have run, finished turns the share of theirs that falls in the last
  minute. A typical trial is the median tokens per minute of recent eval turns on that provider, else
  `HANGAR_EVAL_TPM_PER_TRIAL` (20000). Turn-end usage lags: sandboxes that record through the
  [model gateway](model-gateway.md) are counted call by call as each completes instead.

A held eval job stays `queued` in its place; its `reason` says why (`eval_window`, `eval_slots`, `eval_tpm`) with an
event (`waiting: eval jobs start inside the eval window (20:00-00:15 America/New_York); next at 20:00 EDT`). The queue
looks again every 15 seconds, so the window opening or the rate dropping starts it without an event.

**Seeing it**: `GET /api/admin/eval-queue` (admin) shows the window, the eval jobs running and waiting, and per
provider the observed rate, the per-trial estimate and the budget. Metrics: `hangar_eval_window_open`,
`hangar_eval_observed_tpm{provider}`, `hangar_eval_tpm_budget{provider}` and `hangar_eval_jobs_waiting{reason}`.

**Monthly budget**: eval spend has a team budget per calendar month and a cap per author (#182,
[repo-policy.md](repo-policy/budgets.md#the-eval-budget)); the eval queue decides when trials run, the budget whether more may.

**Power-down**: a drain (`power.sh down`, [gke.md](gke/power.md)) cancels running
trials rather than checkpointing them, marks them `infra_error` / `power_down` ([jobs/experiments.md](jobs/experiments.md#the-eval-queue)),
and the experiment queues a fresh attempt; like every queued eval job it waits for the next window, not for the 10:00
power-up. When people work late, evals wait: that starvation is accepted.

## Limits and follow-ups

- **Budgets before concurrency.** Auto job slots stop at 8 (`fairness.AUTO_JOB_SLOTS_MAX`), not 24. Job budgets
  and daily spend caps per repo and per person ([repo-policy.md](repo-policy/budgets.md#budgets)) are opt-in, so on defaults
  nothing bounds what a job spends across its follow-ups, verify-fix and auto-fix turns, and the per-person cap
  bounds one person, not a GitHub label storm across many. A slot is also a job, not a sandbox: 24 best-of-4 jobs
  could hold all 120 sandboxes, and every new session would be refused at `max_sandboxes`. Once
  `HANGAR_JOB_BUDGET_MAX_COST_USD` and the repos' `max_usd_per_day` (or `HANGAR_PERSON_MAX_USD_PER_DAY`) are set,
  `HANGAR_JOB_CONCURRENCY=24` gives the measured 24.
- **The reconciler** reads only live and unfinished sandboxes, from the state index (`reconciler.sandboxes_in`), once
  per pass on its own task. `hangar gc` still lists every record; it is an admin's occasional command.
- **Jobs and sessions in memory** are the live ones and the last hour's ([the load test](team-scale/load-test.md#months-of-job-and-session-history)).
  The archive keeps finished ones forever: nothing deletes them yet. At 100 jobs a day that is ~1 GB of SQLite a year,
  read only by id, by index and a page at a time.
- **Token lookups** decode every API token on every request (`auth.principal`), ~1 ms at a handful of tokens. An index
  on the token hash, with the constant-time compare kept, removes it.
- **Snapshots on the pods/exec fallback** still read the whole archive as base64 text. The budget bounds it; reading it
  in pieces (or through envd, which streams) removes it.
- **One replica.** SQLite and in-memory queues; a second replica needs a lease (hardening's H5) first.

[sandbox]: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/sandbox-pods
[smt]: https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-smt
[nodes]: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/plan-node-sizes
[spot]: https://cloud.google.com/spot-vms/pricing
[disks]: https://cloud.google.com/compute/disks-image-pricing
[gke]: https://cloud.google.com/kubernetes-engine/pricing
[lb]: https://cloud.google.com/load-balancing/pricing
[holori]: https://calculator.holori.com/gcp/vm/n2d-standard-16
[overlay]: https://gvisor.dev/blog/2023/05/08/rootfs-overlay/
[quotas]: https://docs.cloud.google.com/compute/quotas
[e2]: https://gcloud-compute.com/e2-standard-2.html
[e2-16]: https://gcloud-compute.com/e2-standard-16.html

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="measured-the-team-load-test"></span>Measured: the team load test | [The team load test](team-scale/load-test.md) |
| <span id="months-of-job-and-session-history"></span>Months of job and session history | [The team load test](team-scale/load-test.md#months-of-job-and-session-history) |
| <span id="gke-capacity-for-60-sessions"></span>GKE capacity for 60 sessions | [GKE capacity for 60 sessions](team-scale/gke-capacity.md) |
| <span id="node-type-why-amd"></span>Node type: why AMD | [GKE capacity for 60 sessions](team-scale/gke-capacity.md#node-type-why-amd) |
| <span id="packing"></span>Packing | [GKE capacity for 60 sessions](team-scale/gke-capacity.md#packing) |
| <span id="how-many-nodes"></span>How many nodes | [GKE capacity for 60 sessions](team-scale/gke-capacity.md#how-many-nodes) |
| <span id="the-control-plane"></span>The control plane | [GKE capacity for 60 sessions](team-scale/gke-capacity.md#the-control-plane) |
| <span id="applying-it"></span>Applying it | [GKE capacity for 60 sessions](team-scale/gke-capacity.md#applying-it) |
| <span id="upgrading-and-rolling-back"></span>Upgrading and rolling back | [GKE capacity for 60 sessions](team-scale/gke-capacity.md#upgrading-and-rolling-back) |
| <span id="cost"></span>Cost | [What 60 sessions cost on GKE](team-scale/cost.md) |
| <span id="heavy-tier"></span>Heavy tier | [The heavy tier at team scale](team-scale/heavy-tier.md) |
| <span id="machine-type"></span>Machine type | [The heavy tier at team scale](team-scale/heavy-tier.md#machine-type) |
| <span id="packing-an-n2-standard-32"></span>Packing an n2-standard-32 | [The heavy tier at team scale](team-scale/heavy-tier.md#packing-an-n2-standard-32) |
| <span id="how-many-nodes-and-the-caps"></span>How many nodes, and the caps | [The heavy tier at team scale](team-scale/heavy-tier.md#how-many-nodes-and-the-caps) |
| <span id="cost_1"></span>Cost (heavy tier) | [The heavy tier at team scale](team-scale/heavy-tier.md#cost) |
