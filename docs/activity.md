# Activity, usage and budgets

While an agent works, hangar reads its run log and turns it into one stream of events, whichever harness it is (Claude Code, Codex, pi or Antigravity). It also counts the tokens and money each turn uses, and stops a turn that goes over its budget. This is workstream C of [spec 0011](specs/0011-factory-roadmap.md); it builds on 0007 M3 and M6 and the usage part of 0003 M9.

## Events

Every job and session has an event stream. Each event looks like this:

```json
{"seq": 41, "ts": 1791866712.4, "turn": 2, "run_id": "run-3fa91c20", "kind": "command",
 "text": "uv run pytest -q", "data": {"id": "toolu_01…", "tool": "Bash"}}
```

| `kind` | What it is |
| --- | --- |
| `message` | Something the agent said (assistant text, up to 2,000 characters) |
| `command` | A shell command it started (`Bash`, `bash`, `run_command`, Codex `command_execution`) |
| `tool_call` | Any other tool it called (`Read`, `Write`, `apply_patch`, MCP tools…) |
| `tool_result` | A tool's result: `data.ok`, the start of its output, and `exit_code` for Codex commands |
| `file_change` | A file it created or edited (`data.path`, `data.op`) |
| `error` | A model or harness error, or hangar stopping the turn (`stopped: over budget (…)`) |
| `usage` | Tokens and cost so far for the turn; `data.final` once the harness reports its own totals |
| `progress` | The agent started, or reported progress (workstream E feeds `$HANGAR_IO/out/progress.jsonl` here) |

Event text is redacted (`broker.redact`) and kept short. `data` holds only small scalar fields.

Read events with `seq` as the cursor. Pass `since` as the last `seq` you saw, and you get only newer events:

| | |
| --- | --- |
| REST | `GET /api/jobs/{id}/events?since=&limit=&tail=` (`jobs` tokens see their own jobs) and `GET /api/sessions/{ref}/events?since=&limit=&tail=`. They return `{"events": [...], "next": <seq>}`. `tail=N` gives the last N. |
| MCP | `get_events(job=… or session=…, since=0, limit=100)` |
| CLI | `hangar job events ID [--follow] [--since N] [--json]` and `hangar session events NAME [--follow]` |
| Dashboard | The **Activity** list on the Jobs and Sessions pages |
| Python | `SessionService.events(ref, since, limit, tail)` and `JobService.events(...)`. `SessionService.activity.append(session_id, [RunEvent(kind="progress", …)])` adds events. |

### How events are collected

The session's poll loop in `end_turn` already checks every few seconds whether the agent is done. On the same tick it reads the bytes the run log has gained since the last read, in a single `exec`: `tail -c +OFFSET | head -c 128K | base64`. It never re-reads the whole log. The parsers in `events.py` take those bytes, keep any unfinished line for the next read, and skip lines over 1 MiB (they are counted).

Events go to `<data_dir>/events/<session id>.jsonl`. Past `HANGAR_EVENTS_MAX_MB` the file rotates once, so the newest `2 × HANGAR_EVENTS_MAX_MB` are kept. The read position, parser state and running usage are saved on the turn (`SessionTurn.event_cursor`), along with the rest of the session.

When the turn ends, hangar reads the log once more. A last line without a newline is parsed then.

After a control-plane restart, the session re-attaches to the running turn and continues from the saved position. Events are written before the position is saved. So if hangar went down between the two, the same lines are read again. Their events aren't recorded twice, because every event carries the byte offset of its source line. Their tokens aren't counted twice either, because the saved usage matches the saved position.

### Formats

The parsers were written against real output of each CLI. `tests/fixtures/streams/` holds that output, and `capture/capture.sh` re-records it offline: each CLI runs exactly as hangar runs it, against a scripted local model, so no real model API is called. The fixtures were recorded with Claude Code 2.1.296, codex 0.162.1, pi 1.1.0 and agy 1.3.3.

| Harness | Output | Usage it reports |
| --- | --- | --- |
| Claude Code | `--output-format stream-json --verbose`: `system/init`, one `assistant` line per content block (all repeating the message's usage), `user` tool results with `tool_use_result`, then `result` | Per message while it runs (output tokens show as 1 until the end). `result` has the turn's totals and `total_cost_usd`, which hangar takes as the cost. |
| Codex | `exec --json`: `thread.started`, `item.started`/`item.completed` (`command_execution`, `file_change`, `agent_message`, `error`), `turn.completed`/`turn.failed`, `error` | Only in `turn.completed`, at the end, with no cost. `input_tokens` includes cached tokens. Hangar prices it with the turn's model. |
| pi | `--mode json`: `session`, `message_end` (assistant messages carry usage and cost), `tool_execution_start`/`_end` | Per message, priced by pi from its own catalog |
| Antigravity | `--output-format stream-json`: `init`, `step_update` (`agent_response` with usage, `tool` steps ACTIVE then DONE/ERROR, `error_message`), `result` | Per model step and totals in `result`, with no cost. The stream doesn't name the model, so hangar prices it with the turn's model. |

A harness that isn't one of these is detected from its first JSON line. Output that matches none of them produces no events; everything else still works.

## Usage

Every turn records `usage`: `input_tokens` (not counting the cache), `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `cost_source` (`harness` or `prices`) and `model`. The turn's usage is live while it runs. Jobs and sessions add theirs up in `usage`, shown by `get_job`, `get_session`, `hangar job submit --wait` and `hangar session send`. A job's final GitHub comment and its PR body include a `Usage:` line.

The cost is the harness's own figure when it reports one (Claude Code's `total_cost_usd`, pi's per-message cost). Otherwise hangar computes it from a price table in USD per million tokens. The built-in table in `events.py` has the list prices of current Anthropic, OpenAI and Google models, taken from their pricing pages on 2026-10-13. `HANGAR_MODEL_PRICES` overrides entries or adds new ones:

```bash
HANGAR_MODEL_PRICES='{"gpt-5.5": {"input": 5, "output": 30, "cache_read": 0.5}, "my-vllm-model": {"input": 0, "output": 0}}'
```

A model id matches a table entry exactly, or as that entry followed by a date suffix (`claude-sonnet-4-5-20250929`). Provider prefixes such as `anthropic/` are ignored. If the model isn't in the table, its tokens still count but `cost_usd` is `null`, meaning unknown rather than zero.

The `tokens` total that budgets use leaves out cache reads. Every model call re-reads the cached context, so cache reads would dwarf everything else. They are still counted (`cache_read_tokens`) and priced.

### Prompt cache

Usage also says how well the provider's prompt cache worked. [Sessions](sessions.md#prompt-caching) explains how hangar chooses each turn's cache TTL.

| Field | |
| --- | --- |
| `cache_write_1h_tokens` | The part of `cache_write_tokens` written with a 1-hour TTL. Claude Code reports it in `usage.cache_creation.ephemeral_1h_input_tokens` and pi as `cacheWrite1h`. These tokens are priced at 2x input, not 1.25x. |
| `cache_hit_ratio` | `cache_read_tokens / (input_tokens + cache_read_tokens + cache_write_tokens)`: the share of prompt tokens read from the cache. A multi-turn session should stay above 0.85. |
| `saved_usd` | What caching saved compared with sending the same tokens as plain input: `reads × (input − read price) − 5m writes × (5m write − input) − 1h writes × (1h write − input)`. It comes from the price table even when the harness reports its own cost. It goes negative when a turn writes cache entries that nothing reads back. It is `null` when the model has no price. |

Turns, sessions and jobs (`get_session`, `get_job`) carry these fields, as do the `usage` events and `/api/usage` rows. Each turn also records `cache_ttl`, the TTL it asked for, and `cache_wait_seconds`, how long it waited for a best-of-N or fork sibling.

Example: Opus 5.5 at $4 input, $0.20 read, $5 5m write and $8 1h write per MTok. A turn reads 9,000 cached tokens, writes 1,000 at 1h and sends 10 new ones. It costs $0.0098 (output aside), not the $0.0400 the same 10,010 tokens would cost uncached, so it saved `9000 × 3.80 − 1000 × 4 = $0.0302`. Its hit ratio is 0.90.

The cost report:

```bash
hangar usage --by session --cache --since 7d
SESSION                           TURNS    HIT     READ    WRITE       1H       COST      SAVED
csv-fix                              12    93%     4.1M   210.0k   210.0k      $2.31     $14.74
onboarding-fork-2                     3    41%   180.0k   250.0k   250.0k      $1.48     -$0.32
```

`--by` takes `day`, `model`, `environment`, `requester`, `repo`, `harness`, `session`, `job` or `cache_ttl`. A session with a low hit ratio and little saved is paying for writes it never reads back. Common causes are a model switch, a TTL shorter than its gaps, or forks started together.

## Budgets

A budget caps each agent turn. That means the job's first run and each follow-up separately, and each message to a session. A best-of-N job's budget caps each attempt and the judge separately, so the job as a whole can spend up to N + 1 budgets. To bound a whole job, give it a `job_budget`, and to bound a repo's day, a daily cap: see [repo-policy.md](repo-policy.md#budgets). A turn's structured-result repair run counts toward that turn: it is stopped when the turn as a whole goes over, and its usage is the turn's `repair_usage`, included in every total.

```json
"budget": {"max_minutes": 30, "max_tokens": 2000000, "max_cost_usd": 5}
```

You can set it in several places:

- `budget` on `POST /api/jobs`, `submit_job` and `POST /api/sessions` / `new_session`;
- `--max-cost`, `--max-tokens` and `--max-minutes` on `hangar job submit` and `hangar session new`;
- defaults for any field left unset: `HANGAR_BUDGET_MAX_MINUTES`, `HANGAR_BUDGET_MAX_TOKENS`, `HANGAR_BUDGET_MAX_COST_USD` (all unset by default).

A `jobs` token can carry a cap: `hangar token create svc --scope jobs --max-cost 2`, or `max_budget` on `POST /api/tokens`. Its jobs never get more than the cap, whatever they ask for, and the cap also applies when they ask for nothing.

Hangar checks the budget on every poll. When a turn crosses it, hangar stops the agent the way a timeout does (its tmux session is killed). It records the turn's `outcome: "over_budget"` and the reason in `outcome_reason`, for example `spent $1.26; the budget is $1.00`, and adds an `error` event. A job then:

- verifies;
- commits what the agent left;
- pushes and opens the PR as a **draft**;
- finishes as `failed` with `reason: "over_budget"`.

The GitHub comment says so: "Stopped over budget (…). What the agent had done so far is delivered as a draft."

How far a turn overshoots depends on when the harness reports usage:

- **Claude Code, pi and Antigravity** report usage per model call. They are stopped within one poll (`HANGAR_SESSION_POLL_SECONDS`) of the call that crosses the budget.
- **Codex** reports usage only when its turn ends. For Codex, only `max_minutes` takes effect mid-turn.
- **A model with no price** never trips `max_cost_usd`. Use `max_tokens` for it, or add the model to `HANGAR_MODEL_PRICES`.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_MODEL_PRICES` | `{}` | JSON, USD per million tokens; merged over the built-in table |
| `HANGAR_BUDGET_MAX_MINUTES` / `_MAX_TOKENS` / `_MAX_COST_USD` | unset | Default budget for each turn |
| `HANGAR_EVENTS_MAX_MB` | 20 | Event log size per session before it rotates |
| `HANGAR_CACHE` | `auto` | Prompt-cache TTL when neither the session nor its environment sets one ([sessions.md](sessions.md#prompt-caching)) |
| `HANGAR_CACHE_STAGGER_SECONDS` | 15 | How long best-of-N attempts and forks hold back for the first one to answer (0 = no wait) |
