#!/bin/bash
# Grade a finished trial: copy the agent's semver.py next to the hidden tests, run them, summarise by category.
set -u
G=/tmp/grade; rm -rf "$G"; mkdir -p "$G"
W=/workspace/semver
echo "files: $(cd $W && ls | tr '\n' ' ')"
echo "commits: $(cd $W && git log --oneline | wc -l) dirty: $(cd $W && git status --porcelain | wc -l)"
echo "loc: semver.py=$(wc -l < $W/semver.py 2>/dev/null || echo 0) test_semver.py=$(wc -l < $W/test_semver.py 2>/dev/null || echo 0)"
OWN=$(cd $W && timeout 120 python3 -m unittest 2>&1 | tail -3 | tr '\n' ' ')
echo "own_tests: $OWN"
[ -f $W/semver.py ] || { echo "RESULT {}"; exit 0; }
cp $W/semver.py "$G/" && cp "${KIT:-$(dirname "$0")}/hidden_test_semver.py" "$G/"
cd "$G" && timeout 300 python3 -m unittest -v hidden_test_semver > out.txt 2>&1
python3 - <<'EOF'
import json, re, collections
tot=collections.Counter(); ok=collections.Counter(); fails=[]
for ln in open('/tmp/grade/out.txt', errors='replace'):
    m=re.match(r'test_([a-z_]+?)__(\d+) .*\.\.\. (ok|FAIL|ERROR)', ln)
    if m:
        tot[m.group(1)]+=1
        if m.group(3)=='ok': ok[m.group(1)]+=1
        else: fails.append(f"{m.group(1)}#{m.group(2)}")
print("RESULT "+json.dumps({"passed": sum(ok.values()), "total": sum(tot.values()),
      "by_category": {k: [ok[k], tot[k]] for k in sorted(tot)}, "failed": fails}))
EOF
