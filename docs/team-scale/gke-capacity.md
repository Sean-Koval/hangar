# GKE capacity for 60 sessions

This page is part of [Team scale](../team-scale.md).

## Node type: why AMD

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

## Packing

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

**Services** ([environments.md](../environments/services.md)) are sidecars in the sandbox's pod, so their requests add to
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

## How many nodes

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

## The control plane

Measured on the fake provider at 60 sessions and 12–24 jobs: ~0.13–0.2 of a core on average (the event loop is one
thread), and 260–280 MB RSS (220 MB with six months of history: finished jobs and sessions stay in the store). Forks and snapshots add up to `HANGAR_WORKSPACE_TRANSFER_MB`
(1 GiB) of archives in flight, or one oversized archive at 2–3× its size. `deploy/k8s/deployment.yaml` now requests
`500m` / `1Gi` with a `3Gi` memory limit (was `250m` / `512Mi` / `2Gi`), which fits the `e2-standard-2` system node
(~6.1 GiB allocatable by the same formulas). Still one replica: state is one SQLite file.

## Applying it

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
   this release, people included, is in [releases/team-release.md](../releases/team-release.md).

On the first start, retention moves every settled record older than 7 days into the archive, 100 rows at a time in
the background (20,000 records took 201 batches, ~1.3 s of lock time in all, at most 0.11 s at once, during the
soak run below). Records a snapshot or a credential lease points at stay put and are never deleted: a fork of a
snapshot inherits the snapshot's sandbox's credentials, harness, labels and git mode.

## Upgrading and rolling back

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
