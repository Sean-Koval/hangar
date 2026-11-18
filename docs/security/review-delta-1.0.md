# Gaps found in the second 1.0 review

What a second review found in the code that landed after the [first 1.0 review](review-1.0.md), and where each
finding stands. It is for the reviewer who signs hangar off for production.

The first review read hangar at `01c136c`. About 330 commits followed, with SSO, Tower governance, the driver
sidecar, data handling and the local runner. Three reviewers who didn't build that code read it at `2fc1175`, each in one area.
They looked for what an agent, a member or an outsider could do that the [threat model](threat-model.md) says they
can't.

They found one High, seven Medium and four Low gaps. Each is tracked in Jira. An open row blocks 1.0 until it is
fixed, or the owner accepts it on [open findings and accepted risks](review/risks.md).

## Where each finding stands

| Finding | Severity | Ticket | Status |
| --- | --- | --- | --- |
| The agent can kill or stop the Tower hook, so the call runs without Tower | High | KAN-551 | Open. The hook is off by default (`HANGAR_GOVERNANCE_HOOK=off`); hangar's own approval rules still apply to the call |
| A removed person's SSO binding survives, so their old account signs in as whoever gets the id next | Medium | KAN-552 | Fix in review (#484) |
| A token named `tower` skips Tower's signature on a person's allow | Medium | KAN-552 | Fix in review (#484) |
| A purge-redacted audit row can be copied over another row, and doctor still says ok | Medium | KAN-553 | Fix in review (#485) |
| Purging a person named like one of hangar's own owners (`jobs`) deletes other people's records | Medium | KAN-553 | Fix in review (#485); such ids are reserved in #484 |
| One admin can meet break-glass's two-person rule by minting a token for another admin | Medium | KAN-554 | Open: needs a decision on how the second person proves who they are |
| The driver sidecar's probe of the agent's container runs `sh` from the agent's `PATH` | Medium | KAN-555 | Open; the sidecar is off by default (KAN-186) |
| The local runner's network still reaches the developer's host | Medium | KAN-555 | Open; `hangar env local` only |
| A purge leaves the person's name on branches they created | Low | KAN-553 | Fix in review (#485): kept as nobody's |
| Forged `hangar.*` lines through stderr; a sidecar run's log tail read through the agent's container | Low | KAN-556 | Open |
| An IAP first sign-in binds a person by email without the provider vouching for it | Low | KAN-556 | Open |
| The audit sink takes any log line containing `hangar.audit: {` | Low | KAN-556 | Open |

## Reviewed and sound

On identity, the reviewers found the OIDC flow, the session cookie and the CLI hand-off sound. So were the checks
on members' scopes and Tower passports.

On governance, the events receiver, pre-cleared calls, `governed` failing closed and the relay's token handshake held.

On data handling, purge and export are admin-only, the pseudonym can't be reversed and the export withholds secrets.
The local egress sidecar's rules match the control plane's, and trace import runs offline.
