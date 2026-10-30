#!/bin/bash
# semver-mini's grader: the agent's semver.py (and nothing else from its tree) beside the hidden tests in a scratch
# directory. Writes {"passed", "total", "failed"} to $HANGAR_GRADE_OUT and exits 0 only when all five pass.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=${HANGAR_REPO:-$PWD}
G=$(mktemp -d "${TMPDIR:-/tmp}/semver-mini.XXXXXX")
trap 'rm -rf "$G"' EXIT
[ -f "$WORK/semver.py" ] && [ ! -L "$WORK/semver.py" ] && cp "$WORK/semver.py" "$G/"
cp "$HERE/hidden_test_semver.py" "$G/"
cd "$G" || exit 2
timeout 60 python3 -I hidden_test_semver.py -v > out.txt 2>&1
rc=$?
python3 -I - out.txt "${HANGAR_GRADE_OUT:-}" <<'PY'
import json, re, sys
cases = [m.groups() for m in map(re.compile(r"(test_\w+) .*\.\.\. (ok|FAIL|ERROR)").match, open(sys.argv[1])) if m]
result = {"passed": sum(s == "ok" for _, s in cases), "total": 5, "failed": [n for n, s in cases if s != "ok"]}
if sys.argv[2]:
    with open(sys.argv[2], "w") as f:
        json.dump(result, f)
print("RESULT " + json.dumps(result))
PY
exit $rc
