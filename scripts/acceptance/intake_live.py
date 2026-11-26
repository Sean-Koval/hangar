"""KAN-27: the live intake check on a deployed hangar. A real Jira ticket goes through intake once, end to end.

    python scripts/acceptance/intake_live.py preflight --ticket KAN-123
    python scripts/acceptance/intake_live.py run --ticket KAN-123 [--report intake-live.json] [--summary intake-live.md]

`docs/runbooks/intake-check.md` is the checklist. This script never moves the ticket itself. A person moves it in
Jira when the script asks: the point is that Jira's own webhook, from a real move by a linked person, starts the job.
`run` then checks, and records in --report and --summary:

1. **One job.** Within --start-timeout of the move, exactly one `source=jira` job appears for the ticket. The job is
   the mover's, not waiting for approval (the mover is linked), with the ticket's key in its title.
2. **No duplicate.** While that job is still open, the person moves the ticket out of the trigger status and back in.
   After --dup-wait seconds the ticket still has exactly one job (`intake.open_job` ignores the second move).
3. **A draft PR.** The job ends `succeeded` within --job-timeout, with a PR. When a GitHub token is at hand
   (`HANGAR_ACCEPT_GITHUB_TOKEN`, `GITHUB_TOKEN` or `GH_TOKEN`), the PR is a draft, its title starts with the key
   and its body has a `Jira: KEY` line (what the GitHub for Jira app links). Without a token those three are "not
   checked", never passed.

It spends what one real job spends on the ticket's repo (the job's budget caps it); `preflight` spends nothing. The
hangar is $HANGAR_URL with $HANGAR_TOKEN, else what `hangar login` saved, as for gke.sh. Exit status: 0 when every
check passed, 1 when one failed, 3 when preflight found the setup not ready.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from hangar_accept import Api, Fail, target  # noqa: E402

KEY = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
TERMINAL = {"succeeded", "failed", "cancelled"}
OPEN_STATES_PAST_START = {"provisioning", "running", "verifying", "needs_input"}
NOT_READY = 3
GITHUB_TRANSPORT: httpx.BaseTransport | None = None  # tests put a stand-in GitHub here


def say(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------- reading hangar
def setting(api: Api, name: str) -> Any:
    r = api.http.get(f"/api/settings/{name}")
    return r.json().get("value") if r.status_code == 200 else None


def ticket_jobs(api: Api, key: str, since: float = 0.0, pages: int = 20) -> list[dict[str, Any]]:
    """The ticket's Jira jobs created at or after `since`, newest first (a page at a time, newest first)."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(pages):
        params: dict[str, Any] = {"source": "jira", "limit": 100, **({"cursor": cursor} if cursor else {})}
        r = api.http.get("/api/jobs", params=params)
        if r.status_code != 200:
            raise Fail(f"GET /api/jobs: HTTP {r.status_code}: {r.text[:200]}")
        page = r.json()
        older = False
        for j in page:
            if (j.get("created_at") or 0) < since:
                older = True
                continue
            if is_for(j, key):
                found.append(j)
        cursor = r.headers.get("X-Next-Cursor")
        if not cursor or older:
            break
    return found


def is_for(job: dict[str, Any], key: str) -> bool:
    labels = job.get("labels") or {}
    return labels.get("jira.issue") == key or job.get("source_ref") == f"jira:{key}"


def project_of(key: str) -> str:
    return key.rsplit("-", 1)[0]


# ---------------------------------------------------------------------------- preflight
def preflight(api: Api, key: str) -> tuple[list[str], dict[str, Any]]:
    """What stops the live check, if anything, and what it found. Spends nothing."""
    problems: list[str] = []
    found: dict[str, Any] = {}
    if not KEY.match(key):
        return [f"{key!r} isn't a Jira issue key (like KAN-123)"], found
    cfg = setting(api, "HANGAR_JIRA")
    if not cfg:
        return ["Jira intake isn't configured, or this token can't read it (HANGAR_JIRA; an admin token can; "
                "docs/intake/jira.md)"], found
    trigger = cfg.get("trigger") or {}
    found["trigger"] = trigger
    if not (trigger.get("status") or trigger.get("label")):
        problems.append("HANGAR_JIRA has no trigger status or label")
    projects = cfg.get("projects") or {}
    mapped = projects.get(project_of(key))
    if not mapped:
        problems.append(f"project {project_of(key)} isn't mapped to a repo in HANGAR_JIRA "
                        f"(mapped: {', '.join(sorted(projects)) or 'none'})")
    else:
        found["repo"] = mapped.get("repo")
    open_jobs = [j for j in ticket_jobs(api, key) if j.get("state") not in TERMINAL]
    if open_jobs:
        problems.append(f"{key} already has an open job ({open_jobs[0]['id']}, {open_jobs[0].get('state')}): a "
                        "move now would be ignored, so finish or cancel it first")
    power = api.http.get("/api/admin/power")
    if power.status_code == 200:
        found["power"] = (power.json().get("power") or {}).get("state")
        if found["power"] not in (None, "up"):
            problems.append(f"the sandbox pools are {found['power']}: the job would queue until they're up "
                            "(hangar power up)")
    doctor = api.http.get("/api/doctor")
    if doctor.status_code == 200:
        body = doctor.json()
        rows = body if isinstance(body, list) else []
        jira_rows = [c for c in rows if isinstance(c, dict) and str(c.get("name", "")).startswith("jira")]
        found["doctor_jira"] = [{k: c.get(k) for k in ("name", "status", "detail")} for c in jira_rows]
        problems += [f"doctor: {c.get('name')}: {c.get('detail')}" for c in jira_rows if c.get("status") == "fail"]
    return problems, found


# ---------------------------------------------------------------------------- GitHub
def github_token() -> str | None:
    for name in ("HANGAR_ACCEPT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        if v := (os.environ.get(name) or "").strip():
            return v
    return None


def pr_checks(pr_url: str, key: str, token: str | None) -> dict[str, str]:
    """The PR's draft, title and `Jira:` line: "pass", "fail: why" or "not checked: why"."""
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url or "")
    names = ("draft", "title", "jira_line")
    if not m:
        return dict.fromkeys(names, f"not checked: {pr_url!r} isn't a GitHub PR URL")
    if not token:
        return dict.fromkeys(names, "not checked: no GitHub token (HANGAR_ACCEPT_GITHUB_TOKEN)")
    owner, repo, num = m.groups()
    with httpx.Client(base_url="https://api.github.com", transport=GITHUB_TRANSPORT, timeout=30,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}) as gh:
        r = gh.get(f"/repos/{owner}/{repo}/pulls/{num}")
    if r.status_code != 200:
        return dict.fromkeys(names, f"not checked: GitHub answered HTTP {r.status_code}")
    pr = r.json()
    body = pr.get("body") or ""
    return {
        "draft": "pass" if pr.get("draft") else "fail: the PR isn't a draft",
        "title": "pass" if str(pr.get("title") or "").startswith(key) else
                 f"fail: the title {str(pr.get('title'))[:80]!r} doesn't start with {key}",
        "jira_line": "pass" if re.search(rf"^Jira: {re.escape(key)}\s*$", body, re.M) else
                     f"fail: the body has no `Jira: {key}` line",
    }


# ---------------------------------------------------------------------------- the run
def wait_for(fn: Callable[[], Any], timeout: float, poll: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        got = fn()
        if got:
            return got
        if time.monotonic() > deadline:
            raise Fail(f"{what}: nothing after {timeout:.0f}s")
        time.sleep(poll)


def ask(prompt: str, opts: argparse.Namespace) -> None:
    say(f"\n>>> {prompt}")
    if opts.yes:
        return
    input("    Press Enter once done. ")


def run(api: Api, opts: argparse.Namespace, results: dict[str, Any]) -> list[str]:
    key = opts.ticket
    checks: dict[str, str] = results.setdefault("checks", {})
    failures: list[str] = []
    problems, found = preflight(api, key)
    results["preflight"] = found
    if problems:
        raise Fail("preflight: " + "; ".join(problems))
    trigger = found.get("trigger") or {}
    door = f"the status '{trigger['status']}'" if trigger.get("status") else f"the label '{trigger.get('label')}'"
    t0 = time.time()
    ask(f"In Jira, move {key} to {door} now, as the person linked to your hangar account.", opts)
    jobs = wait_for(lambda: ticket_jobs(api, key, since=t0 - 5), opts.start_timeout, opts.poll,
                    f"a job for {key} after the move")
    job = jobs[-1]  # the oldest since the move
    results["job"] = job["id"]
    results["timings"] = {"move_to_job_seconds": round((job.get("created_at") or t0) - t0, 1)}
    say(f"    job {job['id']} ({job.get('state')}) for {job.get('person')}: {job.get('title')}")
    one = len(jobs) == 1
    checks["one_job"] = "pass" if one else f"fail: {len(jobs)} jobs for {key} after one move"
    if job.get("state") == "awaiting_approval":
        checks["linked_person"] = "fail: the job waits for approval: the mover isn't linked to a hangar person"
    else:
        checks["linked_person"] = "pass" if job.get("person") else "fail: the job belongs to no person"
    checks["title_key"] = "pass" if str(job.get("title") or "").startswith(key) else \
        f"fail: the job's title {job.get('title')!r} doesn't start with {key}"

    # --- the second move, while the job is still open
    current = api.call("GET", f"/api/jobs/{job['id']}")
    if current.get("state") in TERMINAL:
        checks["no_duplicate"] = (f"not checked: the job was already {current['state']} before the second move; "
                                  "rerun on a ticket whose job takes longer")
    else:
        ask(f"Move {key} out of {door} and back into it (the job {job['id']} is still {current.get('state')}).", opts)
        time.sleep(opts.dup_wait)
        after = ticket_jobs(api, key, since=t0 - 5)
        checks["no_duplicate"] = "pass" if len(after) == 1 else \
            f"fail: {len(after)} jobs for {key} after the second move: {', '.join(j['id'] for j in after)}"

    # --- the end: a draft PR linked to the ticket
    say(f"    waiting up to {opts.job_timeout / 60:.0f} min for {job['id']} to finish")
    start = time.monotonic()
    seen = ""
    asked: list[bool] = []  # the job's question is printed once

    def finished() -> dict[str, Any] | None:
        nonlocal seen
        j = api.call("GET", f"/api/jobs/{job['id']}")
        if j.get("state") != seen:
            seen = j.get("state") or ""
            say(f"    {job['id']}: {seen}")
        if j.get("state") == "needs_input" and not asked:
            asked.append(True)
            say(f"    the job asks a question: {(j.get('result') or {}).get('question') or 'see the ticket'}. "
                "Answer it on the ticket (a comment); this keeps waiting")
        return j if j.get("state") in TERMINAL else None

    end = wait_for(finished, opts.job_timeout, opts.poll, f"{job['id']} to finish")
    results["timings"]["job_seconds"] = round(time.monotonic() - start, 1)
    results["state"] = end.get("state")
    pr_url = (end.get("result") or {}).get("pr_url")
    results["pr"] = pr_url
    checks["succeeded"] = "pass" if end.get("state") == "succeeded" else \
        f"fail: the job ended {end.get('state')}: {end.get('reason') or ''}".rstrip(": ")
    checks["pr_opened"] = "pass" if pr_url else \
        f"fail: no PR ({(end.get('result') or {}).get('pr_error') or 'no reason given'})"
    if pr_url:
        checks.update({f"pr_{k}": v for k, v in pr_checks(pr_url, key, github_token()).items()})
    usage = end.get("usage") or {}
    if usage.get("cost_usd") is not None:
        results["cost_usd"] = usage["cost_usd"]
    failures = [f"{k}: {v.removeprefix('fail: ')}" for k, v in checks.items() if v.startswith("fail")]
    return failures


def summary(results: dict[str, Any]) -> str:
    """Markdown to paste on KAN-27 and hangar#151."""
    key = results.get("ticket")
    lines = [f"**Live intake check** on {results.get('url')}, {time.strftime('%Y-%m-%d %H:%M %Z')}: "
             f"{'PASS' if results.get('passed') else 'FAIL'}", "",
             f"* Ticket: {key}; job: `{results.get('job') or '-'}` ({results.get('state') or '-'}); "
             f"PR: {results.get('pr') or '-'}"]
    t = results.get("timings") or {}
    if t:
        lines.append(f"* Move to job: {t.get('move_to_job_seconds', '-')} s; job: "
                     f"{round(t['job_seconds'] / 60, 1) if 'job_seconds' in t else '-'} min"
                     + (f"; cost ${results['cost_usd']:.2f}" if results.get("cost_usd") is not None else ""))
    lines += ["", "| Check | Result |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in (results.get("checks") or {}).items()]
    if results.get("error"):
        lines += ["", f"Stopped: {results['error']}"]
    lines += ["", "Defects: file each as a hangar issue and list it here."]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=("preflight", "run"))
    p.add_argument("--ticket", required=True, help="the Jira issue key to send through intake, like KAN-123")
    p.add_argument("--start-timeout", type=float, default=600, help="seconds from the move to the job (600)")
    p.add_argument("--dup-wait", type=float, default=60,
                   help="seconds to wait after the second move before counting jobs (60)")
    p.add_argument("--job-timeout", type=float, default=5400, help="seconds for the job to finish (5400)")
    p.add_argument("--poll", type=float, default=5, help="seconds between looks (5)")
    p.add_argument("--yes", action="store_true", help="don't wait for Enter at each step (the moves are timed)")
    p.add_argument("--report", default="intake-live-report.json")
    p.add_argument("--summary", default="intake-live-summary.md")
    opts = p.parse_args(argv)
    opts.ticket = opts.ticket.strip().upper()
    try:
        url, token = target()
    except Fail as e:
        say(str(e))
        return NOT_READY
    api = Api(url, token)
    if opts.command == "preflight":
        problems, found = preflight(api, opts.ticket)
        say(json.dumps(found, indent=2))
        for x in problems:
            say(f"NOT READY: {x}")
        if not problems:
            say(f"READY: move {opts.ticket} when `run` asks")
        return NOT_READY if problems else 0
    results: dict[str, Any] = {"ticket": opts.ticket, "url": url, "started_at": time.time()}
    try:
        failures = run(api, opts, results)
    except Fail as e:
        results["error"] = str(e)
        failures = [str(e)]
    except KeyboardInterrupt:
        results["error"] = "interrupted"
        failures = ["interrupted"]
    results["passed"] = not failures
    Path(opts.report).write_text(json.dumps(results, indent=2) + "\n")
    Path(opts.summary).write_text(summary(results))
    say("\n" + summary(results))
    say(f"report: {opts.report}; summary for KAN-27 and hangar#151: {opts.summary}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
