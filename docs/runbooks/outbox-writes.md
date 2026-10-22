# Run capture: how runs are written

How the outbox writes a finished run, step by step, and what to do when writes fail. Switching it on and checking it
are on [the outbox runbook](outbox.md).

## How a run is written

1. **At the turn's end** hangar builds the run's objects from what it has on disk. They are `manifest.json`,
   `trace.otlp.json`, `tests.json` when tests ran, and `run.json`. `patch.diff` is read from the sandbox while it is
   still up.
    - A job's turn is captured after hangar's check and the delivery; a session's as the turn ends.
    - Capture never sits on the turn's path: it runs as a task of its own, and a capture that fails only logs a
      warning.
2. **Each object is redacted** (stored credentials and their encodings, and the token formats secretscan knows) and
   scanned again. One that still holds a secret is never uploaded: it is recorded as `failed` and listed in
   `run.json`'s `withheld`.
3. **It is staged**: gzipped into `<data>/outbox/blobs/` on the control plane's volume, with one queue record per
   object. The record (`OutboxItem`) has the key, run, `pending` | `done` | `failed`, attempts, next try and last
   error.
4. **A drain loop uploads it** every 5 seconds, or at once when something new is staged. It uploads up to 50
   objects a pass, 4 at a time.
    - `run.json` waits until every other object of its run is uploaded, so a run directory without it is still
      arriving.
    - Each upload is a create that can't overwrite (`ifGenerationMatch=0`); a `412` means the object is already there
      and counts as done. The staged copy is deleted once uploaded.
5. **A failed upload is retried** with backoff, 1 minute doubling to 6 hours, for as long as it stays pending. The
   queue and the staged copies survive restarts. The control plane stays up while the sandbox pools are off, so the
   queue drains overnight too.

Job events (PR opened, first review, merged, closed, abandoned, reverts, failure labels) go the same way. hangar scans
the jobs that changed in the last minute and stages each new fact once.

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
   export. After that they are deleted unexported, and the audit event `retention.unexported_deleted` names the runs
   lost. Raise the setting during an outage you can't fix in time.

Two kinds of `failed` objects aren't retried:

- **`redaction: …`**: withheld on purpose. The object still held a secret after redaction, so it is never uploaded;
  `run.json` lists it in `withheld`. Nothing to do in the outbox; find where the secret came from.
- **`its staged payload is gone`**: the staged copy disappeared from the volume (a lost or replaced volume).
  `hangar outbox backfill` skips runs that already have queue records, so those objects stay missing.
