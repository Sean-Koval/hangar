# Running hangar on GKE

The architecture is one control-plane pod (in namespace `hangar`) plus sandbox pods (in namespace `hangar-sandboxes`). Sandbox pods are managed through Agent Sandbox CRDs: a SandboxTemplate and SandboxWarmPool per environment, and a SandboxClaim per sandbox. They run under gVisor by default, or Kata microVMs on an optional nested-virtualization pool.

## 1. Infrastructure

```bash
cd deploy/terraform/gke
cp terraform.tfvars.example terraform.tfvars   # set project_id, region, domain
terraform init && terraform apply
```

This creates:

- **A Standard cluster** on the RAPID channel, since Agent Sandbox needs GKE 1.35.2-gke.1210000 or later. It uses Workload Identity and Dataplane V2.
- **A system pool**, plus a gVisor sandbox pool (Spot by default) that autoscales from 0 to N nodes.
- **The Agent Sandbox add-on**, enabled through `gcloud`, and optionally Pod snapshots.
- **Supporting resources:** an Artifact Registry repo, a `hangar` Google service account bound to the pod's KSA, and a global static IP.

## 2. Control plane

```bash
$(terraform output -raw get_credentials)
REG=$(terraform output -raw registry)
docker buildx build --platform linux/amd64 -t $REG/control-plane:latest --push .

kubectl apply -f deploy/k8s/namespace.yaml
kubectl -n hangar create secret generic hangar \
  --from-literal=HANGAR_ADMIN_TOKEN=hgr_$(openssl rand -hex 24) \
  --from-literal=HANGAR_SECRET_KEY=$(python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
```

Next, edit the placeholders in `deploy/k8s`: `PROJECT_ID` and `REGION` in `rbac.yaml`, `config.yaml` and `deployment.yaml`, and the domain in `config.yaml` and `ingress.yaml`. Then deploy:

```bash
kubectl apply -k deploy/k8s
```

Point your domain's A record at `terraform output static_ip`. The managed certificate provisions in about 15 minutes.

## 3. Environment images

The control plane can build with Cloud Build (gcloud ships in the image). Building from your laptop is usually faster, because it reuses your local cache:

```bash
export HANGAR_URL=https://hangar.example.com HANGAR_TOKEN=hgr_...
uv run hangar env publish claude-python --provider kubernetes
```

`publish` asks the server for the exact build context, builds and pushes it with your local Docker, then registers the image. The server then syncs that environment's SandboxTemplate and SandboxWarmPool.

## 4. Kata microVMs (optional)

Set `enable_kata_pool = true` and re-apply. That adds an Intel N2 Ubuntu pool with nested virtualization, labeled and tainted `hangar.dev/runtime=kata`. Then:

1. Install Kata on that pool by applying kata-deploy, restricted with a nodeSelector of `hangar.dev/runtime: kata`. Follow the Agent Sandbox "Kata on GKE" example.
2. Set `HANGAR_K8S_RUNTIME_CLASS: kata-qemu` in `deploy/k8s/config.yaml` and restart hangar.

Hangar adds the matching nodeSelector and toleration to sandbox pods automatically.

## Notes

- **Network.** The default SandboxTemplate network policy allows egress to the public internet only, and blocks RFC1918 ranges and the metadata server. Agents can reach GitHub and package registries but not your VPC.
- **Expiry.** Each claim gets `lifecycle.shutdownTime` (sandbox TTL plus 5 minutes) as a backstop to hangar's reaper.
- **Storage.** The control plane keeps state in SQLite on a 20 Gi PVC. Back it up, or snapshot the disk.
