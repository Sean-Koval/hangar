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

`hangar session send` to a running Claude Code or Codex session is a steer now, not an error. If the turn ends before the message arrives, the message starts the next turn instead, so it is never lost.

## Steer

The message is added to the running turn:
- **Claude Code** reads it with the next tool result.
- **Codex** gets it with `turn/steer`.

The agent keeps the work it has done and adjusts. Each steer is recorded on the turn (`steers`, status `delivered` or `queued` for one that started the next turn) and in the session's events (`progress`, "steered: …").

## Interrupt

`interrupt` without a message stops the turn. Its outcome is `interrupted` (exit status 130). The session is idle and the conversation is kept, so the next `send` continues it.

`interrupt` with a message stops what the agent is doing and gives it that message. On a live turn this all happens in the same turn and the same conversation. On a one-shot turn the run is stopped and the message becomes the next turn.

On a job, an interrupted turn ends the job as `failed` with reason `interrupted`. What the agent did is delivered as a draft, and `follow_up_job` continues it.

## Tool approvals

The policy is set per session (`approvals` on `new_session`, `--approvals`), per environment (`approvals:` in its YAML), or for the whole deployment (`HANGAR_APPROVALS`). The first one set wins.

| Policy | What happens when the agent wants to run a command or edit a file |
| --- | --- |
| `auto` (default) | Nothing is asked. The harness runs with its permission checks off, as before. |
| `ask` | The agent waits. The request shows as `pending_approvals` on the session and its job, and as an `approval_request` event. Anyone with access answers `allow`, `deny` or `allow_always`. Unanswered after `HANGAR_APPROVAL_TIMEOUT_SECONDS` (default 600), it is denied. |
| `deny-risky` | Hangar answers at once. It denies force-pushes and remote branch deletion, `sudo`/`su`, piping a download into a shell, recursive deletes outside the working tree, system commands (`mkfs`, `dd of=/dev/…`, `shutdown`), anything touching credentials (`~/.ssh`, `.git-credentials`, `~/.hangar`, `/proc/*/environ`), and file writes outside the repository. It allows everything else. The rules are a guard rail against mistakes, not a sandbox. |

- **Deny:** the agent sees the denial, with your message if you gave one, and carries on: the turn does not fail.
- **`allow_always`:** also allows calls like this one for the rest of the agent's conversation. For Claude Code the rule goes to the conversation, never to the repository's settings. For Codex it is `acceptForSession`.
- **No prompts:** `ask` and `deny-risky` need a live turn. A session on pi or Antigravity with one of these policies fails its turn with a clear message rather than run with checks off. So does a sandbox without `python3`, which the driver needs. The one repair run that asks for a missing structured result is skipped under these policies, for the same reason.

### Answering from an MCP client

A client that supports elicitation is asked directly:
- **`wait_for_turn`** returns early when an approval is pending. For 2026-07-28 clients this is an `input_required` result with a form (`decision`: allow | deny | allow_always, and an optional `message`). On handshake-era sessions it is an `elicitation/create` request. Once you answer, it keeps waiting. The result's `approval` says what happened: `answered`, `declined`, `cancelled` or `stale`.
- **Tasks** turn `input_required` with the same form, under keys like `approve-ap-3f9c0a1b2c4d`; answer with `tasks/update`.

If you decline the form, you aren't asked about that approval again. It stays pending until someone else answers it or it times out.

A client without elicitation sees `pending_approvals` in `wait_for_turn`, `get_session` and `get_job`, and answers with `answer_approval(ref, approval, decision, message)`. To steer or interrupt, use `steer_turn(ref, message, interrupt=false)`, where `ref` is a job id or a session name.

## REST

| Route | Body |
| --- | --- |
| `POST /api/sessions/{ref}/steer` | `{"message": "..."}` |
| `POST /api/sessions/{ref}/interrupt` | optional `{"message": "..."}` |
| `GET /api/sessions/{ref}/approvals` | the running turn's policy, pending and recent approvals |
| `POST /api/sessions/{ref}/approvals/{id}` | `{"decision": "allow" \| "deny" \| "allow_always", "message": "..."}` |

The same four routes exist under `/api/jobs/{job_id}/…` for the job's submitter (a `jobs`-scoped token) and for admins. The session routes are admin only.

## How it works

The turn still runs in tmux, so `hangar sb attach` and the run log work as before. Instead of the harness's one-shot command, the run script starts hangar's driver (`src/hangar/driver.py`, standard library only), copied into the sandbox for each turn:

- **Claude Code:** `claude -p --input-format stream-json --output-format stream-json --verbose`.
  - Prompts and steers go to stdin as user messages.
  - Permission prompts come back as `can_use_tool` control requests (`--permission-prompt-tool stdio`); `auto` uses `--dangerously-skip-permissions` instead.
  - An interrupt is an `interrupt` control request.
  - The driver writes the stream to the run log unchanged, except that each `result` carries the process's token totals, as its cost already does.
- **Codex:** `codex app-server`, JSON-RPC over stdio.
  - The driver runs `thread/start`, `thread/resume` or `thread/fork` and `turn/start`; steers use `turn/steer`, interrupts `turn/interrupt`.
  - `approvalPolicy` is `untrusted` under `ask` and `deny-risky`, `never` under `auto`. The sandbox mode is `danger-full-access`, as before.
  - The driver translates notifications into `codex exec --json` lines, so event parsing, usage and conversation capture work as they did.

**Commands.** Hangar writes each command to `~/.hangar/runs/<run>.ctl/`, moves it into `in/` and reads the driver's answer from `acks`. Each command is signed (HMAC-SHA256) with a key made for that run. Hangar keeps the key encrypted in its database. The driver reads its copy and deletes it before the agent starts, so nothing else in the sandbox can approve a call or steer the agent by writing a file. Unsigned, replayed and malformed commands are refused.

**Ending the turn.** When the turn is over, the driver renames `in/` away first. A command that comes too late then fails to land, and hangar knows at once; a command that landed just before is still carried out. A late steer continues the turn.

**Status and restarts.** The driver's exit status is the turn's: 130 after an interrupt that brought no new message. The driver runs inside the sandbox, so a control-plane restart doesn't affect it. The restarted control plane re-attaches to the turn (from its saved event cursor), and pending approvals can still be answered.

`HANGAR_LIVE_TURNS=false` turns all of this off: every turn runs one-shot, as before.

## Verified

Against the real CLIs, offline, with scripted model APIs (`tests/test_driver_real_clis.py`; set `HANGAR_TEST_CLAUDE` / `HANGAR_TEST_CODEX`):

| CLI | What was checked |
| --- | --- |
| Claude Code 2.1.296 | Steering mid-turn (`command_lifecycle`, the steer surfaced in the next request); `can_use_tool` allow, deny (an error tool result, then the turn goes on) and `updatedPermissions`; interrupt (`control_cancel_request` for open prompts, the result `error_during_execution`, the process takes the next message); cumulative `total_cost_usd` and `modelUsage`; `--resume` and `--fork-session` |
| codex 0.162.1 | `initialize` / `initialized` / `thread/start` / `turn/start`; `turn/steer` (-32600 "no active turn to steer" when idle); `turn/interrupt` (status `interrupted`, the approval resolved via `serverRequest/resolved`); `item/commandExecution/requestApproval` with accept and decline; `thread/tokenUsage/updated` totals; `thread/resume` and `thread/fork` |

pi 1.2's `--mode rpc` has `prompt`, `steer` and `abort` but no permission prompts. It is not wired up yet, so pi turns run one-shot.
