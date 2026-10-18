# Runbook: run capture (the outbox)

The outbox writes every finished agent run, once, to a bucket that flightdeck reads (#163, #189). This runbook is for
the admin who switches it on and keeps it healthy. The contract itself (the layout, `run.json`, job events, what is
redacted) is [Run capture (hangar.run/v1)](../api/outbox.md); the code is `src/hangar/outbox.py`, and the bucket is
`deploy/terraform/gke/outbox.tf`.

## How a run is written

1. **At the turn's end** hangar builds the run's objects from what it has on disk: `manifest.json`, `trace.otlp.json`,
   `tests.json` when tests ran, `patch.diff` read from the sandbox while it is still up, and `run.json`. A job's turn is
   captured after hangar's check and the delivery; a session's as the turn ends. Capture never sits on the turn's
   path: it runs as a task of its own, and a capture that fails only logs a warning.
2. **Each object is redacted** (stored credentials and their encodings, and the token formats secretscan knows) and
   scanned again. One that still holds a secret is never uploaded: it is recorded as `failed` and listed in `run.json`'s
   `withheld`.
3. **It is staged**: gzipped into `<data>/outbox/blobs/` on the control plane's volume, with one queue record per
   object (`OutboxItem`: key, run, `pending` | `done` | `failed`, attempts, next try, last error).
4. **A drain loop uploads it**: every 5 seconds, or at once when something new is staged, up to 50 objects a pass,
   4 at a time. `run.json` waits until every other object of its run is uploaded, so a run directory without it is
   still arriving. Each upload is a create that can't overwrite (`ifGenerationMatch=0`); a `412` means the object is
   already there and counts as done. The staged copy is deleted once uploaded.
5. **A failed upload is retried** with backoff, 1 minute doubling to 6 hours, for as long as it stays pending. The
   queue and the staged copies survive restarts, and the control plane stays up while the sandbox pools are off, so
   the queue drains overnight too.

Job events (PR opened, first review, merged, closed, abandoned, reverts, failure labels) go the same way: hangar scans
the jobs that changed in the last minute and stages each new fact once.

## Switching it on

On GKE, terraform makes the bucket (`enable_run_bucket`, on by default; [gke.md](../gke.md#run-capture-bucket)).
Hangar writes nothing until `HANGAR_OUTBOX_URL` points at it. Turning it on in the GKE deploy is #273.

1. Give flightdeck's compactor read access: `run_bucket_readers = ["serviceAccount:…"]` in your tfvars, then
   `terraform apply` in `deploy/terraform/gke`.
2. Set the URL in the `hangar-site` config map, from `terraform output -raw run_bucket` (the commented line in
   `deploy/k8s/config.yaml`), and restart: the outbox reads the URL when hangar starts.

   ```bash
   BUCKET=$(terraform -chdir=deploy/terraform/gke output -raw run_bucket)      # gs://<project>-hangar-runs
   kubectl -n hangar patch configmap hangar-site --type merge -p "{\"data\":{\"HANGAR_OUTBOX_URL\":\"$BUCKET\"}}"
   #   no hangar-site yet: kubectl -n hangar create configmap hangar-site --from-literal=HANGAR_OUTBOX_URL="$BUCKET"
   kubectl -n hangar rollout restart deploy/hangar
   ```

3. `HANGAR_OUTBOX_SESSIONS=jobs_only` leaves out sessions that no job started (people's own interactive sessions);
   the default, `all`, captures them too.

On a laptop, any directory works (`HANGAR_OUTBOX_URL=~/hangar-runs`): the same layout and bytes a GCS reader gets.

Runs from before the outbox was on aren't captured. `hangar outbox backfill --since 7d` captures the runs of the
last week that have nothing queued, from what hangar still has on disk (the patch only while the sandbox is up).

## Checking that a run landed

```bash
hangar outbox status
```

It prints the sink, the counts (`done`, `pending` and its size, `withheld`), the oldest pending object and the next
try, the last error, each withheld object with its reason, and how many deleted sessions wait for their export
(`GET /api/admin/outbox`, admins).

For one run, find its key, then look in the bucket:

1. The job's session: `hangar job get <job>` shows `session_id`. Then `hangar session get <session>` lists the turns,
   each with its `run_id` and `finished_at`. For a trial, `GET /api/trials/{id}` gives each run's `outbox` prefix
   directly.
2. The run's prefix is `v1/runs/<date>/<session_id>.<run_id>/`, where `<date>` is the UTC date the run finished
   (`YYYY-MM-DD`).
3. List it with your own credentials. Hangar's service account can create objects there but can't read them:

   ```bash
   gcloud storage ls gs://<project>-hangar-runs/v1/runs/2026-10-18/<session_id>.<run_id>/
   gcloud storage cat gs://<project>-hangar-runs/v1/runs/2026-10-18/<session_id>.<run_id>/run.json | jq '.objects, .withheld, .expects'
   ```

   `run.json` present means the run is complete. Its `objects` are what is there, `withheld` what never will be, and
   `expects` what comes later (a graded trial's `grade.json`).

On flightdeck's side, the compactor turns a date's objects into its tables (`flightdeck compact --date D`); it looks
back 3 days for objects that arrived late.

Watching it: `/metrics` has `hangar_outbox_pending`, and `hangar doctor` warns (`run capture (outbox)`) past 1,000
pending objects or 24 hours of age, lists withheld objects (`run capture: withheld objects`), and reports the space
deleted sessions hold until their export (`retention held for export`).

## Retention

| What | How long |
| --- | --- |
| Objects in the bucket | Until an admin deletes them: hangar can't delete or overwrite (an IAM deny policy). Tapes under `v1/tapes/` expire after `run_tape_days` (30) |
| Staged copies on the control plane's volume | Until uploaded; a failed upload keeps its copy and retries |
| Queue records | Kept after upload: they are how hangar knows a run is exported. One row per object, up to six a run |
| A deleted session's event log and manifests | In `<data>/pending-delete/<session>/` until every run of it is exported, then deleted (a pass every 10 minutes); after `HANGAR_EXPORT_GRACE_DAYS` (30) deleted anyway, audited `retention.unexported_deleted` |
| Test runs of failed, abandoned or labelled jobs | Past the usual windows (cases 3 days, runs 14) until the job's runs are exported |

**Deletion requests** (personal data): delete the run's objects with an admin's own credentials, and its job's event
objects, then have flightdeck recompact the dates:

```bash
gcloud storage rm -r gs://<project>-hangar-runs/v1/runs/<date>/<session_id>.<run_id>/
flightdeck compact --date <date> --rebuild
hangar session delete <session> --force-now     # hangar's own copy, without waiting for the export (audited)
```

## When writes fail

Symptoms: `hangar doctor` warns on `run capture (outbox)`, `hangar outbox status` shows a growing `pending` and a
`last error`, `hangar_outbox_pending` climbs.

1. **Read the last error.** `hangar outbox status` prints it; the doctor's line carries it once the outbox is behind.
2. **`403`, permission denied:** hangar's service account lacks `roles/storage.objectCreator` on the bucket, or the
   URL names another bucket. Check `HANGAR_OUTBOX_URL` against `terraform output -raw run_bucket`, and re-apply
   terraform (`google_storage_bucket_iam_member.runs_writer`).
3. **`404`, no such bucket:** the bucket was never created (`enable_run_bucket = false`) or the URL is wrong.
4. **A GCS outage or the network:** nothing to fix in hangar. The queue keeps the objects and retries with backoff;
   no turn waits for it.
5. **After the fix,** retry now instead of waiting out the backoff (up to 6 hours):

   ```bash
   hangar outbox retry
   hangar outbox status      # pending goes down; done goes up
   ```

6. **Space on the volume.** Staged objects live on the control plane's `hangar-data` volume (20 GiB in
   `deploy/k8s/deployment.yaml`); `pending` shows their size. A long outage with a busy team fills it: fix the cause
   before it does.
7. **A long outage and retention.** A deleted session's files wait at most `HANGAR_EXPORT_GRACE_DAYS` (30) for their
   export; after that they are deleted unexported, and the audit event `retention.unexported_deleted` names the runs
   lost. Raise the setting during an outage you can't fix in time.

Two kinds of `failed` objects aren't retried:

- **`redaction: …`**: withheld on purpose. The object still held a secret after redaction, so it is never uploaded;
  `run.json` lists it in `withheld`. Nothing to do in the outbox; find where the secret came from.
- **`its staged payload is gone`**: the staged copy disappeared from the volume (a lost or replaced volume).
  `hangar outbox backfill` skips runs that already have queue records, so those objects stay missing.

## Turning it off

Unset `HANGAR_OUTBOX_URL` (in `hangar-site`) and restart. Nothing new is staged, retention works as before for new
deletions, and what is in the bucket stays. The drain loop doesn't start, and neither does its pending-delete pass:
files already waiting in `<data>/pending-delete/` stay until the outbox is on again.
