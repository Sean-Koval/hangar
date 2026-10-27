# The overnight lane (#186; docs/gke.md section 6): one Spot gVisor node kept up 01:00-10:00 while the other sandbox
# pools are at 0, for intake jobs of repos set to `overnight: true` (or labelled `lane: overnight`) and evals.
#
# - A fixed-size pool, created at 0 nodes: power.sh resizes it to 1 at `down` while the lane is on (`hangar power
#   overnight on`, which needs 60% agent PR yield over 14 days, or --force), and back to 0 at the morning `up` once
#   the other pools are ready. Nothing else scales it, so it never runs by day.
# - Labelled and tainted hangar.dev/lane=overnight: gVisor sandboxes tolerate the taint (power.LANE_TOLERATION), so
#   at night they land on it; no other workload does.
# - Spot e2-standard-16 by default, not the sandbox pool's n2d-standard-16: E2 draws on the regional CPUS quota, not
#   N2D_CPUS, which the sandbox pool may already use up (one project here sits at 16/16), so the lane needs no N2D
#   quota request. E2 runs on Intel or AMD, and GKE Sandbox turns SMT off on Intel, so a node shows 8 or 16 vCPUs:
#   HANGAR_OVERNIGHT_LANE_SANDBOXES defaults to the 7 that fit either way. About 9 h x $0.3217 x 30 = $87/month
#   (docs/team-scale.md, "Cost"). A preemption has no guaranteed replacement: the lane's running turns are marked
#   infra_error/preempted and resume at 10:00.
resource "google_container_node_pool" "overnight" {
  count      = var.enable_overnight_lane ? 1 : 0
  name       = "sandboxes-overnight"
  cluster    = google_container_cluster.hangar.name
  location   = var.zone
  node_count = 0

  node_config {
    machine_type = var.overnight_machine_type
    image_type   = "COS_CONTAINERD"
    spot         = true
    disk_size_gb = var.sandbox_disk_gb
    disk_type    = var.sandbox_disk_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    sandbox_config {
      type = "GVISOR"
    }
    labels = {
      "hangar.dev/lane" = "overnight"
    }
    taint {
      key    = "hangar.dev/lane"
      value  = "overnight"
      effect = "NO_SCHEDULE"
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

  lifecycle {
    ignore_changes = [node_count] # power.sh owns the size (0 by day, 1 at night)
  }
}

output "overnight_pool" {
  description = "The overnight lane's pool (0 nodes until power.sh down with the lane on). Then: hangar power overnight on."
  value       = var.enable_overnight_lane ? google_container_node_pool.overnight[0].name : null
}
