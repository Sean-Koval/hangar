# Infra states on GKE

This page is part of [Running hangar on GKE](../gke.md).

| State | What runs | What it costs a week (*estimate*, [team-scale.md](../team-scale/cost.md)) | Webhooks | Jobs and sessions | A merge to main ([cicd.md](../cicd/infra-states.md)) | How to get back |
| --- | --- | --- | --- | --- | --- | --- |
| **up** | System node, hangar, sandbox pools (gVisor at its warm floor, autoscaling) | Idle, the warm floor around the clock ~$78; a typical team week on the schedule ~$157 | Received | Run | Builds, waits for your approval, deploys | - |
| **down** (nightly, 01:00 to 09:50 ET) | System node and hangar; sandbox pools at 0 nodes | The system node, ~$18 for a whole week down | Received and queued | Queued jobs wait; creates answer at once (`503` with `Retry-After`); sessions are suspended (checkpointed) and resume on their next message | Skips green; deploys on the next power-up | The 09:50 power-up, `hangar power up`, `power.sh up` |
| **night** (nightly with the overnight lane on) | As **down**, plus one Spot node of `sandboxes-overnight` | **down** plus ~$20 a week | Received and queued | Lane jobs (overnight repos' intake jobs, `lane: overnight`, evals) run, up to 7; everything else as **down** | As **down** | The 09:50 power-up (the lane's node goes once the pools are up), `hangar power up`, `power.sh up` |
| **held** | Whatever ran when the hold began; the scheduled power-ups and power-downs skip | That state's | That state's | That state's | Skips green while held | Ends by itself at its date (the schedule resumes at its next time; nothing powers up early); `release`; `up` ends it and powers on |
| **off** | Nothing: every pool at 0 nodes, hangar at 0 replicas | ~$5, plus the management fee if the free tier is spent elsewhere | **Not received** (the load balancer answers 502): GitHub's are redelivered on the next start if at most 3 days old; Jira's and Slack's are lost | Nothing runs; queued jobs and suspended sessions wait in the store and carry on after `up` | Skips green; `power.sh up` deploys main | `power.sh up`; or the first scheduled power-up after the hold `off` set ends |

**Holds.** A hold pauses the schedule in whatever state it finds: held up (working late: the 01:00 power-down skips)
or held down (the pools stay off past 09:50). It lives in the cluster's resource labels (`hangar-power-hold`, when it
ends, and `hangar-power-hold-set`, when it last changed), beside the saved autoscaling ranges, so the scheduled Cloud
Run jobs read it with one `gcloud` call even while hangar is off. Each scheduled run looks at it first and exits with a
log line (`held until Mon 26 Oct 00:00 EDT: skipping the scheduled power-up`). `hangar power hold` and `release`
(`POST /api/admin/power/hold`, `/release`, admins; no MCP tool) are recorded in hangar instead, because hangar can't
change the cluster's labels and gets no permission to (`container.clusters.update` would let it change anything about
the cluster): each scheduled run reads hangar's record before deciding and the newer of the two wins on both sides.
So `hangar power hold` takes effect at the next scheduled run, and `power.sh hold` at once. When a hold ends, the next
scheduled run clears it and does its job; nothing powers up early. `hangar power`, `hangar status` and `hangar doctor`
show it, and hangar's own "sandboxes are back at" moves to the first power-up after it. `hangar power up` and
`power.sh up` end a hold; holds last at most 90 days. The newer-wins copy compares the clocks of whoever set each side
(your laptop's for `power.sh`), which are assumed to be in sync.

**One run at a time, and failing closed.** `down`, `up` and `off` take a lock (the `hangar-power-lock` label, with
the time of the run's last step): a run that finds another under way does nothing and fails, saying so (a scheduled
job then shows as failed and retries once; a `hangar power up` that hits it leaves hangar's state to go stale, which
`hangar doctor` reports). A lock not refreshed for 45 minutes belonged to a run that died and is taken over. It's a
best effort: GKE's labels can't be compare-and-swapped through `gcloud`, so two runs started in the same second could
both take it. The scheduled
power-down that can't reach hangar (no kubectl, or no answer) doesn't power down: without hangar there's no drain, so
running turns would be cut off, and a `hangar power hold` couldn't be read. The job fails, visibly, and the pools stay
up; `power.sh down` by hand still goes ahead. A pool's saved range is cleared only once its resize back has worked,
and a size that can't be read stops `down` before that pool goes, so a failed run never loses what `up` needs.

**Off.** `power.sh off` first sets the hold (7 days, or `--days` / `--until`) and the `hangar-power-off` label, so a
scheduled power-up meanwhile skips. Then it does what `down` does (tell hangar, drain, suspend sessions, checkpoint
running turns, the sandbox pools to 0), scales hangar to 0 and waits for its pod to be gone, and only then takes the
system pool to 0 nodes. If hangar's pod hasn't stopped after 3 minutes, the system node stays and `off` says so: the
node goes only once nothing can be writing the database. It needs `kubectl`, and without it changes nothing. Nothing
is deleted:

- The `hangar-data` volume, with the SQLite store, survives its node: Kubernetes' persistent volumes "exist
  independently of Pods", and "the disk and data represented by a PersistentVolume continue to exist as the cluster
  changes" ([GKE persistent volumes][gkepv]). The disk is deleted only with its PersistentVolumeClaim (the default
  `reclaimPolicy: Delete`), and nothing here deletes a claim. Node boot disks go with their nodes; they hold nothing
  of hangar's.
- The cluster, the bucket with its checkpoints and backups, Artifact Registry, the static IP and the load balancer stay.
- GKE's management fee is charged "irrespective of the mode of operation, cluster size, or topology" ([GKE
  pricing][gkeprice]), so count on it while off too. The free tier's $74.40 a month per billing account covers one zonal
  cluster, this one if it's your only one.
- [GKE Sandbox][gkesandbox] wants a pool without it to "contain at least one node, even if all your workloads are
  sandboxed". Off, nothing runs at all, and `up` brings the system pool back before any sandbox node. If GKE ever
  refuses the system pool 0 nodes, `off` says so and leaves its node.

`up` from off brings back the system pool, waits for its node, starts hangar and waits for its rollout, tells hangar
the sandboxes are starting, then brings back the sandbox pools at their warm floor with the prepull, tells hangar
they're up, and ends the hold. hangar's first start asks GitHub to redeliver what it missed (3 days at most, GitHub's
limit: [redelivering webhooks][redeliver]); [cicd.md](../cicd/infra-states.md#taking-a-week-off) has the week-off recipe, and what
is lost.

**Deploys and power.** `power.sh up` by hand ends by starting `deploy.yml` for main when main is ahead of what hangar
runs (it waits for your approval; [cicd.md](../cicd.md#merges-to-main-deploy-while-the-infra-is-up)). The scheduled
power-up holds no GitHub credential and only reports it: set `power_deploy_repo` and `hangar power` shows the deploy
pending with its command.

**While the pools are off**, hangar keeps working on everything that doesn't need a sandbox:

- GitHub, Slack and Jira events (and schedules, MCP `submit_job`) are accepted and queued as usual. Queued jobs wait with the status `waiting: sandboxes power on at 10:00 EDT (in 8 h 12 min)`, and start a few at a time (`HANGAR_JOB_RESTART_STAGGER_SECONDS` apart) once the pools are up.
- A create (`create_sandbox`, REST, `new_session`, a message to a suspended session) answers at once with a plain message and when sandboxes are back, instead of waiting for a node that won't come: REST answers `503` with `Retry-After`. Nothing is left half made.
- Sessions and the dashboard are readable; suspended sessions resume when their next message arrives after power-up.

`down` tells hangar first (`POST /api/admin/power/state`), so new creates are refused from that moment, then drains (`POST /api/admin/drain`): queued jobs wait instead of starting, and running experiment trials are cancelled as `infra_error` / `power_down` and run again in the next eval window (20:00, not at power-up: [team-scale.md](../team-scale.md#the-eval-queue-class)); queued eval jobs wait for that window too. It suspends idle sessions and waits up to 10 minutes for running agent turns to finish. Turns still running then get a fresh checkpoint, and it waits up to 3 more minutes for sandbox creates, destroys and suspends already under way. Only then do the pools go. A job turn the pools take away anyway resumes from its checkpoint after power-up (at most `HANGAR_JOB_RESTART_RESUMES` times), as after a restart, and so does each best-of-N attempt. Each pool's autoscaling range (or a fixed pool's node count, for the microVM pool) is saved as a label on the cluster, and `up` restores it; a range a `terraform apply` set while the pools were down wins over the saved one.

**Warm start.** `up` runs at 09:50 so that sandboxes are ready by 10:00:

1. The gVisor pool comes back at its warm floor, `sandbox_warm_nodes` (default 1 with the power schedule: the pool's autoscaling minimum while it's on).
2. Once the floor node is ready, `up` applies the `hangar-prepull` DaemonSet (namespace `hangar`), which hangar renders from `hangar images plan`: every environment image and harness pack the registry has, by digest, mounted as [image volumes][imagevol], so each sandbox node pulls them into its image store as it joins and keeps them from image garbage collection. Nothing in those images runs; its one container is `registry.k8s.io/pause`. It waits up to `PREPULL_TIMEOUT` (600 s) for the pull, and later nodes the autoscaler adds pull the same set as they join.
3. It tells hangar the pools are up: the jobs that waited start, and each environment's warm pool is sized for the morning. `HANGAR_POWER_WARM_POOLS=auto` (the default) takes, for each environment, the most of its sandboxes created in the hour after the ready time on any of the last 7 days (forks excluded), at most `HANGAR_POWER_WARM_MAX` (3); an environment's own `warm_pool` is the minimum. `env=N,env2=M` sets them instead, `off` turns it off. The sizes hold until the next power-down.

`scripts/acceptance/gke.sh`'s `warmstart` check measures what this buys: two creates back to back on an environment with a warm pool, the first claiming the warm pod and the second starting cold.

To run it on a schedule, set these in `terraform.tfvars` and run `terraform apply`:

```hcl
enable_power_schedule = true
power_up_cron         = "50 9 * * *"   # 9:50: the floor node and the prepull
power_warm_minutes    = 10             # hangar tells people sandboxes are ready at 10:00
power_down_cron       = "0 1 * * *"    # 1 AM
power_time_zone       = "America/New_York"
# sandbox_warm_nodes  = 1              # gVisor nodes kept while on (default 1 with the schedule)
```

This creates two Cloud Run jobs (`hangar-power-up`, `hangar-power-down`) that run the same script with `POWER_SCHEDULED=1` (so a hold skips them), Cloud Scheduler entries that start them, and a `hangar-power` service account with `roles/container.admin`. `power_deploy_repo = "OWNER/REPO"` (a public repository) has the power-up report a deploy pending to hangar. The jobs pass the cron, time zone and warm minutes to hangar, which tells people when sandboxes are back. To run one by hand: `gcloud run jobs execute hangar-power-up --region <region> --wait`.

**Powering on early.** `hangar power up` (`POST /api/admin/power/up`, admins) runs the `hangar-power-up` job through the Cloud Run Admin API's [`jobs.run`][jobsrun], as hangar's own service account; terraform grants it `roles/run.invoker` on that job, which is what [executing a job][execute] needs (overrides would need more; hangar sends none). Set `HANGAR_POWER_UP_JOB` to `terraform output -raw power_up_job` (`deploy/gke/up.sh` does it). `--wait` waits until the pools report they're on. There is no MCP tool for it: the operator view stays REST and CLI, admin-scoped.

**If power.sh can't reach hangar**, hangar's `down` or `starting` state ends on its own an hour after the time it said sandboxes would be ready, and `hangar doctor` says the power-up never reported. `hangar doctor` and `hangar power` also show the last power-up (images pulled, how long, warm pools), the last deploy and its acceptance result, and whether hangar's background loops (reaper, reconciler, sessions, schedules) are passing on time.

While the pools are off you pay for the system node, disks, images, the bucket and the load balancer, not for sandbox nodes ([team-scale.md](../team-scale/cost.md) has the numbers); while off, not for the system node either. GKE's free tier covers the management fee of one zonal cluster per billing account. GKE Sandbox also needs a node pool without it that keeps at least one node ([GKE Sandbox][gkesandbox]), which the system pool is, always on except while off.

Things to know:

- `terraform apply` while the pools are off turns the gVisor pool's autoscaling back on at its floor, so a node starts. Run `power.sh down` again after (it keeps the range the apply set for the morning).
- `up` also brings back a cluster an older `power.sh` powered down entirely (the system pool and a hangar scaled to 0).
- Set `HANGAR_POWER_PROVIDERS` to the providers whose sandboxes run on the powered pools (default `["kubernetes"]`; add `e2b` when it runs on the microVM pool).

[imagevol]: https://www.k8s.dev/resources/keps/4639/
[jobsrun]: https://docs.cloud.google.com/run/docs/reference/rest
[execute]: https://docs.cloud.google.com/run/docs/execute/jobs
[gkesandbox]: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/sandbox-pods
[gkepv]: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/persistent-volumes
[gkeprice]: https://cloud.google.com/kubernetes-engine/pricing
[redeliver]: https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks
