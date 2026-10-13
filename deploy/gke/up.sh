#!/bin/bash
# Deploy the hangar control plane onto the cluster created by deploy/terraform/gke.
#
#   bash deploy/gke/up.sh            # build the image with Cloud Build, deploy, print the URL
#   KATA=1 bash deploy/gke/up.sh     # also install Kata on the nested-virt pool (enable_kata_pool=true)
#   DOMAIN=hangar.me.dev bash ...    # use your own DNS name (A record -> terraform output static_ip)
#
# Without DOMAIN it uses <static-ip>.sslip.io, a public wildcard DNS name that resolves to the IP, so the
# Google-managed certificate can provision with no DNS setup. Certificates take ~15-60 min the first time.
# The admin token is generated once and saved to ~/.hangar/gke-admin-token (mode 0600); it is never printed.
set -euo pipefail
cd "$(dirname "$0")/../.."
TF=deploy/terraform/gke
tfo() { terraform -chdir=$TF output -raw "$1"; }
REG=$(tfo registry); SA=$(tfo service_account); IP=$(tfo static_ip)
PROJECT=$(cut -d/ -f2 <<<"$REG"); REGION=${REG%%-docker.pkg.dev*}
DOMAIN=${DOMAIN:-$IP.sslip.io}
IMAGE="$REG/control-plane:$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- src envs pyproject.toml Dockerfile || echo -dirty)"

echo "== cluster credentials"; eval "$(tfo get_credentials)"

if gcloud artifacts docker images describe "$IMAGE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "== image $IMAGE already built"
else
  echo "== building $IMAGE with Cloud Build"
  gcloud builds submit --project "$PROJECT" --tag "$IMAGE" --timeout 1800s .
fi

echo "== namespaces + secret"
kubectl apply -f deploy/k8s/namespace.yaml
mkdir -p ~/.hangar && chmod 700 ~/.hangar
if ! kubectl -n hangar get secret hangar >/dev/null 2>&1; then
  ( umask 077; echo "hgr_$(openssl rand -hex 24)" > ~/.hangar/gke-admin-token )
  kubectl -n hangar create secret generic hangar \
    --from-file=HANGAR_ADMIN_TOKEN=<(tr -d '\n' < ~/.hangar/gke-admin-token) \
    --from-literal=HANGAR_SECRET_KEY="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
fi

echo "== manifests for https://$DOMAIN"
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT
cp deploy/k8s/*.yaml "$OUT"/
sed -i -e "s|REGION-docker.pkg.dev/PROJECT_ID/hangar/control-plane:latest|$IMAGE|" \
       -e "s|REGION-docker.pkg.dev/PROJECT_ID/hangar|$REG|" \
       -e "s|hangar@PROJECT_ID.iam.gserviceaccount.com|$SA|" \
       -e "s|hangar.example.com|$DOMAIN|g" "$OUT"/*.yaml
kubectl apply -k "$OUT"
kubectl -n hangar rollout status deploy/hangar --timeout=10m

if [ "${KATA:-0}" = 1 ]; then
  echo "== kata-deploy on the hangar.dev/runtime=kata pool"
  helm upgrade --install kata-deploy oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy \
    --version "${KATA_VERSION:-4.2.0}" --namespace kata-system --create-namespace \
    --set shims.disableAll=true --set shims.qemu-runtime-rs.enabled=true --set nfd.enabled=false \
    --set nodeSelector."hangar\.dev/runtime"=kata \
    --set 'tolerations[0].key=hangar.dev/runtime' --set 'tolerations[0].value=kata' \
    --set 'tolerations[0].effect=NoSchedule'
  kubectl get runtimeclass
fi

echo
echo "Dashboard + MCP:  https://$DOMAIN   (MCP endpoint https://$DOMAIN/mcp)"
echo "Admin token:      ~/.hangar/gke-admin-token"
echo "Certificate:      $(kubectl -n hangar get managedcertificate hangar -o jsonpath='{.status.certificateStatus}' 2>/dev/null || echo unknown)  (Active = ready)"
echo
echo "Next (once the certificate is Active):"
echo "  hangar login https://$DOMAIN --token-file ~/.hangar/gke-admin-token"
echo "  hangar doctor --fix          # what's missing, and the fixes it can apply"
echo "  hangar setup github          # token, webhook, settings (kept in hangar's database), test delivery"
echo "  hangar connect claude-code   # or codex, cursor, vscode, gemini, claude-desktop"
