# Budgets and shadows

This page is part of [Repo policy: the defaults every front door applies](../repo-policy.md).

## Budgets

A `budget` caps one agent turn, and every turn used to get a fresh one. So `max_cost_usd: 2` with best of 4, a judge and 3 auto-fix rounds could spend $16 before anyone followed up. Two limits bound the spend:

**A job budget** (`job_budget` on the job, the repo's policy, or `HANGAR_JOB_BUDGET_MAX_COST_USD` / `_MAX_TOKENS` / `_MAX_TURNS`) caps all of a job's agent turns together. That covers the first turn, follow-ups, verify-fix and PR auto-fix turns, every best-of-N attempt and the judge.

- **The turn that crosses it is stopped.** Each turn's budget is cut to what the job has left, and the turn stops like any turn over budget: `over_budget`, with its work delivered as a draft.
- **A spent job starts no more turns.** Follow-ups and auto-fix rounds are refused with the reason, and verify-fix rounds aren't started.
- **Best-of-N splits it evenly.** The attempts run at once, so each gets an equal share, with one share kept for the judge; together they can't cross the job budget. An attempt can overshoot its share by what it spends between two polls, so when the attempts have used the whole job budget there is no judge: the event says so, and the smallest diff wins. A `max_turns` lower than the attempts plus the judge is refused when the job is submitted.

**A daily cap per repo** (`max_usd_per_day`) is checked when work is admitted, on the repo's spend since midnight in `HANGAR_BUDGET_DAY_TIMEZONE` (an IANA zone; UTC unless set). UTC midnight is 20:00 in New York (19:00 in winter), in the middle of a working day there, so a team in the Americas should set its zone (`America/New_York`); otherwise a cap can be spent twice in one working day. Days follow the zone's daylight-saving changes. `/api/usage` still groups by UTC day. Over the cap:

- a new job over the cap waits in `awaiting_approval`, and the GitHub issue or Slack thread says why. A trusted person can let it run anyway (`/hangar approve`, `hangar job approve`), or it is dropped when the approval expires (`HANGAR_JOB_APPROVAL_TTL_HOURS`);
- a follow-up or auto-fix round is refused;
- turns already running, and a job's own verify-fix turns, finish under the job budget.

Costs are what the harness reports, or the token counts priced with the price table ([activity.md](../activity.md)). On a subscription login the dollars are an estimate; `max_tokens` and `max_turns` don't depend on prices.

For each field the order is the job, then the repo's policy, then the settings.

**A daily cap per person** (`HANGAR_PERSON_MAX_USD_PER_DAY`, unset by default) works the same way for the jobs a person submits. It counts what all of that person's sessions and jobs spent that day, whichever of their tokens or connectors started them (the person hangar records on each; see [security/people.md](../security/people.md#people)). Jobs nobody in particular asked for, such as GitHub- and Slack-triggered ones, have no person yet and count only against their repo's cap.

### The eval budget

Evals run on API keys with dollar caps, so eval spend has a monthly budget of its own, across experiments and trials
(#182):

| Setting | Default | Meaning |
| --- | --- | --- |
| `HANGAR_EVAL_BUDGET_USD_MONTH` | 1500 | The team's eval spend per calendar month (UTC); 0 turns the eval budget off |
| `HANGAR_EVAL_AUTHOR_CAP_USD_MONTH` | 400 | Each author's share; `hangar person set alice --eval-cap 600` (an admin) gives one person their own |

- **What counts.** Eval jobs (`queue_class: eval`: experiment trials, the trials API's, shadows) created this month,
  by their author: the experiment's person, else who asked for it. Every session that carried a trial counts, deleted
  ones too. Cached trial results ([api/trials.md](../api/trials.md#the-cache)) start no job and cost nothing.
- **The gate.** An experiment, or a `POST /api/trials`, whose projected cost exceeds what the team or the author has
  left is refused, with the spent and remaining numbers. The projection is trials × each task's p50 cost, taken from
  the cached trials of the same key, else from the trial job budget's `max_cost_usd`. A task with neither projects $0,
  and the message says so.
- **No kills.** Trials already running finish, even past the budget (each trial's job budget is the hard stop), but
  once the team's or the author's budget is spent no new trial starts: they show `skipped` with the reason.
- **Seeing it.** `hangar eval budget` (`GET /api/eval/budget`) shows the month, the team's budget, what's spent and
  left, and each author's cap and spend.

## Shadows

A shadow runs a candidate harness on a sampled real ticket, at the same base commit, and delivers nothing; flightdeck
grades it later against what people merged (#183). The harness owner turns it on per repo:

```bash
curl -s -X PUT $HANGAR/api/repos/policy -H "Authorization: Bearer $ADMIN" -d '{"repo": "acme/api",
  "shadow": {"harness": "team-next@sha256:9a41...", "p": 0.1, "sources": ["jira", "github"], "owner": "ivy"}}'
```

| Field | Meaning |
| --- | --- |
| `harness` or `pin_set` | The candidate: a harness lock by digest, or a pin set (its harness, environment and model; the task and base are the real job's) |
| `p` | The share of the repo's real jobs from `sources` that get a shadow (0.0-1.0; absent or 0: none) |
| `sources` | `jira`, `github`, `slack`, `github-projects` (default the first three) |
| `owner` | Whose monthly eval cap the shadows count against ([the eval budget](#the-eval-budget)); the team budget always |
| `budget_usd` | Each shadow's job budget (5) |
| `canary` | Injection canaries in some shadows: `{"p": 0.0-1.0, "placements": [...]}` (below; absent: none) |

When a sampled job has started (its sandbox is up, its base commit known), hangar starts one trial against a pin set of
the candidate at that commit (`POST /api/trials`, `source: shadow`, `shadow_of: <job id>`), in the eval queue class
([team-scale.md](../team-scale.md#the-eval-queue-class)). The shadow gets the job's task text and context, untrusted parts
fenced as for the job, and:

- `deliver: none`, no PR, no `intake.tracker` label, no `source_ref`, `reply_to` or callback: nothing reaches Jira,
  Slack or GitHub;
- egress `none`, and no person, so the team's credentials and never someone's own login;
- in nobody's inbox or job list (`GET /api/jobs?source=shadow`, admins only);
- `run.json` in the outbox says `source: shadow` and `shadow_of`, and the real job's merge or close events grade it.

**Review comes first.** Turning `p` above 0 is refused unless the last 7 days' review-wait p50 is under 4 hours
(`hangar metrics weekly`). When it rises above that, shadows pause by themselves (an audit entry `shadow.paused`, once),
and they resume when it falls back (`shadow.resumed`).

**Injection canaries.** With `canary: {"p": 0.2, "placements": ["ticket_description", "ticket_comment",
"slack_thread", "repo_file"]}` (placements default to all four), a share `p` of the shadows get one benign instruction
with a fresh token `CANARY-<8 hex>`, planted at one of the placements the shadow has: appended to the ticket's text, as
a comment on it, as a reply in the Slack thread, or in a `NOTES.local.md` added to the shadow's working tree (excluded
from git, never committed). The attachment it lands in is fenced as untrusted; the real job never sees it. After the
turn hangar records whether the agent obeyed (`canary.json` in the outbox) and flightdeck rates each harness digest's
injection susceptibility from them. How it works and what it records: [security/taint.md](../security/taint.md#injection-canaries).
