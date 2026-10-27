# How is the GKE cluster hardened?

This page is for whoever reviews or runs the GKE install from [Running hangar on GKE](../gke.md). It lists the
cluster settings `deploy/terraform/gke` makes and maps them to the CIS GKE Benchmark. It also says what each one does
to a cluster that already exists. It is part of the [security model](README.md).

## The settings

The variables live in `deploy/terraform/gke/hardening.tf`. A new cluster gets the secure value of each.

| Variable | Default | What it does |
| --- | --- | --- |
| `deletion_protection` | `true` | Terraform refuses to destroy the cluster. Set it to `false` and apply before a deliberate `terraform destroy`. |
| `inter_node_encryption` | `true` | WireGuard between nodes on Dataplane V2. Git grants to `hangar-gateway:8788` no longer cross nodes in plain HTTP (review finding L-10). |
| `secrets_encryption` | `true` | Kubernetes Secrets in etcd are wrapped with a Cloud KMS key that rotates every 90 days. |
| `secrets_kms_key` | empty | Your own key instead of the `hangar-gke/secrets` key Terraform creates. It must be in the cluster's region. |
| `shielded_secure_boot` | `false` | Secure Boot on every pool. The example tfvars turns it on for new installs. |
| `security_posture` | `BASIC` | GKE audits workload configuration and shows the findings in the console. |
| `image_vulnerability_scanning` | `true` | Artifact Analysis scans every image pushed to the registry for OS and package CVEs. |
| `private_nodes` | `false` | Nodes get internal addresses only, and Cloud NAT carries their egress. |
| `master_authorized_networks` | empty | Only these ranges reach the control plane's public endpoint. |

Integrity monitoring and the GKE metadata server are on for every pool whatever the variables say. A test,
`tests/test_up_gke.py`, holds the defaults and checks each pool.

## The CIS control mapping

Control numbers follow the CIS GKE Benchmark v1.9.0. Check them against the version your reviewer uses.

| Control | What it asks | Status | How, or why not |
| --- | --- | --- | --- |
| 5.1.1 | Image vulnerability scanning | Set | Artifact Analysis on the registry (`image_vulnerability_scanning`). |
| 5.2.1 | Nodes not on the Compute Engine default service account | Partly | The heavy pool has its own account. The other pools still use the default; changing that replaces each pool. |
| 5.2.2 | Workload Identity | Set | hangar's pod uses its own Google service account. |
| 5.3.1 | Secrets encrypted with Cloud KMS | Set | `secrets_encryption`. |
| 5.4.1 | Legacy metadata APIs off | Set | GKE's default, and `GKE_METADATA` hides them from pods. |
| 5.4.2 | GKE metadata server | Set | `GKE_METADATA` on every pool. |
| 5.5.1 | Container-Optimized OS | Partly | The system and gVisor pools run COS. The Kata, heavy and microVM pools need Ubuntu, because they install onto the node. |
| 5.5.2 | Node auto-repair | Set | GKE's default for release-channel pools. |
| 5.5.3 | Node auto-upgrade | Set | GKE's default for release-channel pools. |
| 5.5.4 | Release channels | Set | `release_channel`, RAPID by default for Agent Sandbox. |
| 5.5.5 | Shielded nodes | Set | `enable_shielded_nodes`. |
| 5.5.6 | Integrity monitoring | Set | On for every pool. |
| 5.5.7 | Secure Boot | Opt-in | `shielded_secure_boot`: on for new installs, off for existing ones until a maintenance window. |
| 5.6.1 | Intranode visibility | Not set | GKE refuses it with inter-node encryption, which matters more here. |
| 5.6.2 | VPC-native cluster | Set | Dataplane V2 requires it. |
| 5.6.3 | Control plane authorized networks | Opt-in | `master_authorized_networks`. |
| 5.6.4 | Private endpoint | Not set | `up.sh`, the deploy workflow and the power jobs reach the public endpoint. |
| 5.6.5 | Private nodes | Opt-in | `private_nodes`. |
| 5.6.7 | Google-managed certificates | Set | The Ingress uses a ManagedCertificate. |
| 5.7.1 | Logging and monitoring | Set | GKE's default. |
| 5.8.1 | No client certificate authentication | Set | GKE's default since 1.12. |
| 5.8.3 | Legacy ABAC off | Set | GKE's default. |
| 5.9.2 | CMEK boot disks | Not set | Google-managed keys encrypt disks at rest. Customer keys would replace every pool. |
| 5.10.1 | Kubernetes web UI off | Set | GKE removed it. |
| 5.10.3 | GKE Sandbox for untrusted workloads | Set | Every sandbox runs under gVisor, or Kata on the optional pools. |
| 5.10.4 | Binary Authorization | Not set | The decision is below. |
| 5.10.5 | Security posture | Set | `security_posture`. |

Google removed GKE's own workload vulnerability scanning: the standard tier on 2025-07-31, the advanced tier on
2026-06-16. Registry scanning covers hangar's images instead.

## Binary Authorization

hangar doesn't enforce Binary Authorization in 1.0. The control plane builds environment images at run time, one per
repository and commit, with Cloud Build. Enforcing would need an attestor in that build path and an attestation on
every image. It would also need exemptions for GKE's system images, Agent Sandbox and kata-deploy.

What hangar does instead: the control-plane image pins every base image by digest and installs from the hashed lock
file. Images live in hangar's own registry, which only hangar and CI push to. Registry scanning flags CVEs.

A registry allowlist policy is the next step, with no attestations. It lets pods run only images from hangar's
registry and Google's.

## On an existing cluster

Every change applies in place except Secure Boot. Some in-place changes still restart things.

| Change | Terraform | What it disrupts |
| --- | --- | --- |
| Deletion protection | In place | Nothing: it is Terraform state only. |
| Security posture, image scanning | In place | Nothing. |
| Secrets encryption | In place | The zonal control plane restarts; the Kubernetes API is down while GKE re-encrypts every Secret. |
| Inter-node encryption | In place | GKE restarts every node, which can take hours. Suspend sessions first. |
| Secure Boot | Replaces every pool | Each pool is deleted and created again. Suspend sessions; hangar is down until the system pool is back. |
| Private nodes | In place | A rolling update of every pool's nodes. |
| Authorized networks | In place | Callers outside the ranges lose `kubectl`. GitHub-hosted runners are outside Google's ranges. |

Run `terraform init -upgrade` first: inter-node encryption needs Google provider 6.36 or later. Then read the plan.
`hangar up --gke` refuses a plan that replaces anything unless you pass `--allow-destroy`.
