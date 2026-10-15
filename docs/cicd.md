# Deploying from CI/CD

Hangar's control plane deploys from GitHub Actions, not from a laptop. `.github/workflows/images.yml` builds the
environment images and harness packs on every push to `main`; `.github/workflows/deploy.yml` builds the control-plane
image, waits for your approval, rolls it out, proves it with the acceptance checks and rolls back on failure. A merge
to `main` starts a deploy while the infra is up, and skips it while the infra is off, held or powered down
([Infra states](#infra-states)). The nightly harness canary needs no cloud access. This page takes you through the
first deploy, end to end.

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
| `hangar-state@` (is the infra up?) | Any run the provider admits | A custom role, `hangarClusterState`, with `container.clusters.get` alone, on the project (GKE has no per-cluster IAM): the clusters' metadata and power labels. Not `clusterViewer` (which adds `list` and `connect`); inside the cluster no binding, so only the API discovery Kubernetes grants every authenticated identity; nothing it can change |
| `hangar-deploy@` (deploys) | Only jobs in the `production` environment: subject `repo:OWNER/REPO:environment:production`, or `repo:OWNER@OWNER_ID/REPO@REPO_ID:environment:production` for repositories created after 2026-07-15 ([GitHub's immutable subjects][immutable]); both are bound | `roles/container.clusterViewer` (fetch the cluster's credentials) and a Role in `hangar`: `get`, `list`, `watch`, `patch` on `deployments/hangar`. No secrets, no exec, nothing else |
| The `production` environment | You approve each deploy; only `main` and `v*` tags deploy | Releases its secrets to the deploy job after your approval |
| A tag ruleset on `refs/tags/v*` | Only repository admins (you) create, move or delete `v*` tags | A `v*` tag starts a deploy, and its builds run as `hangar-ci@` before your approval |
| Repository secrets | Every workflow run | `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `HANGAR_REGISTRY`, `GCP_STATE_SERVICE_ACCOUNT`, `HANGAR_CLUSTER` (`projects/P/locations/ZONE/clusters/NAME`): names, not credentials, and none of them deploys |
| `production` secrets | The approved deploy job | `GCP_DEPLOY_SERVICE_ACCOUNT`, `GKE_CLUSTER`, `GKE_LOCATION`, `HANGAR_URL`, and `HANGAR_DEPLOY_TOKEN` |

`HANGAR_DEPLOY_TOKEN` is an admin token named `github-deploy`, minted through hangar's REST API and piped straight into
`gh secret set`. Hangar has no deploy-only scope: the deploy drains hangar, reads its power state, runs the acceptance
checks and records the deploy, all admin endpoints, and the `jobs`, `mcp` and `metrics` scopes reach none of them.
Only the approved deploy job can read it. `--rotate-deploy-token` mints a new one, stores it, then revokes the old.

The script also does the [upgrade](upgrading.md#upgrading-to-the-team-release-75117) steps a script can do safely: it
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
anyway and runs only the checks that need no sandbox (`doctor`, `power`, `metrics`). With hangar off
(`power.sh off`) it refuses even forced: there is nothing running to deploy to; `power.sh up` deploys main for you.

### Merges to main deploy while the infra is up

A push to `main` starts `deploy.yml` too. Its first job, `state`, runs `bash deploy/gke/power.sh state` as
`hangar-state@`, which reads the cluster's power labels and nothing else: no hangar token, no kubectl, no approval
needed because it can't change anything.

- **Up:** the run builds and then waits for your approval on `production`, like any deploy. A merge never deploys
  without your click: the deploy job, the only one with the deploy account, `kubectl` and hangar's admin token, is the
  `production` environment's.
- **Off, held or down:** the run ends green with a notice, *infra is down; not deploying `<sha>`; it will deploy on
  the next power-up*, and builds nothing. No failure, no rollback. `images.yml` still builds the images on every merge,
  whatever the power state.
- **A burst of merges:** the `deploy-production` concurrency group keeps one run going and at most one waiting;
  GitHub cancels the waiting run when a newer one queues ([concurrency][concurrency]: "any existing `pending` job or
  workflow in the same concurrency group will be canceled"). A merge's run whose commit is no longer `main`'s head
  skips with a notice, both before its builds and again right after your approval (a re-run of an old one too), so you
  approve the newest commit, not each one. Two things follow from GitHub's rules: a `v*` tag run waiting behind a
  deploy is replaced by a merge queued after it (push the tag again, or run it once the queue is clear), and a merge
  whose push started no run (`[skip ci]`) leaves the one before it skipped too, until the next merge or power-up.
- **The infra went down while a run waited for you:** the deploy job checks hangar's power state again after the
  approval, and skips green if the pools are down or held.

Before `setup-cicd.sh` has made `hangar-state@` (an older setup), a merge skips with a warning instead: re-run the
script, or deploy by hand.

**Catch-up on power-up.** `bash deploy/gke/power.sh up` (by hand, from off, down or a hold) ends by comparing `main`'s
head with the commit hangar runs (its newest deploy record, else the running image's tag) through GitHub's compare
API. When main is ahead, and no `deploy.yml` run for main is already queued or waiting, it starts one with
`gh workflow run deploy.yml --ref main`, as you; it waits for your approval. Same commit, behind, or diverged (a tag
deploy): nothing. `POWER_CATCH_UP=0` turns it off.

The scheduled power-up job doesn't start deploys. It holds no GitHub credential, and gets none: a token that can start
workflows (`actions: write`) can also re-run, cancel and delete runs and their logs, and stored in Secret Manager it
would be one more long-lived secret, readable by an account that already administers the cluster, to protect and
rotate, for the sake of saving one command. Instead, with `power_deploy_repo` set in terraform (a public repository:
it compares anonymously), it tells hangar that main is ahead. `hangar power`, `hangar status` and `hangar doctor`
then show **deploy pending** with the command to run, `gh workflow run deploy.yml --repo OWNER/REPO --ref main`, until
the next deploy is recorded. Nothing is recorded at merge time: what's pending is worked out at power-up, from GitHub
and hangar, so it can't go stale.

## Infra states

| State | What runs | What it costs a week (*estimate*, [team-scale.md](team-scale.md#cost)) | Webhooks | Jobs and sessions | A merge to main | How to get back |
| --- | --- | --- | --- | --- | --- | --- |
| **up** | System node, hangar, sandbox pools (gVisor at its warm floor, autoscaling) | Idle, the warm floor around the clock ~$78; a typical team week on the schedule ~$157 | Received | Run | Builds, waits for your approval, deploys | - |
| **down** (nightly, 01:00 to 09:50 ET) | System node and hangar; sandbox pools at 0 nodes | The system node, ~$18 for a whole week down | Received and queued | Queued jobs wait (`waiting: sandboxes power on at 10:00`); creates answer `503` with `Retry-After`; sessions are suspended (checkpointed) and resume on their next message | Skips green; deploys on the next power-up | The 09:50 power-up, `hangar power up`, or `bash deploy/gke/power.sh up` |
| **held** (`power.sh hold`, `hangar power hold`) | Whatever ran when the hold began: the scheduled power-ups and power-downs skip until it ends | That state's | That state's | That state's | Skips green while held | It ends by itself at its date and the schedule resumes at its next time (nothing powers up early); `power.sh release` / `hangar power release` end it now; `power.sh up` / `hangar power up` end it and power on |
| **off** (`power.sh off`) | Nothing: every pool at 0 nodes, hangar scaled to 0. Kept: the cluster, the `hangar-data` disk with the SQLite store, the bucket and checkpoints, images, the IP and the load balancer | ~$5, plus GKE's management fee when the free tier is spent on another cluster | **Not received** (the load balancer answers 502). GitHub's are redelivered on the next start if at most 3 days old; Jira's and Slack's are lost | Nothing runs; queued jobs and suspended sessions wait in the store and carry on after `up` | Skips green; `power.sh up` deploys main | `bash deploy/gke/power.sh up`; or when the hold `off` set ends, the next scheduled power-up brings everything back (without the catch-up deploy) |

### Taking a week off

```bash
hangar status                                 # anything running you want to finish first?
bash deploy/gke/power.sh off --until 2026-10-26   # drain, checkpoint and suspend sessions, then everything to 0
                                              # nodes; held until then so the schedule leaves it off
# ... keep developing: merges build images (images.yml) and skip the deploy, green
bash deploy/gke/power.sh status               # cluster: off (everything at 0 nodes); held until Mon 26 Oct 00:00
bash deploy/gke/power.sh up                   # system pool, hangar (ready), sandbox pools, warm floor and prepull;
                                              # ends the hold; starts deploy.yml for main if it's ahead: approve it
hangar doctor                                 # the deploy, the loops, anything stale
```

While it's off, nothing receives webhooks. On its next start hangar asks GitHub to redeliver the deliveries it missed,
but GitHub keeps them for 3 days only ([redelivering webhooks][redeliver]: "You can redeliver webhook deliveries that
occurred in the past 3 days"), so a `/hangar` label or comment from earlier in the week is lost: add it again. Jira
retries a failed webhook up to five times, 5 to 15 minutes apart ([Jira webhooks][jirahooks]), and has no
redelivery, so a Jira issue moved during the week is lost: move it again. Slack events and slash commands are lost
too. A hold from `off` ends by itself at its date, and the next scheduled power-up then brings everything back; pick a
date after your return, or come back with `power.sh up`.

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

- **`deploy: skipped` with a warning on a merge, "Couldn't read the infra state":** `GCP_STATE_SERVICE_ACCOUNT` or
  `HANGAR_CLUSTER` is missing, or `hangar-state@` can't sign in or read the cluster. Re-run the script (it creates and
  checks them); meanwhile deploy by hand.

[wif]: https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
[concurrency]: https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/control-the-concurrency-of-workflows-and-jobs
[redeliver]: https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks
[jirahooks]: https://developer.atlassian.com/cloud/jira/platform/webhooks/
[immutable]: https://github.blog/changelog/2026-04-23-immutable-subject-claims-for-GitHub-actions-oidc-tokens/
[review]: https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments
