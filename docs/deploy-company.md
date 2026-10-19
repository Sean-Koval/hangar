# Deploy on your company's cluster

[Running on GKE](gke.md) describes the owner's own install. `deploy/terraform/gke` creates a whole cluster for it;
`deploy/gke/up.sh` builds the image, `sed`s the project into `deploy/k8s` and applies it; and `deploy.yml` deploys
into that project's Artifact Registry. None of that suits a company that already runs Kubernetes. This page covers
the other path: hangar on a cluster your platform team owns, from released images, with your secrets coming from your
secret manager and hangar's admin configuration kept as reviewed files in your own deploy repository.

| Piece | Where | What it is |
| --- | --- | --- |
| Kustomize base | `deploy/kustomize/base` | The control plane, its sandbox namespace, RBAC and network policies. Names no project, registry or domain |
| Components | `deploy/kustomize/components/` | `gke-workload-identity`, `ingress-gce`, `ingress-nginx`, `external-secrets`, `outbox` |
| Example overlay | `deploy/kustomize/example` | Copy it into your deploy repository |
| Terraform module | `deploy/terraform/modules/hangar-workload` | hangar's Google service account, Workload Identity binding, buckets, and optionally a registry. Never creates a cluster |
| Released images | `.github/workflows/release.yml` | `ghcr.io/sean-koval/hangar:<tag>` and the harness packs, listed by digest in each release |
| Config as code | `hangar apply -f` | Environments, credential references and repo policies from YAML |
| Mounted credentials | `HANGAR_SECRETS_DIR` | A credential whose value is a file from your secret manager |

The owner's path is unchanged: `deploy/k8s`, `deploy/gke/up.sh`, `deploy/terraform/gke` and `deploy.yml` work as
before. The base and `deploy/k8s` share five files (namespaces, the gateway, the sandbox and envd network policies, the
heavy admission policy). `tests/test_deploy_kustomize.py` keeps them byte-identical, so the
[network checks](security.md) cover both.

## What the cluster needs

Your platform team provides these. Neither the module nor the manifests change a cluster:

- **NetworkPolicy enforcement** (GKE Dataplane V2, or a CNI that enforces it). The sandbox network policies are
  hangar's isolation between sandboxes and from the control plane.
- **The Agent Sandbox controller.** On GKE, `gcloud beta container clusters update CLUSTER --enable-agent-sandbox`.
- **A gVisor node pool** for sandboxes. The base sets `HANGAR_K8S_RUNTIME_CLASS=gvisor`; set
  `kata-qemu-runtime-rs` for Kata pools in your `hangar-site`.
- **Workload Identity** on the cluster and its node pools, for the `gke-workload-identity` component.
- **Kubernetes 1.36** for harness packs mounted as image volumes. On older clusters set
  `HANGAR_K8S_IMAGE_VOLUMES=false` and hangar bakes the harness into a per-harness image instead
  ([Harness packs](gke.md#harness-packs)).
- **External Secrets Operator** (`external-secrets.io/v1`, ESO 0.17 or later) and a `ClusterSecretStore`, if you use
  the `external-secrets` component.

The namespaces are fixed: `hangar` for the control plane and `hangar-sandboxes` for sandboxes. The network policies
and `HANGAR_GATEWAY_URL` name them, so don't set `namespace:` in your overlay.

## 1. The cloud side: `hangar-workload`

Call the module from your infra repository, pinned to a release tag. Use a git source of the whole repository: the
optional power schedule runs `deploy/gke/power.sh`, which lives beside the module.

```hcl
module "hangar" {
  source     = "git::https://github.com/Sean-Koval/hangar.git//deploy/terraform/modules/hangar-workload?ref=vX.Y.Z"
  project_id = "my-project"
  region     = "us-central1"

  registry         = { create = true }   # or { create = false, repository_id = "images", location = "us" }
  create_static_ip = true                # for the ingress-gce component
  # run_bucket_readers = ["serviceAccount:flightdeck@my-project.iam.gserviceaccount.com"]
}
```

| Variable | Default | What it does |
| --- | --- | --- |
| `name` | `hangar` | Prefix for the service account, buckets and IP. Change it for a second install in one project |
| `namespace`, `kubernetes_service_account` | `hangar`, `hangar` | Bound with `roles/iam.workloadIdentityUser` |
| `workload_identity_pool` | `<project_id>.svc.id.goog` | Set it when the cluster lives in another project |
| `registry` | `null` | `create = true` makes an Artifact Registry repository; `false` grants writer on yours. `null` grants nothing |
| `enable_cloud_build` | `false` | Lets hangar build environment images with Cloud Build: `roles/cloudbuild.builds.editor` and `roles/storage.admin` on the project. Off: build in CI and let hangar only pull |
| `enable_session_bucket`, `enable_run_bucket` | `true` | Session checkpoints (objectAdmin) and run capture (objectCreator only) |
| `create_static_ip` | `false` | A global address for GKE Ingress |
| `enable_apis` | `true` | Turn it off where the platform team manages project services |
| `power_schedule` | `null` | Opt-in, below |

Its outputs are the values the overlay needs: `gcp_service_account`, `static_ip_name`, `static_ip`,
`run_bucket_url`, `session_bucket_url`, `registry`, and `build_service_account` and `power_up_job` when those
features are on.

**The power schedule is off unless you set it.** With
`power_schedule = { cluster = "shared", zone = "us-central1-a" }`, two Cloud Run jobs that Cloud Scheduler starts
scale the node pools matching `pools` (default `^sandboxes-`) to zero at `down_cron` and back at `up_cron`, as on the
owner's cluster ([section 6](gke.md#6-sandbox-nodes-off-at-night-optional)). Its service account gets
`roles/container.admin` on the project, and the cluster must be zonal. Most shared clusters leave it off.

The owner-only parts of `deploy/terraform/gke` aren't in the module: the cluster and node pools, the overnight lane,
the pod-snapshot bucket, the Docker Hub mirror and the run bucket's write-once deny policy.

## 2. The overlay

Copy `deploy/kustomize/example` into your deploy repository and point it at a release:

```yaml
resources:
  - https://github.com/Sean-Koval/hangar//deploy/kustomize/base?ref=vX.Y.Z
components:
  - https://github.com/Sean-Koval/hangar//deploy/kustomize/components/gke-workload-identity?ref=vX.Y.Z
  - https://github.com/Sean-Koval/hangar//deploy/kustomize/components/ingress-gce?ref=vX.Y.Z
  - https://github.com/Sean-Koval/hangar//deploy/kustomize/components/external-secrets?ref=vX.Y.Z
  - https://github.com/Sean-Koval/hangar//deploy/kustomize/components/outbox?ref=vX.Y.Z
images:
  - name: ghcr.io/sean-koval/hangar
    newName: registry.example.com/mirror/hangar   # optional: your mirror
    digest: sha256:...                            # from the release notes
```

The components take their values from a `hangar-deploy` ConfigMap that your overlay generates with
`config.kubernetes.io/local-config: "true"`. Kustomize reads it and never applies it. A component whose value is
missing fails the build, so nothing ships with a placeholder.

| Key | Component | Value |
| --- | --- | --- |
| `gcp_service_account` | `gke-workload-identity` | module output `gcp_service_account` |
| `domain` | `ingress-gce`, `ingress-nginx` | the DNS name people and MCP clients use |
| `static_ip_name` | `ingress-gce` | module output `static_ip_name` |
| `secret_store` | `external-secrets` | your `ClusterSecretStore` |
| `outbox_url` | `outbox` | module output `run_bucket_url` |

Your settings go in the `hangar-site` ConfigMap. The Deployment reads it after the base's `hangar` ConfigMap, so its
keys win. At the least, set `HANGAR_PUBLIC_URL`, `HANGAR_REGISTRY` (module output `registry`, or your own) and
`HANGAR_SESSION_STORE` (`session_bucket_url`). Doors, egress and the rest are `HANGAR_*` settings ([Configuration](guide.md#configuration)).

**Ingress.** `ingress-gce` is a GKE Ingress with a Google-managed certificate and a one-hour backend timeout for
terminals and long MCP calls. `ingress-nginx` is the same for ingress-nginx: TLS from Secret `hangar-tls`, which you
create or have cert-manager issue (add its issuer annotation in your overlay).

**The `hangar` Secret** holds `HANGAR_ADMIN_TOKEN` and `HANGAR_SECRET_KEY`. The `external-secrets` component syncs it
from remote keys `hangar-admin-token` and `hangar-secret-key`; patch the names if yours differ. Without ESO, create it
once by hand as [gke.md](gke.md#2-control-plane) shows. `HANGAR_SECRET_KEY` seals hangar's vault: rotate it with
[`hangar admin rotate-key`](security.md#rotating-the-vault-key), never by replacing it.

Build and check the overlay, then let your CD apply it:

```bash
kubectl kustomize overlays/prod       # or: kustomize build overlays/prod
kubectl apply -k overlays/prod --dry-run=server
```

## 3. Images

Every `v*` tag runs `release.yml`. It publishes:

- **the control plane**, `ghcr.io/sean-koval/hangar:<tag>`, with an SBOM and provenance attached. hangar-envd, the
  in-sandbox daemon, ships inside it ([hangar-envd](envd.md)), so there is no second image;
- **the harness packs**, `ghcr.io/sean-koval/hangar-packs/harness-<harness>:<version>-<hash>`;
- **a GitHub release** whose notes list each image by digest, with the `images:` entry for your overlay.

Pin the digest, not the tag: running the workflow by hand for an existing tag rebuilds it and moves the tag.

Environment images are yours: build them from your environments in CI with `hangar images build` into your
`HANGAR_REGISTRY` ([Images and packs, built in CI](gke.md#images-and-packs-built-in-ci)). hangar looks packs up under
`HANGAR_REGISTRY` by name and tag. If your cluster can't pull from GHCR, copy each pack to the same name and tag
under your registry, by digest. Otherwise let `hangar images build` build them there too.

## 4. Credentials from your secret manager

A credential can take its value from a file instead of from a value pasted into hangar. The flow is your secret
manager, then External Secrets, then the Kubernetes Secret `hangar-credentials`, then files under
`/var/run/hangar-secrets` (`HANGAR_SECRETS_DIR`), then hangar. The base mounts that Secret read-only, and marks it
optional so hangar starts without it. The `external-secrets` component fills it from every remote secret named
`hangar-credential-<name>`, as the file `<name>`.

You declare the credential with a `source` instead of a value (next section). Hangar stores and seals the reference,
never the value, and reads the file each time the credential is used. A rotation in your secret manager therefore
reaches hangar on ESO's next refresh, with no restart. The value is redacted from agent output like any other
credential, and never returned by the API or written to a log.

The rules:

- **A credential reads only its own file.** The source must be `file:<HANGAR_SECRETS_DIR>/<credential name>`;
  anything else is refused with a 400. A symlink out of the directory, or a file over 1 MiB, reads as no value. So
  does a file that isn't mounted yet; the warning in hangar's log names the path, never the contents.
- **Declare every mounted name with `hangar apply`** before members store credentials of their own. A member can
  create a credential only under a name nobody holds, and could then reference the file of that name.
- **Static secrets only.** An `auth_file` credential whose tokens rotate inside the sandbox is written back to the
  vault when its lease ends. That write replaces the reference with the stored value.
- **Not yet for `github_app` credentials.** The broker validates a GitHub App key's JSON when it is stored, before
  the vault sees the reference, so it refuses one. Keep the App key as a stored value for now.

## 5. Config as code: `hangar apply`

Keep hangar's admin configuration in your deploy repository, reviewed like everything else, and apply it from CD with
an admin token (`HANGAR_URL`, `HANGAR_TOKEN`):

```bash
hangar apply -f policy/            # make hangar match the files
hangar apply -f policy/ --dry-run  # what would change
hangar apply -f policy/ --check    # the same, and exit 1 on any drift (a scheduled drift job)
hangar apply -f policy/ --prune    # also remove repo policies no file declares
```

`-f` takes files or directories (read recursively, `*.yaml` and `*.yml`), and a file may hold several documents.
Each document names its schema:

```yaml
schema: hangar.environment/v1        # an environment spec (environments.md)
name: payments
extends: python
description: payments toolchain
---
schema: hangar.credential/v1         # a mounted credential: a source, never a value
name: anthropic-team
harness: claude-code
kind: api_key
source: file:/var/run/hangar-secrets/anthropic-team
---
schema: hangar.repo-policy/v1        # a repo policy (repo-policy.md): every field `hangar repo set` takes
repo: acme/payments
environment: payments
verify: make test
land: auto_after_approval
approvers: [alice, bob]
```

Applying is declarative. A repo policy becomes exactly what its file says: a field the file leaves out is cleared. An
environment becomes its file's spec. A credential is a team credential unless it names a `person`. Environments are
applied first, then credentials, then the policies that may name them. Applying is idempotent: an object that already
matches is reported `unchanged` and not written, so the second run of the same files writes nothing. A file with an
unknown schema, an unknown field, a credential `value`, or an object declared twice is refused before anything is
applied. A run is all or nothing: if the server refuses a write, the writes already made are rolled back
and apply exits 1 naming the refused document (`file#index`), or lists each object a failed rollback left changed.

Drift is everything apply would create or change, plus repo policies that live in hangar but in no file. `--check`
and `--dry-run` print each field as `live -> declared`, and `--json` gives the same for scripts. The API never returns
a credential's value or source, so a credential counts as unchanged when its metadata (harness, kind, description,
owner, and `env_var`, `file_path` and `exposure` when the file sets them) matches.

## What stays the owner's

`deploy/k8s`, `deploy/gke/*.sh`, `deploy/terraform/gke`, `deploy.yml`, `HANGAR_POWER_UP_JOB` as up.sh sets it, and the
`envs/` examples are the owner's install and examples. Nothing on this page changes them. `deploy.yml` also runs on
`v*` tags, so a release deploys the owner's cluster behind its approval as well. `release.yml` itself never talks to
a cluster.
