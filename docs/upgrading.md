# Upgrading a running hangar

Two guides, newest first:

- [**This release**](#upgrading-to-the-team-release-75117) (#75–#117): one ordered procedure for everything since the overnight stack. Wave 1 (#75–#90): one hangar for a team of six at ~60 sessions, people and per-person credentials, front-door defaults and job budgets, the reconciler, the MCP deadline contract, and images built before anyone waits. Wave 2 (#91–#100): team credentials and linked accounts, Jira and GitHub Projects intake, egress allowlists, secret scanning and short-lived cloud credentials, a computer for every agent, environments that live with the repo, an approval that lands the PR, the always-on control plane and gated deploys. Wave 3 (#101–#110): review-round trust and the stale-approval gate, egress closed further (no ECH, read-only package mirrors, pushes scanned in the gateway), history out of memory, a rotatable vault key, repo builds that pass on less, the operator console and the Cloud Doctor, the harness canary and `frontier` models, agent-led environment setup, and the acceptance suite for all of it; then least privilege for jobs and builds, CI/CD setup and the infra states (#111–#113). Wave 4 (#114–#117): heavy development environments (Docker inside a VM of the sandbox's own on the heavy tier, deny by default for a repo's own request, and bigger resource profiles), warm dependency and compile caches, structured test results from every check, and the sister tool's three interfaces (harness manifests, trace export, experiment jobs).
- [**The overnight stack**](#upgrading-from-before-the-overnight-stack-4869) (#48–#69), for a hangar older than that. Do its backup and read its defaults first, then follow this release's procedure: its prebuild also covers the overnight guide's "republish the Codex pack" step.

Both are written for the GKE install from [gke.md](gke.md): one zonal cluster, gVisor sandboxes, the control plane with SQLite on the `hangar-data` PVC, checkpoints in the session bucket, site settings in the `hangar-site` config map, and `deploy/gke/up.sh` / `power.sh`. [CHANGELOG.md](../CHANGELOG.md) lists what each adds.

## Upgrading to the team release (#75–#117)

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

### What you configure

Everything the owner sets up, in the order of the steps. **Required** means the release, or the feature named, doesn't work without it; **optional** means it is off (or keeps its old behaviour) until you set it. `hangar config set NAME VALUE` keeps a setting in hangar's database; `hangar-site` wins over it.

| What | Required? | Step |
| --- | --- | --- |
| The vault key in Secret Manager (`hangar-vault-key`), copied from the `hangar` Secret, never to a laptop | **Required** on GKE before step 5 (a backup can't be restored without it, and rotation reads it from there) | [1](#1-back-up) |
| `HANGAR_BACKUP_STORE`: a bucket of its own, write-only for hangar's service account (`roles/storage.objectCreator`), with a lifecycle rule | Optional (backups stay beside the checkpoints; `hangar doctor` warns) | [1](#1-back-up) |
| Quotas: `N2D_CPUS` ≥ 160, `CPUS_ALL_REGIONS`, `SSD_TOTAL_GB` ≥ 2,250 | **Required** on GKE | [2](#2-request-quotas-days-ahead) |
| Quotas for the heavy pool: `N2_CPUS` ≥ 128 (4 × `n2-standard-32`) on top of the Kata pool's, and 2,000 GB more `SSD_TOTAL_GB` | Optional; **required** for the heavy tier on GKE | [2](#2-request-quotas-days-ahead) |
| Images workflow secrets (`GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `HANGAR_REGISTRY`, repository secrets) | Optional: without them, prebuild by hand | [3](#3-prebuild-the-new-image-and-pack-tags) |
| `terraform apply`: the `n2d-standard-16` node pool, `power_up_cron` 09:50, `sandbox_warm_nodes`, hangar's `roles/run.invoker` on the power-up job | **Required** on GKE | [4](#4-in-the-power-down-window-the-node-pool-and-the-power-schedule) |
| `terraform apply` with `enable_heavy_pool = true` (and `heavy_machine_type`, `heavy_max_nodes`, `heavy_spot`, `heavy_disk_gb`): the `sandboxes-heavy` pool and its `hangar-heavy-nodes` service account | Optional; **required** for the heavy tier on GKE | [4](#4-in-the-power-down-window-the-node-pool-and-the-power-schedule) |
| `terraform apply` with `enable_dockerhub_mirror = true` (`dockerhub_username`, `dockerhub_token_secret`): the Artifact Registry remote repository for Docker Hub | Optional (heavy sandboxes pull from Docker Hub, rate-limited, or `HANGAR_HEAVY_REGISTRY_MIRRORS`) | [4](#4-in-the-power-down-window-the-node-pool-and-the-power-schedule) |
| `HANGAR_POWER_UP_JOB` | Set by `up.sh`; **required** for `hangar power up` | [5](#5-bring-the-cluster-up-and-roll-out) |
| Team credentials: API keys (`hangar cred add NAME --team --kind api_key`). A subscription login is refused as one | **Required** for work no person owns (webhooks, schedules without a person, unlinked senders) | [7](#7-people-your-logins-stay-yours-add-team-credentials-then-the-team) |
| People, their tokens and linked GitHub, Slack and Jira accounts | Optional (required for a team) | [7](#7-people-your-logins-stay-yours-add-team-credentials-then-the-team) |
| `HANGAR_BUDGET_DAY_TIMEZONE`, `HANGAR_JOB_BUDGET_MAX_COST_USD`, `HANGAR_PERSON_MAX_USD_PER_DAY`, repo `max_usd_per_day` | Optional; **required** before raising `HANGAR_JOB_CONCURRENCY`. Hangar sets no amount: jobs default to `balanced`, uncapped, and `hangar doctor` warns (`model spend`) until one is set | [8](#8-budgets-then-job-concurrency) |
| The webhook's `pull_request` event (`hangar doctor --fix`) | **Required** | [9a](#9a-the-github-webhook) |
| GitHub App: `github_app` credential and `HANGAR_GITHUB_APP_CREDENTIAL` | Optional; **required** for landing | [9b](#9b-the-github-app-and-landing) |
| Branch protection (1 approval, **dismiss stale approvals**, approval of the latest push, required checks) and *Allow auto-merge* | **Required** on every repo with `land: auto_after_approval`: without stale dismissal hangar doesn't arm and `hangar doctor` fails (opt out per repo: `--no-require-stale-dismissal`) | [9b](#9b-the-github-app-and-landing) |
| Repo policy `land`, `approvers`, `merge_method`, `risk_paths` | Optional (landing is off by default) | [9b](#9b-the-github-app-and-landing) |
| Jira: an account and API token (`jira-api`), the webhook and its secret (`jira-webhook`) or an Automation token, `HANGAR_JIRA` | Optional | [9c](#9c-jira) |
| GitHub Projects: an organization webhook (Projects v2 items), the control token's `project` scope, `HANGAR_GITHUB_PROJECTS` | Optional | [9d](#9d-github-projects) |
| The control token's organization *Webhooks: Read and write* (classic: `admin:org_hook`) | Optional: without it, missed Projects deliveries aren't redelivered (a warning) | [9d](#9d-github-projects) |
| `HANGAR_EGRESS_URL` | **Required** on GKE before any environment uses `egress: allowlist` or `none` | [10a](#10a-egress-allowlists) |
| `HANGAR_EGRESS_DEFAULT` | Optional (default `open`, as before) | [10a](#10a-egress-allowlists) |
| GKE: Artifact Registry remote repositories for PyPI and npm (Go stays on the public proxy), `roles/artifactregistry.reader` for hangar's service account, and `HANGAR_PACKAGE_MIRRORS` | Optional (the mirror reads the public registries by default) | [10a](#10a-egress-allowlists) |
| `presets: [packages, github]` on proxied `credential_mode: direct` environments that clone from GitHub | **Required** for them: `packages` no longer includes `github` | [10a](#10a-egress-allowlists) |
| GitHub App: *Actions*, *Checks* and *Issues*: Read | Optional: the read-only API through the gateway asks for them | [10a](#10a-egress-allowlists) |
| `cloud.lifetime_minutes` at most 60 | **Required** for environments that set more (they no longer load) | [10b](#10b-short-lived-cloud-credentials) |
| `HANGAR_OIDC_KEY_ROTATION_DAYS` | Optional (off) | [10b](#10b-short-lived-cloud-credentials) |
| Computer environments: rebuild their images | **Required** for their browsers in proxied environments (Chromium's ECH policy) | [10a](#10a-egress-allowlists) |
| Docker hosts: Docker Engine ≥ 26.0.0 (or 25.0.4, 23.0.11), and the host firewall accepting TCP 3128 and 8788 from `hangar1` | **Required** for proxied environments on Docker | [10a](#10a-egress-allowlists) |
| `HANGAR_OIDC_ISSUER` (https; public for AWS), and the cloud's trust (a GCP workload identity pool, an AWS role) | Optional; **required** for environments with `cloud:` | [10b](#10b-short-lived-cloud-credentials) |
| Build secrets: `build_secrets` on an environment or repo policy | Optional; **required** for repos whose `.hangar/environment.yaml` names any | [10c](#10c-environments-that-live-with-the-repo) |
| `HANGAR_ENV_SERVICE_IMAGES` | Optional (Docker's official images by default) | [10c](#10c-environments-that-live-with-the-repo) |
| `HANGAR_PREVIEW_DOMAIN`, its wildcard DNS and certificate, and `deploy/k8s/e2b.yaml`'s NetworkPolicy | Optional (previews are off) | [10d](#10d-preview-ports) |
| `HANGAR_PR_MEDIA_LINKS=signed` | Optional (screenshots and demos are listed by name) | [10e](#10e-the-agents-computer) |
| The heavy tier: `HANGAR_TIERS` `heavy` (`["kubernetes"]` or `["e2b"]`), kata-deploy on the heavy pool (`KATA=1 up.sh`), `HANGAR_HEAVY_RUNTIME_CLASS`, `HANGAR_HEAVY_NODE_SELECTOR` | Optional (the tier is off; heavy environments fail to start, saying so); **required** for Docker in sandboxes and the `large`/`xlarge` profiles | [10f](#10f-the-heavy-tier) |
| On a cluster with GKE's Agent Sandbox add-on: the open-source controller instead, or the heavy tier on microVMs | **Required** for the heavy tier there (the add-on's policy refuses privileged Kata pods) | [10f](#10f-the-heavy-tier) |
| Repo policy `heavy: allow` (or `on`) per repo, `profile`, `max_profile` | Optional; **required** for a repo whose `.hangar/environment.yaml` asks for Docker: it is deny by default | [10f](#10f-the-heavy-tier) |
| `HANGAR_HEAVY_MIRROR` and `HANGAR_HEAVY_MIRROR_UPSTREAM` (terraform output `dockerhub_mirror`), `HANGAR_HEAVY_REGISTRY_MIRRORS` | Optional (off) | [10f](#10f-the-heavy-tier) |
| `HANGAR_HEAVY_PERSON_MAX`, `HANGAR_HEAVY_WARM_POOLS`, `HANGAR_HEAVY_DOCKER_IMAGE` | Optional (3 `large`, 1 `xlarge` per person and for unattributed work together; no heavy warm pools; `docker:29.8.2-dind`) | [10f](#10f-the-heavy-tier) |
| `HANGAR_HEAVY_DOCKER_PRIVILEGED` | Optional, Docker provider only, for CI and throwaway machines (root on the host) | [10f](#10f-the-heavy-tier) |
| Build caches: `HANGAR_GATEWAY_URL` | **Required** for caches (sandboxes reach `/cache` on the gateway with its grant; without it nothing is cached) | [10g](#10g-build-caches) |
| A cache bucket of its own: `HANGAR_BUILD_CACHE_STORE=gs://…`, with `roles/storage.objectAdmin` on it for hangar's service account | Optional (the session bucket's `caches/` prefix, which hangar can already write) | [10g](#10g-build-caches) |
| `HANGAR_BUILD_CACHE_MAX_GB`, `_REPO_MAX_GB`, `_RETENTION_DAYS`, `_PRIVATE_DAYS`, `_PRIVATE_WRITES`, `_TRANSFER_MB`, `_WAIT_MINUTES` | Optional (100 GB in a bucket, 20 GB a repo, 14 and 3 days, private writes on, 256 MB, 10 min) | [10g](#10g-build-caches) |
| `HANGAR_TEST_REPORTS`; repo policy `verify_reports` and `rerun_flaky` | Optional (`inject`; reports at the usual paths; no reruns) | [10h](#10h-test-results) |
| `HANGAR_TRACE_LIVE_TOOLS`, `HANGAR_EXPERIMENT_MAX_TRIALS`, `HANGAR_EXPERIMENT_PARALLEL` | Optional (off, 200, 8) | [11d](#11d-the-sister-tools-interfaces) |
| Model list keys: `HANGAR_MODEL_LIST_CREDENTIALS` (`webhook` credentials holding Anthropic, OpenAI and Gemini API keys, kept from sandboxes) | Optional: without them, `balanced` and `frontier` resolve to hangar's built-in ids (OpenRouter's list needs no key). Env setup now lists models with these, not with the team's harness keys | [11a](#11a-model-list-keys) |
| Harness canary, on hangar's own repository: branch protection on `main` (required approval, `contract` as a required check) and *Allow auto-merge* | **Required** before the first bump PR can land (without required approvals auto-merge would need none) | [11b](#11b-the-harness-canary) |
| Harness canary: repository secrets `CANARY_APP_CLIENT_ID` and `CANARY_APP_PRIVATE_KEY` (a dedicated App, Contents and Pull requests: Read and write); variable `CANARY_APPROVERS` | Optional; the App is **required** for checks to run on the bump PR by themselves and for `images` to run on its merge | [11b](#11b-the-harness-canary) |
| Env setup: `HANGAR_ENV_SETUP_MODELS`, `HANGAR_ENV_SETUP_MAX_COST_USD` / `_MAX_TURNS`, `HANGAR_ENV_SETUP_LABEL` | Optional (defaults: `balanced`, $30 and 16 turns, `hangar-env-setup`) | [11c](#11c-agent-led-environment-setup) |
| The Cloud Doctor: a **team API key** for its harness (Claude Code unless `HANGAR_DOCTOR_HARNESS`) | Optional: without one the doctor only reports, never starts a job | [12a](#12a-the-cloud-doctor) |
| `HANGAR_DOCTOR_SPECS_REPO` (and `HANGAR_DOCTOR_SPECS_PATHS`): the repo holding environment specs as files | Optional: without it, fixes to environment specs are reported, not opened as PRs | [12a](#12a-the-cloud-doctor) |
| `HANGAR_DOCTOR_MODEL` | Optional (`balanced`); `frontier` for the strongest, or `best` for that on a team key that may lack Fable access | [12a](#12a-the-cloud-doctor) |
| `HANGAR_DOCTOR_MAX_USD`, `HANGAR_DOCTOR_JOB_MAX_USD`, `HANGAR_DOCTOR_SCHEDULE` | Optional ($10 a run, $3 a job, 00:30; `off` turns it off) | [12a](#12a-the-cloud-doctor) |
| A new vault key in Secret Manager, then `hangar admin rotate-key` | Optional (rotate on a schedule, when someone who could read it leaves, at once on a leak) | [12c](#12c-rotating-the-vault-key) |
| deploy.yml: the `production` environment (you as required reviewer; `main` and `v*` tags only), its secrets `GCP_DEPLOY_SERVICE_ACCOUNT`, `GKE_CLUSTER`, `GKE_LOCATION`, `HANGAR_URL`, `HANGAR_DEPLOY_TOKEN`, and the `hangar-deploy` service account and Role | Optional; **required** to deploy with deploy.yml | [13](#13-deploying-with-deployyml) |
| claude.ai users re-add the hangar connector | **Required** for claude.ai | [14](#14-clients-and-proof) |
| Acceptance fixtures: a test repo (App installed, *Allow auto-merge*, branch protection with code owners), an approver account's classic `repo` token, a fixture repo, a Jira project mapped to the test repo; deploy.yml's `HANGAR_ACCEPT_TEST_REPO`, `HANGAR_ACCEPT_FIXTURE_REPO` (variables), `HANGAR_ACCEPT_GITHUB_TOKEN`, `HANGAR_ACCEPT_APPROVER_TOKEN`, `HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET` (secrets) | Optional: each check skips, saying why, without its fixture; **required** for `secrets`, `landing`, `envbuild` and `intake` to prove anything | [14](#14-clients-and-proof) |

### What changes as soon as it starts

To opt out, use `hangar config set NAME VALUE` (kept in hangar's database) or `hangar-site` (wins over the database). Settings marked *restart* apply after `kubectl -n hangar rollout restart deploy/hangar`.

| What | Before → now | Opt out |
| --- | --- | --- |
| **The store** (#89) | every lookup scanned every sandbox record ever made → expression indexes on state and name, and stopped or failed sandbox records idle for 7 days move to a `sandboxes_archive` table (still found by id; deleted after 180 days; never while a snapshot or a credential lease points at one). Both happen on the first start, in the background, 100 rows at a time. | `HANGAR_SANDBOX_RETENTION_DAYS=0`, `HANGAR_SANDBOX_ARCHIVE_DAYS=0` |
| **Fleet size and caps** (#89, #90) | `HANGAR_MAX_SANDBOXES` 50 → **120**. Per person: 15 live sessions, 20 sandboxes started directly, half the job slots (at least 4) running jobs; a cap reached is a 429 or an MCP error. Caps count the person hangar records on the work, so all of a person's tokens share them ([team-scale.md](team-scale.md#settings)). | `HANGAR_PERSON_MAX_SESSIONS`, `_MAX_SANDBOXES`, `_MAX_RUNNING_JOBS` (0 = off) |
| **Job concurrency** (#89, #90) | 4 → `HANGAR_JOB_CONCURRENCY=0` (auto): 4 on Docker, **8 on GKE**. A free slot goes to whoever has the fewest jobs running. Step 8 raises it to 24 once budgets are set. | set a number (*restart*) |
| **Sandbox CPU** (#89) | each sandbox pod requested its whole CPU limit → half of it (`HANGAR_K8S_CPU_REQUEST_FRACTION=0.5`); the limit and all memory stay reserved. Each environment's warm-pool template is rewritten once, so the first create of each after the rollout is cold. The control plane requests `500m`/`1Gi` (limit `3Gi`). | `HANGAR_K8S_CPU_REQUEST_FRACTION=1` (*restart*) |
| **People** (#88) | every token was root over everyone's work → every token belongs to a person. Existing tokens, the admin token and the bootstrap token belong to **`owner`**, an admin, so nothing changes until you add someone (step 7). | — |
| **Your credentials** (#91) | an unowned credential went into every sandbox → **every credential stored before this release becomes the owner's own** on the first start, once (`hangar cred migration`; control-plane ones excepted). Jobs no person owns need a **team credential**, which must be an API key: hangar refuses a Claude Pro or Max login or a ChatGPT login as one (step 7). A git token (PAT) you stored for sandboxes is yours too until `hangar cred owner NAME team`. | — |
| **Who a front-door job belongs to** (#91, #92) | GitHub and Slack jobs ran for no one → a sender linked to a person (`hangar person link`) acts as that person: trusted, their caps, budget and login, and **only they continue their jobs** from a front door (others get a refusal; an admin uses `hangar job follow-up`). Unlinked senders' jobs run for the team as before. Nothing changes until you link someone. | — |
| **Control-plane credentials** (#81) | an `mcp` token could map the webhook secret or bot token into a sandbox → signing keys, GitHub App keys, Slack and Jira secrets and the GitHub tokens the settings name never reach a sandbox. A direct-mode environment that attaches the same GitHub token as `HANGAR_GITHUB_TOKEN_CREDENTIAL` or `HANGAR_GATEWAY_CREDENTIAL` now fails to start; `hangar doctor` names it (step 6). Saving an environment that sets `secrets`, `image`, `base`, `runtime_class`, `credential_mode: direct`, `egress`, `build_secrets` or `cloud` needs an admin. | `hangar cred exposure <name> sandbox` |
| **Front-door defaults** (#82) | jobs from GitHub issues, Slack, A2A and webhooks had no `verify` → they take the repo's [policy](repo-policy.md) (`hangar repo set`), then the environment's new `verify:` field. **`envs/hangar.yaml` (and `hangar-jobs`) now sets `verify: bash scripts/ci/verify.sh`**. A standing check also runs once per base commit before the agent starts; on a red base only new failures count. | `hangar repo set owner/repo --verify ""`, or `verify: ""` in the environment |
| **Agent scratch files** (#82) | delivery committed everything the agent left, including the `HANDOFF.md` the `repo-conventions` skill asked for → the skill no longer asks for it, and new root `HANDOFF.md`, `RESULT.md` and `NOTES.md` files and `*.orig`/`*.rej` leftovers beside their source stay out of PRs unless the task names them. Images with the skill rebuild once, since its layer changed. | name the file in the task |
| **Job budgets and daily caps** (#85) | none → `job_budget` caps all of a job's turns together, and a repo's `max_usd_per_day` or `HANGAR_PERSON_MAX_USD_PER_DAY` holds new jobs for approval. **All unset by default**, and a day starts at UTC midnight (20:00 in New York) until `HANGAR_BUDGET_DAY_TIMEZONE` is set (step 8). | — |
| **Secret scanning** (#98) | a job's branch was pushed as the agent left it → every new commit's patch, the uncommitted change and the commit messages are scanned first; a stored credential's value or a known token format stops the push and goes back to the agent as a fix turn (2 at most, then `secret_found`). PR text, comments and text artifacts are masked. | `HANGAR_SECRET_SCAN=warn` or `off` |
| **Redaction** (#98) | stored values were masked verbatim → also reversed, in hex, base64 and percent-encoded (values of 12+ characters). | — |
| **Egress** (#97) | sandboxes reached the public internet → **unchanged by default** (`HANGAR_EGRESS_DEFAULT=open`). An environment's `egress: {mode: allowlist}` sends its sandboxes through hangar's egress proxy (step 10a). The rollout adds port 3128 to the `hangar-gateway` Service and the proxied NetworkPolicies, which select nothing until an environment is proxied. | — |
| **Egress, closed further** (#107) | in proxied environments: a ClientHello with Encrypted ClientHello (GREASE too), several or no server names, or an SSL 2/3 hello is cut off; with the gateway configured, `pypi`, `npm` and `go` are read through hangar's read-only mirror (`/pkg/…` on the gateway) instead of the registries' own hosts; `packages` no longer opens GitHub, and `crates` no longer opens crates.io (the publish API); one person's sandboxes share 640 tunnels; a restart lets open tunnels finish for 20 s. Open environments see no change. | `HANGAR_PACKAGE_MIRROR=false` (the registries' hosts again); `presets: [packages, github]` |
| **Pushes through the gateway are scanned** (#107) | the agent's own `git push` went up unscanned → the gateway holds each push, scans it for secrets, and refuses one with a hit (file and line in git's output). Pushes over 64 MB are refused. | `HANGAR_SECRET_SCAN=warn` or `off`; `HANGAR_GATEWAY_SCAN_MAX_MB` |
| **Environments that live with the repo** (#93, #99) | a repo's own setup ran in every sandbox, every time → a repo's `.hangar/environment.yaml` with an `install` gets versioned builds in the background (first sandbox falls back, with a warning, and schedules one); `start`, `terminals` and `services` run with the sandbox. Repos without the file see no change. | `HANGAR_ENV_BUILDS=false` |
| **The agent's environment report** (#94) | → every harness's MCP config gets a `hangar-env` server (the environment doctor and requests), and job prompts tell the agent to file blockers with `hangar-agent env request` and keep going. Requests are never granted on their own: they wait in `hangar requests`, MCP `pending_approvals` and the inbox. | `HANGAR_ENV_DOCTOR_MCP=false` |
| **PRs and the inbox** (#96) | → each agent PR gets a review packet comment, and `hangar inbox` / `hangar://inbox` / `GET /api/inbox` lists what waits on each person. Agent PRs keep coming from the control token, and nothing lands by itself, until you set up the App and `land` (step 9b). | — |
| **Landing needs stale approvals dismissed** (#101) | `land: auto_after_approval` armed auto-merge whatever the base branch's protection → it arms only when the base branch's protection or a ruleset dismisses stale approvals on a push (or hangar can read neither: it doesn't arm), and `hangar doctor` fails until then. | `hangar repo set owner/repo --no-require-stale-dismissal` |
| **Auto-fix review rounds** (#101) | any collaborator's review started a round on any job, as an instruction → on a person's job only their linked account, an admin's, the policy's `approvers` or a code owner of every touched path; never a bot; and review text is an untrusted attachment ([team.md](team.md#who-is-trusted), rule 6). | `HANGAR_GITHUB_AUTOFIX` (`ci` only) |
| **`power.sh down`** (#84, #90, #95) | every node pool and hangar went to 0 at 01:00, so GitHub, Slack and Jira events were lost → it drains (`POST /api/admin/drain`), checkpoints running turns, then powers down **only the sandbox pools** (`^sandboxes-`: gVisor, Kata, microVM). Hangar and the `system` pool stay up: events are queued, jobs wait with `waiting: sandboxes power on at 10:00 EDT`, creates answer at once (REST `503` with `Retry-After`). A job turn the pools take away resumes from its checkpoint after power-up. | `POWER_POOLS` (a regex) for the script |
| **`power.sh up`** (#90, #95) | pools back at their ranges at 10:00, a cold first create (2–4.5 min) → at 09:50 the gVisor pool comes back at a warm floor of 1 node, every image and pack is pulled onto it (the `hangar-prepull` DaemonSet), then queued jobs start and warm pools are sized from last week's 10:00 hour. It keeps an autoscaling range a `terraform apply` set while the pools were down, and the microVM pool comes back at its fixed size. | `sandbox_warm_nodes = 0`; `HANGAR_POWER_WARM_POOLS=off` |
| **Cost** (#95) | the system node runs around the clock, and one Spot sandbox node all powered hours: about **+$22/month** and **up to +$163/month** ([team-scale.md](team-scale.md#cost)) | as above |
| **Interrupted jobs** (#84) | a job caught by a restart or the power-down failed as `interrupted` → it resumes from its session's checkpoint, at most twice, 20 s apart. | `HANGAR_JOB_RESTART_RESUMES=0` |
| **The reconciler** (#84) | rows a restart left `starting` or `stopping` stayed so, with their leases → a pass at startup and every `HANGAR_RECONCILE_INTERVAL_SECONDS` (30) finishes them; a GKE pod recreated blank under its claim counts as gone and its session resumes from its checkpoint. Sandboxes from before adopt their current pod's UID. It assumes one hangar process per database (`replicas: 1`, `strategy: Recreate`, as shipped). | — |
| **GitHub redelivery** (#84) | deliveries that failed while hangar was down were lost → at each start hangar asks GitHub to redeliver them (up to 3 days back). The stored GitHub token needs *Webhooks: Read and write*, as `hangar setup github` already asks. | `HANGAR_GITHUB_REDELIVER_ON_START=false` |
| **GitHub Projects redelivery** (#101) | missed `projects_v2_item` deliveries were lost → the startup pass also asks each `HANGAR_GITHUB_PROJECTS` organization's webhook to redeliver them. The control token needs the organization's *Webhooks: Read and write* (classic: `admin:org_hook`); without it hangar logs a warning. | `HANGAR_GITHUB_REDELIVER_ON_START=false` |
| **MCP waits** (#86) | `wait_for_turn` waited up to 300 s by default (600 at most), `exec` as long as its `timeout`, `process` `attach` up to 600 s, `terminal_send` up to its `wait_ms` → each at most `HANGAR_MCP_TOOL_WAIT_SECONDS` (150 s), so claude.ai (which gives up after about 230 s) gets an answer: `wait_for_turn` says `done: false`, an `exec` that runs into the cap points at `process`. Destroy, pause, resume, extend, snapshot, `fork_session` and `run_agent` answer within it too and finish in the background. Any other tool still waiting just past it (165 s) gets an error. | Raise `HANGAR_MCP_TOOL_WAIT_SECONDS` only if all your clients wait longer than claude.ai |
| **Tool-list notice** (#86) | `serverInfo.version` was `0.1.0` → `<version>+tools.<hash>`. A claude.ai connector whose tool list is older than the server's gets a one-line note (at most hourly) asking to remove and re-add the connector (step 14). | — |
| **Models** (#105, least privilege) | `envs/bare.yaml` ran Claude Code on the alias `opus`, pi and Antigravity on flash ids, Codex on its account default, and jobs on whatever the environment said → **`model: balanced`** for every harness of `bare` and of every environment that inherits its `model`, and jobs default to `balanced`, as do env setup's agents and the Cloud Doctor's jobs (the owner's decision: an earlier build of this release defaulted to `frontier`). It is resolved when a sandbox or session is created and pinned for it: Opus on Claude Code (`claude-opus-5-5`, $4/$20 per MTok), `gpt-6.1-sol` on Codex. `frontier` (Fable 5.1, $10/$50) is still there per environment, repo policy or job. Running sessions keep their model. | `model: frontier` or a pinned id in the environment, the repo policy or the job |
| **Session CLI versions** (#105) | → a session records the locked CLI version it was created under. A follow-up on a sandbox running another version hands off (a summary in a new conversation) instead of resuming; sessions from before this release adopt the version of their next sandbox. | — |
| **History out of memory** (#108) | every job and session ever made was loaded at startup and kept (~30 KB a finished job) → live work and the last `HANGAR_RESIDENT_MINUTES` (60) stay in memory; the rest is read back by id, name or index. The **first start indexes** the jobs and sessions tables (~2 s per 20,000 records on 2 vCPUs) before it answers. | `HANGAR_RESIDENT_MINUTES` (more memory, fewer reads) |
| **Listings page** (#108) | `list_jobs`, `list_sessions`, `GET /api/jobs` and `/api/sessions` sorted the team's newest 500, then filtered by person → pages from the indexes, filtered by person first; each item has a `cursor` for the next page (REST also sets `X-Next-Cursor`). | — |
| **Job and session archive** (#108) | → finished jobs and settled sessions untouched for 30 days move to `jobs_archive` and `sessions_archive`: still found, listed and in the usage report, never deleted. Kept out: open or approved PRs, merged or closed ones whose session hasn't ended, auto-fix triggers waiting, a schedule's last job, sessions with checkpoints, a session a job points at. `/metrics` counts them off the event loop. | `HANGAR_JOB_RETENTION_DAYS=0`, `HANGAR_SESSION_RETENTION_DAYS=0` |
| **The vault key ring** (#109) | one key → `HANGAR_SECRET_KEY` (or the key file) holds keys newest first; a single key is a ring of one, so nothing changes until you rotate (step 12c). The first start records which key sealed the store (a fingerprint, never the key). | — |
| **Repo builds' `.git`** (#110) | hooks and git config that `install` left went into every sandbox from the build → hooks (but the samples) and every config key but remotes, branches, user, core, sparse-checkout and git-lfs's own filters are removed before the checkout is captured; hangar's git commands run with hooks off. A repo that relied on `install` setting up hooks (husky's `prepare`) sets them up in `start`. | — |
| **Kubernetes templates per build and per set of services** (#110) | services' templates were never deleted; a build's was deleted under a session still running from it → the reaper deletes either, with its warm pool, once no live sandbox uses it (every 10 minutes at most, never in its first 10). The first sweep deletes the services' templates that piled up. | — |
| **Credential health** (#104) | → every agent credential is checked without a model (by age, format, `codex login status`, `gcloud`) at capture, nightly and from `hangar status`; one that fails or expires within 14 days shows in `hangar doctor`, `hangar status` and its renewer's inbox. It never rolls a deploy back (`deploy_gate: false`). | — |
| **Flight records and the nightly pass** (#104, #106) | → a record for each failed, timed-out or slow job and turn (its log end masked and sealed, kept 14 days); at 00:30 the nightly pass checks credentials and runs the Cloud Doctor, which **only reports until it has a team key** (step 12a). | `HANGAR_DOCTOR_SCHEDULE=off` |
| **Model spend** (#105) | → `hangar doctor` warns (`model spend`) and `hangar status`'s `models` line says `NO job budget or daily cap` while neither `HANGAR_JOB_BUDGET_MAX_COST_USD` nor `HANGAR_PERSON_MAX_USD_PER_DAY` is set, whatever the default model (`balanced`; `frontier` is a job away) (step 8). | set a budget |
| **Jobs only fetch; hangar pushes** (least privilege) | a job's sandbox could push `hangar/**` branches itself through the gateway (the Cloud Doctor's root-cause job included), and a `credential_mode: direct` environment put its git token in a job's sandbox → with `HANGAR_GATEWAY_URL` set, **every job's sandbox gets a fetch-only grant** whatever its environment's `credential_mode` or `HANGAR_GATEWAY_FOR_JOBS` (which now governs sessions only); a git credential its `secrets:` map is left out, with a warning. Hangar pushes the commit it checked with a grant pinned to that branch and commit, without the sandbox's git hooks (a husky `pre-push` no longer runs on delivery: put the check in `verify`). Sessions keep their branch-scoped grant. A job's agent that runs `git push` gets `hangar: this sandbox may fetch but not push`. Job sandboxes alive across the upgrade (kept for follow-ups) are narrowed to fetch-only at the first start (`gateway.narrow` in the audit log); a job with no repo, or on a host the gateway doesn't serve, gets no git credential at all. The gateway's upstream credential now matters for jobs on direct-mode environments: their `git_token`, else `HANGAR_GATEWAY_CREDENTIAL` or the App | — (fork the job's session to push by hand) |
| **Builds and validation hold nothing** (least privilege) | a repo build's sandbox got the environment's non-agent credentials, `secrets:` and cloud tokens while it ran `install` → it gets a fetch-only grant for its clone, its mapped build secrets (in `install`'s environment only) and egress per policy, nothing else; env setup's validation the same, without build secrets. **Without the gateway** neither gets a git credential, so a private repo's build and validation fail with a message naming `HANGAR_GATEWAY_URL`; set it (Docker on a laptop: `http://host.docker.internal:8788`, [security.md](security.md#turning-it-on)) | — |
| **Env setup** (#102) | → `hangar env setup <repo>`, the `hangar.kind: env-setup` job label and the `hangar-env-setup` issue label start an agent-led setup that opens a PR with a validated `.hangar/environment.yaml`. Its PR opens only if the branch is still at the commit hangar validated; `.hangar/**` is **high risk** in every PR's packet. | don't use the label |
| **Build caches** (#115) | every sandbox installed dependencies and compiled from nothing → **on by default** wherever the gateway is configured: after the clone, `hangar-cache` restores the repo's dependency caches in the background (the sandbox is ready at once), and sandboxes get the variables for sccache, Bazel's HTTP cache and Turborepo's remote cache on the gateway's `/cache`. Only repo builds write the shared layer; sessions and jobs write a private layer of their own. Objects go to the session bucket's `caches/` prefix (up to 100 GB, LRU) unless step 10g names another. | `HANGAR_BUILD_CACHES=false` (*restart*); `HANGAR_BUILD_CACHE_PRIVATE_WRITES=false` |
| **Test reports** (#116) | a check was its exit code and the end of its output → every check hangar runs sets the reporter variables (`PYTEST_ADDOPTS` gains `--junitxml=$HANGAR_TESTS_DIR/pytest.xml`, `GOTESTSUM_JUNITFILE`, `JEST_JUNIT_OUTPUT_DIR`) and reads the JUnit, Cobertura and LCOV files it wrote. Nothing is injected where the command or the repo's pytest config already names `junitxml`, and the check's own output and exit code are unchanged. A red base is then subtracted by test id, and the repo's flaky list starts filling. | `HANGAR_TEST_REPORTS=collect` (read only) or `off` |
| **Turn manifests** (#114) | → each turn records the configuration it ran with (one command in the sandbox as it starts), stored under the data dir's new `manifests/` (and `experiments/` for experiment jobs). Nothing to migrate. | — |
| **Warm pools refill on create** (Wave 4 review of #114) | since repo builds (#93) no create refilled its environment's warm pool, so a claimed member was replaced only at the next image build or scheduled sync → a create that started from the environment's own image refills it (never from a repo build's image or for an experiment trial), so warm pools stay at their size through the day, as they did before #93. | the environment's `warm_pool: 0`, `HANGAR_POWER_WARM_POOLS=off` |
| **Sandbox admission** (#117) | → `kubectl apply -k deploy/k8s` also applies `heavy-policy.yaml`: in `hangar-sandboxes`, no pod gets host namespaces, `hostPath` or `hostPort`, and a privileged container or added capability needs a Kata runtime class (step 10f). Hangar's own pods use none of these. | — |
| **Harness versions and packs** (#83, #87) | Claude Code and Antigravity followed "latest" → `src/hangar/harnesses.lock.yaml` pins every harness (Claude Code 2.1.296, Codex 0.162.1, Antigravity 1.3.3, pi 1.1.0, Gemini 0.64.0); a pack build fails unless the CLI reports the locked version. Packs hold only the vendor's files and are mounted by digest. **Every pack tag changes once**, and so does every image that bakes a harness in (step 3). `hangar pack publish` no longer overwrites an existing tag without `--force`. | — |

### 1. Back up

```bash
kubectl -n hangar get deploy hangar -o jsonpath='{.spec.template.spec.containers[0].image}' | tee ~/.hangar/pre-upgrade-image
hangar admin backup && hangar admin backups      # encrypted with the vault key, into the session store
```

**The vault key in Secret Manager (required on GKE, before step 5).** Every credential, checkpoint and backup is encrypted with it, a backup can't be restored without it, and a rotation (step 12c) works from there. If it lives only in the `hangar` Secret (what `up.sh` does), copy it once, piped, never to a file on your machine ([security.md](security.md#where-the-keys-live)):

```bash
kubectl -n hangar get secret hangar -o jsonpath='{.data.HANGAR_SECRET_KEY}' | base64 -d | \
  gcloud secrets create hangar-vault-key --data-file=- --replication-policy=automatic
```

Give `roles/secretmanager.secretAccessor` on it only to the operators who rotate keys. `deploy/gke/setup-cicd.sh` does this copy when the secret doesn't exist yet ([cicd.md](cicd.md)).

**Backups apart from the checkpoints (optional).** Backups go to `_backups/` in the checkpoints' bucket, under hangar's own service account, and `hangar doctor` warns about that (`backup isolation`). To fix it, create a bucket of its own where hangar's service account has only `roles/storage.objectCreator` (it writes backups, can't read or delete them), add a lifecycle rule that deletes them after the time you keep them, and `hangar config set HANGAR_BACKUP_STORE gs://<bucket>` (restart). Restores are then an operator's (`hangar admin restore --local`).

The previous version runs on the upgraded database file (see [rolling back](#rolling-back-this-release)), so the backup is for a disaster, not for the rollback.

### 2. Request quotas, days ahead

The sandbox pool moves to `n2d-standard-16` (AMD keeps SMT on under GKE Sandbox, so 14 sandboxes fit a node) with up to 10 nodes ([team-scale.md](team-scale.md#how-many-nodes)). In *IAM & Admin → Quotas* for your region request `N2D_CPUS` ≥ 160, `CPUS_ALL_REGIONS` to match, and `SSD_TOTAL_GB` ≥ 2,250. Spot VMs take standard `N2D_CPUS` quota unless the region granted preemptible quota. A refused scale-up shows as sandboxes stuck "waiting for a node", and `hangar doctor` names the quota.

**The heavy pool (optional; required for the heavy tier on GKE, step 10f).** Its `n2-standard-32` nodes take `N2_CPUS`: request 128 for the default `heavy_max_nodes = 4`, on top of what the Kata pool uses, and 2,000 GB more `SSD_TOTAL_GB` for their 500 GB disks ([team-scale.md](team-scale.md#how-many-nodes-and-the-caps)).

### 3. Prebuild the new image and pack tags

Before the rollout, so no create waits on a Cloud Build. This talks only to Artifact Registry, so it works while the cluster is powered down:

```bash
REG=$(terraform -chdir=deploy/terraform/gke output -raw registry)
uv run hangar images plan --registry "$REG"    # every tag the commit needs; exit 3 = something to build
uv run hangar images build --registry "$REG"   # Docker here (linux/amd64), else Cloud Build
uv run hangar images plan --registry "$REG"    # exit 0: nothing missing
```

If the cluster sets `HANGAR_IMAGE_PREFIX`, `HANGAR_SANDBOX_USER`, `HANGAR_WORKSPACE_DIR`, `HANGAR_EXTRA_CA_BUNDLE` or `HANGAR_K8S_IMAGE_VOLUMES` in `hangar-site` or its database, export the same values first: they change the tags. With the images workflow's secrets set ([gke.md](gke.md#images-and-packs-built-in-ci); *optional*), the push to `main` does this for you and the run's summary lists each tag. `up.sh` runs the same build before it touches the cluster (`PREBUILD=0` skips it). If nothing prebuilt them, nothing breaks: running sandboxes keep the pack they mounted, the new control plane rebuilds the packs in the background, and the first sandbox for a harness whose pack isn't built yet waits for it. Environments you give `computer:` later (step 10e) build a new image layer then; repo environment builds (step 10c) build in the background, never on a create.

### 4. In the power-down window: the node pool and the power schedule

After `power.sh down` has finished (it runs at 01:00 ET; the cluster is down until 10:00), so no sandbox is on the pool:

```bash
cd deploy/terraform/gke
terraform plan    # expect "~ update in-place" on google_container_node_pool.sandboxes, the new cron and IAM
terraform apply
```

One apply does both halves of the release:

- **The node pool (required).** `machine_type` is an in-place node pool update with the google provider ≥ 5.x (`versions.tf` asks for ≥ 6.0): GKE replaces the nodes, not the pool, and with the pool at zero nodes that is quick. **If the plan shows the pool destroyed and created (`-/+`), stop**: something else in it changed (`sandbox_spot` forces a new pool). To keep the old pool, set `sandbox_machine_type` and `sandbox_max_nodes` in your tfvars. Done outside the window, every sandbox on the pool is killed, and GKE needs a node's worth of spare quota to surge.
- **The power schedule (required with `enable_power_schedule = true`).** `power_up_cron` `0 10 * * *` → `50 9 * * *` with `power_warm_minutes = 10`; the gVisor pool's minimum becomes `sandbox_warm_nodes` (1 with the power schedule, else 0); hangar's service account gets `roles/run.invoker` on the power-up job; output `power_up_job`. The apply turns the gVisor pool's autoscaling back on at its floor, which starts a node: that's fine here, since step 5 brings the cluster up.
- **The heavy pool (optional; required for the heavy tier on GKE).** `enable_heavy_pool = true` in `terraform.tfvars` (and `heavy_machine_type`, `heavy_max_nodes`, `heavy_spot`, `heavy_disk_gb` if the defaults don't fit) adds `sandboxes-heavy`: Ubuntu, nested virtualization, scaling from zero, tainted `hangar.dev/runtime=heavy`, under the power schedule like the other `sandboxes-*` pools, its nodes running as a service account of their own (`hangar-heavy-nodes`) with only a node's roles and read access to the `hangar` repository. The plan shows only additions. [gke.md §4a](gke.md#4a-the-heavy-tier-optional).
- **The Docker Hub remote repository (optional).** `enable_dockerhub_mirror = true` creates an Artifact Registry remote repository for Docker Hub, read by hangar's service account, so heavy sandboxes' pulls aren't rate-limited (output `dockerhub_mirror`, used in step 10f). With `dockerhub_username` and `dockerhub_token_secret` (a Secret Manager secret holding an access token) it pulls as that account: use one with **no private repositories**, since every heavy sandbox can pull whatever the account can, through the mirror. Without them it pulls anonymously.

### 5. Bring the cluster up and roll out

```bash
bash deploy/gke/power.sh up     # the new script: brings back the system pool and hangar an old down stopped too,
                                # and keeps the 0-10 range the apply set, instead of the 0-4 saved at down
bash deploy/gke/up.sh           # prebuild (finds nothing missing), deploy/k8s (incl. the egress port 3128 and its
                                # NetworkPolicies), the rollout; sets HANGAR_POWER_UP_JOB from terraform
```

Do this before the team starts, or let the 10:00 schedule run `power.sh up` and deploy right after. The Deployment uses `Recreate`, so the URL answers 502 until the new pod is ready. On its first start hangar makes the earlier credentials the owner's, creates the store's indexes and archive tables (20,000 sandbox records index in well under a second; the jobs and sessions tables take ~2 s per 20,000 before hangar answers), records which vault key sealed the store, runs a reconciler pass before sessions and jobs look at their sandboxes, resumes jobs the power-down interrupted, and asks GitHub to redeliver what it missed overnight. From the next 01:00, `power.sh down` leaves hangar up.

### 6. Check

```bash
hangar doctor
hangar status    # the console: power, loops, versions, builds, failures, requests, SLOs, credentials, models
hangar power     # "sandboxes: on"; the last power-up, the last deploy, the loops' heartbeats
```

The doctor names environments that attach the control plane's own GitHub token in direct mode (fix with `hangar cred exposure <name> sandbox`, or switch them to the gateway), mapped repos whose jobs would run with no check (`hangar repo set <repo> --verify "…"`), the webhook's events (step 9a) and quota errors. New in this release: **personal credentials shared** (fails on a subscription login stored as the team's), **team credentials for jobs**, **unlinked senders**, the Jira configuration, landing repos' branch protection (**fails** without stale-approval dismissal), **sandbox power**, **last deploy**, **background loops**, **vault key** (a pending rotation, a retired key still configured), **backup isolation**, **credential health** and **model spend** (warns until step 8 sets a budget).

### 7. People: your logins stay yours; add team credentials, then the team

Every existing token now belongs to `owner` (you, an admin), and records from before have no person, so only admins see them. **Every credential stored before this release became yours on the first start**, once, so your Claude Max or ChatGPT login never goes into a member's sandbox. Control-plane credentials (signing keys, bot tokens, hangar's own GitHub token) stayed the control plane's. That also means jobs no person owns (GitHub, Slack, Jira and Projects senders you haven't linked, webhooks, schedules) no longer get your login: give them a team credential (**required** if any such work runs), and link yourself so your own issues run as you:

```bash
hangar cred migration                             # what became yours
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt   # an Anthropic Console key
hangar cred add codex-team --team --harness codex --kind api_key --file openai-key.txt        # an OpenAI project key
hangar cred owner gh-jobs team                    # an API key or git token of yours the team may use
hangar person link owner github <your-login>      # your GitHub issues and comments run as you, with your login
hangar doctor                                     # "team credentials for jobs" names what still has none
```

**A team credential is an API key** (or another credential the vendor's terms allow for automation). Hangar refuses a Claude Pro or Max login or a ChatGPT login as the team's, whether added with `--team` or moved with `hangar cred owner NAME team`: the vendors' terms make a login one person's, and Claude Code's forbid routing requests through a Pro or Max login on behalf of others. The full guide, including what the vendors allow and Codex logins, is [team.md](team.md).

Then add each engineer (optional, and the point of the release):

```bash
hangar person add alice --name "Alice"            # prints her login token once, and lists the team credentials she will get
hangar person link alice github alice-gh          # stored as her numeric GitHub id; also: slack T…/U…, jira <account id>, email
```

She runs `hangar login <url>` with it, `hangar connect` for her MCP clients, and `hangar cred add` for her own Claude or Codex login, which then goes only into her sandboxes. Members see and act on their own sandboxes, sessions and jobs; admins on everyone's ([security.md](security.md#people)). The per-person caps count her as one person across all her tokens. Only admins link accounts, and a linked account acts as its person ([team.md](team.md#who-is-trusted)): link one you've seen the person use.

### 8. Budgets, then job concurrency

Job slots stay at 8 on GKE because nothing bounds what a job spends until budgets are set. Set them (optional, but **required** before raising the slots), then raise the slots to the measured 24:

```bash
hangar config set HANGAR_BUDGET_DAY_TIMEZONE America/New_York     # a day starts at local midnight
hangar config set HANGAR_JOB_BUDGET_MAX_COST_USD 10                # every job's turns together (an example)
hangar config set HANGAR_PERSON_MAX_USD_PER_DAY 150                # per person, all their tokens (an example)
hangar repo set acme/app --max-usd-per-day 60 --verify "uv run pytest -q"
hangar config set HANGAR_JOB_CONCURRENCY 24                        # restart
kubectl -n hangar rollout restart deploy/hangar
```

A slot is a job, not a sandbox: 24 best-of-4 jobs could hold all 120 sandboxes, so keep `HANGAR_PERSON_MAX_RUNNING_JOBS` (half the slots) and the budgets in place ([team-scale.md](team-scale.md#limits-and-follow-ups)).

**Jobs default to `balanced`** (Opus 5.5 on Claude Code, $4/$20 per MTok; [harnesses.md](harnesses.md#models-the-latest-strong-model-per-provider)), and **hangar sets no spend cap of its own**: the amounts above are examples, and choosing them is yours. Until `HANGAR_JOB_BUDGET_MAX_COST_USD` or `HANGAR_PERSON_MAX_USD_PER_DAY` is set, `hangar doctor` warns (`model spend`) and `hangar status` shows `NO job budget or daily cap` on its `models:` line. A repo policy's `job_budget` or `--max-usd-per-day` caps only that repo. Where the last bit of capability matters more than the price, use `model: frontier` (Fable 5.1, $10/$50) in the environment, the repo policy or the job.

### 9. Front doors

#### 9a. The GitHub webhook

**Required.** Landing needs the `pull_request` event on each mapped repo's webhook, which hooks set up before don't have:

```bash
hangar doctor --fix      # adds pull_request to every mapped repo's hook
```

#### 9b. The GitHub App and landing

**Optional**; required for an approval to land the PR. Until then agent PRs come from the control token and are merged by hand. [landing.md](landing.md) has the details.

1. Create a GitHub App under the organization with exactly **Contents: Read and write**, **Pull requests: Read and write** and **Metadata: Read**, its webhook off; generate a private key; install it on only the repos agents work on. Don't put it on any bypass list.
2. Store it and point hangar at it:
   ```bash
   jq -n --arg id 123456 --rawfile key ~/Downloads/hangar-agents.*.private-key.pem \
     '{app_id: $id, private_key: $key}' | hangar cred add github-app --harness git --kind github_app
   hangar config set HANGAR_GITHUB_APP_CREDENTIAL github-app
   ```
3. On each repo that should land (**required** for it), under Settings → Branches or Rules: require a pull request with at least 1 approval, dismiss stale approvals on new commits, require approval of the most recent push, require status checks; and under Settings → General, **Allow auto-merge**.
4. Turn it on per repo: `hangar repo set acme/app --land auto_after_approval --approver alice --risk-path 'infra/**'`. Then `hangar doctor` checks the protection for every such repo.

#### 9c. Jira

**Optional.** [intake.md](intake.md#jira-cloud) has the details, and Data Center's differences.

```bash
hangar cred add jira-api --harness webhook --kind generic        # the hangar account's API token (Ctrl-D)
hangar cred add jira-webhook --harness webhook --kind generic    # the webhook's secret
hangar config set jira '{"url": "https://acme.atlassian.net", "email": "hangar@acme.com",
  "token_credential": "jira-api", "webhook_secret_credential": "jira-webhook",
  "projects": {"ABC": {"repo": "acme/api"}}, "trigger": {"status": "Ready for agent"},
  "statuses": {"started": "In Progress", "needs_input": "Blocked", "delivered": "In Review"}}'
```

In Jira, *Settings → System → WebHooks*: URL `https://<your hangar>/api/hooks/jira`, the same secret, events Issue created and updated and Comment created, with JQL `project in (ABC)`. Prefer this signed webhook to an Automation rule. Mapped repos must be in `HANGAR_GITHUB_REPOS`. Then link your teammates' Jira accounts (`hangar person link alice jira <account id>`): an unlinked Jira sender's job always waits for a linked teammate's `/hangar approve`.

#### 9d. GitHub Projects

**Optional.** [intake.md](intake.md#github-projects) has the details.

1. An **organization** webhook (repository webhooks never get project events): payload URL `https://<your hangar>/api/hooks/github`, `application/json`, the same secret as the repository webhooks, only the **Projects v2 items** event.
2. The control token (`HANGAR_GITHUB_TOKEN_CREDENTIAL`) needs the organization's projects: the `project` scope on a classic token, or **Projects: Read and write** on a fine-grained one. For hangar to redeliver the board moves it missed while down, it also needs the organization's **Webhooks: Read and write** (classic: `admin:org_hook`).
3. The boards:
   ```bash
   hangar config set github_projects '{"acme/7": {"trigger": "Ready for agent", "repos": ["acme/api"],
     "statuses": {"started": "In progress", "needs_input": "Blocked", "delivered": "In review"}}}'
   ```

A board mover is trusted when linked to a person or listed in `HANGAR_GITHUB_TRUSTED`; anyone else's job waits for approval on the issue.

### 10. Sandboxes

#### 10a. Egress allowlists

Egress stays `open` until you say otherwise. To proxy an environment's sandboxes ([security.md](security.md#egress-allowlists)):

- **GKE: `HANGAR_EGRESS_URL` (required** before any environment uses `allowlist` or `none`; without it such sandboxes fail to start, saying so). Step 5 already applied the Service port and the NetworkPolicies:
  ```bash
  hangar config set HANGAR_EGRESS_URL http://hangar-gateway.hangar.svc:3128   # or in hangar-site; restart
  kubectl -n hangar rollout restart deploy/hangar
  ```
- **Docker on a Linux host (required** for proxied environments there): Docker Engine 26.0.0 or later (or 25.0.4, 23.0.11), since older engines forward DNS out of internal networks (CVE-2024-29018) and hangar refuses them; a host firewall that drops input must accept TCP 3128 and 8788 from `hangar1`. E2B and Docker Desktop can't enforce egress, so proxied environments don't start there.
- **Per environment (admin):** `egress: {mode: allowlist, presets: [packages], allow: [docs.python.org]}`. A repo policy can add hosts and tighten the mode (`hangar repo set acme/app --egress-mode none`).
- **`HANGAR_EGRESS_DEFAULT` (optional):** `allowlist` makes every environment that doesn't set a mode proxied. Turn it on only after the URL above works, and after checking what each environment's agents need.
- **Package mirrors (#107).** With `HANGAR_GATEWAY_URL` set, proxied sandboxes read PyPI, npm and Go through `/pkg/…` on the gateway ([security.md](security.md#package-mirrors)); nothing to do on Docker, where hangar caches files on the data volume (`HANGAR_PACKAGE_MIRROR_CACHE_MB`, 1024). On GKE, optionally put Artifact Registry remote repositories behind it, so the cluster pulls from Google's cache:
  ```bash
  gcloud artifacts repositories create pypi-remote --location=us --repository-format=python \
    --mode=remote-repository --remote-python-repo=pypi --remote-repo-config-desc="PyPI"
  gcloud artifacts repositories create npm-remote --location=us --repository-format=npm \
    --mode=remote-repository --remote-npm-repo=npmjs --remote-repo-config-desc="npm"
  for r in pypi-remote npm-remote; do
    gcloud artifacts repositories add-iam-policy-binding $r --location=us \
      --member="serviceAccount:$HANGAR_SA" --role=roles/artifactregistry.reader
  done
  hangar config set HANGAR_PACKAGE_MIRRORS \
    '{"pypi": "https://us-python.pkg.dev/PROJECT/pypi-remote", "npm": "https://us-npm.pkg.dev/PROJECT/npm-remote"}'
  ```
  Keep Go on the public proxy unless an AR Go repository's fetches from origins are acceptable (security.md explains). Proxied environments in `credential_mode: direct` that clone from GitHub need `presets: [packages, github]` now. For the read-only GitHub API through the gateway, give the GitHub App *Actions*, *Checks* and *Issues*: Read (without them it falls back to the push token, reads only).
- **Browsers (#107):** rebuild computer environments' images (`hangar env build NAME`): Chromium sends GREASE ECH, which the proxy refuses, and the new image turns ECH off by policy.

Agents ask for a missing host with `hangar-agent env request egress HOST`, which waits for you in `hangar requests` and the inbox.

#### 10b. Short-lived cloud credentials

**Optional**; required for environments with `cloud:` ([security.md](security.md#short-lived-cloud-credentials)). Set `HANGAR_OIDC_ISSUER` if `HANGAR_PUBLIC_URL` isn't the https URL to use (AWS fetches `<issuer>/.well-known/openid-configuration` itself, so for AWS it must be public), then trust it in the cloud: a GCP workload identity pool with the JWKS from `/oidc/jwks.json` uploaded, granting `principal://…/subject/hangar:env:<env>`; or an AWS role whose trust policy checks `:sub`. Re-upload the JWKS after each `POST /api/cloud/rotate`. `lifetime_minutes` is now at most 60 (tokens are renewed while the sandbox runs); lower any environment that sets more. Where the cloud fetches the JWKS itself, set `HANGAR_OIDC_KEY_ROTATION_DAYS` (90, say) to rotate the signing key on a schedule; where it is uploaded, upload it again after each `cloud.key.next` audit entry.

#### 10c. Environments that live with the repo

Nothing to set: a repo opts in by committing `.hangar/environment.yaml` ([environments.md](environments.md)).

- **Build secrets (required** for a repo whose file names `build_secrets`; its builds fail, naming the id, until mapped): `build_secrets: {NPM_TOKEN: npm-ci-token}` on the environment or the repo policy (admin), and preferably `hangar cred exposure npm-ci-token control_plane`.
- **Service images (optional):** `HANGAR_ENV_SERVICE_IMAGES` lists what `services:` may run (Docker's official images by default); `HANGAR_ENV_SERVICES_MAX_CPUS` and `_MAX_MEMORY_GB` (2 and 2) bound them per sandbox.

#### 10d. Preview ports

**Optional** (off until set). A registrable domain of its own, not a subdomain of your company's or hangar's, with wildcard DNS pointing at hangar, and on GKE a wildcard certificate for `*.<domain>` on hangar's ingress and `deploy/k8s/e2b.yaml`'s `allow-hangar-sandbox-ports` NetworkPolicy:

```bash
hangar config set HANGAR_PREVIEW_DOMAIN hangar-preview.example.net    # restart
```

#### 10e. The agent's computer

Nothing to set: an environment opts in with `computer: true` (its image gets a layer). **Optional:** `HANGAR_PR_MEDIA_LINKS=signed` embeds screenshots and demos in PRs through expiring links anyone holding them can open; leave it `off` for private repos.

#### 10f. The heavy tier

**Optional** (off until a `heavy` tier is configured; [environments.md](environments.md#the-heavy-tier)). Docker inside a VM of the sandbox's own, and the `large` and `xlarge` profiles.

- **The admission policy (applied by step 5, whether you use the tier or not).** `kubectl apply -k deploy/k8s` now also applies `heavy-policy.yaml`: ValidatingAdmissionPolicies (Kubernetes 1.30 or later) that refuse, in `hangar-sandboxes`, privileged containers and added capabilities unless the pod runs under a Kata runtime class, and host namespaces, `hostPath` volumes and `hostPort` under any runtime; and, cluster-wide, a RuntimeClass named `kata*` whose handler isn't `kata*`. Hangar's own sandbox pods use none of these (heavy ones are privileged under Kata), so nothing changes for them. If anything else runs pods in `hangar-sandboxes`, check them first with `kubectl apply --dry-run=server`.
- **The tier (required for it):** the heavy pool from step 4, then Kata on it and the tier in `hangar-site` ([gke.md §4a](gke.md#4a-the-heavy-tier-optional)), or E2B or Cube microVMs ([microvm.md](microvm.md#the-heavy-tier-on-microvms)):
  ```bash
  KATA=1 bash deploy/gke/up.sh     # kata-deploy on the Kata and heavy pools
  hangar config set HANGAR_TIERS '{"isolated": ["kubernetes"], "heavy": ["kubernetes"]}'   # keep your other tiers; restart
  ```
  `HANGAR_HEAVY_RUNTIME_CLASS` (`kata-qemu-runtime-rs`) and `HANGAR_HEAVY_NODE_SELECTOR` (`true`) fit that setup. **On a cluster with GKE's Agent Sandbox add-on (required there):** its policy refuses privileged Kata pods, so run the open-source Agent Sandbox controller instead of the add-on, or put the tier on microVMs (`HANGAR_TIERS='{"heavy": ["e2b"]}'`); [gke.md §4a](gke.md#4a-the-heavy-tier-optional) has the trade-offs.
- **Which repos get Docker (required per repo that asks): deny by default.** A heavy environment (an admin's `heavy: true` or `tier: heavy`) has it. A repo's own `.hangar/environment.yaml` asking for it (`heavy: true`) is only honored where you allow it, and otherwise runs on the default tier with the agent told why and how you enable it:
  ```bash
  hangar repo set acme/app --heavy allow       # the repo's file may ask for Docker; `on`: all its sandboxes have it
  hangar repo set acme/app --max-profile large # what a job or the repo's file may ask for
  ```
- **Docker Hub through hangar (optional).** With the remote repository from step 4: `HANGAR_HEAVY_MIRROR=true` and `HANGAR_HEAVY_MIRROR_UPSTREAM` set to terraform's `dockerhub_mirror` output, so heavy sandboxes pull through hangar's read-only mirror on the gateway. `HANGAR_HEAVY_REGISTRY_MIRRORS='["https://mirror.gcr.io"]'` adds others.
- **Caps and cost (optional).** `HANGAR_HEAVY_PERSON_MAX` (3 `large` or larger and 1 `xlarge` per person; work no person owns shares one bucket with the same caps), `HANGAR_HEAVY_WARM_POOLS` (off: a warm heavy node costs; [team-scale.md](team-scale.md#heavy-tier)).
- **Check it:** `bash scripts/acceptance/gke.sh --only heavy` (a Compose Postgres and a Testcontainers test, and no metadata server reachable from inside the VM).

#### 10g. Build caches

On by default; **`HANGAR_GATEWAY_URL` is required** for them (sandboxes reach `/cache` on the gateway's port with its grant), which every gateway-mode install already sets. Nothing else to do on GKE: objects go to the session bucket's `caches/` prefix, which hangar's service account can already write ([caches.md](caches.md)).

- **A bucket of its own (optional):** create one, give hangar's service account `roles/storage.objectAdmin` on it (it writes, reads and evicts), then `hangar config set HANGAR_BUILD_CACHE_STORE gs://<bucket>` and restart. Objects are sealed with the vault key, as checkpoints are.
- **Sizes (optional):** `HANGAR_BUILD_CACHE_MAX_GB` (100 in a bucket, 10 on Docker's data volume, which holds the database too: size it), `HANGAR_BUILD_CACHE_REPO_MAX_GB` (20), `HANGAR_BUILD_CACHE_RETENTION_DAYS` (14). `hangar status` shows the sizes; `hangar admin caches purge` empties a repo's.
- **Off:** `HANGAR_BUILD_CACHES=false` (restart).

#### 10h. Test results

**Optional**, nothing to set: every check now leaves structured results ([jobs.md §8](jobs.md#8-test-results)). Where a runner writes its report somewhere hangar doesn't look, name it in the repo's file or policy (`hangar repo set acme/app --verify-report 'build/**/TEST-*.xml'`), and `--rerun-flaky` lets a check that failed only on the repo's known-flaky tests run once more. `HANGAR_TEST_REPORTS=collect` stops hangar setting the reporter variables; `off` stops it reading reports too.

### 11. Models, the canary and environment setup

#### 11a. Model list keys

**Optional.** `frontier` and `balanced` resolve from each provider's live model list. Without a key for a provider hangar uses the last id it resolved, else a built-in one (`balanced`: `claude-opus-5-5`, `gpt-6.1-sol`; `frontier`: `claude-fable-5-1`, `gpt-6-astra`), and the session says so. To list with your accounts (the keys are control-plane credentials: they never reach a sandbox):

```bash
hangar cred add anthropic-models --harness webhook --kind generic   # an Anthropic API key (Ctrl-D)
hangar cred add openai-models --harness webhook --kind generic
hangar config set HANGAR_MODEL_LIST_CREDENTIALS '{"anthropic": "anthropic-models", "openai": "openai-models"}'
hangar env list                                                      # each harness's model, its policy and list price
```

Env setup (`hangar env setup`) uses the same lists; it used to list with the team's harness API keys. [harnesses.md](harnesses.md#models-the-latest-strong-model-per-provider) has how each provider's strongest model is picked.

#### 11b. The harness canary

On **hangar's own repository**, not the repos agents work on ([harnesses.md](harnesses.md#setup)). The contract tests (`harness-contract.yml`) run on their own; the nightly canary (`harness-canary.yml`, 03:17 ET) opens a lock-bump PR that lands on one approval (`harness-canary-land.yml`).

1. **Branch protection on `main` (required** before the first bump lands): a required approval, dismiss stale approvals, and **`contract` as a required check**; and under Settings → General, **Allow auto-merge**. Without required approvals, auto-merge would merge a bump nobody approved.
2. **A dedicated GitHub App (optional, recommended):** Contents and Pull requests: Read and write, no Workflows permission, installed on this repository only; not hangar's own App. Store it as repository secrets `CANARY_APP_CLIENT_ID` and `CANARY_APP_PRIVATE_KEY`. Without it the bump PR comes from the workflow token, so no checks run on it (close and reopen it) and `images` doesn't run on its merge (run it by hand).
3. **`CANARY_APPROVERS` (optional):** a repository variable with the logins that may approve a bump; the repository's owner otherwise.

#### 11c. Agent-led environment setup

**Optional**, nothing to set. `hangar env setup acme/app [--wait | --drift | --every CRON]` (or `POST /api/env/setup`, or an issue labelled `hangar-env-setup` on GitHub, Jira or GitHub Projects) runs a goal agent and a setup agent on `balanced`, validates the `.hangar/environment.yaml` they write in a clean sandbox with no secrets, and opens a PR through the landing path ([environments.md](environments.md#agent-led-setup)). Its budget is the job's, else the repo policy's, else `HANGAR_ENV_SETUP_MAX_COST_USD` ($30) and `_MAX_TURNS` (16); `HANGAR_ENV_SETUP_MODELS` pins a model or policy per harness.

#### 11d. The sister tool's interfaces

**Optional**, nothing to set ([spec 0014](specs/0014-sister-tool-interfaces.md)). Harness manifests (`hangar session|job|env manifest`), trace export (`hangar trace export`, `GET /api/jobs/{id}/trace`) and experiment jobs (`hangar experiment run`) work as soon as it starts, owner-scoped like the rest. `HANGAR_TRACE_LIVE_TOOLS=true` also sends each turn's tool spans to the configured OpenTelemetry collector; `HANGAR_EXPERIMENT_MAX_TRIALS` (200) and `HANGAR_EXPERIMENT_PARALLEL` (8) bound an experiment.

### 12. Operations: the Cloud Doctor, the console and the vault key

#### 12a. The Cloud Doctor

**Optional**, on by default in report-only form: every night at 00:30 (`HANGAR_DOCTOR_SCHEDULE`, in the power schedule's zone) it reads the flight records, counts transient failures, files requests for hosts and secrets agents keep needing, and, **with a team API key** for its harness, starts root-cause jobs and opens fix PRs only when sure ([doctor.md](doctor.md)). Every PR goes through landing and a human approval.

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt   # if step 7 didn't
hangar config set HANGAR_DOCTOR_SPECS_REPO acme/hangar-envs         # optional: where environment specs live as files
hangar config set HANGAR_DOCTOR_MODEL frontier                      # optional: the default is balanced
hangar admin doctor run --dry-run                                   # what a run would do now, changing nothing
```

- **Model:** `balanced` for every harness (resolved and pinned on each job's session): Opus on Claude Code, which any team key can use. `HANGAR_DOCTOR_MODEL=frontier` asks for Fable; on a team key that may lack Fable access, `best` instead, which Claude Code resolves against the key in use.
- **Budget:** `HANGAR_DOCTOR_MAX_USD` ($10 a run) and `HANGAR_DOCTOR_JOB_MAX_USD` ($3 a job); at most 3 doctor PRs open at once.
- **Specs:** without `HANGAR_DOCTOR_SPECS_REPO` (and `HANGAR_DOCTOR_SPECS_PATHS`), a fix to an environment spec is reported instead of opened.

#### 12b. The operator console

Nothing to set. `hangar status` (`GET /api/status`, `hangar://status`; admins) shows the power state and last deploy, the loops' heartbeats, harness versions, builds, the day's failures by cause, egress denies, waiting requests, SLOs, credential health, the `models` line, the Cloud Doctor and recent warnings. `hangar admin records ls|show` reads the flight records; `hangar cred check` checks credentials now ([observability.md](observability.md)).

#### 12c. Rotating the vault key

**Optional**; nothing changes until you do it. Rotate on a schedule, when an operator who could read the key leaves, and at once when it may have leaked. The full procedure, with every command, is [security.md](security.md#rotating-the-vault-key); in short: put a new key first in Secret Manager (`<new>,<old>`) and the `hangar` Secret, restart, run `hangar admin rotate-key` (it re-seals the vault, re-encrypts checkpoints and backups in the background, and retires the old key when nothing holds it; `--status` shows where it is), then remove the old key from the configuration and **disable** (not destroy) its Secret Manager version until every backup made before the rotation has aged out. Take a backup afterwards.

### 13. Deploying with deploy.yml

**Optional**; required to deploy through `.github/workflows/deploy.yml` rather than `up.sh`. The setup is [below](#deploying-with-deployyml): the `production` environment with you as its required reviewer and only `main` and `v*` tags allowed; its five environment secrets; the `hangar-deploy` service account with `roles/container.clusterViewer` and a Role on the `hangar` Deployment. `bash deploy/gke/setup-cicd.sh` does all of it, with the images workflow's secrets (step 3), the vault key (step 1) and the package mirrors (step 10a), and prints the rest; [cicd.md](cicd.md) walks through the first deploy.

### 14. Clients and proof

- **claude.ai users remove the hangar connector and add it again** (Settings → Connectors): the apps never refetch the tool list ([clients.md](clients.md)). CLIs refetch it when a session starts. Waves 2, 3 and 4 add no MCP tools (the inbox and the console are resources, requests ride on `pending_approvals` and `answer_approval`, listings gain a `cursor` field; Wave 4's manifests, traces and test results are resources, REST and CLI): 54 tools, 24,647 B.
- `bash scripts/acceptance/gke.sh` runs the checks from the overnight guide's [§5](#5-prove-it-on-your-cluster) plus `mcp` (`create_sandbox` over MCP on a multi-harness environment within claude.ai's 230 s, a second create whose client gives up after 5 s still comes up, then `exec`), `power` (a create refused while the pools are off) and `warmstart` (a warm and a cold create, timed). It prints the server's version with its tool-list hash.
- **Acceptance fixtures (optional; each check skips without its own):** a test repo in `HANGAR_GITHUB_REPOS` with the GitHub App installed, *Allow auto-merge*, and branch protection that requires code owners' review; a second account's classic `repo` token (`HANGAR_ACCEPT_APPROVER_TOKEN`); a token that writes to the test and fixture repos (`HANGAR_ACCEPT_GITHUB_TOKEN`); a fixture repo with an unprotected default branch; a Jira project mapped to the test repo (`HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET`). For deploy.yml, the repos are `production` variables (`HANGAR_ACCEPT_TEST_REPO`, `HANGAR_ACCEPT_FIXTURE_REPO`) and the tokens `production` secrets. [acceptance.md](acceptance.md#before-the-first-run-fixtures) has each one.
- **The first live run follows [acceptance.md](acceptance.md)**, in its order: the core checks, the operator's views (`status`, a Cloud Doctor dry run), then the team release's own (`computer`, `egress`, `services`, `envbuild`, `secrets`, `landing`, `intake`, `scale-smoke`, `power --power-cycle`, `prepull`), with the fixtures each needs and what each failure most likely means. The harness contract and canary are proved in GitHub Actions, not on the cluster: `contract` green on the commit you deploy.
- `uv run hangar images plan --registry "$REG"` exits 0, and `hangar pack list` shows each pack's locked and installed version.
- The first 09:50 power-up should report `prepull.ready: true` in `hangar power`.

### Rolling back this release

In this order:

1. **Remove members first** (`hangar person rm <id>`, which revokes their tokens). The previous version has no people: every `mcp` token would see and act on everyone's work, and `user` tokens are refused.
2. **Disarm landing.** `hangar repo set REPO --land manual` on each landing repo, and disable auto-merge on any agent PR the inbox shows as approved: the previous version doesn't disarm on a push.
3. **Know what goes open.** The previous version ignores `egress`, `cloud`, `build_secrets`, `computer`, `services` and `ports`: proxied environments lose their allowlist as the previous version rewrites their templates and starts their sandboxes, cloud tokens stop being minted, and secret scanning stops. Take sensitive environments out of service first. It also ignores `heavy`, `tier: heavy` and `profile`: take heavy environments out of service too, or their sandboxes start on the default tier without Docker.
4. **Hand archived sandbox records back.** The previous version runs on the upgraded file (it ignores the archive table and keeps the indexes up to date), but it can't see archived records:
   ```bash
   kubectl -n hangar exec deploy/hangar -- python3 -c "import sqlite3; db = sqlite3.connect('/data/hangar.db'); \
   db.execute('INSERT OR IGNORE INTO sandboxes(key, data) SELECT key, data FROM sandboxes_archive'); db.commit()"
   ```
5. **Hand archived jobs and sessions back, moved, not copied** (the previous version doesn't read the archive, and changes only the main table's copy, so a copy left behind would be stale):
   ```bash
   kubectl -n hangar exec deploy/hangar -- python3 -c "import sqlite3; db = sqlite3.connect('/data/hangar.db'); \
   [db.execute(q.format(t=t)) for t in ('jobs', 'sessions') for q in ( \
   'INSERT OR IGNORE INTO {t}(key, data) SELECT key, data FROM {t}_archive', 'DELETE FROM {t}_archive')]; \
   db.commit()"
   ```
   The previous version loads every job and session at startup again, so give its pod the memory the history needs. (An upgraded hangar drops an archive row whose key is also in the main table when it starts, so a copy an earlier rollback left is harmless.)
6. **One vault key.** Finish a rotation (or don't start one), then put the single key `hangar admin rotate-key --status` lists as encrypting back in `HANGAR_SECRET_KEY`: the previous version reads one key and doesn't start with two. With a key file, `secret.key` holds one key once a rotation is retired.
7. **Set `HANGAR_JOB_CONCURRENCY` back to 4** if you raised it: the previous version has no job budgets or daily caps.
8. **Suspend live sessions** (as `power.sh down` does), or let running turns finish.
9. **The power schedule:** set `power_up_cron = "0 10 * * *"` and `sandbox_warm_nodes = 0`, and `terraform apply`. The old `power.sh` is in the old checkout; the Cloud Run jobs embed whichever script the last apply read.
10. **Put the previous image back:**
   ```bash
   kubectl -n hangar set image deploy/hangar hangar="$(cat ~/.hangar/pre-upgrade-image)"
   kubectl -n hangar rollout status deploy/hangar
   ```

What stays: the node pool (the previous version runs on it unchanged), the database, every session and checkpoint, and the old pack tags in Artifact Registry, which the previous version mounts by tag as before. What the previous version ignores: repo policies, budgets, `person` on records and the owners the migration set (every credential is shared again: remove members first, step 1), `packinfo` digests, the drain (the new `power.sh` treats its 404 as settled), the stale-tool-list records, links, environment builds and requests, flight records, the Cloud Doctor's findings, model policies (a session pinned to `claude-fable-5-1` keeps that id), the key ring's state and `HANGAR_BACKUP_STORE` (backups go beside the checkpoints again), Wave 4's repo policy fields (`heavy`, `profile`, `max_profile`, `verify_reports`, `rerun_flaky`), test runs and flaky lists, turn manifests and experiments, and the build caches (their objects stay in the `caches/` prefix until you delete it). The heavy pool, its node service account, the Docker Hub remote repository and `heavy-policy.yaml` stay; the policy refuses nothing the previous version creates. GitHub replay marks are compatible both ways. The next upgrade archives the records again; the credential migration doesn't run twice.

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

## Upgrading from before the overnight stack (#48–#69)

This guide takes a hangar that runs the version before the overnight stack (main at `2e3e0a3`, "Merge pull request #43") to the top of the stack, PRs #48–#69. From there, continue with [this release](#upgrading-to-the-team-release-75117). It is written for the GKE install from [gke.md](gke.md): one zonal cluster, gVisor sandboxes, the control plane with SQLite on the `hangar-data` PVC, checkpoints in the session bucket, site settings in the `hangar-site` config map, and `deploy/gke/up.sh` / `power.sh`. [CHANGELOG.md](../CHANGELOG.md) lists what the stack adds.

Checkpoints are in the bucket only if `hangar-site` names it, as terraform's comment on the bucket says to. Check before you start; a few things below differ when it prints nothing:

```bash
kubectl -n hangar get configmap hangar-site -o jsonpath='{.data.HANGAR_SESSION_STORE}'; echo   # gs://<project>-hangar-sessions
```

The short version:

```bash
# before: record the image, save the vault key, back up the database (§1)
git pull && uv sync                       # the stack, and the CLI that goes with it
bash deploy/gke/up.sh                     # builds (now with a Rust stage), applies two new NetworkPolicy files
hangar doctor --fix                       # adds the new GitHub webhook events (§4)
uv run hangar pack publish codex          # Codex's harness pack changed (§4)
bash scripts/acceptance/gke.sh            # proves it on your cluster (§5)
```

Every claim below was checked against the code, not the PR descriptions. A few things can only be seen on a cluster. [The last section](#what-couldnt-be-checked-without-gke) lists them, and the acceptance script checks them.

### 1. Back up first

`hangar admin backup` arrives with the upgrade, so the old version can't run it. Take one of these before `up.sh`, preferably both.

**Record what's running now**, for a rollback (§6):

```bash
kubectl -n hangar get deploy hangar -o jsonpath='{.spec.template.spec.containers[0].image}' | tee ~/.hangar/pre-upgrade-image
```

**Save the vault key** in Secret Manager if it isn't already somewhere outside the cluster. Every credential, every checkpoint and every backup is encrypted with it. Pipe it straight there; don't write it to a file on your machine ([security.md](security.md#where-the-keys-live)):

```bash
kubectl -n hangar get secret hangar -o jsonpath='{.data.HANGAR_SECRET_KEY}' | base64 -d | \
  gcloud secrets create hangar-vault-key --data-file=- --replication-policy=automatic
```

**A copy of the database.** Copying `hangar.db` and `hangar.db-wal` separately from a running hangar can tear: SQLite may checkpoint the WAL into the main file between the two copies. Use SQLite's online backup API inside the pod instead. It is what `hangar admin backup` uses: one read transaction, while hangar keeps working.

```bash
POD=$(kubectl -n hangar get pod -l app=hangar -o jsonpath='{.items[0].metadata.name}')
kubectl -n hangar exec "$POD" -c hangar -- python3 -c "import sqlite3
s, d = sqlite3.connect('/data/hangar.db'), sqlite3.connect('/data/pre-upgrade.db')
s.backup(d); d.close(); s.close()"
kubectl -n hangar cp -c hangar "$POD:/data/pre-upgrade.db" ~/.hangar/pre-upgrade.db
python3 -c "import sqlite3; print(sqlite3.connect('$HOME/.hangar/pre-upgrade.db').execute('PRAGMA integrity_check').fetchone()[0])"   # ok
kubectl -n hangar exec "$POD" -c hangar -- rm /data/pre-upgrade.db
```

The copy holds every table: sessions and their checkpoint lists, jobs, schedules, tokens, the vault and the audit log. Checkpoints themselves stay in the bucket. The upgrade doesn't rewrite them.

**A PD snapshot of the volume** copies everything on the disk, logs and local files included, and restores without the vault key:

```bash
PV=$(kubectl -n hangar get pvc hangar-data -o jsonpath='{.spec.volumeName}')
DISK=$(kubectl get pv "$PV" -o jsonpath='{.spec.csi.volumeHandle}')    # projects/<project>/zones/<zone>/disks/<disk>
IFS=/ read -r _ PROJECT _ ZONE _ NAME <<<"$DISK"
gcloud compute snapshots create "hangar-pre-upgrade-$(date +%Y%m%d-%H%M)" --project "$PROJECT" \
  --source-disk "$NAME" --source-disk-zone "$ZONE"
```

A snapshot of a running hangar is crash-consistent, like a power loss: SQLite recovers it, and may drop the last commits. For a clean one, run `kubectl -n hangar scale deploy/hangar --replicas=0` first. Sandboxes keep running meanwhile, and `up.sh` sets the replicas back to 1. [gke.md §7](gke.md#7-backups-and-restore) explains how to restore a snapshot.

### 2. What happens to your data

**No migration, and nothing one-way.** The schema is unchanged: the same tables, each a key and a JSON document (`src/hangar/store.py`). New state goes into new JSON fields, which all have defaults, and into new keys in the `kv` table:

- stored settings (`setting:*`);
- MCP tasks;
- E2B sandbox records;
- live-turn keys;
- the setup wizard's webhook URL.

No startup step converts anything. Records gain the new fields the next time hangar saves them.

**The old version can read what the new one writes.** Pydantic ignores unknown fields, and no existing field changed type or lost an allowed value. To check that, the test suite's sessions, jobs, steering, auto-fix, cache, gateway, E2B, trigger and strategy tests were run with every `Store.put` recorded. All 8,986 records written, across sessions, sandboxes, jobs, credentials, grants, tokens and schedules, parse with `2e3e0a3`'s `models.py`.

**Checkpoints work in both directions.**

- *Upgrade:* checkpoints written before it restore with the new code (`tests/test_ckpt_compat.py`, which writes them with `2e3e0a3`'s capture, frozen in the test).
- *Rollback:* checkpoints written after it, including parts copied inside the bucket instead of being uploaded again, restore with `2e3e0a3`'s restore (`tests/test_ckpt_rollback.py`). The part names, encryption and formats are unchanged.
- *End to end, on Docker:* a `2e3e0a3` hangar with a suspended session, a session whose sandbox kept running, a finished job, a `jobs` token and a vault credential was stopped and started as the stack top on the same data directory, then rolled back the same way. Both times every session went on with its commit, uncommitted change, untracked file and conversation; the running sandbox was re-attached; the job took a follow-up through its token and signed its callback with the credential. After the rollback, the `mcp` token the new version minted got 401 and 403.

**Other changes to stored data:**

- *Durability.* SQLite now commits with `synchronous=NORMAL` in WAL mode (#53). Tokens, credentials, gateway grants, stored settings and replay guards still sync on every commit. A crash of hangar loses nothing. A node crash or power loss can roll back the last commits of anything else, such as a session's latest state.
- *Job events* are also appended to a log per job (#49), so job sessions' events take twice the space on the volume.
- *Backups* go to `gs://<bucket>/_backups/` (§3), or to `/data/sessions/_backups/` on the PVC without a bucket. If you set `session_retention_days` in terraform, the bucket's lifecycle rule deletes backups older than that too.

### 3. Defaults that change behaviour

Each of these is on, or different, as soon as the new version starts. To opt out, use `hangar config set NAME VALUE`, which keeps the value in hangar's database across redeploys, or the `hangar-site` config map, which wins over the database. Settings marked *restart* apply after `kubectl -n hangar rollout restart deploy/hangar`; `hangar config set` says so. A change to `hangar-site` always needs that restart: the pod reads it as environment variables when it starts.

| What | Before → now | Opt out |
| --- | --- | --- |
| **hangar-envd** (#60) | an exec through the Kubernetes API server (pods/exec) for every command, file and terminal → `HANGAR_ENVD=auto`: on a sandbox's first use, hangar streams a ~3 MB static daemon into its `/tmp` over one exec, then reaches it at the pod IP on port 49983. That needs `deploy/k8s/envd-network.yaml`, which `up.sh` now applies. If the daemon can't be reached, hangar falls back to exec and tries again after 5 minutes, so nothing breaks; it is just slower. **The image build is slower too:** the Dockerfile compiles the daemon for x86_64 and aarch64 in a `rust:1.97-alpine` stage. On a 2-vCPU machine, with the crates already downloaded, one architecture took 2 min 1 s (release, LTO). Expect Cloud Build to take several minutes longer than before. `up.sh` allows 30. | `HANGAR_ENVD=off` (*restart*). The build stage stays. |
| **Live turns** (#59) | Claude Code and Codex turns ran one-shot → they run under hangar's driver, which can steer, interrupt and ask for approvals. That needs `python3` ≥ 3.8 in the sandbox; without it a turn falls back to one-shot. Approvals stay `auto`, meaning no prompts and permission checks off, as before. | `HANGAR_LIVE_TURNS=false` |
| **The uid split** (#66) | n/a → `HANGAR_UID_SPLIT=auto`. **On GKE it falls back to a single user:** GKE Sandbox runs a pod as one non-root user with no capabilities, so hangar logs "turns run … as the agent's own user" and carries on. With `required`, turns that use `ask` or `deny-risky` approvals fail on GKE (#69 added `deny-risky`). The split only happens on Docker. | `HANGAR_UID_SPLIT=off` |
| **Hardened turns** (#69) | *Docker only; on GKE turns run in one user, as above.* An `ask` or `deny-risky` turn in a split sandbox starts its harness hangar's way: the binary from the pack or bake, hangar's copy of the environment, and settings hangar writes. **Such a turn no longer reads the repository's `.claude/settings*.json` or `.mcp.json`, the agent's user settings, or skills, and plugins the environment enables don't load.** The environment's own `.claude/settings.json` (`agent_config.files`), its `mcp:` servers and `instructions`, and the repository's CLAUDE.md still apply. Codex turns run with `--disable hooks`. Every harness in a split sandbox, `auto` turns' too, runs with `no_new_privs`: setuid programs and `sudo` inside a turn don't gain root. Commands hangar runs as the agent outside a turn (`exec`, streamed execs, background processes) don't get it. On GKE, `ask` and `deny-risky` turns now carry a warning naming what isn't protected there. | Move what those turns need from the repository into the environment ([steering.md](steering.md#hangar-owned-harness-settings)); `HANGAR_UID_SPLIT=off` turns the split, and all of this, off |
| **Streaming** (#68) | turns' events were read every `session_poll_seconds` (5 s) → each running turn's log is followed through hangar-envd (`tail -F`, one more process per running turn in its sandbox), so events reach readers as the agent writes them; without the daemon, polling as before. New for clients: `exec(stream=true)`, the `process` MCP tool, SSE routes for exec, background processes, watches and session and job events, and MCP watch resources ([streaming.md](streaming.md)). Caps per token: 16 open streams, 16 background processes, 8 directory watches; 512 streams in all; per stream 1 MiB/s (a command printing faster to a streamed exec is slowed down) and 64 MiB; event and watch streams close after 12 h. Over a cap: an MCP error or HTTP 429. **On GKE the load balancer ends an HTTP response after the BackendConfig's `timeoutSec` (3600 s)**, so an SSE stream lasts at most an hour there; clients reconnect with `Last-Event-ID`. | `HANGAR_STREAM_TURN_LOGS=false`; the caps are `HANGAR_STREAM_MAX_PER_TOKEN`, `HANGAR_STREAM_MAX_TOTAL`, `HANGAR_STREAM_BYTES_PER_SECOND`, `HANGAR_STREAM_MAX_BYTES`, `HANGAR_STREAM_MAX_HOURS`, `HANGAR_BACKGROUND_MAX_PER_TOKEN`, `HANGAR_WATCH_MAX_PER_TOKEN` |
| **Prompt-cache TTL** (#63) | the harness default (5 min) → `HANGAR_CACHE=auto`: a Claude Code turn asks for the **1-hour** cache when its session can wait more than 5 minutes for the next message. With the default `HANGAR_SESSION_IDLE_MINUTES=30`, that is most sessions. **1-hour cache writes cost 2× the input price, against 1.25× for 5-minute writes**; reads cost the same. It pays off when a session's next message comes after 5 minutes and before an hour. With a Claude subscription login rather than an API key, nothing is billed per token. pi gets the same policy; Codex and Antigravity cache by themselves. Best-of-N attempts and forks started together now wait up to `HANGAR_CACHE_STAGGER_SECONDS` (15) for the first one to write the shared prefix. | `HANGAR_CACHE=5m` (or `off`), `HANGAR_CACHE_STAGGER_SECONDS=0` |
| **Verify inside the turn** (#65) | a job's `verify` ran after the turn → it also runs inside the turn, and a failure goes back to the agent, up to `HANGAR_MAX_VERIFY_ROUNDS` (3) times per turn. Turns can take longer and use more tokens. While the gate is on, a job whose diff changes the check's own test files is **delivered as a draft PR**, with the files listed in `result.touched_tests`. | `HANGAR_VERIFY_IN_LOOP=false`, or `verify_in_loop: false` per job |
| **PR auto-fix** (#65) | n/a → on for every repo (`ci,reviews`). A failing check on a job's own PR, or a trusted reviewer's "changes requested" or inline comments, starts a follow-up turn on that job's session (which costs money). There are at most `HANGAR_AUTOFIX_MAX_ROUNDS` (3) rounds per PR, 120 s apart. **It only fires once the webhook sends the new events (§4).** | `hangar config set HANGAR_GITHUB_AUTOFIX '{"owner/repo": "off"}'` per repo, or `"autofix": "off"` per job. `HANGAR_AUTOFIX_MAX_ROUNDS=0` also stops it, but posts an "Auto-fix stopped" comment the first time. |
| **`/metrics`** (#55) | none → `GET /metrics` on the public URL, for admin tokens and new `metrics`-scoped tokens only. Traces stay off unless `OTEL_*` variables name an exporter. | `HANGAR_METRICS_ENABLED=false` (*restart*) |
| **The E2B-compatible API** (#61) | none → mounted at `/e2b` on the public URL, with the sandbox router in front of the app. It is for admin tokens and new `sandboxes`-scoped tokens, rate-limited per `sandboxes` token. | `HANGAR_E2B_API_PATH=""` (*restart*) |
| **Database backups** (#57) | none → with `HANGAR_SESSION_STORE` on a `gs://` bucket: one ~5 minutes after each start when none is recent, then every 24 h, into `gs://<bucket>/_backups/`, keeping the newest 14. They are gzipped and encrypted with the vault key. Until the first one exists, `hangar doctor` warns "none yet". Without a bucket nothing is scheduled, and `hangar doctor` warns "no scheduled backups of hangar's database": `hangar config set HANGAR_BACKUP_HOURS 24` writes them to the PVC, which a PD snapshot then covers too. | `HANGAR_BACKUP_HOURS=0`; `HANGAR_BACKUP_KEEP` sets the count |
| **Rate limits and caps on the front doors** (#50, #56) | none → per caller (a `jobs` or `mcp` token, a GitHub or Slack user, a webhook source): 30 job submissions or follow-ups a minute (bursts of 10) and 10 unfinished jobs. Each front door (github, slack, webhook, a2a, api, mcp) gets 120 a minute (bursts of 60). Request bodies are capped at 100 MB for a valid token and 64 KB without one. Admin tokens and schedules are exempt. A refused request gets a 429 and is audited. | `0` turns one off: `HANGAR_JOB_RATE_PER_MINUTE`, `HANGAR_JOB_SOURCE_RATE_PER_MINUTE`, `HANGAR_JOB_MAX_ACTIVE_PER_OWNER`; size with `HANGAR_MAX_REQUEST_MB` |
| **Replays** (#50) | a webhook redelivered with a new delivery id ran again → a signed body seen in the last `HANGAR_WEBHOOK_REPLAY_HOURS` (168) is a duplicate and does nothing; so is a GitHub redelivery of an `issues` or `issue_comment` event that was already handled. | `HANGAR_WEBHOOK_REPLAY_HOURS` |
| **OAuth for MCP clients** (#50, #56) | any https redirect on the allowed hosts → when a client *registers*, its redirect must be https, or http to loopback only (`localhost`, `127.0.0.1`, `::1`). A custom scheme needs an exact `scheme://host` entry; `cursor://anysphere.cursor-mcp` is now allowed by default, so Cursor signs in. An empty `HANGAR_OAUTH_REDIRECT_HOSTS` now allows no hosts (it used to allow any). `/authorize` refuses a `resource` other than `HANGAR_PUBLIC_URL` or `<HANGAR_PUBLIC_URL>/mcp` (RFC 8707), so **clients must use the same URL as `HANGAR_PUBLIC_URL`**. `up.sh` sets both from the same domain. Clients registered before the upgrade keep their registration. | add entries to `HANGAR_OAUTH_REDIRECT_HOSTS` (*restart*) |
| **MCP sessions** (#52) | stateless → pre-2026 clients get an `Mcp-Session-Id`, so hangar can ask them questions and send them notifications. Sessions live in the pod's memory: after this upgrade's restart, and after every one since, a client's old session id gets a 404, and the MCP spec has the client start a new session. | `HANGAR_MCP_STATELESS_HTTP=true` (*restart*) |
| **Slack Connect** (#50) | anyone in a shared channel → a member of another organization counts only when listed in `HANGAR_SLACK_USERS` (as `<team>/<user>` or by user id); Enterprise Grid workspaces go in `HANGAR_SLACK_TEAMS`. | list them |
| **The git gateway** (#48) | still off unless `HANGAR_GATEWAY_URL` is set. When it's on: branches under `hangar/session/` are reserved for their sessions, a grant gets at most 8 requests at once, and a source address with 30 failed grant checks in a minute gets 429s. | — |
| **The Docker network** (#62) | *laptop and single VM only, not GKE:* sandboxes go on hangar's own `hangar-sandboxes` network (bridge `hangar0`), where they can't reach each other. Running sandboxes stay on their network until they're recreated. | `HANGAR_DOCKER_NETWORK=bridge` (*restart*) |
| **Shutdown** (#57) | SIGTERM ended everything at once → long-polls answer straight away, streams and terminals get `HANGAR_SHUTDOWN_GRACE_SECONDS` (15), then hangar shuts down. The Deployment's `terminationGracePeriodSeconds` is now 45 (§4). | — |
| **I/O threads** (#53) | Python's default (CPUs + 4) → 32 per CPU, between 64 and 128, for blocking provider calls. | `HANGAR_IO_THREADS` (*restart*) |

Not changed for GKE: the default provider, the runtime class, warm pools, and approvals (`auto`). Harness packs are the same except Codex's, whose tag changes (§4). Tiers and hot resume (#64) apply only to environments that ask for `tier: fast` and to the `e2b` provider, which isn't enabled on the GKE install.

### 4. Upgrade

```bash
git pull                      # main with the stack merged
uv sync                       # the laptop CLI: hangar admin, config, setup, connect are new
bash deploy/gke/up.sh
```

**What `up.sh` does now:**

- It builds `control-plane:<short sha>` with Cloud Build, including the Rust stage.
- It applies `deploy/k8s` with two files the kustomization now lists:
  - `sandbox-network.yaml`: the sandboxes' egress baseline (DNS, plus the internet minus private, CGNAT, reserved and link-local ranges), and a deny-all-ingress policy for sandbox pods (#48, #62);
  - `envd-network.yaml`: hangar's pods may connect to sandbox pods on port 49983 and nothing else (#60).

  Both are additive to GKE Agent Sandbox's managed policy, and Dataplane V2 enforces them. Sandboxes keep their internet access; other sandboxes can no longer open connections to them. Hangar's own path in, exec through the API server, doesn't use the pod network.
- It sets the Deployment's `terminationGracePeriodSeconds: 45`.
- It leaves `HANGAR_SESSION_STORE` alone on an existing install: it fills in the bucket only when the `hangar` config map already names one, and a value in `hangar-site` wins anyway. Without either it prints a note: moving a running hangar to the bucket would strand the checkpoints of suspended sessions on the PVC.

The Deployment uses `Recreate`, so the URL answers 502 from the moment the old pod stops until the new one is ready. Sandboxes and running agent turns outlive the pod, as before, and the new pod re-attaches to them. A turn that started under the old version finishes as a one-shot turn: it can't be steered.

`terraform apply` isn't needed. The only terraform change is `microvm.tf`, an optional microVM node pool that is off by default (#64); a plan shows no changes besides a new `microvm_pool` output that is null. No new GCP API and no new IAM role are needed. Backups go to the session bucket, where the hangar service account already has `roles/storage.objectAdmin` (`deploy/terraform/gke/main.tf`). That covers writing, listing and deleting backups, and the server-side copies checkpoints now use.

**Steps after the rollout:**

1. **GitHub webhook events** (#65). Auto-fix needs `pull_request_review`, `pull_request_review_comment`, `check_run`, `check_suite` and `status`, on top of `issues` and `issue_comment`. Until the webhook sends them, `hangar doctor` fails with "github webhook <repo>: the webhook doesn't send …", and auto-fix never fires. Then either:
   - run `hangar doctor --fix`, which adds the events to the existing hook (only adding: other events and the TLS setting stay);
   - or run `hangar setup github` again, which also moves the GitHub settings into hangar's database.

   Both need the stored GitHub token to manage webhooks: on a fine-grained token, *Webhooks: Read and write*. Otherwise add the events in the repo's Settings → Webhooks.
2. **GitHub token permissions** (#65). Auto-fix reads failing Actions logs, which needs *Actions: Read* on a fine-grained token. GitHub doesn't open the check-runs API to fine-grained tokens. With one, auto-fix describes a failing check from its webhook payload and Actions log, without annotations; a classic token with `repo` gets those too ([jobs.md §4](jobs.md)).
3. **New hangar token scopes** (#50, #51, #55, #61). Existing tokens keep working.
   - `mcp`: `/mcp` only. `hangar connect` mints these for MCP clients.
   - `metrics`: `GET /metrics` only.
   - `sandboxes`: the E2B API only, for the sandboxes the token creates.

   Mint them with `hangar token create <name> --scope <scope>`.
4. **Republish the Codex pack** (#69). #69 changed the wrapper script in Codex's harness pack, so the pack's tag changes. Run `uv run hangar pack publish codex` from a machine with Docker, as in [gke.md §3](gke.md#harness-packs). Otherwise the first Codex sandbox after the upgrade builds the pack itself, with Cloud Build from the control-plane pod, and waits several minutes for it. Running sandboxes keep the pack they mounted. Environments that bake Codex into their image rebuild on their next use.
5. **Move repository settings that `ask` turns rely on into the environment** (#69, Docker only). On GKE nothing changes. On a Docker hangar, an `ask` or `deny-risky` turn ignores the repository's `.claude/settings*.json`, `.mcp.json` and skills: put allowed tools, rules and hooks in the environment's `agent_config.files: {.claude/settings.json: ...}`, MCP servers in `mcp:` and instructions in `agent_config.instructions` ([steering.md](steering.md#hangar-owned-harness-settings)). A turn's warnings say what was left out.
6. **Optional manifests**, none of them in the kustomization:
   - `deploy/k8s/observability/` (#55) holds the Managed Prometheus or Prometheus Operator scrape config, which needs a `metrics` token in a Secret, and the OpenTelemetry Collector policy. Cloud Trace needs Google's collector and its own IAM roles ([observability.md](observability.md)).
   - `deploy/k8s/e2b.yaml` (#61) gives E2B SDKs per-sandbox host names under your own domain. It needs the Gateway API on the cluster, Certificate Manager and a static IP ([gke.md §8](gke.md#8-e2b-sdk-clients-optional)). Without it, SDKs work through the existing URL (`E2B_API_URL=https://<host>/e2b`, `E2B_SANDBOX_URL=https://<host>/e2b/sandbox`).
7. **Settings can move into the database.** `hangar config list` shows where each value comes from. Values in `hangar-site` keep winning; [gke.md §2a](gke.md#2a-log-in-connect-and-configure) explains when to keep them there.

### 5. Prove it on your cluster

```bash
hangar login https://<your hangar> --token-file ~/.hangar/gke-admin-token   # if you haven't
bash scripts/acceptance/gke.sh                                              # free checks, ~5-10 min
bash scripts/acceptance/gke.sh --gateway-repo you/scratch-repo              # + the git gateway, if HANGAR_GATEWAY_URL is set
bash scripts/acceptance/gke.sh --with-model --session-env <env with a Claude credential>   # + paid turns
```

Each check prints PASS, FAIL or SKIP with its time:

| Check | What it proves |
| --- | --- |
| `doctor` | `hangar doctor` reports no failures (warnings are listed) |
| `k8s` | the new NetworkPolicies exist in `hangar-sandboxes`, the grace period is 45, the pod is ready |
| `sandbox` | a `minimal` sandbox starts |
| `exec` | 20 exec round trips: their p50/p95 from your laptop, and the transport they used (from `/metrics`: `envd` or `exec`). It fails unless all of them went through hangar-envd (`--no-expect-envd` relaxes that). It also measures **inside the control-plane pod**: 20 × `true` through pods/exec, then 20 × through hangar-envd, the in-cluster comparison spec 0013 asked for. |
| `stream` | a streamed exec over Server-Sent Events, through the ingress (#68): the first line arrives while the command still runs, so nothing in between buffers the stream. It fails when the command wasn't streamed through hangar-envd (`--no-expect-envd` relaxes that). |
| `files` | a 1 MiB file written and read back intact through MCP `write_file`/`read_file` |
| `isolation` | from sandbox A: sandbox B's hangar-envd port, the metadata server (169.254.169.254:80), GKE's Workload Identity metadata server (169.254.169.252:988), hangar's Service and hangar's API port on its pod are all closed, while the internet is open |
| `session` | a session with no model turn: a commit, a change and an untracked file, then checkpoint, suspend (its sandbox goes) and open (a new sandbox restores all three), each timed |
| `model` | `--with-model`: a short real turn whose answer contains a requested word, with its cost |
| `steer` | `--with-model`: a live turn with `approvals=ask`. The agent's command waits for an approval, a steer message is delivered while it waits, the command runs once allowed |
| `gateway` | `--gateway-repo`: the session's sandbox is in gateway mode with no GitHub token in it; a push to `hangar/accept-<run>` goes through and a push to `accept-<run>-outside`, outside the sandbox's grant like `main`, is refused by the gateway. It never pushes to `main`: if the gateway let a push through, the run's own branch takes it. Both branches are deleted afterwards with `gh`, if you have it. |
| `e2b` | the stock `e2b` SDK (if installed: `uv sync --extra e2b`) with a `sandboxes` token: create, run, files, list, kill |
| `backup` | `hangar admin backup` now and in the list, stored in `gs://` |
| `metrics` | `/metrics` serves hangar's series, and a `metrics` token reads it and nothing else |
| `power` | hangar's power state answers. With the sandbox pools on: what the last power-up pulled and the warm pools. With them off: a create is refused at once (HTTP 503) with when they're back |
| `warmstart` | two creates back to back on `--warm-env` (default `claude-python`, which keeps a warm pool): the first claims the warm pod, the second starts cold; both times are printed, and `--record FILE` keeps them as JSON lines |

The script runs `power.sh` only with `--power-cycle`, which powers the pools down (checking the refusal) and up again, timed, and refuses while others are working ([acceptance.md](acceptance.md)). Everything the script creates is named `accept-<run>` and is removed at the end, also after a failure or Ctrl-C (`--keep` leaves it). `tests/test_acceptance_script.py` runs the script against a hangar on Docker, with GitHub replaced by a local git server behind the gateway. Every check but `k8s` and `steer` runs there for real, `model` with a scripted agent in place of Claude. `k8s`, `steer` and the in-cluster exec measurement run against stand-ins: a fake kubectl, a fake API, and the Docker provider in place of the Kubernetes one.

### 6. Rolling back

Put the previous image back:

```bash
kubectl -n hangar set image deploy/hangar hangar="$(cat ~/.hangar/pre-upgrade-image)"
kubectl -n hangar rollout status deploy/hangar
```

The tag is `control-plane:<short sha>` of the commit you deployed from, `control-plane:2e3e0a3` if that was main at `2e3e0a3`. Images stay in Artifact Registry. `kubectl -n hangar rollout undo deploy/hangar` does the same, and also restores the old grace period.

**What keeps working:** the database as it is (§2), every session, and every checkpoint, old or new (§2). Leave the new NetworkPolicies in place. The old version reaches sandboxes through exec, so the deny-ingress and envd policies don't affect it, and the egress baseline only allows.

**What stops working:**

- **Settings stored with `hangar config set` or `hangar setup`** are ignored: the old version reads only its environment. If `hangar setup github` moved your GitHub settings into the database, put them back in `hangar-site` before you roll back, or GitHub jobs stop.
- **Tokens with the new scopes** (`mcp`, `metrics`, `sandboxes`) are refused: the old version lets non-admin tokens reach only `/api/jobs`. MCP clients set up with `hangar connect` need an admin token until you upgrade again.
- **Everything new is gone:** steering and approvals, envd (daemons left running in sandboxes are ignored), streaming (background processes started with `process` keep running in their sandboxes, unseen), the E2B API, `/metrics`, auto-fix, backups (the files in `_backups/` stay), MCP tasks and the new MCP features.
- **Turns that are running** when you roll back were started under the new driver. Suspend sessions first, as `power.sh down` does, or let running turns finish.

### What couldn't be checked without GKE

These were checked on Docker and with fakes only. The acceptance script checks each on the cluster:

- **hangar-envd under gVisor** (runsc on GKE), streamed in over pods/exec and reached at the pod IP through Dataplane V2 with `envd-network.yaml`: the `exec` check, which fails when calls don't go through envd.
- **How the new NetworkPolicies interact with Agent Sandbox's managed policy**: the `isolation` check, from inside a sandbox.
- **GKE's exec latency, and envd's on GKE** (spec 0013's open item): the `exec` check prints both.
- **Backups and server-side copies in the bucket through Workload Identity**: the `backup` check, and the `session` check's checkpoint (each checkpoint after the first copies its unchanged parts).
- **E2B streaming through the Google load balancer**: the `e2b` check.
- **Server-Sent Events through the Google load balancer, unbuffered**: the `stream` check.
- **Following a turn's log under gVisor** (#68). In a Docker container GNU `tail -F` polls, hence its `-s 0.1`; gVisor wasn't measured. The `model` and `steer` checks (`--with-model`) run followed turns, but don't time how late their events arrive.
- **Cloud Build time with the Rust stage**: watch the first `up.sh`.
