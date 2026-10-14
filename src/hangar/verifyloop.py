"""The verify gate inside the agent's turn (spec 0012 item 5, --- overnight/autofix).

Hangar used to run a job's `verify` only after the agent stopped: a failure meant a draft PR. Now the agent sees the
failure before it stops, and fixes it in the same conversation:

* Claude Code: a Stop hook that runs the check and blocks the end of the turn with its output (exit 2). It is passed
  inline with `--settings` (HANGAR_CLAUDE_SETTINGS), so no file in the repo or in $HOME carries it, and it sets
  `disableAllHooks: false`, which outranks the user, project and local settings files.
* Codex in a live turn: hangar's driver runs the check when the turn completes and steers the failure back into the
  conversation (HANGAR_VERIFY_GATE, read once at start).
* One-shot harnesses (pi, agy, codex without python3): hangar starts a follow-up turn with the failure.

Each failure sent back is a round; a turn and the follow-ups it needs share `max_verify_rounds`, the turn's time
limit and its budget. Both in-turn gates record their rounds in `~/.hangar/runs/<run>.verify`, which hangar reads
after the turn. Both are advisory: they run in the sandbox as the agent's user, which can get past them (managed
settings with the image's sudo, the rounds file, a command of its own first on PATH). So nothing they report feeds
delivery: hangar's own check after the turn decides it, and a diff that edits the check's own tests is flagged and
delivered as a draft.
"""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

from hangar import baseline  # --- frontdoor
from hangar.events import total_usage
from hangar.harnesses import HARNESSES
from hangar.models import Budget, Job, Turn, now

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.jobs import JobService

CLAUDE_ENV = "HANGAR_CLAUDE_SETTINGS"  # read by the claude-code harness's run commands (`--settings`)
GATE_ENV = "HANGAR_VERIFY_GATE"  # read by driver.py
TAIL = 6000  # characters of the check's output the agent is shown


def max_rounds(settings: Settings, job: Job) -> int:
    if not job.verify:
        return 0
    on = settings.verify_in_loop if job.verify_in_loop is None else job.verify_in_loop
    if not on:
        return 0
    return settings.max_verify_rounds if job.max_verify_rounds is None else job.max_verify_rounds


def chain(job: Job) -> list[Turn]:
    """The latest turn someone asked for and the verify-fix turns hangar added after it: one unit of work."""
    out: list[Turn] = []
    for t in reversed(job.turns):
        out.append(t)
        if t.trigger != "verify":
            break
    return out[::-1]


def rounds_used(job: Job) -> int:
    """Failures already sent back in this unit: each in-turn round, and each verify-fix turn."""
    return sum(t.verify_rounds + (t.trigger == "verify") for t in chain(job))


def remaining_budget(job: Job) -> tuple[Budget | None, str | None]:
    """What the unit's budget has left for one more turn, or (None, why) when it is spent. A job's budget is per
    turn; verify-fix turns finish that turn's work, so they share it instead of each getting a new one."""
    b = job.budget
    if b is None:
        return None, None
    turns = chain(job)
    vals: dict[str, float | int] = {}
    if b.max_minutes:
        left = b.max_minutes - (now() - (turns[0].started_at or now())) / 60
        if left < 1:
            return None, f"the turn's {b.max_minutes} min are used up"
        vals["max_minutes"] = int(left)
    u = total_usage(turns)
    if b.max_tokens:
        left_tokens = b.max_tokens - (u.tokens if u else 0)
        if left_tokens <= 0:
            return None, f"the turn's {b.max_tokens:,} tokens are used up"
        vals["max_tokens"] = left_tokens
    if b.max_cost_usd:
        spent = u.cost_usd if u and u.cost_usd is not None else 0.0
        if b.max_cost_usd - spent <= 0.005:
            return None, f"the turn's ${b.max_cost_usd:.2f} are spent"
        vals["max_cost_usd"] = round(b.max_cost_usd - spent, 4)
    return Budget(**vals), None


def deadline(settings: Settings, job: Job, timeout_minutes: int, budget: Budget | None) -> int:
    """When the in-turn gate stops sending failures back: with room left in the turn's limit for one more check
    and a fix, so the gate itself doesn't run the turn into its timeout."""
    limit = timeout_minutes * 60
    if budget and budget.max_minutes:
        limit = min(limit, budget.max_minutes * 60)
    return int(now() + limit - min(settings.job_verify_timeout_seconds, limit // 4))


def stop_hook(verify: str, cwd: str, rounds: int, until: int) -> str:
    """The Stop hook's command (bash, as Claude Code runs command hooks). Exit 2 with the output on stderr blocks
    the stop and hands Claude the output; exit 0 lets it stop. Verified against the hooks reference for 2.1.x:
    exit 2 on Stop "sends Claude back to keep working" with stderr as the reason."""
    q = shlex.quote
    intro = ("hangar: the job's check `%s` failed (exit %s), round %s of %s. Fix the cause and run the check "
             "again before you finish. Don't change the check or the tests it runs to make it pass; if the failure "
             "is outside this task, say so in RESULT.md. The check's output ends with:\n")
    return (
        'f="$HOME/.hangar/runs/${HANGAR_RUN_ID:-run}.verify"; '
        'n=$(grep -c "^fail" "$f" 2>/dev/null); n=${n:-0}; '
        f'[ "$n" -ge {int(rounds)} ] && exit 0; '
        f'[ "$(date +%s)" -ge {int(until)} ] && exit 0; '
        f'cd {q(cwd)} 2>/dev/null || exit 0; '
        f'out=$(bash -c {q(verify)} 2>&1 </dev/null); rc=$?; '
        'if [ "$rc" -eq 0 ]; then echo pass >> "$f"; exit 0; fi; '
        'echo "fail $rc" >> "$f"; n=$((n + 1)); '
        f'printf {q(intro)} {q(verify)} "$rc" "$n" {int(rounds)} >&2; '
        f'printf "%s\\n" "$out" | tail -c {TAIL} >&2; '
        'exit 2'
    )


def claude_settings(verify: str, cwd: str, rounds: int, until: int, timeout: int) -> str:
    hook = {"type": "command", "command": stop_hook(verify, cwd, rounds, until), "timeout": int(timeout) + 30,
            "statusMessage": "hangar: running the job's check"}
    return json.dumps({"disableAllHooks": False, "hooks": {"Stop": [{"hooks": [hook]}]}})


def turn_env(settings: Settings, job: Job, harness: str | None, repo_path: str | None, timeout_minutes: int,
             budget: Budget | None) -> dict[str, str]:
    """The env that turns the gate on for this turn's harness ({} when it has none, or no rounds are left)."""
    rounds = max_rounds(settings, job) - rounds_used(job)
    h = HARNESSES.get(harness or "")
    if rounds <= 0 or not repo_path or h is None or not job.verify:
        return {}
    if baseline.red(job):  # --- frontdoor: the gate can't tell the base's failures from new ones; hangar's check can
        return {}
    until = deadline(settings, job, timeout_minutes, budget)
    timeout = settings.job_verify_timeout_seconds
    if h.name == "claude-code" or h.live == "claude":
        return {CLAUDE_ENV: claude_settings(job.verify, repo_path, rounds, until, timeout)}
    if h.live:  # the driver runs it when the turn completes (when the turn falls back to one-shot, nobody does)
        return {GATE_ENV: json.dumps({"command": job.verify, "cwd": repo_path, "rounds": rounds, "deadline": until,
                                      "timeout": timeout})}
    return {}


async def read_rounds(jobs: JobService, job: Job, turn: Turn) -> int:
    """How often the in-turn gate sent a failure back, from the file both gates append to ("fail <exit>" or
    "pass" per check). The agent can write it too; it can only spend its own rounds that way."""
    if not turn.run_id or not job.sandbox_id:
        return 0
    path = f"/home/{jobs.settings.sandbox_user}/.hangar/runs/{turn.run_id}.verify"
    try:
        raw = await jobs.fleet.read_file(job.sandbox_id, path, limit=64_000)
    except Exception:  # noqa: BLE001 - the sandbox went away; the check after the turn reports what matters
        return 0
    lines = (raw or b"").decode(errors="replace").splitlines()
    return min(sum(1 for ln in lines if ln.startswith("fail")), max_rounds(jobs.settings, job))


def fix_message(job: Job, exit_code: int | None, tail: str | None, n: int, of: int) -> str:
    out = (tail or "").strip()[-TAIL:] or "(no output)"
    new = ""  # --- frontdoor: on a red base, only what the change added came back
    if job.result.new_failures:
        new = ("It already failed on the base commit; these failures are new: "
               + ", ".join(job.result.new_failures[:20]) + ". Fix those; leave the base's.\n\n")
    return (f"hangar ran the job's check after your turn and it failed (exit {exit_code}), round {n} of {of}:\n\n"
            f"    {job.verify}\n\n{new}Its output ends with:\n\n```\n{out}\n```\n\n"
            "Fix the cause and run the check again before you finish. Don't change the check or the tests it runs "
            "to make it pass; if the failure is outside this task, say so in RESULT.md.")


def fix_turn(jobs: JobService, job: Job, turn: Turn, outcome: str) -> Turn | None:
    """The follow-up turn that hands a failing check back to the agent, or None (and why, as a job event) when
    the agent didn't finish cleanly, the rounds are used up or the budget is spent."""
    if outcome != "exited" or turn.exit_code != 0 or job.cancel_requested:
        return None  # it stopped for another reason: report that rather than piling turns on top
    of = max_rounds(jobs.settings, job)
    used = rounds_used(job)
    if of <= 0:
        return None
    if used >= of:
        jobs._event(job, f"the check still fails after {used} round(s) of fixes; delivering what there is")
        return None
    budget, spent = remaining_budget(job)
    if spent:
        jobs._event(job, f"the check failed; no verify-fix turn: {spent}")
        return None
    jobs._event(job, f"the check failed; sending it back to the agent (round {used + 1} of {of})")
    return Turn(kind="follow_up", requester="hangar", reply_to=turn.reply_to, trigger="verify", budget=budget,
                message=fix_message(job, job.result.verify_exit_code, job.result.verify_tail, used + 1, of))


async def touched_tests(jobs: JobService, job: Job) -> list[str]:
    """Existing files the check relies on that the agent's work changed (strategies.touched_tests: #49's check)."""
    from hangar.strategies import touched_tests as touched

    base = shlex.quote(job.result.base_sha or f"origin/{job.base_branch}")
    res = await jobs._sh(job, f"git diff --name-status {base} --")
    if res.exit_code != 0:
        return []
    return touched(job.verify, [ln for ln in res.stdout.splitlines() if ln.strip()])
