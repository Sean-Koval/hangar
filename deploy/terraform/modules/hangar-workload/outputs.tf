output "gcp_service_account" {
  description = "hangar's Google service account: gcp_service_account in your overlay's hangar-deploy ConfigMap (the gke-workload-identity component)."
  value       = local.sa_email
}

output "registry" {
  description = "HANGAR_REGISTRY for your overlay's hangar-site ConfigMap (null without var.registry)."
  value       = var.registry == null ? null : "${local.registry_location}-docker.pkg.dev/${var.project_id}/${var.registry.repository_id}"
}

output "session_bucket_url" {
  description = "HANGAR_SESSION_STORE for hangar-site."
  value       = var.enable_session_bucket ? "gs://${google_storage_bucket.sessions[0].name}" : null
}

output "run_bucket_url" {
  description = "outbox_url in hangar-deploy (the outbox component), which sets HANGAR_OUTBOX_URL."
  value       = var.enable_run_bucket ? "gs://${google_storage_bucket.runs[0].name}" : null
}

output "static_ip_name" {
  description = "static_ip_name in hangar-deploy (the ingress-gce component)."
  value       = var.create_static_ip ? google_compute_global_address.hangar[0].name : null
}

output "static_ip" {
  description = "Point your domain's A record here."
  value       = var.create_static_ip ? google_compute_global_address.hangar[0].address : null
}

output "build_service_account" {
  description = "HANGAR_K8S_BUILD_SERVICE_ACCOUNT for hangar-site, when enable_cloud_build is on."
  value       = var.enable_cloud_build ? local.sa_email : null
}

output "build_staging" {
  description = "HANGAR_BUILD_STAGING for hangar-site, when enable_cloud_build is on (KAN-172)."
  value       = var.enable_cloud_build ? "gs://${google_storage_bucket.build_staging[0].name}" : null
}

output "power_up_job" {
  description = "HANGAR_POWER_UP_JOB for hangar-site, when the power schedule is on."
  value       = var.power_schedule == null ? null : "projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.power["up"].name}"
}
