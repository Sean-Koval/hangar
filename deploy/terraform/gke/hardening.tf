# --------------------------------------------------------------------------- cluster hardening (KAN-180)
# The CIS GKE baseline for the cluster in main.tf: deletion protection, inter-node encryption, secrets encryption with
# Cloud KMS, shielded nodes, the security posture, image scanning, and optional private nodes and authorized networks.
# docs/security/gke-cluster.md maps each to its CIS control and says what it does to an existing cluster.
#
# New clusters get the secure setting. Every change here applies in place on an existing cluster except Secure Boot:
# the provider marks node_config.shielded_instance_config ForceNew, so turning it on replaces every node pool. It
# therefore defaults to off (today's pools); terraform.tfvars.example turns it on for new installs.

variable "deletion_protection" {
  description = "Terraform refuses to destroy the cluster. Set false (and apply) before a deliberate terraform destroy."
  type        = bool
  default     = true
}

variable "inter_node_encryption" {
  description = "Inter-node transparent encryption (WireGuard, Dataplane V2): pod traffic between nodes is encrypted, git grants to hangar-gateway:8788 included. Turning it on restarts every node (several hours on a busy cluster): suspend sessions first. Not with intranode visibility or FQDN network policies; at most 500 nodes."
  type        = bool
  default     = true
}

variable "secrets_encryption" {
  description = "Application-layer encryption of Kubernetes Secrets with a Cloud KMS key (secrets_kms_key, or one created here). Turning it on restarts the zonal control plane: the Kubernetes API is down while GKE re-encrypts every Secret."
  type        = bool
  default     = true
}

variable "secrets_kms_key" {
  description = "An existing Cloud KMS key for secrets encryption (projects/P/locations/REGION/keyRings/R/cryptoKeys/K), in the cluster's region. Empty: hangar creates key ring hangar-gke and key secrets in var.region."
  type        = string
  default     = ""
  validation {
    condition     = var.secrets_kms_key == "" || can(regex("^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+$", var.secrets_kms_key))
    error_message = "secrets_kms_key: projects/P/locations/L/keyRings/R/cryptoKeys/K, or empty."
  }
}

variable "shielded_secure_boot" {
  description = "Secure Boot on every node pool (integrity monitoring is always on). The provider replaces a node pool to change it, so it defaults to off for existing installs: turn it on in a maintenance window, sessions suspended. Unsigned kernel modules no longer load (the Ubuntu pools: check kata-deploy and the microVM stack first)."
  type        = bool
  default     = false
}

variable "security_posture" {
  description = "GKE security posture mode: BASIC audits workload configuration (free), ENTERPRISE adds more, DISABLED turns it off."
  type        = string
  default     = "BASIC"
  validation {
    condition     = contains(["DISABLED", "BASIC", "ENTERPRISE"], var.security_posture)
    error_message = "security_posture: DISABLED, BASIC or ENTERPRISE."
  }
}

variable "image_vulnerability_scanning" {
  description = "Artifact Analysis scans every image pushed to Artifact Registry (the hangar repository included) for OS and language-package CVEs. Billed per image scanned."
  type        = bool
  default     = true
}

variable "private_nodes" {
  description = "Nodes get internal IP addresses only, with Cloud NAT for their egress. In place: existing pools get a rolling node update. The control plane keeps its public endpoint."
  type        = bool
  default     = false
}

variable "master_authorized_networks" {
  description = "CIDR ranges allowed to reach the control plane's public endpoint. Empty: no restriction. Include wherever up.sh and kubectl run; GitHub-hosted runners (deploy.yml) are not in Google's ranges."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

variable "master_authorized_gcp_ranges" {
  description = "With master_authorized_networks set, also let Google Cloud's public ranges reach the endpoint (Cloud Build, the Cloud Run power jobs). Any Google Cloud customer's VM is in those ranges too."
  type        = bool
  default     = true
}

resource "google_project_service" "hardening" {
  for_each = toset(compact([
    var.secrets_encryption && var.secrets_kms_key == "" ? "cloudkms.googleapis.com" : "",
    var.image_vulnerability_scanning ? "containerscanning.googleapis.com" : "",
  ]))
  service            = each.value
  disable_on_destroy = false
}

# --------------------------------------------------------------------------- secrets encryption key
# A zonal cluster takes a key from the region that holds its zone. A key ring can't be deleted: on a reinstall in the
# same project, import it (terraform import google_kms_key_ring.gke[0] projects/P/locations/R/keyRings/hangar-gke).
# Destroying the key destroys etcd's Secrets with it; key versions wait 30 days before they are destroyed.
resource "google_kms_key_ring" "gke" {
  count      = var.secrets_encryption && var.secrets_kms_key == "" ? 1 : 0
  name       = "hangar-gke"
  location   = var.region
  depends_on = [google_project_service.hardening]
}

resource "google_kms_crypto_key" "gke_secrets" {
  count           = length(google_kms_key_ring.gke)
  name            = "secrets"
  key_ring        = google_kms_key_ring.gke[0].id
  rotation_period = "7776000s" # 90 days; GKE uses the newest version for new writes
}

locals {
  secrets_key = var.secrets_kms_key != "" ? var.secrets_kms_key : try(google_kms_crypto_key.gke_secrets[0].id, null)
}

# The Kubernetes Engine service agent encrypts and decrypts with the key.
resource "google_kms_crypto_key_iam_member" "gke_secrets" {
  count         = var.secrets_encryption ? 1 : 0
  crypto_key_id = local.secrets_key
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.this.number}@container-engine-robot.iam.gserviceaccount.com"
  depends_on    = [google_project_service.apis]
}

# --------------------------------------------------------------------------- private nodes' egress
# Private nodes have no external IP: sandboxes reach the internet (GitHub, registries, the egress proxy's upstreams)
# and kata-deploy pulls its images through Cloud NAT. Google APIs go over Private Google Access, which GKE turns on for
# the subnet. The cluster uses the project's default network.
resource "google_compute_router" "nat" {
  count   = var.private_nodes ? 1 : 0
  name    = "hangar-nat"
  region  = var.region
  network = "default"
}

resource "google_compute_router_nat" "nat" {
  count                              = var.private_nodes ? 1 : 0
  name                               = "hangar-nat"
  router                             = google_compute_router.nat[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
