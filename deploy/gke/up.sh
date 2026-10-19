#!/bin/bash
# Deploy the hangar control plane onto the cluster created by deploy/terraform/gke.
#
#   bash deploy/gke/up.sh            # build the image with Cloud Build, deploy, print the URL
#   KATA=1 bash deploy/gke/up.sh     # also install Kata on the nested-virt pools (enable_kata_pool, enable_heavy_pool)
#   DOMAIN=hangar.me.dev bash ...    # use your own DNS name (A record -> terraform output static_ip)
#   PREBUILD=0 bash ...              # skip building environment images and harness packs before rollout
#
# Without DOMAIN it uses <static-ip>.sslip.io, a public wildcard DNS name that resolves to the IP, so the
# Google-managed certificate can provision with no DNS setup. Certificates take ~15-60 min the first time.
# The admin token is generated once and saved to ~/.hangar/gke-admin-token (mode 0600); it is never printed.
set -euo pipefail
cd "$(dirname "$0")/../.."
TF=deploy/terraform/gke
tfo() { terraform -chdir=$TF output -raw "$1"; }
REG=$(tfo registry); SA=$(tfo service_account); IP=$(tfo static_ip)
BUCKET=$(tfo session_bucket 2>/dev/null || true)  # null (enable_session_bucket=false) fails -raw: no bucket
PROJECT=$(cut -d/ -f2 <<<"$REG"); REGION=${REG%%-docker.pkg.dev*}
DOMAIN=${DOMAIN:-$IP.sslip.io}
IMAGE="$REG/control-plane:$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- src envs envd pyproject.toml Dockerfile || echo -dirty)"

echo "== cluster credentials"; eval "$(tfo get_credentials)"

if gcloud artifacts docker images describe "$IMAGE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "== image $IMAGE already built"
else
  echo "== building $IMAGE with Cloud Build"
  gcloud builds submit --project "$PROJECT" --tag "$IMAGE" --timeout 1800s .
fi

# Every environment image and harness pack this commit needs, built and pushed before the new control plane rolls
# out, so no create waits on a build. CI (.github/workflows/images.yml) usually built them already; then this is a
# registry lookup per tag. Docker if it runs here, else Cloud Build.
if [ "${PREBUILD:-1}" = 1 ]; then
  echo "== environment images and harness packs"
  if [ -z "${HANGAR_CLI:-}" ]; then
    if command -v uv >/dev/null; then HANGAR_CLI="uv run --quiet hangar"
    elif command -v hangar >/dev/null; then HANGAR_CLI=hangar
    else echo "the prebuild needs uv or the hangar CLI (or PREBUILD=0)" >&2; exit 1; fi
  fi
  HANGAR_REGISTRY="$REG" $HANGAR_CLI images build --provider kubernetes \
      --builder "${BUILDER:-auto}" --jobs "${JOBS:-3}" ||
    { echo "prebuild failed (above). Fix it, or deploy anyway with PREBUILD=0: creates then build what's missing." >&2
      exit 1; }
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
# Checkpoints go to the bucket terraform made, so they outlive the control plane's disk (hangar-site still wins).
# Only on a first install, or where it's already set: moving a running hangar off its disk would strand the local
# checkpoints of its suspended sessions.
if [[ "$BUCKET" == gs://* ]]; then
  live() { kubectl -n hangar get configmap "$1" -o jsonpath='{.data.HANGAR_SESSION_STORE}' 2>/dev/null || true; }
  LIVE=$(live hangar)
  if [ -n "$LIVE" ] || ! kubectl -n hangar get deployment hangar >/dev/null 2>&1; then
    sed -i "s|^  # HANGAR_SESSION_STORE: .*|  HANGAR_SESSION_STORE: \"${LIVE:-$BUCKET}\"|" "$OUT/config.yaml"
  elif [ -z "$(live hangar-site)" ]; then
    echo "note: session checkpoints stay on the control plane's disk; $BUCKET is ready for them (docs/sessions.md)"
  fi
fi
# --- ops: `hangar power up` runs the power-up job (null, so empty, without the power schedule)
POWER_JOB=$(tfo power_up_job 2>/dev/null || true)
if [[ "$POWER_JOB" == projects/* ]]; then
  sed -i "s|^  # HANGAR_POWER_UP_JOB: .*|  HANGAR_POWER_UP_JOB: \"$POWER_JOB\"|" "$OUT/config.yaml"
fi
# --- outbox (#273): run capture to the bucket terraform made, which flightdeck's compactor reads (docs/api/outbox.md).
# On every install, a running one too: nothing is stranded by turning it on. A value already in the `hangar` config map
# is kept, and hangar-site still wins. null (enable_run_bucket=false) fails -raw: the outbox stays off.
RUNS=$(tfo run_bucket 2>/dev/null || true)
OUTBOX_WAS=$(kubectl -n hangar get configmap hangar -o jsonpath='{.data.HANGAR_OUTBOX_URL}' 2>/dev/null || true)
OUTBOX=${OUTBOX_WAS:-$RUNS}
if [[ "$OUTBOX" == gs://* ]]; then
  sed -i "s|^  # HANGAR_OUTBOX_URL: .*|  HANGAR_OUTBOX_URL: \"$OUTBOX\"|" "$OUT/config.yaml"
fi
RUNNING=$(kubectl -n hangar get deployment hangar -o jsonpath='{.spec.template.spec.containers[0].image}' \
  2>/dev/null || true)
kubectl apply -k "$OUT"
# The outbox reads its URL when hangar starts: a new image restarts the pod anyway, the same image doesn't.
if [[ "$OUTBOX" == gs://* && "$OUTBOX" != "$OUTBOX_WAS" && "$RUNNING" == "$IMAGE" ]]; then
  kubectl -n hangar rollout restart deploy/hangar
fi
kubectl -n hangar rollout status deploy/hangar --timeout=10m

if [ "${KATA:-0}" = 1 ]; then
  # --- heavy: on every pool terraform labels for Kata (the Kata pool and the heavy tier's), whatever its taint
  echo "== kata-deploy on the Kata pools (katacontainers.io/kata-runtime=true: sandboxes-kata, sandboxes-heavy)"
  helm upgrade --install kata-deploy oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy \
    --version "${KATA_VERSION:-4.2.0}" --namespace kata-system --create-namespace \
    --set shims.disableAll=true --set shims.qemu-runtime-rs.enabled=true --set nfd.enabled=false \
    --set-string nodeSelector."katacontainers\.io/kata-runtime"=true \
    --set 'tolerations[0].key=hangar.dev/runtime' --set 'tolerations[0].operator=Exists' \
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
