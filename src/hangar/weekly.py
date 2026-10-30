"""Weekly adoption metrics and the standup line (--- #168; docs/metrics.md).

The definitions are flightdeck's (its docs/metrics.md, DEFINITIONS: ISO weeks in UTC, agent jobs, terminal and
counted jobs); flightdeck computes them over the outbox's Parquet with intervals and history. Hangar computes the same
definitions from its own run store, counts, rates and percentiles only, for the team and the morning standup, so the
line works before (and without) flightdeck's report.

What it records for that, beyond what landing already kept (models.LandingState):

* `opened_at`, `ready_at` (the PR opened non-draft or marked ready), `first_review_at` / `first_review_by` (the first
  review a human submitted, any state; bots, the App's own AI review included, never count: reviewtrust.is_bot),
  `merge_commit_sha` (what a squash merge put on the default branch);
* `abandon_reason` for a PR closed unmerged: `/hangar abandon <reason>` on the PR from a trusted person, or a label
  `agent-abandon:<reason>`, one of ABANDON_REASONS (anything else is refused with the list); the inbox lists closed
  agent PRs without one;
* `reverted_at` / `revert_sha` / `fix_forward_pr`: a nightly pass reads each mapped repo's default branch for the last
  14 days (the control token, read-only) for `Revert "…"` / `This reverts commit <sha>` commits of a merged agent PR's
  merge commit, and merged PRs whose body says `Fixes-forward: #N`; the same pass caches every merged PR of the repo
  (humans' too), which `agent_share` divides by;
* `review_activity` (--- KAN-70): the same pass reads each agent PR merged in the last ATTENTION_DAYS once, its
  reviews and review comments, raw; `attention` decides which of them are a human reviewer's attention.

`GET /api/metrics/weekly?week=2026-W43&repo=…&person=…`, `hangar metrics weekly [--repo] [--person] [--json]`. Per
person: that person and admins only. The standup line goes to HANGAR_STANDUP_CHANNEL at 10:18 ET on weekdays
(Mondays: last week's summary), team-level only, from the control plane (it runs while the pools are off).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from hangar.models import Job, JobState, now

if TYPE_CHECKING:
    from hangar.auth import Principal
    from hangar.jobs import JobService

log = logging.getLogger("hangar.weekly")

SCHEMA = "hangar.metrics.weekly/v1"
ABANDON_REASONS = ("wrong_approach", "bad_ticket", "flaky_env", "too_big", "superseded", "other")
ABANDON_LABEL = "agent-abandon:"
YIELD_DAYS = 30
REVERT_DAYS = 14
DELEGATOR_MIN = 3  # merged agent PRs in the week that make a person an active delegator
NIGHTLY_AFTER = (3, 0)  # local time the nightly pass runs after
STANDUP_LATEST = (13, 0)  # past this, a missed standup line (hangar was down) is skipped for the day
TICK = 60.0
KV_MERGED = "weekly:merged:"  # + repo key -> {"at", "prs": [{number, merged_at, merge_commit_sha, bot}]}
KV_NIGHTLY = "weekly:nightly-at"
# --- KAN-70: reviewer attention per merged agent PR. The ticket owner's default definition, to change here:
# review events are submitted reviews and review comments (ATTENTION_KINDS) by humans; bots, the PR's author (the
# agent's account) and hangar's own AI review never count (attention()).
ATTENTION_KINDS = ("review", "review_comment")
ATTENTION_DAYS = YIELD_DAYS + 7  # merged agent PRs the nightly pass reads reviews for
ATTENTION_MAX_PRS = 200  # per repo and night: bounds the API calls; the rest wait for the next night
ACTIVE_MINUTES_NOTE = ("unavailable: hangar records when reviews and comments were submitted, not how long anyone "
                       "spent reviewing")
KV_STANDUP = "weekly:standup:"  # + local date -> when it was posted
_REVERT_TITLE = re.compile(r'^Revert "(.+)"', re.M)
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.I)
_FIXES_FORWARD = re.compile(r"(?im)^\s*Fixes-forward:\s*#(\d+)\b")
_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")


UTC_ZONE = ZoneInfo("UTC")
DEFINITIONS = "https://sean-koval.github.io/flightdeck/metrics/"  # flightdeck docs/metrics.md, the one definition


class WeeklyError(ValueError):
    pass


# ====================================================================== time
def tz(settings: Any) -> ZoneInfo:
    return ZoneInfo(getattr(settings, "standup_timezone", None) or "America/New_York")


def week_bounds(week: str | None, zone: ZoneInfo, at: float | None = None) -> tuple[float, float, str]:
    """[start, end) of an ISO week (`2026-W43`, Monday 00:00 local to Monday 00:00), and its label; without `week`,
    the week holding `at` (now)."""
    if week:
        m = _WEEK.match(week.strip())
        if m is None:
            raise WeeklyError(f"'{week}' isn't an ISO week (like 2026-W43)")
        try:
            monday = datetime.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        except ValueError as e:
            raise WeeklyError(f"'{week}': {e}") from None
    else:
        d = datetime.fromtimestamp(at if at is not None else now(), zone)
        monday = datetime(d.year, d.month, d.day) - timedelta(days=d.weekday())
    start = monday.replace(tzinfo=zone)
    end = start + timedelta(days=7)
    y, w, _ = start.isocalendar()
    return start.timestamp(), end.timestamp(), f"{y}-W{w:02d}"


def day_bounds(zone: ZoneInfo, at: float, back: int = 1) -> tuple[float, float, str]:
    """[start, end) of the local day `back` days before `at`'s."""
    d = datetime.fromtimestamp(at, zone)
    start = datetime(d.year, d.month, d.day, tzinfo=zone) - timedelta(days=back)
    return start.timestamp(), (start + timedelta(days=1)).timestamp(), start.strftime("%Y-%m-%d")


def pct(xs: list[float], q: float) -> float | None:
    """Nearest-rank percentile (docs/metrics.md): the smallest value with at least q% of the values at or below."""
    s = sorted(xs)
    if not s:
        return None
    k = max(0, min(len(s) - 1, -(-len(s) * q // 100) - 1))
    return s[int(k)]


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _in(ts: float | None, lo: float, hi: float) -> bool:
    return ts is not None and lo <= ts < hi


# ====================================================================== the records
def agent_prs(store: Any, repo: str | None = None, person: str | None = None) -> list[Job]:
    """Jobs that opened a PR (hangar's agent PRs), archived ones included; review jobs (#161) are not agent PRs."""
    from hangar.repopolicy import repo_key
    from hangar.store import NOT_NULL

    want = repo_key(repo) if repo else None
    out = []
    for j in store.list_where(Job, archived=True, **{"result.pr_number": NOT_NULL}):
        if j.source == "review" or (want and repo_key(j.repo) != want) or (person and j.person != person):
            continue
        out.append(j)
    return out


def opened_at(job: Job) -> float:
    st = job.landing
    if st.opened_at:
        return st.opened_at
    url = job.result.pr_url or ""
    return next((e.ts for e in job.events if url and url in (e.message or "")), job.created_at)


def infra(job: Job) -> bool:
    """--- #166: infrastructure ended it: left out of every statistic."""
    return job.result.outcome_class == "infra_error"


def merged_at(job: Job) -> float | None:
    return job.landing.merged_at if job.landing.pr_state == "merged" or job.landing.merged_at else None


def closed_unmerged_at(job: Job) -> float | None:
    st = job.landing
    return st.closed_at if st.pr_state == "closed" and not st.merged_at else None


def _steers(store: Any, job: Job) -> tuple[int, int, int]:
    """(steers, corrections, unclassified) over the job's sessions' turns and its follow-up turns (#162's
    `steer_class` where it is set)."""
    from hangar.models import Session

    n = corr = uncl = 0
    for s in store.list_where(Session, archived=True, job_id=job.id):
        for t in s.turns:
            for st in t.steers:
                n += 1
                c = getattr(st, "steer_class", None)
                corr += c == "correction"
                uncl += c in (None, "unclassified")
    for t in job.turns:
        if t.kind == "follow_up":
            c = getattr(t, "steer_class", None)
            corr += c == "correction"
            uncl += c in (None, "unclassified")
    return n, corr, uncl


def _cost(svc: JobService, job: Job) -> float:
    total = 0.0
    with contextlib.suppress(Exception):
        u = svc.usage(job)
        total += (u.cost_usd or 0.0) if u else 0.0
    review = getattr(job.landing, "ai_review", None)  # --- #161: its independent review job's spend
    rid = review.get("job") if isinstance(review, dict) else None
    if rid and (rj := svc.store.get(Job, rid)) is not None:
        with contextlib.suppress(Exception):
            u = svc.usage(rj)
            total += (u.cost_usd or 0.0) if u else 0.0
    return total


def all_merged(store: Any, repos: list[str], lo: float, hi: float) -> int | None:
    """Every merged PR on these repos in [lo, hi) (humans' too), from the nightly cache; None when a repo has none."""
    n = 0
    for r in repos:
        got = store.kv_get_json(KV_MERGED + r, None)
        if not isinstance(got, dict):
            return None
        n += sum(1 for p in got.get("prs") or [] if _in(p.get("merged_at"), lo, hi))
    return n


def mapped_repos(settings: Any, repo: str | None = None) -> list[str]:
    from hangar.repopolicy import repo_key

    if repo:
        return [k] if (k := repo_key(repo)) else []
    return sorted({k for r in (settings.github_repos or {}) if (k := repo_key(r))})


# ====================================================================== computing
def agent_jobs(store: Any, since: float, repo: str | None = None, person: str | None = None) -> list[Job]:
    """flightdeck's *agent jobs*: delegated work (not experiment trials, shadow runs or review jobs) changed since
    `since`, archived ones included, narrowed to a repo and a person."""
    from hangar import experiments
    from hangar.repopolicy import repo_key
    from hangar.store import Ge

    want = repo_key(repo) if repo else None
    out = []
    for j in store.list_where(Job, archived=True, updated_at=Ge(since)):
        if j.source in ("experiment", "review", "shadow") or experiments.EXP_LABEL in j.labels:
            continue
        if (want and repo_key(j.repo) != want) or (person and j.person != person):
            continue
        out.append(j)
    return out


def terminal_at(job: Job) -> float | None:
    """flightdeck's *terminal*: the first of its PR merged, its PR closed unmerged, the job abandoned (cancelled),
    or, with no PR, the job ended."""
    st = job.landing
    times = [x for x in (st.merged_at, closed_unmerged_at(job)) if x]
    if job.state == JobState.CANCELLED:
        times.append(job.updated_at)
    if not job.result.pr_number and job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        times.append(job.updated_at)
    return min(times) if times else None


def counted(job: Job) -> bool:
    """flightdeck's *counted*: it ran at least one turn, and its last run wasn't an infra_error (#166)."""
    runs = [t for t in job.turns if t.run_id]
    return bool(runs) and (job.result.outcome_class or runs[-1].outcome_class) != "infra_error"


def abandoned(job: Job) -> bool:
    return not job.landing.merged_at and (closed_unmerged_at(job) is not None or job.state == JobState.CANCELLED)


def compute(svc: JobService, lo: float, hi: float, repo: str | None = None,
            person: str | None = None) -> dict[str, Any]:
    """Every metric of flightdeck's docs/metrics.md (the definitions; docs/metrics.md here cites them) over
    [lo, hi), from hangar's own records."""
    store = svc.store
    cohort_lo, cohort_hi = lo - REVERT_DAYS * 86400, hi - REVERT_DAYS * 86400
    jobs = agent_jobs(store, cohort_lo - 86400, repo, person)
    terminal = [j for j in jobs if _in(terminal_at(j), lo, hi)]
    excluded = sum(1 for j in terminal if any(t.run_id for t in j.turns) and not counted(j))  # infra_error
    denom = [j for j in terminal if counted(j)]
    y_merged = [j for j in denom if j.landing.merged_at and terminal_at(j) == j.landing.merged_at]
    lost = [j for j in denom if abandoned(j)]
    prs = [j for j in jobs if j.result.pr_number]
    merged = [j for j in prs if _in(j.landing.merged_at, lo, hi)]
    everyone = all_merged(store, mapped_repos(svc.settings, repo), lo, hi) if not person else None
    waits = [j.landing.first_review_at - (j.landing.ready_at or opened_at(j)) for j in prs
             if _in(j.landing.first_review_at, lo, hi) and j.landing.first_review_at >= (j.landing.ready_at
                                                                                        or opened_at(j))]
    g2m = [j.landing.green_to_merge_seconds for j in merged if j.landing.green_to_merge_seconds is not None]
    follow = steers = corr = uncl = needs = 0
    for j in merged:
        follow += sum(1 for x in j.turns if x.kind == "follow_up")
        needs += sum(1 for x in j.turns if x.outcome == "needs_input")
        n, c, u = _steers(store, j)
        steers, corr, uncl = steers + n, corr + c, uncl + u
    reasons = Counter(j.landing.abandon_reason or "unlabelled" for j in lost)
    cohort = [j for j in prs if _in(j.landing.merged_at, cohort_lo, cohort_hi)]
    reverted = [j for j in cohort if _reverted(j)]
    cost = sum(_cost(svc, j) for j in merged)
    per_person = Counter(j.person for j in merged if j.person)
    delegators = sorted(p for p, n in per_person.items() if n >= DELEGATOR_MIN)
    active = {j.person for j in jobs if j.person and any(_in(t.started_at, lo, hi) or _in(t.finished_at, lo, hi)
                                                         for t in j.turns)} | set(per_person)
    return {
        "yield": {"value": _rate(len(y_merged), len(denom)), "merged": len(y_merged), "terminal": len(denom)},
        "agent_share": {"value": _rate(len(merged), everyone) if everyone else None,
                        "agent_merged": len(merged), "all_merged": everyone,
                        **({"note": "the nightly pass hasn't read every mapped repo's merged PRs yet"}
                           if everyone is None and not person else {})},
        "review_wait": {"p50_hours": _h(pct(waits, 50)), "p90_hours": _h(pct(waits, 90)), "n": len(waits)},
        "green_to_merge": {"p50_hours": _h(pct(g2m, 50)), "p90_hours": _h(pct(g2m, 90)), "n": len(g2m)},
        "steer_rate": {"value": _rate(follow + steers + needs, len(merged)),
                       "corrections": _rate(corr, len(merged)), "follow_ups": follow, "steers": steers,
                       "needs_input": needs, "unclassified": uncl, "merged": len(merged)},
        "abandon_rate": {"value": _rate(len(lost), len(denom)), "abandoned": len(lost), "terminal": len(denom),
                         "by_reason": dict(sorted(reasons.items())), "unlabelled": reasons.get("unlabelled", 0)},
        "revert_rate": {"value": _rate(len(reverted), len(cohort)), "reverted": len(reverted), "cohort": len(cohort),
                        "cohort_start": cohort_lo, "cohort_end": cohort_hi},
        "cost_per_merged_pr": {"usd": round(cost / len(merged), 4) if merged else None,
                               "total_usd": round(cost, 4), "merged": len(merged)},
        "reviewer_attention": reviewer_attention(merged),
        "active_delegators": {"value": len(delegators), "of": len(active), "min_merged": DELEGATOR_MIN,
                              **({"people": delegators} if person is None else {})},
        "excluded_infra_error": excluded,
    }


def attention(job: Job) -> list[dict[str, Any]] | None:
    """--- KAN-70: the review events of a PR that count as reviewer attention (ATTENTION_KINDS, by a human who isn't
    the PR's author, not hangar's own AI review); None when the nightly pass hasn't read the PR yet."""
    ra = job.landing.review_activity
    if not isinstance(ra, dict):
        return None
    author = str(ra.get("author") or "").lower()
    return [e for e in ra.get("events") or [] if isinstance(e, dict) and e.get("kind") in ATTENTION_KINDS
            and not e.get("bot") and not e.get("hangar") and str(e.get("login") or "").lower() not in ("", author)]


def reviewer_attention(merged: list[Job]) -> dict[str, Any]:
    """--- KAN-70: review events per merged agent PR, over the merged PRs whose reviews were read (`measured`)."""
    per = {j.id: got for j in merged if (got := attention(j)) is not None}
    counts = [float(len(x)) for x in per.values()]
    events = [e for x in per.values() for e in x]
    kinds = Counter(e["kind"] for e in events)
    return {"events_per_merged_pr": _rate(len(events), len(per)), "events": len(events),
            "reviews": kinds.get("review", 0), "review_comments": kinds.get("review_comment", 0),
            "p50": pct(counts, 50), "p90": pct(counts, 90), "merged": len(merged), "measured": len(per),
            "unreviewed": sum(1 for c in counts if not c),
            "reviewers": len({str(e["login"]).lower() for e in events}),
            "active_minutes": None, "active_minutes_note": ACTIVE_MINUTES_NOTE,
            **({"note": "the nightly pass hasn't read the reviews of every merged agent PR yet"}
               if len(per) < len(merged) else {})}


def _h(seconds: float | None) -> float | None:
    return round(seconds / 3600, 2) if seconds is not None else None


def _reverted(job: Job) -> bool:
    st, m = job.landing, job.landing.merged_at or 0
    return any(x is not None and x - m <= REVERT_DAYS * 86400 for x in (st.reverted_at, st.fix_forward_at))


def weekly(svc: JobService, week: str | None = None, repo: str | None = None,
           person: str | None = None) -> dict[str, Any]:
    lo, hi, label = week_bounds(week, UTC_ZONE)  # flightdeck's weeks: ISO, UTC
    return {"schema": SCHEMA, "week": label, "start": lo, "end": hi, "repo": repo, "person": person,
            "timezone": "UTC", "definitions": DEFINITIONS, "computed_at": now(),
            "metrics": compute(svc, lo, hi, repo, person)}


# ====================================================================== the standup line
def _held(svc: JobService) -> int | None:
    """--- #159: new jobs held for review capacity (queued, reason `awaiting_capacity`)."""
    from hangar import fairness

    reason = getattr(fairness, "AWAITING", None)
    if reason is None:
        return None
    return sum(1 for j in svc.store.list_where(Job, state=JobState.QUEUED.value) if j.reason == reason)


def _top_failure(svc: JobService, lo: float, hi: float) -> str | None:
    from hangar.store import Ge

    reasons = Counter()
    for j in svc.store.list_where(Job, archived=True, updated_at=Ge(lo)):
        if j.state == JobState.FAILED and _in(j.updated_at, lo, hi) and not infra(j):
            reasons[(j.reason or "unknown")[:80]] += 1
    return reasons.most_common(1)[0][0] if reasons else None


def standup_line(svc: JobService, at: float | None = None) -> str:
    """Team-level only. Tuesday to Friday: yesterday; Monday: last week."""
    zone = tz(svc.settings)
    at = at if at is not None else now()
    monday = datetime.fromtimestamp(at, zone).weekday() == 0
    if monday:
        lo, hi, label = week_bounds(None, UTC_ZONE, at - 7 * 86400)
        head = f"Last week ({label})"
    else:
        lo, hi, _ = day_bounds(zone, at)
        head = "Yesterday"
    m = compute(svc, lo, hi)
    ab = m["abandon_rate"]
    reasons = ", ".join(f"{n} {r}" for r, n in sorted(ab["by_reason"].items(), key=lambda kv: (-kv[1], kv[0])))
    bits = [f"{m['steer_rate']['merged']} agent PRs merged",
            f"{ab['abandoned']} abandoned" + (f" ({reasons})" if ab["abandoned"] else "")]
    w = m["review_wait"]["p50_hours"]
    bits.append(f"review wait p50 {w:.1f} h" if w is not None else "review wait p50 -")
    if (held := _held(svc)) is not None:
        bits.append(f"{held} jobs held for review capacity")
    bits.append(f"top failure: {_top_failure(svc, lo, hi) or 'none'}")
    if monday:
        y = m["yield"]
        bits.append(f"yield {round(y['value'] * 100)}% ({y['merged']}/{y['terminal']})" if y["value"] is not None
                    else "yield -")
        bits.append(f"{m['active_delegators']['value']} active delegators")
        ra = m["reviewer_attention"]["events_per_merged_pr"]
        bits.append(f"{ra:g} review events per merged PR" if ra is not None else "review events per merged PR -")
    return f"{head}: " + ", ".join(bits)


def standup_due(settings: Any, at: float, posted: bool) -> bool:
    """A weekday, at or after HANGAR_STANDUP_TIME local, before STANDUP_LATEST, not posted yet, a channel set."""
    if not getattr(settings, "standup_channel", None) or posted:
        return False
    d = datetime.fromtimestamp(at, tz(settings))
    hh, mm = _hhmm(getattr(settings, "standup_time", "10:18"))
    return d.weekday() < 5 and (hh, mm) <= (d.hour, d.minute) < STANDUP_LATEST


def _hhmm(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if m is None or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return 10, 18
    return int(m.group(1)), int(m.group(2))


async def post_standup(svc: JobService, at: float | None = None) -> str | None:
    """Post today's line once (marked in the store before posting: a crash mid-post never posts twice). Returns the
    line, None when it isn't due."""
    from hangar import slack

    at = at if at is not None else now()
    day = datetime.fromtimestamp(at, tz(svc.settings)).strftime("%Y-%m-%d")
    if not standup_due(svc.settings, at, svc.store.kv_get(KV_STANDUP + day) is not None):
        return None
    line = await asyncio.to_thread(standup_line, svc, at)
    svc.store.kv_set(KV_STANDUP + day, str(at), durable=True)
    api = slack.client_for(svc)
    if api is None:
        log.warning("standup: HANGAR_SLACK_BOT_TOKEN_CREDENTIAL is not set or not stored; not posted: %s", line)
        return line
    try:
        await api.post(svc.settings.standup_channel, svc.broker.redact(line))
    except slack.SlackError as e:
        log.warning("standup: could not post to %s: %s", svc.settings.standup_channel, e)
    finally:
        await api.close()
    return line


# ====================================================================== the nightly pass
async def nightly(svc: JobService) -> dict[str, int]:
    """Each mapped repo: merged PRs of the last YIELD_DAYS + a week (cached for agent_share), and reverts and
    fix-forwards of merged agent PRs in the last REVERT_DAYS. Read-only, with the control token."""
    out = {"repos": 0, "merged": 0, "reverted": 0, "fixed_forward": 0}
    gh = svc._github()
    if gh is None:
        return out
    try:
        for repo in mapped_repos(svc.settings):
            try:
                got = await _scan_repo(svc, gh, repo)
            except Exception as e:  # noqa: BLE001 - one repo's failure leaves the others
                log.warning("weekly: nightly pass of %s failed: %s", repo, e)
                continue
            out["repos"] += 1
            for k in ("merged", "reverted", "fixed_forward"):
                out[k] += got[k]
    finally:
        with contextlib.suppress(Exception):
            await gh.close()
    svc.store.kv_set(KV_NIGHTLY, str(now()), durable=True)
    return out


async def _scan_repo(svc: JobService, gh: Any, repo: str) -> dict[str, int]:
    t = now()
    since = t - (YIELD_DAYS + 7) * 86400
    prs = await gh.merged_pulls(repo, since)
    svc.store.kv_set_json(KV_MERGED + repo, {"at": t, "prs": [
        {"number": p["number"], "merged_at": _ts(p.get("merged_at")), "merge_commit_sha": p.get("merge_commit_sha"),
         "bot": str((p.get("user") or {}).get("type")) == "Bot"} for p in prs if p.get("merged_at")]})
    live = getattr(svc, "_jobs", {})  # a job in memory is the one to change (it is saved from there)
    ours = {j.result.pr_number: live.get(j.id) or j for j in agent_prs(svc.store, repo) if j.landing.merged_at
            and j.landing.merged_at >= t - (REVERT_DAYS + 1) * 86400}
    got = {"merged": len(prs), "reverted": 0, "fixed_forward": 0}
    await _read_reviews(svc, gh, repo, prs, live, t)
    if not ours:
        return got
    for p in prs:  # a later PR that says it fixes an agent PR forward
        for n in {int(x) for x in _FIXES_FORWARD.findall(p.get("body") or "")}:
            j = ours.get(n)
            if j is not None and p.get("number") != n and j.landing.fix_forward_pr is None:
                j.landing.fix_forward_pr, j.landing.fix_forward_at = p["number"], _ts(p.get("merged_at")) or t
                svc._save(j)
                svc._event(j, f"PR #{n} was fixed forward by #{p['number']}")
                got["fixed_forward"] += 1
    commits = await gh.commits_since(repo, t - REVERT_DAYS * 86400)
    by_sha = {(j.landing.merge_commit_sha or "").lower(): j for j in ours.values() if j.landing.merge_commit_sha}
    for c in commits:
        msg = (c.get("commit") or {}).get("message") or ""
        for sha in _REVERTS.findall(msg):
            j = next((x for s, x in by_sha.items() if s.startswith(sha.lower())), None)
            if j is not None and j.landing.reverted_at is None:
                when = _ts(((c.get("commit") or {}).get("committer") or {}).get("date")) or t
                j.landing.reverted_at, j.landing.revert_sha = when, c.get("sha")
                svc._save(j)
                svc._event(j, f"PR #{j.result.pr_number} was reverted by {str(c.get('sha'))[:12]}")
                with contextlib.suppress(Exception):
                    from hangar import outbox

                    outbox.of(svc.fleet).job_event(j, "revert", when, {"sha": c.get("sha")})
                got["reverted"] += 1
    return got


async def _read_reviews(svc: JobService, gh: Any, repo: str, prs: list[dict[str, Any]], live: dict[str, Job],
                        t: float) -> int:
    """--- KAN-70: each agent PR merged in the last ATTENTION_DAYS whose reviews aren't read yet: its reviews and
    review comments, raw, on `landing.review_activity` (read once, after the merge; read-only GETs)."""
    from hangar.github import MARKER
    from hangar.reviewtrust import is_bot

    authors = {p.get("number"): str((p.get("user") or {}).get("login") or "") for p in prs}
    todo = [live.get(j.id) or j for j in agent_prs(svc.store, repo) if j.landing.merged_at
            and j.landing.merged_at >= t - ATTENTION_DAYS * 86400 and j.landing.review_activity is None]
    n = 0
    for j in sorted(todo, key=lambda x: x.landing.merged_at or 0, reverse=True)[:ATTENTION_MAX_PRS]:
        num = j.result.pr_number
        ai = (j.landing.ai_review or {}).get("review_id")
        try:
            read = (("review", await gh.pull_reviews(repo, num)),
                    ("review_comment", await gh.pull_review_comments(repo, num)))
        except Exception as e:  # noqa: BLE001 - the reverts still get read; the rest wait for the next night
            log.warning("weekly: could not read the reviews of %s#%s: %s", repo, num, e)
            break
        events = []
        for kind, items in read:
            for x in items:
                if kind == "review" and str(x.get("state") or "").upper() == "PENDING":
                    continue  # a draft review nobody submitted
                user = x.get("user") or {}
                rid = x.get("id") if kind == "review" else x.get("pull_request_review_id")
                events.append({"kind": kind, "login": user.get("login"), "bot": is_bot(user),
                               "hangar": (ai is not None and rid == ai) or MARKER in (x.get("body") or ""),
                               "at": _ts(x.get("submitted_at") if kind == "review" else x.get("created_at"))})
        j.landing.review_activity = {"at": t, "author": authors.get(num) or None, "events": events}
        svc._save(j)
        n += 1
    return n


def _ts(iso: Any) -> float | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ====================================================================== abandon reasons
def set_abandon(svc: JobService, job: Job, reason: str, by: str, via: str) -> str | None:
    """Record why the PR was abandoned; returns why it was refused (an unknown reason: the list)."""
    r = reason.strip().lower()
    if r not in ABANDON_REASONS:
        return f"'{reason[:40]}' isn't an abandon reason; use one of: {', '.join(ABANDON_REASONS)}"
    st = job.landing
    before = st.abandon_reason
    st.abandon_reason, st.abandon_by, st.abandon_at = r, by, now()
    svc._save(job)
    svc._event(job, f"abandon reason: {r} (by {by}, {via})" + (f"; was {before}" if before and before != r else ""))
    svc.fleet.audit(by, "landing.abandon_reason", job.id, reason=r, via=via, before=before)
    with contextlib.suppress(Exception):  # --- outbox (#163): flightdeck reads abandon reasons as job events
        from hangar import outbox

        outbox.of(svc.fleet).job_event(job, "abandon", st.abandon_at, {"reason": r, "by": by, "via": via,
                                                                        "pr": "closed"}, once=f"abandon:{r}")
    return None


def parse_abandon(text: str) -> str | None:
    """`abandon <reason>` (what follows `/hangar`), the reason; None for anything else."""
    m = re.match(r"^\s*abandon\b\s*(\S*)", text or "", re.I)
    return m.group(1) if m else None


def label_reason(name: Any) -> str | None:
    return name[len(ABANDON_LABEL):] if isinstance(name, str) and name.lower().startswith(ABANDON_LABEL) else None


# ====================================================================== the inbox
def inbox_source(svc: JobService, p: Principal | None) -> list[dict[str, Any]]:
    """Agent PRs closed unmerged in the last week without an abandon reason (the digest, #158, shows them too)."""
    from hangar import inbox
    from hangar.store import Ge

    out = []
    lo = now() - 7 * 86400
    for j in svc.store.list_where(Job, **{"updated_at": Ge(lo)}):
        st = j.landing
        if not j.result.pr_number or st.pr_state != "closed" or st.abandon_reason or not st.closed_at \
                or st.closed_at < lo or not inbox.yours(j.person, p):
            continue
        out.append(inbox._item("abandon", f"PR #{j.result.pr_number} was closed without merging and has no reason",
                               f"comment `/hangar abandon <{'|'.join(ABANDON_REASONS)}>` on {j.result.pr_url}",
                               st.closed_at, job=j.id))
    return out


# ====================================================================== the loop
class Weekly:
    """The nightly pass after NIGHTLY_AFTER local, and the standup line, on a one-minute tick."""

    def __init__(self, svc: JobService):
        self.svc = svc
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    async def tick(self, at: float | None = None) -> None:
        at = at if at is not None else now()
        d = datetime.fromtimestamp(at, tz(self.svc.settings))
        last = float(self.svc.store.kv_get(KV_NIGHTLY) or 0)
        today = datetime(d.year, d.month, d.day, *NIGHTLY_AFTER, tzinfo=d.tzinfo).timestamp()
        if at >= today > last:
            await nightly(self.svc)
        await post_standup(self.svc, at)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - next minute
                log.exception("weekly metrics tick failed")
            await asyncio.sleep(TICK)


def of(svc: JobService) -> Weekly:
    w = svc.__dict__.get("_weekly")
    if w is None:
        w = svc.__dict__["_weekly"] = Weekly(svc)
    return w


# ====================================================================== REST
def mount(app: Any, h: Any, person_actor: Any) -> None:
    """GET /api/metrics/weekly: team and repo numbers for anyone with a person's token; `person=` only for that
    person and admins."""
    from fastapi import HTTPException

    from hangar import inbox, people

    if inbox_source not in inbox.SOURCES:
        inbox.SOURCES.append(inbox_source)

    @app.get("/api/metrics/weekly")
    async def metrics_weekly(week: str | None = None, repo: str | None = None, person: str | None = None,
                             _: str = person_actor):
        if person and not people.visible(person):
            raise HTTPException(403, "per-person metrics are visible to that person and admins only")
        try:
            return await asyncio.to_thread(weekly, h.jobs, week, repo, person)
        except WeeklyError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/metrics/standup")
    async def metrics_standup(_: str = person_actor):
        return {"line": await asyncio.to_thread(standup_line, h.jobs), "channel": h.settings.standup_channel}

