#!/usr/bin/env bash
# The check hangar jobs on this repo run (`verify:` in .hangar/environment.yaml): ruff on the Python files the branch
# changed, then the tests those changes can break (select_tests.py), capped to fit a job (docs/ci.md#verify-in-a-job).
# It exits 0 when nothing is selected; an empty list must never reach pytest, which would run the whole suite.
#
# Tests that need what the machine lacks (Docker, KVM, a uid split) skip with a "capability:" reason. A test that
# fails here counts only when it passes on the base commit (verify_baseline.py); failures the base shares are
# printed as pre-existing, and verify passes. CI runs every test, uncapped, and lets no capability skip.
#
#   bash scripts/ci/verify.sh                 # against origin/main
#   bash scripts/ci/verify.sh --full          # every test: no selection, no cap
#   VERIFY_BASE=origin/dev bash scripts/ci/verify.sh
#   VERIFY_BUDGET=600 bash scripts/ci/verify.sh   # seconds of recorded CI time the selection may take
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
base=${VERIFY_BASE:-origin/main}
since=$(git merge-base "$base" HEAD)
full=0
for arg in "$@"; do
  case $arg in
    --full) full=1 ;;
    *) echo "verify: unknown argument $arg (want --full)" >&2; exit 2 ;;
  esac
done

mapfile -t py < <({ git diff --name-only --diff-filter=ACMR "$since" -- '*.py'
                    git ls-files --others --exclude-standard -- '*.py'; } | sort -u)
if ((${#py[@]})); then
  uv run ruff check "${py[@]}"
fi

if ((full)); then
  mapfile -t tests < <(python3 scripts/ci/select_tests.py --all)
else
  mapfile -t tests < <(python3 scripts/ci/select_tests.py --base "$base" --budget "${VERIFY_BUDGET:-240}")
fi
if ((${#tests[@]} == 0)); then
  echo "verify: no tests selected for this change"
  exit 0
fi

workers=${VERIFY_WORKERS:-4}
report=$(mktemp -d)
trap 'rm -rf "$report"' EXIT
status=0
uv run pytest -q -n "$workers" --junitxml "$report/head.xml" "${tests[@]}" || status=$?
if ((status == 0)); then
  exit 0
fi
if ((status != 1)) || [[ ! -s $report/head.xml ]]; then
  exit "$status"  # interrupted, a usage or internal error: nothing to compare
fi
uv run python3 scripts/ci/verify_baseline.py --base-sha "$since" --junit "$report/head.xml" --workers "$workers"
