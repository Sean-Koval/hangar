"""Classify trace spans with Microsoft Decision-1 (OpenRouter Decisions API) and a rule baseline.

Usage: python3 classify.py spans1.jsonl [spans2.jsonl ...] > classified.json
Needs OPENROUTER_API_KEY. Stdlib only.
"""

import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.request

URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "microsoft/microsoft-decision-1"
CATS = {
    "explore": "reading or searching the agent's own workspace, listing directories, checking the environment",
    "reference": "reading or running a third-party/reference implementation outside the workspace "
                 "(e.g. node_modules, another library's source) to learn expected behaviour",
    "edit": "creating or modifying the project's source or test files",
    "test": "running the project's own test suite (unittest/pytest)",
    "verify": "running the program or ad-hoc scripts to check behaviour, other than the test suite",
    "vcs": "git add/commit/status/log/diff",
    "other": "none of the above",
}


def rule(span: dict) -> str:
    a = (span.get("name") or "") + " " + (span.get("args") or "")
    if re.search(r"node_modules|/opt/hangar/harness|require\(", a):
        return "reference"
    if re.search(r"\bgit (add|commit|status|log|diff|show)", a):
        return "vcs"
    if re.search(r"unittest|pytest", a):
        return "test"
    if re.search(r"^(write|edit|write_to_file|replace_file_content|multi_replace|create_file)|cat >|tee |apply_patch", a):
        return "edit"
    if re.search(r"^(read|view_file|list_dir|find_by_name|grep_search|codebase_search)|\b(ls|cat|find|grep|rg|head|sed -n)\b", a):
        return "explore"
    if re.search(r"python3", a):
        return "verify"
    return "other"


def decide(state: dict, questions: dict) -> dict:
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            err = str(e)
    return {"error": err}


def classify_span(span: dict) -> dict:
    res = decide({"tool_name": span.get("name"), "arguments": span.get("args"),
                  "result_excerpt": span.get("result", "")[:200]},
                 {"category": {"type": "choice", "instructions": "What is the purpose of this single coding-agent "
                               "tool call, given its name, arguments and output excerpt?", "criteria": CATS},
                  "failed": {"type": "noul", "instructions": "Did this tool call fail or produce an error "
                             "(non-zero exit, exception, failing tests, tool error)?"}})
    a = res.get("answers", {})
    c = a.get("category", {})
    return {"d1_category": c.get("choice"), "d1_conf": c.get("confidence"),
            "d1_failed": (a.get("failed") or {}).get("noul"), "d1_cost": (res.get("usage") or {}).get("cost", 0),
            "d1_error": res.get("error")}


def main() -> None:
    out = {}
    for path in sys.argv[1:]:
        spans = [json.loads(ln) for ln in open(path) if ln.strip()]
        tools = [s for s in spans if s.get("kind") == "tool"]
        with cf.ThreadPoolExecutor(8) as ex:
            results = list(ex.map(classify_span, tools))
        for s, r in zip(tools, results):
            s.update(r)
            s["rule_category"] = rule(s)
        out[os.path.basename(path)] = [{k: s.get(k) for k in ("name", "args", "error", "dur_s", "rule_category",
                                         "d1_category", "d1_conf", "d1_failed", "d1_cost", "d1_error")}
                                       for s in tools]
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
