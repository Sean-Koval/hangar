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
- an agent turn running;
- a terminal open on it (dashboard or `hangar sb attach`);
- anything using the sandbox through hangar (commands, files, MCP tools).

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

In the dashboard, the **Sessions** page does all of this:
- **Resume** a session;
- message the agent;
- **Talk to the agent here**, which opens a terminal in the agent's own conversation;
- open a shell;
- set the idle time.

To work alongside the agent interactively from a terminal:
1. Open the session and attach to its sandbox: `hangar sb attach <sandbox_id>`.
2. Run the `resume_command` that `get` shows, for example `claude --resume 3f2a…`. You're now in the same conversation the agent had.

## Settings

| Variable | Default | |
| --- | --- | --- |
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
