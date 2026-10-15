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
  description = "Nodes that run gVisor sandboxes. AMD (N2D): GKE Sandbox turns Hyper-Threading off on Intel machine types, which leaves an n2-standard-8 with 4 usable vCPUs; AMD keeps SMT on. An n2d-standard-16 holds 14 sandboxes of 2 vCPU / 4 GiB (memory-bound; docs/team-scale.md). Changing it recreates the sandbox nodes: suspend sessions first."
  type        = string
  default     = "n2d-standard-16"
}

variable "sandbox_max_nodes" {
  description = "Most sandbox nodes the autoscaler adds. 10 × n2d-standard-16 holds ~140 sandboxes of 2 vCPU / 4 GiB: a team of 6 with 10 sessions each, job slots, warm pools and a preempted Spot node's worth of headroom (docs/team-scale.md). Needs N2D_CPUS and SSD_TOTAL_GB quota to match."
  type        = number
  default     = 10
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
  description = "Boot disk per sandbox node. pd-balanced/pd-ssd count against SSD_TOTAL_GB (500 GB default quota). Sandbox workspaces and images share it: 200 leaves ~104 GiB of ephemeral storage for 14 sandboxes a node, 100 only ~49 GiB (docs/team-scale.md). Changing it recreates the sandbox nodes."
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
  description = "Scale the sandbox node pools (gVisor, Kata, microVM) to 0 at power_down_cron and back at power_up_cron (deploy/gke/power.sh), via Cloud Scheduler and Cloud Run jobs. Hangar and the system pool stay up."
  type        = bool
  default     = false
}

variable "power_up_cron" {
  description = "When the power-up job starts: power_warm_minutes before people arrive, so the floor node is up and the images are pulled by then."
  type        = string
  default     = "50 9 * * *"
}

variable "power_warm_minutes" {
  description = "How long the power-up takes (floor node, image prepull): hangar tells people sandboxes are ready this long after power_up_cron (10:00 with the defaults)."
  type        = number
  default     = 10
}

variable "sandbox_warm_nodes" {
  description = "gVisor sandbox nodes kept up while the pools are on (the autoscaler's minimum), so the first create of the day lands on a node that is already up with its images pulled. null = 1 with the power schedule (the pool is off at night anyway), 0 without it (a node around the clock costs ~$240/month on Spot)."
  type        = number
  default     = null
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
