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
| The console has no CSP and keeps the admin token in browser storage | Low (L-7) | KAN-174 | Agent text goes through `esc()` at the known sinks; use member tokens day to day |
| The control-plane pod has no restricted security context or ingress NetworkPolicy | Low (L-8) | KAN-174 | The pod runs as a non-root user; sandboxes can't reach RFC1918 addresses |
| Git grants cross nodes in plain HTTP | Low (L-10) | KAN-180 | Grants are per sandbox, short-lived and end with the sandbox |
| Kubernetes Secrets aren't encrypted with a customer key | Not rated | KAN-180 | Google encrypts etcd at rest; the vault inside is Fernet-encrypted |
| No dependency, image or secret scanning in CI | Not rated | KAN-179 | Locked, hashed dependencies; digest-pinned images; Dependabot for actions |
| The audit log lives on the same disk as the database it records | Not rated | KAN-182 | Only the pod's service account mounts the disk; backups copy it daily |

## Accepted for 1.0

| Risk | Severity | Why it is acceptable | What limits it |
| --- | --- | --- | --- |
| On GKE an agent can answer its own approval in an `ask` or `deny-risky` turn (no uid split) | High (H-1), mitigated | The Kubernetes manifests set `HANGAR_UID_SPLIT=required`, which refuses those turns | The gateway, egress proxy and landing checks still hold; the sidecar is KAN-83 |
| A Codex rules file the agent writes during a turn lets a call run unasked | Medium | Files present at turn start already refuse the turn; Codex has no switch to ignore rules | Same limits as above; closing it needs a hangar-owned Codex home (KAN-169) |
| A person's own login file can be swapped for another account's refresh token after a refresh | Medium | Team logins are never written back; only that person's later sandboxes are affected | Same-account check, rotation check, and a new login ends old leases (KAN-168, KAN-178) |
| Tower isn't consulted yet: `HANGAR_GOVERNANCE` changes nothing | Low (L-1) | hangar's own approvals, gateway and landing are the controls until then | The wiring is KAN-77 to KAN-79 |
| E2B command output may be buffered whole before hangar sees it | Low (gap 9) | E2B is an optional provider, not the GKE default | A re-test is due before it leaves [what is not covered](../not-covered.md) |

## Waiting on a decision

These stay open until Sean decides, because each fix changes what users can do.

| Question | The trade-off |
| --- | --- |
| Make `allowlist` the GKE egress default? (L-9) | Stops data going to arbitrary hosts. Jobs that install from the internet need their hosts listed |
| Remove the warn switches, including `HANGAR_SUBSCRIPTION_LOGINS=any`? (KAN-74) | Ends automated use of personal subscription logins, which vendor terms forbid. Needs API keys for every harness first |
| Reserve custom session branches before the session exists? (gap 8) | Stops another sandbox pre-creating the branch. Breaks continuing a branch you pushed yourself |
