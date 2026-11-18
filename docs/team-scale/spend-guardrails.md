# Spend guardrails

What caps a team's spend by default, what can still overshoot a cap, and who raises one.

The deploys cap spend by default (KAN-290), from a cap of $400 per engineer a month (KAN-214). A company sets its
own amounts in its overlay's `hangar-site`.

| Setting | Default | What it bounds |
| --- | --- | --- |
| `HANGAR_JOB_BUDGET_MAX_COST_USD` | 5 | All of one job's turns together; an env-setup job keeps its own $30 |
| `HANGAR_PERSON_MAX_USD_PER_DAY` | 20 | One person's jobs and sessions in a day, env-setup's agents included |
| `HANGAR_PERSON_MAX_USD_PER_MONTH` | 400 | The same per calendar month |
| `HANGAR_REPO_MAX_USD_PER_DAY` | 40 | A repo whose policy sets no `max_usd_per_day`; a policy can raise it but not remove it |
| `HANGAR_BUDGET_DAY_TIMEZONE` | UTC; `America/New_York` in the owner deploy and the example overlay | When a day starts for the daily caps: set your team's zone |
| `HANGAR_SPEND_ALERT_AT` | `[0.8, 1.0]` | When a daily cap alerts: a Slack DM to the person and the admins, and `spend-cap-80` or `spend-cap-100` ([alerting.md](../alerting.md)) |

Only an admin may ask for a job budget above the deploy's or the repo policy's; anyone may ask for less. A new job's
budget is also lowered to what is left of its person's and repo's caps.

A spent cap holds new jobs for approval
([budgets](../repo-policy/budgets.md#budgets)). **To raise a cap, ask one of your deploy's hangar admins** (`hangar
person ls` shows them).

A cap can still be overshot in two ways. Jobs already running when it is reached finish within their own job
budgets, so the worst case is the running jobs times the job budget. Interactive session turns aren't checked
against the caps, though their spend counts toward them.
