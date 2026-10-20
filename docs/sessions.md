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

A job with `strategy: best_of_n` is built on this: it forks its prepared session into N attempts and delivers the best one. See [Jobs §6](jobs/working-with-the-agent.md#6-forks-and-best-of-n).

**Cost:** every fork is its own sandbox while it runs: N forks cost N times one session. They suspend when idle like any session, and a suspended fork costs storage only (each has its own copy of the checkpoint).

### Fork at a step

A session fork starts from a checkpoint, and checkpoints exist only at turn boundaries; background shells and
subagent contexts aren't in the event log either. To fork *inside* a turn, hangar rebuilds the state at step N by
re-execution (#179): a fresh sandbox at the run's base commit replays the run's model tape
([model-gateway.md](model-gateway.md#replay)) for steps 1..N, running every tool call again for real and checking
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
| `pin_set` | Continue with a pin set's harness lock and environment ([api/trials.md](api/trials.md)) instead of the run's own harness and model. A different harness diverges at step 1, by design. |
| `on_divergence` | `stop` (the default: the trial ends at the divergence) or `live` (it goes live from there). |
| `budget` | Each continuation's job budget (default `{"max_cost_usd": 5}`). |
| `wait_seconds` | Answer once every prefix has settled, or after this long (0: at once, `status: replaying`). |

`divergence` is `null` when every trial's prefix was clean, else the earliest trial's `{seq, kind, ...}`: `model`
(the requests differ outside tool outputs), `tool` (a tool's output differs: a test that reads the clock), `network`
(a tool output differs and shows a network error: replays have no egress) or `tree` (the working tree after an edit
or command isn't the one the run recorded, [steering.md](steering.md#trees), checked once the trial has ended).
`status` is `replaying`, `forked`, `verified` (continuations 0), `diverged`, or `unreplayed` when the run's harness
has no model gateway path (only Claude Code and Codex do).

- **Isolation.** Replay sandboxes have egress `none`, no credential (the API key stays behind the gateway), no
  `secrets:`, cloud token, MCP server, GitHub API or build cache, and git fetches through the gateway's fetch-only
  grant. Tools with effects outside the sandbox (MCP tools: Jira, Slack; web fetch and search) are answered from the
  tape, never run again.
- **Tainted tapes** ([model-gateway.md](model-gateway.md#tapes), `untrusted`) can be forked; the fork and its trials
  say `tainted: true`, and flightdeck doesn't count them as promotion evidence without review.
- **A/A.** A continuation whose prefix was clean and which ran the run's own harness records `aa`: whether it
  reproduced the run's outcome (the original job's check against its own). Flightdeck aggregates them.
- **Limits.** Only a session's first run can be forked (a later turn resumes a conversation a fresh sandbox doesn't
  have), and only while its tape is kept (`HANGAR_MODEL_TAPE_DAYS`, 30). The run's prompt is taken from its tape, so
  the replay's first request is the original's. Cross-harness prefix replay isn't supported.
- **Cost.** Sandbox time for the prefix (the taped steps make no model call); the continuations cost what any trial
  does, against the eval budget ([repo-policy.md](repo-policy.md#the-eval-budget)).

## Prompt caching

Every model call in a turn re-sends the whole conversation. The provider's prompt cache bills the unchanged prefix at a tenth of the input price, or a twentieth on Opus 5.5 and Sonnet 5.5. A cache entry lives for a fixed time after its last use, 5 minutes by default. Sessions idle for up to 30 minutes, and agents wait on people (`needs_input`, `ask` approvals). Returning to a 150k-token conversation on Opus 5.5 costs $0.03 while the cache is warm and $0.75 once it has expired ([spec 0012 §6.5](specs/0012-cutting-edge.md#65-prompt-caching-and-long-sessions)). hangar does three things about this:

1. it asks for the right TTL;
2. it keeps one model per session;
3. it lets best-of-N and fork siblings share one cache write.

What it saves shows up in usage ([activity.md](activity.md#prompt-cache)).

### TTL policy

`cache: auto | 5m | 1h | off`. hangar takes the first of these that is set:

1. the session's (`new_session(cache=…)`, `POST /api/sessions {"cache": "1h"}`);
2. its environment's (`cache:` in the env spec);
3. `HANGAR_CACHE` (default `auto`).

`auto` asks for **1h** when the session expects to wait more than 5 minutes for its next message:

- its idle time (`idle_minutes`, a job's `keep_minutes`) is over 5 minutes, or 0 (never suspends);
- its tool approvals are `ask` and `HANGAR_APPROVAL_TIMEOUT_SECONDS` is over 5 minutes;
- its agent has asked a question before (`needs_input`). The TTL has to be set before the wait: the turn that asks is the one whose cache entries must survive until the answer arrives.

The harness must also support 1h for the model. Otherwise `auto` asks for **5m**. Each turn records the TTL it asked for in `cache_ttl`.

The TTL lives on the provider, not in the sandbox. A session that suspends and resumes in a new sandbox within the hour still reads its cache: a resumed Claude Code conversation keeps the system prompt it started with.

| Harness (verified 2026-10-13) | `5m` | `1h` | `off` | Notes |
| --- | --- | --- | --- | --- |
| Claude Code 2.1.296 | `CLAUDE_CODE_PROMPT_CACHE_TTL=5m` | `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` | `DISABLE_PROMPT_CACHING=1` | See below. |
| pi 1.1.0 / 1.2.0 | `PI_CACHE_RETENTION=short` | `PI_CACHE_RETENTION=long` | no setting: pi's default | `long` is `ttl: "1h"` on Anthropic models. On OpenAI models it is 24h retention (GPT-5.5 and earlier) or the 30-minute TTL (GPT-5.6+), where the model has it. |
| Codex 0.162.1 | – | – | – | OpenAI caches automatically. Codex sends `prompt_cache_key` (the thread id) and no retention option, and has no flag or config key for one. GPT-5.6+ keeps entries at least 30 minutes, the only TTL; writes cost 1.25x input and reads 0.1x (0.05x on gpt-6.1-sol). |
| Antigravity 1.3.3 | – | – | – | No flag, setting or variable. Gemini caches implicitly. |

More on Claude Code:

- **Main conversation only.** The TTL applies to the main conversation. Subagents keep 5 minutes.
- **Its own default.** Without the variable, Claude Code asks for 1h only on a subscription within plan usage. An API key or a cloud provider gets 5m.
- **What was checked.** A scripted Messages API received exactly these:
  - by default, `cache_control` with no `ttl`;
  - with `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`, `"ttl": "1h"`, plus the `extended-cache-ttl-2025-04-11` beta header;
  - with `DISABLE_PROMPT_CACHING=1`, no `cache_control` at all.
- **LLM gateways.** A gateway behind `ANTHROPIC_BASE_URL` must forward that `anthropic-beta` header.
- **Other providers.** On Bedrock, 1-hour availability varies by model; set `cache: 5m` there if requests fail.

**Break-even.** Let `p` be the input price and `r` the cache-read multiplier: 0.05 on Opus and Sonnet 5.5, 0.1 on most others.

- A 1-hour write costs 2p instead of 1.25p, so each token written costs 0.75p more.
- Each gap of 5–60 minutes saves rewriting the context `C` at 1.25p instead of reading it at r·p, a saving of (1.25 − r)·p·C.

So the 1-hour TTL pays for itself once

```
gaps ≥ 0.75 · W / ((1.25 − r) · C)        W = tokens written over the session, C = context at the gaps
```

| Opus 5.5 ($4/MTok input) | Extra for 1h writes | Saved per 5–60 min gap | Gaps to break even |
| --- | --- | --- | --- |
| Nothing expired: W ≈ C = 150k | $0.45 | $0.72 | 0.63, so one gap |
| W = 400k, gaps at C = 150k | $1.20 | $0.72 | 1.7, so two gaps |
| A batch turn that never pauses | +60% on every write | $0 | never, so `auto` picks 5m |

Gaps longer than an hour lose the cache with either TTL. Use `hangar usage --by cache_ttl --cache` to check the choice against real turns.

### One model per session

Each model has its own cache, so switching models makes the next turn re-read the whole conversation uncached. The session's model is fixed when the session is created: the model asked for, else the environment's default for the harness. A later change to the environment doesn't move running conversations.

You can still switch. `send_to_session(model=…)` (REST `POST /api/sessions/{name}/send {"model": …}`) switches from that turn on. `steer_turn(model=…)` (REST `…/steer {"model": …}`) switches from the next turn, because a running turn keeps its model. Either way:

- the turn gets a warning in `warnings`;
- the session's events get a `progress` event with `data.warning: "model_switch"`;
- the audit log records `session.model`.

Switching harness (`open_session(harness=…)`) is a handoff anyway, and takes that harness's default model.

### Best-of-N and forks share one cache write

Best-of-N attempts, and forks started with a message, begin with the same prompt prefix. Anthropic serves a cache entry only once the first response that writes it has begun. Siblings that all start at once therefore each write the whole prefix.

hangar staggers them instead:

- The siblings boot their sandboxes in parallel.
- The first one to begin its turn goes ahead.
- The others hold the agent's start until that turn shows its first model response in the event stream (a message, a tool call, a command or usage), or until `HANGAR_CACHE_STAGGER_SECONDS` (15) after it started.
- Each turn records how long it waited in `cache_wait_seconds`.
- There is no waiting with `cache: off`, or after a control-plane restart (the groups live in memory).

Claude Code's own fan-out holds for 5 s. hangar's default is longer because the first sibling must start its CLI in a sandbox, and its response reaches hangar on the next poll.

Four Opus 5.5 attempts on a 100k-token prefix cost $2.00 when each writes it, and $0.56 when one writes and three read.

Best-of-N attempts all get the same prompt (the task, context and protocol); each attempt's branch is named only at the very end. One gap is left, measured with Claude Code 2.1.296: Claude Code opens the first user message with a `gitStatus` block naming the current branch, ahead of hangar's prompt. Attempts sit on different branches, so for a new best-of-N job the shared prefix ends after the system prompt and tool definitions, and each attempt writes the task text itself (usually a few thousand tokens). `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` removes that block (verified), but it also removes Claude Code's commit and PR instructions, so hangar leaves it on. Two cases are not affected:

- Forks of a session that already has a conversation share all of it, because the resumed conversation keeps the git status it started with.
- Codex and pi don't put the branch in the prompt.

### Keeping the cache warm: not done

A session that waits on a person for over 55 minutes could refresh its 1-hour entry with a one-token request. A refresh is billed as a read: $0.03 for 150k tokens on Opus 5.5. hangar doesn't do this, for two reasons:

- **It would miss the cache.** The refresh must match the CLI's own request byte for byte: system prompt, tool definitions, beta headers and cache breakpoints. Only the CLI can build that request. A request hangar built would write a new entry, and so cost a write.
- **It needs the model credential in the control plane.** That credential belongs only in the sandbox (and should eventually leave the sandbox too; spec 0012 item 7).

pi has warming built in (`cacheWarming`, `streaming` by default), but its `idle` mode needs a pi process that outlives the turn. hangar's `pi -p` turns exit when they finish. Revisit when a CLI offers a refresh command, or when hangar keeps one long-lived CLI process per session.

## Thawed from a harness lock

`hangar harness thaw team-default@r14 --env python "fix the flaky test"` (or `harness_lock` on `POST
/api/sessions`) starts a session from a [harness lock](harness-lock.md#thaw-and-drift): the lock's harness, its
concrete model and its approval mode, on the CLI version `harnesses.lock.yaml` pins (a lock on another version is
refused, with what to do). Its turn 1 is checked against the lock; `SessionTurn.drift` lists what differed (`[]`:
nothing), with a `harness.drift` event and a warning on the turn. Under `HANGAR_HARNESS_DRIFT=fail` turn 1 stops with
outcome `failed`, reason `harness_drift`. Later turns aren't checked: what the agent changes is its behaviour. A
thawed session can't switch harness; start another session for another lock.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_FORK_MAX` | 8 | Most forks per `fork_session` call |
| `HANGAR_CACHE` | `auto` | Prompt-cache TTL (`auto`, `5m`, `1h`, `off`) when neither the session nor its environment sets one |
| `HANGAR_CACHE_STAGGER_SECONDS` | 15 | How long best-of-N attempts and forks hold back for the first sibling to answer (0 = no wait) |
| `HANGAR_SESSION_IDLE_MINUTES` | 30 | Suspend after this long idle (0 = never); per session with `--idle` |
| `HANGAR_SESSION_CHECKPOINT_MINUTES` | 10 | Periodic checkpoint while changed |
| `HANGAR_SESSION_KEEP_CHECKPOINTS` | 5 | Per session |
| `HANGAR_SESSION_MAX_UNTRACKED_MB` / `_MAX_PART_MB` | 20 / 100 | Larger parts are skipped with a warning |
| `HANGAR_SESSION_TURN_TIMEOUT_MINUTES` | 120 | |
| `HANGAR_HARNESS_REPO`, `HANGAR_HARNESS_DRIFT` | unset, `warn` | Sessions thawed from a harness lock (below) |
| `HANGAR_SESSION_STORE` | data volume | `gs://bucket[/prefix]` for durable checkpoints on GKE (terraform output `session_bucket`) |

## Cost and speed

- **Suspended session:** a few MB on the control plane's volume.
- **Resume:** a sandbox start plus the restore (seconds for typical sizes). That's about 10 s from a warm pool, or 30–60 s cold when the image is built and a node is up.
- **Keeping a sandbox running overnight instead:** it pins a sandbox node, roughly $280/month on demand for an n2-standard-8.
- **Faster cold starts on GKE (opt-in, recreates the sandbox nodes):** terraform `image_streaming = true` starts pods before the image is fully pulled, and `sandbox_disk_gb = 100` lets more nodes fit the default 500 GB SSD quota. Suspend sessions before applying; they resume from their checkpoints.
- **Durability:** with `HANGAR_SESSION_STORE=gs://…`, checkpoints survive the control plane's disk. Uploads retry on throttling and server errors. The bucket has no age-based deletion unless you set `session_retention_days`.
