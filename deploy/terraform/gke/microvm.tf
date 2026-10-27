# --------------------------------------------------------------------------- microVM tier (optional, off)
# A dedicated nested-virtualization pool for a snapshot-restoring microVM stack: CubeSandbox's node DaemonSets or
# E2B's self-hosted (Embed) orchestrator. Hangar drives either through its e2b provider (HANGAR_E2B_FLAVOR=cube |
# selfhosted) as the `fast` tier. docs/microvm.md has the whole setup; this only makes the nodes.
#
# What this pool is and isn't:
# - /dev/kvm through GCE nested virtualization: C3/C4/C4D/N2/N4/N4D (not E2, N2D, T2A/T2D, Arm). Google states a
#   CPU penalty of 10% or more, more for I/O. GKE can't run -metal machine types; bare metal is docs/microvm.md's
#   option B, outside GKE.
# - Ubuntu, not COS: E2B needs kernel >= 6.8, cgroup v2 and 2 MiB hugepages reserved before the kubelet starts, and
#   both stacks load kernel modules (NBD, TAP). Nested virtualization can't be turned on for an existing pool.
# - Privileged, hostNetwork Pods run here (the microVM stack must reach /dev/kvm): the taint keeps everything else,
#   hangar's own gVisor sandboxes included, off these nodes.
# - A fixed size, not scale-to-zero: templates and snapshots are cached on the node's disk (CubeSandbox's are local
#   unless its S3 backend is on), and E2B Embed is a one-node StatefulSet.

variable "enable_microvm_pool" {
  description = "Adds a nested-virtualization Ubuntu pool for CubeSandbox or self-hosted E2B (the fast tier). Off by default."
  type        = bool
  default     = false
}

variable "microvm_machine_type" {
  description = "A series with nested virtualization: c3, c4, c4d, n2, n4, n4d (c4/c4d take Hyperdisk only; set microvm_disk_type)."
  type        = string
  default     = "c3-standard-8"
  validation {
    condition     = can(regex("^(c3|c4|c4d|n2|n4|n4d)-", var.microvm_machine_type)) && !can(regex("-metal$", var.microvm_machine_type))
    error_message = "Nested virtualization on GKE needs C3, C4, C4D, N2, N4 or N4D machines (not E2, N2D, T2A/T2D or -metal)."
  }
}

variable "microvm_node_count" {
  description = "Nodes in the pool. Fixed: the stack keeps templates and snapshots on each node."
  type        = number
  default     = 1
}

variable "microvm_spot" {
  description = "Spot nodes lose their cached snapshots when reclaimed; sessions then resume from checkpoints."
  type        = bool
  default     = false
}

variable "microvm_disk_gb" {
  description = "Boot disk: images, the template rootfs cache and snapshot memory files."
  type        = number
  default     = 300
}

variable "microvm_disk_type" {
  description = "Boot disk type (null = GKE's default; c4 and c4d need a hyperdisk type)."
  type        = string
  default     = null
}

variable "microvm_hugepages_2m" {
  description = "2 MiB hugepages reserved at boot: E2B needs them (its Embed default is 4 GiB = 2048); CubeSandbox doesn't (0)."
  type        = number
  default     = 2048
}

variable "microvm_local_ssds" {
  description = "Raw local NVMe SSDs (with a -lssd machine type), which CubeSandbox's installer formats as XFS for /data/cubelet (reflink clones). 0 = none."
  type        = number
  default     = 0
}

resource "google_container_node_pool" "microvm" {
  count      = var.enable_microvm_pool ? 1 : 0
  name       = "sandboxes-microvm"
  cluster    = google_container_cluster.hangar.name
  location   = var.zone
  node_count = var.microvm_node_count

  node_config {
    machine_type = var.microvm_machine_type
    image_type   = "UBUNTU_CONTAINERD"
    spot         = var.microvm_spot
    disk_size_gb = var.microvm_disk_gb
    disk_type    = var.microvm_disk_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = {
      "hangar.dev/runtime"    = "microvm"
      "nested-virtualization" = "enabled" # the label GKE's own nested-virtualization guide schedules on
    }
    taint {
      key    = "hangar.dev/runtime"
      value  = "microvm"
      effect = "NO_SCHEDULE"
    }
    advanced_machine_features {
      threads_per_core             = 0
      enable_nested_virtualization = true
    }
    dynamic "linux_node_config" {
      for_each = var.microvm_hugepages_2m > 0 ? [1] : []
      content {
        hugepages_config {
          hugepage_size_2m = var.microvm_hugepages_2m
        }
      }
    }
    dynamic "local_nvme_ssd_block_config" {
      for_each = var.microvm_local_ssds > 0 ? [1] : []
      content {
        local_ssd_count = var.microvm_local_ssds
      }
    }
    shielded_instance_config { # KAN-180; secure boot forces a new pool on an existing cluster (hardening.tf)
      enable_secure_boot          = var.shielded_secure_boot
      enable_integrity_monitoring = true
    }
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
  dynamic "network_config" { # KAN-180: private nodes on existing pools too (in place, a rolling node update)
    for_each = var.private_nodes ? [1] : []
    content {
      enable_private_nodes = true
    }
  }
}

output "microvm_pool" {
  description = "Schedule the microVM stack here: nodeSelector hangar.dev/runtime=microvm, tolerate the same taint, privileged."
  value       = var.enable_microvm_pool ? google_container_node_pool.microvm[0].name : null
}
