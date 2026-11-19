# --------------------------------------------------------------------------- the audit log, off the pod (KAN-182)
# hangar's audit log is a table in its SQLite database, on the pod's disk. Each entry is also written to the container
# log as one `hangar.audit: {json}` line (src/hangar/store.py). This routes those lines to a Cloud Logging bucket of
# their own, kept for audit_retention_days. Nobody can delete a log entry from a bucket before its retention ends, and
# hangar's service account only writes logs (roles/logging.logWriter) and reads this bucket's view (KAN-541): it can't
# change the bucket, the sink or the retention. With audit_bucket_locked = true, nobody can shorten the retention or delete the bucket either: Google
# refuses both for as long as the lock holds, and the lock can't be undone. docs/security/audit.md.
#
#   enable_audit_export   = true
#   audit_retention_days  = 400     # Cloud Logging allows 1 to 3650
#   audit_bucket_locked   = false   # true once the retention is right: it is permanent

variable "enable_audit_export" {
  description = "Route hangar's audit lines to a Cloud Logging bucket of their own, kept for audit_retention_days."
  type        = bool
  default     = false
}

variable "audit_retention_days" {
  description = "How long the audit bucket keeps each entry (1 to 3650 days)."
  type        = number
  default     = 400
  validation {
    condition     = var.audit_retention_days >= 1 && var.audit_retention_days <= 3650
    error_message = "Cloud Logging buckets keep entries for 1 to 3650 days."
  }
}

variable "audit_bucket_locked" {
  description = "Lock the audit bucket's retention: it can then never be shortened, nor the bucket deleted early. Permanent."
  type        = bool
  default     = false
}

locals {
  audit_filter = join(" ", [
    "resource.type=\"k8s_container\"",
    "AND resource.labels.namespace_name=\"${var.hangar_namespace}\"",
    "AND resource.labels.container_name=\"hangar\"",
    "AND textPayload:\"hangar.audit: {\"",
  ])
}

resource "google_logging_project_bucket_config" "audit" {
  count          = var.enable_audit_export ? 1 : 0
  project        = var.project_id
  location       = "global"
  bucket_id      = "hangar-audit"
  description    = "hangar's audit log (KAN-182): one entry per approval decision and refused request"
  retention_days = var.audit_retention_days
  locked         = var.audit_bucket_locked
}

resource "google_logging_project_sink" "audit" {
  count       = var.enable_audit_export ? 1 : 0
  project     = var.project_id
  name        = "hangar-audit"
  description = "hangar's audit lines to the hangar-audit bucket"
  destination = "logging.googleapis.com/${google_logging_project_bucket_config.audit[0].id}"
  filter      = local.audit_filter
  # A sink to a bucket in the same project writes as Logging itself; a writer identity of its own keeps it separate
  # from every person's and hangar's.
  unique_writer_identity = true
}

# --- KAN-541: hangar may read the bucket's _AllLogs view, and nothing else of Logging, so `hangar doctor` can compare
# the table's newest entry with the bucket's (HANGAR_AUDIT_BUCKET = the output audit_log.view). viewAccessor can't
# write to the bucket, change it, the sink or the retention; the condition keeps it to this one view.
resource "google_project_iam_member" "audit_reader" {
  count   = var.enable_audit_export ? 1 : 0
  project = var.project_id
  role    = "roles/logging.viewAccessor"
  member  = "serviceAccount:${local.sa_email}"
  condition {
    title      = "hangar-audit-view"
    expression = "resource.name == \"${google_logging_project_bucket_config.audit[0].id}/views/_AllLogs\""
  }
  depends_on = [google_logging_project_bucket_config.audit]
}

output "audit_log" {
  description = "Where hangar's audit entries are kept, and a query that reads them (docs/security/audit.md)."
  value = var.enable_audit_export ? {
    bucket         = google_logging_project_bucket_config.audit[0].id
    retention_days = var.audit_retention_days
    locked         = var.audit_bucket_locked
    view           = "${google_logging_project_bucket_config.audit[0].id}/views/_AllLogs"
    read           = "gcloud logging read 'textPayload:\"hangar.audit: {\"' --project ${var.project_id} --bucket hangar-audit --location global --view _AllLogs --freshness 30d"
  } : null
}

output "audit_view" {
  description = "HANGAR_AUDIT_BUCKET: the audit bucket's view hangar reads for `hangar doctor` (KAN-541); null when off."
  value       = var.enable_audit_export ? "${google_logging_project_bucket_config.audit[0].id}/views/_AllLogs" : null
}
