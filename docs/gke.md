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
- **Cluster hardening:** see [the CIS mapping](security/gke-cluster.md).
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
`objects.update` away from it, as a backstop: hangar can create runs, never delete or overwrite one. Applying the deny policy needs
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

## Shutdown

`kubectl rollout restart`, a deploy (`set image`) and node upgrades stop hangar with SIGTERM (`power.sh down` no longer does: hangar stays up). Requests that wait (MCP `agent_status` with `wait_seconds`, `wait_for_turn`) answer at once with the state as it is, and their clients ask again. Event streams and terminals are cut after `HANGAR_SHUTDOWN_GRACE_SECONDS` (15). Then hangar stops its loops and closes its connections. The pod's `terminationGracePeriodSeconds` is 45 to cover that.

Sandboxes, agent turns and sessions don't stop with the control plane. A new pod re-attaches to them. Shutdown takes no checkpoints. A checkpoint, destroy or backup cut off by it is redone after the restart (the idle check and the reaper run again), so the 300 s pre-destroy budget doesn't need to fit in the grace period.

## 8. E2B SDK clients (removed) <span id="8-e2b-sdk-clients-optional"></span>

hangar's E2B-compatible API was removed after v0.2.0 ([removed surfaces](upgrading.md#removed-surfaces)). If you applied the old `deploy/k8s/e2b.yaml`, delete its Gateway and route: `kubectl -n hangar delete gateway,httproute hangar-e2b`. Preview ports still need its NetworkPolicy, now `deploy/k8s/preview-network.yaml`.

## Notes

- **Network.** The default SandboxTemplate network policy allows egress to the public internet only, and blocks RFC1918 ranges and the metadata server. Agents in `open` environments reach GitHub and registries, not your VPC. `deploy/k8s/sandbox-network.yaml` applies the same baseline as hangar's own NetworkPolicies (DNS, public internet minus private and link-local ranges, no ingress), so it still holds if the managed policy is turned off or you run the open-source controller.
- **Git gateway.** `deploy/k8s/gateway.yaml` adds the `hangar-gateway` Service (port 8788) and a NetworkPolicy that lets sandbox pods reach that port, and only that port, of the control plane. `config.yaml` sets `HANGAR_GATEWAY_URL=http://hangar-gateway.hangar.svc:8788`, which keeps GitHub tokens out of sandboxes ([security/git-gateway.md](security/git-gateway.md)); intake-triggered jobs need it.
- **Intake posture.** `config.yaml` sets `HANGAR_INTAKE_POSTURE: "enforce"`: intake jobs (GitHub, Slack, Jira, webhooks) need the gateway and get at least `allowlist` egress, even in `open` environments ([security/front-doors.md](security/front-doors.md#intake-posture)). Nothing else is accepted ([removed switches](upgrading.md#removed-switches)).
- **Egress allowlists.** The same Service serves hangar's egress proxy on 3128 (`HANGAR_EGRESS_URL`). `config.yaml` sets `HANGAR_EGRESS_DEFAULT: "allowlist"`, so environments without their own `egress.mode` are proxied ([what gets through](security/egress-default.md)). Their pods carry `dev.hangar/egress=proxied` and reach only the gateway and the proxy, with no DNS. Their SandboxTemplate carries that policy in place of Agent Sandbox's default, which allows the internet ([security/egress.md](security/egress.md)).
- **Expiry.** Each claim gets `lifecycle.shutdownTime` (sandbox TTL plus 5 minutes) as a backstop to hangar's reaper.
- **Storage.** The control plane keeps state in SQLite on a 20 Gi PVC. It backs itself up to the bucket; PD snapshots are the alternative ([§7](gke/backups.md)).

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="3-environment-images"></span>3. Environment images | [Environment images on GKE](gke/images.md) |
| <span id="harness-packs"></span>Harness packs | [Environment images on GKE](gke/images.md#harness-packs) |
| <span id="images-and-packs-built-in-ci"></span>Images and packs, built in CI | [Environment images on GKE](gke/images.md#images-and-packs-built-in-ci) |
| <span id="4-kata-microvms-optional"></span>4. Kata microVMs (optional) | [Kata, the heavy tier and Pod snapshots on GKE](gke/microvms.md#4-kata-microvms-optional) |
| <span id="4a-the-heavy-tier-optional"></span>4a. The heavy tier (optional) | [Kata, the heavy tier and Pod snapshots on GKE](gke/microvms.md#4a-the-heavy-tier-optional) |
| <span id="5-pause-and-resume-with-pod-snapshots-optional"></span>5. Pause and resume with Pod snapshots (optional) | [Kata, the heavy tier and Pod snapshots on GKE](gke/microvms.md#5-pause-and-resume-with-pod-snapshots-optional) |
| <span id="6-sandbox-nodes-off-at-night-optional"></span>6. Sandbox nodes off at night (optional) | [Sandbox nodes off at night](gke/power.md) |
| <span id="the-overnight-lane"></span>The overnight lane | [Sandbox nodes off at night](gke/power.md#the-overnight-lane) |
| <span id="infra-states"></span>Infra states | [Infra states on GKE](gke/infra-states.md) |
| <span id="7-backups-and-restore"></span>7. Backups and restore | [Backups and restore on GKE](gke/backups.md) |
