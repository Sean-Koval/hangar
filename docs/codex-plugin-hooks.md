# Codex plugin hooks

Do a Codex harness lock's plugin hooks run in its sandboxes, and how do you find one that doesn't? This page is for
whoever maintains the team harness repo or runs a plugin A/B on Codex.

## Why it matters

Codex 0.162.1 runs a plugin's hooks only once they are trusted. It skips an untrusted hook silently, and installing a
plugin doesn't trust its hooks. So a Codex arm with ai-dlc's workflow plugin would get the skills without the hooks.
Nothing would fail, and the A/B would quietly compare different things.

## What hangar trusts

hangar trusts exactly the hooks of a lock's Codex forms, by their content hash. A Codex form is
`native/codex/<name>/` in the lock's directory, every file pinned by the lock ([Codex plugins](harness-lock.md#codex-plugins)).

- The hash is Codex's own: over the hook's event, matcher, command, timeout and options.
- hangar computes it from the lock's files on the control plane, never from the sandbox.
- It reaches each Codex turn as one `-c hooks.state={...}` flag (`HANGAR_CODEX_HOOK_TRUST`). Nothing is written to
  the agent's `~/.codex`.

What the agent can write is never trusted. A hook it adds to `~/.codex` keeps whatever trust Codex gave it, which
hangar doesn't change. A hook it edits in its plugin cache no longer matches its hash: Codex reports it `modified` and
skips it. A hardened turn still runs Codex with hooks off (`--disable hooks`), plugin hooks included.

Some hooks hangar can't trust:

| Hook | Why it stays off |
| --- | --- |
| a `prompt` or `agent` hook | Codex 0.162.1 skips them as unsupported |
| an `mcp_tool` hook | Codex loads it, but hangar trusts command hooks only |
| an empty command | Codex never runs it |
| a hooks file the manifest names that is missing, outside the plugin or unparseable | Codex skips the whole file |

## Before any sandbox: `hangar doctor`

With `HANGAR_HARNESS_REPO` set, `hangar doctor` has a `codex plugin hooks` check. It reads the current revision of
each Codex harness whose lock enables plugins. It passes with the count of hooks trusted by hash.

It fails, naming the harness, the plugin and the hook, when:

- a claim would refuse the lock: an enabled plugin without a Codex form, or a form file the lock doesn't pin;
- a form declares a hook from the table above.

The fix is in the plugin's Codex form: command hooks only, in a hooks file its manifest names. Then re-freeze the
harness and run `hangar harness check`. A harness that isn't Codex, or enables no plugin, adds no check.

## In a sandbox: turn 1

Turn 1's probe asks the sandbox's Codex for its hooks (app server `hooks/list`, with the same trust flag). Each
locked hook that isn't `trusted` and enabled is drift, `codex-hook:<key>`, with Codex's status: `untrusted`,
`modified`, `missing` or `disabled`.

The probe also lists every other hook Codex loaded for the lock's plugins. hangar didn't trust those, so each is
drift with status `untrusted`. A plugin whose hooks Codex warns it skipped is drift as `codex-hook:<name>@<marketplace>`,
status `skipped`. Drift shows on the turn, in its events and in the job's review packet ([thaw and
drift](harness-lock.md#thaw-and-drift)).

## Check it with the real CLI

The tests can run against a real Codex. One installs ai-dlc's workflow form as the mount does. It then runs a turn
with hangar's own Codex command, against a local stand-in for the model API. Its `SessionStart` and `Stop` hooks fire
only with hangar's trust.

```bash
HANGAR_TEST_CODEX=$(command -v codex) uv run pytest tests/test_codex_plugins.py
```
