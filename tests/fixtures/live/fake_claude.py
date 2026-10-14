"""A scripted Claude Code for tests: `claude -p --input-format stream-json --output-format stream-json`.

It speaks the protocol as verified against Claude Code 2.1.296 (see hangar/driver.py): user messages on stdin,
`command_lifecycle` frames, `can_use_tool` control requests with `--permission-prompt-tool stdio`, `interrupt`
control requests (open prompts cancelled, result `error_during_execution`), messages that arrive mid-turn folded
into it, and results whose `total_cost_usd` and `modelUsage` count the whole process.

What it does is scripted by the message: TOOL runs `touch tool-ran.txt` (asking first when prompts are on), SLOW
works for a few seconds (steers fold in), and every turn ends with "did: <first line> | steered: <messages>".
It keeps a conversation file under ~/.claude/projects like the real CLI, so resume and fork can be checked.
"""

import argparse
import json
import os
import select
import shutil
import sys
import time
import uuid

ap = argparse.ArgumentParser()
ap.add_argument("--session-id")
ap.add_argument("--resume")
ap.add_argument("--fork-session", action="store_true")
ap.add_argument("--permission-prompt-tool")
ap.add_argument("--dangerously-skip-permissions", action="store_true")
args, _ = ap.parse_known_args()

projects = os.path.expanduser("~/.claude/projects/fake")
os.makedirs(projects, exist_ok=True)
if args.resume and not os.path.exists(f"{projects}/{args.resume}.jsonl"):
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": f"No conversation found with session ID: {args.resume}"}), flush=True)
    sys.exit(1)
sid = args.session_id if (args.fork_session or not args.resume) else args.resume
sid = sid or str(uuid.uuid4())
if args.fork_session:
    shutil.copy(f"{projects}/{args.resume}.jsonl", f"{projects}/{sid}.jsonl")
conversation = f"{projects}/{sid}.jsonl"
asking = args.permission_prompt_tool == "stdio" and not args.dangerously_skip_permissions
usage = {"in": 0, "out": 0}
buf = b""


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def lifecycle(u, state):
    emit({"type": "command_lifecycle", "command_uuid": u, "state": state, "uuid": str(uuid.uuid4()),
          "session_id": sid})


def read(timeout):
    """Next stdin object within `timeout` (None on timeout); EOF exits."""
    global buf
    end = time.time() + timeout
    while b"\n" not in buf:
        left = end - time.time()
        if left <= 0:
            return None
        r, _, _ = select.select([sys.stdin.fileno()], [], [], left)
        if not r:
            return None
        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            sys.exit(0)
        buf += chunk
    line, buf = buf.split(b"\n", 1)
    return json.loads(line)


def model_call(text):
    usage["in"] += 50
    usage["out"] += 10
    emit({"type": "assistant", "session_id": sid, "message": {
        "id": f"msg_{uuid.uuid4().hex[:8]}", "model": "claude-sonnet-5-5", "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}}})


def result(text, subtype="success"):
    emit({"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": text,
          "session_id": sid, "total_cost_usd": round(usage["in"] * 2e-6 + usage["out"] * 1e-5, 6),
          "usage": {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0},
          "modelUsage": {"claude-sonnet-5-5": {"inputTokens": usage["in"], "outputTokens": usage["out"],
                                               "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}},
          "queued_turn_count": 0})


class Interrupted(Exception):
    pass


def wait_for(pred, timeout, steers, prompt_id=None):
    """Read stdin until `pred(obj)`; user messages fold into `steers`, an interrupt raises Interrupted."""
    end = time.time() + timeout
    while time.time() < end:
        obj = read(min(0.1, max(0.0, end - time.time())))
        if obj is None:
            continue
        if obj.get("type") == "user":
            lifecycle(obj["uuid"], "queued")
            steers.append(obj)
        elif obj.get("type") == "control_request" and obj["request"].get("subtype") == "interrupt":
            if prompt_id:
                emit({"type": "control_cancel_request", "request_id": prompt_id})
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": obj["request_id"],
                                                           "response": {"still_queued": []}}})
            raise Interrupted
        elif pred(obj):
            return obj
    return None


def turn(msg, first):
    text = msg["message"]["content"]
    u = msg["uuid"]
    lifecycle(u, "started")
    if first:
        emit({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-sonnet-5-5",
              "permissionMode": "default" if asking else "bypassPermissions"})
    with open(conversation, "a") as f:
        f.write(json.dumps({"user": text}) + "\n")
    steers: list = []
    try:
        if "TOOL" in text:
            tid = f"toolu_{uuid.uuid4().hex[:8]}"
            cmd = "touch tool-ran.txt"
            emit({"type": "assistant", "session_id": sid, "message": {
                "id": f"msg_{uuid.uuid4().hex[:8]}", "model": "claude-sonnet-5-5", "role": "assistant",
                "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}],
                "usage": {"input_tokens": 50, "output_tokens": 10}}})
            usage["in"] += 50
            usage["out"] += 10
            allowed, note = True, ""
            if asking:
                rid = str(uuid.uuid4())
                emit({"type": "control_request", "request_id": rid, "request": {
                    "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": cmd}, "tool_use_id": tid,
                    "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash",
                                                                               "ruleContent": "touch:*"}],
                                                "behavior": "allow", "destination": "localSettings"}]}})
                reply = wait_for(lambda o: o.get("type") == "control_response"
                                 and o["response"].get("request_id") == rid, 600, steers, rid)
                resp = (reply or {}).get("response", {}).get("response", {})
                allowed = resp.get("behavior") == "allow"
                note = resp.get("message", "")
                if resp.get("updatedPermissions"):
                    with open(conversation, "a") as f:
                        f.write(json.dumps({"rules": resp["updatedPermissions"]}) + "\n")
            if allowed:
                open("tool-ran.txt", "w").close()
            emit({"type": "user", "session_id": sid, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": not allowed,
                 "content": "" if allowed else note}]}})
        if "SLOW" in text:
            wait_for(lambda o: False, 4, steers)
        for s in steers:
            lifecycle(s["uuid"], "started")
            with open(conversation, "a") as f:
                f.write(json.dumps({"user": s["message"]["content"]}) + "\n")
        said = "did: " + text.splitlines()[0][:60]
        said += "".join(" | steered: " + s["message"]["content"] for s in steers)
        model_call(said)
        for s in steers:
            lifecycle(s["uuid"], "completed")
        result(said)
        lifecycle(u, "completed")
    except Interrupted:
        emit({"type": "user", "session_id": sid,
              "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}})
        result("", subtype="error_during_execution")
        return u  # cancelled once the next message comes, as the real CLI reports it
    return None


first, cancelled = True, None
while True:
    obj = read(3600)
    if obj is None or obj.get("type") != "user":
        continue
    lifecycle(obj["uuid"], "queued")
    if cancelled:
        lifecycle(cancelled, "cancelled")
    cancelled = turn(obj, first)
    first = False
