# Streaming to MCP and REST clients

Long-running work streams to clients as it happens instead of being polled for: a command's output as it prints,
background processes you can attach to, a session's or job's events as the agent produces them, and file changes in
a sandbox. Everything here goes through the sandbox's daemon (hangar-envd on Docker and Kubernetes, E2B's own envd on
e2b; see [envd.md](envd.md)), and falls back to the provider's exec when there is none.

## Commands: `exec(stream=true)`

Over MCP, `exec` with `stream: true` sends each chunk of output as a `notifications/progress` on the call, in order
(`progress` is the bytes so far, `message` the text, stdout and stderr interleaved as a terminal shows them), then
returns exec's usual result: `exit_code`, the last 256 KiB of `stdout` and `stderr`, and `streamed`. Ask for progress
the usual way (a `progressToken` in the call's `_meta`; the SDKs do it when you pass a progress callback).

The 2026-07-28 revision has no other way to stream a tool's output: tool results are single responses, and
request-scoped notifications (progress, log messages) are what flows on a call's response stream
([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog), item 4). Progress works the same
way for 2025-era clients.

```bash
# REST: Server-Sent Events. `stdout` / `stderr` events ({"data": text}) as it prints, then one `result` event.
curl -N -H "Authorization: Bearer $HANGAR_TOKEN" -H 'Content-Type: application/json' \
  -d '{"command": "npm test", "timeout": 600}' "$HANGAR_URL/api/sandboxes/api-refactor/exec/stream"
```

A command whose client goes away (the call cancelled, the connection closed), or whose token is revoked, is killed.
One cut off by a shutdown or a destroyed sandbox isn't: it runs to its end or its timeout, like an unstreamed exec,
and the result says `closed` and why.

Without a daemon the command runs through the provider's exec and its output arrives in one piece at the end
(`streamed: false`).

## Background processes: `process`

E2B's `commands.run(background=True)`, `connect`, `send_stdin`, `kill` and `list`, as one MCP tool:

| `action` | Arguments | |
| --- | --- | --- |
| `start` | `command`, `cwd` | Starts it (`bash -lc`) and returns its `process_id` at once |
| `attach` | `process_id`, `since`, `wait_seconds` (up to 600) | Output from `since` (default: all still kept), then whatever comes for `wait_seconds`, as progress notifications; returns the tail (64 KiB), `state`, `exit_code` and `next` (pass it as `since` next time) |
| `input` | `process_id`, `input`, `eof` | Writes to its stdin; `eof` closes it |
| `kill` | `process_id` | SIGTERM to its process group, SIGKILL 3 s later if it is still running |
| `list` | | Your processes in the sandbox |

hangar follows each process and keeps the last 256 KiB of its output (stdout and stderr together) while nobody is
attached, so attaching later shows what it printed. A process belongs to the token that started it: no other token
can list, attach to, write to or kill it. Exited processes stay listed for 10 minutes.

REST: `POST /api/sandboxes/{ref}/processes` (`{"command", "cwd"}`), `GET .../processes`, `GET .../processes/{id}?since=`
(the buffered output, for polling), `GET .../processes/{id}/stream?since=` (SSE: `output` events with `id` = the
offset, so `Last-Event-ID` resumes; then `end`), `POST .../processes/{id}/input` (`{"input", "base64", "eof"}`),
`DELETE .../processes/{id}`.

After a control-plane restart, processes keep running in their sandboxes. hangar finds a token's processes again by
their envd tag (`hangar:<id>:<MAC>`, a MAC of the owner, sandbox and id under the control plane's key, so the agent,
which can start processes through its own daemon, can't make one up for a token) the next time that token lists or
attaches; their earlier output is gone (`adopted: true`).

Without a daemon a process runs detached (`setsid`), its output goes to `~/.hangar/bg/<id>.out` and hangar polls it
(every 0.5 s while someone is attached, 5 s otherwise). `input` needs the daemon.

Background processes and streamed commands run as the sandbox user, like `exec`. On Docker they don't get
`no_new_privs`, unlike a turn's harness: they are commands hangar runs as the agent outside a turn
([what the split doesn't cover](steering.md#the-uid-split)). On GKE the pod's `allowPrivilegeEscalation: false`
covers them.

## Turn events, live

A session's turn watcher used to read the run log every `session_poll_seconds` (5 s). It now follows the log through
the daemon (`tail -F`, started after the first poll), so each event is recorded, and reaches live readers, as the
agent writes it. Polling takes over whenever following isn't possible: no daemon, the log truncated or replaced
(`tail` says so on stderr), the stream breaking, or the log passing `HANGAR_STREAM_MAX_BYTES` (following is paced
like any stream; past the cap polling's bounded reads carry on). The read position is saved with the turn as before, so a restart
resumes without recording an event twice. `HANGAR_STREAM_TURN_LOGS=false` turns following off.

In a split sandbox ([the uid split](steering.md#the-uid-split)) the follower tails hangar's own copy of the run log,
never the agent's link to it. The daemon runs as the agent, so what it passes on is informational, like the rest of
the log: approvals still come from the driver's own file, and the turn's status, exit code and summary from hangar's
own reads.

Following them:

- **SSE**: `GET /api/sessions/{ref}/events/stream?since=<seq>` and `GET /api/jobs/{id}/events/stream?since=<seq>`
  (a `jobs` token sees only its own jobs). Each event is `id: <seq>` and `data: <event JSON>`; reconnecting with
  `Last-Event-ID` resumes. A comment every 15 s keeps proxies from timing the connection out.
- **MCP**: subscribe to `hangar://sessions/<name>/events` or `hangar://jobs/<id>/events` (`subscriptions/listen`, or
  `resources/subscribe` for 2025-era clients). New events send `notifications/resources/updated` as they are recorded
  (at most every 250 ms per URI); read `hangar://sessions/<name>/events?since=<seq>` for just the new ones.

## The Fleet, live

`GET /api/fleet/stream` is the Fleet page's read (`GET /api/fleet`) as it changes: a `snapshot`, then `agent` (one
unit, at most one per agent every 2 s), `gone`, `inbox` and `summary` events, resumable with `Last-Event-ID`. A
browser gets a one-time ticket from `POST /api/fleet/tickets` (any `user` token: the stream shows that person's
fleet). See [the fleet stream](fleet-api.md#the-fleet-stream).

## File watches

Subscribe to `hangar://sandboxes/<sandbox>/watch?path=<dir>[&recursive=true]` and hangar watches that directory
(envd's `WatchDir`, inotify) for as long as you stay subscribed. Each change sends
`notifications/resources/updated`; reading the URI returns the latest events (`{"seq", "ts", "name", "type"}`, type
`create`, `write`, `remove`, `rename` or `chmod`, the last 200 kept) and `next`; add `&since=<next>` for only newer
ones. `path` is absolute or relative to the sandbox's repo. Without a daemon the resource says the watch needs one.
REST: `GET /api/sandboxes/{ref}/watch?path=...&recursive=true` (SSE).

## Limits and closing

| Setting | Default | |
| --- | --- | --- |
| `HANGAR_STREAM_MAX_PER_TOKEN` | 16 | Open streams per token: streamed execs, attaches, event and watch streams. One more is refused (MCP error, REST 429). |
| `HANGAR_STREAM_MAX_TOTAL` | 512 | Open streams in all, hangar's own included |
| `HANGAR_STREAM_BYTES_PER_SECOND` | 1 MiB | Per stream; a command printing faster is slowed down, as by a slow terminal (envd's queue is bounded). Each message also counts 64 bytes, so output a byte at a time can't become a flood of notifications |
| `HANGAR_STREAM_MAX_BYTES` | 64 MiB | Per stream; past it nothing more is sent (the result still has the end of the output) |
| `HANGAR_STREAM_MAX_HOURS` | 12 | An event or watch stream open longer is closed |
| `HANGAR_BACKGROUND_MAX_PER_TOKEN` | 16 | Running background processes per token |
| `HANGAR_WATCH_MAX_PER_TOKEN` | 8 | Directory watches per token (envd allows 64 per sandbox) |
| `HANGAR_STREAM_RECHECK_SECONDS` | 5 | How often open streams check that their token is still valid |
| `HANGAR_STREAM_TURN_LOGS` | true | Follow running turns' logs through the daemon |

Every stream ends, with what it has so far and why (`closed`, or an SSE `close` event), when:

- its sandbox is destroyed or paused;
- the token that opened it is revoked (static tokens, `mcp`-scoped tokens and OAuth access tokens alike; checked
  every `HANGAR_STREAM_RECHECK_SECONDS`): streamed commands and background processes it started are killed;
- hangar shuts down (#57): streams end at once rather than holding the pod for the grace period, `subscriptions/listen`
  streams get their final result, and new streams are refused until the next pod answers;
- it hits a limit above.

## Observability

Each stream is a span (`stream exec`, `stream events`, `stream watch`, `stream process`, `stream turn_log`, ...) with
its bytes and why it ended. Metrics:

| Metric | |
| --- | --- |
| `hangar_streams_open{kind}` | Open streams |
| `hangar_streams{kind, reason}` | Streams closed: `done`, `client`, `shutdown`, `destroyed`, `paused`, `revoked`, `lifetime`, `error` |
| `hangar_stream_bytes{kind}` | Bytes streamed |
| `hangar_stream_first_output_seconds{kind}` | From a stream's start to its first output |

## Numbers

Docker, 5 runs of a command printing a line every 100 ms for 3 s (`for i in $(seq 30); do sleep 0.1; echo ...`),
each line stamped with the time it was printed (`scripts/bench/streams.py`,
`bench/results/2026-10-13-streams-docker.json`, a 2-vCPU VM):

| How the client sees output | First line (p50 / p95) | Each line's delay (p50 / p95) |
| --- | --- | --- |
| `exec(stream=true)`, progress notifications | **112 / 117 ms** | **0.8 / 2.0 ms** |
| `process` start + attach | 111 / 116 ms | 0.9 / 2.3 ms |
| `exec`, the result at the end (before) | 3,124 / 3,132 ms | 1,553 / 2,909 ms |
| `terminal_open` + `terminal_read` every second (before) | 1,058 / 1,063 ms | 503 / 981 ms |

The first line is printed 100 ms after the start, so streaming shows it about 12 ms after it exists.

Turn events, from a run log line written to its event recorded, for a real Claude Code log replayed a line every
100 ms: **15 / 22 ms** following the log, against 4,727 / 4,936 ms polling every `session_poll_seconds` (5 s).
With tail's default 1 s interval, following was 714 ms p50 in a container (on the host it is instant), consistent with
GNU tail polling the file rather than using inotify on the container's overlayfs; hangar runs it with `-s 0.1`.
