# Prompt caching in sessions

This page is part of [Sessions: picking up where you left off](../sessions.md).

Every model call in a turn re-sends the whole conversation. The provider's prompt cache bills the unchanged prefix at a tenth of the input price, or a twentieth on Opus 5.5 and Sonnet 5.5. A cache entry lives for a fixed time after its last use, 5 minutes by default. Sessions idle for up to 30 minutes, and agents wait on people (`needs_input`, `ask` approvals). Returning to a 150k-token conversation on Opus 5.5 costs $0.03 while the cache is warm and $0.75 once it has expired ([spec 0012 §6.5](../specs/0012-cutting-edge.md#65-prompt-caching-and-long-sessions)). hangar does three things about this:

1. it asks for the right TTL;
2. it keeps one model per session;
3. it lets best-of-N and fork siblings share one cache write.

What it saves shows up in usage ([activity.md](../activity.md#prompt-cache)).

## TTL policy

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

## One model per session

Each model has its own cache, so switching models makes the next turn re-read the whole conversation uncached. The session's model is fixed when the session is created: the model asked for, else the environment's default for the harness. A later change to the environment doesn't move running conversations.

You can still switch. `send_to_session(model=…)` (REST `POST /api/sessions/{name}/send {"model": …}`) switches from that turn on. `steer_turn(model=…)` (REST `…/steer {"model": …}`) switches from the next turn, because a running turn keeps its model. Either way:

- the turn gets a warning in `warnings`;
- the session's events get a `progress` event with `data.warning: "model_switch"`;
- the audit log records `session.model`.

Switching harness (`open_session(harness=…)`) is a handoff anyway, and takes that harness's default model.

## Best-of-N and forks share one cache write

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

## Keeping the cache warm: not done

A session that waits on a person for over 55 minutes could refresh its 1-hour entry with a one-token request. A refresh is billed as a read: $0.03 for 150k tokens on Opus 5.5. hangar doesn't do this, for two reasons:

- **It would miss the cache.** The refresh must match the CLI's own request byte for byte: system prompt, tool definitions, beta headers and cache breakpoints. Only the CLI can build that request. A request hangar built would write a new entry, and so cost a write.
- **It needs the model credential in the control plane.** That credential belongs only in the sandbox (and should eventually leave the sandbox too; spec 0012 item 7).

pi has warming built in (`cacheWarming`, `streaming` by default), but its `idle` mode needs a pi process that outlives the turn. hangar's `pi -p` turns exit when they finish. Revisit when a CLI offers a refresh command, or when hangar keeps one long-lived CLI process per session.
