# Upgrading a running hangar

Two guides, newest first:

- [**This release**](#upgrading-to-the-team-release-7590) (#75–#90): one hangar for a team of six at ~60 sessions, people and per-person credentials, front-door defaults and job budgets, the reconciler, the MCP deadline contract, and images built before anyone waits.
- [**The overnight stack**](#upgrading-from-before-the-overnight-stack-4869) (#48–#69), for a hangar older than that. Do its backup and read its defaults first, then follow this release's procedure: its prebuild also covers the overnight guide's "republish the Codex pack" step.

Both are written for the GKE install from [gke.md](gke.md): one zonal cluster, gVisor sandboxes, the control plane with SQLite on the `hangar-data` PVC, checkpoints in the session bucket, site settings in the `hangar-site` config map, and `deploy/gke/up.sh` / `power.sh`. [CHANGELOG.md](../CHANGELOG.md) lists what each adds.

## Upgrading to the team release (#75–#90)

The short version, in order:

```bash
hangar admin backup                                   # 1. back up; record the running image
#                                                       2. request quotas (days ahead)
uv run hangar images build --registry "$REG"          # 3. prebuild the new image and pack tags
#                                                       4. in the power-down window: terraform apply (the node pool)
bash deploy/gke/power.sh up && bash deploy/gke/up.sh  # 5. bring the cluster up, roll out
hangar doctor                                         # 6. check
hangar cred migration                                 # 7. people: your logins became yours; add team credentials
hangar person add alice
hangar config set HANGAR_BUDGET_DAY_TIMEZONE America/New_York   # 8. budgets, then job concurrency
bash scripts/acceptance/gke.sh                        # 9. prove it
```

### What changes as soon as it starts

To opt out, use `hangar config set NAME VALUE` (kept in hangar's database) or `hangar-site` (wins over the database). Settings marked *restart* apply after `kubectl -n hangar rollout restart deploy/hangar`.

| What | Before → now | Opt out |
| --- | --- | --- |
| **The store** (#89) | every lookup scanned every sandbox record ever made → expression indexes on state and name, and stopped or failed sandbox records idle for 7 days move to a `sandboxes_archive` table (still found by id; deleted after 180 days; never while a snapshot or a credential lease points at one). Both happen on the first start, in the background, 100 rows at a time. | `HANGAR_SANDBOX_RETENTION_DAYS=0`, `HANGAR_SANDBOX_ARCHIVE_DAYS=0` |
| **Fleet size and caps** (#89, #90) | `HANGAR_MAX_SANDBOXES` 50 → **120**. Per person: 15 live sessions, 20 sandboxes started directly, half the job slots (at least 4) running jobs; a cap reached is a 429 or an MCP error. Caps count the person hangar records on the work, so all of a person's tokens share them ([team-scale.md](team-scale.md#settings)). | `HANGAR_PERSON_MAX_SESSIONS`, `_MAX_SANDBOXES`, `_MAX_RUNNING_JOBS` (0 = off) |
| **Job concurrency** (#89, #90) | 4 → `HANGAR_JOB_CONCURRENCY=0` (auto): 4 on Docker, **8 on GKE**. A free slot goes to whoever has the fewest jobs running. Step 8 raises it to 24 once budgets are set. | set a number (*restart*) |
| **Sandbox CPU** (#89) | each sandbox pod requested its whole CPU limit → half of it (`HANGAR_K8S_CPU_REQUEST_FRACTION=0.5`); the limit and all memory stay reserved. Each environment's warm-pool template is rewritten once, so the first create of each after the rollout is cold. The control plane requests `500m`/`1Gi` (limit `3Gi`). | `HANGAR_K8S_CPU_REQUEST_FRACTION=1` (*restart*) |
| **People** (#88) | every token was root over everyone's work → every token belongs to a person. Existing tokens, the admin token and the bootstrap token belong to **`owner`**, an admin, so nothing changes until you add someone (step 7). Credentials stored before this release become **the owner's own** on the first start (control-plane ones excepted): jobs no person owns need a team credential (step 7). | `hangar cred owner NAME team` |
| **Control-plane credentials** (#81) | an `mcp` token could map the webhook secret or bot token into a sandbox → signing keys, GitHub App keys, Slack secrets and the GitHub tokens the settings name never reach a sandbox. A direct-mode environment that attaches the same GitHub token as `HANGAR_GITHUB_TOKEN_CREDENTIAL` or `HANGAR_GATEWAY_CREDENTIAL` now fails to start; `hangar doctor` names it (step 6). Saving an environment that sets `secrets`, `image`, `base`, `runtime_class` or `credential_mode: direct` needs an admin. | `hangar cred exposure <name> sandbox` |
| **Front-door defaults** (#82) | jobs from GitHub issues, Slack, A2A and webhooks had no `verify` → they take the repo's [policy](repo-policy.md) (`hangar repo set`), then the environment's new `verify:` field. **`envs/hangar.yaml` (and `hangar-jobs`) now sets `verify: bash scripts/ci/verify.sh`**. A standing check also runs once per base commit before the agent starts; on a red base only new failures count. | `hangar repo set owner/repo --verify ""`, or `verify: ""` in the environment |
| **Agent scratch files** (#82) | delivery committed everything the agent left, including the `HANDOFF.md` the `repo-conventions` skill asked for → the skill no longer asks for it, and new root `HANDOFF.md`, `RESULT.md` and `NOTES.md` files and `*.orig`/`*.rej` leftovers beside their source stay out of PRs unless the task names them. Images with the skill rebuild once, since its layer changed. | name the file in the task |
| **Job budgets and daily caps** (#85) | none → `job_budget` caps all of a job's turns together, and a repo's `max_usd_per_day` or `HANGAR_PERSON_MAX_USD_PER_DAY` holds new jobs for approval. **All unset by default**, and a day starts at UTC midnight (20:00 in New York) until `HANGAR_BUDGET_DAY_TIMEZONE` is set (step 8). | — |
| **The reconciler** (#84) | rows a restart left `starting` or `stopping` stayed so, with their leases → a pass at startup and every `HANGAR_RECONCILE_INTERVAL_SECONDS` (30) finishes them; a GKE pod recreated blank under its claim counts as gone and its session resumes from its checkpoint. Sandboxes from before adopt their current pod's UID. It assumes one hangar process per database (`replicas: 1`, `strategy: Recreate`, as shipped). | — |
| **Interrupted jobs** (#84) | a job caught by a restart or the power-down failed as `interrupted` → it resumes from its session's checkpoint, at most twice, 20 s apart. | `HANGAR_JOB_RESTART_RESUMES=0` |
| **GitHub redelivery** (#84) | deliveries that failed while hangar was down were lost → at each start hangar asks GitHub to redeliver them (up to 3 days back). The stored GitHub token needs *Webhooks: Read and write*, as `hangar setup github` already asks. | `HANGAR_GITHUB_REDELIVER_ON_START=false` |
| **`power.sh down`** (#84, #90) | stopped hangar after 10 minutes → it drains first (`POST /api/admin/drain`), checkpoints running turns and waits for creates and destroys in flight. `power.sh up` keeps an autoscaling range set while the cluster was down. | — |
| **MCP waits** (#86) | `wait_for_turn` waited up to 300 s by default (600 at most), `exec` as long as its `timeout`, `process` `attach` up to 600 s, `terminal_send` up to its `wait_ms` → each at most `HANGAR_MCP_TOOL_WAIT_SECONDS` (150 s), so claude.ai (which gives up after about 230 s) gets an answer: `wait_for_turn` says `done: false`, an `exec` that runs into the cap points at `process`. Destroy, pause, resume, extend, snapshot, `fork_session` and `run_agent` answer within it too and finish in the background. Any other tool still waiting just past it (165 s) gets an error. | Raise `HANGAR_MCP_TOOL_WAIT_SECONDS` only if all your clients wait longer than claude.ai |
| **Tool-list notice** (#86) | `serverInfo.version` was `0.1.0` → `<version>+tools.<hash>`. A claude.ai connector whose tool list is older than the server's gets a one-line note (at most hourly) asking to remove and re-add the connector (step 9). | — |
| **Harness versions and packs** (#83, #87) | Claude Code and Antigravity followed "latest" → `src/hangar/harnesses.lock.yaml` pins every harness (Claude Code 2.1.296, Codex 0.162.1, Antigravity 1.3.3, pi 1.1.0, Gemini 0.64.0); a pack build fails unless the CLI reports the locked version. Packs hold only the vendor's files and are mounted by digest. **Every pack tag changes once**, and so does every image that bakes a harness in (step 3). `hangar pack publish` no longer overwrites an existing tag without `--force`. | — |

### 1. Back up

```bash
kubectl -n hangar get deploy hangar -o jsonpath='{.spec.template.spec.containers[0].image}' | tee ~/.hangar/pre-upgrade-image
hangar admin backup && hangar admin backups      # encrypted with the vault key, into the session store
```

Keep the vault key outside the cluster too ([§1 of the overnight guide](#1-back-up-first) shows how). The previous version runs on the upgraded database file (see [rolling back](#rolling-back-this-release)), so the backup is for a disaster, not for the rollback.

### 2. Request quotas, days ahead

The sandbox pool moves to `n2d-standard-16` (AMD keeps SMT on under GKE Sandbox, so 14 sandboxes fit a node) with up to 10 nodes ([team-scale.md](team-scale.md#how-many-nodes)). In *IAM & Admin → Quotas* for your region request `N2D_CPUS` ≥ 160, `CPUS_ALL_REGIONS` to match, and `SSD_TOTAL_GB` ≥ 2,250. Spot VMs take standard `N2D_CPUS` quota unless the region granted preemptible quota. A refused scale-up shows as sandboxes stuck "waiting for a node", and `hangar doctor` names the quota.

### 3. Prebuild the new image and pack tags

Before the rollout, so no create waits on a Cloud Build. This talks only to Artifact Registry, so it works while the cluster is powered down:

```bash
REG=$(terraform -chdir=deploy/terraform/gke output -raw registry)
uv run hangar images plan --registry "$REG"    # every tag the commit needs; exit 3 = something to build
uv run hangar images build --registry "$REG"   # Docker here (linux/amd64), else Cloud Build
uv run hangar images plan --registry "$REG"    # exit 0: nothing missing
```

If the cluster sets `HANGAR_IMAGE_PREFIX`, `HANGAR_SANDBOX_USER`, `HANGAR_WORKSPACE_DIR`, `HANGAR_EXTRA_CA_BUNDLE` or `HANGAR_K8S_IMAGE_VOLUMES` in `hangar-site` or its database, export the same values first: they change the tags. With the images workflow's secrets set ([gke.md](gke.md#images-and-packs-built-in-ci)), the push to `main` does this for you and the run's summary lists each tag. `up.sh` runs the same build before it touches the cluster (`PREBUILD=0` skips it). If nothing prebuilt them, nothing breaks: running sandboxes keep the pack they mounted, the new control plane rebuilds the packs in the background, and the first sandbox for a harness whose pack isn't built yet waits for it.

### 4. In the power-down window: the node pool

After `power.sh down` has finished (it runs at 01:00 ET; the cluster is down until 10:00), so no sandbox is on the pool:

```bash
cd deploy/terraform/gke
terraform plan    # expect "~ update in-place" on google_container_node_pool.sandboxes
terraform apply
```

`machine_type` is an in-place node pool update with the google provider ≥ 5.x (`versions.tf` asks for ≥ 6.0): GKE replaces the nodes, not the pool, and with the pool at zero nodes that is quick. **If the plan shows the pool destroyed and created (`-/+`), stop**: something else in it changed (`sandbox_spot` forces a new pool). To keep the old pool, set `sandbox_machine_type` and `sandbox_max_nodes` in your tfvars. Done outside the window, every sandbox on the pool is killed, and GKE needs a node's worth of spare quota to surge.

### 5. Bring the cluster up and roll out

```bash
bash deploy/gke/power.sh up     # keeps the 0-10 range the apply set, instead of the 0-4 saved at down
bash deploy/gke/up.sh           # prebuild (finds nothing missing), then the rollout
```

Do this before the team starts, or let the 10:00 schedule run `power.sh up` and deploy right after. The Deployment uses `Recreate`, so the URL answers 502 until the new pod is ready. On its first start hangar creates the store's indexes and archive table (20,000 records index in well under a second), runs a reconciler pass before sessions and jobs look at their sandboxes, resumes jobs the power-down interrupted, and asks GitHub to redeliver what it missed overnight.

### 6. Check

```bash
hangar doctor
```

It names environments that attach the control plane's own GitHub token in direct mode (fix with `hangar cred exposure <name> sandbox`, or switch them to the gateway), mapped repos whose jobs would run with no check (`hangar repo set <repo> --verify "…"`), the webhook's events and quota errors.

### 7. People: your logins stay yours; add team credentials, then the team

Every existing token now belongs to `owner` (you, an admin), and records from before have no person, so only admins see them. **Every credential stored before this release became yours on the first start**, once, so your Claude Max or ChatGPT login never goes into a member's sandbox. Control-plane credentials (signing keys, bot tokens, hangar's own GitHub token) stayed the control plane's. That also means jobs no person owns (GitHub and Slack senders you haven't linked, webhooks, schedules) no longer get your login: give them a team credential, and link yourself so your own issues run as you:

```bash
hangar cred migration                             # what became yours
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt   # an API key
hangar cred owner gh-jobs team                    # an API key or git token of yours the team may use
hangar person link owner github <your-login>      # your GitHub issues and comments run as you, with your login
hangar doctor                                     # "team credentials for jobs" names what still has none
```

The full guide, including what the vendors allow and Codex logins, is [team.md](team.md).

Then add each engineer:

```bash
hangar person add alice --name "Alice"            # prints her login token once, and lists the team credentials she will get
hangar person link alice github alice-gh          # her GitHub issues and comments run as her (also: slack, jira, email)
```

She runs `hangar login <url>` with it, `hangar connect` for her MCP clients, and `hangar cred add` for her own Claude or Codex login, which then goes only into her sandboxes. Members see and act on their own sandboxes, sessions and jobs; admins on everyone's ([security.md](security.md#people)). The per-person caps count her as one person across all her tokens.

### 8. Budgets, then job concurrency

Job slots stay at 8 on GKE because nothing bounds what a job spends until budgets are set. Set them, then raise the slots to the measured 24:

```bash
hangar config set HANGAR_BUDGET_DAY_TIMEZONE America/New_York     # a day starts at local midnight
hangar config set HANGAR_JOB_BUDGET_MAX_COST_USD 10                # every job's turns together (an example)
hangar config set HANGAR_PERSON_MAX_USD_PER_DAY 150                # per person, all their tokens (an example)
hangar repo set acme/app --max-usd-per-day 60 --verify "uv run pytest -q"
hangar config set HANGAR_JOB_CONCURRENCY 24                        # restart
kubectl -n hangar rollout restart deploy/hangar
```

A slot is a job, not a sandbox: 24 best-of-4 jobs could hold all 120 sandboxes, so keep `HANGAR_PERSON_MAX_RUNNING_JOBS` (half the slots) and the budgets in place ([team-scale.md](team-scale.md#limits-and-follow-ups)).

### 9. Clients and proof

- **claude.ai users remove the hangar connector and add it again** (Settings → Connectors): the apps never refetch the tool list ([clients.md](clients.md)). CLIs refetch it when a session starts.
- `bash scripts/acceptance/gke.sh` runs the checks from the overnight guide's [§5](#5-prove-it-on-your-cluster) plus `mcp`: `create_sandbox` over MCP on a multi-harness environment (`--mcp-env`, default `python`) answers within claude.ai's 230 s, a second create whose client gives up after 5 s still comes up, then `exec`. It prints the server's version with its tool-list hash.
- `uv run hangar images plan --registry "$REG"` exits 0, and `hangar pack list` shows each pack's locked and installed version.

### Rolling back this release

In this order:

1. **Remove members first** (`hangar person rm <id>`, which revokes their tokens). The previous version has no people: every `mcp` token would see and act on everyone's work, and `user` tokens are refused.
2. **Hand archived sandbox records back.** The previous version runs on the upgraded file (it ignores the archive table and keeps the indexes up to date), but it can't see archived records:
   ```bash
   kubectl -n hangar exec deploy/hangar -- python3 -c "import sqlite3; db = sqlite3.connect('/data/hangar.db'); \
   db.execute('INSERT OR IGNORE INTO sandboxes(key, data) SELECT key, data FROM sandboxes_archive'); db.commit()"
   ```
3. **Set `HANGAR_JOB_CONCURRENCY` back to 4** if you raised it: the previous version has no job budgets or daily caps.
4. **Suspend live sessions** (as `power.sh down` does), or let running turns finish.
5. **Put the previous image back:**
   ```bash
   kubectl -n hangar set image deploy/hangar hangar="$(cat ~/.hangar/pre-upgrade-image)"
   kubectl -n hangar rollout status deploy/hangar
   ```

What stays: the node pool (the previous version runs on it unchanged), the database, every session and checkpoint, and the old pack tags in Artifact Registry, which the previous version mounts by tag as before. What the previous version ignores: repo policies, budgets, `person` on records, `packinfo` digests, the drain (the new `power.sh` treats its 404 as settled) and the stale-tool-list records. GitHub replay marks are compatible both ways. The next upgrade archives the records again.

## Upgrading from before the overnight stack (#48–#69)

This guide takes a hangar that runs the version before the overnight stack (main at `2e3e0a3`, "Merge pull request #43") to the top of the stack, PRs #48–#69. From there, continue with [this release](#upgrading-to-the-team-release-7590). It is written for the GKE install from [gke.md](gke.md): one zonal cluster, gVisor sandboxes, the control plane with SQLite on the `hangar-data` PVC, checkpoints in the session bucket, site settings in the `hangar-site` config map, and `deploy/gke/up.sh` / `power.sh`. [CHANGELOG.md](../CHANGELOG.md) lists what the stack adds.

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

**Save the vault key** if it isn't already somewhere outside the cluster. Every credential, every checkpoint and every backup is encrypted with it:

```bash
( umask 077; kubectl -n hangar get secret hangar -o jsonpath='{.data.HANGAR_SECRET_KEY}' | base64 -d > ~/.hangar/vault-key )
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

The script never runs `power.sh`. To check power down and up: `bash deploy/gke/power.sh down`, then `power.sh up`, then run the script again. Everything the script creates is named `accept-<run>` and is removed at the end, also after a failure or Ctrl-C (`--keep` leaves it). `tests/test_acceptance_script.py` runs the script against a hangar on Docker, with GitHub replaced by a local git server behind the gateway. Every check but `k8s` and `steer` runs there for real, `model` with a scripted agent in place of Claude. `k8s`, `steer` and the in-cluster exec measurement run against stand-ins: a fake kubectl, a fake API, and the Docker provider in place of the Kubernetes one.

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
