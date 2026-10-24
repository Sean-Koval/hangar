# Gaps found in the 1.0 security review

What an independent review of hangar found before 1.0, and what stays open until each fix lands. It adds to [what is not covered](not-covered.md). This is part of the [security model](README.md).

The review read hangar at `01c136c` as an attacker would. It found one High, five Medium and five Low gaps in hangar. Each is tracked in Jira, and this page shrinks as the fixes land.

## Approvals inside a turn on GKE and E2B (High)

On GKE and E2B there is no [uid split](../steering.md#the-uid-split). The driver, its run key and the harness settings belong to the same user as the agent. In an `ask` or `deny-risky` turn, the agent can allow itself or answer its own approval.

By default (`HANGAR_UID_SPLIT=auto`) hangar only warns. `required` refuses those turns instead. The gateway, the egress proxy and the landing checks still hold. Tracked in KAN-165, with the sidecar fix in KAN-83.

## Codex rules in hardened turns (Medium)

Codex reads `~/.codex/rules`, a repository's `.codex/rules` and MCP servers set to `approve`. The agent can write the first and last, and anyone who pushes can write the second. Any of them lets a call in an `ask` turn run unasked. This holds on Docker with the split too.

Fixed for files there when a turn starts. A Codex turn that asks won't start while either rules directory holds a `.rules` file. Nor will it while `~/.codex/config.toml` sets an MCP tool to `approve`.

Open: a file the agent writes during the turn. Codex has no switch to ignore rules, so closing it means giving Codex a home directory hangar owns. Tracked in KAN-169.

## Login files written back from a sandbox (Medium)

When a sandbox releases a login file, hangar stores the file the sandbox returns, so a refreshed token survives. A sandbox holding a team login can return another account's file instead. Members who fall back to the team login would then work in that account. Tracked in KAN-168.

## The control plane's image and identity (Medium)

The production image installs from the version ranges in `pyproject.toml`, not from `uv.lock`, and runs as root. GitHub Actions in the deploy and canary jobs are pinned by tag, not by commit. Tracked in KAN-170 and KAN-171.

The control plane's Google identity can write any bucket and the whole image repository. Tower's image lives in that repository. Tracked in KAN-172.

## Low

- **The dashboard** sends no Content-Security-Policy, and it keeps the admin token in browser storage. One missed escape in agent text would leak it.
- **The control-plane pod** runs without a restricted security context. No NetworkPolicy limits who reaches its ports.
- **GKE ships with `open` egress.** Set `HANGAR_EGRESS_DEFAULT=allowlist` for untrusted work.
- **Grants and keys cross the pod network in plain HTTP.** Turn on GKE's inter-node encryption.
- **Tower is not consulted yet.** The governance client exists, but no turn calls it and hangar has no events route. `HANGAR_GOVERNANCE` changes nothing in 1.0.

These are tracked in KAN-174, with the review's notes on each known gap in [what is not covered](not-covered.md).
