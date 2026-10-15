# Team scale

One control plane for a team: **6 engineers, each with up to ~10 sessions at once (~60 sandboxes), plus jobs.** This
page covers what keeps that responsive and fair, how to size GKE for it, and what it costs.

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
| `HANGAR_JOB_CONCURRENCY` | 0 = auto (was 4) | 4 on Docker; elsewhere `max_sandboxes / 5`, between 4 and 8. Set 24 by hand once job budgets and daily spend caps are set ([repo-policy.md](repo-policy.md#budgets)) |
| `HANGAR_PERSON_MAX_SESSIONS` | 15 | Live sessions per person (each holds a sandbox); new, resumed (`send`/`open`) and forked ones count. Suspended ones don't |
| `HANGAR_PERSON_MAX_SANDBOXES` | 20 | Sandboxes a person starts directly (MCP, REST, the E2B API). Sessions' and jobs' own sandboxes are capped by the two caps around this one |
| `HANGAR_PERSON_MAX_RUNNING_JOBS` | 0 = half the job slots, at least 4 | Jobs one person runs at once. Queued jobs past it wait while others' run |
| `HANGAR_SANDBOX_RETENTION_DAYS` / `HANGAR_SANDBOX_ARCHIVE_DAYS` | 7 / 180 | When settled records move to the archive, and when archived ones are deleted (0 = never) |
| `HANGAR_WORKSPACE_TRANSFER_MB` | 1024 | Workspace archives in the control plane's memory at once (read at startup) |
| `HANGAR_K8S_CPU_REQUEST_FRACTION` | 0.5 | Share of an environment's `cpus` a sandbox pod requests; the limit stays `cpus`. 1 = Guaranteed QoS, as before |

A cap reached answers 429 over REST and the E2B API, and an error naming the cap over MCP. Every cap is per
**person**, the person hangar records on each token and on the work it starts ([security.md](security.md#people)):
all of a person's tokens, MCP and REST calls, and the OAuth connectors they approved count together, and the
admin's existing tokens all count as `owner`. `fairness.person_of` is the one place that maps who acted to a person;
records from before people fall back to the token's name. Hangar's own actors (sessions' and jobs' sandboxes, the
reaper, schedules) are not people. For the caps to be per engineer, **add each engineer as a person**
(`hangar person add <name>`, which prints their login token); jobs triggered from GitHub or Slack have no person
yet and are capped per GitHub login or Slack user.

The caps sit beside the per-caller limits that already exist: `HANGAR_JOB_MAX_ACTIVE_PER_OWNER` (unfinished jobs per
caller, 10), the job rate limits, and `HANGAR_E2B_MAX_SANDBOXES_PER_OWNER`.

## Measured: the team load test

`scripts/bench/team_load.py` runs the real app on the fake provider (sandboxes are local directories; agents are
scripts that print an event every 0.5 s) and plays 6 engineers against it. Each has its own token and, at the
rates an MCP client and an open dashboard produce:

- starts 10 sessions over MCP, a second apart, and submits 2 jobs;
- polls each session with `get_session` every 5 s and sends the next message when a turn ends (2 turns each);
- calls `list_sessions`, `list_sandboxes` and `list_jobs` every 10 s, and `exec` (by sandbox name) every 4 s;
- refreshes `/api/sessions`, `/api/sandboxes` and `/api/jobs` every 5 s, and creates and destroys a sandbox over REST
  every 20–60 s.

Before it starts, the database gets 20,000 settled sandboxes and 5,000 finished jobs: about six months of a team's
history. The server reports event-loop lag (how late a 50 ms timer fires), RSS, CPU time, and the time spent in
SQLite per store method, on the event loop and in threads. `tests/test_team_scale.py::test_team_load_smoke` runs a
small version on every CI run and fails if any request scans the sandbox table on the event loop.

```
uv run python scripts/bench/team_load.py --out bench/results/<date>-team-load.json
uv run python scripts/bench/report.py <before.json> <after.json>
```

Results on a shared 2-vCPU VM (`bench/results/2026-10-14-team-load-*.json`). The fake agents (and hangar's turn
driver, which on a real provider runs in the sandbox) run on the same two vCPUs as the server, and other work shared
the machine, so the after numbers are upper bounds; a second after run, under a host load of ~9, shows how they
degrade. The request rates are the same in every column; the after runs finish the same work 7–9× sooner.

| 6 × 10 sessions, 20k records of history | Before (main, host load ~2) | After (host load ~1–4) | After (host load ~9) |
| --- | --- | --- | --- |
| Event-loop lag p50 / p95 / max | 1,296 / 5,221 / 8,326 ms | 1.0 / 70 / 258 ms | 4 / 263 / 765 ms |
| `get_session` (MCP) p50 / p95 | 3,364 / 5,914 ms | 42 / 305 ms | 214 / 807 ms |
| `list_sandboxes` (MCP) p50 / p95 | 13,363 / 17,034 ms | 60 / 553 ms | 434 / 1,755 ms |
| `exec` by name (MCP) p50 / p95 | 9,249 / 15,395 ms | 63 / 526 ms | 577 / 1,693 ms |
| `GET /api/sandboxes` p50 / p95 | 2,369 / 5,633 ms | 68 / 247 ms | 159 / 438 ms |
| Create a sandbox (REST) p50 | 21,213 ms (n=23) | 32 ms (n=1) | 1,847 ms (n=2) |
| An 8 s agent turn, send to done, p50 / p95 | 44.0 / 65.8 s | 8.6 / 10.8 s | 9.5 / 11.5 s |
| Job queue wait p50 / p95 (12 jobs) | 83 / 261 s (4 slots) | 0.01 / 0.06 s (24 slots, set with `--set job_concurrency=24`; the default is now 8) | 0.02 / 0.11 s |
| SQLite time on the event loop | 310 s of a 354 s run | 4.8 s of 38 s | 8.6 s of 49 s |
| Server CPU / peak RSS | 330 CPU-s / 1,031 MB | 7.7 CPU-s / 258 MB | 8.3 CPU-s / 264 MB |
| Wall time for the same work | 354 s | 38 s | 49 s |

Before, the server spent 87% of the run parsing sandbox records on the event loop: `fleet.list()` behind every
listing, `fleet.get(name)` behind every MCP call that names a sandbox, two scans per create. Every other request
waited behind them.

**Soak.** 6 × 10 sessions with 20 s turns (4 each, all 60 running at once) and 24 jobs, while retention moved the
20,000 seeded records to the archive (`2026-10-14-team-load-soak.json`): event-loop lag p50 0.6 / p95 47 / max
528 ms, `get_session` p50 14 / p95 545 ms, every turn and job finished, peak RSS 267 MB, 22.5 CPU-s in 117 s. The
first soak, with retention moving 500 rows a transaction, had a loop lag p95 of 811 ms: one batch held the store's
lock for up to 0.9 s. Hence 100.

### Months of job and session history

The job and session services used to load every job and session ever made at startup and keep them: ~30 KB for a
finished job with its timeline, so 18,000 jobs (six months at 100 a day) cost over half a GB, and every listing sorted
all of them. Now each holds what is live (unfinished jobs, jobs whose PR is still on its way to landed or with
auto-fix triggers waiting, live, suspending or busy sessions) and whatever was saved or asked for in the last
`HANGAR_RESIDENT_MINUTES` (60). Everything else is read back from the store when asked for, by id, by name, or
through the store's indexes (`resident.py`): the GitHub, Jira and Projects lookups (by PR number, branch, head commit,
ticket), idempotent retries, the inbox, the usage report and the daily caps. Every 5 minutes the retention loop lets
go of what nothing needs any more. A record read back stays one object while it's in use, so two callers never hold
copies that overwrite each other's changes.

Listings (`list_jobs`, `list_sessions`, `GET /api/jobs`, `GET /api/sessions`, the recent-work resources) are pages
from the store's indexes, newest first, filtered there by person: a member's listing is all of their own work, not
what of it was left after the team's newest 500. Each item carries a `cursor`; pass the last one's back for the next
page (REST also answers `X-Next-Cursor` when there is one). Finished jobs and settled sessions untouched for 30 days
move to `jobs_archive` and `sessions_archive` (below), which listings and lookups read too.

The team load test with that history seeded (6 people × 5 sessions, 12 jobs; 20,000 sandbox records, **18,000
finished jobs and 3,000 sessions over six months**; `--job-history 18000 --session-history 3000`), two runs each on
the shared 2-vCPU VM, `bench/results/2026-10-14-team-load-history-*.json`:

| | Before (build/ops) | After |
| --- | --- | --- |
| RSS at idle, history loaded | 735 / 736 MB | 213 / 213 MB |
| Peak RSS | 744 / 746 MB | 222 / 222 MB |
| Server CPU for the same work | 25.6 / 25.7 CPU-s | 14.5 / 16.5 CPU-s |
| Event-loop lag p95 / max | 69 / 881, 67 / 859 ms | 60 / 127, 61 / 132 ms |
| `list_jobs` (MCP) p50 / p95 | 115 / 225, 115 / 265 ms | 87 / 191, 77 / 168 ms |
| `list_sessions` (MCP) p50 / p95 | 139 / 245, 112 / 722 ms | 73 / 152, 63 / 134 ms |
| `GET /api/jobs` p50 / p95 | 76 / 178, 65 / 223 ms | 65 / 133, 42 / 124 ms |
| `GET /api/sessions` p50 / p95 | 74 / 126, 87 / 400 ms | 65 / 176, 65 / 134 ms |
| SQLite time on the event loop | 1.2 s | 6.5 s |

The loop now spends more time in SQLite (a page of jobs is read and decoded per listing, ~6 ms for 50) and less in
Python over every job in memory. Reading the pages in a worker thread was tried: decoding holds the GIL, so it saved
the loop nothing and raised the peak RSS by ~35 MB. The `hangar_jobs` and `hangar_sessions` gauges are counted by
SQLite in a worker thread on a connection of their own, at most a minute old.

What remains, by SQLite time on the loop: `get` of one sandbox by id (sessions check that theirs is alive, ~0.4 ms
each, 5k calls a run), `put` of sessions (every state change writes the whole record), and the token lookup in
`auth.principal`, which decodes every API token on every request (~1 ms; see follow-ups).

**Provider calls.** At 60 sessions and 12–24 jobs the fake saw ~30–60 `exec`s a second, plus 5–15 file reads and as many writes:
turn-status long-polls, the activity watch, checkpoints, setup. On GKE each of those is a pods/exec through the API
server unless hangar-envd answers it, so **envd must be up at team scale** (`HANGAR_ENVD=auto`, the default).

## GKE capacity for 60 sessions

### Node type: why AMD

GKE Sandbox (gVisor) uses Linux core scheduling against side channels, and **on Intel machine types it turns
Hyper-Threading off by default** ([GKE Sandbox][sandbox]); with SMT off a VM shows half its vCPUs
([Configure SMT][smt]), and it is still billed for all of them. AMD machine types keep SMT on. So the previous default,
an `n2-standard-8`, gives gVisor pods **4 usable vCPUs**, not 8: with 2-vCPU sandboxes reserving their full limit,
that is *one sandbox per node*, and `sandbox_max_nodes = 4` meant about 4 sandboxes in all. (This follows from the
docs; nobody has measured it on this cluster. `kubectl get nodes -o jsonpath='{..allocatable.cpu}'` on a sandbox
node shows it.)

The new default is **`n2d-standard-16`** (AMD EPYC, 16 vCPU, 64 GiB, Spot). The Kata pool needs a series with
nested virtualization, so `kata_machine_type` is unchanged. (Keeping Intel and turning SMT back on with
`--threads-per-core=2` is also possible, but Google warns it exposes MDS-vulnerable CPUs to side channels.)

### Packing

Allocatable per node, from GKE's reservation formulas ([plan node sizes][nodes]; node pools created on GKE 1.36 or
earlier, the conservative case) less an *estimate* of ~0.5 vCPU and ~1 GiB for the node's DaemonSets (Dataplane V2,
metadata server, logging and metrics agents, CSI):

| | n2-standard-8 (Intel, previous) | n2d-standard-16 (AMD, new) |
| --- | --- | --- |
| vCPUs gVisor pods see | 4 (SMT off) | 16 |
| CPU reserved by GKE | 80 m | 110 m |
| Memory reserved by GKE (+100 MiB eviction) | 3.66 GiB | 5.58 GiB |
| Left for sandboxes (*estimate*) | ~3.4 vCPU, ~27 GiB | ~15.4 vCPU, ~57.4 GiB |
| Sandboxes of 2 vCPU / 4 GiB, CPU requested in full (before) | **1** (CPU-bound) | 7 (CPU-bound) |
| Same, CPU request 1 vCPU (`HANGAR_K8S_CPU_REQUEST_FRACTION=0.5`) | 3 | **14** (memory-bound) |

Why request half the CPU: a coding agent spends most of a turn waiting on the model, then bursts (tests, builds).
Each sandbox keeps its 2-vCPU limit and its full 4 GiB; only the CPU it is *guaranteed* under contention halves.
Memory is never overcommitted, so nothing is OOM-killed for its neighbours. gVisor enforces both limits only when all
containers set them (they do) ([GKE Sandbox][sandbox]).

**Services** ([environments.md](environments.md#services)) are sidecars in the sandbox's pod, so their requests add to
it, up to `HANGAR_ENV_SERVICES_MAX_CPUS` and `HANGAR_ENV_SERVICES_MAX_MEMORY_GB` (2 and 2 GiB by default; the repo
writes each service's size, the budget caps the sum). A sandbox that uses the whole budget requests 2 vCPU and 6 GiB:
7 a node (CPU-bound) instead of 14. Size the node count for the share of sessions whose repos declare services.

**gVisor overhead.** Google gives no number for gVisor ("dynamic overhead with low base memory for the userspace
kernel") and declares no pod overhead for it ([GKE Sandbox][sandbox]). Google doesn't say where the sentry's memory is
charged; gVisor runs it in the pod's cgroup, so expect it to come out of the sandbox's own 4 GiB rather than the
node's (*estimate*: tens of MB per sandbox). Agents that need the whole 4 GiB for themselves should get a larger
`resources.memory_gb`.

**Disk is the next limit.** gVisor writes a sandbox's root filesystem (the workspace lives there) to a file in the
container's upper layer on the node's boot disk ([rootfs overlay][overlay]), so it counts as ephemeral storage. GKE
keeps 10% of the boot disk for eviction plus min(50%, 6 GiB + 35%, 100 GiB) for the system ([plan node
sizes][nodes]): a 200 GB disk leaves ~104 GiB for environment images and 14 workspaces, a 100 GB one ~49 GiB.
Sandboxes set no ephemeral-storage request unless their environment sets `resources.disk_gb`, so a full disk shows as
DiskPressure evictions, not as a scheduling wait. Keep `sandbox_disk_gb = 200` at 14 a node, and check
`kubectl describe node` on a busy node (*not measured*).

### How many nodes

| Load | Sandboxes | Nodes at 14 a node |
| --- | --- | --- |
| Typical day: ~60 live sessions (idle ones suspend after 30 min), ~12 jobs, warm pools | ~75 | 6 |
| Peak: `HANGAR_MAX_SANDBOXES` (sessions, job slots raised to 24, best-of-N attempts) | 120 | 9 |
| `sandbox_max_nodes` default (peak, plus a preempted Spot node's worth) | ~140 | **10** (was 4) |

**Quotas** to request before applying (check *IAM & Admin → Quotas* for your region): `N2D_CPUS` ≥ 160 for the
sandbox pool at its maximum (Spot VMs use preemptible CPU quota where the project has it), `CPUS_ALL_REGIONS` to
match, and `SSD_TOTAL_GB` ≥ 2,250: 10 sandbox nodes with 200 GB pd-balanced boot disks, the system pool at its
2 nodes of 100 GB, and the 20 GiB `hangar-data` volume (the default quota is 500 GB; pd-balanced counts against it,
[quotas][quotas]). Don't halve it with `sandbox_disk_gb = 100` at 14 sandboxes a node (see *Disk* above). Spot VMs
take standard `N2D_CPUS` quota unless the region has been granted preemptible quota. `hangar doctor` and the create
error name the quota when a scale-up is refused for one.

### The control plane

Measured on the fake provider at 60 sessions and 12–24 jobs: ~0.13–0.2 of a core on average (the event loop is one
thread), and 260–280 MB RSS (220 MB with six months of history: finished jobs and sessions stay in the store). Forks and snapshots add up to `HANGAR_WORKSPACE_TRANSFER_MB`
(1 GiB) of archives in flight, or one oversized archive at 2–3× its size. `deploy/k8s/deployment.yaml` now requests
`500m` / `1Gi` with a `3Gi` memory limit (was `250m` / `512Mi` / `2Gi`), which fits the `e2-standard-2` system node
(~6.1 GiB allocatable by the same formulas). Still one replica: state is one SQLite file.

### Cost

us-central1, **Spot** sandbox nodes (`sandbox_spot = true`, the default). The power schedule keeps the sandbox nodes up
10:00–01:00 ET, about 456 h a month (461 h since the power-up moved to 09:50 for the warm start). All prices checked
2026-10-14; the usage scenarios are *estimates*.

| Item | Price | Source |
| --- | --- | --- |
| n2d-standard-16, Spot | $0.327008 / h | [Spot VM pricing][spot] (the page's rows are not labelled by region; Iowa is listed first) |
| n2d-standard-16, on demand | $0.6759 / h | [Holori][holori] (third party, data from 2026-10-10; Google's general-purpose page did not render N2D rows) |
| pd-balanced boot disk | $0.000136986 / GiB-h (~$0.10 / GiB-month) | [Disk pricing][disks] |
| Cluster management fee | $0.10 / h, covered by the $74.40 monthly free-tier credit for one zonal cluster | [GKE pricing][gke] |

| Scenario (*estimate*) | Node-hours / month | Sandbox nodes, Spot | Boot disks (200 GB) | On demand instead |
| --- | --- | --- | --- | --- |
| Typical: 6 nodes for 9 h on 22 workdays, 2 nodes the rest of the powered hours | ~1,700 | ~$557 | ~$47 | ~$1,150 |
| Peak all powered hours: 10 nodes × 456 h | 4,560 | ~$1,491 | ~$125 | ~$3,082 |
| No power schedule, 10 nodes around the clock | 7,300 | ~$2,387 | ~$200 | ~$4,934 |

Not included: the load balancer, Artifact Registry, the session bucket, Cloud Build and egress. Spot prices can change
daily ([Spot VM pricing][spot]); committed-use discounts don't apply to Spot.

**The always-on control plane and the warm floor.** Only the sandbox pools power down now: hangar and the `system`
pool run around the clock (730 h a month), so the URL answers and GitHub, Slack and Jira events are queued overnight
([gke.md](gke.md#6-sandbox-nodes-off-at-night-optional)). And the gVisor pool keeps a warm floor of
`sandbox_warm_nodes` (1) while it's on, so the first create of the day doesn't wait for a node.

| Item (*estimate* from the prices above) | Hours / month | Per month |
| --- | --- | --- |
| e2-standard-2 system node, on demand: $0.067 / h ([gcloud-compute.com][e2], third party, Google's SKU prices updated 2026-10-11) | 730 | $48.91 |
| its 100 GB pd-balanced boot disk | 730 | $10.00 |
| **System pool, always on** | | **~$59** (a second node only under load) |
| Of which new: the night hours it used to be off (01:00–10:00) | 274 | **+$22** |
| Warm floor: one n2d-standard-16 Spot node and its 200 GB disk ($0.354 / h), all powered hours, at most | 461 | **+$163** |
| … when the pool would hold a node anyway on workdays (the typical scenario), and weekends sit idle | ~132 | ~+$47 |

The floor costs nothing extra in hours when sandboxes run anyway; it is the price of idle evenings and weekends with a
node up. `sandbox_warm_nodes = 0` gives it back (and the first create of the day waits for a node again: 1–2 min, plus
image pulls), as does a weekday-only `power_up_cron = "50 9 * * 1-5"` (then weekends stay off until someone runs
`hangar power up`). The prepull pulls from Artifact Registry in the same region, which costs no egress.

### Applying it

1. **Request the quotas above first**; a refused scale-up shows as sandboxes stuck "waiting for a node".
2. **Apply while the sandbox pools are powered down** (01:00–09:50 ET, after `power.sh down` has finished; with the
   power schedule the apply also brings back the gVisor pool's warm floor node, so run `power.sh down` again after),
   or suspend live sessions first (`hangar session suspend <name>`). The pool itself is not replaced: with the google provider ≥ 5.x
   (`versions.tf` asks for ≥ 6.0) `machine_type` is an in-place node pool update, and `terraform plan` shows
   `~ update in-place` on `google_container_node_pool.sandboxes`. GKE carries it out by replacing every node, so
   every sandbox on the pool is killed. Powered down, the pool has no nodes and the update is quick. With nodes up,
   GKE surges a node at a time, which needs a node's worth of spare quota. If the plan shows the pool being destroyed
   and created (`-/+`), stop: something else in it changed (`sandbox_spot`, for one, forces a new pool).
3. `terraform apply` in `deploy/terraform/gke` (or set `sandbox_machine_type` / `sandbox_max_nodes` in your tfvars to
   keep the old pool). The apply turns the pool's autoscaling back on at 0–10; the next `power.sh up` keeps that range
   rather than putting back the 0–4 it saved at `down`.
4. Deploy the control plane (`deploy/gke/up.sh`): the new manifest's requests and `HANGAR_MAX_SANDBOXES=120`. The first
   create after the deploy rewrites each environment's warm-pool template once (the CPU request changed), so those
   creates are cold.
5. Add each engineer as a person (`hangar person add <name>`), so the caps are per person. The whole procedure for
   this release, people included, is in [upgrading.md](upgrading.md#upgrading-to-the-team-release-75110).

On the first start, retention moves every settled record older than 7 days into the archive, 100 rows at a time in
the background (20,000 records took 201 batches, ~1.3 s of lock time in all, at most 0.11 s at once, during the
soak run below). Records a snapshot or a credential lease points at stay put and are never deleted: a fork of a
snapshot inherits the snapshot's sandbox's credentials, harness, labels and git mode.

### Upgrading and rolling back

The upgrade adds indexes and a `sandboxes_archive` table to the SQLite store when the new version first opens it
(no existing row is rewritten; 20,000 records index in well under a second). The previous version runs on the
upgraded file unchanged: it ignores the archive table and keeps the indexes up to date as it writes. What it can't
see is the archive. After a rollback, sandboxes stopped more than 7 days ago are gone from `list_sandboxes`
(`include_stopped`) and from lookups by id, which only matters for looking them up by hand. To give them back to the
old version, before rolling back:

```
kubectl -n hangar exec deploy/hangar -- python3 -c "import sqlite3; db = sqlite3.connect('/data/hangar.db'); \
db.execute('INSERT OR IGNORE INTO sandboxes(key, data) SELECT key, data FROM sandboxes_archive'); db.commit()"
```

The next upgrade archives them again. `HANGAR_SANDBOX_RETENTION_DAYS=0` turns archiving off.

**Jobs and sessions** get the same treatment, and more indexes (person, owner, ticket, idempotency key, PR number,
branch, head commit, landing state, name, job, parent, conversation; and per person by date for listings), in the main
and the archive tables. The first start indexes the existing rows: ~2 s for 21,000 jobs and sessions on the 2-vCPU
VM, before hangar answers. Finished jobs untouched for `HANGAR_JOB_RETENTION_DAYS` (30) and settled sessions for
`HANGAR_SESSION_RETENTION_DAYS` (30) move to `jobs_archive` and `sessions_archive`, in batches of 100, and are never
deleted. Not archived, whatever their age: a job whose PR is open or approved, or merged or closed with its session
not yet ended, one with auto-fix triggers waiting, a schedule's last job (its next run updates that PR), a session
with checkpoints (it can always be resumed), and a session a job in the main table points at (the job goes first).
A record changed again (a follow-up on an archived job) moves back to the main table. Before rolling back to a version
without the archive, hand them back as above:

```
kubectl -n hangar exec deploy/hangar -- python3 -c "import sqlite3; db = sqlite3.connect('/data/hangar.db'); \
[db.execute(f'INSERT OR IGNORE INTO {t}(key, data) SELECT key, data FROM {t}_archive') for t in ('jobs', 'sessions')]; \
db.commit()"
```

## Limits and follow-ups

- **Budgets before concurrency.** Auto job slots stop at 8 (`fairness.AUTO_JOB_SLOTS_MAX`), not 24. Job budgets
  and daily spend caps per repo and per person ([repo-policy.md](repo-policy.md#budgets)) are opt-in, so on defaults
  nothing bounds what a job spends across its follow-ups, verify-fix and auto-fix turns, and the per-person cap
  bounds one person, not a GitHub label storm across many. A slot is also a job, not a sandbox: 24 best-of-4 jobs
  could hold all 120 sandboxes, and every new session would be refused at `max_sandboxes`. Once
  `HANGAR_JOB_BUDGET_MAX_COST_USD` and the repos' `max_usd_per_day` (or `HANGAR_PERSON_MAX_USD_PER_DAY`) are set,
  `HANGAR_JOB_CONCURRENCY=24` gives the measured 24.
- **The reconciler** reads only live and unfinished sandboxes, from the state index (`reconciler.sandboxes_in`), once
  per pass on its own task. `hangar gc` still lists every record; it is an admin's occasional command.
- **Jobs and sessions in memory** are the live ones and the last hour's ([above](#months-of-job-and-session-history)).
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
[holori]: https://calculator.holori.com/gcp/vm/n2d-standard-16
[overlay]: https://gvisor.dev/blog/2023/05/08/rootfs-overlay/
[quotas]: https://docs.cloud.google.com/compute/quotas
[e2]: https://gcloud-compute.com/e2-standard-2.html
