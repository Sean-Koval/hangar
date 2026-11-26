"""KAN-218: the capacity rehearsal. 60 concurrent sessions from 6 synthetic people on a stub model, on the cluster.

    python scripts/acceptance/capacity.py run [--people 6] [--sessions 10] [--turns 3] [--report FILE]
    python scripts/acceptance/capacity.py env      # print the stub environment it would save, and change nothing
    python scripts/acceptance/capacity.py cleanup  # remove what an interrupted run left (from --state)

`docs/team-scale/rehearsal.md` is the runbook. What it does, in order:

1. **The stub environment** (`<prefix>-stub`, saved only when it differs): `base` with Claude Code, and the stub
   Messages API (`scripts/bench/stub_model.py`) as a terminal on 127.0.0.1 inside each sandbox. `ANTHROPIC_BASE_URL`
   points the real CLI at it. A turn thinks for --think seconds, runs --work through Bash, thinks again and ends. No
   request leaves the pod and no model is paid for. `--env NAME` uses an existing environment instead, such as one
   on the cheapest real model.
2. **People.** --people members `accept-<run>-pN`, each with an MCP token. Their caps are the install's own.
3. **Boots.** Every session at once, over MCP as engineers start them. Recorded: new_session to idle, p50 and p95,
   split into warm claims and cold creates, and how many were warm.
4. **The cap.** One session past HANGAR_PERSON_MAX_SESSIONS for the first person must be refused.
5. **Turns.** Every session runs --turns turns at once: turn time p50 and p95, and any turn that failed.
6. **Fairness.** --jobs jobs on the stub: the first person submits their running-jobs cap plus two, the others one
   each. Nobody runs more than the cap at once, and nobody waits behind the first person's batch.
7. **Nodes.** All along, with kubectl: the sandbox pool's node count and Ready count, every --sample seconds. The
   report has the count at the start and at its peak, and the time from the first boot to the first new node, to
   the peak and to every session idle.
8. **The power-down.** With --watch-power, the sessions stay up and keep turning until the pools go down. The
   report records what each session and job did as `down` (or `night`) came, and when.

Everything is released at the end, and after a failure or Ctrl-C, unless --keep. The JSON report and a Markdown
summary for KAN-218 are written either way. It needs an admin token ($HANGAR_URL and $HANGAR_TOKEN, or `hangar
login`), and kubectl for step 7 (--no-kubectl leaves it out). Exit status: 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import hangar_accept as ha  # noqa: E402
import team_checks as tc  # noqa: E402
from hangar_accept import Api, Fail, State, pct, say, target  # noqa: E402

ROOT = Path(__file__).parents[2]
STUB = ROOT / "scripts" / "bench" / "stub_model.py"
STUB_PATH = "/opt/hangar-stub/stub_model.py"
STUB_PORT = 18080
DESCRIPTION = "hangar capacity rehearsal (scripts/acceptance/capacity.py, KAN-218): a stub model, no spend; kept"
POOL_LABEL = "cloud.google.com/gke-nodepool"
TERMINAL_JOB = ("succeeded", "failed", "cancelled")


# ---------------------------------------------------------------------------- the stub environment
def stub_spec(opts: argparse.Namespace) -> dict[str, Any]:
    """The rehearsal's environment: `base` + Claude Code, pointed at the stub on loopback (NO_PROXY covers it)."""
    return {
        "description": DESCRIPTION,
        "extends": opts.extends,
        "harnesses": ["claude-code"],
        "files": {STUB_PATH: STUB.read_text()},
        "env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{STUB_PORT}",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"},
        "terminals": [{"name": "stub-model", "command": f"exec python3 {STUB_PATH}", "port": STUB_PORT,
                       "env": {"STUB_PORT": str(STUB_PORT), "STUB_THINK_SECONDS": str(opts.think),
                               "STUB_WORK": opts.work}}],
        "approvals": "auto",
        "warm_pool": opts.warm,
    }


def ensure_env(api: Api, opts: argparse.Namespace) -> str:
    """The stub environment, saved only when it doesn't hold the spec already; never over someone else's."""
    if opts.env:
        return str(opts.env)
    name = f"{opts.env_prefix}-stub"
    want = stub_spec(opts)
    r = api.http.get(f"/api/environments/{name}")
    if r.status_code in (401, 403):
        raise Fail(f"GET /api/environments/{name}: HTTP {r.status_code}: the rehearsal needs an admin token")
    raw = (r.json().get("raw") or {}) if r.status_code == 200 else {}
    if r.status_code == 200 and not str(raw.get("description") or "").startswith(DESCRIPTION[:30]):
        raise Fail(f"an environment named {name} exists and isn't the rehearsal's: pick another --env-prefix")
    if r.status_code != 200 or any(raw.get(k) != v for k, v in want.items()):
        api.call("PUT", f"/api/environments/{name}", json=want)
        say(f"environment {name}: {'updated' if r.status_code == 200 else 'created'}")
    return name


# ---------------------------------------------------------------------------- nodes
def node_counts(nodes: dict[str, Any], pool: str) -> tuple[int, int]:
    """(nodes, Ready nodes) of the pool in `kubectl get nodes -o json`."""
    total = ready = 0
    for n in nodes.get("items") or []:
        if ((n.get("metadata") or {}).get("labels") or {}).get(POOL_LABEL) != pool:
            continue
        total += 1
        conds = (n.get("status") or {}).get("conditions") or []
        ready += any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds)
    return total, ready


class Sampler(threading.Thread):
    """The pool's node count and hangar's sandbox count every `every` seconds, into `samples`."""

    def __init__(self, api: Api, opts: argparse.Namespace, t0: float):
        super().__init__(daemon=True)
        self.api, self.opts, self.t0 = api, opts, t0
        self.samples: list[dict[str, Any]] = []
        self.stop = threading.Event()

    def nodes(self) -> tuple[int, int] | None:
        if not self.opts.kubectl:
            return None
        try:
            out = subprocess.run(["kubectl", "get", "nodes", "-l", f"{POOL_LABEL}={self.opts.pool}", "-o", "json"],
                                 capture_output=True, text=True, timeout=60, check=True).stdout
            return node_counts(json.loads(out), self.opts.pool)
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            say(f"kubectl: {e}")
            return None

    def sample(self) -> dict[str, Any]:
        s: dict[str, Any] = {"t": round(time.monotonic() - self.t0, 1)}
        if (n := self.nodes()) is not None:
            s["nodes"], s["ready"] = n
        with contextlib.suppress(Exception):
            sbs = self.api.call("GET", "/api/sandboxes")
            s["sandboxes"] = {st: sum(1 for b in sbs if b.get("state") == st)
                              for st in ("starting", "running", "pending")}
        with contextlib.suppress(Exception):
            s["power"] = (self.api.call("GET", "/api/admin/power").get("power") or {}).get("state")
        self.samples.append(s)
        return s

    def run(self) -> None:
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(self.opts.sample)


def scaling(samples: list[dict[str, Any]], all_idle_at: float | None) -> dict[str, Any]:
    """Node numbers from the samples: start, peak, and seconds to the first new node and to the peak."""
    counted = [s for s in samples if "nodes" in s]
    if not counted:
        return {"nodes": "not measured (no kubectl)"}
    start, peak = counted[0]["nodes"], max(s["nodes"] for s in counted)
    first_new = next((s["t"] for s in counted if s["nodes"] > start), None)
    at_peak = next(s["t"] for s in counted if s["nodes"] == peak)
    return {"nodes_start": start, "nodes_peak": peak, "ready_peak": max(s.get("ready", 0) for s in counted),
            "first_new_node_s": first_new, "peak_s": at_peak if peak > start else None, "all_idle_s": all_idle_at}


# ---------------------------------------------------------------------------- turns
async def _turns(url: str, token: str, names: list[str], n: int, timeout: float) -> list[Any]:
    """`n` turns on each of a person's sessions, all at once, over one MCP client: each turn's seconds, or what
    went wrong."""
    http, client = tc._mcp_client(url, token)

    def finished(s: dict[str, Any]) -> int:
        return sum(1 for t in s.get("turns") or [] if t.get("finished_at"))

    async def one(c: Any, name: str) -> list[Any]:
        out: list[Any] = []
        for k in range(n):
            t = time.monotonic()
            try:
                before = finished(tc._sc(await c.call_tool("get_session", {"session": name})) or {})
                res = await c.call_tool("send_to_session", {"session": name, "message": f"Rehearsal turn {k + 1}."})
                if res.is_error:
                    raise Fail(" ".join(getattr(b, "text", "") for b in res.content)[:200])
                while True:
                    await asyncio.sleep(ha.POLL_SECONDS)
                    s = tc._sc(await c.call_tool("get_session", {"session": name})) or {}
                    if finished(s) > before and not s.get("busy"):
                        break
                    if s.get("state") in ("failed", "archived"):
                        raise Fail(f"session ended {s.get('state')}: {s.get('reason')}")
                    if time.monotonic() - t > timeout:
                        raise Fail(f"turn {k + 1} still running after {timeout:.0f}s")
                out.append(time.monotonic() - t)
            except Exception as e:  # noqa: BLE001 - the report counts it; the other turns go on
                out.append(Fail(f"{name} turn {k + 1}: {tc.leaf(e)}"))
        return out

    async with http, client as c:
        per = await asyncio.gather(*(one(c, name) for name in names))
    return [x for xs in per for x in xs]


async def _all_turns(url: str, plan: dict[str, tuple[str, list[str]]], n: int, timeout: float) -> list[Any]:
    per = await asyncio.gather(*(_turns(url, tok, names, n, timeout) for tok, names in plan.values()))
    return [x for xs in per for x in xs]


# ---------------------------------------------------------------------------- power-down
def watch_power(api: Api, state: State, plan: dict[str, tuple[str, list[str]]], opts: argparse.Namespace,
                results: dict[str, Any]) -> None:
    """Keep the sessions turning until the pools go down, then record what happened to sessions and jobs."""
    say(f"--watch-power: waiting up to {opts.watch_power / 60:.0f} min for the pools to go down")
    deadline = time.monotonic() + opts.watch_power
    names = [n for _, ns in plan.values() for n in ns]
    while time.monotonic() < deadline:
        power = (api.call("GET", "/api/admin/power").get("power") or {}).get("state")
        if power in ("down", "night"):
            break
        asyncio.run(_all_turns(api.url, plan, 1, opts.turn_timeout))
    else:
        results["power_down"] = "not seen: the pools stayed up for --watch-power"
        return
    went = time.monotonic()
    seen: dict[str, list[str]] = {}
    end = went + 600
    while time.monotonic() < end:
        for name in names:
            r = api.http.get(f"/api/sessions/{name}")
            st = r.json().get("state") if r.status_code == 200 else "gone"
            if not seen.get(name) or seen[name][-1] != st:
                seen.setdefault(name, []).append(st)
        if all(v[-1] in ("suspended", "gone", "failed") for v in seen.values()):
            break
        time.sleep(10)
    jobs = [api.call("GET", f"/api/jobs/{j}") for j in state.get("jobs", [])]
    results["power_down"] = {
        "after_s": round(time.monotonic() - went, 1),
        "sessions": {k: sum(1 for v in seen.values() if v[-1] == k) for k in {v[-1] for v in seen.values()}},
        "session_paths": sorted({" -> ".join(v) for v in seen.values()}),
        "jobs": {j["id"]: f"{j['state']}" + (f" ({j['reason']})" if j.get("reason") else "") for j in jobs},
    }


# ---------------------------------------------------------------------------- fairness
def fair_jobs(api: Api, state: State, opts: argparse.Namespace, tokens: dict[str, str], env: str) -> str:
    """team_checks' fairness batch on the stub, on --job-repo: the first person submits their running-jobs cap plus
    two, then each other person one. Nobody runs more than the cap at once, and nobody waits behind the batch."""
    people = list(tokens)
    cap = int(tc.setting(api, "HANGAR_PERSON_MAX_RUNNING_JOBS") or 0) or opts.job_cap
    body = {"instructions": "A hangar capacity rehearsal job. Reply with exactly: DONE", "environment": env,
            "repo": opts.job_repo, "deliver": "none", "job_budget": tc.paid(opts),
            "labels": {ha.LABEL: state.run}, "timeout_minutes": 20}
    clients = {p: Api(api.url, tokens[p]) for p in people}
    ids = []
    try:
        for i in range(cap + 2):
            j = clients[people[0]].call("POST", "/api/jobs", json={**body, "title": f"capacity rehearsal {i}"})
            state.add("jobs", j["id"])
            ids.append(j["id"])
        for p in people[1:]:
            j = clients[p].call("POST", "/api/jobs", json={**body, "title": f"capacity rehearsal {p}"})
            state.add("jobs", j["id"])
            ids.append(j["id"])
    finally:
        for c in clients.values():
            c.http.close()
    jobs = [tc.wait_job(api, i, opts.turn_timeout) for i in ids]
    if problems := tc.job_intervals_ok(jobs, cap):
        raise Fail("; ".join(problems))
    ended = {j["state"] for j in jobs}
    return f"{len(ids)} jobs ({', '.join(sorted(ended))}): nobody ran more than {cap} at once or waited behind a batch"


# ---------------------------------------------------------------------------- the run
def people(api: Api, state: State, n: int) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for i in range(n):
        p = f"accept-{state.run}-p{i + 1}"
        state.add("people", p)
        api.call("POST", "/api/people", json={"id": p, "name": f"capacity rehearsal {p}", "role": "member",
                                              "token": False})
        tok, _ = api.scopes_token(state, f"p{i + 1}", ["mcp", "jobs"], person=p)
        tokens[p] = tok
    return tokens


def preflight(api: Api, opts: argparse.Namespace) -> list[str]:
    problems = []
    want = opts.people * opts.sessions
    limit = int(tc.setting(api, "HANGAR_MAX_SANDBOXES") or 0)
    if limit and want + opts.jobs + 1 > limit:
        problems.append(f"HANGAR_MAX_SANDBOXES is {limit}: {want} sessions and {opts.jobs} jobs don't fit")
    live = len(api.call("GET", "/api/sandboxes"))
    if live and not opts.allow_busy:
        problems.append(f"{live} sandboxes are live: run the rehearsal when nobody else is working, or the numbers "
                        "measure their work too (--allow-busy to go on)")
    power = (api.call("GET", "/api/admin/power").get("power") or {}).get("state")
    if power not in (None, "up"):
        problems.append(f"the sandbox pools are {power}: power them up first (hangar power up)")
    if opts.jobs and opts.job_repo:
        repos = tc.setting(api, "HANGAR_GITHUB_REPOS") or {}
        mapped = repos.get(opts.job_repo) or repos.get(tc._repo_of(opts.job_repo)) or ""
        env = opts.env or f"{opts.env_prefix}-stub"
        if env not in [e.strip() for e in str(mapped).split(",")]:
            problems.append(f"--job-repo {opts.job_repo} must list {env} in HANGAR_GITHUB_REPOS (it has "
                            f"{mapped or 'no entry'}): members' jobs run only in a repo's mapped environments")
    if opts.kubectl and not shutil.which("kubectl"):
        problems.append("no kubectl: install it and get the cluster's credentials, or pass --no-kubectl")
    return problems


def run(api: Api, state: State, opts: argparse.Namespace, results: dict[str, Any]) -> list[str]:
    checks: dict[str, str] = results.setdefault("checks", {})
    if problems := preflight(api, opts):
        raise Fail("not ready: " + "; ".join(problems))
    env = ensure_env(api, opts)
    results["environment"] = env
    cap = int(tc.setting(api, "HANGAR_PERSON_MAX_SESSIONS") or 0)
    results["caps"] = {"sessions_per_person": cap or None,
                       "running_jobs_per_person": int(tc.setting(api, "HANGAR_PERSON_MAX_RUNNING_JOBS") or 0) or None}
    if opts.warm_wait:
        say(f"waiting {opts.warm_wait:.0f}s for {env}'s image and warm pool")
        time.sleep(opts.warm_wait)
    tokens = people(api, state, opts.people)
    names = {p: [f"accept-{state.run}-p{i + 1}-s{k:02d}" for k in range(opts.sessions)]
             for i, p in enumerate(tokens)}
    for ns in names.values():
        for n in ns:
            state.add("sessions", n)
    t0 = time.monotonic()
    sampler = Sampler(api, opts, t0)
    sampler.sample()
    sampler.start()
    all_idle: float | None = None
    try:
        say(f"{opts.people * opts.sessions} sessions on {env} for {opts.people} people, all at once")
        plan = [(tokens[p], n) for p, ns in names.items() for n in ns]
        booted = asyncio.run(tc._boot_all(api.url, plan, env, opts.boot_timeout))
        all_idle = round(time.monotonic() - t0, 1)
        ok = [(n, b) for (_, n), b in zip(plan, booted, strict=True) if not isinstance(b, BaseException)]
        bad = [(n, b) for (_, n), b in zip(plan, booted, strict=True) if isinstance(b, BaseException)]
        warm, cold = [], []
        for _name, (secs, sid) in ok:
            info = (api.http.get(f"/api/sandboxes/{sid}").json() or {}).get("info") or {}
            (warm if info.get("warm") else cold).append(secs)
        times = [s for s, _ in (b for _, b in ok)]
        results["boots"] = {"up": len(ok), "failed": len(bad), "warm": len(warm), "cold": len(cold),
                            **({"p50": round(pct(times, 50), 1), "p95": round(pct(times, 95), 1),
                                "max": round(max(times), 1)} if times else {}),
                            **({"warm_p50": round(pct(warm, 50), 1), "warm_p95": round(pct(warm, 95), 1)}
                               if warm else {}),
                            **({"cold_p50": round(pct(cold, 50), 1), "cold_p95": round(pct(cold, 95), 1)}
                               if cold else {}),
                            "errors": [f"{n}: {tc.leaf(b)}"[:200] for n, b in bad[:5]]}
        checks["boots"] = "pass" if not bad else f"fail: {len(bad)} of {len(plan)} sessions didn't start"
        say(f"boots: {results['boots']}")

        first = next(iter(tokens))
        if cap and cap == opts.sessions:
            extra = f"accept-{state.run}-over"
            state.add("sessions", extra)
            err, text, _ = asyncio.run(tc._mcp_call(api.url, tokens[first], "new_session",
                                                    {"name": extra, "environment": env, "idle_minutes": 0}))
            checks["session_cap"] = "pass" if err and "limit per person" in text else \
                f"fail: {first}'s session {cap + 1} wasn't refused: {text[:160]}"
        else:
            checks["session_cap"] = (f"not checked: the cap is {cap or 'off'}, not --sessions {opts.sessions}; "
                                     "set HANGAR_PERSON_MAX_SESSIONS to match for the rehearsal")

        live = {p: (tokens[p], [n for n in ns if n in dict(ok)]) for p, ns in names.items()}
        if opts.turns:
            say(f"{opts.turns} turns on each of {len(ok)} sessions, all at once")
            t = time.monotonic()
            done = asyncio.run(_all_turns(api.url, live, opts.turns, opts.turn_timeout))
            secs = [x for x in done if not isinstance(x, BaseException)]
            errs = [str(x) for x in done if isinstance(x, BaseException)]
            results["turns"] = {"ran": len(secs), "failed": len(errs), "wall_s": round(time.monotonic() - t, 1),
                                **({"p50": round(pct(secs, 50), 1), "p95": round(pct(secs, 95), 1)} if secs else {}),
                                "errors": errs[:5]}
            checks["turns"] = "pass" if not errs else f"fail: {len(errs)} of {len(done)} turns failed"

        if opts.jobs and not opts.job_repo:
            checks["fairness"] = ("not checked: members submit jobs only on a repo in HANGAR_GITHUB_REPOS; pass "
                                  "--job-repo (the acceptance test repo; the jobs deliver nothing)")
        elif opts.jobs:
            try:
                checks["fairness"] = "pass: " + fair_jobs(api, state, opts, tokens, env)
            except Fail as e:
                checks["fairness"] = f"fail: {e}"

        if opts.watch_power:
            watch_power(api, state, live, opts, results)
    finally:
        sampler.stop.set()
        sampler.join(timeout=opts.sample + 70)
        results["nodes"] = scaling(sampler.samples, all_idle)
        results["samples"] = sampler.samples
    return [f"{k}: {v.removeprefix('fail: ')}" for k, v in checks.items() if v.startswith("fail")]


def release(api: Api, state: State, opts: argparse.Namespace) -> list[str]:
    """Everything the run made: jobs cancelled, sessions released, tokens and people removed."""
    left: list[str] = []
    for jid in state.get("jobs", []):
        r = api.http.get(f"/api/jobs/{jid}")
        if r.status_code == 200 and r.json()["state"] not in TERMINAL_JOB:
            with contextlib.suppress(Fail):
                api.call("POST", f"/api/jobs/{jid}/cancel")
    left += tc._release_sessions(api, state.get("sessions", []), opts.boot_timeout)
    for tid in state.get("tokens", []):
        if api.http.delete(f"/api/tokens/{tid}").status_code not in (200, 204, 404):
            left.append(f"token {tid}")
    for p in state.get("people", []):
        if api.http.delete(f"/api/people/{p}").status_code not in (200, 404):
            left.append(f"person {p}")
    return left


def summary(results: dict[str, Any]) -> str:
    """Markdown for KAN-218 and docs/team-scale/rehearsal.md's results table."""
    b, n, t = results.get("boots") or {}, results.get("nodes") or {}, results.get("turns") or {}
    lines = [f"**Capacity rehearsal** on {results.get('url')}, {time.strftime('%Y-%m-%d %H:%M %Z')}: "
             f"{'PASS' if results.get('passed') else 'FAIL'}", "",
             f"* {results.get('people')} people x {results.get('sessions')} sessions on `{results.get('environment')}`"
             f" (caps: {results.get('caps')})",
             f"* Boots: {b.get('up', 0)} up, {b.get('failed', 0)} failed; p50 {b.get('p50', '-')} s, p95 "
             f"{b.get('p95', '-')} s; warm {b.get('warm', 0)} (p50 {b.get('warm_p50', '-')} s, p95 "
             f"{b.get('warm_p95', '-')} s), cold {b.get('cold', 0)} (p50 {b.get('cold_p50', '-')} s, p95 "
             f"{b.get('cold_p95', '-')} s)",
             f"* Nodes: {n.get('nodes_start', n.get('nodes', '-'))} at the start, {n.get('nodes_peak', '-')} at the "
             f"peak; first new node at {n.get('first_new_node_s', '-')} s, peak at {n.get('peak_s', '-')} s, every "
             f"session idle at {n.get('all_idle_s', '-')} s"]
    if t:
        lines.append(f"* Turns: {t.get('ran')} ran, {t.get('failed')} failed; p50 {t.get('p50', '-')} s, p95 "
                     f"{t.get('p95', '-')} s, {t.get('wall_s')} s in all")
    if pd := results.get("power_down"):
        lines.append(f"* Power-down: {json.dumps(pd)}")
    lines += ["", "| Check | Result |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in (results.get("checks") or {}).items()]
    if results.get("error"):
        lines += ["", f"Stopped: {results['error']}"]
    if results.get("left"):
        lines += ["", f"Left behind: {', '.join(results['left'])}"]
    return "\n".join(lines) + "\n"


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("command", choices=("run", "env", "cleanup"))
    p.add_argument("--people", type=int, default=6)
    p.add_argument("--sessions", type=int, default=10, help="sessions per person (10)")
    p.add_argument("--turns", type=int, default=3, help="turns per session, all at once (3; 0: boots only)")
    p.add_argument("--jobs", type=int, default=1, help="1: run the fairness batch; 0: skip it")
    p.add_argument("--job-repo", default=os.environ.get("HANGAR_ACCEPT_TEST_REPO"),
                   help="repo the fairness jobs name, in HANGAR_GITHUB_REPOS (default: $HANGAR_ACCEPT_TEST_REPO)")
    p.add_argument("--job-cap", type=int, default=4,
                   help="the running-jobs cap when HANGAR_PERSON_MAX_RUNNING_JOBS is 0: half the job slots, at "
                        "least 4 (4)")
    p.add_argument("--max-usd", type=float, default=0.5, help="each job's budget; a stub job spends nothing (0.5)")
    p.add_argument("--env", help="an existing environment instead of the stub (a cheap real model, say)")
    p.add_argument("--env-prefix", default="capacity", help="the stub environment is <prefix>-stub (capacity)")
    p.add_argument("--extends", default="base", help="what the stub environment extends (base)")
    p.add_argument("--think", type=float, default=3.0, help="the stub's seconds per model answer (3)")
    p.add_argument("--work", default="sleep 20", help="the shell the stub has the agent run each turn (sleep 20)")
    p.add_argument("--warm", type=int, default=10, help="the stub environment's warm pool (10)")
    p.add_argument("--warm-wait", type=float, default=0, help="seconds to let the warm pool fill before booting")
    p.add_argument("--boot-timeout", type=float, default=1200)
    p.add_argument("--turn-timeout", type=float, default=600)
    p.add_argument("--pool", default="sandboxes-gvisor", help="the node pool kubectl counts (sandboxes-gvisor)")
    p.add_argument("--sample", type=float, default=15, help="seconds between node samples (15)")
    p.add_argument("--no-kubectl", dest="kubectl", action="store_false")
    p.add_argument("--allow-busy", action="store_true", help="go on with other sandboxes live")
    p.add_argument("--watch-power", type=float, default=0,
                   help="seconds to keep turning and wait for the pools to go down (start before 01:00 ET)")
    p.add_argument("--keep", action="store_true", help="leave the sessions and people for a look")
    p.add_argument("--state", default="capacity-state.json")
    p.add_argument("--report", default="capacity-report.json")
    p.add_argument("--summary", default="capacity-summary.md")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_args(p)
    opts = p.parse_args(argv)
    if opts.command == "env":
        print(json.dumps(stub_spec(opts), indent=2))
        return 0
    url, token = target()
    api = Api(url, token)
    state = State(Path(opts.state))
    if opts.command == "cleanup":
        left = release(api, state, opts)
        say("left behind: " + ", ".join(left) if left else "nothing left behind")
        return 1 if left else 0
    results: dict[str, Any] = {"url": url, "run": state.run, "people": opts.people, "sessions": opts.sessions,
                               "started_at": time.time()}
    try:
        failures = run(api, state, opts, results)
    except (Fail, KeyboardInterrupt) as e:
        results["error"] = str(e) or "interrupted"
        failures = [results["error"]]
    finally:
        if not opts.keep:
            results["left"] = release(api, state, opts)
    results["passed"] = not failures and not results.get("left")
    Path(opts.report).write_text(json.dumps(results, indent=2) + "\n")
    Path(opts.summary).write_text(summary(results))
    print("\n" + summary(results))
    say(f"report: {opts.report}; summary for KAN-218: {opts.summary}")
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
