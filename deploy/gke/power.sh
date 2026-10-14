#!/bin/bash
# Turn the sandbox node pools off at night and on in the morning. Hangar and the system pool stay up, so GitHub, Slack
# and Jira events are still taken and queued overnight, and creates answer with when sandboxes are back (docs/gke.md,
# section 6). Nothing is deleted: disks, images, session checkpoints and the IP stay.
#
#   bash deploy/gke/power.sh down     # drain, checkpoint and suspend sessions, tell hangar, sandbox pools to 0 nodes
#   bash deploy/gke/power.sh up       # sandbox pools back to their autoscaling ranges (the gVisor pool at its warm
#                                     # floor), images pulled onto the floor node, tell hangar: queued jobs start
#   bash deploy/gke/power.sh status
#
# From a laptop it reads deploy/terraform/gke's outputs. The scheduled Cloud Run jobs (terraform
# enable_power_schedule) run this same script with PROJECT, ZONE and CLUSTER set, and with what hangar tells people
# about the morning: POWER_UP_CRON, POWER_TIME_ZONE and POWER_WARM_MINUTES (sandboxes are ready that many minutes
# after the power-up job starts). POWER_POOLS is the pools it powers (a regex, default ^sandboxes-: gVisor, Kata,
# microVM). PREPULL_TIMEOUT is how long `up` waits for the image prepull (seconds, default 600).
#
# While down you pay for the system node, disks, images, the bucket and the load balancer, not for sandbox nodes
# (docs/team-scale.md, "Cost"). GKE's free tier covers the management fee of one zonal cluster per billing account.
set -euo pipefail
ACTION=${1:-status}
NS=${NAMESPACE:-hangar}
POOLS_RE=${POWER_POOLS:-^sandboxes-}

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
powered() { grep -Eq -- "$POOLS_RE" <<<"$1"; }
pool_size() {  # a pool's node count now: the target size of its instance groups
  local n=0 url s
  for url in $(gcloud container node-pools describe "$1" --cluster "$CLUSTER" "${G[@]}" \
      --format='value(instanceGroupUrls)' | tr ';,' '  '); do
    s=$(gcloud compute instance-groups managed describe "$url" --format='value(targetSize)' 2>/dev/null || echo 0)
    n=$((n + ${s:-0}))
  done
  echo "$n"
}

# kubectl is optional: without it, `down` skips the graceful part (sessions still have their last checkpoint) and
# hangar isn't told, so it finds out from the pools themselves (its state ends an hour after the ready time).
have_kubectl() {
  if [ "${HANGAR_POWER_INSTALL:-}" = 1 ] && ! command -v kubectl >/dev/null; then
    # The scheduled job runs in the google-cloud-cli image, which may not include them.
    { apt-get update -qq && apt-get install -y -qq kubectl google-cloud-cli-gke-gcloud-auth-plugin; } \
      >/dev/null 2>&1 || gcloud components install kubectl gke-gcloud-auth-plugin --quiet >/dev/null 2>&1 || true
  fi
  command -v kubectl >/dev/null && command -v gke-gcloud-auth-plugin >/dev/null &&
    gcloud container clusters get-credentials "$CLUSTER" "${G[@]}" >/dev/null 2>&1
}
hangar_ready() { [ "$(kubectl -n "$NS" get deploy hangar -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" = "1" ]; }

hangar_api() {  # METHOD PATH [JSON]: hangar's REST API from inside its pod, with its own admin token; prints the reply
  kubectl -n "$NS" exec -i deploy/hangar -- python3 - "$@" <<'PY'
import os, sys, urllib.request
method, path = sys.argv[1], sys.argv[2]
body = sys.argv[3].encode() if len(sys.argv) > 3 else (b"" if method == "POST" else None)
token = os.environ.get("HANGAR_ADMIN_TOKEN") or open(
    os.path.join(os.environ.get("HANGAR_DATA_DIR", "/data"), "bootstrap-token")).read().strip()
req = urllib.request.Request("http://127.0.0.1:8787" + path, method=method, data=body,
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=300) as r:
    sys.stdout.write(r.read().decode())
PY
}

state_body() {  # STATE [PREPULL_JSON]: what hangar is told; POWER_* come from the scheduled job's environment
  python3 - "$@" <<'PY'
import json, os, sys
b = {"state": sys.argv[1], "up_cron": os.environ.get("POWER_UP_CRON") or None,
     "time_zone": os.environ.get("POWER_TIME_ZONE") or None, "warm_minutes": os.environ.get("POWER_WARM_MINUTES") or None}
if len(sys.argv) > 2 and sys.argv[2]:
    b["prepull"] = json.loads(sys.argv[2])
print(json.dumps({k: v for k, v in b.items() if v is not None}))
PY
}

suspend_sessions() {
  # Inside the hangar pod: drain (queued jobs wait instead of starting), suspend every session with a sandbox (each
  # checkpoints first), and wait up to 10 minutes for running turns to finish. Turns still running then get a fresh
  # checkpoint, and their jobs resume from it after `up`. Last, wait (up to 3 more minutes) for sandbox creates,
  # destroys and suspends already under way to finish, so the pools going away cuts nothing off half way. With $1 = 1
  # (hangar knows the pools are going down, so it holds jobs itself), the drain ends here.
  kubectl -n "$NS" exec -i deploy/hangar -- python3 - "${1:-0}" <<'PY' || echo "could not reach hangar; continuing"
import json, os, sys, time, urllib.error, urllib.request
token = os.environ.get("HANGAR_ADMIN_TOKEN") or open(
    os.path.join(os.environ.get("HANGAR_DATA_DIR", "/data"), "bootstrap-token")).read().strip()

def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request("http://127.0.0.1:8787" + path, method=method, data=data,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def drain_status():
    try:
        return call("GET", "/api/admin/drain")
    except urllib.error.HTTPError as e:  # a hangar from before the drain: nothing to wait for
        return {"settled": True} if e.code == 404 else None
    except Exception:
        return None

try:
    call("POST", "/api/admin/drain", {"minutes": 60})
    print("draining: queued jobs wait until the sandboxes are back")
except Exception as e:
    print("no drain (older hangar?):", e)

deadline, asked = time.time() + 600, set()
while True:
    live = [s for s in call("GET", "/api/sessions?limit=500") if s.get("sandbox_id")]
    st = drain_status() or {}
    if not live and st.get("settled", True):
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
        stop = time.time() + 480  # the scheduled power job has 40 minutes in all
        for s in live:
            if s["state"] == "running" and time.time() < stop:
                try:
                    call("POST", f"/api/sessions/{s['id']}/checkpoint", timeout=max(10, stop - time.time()))
                    print("checkpointed", s["name"], "(still running; resumes from here)")
                except Exception as e:
                    print("could not checkpoint", s["name"], e)
        print("still running, resumes from its last checkpoint:", ", ".join(f"{s['name']} ({s['state']})" for s in live))
        break
    time.sleep(10)

settle = time.time() + 180
while (st := drain_status()) is not None and not st.get("settled", True) and time.time() < settle:
    print("waiting for:", json.dumps({k: st.get(k) for k in ("creating", "destroying")} | {
        k: (st.get("sessions") or {}).get(k) for k in ("starting", "suspending")}))
    time.sleep(5)
if st is not None and not st.get("settled", True):
    print("not settled; the reconciler finishes it (sandboxes are reconciled, jobs resume)")
if sys.argv[1] == "1":
    try:
        call("DELETE", "/api/admin/drain")  # hangar holds queued jobs itself until the pools are back
    except Exception as e:
        print("could not end the drain (it ends by itself):", e)
PY
}

wait_nodes() {  # POOL COUNT: wait (up to 10 min) until COUNT of the pool's nodes are Ready
  local deadline=$((SECONDS + 600)) ready
  while :; do
    ready=$(kubectl get nodes -l "cloud.google.com/gke-nodepool=$1" --no-headers 2>/dev/null | awk '$2 == "Ready"' |
      wc -l)
    [ "$ready" -ge "$2" ] && { log "$1: $ready node(s) ready"; return 0; }
    [ $SECONDS -ge $deadline ] && { log "$1: $ready of $2 node(s) ready after 10 min; carrying on"; return 1; }
    sleep 10
  done
}

prepull() {  # the images hangar's plan lists, pulled onto the sandbox nodes; prints the summary JSON for hangar
  local reply manifest start n nodes ready=true
  reply=$(hangar_api GET "/api/admin/power/prepull?namespace=$NS") || { echo '{}'; return 0; }
  manifest=$(python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["manifest"]))' <<<"$reply")
  n=$(python3 -c 'import json,sys; r = json.load(sys.stdin); print(len(r["images"]))' <<<"$reply")
  python3 -c 'import json,sys; m = json.load(sys.stdin)["missing"]; m and print("   not in the registry yet:", *m)' \
    <<<"$reply" >&2
  start=$SECONDS
  kubectl apply -f - <<<"$manifest" >&2
  kubectl -n "$NS" rollout status daemonset/hangar-prepull --timeout="${PREPULL_TIMEOUT:-600}s" >&2 || ready=false
  nodes=$(kubectl -n "$NS" get daemonset hangar-prepull -o jsonpath='{.status.numberReady}' 2>/dev/null || echo 0)
  echo "{\"images\": $n, \"seconds\": $((SECONDS - start)), \"nodes\": ${nodes:-0}, \"ready\": $ready}"
}

case "$ACTION" in
down)
  told=0
  if have_kubectl && hangar_ready; then
    log "telling hangar the sandboxes are powering down"
    if hangar_api POST /api/admin/power/state "$(state_body down)" >/dev/null; then told=1
    else echo "could not tell hangar (older hangar?); its drain still holds jobs for an hour"; fi
    log "suspending sessions"; suspend_sessions "$told"
  else
    log "hangar unreachable: skipping the drain and session suspend (sessions resume from their last checkpoint)"
  fi
  saved=$(gcloud container clusters describe "$CLUSTER" "${G[@]}" --format='json(resourceLabels)')
  for p in $(pools); do
    powered "$p" || continue
    read -r on min max < <(gcloud container node-pools describe "$p" --cluster "$CLUSTER" "${G[@]}" \
      --format='value(autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount)')
    had=$(python3 -c 'import json,sys;print((json.load(sys.stdin).get("resourceLabels") or {}).get(sys.argv[1],""))' \
      "$(label_key "$p")" <<<"$saved")
    # Remember how to bring it back as a label on the cluster (not the pool: pool labels become node labels), so
    # `up` restores it without terraform: an autoscaling range, or a fixed pool's node count (the microVM pool).
    if [ "$on" = "True" ]; then
      keep="${min:-0}-${max:-1}"
    elif [ -n "$had" ]; then
      keep=""  # already down, already saved: a second `down` must not save 0 nodes over it
    else
      n=$(pool_size "$p"); keep=$([ "${n:-0}" -gt 0 ] && echo "fixed-$n" || true)
    fi
    if [ -n "$keep" ]; then
      gcloud container clusters update "$CLUSTER" "${G[@]}" --quiet --update-labels="$(label_key "$p")=$keep" >/dev/null
      log "$p: saved $keep"
    fi
    if [ "$on" = "True" ]; then
      gcloud container clusters update "$CLUSTER" "${G[@]}" --node-pool "$p" --no-enable-autoscaling --quiet >/dev/null
    fi
    log "$p: 0 nodes"
    gcloud container clusters resize "$CLUSTER" "${G[@]}" --node-pool "$p" --num-nodes 0 --quiet >/dev/null
  done
  log "down; hangar stays up. Bring the sandboxes back with: bash deploy/gke/power.sh up (or hangar power up)"
  ;;
up)
  kube=0
  if have_kubectl; then kube=1; fi
  if [ $kube = 1 ] && hangar_ready; then
    hangar_api POST /api/admin/power/state "$(state_body starting)" >/dev/null || true
  fi
  saved=$(gcloud container clusters describe "$CLUSTER" "${G[@]}" --format='json(resourceLabels)')
  floors=()
  for p in $(pools); do
    range=$(python3 -c 'import json,sys;print((json.load(sys.stdin).get("resourceLabels") or {}).get(sys.argv[1],""))' \
      "$(label_key "$p")" <<<"$saved")
    # The powered pools, and any pool an older power.sh saved (it powered the system pool down too).
    if ! powered "$p" && [ -z "$range" ]; then continue; fi
    read -r on cur_min cur_max < <(gcloud container node-pools describe "$p" --cluster "$CLUSTER" "${G[@]}" \
      --format='value(autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount)') || true
    if [ "$on" = "True" ]; then
      # `down` turned autoscaling off, so it is on again only if its range was set while the pools were down (a
      # terraform apply in the power-down window, docs/team-scale.md): that range wins over the one saved at `down`.
      min=${cur_min:-0}
      log "$p: autoscaling ${min}-${cur_max:-?} (set while down; kept)"
    elif [[ "$range" == fixed-* ]]; then
      min=${range#fixed-}
      log "$p: ${min} nodes (a fixed-size pool)"
    elif [ -n "$range" ]; then
      min=${range%-*}; max=${range#*-}
      log "$p: autoscaling ${min}-${max}"
      gcloud container clusters update "$CLUSTER" "${G[@]}" --node-pool "$p" --enable-autoscaling \
        --min-nodes "$min" --max-nodes "$max" --quiet >/dev/null
    else
      log "$p: nothing saved at down; left as it is"
      continue
    fi
    if [ -n "$range" ]; then
      gcloud container clusters update "$CLUSTER" "${G[@]}" --quiet --remove-labels="$(label_key "$p")" >/dev/null
    fi
    if [ "$min" -gt 0 ]; then
      gcloud container clusters resize "$CLUSTER" "${G[@]}" --node-pool "$p" --num-nodes "$min" --quiet >/dev/null
      # an autoscaled sandbox pool's minimum is its warm floor: the prepull waits for those nodes
      if powered "$p" && [[ "$range" != fixed-* ]]; then floors+=("$p:$min"); fi
    fi
  done
  if [ $kube = 0 ]; then
    log "kubectl unavailable: no image prepull, and hangar finds out on its own (an hour after the ready time)"
    log "up"; exit 0
  fi
  if [ "$(kubectl -n "$NS" get deploy hangar -o jsonpath='{.spec.replicas}' 2>/dev/null)" = "0" ]; then
    log "starting hangar (an older power.sh stopped it)"
    kubectl -n "$NS" scale deploy/hangar --replicas=1
    kubectl -n "$NS" rollout status deploy/hangar --timeout=900s
  fi
  for f in ${floors[@]+"${floors[@]}"}; do wait_nodes "${f%%:*}" "${f##*:}" || true; done
  pre='{}'
  if [ ${#floors[@]} -gt 0 ]; then
    log "pulling environment images and harness packs onto the sandbox nodes"
    pre=$(prepull)
    log "prepull: $pre"
  fi
  log "telling hangar the sandboxes are on"
  hangar_api POST /api/admin/power/state "$(state_body up "$pre")" | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("   queued jobs starting:", len(r.get("released_jobs") or []))
print("   warm pools for the day:", ", ".join(f"{k}={v}" for k, v in sorted((r.get("warm_pools") or {}).items())) or "none")
' || echo "could not tell hangar; it finds out on its own (an hour after the ready time), or run: hangar power up"
  log "up"
  ;;
status)
  gcloud container node-pools list --cluster "$CLUSTER" "${G[@]}" \
    --format='table(name,autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount,config.machineType)'
  gcloud compute instances list --project "$PROJECT" --filter="name~^gke-${CLUSTER}-" --format='table(name,status)'
  if have_kubectl && hangar_ready; then
    hangar_api GET /api/admin/power | python3 -c 'import json,sys; print("hangar:", json.dumps(json.load(sys.stdin)["power"]))' ||
      true
  fi
  ;;
*) echo "usage: $0 up|down|status" >&2; exit 2 ;;
esac
