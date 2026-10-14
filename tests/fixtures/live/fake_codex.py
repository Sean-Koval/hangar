"""A scripted `codex app-server` for tests: JSON-RPC over stdio without the `jsonrpc` field.

It follows the protocol as verified against codex 0.162.1 (see hangar/driver.py): initialize, thread/start,
thread/resume and thread/fork (a rollout file per thread under $CODEX_HOME/sessions), turn/start, turn/steer
(-32600 when no turn is active), turn/interrupt (status `interrupted`, its open approval resolved), approval
requests under approvalPolicy `untrusted`, and thread token totals that include earlier turns of the thread.

The message scripts it: TOOL runs `touch tool-ran.txt` (asking first unless approvalPolicy is never), SLOW works for
a few seconds, and every turn ends with "did: <first line> | steered: <messages>".
"""

import json
import os
import select
import shutil
import sys
import time
import uuid

home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
sessions = os.path.join(home, "sessions")
os.makedirs(sessions, exist_ok=True)
state = {"thread": None, "policy": "untrusted", "turn": None, "ids": 1000}
totals = {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0}
buf = b""


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def notify(method, **params):
    send({"method": method, "params": params})


def read(timeout):
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


def rollout(tid):
    return os.path.join(sessions, f"rollout-2026-10-13T00-00-00-{tid}.jsonl")


def model_call():
    last = {"inputTokens": 100, "cachedInputTokens": 20, "outputTokens": 10}
    for k, v in last.items():
        totals[k] += v
    notify("thread/tokenUsage/updated", threadId=state["thread"], turnId=state["turn"]["id"],
           tokenUsage={"total": dict(totals, totalTokens=totals["inputTokens"] + totals["outputTokens"]),
                       "last": dict(last, totalTokens=110)})


def open_thread(tid, params):
    state["thread"], state["policy"] = tid, params.get("approvalPolicy") or "untrusted"
    with open(rollout(tid)) as f:
        earlier = sum(1 for _ in f)
    for k, v in {"inputTokens": 100, "cachedInputTokens": 20, "outputTokens": 10}.items():
        totals[k] = v * earlier  # the thread's totals count its earlier turns
    return {"thread": {"id": tid, "path": rollout(tid), "status": {"type": "idle"}}}


def request(rid, method, p):
    t = state["turn"]
    if method == "initialize":
        return send({"id": rid, "result": {"userAgent": "fake-codex/0.162.1"}})
    if method == "thread/start":
        tid = str(uuid.uuid4())
        open(rollout(tid), "w").close()
        res = open_thread(tid, p)
        notify("thread/started", thread=res["thread"])
        return send({"id": rid, "result": res})
    if method in ("thread/resume", "thread/fork"):
        src = p.get("threadId")
        if not src or not os.path.exists(rollout(src)):
            return send({"id": rid, "error": {"code": -32600, "message": f"no rollout found for thread id {src}"}})
        tid = src
        if method == "thread/fork":
            tid = str(uuid.uuid4())
            shutil.copy(rollout(src), rollout(tid))
        return send({"id": rid, "result": open_thread(tid, p)})
    if method == "turn/start":
        if t is not None:
            return send({"id": rid, "error": {"code": -32600, "message": "a turn is already active"}})
        text = p["input"][0]["text"]
        with open(rollout(state["thread"]), "a") as f:
            f.write(json.dumps({"user": text}) + "\n")
        steps = (["tool"] if "TOOL" in text else []) + (["slow"] if "SLOW" in text else []) + ["finish"]
        turn = {"id": str(uuid.uuid4()), "text": text, "steps": steps, "steers": [], "waiting": None, "until": 0}
        state["turn"] = turn
        send({"id": rid, "result": {"turn": {"id": turn["id"], "status": "inProgress", "items": []}}})
        notify("turn/started", threadId=state["thread"], turn={"id": turn["id"], "status": "inProgress"})
        return None
    if method == "turn/steer":
        if t is None or p.get("expectedTurnId") != t["id"]:
            return send({"id": rid, "error": {"code": -32600, "message": "no active turn to steer"}})
        t["steers"].append(p["input"][0]["text"])
        return send({"id": rid, "result": {"turnId": t["id"]}})
    if method == "turn/interrupt":
        send({"id": rid, "result": {}})
        if t is not None:
            if t["waiting"] is not None:
                notify("serverRequest/resolved", threadId=state["thread"], requestId=t["waiting"])
            notify("turn/completed", threadId=state["thread"], turn={"id": t["id"], "status": "interrupted"})
            state["turn"] = None
        return None
    return send({"id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})


def decided(t, decision):
    t["waiting"] = None
    ok = decision in ("accept", "acceptForSession")
    if ok:
        open("tool-ran.txt", "w").close()
    item = {"type": "commandExecution", "id": t["item"], "command": "touch tool-ran.txt", "cwd": os.getcwd(),
            "status": "completed" if ok else "declined", "exitCode": 0 if ok else None, "aggregatedOutput": ""}
    notify("item/completed", threadId=state["thread"], turnId=t["id"], item=item)


def advance():
    t = state["turn"]
    if t is None or t["waiting"] is not None or time.time() < t["until"]:
        return
    step = t["steps"].pop(0)
    if step == "tool":
        model_call()
        t["item"] = f"call_{uuid.uuid4().hex[:6]}"
        notify("item/started", threadId=state["thread"], turnId=t["id"], item={
            "type": "commandExecution", "id": t["item"], "command": "touch tool-ran.txt", "status": "inProgress"})
        if state["policy"] == "never":
            return decided(t, "accept")
        state["ids"] += 1
        t["waiting"] = state["ids"]
        send({"id": t["waiting"], "method": "item/commandExecution/requestApproval", "params": {
            "threadId": state["thread"], "turnId": t["id"], "itemId": t["item"], "command": "touch tool-ran.txt",
            "cwd": os.getcwd(), "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"]}})
    elif step == "slow":
        t["until"] = time.time() + 4
    else:
        model_call()
        said = "did: " + t["text"].splitlines()[0][:60] + "".join(" | steered: " + s for s in t["steers"])
        item = {"type": "agentMessage", "id": f"msg_{uuid.uuid4().hex[:6]}", "text": said}
        notify("item/started", threadId=state["thread"], turnId=t["id"], item=item)
        notify("item/completed", threadId=state["thread"], turnId=t["id"], item=item)
        notify("turn/completed", threadId=state["thread"], turn={"id": t["id"], "status": "completed"})
        state["turn"] = None


while True:
    obj = read(0.05)
    if obj is not None:
        if "method" in obj and "id" in obj:
            request(obj["id"], obj["method"], obj.get("params") or {})
        elif "id" in obj and state["turn"] is not None and obj["id"] == state["turn"]["waiting"]:
            decided(state["turn"], (obj.get("result") or {}).get("decision"))
    advance()
