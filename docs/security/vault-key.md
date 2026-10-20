# The vault key ring

The key ring that encrypts everything hangar keeps secret at rest, where it lives, and how to rotate it. Part of the [security model](README.md).

One key ring encrypts what hangar keeps secret at rest: the vault (stored credentials), every checkpoint and artifact in the session store, every database backup, and a few rows in the database (live turns' control keys, E2B sandboxes' envVars). Tokens hangar derives per sandbox or per link come from it too: envd's, the egress proxy's credential, preview tickets and sessions, E2B access tokens, signed media links and process tags. `HANGAR_SECRET_KEY` (or `<data dir>/secret.key` when it isn't set) holds the keys, **newest first**, separated by commas (lines, in the file). The newest encrypts and signs; every key hangar hasn't retired decrypts and checks ([`MultiFernet`](https://cryptography.io/en/latest/fernet/#cryptography.fernet.MultiFernet)).

## Where the keys live

- **Secret Manager is where the operator keeps them** (one secret, say `hangar-vault-key`, one version per change): versioned, access-logged, and outside the cluster, so it survives losing it. The `hangar` Kubernetes Secret holds a copy for the pod.
- **Never in a file on a laptop.** The commands below pipe a key from one service to the other; nothing is written to disk. A backup can't be restored without the key it was made with, which is why the key must live outside the cluster, and why it belongs in Secret Manager rather than in someone's home directory.
- If it is only in the Kubernetes Secret today (what `up.sh` does), copy it to Secret Manager once:
  ```bash
  kubectl -n hangar get secret hangar -o jsonpath='{.data.HANGAR_SECRET_KEY}' | base64 -d | \
    gcloud secrets create hangar-vault-key --data-file=- --replication-policy=automatic
  ```
- Give the secret's `secretAccessor` role to the operators who rotate keys, and nobody else: whoever reads it reads every checkpoint and backup they can also fetch.

## Rotating the vault key

Rotate on a schedule, when an operator who could read the key leaves, and at once when the key may have leaked. A rotation:

1. **Put a new key first.** In Secret Manager, as a new version holding `<new>,<old>`, then in the `hangar` Secret, then restart:
   ```bash
   NEW=$(openssl rand -base64 32 | tr '+/' '-_')       # a Fernet key: 32 random bytes, url-safe base64
   { printf '%s,' "$NEW"; gcloud secrets versions access latest --secret=hangar-vault-key; } | \
     gcloud secrets versions add hangar-vault-key --data-file=-
   unset NEW
   kubectl -n hangar patch secret hangar --type merge -p "{\"stringData\":{\"HANGAR_SECRET_KEY\":\"$(
     gcloud secrets versions access latest --secret=hangar-vault-key)\"}}"
   kubectl -n hangar rollout restart deploy/hangar
   ```
   From that start, hangar encrypts with the new key and records the rotation as pending. With a key file (no `HANGAR_SECRET_KEY`, a laptop or a single VM), skip this step: the next one writes the new key first in the file.
2. **`hangar admin rotate-key`.** It re-seals the vault and the database's other sealed rows at once (one transaction), and starts a background pass over the session store: every `.enc` object (checkpoint parts, artifacts, backups) the old key sealed is re-encrypted, keeping Fernet's timestamp. Meanwhile, a checkpoint part copied into a new checkpoint is re-encrypted as it's copied rather than carried forward under the old key. The pass records where it is in the database, so after a restart (a deploy, a crash) it carries on from there. `hangar admin rotate-key --status` (or `GET /api/admin/rotate-key`) shows the progress, and `hangar doctor` the phase.
3. **The old key is retired by itself** when the pass is done and no sandbox started before the rotation is still running. Those sandboxes hold tokens of the old key (envd's, the egress proxy's), and they keep working until then. A long-lived one holds retirement back; `hangar admin rotate-key --retire --force` retires anyway, after which such a sandbox's envd falls back to exec through the provider and a proxied one loses the egress proxy (restart its session). Retired, the old key reads nothing: hangar stops using it even while it is still configured. A key file keeps it in `secret.key.retired` instead: hangar doesn't read with it, but a backup made before the rotation needs it (below). Before retiring, the pass waits while the old key isn't configured (`hangar doctor` shows the error): without it, what it sealed can't be re-encrypted.
4. **Remove the old key from the configuration**: a new Secret Manager version with only the new key, the Secret patched as in step 1, a restart. `hangar doctor` warns until then. **Disable** the old versions (`gcloud secrets versions disable`), don't destroy them, until every backup made before the rotation has aged out (`HANGAR_BACKUP_KEEP`, or the backup bucket's lifecycle rule). The pass re-encrypts a backup it can read, but the database inside it still holds the vault sealed with the old key, and a backup in a bucket hangar may only write to (below) isn't re-encrypted at all. Then take a backup (`hangar admin backup`): it is the first that needs only the new key.

**Restoring a backup made before a rotation**: put the old key back in `HANGAR_SECRET_KEY` after the new one (`<new>,<old>`), restore, and restart. The restored database doesn't know the old key was retired, so hangar reads with it and reports a pending rotation: run `hangar admin rotate-key` again. With a key file, nothing to do: hangar reads `secret.key.retired` too, and `hangar admin restore --local` uses it.

hangar keeps only key fingerprints (`key_id`: a hash of the key) in its database and status output. Previews' 12-hour sessions, signed media links and E2B access tokens made with the old key stop working when it is retired; opening the preview link again, or `connect` for an E2B client, gets new ones.

## Backups and the vault key

Backups go into the session store (`_backups/` in the checkpoints' bucket) unless `HANGAR_BACKUP_STORE` names another place. Next to the checkpoints, under hangar's own service account, whoever can read that bucket with that account's token reads every checkpoint and every backup, and every backup holds the vault, all under one key. `hangar doctor` warns about that (`backup isolation`). What it wants instead: a bucket of its own where hangar's service account has only `roles/storage.objectCreator` (it can write backups, not read or delete them), a lifecycle rule that deletes them after the time you keep them, and restores done by an operator, with `hangar admin restore --local` and their own access. hangar then skips pruning (the lifecycle rule keeps the count), its doctor says it can't see the newest backup's age, and a key rotation can't re-encrypt those backups: they keep the key they were made with until they age out, so keep that key's disabled version in Secret Manager until then (as for every backup made before a rotation, above).

