#!/bin/bash
# The cluster's power: the sandbox node pools off at night and on in the morning, holds that pause that schedule, and
# the whole cluster off for a week. Nothing is ever deleted: the cluster, disks (the hangar-data volume with the
# SQLite store among them), images, the bucket, session checkpoints and the IP stay (docs/gke.md, section 6).
#
#   bash deploy/gke/power.sh down     # drain, checkpoint and suspend sessions, tell hangar, sandbox pools to 0 nodes;
#                                     # hangar and the system pool stay up (GitHub, Slack, Jira events are queued)
#   bash deploy/gke/power.sh up       # (from off: the system pool, then hangar, waiting until it's ready) sandbox
#                                     # pools back to their ranges (the gVisor pool at its warm floor), images pulled
#                                     # onto the floor node, tell hangar: queued jobs start. Ends a hold. Then, when
#                                     # main is ahead of what hangar runs, starts deploy.yml (it waits for approval)
#   bash deploy/gke/power.sh off [--days N | --until DATE]
#                                     # everything to zero compute: `down`, then hangar to 0 and the system pool to 0
#                                     # nodes; holds the schedule for N days (default 7) so it stays off
#   bash deploy/gke/power.sh hold --days N | --until DATE
#                                     # the scheduled power-ups and power-downs skip until then; the pools stay as
#                                     # they are. DATE: 2026-10-22 (that day's 00:00) or 2026-10-22T09:00, local time
#   bash deploy/gke/power.sh release  # end a hold: the schedule runs again from its next time
#   bash deploy/gke/power.sh overnight on [--force] | off
#                                     # the overnight lane (#186; terraform enable_overnight_lane): while on, `down`
#                                     # leaves one Spot node of the sandboxes-overnight pool up and tells hangar
#                                     # `night`; `up` scales it back to 0 once the other pools are ready. On asks
#                                     # hangar, which refuses below 60% agent PR yield without --force (audited)
#   bash deploy/gke/power.sh status   # pools, nodes, the hold, and what hangar thinks
#   bash deploy/gke/power.sh state    # one JSON line, read-only (only reads the cluster's labels): deploy.yml's
#                                     # check. {"summary": "up" | "down" | "starting" | "held" | "off", ...}
#
# From a laptop it reads deploy/terraform/gke's outputs. The scheduled Cloud Run jobs (terraform
# enable_power_schedule) run this same script with PROJECT, ZONE and CLUSTER set, POWER_SCHEDULED=1 (they honour a
# hold), and with what hangar tells people about the morning: POWER_UP_CRON, POWER_TIME_ZONE and POWER_WARM_MINUTES
# (sandboxes are ready that many minutes after the power-up job starts). POWER_POOLS is the pools `down` powers (a
# regex, default ^sandboxes-: gVisor, Kata, microVM); `off` powers the rest too. PREPULL_TIMEOUT is how long `up`
# waits for the image prepull (seconds, default 600). POWER_DEPLOY_REPO (OWNER/REPO; by hand, `gh repo view`) is
# where `up` looks for main's head; POWER_CATCH_UP=0 turns that off.
#
# State lives in the cluster's resource labels, which the scheduled jobs read with one gcloud call even while hangar
# is off: hangar-power-<pool> (a pool's range, saved at down), hangar-power-hold (when the hold ends, epoch seconds),
# hangar-power-hold-set (when it last changed), hangar-power-off and hangar-power-starting. `hangar power hold` and
# `release` are recorded in hangar; each scheduled run copies the newer of the two to the other before it decides.
#
# While down you pay for the system node, disks, images, the bucket and the load balancer, not for sandbox nodes.
# While off, not for the system node either (docs/team-scale.md, "Cost"). GKE's management fee is charged with no
# nodes too; its free tier covers one zonal cluster per billing account.
set -euo pipefail
ACTION=${1:-status}
[ $# -gt 0 ] && shift
DAYS="" UNTIL="" FORCE=false LANE_ARG=""
while [ $# -gt 0 ]; do
  case $1 in
    on|off) [ "$ACTION" = overnight ] || { echo "unknown option: $1" >&2; exit 2; }; LANE_ARG=$1 ;;
    --force) FORCE=true ;;
    --days) DAYS=${2:?--days needs a number of days}; shift ;;
    --days=*) DAYS=${1#--days=} ;;
    --until) UNTIL=${2:?--until needs a date}; shift ;;
    --until=*) UNTIL=${1#--until=} ;;
    *) echo "unknown option: $1 (usage: $0 up|down|off|hold|release|overnight|status|state)" >&2; exit 2 ;;
  esac
  shift
done
NS=${NAMESPACE:-hangar}
POOLS_RE=${POWER_POOLS:-^sandboxes-}
SCHEDULED=${POWER_SCHEDULED:-0}
HOLD=hangar-power-hold HOLD_SET=hangar-power-hold-set OFF=hangar-power-off STARTING=hangar-power-starting
LANE_POOL=${POWER_OVERNIGHT_POOL:-sandboxes-overnight} LANE_LABEL=hangar-power-overnight  # --- #186

if [ -z "${PROJECT:-}" ] || [ -z "${ZONE:-}" ] || [ -z "${CLUSTER:-}" ]; then
  cd "$(dirname "$0")/../.."
  creds=$(terraform -chdir=deploy/terraform/gke output -raw get_credentials)
  CLUSTER=$(awk '{print $5}' <<<"$creds")
  ZONE=$(sed -E 's/.*--zone ([^ ]+).*/\1/' <<<"$creds")
  PROJECT=$(sed -E 's/.*--project ([^ ]+).*/\1/' <<<"$creds")
fi
G=(--project "$PROJECT" --zone "$ZONE")
log() { echo "== $*"; }
die() { echo "error: $*" >&2; exit 1; }

# Small helpers over JSON and dates, in python3 (which the scheduled job's image has): py COMMAND ARGS...
PYLIB=$(cat <<'PY'
import json, os, re, sys
from datetime import datetime

SPECIAL = {"hangar-power-hold", "hangar-power-hold-set", "hangar-power-off", "hangar-power-starting",
           "hangar-power-lock", "hangar-power-overnight"}
STARTING_GRACE = 3600  # an `up` that never finished: its starting mark counts for this long
MAX_DAYS = 90


def die(msg):
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(2)


def tz():
    name = os.environ.get("POWER_TIME_ZONE")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def labels(text):
    try:
        return json.loads(text or "{}").get("resourceLabels") or {}
    except (ValueError, AttributeError):
        return {}


def num(v):
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return None


def fmt(t):
    return datetime.fromtimestamp(int(t), tz()).strftime("%a %d %b %H:%M %Z")


def load(text):
    try:
        return json.loads(text or "null")
    except ValueError:
        return None


cmd, a = sys.argv[1], sys.argv[2:]
if cmd == "label":  # LABELS_JSON KEY
    print(labels(a[0]).get(a[1], ""))
elif cmd == "fmt":  # EPOCH
    print(fmt(a[0]))
elif cmd == "until":  # DAYS UNTIL NOW DEFAULT_DAYS: when a hold ends, epoch seconds
    days, until, now, default = a[0], a[1], int(a[2]), a[3]
    if days and until:
        die("--days or --until, not both")
    if until:
        if re.fullmatch(r"\d{9,11}", until):
            t = int(until)
        else:
            try:
                d = datetime.fromisoformat(until)
            except ValueError:
                die(f"--until: '{until}' isn't a date (2026-10-22, or 2026-10-22T09:00)")
            t = int((d if d.tzinfo else d.replace(tzinfo=tz())).timestamp())
    else:
        if not (days or default):
            die("hold needs --days N or --until DATE")
        try:
            n = float(days or default)
        except ValueError:
            die(f"--days: '{days}' isn't a number")
        if not 0 < n <= MAX_DAYS:
            die(f"--days: between 0 and {MAX_DAYS}")
        t = now + int(n * 86400)
    if t <= now:
        die(f"{fmt(t)} has passed")
    if t > now + MAX_DAYS * 86400:
        die(f"at most {MAX_DAYS} days ahead")
    print(t)
elif cmd == "summary":  # LABELS_JSON POOLS_RE NOW: the cluster's power state, from its labels alone
    lb, pools_re, now = labels(a[0]), re.compile(a[1]), int(a[2])
    saved = [k[len("hangar-power-"):] for k in lb if k.startswith("hangar-power-") and k not in SPECIAL]
    # off: marked so, or a pool outside the sandbox pools was saved (an older power.sh took everything down)
    off = "hangar-power-off" in lb or any(not pools_re.search(p) for p in saved)
    started = num(lb.get("hangar-power-starting"))
    starting = started is not None and now - started < STARTING_GRACE
    down = any(pools_re.search(p) for p in saved)
    until = num(lb.get("hangar-power-hold"))
    held = until is not None and until > now
    state = "off" if off else "starting" if starting else "down" if down else "up"
    print(json.dumps({"summary": "off" if off else "held" if held else state, "state": state, "held": held,
                      "hold_until": until if held else None, "hold_set": num(lb.get("hangar-power-hold-set")),
                      "off_since": num(lb.get("hangar-power-off")), "saved_pools": sorted(saved)}))
elif cmd == "is_off":  # LABELS_JSON POOLS_RE: "1" when off (marked so, or a non-sandbox pool was saved), else ""
    lb, pools_re = labels(a[0]), re.compile(a[1])
    saved = [k[len("hangar-power-"):] for k in lb if k.startswith("hangar-power-") and k not in SPECIAL]
    print("1" if "hangar-power-off" in lb or any(not pools_re.search(p) for p in saved) else "")
elif cmd == "describe":  # SUMMARY_JSON: one line for people
    s = load(a[0]) or {}
    line = {"up": "on", "down": "sandbox pools powered down", "starting": "powering on",
            "off": "off (everything at 0 nodes)"}.get(s.get("state"), str(s.get("state")))
    if s.get("held"):
        line += f"; held until {fmt(s['hold_until'])}: the scheduled power-up and power-down skip until then"
    print(line)
elif cmd == "holdplan":  # LABELS_JSON HANGAR_HOLD_JSON NOW: which side of the hold is newer, and what to copy
    lb, rec, now = labels(a[0]), (load(a[1]) or {}), int(a[2])
    rec = rec.get("record") if isinstance(rec, dict) else None
    lset, luntil = num(lb.get("hangar-power-hold-set")) or 0, num(lb.get("hangar-power-hold"))
    hset = num(rec.get("set_at")) if isinstance(rec, dict) else None
    huntil = num(rec.get("until")) if isinstance(rec, dict) else None
    if hset is not None and hset > lset:
        print(f"label-set {huntil} {hset}" if huntil and huntil > now else f"label-clear {hset}")
    elif lset and (hset is None or lset > hset):
        print(f"hangar-set {luntil} {lset}" if luntil and luntil > now else f"hangar-clear {lset}")
    else:
        print("none")
elif cmd == "held":  # HANGAR_HOLD_JSON NOW: when hangar's hold ends, if one is on
    rec = (load(a[0]) or {}).get("record") if isinstance(load(a[0]), dict) else None
    until = num(rec.get("until")) if isinstance(rec, dict) else None
    print(until if until and until > int(a[1]) else "")
elif cmd == "deployed":  # DEPLOYS_JSON RUNNING_IMAGE: the commit hangar runs (its newest deploy record, else the tag)
    recs = load(a[0])
    sha = next((r.get("sha") for r in recs if isinstance(r, dict) and r.get("sha")), None) \
        if isinstance(recs, list) else None
    m = re.search(r":([0-9a-f]{40})(@|$)", a[1] or "")
    sha = sha or (m.group(1) if m else "")
    print(sha if re.fullmatch(r"[0-9a-f]{7,40}", sha or "") else "")
elif cmd == "field":  # JSON KEY
    d = load(a[0])
    v = d.get(a[1]) if isinstance(d, dict) else None
    print(v if isinstance(v, (str, int)) else "")
elif cmd == "compare":  # COMPARE_JSON: "<status> <ahead_by>"
    d = load(a[0]) or {}
    print(f"{d.get('status', 'unknown')} {d.get('ahead_by', 0)}")
elif cmd == "open_runs":  # RUNS_JSON (gh run list --json status): how many haven't finished
    runs = load(a[0])
    print(sum(1 for r in runs if r.get("status") != "completed") if isinstance(runs, list) else 0)
elif cmd == "pending":  # REPO HEAD DEPLOYED AHEAD STARTED: the body hangar shows as the deploy waiting
    print(json.dumps({"repo": a[0], "head": a[1], "deployed": a[2], "ahead": int(a[3] or 0), "started": a[4] == "1"}))
else:
    die(f"py: unknown command {cmd}")
PY
)
py() { python3 -c "$PYLIB" "$@"; }

labels() { gcloud container clusters describe "$CLUSTER" "${G[@]}" --format='json(resourceLabels)'; }
set_labels() { gcloud container clusters update "$CLUSTER" "${G[@]}" --quiet --update-labels="$1" >/dev/null; }
drop_label() {  # KEY: only when it's there
  if [ -n "$(py label "$LABELS" "$1")" ]; then
    gcloud container clusters update "$CLUSTER" "${G[@]}" --quiet --remove-labels="$1" >/dev/null
  fi
}

label_key() { echo "hangar-power-$(tr -c 'a-z0-9_\n-' '-' <<<"$1" | cut -c1-50)"; }
pools() { gcloud container node-pools list --cluster "$CLUSTER" "${G[@]}" --format='value(name)'; }
# the pools `down` powers: POWER_POOLS, except the overnight lane's (#186), which `down` and `up` handle themselves
powered() { [ "$1" != "$LANE_POOL" ] && grep -Eq -- "$POOLS_RE" <<<"$1"; }
pool_size() {  # a pool's node count now: the target size of its instance groups. A failed read stops the run: a
  # fixed-size pool's count is what `up` brings it back to, and none saved would leave it at 0 nodes for good
  local n=0 urls url s
  urls=$(gcloud container node-pools describe "$1" --cluster "$CLUSTER" "${G[@]}" --format='value(instanceGroupUrls)') ||
    die "couldn't read $1's instance groups; nothing was saved or resized for it"
  for url in $(tr ';,' '  ' <<<"$urls"); do
    s=$(gcloud compute instance-groups managed describe "$url" --format='value(targetSize)') ||
      die "couldn't read $1's size ($url); nothing was saved or resized for it"
    [[ "$s" =~ ^[0-9]+$ ]] || die "$1's size reads '$s'; nothing was saved or resized for it"
    n=$((n + s))
  done
  echo "$n"
}

# One power run at a time (the scheduled jobs, `hangar power up`, a person): a label with when the run last moved on
# a step, and its id. One not refreshed for 45 minutes died (no step takes 25; the jobs time out at 40) and is taken
# over. It narrows the window rather than closing it: two runs starting within the same second can both get it (GKE's
# labels have no compare-and-swap through gcloud). A scheduled run that finds it held fails, so the job shows it.
LOCK=hangar-power-lock LOCKED=""
lock() {
  local mine held raw
  raw=$(labels) || die "couldn't read the cluster's labels (the lock and the hold); nothing was changed"
  held=$(py label "$raw" "$LOCK")
  if [ -n "$held" ] && [ $(( $(date +%s) - ${held%%-*} )) -lt 2700 ]; then
    die "another power run (last step $(py fmt "${held%%-*}")) is under way, so this power-$ACTION does nothing." \
      "Wait for it; if it died, remove the cluster label $LOCK and run this again."
  fi
  mine="$(date +%s)-$RANDOM$RANDOM"
  LOCKED=$mine  # before the label: whatever happens next, the trap removes ours (and only ours)
  trap unlock EXIT
  trap 'exit 143' TERM INT  # a stopped job (bash is PID 1 there) still releases it
  set_labels "$LOCK=$mine"
  [ "$(py label "$(labels)" "$LOCK")" = "$mine" ] || die "another power run took the lock at the same moment; run this again"
}
touch_lock() {  # a step done: the lock stays fresh however long the whole run takes
  [ -n "$LOCKED" ] || return 0
  local mine
  mine="$(date +%s)-${LOCKED#*-}"
  set_labels "$LOCK=$mine"
  LOCKED=$mine
}
unlock() {
  if [ -n "$LOCKED" ] && [ "$(py label "$(labels)" "$LOCK")" = "$LOCKED" ]; then
    gcloud container clusters update "$CLUSTER" "${G[@]}" --quiet --remove-labels="$LOCK" >/dev/null 2>&1 || true
  fi
}

# kubectl is optional for down and up: without it, `down` skips the graceful part (sessions still have their last
# checkpoint) and hangar isn't told, so it finds out from the pools themselves (its state ends an hour after the ready
# time). `off`, and `up` from off, need it: they stop and start hangar.
have_kubectl() {
  if [ "${HANGAR_POWER_INSTALL:-}" = 1 ] && ! command -v kubectl >/dev/null; then
    # The scheduled job runs in the google-cloud-cli image, which may not include them.
    { apt-get update -qq && apt-get install -y -qq kubectl google-cloud-cli-gke-gcloud-auth-plugin; } \
      >/dev/null 2>&1 || gcloud components install kubectl gke-gcloud-auth-plugin --quiet >/dev/null 2>&1 || true
  fi
  command -v kubectl >/dev/null && command -v gke-gcloud-auth-plugin >/dev/null &&
    gcloud container clusters get-credentials "$CLUSTER" "${G[@]}" >/dev/null 2>&1
}
KUBE=""
kube() {  # have_kubectl, once
  if [ -z "$KUBE" ]; then if have_kubectl; then KUBE=1; else KUBE=0; fi; fi
  [ "$KUBE" = 1 ]
}
hangar_ready() { [ "$(kubectl -n "$NS" get deploy hangar -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" = "1" ]; }
hangar_replicas() { kubectl -n "$NS" get deploy hangar -o jsonpath='{.spec.replicas}' 2>/dev/null || true; }

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

# ---------------------------------------------------------------- --- #186: the overnight lane
lane_pool() { pools | grep -qxF -- "$LANE_POOL"; }
lane_wanted() {  # the pool exists, and the lane is on: hangar's record when it answers (copied to the label), else the label
  lane_pool || return 1
  local on=""
  LABELS=$(labels)
  if kube && hangar_ready; then
    on=$(hangar_api GET /api/admin/power 2>/dev/null |
      python3 -c 'import json,sys; print("on" if (json.load(sys.stdin)["power"].get("overnight") or {}).get("on") else "")' \
      2>/dev/null) || on=$(py label "$LABELS" "$LANE_LABEL")
    if [ "$on" = on ]; then set_labels "$LANE_LABEL=on"; else drop_label "$LANE_LABEL"; fi
  else
    on=$(py label "$LABELS" "$LANE_LABEL")
  fi
  [ "$on" = on ]
}
lane_nodes() {  # N: the lane's pool to N nodes (a fixed-size pool: the autoscaler never adds a second)
  gcloud container clusters resize "$CLUSTER" "${G[@]}" --node-pool "$LANE_POOL" --num-nodes "$1" --quiet >/dev/null
  log "$LANE_POOL: $1 node(s)"
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

tell_hold() {  # UNTIL|"" SET_AT: hangar's copy of the hold (`hangar power`, `hangar status`, `hangar doctor`)
  local path=/api/admin/power/release body="{\"set_at\": $2}"
  if [ -n "$1" ]; then path=/api/admin/power/hold body="{\"until\": $1, \"set_at\": $2}"; fi
  hangar_api POST "$path" "$body" >/dev/null || echo "   could not tell hangar about the hold (older hangar?)"
}

sync_hold() {
  # `hangar power hold` and `release` are recorded in hangar, which can't write the cluster's labels (it has no
  # container.clusters.update, and gets none). Whichever side changed last wins and is copied to the other.
  local rec plan
  rec=$(hangar_api GET /api/admin/power/hold 2>/dev/null) || return 1
  read -r -a plan <<<"$(py holdplan "$LABELS" "$rec" "$(date +%s)")"
  # Called as a condition, so `set -e` is off in here: every write that fails returns 1 (hangar's hold not applied)
  case ${plan[0]} in
    label-set) set_labels "$HOLD=${plan[1]},$HOLD_SET=${plan[2]}" || return 1
               log "hangar holds the schedule until $(py fmt "${plan[1]}") (hangar power hold)" ;;
    label-clear) set_labels "$HOLD_SET=${plan[1]}" || return 1
                 drop_label "$HOLD" || return 1
                 log "hangar ended the hold (hangar power release)" ;;
    hangar-set) tell_hold "${plan[1]}" "${plan[2]}" ;;
    hangar-clear) tell_hold "" "${plan[1]}" ;;
  esac
  LABELS=$(labels) || return 1
}

schedule_gate() {
  # The scheduled jobs: a hold skips this run. The cluster's labels first (they're there even while hangar is off),
  # then hangar's record of `hangar power hold`/`release` when hangar answers. A hold that has ended is cleared and
  # the run goes ahead: the schedule resumes at its next time, nothing powers up early.
  [ "$SCHEDULED" = 1 ] || return 0
  LABELS=$(labels)
  local until now read=0
  if kube && hangar_ready && sync_hold; then read=1; fi
  until=$(py label "$LABELS" "$HOLD")
  now=$(date +%s)
  if [ -n "$until" ] && [ "$until" -gt "$now" ]; then
    log "held until $(py fmt "$until"): skipping the scheduled power-$ACTION (power.sh up or release, or" \
      "hangar power release, ends the hold)"
    exit 0
  fi
  # Powering down without hangar would skip the drain (running turns are cut off at their last checkpoint) and any
  # `hangar power hold`: fail closed, and leave the pools up, unless the cluster is off and hangar meant to be stopped.
  if [ "$ACTION" = down ] && [ $read = 0 ] && [ -z "$(py is_off "$LABELS" "$POOLS_RE")" ]; then
    echo "error: couldn't reach hangar (kubectl, or its hold record): not powering down, so nothing is cut off" \
      "and a hold set in hangar isn't overridden. The sandbox pools stay up; see this job's log, then run" \
      "bash deploy/gke/power.sh down by hand." >&2
    exit 1
  fi
  if [ -n "$until" ]; then
    drop_label "$HOLD"
    log "the hold ended at $(py fmt "$until"); back on the schedule"
  fi
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

DOWN_STATE=down  # --- #186: `night` when the overnight lane stays up
quiesce() {  # [HOLD_UNTIL SET_AT]: tell hangar the sandboxes are going (and the hold), then drain and suspend
  local told=0
  if kube && hangar_ready; then
    log "telling hangar the sandboxes are powering down ($DOWN_STATE)"
    if hangar_api POST /api/admin/power/state "$(state_body "$DOWN_STATE")" >/dev/null; then told=1
    else echo "could not tell hangar (older hangar?); its drain still holds jobs for an hour"; fi
    if [ -n "${1:-}" ]; then tell_hold "$1" "$2"; fi
    log "suspending sessions"; suspend_sessions "$told"
  else
    log "hangar unreachable: skipping the drain and session suspend (sessions resume from their last checkpoint)"
  fi
}

pools_to_zero() {  # sandbox|control: the sandbox pools (POWER_POOLS), or every other pool (the system pool)
  local p on min max had keep n
  for p in $(pools); do
    if [ "$1" = sandbox ]; then powered "$p" || continue; else ! powered "$p" || continue; fi
    # csv, not value(): GKE omits a minNodeCount of 0, and `read` folds the empty tab-separated field into the next,
    # so a 0-2 pool read as min 2. Commas aren't IFS whitespace, so an empty field stays empty.
    IFS=, read -r on min max < <(gcloud container node-pools describe "$p" --cluster "$CLUSTER" "${G[@]}" \
      --format='csv[no-heading](autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount)')
    had=$(py label "$(labels)" "$(label_key "$p")")  # read now, not before the loop
    # Remember how to bring it back as a label on the cluster (not the pool: pool labels become node labels), so
    # `up` restores it without terraform: an autoscaling range, or a fixed pool's node count (the microVM pool).
    if [ "$on" = "True" ]; then
      keep="${min:-0}-${max:-1}"
    elif [ -n "$had" ]; then
      keep=""  # already down, already saved: a second `down` must not save 0 nodes over it
    else
      n=$(pool_size "$p"); keep=""
      if [ "${n:-0}" -gt 0 ]; then keep="fixed-$n"; fi
    fi
    if [ -n "$keep" ]; then
      set_labels "$(label_key "$p")=$keep"
      log "$p: saved $keep"
    fi
    if [ "$on" = "True" ]; then
      gcloud container clusters update "$CLUSTER" "${G[@]}" --node-pool "$p" --no-enable-autoscaling --quiet >/dev/null
    fi
    log "$p: 0 nodes"
    if ! gcloud container clusters resize "$CLUSTER" "${G[@]}" --node-pool "$p" --num-nodes 0 --quiet >/dev/null; then
      [ "$1" = control ] || die "couldn't resize $p to 0 nodes"
      # GKE Sandbox wants a node in a pool without it; were GKE to enforce that, the system node stays (and costs).
      log "$p: GKE refused 0 nodes; it keeps its node (bring it back to its range with: power.sh up)"
    fi
  done
}

FLOORS=()
restore_pools() {  # sandbox|control: back to the ranges saved at down/off; FLOORS gets each pool's POOL:MIN to wait on
  local saved p range on cur_min cur_max min max
  saved=$(labels)
  FLOORS=()
  for p in $(pools); do
    range=$(py label "$saved" "$(label_key "$p")")
    if [ "$1" = sandbox ]; then powered "$p" || continue
    else ! powered "$p" || continue; [ -n "$range" ] || continue; fi
    IFS=, read -r on cur_min cur_max < <(gcloud container node-pools describe "$p" --cluster "$CLUSTER" "${G[@]}" \
      --format='csv[no-heading](autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount)') || true  # csv: see pools_to_zero
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
    if [ "$min" -gt 0 ]; then
      gcloud container clusters resize "$CLUSTER" "${G[@]}" --node-pool "$p" --num-nodes "$min" --quiet >/dev/null
      # an autoscaled sandbox pool's minimum is its warm floor: the prepull waits for those nodes
      if [ "$1" = control ] || [[ "$range" != fixed-* ]]; then FLOORS+=("$p:$min"); fi
    fi
    # Only once it's back: a resize that failed leaves the saved range for the next `up`
    if [ -n "$range" ]; then
      gcloud container clusters update "$CLUSTER" "${G[@]}" --quiet --remove-labels="$(label_key "$p")" >/dev/null
    fi
  done
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

catch_up() {
  # The deploys deploy.yml skipped while the infra was off, held or down: when main's head is ahead of the commit
  # hangar runs (its newest deploy record, else the running image's tag), start deploy.yml for main. It still waits
  # for the owner's approval. Only where `gh` is logged in (by hand): the scheduled job holds no GitHub credential,
  # so it compares anonymously (public repositories) and tells hangar, which shows the deploy waiting in `hangar
  # power`, `hangar status` and `hangar doctor` with the command to start it. docs/cicd.md, "Infra states".
  [ "${POWER_CATCH_UP:-1}" = 1 ] || return 0
  kube || return 0
  local gh_ok=0 repo deployed head status ahead started=0 runs
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then gh_ok=1; fi
  repo=${POWER_DEPLOY_REPO:-}
  if [ -z "$repo" ] && [ $gh_ok = 1 ]; then
    repo=$(cd "$(dirname "$0")" 2>/dev/null && gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) ||
      repo=""
  fi
  if [[ ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    log "deploy catch-up: skipped (set POWER_DEPLOY_REPO=OWNER/REPO, or run this where gh is logged in)"
    return 0
  fi
  gh_get() {  # PATH: GitHub's REST API, as you with gh, or anonymously
    if [ $gh_ok = 1 ]; then gh api "$1"; else curl -fsS --max-time 20 -H 'Accept: application/vnd.github+json' \
      "https://api.github.com/$1"; fi
  }
  deployed=$(py deployed "$(hangar_api GET /api/admin/deploys 2>/dev/null || echo '[]')" \
    "$(kubectl -n "$NS" get deploy hangar -o jsonpath='{.spec.template.spec.containers[?(@.name=="hangar")].image}' \
      2>/dev/null || true)")
  if [ -z "$deployed" ]; then
    log "deploy catch-up: skipped (no deploy recorded in hangar, and the running image has no commit tag)"
    return 0
  fi
  head=$(py field "$(gh_get "repos/$repo/commits/main" 2>/dev/null || true)" sha)
  if [[ ! "$head" =~ ^[0-9a-f]{40}$ ]]; then
    log "deploy catch-up: skipped (couldn't read main's head on $repo)"
    return 0
  fi
  read -r status ahead <<<"$(py compare "$(gh_get "repos/$repo/compare/$deployed...$head" 2>/dev/null || true)")"
  case $status in
    identical|behind)
      log "nothing to deploy: main's head (${head:0:12}) is the last commit deploy.yml deployed or tried"
      hangar_api POST /api/admin/deploys/pending '{"head": null}' >/dev/null 2>&1 || true
      return 0 ;;
    ahead) ;;
    *) log "deploy catch-up: skipped (hangar runs ${deployed:0:12}, which GitHub compares to main as '$status')"
       return 0 ;;
  esac
  if [ $gh_ok = 1 ]; then
    runs=$(gh run list --repo "$repo" --workflow deploy.yml --branch main --limit 20 --json status 2>/dev/null ||
      echo '[]')
    if [ "$(py open_runs "$runs")" -gt 0 ]; then
      log "main (${head:0:12}) is $ahead commit(s) ahead of hangar (${deployed:0:12}); a deploy.yml run for main is" \
        "already queued or waiting: https://github.com/$repo/actions/workflows/deploy.yml"
      started=1
    elif gh workflow run deploy.yml --repo "$repo" --ref main >/dev/null; then
      log "main (${head:0:12}) is $ahead commit(s) ahead of hangar (${deployed:0:12}): started deploy.yml for main." \
        "Approve it: https://github.com/$repo/actions/workflows/deploy.yml"
      started=1
    else
      log "couldn't start deploy.yml (gh workflow run); start it with: gh workflow run deploy.yml --repo $repo --ref main"
    fi
  else
    log "main (${head:0:12}) is $ahead commit(s) ahead of hangar (${deployed:0:12}). Deploy it:" \
      "gh workflow run deploy.yml --repo $repo --ref main"
  fi
  hangar_api POST /api/admin/deploys/pending "$(py pending "$repo" "$head" "$deployed" "$ahead" "$started")" \
    >/dev/null 2>&1 || true
}

case "$ACTION" in
down)
  lock
  schedule_gate
  LABELS=$(labels)
  if [ -n "$(py is_off "$LABELS" "$POOLS_RE")" ]; then
    log "the cluster is off (power.sh off): nothing to power down"; exit 0
  fi
  if lane_wanted; then DOWN_STATE=night; fi
  quiesce
  touch_lock
  pools_to_zero sandbox
  if [ "$DOWN_STATE" = night ]; then
    lane_nodes 1
    log "night: the overnight lane's node stays up for overnight repos' jobs and evals"
  fi
  log "down; hangar stays up. Bring the sandboxes back with: bash deploy/gke/power.sh up (or hangar power up)"
  ;;
off)
  now=$(date +%s)
  until=$(py until "$DAYS" "$UNTIL" "$now" 7) || exit 2
  # Hangar is stopped with kubectl, and must have stopped (its database closed) before its node goes.
  kube || die "off stops hangar with kubectl (and gke-gcloud-auth-plugin), which aren't available here. Nothing was changed."
  lock
  LABELS=$(labels)
  # The hold first, so a scheduled run that starts meanwhile skips instead of bringing things back.
  set_labels "$HOLD=$until,$HOLD_SET=$now"
  log "held until $(py fmt "$until"): the scheduled power-ups and power-downs skip until then"
  quiesce "$until" "$now"
  touch_lock
  pools_to_zero sandbox
  if lane_pool; then lane_nodes 0; fi  # --- #186: off is off, the lane too
  touch_lock
  if [ "$(hangar_replicas)" != 0 ]; then
    log "stopping hangar (its data stays on the hangar-data volume)"
    kubectl -n "$NS" scale deploy/hangar --replicas=0 >/dev/null
  fi
  kubectl -n "$NS" wait --for=delete pod -l app=hangar --timeout=180s >/dev/null 2>&1 || true
  # Gone means listed as gone: a failed listing counts as still there
  if ! running=$(kubectl -n "$NS" get pods -l app=hangar -o name) || [ -n "$running" ]; then
    die "hangar's pod hasn't stopped after 3 minutes (or couldn't be listed), so the system pool stays up" \
      "(hangar's database is on that node's volume). Run power.sh off again, or power.sh up to bring it all back."
  fi
  touch_lock
  pools_to_zero control
  set_labels "$OFF=$now"  # last: only an off that got here is "off"
  log "off until $(py fmt "$until"). Nothing was deleted. Bring it back with: bash deploy/gke/power.sh up"
  log "while off, webhooks aren't received: GitHub's are redelivered at the next start if at most 3 days old;" \
    "Jira's and Slack's are lost (docs/gke.md, \"Infra states\")"
  ;;
hold)
  now=$(date +%s)
  until=$(py until "$DAYS" "$UNTIL" "$now" "") || exit 2
  set_labels "$HOLD=$until,$HOLD_SET=$now"
  log "held until $(py fmt "$until"): the scheduled power-ups and power-downs skip until then; the pools stay as" \
    "they are. End it early with: bash deploy/gke/power.sh release (or up)"
  if kube && hangar_ready; then tell_hold "$until" "$now"; fi
  ;;
release)
  now=$(date +%s)
  LABELS=$(labels)
  set_labels "$HOLD_SET=$now"
  drop_label "$HOLD"
  log "no hold: the schedule runs from its next time"
  if kube && hangar_ready; then tell_hold "" "$now"; fi
  ;;
up)
  lock
  schedule_gate
  LABELS=$(labels)
  off_since=$(py label "$LABELS" "$OFF")
  if [ -n "$off_since" ] && ! kube; then
    die "the cluster is off, and starting hangar needs kubectl (and gke-gcloud-auth-plugin). Nothing was changed."
  fi
  now=$(date +%s)
  if [ "$SCHEDULED" != 1 ] && [ -n "$(py label "$LABELS" "$HOLD")" ]; then  # by hand: ends the hold
    log "ending the hold (it was until $(py fmt "$(py label "$LABELS" "$HOLD")"))"
    set_labels "$HOLD_SET=$now"
    drop_label "$HOLD"
  fi
  set_labels "$STARTING=$now"
  # 1. The control plane first: the system pool (when off), then hangar, ready before any sandbox node comes.
  restore_pools control
  if kube; then
    for f in ${FLOORS[@]+"${FLOORS[@]}"}; do wait_nodes "${f%%:*}" "${f##*:}" || true; done
    touch_lock
    if [ "$(hangar_replicas)" = "0" ]; then
      log "starting hangar"
      kubectl -n "$NS" scale deploy/hangar --replicas=1
      kubectl -n "$NS" rollout status deploy/hangar --timeout=900s
      touch_lock
    fi
    if hangar_ready; then
      hangar_api POST /api/admin/power/state "$(state_body starting)" >/dev/null || true
      # by hand, a hold hangar has (power.sh off told it, or `hangar power hold`) ends too
      if [ "$SCHEDULED" != 1 ] &&
         [ -n "$(py held "$(hangar_api GET /api/admin/power/hold 2>/dev/null || true)" "$now")" ]; then
        tell_hold "" "$now"
      fi
    fi
  fi
  # 2. The sandbox pools, the warm floor and the prepull.
  restore_pools sandbox
  floors=(${FLOORS[@]+"${FLOORS[@]}"})
  LABELS=$(labels)
  if ! kube; then
    log "kubectl unavailable: no image prepull, and hangar finds out on its own (an hour after the ready time)"
    drop_label "$OFF"; drop_label "$STARTING"
    log "up"; exit 0
  fi
  for f in ${floors[@]+"${floors[@]}"}; do wait_nodes "${f%%:*}" "${f##*:}" || true; done
  touch_lock
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
  drop_label "$OFF"; drop_label "$STARTING"
  # --- #186: the other pools are ready and hangar knows: the overnight node goes (its jobs resume from checkpoints)
  if lane_pool && [ "$(pool_size "$LANE_POOL")" -gt 0 ]; then lane_nodes 0; fi
  log "up"
  catch_up
  ;;
overnight)
  [ -n "$LANE_ARG" ] || die "usage: $0 overnight on [--force] | off"
  lane_pool || die "there is no $LANE_POOL node pool: set enable_overnight_lane = true in deploy/terraform/gke and apply"
  kube && hangar_ready || die "turning the lane on or off goes through hangar (the yield gate), which isn't reachable"
  hangar_api POST /api/admin/power/overnight "{\"on\": $([ "$LANE_ARG" = on ] && echo true || echo false), \"force\": $FORCE}" |
    python3 -c 'import json,sys; r = json.load(sys.stdin); print("   overnight lane", "on" if r["on"] else "off",
    "(forced)" if r.get("forced") else "")' || die "hangar refused (hangar power overnight on says why; below the yield gate it takes --force)"
  if [ "$LANE_ARG" = on ]; then
    set_labels "$LANE_LABEL=on"
    log "the next power-down leaves one $LANE_POOL node up"
  else
    LABELS=$(labels)
    drop_label "$LANE_LABEL"
    if [ "$(pool_size "$LANE_POOL")" -gt 0 ]; then lane_nodes 0; fi
  fi
  ;;
status)
  gcloud container node-pools list --cluster "$CLUSTER" "${G[@]}" \
    --format='table(name,autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount,config.machineType)'
  gcloud compute instances list --project "$PROJECT" --filter="name~^gke-${CLUSTER}-" --format='table(name,status)'
  echo "cluster: $(py describe "$(py summary "$(labels)" "$POOLS_RE" "$(date +%s)")")"
  if kube && hangar_ready; then
    hangar_api GET /api/admin/power | python3 -c 'import json,sys; print("hangar:", json.dumps(json.load(sys.stdin)["power"]))' ||
      true
  fi
  ;;
state)
  py summary "$(labels)" "$POOLS_RE" "$(date +%s)"
  ;;
*) echo "usage: $0 up|down|off|hold|release|overnight|status|state" >&2; exit 2 ;;
esac
