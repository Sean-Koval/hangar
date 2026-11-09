# Forks and best-of-N

This page is part of [Sessions: picking up where you left off](../sessions.md).

Fork a session to try several directions from the same point:

```bash
hangar session fork onboarding -n 3 "try the email step with magic links instead" -w
hangar session fork onboarding -n 2 --name onboarding-a --name onboarding-b   # no message: just boot them
```

MCP `fork_session(session, n, message?, harness?, names?)`; REST `POST /api/sessions/{name}/fork` with `{"n": 3, "message": "..."}`.

Each fork is a new session:

- `parent_id` names the session it came from, and `forked_from_checkpoint` names the checkpoint it started from. If the parent's sandbox is up and anything changed since its last checkpoint, hangar takes a fresh one first. A parent that is mid-turn can't be forked; wait for the turn to finish.
- It has the parent's commits, uncommitted changes, untracked files and conversation files, on its own branch `<parent branch>-fork-<i>`, in its own sandbox. It keeps the parent's turns as history, with their artifacts (still downloadable from the fork), the parent's open question if it had one, and `get_session_patch` works on it before it ever boots. The inherited turns carry no usage: a fork's `usage` is its own.
- **The conversation forks too.** The fork's first turn branches the agent's conversation with the CLI's own fork: each fork remembers everything up to the fork, and the forks diverge from there. Later turns resume the fork's own conversation.

  | Harness | Fork (verified 2026-10-13) |
  | --- | --- |
  | Claude Code 2.1.296 | `claude --resume <parent> --fork-session --session-id <new>` (`--session-id` with `--resume` is only accepted together with `--fork-session`) |
  | Codex 0.162.1 | `codex exec fork <parent> [prompt]`; the new thread id is read from `thread.started`. The forked rollout refers to the parent's (`forked_from_id`), which the checkpoint carries along. |
  | pi 1.1.0 | `pi --fork <parent> --session-id <new>` |
  | Antigravity 1.3.3 | No fork flag. The fork starts a new conversation with a summary handoff. |

  The same handoff is used when you fork into another harness (`--harness`).
- Two sessions never continue the same conversation id. If two sessions ever hold one id, the oldest keeps it, and the other forks it on its next turn instead of resuming it.

A job with `strategy: best_of_n` is built on this: it forks its prepared session into N attempts and delivers the best one. See [Jobs §6](../jobs/working-with-the-agent.md#6-forks-and-best-of-n).

**Cost:** every fork is its own sandbox while it runs: N forks cost N times one session. They suspend when idle like any session, and a suspended fork costs storage only (each has its own copy of the checkpoint).

## Fork at a step

A session fork starts from a checkpoint, and checkpoints exist only at turn boundaries; background shells and
subagent contexts aren't in the event log either. To fork *inside* a turn, hangar rebuilds the state at step N by
re-execution (#179): a fresh sandbox at the run's base commit replays the run's model tape
([model-gateway.md](../model-gateway.md#replay)) for steps 1..N, running every tool call again for real and checking
each step's request (and so every tool output) against the tape. A clean prefix means the state at N is rebuilt; k live
continuations then go on from there.

```bash
curl -X POST "$HANGAR_URL/api/runs/ses-1a2b3c4d.run-5e6f7a8b/fork" -H "Authorization: Bearer $TOKEN" \
  -d '{"at_seq": 20, "continuations": 3, "wait_seconds": 120}'
# {"fork": "job-9c8d7e6f", "status": "forked", "divergence": null,
#  "trial_ids": ["job-9c8d7e6f.1", "job-9c8d7e6f.2", "job-9c8d7e6f.3"], "trials": [...]}
curl "$HANGAR_URL/api/forks/job-9c8d7e6f" -H "Authorization: Bearer $TOKEN"
```

| Field | |
| --- | --- |
| `at_seq` | The step to fork at: the tape's answers for steps 1..N are replayed, the request after step N is checked, then the run goes live. `0` checks the first request and goes live at once. |
| `continuations` | k live continuations (1 by default, at most 10), each its own trial (`<fork>.<n>`, `source: "experiment"`, `fork_of: <run>`). `0` replays the prefix strictly and stops: a check that the run reproduces up to N. |
| `pin_set` | Continue with a pin set's harness lock and environment ([api/trials.md](../api/trials.md)) instead of the run's own harness and model. A different harness diverges at step 1, by design. |
| `on_divergence` | `stop` (the default: the trial ends at the divergence) or `live` (it goes live from there). |
| `budget` | Each continuation's job budget (default `{"max_cost_usd": 5}`). |
| `wait_seconds` | Answer once every prefix has settled, or after this long (0: at once, `status: replaying`). |

`divergence` is `null` when every trial's prefix was clean, else the earliest trial's `{seq, kind, ...}`: `model`
(the requests differ outside tool outputs), `tool` (a tool's output differs: a test that reads the clock), `network`
(a tool output differs and shows a network error: replays have no egress) or `tree` (the working tree after an edit
or command isn't the one the run recorded, [steering.md](../steering/how-it-works.md#trees), checked once the trial has ended).
`status` is `replaying`, `forked`, `verified` (continuations 0), `diverged`, or `unreplayed` when the run's harness
has no model gateway path (only Claude Code and Codex do).

- **Isolation.** Replay sandboxes have egress `none`, no credential (the API key stays behind the gateway), no
  `secrets:`, cloud token, MCP server, GitHub API or build cache, and git fetches through the gateway's fetch-only
  grant. Tools with effects outside the sandbox (MCP tools: Jira, Slack; web fetch and search) are answered from the
  tape, never run again.
- **Tainted tapes** ([model-gateway.md](../model-gateway.md#tapes), `untrusted`) can be forked; the fork and its trials
  say `tainted: true`, and flightdeck doesn't count them as promotion evidence without review.
- **A/A.** A continuation whose prefix was clean and which ran the run's own harness records `aa`: whether it
  reproduced the run's outcome (the original job's check against its own). Flightdeck aggregates them.
- **Limits.** Only a session's first run can be forked (a later turn resumes a conversation a fresh sandbox doesn't
  have), and only while its tape is kept (`HANGAR_MODEL_TAPE_DAYS`, 30). The run's prompt is taken from its tape, so
  the replay's first request is the original's. Cross-harness prefix replay isn't supported.
- **Cost.** Sandbox time for the prefix (the taped steps make no model call); the continuations cost what any trial
  does, against the eval budget ([repo-policy.md](../repo-policy/budgets.md#the-eval-budget)).
