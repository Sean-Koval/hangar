# Runbook: Tower is down, and hooks fail closed

For Tower's `tower-down` alert and hangar's `tower-fail-closed` alert ([alerting](../alerting.md)). Tower decides whether risky agent actions are allowed. When it doesn't answer, nothing risky is allowed: that is the design (KAN-78), not a bug. This page is hangar's side. Tower's own runbook, [Tower down](https://sean-koval.github.io/tower/runbooks/tower-down/), brings Tower back.

## What is blocked while Tower is down

- **Laptops with Tower's Claude Code hook.** The hook fails closed, so every call it would send to Tower is blocked. Engineers see "Blocked by the Tower hook (it fails closed)".
- **hangar, for kinds in `enforce`.** Those decisions fail closed: jobs wait in `awaiting_approval`, and tool calls are denied with "approval service unavailable". In `shadow` (the default) hangar's own rules decide and nothing waits.
- **Nothing else in hangar.** Jobs, sessions and sandboxes keep running.

hangar asks Tower for every kind not set to `off`: [job release and landing](../tower-release-landing.md), and [governed approvals](../governed-approvals.md). With every kind `off`, hangar's alert can't fire, and Tower's uptime check is the only signal.

## 1. Confirm it

```bash
bash deploy/gke/tower.sh status            # in Tower's checkout: pod, disk, certificate
kubectl -n hangar logs deploy/hangar --since=30m | grep 'hangar.slo: event=fail_closed'
```

The `failure=` field says how hangar failed to reach it: `unreachable`, `server_error`, `circuit_open` or `rate_limited`. An `auth` failure means hangar's credential for Tower is wrong, not that Tower is down.

## 2. Bring Tower back

Follow Tower's [Tower down runbook](https://sean-koval.github.io/tower/runbooks/tower-down/). Usually it is `tower.sh up` after a `tower.sh down`, a crash loop after a deploy, or a full disk.

## 3. If it stays down

Tell the team, so nobody fights the hook: risky actions wait until Tower is back. Don't uninstall the hook to get past it.

For hangar, an admin can put an enforcing kind back to `shadow`, or turn on break-glass. Break-glass means hangar's local rules, two admins, at most 24 hours, audited. Both are in [break-glass: once Tower decides](break-glass-tower.md). Today no kind is in `enforce`, so there is nothing to change in hangar.

## 4. Afterwards

The alert closes ten minutes after the last fail-closed decision. Check that hangar's waiting approvals moved on: hangar re-reads each one's status once Tower answers. Open a KAN ticket for the outage, with Tower's cause.
