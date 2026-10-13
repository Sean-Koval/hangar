variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  description = "Zonal cluster keeps a solo setup cheap; switch location to var.region for HA."
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  type    = string
  default = "hangar"
}

variable "release_channel" {
  description = "Agent Sandbox needs GKE 1.35.2-gke.1210000+; RAPID gets there first."
  type        = string
  default     = "RAPID"
}

variable "sandbox_machine_type" {
  description = "Nodes that run gVisor sandboxes. Each sandbox reserves its env's CPU/memory."
  type        = string
  default     = "n2-standard-8"
}

variable "sandbox_max_nodes" {
  type    = number
  default = 4
}

variable "sandbox_spot" {
  description = "Spot VMs cut sandbox cost ~60-90%; sandboxes are disposable, so this is usually fine."
  type        = bool
  default     = true
}

variable "enable_kata_pool" {
  description = "Adds an Intel N2 Ubuntu pool with nested virtualization for Kata (real microVM) sandboxes."
  type        = bool
  default     = false
}

variable "kata_machine_type" {
  description = "Must be Intel (N2/C3/N4); E2, N2D (AMD) and T2A (Arm) lack nested virt on GKE."
  type        = string
  default     = "n2-standard-8"
}

variable "enable_pod_snapshots" {
  type    = bool
  default = false
}

variable "domain" {
  description = "DNS name for the dashboard/MCP endpoint (point an A record at the static IP output)."
  type        = string
  default     = ""
}

variable "sandbox_disk_gb" {
  description = "Boot disk per sandbox node. pd-balanced/pd-ssd count against SSD_TOTAL_GB (500 GB default quota): 100 leaves room to scale. Changing it recreates the sandbox nodes."
  type        = number
  default     = 200
}

variable "sandbox_disk_type" {
  description = "Boot disk type for sandbox nodes (null = GKE's default). Changing it recreates the sandbox nodes."
  type        = string
  default     = null
}

variable "image_streaming" {
  description = "GKE image streaming for sandbox nodes: pods start before the whole image is pulled. Turning it on recreates the sandbox nodes."
  type        = bool
  default     = false
}

variable "enable_session_bucket" {
  description = "GCS bucket for session checkpoints (HANGAR_SESSION_STORE=gs://<bucket>)."
  type        = bool
  default     = true
}

variable "session_retention_days" {
  description = "Delete checkpoint objects older than this many days; 0 = never. A suspended session's newest checkpoint is its only copy of the work, so set this longer than any session should sit suspended."
  type        = number
  default     = 0
}

variable "enable_power_schedule" {
  description = "Scale every node pool to 0 at power_down_cron and back at power_up_cron (deploy/gke/power.sh), via Cloud Scheduler and Cloud Run jobs."
  type        = bool
  default     = false
}

variable "power_up_cron" {
  type    = string
  default = "0 10 * * *"
}

variable "power_down_cron" {
  type    = string
  default = "0 1 * * *"
}

variable "power_time_zone" {
  type    = string
  default = "America/New_York"
}

variable "power_image" {
  description = "Image the power jobs run in: needs gcloud and python3 (kubectl is installed at run time if missing)."
  type        = string
  default     = "gcr.io/google.com/cloudsdktool/google-cloud-cli:stable"
}
