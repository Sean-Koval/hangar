#!/usr/bin/env bash
# The check hangar jobs on this repo run (`verify:` in envs/hangar.yaml): ruff on the Python files the branch
# changed, then the tests those changes can break (select_tests.py, as pre-merge CI picks them). It exits 0 when
# nothing is selected; an empty list must never reach pytest, which would run the whole suite.
#
#   bash scripts/ci/verify.sh                 # against origin/main
#   VERIFY_BASE=origin/dev bash scripts/ci/verify.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
base=${VERIFY_BASE:-origin/main}
since=$(git merge-base "$base" HEAD)

mapfile -t py < <({ git diff --name-only --diff-filter=ACMR "$since" -- '*.py'
                    git ls-files --others --exclude-standard -- '*.py'; } | sort -u)
if ((${#py[@]})); then
  uv run ruff check "${py[@]}"
fi

mapfile -t tests < <(python3 scripts/ci/select_tests.py --base "$base")
if ((${#tests[@]} == 0)); then
  echo "verify: no tests selected for this change"
  exit 0
fi
uv run pytest -q -n "${VERIFY_WORKERS:-4}" "${tests[@]}"
