# Waiting, answering and subscribing

How an MCP client waits for an agent's turn, answers its question or approves its tool call, and follows a job or
session without polling. The servers and tools are on [using MCP](../mcp.md).

## Waiting for work, and answering the agent

`wait_for_turn(ref)` waits for a job's or session's current turn (up to 10 minutes per call) and sends progress
notifications while it runs. It returns the turn: `outcome`, `summary`, the structured `result`, `artifacts` (as
`resource_link` blocks too), the PR for a job.

When the agent stops on a question (`hangar-agent ask`, state `needs_input`), hangar asks you instead of returning:

- **2026-07-28**: the call returns `resultType: "input_required"` with an elicitation. Your client shows the form,
  then retries the same call with your answer. hangar sends it to the agent as the next message, in the same
  conversation, and keeps waiting.

  ```json
  {"resultType": "input_required",
   "inputRequests": {"answer-1": {"method": "elicitation/create", "params": {"mode": "form",
     "message": "Session csv-fix asks: Use the streaming parser or load the whole file?",
     "requestedSchema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}}}},
   "requestState": "<sealed by the SDK>"}
  ```
- **2025-era clients**: hangar sends `elicitation/create` on your session and waits up to 15 minutes for the answer.
- The turn it finally returns says what happened in `elicitation`: `answered`, `declined` or `cancelled`. If you
  decline, nothing is sent and the question stays open.
- A client that declared no elicitation capability gets the question in the result, as before; answer with
  `follow_up_job` or `send_to_session`.

An answer goes to the agent at most once: if the question was already answered (another client, a retry), hangar
sends nothing.

## Tool approvals and steering

When a session or its environment uses `approvals: ask`, the agent waits for a person before it runs a command or
edits a file. `wait_for_turn` puts each such request to you in the same way as a question:

- an `input_required` round for 2026-07-28 clients, or `elicitation/create` for earlier ones;
- the form has `decision` (`allow`, `deny` or `allow_always`) and an optional `message` for the agent;
- the result's `approval` says what happened.

A task turns `input_required` with the same form (keys `approve-<id>`).

Those are a turn's tool approvals. Releasing a whole job that waits in `awaiting_approval` is `approve_job` /
`reject_job`. Only admins and the repo's policy approvers may, with a login token, never on their own job. An
`mcp`-only `hangar connect` token is refused. The rule and refusal text are the same as REST's
([who may approve](../triggers.md#who-may-approve)).

Clients without elicitation see the requests in `pending_approvals` (on `wait_for_turn`, `get_session` and
`get_job`) and answer with `answer_approval`. `steer_turn(ref, message)` tells a running agent something mid-turn;
`steer_turn(ref, interrupt=true)` stops it. See [steering](../steering.md).

## Resources and subscriptions

| URI | Content |
| --- | --- |
| `hangar://sessions/<name>` | the session (JSON): state, turns, summaries, question, checkpoints |
| `hangar://sessions/<name>/events` | its last 200 activity events |
| `hangar://sessions/<name>/patch` | its work as one patch against the base branch (`git apply`) |
| `hangar://sessions/<name>/artifacts/<turn>/<path>` | a file the agent handed back |
| `hangar://jobs/<id>` | the job (JSON): state, timeline, result, attempts |
| `hangar://jobs/<id>/events` | its agent's last 200 events |
| `.../events?since=<seq>` | only the events after `seq` (what a subscriber reads after a notification) |
| `hangar://sandboxes/<sandbox>/watch?path=<dir>` | file changes under a directory, watched while you subscribe ([streaming](../streaming.md#file-watches)) |
| `hangar://inbox` | what needs you: PRs to review, agents' questions, approvals, failed jobs ([landing](../landing.md#7-the-inbox-what-needs-you)); it changes with any job or session |

`exec(stream=true)` sends a command's output as progress notifications while it runs, and `process` runs background
commands you attach to later; see [streaming](../streaming.md).

Subscribe to follow work without polling. A 2026-07-28 client opens `subscriptions/listen` with
`resourceSubscriptions: ["hangar://jobs/job-1a2b"]`; a 2025-era client sends `resources/subscribe`.

hangar sends `notifications/resources/updated` whenever the job or session changes (state, a turn starting or ending,
new events). It sends at most one every 250 ms per URI. A notification only says "read it again".
