# Where the audit log is kept

This page is for the admin or reviewer who needs hangar's audit trail to outlive the pod and resist tampering. What
goes into the log is on [front-door security](front-doors.md#audit). This is part of the [security model](README.md).

## Two copies

hangar writes each audit entry to a table in its database, on the `hangar-data` disk. `GET /api/audit` and the
console's audit view read that copy. Whoever controls the pod or the disk could rewrite it, so it isn't the record of
last resort.

Each entry is also written to the container log as one line: `hangar.audit: ` followed by the entry's JSON. The line
holds the same fields as the table and nothing more. On GKE, Cloud Logging keeps a copy of every line, outside the
pod and its disk.

## The audit bucket

Set `enable_audit_export = true` in `deploy/terraform/gke/` and apply. A log sink then routes the audit lines to a
Cloud Logging bucket of their own, `hangar-audit`, kept for `audit_retention_days` (400 by default).

Nobody can delete an entry from a log bucket before its retention ends. hangar's service account only writes logs, so
it can't change the bucket, the sink or the retention. Set `audit_bucket_locked = true` once the retention is right.
After that, nobody can shorten it or delete the bucket early. The lock is permanent.

## Reading it

```bash
gcloud logging read 'textPayload:"hangar.audit: {"' --project PROJECT_ID \
  --bucket hangar-audit --location global --view _AllLogs --freshness 30d
```

The Terraform output `audit_log` prints this query for your project. The Logs Explorer reads the same bucket under
"Refine scope". Lines written before the sink existed stay in the `_Default` bucket, for its 30 days.

## What it doesn't cover

- The table in the database has no retention of its own: it grows until a restore or a manual clean-up.
- Entries are not chained, so the local copy can't prove on its own that none was removed. Compare it with the
  bucket.
- A Docker or single-VM install has no Cloud Logging. Ship the container log to your own store to get a second copy.
