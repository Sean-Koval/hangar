# The Tower hook

How do you make every tool call in a sandbox ask Tower first, so the agent can't switch that off? This page is for whoever runs hangar beside Tower. The contract is [spec 0015 §9](specs/0015-governance-decision-point.md#9-identity).

## Turning it on

Require it for one environment:

```yaml
governance:
  hook: required
```

Or for every environment with `HANGAR_GOVERNANCE_HOOK=required`. Either one is enough: an environment can't loosen the default.

| Setting | What it holds |
| --- | --- |
| `HANGAR_GOVERNANCE_URL` | Tower's address, as the sandbox reaches it |
| `HANGAR_GOVERNANCE_HOOK_CREDENTIAL` | The vault credential with a coding agent's key in Tower |
| `HANGAR_GOVERNANCE_HOOK_WAIT_SECONDS` | How long the hook waits for a person to sign, 300 by default |

An environment with `allowlist` egress needs Tower's host on its list.

## What the hook does

Before each tool call, Claude Code runs hangar's hook, `tower_hook.py`. It sends the call to Tower's `/hooks/claude-code`, the same contract as Tower's own laptop hook.

| Tower answers | The call |
| --- | --- |
| `deny` | Refused, with Tower's reason |
| `allow` | Goes on to hangar's own checks |
| `wait` | Waits for a signature, then runs or is refused |

The hook fails closed. Tower unreachable, a timeout, a 5xx, a bad key or an answer it can't read all exit 2, which blocks the call. The settings add `|| exit 2`, so a hook that can't start blocks too. The turn shows the reason on the refused call.

## One path per turn

With the hook on, Tower sees every call through the hook. The driver then answers from hangar's `deny-risky` rules, even when the session asks for `ask` or `governed`. It never relays a call to Tower as well, so one call never raises two approvals. The turn says so in a warning, and its manifest keeps the policy you asked for.

A Tower `allow` never skips the `deny-risky` rules. Tower never makes hangar more permissive than its own enforcement.

## Where it runs

The hook must sit where the agent can't remove it. So a turn that requires it starts only where hangar owns the harness's settings:

| Provider | Hook | Why |
| --- | --- | --- |
| Docker, with the uid split | Installed, tamper-proof | The script and settings are hangar's user's files |
| Docker, split off or `sudo: true` | Turn refused | The agent shares the driver's user |
| GKE | Turn refused | One user per pod, until [the driver sidecar](security/driver-sidecar.md) is wired |
| E2B | Turn refused | No uid split yet |

This holds whatever `HANGAR_UID_SPLIT` says. On Docker, the script is `/var/lib/hangar/tower_hook.py`, beside the driver. The settings are the turn's own file, loaded with `--setting-sources ''`. The agent's `~/.claude/settings.json`, the repository's `.claude/` and their `disableAllHooks` don't count. Deleting them changes nothing.

## The key

`HANGAR_GOVERNANCE_HOOK_AUTH` picks what the hook sends Tower. Either way it is a file of the run, written by hangar's user. The agent can't change it, and it is removed when the run ends.

| Value | The key |
| --- | --- |
| `key` (default) | The coding agent's key in Tower, from `HANGAR_GOVERNANCE_HOOK_CREDENTIAL`, shared by every sandbox |
| `passport` | The sandbox's own passport, about 15 minutes, renewed while the run lasts |

With `passport`, the control plane mints the sandbox's token. Its subject is `hangar:env:<env>`, with the sandbox's person and the email linked to them, for Tower's `requester_claim`. hangar exchanges it at Tower's token endpoint and checks the passport before it writes it.

The passport must name the agent `HANGAR_GOVERNANCE_HOOK_AGENT` (`hangar`), and a sandbox with a person must get that person's passport. No passport, no turn.

The refresh loop renews a passport with under ten minutes left. It swaps the new file in only while the run's key is still there, so a run that ended never gets one back.

**What the isolation can't do.** The hook runs as the agent, so the agent can read the key, whichever it is. It can ask Tower what the hook asks, as the sandbox's person, nothing more.

A passport limits that to one sandbox, one person and a quarter of an hour. The shared key is every sandbox's, for as long as it lives. Keeping the key from the agent entirely would need the hook to call Tower through the driver, which runs as hangar's user. That isn't built.

## Integrity

hangar writes the script at each turn's start and checks its digest in the sandbox. Another digest refuses the turn. The digest is on the turn and in its [manifest](specs/0014-sister-tool-interfaces.md), as a `hangar:tower_hook` hook, so it is part of `harness_digest`.

Before writing it again, hangar reads what is there. A script that changed since hangar installed it is audited `governance.hook_tampered` and warned about on the turn.

## Codex

Codex turns that require the hook are refused for now. Codex 0.162.1 has PreToolUse hooks, and it accepts one from a `-c` flag with its trust hash.

But a hardened Codex turn runs with `--disable hooks`. Turning hooks on would also run hooks the agent writes and trusts in `~/.codex`. A safe version needs hangar to refuse a turn with any other hook, as it does for rules files ([KAN-169](steering.md#hangar-owned-harness-settings)).
