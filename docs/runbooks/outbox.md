# Runbook: run capture (the outbox)

The outbox writes every finished agent run, once, to a bucket that flightdeck reads (#163, #189). This runbook is for
the admin who switches it on and keeps it healthy. How a run is written, and what to do when writes fail, is on
[how runs are written](outbox-writes.md).

The contract itself (the layout, `run.json`, job events, what is redacted) is
[Run capture (hangar.run/v1)](../api/outbox.md). The code is `src/hangar/outbox.py`, and the bucket is
`deploy/terraform/gke/outbox.tf`.

## Switching it on

On GKE, terraform makes the bucket (`enable_run_bucket`, on by default), and `deploy/gke/up.sh` turns run capture on
(#273, PR #307). It sets `HANGAR_OUTBOX_URL` in the `hangar` config map to `terraform output -raw run_bucket` on every
install, a running one too.

It keeps a value the `hangar` config map already has, and a value in `hangar-site` still wins. It restarts hangar when
the URL changed but the image didn't, because the outbox reads the URL when hangar starts
([the run capture bucket](../gke.md#run-capture-bucket)).

1. Give flightdeck's compactor read access: `run_bucket_readers = ["serviceAccount:…"]` in your tfvars. Then run
   `terraform apply` in `deploy/terraform/gke`, after flightdeck's `deploy/compactor/setup.sh` has made the account.
2. Run `bash deploy/gke/up.sh`. By hand instead: uncomment the `HANGAR_OUTBOX_URL` line in `deploy/k8s/config.yaml`,
   or set it in `hangar-site`, and restart (`kubectl -n hangar rollout restart deploy/hangar`). `hangar config set`
   says a restart is needed too.
3. Check it: `hangar doctor` reports `run capture (outbox)` as on, with the objects pending; while it is off on GKE it
   warns.
4. `HANGAR_OUTBOX_SESSIONS=jobs_only` leaves out sessions that no job started (people's own interactive sessions);
   the default, `all`, captures them too.

On a laptop, any directory works (`HANGAR_OUTBOX_URL=~/hangar-runs`): the same layout and bytes a GCS reader gets.

Runs from before the outbox was on aren't captured. `hangar outbox backfill --since 7d` captures the runs of the last
week that have nothing queued. It works from what hangar still has on disk (the patch only while the sandbox is up).

## Checking that a run landed

```bash
hangar outbox status
```

It prints the sink, the counts (`done`, `pending` and its size, `withheld`), the oldest pending object and the next
try, and the last error. It also lists each withheld object with its reason, and how many deleted sessions wait for
their export (`GET /api/admin/outbox`, admins).

For one run, find its key, then look in the bucket:

1. The job's session: `hangar job get <job>` shows `session_id`. Then `hangar session get <session>` lists the turns,
   each with its `run_id` and `finished_at`. For a trial, `GET /api/trials/{id}` gives each run's `outbox` prefix
   directly.
2. The run's prefix is `v1/runs/<date>/<session_id>.<run_id>/`, where `<date>` is the UTC date the run finished
   (`YYYY-MM-DD`).
3. List it with your own credentials. hangar's service account can create objects there but can't read them:

   ```bash
   gcloud storage ls gs://<project>-hangar-runs/v1/runs/2026-10-18/<session_id>.<run_id>/
   gcloud storage cat gs://<project>-hangar-runs/v1/runs/2026-10-18/<session_id>.<run_id>/run.json | jq '.objects, .withheld, .expects'
   ```

   `run.json` present means the run is complete. Its `objects` are what is there, `withheld` what never will be, and
   `expects` what comes later (a graded trial's `grade.json`).

On flightdeck's side, the compactor turns a date's objects into its tables (`flightdeck compact --date D`); it looks
back 3 days for objects that arrived late.

Watching it: `/metrics` has `hangar_outbox_pending`. `hangar doctor` warns (`run capture (outbox)`) past 1,000
pending objects or 24 hours of age, and lists withheld objects (`run capture: withheld objects`). It also reports the
space deleted sessions hold until their export (`retention held for export`).

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

## Turning it off

Unset `HANGAR_OUTBOX_URL` (in `hangar-site`) and restart. Nothing new is staged, retention works as before for new
deletions, and what is in the bucket stays. The drain loop doesn't start, and neither does its pending-delete pass.
Files already waiting in `<data>/pending-delete/` stay until the outbox is on again.

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="how-a-run-is-written"></span>How a run is written | [How runs are written](outbox-writes.md#how-a-run-is-written) |
| <span id="when-writes-fail"></span>When writes fail | [How runs are written: when writes fail](outbox-writes.md#when-writes-fail) |
