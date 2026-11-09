# What 60 sessions cost on GKE

This page is part of [Team scale](../team-scale.md).

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
([gke.md](../gke/power.md)). And the gVisor pool keeps a warm floor of
`sandbox_warm_nodes` (1) while it's on, so the first create of the day doesn't wait for a node.

| Item (*estimate* from the prices above) | Hours / month | Per month |
| --- | --- | --- |
| e2-standard-2 system node, on demand: $0.067 / h ([gcloud-compute.com][e2], third party, Google's SKU prices updated 2026-10-11) | 730 | $48.91 |
| its 100 GB pd-balanced boot disk | 730 | $10.00 |
| **System pool, always on** | | **~$59** (a second node only under load) |
| Of which new: the night hours it used to be off (01:00–10:00) | 274 | **+$22** |
| Warm floor: one n2d-standard-16 Spot node and its 200 GB disk ($0.354 / h), all powered hours, at most | 461 | **+$163** |
| … when the pool would hold a node anyway on workdays (the typical scenario), and weekends sit idle | ~132 | ~+$47 |

**The overnight lane** (#186, off by default; [gke.md](../gke/power.md#the-overnight-lane)). One e2-standard-16 Spot node
through the night while the lane is on: 9 h × $0.3217 ([gcloud-compute.com][e2-16], us-central1, fetched 2026-10-18)
× 30 ≈ **$87 a month** (*estimate*), plus its 200 GB disk for those hours, 270 h × $0.027 ≈ $7. E2 rather than the
sandbox pool's N2D because it takes the regional `CPUS` quota, not `N2D_CPUS` (which a project sized for the sandbox
pool may have used in full). E2 lands on Intel or AMD, and GKE Sandbox turns SMT off on Intel, so the node shows 8 or
16 vCPUs: it runs up to 7 standard sandboxes (`HANGAR_OVERNIGHT_LANE_SANDBOXES`; 14 on AMD or an n2d-standard-16, the
[packing](gke-capacity.md#packing) of one node), shared by
overnight repos' intake jobs and evals. The plan turns it on only once agent PR yield ([weekly metrics](../metrics.md)) is at least 60%
(`hangar power overnight on` refuses below that without `--force`).

The floor costs nothing extra in hours when sandboxes run anyway; it is the price of idle evenings and weekends with a
node up. `sandbox_warm_nodes = 0` gives it back (and the first create of the day waits for a node again: 1–2 min, plus
image pulls), as does a weekday-only `power_up_cron = "50 9 * * 1-5"` (then weekends stay off until someone runs
`hangar power up`). The prepull pulls from Artifact Registry in the same region, which costs no egress.

**A week in each infra state** (--- infra-state; [gke.md](../gke/infra-states.md)). All *estimates*, from the prices
above and these, checked 2026-10-15: a global forwarding rule, $0.025 / h for the first five ([load balancing
pricing][lb]: GKE's Ingress needs one or two, both within that rate); the `hangar-data` volume, 20 GiB of pd-balanced
at the disk price above; the management fee, $0.10 / h, charged "irrespective of ... cluster size" ([GKE
pricing][gke]), so with no nodes too, and covered by the free tier for one zonal cluster. Not included, the same in
every state: the bucket, Artifact Registry storage, the static IP (its price wasn't re-checked here) and egress.

| Held for a whole week in (*estimate*) | What's billed | Per week |
| --- | --- | --- |
| **off** (`power.sh off`) | Load balancer rule $4.20, `hangar-data` disk $0.46 | **~$5** (+$16.80 when the free tier is spent on another cluster) |
| **down** (held down: sandbox pools at 0) | + the system node $11.26 and its 100 GB disk $2.30 | **~$18** |
| **up, on the nightly schedule, idle** | + the warm floor node and its disk, ~106 powered hours × $0.354 | **~$56** |
| **up, on the nightly schedule, a typical team week** | + ~390 sandbox node-hours (the typical scenario above) | **~$157** |
| **up around the clock, idle** (no schedule, or held up) | + the warm floor node, 168 h | **~$78** |
| **up around the clock, 10 nodes busy** | 1,680 node-hours | **~$613** |

So a week off instead of letting the schedule run saves about $50 idle and about $150 in a working week; off instead
of held down saves the system node, ~$13 a week, and costs what's lost while nothing receives webhooks
([cicd.md](../cicd/infra-states.md#taking-a-week-off)).
