# The team release: steps 9–11, front doors and sandboxes

Front doors, sandboxes, models, the canary and environment setup. Part of the [team release](team-release.md) (#75–#117); steps 1–8 are on [the procedure](team-release-procedure.md), 12–14 and the rollback on [operations](team-release-operations.md).

## 9. Front doors

### 9a. The GitHub webhook

**Required.** Landing needs the `pull_request` event on each mapped repo's webhook, which hooks set up before don't have:

```bash
hangar doctor --fix      # adds pull_request to every mapped repo's hook
```

### 9b. The GitHub App and landing

**Optional**; required for an approval to land the PR. Until then agent PRs come from the control token and are merged by hand. [landing.md](../landing.md) has the details.

1. Create a GitHub App under the organization with exactly **Contents: Read and write**, **Pull requests: Read and write** and **Metadata: Read**, its webhook off; generate a private key; install it on only the repos agents work on. Don't put it on any bypass list.
2. Store it and point hangar at it:
   ```bash
   jq -n --arg id 123456 --rawfile key ~/Downloads/hangar-agents.*.private-key.pem \
     '{app_id: $id, private_key: $key}' | hangar cred add github-app --harness git --kind github_app
   hangar config set HANGAR_GITHUB_APP_CREDENTIAL github-app
   ```
3. On each repo that should land (**required** for it), under Settings → Branches or Rules: require a pull request with at least 1 approval, dismiss stale approvals on new commits, require approval of the most recent push, require status checks; and under Settings → General, **Allow auto-merge**.
4. Turn it on per repo: `hangar repo set acme/app --land auto_after_approval --approver alice --risk-path 'infra/**'`. Then `hangar doctor` checks the protection for every such repo.

### 9c. Jira

**Optional.** [intake.md](../intake.md#jira-cloud) has the details, and Data Center's differences.

```bash
hangar cred add jira-api --harness webhook --kind generic        # the hangar account's API token (Ctrl-D)
hangar cred add jira-webhook --harness webhook --kind generic    # the webhook's secret
hangar config set jira '{"url": "https://acme.atlassian.net", "email": "hangar@acme.com",
  "token_credential": "jira-api", "webhook_secret_credential": "jira-webhook",
  "projects": {"ABC": {"repo": "acme/api"}}, "trigger": {"status": "Ready for agent"},
  "statuses": {"started": "In Progress", "needs_input": "Blocked", "delivered": "In Review"}}'
```

In Jira, *Settings → System → WebHooks*: URL `https://<your hangar>/api/hooks/jira`, the same secret, events Issue created and updated and Comment created, with JQL `project in (ABC)`. Prefer this signed webhook to an Automation rule.

**Already using an Automation rule (#128)?** An Automation request now starts a job only when the issue's history shows the trigger transition within the last 10 minutes (`automation_max_transition_age_minutes`), at most 3 times per issue a day (`automation_max_starts_per_issue_per_day`), and only as `"webhookEvent": "hangar:automation"`: a rule that fires on a schedule or long after the transition, or sends any other event with the token, is now ignored, audited and alerted to admins. `hangar doctor` warns on a token older than 90 days and while issue events don't arrive through the signed webhook. Move issue events to the signed webhook if you can; otherwise rotate the token (`hangar cred add jira-automation --rotate`) and update the rule's header. Mapped repos must be in `HANGAR_GITHUB_REPOS`. Then link your teammates' Jira accounts (`hangar person link alice jira <account id>`): an unlinked Jira sender's job always waits for a linked teammate's `/hangar approve`.

### 9d. GitHub Projects

**Optional.** [intake.md](../intake.md#github-projects) has the details.

1. An **organization** webhook (repository webhooks never get project events): payload URL `https://<your hangar>/api/hooks/github`, `application/json`, the same secret as the repository webhooks, only the **Projects v2 items** event.
2. The control token (`HANGAR_GITHUB_TOKEN_CREDENTIAL`) needs the organization's projects: the `project` scope on a classic token, or **Projects: Read and write** on a fine-grained one. For hangar to redeliver the board moves it missed while down, it also needs the organization's **Webhooks: Read and write** (classic: `admin:org_hook`).
3. The boards:
   ```bash
   hangar config set github_projects '{"acme/7": {"trigger": "Ready for agent", "repos": ["acme/api"],
     "statuses": {"started": "In progress", "needs_input": "Blocked", "delivered": "In review"}}}'
   ```

A board mover is trusted when linked to a person or listed in `HANGAR_GITHUB_TRUSTED`; anyone else's job waits for approval on the issue.

## 10. Sandboxes

### 10a. Egress allowlists

Egress stays `open` for people's own work until you say otherwise; intake jobs get at least `allowlist` (below). To proxy an environment's sandboxes ([security/egress.md](../security/egress.md)):

- **Intake posture (#123).** `deploy/k8s/config.yaml` now sets `HANGAR_GATEWAY_URL`, `HANGAR_EGRESS_URL` and `HANGAR_INTAKE_POSTURE: "enforce"`. If your `hangar-site` or database settings leave the gateway off, GitHub, Slack, Jira, Projects and webhook jobs are refused until it is on. Turn it on before you upgrade; the `warn` switch is [gone](../upgrading.md#removed-switches):
  ```bash
  hangar doctor | grep -i intake                   # the gateway, the egress proxy, each intake environment's mode
  ```
  Intake jobs in an `open` environment cold-start (warm sandboxes there are on the open network). For warm starts, map intake repos to an environment with `egress: {mode: allowlist}` and its own `warm_pool` ([security/front-doors.md](../security/front-doors.md#intake-posture)). On a laptop with Docker Desktop, proxied sandboxes can't start, so intake work needs Linux or GKE.

- **GKE: `HANGAR_EGRESS_URL` (required** before any environment uses `allowlist` or `none`; without it such sandboxes fail to start, saying so). Step 5 already applied the Service port and the NetworkPolicies:
  ```bash
  hangar config set HANGAR_EGRESS_URL http://hangar-gateway.hangar.svc:3128   # or in hangar-site; restart
  kubectl -n hangar rollout restart deploy/hangar
  ```
- **Docker on a Linux host (required** for proxied environments there): Docker Engine 26.0.0 or later (or 25.0.4, 23.0.11), since older engines forward DNS out of internal networks (CVE-2024-29018) and hangar refuses them; a host firewall that drops input must accept TCP 3128 and 8788 from `hangar1`. E2B and Docker Desktop can't enforce egress, so proxied environments don't start there.
- **Per environment (admin):** `egress: {mode: allowlist, presets: [packages], allow: [docs.python.org]}`. A repo policy can add hosts and tighten the mode (`hangar repo set acme/app --egress-mode none`).
- **`HANGAR_EGRESS_DEFAULT` (optional):** `allowlist` makes every environment that doesn't set a mode proxied. Turn it on only after the URL above works, and after checking what each environment's agents need.
- **Package mirrors (#107).** With `HANGAR_GATEWAY_URL` set, proxied sandboxes read PyPI, npm and Go through `/pkg/…` on the gateway ([security/package-mirrors.md](../security/package-mirrors.md)); nothing to do on Docker, where hangar caches files on the data volume (`HANGAR_PACKAGE_MIRROR_CACHE_MB`, 1024). On GKE, optionally put Artifact Registry remote repositories behind it, so the cluster pulls from Google's cache:
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
  Keep Go on the public proxy unless an AR Go repository's fetches from origins are acceptable (security/README.md explains). Proxied environments in `credential_mode: direct` that clone from GitHub need `presets: [packages, github]` now. For the read-only GitHub API through the gateway, give the GitHub App *Actions*, *Checks* and *Issues*: Read (without them it falls back to the push token, reads only).
- **Browsers (#107):** rebuild computer environments' images (`hangar env build NAME`): Chromium sends GREASE ECH, which the proxy refuses, and the new image turns ECH off by policy.

Agents ask for a missing host with `hangar-agent env request egress HOST`, which waits for you in `hangar requests` and the inbox.

### 10b. Short-lived cloud credentials

**Optional**; required for environments with `cloud:` ([security/credentials.md](../security/credentials.md#short-lived-cloud-credentials)). Set `HANGAR_OIDC_ISSUER` if `HANGAR_PUBLIC_URL` isn't the https URL to use (AWS fetches `<issuer>/.well-known/openid-configuration` itself, so for AWS it must be public), then trust it in the cloud: a GCP workload identity pool with the JWKS from `/oidc/jwks.json` uploaded, granting `principal://…/subject/hangar:env:<env>`; or an AWS role whose trust policy checks `:sub`. Re-upload the JWKS after each `POST /api/cloud/rotate`. `lifetime_minutes` is now at most 60 (tokens are renewed while the sandbox runs); lower any environment that sets more. Where the cloud fetches the JWKS itself, set `HANGAR_OIDC_KEY_ROTATION_DAYS` (90, say) to rotate the signing key on a schedule; where it is uploaded, upload it again after each `cloud.key.next` audit entry.

### 10c. Environments that live with the repo

Nothing to set: a repo opts in by committing `.hangar/environment.yaml` ([environments.md](../environments.md)).

- **Build secrets (required** for a repo whose file names `build_secrets`; its builds fail, naming the id, until mapped): `build_secrets: {NPM_TOKEN: npm-ci-token}` on the environment or the repo policy (admin), and preferably `hangar cred exposure npm-ci-token control_plane`.
- **Service images (optional):** `HANGAR_ENV_SERVICE_IMAGES` lists what `services:` may run (Docker's official images by default); `HANGAR_ENV_SERVICES_MAX_CPUS` and `_MAX_MEMORY_GB` (2 and 2) bound them per sandbox.

### 10d. Preview ports

**Optional** (off until set). A registrable domain of its own, not a subdomain of your company's or hangar's, with wildcard DNS pointing at hangar, and on GKE a wildcard certificate for `*.<domain>` on hangar's ingress and `deploy/k8s/preview-network.yaml`'s `allow-hangar-sandbox-ports` NetworkPolicy:

```bash
hangar config set HANGAR_PREVIEW_DOMAIN hangar-preview.example.net    # restart
```

### 10e. The agent's computer

Nothing to set: an environment opts in with `computer: true` (its image gets a layer). **Optional:** `HANGAR_PR_MEDIA_LINKS=signed` embeds screenshots and demos in PRs through expiring links anyone holding them can open; leave it `off` for private repos.

### 10f. The heavy tier

**Optional** (off until a `heavy` tier is configured; [environments.md](../environments/heavy-tier.md)). Docker inside a VM of the sandbox's own, and the `large` and `xlarge` profiles.

- **The admission policy (applied by step 5, whether you use the tier or not).** `kubectl apply -k deploy/k8s` now also applies `heavy-policy.yaml`: ValidatingAdmissionPolicies (Kubernetes 1.30 or later) that refuse, in `hangar-sandboxes`, privileged containers and added capabilities unless the pod runs under a Kata runtime class, and host namespaces, `hostPath` volumes and `hostPort` under any runtime; and, cluster-wide, a RuntimeClass named `kata*` whose handler isn't `kata*`. hangar's own sandbox pods use none of these (heavy ones are privileged under Kata), so nothing changes for them. If anything else runs pods in `hangar-sandboxes`, check them first with `kubectl apply --dry-run=server`.
- **The tier (required for it):** the heavy pool from step 4, then Kata on it and the tier in `hangar-site` ([gke.md §4a](../gke.md#4a-the-heavy-tier-optional)), or E2B or Cube microVMs ([microvm.md](../microvm.md#the-heavy-tier-on-microvms)):
  ```bash
  KATA=1 bash deploy/gke/up.sh     # kata-deploy on the Kata and heavy pools
  hangar config set HANGAR_TIERS '{"isolated": ["kubernetes"], "heavy": ["kubernetes"]}'   # keep your other tiers; restart
  ```
  `HANGAR_HEAVY_RUNTIME_CLASS` (`kata-qemu-runtime-rs`) and `HANGAR_HEAVY_NODE_SELECTOR` (`true`) fit that setup. **On a cluster with GKE's Agent Sandbox add-on (required there):** its policy refuses privileged Kata pods, so run the open-source Agent Sandbox controller instead of the add-on, or put the tier on microVMs (`HANGAR_TIERS='{"heavy": ["e2b"]}'`); [gke.md §4a](../gke.md#4a-the-heavy-tier-optional) has the trade-offs.
- **Which repos get Docker (required per repo that asks): deny by default.** A heavy environment (an admin's `heavy: true` or `tier: heavy`) has it. A repo's own `.hangar/environment.yaml` asking for it (`heavy: true`) is only honored where you allow it, and otherwise runs on the default tier with the agent told why and how you enable it:
  ```bash
  hangar repo set acme/app --heavy allow       # the repo's file may ask for Docker; `on`: all its sandboxes have it
  hangar repo set acme/app --max-profile large # what a job or the repo's file may ask for
  ```
- **Docker Hub through hangar (optional).** With the remote repository from step 4: `HANGAR_HEAVY_MIRROR=true` and `HANGAR_HEAVY_MIRROR_UPSTREAM` set to terraform's `dockerhub_mirror` output, so heavy sandboxes pull through hangar's read-only mirror on the gateway. `HANGAR_HEAVY_REGISTRY_MIRRORS='["https://mirror.gcr.io"]'` adds others.
- **Caps and cost (optional).** `HANGAR_HEAVY_PERSON_MAX` (3 `large` or larger and 1 `xlarge` per person; work no person owns shares one bucket with the same caps), `HANGAR_HEAVY_WARM_POOLS` (off: a warm heavy node costs; [team-scale.md](../team-scale.md#heavy-tier)).
- **Check it:** `bash scripts/acceptance/gke.sh --only heavy` (a Compose Postgres and a Testcontainers test, and no metadata server reachable from inside the VM).

### 10g. Build caches

On by default; **`HANGAR_GATEWAY_URL` is required** for them (sandboxes reach `/cache` on the gateway's port with its grant), which every gateway-mode install already sets. Nothing else to do on GKE: objects go to the session bucket's `caches/` prefix, which hangar's service account can already write ([caches.md](../caches.md)).

- **A bucket of its own (optional):** create one, give hangar's service account `roles/storage.objectAdmin` on it (it writes, reads and evicts), then `hangar config set HANGAR_BUILD_CACHE_STORE gs://<bucket>` and restart. Objects are sealed with the vault key, as checkpoints are.
- **Sizes (optional):** `HANGAR_BUILD_CACHE_MAX_GB` (100 in a bucket, 10 on Docker's data volume, which holds the database too: size it), `HANGAR_BUILD_CACHE_REPO_MAX_GB` (20), `HANGAR_BUILD_CACHE_RETENTION_DAYS` (14). `hangar status` shows the sizes; `hangar admin caches purge` empties a repo's.
- **Off:** `HANGAR_BUILD_CACHES=false` (restart).

### 10h. Test results

**Optional**, nothing to set: every check now leaves structured results ([jobs.md §8](../jobs/test-results.md)). Where a runner writes its report somewhere hangar doesn't look, name it in the repo's file or policy (`hangar repo set acme/app --verify-report 'build/**/TEST-*.xml'`), and `--rerun-flaky` lets a check that failed only on the repo's known-flaky tests run once more. `HANGAR_TEST_REPORTS=collect` stops hangar setting the reporter variables; `off` stops it reading reports too.

## 11. Models, the canary and environment setup

### 11a. Model list keys

**Optional.** `frontier` and `balanced` resolve from each provider's live model list. Without a key for a provider hangar uses the last id it resolved, else a built-in one (`balanced`: `claude-opus-5-5`, `gpt-6.1-sol`; `frontier`: `claude-fable-5-1`, `gpt-6-astra`), and the session says so. To list with your accounts (the keys are control-plane credentials: they never reach a sandbox):

```bash
hangar cred add anthropic-models --harness webhook --kind generic   # an Anthropic API key (Ctrl-D)
hangar cred add openai-models --harness webhook --kind generic
hangar config set HANGAR_MODEL_LIST_CREDENTIALS '{"anthropic": "anthropic-models", "openai": "openai-models"}'
hangar env list                                                      # each harness's model, its policy and list price
```

Env setup (`hangar env setup`) uses the same lists; it used to list with the team's harness API keys. [harnesses.md](../harnesses.md#models-the-latest-strong-model-per-provider) has how each provider's strongest model is picked.

### 11b. The harness canary

On **hangar's own repository**, not the repos agents work on ([harnesses.md](../harnesses.md#setup)). The contract tests (`harness-contract.yml`) run on their own; the nightly canary (`harness-canary.yml`, 03:17 ET) opens a lock-bump PR that lands on one approval (`harness-canary-land.yml`).

1. **Branch protection on `main` (required** before the first bump lands): a required approval, dismiss stale approvals, and **`contract` as a required check**; and under Settings → General, **Allow auto-merge**. Without required approvals, auto-merge would merge a bump nobody approved.
2. **A dedicated GitHub App (optional, recommended):** Contents and Pull requests: Read and write, no Workflows permission, installed on this repository only; not hangar's own App. Store it as repository secrets `CANARY_APP_CLIENT_ID` and `CANARY_APP_PRIVATE_KEY`. Without it the bump PR comes from the workflow token, so no checks run on it (close and reopen it) and `images` doesn't run on its merge (run it by hand).
3. **`CANARY_APPROVERS` (optional):** a repository variable with the logins that may approve a bump; the repository's owner otherwise.

### 11c. Agent-led environment setup

**Optional**, nothing to set. `hangar env setup acme/app [--wait | --drift | --every CRON]` (or `POST /api/env/setup`, or an issue labelled `hangar-env-setup` on GitHub, Jira or GitHub Projects) runs a goal agent and a setup agent on `balanced`, validates the `.hangar/environment.yaml` they write in a clean sandbox with no secrets, and opens a PR through the landing path ([environments.md](../environments/agent-led-setup.md)). Its budget is the job's, else the repo policy's, else `HANGAR_ENV_SETUP_MAX_COST_USD` ($30) and `_MAX_TURNS` (16); `HANGAR_ENV_SETUP_MODELS` pins a model or policy per harness.

### 11d. The sister tool's interfaces

**Optional**, nothing to set ([spec 0014](../specs/0014-sister-tool-interfaces.md)). Harness manifests (`hangar session|job|env manifest`), trace export (`hangar trace export`, `GET /api/jobs/{id}/trace`) and experiment jobs (`hangar experiment run`) work as soon as it starts, owner-scoped like the rest. `HANGAR_TRACE_LIVE_TOOLS=true` also sends each turn's tool spans to the configured OpenTelemetry collector; `HANGAR_EXPERIMENT_MAX_TRIALS` (200) and `HANGAR_EXPERIMENT_PARALLEL` (8) bound an experiment.

