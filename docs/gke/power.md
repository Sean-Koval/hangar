# Sandbox nodes off at night

This page is part of [Running hangar on GKE](../gke.md).

`deploy/gke/power.sh` scales the sandbox node pools (gVisor, Kata and microVM: every pool whose name starts with `sandboxes-`) to zero at night and back in the morning. **hangar and the `system` pool stay up**, so the URL keeps answering overnight. Nothing is deleted: disks, images, session checkpoints and the IP stay.

```bash
bash deploy/gke/power.sh down     # drain, suspend sessions (each checkpoints), tell hangar, sandbox pools to 0 nodes
bash deploy/gke/power.sh up       # pools back (gVisor at its warm floor), images pulled onto it, tell hangar (~5-10 min)
bash deploy/gke/power.sh status   # pools, nodes, the hold, and what hangar thinks
bash deploy/gke/power.sh hold --days 3          # the schedule skips until then (or --until 2026-10-22)
bash deploy/gke/power.sh release                # end the hold: the schedule runs from its next time
bash deploy/gke/power.sh off [--days 7]         # everything to 0 nodes, hangar too; held so it stays off
hangar power                      # the same from anywhere: on or off, when they're back, jobs waiting, the hold
hangar power up                   # an admin powers the pools on early (runs the power-up job; ends a hold)
hangar power hold --days 3        # admins: the hold, through hangar; `hangar power release` ends it
hangar power overnight on         # admins: the overnight lane (below); needs 60% agent PR yield, or --force
```

## The overnight lane

With the pools down 01:00–10:00, a ticket moved to "Ready for agent" at 23:00 waits for the morning. The overnight lane
keeps **one Spot e2-standard-16** up through the night for the work that should not wait: intake jobs of repos whose
policy says `overnight: true` (`hangar repo set acme/api --overnight`, [repo-policy.md](../repo-policy.md)), intake jobs
labelled `lane: overnight`, and eval-class jobs ([team-scale.md](../team-scale.md#the-eval-queue-class)). About $87 a month
([team-scale.md](../team-scale/cost.md)); evening tickets become PRs waiting at the 10:00 triage.

1. `enable_overnight_lane = true` in `deploy/terraform/gke`, then `terraform apply`: a `sandboxes-overnight` pool (gVisor,
   Spot, fixed size, 0 nodes), labelled and tainted `hangar.dev/lane=overnight`. Every gVisor sandbox tolerates the
   taint, so at night, with every other sandbox pool at 0, sandboxes land on it; by day it has no nodes. The node is an
   `e2-standard-16` (`overnight_machine_type`), not the sandbox pool's `n2d-standard-16`: E2 takes the regional `CPUS`
   quota, so the lane needs no `N2D_CPUS` the sandbox pool may already use in full. **The owner runs `terraform apply`**;
   nothing in hangar creates the pool.
2. `hangar power overnight on` (or `bash deploy/gke/power.sh overnight on`). Refused unless the last 14 days' agent PR
   yield is at least 60% (`hangar metrics weekly`; `HANGAR_OVERNIGHT_MIN_YIELD`): a lane that produces PRs nobody
   merges only adds to the morning's review queue. `--force` turns it on anyway, audited (`power.overnight.on`,
   `forced: true`). `off` turns it off and scales a running lane node to 0.
3. From then on, `power.sh down` leaves the overnight pool at **exactly one node** and tells hangar **`night`** instead of
   `down`; the morning `up` scales it back to 0 once the other pools are ready and hangar knows they are.

In `night`, hangar admits lane jobs up to `HANGAR_OVERNIGHT_LANE_SANDBOXES` (7: E2 lands on Intel or
AMD, GKE Sandbox turns SMT off on Intel, and 7 sandboxes fit the 8 vCPUs that leaves; set 14 if
`kubectl get nodes -l hangar.dev/lane=overnight -o jsonpath='{..allocatable.cpu}'` shows 16, or with an
`n2d-standard-16`, [team-scale.md](../team-scale/gke-capacity.md#packing)); the rest wait for the morning as in `down`, and creates for anything else still
answer at once with when the pools are back. Review-aware admission ([team-scale.md](../team-scale.md#review-capacity))
still applies at night: a requester past their open-agent-PR cap waits as by day. A Spot preemption has no guaranteed
replacement: the lane's running turns are marked `infra_error/preempted` and wait for 10:00, when they resume from their
checkpoints like any turn the power-down cut off. A deploy in the window ([team-scale.md](../team-scale/gke-capacity.md#applying-it))
must allow for the one live node: suspend its sessions or leave sandbox node changes for the day.
