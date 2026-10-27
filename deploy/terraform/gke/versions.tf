terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      # 6.36: in_transit_encryption_config (KAN-180). An existing install runs `terraform init -upgrade` once.
      version = ">= 6.36"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
