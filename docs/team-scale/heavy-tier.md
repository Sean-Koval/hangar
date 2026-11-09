# The heavy tier at team scale

This page is part of [Team scale](../team-scale.md).

Sandboxes with Docker inside, and the `large` (8 vCPU / 32 GiB / 100 GiB) and `xlarge` (16 / 64 / 200) profiles, run
as Kata microVMs on their own pool, `sandboxes-heavy` (terraform's `enable_heavy_pool`;
[environments.md](../environments/heavy-tier.md), [gke.md](../gke/microvms.md#4a-the-heavy-tier-optional)). It scales from zero
and powers down with the other sandbox pools, so it costs nothing while nobody runs heavy work.

## Machine type

Kata boots a VM per pod, so the pool needs nested virtualization: C3, C4, C4D, N2, N4 or N4D. Compute Engine
excludes E2, memory-optimized, Arm, and AMD machines other than N4D, and states a CPU penalty of 10% or more for
nested VMs, more for I/O ([nested virtualization][nested]). GKE Sandbox's Hyper-Threading rule is gVisor's, so it
doesn't halve an Intel Kata node. Prices for us-central1 from [gcloud-compute.com][gc-n2] (third party, Google's SKU
prices, fetched 2026-10-15; Spot prices change daily):

| Machine | vCPU / GiB | On demand / h | Spot / h | Large sandboxes a node (by memory) | Spot per large / h |
| --- | --- | --- | --- | --- | --- |
| n2-standard-32 (**default**) | 32 / 128 | $1.5539 | $0.8854 | 3 | $0.30 |
| c3-standard-44 | 44 / 176 | $2.2177 | $0.8332 | 5 | $0.17 |
| n4-standard-32 (Hyperdisk only) | 32 / 128 | $1.4512 | $0.8194 | 3 | $0.27 |
| n2-standard-16 | 16 / 64 | $0.7769 | $0.4427 | 1 | $0.44 |

The default is N2, the series the Kata pool already uses and whose quota (`N2_CPUS`) projects usually have. On Spot,
`c3-standard-44` holds more per dollar; check your `C3_CPUS` quota first. A `large` gVisor-sized comparison: an
n2d-standard-16 Spot node holds 14 standard sandboxes at ~$0.023 each an hour, so a large heavy sandbox costs about
13 standard ones.

## Packing an n2-standard-32

From GKE's reservation formulas ([plan node sizes][nodes], the conservative case) and kata-deploy's RuntimeClass
overhead for its QEMU runtime (320 MiB and 250 m a pod, [kata-deploy chart][kata-rc]); *estimates*, not measured:

| | |
| --- | --- |
| Memory left for pods | 128 GiB − 9.4 GiB (GKE) − ~1 GiB (DaemonSets) ≈ **117.6 GiB** |
| A large sandbox requests | 32 GiB + 0.31 (Kata) memory; 4 + 0.25 vCPU (`HANGAR_K8S_CPU_REQUEST_FRACTION=0.5`); 100 GiB disk |
| An xlarge one | 64.3 GiB; 8.25 vCPU; 200 GiB disk |
| Fits | 3 large, or 1 xlarge and 1 large, plus standard-size Docker sandboxes (4.3 GiB, 30 GiB disk each) in what's left |
| Disk | a 500 GB boot disk leaves ~319 GiB of ephemeral storage after GKE's 10% eviction threshold and 100 GiB system reserve; 3 large request 300. Images share the rest of the disk |

Memory decides, as on the gVisor pool, and a sandbox's Docker (its containers, its page cache) lives inside its own
memory limit: a stack that needs more than the profile gives is OOM-killed inside the VM, not on the node.

## How many nodes, and the caps

`HANGAR_HEAVY_PERSON_MAX` caps what one person holds: 3 `large` or larger, at most 1 of them `xlarge`. Work no person
owns (jobs from senders linked to nobody, webhooks, schedules without a person) counts as one more person, a team
bucket with the same caps; repo builds aren't counted. Six people at their caps would need ~7 nodes; `heavy_max_nodes = 4` (12 large) assumes about two heavy sandboxes per person at
the busiest time. Past it, sandboxes wait with "the sandbox node pool is at its maximum". Quota: `N2_CPUS` ≥ 128 for
4 nodes, on top of the Kata pool's, and 2,000 GB of `SSD_TOTAL_GB` for their disks.

## Cost

*Estimates* from the prices above, with each node's 500 GB pd-balanced boot disk ($0.0685 / h while the node exists;
the autoscaler deletes it with the node):

| Scenario | Node-hours / month | Spot | On demand |
| --- | --- | --- | --- |
| Light: 2 nodes for 6 h on 22 workdays | 264 | ~$252 | ~$428 |
| Typical: 3 nodes for 9 h on 22 workdays | 594 | ~$567 | ~$964 |
| Busiest: 4 nodes all powered hours (461 h) | 1,844 | ~$1,759 | ~$2,992 |
| A warm floor of 1 node all powered hours (why heavy warm pools are off) | 461 | ~$440 | ~$748 |

Not included: the nested-virtualization penalty (CPU-bound work runs ~10% slower, I/O more), and the Docker Hub
remote repository's storage (Artifact Registry, cleaned after 30 days).

[nested]: https://docs.cloud.google.com/compute/docs/instances/nested-virtualization/overview
[gc-n2]: https://gcloud-compute.com/n2-standard-32.html
[kata-rc]: https://github.com/kata-containers/kata-containers/blob/main/tools/packaging/kata-deploy/helm-chart/kata-deploy/templates/runtimeclasses.yaml
