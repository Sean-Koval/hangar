locals {
  sa_email = google_service_account.hangar.email
}

resource "google_project_service" "apis" {
  for_each = toset([
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "iam.googleapis.com",
    "containerfilesystem.googleapis.com", # image streaming (var.image_streaming)
  ])
  service            = each.value
  disable_on_destroy = false
}

# --------------------------------------------------------------------------- registry
resource "google_artifact_registry_repository" "hangar" {
  location      = var.region
  repository_id = "hangar"
  format        = "DOCKER"
  description   = "hangar control plane + environment images"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  depends_on = [google_project_service.apis]
}

# --------------------------------------------------------------------------- cluster
resource "google_container_cluster" "hangar" {
  name     = var.cluster_name
  location = var.zone

  release_channel {
    channel = var.release_channel
  }

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # NetworkPolicy enforcement (Dataplane V2) backs Agent Sandbox's default-deny egress to internal ranges.
  datapath_provider = "ADVANCED_DATAPATH"

  depends_on = [google_project_service.apis]
}

resource "google_container_node_pool" "system" {
  name     = "system"
  cluster  = google_container_cluster.hangar.name
  location = var.zone

  autoscaling {
    min_node_count = 1
    max_node_count = 2
  }

  node_config {
    machine_type = "e2-standard-2"
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

# gVisor (GKE Sandbox) pool: default isolation for sandboxes. Scales to zero when idle.
resource "google_container_node_pool" "sandboxes" {
  name     = "sandboxes-gvisor"
  cluster  = google_container_cluster.hangar.name
  location = var.zone

  autoscaling {
    min_node_count = 0
    max_node_count = var.sandbox_max_nodes
  }

  node_config {
    machine_type = var.sandbox_machine_type
    image_type   = "COS_CONTAINERD"
    spot         = var.sandbox_spot
    # Changing disk size/type or image streaming recreates the sandbox nodes (and every sandbox on them):
    # suspend sessions first (`hangar session suspend`), they resume from their checkpoints afterwards.
    disk_size_gb = var.sandbox_disk_gb
    disk_type    = var.sandbox_disk_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    sandbox_config {
      type = "GVISOR"
    }
    # Image streaming: pods start before the whole environment image is pulled (Artifact Registry images).
    dynamic "gcfs_config" { # only when on, so existing pools see no change
      for_each = var.image_streaming ? [1] : []
      content {
        enabled = true
      }
    }
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

# Optional Kata pool (hardware-virtualized microVMs). After apply, install kata-deploy on it
# (see docs/gke.md) and set HANGAR_K8S_RUNTIME_CLASS=kata-qemu-runtime-rs.
resource "google_container_node_pool" "kata" {
  count    = var.enable_kata_pool ? 1 : 0
  name     = "sandboxes-kata"
  cluster  = google_container_cluster.hangar.name
  location = var.zone

  autoscaling {
    min_node_count = 0
    max_node_count = var.sandbox_max_nodes
  }

  node_config {
    machine_type = var.kata_machine_type
    image_type   = "UBUNTU_CONTAINERD"
    spot         = var.sandbox_spot
    disk_size_gb = 200
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    # kata-deploy's RuntimeClass selects nodes by the two katacontainers labels, which kata-deploy
    # only adds after installing. Pre-labelling the pool puts them in the autoscaler's node
    # template, so a pending Kata pod can scale the pool up from zero. A pod that lands before the
    # install finishes fails sandbox creation briefly and the kubelet retries.
    labels = {
      "hangar.dev/runtime"                    = "kata"
      "katacontainers.io/kata-runtime"        = "true"
      "kata-deploy.katacontainers.io/default" = "true"
    }
    taint {
      key    = "hangar.dev/runtime"
      value  = "kata"
      effect = "NO_SCHEDULE"
    }
    advanced_machine_features {
      threads_per_core             = 0
      enable_nested_virtualization = true
    }
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

# The Agent Sandbox add-on (and optionally Pod snapshots) is enabled with gcloud until the
# provider exposes it as a cluster field.
resource "terraform_data" "agent_sandbox" {
  triggers_replace = [google_container_cluster.hangar.id, var.enable_pod_snapshots]
  provisioner "local-exec" {
    command = join(" ", compact([
      "gcloud beta container clusters update ${google_container_cluster.hangar.name}",
      "--location ${var.zone} --project ${var.project_id} --enable-agent-sandbox",
      var.enable_pod_snapshots ? "--enable-pod-snapshots" : "",
      "--quiet",
    ]))
  }
  depends_on = [google_container_node_pool.sandboxes]
}

# --------------------------------------------------------------------------- identity
resource "google_service_account" "hangar" {
  account_id   = "hangar"
  display_name = "hangar control plane"
}

resource "google_project_iam_member" "hangar" {
  for_each = toset([
    "roles/artifactregistry.writer",
    "roles/cloudbuild.builds.editor",
    "roles/storage.admin", # Cloud Build source staging bucket
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${local.sa_email}"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.hangar.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[hangar/hangar]"
  # The PROJECT.svc.id.goog identity pool only exists once a cluster with Workload Identity does.
  depends_on = [google_container_cluster.hangar]
}

# In-cluster builds run Cloud Build *as* the hangar service account (HANGAR_K8S_BUILD_SERVICE_ACCOUNT),
# which needs actAs on itself; otherwise Cloud Build uses the default compute SA, which hangar can't act as.
resource "google_service_account_iam_member" "hangar_self_user" {
  service_account_id = google_service_account.hangar.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.sa_email}"
}

resource "google_compute_global_address" "hangar" {
  name = "hangar-ip"
}

# --------------------------------------------------------------------------- pod snapshots
# Bucket + IAM for GKE Pod snapshots (full-state forks). Apply deploy/k8s/pod-snapshots.yaml after.
data "google_project" "this" {}

resource "google_storage_bucket" "pod_snapshots" {
  count                       = var.enable_pod_snapshots ? 1 : 0
  name                        = "${var.project_id}-hangar-snapshots"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  hierarchical_namespace {
    enabled = true
  }
  soft_delete_policy {
    retention_duration_seconds = 0
  }
  lifecycle_rule {
    condition {
      age = 14
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_project_iam_custom_role" "pod_snapshot_rw" {
  count       = var.enable_pod_snapshots ? 1 : 0
  role_id     = "podSnapshotGcsReadWriter"
  title       = "Pod snapshot GCS read/write"
  permissions = ["storage.objects.get", "storage.objects.create", "storage.objects.delete", "storage.folders.create"]
}

locals {
  wi_pool      = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog"
  snapshot_ksa = "${local.wi_pool}/subject/ns/hangar-sandboxes/sa/hangar-sandbox"
  snapshot_ns  = "principalSet://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/namespace/hangar-sandboxes"
}

resource "google_storage_bucket_iam_member" "pod_snapshots" {
  for_each = var.enable_pod_snapshots ? {
    viewer = { role = "roles/storage.bucketViewer", member = local.snapshot_ns }
    rw     = { role = google_project_iam_custom_role.pod_snapshot_rw[0].id, member = local.snapshot_ksa }
    user   = { role = "roles/storage.objectUser", member = local.snapshot_ksa }
    robot  = { role = "roles/storage.objectUser", member = "serviceAccount:service-${data.google_project.this.number}@container-engine-robot.iam.gserviceaccount.com" }
  } : {}
  bucket     = google_storage_bucket.pod_snapshots[0].name
  role       = each.value.role
  member     = each.value.member
  depends_on = [google_container_cluster.hangar] # the identity pool appears with the cluster
}

# --------------------------------------------------------------------------- session checkpoints (spec 0010)
# Durable storage for session checkpoints: set HANGAR_SESSION_STORE=gs://<bucket> (hangar-site config map).
resource "google_storage_bucket" "sessions" {
  count                       = var.enable_session_bucket ? 1 : 0
  name                        = "${var.project_id}-hangar-sessions"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  # Off by default: hangar deletes old checkpoints itself, and a suspended session's newest checkpoint is
  # its only copy of the work, however old it is.
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
}

resource "google_storage_bucket_iam_member" "sessions" {
  count  = var.enable_session_bucket ? 1 : 0
  bucket = google_storage_bucket.sessions[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${local.sa_email}"
}
