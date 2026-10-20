# The Fleet API

The [console's Fleet page](console.md) reads one server-side join of sandboxes, jobs and sessions, and a stream that keeps it current. Other clients, such as the decision deck, can use the same two endpoints.

## The fleet read: `GET /api/fleet`

The page is one read (KAN-140). Where the console used to fetch sandboxes, jobs and sessions and join them in the
browser, `GET /api/fleet` joins them on the server, from what is already in memory, and adds what each agent is doing
right now. An admin's token sees every sandbox; a person's own `user` token their work, as `GET /api/jobs` does. Text
passes through the same redaction as every other view. Query: `?owner=<person id>` (or `none` for sandboxes nobody
owns) keeps one person's units, the Owners filter; `?include_stopped=true` adds settled sandboxes.

```json
{"at": 1760900000.0, "person": "sean",
 "counts": {"approval": 1, "failed": 0, "denied": 0, "running": 2, "queued": 0, "off": 0},
 "owners": [{"id": "sean", "name": "Sean", "count": 2}, {"id": "none", "name": null, "count": 1}],
 "slots": {"used": 3, "max": 120},
 "inbox": {"counts": {"approval": 1}, "items": [{"id": "inb-…", "kind": "approval", "…": "as GET /api/inbox"}]},
 "units": [{
   "id": "sb-7f3a…3mx9", "tail": "3mx9", "name": "checkout-python-2", "environment": "checkout-python",
   "provider": "docker", "state": "running", "kind": "job", "job": "job-1a2b3c4d", "job_state": "running",
   "session": {"id": "ses-…", "name": "job-1a2b3c4d", "state": "running"},
   "owner": "sean", "actor": "ui:sean", "harness": "claude-code", "model": "claude-opus-5-5",
   "repo": "https://github.com/acme/checkout.git", "branch": "hangar/job-1a2b3c4d",
   "role": "approval", "label": "Needs approval",
   "reason": {"title": "Held", "text": "Bash: git push origin main"},
   "ask": {"kind": "tool", "approval": "apr-9c1e", "job": "job-1a2b3c4d", "session": "ses-…"},
   "ask_expires_at": 1760903200.0, "inbox": ["inb-4b0d…"],
   "work": "Fix the refunds export", "task": "The CSV export drops…", "question": null, "question_options": [],
   "now": "waiting on you: Bash", "last_activity_at": 1760899940.0,
   "stage": "pr", "stage_pct": null,
   "stage_estimate": {"pct": 35, "elapsed_s": 64, "typical_s": 180, "basis": "median of 12 recent pr stages",
                      "samples": 12},
   "stages": [{"stage": "plan", "started_at": 1760898100.0, "ended_at": 1760898260.0},
              {"stage": "edit", "started_at": 1760898260.0, "ended_at": 1760899500.0},
              {"stage": "test", "started_at": 1760899500.0, "ended_at": 1760899876.0},
              {"stage": "pr", "started_at": 1760899876.0, "ended_at": null}],
   "files_changed": 4, "commits": 2,
   "usage": {"input_tokens": 182000, "output_tokens": 9100, "cache_read_tokens": 1500000, "cost_usd": 1.42, "…": "…"},
   "tokens_1h": [0, 0, 0, 0, 0, 0, 12400, 30100, 41800, 22050, 51000, 34200],
   "pr": null, "created_at": 1760898000.0, "expires_at": 1760905200.0, "turn": 1, "live": true}]}
```

Per unit:

- `role` is one of the six statuses (`approval`, `failed`, `denied`, `running`, `queued`, `off`), with `label` its
  word and `reason` why, when it needs someone or stopped. `ask` says what the person decides: a held tool call
  (`tool`, with the approval id), a job waiting to start (`job`), the agent's question (`question`) or a request
  another module raised (`inbox`, with the item). `inbox` lists the caller's inbox items that wait on this unit, so
  the deck and the tile agree; `ask_expires_at` is when the wait ends by itself.
- `question` is the agent's open question, and `question_options` the answers it offered
  (`hangar-agent question --option …`, at most 5 of at most 80 characters; empty otherwise), for the deck's buttons.
- `now` is the humanised latest event of the live turn: `editing <path>`, `running <command>` (at most 60
  characters), the agent's progress message, `waiting on you: <tool>`, or the tool call. While hangar itself drives
  the work (verifying, delivering), it is the job's own timeline line. `last_activity_at` is that event's time.
- `stage` is `plan`, `edit`, `test` or `pr`, only on a real signal: a read or search before anything else is
  planning, a file change editing, a test runner or linter testing, a push or `gh pr` the PR; a job hangar is
  verifying is in `test`, delivering in `pr`. No signal, no stage (a queued or idle unit has none). `stages[]` is the
  timeline so far with `started_at` and `ended_at` (`null` while open).
- `stage_pct` is set only when the agent itself reported progress (`hangar-agent progress --pct N`); otherwise it is
  `null` and `stage_estimate` guesses from time: `elapsed_s` in the stage against `typical_s`, the median of the same
  stage in this control plane's recent turns for the environment and agent (`basis` says so, and `samples` how many),
  or a share of recent jobs' median turn, or a default. The UI shows an estimate as one; it never reaches 100.
- `files_changed`, `commits` and `usage` are the live turn's (the delivered job's result once it has one).
- `tokens_1h` is the agent's new tokens (input, output and cache writes; not cache reads) in each of the last twelve
  five-minute buckets, oldest first, the last being the current five minutes: the tile's sparkline. Each usage event
  carries the turn's running totals, so a bucket gets the increase since the previous one. The buckets are kept only
  in memory: after a control-plane restart they start at zero and cover the time since, at most an hour.

The server keeps one small `LiveState` per session, fed by the turn watcher as it records events; nothing on this
path reads a log. A control-plane restart starts with empty states, which fill again as the running turns' logs are
re-read from their saved cursors. Two hundred sandboxes read in a few milliseconds.

## The fleet stream

`GET /api/fleet/stream?ticket=<ticket>` keeps the page current without polling: Server-Sent Events of the same read.
Browsers can't set headers on an EventSource, so the console first trades its token for a ticket,
`POST /api/fleet/tickets` (any person's `user` token or an admin's; one use, 60 seconds, bound to the caller, so a
member's stream shows the member's fleet). An admin's `POST /api/tickets` ticket and a bearer token work too.
`?owner=` filters as on the read.

| Event | Data |
| --- | --- |
| `snapshot` | The whole read, as `GET /api/fleet` returns it, first on every new stream |
| `agent` | One unit that changed, whole. At most one per agent every 2 seconds: a burst of events is coalesced into one update with the latest picture |
| `gone` | `{"id"}`: a unit that left the fleet (destroyed, or out of the filter) |
| `inbox` | `{"counts", "items"}`: the caller's inbox when it changed (an approval asked or decided, a question, a failure, an ack); the deck on Needs-you and atop Fleet reads this one source |
| `summary` | `{"counts", "owners", "slots"}` when they changed |
| `close` | `{"reason", "code"}` before hangar ends the stream: `cap` (over `HANGAR_STREAM_MAX_BYTES`), `revoked`, `lifetime`, `shutdown`, `limit` |

What wakes a stream is a change: the turn watcher's update of an agent's live picture, or a write of a sandbox, job,
session or request (a state, an approval, a question), or an inbox ack. A `stage_estimate` moving with the clock is
not a change by itself; the next update carries the latest. Every event's `id` is `<epoch>.<n>`, the change it
reflects. Reconnecting with `Last-Event-ID` sends only the units changed since (and the inbox and summary); an id
from before a restart, or older than the last 4,096 changes, gets a fresh `snapshot`. The stream counts against the
[stream limits](streaming.md#limits-and-closing) like any other (`HANGAR_STREAM_MAX_PER_TOKEN`: one more is a 429)
and closes when the token behind it is revoked.
