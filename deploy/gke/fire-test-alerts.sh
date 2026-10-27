#!/bin/bash
# Fire each of hangar's SLO alerts once, on purpose, after `terraform apply` with enable_alerting = true
# (deploy/terraform/gke/alerting.tf, docs/alerting.md). KAN-144's "alerts have fired at least once in a test".
#
#   bash deploy/gke/fire-test-alerts.sh            # every log-based alert
#   bash deploy/gke/fire-test-alerts.sh fail-closed job-starts
#   DRY_RUN=1 bash deploy/gke/fire-test-alerts.sh  # print the gcloud commands, change nothing
#
# It writes `hangar.slo:` lines, shaped exactly like hangar's own (src/hangar/slolog.py), to the hangar-slo-test log.
# The log-based metrics count that log as well as hangar's container, so each alert fires within a few minutes and
# its email names its runbook. Nothing in the cluster changes and no job runs. Each alert closes by itself once its
# window passes with no more test lines (an hour or less), or close it in the console.
#
# Test lines are marked reason=test, environment=test and kind=test, so they're easy to tell apart in Logs Explorer.
# The uptime, restart and disk alerts need real failures: docs/alerting.md has a drill for each.
#
# Needs gcloud signed in with roles/logging.logWriter on the project. The project comes from PROJECT, or from hangar's
# terraform outputs (deploy/terraform/gke).
set -euo pipefail
cd "$(dirname "$0")/../.."

DRY=${DRY_RUN:-0}
LOG=hangar-slo-test
ALL="job-starts sandbox-creates queue-slow fail-closed stuck"
WHICH=${*:-$ALL}

if [ -z "${PROJECT:-}" ]; then
  if command -v terraform >/dev/null; then
    PROJECT=$(terraform -chdir=deploy/terraform/gke output -raw registry 2>/dev/null | cut -s -d/ -f2 || true)
  fi
fi
[ -n "${PROJECT:-}" ] || { echo "error: set PROJECT (the GCP project hangar runs in)" >&2; exit 1; }

write() {  # N LINE: the line N times
  local i
  printf '   + gcloud logging write %s %q --project %s   (x%s)\n' "$LOG" "hangar.slo: $2" "$PROJECT" "$1"
  [ "$DRY" = 1 ] && return 0
  for i in $(seq "$1"); do
    gcloud logging write "$LOG" "hangar.slo: $2" --project "$PROJECT" --severity=INFO >/dev/null
  done
}

for a in $WHICH; do
  case $a in
    job-starts)
      echo "== hangar job-starts-failing, job-starts-slow-burn: 8 failed starts in the hour (both burn alerts)"
      write 8 "event=job_start result=error queued_s=1.0 held=no reason=test" ;;
    sandbox-creates)
      echo "== hangar sandbox-creates-failing: 8 failed creates in the hour"
      write 8 "event=sandbox_create result=error provider=test environment=test seconds=1.0 error=Test" ;;
    queue-slow)
      echo "== hangar queue-slow: 5 starts that waited 15 minutes in the queue"
      write 5 "event=job_start result=ok queued_s=900.0 held=no reason=-" ;;
    fail-closed)
      echo "== hangar tower-fail-closed: hangar couldn't reach Tower once"
      write 1 "event=fail_closed kind=test failure=unreachable" ;;
    stuck)
      echo "== hangar jobs-stuck: one stuck-job minute"
      write 1 "event=jobs_stuck n=1 oldest_s=4000 states=queued" ;;
    *) echo "unknown alert: $a (one of: $ALL)" >&2; exit 2 ;;
  esac
done

echo
echo "Written. Each alert should reach the hangar-oncall channel within about 5 minutes:"
echo "   https://console.cloud.google.com/monitoring/alerting?project=$PROJECT"
echo "Run it at a quiet hour (the pools down at night is best): real starts and creates in the same hour dilute"
echo "the test failures below the alerts' ratios. The test lines count in the 28-day SLO tiles too; filter"
echo "logName=\"projects/$PROJECT/logs/$LOG\" in Logs Explorer to see them."
