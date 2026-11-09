# Backups and restore on GKE

This page is part of [Running hangar on GKE](../gke.md).

Everything hangar knows is one SQLite file on the `hangar-data` volume: sessions and their checkpoint lists, jobs, schedules, tokens, the vault and the settings `hangar config` stores. Checkpoints themselves are in the bucket. Losing the disk loses the database, so back it up.

**Backups.** With `HANGAR_SESSION_STORE` on a bucket (what `up.sh` sets), hangar backs up its database every 24 hours into `gs://<bucket>/_backups/` and keeps the newest 14. Each backup is taken with SQLite's online backup API in one step (one read transaction: hangar keeps working, and the copy never holds half a write), gzipped, and encrypted with the vault key.

```bash
hangar admin backup        # one now
hangar admin backups       # what's there, newest first
hangar doctor              # "database backups": fails when the newest is more than two intervals old
```

`HANGAR_BACKUP_HOURS` changes the interval (`0` turns it off; unset means 24 with a bucket, off without one) and `HANGAR_BACKUP_KEEP` the count. Both apply without a restart, from the next scheduled run (within an hour when backups were off). Without a bucket, backups go to `<data dir>/sessions/_backups`, which is the same disk: use PD snapshots instead (below).

**Keep the vault key somewhere else.** A backup is useless without the `HANGAR_SECRET_KEY` it was encrypted with, and so is everything in the vault. `up.sh` keeps it in the `hangar` Secret, which dies with the cluster. Keep it in Secret Manager, piped straight there, not in a file on your machine; [security/vault-key.md](../security/vault-key.md) has how to rotate it:

```bash
kubectl -n hangar get secret hangar -o jsonpath='{.data.HANGAR_SECRET_KEY}' | base64 -d | \
  gcloud secrets create hangar-vault-key --data-file=- --replication-policy=automatic
```

**Restore.** A restore is staged, then applied by the next start, before anything opens the database. The database it replaces is kept beside it as `hangar.db.before-restore-<time>`.

```bash
hangar admin restore latest          # or a name from `hangar admin backups`
kubectl -n hangar rollout restart deploy/hangar
```

If hangar can't start at all, stage the restore from a one-off pod with the same image, settings and volume:

```bash
kubectl -n hangar scale deploy/hangar --replicas=0
IMAGE=$(kubectl -n hangar get deploy/hangar -o jsonpath='{.spec.template.spec.containers[0].image}')
kubectl -n hangar run hangar-restore --rm -i --restart=Never --image="$IMAGE" --overrides='{"spec": {
  "serviceAccountName": "hangar", "enableServiceLinks": false, "securityContext": {"runAsNonRoot": true, "runAsUser": 1000,
    "runAsGroup": 1000, "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
  "containers": [{"name": "hangar-restore", "image": "'"$IMAGE"'",
    "command": ["hangar", "admin", "restore", "--local", "latest"],
    "securityContext": {"allowPrivilegeEscalation": false, "readOnlyRootFilesystem": true,
      "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
    "envFrom": [{"configMapRef": {"name": "hangar"}}, {"configMapRef": {"name": "hangar-site", "optional": true}},
                {"secretRef": {"name": "hangar"}}],
    "volumeMounts": [{"name": "data", "mountPath": "/data"}, {"name": "tmp", "mountPath": "/tmp"},
                     {"name": "home", "mountPath": "/home/hangar"}]}],
  "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "hangar-data"}},
              {"name": "tmp", "emptyDir": {"sizeLimit": "4Gi"}}, {"name": "home", "emptyDir": {"sizeLimit": "256Mi"}}]}}'
kubectl -n hangar scale deploy/hangar --replicas=1
```

After a restore:

- Tokens and credentials are as they were at the backup. A token revoked since then works again: revoke it again, or rotate them all.
- Jobs and turns that were running at the backup restart or are marked interrupted, as after any restart.
- A session restores from the checkpoints the backup lists. hangar keeps 5 per session (`HANGAR_SESSION_KEEP_CHECKPOINTS`), so a session that checkpointed more than 5 times after the backup has none of those left in the bucket and can't resume. Restore from a recent backup.

**PD snapshots** are the other option, or a second layer: they copy the whole volume (database, local checkpoints, logs) and don't need the vault key to restore the disk. A snapshot of a running hangar is crash-consistent: SQLite recovers it like a power loss, and may drop the last second of commits. Schedule them on the volume's disk:

```bash
PV=$(kubectl -n hangar get pvc hangar-data -o jsonpath='{.spec.volumeName}')
kubectl get pv "$PV" -o jsonpath='{.spec.csi.volumeHandle}'   # projects/<p>/zones/<zone>/disks/<disk>
gcloud compute resource-policies create snapshot-schedule hangar-data-daily --region <region> \
  --daily-schedule --start-time 04:00 --max-retention-days 14 --on-source-disk-delete keep-auto-snapshots
gcloud compute disks add-resource-policies <disk> --zone <zone> --resource-policies hangar-data-daily
```

To restore one, create a disk from the snapshot, then a PV and PVC for it (or swap it in for `hangar-data`), with hangar scaled to 0.
