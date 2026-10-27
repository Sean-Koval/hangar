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
- **Alerting, opt-in** (`enable_alerting`): [SLO alerts and a dashboard](alerting.md), with [runbooks](runbooks/README.md) for each alert.

## 2. Control plane

The quick way is one script. It reads the Terraform outputs, builds the control-plane image with Cloud Build, fills in the `deploy/k8s` placeholders and deploys:

```bash
bash deploy/gke/up.sh            # KATA=1 also installs Kata on the microVM pool (step 4)
```

Without `DOMAIN=...` it serves on `https://<static-ip>.sslip.io`, a public wildcard DNS name, so the certificate needs no DNS setup. The admin token is saved to `~/.hangar/gke-admin-token`. On a first install `up.sh` also sets `HANGAR_SESSION_STORE` to the bucket terraform made (`session_bucket`), so checkpoints outlive the control plane's disk. On a running hangar with checkpoints on its disk it leaves the setting alone: switching would strand them. On every install it sets `HANGAR_OUTBOX_URL` to the run bucket (`run_bucket`, [below](#run-capture-bucket)), so finished runs reach flightdeck. The manual steps below do the same thing by hand.


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

To run jobs from the team's harness repo, add to `deploy/k8s/config.yaml`
`HANGAR_HARNESS_REPO: https://github.com/<org>/<team-harnesses>#main` (read with the GitHub App; install it on that
repo with Contents: read) and, to make it intake jobs' default, `HANGAR_DEFAULT_HARNESS: team-default@stable`. The
control plane keeps the checkout on its disk and refreshes it every 10 minutes
([harness-lock.md](harness-lock.md#the-team-harness-repo)).

### Run capture bucket

Terraform also creates `<project>-hangar-runs` (`enable_run_bucket`, default on; `deploy/terraform/gke/outbox.tf`) for
run capture ([docs/api/outbox.md](api/outbox.md)). `up.sh` sets `HANGAR_OUTBOX_URL` in the `hangar` config map to
`terraform output -raw run_bucket` on every install, a running one too: turning it on strands nothing. It keeps a value
the `hangar` config map already has, a value in `hangar-site` wins, and it restarts hangar when the URL changed but the
image didn't, because the outbox reads the URL at startup. By hand, uncomment the line in `deploy/k8s/config.yaml`.
While it's off, `hangar doctor` on GKE warns `run capture (outbox)`. The bucket has uniform access
and no public access; hangar's service account gets `roles/storage.objectCreator` on it only, and an IAM deny policy
(`hangar-runs-write-once`, matching the bucket's `hangar-write-once=runs` tag) takes `objects.delete` and
`objects.update` away from it, because its project-wide `roles/storage.admin` (Cloud Build's staging bucket) would
otherwise allow them: hangar can create runs, never delete or overwrite one. Applying the deny policy needs
`roles/iam.denyAdmin` on the project. Readers go in `run_bucket_readers` and get `roles/storage.objectViewer`.
flightdeck's nightly compactor is the one reader: its `deploy/compactor/setup.sh` creates the service account
`flightdeck-compactor@<project>.iam.gserviceaccount.com` and grants it that role on the bucket itself; listing it in
`run_bucket_readers` keeps the grant in terraform (apply after the account exists). Objects under `v1/tapes/` expire
after `run_tape_days` (30). Deleting a run for a deletion request takes an admin's own credentials
([outbox.md](api/outbox.md#retention-and-the-export-189)).

To check that runs land: `hangar doctor` reports `run capture (outbox)` as `on: N object(s) pending to GCS bucket
gs://<project>-hangar-runs`, `hangar outbox status` lists what's queued, and once a job has finished,
`gcloud storage ls "gs://<project>-hangar-runs/v1/runs/$(date -u +%F)/*/run.json"` lists its record set's last object
(dates are UTC). flightdeck compacts them overnight, in the pools-off window
([flightdeck data-plane.md](https://sean-koval.github.io/flightdeck/data-plane/#compactor)).

## 2a. Log in, connect and configure

```bash
hangar login https://<ip>.sslip.io --token-file ~/.hangar/gke-admin-token
hangar doctor --fix              # each problem, with the fixes it can apply on its own
hangar setup github              # token, webhook secret, repo webhook, settings, test delivery (jobs/README.md §4)
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

Set `HANGAR_K8S_IMAGE_VOLUMES=false` to turn image volumes off. hangar then bakes the selected harness into a per-harness image. Only the harness the sandbox was created for is baked.

See [specs/0006-environments-and-harness-packs.md](specs/0006-environments-and-harness-packs.md) for the design.

### Images and packs, built in CI

`.github/workflows/images.yml` runs on every push to `main` and on demand (`workflow_dispatch`). It signs in to Google Cloud with Workload Identity Federation (no service-account key), runs `hangar images build --provider kubernetes`, and pushes to Artifact Registry. Until the three secrets below exist, it skips with a notice and stays green; it also skips on refs other than `main` and `v*` tags, which Google Cloud doesn't trust. It never talks to the cluster, so it also works while the sandbox pools are powered down. `deploy.yml` calls it before every deploy ([upgrading.md](upgrading.md#deploying-with-deployyml)).

| Secret | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/<project number>/locations/global/workloadIdentityPools/github/providers/hangar` |
| `GCP_SERVICE_ACCOUNT` | `hangar-ci@<project>.iam.gserviceaccount.com`, with `roles/artifactregistry.writer` on the `hangar` repository |
| `HANGAR_REGISTRY` | `terraform -chdir=deploy/terraform/gke output -raw registry`, e.g. `us-central1-docker.pkg.dev/<project>/hangar` |

One-time setup, as a project owner: `bash deploy/gke/setup-cicd.sh` does this, deploy.yml's setup too, and checks it
([cicd.md](cicd.md)). By hand (replace `OWNER/REPO` with the repository, e.g. `Sean-Koval/hangar`), the same provider:
only this repository, by name and by id, and only push or manual runs of `main` or a `v*` tag. Use a pool of its own:
the accounts trust every provider in it. Then restrict who can create `v*` tags with a tag ruleset (the script does).

```bash
PROJECT=<project>; REGION=<region>; REPO=OWNER/REPO
NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
REPO_ID=$(gh api "repos/$REPO" --jq .id); OWNER_ID=$(gh api "repos/$REPO" --jq .owner.id)
gcloud services enable sts.googleapis.com iamcredentials.googleapis.com --project "$PROJECT"
gcloud iam workload-identity-pools create github --project "$PROJECT" --location global
gcloud iam workload-identity-pools providers create-oidc hangar --project "$PROJECT" --location global \
  --workload-identity-pool github --issuer-uri https://token.actions.githubusercontent.com \
  --attribute-mapping 'google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref' \
  --attribute-condition "assertion.repository == '$REPO' && assertion.repository_id == '$REPO_ID' && assertion.repository_owner_id == '$OWNER_ID' && (assertion.ref == 'refs/heads/main' || assertion.ref.startsWith('refs/tags/v')) && (assertion.event_name == 'push' || assertion.event_name == 'workflow_dispatch')"
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

Kata 4.x defaults to its Rust runtime, `kata-qemu-runtime-rs`. The older Go runtime is still available as `kata-qemu` (enable `shims.qemu`), but it's deprecated. hangar pins any `kata*` runtime class to the Kata pool automatically.

Kata needs real nested virtualization, which is why the pool uses Intel N2 machines with `enable_nested_virtualization`. Running Kata inside kind on WSL boots the VM but can't reach the guest agent over vsock, so test Kata on a GCE or bare-metal Linux node.

hangar adds the matching nodeSelector and toleration to sandbox pods automatically.

## 4a. The heavy tier (optional)

Sandboxes with Docker inside, as Kata microVMs, and the `large` and `xlarge` profiles
([environments.md](environments/heavy-tier.md)). Off until you turn it on:

1. **The pool.** `enable_heavy_pool = true` in `terraform.tfvars` (and `heavy_machine_type`, `heavy_max_nodes`,
   `heavy_spot`, `heavy_disk_gb` if the defaults don't fit: [team-scale.md](team-scale.md#heavy-tier)), then apply.
   That adds `sandboxes-heavy`: Ubuntu, nested virtualization, `n2-standard-32` by default, scaling from zero,
   labelled and tainted `hangar.dev/runtime=heavy`. Its name puts it under the power schedule: it goes to zero at night,
   and holds and `off` keep it there ([6](#6-sandbox-nodes-off-at-night-optional)). Its nodes run as a service account
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
> instead of the add-on, or put the heavy tier on microVMs ([microvm.md](microvm.md#the-heavy-tier-on-microvms)):
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

## 6. Sandbox nodes off at night (optional)

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

### The overnight lane

With the pools down 01:00–10:00, a ticket moved to "Ready for agent" at 23:00 waits for the morning. The overnight lane
keeps **one Spot e2-standard-16** up through the night for the work that should not wait: intake jobs of repos whose
policy says `overnight: true` (`hangar repo set acme/api --overnight`, [repo-policy.md](repo-policy.md)), intake jobs
labelled `lane: overnight`, and eval-class jobs ([team-scale.md](team-scale.md#the-eval-queue-class)). About $87 a month
([team-scale.md](team-scale.md#cost)); evening tickets become PRs waiting at the 10:00 triage.

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
`n2d-standard-16`, [team-scale.md](team-scale.md#packing)); the rest wait for the morning as in `down`, and creates for anything else still
answer at once with when the pools are back. Review-aware admission ([team-scale.md](team-scale.md#review-capacity))
still applies at night: a requester past their open-agent-PR cap waits as by day. A Spot preemption has no guaranteed
replacement: the lane's running turns are marked `infra_error/preempted` and wait for 10:00, when they resume from their
checkpoints like any turn the power-down cut off. A deploy in the window ([team-scale.md](team-scale.md#applying-it))
must allow for the one live node: suspend its sessions or leave sandbox node changes for the day.

### Infra states

| State | What runs | What it costs a week (*estimate*, [team-scale.md](team-scale.md#cost)) | Webhooks | Jobs and sessions | A merge to main ([cicd.md](cicd.md#infra-states)) | How to get back |
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
limit: [redelivering webhooks][redeliver]); [cicd.md](cicd.md#taking-a-week-off) has the week-off recipe, and what
is lost.

**Deploys and power.** `power.sh up` by hand ends by starting `deploy.yml` for main when main is ahead of what hangar
runs (it waits for your approval; [cicd.md](cicd.md#merges-to-main-deploy-while-the-infra-is-up)). The scheduled
power-up holds no GitHub credential and only reports it: set `power_deploy_repo` and `hangar power` shows the deploy
pending with its command.

**While the pools are off**, hangar keeps working on everything that doesn't need a sandbox:

- GitHub, Slack and Jira events (and GitHub Projects, schedules, A2A, MCP `submit_job`) are accepted and queued as usual. Queued jobs wait with the status `waiting: sandboxes power on at 10:00 EDT (in 8 h 12 min)`, and start a few at a time (`HANGAR_JOB_RESTART_STAGGER_SECONDS` apart) once the pools are up.
- A create (`create_sandbox`, REST, the E2B API, `new_session`, a message to a suspended session) answers at once with a plain message and when sandboxes are back, instead of waiting for a node that won't come: REST answers `503` with `Retry-After`. Nothing is left half made.
- Sessions and the dashboard are readable; suspended sessions resume when their next message arrives after power-up.

`down` tells hangar first (`POST /api/admin/power/state`), so new creates are refused from that moment, then drains (`POST /api/admin/drain`): queued jobs wait instead of starting, and running experiment trials are cancelled as `infra_error` / `power_down` and run again in the next eval window (20:00, not at power-up: [team-scale.md](team-scale.md#the-eval-queue-class)); queued eval jobs wait for that window too. It suspends idle sessions and waits up to 10 minutes for running agent turns to finish. Turns still running then get a fresh checkpoint, and it waits up to 3 more minutes for sandbox creates, destroys and suspends already under way. Only then do the pools go. A job turn the pools take away anyway resumes from its checkpoint after power-up (at most `HANGAR_JOB_RESTART_RESUMES` times), as after a restart, and so does each best-of-N attempt. Each pool's autoscaling range (or a fixed pool's node count, for the microVM pool) is saved as a label on the cluster, and `up` restores it; a range a `terraform apply` set while the pools were down wins over the saved one.

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

While the pools are off you pay for the system node, disks, images, the bucket and the load balancer, not for sandbox nodes ([team-scale.md](team-scale.md#cost) has the numbers); while off, not for the system node either. GKE's free tier covers the management fee of one zonal cluster per billing account. GKE Sandbox also needs a node pool without it that keeps at least one node ([GKE Sandbox][gkesandbox]), which the system pool is, always on except while off.

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

## 7. Backups and restore

Everything hangar knows is one SQLite file on the `hangar-data` volume: sessions and their checkpoint lists, jobs, schedules, tokens, the vault and the settings `hangar config` stores. Checkpoints themselves are in the bucket. Losing the disk loses the database, so back it up.

**Backups.** With `HANGAR_SESSION_STORE` on a bucket (what `up.sh` sets), hangar backs up its database every 24 hours into `gs://<bucket>/_backups/` and keeps the newest 14. Each backup is taken with SQLite's online backup API in one step (one read transaction: hangar keeps working, and the copy never holds half a write), gzipped, and encrypted with the vault key.

```bash
hangar admin backup        # one now
hangar admin backups       # what's there, newest first
hangar doctor              # "database backups": fails when the newest is more than two intervals old
```

`HANGAR_BACKUP_HOURS` changes the interval (`0` turns it off; unset means 24 with a bucket, off without one) and `HANGAR_BACKUP_KEEP` the count. Both apply without a restart, from the next scheduled run (within an hour when backups were off). Without a bucket, backups go to `<data dir>/sessions/_backups`, which is the same disk: use PD snapshots instead (below).

**Keep the vault key somewhere else.** A backup is useless without the `HANGAR_SECRET_KEY` it was encrypted with, and so is everything in the vault. `up.sh` keeps it in the `hangar` Secret, which dies with the cluster. Keep it in Secret Manager, piped straight there, not in a file on your machine; [security/vault-key.md](security/vault-key.md) has how to rotate it:

```bash
kubectl -n hangar get secret hangar -o jsonpath='{.data.HANGAR_SECRET_KEY}' | base64 -d | \
  gcloud secrets create hangar-vault-key --data-file=- --replication-policy=automatic
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
  "serviceAccountName": "hangar", "enableServiceLinks": false, "securityContext": {"runAsNonRoot": true, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
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
- A session restores from the checkpoints the backup lists. hangar keeps 5 per session (`HANGAR_SESSION_KEEP_CHECKPOINTS`), so a session that checkpointed more than 5 times after the backup has none of those left in the bucket and can't resume. Restore from a recent backup.

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

`kubectl rollout restart`, a deploy (`set image`) and node upgrades stop hangar with SIGTERM (`power.sh down` no longer does: hangar stays up). Requests that wait (MCP `agent_status` with `wait_seconds`, `wait_for_turn`, a blocking A2A send) answer at once with the state as it is, and their clients ask again. Event streams and terminals are cut after `HANGAR_SHUTDOWN_GRACE_SECONDS` (15). Then hangar stops its loops and closes its connections. The pod's `terminationGracePeriodSeconds` is 45 to cover that.

Sandboxes, agent turns and sessions don't stop with the control plane. A new pod re-attaches to them. Shutdown takes no checkpoints. A checkpoint, destroy or backup cut off by it is redone after the restart (the idle check and the reaper run again), so the 300 s pre-destroy budget doesn't need to fit in the grace period.

## 8. E2B SDK clients (optional)

hangar answers E2B's API, so the e2b SDKs and the tools built on them can use GKE sandboxes ([e2b.md](e2b.md)). They need two things from the cluster: hangar-envd reachable in each sandbox (`envd-network.yaml`, already applied) and a way to reach sandbox URLs. There are two ways.

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
- **Git gateway.** `deploy/k8s/gateway.yaml` adds the `hangar-gateway` Service (port 8788) and a NetworkPolicy that lets sandbox pods reach that port, and only that port, of the control plane. `config.yaml` sets `HANGAR_GATEWAY_URL=http://hangar-gateway.hangar.svc:8788`, which keeps GitHub tokens out of sandboxes ([security/git-gateway.md](security/git-gateway.md)); intake-triggered jobs need it.
- **Intake posture.** `config.yaml` sets `HANGAR_INTAKE_POSTURE: "enforce"`: jobs from GitHub, Slack, Jira, GitHub Projects and webhooks need the gateway and get at least `allowlist` egress, even in an `open` environment ([security/front-doors.md](security/front-doors.md#intake-posture)). `"warn"` runs them as before for one release ([upgrading.md](upgrading.md)).
- **Egress allowlists.** The same Service serves hangar's egress proxy on 3128. `config.yaml` sets `HANGAR_EGRESS_URL=http://hangar-gateway.hangar.svc:3128`. Pods of environments with `egress.mode: allowlist` or `none` are then labelled `dev.hangar/egress=proxied` and reach only the gateway and the proxy, with no DNS (hangar's Service names are in their /etc/hosts). Their SandboxTemplate carries that policy as its `networkPolicy`, in place of Agent Sandbox's default policy, which allows the internet; the template stays `Managed` ([security/egress.md](security/egress.md)).
- **Expiry.** Each claim gets `lifecycle.shutdownTime` (sandbox TTL plus 5 minutes) as a backstop to hangar's reaper.
- **Storage.** The control plane keeps state in SQLite on a 20 Gi PVC. It backs itself up to the bucket; PD snapshots are the alternative (§7).
