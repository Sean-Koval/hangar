# Recovery drills

This page answers one question for an admin: how do I prove that hangar's database can be restored, upgraded and
rolled back? `scripts/ops/drill.py` runs both drills. The objectives they test are in
[Backups and recovery objectives](recovery.md).

Both drills work in a temporary directory. With `--data-dir`, the drill copies an install's database read-only and
never writes to it. Without it, the drill seeds a database of its own.

## The restore drill

```bash
uv run python scripts/ops/drill.py restore --out restore.json
uv run python scripts/ops/drill.py restore --data-dir ~/.hangar   # a copy of a real install
```

It backs the database up to a separate store, then loses the disk. It restores the latest backup into a clean data
directory, as `hangar admin restore` and the next start do. Then it checks four things:

1. Every table holds exactly the rows it held at the backup.
2. SQLite's integrity check passes.
3. Every record loads, every vault secret decrypts, and every pin set still matches its id.
4. hangar applied the restore when it started, and nothing went missing in the start.

The report records how long the restore took. The vault key comes from `HANGAR_SECRET_KEY`, else the data
directory's `secret.key`, as Secret Manager would hold it.

## The upgrade and rollback rehearsal

```bash
uv run python scripts/ops/drill.py upgrade --from v0.2.0 --out upgrade.json
uv run python scripts/ops/drill.py upgrade --from v0.2.0 --data-dir ~/.hangar
```

It follows [upgrading.md](../upgrading.md) on the previous tag's database:

1. The previous tag seeds the database and takes the pre-upgrade backup.
2. The candidate starts on it, which runs the migrations, and must read every record with nothing lost.
3. The candidate writes records of its own.
4. **Rollback in place:** the previous tag starts on the migrated database and reads every record. Its rows
   may be rewritten, so this compares keys: no record may go missing.
5. **Rollback by restore:** the previous tag restores the pre-upgrade backup, which must match the database
   before the upgrade.

`--to` names the candidate; the default is this checkout. Each version runs in its own process with its own source,
on this checkout's environment.

A seeded database holds only fields every version shares. Run the rehearsal with `--data-dir` on a copy of the real
install too, since real records exercise more of each model.

## What the drills don't cover

- **The cluster.** The drills run hangar's code, not GKE. Before a release, run the restore on the cluster as
  [Backups and restore on GKE](../gke/backups.md) says, and record the time.
- **Tower.** Its decisions, receipts and passports are in its own repository, with its own drill.

## Results

| Date | Commit | Drill | Result | Time to restore |
| --- | --- | --- | --- | --- |
| 2026-11-17 | main at `ca88be3` | Restore, seeded | Pass | 1.7 s |
| 2026-11-17 | main at `ca88be3` | Upgrade from v0.2.0, both rollbacks | Pass | 1.7 s (by restore) |

Add a row for each drill, newest at the bottom. The tests run both drills on every change too (`tests/test_ops_drill.py`).
