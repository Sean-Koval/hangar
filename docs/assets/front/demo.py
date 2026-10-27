"""A hangar with demo data, for the front page's recording and screenshots (and for a look around).

    uv run python docs/assets/front/demo.py            # web UI at http://127.0.0.1:8787, token below
    uv run python docs/assets/front/demo.py ticket     # label issue #42: GitHub's webhook reaches the server

It is the real control plane (REST, MCP, the web UI, jobs, sessions, the inbox) with three stand-ins, so it needs
no Docker, no GitHub and no model key:

* the fake provider from the test suite (`tests/conftest.py`): each sandbox is a directory and commands run locally;
* a scripted agent that edits the repo, runs its tests and commits, and writes Claude Code's `stream-json`;
* a stand-in GitHub API (in process) for the fictional repo `acme/checkout-api`, whose clone is a local bare repo.

Everything lives in a fresh temporary directory and goes when the server stops.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("HANGAR_DEMO_PORT", "8787"))
URL = f"http://127.0.0.1:{PORT}"
TOKEN = "hgr_demo_front_page_token_0000000000"  # the demo's admin token; nothing else accepts it
HOOK_SECRET = "demo-webhook-secret"
REPO = "acme/checkout-api"
GIT = ["git", "-c", "user.email=dev@acme.example", "-c", "user.name=Acme Dev", "-c", "init.defaultBranch=main"]

ISSUE = {
    "number": 42,
    "title": "CSV export drops non-ASCII characters",
    "body": "Exporting orders to CSV loses accented names: `Zoë Ångström` comes out as `Zo ngstrm`.\n\n"
    "Expected: the CSV keeps UTF-8 text as is.",
    "html_url": f"https://github.com/{REPO}/issues/42",
    "user": {"login": "maria", "id": 1001},
    "author_association": "MEMBER",
    "labels": [{"name": "bug"}],
}

# ------------------------------------------------------------------------------------------------- the seed repo
SEED = {
    ".gitignore": "__pycache__/\n",
    "README.md": "# checkout-api\n\nOrders, refunds and exports for the Acme shop.\n",
    "checkout/__init__.py": "",
    "checkout/export.py": textwrap.dedent('''\
        """Order exports."""

        import csv
        import io


        def to_csv(rows: list[dict[str, str]]) -> bytes:
            out = io.StringIO()
            w = csv.DictWriter(out, fieldnames=["id", "name", "total"])
            w.writeheader()
            w.writerows(rows)
            return out.getvalue().encode("ascii", "ignore")
        '''),
    "tests/__init__.py": "",
    "tests/test_export.py": textwrap.dedent("""\
        import unittest

        from checkout.export import to_csv


        class ExportTest(unittest.TestCase):
            def test_header(self):
                self.assertTrue(to_csv([]).startswith(b"id,name,total"))
        """),
}

# ------------------------------------------------------------------------------------------- the scripted agent
# Plays a coding agent: reads the prompt, works in the checkout, and narrates in Claude Code's stream-json, which
# hangar parses into the job's events (messages, commands, file changes, usage) as it would a real run.
AGENT = r"""
import json, os, re, subprocess, sys, time
prompt = open(os.environ["HANGAR_PROMPT_FILE"]).read()
pace = float(os.environ.get("DEMO_PACE", "1"))
n = 0

def emit(**e):
    print(json.dumps(e), flush=True)

def say(text, wait=1.0):
    global n
    n += 1
    emit(type="assistant", message={"id": f"m{n}", "model": "claude-sonnet-5", "role": "assistant",
         "content": [{"type": "text", "text": text}],
         "usage": {"input_tokens": 1800, "output_tokens": 160, "cache_read_input_tokens": 9000}})
    time.sleep(wait * pace)

def tool(name, args, run=None, wait=1.0):
    global n
    n += 1
    tid = f"t{n}"
    emit(type="assistant", message={"id": f"m{n}", "model": "claude-sonnet-5", "role": "assistant",
         "content": [{"type": "tool_use", "id": tid, "name": name, "input": args}],
         "usage": {"input_tokens": 600, "output_tokens": 90, "cache_read_input_tokens": 12000}})
    out, code = "", 0
    if run:
        p = subprocess.run(run, shell=True, capture_output=True, text=True)
        out, code = (p.stdout + p.stderr)[-800:], p.returncode
    emit(type="user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": out or "ok", "is_error": code != 0}]})
    time.sleep(wait * pace)
    return out

def finish(text):
    emit(type="result", subtype="success", result=text, num_turns=n, total_cost_usd=0.21,
         usage={"input_tokens": 14200, "output_tokens": 2300, "cache_read_input_tokens": 61000})

def git_commit(msg):
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.email=agent@hangar", "-c", "user.name=hangar agent", "commit", "-qm", msg],
                   check=True)

def result_md(text):
    if os.environ.get("HANGAR_JOB_DIR"):
        open(os.path.join(os.environ["HANGAR_JOB_DIR"], "RESULT.md"), "w").write(text)

FIX = (
    'import csv\nimport io\n\n\n'
    'def to_csv(rows: list[dict[str, str]]) -> bytes:\n'
    '    out = io.StringIO()\n'
    '    w = csv.DictWriter(out, fieldnames=["id", "name", "total"])\n'
    '    w.writeheader()\n'
    '    w.writerows(rows)\n'
    '    return out.getvalue().encode("utf-8")\n')
TEST = (
    '\n    def test_keeps_non_ascii(self):\n'
    '        rows = [{"id": "1", "name": "Zo\\u00eb \\u00c5ngstr\\u00f6m", "total": "9.50"}]\n'
    '        self.assertIn("Zo\\u00eb \\u00c5ngstr\\u00f6m", to_csv(rows).decode("utf-8"))\n')

if "#42" in prompt or "non-ASCII" in prompt:
    say("Reading issue #42 and the export code.", 1.2)
    tool("Read", {"file_path": "checkout/export.py"}, "cat checkout/export.py", 1.0)
    say("`to_csv` encodes with `ascii` and `ignore`, which silently drops every non-ASCII character. "
        "I'll reproduce it with a test first.", 1.4)
    with open("tests/test_export.py", "a") as f:
        f.write(TEST)
    tool("Edit", {"file_path": "tests/test_export.py", "old_string": "", "new_string": TEST}, None, 0.8)
    tool("Bash", {"command": "python3 -m unittest -q", "description": "Run the tests (expect a failure)"},
         "python3 -m unittest -q", 1.2)
    say("The new test fails as expected. Encoding as UTF-8 instead.", 1.0)
    src = open("checkout/export.py").read()
    open("checkout/export.py", "w").write(src.replace('"ascii", "ignore"', '"utf-8"'))
    tool("Edit", {"file_path": "checkout/export.py", "old_string": 'encode("ascii", "ignore")',
                  "new_string": 'encode("utf-8")'}, None, 0.8)
    tool("Bash", {"command": "python3 -m unittest -q", "description": "Run the tests"}, "python3 -m unittest -q", 1.2)
    git_commit("fix(export): keep non-ASCII text in CSV exports")
    result_md("CSV export now encodes as UTF-8 instead of dropping non-ASCII characters (`ascii`, `ignore`). "
              "Added a regression test with an accented name.")
    finish("Fixed: CSV export keeps non-ASCII text; regression test added.")
elif "ruff" in prompt:
    tool("Bash", {"command": "sed -i 's/0.14/0.15/' README.md"}, "echo 'ruff 0.15' >> README.md", 0.2)
    git_commit("chore: bump ruff to 0.15")
    result_md("Bumped ruff to 0.15; no new findings.")
    finish("Bumped ruff.")
elif "refund" in prompt.lower():
    say("Looking at the refund flow.", 0.2)
    tool("Bash", {"command": "python3 -m unittest -q"}, "python3 -m unittest -q", 0.2)
    sid = os.environ.get("HANGAR_SESSION_NEW") or os.environ.get("HANGAR_SESSION_RESUME")
    if sid:
        d = os.path.expanduser("~/.demo/sessions"); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, sid + ".jsonl"), "a").write(prompt.splitlines()[0] + "\n")
    open("REFUNDS.md", "w").write("# Partial refunds\n\nRefund line items, not whole orders.\n")
    git_commit("docs: sketch partial refunds")
    finish("Sketched partial refunds in REFUNDS.md; next: the API shape.")
elif "flaky" in prompt.lower():
    say("tests/test_payments.py fails about one run in five: it reads the wall clock.", 0.2)
    tool("Bash", {"command": "python3 -m unittest -q"}, "python3 -m unittest -q", 0.2)
    q = "Freeze time with a fixture in the test, or make the retry window in payments configurable?"
    subprocess.run(["hangar-agent", "ask", q])
    finish(q)
else:
    finish("Nothing to do.")
"""

ENV = textwrap.dedent("""\
    name: checkout-python
    description: Python 3.12 with the checkout-api repo and its tests.
    harnesses: [demo-agent]
    verify: python3 -m unittest -q
    """)


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


def make_world(root: Path) -> dict[str, Any]:
    """The bare remote standing in for github.com/acme/checkout-api, the agent and the environment."""
    remote, seed = root / "checkout-api.git", root / "seed"
    _git("init", "-q", "--bare", str(remote))
    _git("init", "-q", str(seed))
    for name, text in SEED.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text)
    _git("add", ".", cwd=seed)
    _git("commit", "-qm", "init", cwd=seed)
    _git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=seed)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    agent = root / "agent.py"
    agent.write_text(AGENT)
    envs = root / "envs"
    envs.mkdir()
    (envs / "checkout-python.yaml").write_text(ENV)
    return {"remote": remote, "agent": agent, "envs": envs}


class FakeGitHub:
    """The few GitHub REST calls a job makes, for acme/checkout-api only. Opened PRs are kept for the recording."""

    def __init__(self) -> None:
        self.prs: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []

    def __call__(self, request):  # an httpx.MockTransport handler
        import httpx

        p, m = request.url.path, request.method
        if request.url.host != "api.github.com":
            return httpx.Response(200, json={"ok": True})
        if p == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main", "full_name": REPO, "private": True})
        if p == f"/repos/{REPO}/pulls" and m == "GET":
            head = request.url.params.get("head", "")
            return httpx.Response(200, json=[pr for pr in self.prs if head.endswith(":" + pr["head"]["ref"])])
        if p == f"/repos/{REPO}/pulls" and m == "POST":
            body = json.loads(request.content)
            n = 43 + len(self.prs)
            pr = {
                **body,
                "number": n,
                "html_url": f"https://github.com/{REPO}/pull/{n}",
                "state": "open",
                "draft": body.get("draft", False),
                "head": {"ref": body["head"], "sha": "0" * 40},
                "user": {"login": "hangar-bot"},
            }
            self.prs.append(pr)
            return httpx.Response(201, json=pr)
        if p.endswith("/comments") and m == "GET":
            return httpx.Response(200, json=[])
        if p.endswith("/comments") and m == "POST":
            self.comments.append({"path": p, **json.loads(request.content)})
            return httpx.Response(201, json={"id": len(self.comments)})
        if p.endswith("/hooks") and m == "GET":
            return httpx.Response(200, json=[])
        if p.endswith("/labels") and m == "POST":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "Not Found (demo GitHub)"})


def build(root: Path):
    """The app, wired to the stand-ins. Returns (app, hangar, fake GitHub)."""
    import httpx

    from hangar import posture

    # enforce needs the git gateway and an egress proxy, which the demo's stand-ins don't run; no setting turns it
    # off since KAN-74, so the demo does what the unit tests do (tests/conftest.py)
    posture.mode = lambda settings: posture.OFF
    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.harnesses import HARNESSES, Harness
    from hangar.models import CredentialKind
    from tests.conftest import FakeProvider

    world = make_world(root)
    url = f"https://github.com/{REPO}.git"
    remap = (
        f"export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0='url.file://{world['remote']}.insteadOf' "
        f"GIT_CONFIG_VALUE_0='{url}'; "
    )

    class DemoProvider(FakeProvider):
        """The test suite's fake provider, with github.com/acme/checkout-api cloned from the local bare repo."""

        async def exec(self, sandbox, command, timeout=120, cwd=None, user=None):
            return await super().exec(sandbox, remap + command, timeout=timeout, cwd=cwd, user=user)

    HARNESSES["demo-agent"] = Harness(
        name="demo-agent",
        title="Claude Code (scripted demo)",
        run=f"python3 {world['agent']}",
        session_dirs=[".demo/sessions"],
        resume="preset",
        resume_hint="claude --resume {id}",
    )
    s = Settings(
        data_dir=root / "data",
        env_dirs=[world["envs"]],
        reaper_interval_seconds=3600,
        admin_token=TOKEN,
        host="127.0.0.1",
        port=PORT,
        session_poll_seconds=0.3,
        job_poll_seconds=0.3,
        github_token_credential="demo-gh",
        github_webhook_secret_credential="demo-hook",
        github_repos={REPO: "checkout-python"},
        gateway_port=0,
        default_provider="docker",
        enabled_providers=[],
    )
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    h = app.state.hangar
    provider = DemoProvider()
    start = h.fleet.start

    async def start_with_demo_provider() -> None:  # the fleet loads its providers when it starts: replace Docker's
        await start()
        h.fleet.providers["docker"] = provider
        h.fleet.provider_errors.pop("docker", None)

    h.fleet.start = start_with_demo_provider
    gh = FakeGitHub()
    h.jobs.transport = httpx.MockTransport(gh)
    h.broker.put("demo-gh", "git", CredentialKind.GIT_TOKEN, "ghp_demo")
    h.broker.put("demo-hook", "webhook", CredentialKind.GENERIC, HOOK_SECRET)
    return app, h, gh


def ticket_payload() -> bytes:
    return json.dumps(
        {
            "action": "labeled",
            "label": {"name": "hangar"},
            "repository": {"full_name": REPO},
            "issue": ISSUE,
            "sender": {"login": "sean", "id": 1002},
        }
    ).encode()


def send_ticket(url: str = URL) -> dict[str, Any]:
    """What GitHub sends when someone adds the `hangar` label to issue #42, signed with the webhook secret."""
    import httpx

    body = ticket_payload()
    sig = "sha256=" + hmac.new(HOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = httpx.post(
        f"{url}/api/hooks/github",
        content=body,
        timeout=30,
        headers={
            "content-type": "application/json",
            "x-github-event": "issues",
            "x-github-delivery": f"demo-{time.time()}",
            "x-hub-signature-256": sig,
        },
    )
    r.raise_for_status()
    return r.json()


def seed_history(url: str = URL) -> None:
    """Some earlier work, so the lists aren't empty: two finished jobs, a failed one and a session."""
    import httpx

    api = httpx.Client(base_url=url, headers={"authorization": f"Bearer {TOKEN}"}, timeout=30)
    jobs = [
        ("Bump ruff to 0.15 and fix new findings", "chore: bump ruff"),
        ("Fix the flaky test in tests/test_payments.py", "flaky payments test"),
    ]
    for text, title in jobs:
        api.post(
            "/api/jobs",
            json={
                "instructions": text,
                "title": title,
                "repo": f"https://github.com/{REPO}.git",
                "environment": "checkout-python",
                "deliver": "branch",
            },
        ).raise_for_status()
    api.post(
        "/api/sessions",
        json={
            "name": "partial-refunds",
            "environment": "checkout-python",
            "repo": f"https://github.com/{REPO}.git",
            "message": "Design partial refunds: refund line items, not whole orders.",
        },
    ).raise_for_status()


def wait_idle(url: str = URL, timeout: float = 60) -> None:
    import httpx

    api = httpx.Client(base_url=url, headers={"authorization": f"Bearer {TOKEN}"}, timeout=30)
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = api.get("/api/jobs").json()
        jobs = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
        if jobs and all(j["state"] in ("succeeded", "failed", "cancelled") for j in jobs):
            return
        time.sleep(0.5)
    raise TimeoutError("demo jobs did not finish")


def serve(seed: bool = True) -> None:
    import threading

    import uvicorn

    from tests.conftest import scratch_root

    # Not under /tmp: the fake provider rewrites /tmp/, /workspace and /home/agent inside commands.
    root = Path(tempfile.mkdtemp(prefix="demo-", dir=scratch_root()))
    app, _h, _gh = build(root)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", loop="asyncio"))
    if seed:

        def _seed() -> None:
            while not server.started:
                time.sleep(0.1)
            seed_history()

        threading.Thread(target=_seed, daemon=True).start()
    print(f"hangar demo: {URL}  (token {TOKEN})", flush=True)
    try:
        server.run()
    finally:
        for p in getattr(_h.fleet.providers.get("docker"), "made", []):
            p.cleanup()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["ticket"]:
        print(json.dumps(send_ticket()))
    else:
        serve(seed="--empty" not in sys.argv)
