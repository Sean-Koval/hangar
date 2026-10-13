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
| `deny-risky` | Hangar answers at once. It denies force-pushes and remote branch deletion, `sudo`/`su`, piping a download into a shell, recursive deletes of the working tree or anything outside it, system commands (`mkfs`, `dd of=/dev/…`, `shutdown`), reading or writing credentials and hangar's files (`~/.ssh`, `.git-credentials`, `~/.hangar`, `/proc/*/environ` and `mem`) except the turn's own `$HANGAR_IO`, and file writes outside the repository and `$HANGAR_IO`. It allows everything else. The rules are a guard rail against mistakes, not a sandbox: paths are resolved against the turn's directory, so `cd /elsewhere && rm -r x` gets past them. |

- **Deny:** the agent sees the denial, with your message if you gave one, and carries on: the turn does not fail.
- **`allow_always`:** also allows calls like this one for the rest of the turn. For Claude Code these are the CLI's own suggestions with destination `session`; for Codex it is `acceptForSession`. Both live in the agent's running process: nothing is written to the repository's or the user's settings, and the next turn asks again (verified with both CLIs).
- **What you are shown** is the whole command, up to 2,000 characters. A longer one shows both ends with the cut marked ("[…N characters not shown…]"); deny what you can't see.
- **No prompts:** `ask` and `deny-risky` need a live turn. A session on pi or Antigravity with one of these policies fails its turn with a clear message rather than run with checks off. So does a sandbox without `python3` (3.8 or later, with `ctypes`), which the driver needs. The one repair run that asks for a missing structured result is skipped under these policies, for the same reason.

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

**Commands.** Hangar writes each command to `~/.hangar/runs/<run>.ctl/`, moves it into `in/` and reads the driver's answer from `acks`. Each command, and each answer, is signed (HMAC-SHA256) with a key made for that run. Hangar keeps the key encrypted in its database. The driver reads its copy and deletes it before the agent starts. Unsigned, replayed and malformed commands are refused, and hangar ignores answers the driver didn't sign. The driver runs with `python3 -I` (nothing the agent can write, such as a module beside the driver or `PYTHONPATH`, is loaded into it), and makes itself non-dumpable before it reads the key: the agent runs as the same user, and without that it could read the key out of `/proc/<driver>/mem` (it could, in this repo's Docker sandboxes, which have no Yama ptrace restriction).

**What approvals protect against.** The agent's own tool calls: a command or an edit runs only if you (or the deny-risky rules) allow it, and the agent can't answer for you through the control channel. They are not a boundary against code you allow to run. The agent and the driver share a user, so a command you allow can do anything that user can, now and in later turns: for example, edit `~/.hangar/env`, which each turn's script reads, or leave a process behind that watches for the next turn's key file. Treat "allow" on a command that runs code the agent wrote (a test suite, a build script) as trusting that code. A boundary against that needs the driver under its own user; it is a follow-up.

**Ending the turn.** When the turn is over, the driver renames `in/` away first. A command that comes too late then fails to land, and hangar knows at once; a command that landed just before is still carried out. A late steer continues the turn. If the agent's process exits on its own, the driver takes no more commands, and answers the ones it had not passed on with "the turn has ended": hangar then starts the next turn with the message, so it is never lost.

**Status and restarts.** The driver's exit status is the turn's: 130 after an interrupt that brought no new message. The driver runs inside the sandbox, so a control-plane restart doesn't affect it. The restarted control plane re-attaches to the turn (from its saved event cursor), and pending approvals can still be answered.

`HANGAR_LIVE_TURNS=false` turns all of this off: every turn runs one-shot, as before.

## Verified

Against the real CLIs, offline, with scripted model APIs (`tests/test_driver_real_clis.py`; set `HANGAR_TEST_CLAUDE` / `HANGAR_TEST_CODEX`):

| CLI | What was checked |
| --- | --- |
| Claude Code 2.1.296 | Steering mid-turn (`command_lifecycle`, the steer surfaced in the next request); `can_use_tool` allow, deny (an error tool result, then the turn goes on) and `updatedPermissions`; interrupt (`control_cancel_request` for open prompts, the result `error_during_execution`, the process takes the next message); cumulative `total_cost_usd` and `modelUsage`; `--resume` and `--fork-session`; `Read` outside the working tree asks first (so deny-risky can refuse credentials); `allow_always` writes no settings file and a resumed turn asks again |
| codex 0.162.1 | `initialize` / `initialized` / `thread/start` / `turn/start`; `turn/steer` (-32600 "no active turn to steer" when idle); `turn/interrupt` (status `interrupted`, the approval resolved via `serverRequest/resolved`); `item/commandExecution/requestApproval` with accept and decline; `thread/tokenUsage/updated` totals; `thread/resume` and `thread/fork`; `cat` of a file outside the working tree asks under `untrusted`; `acceptForSession` does not outlive the process (a resumed turn asks again) |

pi 1.2's `--mode rpc` has `prompt`, `steer` and `abort` but no permission prompts. It is not wired up yet, so pi turns run one-shot.
