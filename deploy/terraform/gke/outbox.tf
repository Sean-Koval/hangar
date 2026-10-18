# --------------------------------------------------------------------------- run capture: the outbox (#163)
# Every finished agent run, written once, as immutable objects (docs/api/outbox.md): the sister tool reads them while
# the sandbox pools are off. deploy/gke/up.sh sets HANGAR_OUTBOX_URL=gs://<bucket> (output run_bucket) in the hangar
# config map.
#
# Write-once: hangar's service account gets roles/storage.objectCreator on this bucket only (create, never read back,
# delete or overwrite), and because its project-wide roles/storage.admin (Cloud Build's staging bucket, main.tf)
# would otherwise allow deleting here, an IAM deny policy takes objects.delete and objects.update away from it on
# any bucket carrying the hangar-write-once=runs tag, which this one does. Readers (run_bucket_readers: the sister
# tool's compactor) get roles/storage.objectViewer. Deleting a run (a deletion request) is an admin's job with their
# own credentials (docs/api/outbox.md). Applying the deny policy needs roles/iam.denyAdmin on the project.
#
# Tapes (#177) expire after run_tape_days: a lifecycle rule can only match an object name's prefix, so tapes live
# under v1/tapes/ rather than inside each run's directory.

variable "enable_run_bucket" {
  description = "GCS bucket for run capture, the outbox (HANGAR_OUTBOX_URL=gs://<bucket>)."
  type        = bool
  default     = true
}

variable "run_bucket_readers" {
  description = "Principals that read captured runs (roles/storage.objectViewer), e.g. the sister tool's compactor: [\"serviceAccount:flightdeck-compactor@P.iam.gserviceaccount.com\"]."
  type        = list(string)
  default     = []
}

variable "run_tape_days" {
  description = "Delete tapes (objects under v1/tapes/) after this many days."
  type        = number
  default     = 30
}

resource "google_storage_bucket" "runs" {
  count                       = var.enable_run_bucket ? 1 : 0
  name                        = "${var.project_id}-hangar-runs"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  lifecycle_rule {
    condition {
      age            = var.run_tape_days
      matches_prefix = ["v1/tapes/"]
    }
    action {
      type = "Delete"
    }
  }
}

# hangar: create objects, nothing else
resource "google_storage_bucket_iam_member" "runs_writer" {
  count  = var.enable_run_bucket ? 1 : 0
  bucket = google_storage_bucket.runs[0].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${local.sa_email}"
}

resource "google_storage_bucket_iam_member" "runs_readers" {
  for_each = var.enable_run_bucket ? toset(var.run_bucket_readers) : toset([])
  bucket   = google_storage_bucket.runs[0].name
  role     = "roles/storage.objectViewer"
  member   = each.value
}

resource "google_tags_tag_key" "write_once" {
  count       = var.enable_run_bucket ? 1 : 0
  parent      = "projects/${var.project_id}"
  short_name  = "hangar-write-once"
  description = "Buckets whose objects hangar may create but never delete or overwrite."
}

resource "google_tags_tag_value" "runs" {
  count      = var.enable_run_bucket ? 1 : 0
  parent     = google_tags_tag_key.write_once[0].id
  short_name = "runs"
}

resource "google_tags_location_tag_binding" "runs" {
  count     = var.enable_run_bucket ? 1 : 0
  parent    = "//storage.googleapis.com/projects/_/buckets/${google_storage_bucket.runs[0].name}"
  tag_value = google_tags_tag_value.runs[0].id
  location  = var.region
}

resource "google_iam_deny_policy" "runs_write_once" {
  count        = var.enable_run_bucket ? 1 : 0
  parent       = urlencode("cloudresourcemanager.googleapis.com/projects/${var.project_id}")
  name         = "hangar-runs-write-once"
  display_name = "hangar: captured runs are write-once"
  rules {
    description = "hangar's service account can't delete or overwrite captured runs (its storage.admin is for Cloud Build)."
    deny_rule {
      denied_principals  = ["principal://iam.googleapis.com/projects/-/serviceAccounts/${local.sa_email}"]
      denied_permissions = ["storage.googleapis.com/objects.delete", "storage.googleapis.com/objects.update"]
      denial_condition {
        title      = "hangar-write-once=runs"
        expression = "resource.matchTagId('${google_tags_tag_key.write_once[0].id}', '${google_tags_tag_value.runs[0].id}')"
      }
    }
  }
  depends_on = [google_tags_location_tag_binding.runs]
}

output "run_bucket" {
  value = var.enable_run_bucket ? "gs://${google_storage_bucket.runs[0].name}" : null
}
