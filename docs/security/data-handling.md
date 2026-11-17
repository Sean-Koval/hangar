# Data handling

This page is for the reviewer who asks what hangar keeps, for how long, and how to delete it.
It is part of the [security model](README.md). Where the audit log's second copy lives is on
[where the audit log is kept](audit.md).

## What hangar keeps

| Data | Where | How long |
| --- | --- | --- |
| Jobs and sessions | The database on the `hangar-data` disk | Archived after `HANGAR_JOB_RETENTION_DAYS` and `HANGAR_SESSION_RETENTION_DAYS` (30), never deleted on their own |
| Session checkpoints, artifacts and event logs | The session store: the disk, or `HANGAR_SESSION_STORE` | Pruned with the session; a bucket deletes only by its own lifecycle rule |
| Outbox records (results sent to a bucket) | The database, then the outbox bucket | The bucket's retention |
| Credentials | The vault, encrypted with the vault key | Until deleted |
| Audit entries | The database, and the container log | `HANGAR_AUDIT_RETENTION_DAYS` (400) in the database; the log bucket's own retention |

## Audit retention

The audit table has its own setting, `HANGAR_AUDIT_RETENTION_DAYS`, separate from jobs and sessions. Each retention
sweep deletes the oldest rows past it, a batch at a time. `0` keeps every entry.

Keep it at or below the audit bucket's `audit_retention_days`. The bucket is then the longer record.

## The audit chain

Each audit row carries an HMAC of its own content and of the row before it. The key is in the vault, not the
database. So someone holding only the database can't edit, remove or reorder a row and rebuild the links after it.
Retention keeps an anchor, so the oldest row left still links to something.

`hangar doctor` walks the chain and reports the first broken row. A row a purge rewrote carries an HMAC of its new
content, so it can't hide a later edit. Doctor also fails if the chain was ever started twice. That happens only
when links were cleared and the mark of the first start removed.

The log line of every entry carries its `seq` and `hash`, so the bucket shows a row cut from the end of the table.

### If the chain breaks

A break means a row was changed, removed or unlinked outside hangar. Doctor names the first broken row.

1. Read that row and its neighbours: `GET /api/audit`, or the console's audit view.
2. Compare them with the same `seq` in the audit bucket ([reading it](audit.md#reading-it)).
3. Treat the bucket as the record. Report a difference that no restore explains as an incident.

## Deleting a person

```bash
hangar admin purge --person alice --dry-run   # count what would go
hangar admin purge --person alice             # asks before deleting
```

A purge deletes the person's sessions with their checkpoints, artifacts and event logs. It also deletes their jobs,
schedules, outbox records, personal credentials, tokens, sandbox records, snapshots and flight records, then the
person.

Their audit entries stay. Their id, tokens and linked accounts are replaced with one stand-in, `person:` and a hash.
The stand-in is the same in every entry, so a reviewer can still follow what they did. Its salt is in the vault, so
the database alone can't confirm a guessed name.

Names are replaced in `actor`, `target` and detail fields such as `person`. Elsewhere, only a value that is the name
as a whole is replaced. A common name such as `admin` needs a kind in front, as in `person:admin`. Their display
name is replaced only in entries that already name them.

A purge refuses while any of the person's work is live: a running session or job, a sandbox or a leased credential.
Stop it first. It refuses the owner, too.

It also deletes jobs and sessions their tokens or linked accounts own, such as GitHub or Slack jobs. Files of
sessions deleted earlier, still waiting for the outbox, go too. The audit entries are rewritten before anything is
deleted. If a step fails, the person's record stays and the purge reports the error. Run it again to finish.

The purge itself is audited (`admin.purge`), with the stand-in and how many entries it rewrote.

## Exporting a job

```bash
hangar admin export --job job-123 --out job-123.tar.gz
```

The archive holds the job's record, instructions, context, events and result. It adds each session's event logs and
artifacts, and every audit entry naming the job, its sessions or its sandboxes. A callback URL and its secret are
withheld.

`manifest.json` lists a SHA-256 for each file and anything that couldn't be read. Only you can read the file, and
the export is audited.

## What it doesn't cover

- Objects already sent to the outbox bucket stay until the bucket's retention. The purge lists them.
- The audit bucket is locked, so its lines keep the original names until its retention ends.
- A name in free text, such as a note, stays. So does a display name or token name another person shares.
- Someone holding both the database and the vault key can rebuild the chain. The bucket is the record then.
- Restoring a database backup rolls the table back, and the chain still verifies. Compare the newest `seq` with the
  bucket after a restore.
- A Docker or single-VM install has no audit bucket, so only the table can be checked.
