"""Normalize a pi (--mode json) or Antigravity (stream-json) run log into one span schema.

Usage: python3 spans.py <harness> <log> > spans.jsonl   (prints one JSON span per line, then a summary line)
Span: {kind: llm|tool, name, args, dur_s, t, in, out, think, cache, cost, error}
"""

import json
import re
import sys

harness, path = sys.argv[1], sys.argv[2]
spans: list[dict] = []
final_text = ""


def short(x, n=240):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    s = re.sub(r"\s+", " ", s)
    return s[:n]


events = []
for ln in open(path, errors="replace"):
    ln = ln.strip()
    if ln.startswith("{"):
        try:
            events.append(json.loads(ln))
        except ValueError:
            pass

if harness == "pi":
    starts = {}
    for e in events:
        t = e.get("type")
        if t == "message_end":
            m = e.get("message") or {}
            if m.get("role") == "assistant":
                u = m.get("usage") or {}
                texts = [c.get("text", "") for c in m.get("content", []) if c.get("type") == "text"]
                think = [c for c in m.get("content", []) if c.get("type") == "thinking"]
                spans.append({"kind": "llm", "name": m.get("model"), "args": short(" ".join(texts), 160),
                              "dur_s": None, "t": m.get("timestamp", 0) / 1000, "in": u.get("input", 0),
                              "out": u.get("output", 0), "think": u.get("reasoning", 0) or 0,
                              "cache": u.get("cacheRead", 0), "cost": (u.get("cost") or {}).get("total", 0.0),
                              "error": m.get("stopReason") == "error", "n_thinking_blocks": len(think)})
                if texts:
                    final_text = " ".join(texts)
        elif t == "tool_execution_start":
            starts[e.get("toolCallId")] = e
        elif t == "tool_execution_end":
            s = starts.get(e.get("toolCallId"), {})
            r = e.get("result") or {}
            r = r if isinstance(r, dict) else {"content": [{"text": str(r)}]}
            out = " ".join(c.get("text", "") for c in r.get("content", []) if isinstance(c, dict))
            sc = r.get("structuredContent") if isinstance(r, dict) else None
            sc = sc if isinstance(sc, dict) else {}
            spans.append({"kind": "tool", "name": e.get("toolName") or s.get("toolName"),
                          "args": short(s.get("args") or {}), "dur_s": sc.get("wall_time_seconds"),
                          "t": None, "in": 0, "out": 0, "think": 0, "cache": 0, "cost": 0.0,
                          "error": bool(e.get("isError")), "result": short(out, 200)})
    # pi timestamps assistant messages; give tools the timestamp of the preceding llm span
    last_t = None
    for s in spans:
        if s["t"] is not None:
            last_t = s["t"]
        else:
            s["t"] = last_t
elif harness == "antigravity":
    for e in events:
        if e.get("event") == "step_update":
            su = e["step_update"]
            if su.get("state") != "DONE":
                continue
            st = su.get("step_type")
            if st == "agent_response":
                u = su.get("usage") or {}
                spans.append({"kind": "llm", "name": "gemini", "args": "", "dur_s": su.get("duration_seconds"),
                              "t": None, "in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
                              "think": u.get("thinking_tokens", 0), "cache": u.get("cache_read_tokens", 0),
                              "cost": None, "error": False, "step": su.get("step_index")})
            elif st == "tool":
                ti = su.get("tool_info") or {}
                out = ti.get("output", "")
                err = bool(re.search(r"(exit code|exited with code)[: ]+[1-9]|Traceback|Error:", str(out)[:4000]))
                spans.append({"kind": "tool", "name": su.get("tool_name"), "args": short(ti.get("parameters") or {}),
                              "dur_s": su.get("duration_seconds"), "t": None, "in": 0, "out": 0, "think": 0,
                              "cache": 0, "cost": 0.0, "error": err, "result": short(out, 200),
                              "step": su.get("step_index")})
            else:
                spans.append({"kind": "other", "name": st, "args": "", "dur_s": su.get("duration_seconds"),
                              "t": None, "in": 0, "out": 0, "think": 0, "cache": 0, "cost": 0.0, "error": False,
                              "step": su.get("step_index")})
        elif e.get("event") == "result":
            r = e["result"]
            final_text = r.get("response", "")
            spans.append({"kind": "result", "name": r.get("status"), "args": "", "dur_s": r.get("duration_seconds"),
                          "t": None, **{k: 0 for k in ("in", "out", "think", "cache")}, "cost": None,
                          "error": r.get("status") != "SUCCESS", "usage": r.get("usage")})

for s in spans:
    print(json.dumps(s, ensure_ascii=False))
print(json.dumps({"summary": True, "events": len(events), "spans": len(spans), "final_text": final_text[:1500]}))
