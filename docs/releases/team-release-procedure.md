# The team release: steps 1–8

Back up, quotas, images, the node pool, the rollout, the check, people and budgets. Part of the [team release](team-release.md) (#75–#117); steps 9–11 are on [sandboxes and front doors](team-release-sandboxes.md), 12–14 and the rollback on [operations](team-release-operations.md).

## 1. Back up

```bash
kubectl -n hangar get deploy hangar -o jsonpath='{.spec.template.spec.containers[0].image}' | tee ~/.hangar/pre-upgrade-image
hangar admin backup && hangar admin backups      # encrypted with the vault key, into the session store
```

**The vault key in Secret Manager (required on GKE, before step 5).** Every credential, checkpoint and backup is encrypted with it, a backup can't be restored without it, and a rotation (step 12c) works from there. If it lives only in the `hangar` Secret (what `up.sh` does), copy it once, piped, never to a file on your machine ([security/vault-key.md](../security/vault-key.md#where-the-keys-live)):

```bash
kubectl -n hangar get secret hangar -o jsonpath='{.data.HANGAR_SECRET_KEY}' | base64 -d | \
  gcloud secrets create hangar-vault-key --data-file=- --replication-policy=automatic
```

Give `roles/secretmanager.secretAccessor` on it only to the operators who rotate keys. `deploy/gke/setup-cicd.sh` does this copy when the secret doesn't exist yet ([cicd.md](../cicd.md)).

**Backups apart from the checkpoints (optional).** Backups go to `_backups/` in the checkpoints' bucket, under hangar's own service account, and `hangar doctor` warns about that (`backup isolation`). To fix it, create a bucket of its own where hangar's service account has only `roles/storage.objectCreator` (it writes backups, can't read or delete them), add a lifecycle rule that deletes them after the time you keep them, and `hangar config set HANGAR_BACKUP_STORE gs://<bucket>` (restart). Restores are then an operator's (`hangar admin restore --local`).

The previous version runs on the upgraded database file (see [rolling back](team-release-operations.md#rolling-back-this-release)), so the backup is for a disaster, not for the rollback.

## 2. Request quotas, days ahead

The sandbox pool moves to `n2d-standard-16` (AMD keeps SMT on under GKE Sandbox, so 14 sandboxes fit a node) with up to 10 nodes ([team-scale.md](../team-scale.md#how-many-nodes)). In *IAM & Admin → Quotas* for your region request `N2D_CPUS` ≥ 160, `CPUS_ALL_REGIONS` to match, and `SSD_TOTAL_GB` ≥ 2,250. Spot VMs take standard `N2D_CPUS` quota unless the region granted preemptible quota. A refused scale-up shows as sandboxes stuck "waiting for a node", and `hangar doctor` names the quota.

**The heavy pool (optional; required for the heavy tier on GKE, step 10f).** Its `n2-standard-32` nodes take `N2_CPUS`: request 128 for the default `heavy_max_nodes = 4`, on top of what the Kata pool uses, and 2,000 GB more `SSD_TOTAL_GB` for their 500 GB disks ([team-scale.md](../team-scale.md#how-many-nodes-and-the-caps)).

## 3. Prebuild the new image and pack tags

**#124 changes every environment's image tag** (sudo is off by default, which changes the Dockerfile). Run `hangar images plan` to list them and build them here, or in CI (#83), before step 5, ideally in the 01:00–10:00 window while the pools are off. Otherwise the first sandbox of each environment waits for its build.

Before the rollout, so no create waits on a Cloud Build. This talks only to Artifact Registry, so it works while the cluster is powered down:

```bash
REG=$(terraform -chdir=deploy/terraform/gke output -raw registry)
uv run hangar images plan --registry "$REG"    # every tag the commit needs; exit 3 = something to build
uv run hangar images build --registry "$REG"   # Docker here (linux/amd64), else Cloud Build
uv run hangar images plan --registry "$REG"    # exit 0: nothing missing
```

If the cluster sets `HANGAR_IMAGE_PREFIX`, `HANGAR_SANDBOX_USER`, `HANGAR_WORKSPACE_DIR`, `HANGAR_EXTRA_CA_BUNDLE` or `HANGAR_K8S_IMAGE_VOLUMES` in `hangar-site` or its database, export the same values first: they change the tags. With the images workflow's secrets set ([gke.md](../gke.md#images-and-packs-built-in-ci); *optional*), the push to `main` does this for you and the run's summary lists each tag. `up.sh` runs the same build before it touches the cluster (`PREBUILD=0` skips it). If nothing prebuilt them, nothing breaks: running sandboxes keep the pack they mounted, the new control plane rebuilds the packs in the background, and the first sandbox for a harness whose pack isn't built yet waits for it. Environments you give `computer:` later (step 10e) build a new image layer then; repo environment builds (step 10c) build in the background, never on a create.

## 4. In the power-down window: the node pool and the power schedule

After `power.sh down` has finished (it runs at 01:00 ET; the cluster is down until 10:00), so no sandbox is on the pool:

```bash
cd deploy/terraform/gke
terraform plan    # expect "~ update in-place" on google_container_node_pool.sandboxes, the new cron and IAM
terraform apply
```

One apply does both halves of the release:

- **The node pool (required).** `machine_type` is an in-place node pool update with the google provider ≥ 5.x (`versions.tf` asks for ≥ 6.0): GKE replaces the nodes, not the pool, and with the pool at zero nodes that is quick. **If the plan shows the pool destroyed and created (`-/+`), stop**: something else in it changed (`sandbox_spot` forces a new pool). To keep the old pool, set `sandbox_machine_type` and `sandbox_max_nodes` in your tfvars. Done outside the window, every sandbox on the pool is killed, and GKE needs a node's worth of spare quota to surge.
- **The power schedule (required with `enable_power_schedule = true`).** `power_up_cron` `0 10 * * *` → `50 9 * * *` with `power_warm_minutes = 10`; the gVisor pool's minimum becomes `sandbox_warm_nodes` (1 with the power schedule, else 0); hangar's service account gets `roles/run.invoker` on the power-up job; output `power_up_job`. The apply turns the gVisor pool's autoscaling back on at its floor, which starts a node: that's fine here, since step 5 brings the cluster up.
- **The heavy pool (optional; required for the heavy tier on GKE).** `enable_heavy_pool = true` in `terraform.tfvars` (and `heavy_machine_type`, `heavy_max_nodes`, `heavy_spot`, `heavy_disk_gb` if the defaults don't fit) adds `sandboxes-heavy`: Ubuntu, nested virtualization, scaling from zero, tainted `hangar.dev/runtime=heavy`, under the power schedule like the other `sandboxes-*` pools, its nodes running as a service account of their own (`hangar-heavy-nodes`) with only a node's roles and read access to the `hangar` repository. The plan shows only additions. [gke.md §4a](../gke.md#4a-the-heavy-tier-optional).
- **The Docker Hub remote repository (optional).** `enable_dockerhub_mirror = true` creates an Artifact Registry remote repository for Docker Hub, read by hangar's service account, so heavy sandboxes' pulls aren't rate-limited (output `dockerhub_mirror`, used in step 10f). With `dockerhub_username` and `dockerhub_token_secret` (a Secret Manager secret holding an access token) it pulls as that account: use one with **no private repositories**, since every heavy sandbox can pull whatever the account can, through the mirror. Without them it pulls anonymously.

## 5. Bring the cluster up and roll out

```bash
bash deploy/gke/power.sh up     # the new script: brings back the system pool and hangar an old down stopped too,
                                # and keeps the 0-10 range the apply set, instead of the 0-4 saved at down
bash deploy/gke/up.sh           # prebuild (finds nothing missing), deploy/k8s (incl. the egress port 3128 and its
                                # NetworkPolicies), the rollout; sets HANGAR_POWER_UP_JOB from terraform
```

Do this before the team starts, or let the 10:00 schedule run `power.sh up` and deploy right after. The Deployment uses `Recreate`, so the URL answers 502 until the new pod is ready. On its first start hangar makes the earlier credentials the owner's, creates the store's indexes and archive tables (20,000 sandbox records index in well under a second; the jobs and sessions tables take ~2 s per 20,000 before hangar answers), records which vault key sealed the store, runs a reconciler pass before sessions and jobs look at their sandboxes, resumes jobs the power-down interrupted, and asks GitHub to redeliver what it missed overnight. From the next 01:00, `power.sh down` leaves hangar up.

## 6. Check

```bash
hangar doctor
hangar status    # the console: power, loops, versions, builds, failures, requests, SLOs, credentials, models
hangar power     # "sandboxes: on"; the last power-up, the last deploy, the loops' heartbeats
```

The doctor names environments that attach the control plane's own GitHub token in direct mode (fix with `hangar cred exposure <name> sandbox`, or switch them to the gateway), mapped repos whose jobs would run with no check (`hangar repo set <repo> --verify "…"`), the webhook's events (step 9a) and quota errors. New in this release: **personal credentials shared** (fails on a subscription login stored as the team's), **team credentials for jobs**, **unlinked senders**, the Jira configuration, landing repos' branch protection (**fails** without stale-approval dismissal), **sandbox power**, **last deploy**, **background loops**, **vault key** (a pending rotation, a retired key still configured), **backup isolation**, **credential health** and **model spend** (warns until step 8 sets a budget).

## 7. People: your logins stay yours; add team credentials, then the team

Every existing token now belongs to `owner` (you, an admin), and records from before have no person, so only admins see them. **Every credential stored before this release became yours on the first start**, once, so your Claude Max or ChatGPT login never goes into a member's sandbox. Control-plane credentials (signing keys, bot tokens, hangar's own GitHub token) stayed the control plane's. That also means jobs no person owns (GitHub, Slack, Jira and Projects senders you haven't linked, webhooks, schedules) no longer get your login: give them a team credential (**required** if any such work runs), and link yourself so your own issues run as you:

```bash
hangar cred migration                             # what became yours
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt   # an Anthropic Console key
hangar cred add codex-team --team --harness codex --kind api_key --file openai-key.txt        # an OpenAI project key
hangar cred owner gh-jobs team                    # an API key or git token of yours the team may use
hangar person link owner github <your-login>      # your GitHub issues and comments run as you, with your login
hangar doctor                                     # "team credentials for jobs" names what still has none
```

**A team credential is an API key** (or another credential the vendor's terms allow for automation). hangar refuses a Claude Pro or Max login or a ChatGPT login as the team's, whether added with `--team` or moved with `hangar cred owner NAME team`: the vendors' terms make a login one person's, and Claude Code's forbid routing requests through a Pro or Max login on behalf of others. The full guide, including what the vendors allow and Codex logins, is [team.md](../team.md).

Then add each engineer (optional, and the point of the release):

```bash
hangar person add alice --name "Alice"            # prints her login token once, and lists the team credentials she will get
hangar person link alice github alice-gh          # stored as her numeric GitHub id; also: slack T…/U…, jira <account id>, email
```

She runs `hangar login <url>` with it, `hangar connect` for her MCP clients, and `hangar cred add` for her own API key (and, if she likes, her Claude or Codex subscription login for her own interactive sessions), which then goes only into her sandboxes.

**Subscription logins are interactive-only (#125).** Jobs, schedules, webhooks, experiments and replays never use an `oauth_token` or `auth_file` credential, yours included. The order: **add API keys first, then upgrade.** Before rolling out, add an API key for each harness your jobs use, your own (`hangar cred add claude-api --harness claude-code --kind api_key --file key.txt`) and the team's, and set a spend limit on each in the vendor's console. After the rollout, `hangar doctor`'s **subscription logins** check names any mapped repo, webhook or schedule that would find only a subscription login, and anyone whose only login for a harness is one. Jobs mid-flight keep their lease; new ones for someone with only a subscription login fail at submit, saying what to add. `HANGAR_SUBSCRIPTION_LOGINS=any` restores the old behaviour while you migrate (with a warning on each job, and a failing doctor check). See [team.md](../team.md#subscription-logins). Members see and act on their own sandboxes, sessions and jobs; admins on everyone's ([security/people.md](../security/people.md#people)). The per-person caps count her as one person across all her tokens. Only admins link accounts, and a linked account acts as its person ([team.md](../team.md#who-is-trusted)): link one you've seen the person use.

## 8. Budgets, then job concurrency

Job slots stay at 8 on GKE because nothing bounds what a job spends until budgets are set. Set them (optional, but **required** before raising the slots), then raise the slots to the measured 24:

```bash
hangar config set HANGAR_BUDGET_DAY_TIMEZONE America/New_York     # a day starts at local midnight
hangar config set HANGAR_JOB_BUDGET_MAX_COST_USD 10                # every job's turns together (an example)
hangar config set HANGAR_PERSON_MAX_USD_PER_DAY 150                # per person, all their tokens (an example)
hangar repo set acme/app --max-usd-per-day 60 --verify "uv run pytest -q"
hangar config set HANGAR_JOB_CONCURRENCY 24                        # restart
kubectl -n hangar rollout restart deploy/hangar
```

A slot is a job, not a sandbox: 24 best-of-4 jobs could hold all 120 sandboxes, so keep `HANGAR_PERSON_MAX_RUNNING_JOBS` (half the slots) and the budgets in place ([team-scale.md](../team-scale.md#limits-and-follow-ups)).

**Jobs default to `balanced`** (Opus 5.5 on Claude Code, $4/$20 per MTok; [harnesses.md](../harnesses.md#models-the-latest-strong-model-per-provider)), and **hangar sets no spend cap of its own**: the amounts above are examples, and choosing them is yours. Until `HANGAR_JOB_BUDGET_MAX_COST_USD` or `HANGAR_PERSON_MAX_USD_PER_DAY` is set, `hangar doctor` warns (`model spend`) and `hangar status` shows `NO job budget or daily cap` on its `models:` line. A repo policy's `job_budget` or `--max-usd-per-day` caps only that repo. Where the last bit of capability matters more than the price, use `model: frontier` (Fable 5.1, $10/$50) in the environment, the repo policy or the job.

