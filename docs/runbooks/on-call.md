# On call: who responds, and the escalation path

Who answers hangar's and Tower's alerts, how fast, and what happens when nobody does. The alerts themselves are in [alerting](../alerting.md).

## The owner

Sean is the on-call owner to start, for both hangar and Tower. Terraform's `alert_email` is his address, and every alert reaches the `hangar-oncall` channels. When a second person joins the rota, add a Slack channel or a group address. Then update this page.

| Severity | Means | Acknowledge within | Resolve or hand off within |
| --- | --- | --- | --- |
| Page | Engineers can't start work, or nothing risky can be approved | 30 minutes, 10:00 to 19:00 ET on weekdays | 4 hours |
| Ticket | An error budget is burning, or something will break soon | The next working day | A week, as a Jira ticket in KAN |

Outside working hours a page waits for the morning, unless Sean chooses otherwise. The sandbox pools are off overnight anyway, so few jobs run then.

## When an alert arrives

1. Open the alert. Its documentation names the runbook.
2. Acknowledge it in Cloud Monitoring, so the 30-minute repeat stops.
3. Follow the runbook. Note what you did in the incident's KAN ticket, or open one.
4. When it's fixed, let the alert close by itself, or close it.
5. For a page, write three lines in the ticket: what broke, why, and what stops it happening again.

## Escalation

An open page is sent again every 30 minutes. When it is still open after an hour, `alert_escalation_email` hears about it too, for the alerts with an escalated copy.

Until a second engineer is on the rota, escalation is not a fix. It tells the team that hangar or Tower is degraded and Sean hasn't answered. They then act on their own:

- **hangar won't start jobs.** Work locally. Queued jobs keep their place and start when hangar recovers.
- **Tower is down.** The Claude Code hook on laptops blocks risky actions by design. A teammate can't override it; they wait, or work without the hook's risky tools. The [Tower down runbook](tower-down.md) has what an admin can do.
- **A credential leaked.** Anyone who finds a live secret revokes it at its source first. They tell Sean after: [credential leak](credential-leak.md).

## Runbooks

| Incident | Runbook |
| --- | --- |
| A job is stuck, or starts keep failing | [Stuck job](stuck-job.md) |
| A credential leaked | [Credential leak](credential-leak.md) |
| Tower is down, and hooks fail closed | [Tower down](tower-down.md) |
| A disk or a quota is full | [Disk or quota](disk-and-quota.md) |
| A deploy broke hangar | [Bad deploy](bad-deploy.md) |
