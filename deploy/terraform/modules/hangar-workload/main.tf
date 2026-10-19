# hangar's cloud side on a cluster you already run (docs/deploy-company.md): its Google service account and the
# Workload Identity binding for its Kubernetes service account, its buckets, and optionally a registry, Cloud Build
# rights, a static IP and the power schedule. It never creates or changes a cluster, a node pool or a network.
# deploy/terraform/gke is the owner's own install, which creates the whole cluster; this module is not used there.

locals {
  sa_email          = google_service_account.hangar.email
  wi_pool           = coalesce(var.workload_identity_pool, "${var.project_id}.svc.id.goog")
  registry_location = var.registry == null ? null : coalesce(var.registry.location, var.region)
  apis = var.enable_apis ? toset(concat(
    ["iam.googleapis.com", "storage.googleapis.com"],
    var.registry != null ? ["artifactregistry.googleapis.com"] : [],
    var.enable_cloud_build ? ["cloudbuild.googleapis.com"] : [],
    var.create_static_ip ? ["compute.googleapis.com"] : [],
  )) : toset([])
}

resource "google_project_service" "apis" {
  for_each           = local.apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# --------------------------------------------------------------------------- identity
resource "google_service_account" "hangar" {
  project      = var.project_id
  account_id   = var.name
  display_name = "hangar control plane (${var.namespace}/${var.kubernetes_service_account})"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.hangar.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${local.wi_pool}[${var.namespace}/${var.kubernetes_service_account}]"
}

resource "google_project_iam_member" "hangar" {
  for_each = toset(var.project_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${local.sa_email}"
}

# --------------------------------------------------------------------------- registry
resource "google_artifact_registry_repository" "hangar" {
  count         = try(var.registry.create, false) ? 1 : 0
  project       = var.project_id
  location      = local.registry_location
  repository_id = var.registry.repository_id
  format        = "DOCKER"
  description   = "hangar environment images and harness packs"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = var.registry.keep_count
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "hangar" {
  count      = var.registry == null ? 0 : 1
  project    = var.project_id
  location   = local.registry_location
  repository = var.registry.create ? google_artifact_registry_repository.hangar[0].repository_id : var.registry.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${local.sa_email}"
}

# --------------------------------------------------------------------------- Cloud Build (opt-in)
resource "google_project_iam_member" "cloud_build" {
  for_each = var.enable_cloud_build ? toset(["roles/cloudbuild.builds.editor", "roles/storage.admin"]) : toset([])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${local.sa_email}"
}

# Cloud Build runs as hangar's own service account, which needs actAs on itself.
resource "google_service_account_iam_member" "self_user" {
  count              = var.enable_cloud_build ? 1 : 0
  service_account_id = google_service_account.hangar.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.sa_email}"
}

# --------------------------------------------------------------------------- session checkpoints
resource "google_storage_bucket" "sessions" {
  count                       = var.enable_session_bucket ? 1 : 0
  project                     = var.project_id
  name                        = "${var.project_id}-${var.name}-sessions"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy_buckets
  dynamic "lifecycle_rule" {
    for_each = var.session_retention_days > 0 ? [1] : []
    content {
      condition {
        age = var.session_retention_days
      }
      action {
        type = "Delete"
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "sessions" {
  count  = var.enable_session_bucket ? 1 : 0
  bucket = google_storage_bucket.sessions[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${local.sa_email}"
}

# --------------------------------------------------------------------------- run capture
resource "google_storage_bucket" "runs" {
  count                       = var.enable_run_bucket ? 1 : 0
  project                     = var.project_id
  name                        = "${var.project_id}-${var.name}-runs"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy_buckets
  lifecycle_rule {
    condition {
      age            = var.run_tape_days
      matches_prefix = ["v1/tapes/"]
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

# hangar: create objects, nothing else (no overwrite, no delete)
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

# --------------------------------------------------------------------------- ingress
resource "google_compute_global_address" "hangar" {
  count      = var.create_static_ip ? 1 : 0
  project    = var.project_id
  name       = "${var.name}-ip"
  depends_on = [google_project_service.apis]
}
