# Hangar call examples

Use these as MCP argument examples, not commands to paste into a shell. Replace
the example repo, environment, ticket, and budget with values established for the
task, and check the connected server's live schema.

## Ticket to draft PR

After reading APP-42 and confirming that `python-team` has the repo's dependencies,
Codex access, and Git credentials:

Tool: `submit_job`

```json
{
  "title": "APP-42: Preserve Unicode in CSV exports",
  "instructions": "Fix APP-42 in acme/app. CSV exports currently lose non-ASCII characters. Preserve accented names and non-Latin text, keep existing column names and empty-export behavior, and add regression coverage. Follow repository instructions. Run pytest -q and report changed files, test results, and anything unresolved.",
  "repo": "https://github.com/acme/app",
  "environment": "python-team",
  "harness": "codex",
  "verify": "pytest -q",
  "deliver": "draft_pr",
  "autofix": "off",
  "budget": {"max_cost_usd": 5},
  "context": [
    {
      "name": "APP-42.txt",
      "kind": "issue",
      "content": "Acceptance: UTF-8 names round-trip through CSV export. Existing column names and empty exports remain compatible."
    }
  ]
}
```

Use the returned ID, not the ticket ID, with
`wait_for_turn(ref=JOB_ID, timeout_seconds=60)`. Read
`get_job(job=JOB_ID)` until verification and delivery finish.
If the draft PR exists but `result.verify_exit_code` is nonzero, report that
failure explicitly. Continue a correction with
`follow_up_job(job=JOB_ID, message="Also cover combining accents.")`.

A budget caps each turn, not all future follow-ups or all parallel attempts
combined. Do not interpret the example's $5 as a default for every task.

## Start or resume ongoing work

For new work, use `new_session`:

```json
{
  "name": "app-42",
  "environment": "python-team",
  "repo": "https://github.com/acme/app",
  "harness": "codex",
  "message": "Investigate APP-42 using the attached ticket. Explain the cause and propose a fix before editing.",
  "context": [
    {
      "name": "APP-42.txt",
      "kind": "issue",
      "content": "CSV export drops accented and non-Latin names. Keep existing columns and empty-export behavior."
    }
  ]
}
```

Use `wait_for_turn(ref="app-42", timeout_seconds=60)`, then
`get_session(session="app-42")`.

To continue tomorrow, inspect `get_session(session="app-42")`, then call
`send_to_session(session="app-42", message="Implement the agreed fix and run pytest -q.")`.
A suspended session boots and restores automatically. Do not call `new_session`
again for the same stream of work. Use `get_session_patch(ref="app-42")` to inspect
its changes; request or perform authorized delivery separately.

## Parallel attempts

For an explicitly requested comparison, add `"strategy": "best_of_n"` and
`"n": 3` to a job submission, keeping the same acceptance criteria and meaningful
`verify` command for each attempt. Inspect `get_job` for every attempt and the
selected result. Passing verification ranks first; `judge: true` adds an agent
comparison of passing attempts, otherwise selection favors the smallest diff.

To branch existing session work instead, wait for its active turn to finish and
call `fork_session(session="app-42", n=2, message="Try an alternative Unicode fix and run pytest -q.")`.
Follow every returned child by its own session name.

## CLI fallback

When Hangar CLI is already configured for the intended deployment:

```bash
hangar job submit "Fix APP-42: preserve Unicode CSV names and add regression tests" \
  --repo https://github.com/acme/app --env python-team --verify "pytest -q" --wait
hangar session send app-42 "Implement the agreed fix and run pytest -q" --wait
```

The abbreviated CLI example uses the deployment's configured defaults. Check
`hangar job submit --help` for delivery, autofix, model, and budget options before
dispatch, and apply the same scope choices as the MCP workflow. From a local
checkout, use `--from-local` when its unpushed work is part of the task.
