output "registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.hangar.repository_id}"
}

output "service_account" {
  value = google_service_account.hangar.email
}

output "static_ip" {
  value = google_compute_global_address.hangar.address
}

output "get_credentials" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.hangar.name} --zone ${var.zone} --project ${var.project_id}"
}

output "next_steps" {
  value = <<-EOT
    1. ${"gcloud container clusters get-credentials ${google_container_cluster.hangar.name} --zone ${var.zone} --project ${var.project_id}"}
    2. docker buildx build --platform linux/amd64 -t ${var.region}-docker.pkg.dev/${var.project_id}/hangar/control-plane:latest --push .
    3. In deploy/k8s: replace PROJECT_ID / REGION / hangar.example.com, create the hangar secret, then
       kubectl apply -k deploy/k8s
    4. Point ${var.domain != "" ? var.domain : "<your domain>"} at ${google_compute_global_address.hangar.address}
    5. HANGAR_URL=https://<domain> HANGAR_TOKEN=... hangar env publish base --provider kubernetes
  EOT
}

output "pod_snapshot_bucket" {
  value = var.enable_pod_snapshots ? google_storage_bucket.pod_snapshots[0].name : null
}

output "session_bucket" {
  value = var.enable_session_bucket ? "gs://${google_storage_bucket.sessions[0].name}" : null
}

output "build_staging" {
  description = "HANGAR_BUILD_STAGING: where Cloud Build stages hangar's build sources (KAN-172)"
  value       = "gs://${google_storage_bucket.build_staging.name}"
}
