# Sessions: picking up where you left off

A session is a stream of work you can come back to, like a cloud Claude session: the agent's conversation and the repo state are kept; the sandbox is not. Design: [spec 0010](specs/0010-sessions.md).

```bash
# Monday
hangar session new onboarding "build the onboarding flow; start with the signup form" -e early-signal -w
hangar session send onboarding "use the existing Button component" -w

# ...30 idle minutes later hangar checkpoints it and releases the sandbox (costs storage only)...

# Wednesday
hangar session list
hangar session send onboarding "now add the email verification step" -w   # boots, restores, resumes the conversation
```

## What is kept

| Kept | How |
| --- | --- |
| The agent's conversation (Claude Code, Codex, pi) | The harness's own session files; the next turn runs `claude --resume <id>` / `codex exec resume <id>` / `pi --session-id <id>` |
| Commits on the session branch (`hangar/session/<name>`) | A git bundle, even if nothing was pushed |
| Uncommitted changes and untracked files | A binary diff and a tarball (respects `.gitignore`) |
| **Not kept:** running processes (dev servers, tmux windows), ignored files such as `.venv`/`node_modules` | They are rebuilt by the environment |

Checkpoints are taken:
- after every agent turn;
- every 10 minutes while something changes;
- when the session suspends;
- before its sandbox is destroyed for any reason, including expiry.

Checkpoints are encrypted with the vault key.

All four agents resume their own conversation: Claude Code, Codex, pi and Antigravity (`agy --conversation <id>`). If you switch a session to a different agent, the next prompt starts with a short account of the earlier turns instead (a **handoff**).

## Staying open while you work

A session never suspends while it's in use. Any of these keeps it up:
- an agent turn running, or any agent run started in its sandbox (`run_agent`) that hasn't finished;
- a terminal open on it (dashboard or `hangar sb attach`);
- anything using the sandbox through hangar: commands (for their whole run), files, reading terminals, following an agent.

A job's session is held for the whole turn, delivery included, and across a control-plane restart until the job picks it up again.

It suspends only after it has been idle for its idle time: 30 minutes by default. To change that:

```bash
hangar session keep onboarding --idle 240   # four hours
hangar session keep onboarding --idle 0     # stay up until I suspend it
```

## Commands

| CLI | MCP | What it does |
| --- | --- | --- |
| `hangar session new NAME [MESSAGE] -e ENV [--repo URL]` | `new_session` | Start a session (and a first turn, given a message) |
| `hangar session list` | `list_sessions` | Streams of work, most recently active first |
| `hangar session send NAME MESSAGE` | `send_to_session` | Continue: resumes the session if suspended, then runs a turn |
| `hangar session open NAME` | `open_session` | Boot and restore without running the agent: work in the sandbox yourself |
| `hangar session keep NAME --idle MIN` | `keep_session` | Idle time before suspending (0 = never) |
| `hangar session suspend NAME` | `suspend_session` | Checkpoint and release the sandbox now |
| `hangar session get NAME` | `get_session` | Turns, summaries, checkpoints, and `resume_command` |
| `hangar session archive NAME` | | Drop the checkpoints (the record stays) |
| `hangar session events NAME [--follow]` | `get_events` | What the agent did: messages, commands, file changes, errors, usage ([activity](activity.md)) |
| `hangar session new NAME MESSAGE --from-local` | `new_session(context=…)` | Continue your local checkout in the cloud: its unpushed commits and uncommitted changes go with the first turn |
| `--file PATH`, `--text TEXT`, `--schema FILE` on `new` and `send` | `context`, `output_schema` | Attachments, and a JSON Schema for a structured result |
| | `wait_for_turn` | Block until the turn ends, with progress notifications; returns result, question, artifacts |
| `hangar session artifacts NAME [--get PATH]` | `get_artifact` | Files the agent handed back |
| `hangar session patch NAME` | `get_session_patch` | The session's work as one patch against its base |
| `hangar session pull NAME [--checkout]` | | Its commits onto local branch `hangar/NAME`, plus (with `--checkout`) its uncommitted changes |
| `hangar session fork NAME [MESSAGE] -n N` | `fork_session` | Branch it into N sessions (below) |
| `hangar session steer NAME MESSAGE` (or `send`) | `steer_turn` | Tell the running agent something; it reads it at its next step ([steering](steering.md)) |
| `hangar session interrupt NAME [MESSAGE]` | `steer_turn(interrupt=true)` | Stop the running turn, or stop and carry on with a new message |
| `hangar session approvals NAME [--allow ID \| --deny ID]` | `answer_approval` | Tool calls the agent waits on under `--approvals ask` |

`hangar session new` takes a per-turn budget (`--max-cost`, `--max-tokens`, `--max-minutes`; `budget` in the API). A turn that crosses it is stopped with outcome `over_budget`. `get` shows each turn's `usage` and the session's total.

If the agent asks a question (`hangar-agent ask`), the turn ends as `needs_input` and the session shows it in `needs_input`; your next `send` answers it. How context moves in and out of a session is in [context exchange](context-exchange.md).

In the dashboard, the **Sessions** page does all of this:
- **Resume** a session;
- message the agent;
- **Talk to the agent here**, which opens a terminal in the agent's own conversation;
- open a shell;
- set the idle time.

To work alongside the agent interactively from a terminal:
1. Open the session and attach to its sandbox: `hangar sb attach <sandbox_id>`.
2. Run the `resume_command` that `get` shows, for example `claude --resume 3f2a…`. You're now in the same conversation the agent had.

## Forks and best-of-N

Fork a session to try several directions from the same point:

```bash
hangar session fork onboarding -n 3 "try the email step with magic links instead" -w
hangar session fork onboarding -n 2 --name onboarding-a --name onboarding-b   # no message: just boot them
```

MCP `fork_session(session, n, message?, harness?, names?)`; REST `POST /api/sessions/{name}/fork` with `{"n": 3, "message": "..."}`.

Each fork is a new session:
- `parent_id` names the session it came from, and `forked_from_checkpoint` names the checkpoint it started from. If the parent's sandbox is up and anything changed since its last checkpoint, hangar takes a fresh one first. A parent that is mid-turn can't be forked; wait for the turn to finish.
- It has the parent's commits, uncommitted changes, untracked files and conversation files, on its own branch `<parent branch>-fork-<i>`, in its own sandbox. It keeps the parent's turns as history, with their artifacts (still downloadable from the fork), the parent's open question if it had one, and `get_session_patch` works on it before it ever boots. The inherited turns carry no usage: a fork's `usage` is what the fork spent.
- **The conversation forks too.** The fork's first turn branches the agent's conversation with the CLI's own fork: each fork remembers everything up to the fork, and the forks diverge from there. Later turns resume the fork's own conversation.

  | Harness | Fork (verified 2026-10-13) |
  | --- | --- |
  | Claude Code 2.1.296 | `claude --resume <parent> --fork-session --session-id <new>` (`--session-id` with `--resume` is only accepted together with `--fork-session`) |
  | Codex 0.162.1 | `codex exec fork <parent> [prompt]`; the new thread id is read from `thread.started`. The forked rollout refers to the parent's (`forked_from_id`), which the checkpoint carries along. |
  | pi 1.1.0 | `pi --fork <parent> --session-id <new>` |
  | Antigravity 1.3.3 | No fork flag. The fork starts a new conversation with a summary handoff. |

  The same handoff is used when you fork into another harness (`--harness`).
- Two sessions never continue the same conversation id. If two sessions ever hold one id, the oldest keeps it, and the other forks it on its next turn instead of resuming it.

A job with `strategy: best_of_n` is built on this: it forks its prepared session into N attempts and delivers the best one. See [Jobs §6](jobs.md#6-forks-and-best-of-n).

**Cost:** every fork is its own sandbox while it runs: N forks cost N times one session. They suspend when idle like any session, and a suspended fork costs storage only (each has its own copy of the checkpoint).

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_FORK_MAX` | 8 | Most forks per `fork_session` call |
| `HANGAR_SESSION_IDLE_MINUTES` | 30 | Suspend after this long idle (0 = never); per session with `--idle` |
| `HANGAR_SESSION_CHECKPOINT_MINUTES` | 10 | Periodic checkpoint while changed |
| `HANGAR_SESSION_KEEP_CHECKPOINTS` | 5 | Per session |
| `HANGAR_SESSION_MAX_UNTRACKED_MB` / `_MAX_PART_MB` | 20 / 100 | Larger parts are skipped with a warning |
| `HANGAR_SESSION_TURN_TIMEOUT_MINUTES` | 120 | |
| `HANGAR_SESSION_STORE` | data volume | `gs://bucket[/prefix]` for durable checkpoints on GKE (terraform output `session_bucket`) |

## Cost and speed

- **Suspended session:** a few MB on the control plane's volume.
- **Resume:** a sandbox start plus the restore (seconds for typical sizes). That's about 10 s from a warm pool, or 30–60 s cold when the image is built and a node is up.
- **Keeping a sandbox running overnight instead:** it pins a sandbox node, roughly $280/month on demand for an n2-standard-8.
- **Faster cold starts on GKE (opt-in, recreates the sandbox nodes):** terraform `image_streaming = true` starts pods before the image is fully pulled, and `sandbox_disk_gb = 100` lets more nodes fit the default 500 GB SSD quota. Suspend sessions before applying; they resume from their checkpoints.
- **Durability:** with `HANGAR_SESSION_STORE=gs://…`, checkpoints survive the control plane's disk. Uploads retry on throttling and server errors. The bucket has no age-based deletion unless you set `session_retention_days`.
