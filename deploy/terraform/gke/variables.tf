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
  description = "Boot disk per sandbox node. pd-balanced counts against SSD_TOTAL_GB (500 GB default quota)."
  type        = number
  default     = 100
}

variable "sandbox_disk_type" {
  type    = string
  default = "pd-balanced"
}

variable "image_streaming" {
  description = "GKE image streaming for sandbox nodes: start pods before the full image is pulled."
  type        = bool
  default     = true
}

variable "enable_session_bucket" {
  description = "GCS bucket for session checkpoints (HANGAR_SESSION_STORE=gs://<bucket>)."
  type        = bool
  default     = true
}

variable "session_retention_days" {
  description = "Delete checkpoint objects older than this (suspended sessions keep their newest checkpoints)."
  type        = number
  default     = 90
}
