variable "project_id" {
  description = "The Google Cloud project hangar's identity, buckets and registry live in (usually the cluster's)."
  type        = string
}

variable "region" {
  description = "Region for the buckets and the registry."
  type        = string
}

variable "name" {
  description = "Prefix for everything this module creates (service account, buckets, IP). Change it to install a second hangar in the same project."
  type        = string
  default     = "hangar"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.name))
    error_message = "name: 3-21 lower-case letters, digits and dashes, starting with a letter."
  }
}

variable "enable_apis" {
  description = "Enable the Google APIs this module uses (IAM, Storage, and Artifact Registry, Cloud Build, Run and Scheduler when their features are on). Turn off where a platform team manages project services."
  type        = bool
  default     = true
}

# --------------------------------------------------------------------------- identity
variable "namespace" {
  description = "Kubernetes namespace of the control plane (deploy/kustomize/base uses hangar)."
  type        = string
  default     = "hangar"
}

variable "kubernetes_service_account" {
  description = "Kubernetes service account of the control plane (deploy/kustomize/base uses hangar)."
  type        = string
  default     = "hangar"
}

variable "workload_identity_pool" {
  description = "The cluster's Workload Identity pool. null: <project_id>.svc.id.goog, the pool of a cluster in project_id."
  type        = string
  default     = null
}

variable "project_roles" {
  description = "Project roles for hangar's service account, beyond the bucket and registry grants this module makes."
  type        = list(string)
  default     = ["roles/logging.logWriter"]
}

# --------------------------------------------------------------------------- registry
variable "registry" {
  description = "Where hangar pushes environment images and harness packs (HANGAR_REGISTRY). create = true makes an Artifact Registry repository; false grants hangar writer on an existing one with that location and repository_id. null: no registry grant (you grant it, or hangar only pulls prebuilt images)."
  type = object({
    create        = bool
    repository_id = optional(string, "hangar")
    location      = optional(string)
    keep_count    = optional(number, 10)
  })
  default = null
}

variable "enable_cloud_build" {
  description = "Let hangar build environment images with Cloud Build as its own service account (HANGAR_K8S_BUILD_SERVICE_ACCOUNT): roles/cloudbuild.builds.editor and roles/storage.admin on the project (Cloud Build's source staging bucket), and actAs on itself. Off: build in CI (`hangar images build`) and let hangar only pull."
  type        = bool
  default     = false
}

# --------------------------------------------------------------------------- buckets
variable "enable_session_bucket" {
  description = "A bucket for session checkpoints (HANGAR_SESSION_STORE, docs/sessions.md), objectAdmin for hangar."
  type        = bool
  default     = true
}

variable "session_retention_days" {
  description = "Delete checkpoints older than this many days. 0 (the default): never; hangar deletes old checkpoints itself, and a suspended session's newest checkpoint is its only copy of the work."
  type        = number
  default     = 0
}

variable "enable_run_bucket" {
  description = "A bucket for run capture (HANGAR_OUTBOX_URL, docs/api/outbox.md): hangar may only create objects in it."
  type        = bool
  default     = true
}

variable "run_bucket_readers" {
  description = "Members that may read the run bucket, e.g. flightdeck's service account (serviceAccount:...)."
  type        = list(string)
  default     = []
}

variable "run_tape_days" {
  description = "Delete model-gateway tapes (v1/tapes/) in the run bucket after this many days."
  type        = number
  default     = 30
}

variable "force_destroy_buckets" {
  description = "Let terraform destroy delete the buckets with their objects in them."
  type        = bool
  default     = false
}

# --------------------------------------------------------------------------- ingress
variable "create_static_ip" {
  description = "A global static IP for GKE Ingress (the ingress-gce component's static_ip_name). Off for ingress-nginx or an IP you already have."
  type        = bool
  default     = false
}

# --------------------------------------------------------------------------- power schedule (opt-in)
variable "power_schedule" {
  description = "Opt-in: scale the cluster's sandbox node pools to 0 at night and back in the morning with deploy/gke/power.sh, from Cloud Run jobs that Cloud Scheduler starts. It needs roles/container.admin on the project for its own service account and a zonal cluster. null (the default): nothing is scheduled and nothing is granted."
  type = object({
    cluster      = string
    zone         = string
    pools        = optional(string, "^sandboxes-")
    up_cron      = optional(string, "50 9 * * *")
    down_cron    = optional(string, "0 1 * * *")
    time_zone    = optional(string, "Etc/UTC")
    warm_minutes = optional(number, 10)
    image        = optional(string, "gcr.io/google.com/cloudsdktool/google-cloud-cli:stable")
  })
  default = null
}
