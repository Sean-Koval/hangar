# One hangar for a team: the team release

One hangar for a team of six. What the four waves add and what you configure; then [what changes as soon as it starts](team-release-changes.md), the steps ([1–8](team-release-procedure.md), [9–11](team-release-sandboxes.md), [12–14 and rolling back](team-release-operations.md)). Every release is listed on [release notes](README.md).

The short version, in order:

```bash
hangar admin backup                                   # 1. back up; record the running image; the vault key in Secret Manager
#                                                       2. request quotas (days ahead; N2 too for a heavy pool)
uv run hangar images build --registry "$REG"          # 3. prebuild the new image and pack tags
#                                                       4. in the power-down window: terraform apply (node pool, power
#                                                          schedule; optional: the heavy pool, the Docker Hub remote repo)
bash deploy/gke/power.sh up && bash deploy/gke/up.sh  # 5. bring the cluster up, roll out
hangar doctor && hangar status                        # 6. check
hangar cred migration                                 # 7. people: your logins became yours; add team API keys
hangar person add alice
hangar config set HANGAR_JOB_BUDGET_MAX_COST_USD 10   # 8. budgets (yours to choose; jobs run on balanced), then concurrency
hangar doctor --fix                                   # 9. front doors: the pull_request event; then the App, Jira, Projects
hangar config set HANGAR_EGRESS_URL http://hangar-gateway.hangar.svc:3128   # 10. sandboxes: egress, mirrors, cloud, previews,
#                                                       the heavy tier, build caches, test results
hangar config set HANGAR_MODEL_LIST_CREDENTIALS '{"anthropic": "anthropic-models"}'   # 11. models, the canary, env setup,
#                                                       the sister tool's interfaces
hangar admin doctor run --dry-run                     # 12. operations: the Cloud Doctor, the console, the vault key
#                                                       13. deploy.yml: the production environment and its secrets
bash scripts/acceptance/gke.sh --no-kubectl           # 14. clients and proof: docs/acceptance.md, in its order
```

## What you configure

Everything the owner sets up, in the order of the steps. **Required** means the release, or the feature named, doesn't work without it; **optional** means it is off (or keeps its old behaviour) until you set it. `hangar config set NAME VALUE` keeps a setting in hangar's database; `hangar-site` wins over it.

| What | Required? | Step |
| --- | --- | --- |
| The vault key in Secret Manager (`hangar-vault-key`), copied from the `hangar` Secret, never to a laptop | **Required** on GKE before step 5 (a backup can't be restored without it, and rotation reads it from there) | [1](team-release-procedure.md#1-back-up) |
| `HANGAR_BACKUP_STORE`: a bucket of its own, write-only for hangar's service account (`roles/storage.objectCreator`), with a lifecycle rule | Optional (backups stay beside the checkpoints; `hangar doctor` warns) | [1](team-release-procedure.md#1-back-up) |
| Quotas: `N2D_CPUS` ≥ 160, `CPUS_ALL_REGIONS`, `SSD_TOTAL_GB` ≥ 2,250 | **Required** on GKE | [2](team-release-procedure.md#2-request-quotas-days-ahead) |
| Quotas for the heavy pool: `N2_CPUS` ≥ 128 (4 × `n2-standard-32`) on top of the Kata pool's, and 2,000 GB more `SSD_TOTAL_GB` | Optional; **required** for the heavy tier on GKE | [2](team-release-procedure.md#2-request-quotas-days-ahead) |
| Images workflow secrets (`GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `HANGAR_REGISTRY`, repository secrets) | Optional: without them, prebuild by hand | [3](team-release-procedure.md#3-prebuild-the-new-image-and-pack-tags) |
| `terraform apply`: the `n2d-standard-16` node pool, `power_up_cron` 09:50, `sandbox_warm_nodes`, hangar's `roles/run.invoker` on the power-up job | **Required** on GKE | [4](team-release-procedure.md#4-in-the-power-down-window-the-node-pool-and-the-power-schedule) |
| `terraform apply` with `enable_heavy_pool = true` (and `heavy_machine_type`, `heavy_max_nodes`, `heavy_spot`, `heavy_disk_gb`): the `sandboxes-heavy` pool and its `hangar-heavy-nodes` service account | Optional; **required** for the heavy tier on GKE | [4](team-release-procedure.md#4-in-the-power-down-window-the-node-pool-and-the-power-schedule) |
| `terraform apply` with `enable_dockerhub_mirror = true` (`dockerhub_username`, `dockerhub_token_secret`): the Artifact Registry remote repository for Docker Hub | Optional (heavy sandboxes pull from Docker Hub, rate-limited, or `HANGAR_HEAVY_REGISTRY_MIRRORS`) | [4](team-release-procedure.md#4-in-the-power-down-window-the-node-pool-and-the-power-schedule) |
| `HANGAR_POWER_UP_JOB` | Set by `up.sh`; **required** for `hangar power up` | [5](team-release-procedure.md#5-bring-the-cluster-up-and-roll-out) |
| Team credentials: API keys (`hangar cred add NAME --team --kind api_key`). A subscription login is refused as one | **Required** for work no person owns (webhooks, schedules without a person, unlinked senders) | [7](team-release-procedure.md#7-people-your-logins-stay-yours-add-team-credentials-then-the-team) |
| People, their tokens and linked GitHub, Slack and Jira accounts | Optional (required for a team) | [7](team-release-procedure.md#7-people-your-logins-stay-yours-add-team-credentials-then-the-team) |
| `HANGAR_BUDGET_DAY_TIMEZONE`, `HANGAR_JOB_BUDGET_MAX_COST_USD`, `HANGAR_PERSON_MAX_USD_PER_DAY`, repo `max_usd_per_day` | Optional; **required** before raising `HANGAR_JOB_CONCURRENCY`. hangar sets no amount: jobs default to `balanced`, uncapped, and `hangar doctor` warns (`model spend`) until one is set | [8](team-release-procedure.md#8-budgets-then-job-concurrency) |
| The webhook's `pull_request` event (`hangar doctor --fix`) | **Required** | [9a](team-release-sandboxes.md#9a-the-github-webhook) |
| GitHub App: `github_app` credential and `HANGAR_GITHUB_APP_CREDENTIAL` | Optional; **required** for landing | [9b](team-release-sandboxes.md#9b-the-github-app-and-landing) |
| Branch protection (1 approval, **dismiss stale approvals**, approval of the latest push, required checks) and *Allow auto-merge* | **Required** on every repo with `land: auto_after_approval`: without stale dismissal hangar doesn't arm and `hangar doctor` fails (opt out per repo: `--no-require-stale-dismissal`) | [9b](team-release-sandboxes.md#9b-the-github-app-and-landing) |
| Repo policy `land`, `approvers`, `merge_method`, `risk_paths` | Optional (landing is off by default) | [9b](team-release-sandboxes.md#9b-the-github-app-and-landing) |
| Jira: an account and API token (`jira-api`), the webhook and its secret (`jira-webhook`) or an Automation token, `HANGAR_JIRA` | Optional | [9c](team-release-sandboxes.md#9c-jira) |
| GitHub Projects: an organization webhook (Projects v2 items), the control token's `project` scope, `HANGAR_GITHUB_PROJECTS` | Optional | [9d](team-release-sandboxes.md#9d-github-projects) |
| The control token's organization *Webhooks: Read and write* (classic: `admin:org_hook`) | Optional: without it, missed Projects deliveries aren't redelivered (a warning) | [9d](team-release-sandboxes.md#9d-github-projects) |
| `HANGAR_EGRESS_URL` | **Required** on GKE before any environment uses `egress: allowlist` or `none` | [10a](team-release-sandboxes.md#10a-egress-allowlists) |
| `HANGAR_EGRESS_DEFAULT` | Optional (default `open` for people's own work, as before; intake jobs get an `allowlist` floor regardless) | [10a](team-release-sandboxes.md#10a-egress-allowlists) |
| `HANGAR_GATEWAY_URL` and `HANGAR_EGRESS_URL` (both now set in `deploy/k8s/config.yaml`); `HANGAR_INTAKE_POSTURE=warn` on v0.2.0 only | **Required** for jobs from GitHub, Slack, Jira, GitHub Projects and webhooks: the default posture `enforce` refuses them without the gateway (#123) | [10a](team-release-sandboxes.md#10a-egress-allowlists) |
| GKE: Artifact Registry remote repositories for PyPI and npm (Go stays on the public proxy), `roles/artifactregistry.reader` for hangar's service account, and `HANGAR_PACKAGE_MIRRORS` | Optional (the mirror reads the public registries by default) | [10a](team-release-sandboxes.md#10a-egress-allowlists) |
| `presets: [packages, github]` on proxied `credential_mode: direct` environments that clone from GitHub | **Required** for them: `packages` no longer includes `github` | [10a](team-release-sandboxes.md#10a-egress-allowlists) |
| GitHub App: *Actions*, *Checks* and *Issues*: Read | Optional: the read-only API through the gateway asks for them | [10a](team-release-sandboxes.md#10a-egress-allowlists) |
| `cloud.lifetime_minutes` at most 60 | **Required** for environments that set more (they no longer load) | [10b](team-release-sandboxes.md#10b-short-lived-cloud-credentials) |
| `HANGAR_OIDC_KEY_ROTATION_DAYS` | Optional (off) | [10b](team-release-sandboxes.md#10b-short-lived-cloud-credentials) |
| Computer environments: rebuild their images | **Required** for their browsers in proxied environments (Chromium's ECH policy) | [10a](team-release-sandboxes.md#10a-egress-allowlists) |
| Docker hosts: Docker Engine ≥ 26.0.0 (or 25.0.4, 23.0.11), and the host firewall accepting TCP 3128 and 8788 from `hangar1` | **Required** for proxied environments on Docker | [10a](team-release-sandboxes.md#10a-egress-allowlists) |
| `HANGAR_OIDC_ISSUER` (https; public for AWS), and the cloud's trust (a GCP workload identity pool, an AWS role) | Optional; **required** for environments with `cloud:` | [10b](team-release-sandboxes.md#10b-short-lived-cloud-credentials) |
| Build secrets: `build_secrets` on an environment or repo policy | Optional; **required** for repos whose `.hangar/environment.yaml` names any | [10c](team-release-sandboxes.md#10c-environments-that-live-with-the-repo) |
| `HANGAR_ENV_SERVICE_IMAGES` | Optional (Docker's official images by default) | [10c](team-release-sandboxes.md#10c-environments-that-live-with-the-repo) |
| `HANGAR_PREVIEW_DOMAIN`, its wildcard DNS and certificate, and `deploy/k8s/preview-network.yaml`'s NetworkPolicy | Optional (previews are off) | [10d](team-release-sandboxes.md#10d-preview-ports) |
| `HANGAR_PR_MEDIA_LINKS=signed` | Optional (screenshots and demos are listed by name) | [10e](team-release-sandboxes.md#10e-the-agents-computer) |
| The heavy tier: `HANGAR_TIERS` `heavy` (`["kubernetes"]` or `["e2b"]`), kata-deploy on the heavy pool (`KATA=1 up.sh`), `HANGAR_HEAVY_RUNTIME_CLASS`, `HANGAR_HEAVY_NODE_SELECTOR` | Optional (the tier is off; heavy environments fail to start, saying so); **required** for Docker in sandboxes and the `large`/`xlarge` profiles | [10f](team-release-sandboxes.md#10f-the-heavy-tier) |
| On a cluster with GKE's Agent Sandbox add-on: the open-source controller instead, or the heavy tier on microVMs | **Required** for the heavy tier there (the add-on's policy refuses privileged Kata pods) | [10f](team-release-sandboxes.md#10f-the-heavy-tier) |
| Repo policy `heavy: allow` (or `on`) per repo, `profile`, `max_profile` | Optional; **required** for a repo whose `.hangar/environment.yaml` asks for Docker: it is deny by default | [10f](team-release-sandboxes.md#10f-the-heavy-tier) |
| `HANGAR_HEAVY_MIRROR` and `HANGAR_HEAVY_MIRROR_UPSTREAM` (terraform output `dockerhub_mirror`), `HANGAR_HEAVY_REGISTRY_MIRRORS` | Optional (off) | [10f](team-release-sandboxes.md#10f-the-heavy-tier) |
| `HANGAR_HEAVY_PERSON_MAX`, `HANGAR_HEAVY_WARM_POOLS`, `HANGAR_HEAVY_DOCKER_IMAGE` | Optional (3 `large`, 1 `xlarge` per person and for unattributed work together; no heavy warm pools; `docker:29.8.2-dind`) | [10f](team-release-sandboxes.md#10f-the-heavy-tier) |
| `HANGAR_HEAVY_DOCKER_PRIVILEGED` | Optional, Docker provider only, for CI and throwaway machines (root on the host) | [10f](team-release-sandboxes.md#10f-the-heavy-tier) |
| Build caches: `HANGAR_GATEWAY_URL` | **Required** for caches (sandboxes reach `/cache` on the gateway with its grant; without it nothing is cached) | [10g](team-release-sandboxes.md#10g-build-caches) |
| A cache bucket of its own: `HANGAR_BUILD_CACHE_STORE=gs://…`, with `roles/storage.objectAdmin` on it for hangar's service account | Optional (the session bucket's `caches/` prefix, which hangar can already write) | [10g](team-release-sandboxes.md#10g-build-caches) |
| `HANGAR_BUILD_CACHE_MAX_GB`, `_REPO_MAX_GB`, `_RETENTION_DAYS`, `_PRIVATE_DAYS`, `_PRIVATE_WRITES`, `_TRANSFER_MB`, `_WAIT_MINUTES` | Optional (100 GB in a bucket, 20 GB a repo, 14 and 3 days, private writes on, 256 MB, 10 min) | [10g](team-release-sandboxes.md#10g-build-caches) |
| `HANGAR_TEST_REPORTS`; repo policy `verify_reports` and `rerun_flaky` | Optional (`inject`; reports at the usual paths; no reruns) | [10h](team-release-sandboxes.md#10h-test-results) |
| `HANGAR_TRACE_LIVE_TOOLS`, `HANGAR_EXPERIMENT_MAX_TRIALS`, `HANGAR_EXPERIMENT_PARALLEL` | Optional (off, 200, 8) | [11d](team-release-sandboxes.md#11d-the-sister-tools-interfaces) |
| Model list keys: `HANGAR_MODEL_LIST_CREDENTIALS` (`webhook` credentials holding Anthropic, OpenAI and Gemini API keys, kept from sandboxes) | Optional: without them, `balanced` and `frontier` resolve to hangar's built-in ids (OpenRouter's list needs no key). Env setup now lists models with these, not with the team's harness keys | [11a](team-release-sandboxes.md#11a-model-list-keys) |
| Harness canary, on hangar's own repository: branch protection on `main` (required approval, `contract` as a required check) and *Allow auto-merge* | **Required** before the first bump PR can land (without required approvals auto-merge would need none) | [11b](team-release-sandboxes.md#11b-the-harness-canary) |
| Harness canary: repository secrets `CANARY_APP_CLIENT_ID` and `CANARY_APP_PRIVATE_KEY` (a dedicated App, Contents and Pull requests: Read and write); variable `CANARY_APPROVERS` | Optional; the App is **required** for checks to run on the bump PR by themselves and for `images` to run on its merge | [11b](team-release-sandboxes.md#11b-the-harness-canary) |
| Env setup: `HANGAR_ENV_SETUP_MODELS`, `HANGAR_ENV_SETUP_MAX_COST_USD` / `_MAX_TURNS`, `HANGAR_ENV_SETUP_LABEL` | Optional (defaults: `balanced`, $30 and 16 turns, `hangar-env-setup`) | [11c](team-release-sandboxes.md#11c-agent-led-environment-setup) |
| The Cloud Doctor: a **team API key** for its harness (Claude Code unless `HANGAR_DOCTOR_HARNESS`) | Optional: without one the doctor only reports, never starts a job | [12a](team-release-operations.md#12a-the-cloud-doctor) |
| `HANGAR_DOCTOR_SPECS_REPO` (and `HANGAR_DOCTOR_SPECS_PATHS`): the repo holding environment specs as files | Optional: without it, fixes to environment specs are reported, not opened as PRs | [12a](team-release-operations.md#12a-the-cloud-doctor) |
| `HANGAR_DOCTOR_MODEL` | Optional (`balanced`); `frontier` for the strongest, or `best` for that on a team key that may lack Fable access | [12a](team-release-operations.md#12a-the-cloud-doctor) |
| `HANGAR_DOCTOR_MAX_USD`, `HANGAR_DOCTOR_JOB_MAX_USD`, `HANGAR_DOCTOR_SCHEDULE` | Optional ($10 a run, $3 a job, 00:30; `off` turns it off) | [12a](team-release-operations.md#12a-the-cloud-doctor) |
| A new vault key in Secret Manager, then `hangar admin rotate-key` | Optional (rotate on a schedule, when someone who could read it leaves, at once on a leak) | [12c](team-release-operations.md#12c-rotating-the-vault-key) |
| deploy.yml: the `production` environment (you as required reviewer; `main` and `v*` tags only), its secrets `GCP_DEPLOY_SERVICE_ACCOUNT`, `GKE_CLUSTER`, `GKE_LOCATION`, `HANGAR_URL`, `HANGAR_DEPLOY_TOKEN`, and the `hangar-deploy` service account and Role | Optional; **required** to deploy with deploy.yml | [13](team-release-operations.md#13-deploying-with-deployyml) |
| claude.ai users re-add the hangar connector | **Required** for claude.ai | [14](team-release-operations.md#14-clients-and-proof) |
| Acceptance fixtures: a test repo (App installed, *Allow auto-merge*, branch protection with code owners), an approver account's classic `repo` token, a fixture repo, a Jira project mapped to the test repo; deploy.yml's `HANGAR_ACCEPT_TEST_REPO`, `HANGAR_ACCEPT_FIXTURE_REPO` (variables), `HANGAR_ACCEPT_GITHUB_TOKEN`, `HANGAR_ACCEPT_APPROVER_TOKEN`, `HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET` (secrets) | Optional: each check skips, saying why, without its fixture; **required** for `secrets`, `landing`, `envbuild` and `intake` to prove anything | [14](team-release-operations.md#14-clients-and-proof) |

