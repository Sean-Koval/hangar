# Backups and recovery objectives

This page answers one question for an admin: what holds hangar's state, how each store is backed up, and how much
can be lost. The steps are in [Backups and restore on GKE](../gke/backups.md). The drills that prove them are in
[Recovery drills](drills.md).

## What holds state

| Store | What is in it | Backed up by | Kept | Encrypted | Where |
| --- | --- | --- | --- | --- | --- |
| The database (`hangar.db`) | Jobs, sessions and their checkpoint lists, schedules, tokens, people, the vault, change-set profiles, settings, the audit table | hangar itself, every `HANGAR_BACKUP_HOURS` (24) | The newest `HANGAR_BACKUP_KEEP` (14) | Vault key, then gzip | `_backups/` in the session bucket, or `HANGAR_BACKUP_STORE` |
| The `hangar-data` volume | The database, local checkpoints and logs | PD snapshots, if scheduled | Per the snapshot schedule | Google's at-rest encryption | The project's snapshots |
| Session checkpoints | Each session's saved work | Not copied: the bucket is the only copy | 5 per session; GCS soft delete covers a deletion for 7 days | Google's at-rest encryption | `<project>-hangar-sessions` |
| Run capture | Each finished run, for flightdeck | Not copied: hangar can't delete or overwrite an object | Until an admin deletes it; tapes 30 days | Google's at-rest encryption | The run bucket ([the outbox](outbox.md)) |
| The audit log | One line per audit entry | Cloud Logging's own log bucket | 400 days, lockable | Google's at-rest encryption | `hangar-audit` ([audit](../security/audit.md)) |
| The vault key | What decrypts the vault and every backup | Secret Manager, by hand | Until deleted | Secret Manager's | `hangar-vault-key` |

Images and harness packs are rebuilt from a tag, and caches refill by themselves, so neither is backed up.

## Recovery objectives

| Objective | Value | How it is set |
| --- | --- | --- |
| RPO, the database | 24 hours | `HANGAR_BACKUP_HOURS`. Set 6 for six hours |
| RPO, checkpoints and run capture | None lost | They are written to the bucket when they are made |
| RTO, the database, on GKE | 30 minutes (a target) | Stage the restore, restart, then run `hangar doctor` |
| RTO, the restore step | 2 seconds (measured) | The drill on a small database ([results](drills.md#results)) |

The cluster RTO is a target until a drill on the cluster records it. Most of it is the restart and image pull.

## After a restore

A restore brings back the database as it was at the backup. Three things follow from that.

- A token revoked after the backup works again. Revoke it again, or rotate every token.
- Jobs that were running at the backup restart, or are marked interrupted.
- A session can resume only from checkpoints the bucket still holds. hangar keeps 5 per session, so restore a recent
  backup.

## Keeping the objectives

- `hangar doctor` fails "database backups" when the newest backup is older than two intervals.
- The `data-disk` alert fires before the volume fills ([a full disk or quota](disk-and-quota.md)).
- Run the [restore drill](drills.md) after any change to the store, and before each release.
