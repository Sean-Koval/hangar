# Running hangar on GKE

The architecture is one control-plane pod (in namespace `hangar`) plus sandbox pods (in namespace `hangar-sandboxes`). Sandbox pods are managed through Agent Sandbox CRDs: a SandboxTemplate and SandboxWarmPool per environment, and a SandboxClaim per sandbox. They run under gVisor by default, or Kata microVMs on an optional nested-virtualization pool.

## 1. Infrastructure

```bash
cd deploy/terraform/gke
cp terraform.tfvars.example terraform.tfvars   # set project_id, region, domain
terraform init && terraform apply
```

This creates:

- **A Standard cluster** on the RAPID channel, since Agent Sandbox needs GKE 1.35.2-gke.1210000 or later. It uses Workload Identity and Dataplane V2.
- **A system pool**, plus a gVisor sandbox pool (Spot by default) that autoscales from 0 to N nodes.
- **The Agent Sandbox add-on**, enabled through `gcloud`, and optionally Pod snapshots.
- **Supporting resources:** an Artifact Registry repo, a `hangar` Google service account bound to the pod's KSA, and a global static IP.

## 2. Control plane

```bash
$(terraform output -raw get_credentials)
REG=$(terraform output -raw registry)
docker buildx build --platform linux/amd64 -t $REG/control-plane:latest --push .

kubectl apply -f deploy/k8s/namespace.yaml
kubectl -n hangar create secret generic hangar \
  --from-literal=HANGAR_ADMIN_TOKEN=hgr_$(openssl rand -hex 24) \
  --from-literal=HANGAR_SECRET_KEY=$(python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
```

Next, edit the placeholders in `deploy/k8s`: `PROJECT_ID` and `REGION` in `rbac.yaml`, `config.yaml` and `deployment.yaml`, and the domain in `config.yaml` and `ingress.yaml`. Then deploy:

```bash
kubectl apply -k deploy/k8s
```

Point your domain's A record at `terraform output static_ip`. The managed certificate provisions in about 15 minutes.

## 3. Environment images

The control plane can build with Cloud Build (gcloud ships in the image). Building from your laptop is usually faster, because it reuses your local cache:

```bash
export HANGAR_URL=https://hangar.example.com HANGAR_TOKEN=hgr_...
uv run hangar env publish claude-python --provider kubernetes
```

`publish` asks the server for the exact build context, builds and pushes it with your local Docker, then registers the image. The server then syncs that environment's SandboxTemplate and SandboxWarmPool.

## 4. Kata microVMs (optional)

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
2. Set `HANGAR_K8S_RUNTIME_CLASS: kata-qemu-runtime-rs` in `deploy/k8s/config.yaml` and restart hangar.

Kata 4.x defaults to its Rust runtime, `kata-qemu-runtime-rs`. The older Go runtime is still available as `kata-qemu` (enable `shims.qemu`), but it's deprecated. Hangar pins any `kata*` runtime class to the Kata pool automatically.

Kata needs real nested virtualization, which is why the pool uses Intel N2 machines with `enable_nested_virtualization`. Running Kata inside kind on WSL boots the VM but can't reach the guest agent over vsock, so test Kata on a GCE or bare-metal Linux node.

Hangar adds the matching nodeSelector and toleration to sandbox pods automatically.

## 5. Pause and resume with Pod snapshots (optional)

Without Pod snapshots, Kubernetes sandboxes can't be paused. With them, `pause_sandbox` snapshots the pod (memory, running processes and filesystem) and suspends it, which frees its node capacity. `resume_sandbox` brings it back exactly where it was.

GKE restores a sandbox only from its *own* snapshots, so this powers pause and resume, not forks. Forks on Kubernetes always copy `/workspace` as a tarball.

1. Set `enable_pod_snapshots = true` in `terraform.tfvars` and re-apply. This enables `--enable-pod-snapshots` and creates the snapshot bucket, custom role and IAM bindings.
2. Put `terraform output pod_snapshot_bucket` into `deploy/k8s/pod-snapshots.yaml`, then `kubectl apply -f deploy/k8s/pod-snapshots.yaml`. That creates the `hangar-sandbox` service account, the storage config, and a policy that selects sandboxes by the `hangar.dev/snapshottable` label.
3. Set `HANGAR_K8S_POD_SNAPSHOTS: "true"` in `config.yaml` and restart hangar. This requires `HANGAR_K8S_RUNTIME_CLASS=gvisor`; Kata pools can't be snapshotted.

How it works:

- **Pause:** creates a `PodSnapshotManualTrigger`, waits for it to complete, then sets the Sandbox's `operatingMode` to `Suspended`.
- **Resume:** sets `operatingMode` back to `Running`. Hangar checks the new pod's `PodRestored` condition. If GKE booted a fresh pod instead of restoring, the resume fails loudly rather than leaving you an empty sandbox.
- **Retention:** the policy keeps 10 snapshots per sandbox, and the bucket deletes objects after 14 days. A sandbox paused longer than that can't be restored.

**Security.** With snapshots on, sandbox pods run as `hangar-sandbox`, which has read/write access to the snapshot bucket. That bucket holds every sandbox's memory, including injected credentials. This relies on Agent Sandbox's default network policy, which blocks the metadata server, so code in a sandbox can't mint a token for that service account. Don't switch templates to `networkPolicyManagement: Unmanaged`. To verify, run this from inside a sandbox; it should fail:

```bash
curl -s -m 5 -H 'Metadata-Flavor: Google' \
  http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token
```

## Notes

- **Network.** The default SandboxTemplate network policy allows egress to the public internet only, and blocks RFC1918 ranges and the metadata server. Agents can reach GitHub and package registries but not your VPC.
- **Expiry.** Each claim gets `lifecycle.shutdownTime` (sandbox TTL plus 5 minutes) as a backstop to hangar's reaper.
- **Storage.** The control plane keeps state in SQLite on a 20 Gi PVC. Back it up, or snapshot the disk.
