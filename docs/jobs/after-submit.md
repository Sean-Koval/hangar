# After you submit a job

A job is in, from [submit a job](README.md#3-submit-a-job). This page says what states it goes through and how you
follow it from REST, the CLI and MCP. Then how its agent asks you a question.

## States and reasons

A job is `queued`, `awaiting_approval`, `provisioning`, `running`, `needs_input`, then `succeeded`, `failed` or
`cancelled`. `reason` says more: why it ended (`verify_failed`, `over_budget`, …) or why a queued job waits.

- `powered_down`: the sandbox pools are off for the night.
- `awaiting_capacity`: its person already has `HANGAR_PERSON_MAX_OPEN_AGENT_PRS` agent PRs awaiting review. Only a
  job's first turn waits; see [review capacity](../team-scale.md#review-capacity).
  `GET /api/jobs?reason=awaiting_capacity` and `hangar job list --reason awaiting_capacity` list the held ones. An
  admin lets one through with `hangar job admit <id>`.

`source` says which door it came through (`api`, `mcp`, `cli`, `github`, `slack`, `jira`, `schedule`, …). `review` is
an [AI first-pass review](../landing.md#6a-ai-first-pass-review) of an agent PR. It delivers nothing and never waits
for review capacity.

## Following it

Poll `GET /api/jobs/{id}`, or wait for the [callback](README.md#4-verify-callbacks). The job view carries `state`, the
`events` timeline, `usage` (tokens and USD), `sandbox_id` and `terminal_session`. Its `result` has the commits,
diffstat, `pr_url`, the verification exit code and output tail, and the agent's summary.

`GET /api/jobs/{id}/events?since=` streams what the agent is doing (messages, commands, file changes, errors). It has
the same shape for every harness.

From the CLI:

```bash
hangar job submit "fix the unicode bug in CSV export" --repo https://github.com/acme/app \
  --env python -c trace.txt --verify "pytest -q" --wait
hangar job list
hangar job follow-up job-3fa2c1d0 "also handle the empty-file case" --wait
hangar job cancel job-3fa2c1d0
hangar job events job-3fa2c1d0 --follow    # what the agent is doing
```

From an MCP client (Claude Code, Codex, …):

- `submit_job`, `get_job`, `list_jobs`, `follow_up_job`, `cancel_job` and `get_events`;
- `wait_for_turn(job)`, to block with progress notifications until the turn ends;
- `approve_job` / `reject_job` for jobs waiting for approval. Admins and the repo's policy approvers use them, with a
  login token, never on their own job ([who may approve](../triggers.md#who-may-approve)).

To run several attempts and keep the best, see
[forks and best-of-N](working-with-the-agent.md#6-forks-and-best-of-n).

## Questions

An agent that needs a decision runs `hangar-agent ask "…"`. The job goes to state `needs_input` with
`result.question`. Nothing is verified or pushed yet, a `job.needs_input` callback is sent, and a GitHub job comments
the question on its issue or PR.

Answer with a follow-up (`/hangar <answer>` on GitHub); the agent continues in the same conversation. Cancelling a
job that needs input ends it.

When the answer is one of a few choices, the agent offers them
(`hangar-agent question --option "A" --option "B" "…"`). The job's view and `result` carry `question_options` (at most
5, each at most 80 characters). So do its inbox item and Fleet unit, so the deck can show them as buttons. Any other
answer still works.

## From another agent

Another agent hands hangar a job over MCP (`submit_job`) or REST, like any client. The A2A endpoint was removed
after v0.2.0 ([removed surfaces](../upgrading.md#removed-surfaces)).
