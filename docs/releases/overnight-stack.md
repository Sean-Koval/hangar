# Upgrading from before the overnight stack (#48–#69)

For a hangar older than the overnight stack: back up, what happens to your data, then [the defaults that change](overnight-stack-defaults.md) and [the upgrade itself](overnight-stack-procedure.md). Every release is listed on [release notes](README.md).

This guide takes a hangar that runs the version before the overnight stack (main at `2e3e0a3`, "Merge pull request #43") to the top of the stack, PRs #48–#69. From there, continue with [this release](team-release.md). It is written for the GKE install from [gke.md](../gke.md): one zonal cluster, gVisor sandboxes, the control plane with SQLite on the `hangar-data` PVC, checkpoints in the session bucket, site settings in the `hangar-site` config map, and `deploy/gke/up.sh` / `power.sh`. [CHANGELOG.md](https://github.com/Sean-Koval/hangar/blob/main/CHANGELOG.md) lists what the stack adds.

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

Every claim below was checked against the code, not the PR descriptions. A few things can only be seen on a cluster. [The last section](overnight-stack-procedure.md#what-couldnt-be-checked-without-gke) lists them, and the acceptance script checks them.

## 1. Back up first

`hangar admin backup` arrives with the upgrade, so the old version can't run it. Take one of these before `up.sh`, preferably both.

**Record what's running now**, for a rollback (§6):

```bash
kubectl -n hangar get deploy hangar -o jsonpath='{.spec.template.spec.containers[0].image}' | tee ~/.hangar/pre-upgrade-image
```

**Save the vault key** in Secret Manager if it isn't already somewhere outside the cluster. Every credential, every checkpoint and every backup is encrypted with it. Pipe it straight there; don't write it to a file on your machine ([security/vault-key.md](../security/vault-key.md#where-the-keys-live)):

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

A snapshot of a running hangar is crash-consistent, like a power loss: SQLite recovers it, and may drop the last commits. For a clean one, run `kubectl -n hangar scale deploy/hangar --replicas=0` first. Sandboxes keep running meanwhile, and `up.sh` sets the replicas back to 1. [gke.md §7](../gke.md#7-backups-and-restore) explains how to restore a snapshot.

## 2. What happens to your data

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

