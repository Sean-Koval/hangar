# Gaps found in the 1.0 security review

What an independent review of hangar found before 1.0, and where each finding stands. It adds to
[what is not covered](not-covered.md). This is part of the [security model](README.md), and the
[review packet](review/index.md) builds on it.

The review read hangar at `01c136c` as an attacker would. It found one High, five Medium and five Low gaps in hangar.
Each is tracked in Jira. What remains open, with its owner and reason, is on
[open findings and accepted risks](review/risks.md).

## Where each finding stands

| Finding | Severity | Ticket | Status |
| --- | --- | --- | --- |
| Approvals inside a turn on GKE and E2B | High | KAN-165, KAN-189 | Mitigated: refused under `required`, the default on E2B; the sidecar is KAN-83 |
| Codex rules in hardened turns | Medium | KAN-169 | Fixed at turn start; a file written during the turn is accepted |
| Login files written back from a sandbox | Medium | KAN-168, KAN-178 | Fixed for team logins; a person's own login is accepted |
| The image ignores `uv.lock`, uses `uv:latest`, runs as root | Medium | KAN-170 | Fixed |
| GitHub Actions pinned by tag | Medium | KAN-171 | Fixed |
| hangar's Google roles are project-wide | Medium | KAN-172 | Fixed in hangar; Tower's own registry is its half |
| The console: no CSP, the admin token in browser storage | Low (L-7) | KAN-174 | Fixed: a strict CSP, audited sinks, the token in `sessionStorage`; an `HttpOnly` cookie is accepted as later work |
| The control-plane pod's security context and NetworkPolicy | Low (L-8) | KAN-174, KAN-170 | Fixed; IAP stays off, with the [reason](control-plane-pod.md) |
| GKE ships with `open` egress | Low (L-9) | KAN-174 | Decided: `allowlist`, in review (#387) |
| Git grants in plain HTTP between nodes | Low (L-10) | KAN-180 | Fixed: [inter-node encryption](gke-cluster.md) |
| Tower is not consulted yet | Low (L-1) | KAN-77 to KAN-79, KAN-33 | Wired and off by default; release and landing are KAN-33 |
| Predictable custom session branches | Known gap 8 | KAN-174 | Fixed: [checked against their creator](branch-creators.md) |
| E2B buffers a command's whole output | Known gap 9 | KAN-174 | Closed: hangar's envd client keeps a bounded tail, re-tested |
| The gateway has no connection cap | Known gap 10 | KAN-174 | Fixed: a per-peer cap |
| NAT64 and 6to4 addresses pass the callback check | Q-2, Q-3 | KAN-174 | Fixed |

## Approvals inside a turn on GKE and E2B (High)

On GKE and E2B there is no [uid split](../steering.md#the-uid-split). The driver, its run key and the harness settings belong to the same user as the agent. In an `ask` or `deny-risky` turn, the agent could allow itself or answer its own approval.

`HANGAR_UID_SPLIT=required` refuses those turns instead; `auto` turns are unaffected. The Kubernetes manifests set it. On E2B it is the default when the setting is unset (KAN-189), and `hangar doctor` flags an explicit `auto`. The gateway, the egress proxy and the landing checks still hold. The sidecar that would let them run is KAN-83.

## Codex rules in hardened turns (Medium)

Codex reads `~/.codex/rules`, a repository's `.codex/rules` and MCP servers set to `approve`. The agent can write the first and last, and anyone who pushes can write the second. Any of them lets a call in an `ask` turn run unasked. This holds on Docker with the split too.

Fixed for files there when a turn starts. A Codex turn that asks won't start while either rules directory holds a `.rules` file. Nor will it while `~/.codex/config.toml` sets an MCP tool to `approve`.

Accepted: a file the agent writes during the turn. Codex has no switch to ignore rules, so closing it means giving Codex a home directory hangar owns.

## Login files written back from a sandbox (Medium)

When a sandbox releases a login file, hangar can store the file the sandbox returns, so a refreshed token survives. The sandbox writes that file, and the identity in it isn't verified.

Fixed for team logins: hangar never stores a file a sandbox returns for one. Storing a new login also ends any lease on the old one, so a sandbox can't write a stale file back (KAN-178).

Accepted for a person's own login. hangar stores a returned file only when it names the same account and its access tokens changed with its refresh token. A sandbox that refreshes the login, then swaps in another account's refresh token, still passes. Only that person's later sandboxes would be affected.

Closing it means hangar refreshing the login itself and checking the identity it gets back.

## The control plane's image and identity (Medium)

Fixed. The image installs from `uv.lock` with hashes, pins every base image by digest and runs as uid 1000 (KAN-170). Every GitHub Action is pinned by commit SHA (KAN-171). hangar's Google identity builds with Cloud Build, writes only its own registry repository and a build-staging bucket, and has no project-wide storage role (KAN-172).

## Low

- **The console** sends a strict Content-Security-Policy. An audit replaced the riskiest HTML sinks, and a test fails on a new one. The admin token is in `sessionStorage`; an `HttpOnly` cookie needs CSRF checks first.
- **The control-plane pod** runs non-root with a restricted security context. An ingress NetworkPolicy limits who reaches its ports ([control-plane pod](control-plane-pod.md)).
- **GKE ships with `open` egress.** #387 makes it `allowlist`. Until it merges, set `HANGAR_EGRESS_DEFAULT=allowlist` for untrusted work.
- **Grants and keys crossed the pod network in plain HTTP.** GKE's inter-node encryption now covers them (KAN-180).
- **Tower decides only when governance is on.** Tool calls, egress and secret requests can ask it; the default mode is `off`. Job release and landing are KAN-33. hangar refuses an answer about a different approval.

These are tracked in KAN-174, with the review's notes on each known gap in [what is not covered](not-covered.md).
