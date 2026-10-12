"""Per-trial metrics from normalized spans (+ optional classification). Prints one compact JSON object.

Usage: python3 metrics.py spans.jsonl [classified.json] [price_in_per_m price_out_per_m price_cache_per_m]
"""

import collections
import json
import re
import sys

spans = [json.loads(ln) for ln in open(sys.argv[1]) if ln.strip()]
summary = next((s for s in spans if s.get("summary")), {})
spans = [s for s in spans if not s.get("summary")]
cls = {}
if len(sys.argv) > 2 and sys.argv[2] != "-":
    cls = list(json.load(open(sys.argv[2])).values())[0]
tools = [s for s in spans if s["kind"] == "tool"]
llm = [s for s in spans if s["kind"] == "llm"]
res = next((s for s in spans if s["kind"] == "result"), None)
for s, c in zip(tools, cls):
    s.update({k: c.get(k) for k in ("d1_category", "d1_conf", "d1_failed", "rule_category")})

tok = {k: sum(s.get(k) or 0 for s in llm) for k in ("in", "out", "think", "cache")}
cost = sum(s.get("cost") or 0 for s in llm)
priced = None
if len(sys.argv) > 5:
    pin, pout, pcache = map(float, sys.argv[3:6])
    # Both harnesses report thinking as a subset of output tokens (pi: usage.reasoning; agy: total = in + out).
    priced = (tok["in"] * pin + tok["out"] * pout + tok["cache"] * pcache) / 1e6

seq = [s.get("d1_category") or s.get("rule_category") or "?" for s in tools]
rseq = [s.get("rule_category") or "?" for s in tools]
first_edit = next((i for i, c in enumerate(rseq) if c == "edit"), None)
test_runs = [s for s in tools if re.search(r"unittest|pytest", s.get("args") or "")]
test_fail = [s for s in test_runs if s.get("error") or re.search(r"FAILED|Error|Traceback", s.get("result") or "")]
ref = [s for s in tools if re.search(r"node_modules|/opt/hangar/harness|require\(", s.get("args") or "")]
writes = [s for s in tools if rseq[tools.index(s)] == "edit"]

# fix loops: a failing test run followed by an edit then another test run
loops = 0
state = None
for s, c in zip(tools, rseq):
    is_test = s in test_runs
    if is_test and s in test_fail:
        state = "failed"
    elif c == "edit" and state == "failed":
        state = "edited"
    elif is_test and state == "edited":
        loops += 1
        state = None

wall = None
ts = [s["t"] for s in spans if s.get("t")]
if ts:
    wall = max(ts) - min(ts)
if res and res.get("dur_s"):
    wall = res["dur_s"]

print(json.dumps({
    "llm_calls": len(llm), "tool_calls": len(tools), "tool_errors": sum(1 for s in tools if s.get("error")),
    "tokens": tok, "cost_reported": round(cost, 4) if cost else None, "cost_priced": round(priced, 4) if priced else None,
    "wall_s": round(wall) if wall else None,
    "by_category_d1": dict(collections.Counter(s.get("d1_category") for s in tools)),
    "by_category_rule": dict(collections.Counter(rseq)),
    "d1_rule_agree": sum(1 for s in tools if s.get("d1_category") == s.get("rule_category")),
    "d1_low_conf": sum(1 for s in tools if (s.get("d1_conf") or 1) < 0.7),
    "first_edit_index": first_edit, "tool_calls_before_first_edit": first_edit,
    "reference_lookups": len(ref), "test_runs": len(test_runs), "failing_test_runs": len(test_fail),
    "fix_loops": loops, "edit_calls": len(writes),
    "result_usage": res.get("usage") if res else None,
    "final_text": (summary.get("final_text") or "")[:600],
}))
