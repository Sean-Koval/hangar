# Open findings and accepted risks

This page lists every finding and known gap in hangar that is not fully fixed. It is for the reviewer deciding
whether hangar can go to production. Each row says who owns it, why it is acceptable for now, what limits the damage,
and when it is looked at again. The [controls](controls.md) page has what is in place.

Sean owns every risk until a team takes hangar over. Each is reviewed at the 1.0 exit (KAN-146) and again at the
next release. A row leaves this page when its fix lands, and its finding moves to "fixed" on
[Gaps found in the 1.0 review](../review-1.0.md).

## Being fixed before 1.0

| Finding | Severity | Ticket | Until then |
| --- | --- | --- | --- |
| Kubernetes Secrets aren't encrypted with a customer key | Not rated | KAN-180 | Google encrypts etcd at rest; the vault inside is Fernet-encrypted |
| No dependency, image or secret scanning in CI | Not rated | KAN-179 | Locked, hashed dependencies; digest-pinned images; Dependabot for actions |
| Five of six node pools run as the Compute Engine default service account (CIS 5.2.1) | Not rated | KAN-183 | gVisor stands between a sandbox and the node; the pools are replaced once, with Secure Boot |
| The audit log lives on the same disk as the database it records | Not rated | KAN-182 | Only the pod's service account mounts the disk; backups copy it daily |

## Accepted for 1.0

| Risk | Severity | Why it is acceptable | What limits it |
| --- | --- | --- | --- |
| On GKE and E2B an agent can answer its own approval in an `ask` or `deny-risky` turn (no uid split) | High (H-1), mitigated | `HANGAR_UID_SPLIT=required` refuses those turns: the Kubernetes manifests set it, and it is E2B's default | The gateway, egress proxy and landing checks still hold; the sidecar is KAN-83, a split on E2B KAN-200 |
| A Codex rules file the agent writes during a turn lets a call run unasked | Medium | Files present at turn start already refuse the turn; Codex has no switch to ignore rules | Same limits as above; closing it needs a hangar-owned Codex home (KAN-169) |
| A person's own login file can be swapped for another account's refresh token after a refresh | Medium | Team logins are never written back; only that person's later sandboxes are affected | Same-account check, rotation check, and a new login ends old leases (KAN-168, KAN-178) |
| Tower decides only when governance is on: `HANGAR_GOVERNANCE` is `off` by default, and release and landing don't ask it yet | Low (L-1) | hangar's own approvals, gateway and landing are the controls until a team turns it on | Tool calls, egress and secret requests are wired (KAN-77 to KAN-79); release and landing are KAN-33 |
| The console's admin token is in `sessionStorage`, readable by a script on the page | Low (L-7) | A strict CSP runs only hangar's own scripts, and the 50 remaining HTML sinks are audited | `tests/test_ui_csp.py` fails on a new sink; an `HttpOnly` cookie needs CSRF checks first |

## Decided, in review

Sean decided these on 2026-10-27. Each fix is a pull request, and its row leaves when it merges.

| Decision | Pull request | What changes for users |
| --- | --- | --- |
| GKE ships with `allowlist` egress (L-9) | #387 | Sandboxes reach model APIs, the package mirror and the gateway. Direct GitHub, run-time toolchain downloads and arbitrary sites need an `allow` entry |
| The warn switches are removed (KAN-74) | #388 | Automated jobs refuse subscription logins and need the gateway. Each harness needs an API-key credential first |
