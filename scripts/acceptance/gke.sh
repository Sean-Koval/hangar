#!/usr/bin/env bash
# Live acceptance checks for a hangar you deployed (docs/acceptance.md is the runbook; docs/upgrading.md the upgrade):
# from your laptop after `bash deploy/gke/up.sh`, logged in with `hangar login`, or from deploy.yml, or anywhere a
# token reaches hangar. The checks use hangar's REST and MCP API; only `k8s` and the exec check's in-cluster numbers
# use kubectl, and --no-kubectl leaves those out.
#
#   bash scripts/acceptance/gke.sh                                   # every check that costs nothing
#   bash scripts/acceptance/gke.sh --gateway-repo you/scratch-repo   # + git gateway push / refused push outside the grant
#   bash scripts/acceptance/gke.sh --with-model --test-repo you/accept-target   # + real agent turns: model, steer,
#                                                                    #   secret scanning, landing, an egress request
#   bash scripts/acceptance/gke.sh --only computer,egress            # just those
#   bash scripts/acceptance/gke.sh --only power,prepull,warmstart --power-cycle   # pools down and up, timed
#
# Each check prints PASS, FAIL or SKIP with its time, and records its numbers in a JSON report (--report). Everything a
# run creates is named accept-<run> (sandboxes, sessions, people, tokens, branches, PRs) and is removed at the end, also
# after a failure or Ctrl-C (--keep leaves it); test environments (accept-*) are kept so their images are built once.
# Exit status: 0 when nothing failed; with --gate core, when no core check failed (what deploy.yml rolls back for).
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HELPER="$ROOT/scripts/acceptance/hangar_accept.py"
CHECKS="doctor k8s sandbox exec stream files mcp isolation session model steer gateway e2b backup metrics computer
egress services envbuild secrets landing intake scale-smoke power prepull warmstart"
# The core checks: the control plane's own paths, quick and with nothing outside hangar to depend on. With --gate core
# (deploy.yml) only these decide the exit status and so a rollback; the others are reported.
CORE="doctor sandbox exec stream files mcp isolation session e2b backup metrics power"
VALUE_OPTS=" --env --session-env --repo --gateway-repo --model --exec-calls --internet-probe --mcp-env --warm-env
 --doctor-baseline --test-env-prefix --egress-allow --egress-deny --egress-direct --dns-probe --service-image
 --service-wait --test-repo --fixture-repo --fixture-push-url --build-timeout --landing-timeout --jira-project
 --scale-sessions --scale-people --scale-env --scale-p95 --job-cap --power-down-cmd --power-up-cmd --power-timeout
 --prepull-envs --boot-timeout --turn-timeout "
FLAG_OPTS=" --with-model --power-cycle --power-cycle-anyway "
VALUE_OPTS=" ${VALUE_OPTS//[[:space:]]/ } "  # one space between names (and around them), so " $1 " matches any
ONLY="" SKIPS="" KEEP=0 EXPECT_ENVD=1 GATE=all REPORT="hangar-accept-report.json" RECORD=""
ARGS=()

usage() {
  cat <<EOF
usage: $0 [options]

  --env NAME            environment for plain sandboxes, the E2B check, and what the test environments extend
                        (default: minimal)
  --session-env NAME    environment for sessions; needs an agent harness, and a model credential for
                        --with-model (default: claude-python)
  --repo URL            repo the sessions check out (default: --gateway-repo's, else
                        https://github.com/octocat/Hello-World.git)
  --gateway-repo O/R    run the git gateway check on github.com/O/R (sessions then use it as their repo): a
                        push to hangar/accept-<run> must go through and a push to accept-<run>-outside (like main,
                        outside the grant) must be refused. It never pushes to main.
                        Both branches are deleted afterwards with the gh CLI if you have it.
  --with-model          also run the checks that need real agent turns (they cost a few cents each): model, steer,
                        secrets, landing, egress's env request, scale-smoke's job queue
  --model NAME          model for those turns (default: the environment's)
  --mcp-env NAME        multi-harness environment the mcp check creates sandboxes from over MCP (default: python)
  --warm-env NAME       environment with a warm pool the warmstart check times a warm and a cold create on
                        (default: claude-python)
  --test-repo O/R       a repo for jobs that push and open PRs (landing, secrets), in HANGAR_GITHUB_REPOS, with the
                        GitHub App installed (default: \$HANGAR_ACCEPT_TEST_REPO)
  --fixture-repo O/R    a repo whose default branch the envbuild check commits its .hangar/environment.yaml to, and
                        puts back afterwards (default: \$HANGAR_ACCEPT_FIXTURE_REPO)
  --jira-project KEY    the HANGAR_JIRA project the intake check's synthetic issue is in (default: the first)
  --scale-sessions N    sessions the scale-smoke check runs at once (default: 20), across --scale-people (3)
  --power-cycle         let the power check power the sandbox pools down and up (it refuses while others work,
                        unless --power-cycle-anyway); --power-down-cmd / --power-up-cmd override how
  --test-env-prefix P   the test environments are P-computer, P-egress, ... (default: accept)
  --record FILE         also append each check's result (and its numbers) to FILE as JSON lines
  --report FILE         the run's JSON report: every check, core or not, its status, time and timings
                        (default: ./hangar-accept-report.json)
  --gate all|core       what decides the exit status: any failed check (default), or only the core ones:
                        ${CORE// /, }
  --doctor-baseline F   JSON list of doctor checks already failing before a deploy: reported, not failed on
  --exec-calls N        exec round trips to time (default: 20)
  --internet-probe H:P  an address sandboxes must reach, so closed ports in the isolation check mean a policy
                        and not a sandbox with no network (default: github.com:443)
  --no-expect-envd      don't fail the exec and stream checks when calls don't go through hangar-envd
  --no-kubectl          skip what needs kubectl (manifest check, in-cluster exec baseline, control-plane probes)
  --only a,b            run only these checks; --skip a,b skips these. Checks: ${CHECKS//[[:space:]]/, }
  --keep                leave what the run created (for debugging); clean up later with
                        python3 $HELPER cleanup --state <the state file it prints>
  More tuning (timeouts, the egress hosts, the services image, ...): python3 $HELPER --help

Environment: HANGAR_URL and HANGAR_TOKEN override \`hangar login\`. HANGAR_ACCEPT_PYTHON is the Python that runs
the checks (default: uv run python, from the repo, so httpx, mcp and, if installed, e2b are there). Secrets for the
checks come from the environment only: HANGAR_ACCEPT_GITHUB_TOKEN (pushes to the test and fixture repos; else gh's
login), HANGAR_ACCEPT_APPROVER_TOKEN (a second GitHub account that approves the landing check's PR),
HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET (signs the intake check's delivery).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --record|--report|--gate)
      [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
      case "$1" in --record) RECORD=$2 ;; --report) REPORT=$2 ;; --gate) GATE=$2 ;; esac
      shift 2 ;;
    --no-expect-envd) EXPECT_ENVD=0; shift ;;
    --no-kubectl) ARGS+=(--no-kubectl); shift ;;
    --only|--skip)
      [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
      if [ "$1" = --only ]; then ONLY=",$2,"; else SKIPS=",$2,"; fi
      shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ "$VALUE_OPTS" == *" $1 "* ]]; then
        [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
        ARGS+=("$1" "$2"); shift 2
      elif [[ "$FLAG_OPTS" == *" $1 "* ]]; then
        ARGS+=("$1"); shift
      else
        echo "unknown option $1 (see --help)" >&2; exit 2
      fi ;;
  esac
done
case "$GATE" in all|core) ;; *) echo "--gate is all or core" >&2; exit 2 ;; esac
[ "$EXPECT_ENVD" = 1 ] && ARGS+=(--expect-envd)

if [ -n "${HANGAR_ACCEPT_PYTHON:-}" ]; then
  read -r -a PY <<<"$HANGAR_ACCEPT_PYTHON"
elif command -v uv >/dev/null; then
  PY=(uv run --project "$ROOT" --quiet python)
else
  PY=(python3)
fi

WHO=$("${PY[@]}" "$HELPER" whoami) || exit 2
WORK=$(mktemp -d "${TMPDIR:-/tmp}/hangar-accept.XXXXXX")
STATE="$WORK/state.json"
RUN_RECORD="$WORK/results.jsonl"  # this run's results, for the report; --record gets a copy
CLEANUP_RESULT="" DONE=0

run_helper() {  # check name, then extra args
  "${PY[@]}" "$HELPER" "$1" --state "$STATE" --summary "$WORK/summary" --record "$RUN_RECORD" \
    ${ARGS[@]+"${ARGS[@]}"} "${@:2}"
}

record() {  # status check seconds summary
  printf '%-4s  %-11s %7ss  %s\n' "$1" "$2" "$3" "$4"
  return 0
}

wanted() {
  if [ -n "$ONLY" ]; then case "$ONLY" in *",$1,"*) ;; *) return 1 ;; esac; fi
  case "$SKIPS" in *",$1,"*) return 1 ;; esac
  return 0
}

check() {  # name, description
  local name=$1 rc secs summary
  if ! wanted "$name"; then return 0; fi
  echo "--    $name: $2"
  rm -f "$WORK/summary"
  run_helper "$name"
  rc=$?
  if ! { read -r secs; read -r summary; } <"$WORK/summary" 2>/dev/null; then
    secs=0.0; summary="the check crashed (exit $rc)"
    printf '{"check": "%s", "status": "fail", "seconds": 0, "summary": "%s", "data": null}\n' "$name" "$summary" \
      >>"$RUN_RECORD"
  fi
  case $rc in
    0) record PASS "$name" "$secs" "$summary" ;;
    3) record SKIP "$name" "$secs" "$summary" ;;
    *) record FAIL "$name" "$secs" "$summary" ;;
  esac
}

CLEANED=0
cleanup() {
  [ "$CLEANED" = 1 ] && return
  CLEANED=1
  trap - INT TERM
  if [ "$KEEP" = 1 ]; then
    echo; echo "--keep: left everything in place. Clean up with:"
    echo "  ${PY[*]} $HELPER cleanup --state $STATE"
    CLEANUP_RESULT=kept
    return
  fi
  if [ -s "$STATE" ]; then
    echo; echo "--    cleanup"
    rm -f "$WORK/summary"
    if run_helper cleanup; then CLEANUP_RESULT=ok; else
      CLEANUP_RESULT=incomplete
      echo "      $(tail -1 "$WORK/summary" 2>/dev/null)" >&2
      echo "      cleanup was incomplete; retry with: ${PY[*]} $HELPER cleanup --state $STATE" >&2
    fi
  else
    CLEANUP_RESULT=ok
  fi
}
finish() {  # also after a failure or Ctrl-C: clean up, then the report, so it says how far the run got
  local rc=$? rep
  cleanup
  echo
  "${PY[@]}" "$HELPER" report --results "$RUN_RECORD" --out "$REPORT" --gate "$GATE" --core "$CORE" \
    --target "$WHO" --cleanup "$CLEANUP_RESULT"
  rep=$?
  [ -z "$RECORD" ] || cat "$RUN_RECORD" >>"$RECORD" 2>/dev/null
  if [ "$KEEP" != 1 ] && [ "$CLEANUP_RESULT" != incomplete ]; then rm -rf "$WORK"; fi
  if [ "$DONE" = 1 ]; then exit "$rep"; fi
  exit "$rc"
}
trap finish EXIT
trap 'echo; echo "interrupted"; exit 130' INT TERM

echo "hangar acceptance against $WHO (state: $STATE)"
echo


check doctor      "hangar doctor reports no failures"
check k8s         "the upgrade's manifests are applied (NetworkPolicies, grace period, pod ready)"
check sandbox     "create a sandbox"
check exec        "exec round trips, the transport they use (hangar-envd or pods/exec), in-cluster baseline"
check stream      "a streamed exec over SSE: the first line arrives while the command runs"
check files       "write and read a file through MCP"
check mcp         "create_sandbox over MCP within claude.ai's 230 s, a dropped client's create still comes up, exec"
check isolation   "a sandbox can't reach another sandbox's envd, the metadata server or hangar's API"
check session     "session: checkpoint, suspend, resume in a new sandbox with the work restored (no model)"
check model       "a real short agent turn"
check steer       "a live turn: an approval held until allowed, a steer message delivered"
check gateway     "git gateway: push to hangar/accept-<run> works, a push outside the grant is refused"
check e2b         "the e2b SDK with a sandboxes token: create, run, files, list, kill"
check backup      "back up the database now and list the backups"
check metrics     "/metrics, and a metrics-scoped token that reaches nothing else"
check computer    "computer: Chromium screenshot, display, 3 s of video, a screen ticket; view-only drops input"
check egress      "allowlist egress: the allowed host through the proxy, 403 otherwise, no direct route, no DNS"
check services    "a Postgres service beside the sandbox, reachable by name"
check envbuild    "a repo's environment builds; a broken install doesn't replace it; svc dev server; preview once"
check secrets     "a job whose change holds a token isn't pushed, and its events name the file and line"
check landing     "a job's PR comes from the GitHub App; an approval arms auto-merge, a push disarms it"
check intake      "a signed Jira webhook makes a job for the linked person; bad signature and replay refused"
check scale-smoke "sessions at once across test people: caps, isolation, p95 create, nothing left behind"
check power       "the power state; with --power-cycle, the pools down (creates refused) and up, timed"
check prepull     "after power-up, the first create of each prepulled environment pulls no image"
check warmstart   "create latency: a warm pool claim against a cold start, back to back"

DONE=1
exit 0  # finish (the EXIT trap) cleans up, writes the report and exits with its status
