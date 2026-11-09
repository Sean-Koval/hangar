# Environment images on GKE

This page is part of [Running hangar on GKE](../gke.md).

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

## Harness packs

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

See [specs/0006-environments-and-harness-packs.md](../specs/0006-environments-and-harness-packs.md) for the design.

## Images and packs, built in CI

`.github/workflows/images.yml` runs on every push to `main` and on demand (`workflow_dispatch`). It signs in to Google Cloud with Workload Identity Federation (no service-account key), runs `hangar images build --provider kubernetes`, and pushes to Artifact Registry. Until the three secrets below exist, it skips with a notice and stays green; it also skips on refs other than `main` and `v*` tags, which Google Cloud doesn't trust. It never talks to the cluster, so it also works while the sandbox pools are powered down. `deploy.yml` calls it before every deploy ([upgrading.md](../upgrading.md#deploying-with-deployyml)).

| Secret | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/<project number>/locations/global/workloadIdentityPools/github/providers/hangar` |
| `GCP_SERVICE_ACCOUNT` | `hangar-ci@<project>.iam.gserviceaccount.com`, with `roles/artifactregistry.writer` on the `hangar` repository |
| `HANGAR_REGISTRY` | `terraform -chdir=deploy/terraform/gke output -raw registry`, e.g. `us-central1-docker.pkg.dev/<project>/hangar` |

One-time setup, as a project owner: `bash deploy/gke/setup-cicd.sh` does this, deploy.yml's setup too, and checks it
([cicd.md](../cicd.md)). By hand (replace `OWNER/REPO` with the repository, e.g. `Sean-Koval/hangar`), the same provider:
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
