# Data handling

This page is for the reviewer who asks what hangar keeps, for how long, and how to delete it. It is part of the
[security model](README.md); the audit log's second copy is on [where the audit log is kept](audit.md).

## What hangar keeps

| Data | Where | How long |
| --- | --- | --- |
| Jobs and sessions | The database on the `hangar-data` disk | Archived after `HANGAR_JOB_RETENTION_DAYS` and `HANGAR_SESSION_RETENTION_DAYS` (30), never deleted on their own |
| Session checkpoints, artifacts and event logs | The session store: the disk, or `HANGAR_SESSION_STORE` | Pruned with the session; a bucket deletes only by its own lifecycle rule |
| Outbox records (results sent to a bucket) | The database, then the outbox bucket | The bucket's retention |
| Credentials | The vault, encrypted with the vault key | Until deleted |
| Audit entries | The database, and the container log | `HANGAR_AUDIT_RETENTION_DAYS` (400) in the database; the log bucket's own retention |

## Audit retention

`HANGAR_AUDIT_RETENTION_DAYS` is separate from jobs and sessions. Each retention
sweep deletes the oldest rows past it. `0` keeps every entry.

Keep it at or below the audit bucket's `audit_retention_days`, so the bucket is the longer record.

## The audit chain

Each audit row carries an HMAC of its own content and of the row before it. The key is in the vault, not the
database. A row a purge rewrote carries an HMAC of its new content. Every entry's log line carries its `seq` and
`hash`.

Retention keeps the last deleted row's link as an anchor. The same step writes an `audit.pruned` entry carrying that
anchor, so the chain vouches for where it starts.

The chain assumes a single hangar process writes the database.

### What doctor proves

`hangar doctor` walks the chain and names the first broken row. The audit bucket remains the record. It catches someone holding only the database who:

- edits, inserts, reorders or removes a row anywhere but at the end;
- deletes the oldest rows and writes a new anchor;
- clears the links and restarts hangar.

Doctor can't see:

- rows cut from the end of the table;
- a table reset to before the chain: its columns dropped and its start entry deleted;
- a database restored from a backup, which rolls the table back with a valid chain;
- changes by someone holding both the database and the vault key.

For those, compare the newest `seq` and `hash` with the bucket. A Docker or single-VM install has no bucket, so
someone with its database file can rewrite the table undetected.

### If the chain breaks

A break means a row was changed, removed or unlinked outside hangar.

1. Read that row and its neighbours: `GET /api/audit`, or the console's audit view.
2. Compare them with the same `seq` in the audit bucket ([reading it](audit.md#reading-it)).
3. Treat the bucket as the record. Report a difference that no restore explains as an incident.

## Deleting a person

```bash
hangar admin purge --person alice --dry-run   # count what would go
hangar admin purge --person alice             # asks before deleting
```

A purge first deletes the person's tokens and unlinks their accounts, so nothing new starts as them. Then it deletes
their sessions with checkpoints, artifacts and event logs, and their jobs, schedules, outbox records and credentials.
Their sandbox records, snapshots, flight records, SSO binding and spend alerts' marks go too, then the person.

Their audit entries stay. Their id, tokens and linked accounts are replaced with one stand-in, `person:` and a hash.
It is the same in every entry, so a reviewer can still follow them. Its salt is in the vault, so
the database alone can't confirm a guessed name.

Names are replaced in `actor`, `target` and detail fields such as `person`. Elsewhere, only a value that is the name
as a whole is replaced. A common name such as `admin` needs a kind in front. Their display
name is replaced only in entries that already name them.

A purge refuses while the person has live work: a running session or job, a sandbox or a leased credential. It refuses the owner and any admin; make an admin a member first.

Jobs and sessions their tokens or linked accounts own go too, as do session files still waiting for the outbox.

After their tokens and links, it rewrites their audit entries before deleting any other record. If a step fails, or a record of theirs
appears meanwhile, the person stays; run it again.

The purge itself is audited (`admin.purge`) under the stand-in.

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

- Objects already in the outbox bucket stay until its retention; the purge lists them.
- The audit bucket is locked, so its lines keep the original names until its retention ends.
- A name in free text, such as a note, stays, as does a display or token name another person shares.
