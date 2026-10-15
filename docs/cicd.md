# Deploying from CI/CD

Hangar's control plane deploys from GitHub Actions, not from a laptop. `.github/workflows/images.yml` builds the
environment images and harness packs on every push to `main`; `.github/workflows/deploy.yml` builds the control-plane
image, waits for your approval, rolls it out, proves it with the acceptance checks and rolls back on failure. The
nightly harness canary needs no cloud access. This page takes you through the first deploy, end to end.

## 1. Set up the identities, once

Neither workflow holds a key. They sign in to Google Cloud with [Workload Identity Federation][wif] and to hangar with
a token kept in a GitHub environment. `deploy/gke/setup-cicd.sh` creates all of it from your machine, where `gcloud`
(as the project's owner), `kubectl` and `gh` (as the repository's admin) are logged in, and `hangar login` has been
run with an admin token. Hangar must already run on the cluster ([gke.md](gke.md): `deploy/gke/up.sh` makes the first
install; deploy.yml updates it):

```bash
bash deploy/gke/setup-cicd.sh --dry-run   # read-only lookups, and every change it would make, printed
bash deploy/gke/setup-cicd.sh             # make them, then check them (read-only) and say what's left
```

It reads the project, cluster, zone and registry from `deploy/terraform/gke`'s outputs (else `gcloud config`), the
repository from `gh repo view`, and hangar's URL from `hangar login`; `--help` lists the variables that override each.
It is safe to re-run: each step looks first and changes only what is missing or different.

| What | Who can use it | What it can do |
| --- | --- | --- |
| Pool `github`, provider `hangar` | Runs of this repository (checked by name and by numeric id) on `main` or a `v*` tag, started by a push or by hand; pull requests, other branches, forks and `pull_request_target`, `workflow_run`, schedule or comment runs get no Google token. The pool holds no other provider (the check fails if it does: any provider in the pool could sign in as these accounts) | Exchange GitHub's OIDC token for the accounts below |
| `hangar-ci@` (images) | Any run the provider admits | `roles/artifactregistry.writer` on the `hangar` repository only. No cluster access, no Cloud Build (images build with Docker on the runner) |
| `hangar-deploy@` (deploys) | Only jobs in the `production` environment: subject `repo:OWNER/REPO:environment:production`, or `repo:OWNER@OWNER_ID/REPO@REPO_ID:environment:production` for repositories created after 2026-07-15 ([GitHub's immutable subjects][immutable]); both are bound | `roles/container.clusterViewer` (fetch the cluster's credentials) and a Role in `hangar`: `get`, `list`, `watch`, `patch` on `deployments/hangar`. No secrets, no exec, nothing else |
| The `production` environment | You approve each deploy; only `main` and `v*` tags deploy | Releases its secrets to the deploy job after your approval |
| A tag ruleset on `refs/tags/v*` | Only repository admins (you) create, move or delete `v*` tags | A `v*` tag starts a deploy, and its builds run as `hangar-ci@` before your approval |
| Repository secrets | Every workflow run | `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `HANGAR_REGISTRY`: names, not credentials, and none of them deploys |
| `production` secrets | The approved deploy job | `GCP_DEPLOY_SERVICE_ACCOUNT`, `GKE_CLUSTER`, `GKE_LOCATION`, `HANGAR_URL`, and `HANGAR_DEPLOY_TOKEN` |

`HANGAR_DEPLOY_TOKEN` is an admin token named `github-deploy`, minted through hangar's REST API and piped straight into
`gh secret set`. Hangar has no deploy-only scope: the deploy drains hangar, reads its power state, runs the acceptance
checks and records the deploy, all admin endpoints, and the `jobs`, `mcp` and `metrics` scopes reach none of them.
Only the approved deploy job can read it. `--rotate-deploy-token` mints a new one, stores it, then revokes the old.

The script also does the [upgrade](upgrading.md#upgrading-to-the-team-release-75110) steps a script can do safely: it
copies the vault key from the `hangar` Secret into Secret Manager (`hangar-vault-key`, piped, never printed), and
creates the PyPI and npm remote repositories for the package mirrors. It prints, without doing them, what needs you:
the quota requests (Google reviews each, so they can't be automated reliably), the `terraform apply` for the node pool
in the power-down window, branch protection, the GitHub Apps, Jira, and the optional
[acceptance fixtures](acceptance.md#before-the-first-run-fixtures).

Re-running it narrows an earlier, wider setup: it removes any other project role from both accounts (an earlier
`deploy.yml` setup gave `roles/container.developer` to one of them), any other federated identity bound to them, a
deployment policy for another branch, repository copies of the deploy secrets, and a looser provider condition.

## 2. Start the deploy, then approve it

Deploy while the sandbox pools are on (10:00 to 01:00 ET): the acceptance checks need sandboxes.

```bash
gh workflow run deploy.yml --ref main
gh run watch "$(gh run list --workflow deploy.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

A `v*` tag push starts one too. The run builds first: `images` (only what Artifact Registry lacks) and the
control-plane image, tagged with the commit and pinned by digest. Then the `deploy` job waits for you: open the run
and choose **Review deployments**, `production`, **Approve and deploy** ([reviewing deployments][review]). Nothing
touches the cluster, and the environment's secrets don't exist for the run, until you approve. Rejecting it ends the
run with nothing changed.

The deploy refuses while the pools are powered down. `gh workflow run deploy.yml --ref main -f force=true` deploys
anyway and runs only the checks that need no sandbox (`doctor`, `power`, `metrics`).

## 3. Read the result in the run summary

Once approved, the job drains hangar (creates and destroys in flight finish), runs `kubectl set image`, waits for the
rollout and for `/healthz`, then runs the acceptance checks over hangar's REST, MCP and E2B endpoints. The run summary
shows:

- the server version before and after (`<version>+tools.<hash>`);
- the MCP tools added and removed (claude.ai users re-add the connector when the list changed: [clients.md](clients.md));
- one row per check, with `core` marking the [core checks](acceptance.md#core-checks-what-a-deploy-rolls-back-for),
  the only ones that decide a rollback; the team checks after them are reported and never fail the deploy;
- the warm and cold create times.

Hangar records the deploy too: `hangar power` and `hangar doctor` show the last one and its result.

## 4. When it fails, it rolls back

Any failure after the image changed, a cancelled or timed-out run included, sets the image back to the one it
replaced (recorded before the rollout; not `rollout undo`, which can go one revision too far), waits for that rollout,
adds **Rolled back to** to the summary, fails the run and records `rolled_back` in hangar. A `doctor` check that was
already failing before the deploy is reported, not rolled back for: a rollback can't fix it. A failure before the
rollout (a build, a refusal, your rejection) changes nothing on the cluster.

To go back to an earlier release later, push or re-run a `v*` tag that points at it: deploys run only from `main` and
`v*` tags. Rolling back a whole release has steps beyond the image: [upgrading.md](upgrading.md#rolling-back-this-release).

## When signing in fails

- **`images: skipped` on a branch:** expected. Google Cloud trusts only runs of `main` and `v*` tags, so `images.yml`
  run by hand on another branch skips with a notice, and `deploy.yml` refuses before it builds anything.
- **`Permission 'iam.serviceAccounts.getAccessToken' denied` in the deploy job:** the job must run in the `production`
  environment with GitHub's default subject claim (either format). Re-run the script; it warns if the repository
  customizes the claim.
- **`deployments.apps "hangar" is forbidden` from kubectl:** the Role is missing or out of date (it needs `list` for
  `rollout status`); re-run the script, which applies it and checks it with `kubectl auth can-i`.

[wif]: https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
[immutable]: https://github.blog/changelog/2026-04-23-immutable-subject-claims-for-GitHub-actions-oidc-tokens/
[review]: https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments
