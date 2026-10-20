# The web console

The control plane serves a web console on the same port as the API, `:8787` by default (`HANGAR_PORT`). Open it,
paste an API token, and it shows the fleet. The server prints a bootstrap admin token when it first starts and saves
it to `~/.hangar/bootstrap-token` (in its data directory). The console uses an admin's routes, so use an admin token;
the token stays in that browser until you choose **Sign out**.

The top bar's first four sections are where the work happens, and this page covers them: **Needs you** (what waits on
a person), **Jobs**, **Sessions** and **Fleet** (every sandbox, four ways), each with a count. The other six are
Environments, Credentials, Snapshots, Connect (MCP snippets and API tokens), Activity (the audit log) and Usage (spend,
budget stops). The open section refreshes every 4 seconds while the browser tab is visible, and the Needs-you count
about every half minute elsewhere. On a phone (below 760 px) the first four sections move to a bottom tab bar, and the
other six, with the providers' state, sit under **More**.

The console follows the browser's light or dark setting. Terminals are always dark.

## Needs you

Everything waiting on a person, from the same list `hangar inbox`, Slack and MCP clients get (`GET /api/inbox`): your
own work, and for an admin also the work nobody in particular owns, such as jobs from GitHub issues and Slack.

First come the cards you decide in place:

- **A held tool call**: a running turn under `approvals: ask` wants to run a command. The card shows the exact
  command, which agent asked and in which sandbox, for whom and how long ago, the policy that held it (and whether the
  turn read untrusted content), and when it expires and what happens then (it is denied and the agent carries on).
  **Allow**, **Allow for this turn** (also allows calls like this one until the turn ends; nothing is written to the
  agent's settings, so the next turn asks again) or **Deny**.
- **A job waiting to start**: from someone the team doesn't trust, for example, or held by a daily spend cap; the card
  says why. **Approve job** or **Reject**. Approving follows the same rule as every other approval path, so you can't
  approve your own work ([security](security.md)).
- **A request** an agent made for access (an environment request): **Approve** or **Deny**.
- **A question** the agent asked: type an answer (or pick one of the options it offered) and **Answer**. The answer
  goes back as the agent's next message.

**Deny** and **Reject** need a reason: the field opens in the card, and the reason goes back to the agent or the
requester. **Open job** or **Open session** shows the work first. After the cards come the jobs that failed in the last
week, the steps policy denied in turns still running (`denied`: the agent carried on without them), and the PRs
waiting for your review on GitHub, with **Open job** and **Open PR**. A failed or denied item has **Acknowledge**
(`POST /api/inbox/{id}/ack`): it leaves your list for a week; a decision never does. With nothing waiting, the page
says so and shows how many jobs and sessions are running.

The same cards appear on the job or session they belong to, and in the Fleet's detail panel.

## Jobs

![The jobs view: issue #42's job succeeded, check passed, draft PR #43, the agent's summary](assets/front/ui-jobs.png)

Work handed to cloud agents by people, services and the [front doors](intake.md). The list filters by **In progress**,
**Succeeded**, **Failed** or **Cancelled**. **New job** submits one with its instructions, environment, agent, model,
repo, base branch, check command (**Check with**), what to deliver (draft PR, PR, branch only, or nothing) and minutes
per turn, plus context files and links. Files go to `~/.hangar/jobs/<job>/` beside the repo, never into it.

Choose a job to see what waits on you (its cards, above), then:

- who asked and through which door, where it runs (environment, agent, model), its repo and branch, the pull request
  and the check's result;
- the agent's summary and, when the check failed, its output;
- the agent's question, or the message box (below);
- **Changes**: **Show the change** reads the commits, uncommitted edits and new files against the base, from the
  sandbox or its last checkpoint, as a diff per file. **Comment** on a line, and the comments go to the agent with your
  next message, each with its file, line and the line itself; **Send comments** sends them on their own;
- **Decisions**: the tool calls decided in the running turn, allowed or denied, with the reason and who decided;
- **Activity**: what the agent did (messages, tool calls, commands, file changes), with tokens and cost;
- the **Timeline** of the job's events, its **Turns**, and its **Instructions**.

**The message box.** While the agent's turn is running, the box steers it: the message applies after the current
tool call, and shows as queued until the agent has it. A one-shot agent can't take a message mid-turn, and the box
says so. Once the job has finished, **Reply to the agent** runs another turn in the same sandbox and branch, and the PR
updates; a job whose sandbox was released resumes from its last checkpoint. Ctrl+Enter (⌘+Enter) sends.

While a job runs, **Watch the agent** opens the agent's own terminal, **Open a shell** opens another one in the same
sandbox, and **Cancel job** stops the agent after you confirm; work committed so far stays in the sandbox and on the
branch. See [jobs](jobs.md) and [steering](steering.md).

## Sessions

![The sessions view: a resumable session with its saved conversation and checkpoint](assets/front/ui-sessions.png)

Streams of work you come back to. The list shows your own sessions; **Show job sessions** adds the ones jobs run in.
**New session** takes a name, an environment, an agent and an optional first message. Without a message the sandbox
just opens.

A session's panel shows what waits on you (its cards, or the agent's question), its conversation (resumed natively, or
by a summary handoff), its checkpoints and its idle setting: suspend after 15 minutes, 30 minutes, 2 hours or 8 hours
idle, or stay up until you suspend it. Then **Changes**, **Decisions**, **Activity** and **Turns**, as on a job. From
there:

- **Message the agent**: an idle or suspended session starts a turn (a suspended one resumes first). While the agent
  is busy, your message queues and applies after the current tool call; until the agent has it, the queued row has a
  × to take it back.
- **Resume** (or **Open**, before anything was saved) starts a fresh sandbox from the last checkpoint.
- **Talk to the agent here** resumes the agent's conversation interactively in a terminal, between turns.
- **Last turn's terminal**, **Open a shell** and **Suspend** (save, then release the sandbox).

A session that carries a job takes its messages on the job. See [sessions](sessions.md).

## Fleet

Every sandbox, whoever started it: a job, a session, or a person directly. The page header leads with what needs you:
the loudest status present, the count of each status, and how many of the hangar's slots (`HANGAR_MAX_SANDBOXES`) are
in use. **Show stopped** adds stopped sandboxes; **New sandbox** starts one or more identical sandboxes from an
environment, each on its own numbered work branch.

The same sandboxes are shown four ways, with one selection shared by all four. The view is kept in the URL
(`#fleet?view=bays`) and remembered by the browser.

### Board

![The Board: every sandbox of the checkout-python environment as a small tile with its tail number, the one waiting on a person selected, its detail panel beside the board with the agent's question](assets/front/ui-fleet-board.png)

Every sandbox at a glance: one small tile per sandbox, grouped by environment, labelled with the last four characters of
its id. A tile shows its status mark and, while the work has a stage, four pips for plan, edit, test and PR. Tiles are
ordered by attention: what needs a person, then failed, then denied, then the quiet states. Hover for the sandbox's
name, status, reason and task. On a wide screen the detail panel sits beside the Board; elsewhere it opens as a drawer
on the right.

### Bays

![Bays: a card per sandbox with its id, status, title, owner, harness, stage track and next action](assets/front/ui-fleet-bays.png)

A card per sandbox you can read and act on: its id and status, the job's or session's title, who it belongs to and the
agent, the stage track, the reason it needs you (a question, a held step, a failure), its age and the time left on its
lease, and one action. Each environment ends with a **Start in** card.

### Columns

![Columns: Needs you, Failed or denied, Running, Starting, and Paused or spun down](assets/front/ui-fleet-columns.png)

One column per state, attention on the left: **Needs you**, **Failed or denied**, **Running**, **Starting**, and
**Paused or spun down**. Cards in the first two columns show why, and their action.

### Owners

![Owners: one row per person with their sandbox and session use against their caps and their spend today, and their sandboxes per environment](assets/front/ui-fleet-owners.png)

People down, environments across. Each row shows the sandboxes the person started directly and their live sessions,
against the per-person caps (`HANGAR_PERSON_MAX_SANDBOXES`, `HANGAR_PERSON_MAX_SESSIONS`), what they spent today, and
how many of theirs need someone. Sandboxes hangar started for itself sit in a `hangar` row. See
[team scale](team-scale.md) for the caps.

### The status key

Every status is a mark, a word and, for the three that need someone, a color. Running work stays grey on purpose: a
screen with no color on it needs nobody.

| Status | Mark | When |
| --- | --- | --- |
| Running | grey dot | the sandbox is up and its work is going on |
| Starting | ring | the sandbox is pending or starting |
| Needs you (needs approval, needs input) | orange diamond | a job waits for approval to start, a running turn waits for a tool call to be allowed, or the agent asked a question |
| Failed | red triangle | the sandbox stopped on an error, or its job failed |
| Denied | red placard | policy denied a step in the running turn |
| Paused or spun down | small dot | paused, stopping or stopped |

The brand yellow marks the selected tile, focus and the primary action, never a status.

### The detail panel

Select a sandbox in any view to open its detail panel. It holds every action, so all four views get the same ones.
What waits on a person comes first: the [cards](#needs-you) for a held tool call or a job waiting to start, decided
here, or the agent's question with its **Answer**. Then its stage, its name, the job or session it belongs to (a link)
and whose it is, where it runs (environment, provider, agent, model), its age and lease, repo, branch, credentials, the
sandbox it was forked from, and its task.

**Sandbox actions.** On a running sandbox: **Terminal** (a shell), **Run agent** (a headless agent on a task, in its own
terminal, which opens), **Run command**, **Fork** (snapshot it and start copies with the same files, dependencies and
repo state), **Snapshot**, **Pause** and **+1 hour** on its lease. A paused one offers **Resume** and **+1 hour**. Every
sandbox that isn't stopped has **Destroy**. The panel also lists the sandbox's last agent runs; choose one to attach to
its terminal.

### Keyboard

- Each view is one tab stop. **Arrow keys** move between tiles by position, across environments, cells and columns;
  **Home** and **End** go to the first and last.
- **Enter** (or **Space**) opens the focused sandbox in the detail panel.
- **Escape** closes the panel where it is a drawer, and focus returns to the tile.
- In a message box, **Ctrl+Enter** (⌘+Enter) sends.

Focus, scroll and a half-written answer or reason survive the 4-second refresh. When the set of sandboxes that need
someone, failed or were denied changes, a screen reader hears how many need you and how many failed or were denied.

### The fleet read: `GET /api/fleet`

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
   "work": "Fix the refunds export", "task": "The CSV export drops…", "question": null,
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

## Terminals

Terminals open in a drawer at the bottom of the console, one tab each; drag its top edge to resize it. Opening an
agent run attaches to the same terminal the agent uses: you see what it types and can type back. **Terminal** opens a
plain shell; to switch to a terminal an agent or an MCP client is using, choose **Sessions…**, pick it and **Attach**.
A closed connection reconnects when you press Enter. Escape inside a terminal goes to the program running there (vim,
less), not to the console.

## Hide, Destroy and Snapshot

- **Hide**, in the terminal drawer, only hides the drawer. The terminals stay open and the sandbox keeps running.
  Close a terminal with its tab's ×.
- **Destroy**, in the detail panel, deletes the sandbox and its filesystem, after you confirm, and closes its
  terminals. Push or save what you want to keep first. A job's branch and a session's conversation are kept: a
  session's sandbox is checkpointed before it goes, so the session suspends and resumes later from that checkpoint.
- **Snapshot** keeps the sandbox's state without stopping it: a full snapshot where the provider has them, otherwise
  the workspace's files. Snapshots are listed under **Snapshots**, where **Start copy** starts a new sandbox from one.
  Take one before you destroy a sandbox whose files you may need.

From the CLI, `hangar sb rm <name>` removes a sandbox.
