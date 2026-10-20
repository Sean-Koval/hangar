# Upgrading a running hangar

The procedure is the same for every release: back up, prebuild images, deploy in the right window, run `hangar doctor`.
What differs per release (new defaults, settings to turn on, rollback notes) is on its own page under
[release notes](releases/README.md), newest first. Both are written for the GKE install from [gke.md](gke.md): one
zonal cluster, gVisor sandboxes, the control plane with SQLite on the `hangar-data` PVC, checkpoints in the session
bucket, site settings in the `hangar-site` config map, and `deploy/gke/up.sh` / `power.sh`.
[CHANGELOG.md](https://github.com/Sean-Koval/hangar/blob/main/CHANGELOG.md) lists what each adds.

## The procedure

1. **Find your release** on [release notes](releases/README.md) and read its page: what changes as soon as it starts,
   and what you configure. Releases stack: a hangar older than the newest one does each procedure in order, oldest
   first, and each page says where to continue.
2. **Back up** the database and record the running image, as the release page's first step says (`hangar admin
   backup`, and the vault key in Secret Manager: [the vault key ring](security/vault-key.md)).
3. **Prebuild** every environment image and harness pack tag the new commit needs, so nobody waits on a cold build
   ([gke.md](gke.md#images-and-packs-built-in-ci)).
4. **Deploy in the right window** ([below](#when-to-deploy)): node-pool and image upgrades by hand with `up.sh` while
   the pools are down; routine deploys with [deploy.yml](#deploying-with-deployyml) while they are up.
5. **Check** with `hangar doctor` and the release page's own checks, then turn on what the release makes optional.
6. **Roll back** by the release page's rollback section if a check fails; `deploy.yml` rolls the image back by itself
   when a core acceptance check fails.

| Release | PRs | What it is |
| --- | --- | --- |
| [Adopt & capture and frontier](releases/adopt-capture.md) | #133–#189, with #140 and #177–#186 | The MCP split, the review rules, Slack DMs, run capture, harness locks, trials and the eval lane; includes [the repo-conventions hand-off](releases/adopt-capture.md#the-repo-conventions-hand-off-143) (#143) and [plugins from a seed](releases/adopt-capture.md#claude-code-plugins-from-a-seed-136) (#136) |
| [The safe release](releases/safe-release.md) | #120–#132 | Secure defaults for intake jobs, API keys for automated work, one approval rule, sealed graders, taint gates, new manifest fields and [the `extends` merge rules](releases/safe-release.md#the-extends-merge-rules-132) (#132: one dry run before upgrading) |
| [The team release](releases/team-release.md) | #75–#117 | One hangar for a team of six: people, credentials, front doors, egress, the console, the Cloud Doctor, caches and test results, in four waves |
| [The overnight stack](releases/overnight-stack.md) | #48–#69 | For a hangar older than that: do its backup and read its defaults first, then follow the team release's procedure (its prebuild also covers the overnight guide's "republish the Codex pack" step) |

## When to deploy

Image or node-pool upgrades, which replace the sandbox pool's nodes or every environment's image
(the team release's node pool, the safe release's `sudo: false`), go by hand with `deploy/gke/up.sh` in the power-down
window, **01:00–09:50 ET**: no sandbox is running, and the control plane stays up and queues events. Routine deploys
with `deploy.yml` go while the sandbox pools are on, **10:00–01:00 ET**: its acceptance checks need sandboxes, and it
refuses while the pools are down ([cicd.md](cicd.md#2-start-the-deploy-then-approve-it)). The power schedule is
terraform's `power_down_cron` (`0 1 * * *`) and `power_up_cron` (`50 9 * * *`, ready at 10:00), in
`America/New_York`. With the [overnight lane](gke.md#the-overnight-lane) on, one Spot node stays up through the
night, and nothing suspends its sessions for an upgrade: suspend them first (`hangar session suspend <name>`), or
leave sandbox node changes for the day.

## Deploying with deploy.yml

`.github/workflows/deploy.yml` runs by hand (*Actions → deploy → Run workflow*), on a `v*` tag, or on a merge to `main`
while the infra is up: a merge skips green while it's off, held or powered down, and `power.sh up` deploys main
afterwards ([cicd.md](cicd.md#merges-to-main-deploy-while-the-infra-is-up)). Re-run `setup-cicd.sh` once for the
read-only `hangar-state@` account that check uses, and `terraform apply` once (in the power-down window) so the
scheduled power jobs honour holds:

1. **Images.** It calls `images.yml`, which builds every environment image and harness pack tag the commit needs ([gke.md](gke.md#images-and-packs-built-in-ci)), and builds and pushes the control-plane image, `<registry>/control-plane:<sha>`, by digest.
2. **Approval.** The deploy job uses the `production` environment, so it waits for a required reviewer (the owner) to approve it ([deployments and environments][envs]).
3. **Preflight.** It reads hangar's power state and **refuses while the sandbox pools are powered down** (the checks need sandboxes) unless the run's `force` input is set; a forced deploy runs only the checks that need no sandbox (`doctor`, `power`, `metrics`). It lists the server's MCP tools and records the running image and the doctor checks already failing.
4. **Drain**, then `kubectl set image deploy/hangar hangar=<image>`, `kubectl rollout status`, and `/healthz` through the public URL.
5. **Acceptance:** `scripts/acceptance/gke.sh --no-kubectl --only core --gate core`, so every check talks to hangar's REST, MCP and E2B endpoints, including #86's MCP deadline check; none uses kubectl. Only these [core checks](acceptance.md#core-checks-what-a-deploy-rolls-back-for) decide the rollback. The team checks run after them (`--skip core,model,steer,gateway`) in a step that can't fail the deploy (`continue-on-error`, its own time limit), and are reported. They use the `production` environment's optional `HANGAR_ACCEPT_TEST_REPO` and `HANGAR_ACCEPT_FIXTURE_REPO` variables and `HANGAR_ACCEPT_GITHUB_TOKEN` and `HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET` secrets, and skip without them.
6. **Run summary:** the server version before → after (`<version>+tools.<hash>`), the MCP tools added and removed, each check's result, and the warm and cold create times.
7. **On any failure after the image changed**, a cancelled or timed-out run included: `kubectl set image` back to the image it replaced (recorded in step 3; not `rollout undo`, which would go one revision further back when the run redeployed the running image), wait for it, and fail the run. A `doctor` check that already failed before the deploy (step 3 records them) is reported in the summary and doesn't roll back: a rollback can't fix it, and would block every deploy, the fix included. Either way it records the result in hangar (`POST /api/admin/deploys`), which `hangar doctor` and `hangar power` show.

Until the repository secrets below exist (or on a fork) it skips with a notice and stays green. The deploy's own secrets are **`production` environment secrets**, which GitHub releases to a job only once the required reviewer approves it. As repository secrets, any workflow on any branch could read the admin token, and the approval would gate nothing.

| Secret | Where | Value |
| --- | --- | --- |
| `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `HANGAR_REGISTRY` | repository | shared with `images.yml` ([gke.md](gke.md#images-and-packs-built-in-ci)); `GCP_SERVICE_ACCOUNT` pushes the control-plane image, and never deploys |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `production` | `hangar-deploy@<project>.iam.gserviceaccount.com`, impersonable only from the `production` environment, with only what a deploy needs (below). Required: there is no fallback to `GCP_SERVICE_ACCOUNT`, which every branch's workflows can impersonate |
| `GKE_CLUSTER`, `GKE_LOCATION` | `production` | the cluster's name and zone (`hangar`, `us-central1-a`; `terraform output get_credentials`) |
| `HANGAR_URL` | `production` | `https://<your hangar>` |
| `HANGAR_DEPLOY_TOKEN` | `production` | an admin token for the acceptance checks and the deploy record: `hangar token create deploy --scope admin` |

`bash deploy/gke/setup-cicd.sh` sets up everything in this section, idempotently, and checks it ([cicd.md](cicd.md)); `--dry-run` prints each change first. By hand: in *Settings → Environments*, create `production`, add yourself under *Required reviewers*, and under *Deployment branches and tags* allow only `main` and `v*` tags. Required reviewers need a public repository, or GitHub Enterprise for a private one ([deployments and environments][envs]). Then, as a project owner, after the images setup in gke.md (same `PROJECT`, `NUM`, `REPO`):

```bash
gcloud iam service-accounts create hangar-deploy --project "$PROJECT"
# IAM: only enough to fetch the cluster's credentials (container.clusters.get); what it may do inside the cluster
# comes from the Role below, in the hangar namespace only
gcloud projects add-iam-policy-binding "$PROJECT" --role roles/container.clusterViewer \
  --member "serviceAccount:hangar-deploy@$PROJECT.iam.gserviceaccount.com"
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: hangar-deploy, namespace: hangar}
rules:
  - apiGroups: [apps]
    resources: [deployments]
    resourceNames: [hangar]
    verbs: [get, list, watch, patch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: hangar-deploy, namespace: hangar}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: hangar-deploy}
subjects:
  - {kind: User, name: hangar-deploy@$PROJECT.iam.gserviceaccount.com}
EOF
# Only jobs in the production environment may act as it. GitHub's subject for them is repo:OWNER/REPO:environment:production,
# or repo:OWNER@OWNER_ID/REPO@REPO_ID:environment:production for repositories created after 2026-07-15: bind both.
REPO_ID=$(gh api "repos/$REPO" --jq .id); OWNER_ID=$(gh api "repos/$REPO" --jq .owner.id)
for subject in "repo:$REPO:environment:production" "repo:${REPO%%/*}@$OWNER_ID/${REPO#*/}@$REPO_ID:environment:production"; do
  gcloud iam service-accounts add-iam-policy-binding "hangar-deploy@$PROJECT.iam.gserviceaccount.com" --project "$PROJECT" \
    --role roles/iam.workloadIdentityUser \
    --member "principal://iam.googleapis.com/projects/$NUM/locations/global/workloadIdentityPools/github/subject/$subject"
done
gh secret set GCP_DEPLOY_SERVICE_ACCOUNT --env production --body "hangar-deploy@$PROJECT.iam.gserviceaccount.com"
gh secret set GKE_CLUSTER --env production --body hangar
gh secret set GKE_LOCATION --env production --body "$(terraform -chdir=deploy/terraform/gke output -raw get_credentials | sed -E 's/.*--zone ([^ ]+).*/\1/')"
gh secret set HANGAR_URL --env production --body "https://<your hangar>"
hangar token create deploy --scope admin | tail -1 | gh secret set HANGAR_DEPLOY_TOKEN --env production
```

Moving from an earlier setup of this workflow: set the four secrets above with `--env production`, delete the repository copies (`gh secret delete HANGAR_DEPLOY_TOKEN`, and the others), and replace `roles/container.developer` with the binding above. The script does all of that, removing every project role but `container.clusterViewer` from `hangar-deploy` and every project role from `hangar-ci` (the first setup let `GCP_SERVICE_ACCOUNT` deploy with `container.developer`), any other federated identity bound to either account, and it narrows a provider made from gke.md's earlier condition (any ref of the repository) to `main` and `v*` tags. It also adds a tag ruleset so only repository admins create `v*` tags, which start deploys. That role reads every Secret and can exec into every pod in every cluster of the project, which a deploy doesn't need, and a deploy run executes the commit's dependencies (`uv sync`) with it.

The deploy needs `get`, `list`, `watch` and `patch` on the `hangar` Deployment (`kubectl get`, `set image`, and `rollout status`, which lists and then watches it with a `metadata.name` field selector: [RBAC allows that on a named resource][rbacnames], and without `list` the first rollout fails as forbidden) and `container.clusters.get` to fetch credentials ([GKE roles][gkeroles], [RBAC on GKE: a service account is a `User` subject named by its email, and `container.clusters.get` authenticates without authorizing anything in the cluster][gkerbac]). The kubeconfig `get-gke-credentials` writes holds a token that lasts an hour, so the workflow fetches a fresh one before a rollback.

[envs]: https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments
[gkeroles]: https://docs.cloud.google.com/iam/docs/roles-permissions/container
[gkerbac]: https://docs.cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control
[rbacnames]: https://kubernetes.io/docs/reference/access-authn-authz/rbac/#referring-to-resources

