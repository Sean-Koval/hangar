#!/usr/bin/env bash
# Live acceptance checks for a hangar you deployed (docs/upgrading.md): run from your laptop after
# `bash deploy/gke/up.sh`, logged in with `hangar login`, with kubectl pointed at the cluster.
#
#   bash scripts/acceptance/gke.sh                                   # the free checks
#   bash scripts/acceptance/gke.sh --gateway-repo you/scratch-repo   # + git gateway push / refused push outside the grant
#   bash scripts/acceptance/gke.sh --with-model --session-env my-env # + a real Claude turn, and steering/approvals
#   bash scripts/acceptance/gke.sh --only exec,isolation             # just those
#
# Each check prints PASS, FAIL or SKIP with its time. Everything the run creates is named accept-<run> (sandboxes,
# sessions, tokens, a branch) and is removed at the end, also after a failure or Ctrl-C (--keep leaves it).
# Exit status: 0 when nothing failed.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HELPER="$ROOT/scripts/acceptance/hangar_accept.py"
CHECKS="doctor k8s sandbox exec stream files mcp isolation session model steer gateway e2b backup metrics power"
ONLY="" SKIPS="" KEEP=0 WITH_MODEL=0 NO_KUBECTL=0 EXPECT_ENVD=1
ARGS=()

usage() {
  cat <<EOF
usage: $0 [options]

  --env NAME            environment for plain sandboxes and the E2B check (default: minimal)
  --session-env NAME    environment for sessions; needs an agent harness, and a model credential for
                        --with-model (default: claude-python)
  --repo URL            repo the sessions check out (default: --gateway-repo's, else
                        https://github.com/octocat/Hello-World.git)
  --gateway-repo O/R    run the git gateway check on github.com/O/R (sessions then use it as their repo): a
                        push to hangar/accept-<run> must go through and a push to accept-<run>-outside (like main,
                        outside the grant) must be refused. It never pushes to main.
                        Both branches are deleted afterwards with the gh CLI if you have it.
  --with-model          also run a real short agent turn and a steered turn with an approval (costs money)
  --model NAME          model for those turns (default: the environment's)
  --mcp-env NAME        multi-harness environment the mcp check creates sandboxes from over MCP (default: python)
  --exec-calls N        exec round trips to time (default: 20)
  --internet-probe H:P  an address sandboxes must reach, so closed ports in the isolation check mean a policy
                        and not a sandbox with no network (default: github.com:443)
  --no-expect-envd      don't fail the exec and stream checks when calls don't go through hangar-envd
  --no-kubectl          skip what needs kubectl (manifest check, in-cluster exec baseline, control-plane probes)
  --only a,b            run only these checks; --skip a,b skips these. Checks: ${CHECKS// /, }
  --keep                leave what the run created (for debugging); clean up later with
                        python3 $HELPER cleanup --state <the state file it prints>

Environment: HANGAR_URL and HANGAR_TOKEN override \`hangar login\`. HANGAR_ACCEPT_PYTHON is the Python that runs
the checks (default: uv run python, from the repo, so httpx, mcp and, if installed, e2b are there).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --env|--session-env|--repo|--gateway-repo|--model|--exec-calls|--internet-probe|--mcp-env)
      [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
      ARGS+=("$1" "$2"); shift 2 ;;
    --with-model) WITH_MODEL=1; shift ;;
    --no-expect-envd) EXPECT_ENVD=0; shift ;;
    --no-kubectl) NO_KUBECTL=1; ARGS+=(--no-kubectl); shift ;;
    --only|--skip)
      [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
      if [ "$1" = --only ]; then ONLY=",$2,"; else SKIPS=",$2,"; fi
      shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done
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
RESULTS=()
FAILED=0

run_helper() {  # check name, then extra args
  "${PY[@]}" "$HELPER" "$1" --state "$STATE" --summary "$WORK/summary" ${ARGS[@]+"${ARGS[@]}"} "${@:2}"
}

record() {  # status check seconds summary
  printf '%-4s  %-9s %7ss  %s\n' "$1" "$2" "$3" "$4"
  RESULTS+=("$(printf '%-4s  %-9s %7ss  %s' "$1" "$2" "$3" "$4")")
  [ "$1" = FAIL ] && FAILED=$((FAILED + 1))
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
  if [ "$name" = k8s ] && [ "$NO_KUBECTL" = 1 ]; then record SKIP "$name" 0.0 "--no-kubectl"; return 0; fi
  if { [ "$name" = model ] || [ "$name" = steer ]; } && [ "$WITH_MODEL" = 0 ]; then
    record SKIP "$name" 0.0 "costs money: run with --with-model"; return 0
  fi
  echo "--    $name: $2"
  rm -f "$WORK/summary"
  run_helper "$name"
  rc=$?
  { read -r secs; read -r summary; } <"$WORK/summary" 2>/dev/null || { secs=0.0; summary="the check crashed (exit $rc)"; }
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
    return
  fi
  if [ -s "$STATE" ]; then
    echo; echo "--    cleanup"
    rm -f "$WORK/summary"
    if run_helper cleanup; then rm -rf "$WORK"; else
      echo "      $(tail -1 "$WORK/summary" 2>/dev/null)" >&2
      echo "      cleanup was incomplete; retry with: ${PY[*]} $HELPER cleanup --state $STATE" >&2
    fi
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT
trap 'echo; echo "interrupted"; exit 130' INT TERM

echo "hangar acceptance against $WHO (state: $STATE)"
echo


check doctor    "hangar doctor reports no failures"
check k8s       "the upgrade's manifests are applied (NetworkPolicies, grace period, pod ready)"
check sandbox   "create a sandbox"
check exec      "exec round trips, the transport they use (hangar-envd or pods/exec), in-cluster baseline"
check stream    "a streamed exec over SSE: the first line arrives while the command runs"
check files     "write and read a file through MCP"
check mcp       "create_sandbox over MCP within claude.ai's 230 s, a dropped client's create still comes up, exec"
check isolation "a sandbox can't reach another sandbox's envd, the metadata server or hangar's API"
check session   "session: checkpoint, suspend, resume in a new sandbox with the work restored (no model)"
check model     "a real short agent turn"
check steer     "a live turn: an approval held until allowed, a steer message delivered"
check gateway   "git gateway: push to hangar/accept-<run> works, a push outside the grant is refused"
check e2b       "the e2b SDK with a sandboxes token: create, run, files, list, kill"
check backup    "back up the database now and list the backups"
check metrics   "/metrics, and a metrics-scoped token that reaches nothing else"
if wanted power; then
  record INFO power 0.0 "not run; to check it yourself: bash deploy/gke/power.sh down, then power.sh up, then rerun this script"
fi

cleanup
echo
echo "== summary"
for r in ${RESULTS[@]+"${RESULTS[@]}"}; do echo "$r"; done
echo
if [ "$FAILED" -gt 0 ]; then echo "$FAILED check(s) failed"; exit 1; fi
echo "no failures"
