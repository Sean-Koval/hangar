# --------------------------------------------------------------------------- heavy tier (optional, off)
# Nodes for hangar's heavy tier (src/hangar/heavy.py, docs/environments.md#the-heavy-tier): sandboxes with Docker
# inside, as Kata microVMs, and the large and xlarge profiles (8 vCPU / 32 GiB / 100 GiB, 16 / 64 / 200).
#
# - Nested virtualization (Kata boots a VM per pod): C3, C4, C4D, N2, N4 or N4D. Not E2, N2D, T2A/T2D, Arm or
#   memory-optimized machines, and not -metal ones on GKE. It can't be turned on for an existing pool. Google states a
#   CPU penalty of 10% or more for nested VMs, more for I/O. (cloud.google.com/compute/docs/instances/nested-
#   virtualization/overview, cloud.google.com/kubernetes-engine/docs/how-to/nested-virtualization, both re-read
#   2026-10-15.)
# - Ubuntu: kata-deploy installs onto the node, which COS's read-only root doesn't allow.
# - Scales from zero, and its name starts with `sandboxes-`, so the power schedule (power.tf, deploy/gke/power.sh)
#   turns it off at night, and holds and `off` keep it off, like the gVisor and Kata pools. No warm floor: a heavy node
#   costs, and heavy warm pools are off unless HANGAR_HEAVY_WARM_POOLS.
# - Labelled and tainted hangar.dev/runtime=heavy: only heavy sandboxes (privileged inside their VM) land here, and
#   they land nowhere else. The kata labels let a pending pod scale the pool up before kata-deploy has run.
#
# After apply: install kata-deploy on it (docs/gke.md, "Heavy tier"), then set HANGAR_TIERS with a heavy entry.

variable "enable_heavy_pool" {
  description = "Adds the heavy tier's pool: Ubuntu, nested virtualization, for Kata sandboxes with Docker inside and the large/xlarge profiles. Off by default."
  type        = bool
  default     = false
}

variable "heavy_machine_type" {
  description = "A series with nested virtualization. n2-standard-32 (32 vCPU, 128 GiB) holds three large sandboxes, or one xlarge and one large (docs/team-scale.md, Heavy tier). c4/c4d/n4/n4d take Hyperdisk only: set heavy_disk_type."
  type        = string
  default     = "n2-standard-32"
  validation {
    condition     = can(regex("^(c3|c4|c4d|n2|n4|n4d)-", var.heavy_machine_type)) && !can(regex("-metal$", var.heavy_machine_type))
    error_message = "Nested virtualization on GKE needs C3, C4, C4D, N2, N4 or N4D machines (not E2, N2D, T2A/T2D or -metal)."
  }
}

variable "heavy_max_nodes" {
  description = "Most heavy nodes the autoscaler adds (it scales from zero). With the per-person caps (HANGAR_HEAVY_PERSON_MAX: 3 large, 1 xlarge), 4 nodes hold a team of 6 at about two heavy sandboxes each. Needs N2_CPUS (or the series') quota to match."
  type        = number
  default     = 4
}

variable "heavy_spot" {
  description = "Spot heavy nodes. Cheaper; a reclaimed node takes its sandboxes' Docker images and volumes with it (sessions resume from checkpoints)."
  type        = bool
  default     = true
}

variable "heavy_disk_gb" {
  description = "Boot disk per heavy node: every sandbox's /var/lib/docker lives there (100 GiB for large, 200 for xlarge) next to the images. 500 leaves ~350 GiB for sandboxes after GKE's reservations."
  type        = number
  default     = 500
}

variable "heavy_disk_type" {
  description = "Boot disk type (null = GKE's default, pd-balanced; c4, c4d, n4 and n4d need hyperdisk-balanced)."
  type        = string
  default     = null
}

resource "google_container_node_pool" "heavy" {
  count    = var.enable_heavy_pool ? 1 : 0
  name     = "sandboxes-heavy"
  cluster  = google_container_cluster.hangar.name
  location = var.zone

  autoscaling {
    min_node_count = 0
    max_node_count = var.heavy_max_nodes
  }

  node_config {
    machine_type = var.heavy_machine_type
    image_type   = "UBUNTU_CONTAINERD"
    spot         = var.heavy_spot
    disk_size_gb = var.heavy_disk_gb
    disk_type    = var.heavy_disk_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = {
      "hangar.dev/runtime"                    = "heavy"
      "katacontainers.io/kata-runtime"        = "true"
      "kata-deploy.katacontainers.io/default" = "true"
    }
    taint {
      key    = "hangar.dev/runtime"
      value  = "heavy"
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

output "heavy_pool" {
  description = "The heavy tier's pool. Then: kata-deploy on it, and HANGAR_TIERS='{\"heavy\": [\"kubernetes\"], ...}' in hangar-site."
  value       = var.enable_heavy_pool ? google_container_node_pool.heavy[0].name : null
}

# --------------------------------------------------------------------------- Docker Hub pull-through (optional, off)
# An Artifact Registry remote repository for Docker Hub: a cache that can't be pushed to, with Docker Hub credentials
# of your own so pulls don't hit the anonymous rate limit. Heavy sandboxes reach it only through hangar's read-only
# pull-through on the git gateway (regmirror.py), with hangar's Google identity: never directly, since *.pkg.dev also
# serves every project's writable repositories. Set HANGAR_HEAVY_MIRROR=true and HANGAR_HEAVY_MIRROR_UPSTREAM to the
# output dockerhub_mirror. The same pattern as the package mirrors (#107).

variable "enable_dockerhub_mirror" {
  description = "Adds an Artifact Registry remote repository for Docker Hub (the heavy tier's pull-through upstream)."
  type        = bool
  default     = false
}

variable "dockerhub_username" {
  description = "Docker Hub user the remote repository pulls as (authenticated pulls have a higher rate limit). Empty: anonymous."
  type        = string
  default     = ""
}

variable "dockerhub_token_secret" {
  description = "Secret Manager secret (its id, in this project) holding that user's Docker Hub access token. Needed with dockerhub_username."
  type        = string
  default     = ""
}

resource "google_project_service" "secretmanager" {
  count              = var.enable_dockerhub_mirror && var.dockerhub_username != "" ? 1 : 0
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

# Artifact Registry's service agent reads the token to authenticate to Docker Hub.
resource "google_secret_manager_secret_iam_member" "dockerhub" {
  count      = var.enable_dockerhub_mirror && var.dockerhub_username != "" ? 1 : 0
  secret_id  = var.dockerhub_token_secret
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-artifactregistry.iam.gserviceaccount.com"
  depends_on = [google_project_service.secretmanager]
}

resource "google_artifact_registry_repository" "dockerhub" {
  count         = var.enable_dockerhub_mirror ? 1 : 0
  location      = var.region
  repository_id = "dockerhub"
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  description   = "Docker Hub pull-through for hangar's heavy sandboxes (read through hangar's gateway only)"
  remote_repository_config {
    description = "Docker Hub"
    docker_repository {
      public_repository = "DOCKER_HUB"
    }
    dynamic "upstream_credentials" {
      for_each = var.dockerhub_username != "" ? [1] : []
      content {
        username_password_credentials {
          username                = var.dockerhub_username
          password_secret_version = "projects/${var.project_id}/secrets/${var.dockerhub_token_secret}/versions/latest"
        }
      }
    }
  }
  cleanup_policies {
    id     = "drop-unused"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days: a cache, refilled on the next pull
    }
  }
  depends_on = [google_project_service.apis, google_secret_manager_secret_iam_member.dockerhub]
}

output "dockerhub_mirror" {
  description = "HANGAR_HEAVY_MIRROR_UPSTREAM for hangar's Docker Hub pull-through."
  value       = var.enable_dockerhub_mirror ? "https://${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.dockerhub[0].repository_id}" : null
}
