# Running hangar on GKE

The architecture is one control-plane pod (in namespace `hangar`) plus sandbox pods (in namespace `hangar-sandboxes`). Sandbox pods are managed through Agent Sandbox CRDs: a SandboxTemplate and SandboxWarmPool per environment, and a SandboxClaim per sandbox. They run under gVisor by default, or Kata microVMs on an optional nested-virtualization pool.

`uv run hangar up --gke` runs sections 1 and 2 for you: it shows the terraform plan (`--dry-run` stops there, having changed nothing), applies exactly that plan once you approve it, runs `deploy/gke/up.sh`, and logs the CLI in. It refuses a plan that destroys or replaces resources unless you pass `--allow-destroy`.

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

The quick way is one script. It reads the Terraform outputs, builds the control-plane image with Cloud Build, fills in the `deploy/k8s` placeholders and deploys:

```bash
bash deploy/gke/up.sh            # KATA=1 also installs Kata on the microVM pool (step 4)
```

Without `DOMAIN=...` it serves on `https://<static-ip>.sslip.io`, a public wildcard DNS name that resolves to the static IP, so the managed certificate provisions with no DNS setup. The admin token is saved to `~/.hangar/gke-admin-token`. On a first install `up.sh` also sets `HANGAR_SESSION_STORE` to the bucket terraform made (`session_bucket`), so checkpoints outlive the control plane's disk. On a hangar that's already running with checkpoints on its disk, it leaves the setting alone, since switching would strand the local checkpoints of suspended sessions. The manual steps below do the same thing by hand.


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

## 2a. Log in, connect and configure

```bash
hangar login https://<ip>.sslip.io --token-file ~/.hangar/gke-admin-token
hangar doctor --fix              # each problem, with the fixes it can apply on its own
hangar setup github              # token, webhook secret, repo webhook, settings, test delivery (jobs.md §4)
hangar connect claude-code       # or codex, cursor, vscode, gemini, claude-desktop (clients.md)
```

**Where settings live.** `hangar setup` and `hangar config set` store settings in hangar's database, on the control plane's volume. They survive `up.sh` and redeploys, and you change them without `kubectl`. The `hangar` and `hangar-site` config maps become the pod's environment, which wins over the database for any key both set. Keep the deployment's fixed values in the config maps (provider, registry, runtime class), and use `hangar config` for the rest. `hangar config list` shows where each value comes from. A value that's read at startup (provider, gateway, OAuth, `HANGAR_SESSION_STORE`, `HANGAR_JOB_CONCURRENCY`) applies after `kubectl -n hangar rollout restart deploy/hangar`; `hangar config set` says so.

## 3. Environment images

Every image and pack tag is content-addressed: an environment's tag is a digest of its rendered build, and a pack's tag is its harness's locked version plus a digest of its recipe. So which images a commit needs is known before it runs, and they are built before anyone waits on them:

- **CI** (`.github/workflows/images.yml`) builds and pushes whatever the registry lacks on every push to `main`. See [Images and packs, built in CI](#images-and-packs-built-in-ci).
- **`deploy/gke/up.sh`** runs the same build before the new control plane rolls out (`PREBUILD=0` skips it). When CI got there first, this is one registry lookup per tag.
- **The cluster** looks a tag up in the registry (an HTTP request, not a `gcloud` process) and builds with Cloud Build only what neither knew about: environments created through the API, or tag-changing settings (`HANGAR_REGISTRY`, `HANGAR_SANDBOX_USER`, `HANGAR_WORKSPACE_DIR`, `HANGAR_EXTRA_CA_BUNDLE`, `HANGAR_IMAGE_PREFIX`, `HANGAR_K8S_IMAGE_VOLUMES`) set only in hangar's database or the `hangar-site` config map. Set those in the workflow's `env` as well, or CI builds tags the cluster doesn't use.

```bash
uv run hangar images plan --registry "$(terraform -chdir=deploy/terraform/gke output -raw registry)"   # table; --json
uv run hangar images build --registry ...   # build and push what's missing (Docker here, else Cloud Build)
```

`plan` lists every environment image and harness pack the checkout needs on a provider (`--provider kubernetes`, the default, or `docker`) and whether each exists. It exits 3 when something is missing and 1 when the registry couldn't say. Both commands read the checkout's `envs/` and harness lock, so they need no running hangar and work while the cluster is powered down.

To build one environment by hand, the control plane can use Cloud Build (gcloud ships in the image). Building from your laptop is usually faster, because it reuses your local cache:

```bash
export HANGAR_URL=https://hangar.example.com HANGAR_TOKEN=hgr_...
uv run hangar env publish claude-python --provider kubernetes
```

`publish` asks the server for the exact build context, builds and pushes it with your local Docker, then registers the image. The server then syncs that environment's SandboxTemplate and SandboxWarmPool.

### Harness packs

Environment images carry no agents. Claude Code, Codex, pi and Antigravity ship as read-only OCI "harness packs" instead. Each pack is mounted at `/opt/hangar/harness/<name>` through a Kubernetes image volume. Image volumes are stable in Kubernetes 1.36 and work under gVisor. Nodes pull a pack once and share it across every environment.

**Versions are pinned.** `src/hangar/harnesses.lock.yaml` names the exact version of every harness (and, for Claude Code and Antigravity, which ship as binaries, the vendor's checksum of each one). A pack build checks what it downloaded against the lock, runs the CLI's `--version`, and fails if that isn't the locked version. It keeps the output in the pack (`/opt/hangar/harness/<name>/.hangar-version`, the pack's last layer), so hangar can read it from the registry. `list_environments` shows each harness's locked and installed version (`harness_versions`), `hangar pack list` shows both, and every turn records the version it ran (`harness_version`, and `hangar.harness.version` on its span). To move a harness forward, change the lock in a reviewed PR; CI builds the new pack.

**Packs are mounted by digest.** Sandboxes mount `repo@sha256:...`, the digest the registry reported when hangar found or built the pack, never a tag a later push could move. Nodes that already pulled one version can't keep running it once hangar mounts another, so one fleet runs one version of each harness.

CI and `up.sh` build packs with everything else. To publish them from a machine with Docker instead:

```bash
HANGAR_URL=https://hangar.example.com HANGAR_TOKEN=hgr_... \
  uv run hangar pack publish claude-code codex pi antigravity
```

`publish` doesn't overwrite a pack whose tag already exists: it registers the existing one. `--force` rebuilds and pushes over it; sandboxes then mount the new digest, and running ones keep the one they started with.

Set `HANGAR_K8S_IMAGE_VOLUMES=false` to turn image volumes off. Hangar then bakes the selected harness into a per-harness image. Only the harness the sandbox was created for is baked.

See [specs/0006-environments-and-harness-packs.md](specs/0006-environments-and-harness-packs.md) for the design.

### Images and packs, built in CI

`.github/workflows/images.yml` runs on every push to `main` and on demand (`workflow_dispatch`). It signs in to Google Cloud with Workload Identity Federation (no service-account key), runs `hangar images build --provider kubernetes`, and pushes to Artifact Registry. Until the three secrets below exist, it skips with a notice and stays green. It never talks to the cluster, so it also works while the cluster is powered down.

| Secret | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/<project number>/locations/global/workloadIdentityPools/github/providers/hangar` |
| `GCP_SERVICE_ACCOUNT` | `hangar-ci@<project>.iam.gserviceaccount.com`, with `roles/artifactregistry.writer` on the `hangar` repository |
| `HANGAR_REGISTRY` | `terraform -chdir=deploy/terraform/gke output -raw registry`, e.g. `us-central1-docker.pkg.dev/<project>/hangar` |

One-time setup, as a project owner (replace `OWNER/REPO` with the repository, e.g. `Sean-Koval/hangar`):

```bash
PROJECT=<project>; REGION=<region>; REPO=OWNER/REPO
NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud iam workload-identity-pools create github --project "$PROJECT" --location global
gcloud iam workload-identity-pools providers create-oidc hangar --project "$PROJECT" --location global \
  --workload-identity-pool github --issuer-uri https://token.actions.githubusercontent.com \
  --attribute-mapping 'google.subject=assertion.sub,attribute.repository=assertion.repository' \
  --attribute-condition "assertion.repository == '$REPO'"
gcloud iam service-accounts create hangar-ci --project "$PROJECT"
gcloud artifacts repositories add-iam-policy-binding hangar --project "$PROJECT" --location "$REGION" \
  --member "serviceAccount:hangar-ci@$PROJECT.iam.gserviceaccount.com" --role roles/artifactregistry.writer
gcloud iam service-accounts add-iam-policy-binding "hangar-ci@$PROJECT.iam.gserviceaccount.com" --project "$PROJECT" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$NUM/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"
gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --body "projects/$NUM/locations/global/workloadIdentityPools/github/providers/hangar"
gh secret set GCP_SERVICE_ACCOUNT --body "hangar-ci@$PROJECT.iam.gserviceaccount.com"
gh secret set HANGAR_REGISTRY --body "$(terraform -chdir=deploy/terraform/gke output -raw registry)"
```

Each run leaves `images.json` (what it found and built, with digests and installed versions) as an artifact and a table in the run summary.

## 4. Kata microVMs (optional)

> **With the GKE Agent Sandbox add-on, this doesn't work yet.** The add-on installs admission policies (`sandbox-core-policy`, `sandbox-hardening-policy`) that allow only the `gvisor` and `microvm` RuntimeClasses, on nodes labelled `sandbox.gke.io/runtime`. `microvm` is GKE's managed Kata (Cloud Hypervisor), but as of gcloud 588 node pools can only be created with `--sandbox type=gvisor`. Until GKE offers microVM node pools, use gVisor on GKE and Kata through the Docker provider on a KVM host ([local-microvms.md](local-microvms.md)). The steps below apply to clusters that run the open-source Agent Sandbox controller instead of the add-on.

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

## 6. Turning it off at night (optional)

`deploy/gke/power.sh` scales every node pool to zero and back. Nothing is deleted: the control plane's disk, images, session checkpoints and the IP stay, so the same hangar comes back at the same URL.

```bash
bash deploy/gke/power.sh down     # drain, suspend sessions (each checkpoints), stop hangar, 0 nodes
bash deploy/gke/power.sh up       # node pools back to their autoscaling ranges, start hangar (~3-5 min)
bash deploy/gke/power.sh status
```

`down` first asks hangar to drain (`POST /api/admin/drain`): queued jobs wait instead of starting, and `GET /api/admin/drain` lists what is still in flight. It suspends idle sessions and waits up to 10 minutes for running agent turns to finish. Turns still running then get a fresh checkpoint, and it waits up to 3 more minutes for sandbox creates, destroys and suspends already under way, so stopping hangar cuts nothing off half way. After `up`, what was still running resumes on its own: jobs go back to the queue a few at a time and continue from their last checkpoint, sandboxes a stop caught half created or half destroyed are finished by the reconciler, and GitHub deliveries that failed while the cluster was down are redelivered. Each pool's autoscaling range is saved as a label on the cluster, and `up` restores it.

To run it on a schedule, set these in `terraform.tfvars` and run `terraform apply`:

```hcl
enable_power_schedule = true
power_up_cron         = "0 10 * * *"   # 10 AM
power_down_cron       = "0 1 * * *"    # 1 AM
power_time_zone       = "America/New_York"
```

This creates two Cloud Run jobs (`hangar-power-up`, `hangar-power-down`) that run the same script, Cloud Scheduler entries that start them, and a `hangar-power` service account with `roles/container.admin`. To run one by hand: `gcloud run jobs execute hangar-power-up --region <region> --wait`.

While it's down, you pay for disks, images, the bucket and the load balancer, not nodes. GKE's free tier covers the management fee of one zonal cluster per billing account. The URL returns 502 until `up` finishes.

Two things to know:
- `terraform apply` while it's down turns autoscaling back on, so the system pool starts a node. Run `power.sh up` first, or `power.sh down` again after.
- `deploy/gke/up.sh` while it's down sets hangar back to one replica, but it won't start until `power.sh up` brings the nodes back.

## 7. Backups and restore

Everything hangar knows is one SQLite file on the `hangar-data` volume: sessions and their checkpoint lists, jobs, schedules, tokens, the vault and the settings `hangar config` stores. Checkpoints themselves are in the bucket. Losing the disk loses the database, so back it up.

**Backups.** With `HANGAR_SESSION_STORE` on a bucket (what `up.sh` sets), hangar backs up its database every 24 hours into `gs://<bucket>/_backups/` and keeps the newest 14. Each backup is taken with SQLite's online backup API in one step (one read transaction: hangar keeps working, and the copy never holds half a write), gzipped, and encrypted with the vault key.

```bash
hangar admin backup        # one now
hangar admin backups       # what's there, newest first
hangar doctor              # "database backups": fails when the newest is more than two intervals old
```

`HANGAR_BACKUP_HOURS` changes the interval (`0` turns it off; unset means 24 with a bucket, off without one) and `HANGAR_BACKUP_KEEP` the count. Both apply without a restart, from the next scheduled run (within an hour when backups were off). Without a bucket, backups go to `<data dir>/sessions/_backups`, which is the same disk: use PD snapshots instead (below).

**Keep the vault key somewhere else.** A backup is useless without the `HANGAR_SECRET_KEY` it was encrypted with, and so is everything in the vault. `up.sh` keeps it in the `hangar` Secret, which dies with the cluster. Save a copy in Secret Manager or your password manager:

```bash
kubectl -n hangar get secret hangar -o jsonpath='{.data.HANGAR_SECRET_KEY}' | base64 -d
```

**Restore.** A restore is staged, then applied by the next start, before anything opens the database. The database it replaces is kept beside it as `hangar.db.before-restore-<time>`.

```bash
hangar admin restore latest          # or a name from `hangar admin backups`
kubectl -n hangar rollout restart deploy/hangar
```

If hangar can't start at all, stage the restore from a one-off pod with the same image, settings and volume:

```bash
kubectl -n hangar scale deploy/hangar --replicas=0
IMAGE=$(kubectl -n hangar get deploy/hangar -o jsonpath='{.spec.template.spec.containers[0].image}')
kubectl -n hangar run hangar-restore --rm -i --restart=Never --image="$IMAGE" --overrides='{"spec": {
  "serviceAccountName": "hangar", "enableServiceLinks": false, "securityContext": {"fsGroup": 1000},
  "containers": [{"name": "hangar-restore", "image": "'"$IMAGE"'",
    "command": ["hangar", "admin", "restore", "--local", "latest"],
    "envFrom": [{"configMapRef": {"name": "hangar"}}, {"configMapRef": {"name": "hangar-site", "optional": true}},
                {"secretRef": {"name": "hangar"}}],
    "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
  "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "hangar-data"}}]}}'
kubectl -n hangar scale deploy/hangar --replicas=1
```

After a restore:
- Tokens and credentials are as they were at the backup. A token revoked since then works again: revoke it again, or rotate them all.
- Jobs and turns that were running at the backup restart or are marked interrupted, as after any restart.
- A session restores from the checkpoints the backup lists. Hangar keeps 5 per session (`HANGAR_SESSION_KEEP_CHECKPOINTS`), so a session that checkpointed more than 5 times after the backup has none of those left in the bucket and can't resume. Restore from a recent backup.

**PD snapshots** are the other option, or a second layer: they copy the whole volume (database, local checkpoints, logs) and don't need the vault key to restore the disk. A snapshot of a running hangar is crash-consistent: SQLite recovers it like a power loss, and may drop the last second of commits. Schedule them on the volume's disk:

```bash
PV=$(kubectl -n hangar get pvc hangar-data -o jsonpath='{.spec.volumeName}')
kubectl get pv "$PV" -o jsonpath='{.spec.csi.volumeHandle}'   # projects/<p>/zones/<zone>/disks/<disk>
gcloud compute resource-policies create snapshot-schedule hangar-data-daily --region <region> \
  --daily-schedule --start-time 04:00 --max-retention-days 14 --on-source-disk-delete keep-auto-snapshots
gcloud compute disks add-resource-policies <disk> --zone <zone> --resource-policies hangar-data-daily
```

To restore one, create a disk from the snapshot, then a PV and PVC for it (or swap it in for `hangar-data`), with hangar scaled to 0.

## Shutdown

`kubectl rollout restart`, `power.sh down` and node upgrades stop hangar with SIGTERM. Requests that wait (MCP `agent_status` with `wait_seconds`, `wait_for_turn`, a blocking A2A send) answer at once with the state as it is, and their clients ask again. Event streams and terminals are cut after `HANGAR_SHUTDOWN_GRACE_SECONDS` (15). Then hangar stops its loops and closes its connections. The pod's `terminationGracePeriodSeconds` is 45 to cover that.

Sandboxes, agent turns and sessions don't stop with the control plane. A new pod re-attaches to them. Shutdown takes no checkpoints. A checkpoint, destroy or backup cut off by it is redone after the restart (the idle check and the reaper run again), so the 300 s pre-destroy budget doesn't need to fit in the grace period.

## 8. E2B SDK clients (optional)

Hangar answers E2B's API, so the e2b SDKs and the tools built on them can use GKE sandboxes ([e2b.md](e2b.md)). They need two things from the cluster: hangar-envd reachable in each sandbox (`envd-network.yaml`, already applied) and a way to reach sandbox URLs. There are two ways.

**No new DNS (the sslip.io install, or any single host).** Clients route every sandbox call by header through the URL you already have, its certificate and its Ingress:

```bash
hangar token create my-agents --scope sandboxes      # E2B_API_KEY
export E2B_API_KEY=hgr_... E2B_API_URL=https://<ip>.sslip.io/e2b E2B_SANDBOX_URL=https://<ip>.sslip.io/e2b/sandbox
```

Everything the SDKs do works this way, including code-interpreter's port 49999. What doesn't is a browser opening `sandbox.get_host(port)`, which needs per-sandbox host names.

**Your own domain, with `*.<domain>`.** Then `E2B_DOMAIN=<domain>` is all a client sets, and apps in sandboxes get URLs like `https://3000-sb-1a2b3c4d.e2b.example.com`. That needs a wildcard certificate, which takes a few extra resources:

- **Google-managed certificates on GKE Ingress don't support wildcards**, and GKE Ingress doesn't take Certificate Manager certificates ("GKE Ingress does not support certificates managed by Certificate Manager. To use certificates managed by Certificate Manager, use the Gateway API").
- **Certificate Manager with DNS authorization does.** One DNS authorization covers a domain and its wildcard, one level deep: `*.e2b.example.com` covers `49983-sb-1a2b3c4d.e2b.example.com`, which is all a sandbox host needs. Load-balancer authorization doesn't do wildcards.
- **sslip.io can't get one**, since nobody can add the `_acme-challenge` record under it; use the header mode above there.

With `E2B_DOMAIN=e2b.example.com`:

```bash
gcloud container clusters update hangar --location <zone> --gateway-api=standard   # once; up to 45 min
gcloud compute addresses create hangar-e2b-ip --global
gcloud certificate-manager dns-authorizations create hangar-e2b --domain=e2b.example.com
gcloud certificate-manager dns-authorizations describe hangar-e2b   # add its dnsResourceRecord (a CNAME) to your DNS
gcloud certificate-manager certificates create hangar-e2b \
  --domains="e2b.example.com,*.e2b.example.com" --dns-authorizations=hangar-e2b
gcloud certificate-manager maps create hangar-e2b
gcloud certificate-manager maps entries create hangar-e2b-wild --map=hangar-e2b \
  --hostname="*.e2b.example.com" --certificates=hangar-e2b
sed 's/E2B_DOMAIN/e2b.example.com/' deploy/k8s/e2b.yaml | kubectl apply -f -
hangar config set e2b_api_domain e2b.example.com     # then: kubectl -n hangar rollout restart deploy/hangar
```

Point `*.e2b.example.com` (an A record) at `gcloud compute addresses describe hangar-e2b-ip --global`. `deploy/k8s/e2b.yaml` holds the Gateway and route for `*.<domain>`, a one-hour backend timeout and the `/healthz` health check for it, and a NetworkPolicy that lets hangar's pods reach apps' ports in sandboxes; without that policy only envd is reachable. The dashboard, MCP and the REST API stay on the Ingress.

Long streams (a command's output, a terminal) end after the backend timeout (one hour, on both the Ingress and the Gateway); the process keeps running, and `commands.connect(pid)` picks it up again.

## Notes

- **Network.** The default SandboxTemplate network policy allows egress to the public internet only, and blocks RFC1918 ranges and the metadata server. Agents can reach GitHub and package registries but not your VPC. `deploy/k8s/sandbox-network.yaml` applies the same baseline as hangar's own NetworkPolicies (DNS, public internet minus private and link-local ranges, no ingress), so it still holds if the managed policy is turned off or you run the open-source controller.
- **Git gateway.** `deploy/k8s/gateway.yaml` adds the `hangar-gateway` Service (port 8788) and a NetworkPolicy that lets sandbox pods reach that port, and only that port, of the control plane. Set `HANGAR_GATEWAY_URL=http://hangar-gateway.hangar.svc:8788` to keep GitHub tokens out of sandboxes ([security.md](security.md#git-credential-gateway)).
- **Expiry.** Each claim gets `lifecycle.shutdownTime` (sandbox TTL plus 5 minutes) as a backstop to hangar's reaper.
- **Storage.** The control plane keeps state in SQLite on a 20 Gi PVC. It backs itself up to the bucket; PD snapshots are the alternative (§7).
