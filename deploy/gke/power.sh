#!/bin/bash
# Turn the hangar cluster's nodes off and on. Nothing is deleted: the control plane's disk, images, session
# checkpoints and the ingress IP stay, so `up` brings back the same hangar at the same URL.
#
#   bash deploy/gke/power.sh down     # checkpoint and suspend sessions, stop hangar, every node pool to 0 nodes
#   bash deploy/gke/power.sh up       # node pools back to their autoscaling ranges, start hangar
#   bash deploy/gke/power.sh status
#
# From a laptop it reads deploy/terraform/gke's outputs. The scheduled Cloud Run jobs (terraform
# enable_power_schedule) run this same script with PROJECT, ZONE and CLUSTER set.
#
# While down you pay for disks, images, the bucket and the load balancer, not for nodes. GKE's free tier
# covers the management fee of one zonal cluster per billing account.
set -euo pipefail
ACTION=${1:-status}
NS=${NAMESPACE:-hangar}

if [ -z "${PROJECT:-}" ] || [ -z "${ZONE:-}" ] || [ -z "${CLUSTER:-}" ]; then
  cd "$(dirname "$0")/../.."
  creds=$(terraform -chdir=deploy/terraform/gke output -raw get_credentials)
  CLUSTER=$(awk '{print $5}' <<<"$creds")
  ZONE=$(sed -E 's/.*--zone ([^ ]+).*/\1/' <<<"$creds")
  PROJECT=$(sed -E 's/.*--project ([^ ]+).*/\1/' <<<"$creds")
fi
G=(--project "$PROJECT" --zone "$ZONE")
log() { echo "== $*"; }

label_key() { echo "hangar-power-$(tr -c 'a-z0-9_\n-' '-' <<<"$1" | cut -c1-50)"; }
pools() { gcloud container node-pools list --cluster "$CLUSTER" "${G[@]}" --format='value(name)'; }

# kubectl is optional: without it, `down` skips the graceful part (sessions still have their last checkpoint).
have_kubectl() {
  if [ "${HANGAR_POWER_INSTALL:-}" = 1 ] && ! command -v kubectl >/dev/null; then
    # The scheduled job runs in the google-cloud-cli image, which may not include them.
    { apt-get update -qq && apt-get install -y -qq kubectl google-cloud-cli-gke-gcloud-auth-plugin; } \
      >/dev/null 2>&1 || gcloud components install kubectl gke-gcloud-auth-plugin --quiet >/dev/null 2>&1 || true
  fi
  command -v kubectl >/dev/null && command -v gke-gcloud-auth-plugin >/dev/null &&
    gcloud container clusters get-credentials "$CLUSTER" "${G[@]}" >/dev/null 2>&1
}

suspend_sessions() {
  # Inside the hangar pod: suspend every session with a sandbox (each checkpoints first), waiting up to
  # 10 minutes for running turns to finish. Turns still running after that resume from their last checkpoint.
  kubectl -n "$NS" exec -i deploy/hangar -- python3 - <<'PY' || echo "could not reach hangar; continuing"
import json, os, time, urllib.request
token = os.environ.get("HANGAR_ADMIN_TOKEN") or open(
    os.path.join(os.environ.get("HANGAR_DATA_DIR", "/data"), "bootstrap-token")).read().strip()

def call(method, path):
    req = urllib.request.Request("http://127.0.0.1:8787" + path, method=method, data=b"" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

deadline, asked = time.time() + 600, set()
while True:
    live = [s for s in call("GET", "/api/sessions?limit=500") if s.get("sandbox_id")]
    if not live:
        print("no sessions running")
        break
    for s in live:
        if s["state"] == "idle" and not s.get("busy") and s["id"] not in asked:
            try:
                call("POST", f"/api/sessions/{s['id']}/suspend")
                asked.add(s["id"])
                print("suspending", s["name"])
            except Exception as e:  # busy now, or already suspending
                print("not yet:", s["name"], e)
    if time.time() > deadline:
        print("still running, resumes from its last checkpoint:", ", ".join(f"{s['name']} ({s['state']})" for s in live))
        break
    time.sleep(10)
PY
}

case "$ACTION" in
down)
  if have_kubectl && kubectl -n "$NS" get deploy hangar >/dev/null 2>&1; then
    if [ "$(kubectl -n "$NS" get deploy hangar -o jsonpath='{.status.readyReplicas}')" = "1" ]; then
      log "suspending sessions"; suspend_sessions
    fi
    log "stopping hangar"
    kubectl -n "$NS" scale deploy/hangar --replicas=0
    kubectl -n "$NS" wait --for=delete pod -l app=hangar --timeout=180s >/dev/null 2>&1 || true
  else
    log "kubectl unavailable: skipping session suspend (sessions resume from their last checkpoint)"
  fi
  for p in $(pools); do
    read -r on min max < <(gcloud container node-pools describe "$p" --cluster "$CLUSTER" "${G[@]}" \
      --format='value(autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount)')
    if [ "$on" = "True" ]; then
      # Remember the range as a label on the cluster (not the pool: pool labels become node labels), so
      # `up` restores it without terraform.
      gcloud container clusters update "$CLUSTER" "${G[@]}" --quiet \
        --update-labels="$(label_key "$p")=${min:-0}-${max:-1}" >/dev/null
      log "$p: autoscaling off (was ${min:-0}-${max:-1})"
      gcloud container clusters update "$CLUSTER" "${G[@]}" --node-pool "$p" --no-enable-autoscaling --quiet >/dev/null
    fi
    log "$p: 0 nodes"
    gcloud container clusters resize "$CLUSTER" "${G[@]}" --node-pool "$p" --num-nodes 0 --quiet >/dev/null
  done
  log "down. Bring it back with: bash deploy/gke/power.sh up"
  ;;
up)
  saved=$(gcloud container clusters describe "$CLUSTER" "${G[@]}" --format='json(resourceLabels)')
  for p in $(pools); do
    range=$(python3 -c 'import json,sys;print((json.load(sys.stdin).get("resourceLabels") or {}).get(sys.argv[1],""))' \
      "$(label_key "$p")" <<<"$saved")
    min=${range%-*}; max=${range#*-}
    if [ -z "$range" ]; then  # never powered down by this script: system pools keep a node, others start at 0
      case "$p" in system*) min=1; max=2 ;; *) min=0; max=4 ;; esac
    fi
    log "$p: autoscaling ${min}-${max}"
    gcloud container clusters update "$CLUSTER" "${G[@]}" --node-pool "$p" --enable-autoscaling \
      --min-nodes "$min" --max-nodes "$max" --quiet >/dev/null
    if [ "$min" -gt 0 ]; then
      gcloud container clusters resize "$CLUSTER" "${G[@]}" --node-pool "$p" --num-nodes "$min" --quiet >/dev/null
    fi
  done
  if have_kubectl && kubectl -n "$NS" get deploy hangar >/dev/null 2>&1; then
    log "starting hangar"
    kubectl -n "$NS" scale deploy/hangar --replicas=1
    kubectl -n "$NS" rollout status deploy/hangar --timeout=900s
  else
    log "kubectl unavailable: hangar starts when its node is ready (if it was scaled to 0, run: kubectl -n $NS scale deploy/hangar --replicas=1)"
  fi
  log "up"
  ;;
status)
  gcloud container node-pools list --cluster "$CLUSTER" "${G[@]}" \
    --format='table(name,autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount,config.machineType)'
  gcloud compute instances list --project "$PROJECT" --filter="name~^gke-${CLUSTER}-" --format='table(name,status)'
  ;;
*) echo "usage: $0 up|down|status" >&2; exit 2 ;;
esac
