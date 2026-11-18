# --------------------------------------------------------------------------- the audit log, off the pod (KAN-296)
# The same locked audit sink as the owner's deploy/terraform/gke/audit.tf (KAN-182), for a company install. hangar
# writes each audit entry to its database and to the container log as one `hangar.audit: {json}` line, with its
# place in the database's hash chain (`seq`, `hash`). This routes those lines to a Cloud Logging bucket of their own,
# kept for audit_retention_days. hangar's service account gets no logging role here: it can't change the bucket, the
# sink or the retention. With audit_bucket_locked = true, nobody can shorten the retention or delete the bucket
# early, and the lock can't be undone. docs/security/audit.md, docs/security/data-handling.md.
#
#   enable_audit_export  = true
#   audit_retention_days = 400     # Cloud Logging allows 1 to 3650
#   audit_bucket_locked  = false   # true once the retention is right: it is permanent

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

variable "audit_bucket_id" {
  description = "The audit log bucket's id, unique in the project. Null: \"<name>-audit\", so a second install with its own name gets its own."
  type        = string
  default     = null
}

variable "audit_container_name" {
  description = "The name of hangar's container in its Deployment, as the sink's filter matches it."
  type        = string
  default     = "hangar"
}

locals {
  audit_filter = join(" ", [
    "resource.type=\"k8s_container\"",
    "AND resource.labels.namespace_name=\"${var.namespace}\"",
    "AND resource.labels.container_name=\"${var.audit_container_name}\"",
    # KAN-556: anchored at the start of the line, as hangar's log format writes an audit record (logfmt.py): a
    # sandbox's text quoted inside another record can't start a line there
    "AND textPayload=~\"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} INFO hangar[.]audit: [{]\"",
  ])
}

locals {
  audit_bucket_id = coalesce(var.audit_bucket_id, "${var.name}-audit")
}

resource "google_project_service" "logging" {
  count              = var.enable_audit_export && var.enable_apis ? 1 : 0
  project            = var.project_id
  service            = "logging.googleapis.com"
  disable_on_destroy = false
}

resource "google_logging_project_bucket_config" "audit" {
  count          = var.enable_audit_export ? 1 : 0
  project        = var.project_id
  location       = "global"
  bucket_id      = local.audit_bucket_id
  description    = "hangar's audit log (${var.namespace}): one entry per approval decision, refused request and admin action"
  retention_days = var.audit_retention_days
  locked         = var.audit_bucket_locked
  depends_on     = [google_project_service.logging]
}

resource "google_logging_project_sink" "audit" {
  count       = var.enable_audit_export ? 1 : 0
  project     = var.project_id
  name        = local.audit_bucket_id
  description = "hangar's audit lines (${var.namespace}) to the ${local.audit_bucket_id} bucket"
  destination = "logging.googleapis.com/${google_logging_project_bucket_config.audit[0].id}"
  filter      = local.audit_filter
  # A writer identity of its own keeps the sink separate from every person's and hangar's.
  unique_writer_identity = true
}

output "audit_log" {
  description = "Where hangar's audit entries are kept, and a query that reads them (docs/security/audit.md)."
  value = var.enable_audit_export ? {
    bucket         = google_logging_project_bucket_config.audit[0].id
    retention_days = var.audit_retention_days
    locked         = var.audit_bucket_locked
    read           = "gcloud logging read 'textPayload=~\"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} INFO hangar[.]audit: [{]\"' --project ${var.project_id} --bucket ${local.audit_bucket_id} --location global --view _AllLogs --freshness 30d"
  } : null
}
