# Sequential best-of-N: more attempts only when the first falls short

This page is for whoever submits best-of-N jobs and pays for them. Part of [jobs](README.md).

A [best-of-N job](working-with-the-agent.md#6-forks-and-best-of-n) runs all N attempts at once and ranks them when the last one ends. When attempt 1 would have passed anyway, the other attempts are spend that bought nothing. A sequential best-of-N runs attempt 1 alone first. Attempts 2..N start, in parallel, only if it falls short.

Parallel stays the default. A job, or its repo's policy, has to ask for the sequential run.

## How to ask for it

```bash
hangar job submit "fix the flaky test_export" --repo https://github.com/acme/app \
  --verify "pytest tests/test_export.py -q" --best-of 3 --sequential --wait
```

Over REST or MCP, add `"sequential": true` to a `best_of_n` job. Add `"escalate_at": 0.4` to set the share of the job budget attempt 1 may use. An `escalate_at` turns the sequential run on by itself.

A repo turns it on for every best-of-N job with its [policy](../repo-policy.md):

```bash
curl -X PUT "$HANGAR/api/repos/policy" -H "Authorization: Bearer $TOKEN" -d '{"repo": "acme/app",
  "best_of_n": {"sequential": true, "escalate_at": 0.4, "max_n": {"low": 2, "normal": 3, "high": 5}}}'
```

A job's own `sequential` comes first, so `"sequential": false` keeps one job parallel.

## What starts attempts 2..N

Attempt 1 falls short, and the others start, when it:

- fails `verify`, or exits with an error when there is no `verify`;
- doesn't finish: it timed out, its sandbox went away, or its agent failed;
- changes nothing;
- changes the verify command's own tests (`touched_tests`);
- asks a question instead of finishing;
- goes over its share of the budget, or uses more than `escalate_at` of the job budget.

An attempt 1 that asked a question met no bar, so the others start. If none of them finishes, the job waits for the answer to attempt 1's question, as a parallel one does.

When attempt 1 passes, it is delivered and attempts 2..N never start. Their forked sessions are archived, and no sandbox was booted for them.

The AI first-pass review doesn't count yet. It runs on the job's PR after delivery ([landing](../landing.md#6a-ai-first-pass-review)), not on each attempt. So a blocking review can't start attempts 2..N.

## The budget across both phases

The [job budget](../repo-policy.md#budgets) still caps the whole job, attempts and judge together.

- Attempt 1 gets `escalate_at` of the job budget. Without it, it gets the equal share a parallel attempt gets.
- The attempts that start after it split what it left equally. The judge keeps one share, as in a parallel run.
- A job budget that attempt 1 used up starts nothing more. The event says so, and attempt 1 is delivered.

Each turn's own `budget` still caps every attempt. A share is fixed when its attempt starts, so a restart doesn't change it.

## Attempts per risk tier

The policy's `max_n` caps N by the job's risk tier: `low`, `normal` or `high`. The tier is the job's `hangar.risk` label.

Only the submitter sets that label: an API caller, or a trigger's labels as its admin wrote them. Ticket text never sets it. A ticket's *Touches risk paths?* answer ([agent tickets](../intake.md#agent-tickets)) is untrusted, so it can't pick the cap.

A job that names no `n` gets its tier's number. A larger `n` is cut to it. Either change adds an event that names the tier and the cap. A job with no tier gets the usual default of 3.

The tier caps attempts only. Landing still judges a PR's risk by its files.

## What the job says

- The events say that attempt 1 runs alone, then what was decided and why. For example: "Attempt 1 failed verify (exit 1): attempts 2..3 started".
- The PR body's attempts table opens with how many attempts ran and why. Attempts that never started show as `not started`.
- The [review packet](../landing.md#6-the-review-packet) has an **Attempts** line with the same count and reason.
- `get_job` and `GET /api/jobs/{id}` carry `sequential`: `escalate_at`, `escalated` and `why`. An attempt that never ran has the outcome `skipped`.

A control-plane restart re-attaches to whichever phase was running. The decision is recorded on the job, so it is never made twice.
