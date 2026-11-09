# How steering works

This page is part of [Steering a running agent](../steering.md).

The turn still runs in tmux, so `hangar sb attach` and the run log work as before. Instead of the harness's one-shot command, the run script starts hangar's driver (`src/hangar/driver.py`, standard library only), copied into the sandbox for each turn:

- **Claude Code:** `claude -p --input-format stream-json --output-format stream-json --verbose`.
  - Prompts and steers go to stdin as user messages.
  - Permission prompts come back as `can_use_tool` control requests (`--permission-prompt-tool stdio`); `auto` uses `--dangerously-skip-permissions` instead.
  - An interrupt is an `interrupt` control request.
  - The driver writes the stream to the run log unchanged, except that each `result` carries the process's token totals, as its cost already does.
- **Codex:** `codex app-server`, JSON-RPC over stdio.
  - The driver runs `thread/start`, `thread/resume` or `thread/fork` and `turn/start`; steers use `turn/steer`, interrupts `turn/interrupt`.
  - `approvalPolicy` is `untrusted`, or `never` under `auto`. The sandbox mode is `danger-full-access`, as before.
  - The driver translates notifications into `codex exec --json` lines, so event parsing, usage and conversation capture work as they did.

**Commands.** hangar writes each command to `~/.hangar/runs/<run>.ctl/`, moves it into `in/` and reads the driver's answer from `acks`. Each command, and each answer, is signed (HMAC-SHA256) with a key made for that run. hangar keeps the key encrypted in its database. The driver reads its copy and deletes it before the agent starts. Unsigned, replayed and malformed commands are refused, and hangar ignores answers the driver didn't sign. The driver runs with `python3 -I` (nothing the agent can write, such as a module beside the driver or `PYTHONPATH`, is loaded into it), and makes itself non-dumpable before it reads the key: the agent runs as the same user, and without that it could read the key out of `/proc/<driver>/mem` (it could, in this repo's Docker sandboxes, which have no Yama ptrace restriction).

**What approvals protect against.** The agent's own tool calls: a command or an edit runs only if you (or the deny-risky rules) allow it, and the agent can't answer for you through the control channel. Where the sandbox allows it, the driver also runs as a user of its own ([the uid split](uid-split.md)), so code you allow to run can't reach it either. Without the split, the agent and the driver share a user, and a command you allow can do anything that user can, now and in later turns: for example, edit `~/.hangar/env`, which each turn's script reads, or leave a process behind that watches for the next turn's key file. There, treat "allow" on a command that runs code the agent wrote (a test suite, a build script) as trusting that code.

**Ending the turn.** When the turn is over, the driver renames `in/` away first. A command that comes too late then fails to land, and hangar knows at once; a command that landed just before is still carried out. A late steer continues the turn. If the agent's process exits on its own, the driver takes no more commands, and answers the ones it had not passed on with "the turn has ended": hangar then starts the next turn with the message, so it is never lost.

**Status and restarts.** The driver's exit status is the turn's: 130 after an interrupt that brought no new message. The driver runs inside the sandbox, so a control-plane restart doesn't affect it. The restarted control plane re-attaches to the turn (from its saved event cursor), and pending approvals can still be answered.

**The job's check.** For a job with `verify`, the driver runs the check whenever a Codex turn completes. If it fails, the driver sends the output back into the same conversation as the next message, up to `max_verify_rounds` times. It doesn't do this after an interrupt that brought no message. Claude Code gets the same check as a Stop hook instead. See [jobs §7](../jobs/agent-loop.md). Under [the uid split](uid-split.md) the check is the repo's own code, so the driver runs it as the agent, like the harness, never as hangar's user.

## Trees

The driver records the repo state at each step (#165), so a fork from any step can be rebuilt and checked (#179).
At the turn's start and end, and after each edit or shell tool's result (Claude Code's `Write`, `Edit`, `MultiEdit`,
`NotebookEdit`, `Bash`, `BashOutput`; Codex's `fileChange` and `commandExecution` items), it hashes the working tree
and writes a `{"type": "hangar.tree", "tool_use_id" | "phase": "start" | "end", "tree": <sha>, "ms": <elapsed>}` line into the run log.
After a tool, the line is written only when the tree changed since the last one, so reads and commands that change
nothing leave no line. The event parser turns each into a `progress` event with action `tree`, and the trace export
into `hangar.git.tree` on the tool call and `hangar.git.tree.start` / `.end` on the turn
([traces.md](../api/traces.md#toolcall)).

- **How.** `git add -A` and `git write-tree` (which honour `.gitignore`), with a git directory, a scratch index (seeded
  once from the repo's, so each hash rehashes only what changed) and an object store of the driver's own in
  `<ctl>/tree/`, deleted when the turn ends, and the repo's objects read as alternates. Nothing is written to the
  agent's `.git`, and its config (filters, `core.fsmonitor`, hooks) runs nothing, which matters under the uid split,
  where the driver is hangar's user. The ids are the ones `git write-tree` gives for the same content in the repo
  itself, unless the repo's config changes content on the way in (`core.autocrlf`, a clean filter).
- **When.** A tree is read when the tool's result reaches the driver. Parallel tool calls that finish together can
  share a tree.
- **Cost.** About 24 ms per edit at p50 on this repo's own checkout (`scripts/bench/tree_hash.py`,
  `bench/results/2026-10-17-tree-hash.json`, a shared 2-vCPU VM). One hash slower than 2 s stops hashing for the turn,
  recorded as `skipped: "slow"` (`hangar.git.tree.skipped` on the turn). Large untracked files that `.gitignore`
  doesn't cover cost what they cost to hash: that is what trips the limit.
- **Trust.** Under [the uid split](uid-split.md) the run log is hangar's, so the lines are trusted. Without it the
  agent shares the driver's user and could append a forged `hangar.tree` line: those trees are recorded with
  `trusted: false` (`hangar.git.tree.trusted`), and #179 checks trees again when it replays a prefix.
- **One-shot turns** (no driver: a harness without a structured mode, or no `python3` in the sandbox) record the start
  and end trees only, through an exec as the sandbox's user at the turn's start and end, kept on the turn record
  (`git_trees`, not in its event log), always `trusted: false`.

`HANGAR_LIVE_TURNS=false` turns all of this off: every turn runs one-shot, as before.
