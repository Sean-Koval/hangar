# Infra states and deploys

This page is part of [Deploying from CI/CD](../cicd.md).

| State | What runs | What it costs a week (*estimate*, [team-scale.md](../team-scale/cost.md)) | Webhooks | Jobs and sessions | A merge to main | How to get back |
| --- | --- | --- | --- | --- | --- | --- |
| **up** | System node, hangar, sandbox pools (gVisor at its warm floor, autoscaling) | Idle, the warm floor around the clock ~$78; a typical team week on the schedule ~$157 | Received | Run | Builds, waits for your approval, deploys | - |
| **down** (nightly, 01:00 to 09:50 ET) | System node and hangar; sandbox pools at 0 nodes | The system node, ~$18 for a whole week down | Received and queued | Queued jobs wait (`waiting: sandboxes power on at 10:00`); creates answer `503` with `Retry-After`; sessions are suspended (checkpointed) and resume on their next message | Skips green; deploys on the next power-up | The 09:50 power-up, `hangar power up`, or `bash deploy/gke/power.sh up` |
| **held** (`power.sh hold`, `hangar power hold`) | Whatever ran when the hold began: the scheduled power-ups and power-downs skip until it ends | That state's | That state's | That state's | Skips green while held | It ends by itself at its date and the schedule resumes at its next time (nothing powers up early); `power.sh release` / `hangar power release` end it now; `power.sh up` / `hangar power up` end it and power on |
| **off** (`power.sh off`) | Nothing: every pool at 0 nodes, hangar scaled to 0. Kept: the cluster, the `hangar-data` disk with the SQLite store, the bucket and checkpoints, images, the IP and the load balancer | ~$5, plus GKE's management fee when the free tier is spent on another cluster | **Not received** (the load balancer answers 502). GitHub's are redelivered on the next start if at most 3 days old; Jira's and Slack's are lost | Nothing runs; queued jobs and suspended sessions wait in the store and carry on after `up` | Skips green; `power.sh up` deploys main | `bash deploy/gke/power.sh up`; or when the hold `off` set ends, the next scheduled power-up brings everything back (without the catch-up deploy) |

## Taking a week off

```bash
hangar status                                 # anything running you want to finish first?
bash deploy/gke/power.sh off --until 2026-10-26   # drain, checkpoint and suspend sessions, then everything to 0
                                              # nodes; held until then so the schedule leaves it off
# ... keep developing: merges build images (images.yml) and skip the deploy, green
bash deploy/gke/power.sh status               # cluster: off (everything at 0 nodes); held until Mon 26 Oct 00:00
bash deploy/gke/power.sh up                   # system pool, hangar (ready), sandbox pools, warm floor and prepull;
                                              # ends the hold; starts deploy.yml for main if it's ahead: approve it
hangar doctor                                 # the deploy, the loops, anything stale
```

While it's off, nothing receives webhooks. On its next start hangar asks GitHub to redeliver the deliveries it missed,
but GitHub keeps them for 3 days only ([redelivering webhooks][redeliver]: "You can redeliver webhook deliveries that
occurred in the past 3 days"), so a `/hangar` label or comment from earlier in the week is lost: add it again. Jira
retries a failed webhook up to five times, 5 to 15 minutes apart ([Jira webhooks][jirahooks]), and has no
redelivery, so a Jira issue moved during the week is lost: move it again. Slack events and slash commands are lost
too. A hold from `off` ends by itself at its date, and the next scheduled power-up then brings everything back; pick a
date after your return, or come back with `power.sh up`.
