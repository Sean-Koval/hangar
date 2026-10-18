"""The nightly environment determinism report (#179; docs/observability.md#the-determinism-report).

Each night, in the eval lane (#181), hangar strictly replays the previous day's recorded production turns of each repo
(replay.py, `replay-strict` with `tolerate: tool`): every taped answer is served again in a fresh replay sandbox and
every tool call re-executed, so a step whose output doesn't reproduce on an identical tree (a flaky test, a build that
needs the network, a command that reads the clock) shows up as a mismatch. It costs sandbox time only: no model call
leaves hangar.

**What runs.** Tapes (#177) whose turn finished on the previous UTC day, of a session's first run (the only ones a
fresh sandbox can rebuild), not of a trial, shadow or replay: up to HANGAR_DETERMINISM_RUNS_PER_REPO (50) per repo,
sampled deterministically, for at most HANGAR_DETERMINISM_MAX_REPOS repos (10, the busiest first). Each is a fork with
no continuation (forks.py, `purpose: determinism`) on HANGAR_DETERMINISM_SCHEDULE (21:00 in HANGAR_EVAL_TZ: the eval
window); `POST /api/determinism/run` (admins) starts one now.

**What it writes.** Once every replay has ended, per repo `v1/determinism/{date}/{repo}.json` in the outbox (and
`<data>/determinism/{date}/`): the steps whose outputs didn't reproduce, ranked by how often (`count`, the runs and
steps it happened in), each with its kind (`tool`, `network`; a `model` divergence means the harness or its system
prompt changed and the replay stopped there), the tool and its command or file, and excerpts of both outputs. A test
on the repo's flaky list (testresults.py) whose id appears in a mismatched output gets the step as `evidence` on its
flaky-list entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hangar import replay
from hangar.models import JobBudget, Session, now

if TYPE_CHECKING:
    from hangar.jobs import JobService

log = logging.getLogger("hangar.determinism")

SCHEMA = "hangar.determinism/v1"
RUN_KEY = "determinism:run"  # the night's run under way: {"date", "entries": [{repo, run, fork | error}]}
SLOT_KEY = "determinism:slot"
EVIDENCE_KEPT = 5


def _day(t: float | None) -> str:
    return datetime.fromtimestamp(t or 0, UTC).strftime("%Y-%m-%d")


def candidates(jobs: JobService, date: str) -> dict[str, list[str]]:
    """{repo: [run, ...]} for `date`'s recorded production turns, sampled and capped."""
    from hangar import experiments, shadows
    from hangar.repopolicy import repo_key

    by_repo: dict[str, list[str]] = {}
    root = Path(jobs.settings.data_dir) / "tapes"
    for idx_path in root.glob("*/tape/index.json") if root.exists() else []:
        try:
            idx = json.loads(idx_path.read_text())
        except (OSError, ValueError):
            continue
        if not idx.get("final") or not idx.get("entries") or _day(idx.get("finished_at")) != date:
            continue
        s = jobs.store.get(Session, idx.get("session_id") or "")
        if s is None or not s.repo or not idx.get("run_id"):
            continue
        if any(k in s.labels for k in (replay.REPLAY_LABEL, experiments.EXP_LABEL, shadows.SHADOW_OF)):
            continue
        first = next((t for t in s.turns if t.run_id), None)
        if first is None or first.run_id != idx["run_id"]:
            continue
        key = repo_key(s.repo) or s.repo
        by_repo.setdefault(key, []).append(idx["run"])
    cap, most = jobs.settings.determinism_runs_per_repo, jobs.settings.determinism_max_repos
    out = {}
    for repo, runs in sorted(by_repo.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:most]:
        out[repo] = sorted(runs, key=lambda r: hashlib.sha256(f"{date}\n{r}".encode()).hexdigest())[:cap]
    return out


def start(jobs: JobService, caller: Any = None, date: str | None = None) -> dict[str, Any]:
    """Start the replays for `date` (default: yesterday, UTC)."""
    from hangar import forks
    from hangar.jobs import Caller

    caller = caller or Caller("hangar:determinism", "admin")
    date = date or _day(now() - 86400)
    entries: list[dict[str, Any]] = []
    for repo, runs in candidates(jobs, date).items():
        for run in runs:
            try:
                n = json.loads((replay.tape_dir(jobs.settings, run) / "index.json").read_text())["entries"]
                req = forks.ForkRequest(at_seq=n, continuations=0,
                                        budget=JobBudget(max_cost_usd=jobs.settings.determinism_replay_budget_usd))
                job = forks.start(jobs, run, req, caller, purpose="determinism", tolerate="tool")
                entries.append({"repo": repo, "run": run, "fork": job.id})
            except Exception as e:  # noqa: BLE001 - one run that can't replay doesn't stop the others
                entries.append({"repo": repo, "run": run, "error": str(e)[:500]})
    run = {"date": date, "started_at": now(), "entries": entries}
    jobs.store.kv_set_json(RUN_KEY, run)
    jobs.fleet.audit(caller.requester, "determinism.run", None, date=date, runs=len(entries))
    return run


def _step_key(m: dict[str, Any]) -> tuple[str, str, str, str]:
    return (m.get("kind") or "", str(m.get("tool") or ""), str(m.get("command") or ""), str(m.get("file") or ""))


def report(jobs: JobService, run: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """{repo: its report} once every replay of the run has ended, else None."""
    from hangar import experiments
    from hangar.models import JOB_TERMINAL

    repos: dict[str, dict[str, Any]] = {}
    for e in run.get("entries") or []:
        rep = repos.setdefault(e["repo"], {"schema": SCHEMA, "date": run["date"], "repo": e["repo"], "replays": 0,
                                           "reproduced": 0, "diverged": 0, "unreplayed": 0, "errors": [],
                                           "steps": {}})
        if "fork" not in e:
            rep["errors"].append({"run": e["run"], "error": e.get("error")})
            continue
        exp = jobs._jobs.get(e["fork"])
        if exp is None:
            rep["errors"].append({"run": e["run"], "error": "its replay job is gone"})
            continue
        if exp.state not in JOB_TERMINAL:
            return None
        st = experiments.load(exp)
        for t in st.trials:
            try:
                p = replay.load_plan(jobs.settings, t.replay or "")
            except KeyError:
                rep["errors"].append({"run": e["run"], "error": t.reason or "the replay didn't start"})
                continue
            rep["replays"] += 1
            if p.get("status") == "unreplayed":
                rep["unreplayed"] += 1
                continue
            found = list(p.get("mismatches") or []) + ([p["divergence"]] if p.get("divergence") else [])
            if p.get("divergence"):
                rep["diverged"] += 1
            if not found:
                rep["reproduced"] += 1
            for m in found:
                k = _step_key(m)
                s = rep["steps"].setdefault(k, {"kind": k[0], "tool": k[1] or None, "command": k[2] or None,
                                               "file": k[3] or None, "count": 0, "evidence": []})
                s["count"] += 1
                s["evidence"].append({"run": p["run"], "seq": m.get("seq"), "replay": p["id"], "fork": e["fork"],
                                      **{x: m[x] for x in ("got", "want", "detail") if m.get(x)}})
    out = {}
    for repo, rep in repos.items():
        steps = sorted(rep["steps"].values(), key=lambda s: (-s["count"], s["command"] or s["file"] or ""))
        for s in steps:
            s["runs"] = len({x["run"] for x in s["evidence"]})
            s["evidence"] = s["evidence"][:10]
        out[repo] = {**rep, "steps": steps, "generated_at": now()}
    return out


def _path(repo: str) -> str:
    """The report's name under the date: the repo key (`owner/repo`), with anything else made path-safe."""
    return "/".join(part.replace("..", "_") or "_" for part in repo.replace("://", "/").split("/") if part) + ".json"


def write(jobs: JobService, rep: dict[str, Any]) -> Path:
    """`<data>/determinism/{date}/{repo}.json`, and `v1/determinism/{date}/{repo}.json` in the outbox (write-once)."""
    from hangar import outbox

    rel = _path(rep["repo"])
    p = Path(jobs.settings.data_dir) / "determinism" / rep["date"] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rep, indent=1, sort_keys=True))
    ob = outbox.of(jobs.fleet)
    if ob.enabled:
        ob._stage(f"determinism:{rep['date']}:{rep['repo']}", f"{outbox.PREFIX}/determinism/{rep['date']}/",
                  {rel: rep})
        ob._kick()
    link_flaky(jobs, rep, f"{outbox.PREFIX}/determinism/{rep['date']}/{rel}")
    return p


def link_flaky(jobs: JobService, rep: dict[str, Any], where: str) -> int:
    """Step-level evidence on the repo's flaky list: a listed test whose id shows in a mismatched step's output."""
    from hangar import testresults

    key = rep["repo"]
    data = testresults._flaky_data(jobs.store, key)
    if not data:
        return 0
    n = 0
    for tid, entry in data.items():
        short = tid.rsplit("::", 1)[-1]
        for s in rep["steps"]:
            for ev in s["evidence"]:
                text = f"{ev.get('got', '')}\n{ev.get('want', '')}"
                if tid in text or (len(short) >= 6 and short in text):
                    link = {"date": rep["date"], "run": ev["run"], "seq": ev["seq"], "replay": ev["replay"],
                            "command": s.get("command"), "report": where}
                    entry["evidence"] = [*[x for x in entry.get("evidence") or [] if x != link], link][-EVIDENCE_KEPT:]
                    n += 1
    if n:
        jobs.store.kv_set_json(testresults.KV_FLAKY + key, data)
    return n


def _cron(settings: Any) -> Any:
    from hangar.triggers import Cron

    expr = (settings.determinism_schedule or "").strip()
    if expr.lower() in ("", "off", "none"):
        return None
    return Cron(expr, settings.eval_tz or "UTC")


async def minutely(h: Any) -> None:
    """The ops loop's step: start the night's replays at the slot, write the reports once they have all ended."""
    jobs = getattr(h, "jobs", None)
    if jobs is None:
        return
    run = jobs.store.kv_get_json(RUN_KEY, None)
    if run and not run.get("written"):
        reps = report(jobs, run)
        if reps is not None:
            for rep in reps.values():
                write(jobs, rep)
            run["written"] = time.time()
            jobs.store.kv_set_json(RUN_KEY, run)
        return
    cron = _cron(jobs.settings)
    if cron is None:
        return
    slot = cron.latest_at_or_before(now())
    last = jobs.store.kv_get_json(SLOT_KEY, None)
    if slot is None or (last is not None and slot <= float(last)):
        return
    jobs.store.kv_set_json(SLOT_KEY, slot)
    if last is None:
        return  # the first look only remembers the slot: a deploy never runs it on the spot
    day = datetime.fromtimestamp(slot, UTC) - timedelta(days=1)
    start(jobs, date=day.strftime("%Y-%m-%d"))


def install() -> None:
    from hangar import opstatus

    if minutely not in opstatus.MINUTELY:
        opstatus.MINUTELY.append(minutely)


def mount(app: Any, h: Any, A: Any) -> None:
    """POST /api/determinism/run (admins): the replays for a day now (default yesterday)."""
    from fastapi import Body

    install()

    @app.post("/api/determinism/run")
    async def determinism_run(body: dict[str, Any] = Body(default_factory=dict), who: str = A):
        from hangar.jobs import Caller

        date = body.get("date")
        if date is not None:
            datetime.strptime(str(date), "%Y-%m-%d")
        return start(h.jobs, Caller(who, "admin"), date)
