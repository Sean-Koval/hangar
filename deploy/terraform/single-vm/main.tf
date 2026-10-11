terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "machine_type" {
  description = "Intel N2/C3/N4 for nested virtualization (needed for Kata microVMs)."
  type        = string
  default     = "n2-standard-16"
}

variable "disk_gb" {
  type    = number
  default = 300
}

variable "repo_url" {
  description = "Git URL of your hangar repo (the VM clones and runs it)."
  type        = string
}

variable "runtime" {
  description = "Sandbox runtime on the VM: runc | runsc (gVisor) | kata (microVM, needs nested virt)."
  type        = string
  default     = "runsc"
}

variable "allowed_cidrs" {
  description = "Who may reach :8787 directly. Leave empty and use an IAP tunnel or Tailscale instead."
  type        = list(string)
  default     = []
}

variable "spot" {
  type    = bool
  default = false
}

resource "google_compute_instance" "hangar" {
  name         = "hangar"
  zone         = var.zone
  machine_type = var.machine_type
  tags         = ["hangar"]

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = var.disk_gb
      type  = "pd-balanced"
    }
  }

  advanced_machine_features {
    enable_nested_virtualization = true
  }

  scheduling {
    preemptible                 = var.spot
    provisioning_model          = var.spot ? "SPOT" : "STANDARD"
    automatic_restart           = !var.spot
    instance_termination_action = var.spot ? "STOP" : null
  }

  network_interface {
    network = "default"
    access_config {}
  }

  service_account {
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  metadata_startup_script = templatefile("${path.module}/startup.sh.tftpl", {
    repo_url = var.repo_url
    runtime  = var.runtime
  })
}

resource "google_compute_firewall" "iap" {
  name          = "hangar-iap"
  network       = "default"
  source_ranges = ["35.235.240.0/20"] # Google IAP TCP forwarding
  target_tags   = ["hangar"]
  allow {
    protocol = "tcp"
    ports    = ["22", "8787"]
  }
}

resource "google_compute_firewall" "direct" {
  count         = length(var.allowed_cidrs) > 0 ? 1 : 0
  name          = "hangar-direct"
  network       = "default"
  source_ranges = var.allowed_cidrs
  target_tags   = ["hangar"]
  allow {
    protocol = "tcp"
    ports    = ["8787"]
  }
}

output "tunnel" {
  description = "Reach the dashboard + MCP endpoint at http://localhost:8787 through IAP."
  value       = "gcloud compute start-iap-tunnel hangar 8787 --local-host-port=localhost:8787 --zone ${var.zone} --project ${var.project_id}"
}

output "bootstrap_token" {
  value = "gcloud compute ssh hangar --zone ${var.zone} --tunnel-through-iap -- sudo cat /opt/hangar/.bootstrap-token"
}
