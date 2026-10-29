# Break-glass: once Tower decides (planned)

The planned half of [the break-glass runbook](break-glass.md): what to do once hangar asks Tower and Tower is down or
wrong.

Spec 0015 (§5–§8, §14) plans it. The modes are built for every kind; `hangar governance local` isn't yet. Check the
CHANGELOG for what has shipped before you rely on a step.

## The modes

| Mode | Who decides | Tower |
| --- | --- | --- |
| `off` (the default) | hangar's own rules, as today | not asked |
| `shadow` | hangar's own rules | asked the same question as a dry run, in the background; a different answer is audited `governance.disagree` and counted in `hangar_governance_disagreements_total{kind}` |
| `enforce` | Tower, failing closed | asked; an `allow` must carry a receipt |

`HANGAR_GOVERNANCE` sets the default mode and `HANGAR_GOVERNANCE_MODES` overrides it per kind
(`tool.call=shadow,landing.approve=enforce`). The kinds are `job.release`, `tool.call`, `landing.approve`,
`egress.request` and `secret.request`. hangar refuses to start in `shadow` or `enforce` without
`HANGAR_GOVERNANCE_URL`.

## When Tower is unreachable

In `enforce`, hangar fails closed, kind by kind (spec 0015 §6):

| Kind | What people see |
| --- | --- |
| `job.release` | The job stays `awaiting_approval`; its issue or ticket says "approval service unavailable" once per outage |
| `landing.approve` | The PR isn't armed; `landing.note` says "approval service unavailable" |
| `tool.call` | The call is denied with "approval service unavailable", and asked again while the driver's deadline allows |
| `egress.request`, `secret.request` | The request stays pending; its owner is told once |

A `5xx` from Tower, whatever its body (including a `503` that says `deny`), counts as *unavailable*. So do a `429` and
an open circuit: never a policy decision. Pending things are polled again every 60 s and settle by themselves when
Tower answers.

1. **Confirm it is Tower.** `hangar doctor` checks the URL, the credential and the webhook secret (planned, spec 0015
   §7). A `401` that a fresh token doesn't fix, or a `403`, is a credential problem. The doctor fails `governance` and
   hangar doesn't retry until the credential changes. Fix the credential instead of breaking glass.
2. **A short outage:** wait. Nothing is allowed meanwhile, and nothing is lost: jobs keep their place, landings are
   evaluated again on the next push, approval or poll.
3. **An outage that blocks the team:** either put the blocked kinds back to `shadow` (below), or break glass.

## Putting a kind back from `enforce`

Set the kind to `shadow` (hangar decides; Tower is still asked as a dry run, so disagreements keep being counted) or
`off` (Tower isn't asked):

```bash
HANGAR_GOVERNANCE_MODES='job.release=shadow,landing.approve=shadow'   # read on every decision: applies live
```

Nothing is migrated, and receipts Tower already wrote stay (spec 0015 §14). hangar's own checks run in every mode.
`enforce` never makes hangar more permissive than its own rules, so going back to `shadow` or `off` loosens nothing
those rules hold.

Go back to `enforce` once Tower answers again; the spec's bar for a kind's first `enforce` is a week with no
disagreements (§14).

## Break-glass

```bash
hangar governance local --until <time> --reason "Tower down since 14:10, its on-call paged"    # planned command
```

An admin starts it, and a **second admin** confirms it. It lasts at most 24 hours and ends by itself. Its start,
confirmation and end are audited (`governance.break_glass`). While it is active, the kinds in `enforce` are decided by
hangar's local rules.

Each such decision is queued to be replayed to Tower as a record once it answers, so its receipts have no gap. That
replay isn't built on either side yet (spec 0015 §16; Tower refuses a `recorded` request today). Break-glass never
applies to kinds in `off` or `shadow`, which are local already.

Unlike a mode change, break-glass ends by itself and needs two people.

## When Tower denies what it shouldn't

A `deny` whose status is `blocked` or `denied` is Tower's policy, or a person, saying no. The job is rejected with
Tower's reason, the call denied, the landing not armed. Don't break glass to get around a decision.

Fix the rule in Tower, or put the kind back to `shadow` while the policy is tuned. Read the `governance.disagree` audit
entries for the requests where hangar's rules and Tower's differ. An `expired` or `withdrawn` approval means nobody
decided: hangar treats it like its own expiry, and the next request opens a new approval.
