# Opt-in (var.power_schedule): the sandbox node pools to 0 at night and back in the morning, by deploy/gke/power.sh
# in two Cloud Run jobs that Cloud Scheduler starts, as in deploy/terraform/gke/power.tf. Hangar and the pools that
# don't match power_schedule.pools stay up; nothing is deleted. Unset: no job, no scheduler, no grant.

locals {
  power = var.power_schedule == null ? {} : {
    up   = var.power_schedule.up_cron
    down = var.power_schedule.down_cron
  }
  power_apis = var.power_schedule != null && var.enable_apis ? toset(["run.googleapis.com", "cloudscheduler.googleapis.com"]) : toset([])
  # The script ships next to this module in the hangar repository; a source that copies only this directory
  # (a registry or a bare local path) has no deploy/gke/power.sh, which the precondition below reports.
  power_script_path = "${path.module}/../../../gke/power.sh"
  power_script      = fileexists(local.power_script_path) ? file(local.power_script_path) : ""
}

resource "google_project_service" "power" {
  for_each           = local.power_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "power" {
  count        = var.power_schedule == null ? 0 : 1
  project      = var.project_id
  account_id   = "${var.name}-power"
  display_name = "hangar power schedule (scales the sandbox node pools)"
}

# Resizing node pools, writing the cluster's labels and scaling the hangar Deployment.
resource "google_project_iam_member" "power" {
  count   = var.power_schedule == null ? 0 : 1
  project = var.project_id
  role    = "roles/container.admin"
  member  = "serviceAccount:${google_service_account.power[0].email}"
}

resource "google_cloud_run_v2_job" "power" {
  for_each            = local.power
  project             = var.project_id
  name                = "${var.name}-power-${each.key}"
  location            = var.region
  deletion_protection = false
  template {
    task_count = 1
    template {
      service_account = google_service_account.power[0].email
      timeout         = "2400s"
      max_retries     = 1
      containers {
        image   = var.power_schedule.image
        command = ["bash", "-c"]
        args    = [local.power_script, "power.sh", each.key]
        env {
          name  = "PROJECT"
          value = var.project_id
        }
        env {
          name  = "ZONE"
          value = var.power_schedule.zone
        }
        env {
          name  = "CLUSTER"
          value = var.power_schedule.cluster
        }
        env {
          name  = "POWER_POOLS"
          value = var.power_schedule.pools
        }
        env {
          name  = "HANGAR_POWER_INSTALL"
          value = "1"
        }
        env {
          name  = "POWER_SCHEDULED"
          value = "1"
        }
        env {
          name  = "POWER_UP_CRON"
          value = var.power_schedule.up_cron
        }
        env {
          name  = "POWER_TIME_ZONE"
          value = var.power_schedule.time_zone
        }
        env {
          name  = "POWER_WARM_MINUTES"
          value = tostring(var.power_schedule.warm_minutes)
        }
        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = local.power_script != ""
      error_message = "power_schedule needs deploy/gke/power.sh next to this module: use a git source of the whole repository, git::https://github.com/Sean-Koval/hangar.git//deploy/terraform/modules/hangar-workload?ref=<tag>."
    }
  }
  depends_on = [google_project_service.power]
}

resource "google_cloud_run_v2_job_iam_member" "power" {
  for_each = local.power
  project  = var.project_id
  name     = google_cloud_run_v2_job.power[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.power[0].email}"
}

# `hangar power up` runs the power-up job as hangar's own service account (HANGAR_POWER_UP_JOB).
resource "google_cloud_run_v2_job_iam_member" "hangar_power_up" {
  count    = var.power_schedule == null ? 0 : 1
  project  = var.project_id
  name     = google_cloud_run_v2_job.power["up"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.sa_email}"
}

resource "google_cloud_scheduler_job" "power" {
  for_each         = local.power
  project          = var.project_id
  name             = "${var.name}-power-${each.key}"
  region           = var.region
  schedule         = each.value
  time_zone        = var.power_schedule.time_zone
  attempt_deadline = "180s"
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.power[each.key].name}:run"
    oauth_token {
      service_account_email = google_service_account.power[0].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_project_service.power, google_cloud_run_v2_job_iam_member.power]
}
