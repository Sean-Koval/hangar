# --------------------------------------------------------------------------- power schedule
# Turns the sandbox node pools off at night and back on in the morning (deploy/gke/power.sh down / up), from two
# Cloud Run jobs that Cloud Scheduler starts. Hangar and the system pool stay up. Nothing is deleted: disks, images,
# checkpoints and the IP stay. A hold (power.sh hold / off, hangar power hold) makes both jobs skip until it ends:
# each reads the cluster's labels first and exits with a log line.
#
#   enable_power_schedule = true      # in terraform.tfvars
#   power_up_cron         = "50 9 * * *"   # floor node and image prepull; ready power_warm_minutes later
#   power_down_cron       = "0 1 * * *"
#   power_time_zone       = "America/New_York"
#
# Run a job by hand: gcloud run jobs execute hangar-power-down --region <region>. `hangar power up` runs the
# power-up job as hangar's own service account (granted below; set HANGAR_POWER_UP_JOB to output power_up_job).

locals {
  power = var.enable_power_schedule ? {
    up   = var.power_up_cron
    down = var.power_down_cron
  } : {}
}

resource "google_project_service" "power" {
  for_each           = var.enable_power_schedule ? toset(["run.googleapis.com", "cloudscheduler.googleapis.com"]) : toset([])
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "power" {
  count        = var.enable_power_schedule ? 1 : 0
  account_id   = "hangar-power"
  display_name = "hangar power schedule (scales the cluster's node pools)"
}

# Resizing node pools and scaling the hangar deployment needs both the GKE API and Kubernetes access.
resource "google_project_iam_member" "power" {
  count   = var.enable_power_schedule ? 1 : 0
  project = var.project_id
  role    = "roles/container.admin"
  member  = "serviceAccount:${google_service_account.power[0].email}"
}

resource "google_cloud_run_v2_job" "power" {
  for_each            = local.power
  name                = "hangar-power-${each.key}"
  location            = var.region
  deletion_protection = false
  template {
    task_count = 1
    template {
      service_account = google_service_account.power[0].email
      timeout         = "2400s"
      max_retries     = 1
      containers {
        image   = var.power_image
        command = ["bash", "-c"]
        args    = [file("${path.module}/../../gke/power.sh"), "power.sh", each.key]
        env {
          name  = "PROJECT"
          value = var.project_id
        }
        env {
          name  = "ZONE"
          value = var.zone
        }
        env {
          name  = "CLUSTER"
          value = google_container_cluster.hangar.name
        }
        env {
          name  = "HANGAR_POWER_INSTALL"
          value = "1"
        }
        # A hold (power.sh hold/off, hangar power hold) skips the scheduled runs; by hand, `up` ends it.
        env {
          name  = "POWER_SCHEDULED"
          value = "1"
        }
        # After a power-up, main ahead of what hangar runs is reported to hangar (`hangar power`, doctor). This job
        # holds no GitHub credential: it compares anonymously (public repositories), and never starts a deploy.
        env {
          name  = "POWER_DEPLOY_REPO"
          value = var.power_deploy_repo
        }
        # What hangar tells people about the morning (when sandboxes are ready); sent at both down and up.
        env {
          name  = "POWER_UP_CRON"
          value = var.power_up_cron
        }
        env {
          name  = "POWER_TIME_ZONE"
          value = var.power_time_zone
        }
        env {
          name  = "POWER_WARM_MINUTES"
          value = tostring(var.power_warm_minutes)
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
  depends_on = [google_project_service.power]
}

resource "google_cloud_run_v2_job_iam_member" "power" {
  for_each = local.power
  name     = google_cloud_run_v2_job.power[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.power[0].email}"
}

# An admin powering the sandboxes on early (`hangar power up`) runs the power-up job as hangar's own service account.
resource "google_cloud_run_v2_job_iam_member" "hangar_power_up" {
  count    = var.enable_power_schedule ? 1 : 0
  name     = google_cloud_run_v2_job.power["up"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.sa_email}"
}

output "power_up_job" {
  description = "Set as HANGAR_POWER_UP_JOB in hangar-site, so `hangar power up` can run it."
  value       = var.enable_power_schedule ? "projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.power["up"].name}" : null
}

resource "google_cloud_scheduler_job" "power" {
  for_each         = local.power
  name             = "hangar-power-${each.key}"
  region           = var.region
  schedule         = each.value
  time_zone        = var.power_time_zone
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
