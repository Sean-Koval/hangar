# Steering a running agent

A session turn used to run the agent one-shot and wait for it to exit. A message sent while it ran was refused, the only way to stop it was to kill it, and the agent ran with its permission checks off. Claude Code and Codex turns now run **live**. While a turn runs you can:

- **steer** it: add a message to the running turn, and the agent reads it at its next step;
- **interrupt** it: stop the turn, or stop and carry on with a new message;
- **approve its tool calls**: with `approvals: ask`, you allow or deny each command the agent wants to run.

Other harnesses (pi, Antigravity) still run one-shot. You can interrupt them (the run is stopped), but you can't steer them.

```bash
hangar session new api-fix "move the retry logic into the client" -e backend --approvals ask
hangar session steer api-fix "keep the public signature unchanged"
hangar session approvals api-fix --watch                # what the agent waits on
hangar session approvals api-fix --allow ap-3f9c0a1b2c4d
hangar session interrupt api-fix "stop; write the failing test first"
```

`hangar session send` to a running Claude Code or Codex session is a steer now, not an error. If the turn ends before the message arrives, the message starts the next turn instead, so it is never lost. While it is still on its way (`sending`), it can be taken back (`withdraw`, below); the web console shows it as a queued row with a take-back until the agent has it.

## Steer

The message is added to the running turn:

- **Claude Code** reads it with the next tool result.
- **Codex** gets it with `turn/steer`.

The agent keeps the work it has done and adjusts. Each steer is recorded on the turn (`steers`, status `delivered` or `queued` for one that started the next turn) and in the session's events (`progress`, "steered: …").

## Interrupt

`interrupt` without a message stops the turn. Its outcome is `interrupted` (exit status 130). The session is idle and the conversation is kept, so the next `send` continues it.

`interrupt` with a message stops what the agent is doing and gives it that message. On a live turn this all happens in the same turn and the same conversation. On a one-shot turn the run is stopped and the message becomes the next turn.

An interrupt always stops the turn. If the driver doesn't confirm it (no answer in time, or an answer it didn't sign), hangar kills the run instead, as it does for a one-shot turn.

On a job, an interrupt is a person taking over, not a failure:

- **With a message**, the job carries on with it: in the same turn when the turn is live, else as the job's next turn (a follow-up).
- **Without one**, the job pauses in `needs_input`, with the question "Interrupted by … Follow up with what the agent should do instead, or cancel the job." Nothing is verified or delivered. Answer it like any question (`follow_up_job`, `/hangar …` on GitHub, or the MCP elicitation): the agent continues the same conversation. Cancel the job to end it.

## Tool approvals

The policy is set per session (`approvals` on `new_session`, `--approvals`), per environment (`approvals:` in its YAML), or for the whole deployment (`HANGAR_APPROVALS`). The first one set wins. A fork, and each best-of-N attempt, keeps its parent's policy.

| Policy | What happens when the agent wants to run a command or edit a file |
| --- | --- |
| `auto` (default) | Nothing is asked. The harness runs with its permission checks off, as before. |
| `ask` | The agent waits. The request shows as `pending_approvals` on the session and its job, and as an `approval_request` event. Anyone with access answers `allow`, `deny` or `allow_always`. Unanswered after `HANGAR_APPROVAL_TIMEOUT_SECONDS` (default 600), it is denied. |
| `deny-risky` | hangar answers at once. It denies force-pushes and remote branch deletion, `sudo`/`su`, piping a download into a shell, recursive deletes of the working tree or anything outside it, system commands (`mkfs`, `dd of=/dev/…`, `shutdown`), reading or writing credentials and hangar's files (`~/.ssh`, `.git-credentials`, `~/.hangar`, `/proc/*/environ` and `mem`) except the turn's own `$HANGAR_IO`, and file writes outside the repository and `$HANGAR_IO`. It allows everything else. The rules are a guard rail against mistakes, not a sandbox: paths are resolved against the turn's directory, so `cd /elsewhere && rm -r x` gets past them. |
| `governed` | The `deny-risky` rules and the taint gate sort the calls. What they flag waits while [Tower decides](governed-approvals.md); the rest runs at once. |

- **Deny:** the agent sees the denial, with your message if you gave one, and carries on: the turn does not fail.
- **`allow_always`:** also allows calls like this one for the rest of the turn. For Claude Code these are the CLI's own suggestions with destination `session`; for Codex it is `acceptForSession`. Both live in the agent's running process: nothing is written to the repository's or the user's settings, and the next turn asks again (verified with both CLIs).
- **hangar's own hand-back commands are not asked about:** under every policy but `auto`, a shell call that is exactly `hangar-agent done`, `progress`, `ask` or `result '<json>'` (one simple command: nothing chained with `;`, `&&` or `|`, no redirection, no `$(...)` or backquotes) is allowed by the policy at once, recorded as an approval resolved `by: policy`. These only write the turn's files under `$HANGAR_IO`. `hangar-agent artifact`, `result @FILE`, `env`, `svc` and the computer commands, and anything chained to an allowed one (`hangar-agent done ok; rm -rf ~`), still ask.
- **What you are shown** is the whole command, up to 2,000 characters. A longer one shows both ends with the cut marked ("[…N characters not shown…]"); deny what you can't see.
- **No prompts:** every policy but `auto` needs a live turn. A session on pi or Antigravity with one fails its turn rather than run with checks off. So does a sandbox without `python3` (3.8 or later, with `ctypes`), which the driver needs. The one repair run that asks for a missing structured result is skipped under these policies, for the same reason.

### Answering from an MCP client

A client that supports elicitation is asked directly:

- **`wait_for_turn`** returns early when an approval is pending. For 2026-07-28 clients this is an `input_required` result with a form (`decision`: allow | deny | allow_always, and an optional `message`). On handshake-era sessions it is an `elicitation/create` request. Once you answer, it keeps waiting. The result's `approval` says what happened: `answered`, `declined`, `cancelled` or `stale`.
- **Tasks** turn `input_required` with the same form, under keys like `approve-ap-3f9c0a1b2c4d`; answer with `tasks/update`.

If you decline the form, you aren't asked about that approval again. It stays pending until someone else answers it or it times out.

A client without elicitation sees `pending_approvals` in `wait_for_turn`, `get_session` and `get_job`, and answers with `answer_approval(ref, approval, decision, message)`. To steer or interrupt, use `steer_turn(ref, message, interrupt=false)`, where `ref` is a job id or a session name.

### Taint

Every turn records whether the agent has read untrusted content (a web page, an untrusted MCP server's result, an
untrusted issue): `taint` on the turn, the session and its job ([security/taint.md](security/taint.md)). It is sticky
across turns, follow-ups and forks. Once a turn is tainted, under `ask` and `deny-risky` these count as risky as well:

- any `git push` (`bash -lc '...'` is looked into), and `gh pr create`;
- `curl`, `wget` or `nc` to a host other than the sandbox itself;
- the environment's MCP write tools: `mcp.<name>.write_tools`, fnmatch patterns of the server's tool names
  (`write_tools: ["create_*", "update_*", "merge_*"]`).

`deny-risky` denies them with "after reading untrusted content"; `ask` asks a person, and the request carries `taint`
(why it asks). `auto` changes nothing. Intake jobs should run under `deny-risky`: they are tainted from turn 1
([security/taint.md](security/taint.md#taint-gates)). The driver hears of the taint at the turn's start (`--tainted`), from
hangar's signed `taint` command mid-turn, or from a web tool it sees in the stream; nothing clears it.

## Classifying steers

A steer is not always the agent's fault. The person may have changed the task, or remembered something the ticket never said. So that a harness is judged only on the interventions it made necessary, each steer, interrupt and job follow-up carries a class (#162):

| Class | Meaning |
| --- | --- |
| `correction` | the agent got it wrong |
| `redirect` | the person changed the scope or the approach |
| `new_info` | the person added something the task didn't say. An answer to the agent's own question (`needs_input`) is this by default: the agent asked |
| `unclassified` | nobody said (the default for everything else, including steers and interrupts sent over REST or MCP) |

Say which:

- **Slack:** click *Correction*, *Redirect* or *New info* under hangar's "Continuing…" reply to your thread message. Only the person who sent the reply can classify it; anyone else's click gets a private refusal ([slack.md](slack.md#classifying-a-reply)).
- **Slack and Jira:** start the reply with `fix:` (or `correction:`), `redirect:` or `info:`. It is classified on arrival and the prefix is stripped from what the agent gets.
- **Jira:** `/hangar class correction|redirect|info` classifies your last unclassified follow-up on the issue ([intake.md](intake/jira.md#talking-to-the-job-from-the-issue)).
- **REST:** `POST /api/jobs/{job_id}/turns/{n}/class` (the job's submitter, or admin; `n` is the 1-based index in the job's `turns`) and `POST /api/sessions/{ref}/steers/{steer_id}/class` (admin), body `{"class": "correction"}`. There is no MCP tool: the tool list is at its cap.

The class, `classified_by` and `classified_at` are on the turn or steer record. Each classification is a `steer.classified` progress event in the session's activity, and the [trace export](api/traces.md) puts `hangar.steer.class` on steer and interrupt events (with `hangar.seq`, their place in the event log) and on follow-up turn spans; the run records in the outbox carry it. It is self-reported and optional: the weekly metrics show the unclassified share.

## REST

| Route | Body |
| --- | --- |
| `POST /api/sessions/{ref}/steer` | `{"message": "..."}` |
| `POST /api/sessions/{ref}/steers/{id}/withdraw` | none: takes back a message that is still `sending` (a `send` to a busy session, on its way to the agent). Once it is `delivered` or `queued` there is nothing to take back; interrupt instead. The steer's status becomes `cancelled`. |
| `POST /api/sessions/{ref}/interrupt` | optional `{"message": "..."}` |
| `GET /api/sessions/{ref}/approvals` | the running turn's policy, pending and recent approvals |
| `POST /api/sessions/{ref}/approvals/{id}` | `{"decision": "allow" \| "deny" \| "allow_always", "message": "..."}` |

The same four routes exist under `/api/jobs/{job_id}/…` for the job's submitter (a `jobs`-scoped token) and for admins. The session routes are admin only. Classifying: `POST /api/sessions/{ref}/steers/{id}/class` and `POST /api/jobs/{job_id}/turns/{n}/class` with `{"class": "correction" | "redirect" | "new_info"}` ([above](#classifying-steers)).

## Verified

Against the real CLIs, offline, with scripted model APIs (`tests/test_driver_real_clis.py`; set `HANGAR_TEST_CLAUDE` / `HANGAR_TEST_CODEX`):

| CLI | What was checked |
| --- | --- |
| Claude Code 2.1.296 | Steering mid-turn (`command_lifecycle`, the steer surfaced in the next request); `can_use_tool` allow, deny (an error tool result, then the turn goes on) and `updatedPermissions`; interrupt (`control_cancel_request` for open prompts, the result `error_during_execution`, the process takes the next message); cumulative `total_cost_usd` and `modelUsage`; `--resume` and `--fork-session`; `Read` outside the working tree asks first (so deny-risky can refuse credentials); `allow_always` writes no settings file and a resumed turn asks again |
| codex 0.162.1 | `initialize` / `initialized` / `thread/start` / `turn/start`; `turn/steer` (-32600 "no active turn to steer" when idle); `turn/interrupt` (status `interrupted`, the approval resolved via `serverRequest/resolved`); `item/commandExecution/requestApproval` with accept and decline; `thread/tokenUsage/updated` totals; `thread/resume` and `thread/fork`; `cat` of a file outside the working tree asks under `untrusted`; `acceptForSession` does not outlive the process (a resumed turn asks again) |

pi 1.2's `--mode rpc` has `prompt`, `steer` and `abort` but no permission prompts. It is not wired up yet, so pi turns run one-shot.

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="how-it-works"></span>How it works | [How steering works](steering/how-it-works.md) |
| <span id="trees"></span>Trees | [How steering works](steering/how-it-works.md#trees) |
| <span id="the-uid-split"></span>The uid split | [The uid split](steering/uid-split.md) |
| <span id="hangar-owned-harness-settings"></span>hangar-owned harness settings | [hangar-owned harness settings](steering/harness-settings.md) |
