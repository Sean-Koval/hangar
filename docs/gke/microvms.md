# Kata, the heavy tier and Pod snapshots on GKE

This page is part of [Running hangar on GKE](../gke.md).

## 4. Kata microVMs (optional)

> **With the GKE Agent Sandbox add-on, this doesn't work yet.** The add-on installs admission policies (`sandbox-core-policy`, `sandbox-hardening-policy`) that allow only the `gvisor` and `microvm` RuntimeClasses, on nodes labelled `sandbox.gke.io/runtime`. `microvm` is GKE's managed Kata (Cloud Hypervisor), but as of gcloud 588 node pools can only be created with `--sandbox type=gvisor`. Until GKE offers microVM node pools, use gVisor on GKE and Kata through the Docker provider on a KVM host ([local-microvms.md](../local-microvms.md)). The steps below apply to clusters that run the open-source Agent Sandbox controller instead of the add-on.

The policies also require every sandbox container to drop all capabilities, block privilege escalation and run as non-root, with CPU and memory limits and at least 1 GiB of memory per vCPU. hangar's pod template does all of this, so `sudo` is unavailable inside Kubernetes sandboxes.

Set `enable_kata_pool = true` and re-apply. That adds an Intel N2 Ubuntu pool with nested virtualization, labeled and tainted `hangar.dev/runtime=kata`. Then:

1. Install Kata on that pool with the official chart, enabling only the QEMU shims and pinning it to the Kata pool:
   ```bash
   helm install kata-deploy oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy \
     --version 4.2.0 --namespace kata-system --create-namespace \
     --set shims.disableAll=true --set shims.qemu-runtime-rs.enabled=true \
     --set nfd.enabled=false \
     --set nodeSelector."hangar\.dev/runtime"=kata \
     --set 'tolerations[0].key=hangar.dev/runtime' --set 'tolerations[0].value=kata' \
     --set 'tolerations[0].effect=NoSchedule'
   kubectl get runtimeclass   # expect kata-qemu-runtime-rs
   ```
2. Use an environment with `runtime_class: kata-qemu-runtime-rs`, such as `envs/claude-python-kata.yaml`. gVisor and Kata environments then share the cluster. To make Kata the default for every environment, set `HANGAR_K8S_RUNTIME_CLASS: kata-qemu-runtime-rs` in `deploy/k8s/config.yaml` instead.

Kata 4.x defaults to its Rust runtime, `kata-qemu-runtime-rs`. The older Go runtime is still available as `kata-qemu` (enable `shims.qemu`), but it's deprecated. hangar pins any `kata*` runtime class to the Kata pool automatically.

Kata needs real nested virtualization, which is why the pool uses Intel N2 machines with `enable_nested_virtualization`. Running Kata inside kind on WSL boots the VM but can't reach the guest agent over vsock, so test Kata on a GCE or bare-metal Linux node.

hangar adds the matching nodeSelector and toleration to sandbox pods automatically.

## 4a. The heavy tier (optional)

Sandboxes with Docker inside, as Kata microVMs, and the `large` and `xlarge` profiles
([environments.md](../environments/heavy-tier.md)). Off until you turn it on:

1. **The pool.** `enable_heavy_pool = true` in `terraform.tfvars` (and `heavy_machine_type`, `heavy_max_nodes`,
   `heavy_spot`, `heavy_disk_gb` if the defaults don't fit: [team-scale.md](../team-scale/heavy-tier.md)), then apply.
   That adds `sandboxes-heavy`: Ubuntu, nested virtualization, `n2-standard-32` by default, scaling from zero,
   labelled and tainted `hangar.dev/runtime=heavy`. Its name puts it under the power schedule: it goes to zero at night,
   and holds and `off` keep it there ([6](power.md)). Its nodes run as a service account
   of their own, `hangar-heavy-nodes`, with only `roles/container.defaultNodeServiceAccount` and read access to the
   `hangar` image repository: a heavy sandbox is root in its VM, so its node shouldn't carry the project's default
   compute identity. (The other sandbox pools still use the default account; giving them the same is a follow-up.)
2. **Kata on it.** `KATA=1 bash deploy/gke/up.sh` installs kata-deploy on every pool labelled
   `katacontainers.io/kata-runtime=true` (the Kata pool and the heavy one), tolerating their taints. kata-deploy
   configures containerd with `privileged_without_host_devices = true` for its runtimes, which is what keeps a heavy
   sandbox's privileges inside its VM; check it on a node if you install Kata another way.
3. **hangar-site:** `HANGAR_TIERS: '{"heavy": ["kubernetes"], ...}'` (keep your other tiers in the same JSON), and
   optionally `HANGAR_HEAVY_REGISTRY_MIRRORS: '["https://mirror.gcr.io"]'`.
4. **Docker Hub without rate limits (optional):** `enable_dockerhub_mirror = true` (with `dockerhub_username` and a
   Secret Manager secret of its access token in `dockerhub_token_secret`, or anonymously), then
   `HANGAR_HEAVY_MIRROR: "true"` and `HANGAR_HEAVY_MIRROR_UPSTREAM` set to the output `dockerhub_mirror`. Heavy
   sandboxes pull Docker Hub images through hangar's read-only mirror, which reads that repository with hangar's own
   service account. Every heavy sandbox can pull whatever that Docker Hub account can, so use one with **no private
   repositories**.
5. **Check it:** `bash scripts/acceptance/gke.sh --only heavy` brings up a Compose stack with Postgres and runs a
   Testcontainers test in a heavy sandbox.

> **The GKE Agent Sandbox add-on refuses heavy sandboxes**, as it refuses Kata ([4](#4-kata-microvms-optional)): its
> admission policy requires `runtimeClassName: gvisor`, non-root containers without added capabilities, and no
> `privileged: true` ([Agent Sandbox requirements](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/how-install-agent-sandbox)).
> A heavy sandbox is a privileged container under Kata, so on a cluster with the add-on its claims never get a pod
> (`hangar sb ls --json` shows the stage and the controller's reason while it waits). Run the open-source Agent Sandbox controller
> instead of the add-on, or put the heavy tier on microVMs ([microvm.md](../microvm.md#the-heavy-tier-on-microvms)):
> `HANGAR_TIERS='{"heavy": ["e2b"]}'`. GKE's managed `microvm` RuntimeClass, which the add-on does allow, refuses
> privileged containers all the same.

Nested virtualization takes C3, C4, C4D, N2, N4 or N4D machines (not E2, N2D, Arm or memory-optimized ones), can't
be turned on for an existing pool, and is blocked by the `compute.disableNestedVirtualization` organization policy.
Google states a CPU penalty of 10% or more for nested VMs, more for I/O-bound work
([nested virtualization](https://docs.cloud.google.com/compute/docs/instances/nested-virtualization/overview);
[on GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nested-virtualization)).

## 5. Pause and resume with Pod snapshots (optional)

Without Pod snapshots, Kubernetes sandboxes can't be paused. With them, `pause_sandbox` snapshots the pod (memory, running processes and filesystem) and suspends it, which frees its node capacity. `resume_sandbox` brings it back exactly where it was.

GKE restores a sandbox only from its *own* snapshots, so this powers pause and resume, not forks. Forks on Kubernetes always copy `/workspace` as a tarball.

1. Set `enable_pod_snapshots = true` in `terraform.tfvars` and re-apply. This enables `--enable-pod-snapshots` and creates the snapshot bucket, custom role and IAM bindings.
2. Put `terraform output pod_snapshot_bucket` into `deploy/k8s/pod-snapshots.yaml`, then `kubectl apply -f deploy/k8s/pod-snapshots.yaml`. That creates the `hangar-sandbox` service account, the storage config, and a policy that selects sandboxes by the `hangar.dev/snapshottable` label.
3. Set `HANGAR_K8S_POD_SNAPSHOTS: "true"` in `config.yaml` and restart hangar. This requires `HANGAR_K8S_RUNTIME_CLASS=gvisor`; Kata pools can't be snapshotted.

How it works:

- **Pause:** creates a `PodSnapshotManualTrigger`, waits for it to complete, then sets the Sandbox's `operatingMode` to `Suspended`.
- **Resume:** sets `operatingMode` back to `Running`. hangar checks the new pod's `PodRestored` condition. If GKE booted a fresh pod instead of restoring, the resume fails loudly rather than leaving you an empty sandbox.
- **Retention:** the policy keeps 10 snapshots per sandbox, and the bucket deletes objects after 14 days. A sandbox paused longer than that can't be restored.

**Security.** With snapshots on, sandbox pods run as `hangar-sandbox`, which has read/write access to the snapshot bucket. That bucket holds every sandbox's memory, including injected credentials. This relies on Agent Sandbox's default network policy, which blocks the metadata server, so code in a sandbox can't mint a token for that service account. Don't switch templates to `networkPolicyManagement: Unmanaged`. To verify, run this from inside a sandbox; it should fail:

```bash
curl -s -m 5 -H 'Metadata-Flavor: Google' \
  http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token
```
