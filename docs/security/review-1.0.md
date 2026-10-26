# Gaps found in the 1.0 security review

What an independent review of hangar found before 1.0, and what stays open until each fix lands. It adds to [what is not covered](not-covered.md). This is part of the [security model](README.md).

The review read hangar at `01c136c` as an attacker would. It found one High, five Medium and five Low gaps in hangar. Each is tracked in Jira, and this page shrinks as the fixes land.

## Approvals inside a turn on GKE and E2B (High)

On GKE and E2B there is no [uid split](../steering.md#the-uid-split). The driver, its run key and the harness settings belong to the same user as the agent. In an `ask` or `deny-risky` turn, the agent can allow itself or answer its own approval.

Elsewhere the default (`HANGAR_UID_SPLIT=auto`) only warns. The Kubernetes manifests now set `required`, which refuses those turns instead; `auto` turns are unaffected. The gateway, the egress proxy and the landing checks still hold. The sidecar that would let them run is KAN-83.

## Codex rules in hardened turns (Medium)

Codex reads `~/.codex/rules`, a repository's `.codex/rules` and MCP servers set to `approve`. The agent can write the first and last, and anyone who pushes can write the second. Any of them lets a call in an `ask` turn run unasked. This holds on Docker with the split too.

Fixed for files there when a turn starts. A Codex turn that asks won't start while either rules directory holds a `.rules` file. Nor will it while `~/.codex/config.toml` sets an MCP tool to `approve`.

Open: a file the agent writes during the turn. Codex has no switch to ignore rules, so closing it means giving Codex a home directory hangar owns. Tracked in KAN-169.

## Login files written back from a sandbox (Medium)

When a sandbox releases a login file, hangar can store the file the sandbox returns, so a refreshed token survives. The sandbox writes that file, and the identity in it isn't verified.

Fixed for team logins: hangar never stores a file a sandbox returns for one. Members who fall back to it can't be moved to another account. Subscription logins can't be team credentials, so none needs its rotation kept.

Open for a person's own login. hangar stores a returned file only when it names the same account and its access tokens changed with its refresh token. A sandbox that refreshes the stored login first, then swaps in another account's refresh token, still passes. Only that person's later sandboxes would be affected.

Closing it means hangar refreshing the login itself and checking the identity it gets back. Tracked in KAN-168.

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
