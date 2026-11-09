# Deploying the microVM tiers

This page is part of [The microVM tier: E2B and CubeSandbox as hangar providers](../microvm.md).

Both platforms need `/dev/kvm` and host-level setup, and neither runs as an unprivileged Pod (spec 0013 §5.3).

## Option A: a nested-virtualization pool on GKE (start here)

`deploy/terraform/gke/microvm.tf` adds the pool, off by default:

```hcl
enable_microvm_pool  = true
microvm_machine_type = "c3-standard-8"   # c3, c4, c4d, n2, n4, n4d: series with nested virtualization
microvm_node_count   = 1                 # fixed: templates and snapshots are cached per node
microvm_hugepages_2m = 2048              # E2B: 4 GiB of 2 MiB hugepages before the kubelet starts; Cube: 0
microvm_local_ssds   = 0                 # Cube: raw local NVMe (a -lssd machine type), formatted XFS for /data/cubelet
```

The pool runs Ubuntu with nested virtualization (`advanced_machine_features.enable_nested_virtualization`), is labelled `hangar.dev/runtime=microvm` and `nested-virtualization=enabled`, and is tainted `hangar.dev/runtime=microvm:NoSchedule`. It was checked with `terraform fmt` and `terraform validate` (Terraform 1.13.4, google provider 8.7.0). Nothing was applied.

The constraints, from GKE's docs as spec 0013 §6.1 records them [G1][G2] (re-read on 2026-10-13):

- **Machines.** C3, C4, C4D, N2, N4 or N4D. Google's own ComputeClass example uses C3, C4 and C4D. E2, N2D, Arm and memory-optimized machines lack nested virtualization. GKE can't run `-metal` machine types [G4].
- **Immutable.** Nested virtualization can't be turned on for an existing pool, and the setting can't be changed later. An org policy that disables nested virtualization blocks it.
- **Privileged Pods.** Pods that use the nested VMs need `securityContext.privileged: true`. Both stacks also want `hostNetwork`. That is why the pool is tainted and dedicated: nothing else, including hangar's own sandboxes, may share these nodes.
- **ComputeClass instead of a pool.** A ComputeClass with `enableNestedVirtualization: true` needs GKE **1.37.0-gke.4713000** or later. It works with auto-created pools and Autopilot. On Autopilot, privileged Pods need a `WorkloadAllowlist`. Google's example:
  ```yaml
  apiVersion: cloud.google.com/v1
  kind: ComputeClass
  metadata: {name: microvm}
  spec:
    priorities:
    - {machineFamily: c3, spot: true, enableNestedVirtualization: true}
    - {machineFamily: c4, spot: false, enableNestedVirtualization: true}
    - {machineFamily: c4d, spot: true, enableNestedVirtualization: true}
    nodePoolConfig: {imageType: cos_containerd}   # use ubuntu_containerd for E2B (not COS)
    nodePoolAutoCreation: {enabled: true}
    whenUnsatisfiable: DoNotScaleUp
  ```
- **The cost.** Google states a CPU penalty of 10% or more for nested virtualization, and more for I/O-bound work [G1].
- **No PVM.** CubeSandbox's PVM kernel, its way to run without VT-x, needs a custom host kernel, which GKE doesn't allow. On GKE it is real nested KVM or nothing.

**CubeSandbox on the pool.** Cube's Helm chart installs four DaemonSets (the node agent "Big Pod", an installer, bootstrap, and PVM; leave PVM off), all `hostNetwork` [C12]. To fit this pool:

- add `nodeSelector: {hangar.dev/runtime: microvm}` and a toleration for the taint;
- give `/data/cubelet` an XFS filesystem with reflink, either formatted from local NVMe SSDs by the installer or, for a trial, as an XFS loop image on the boot disk;
- run CubeAPI, CubeMaster and CubeProxy as ordinary Deployments in the same namespace.

hangar then uses:

```bash
HANGAR_ENABLED_PROVIDERS='["kubernetes","e2b"]'
HANGAR_E2B_FLAVOR=cube
HANGAR_E2B_API_URL=http://cubeapi.cube-system.svc:3000
HANGAR_E2B_SANDBOX_URL='http://{port}-{sandbox_id}.cubeproxy.cube-system.svc.cluster.local'  # or your CubeProxy domain
HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD=true   # only with the NetworkPolicy below
```

Pair it with a NetworkPolicy on the Cube namespace that admits ingress only from the hangar namespace.

**E2B self-hosted on the pool.** E2B's Embed is a one-node StatefulSet with `hostNetwork`, privileged init containers, NBD and 2 MiB hugepages [E4]. That is why the pool defaults to Ubuntu and 2048 hugepages. hangar then uses `HANGAR_E2B_FLAVOR=selfhosted`, `HANGAR_E2B_DOMAIN=<your E2B domain>`, `HANGAR_E2B_API_URL` if the API isn't at `api.<domain>`, and an E2B API key from that deployment.

## Option B: bare-metal C3 outside GKE

A managed instance group of `c3-standard-192-metal` runs CubeSandbox or E2B directly, and hangar reaches it over the VPC.

- **For:** no nested-virtualization penalty, so the published numbers apply, and very high density. Cube measured 1,000 idle 2 GiB VMs in about 25 GiB [C8].
- **Against:** the smallest metal machine has 192 vCPUs, about $9.68/h (≈ $7k/month; from a secondary source [G5]). It is billed per vCPU and memory like a VM, so it only pays above roughly 100 concurrent sandboxes. It needs a dedicated host and Hyperdisk, and it stops (`TERMINATE`) for maintenance, so plan for snapshots to be lost on maintenance. It is a second fleet to run, outside GKE's scheduling.

The provider settings are the same as on GKE, with addresses on the VPC. Restrict the firewall to the control plane's subnet. This is not in Terraform yet.

## Option C: GKE's managed `microvm` RuntimeClass

GKE's Agent Sandbox add-on allows a `microvm` RuntimeClass (managed Kata on Cloud Hypervisor), but node pools couldn't be created with it as of gcloud 588 [H2]. Its restore speed and snapshot API are unknown. Revisit it when it ships; it would plug in as the `kubernetes` provider with `runtime_class`, not as `e2b`.
