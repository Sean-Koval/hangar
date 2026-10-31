"""hangar's managed Tower hook: a Claude Code PreToolUse hook that asks Tower before each tool call (KAN-78).

hangar installs this file in a split sandbox as its own user (/var/lib/hangar/tower_hook.py, which the agent can read
but not change) and names it in the turn's hangar-owned `--settings` (govhook.py; spec 0015 §9). It speaks Tower's
existing hook contract, the one Tower's own laptop hook (tower/server/hooks/aw_claude_code.py) uses:

    POST {url}/hooks/claude-code      the hook's JSON as Claude Code wrote it; Authorization: Bearer <agent key>
    GET  {url}/hooks/approvals/<id>   while a person decides
    -> {"decision": "allow" | "deny" | "wait", "approval": ..., "reason": ...}

What it prints:

  allow  nothing, exit 0: the call goes on to hangar's own checks (the driver's `deny-risky` rules, or none under
         `auto`). A Tower allow never skips hangar's enforcement (spec 0015 §1), so this hook never answers "allow"
  deny   Claude Code's `permissionDecision: deny` with Tower's reason, exit 0
  wait   polls until a person decides, for at most --wait seconds, then denies with "not signed yet"

It fails closed. Claude Code lets a call through when a hook exits with anything but 2, or runs past its timeout, so
every error here exits 2 (the settings' command adds `|| exit 2` for a hook that can't even start), and the wait ends
well before the timeout hangar sets. Standard library only, Python 3.8 or later: it runs on the sandbox's python3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

AGENT = "hangar-tower-hook/1"


class Blocked(Exception):
    """Anything that stops us getting an answer: the call is blocked."""


def read_key(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError as e:
        raise Blocked(f"no agent key at {path} ({e.strerror})") from None
    if not key:
        raise Blocked(f"the agent key at {path} is empty")
    return key


def call(url: str, key: str, method: str, path: str, body: dict | None, timeout: float) -> dict:
    headers = {"authorization": f"Bearer {key}", "content-type": "application/json", "user-agent": AGENT}
    tp = os.environ.get("TRACEPARENT", "")
    if tp:
        headers["traceparent"] = tp[:128]
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            out = json.loads(res.read(1_000_000))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Blocked(f"Tower refused this sandbox's agent key ({e.code})") from None
        raise Blocked(f"Tower answered {e.code}") from None  # a 5xx, even 503 {"decision": "deny"}, is unavailable
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Blocked(f"can't reach Tower at {url} ({getattr(e, 'reason', e)})") from None
    except ValueError:
        raise Blocked("Tower sent an answer this hook can't read") from None
    if not isinstance(out, dict):
        raise Blocked("Tower sent an answer this hook can't read")
    return out


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason[:2000]}}))


def run(args: argparse.Namespace, stdin) -> int:
    url = args.url.rstrip("/")
    hook = json.load(stdin)
    if not isinstance(hook, dict):
        raise Blocked("the hook's input isn't a JSON object")
    key = read_key(args.key_file)
    result = call(url, key, "POST", "/hooks/claude-code", hook, args.timeout)
    deadline = time.monotonic() + args.wait
    reason = ""
    while result.get("decision") == "wait":
        approval = str(result.get("approval") or "")
        reason = str(result.get("reason") or reason)
        if not approval or "/" in approval:
            raise Blocked("Tower asked to wait without an approval id")
        if time.monotonic() >= deadline:
            deny(f"Tower: {reason or 'waiting for a signature'} ({approval} isn't signed yet). Carry "
                 "on with other work and try this step again later: once it's signed, it goes through.")
            return 0
        time.sleep(min(args.poll, max(0.0, deadline - time.monotonic())))
        result = call(url, key, "GET", f"/hooks/approvals/{approval}", None, args.timeout)
    decision = result.get("decision")
    if decision == "deny":
        deny(f"Denied by Tower: {result.get('reason') or 'policy'}")
        return 0
    if decision == "allow":
        return 0  # nothing printed: hangar's own checks still apply
    raise Blocked(f"unexpected answer from Tower: {str(result)[:200]}")


def main(argv: list | None = None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--wait", type=float, default=300.0, help="seconds to wait for a signature")
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--timeout", type=float, default=10.0, help="seconds per request to Tower")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        print("Blocked by hangar's Tower hook (it fails closed): bad arguments", file=sys.stderr)
        return 2
    try:
        return run(args, stdin or sys.stdin)
    except Blocked as e:
        print(f"Blocked by hangar's Tower hook (it fails closed): {e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - anything unexpected also blocks
        print(f"Blocked by hangar's Tower hook (it fails closed): {type(e).__name__}: {e}"[:500], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
