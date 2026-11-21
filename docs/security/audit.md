# Where the audit log is kept

This page is for the admin or reviewer who needs hangar's audit trail to outlive the pod and resist tampering. What
goes into the log is on [front-door security](front-doors.md#audit). This is part of the [security model](README.md).

## Two copies

hangar writes each audit entry to a table in its database, on the `hangar-data` disk. `GET /api/audit` and the
console's audit view read that copy. Whoever controls the pod or the disk could rewrite it, so it isn't the record of
last resort.

Each entry is also written to the container log as one line: `hangar.audit: ` followed by the entry's JSON. The line
holds the table's fields, plus the row's `seq` and its `hash` in the [audit chain](data-handling.md#the-audit-chain).
On GKE, Cloud Logging keeps a copy of every line, outside the
pod and its disk.

## The audit bucket

Set `enable_audit_export = true` in `deploy/terraform/gke/`, or in the `hangar-workload` module on a company
cluster, and apply. A log sink then routes the audit lines to a
Cloud Logging bucket of their own, `hangar-audit`, kept for `audit_retention_days` (400 by default).

Nobody can delete an entry from a log bucket before its retention ends. hangar's service account only writes logs
and reads this bucket, so it can't change the bucket, the sink or the retention. Set `audit_bucket_locked = true`
once the retention is right.
After that, nobody can shorten it or delete the bucket early. The lock is permanent.

## Reading it

```bash
gcloud logging read 'textPayload=~"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} INFO hangar[.]audit: [{]"' --project PROJECT_ID \
  --bucket hangar-audit --location global --view _AllLogs --freshness 30d
```

The Terraform output `audit_log` prints this query for your project. The Logs Explorer reads the same bucket under
"Refine scope". Lines written before the sink existed stay in the `_Default` bucket, for its 30 days.

## Doctor's comparison

`hangar doctor` compares the table's newest entry with the bucket's ([what doctor proves](data-handling.md#what-doctor-proves)).
Set `HANGAR_AUDIT_BUCKET` to the Terraform output `audit_view`. `deploy/gke/up.sh` sets it; on a company cluster,
put the `hangar-workload` module's output in your `hangar-site`. The same apply grants hangar's service account
`roles/logging.viewAccessor` on that view alone, so it can read the bucket but still can't change it.

Doctor reads the bucket's lines of the last 30 days. Suppose a table is restored from an older backup, then written
past. Doctor sees it while the overwritten lines are among those 30 days: the bucket then holds one entry twice.

The read stops after 20 pages, or 30 seconds in all, so doctor never waits on Logging. A read that times out, or
finds no line, says "not run".

## What it doesn't cover

- The table in the database keeps entries for `HANGAR_AUDIT_RETENTION_DAYS` ([data handling](data-handling.md)).
- The chain shows a row edited or removed anywhere but the end ([what doctor proves](data-handling.md#what-doctor-proves)).
  A row cut from the end shows only against the bucket's newest `seq`.
  An entry a purge rewrote is sealed to its row, so it can't be copied over another entry.
- A purge run after a rollback to v0.2.0 writes the older seal. Before upgrading again, delete the
  `audit.redactions_rebound` key from the `kv` table, or doctor reports those entries as edited.
- A Docker or single-VM install has no Cloud Logging. Ship the container log to your own store to get a second copy.
