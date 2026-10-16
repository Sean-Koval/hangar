"""End-to-end agent run through hangar's MCP endpoint, as a remote MCP client would do it.

Creates one sandbox per task (repo cloned, work branch checked out, Claude + GitHub credentials
attached), starts Claude Code in each, and reports progress. Prompts are read from
scripts/agent_tasks/<file>.md next to this script.

    HANGAR_URL=https://hangar.example.com uv run python scripts/agent_fleet_demo.py setup
    ... run | status [lines] | exec <sandbox> <cmd> | resume <branch> | teardown

The bearer token is read from ~/.hangar/gke-admin-token (HANGAR_TOKEN_FILE overrides).
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

HERE = Path(__file__).parent / "agent_tasks"
REPO = "https://github.com/Sean-Koval/hangar"
AGENTS = {  # sandbox name -> (work branch, prompt file)
    "agent-tokens": ("agent/remote-token-cli", "tokens.md"),
    "agent-json": ("agent/json-output", "json.md"),
}
IDENTITY = (
    'L=$(gh api user --jq .login) && I=$(gh api user --jq .id) && git config --global user.name "$L" '
    '&& git config --global user.email "$I+$L@users.noreply.github.com"'
)


def data(res):
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    text = "\n".join(getattr(b, "text", "") for b in res.content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def main(cmd, *args):
    url = os.environ.get("HANGAR_URL", "http://127.0.0.1:8790").rstrip("/") + "/mcp"
    token = Path(os.environ.get("HANGAR_TOKEN_FILE", "~/.hangar/gke-admin-token")).expanduser().read_text().strip()
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(url, http_client=http)) as c:

        async def call(tool, **kw):
            r = await c.call_tool(tool, kw)
            d = data(r)
            if getattr(r, "is_error", False) or getattr(r, "isError", False):
                raise SystemExit(f"{tool} failed: {d}")
            return d

        if cmd == "setup":
            t0 = time.time()
            sbs = await asyncio.gather(
                *[
                    call(
                        "create_sandbox",
                        environment="claude-python",
                        name=n,
                        repo=REPO,
                        work_branch=b,
                        credentials=["claude-api", "github-hangar"],
                        ttl_minutes=180,
                        labels={"run": "gke-e2e"},
                    )
                    for n, (b, _) in AGENTS.items()
                ]
            )
            print(f"created {len(sbs)} sandboxes in {time.time() - t0:.1f}s")
            for sb in sbs:
                print(f"  {sb['name']} {sb['id']} {sb['state']} provider={sb['provider']}")
                r = await call(
                    "exec",
                    sandbox=sb["name"],
                    command=IDENTITY + " && git config user.name && "
                    "git status -sb | head -1 && uname -r && cat /proc/version | cut -c1-60 && "
                    "claude --version && gh auth status 2>&1 | grep -E 'Logged in|Token:' | head -2",
                )
                print("   ", r["stdout"].strip().replace("\n", "\n    "), r.get("stderr", "").strip()[:300])
        elif cmd == "run":
            for n, (_, f) in AGENTS.items():
                r = await call("run_agent", sandbox=n, prompt=(HERE / f).read_text())
                print(n, json.dumps(r))
        elif cmd == "resume":  # bring in a fix branch, keep the agent's uncommitted work, restart the agent
            fix = args[0]
            note = (
                "Your previous run on this task was interrupted while running the test suite: a bug in the suite "
                "killed the agent's terminal. That fix is now included in this branch. Your changes from that run "
                "are still in the working tree; review them with `git status` and `git diff`, then continue the "
                "task below from where it stopped.\n\n"
            )
            for n, (_, f) in AGENTS.items():
                r = await call(
                    "exec",
                    sandbox=n,
                    timeout=120,
                    command=(
                        f"cd /workspace/hangar && git stash -u -q && git fetch -q origin {fix} && "
                        "git merge -q --ff-only FETCH_HEAD && git stash pop -q && git log --oneline -1 && git status -s"
                    ),
                )
                print(
                    n, f"[exit {r['exit_code']}]", r["stdout"].strip().replace("\n", " | "), r.get("stderr", "")[-200:]
                )
                if r["exit_code"] == 0:
                    run = await call("run_agent", sandbox=n, prompt=note + (HERE / f).read_text())
                    print("  started", run["run_id"])
        elif cmd == "status":
            n_lines = int(args[0]) if args else 8
            for sb in await call("list_sandboxes", label="run=gke-e2e"):
                full = await call("get_sandbox", sandbox=sb["id"])
                runs = full.get("runs") or (full.get("info") or {}).get("runs") or []
                print(f"{sb['name']} {sb['id']} {sb['state']} branch={sb['branch']}")
                for run in runs:
                    st = await call("agent_status", sandbox=sb["id"], run_id=run["id"], tail_lines=n_lines)
                    print(f"  {st['run_id']} {st['state']} exit={st['exit_code']}")
                    print("    " + "\n    ".join(st["output_tail"].strip().splitlines()[-n_lines:]))
        elif cmd == "exec":
            r = await call("exec", sandbox=args[0], command=args[1], timeout=int(args[2]) if len(args) > 2 else 120)
            print(r["stdout"], r.get("stderr", ""), f"[exit {r['exit_code']}]")
        elif cmd == "teardown":
            print(await call("destroy_sandbox", label="run=gke-e2e"))


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
