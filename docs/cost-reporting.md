# Cost reporting for a team

This page answers one question for whoever pays for a team's agent work: what did it cost, and what did it buy?
[Weekly adoption metrics](metrics.md) has the other numbers, and [budgets](repo-policy/budgets.md) the per-job caps.

## Cost per verified PR

Cost per merged PR counts only what the merged PRs spent. Cost per verified PR charges all the window's work to the
PRs that passed.

The numerator is every dollar spent on the window's merged PRs and its counted terminal jobs:
abandoned, failed, or ended without a PR. The denominator is the merged PRs whose job's check passed
(`verify_exit_code` 0). A merged PR with no check, or a failed one, adds cost but not a PR.

`GET /api/metrics/weekly` reports it as `cost_per_verified_pr` (`usd`, `total_usd`, `verified`, `merged`, `jobs`).
`GET /api/usage` reports both costs over its `since` window, under `prs`.

## Chargeback export

`hangar usage export --month 2026-11 --by person` (or `--by repo`) writes a CSV for whoever pays. It reads
`GET /api/usage/export` with an admin token. Each row is one person or repo for that calendar month in UTC.

Its columns are turns, tokens by kind, `cost_usd`, `unpriced_turns`, `sandbox_hours` and `merged_prs`. Sandbox hours
are the turns' wall time, so a kept-alive sandbox's idle time isn't charged. A person is the job's or session's owner,
else the turn's requester.

## Monthly caps

Daily caps stop a runaway day. Monthly caps hold a team to its budget. Both are checked when work is admitted, and a
new job over either waits for approval. Follow-ups and auto-fix rounds are refused, as with the daily caps.

| Cap | Set with | Counts |
| --- | --- | --- |
| A repo's | `hangar repo set acme/app --max-usd-per-month 500` (`max_usd_per_month`) | every session on the repo |
| A person's | `HANGAR_PERSON_MAX_USD_PER_MONTH` | their sessions and every session of their jobs |

A month starts at midnight on the first, in `HANGAR_BUDGET_DAY_TIMEZONE`, the zone daily caps use. A repo's policy
shows `spent_this_month_usd` next to `spent_today_usd`.
