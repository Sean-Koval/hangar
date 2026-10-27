# Alerting: who hears, and how to test it

How hangar's [SLO](slos.md) alerts reach the on-call owner, how to turn them on, and how to fire each one once on purpose. The owner and the escalation path are in [the on-call runbook](runbooks/on-call.md).

## Turn it on

Alerting is Terraform, off by default, in `deploy/terraform/gke/alerting.tf`. It needs no Prometheus server and changes nothing in the cluster. Add to `terraform.tfvars`:

```hcl
enable_alerting        = true
alert_email            = "you@example.com"     # the on-call owner
alert_escalation_email = "backup@example.com"  # optional
alert_slack_channel    = "#hangar-alerts"      # optional, with TF_VAR_alert_slack_token
```

Then apply, in this order:

1. `terraform -chdir=deploy/terraform/gke apply` creates the channels, log-based metrics, uptime check, policies and dashboard.
2. Confirm the email: Cloud Monitoring sends nothing to an unverified address. Check the inbox once.
3. In Tower's checkout, run `bash deploy/gke/tower.sh up`. It reads the `alert_channels` output and sends Tower's alerts to the same people.
4. Fire each alert once ([below](#fire-each-alert-once)).

A Slack channel needs the Google Cloud Monitoring Slack app's token. Pass it as `TF_VAR_alert_slack_token`, never in a file. A log-based metric counts only lines written after it exists, so the dashboard starts empty.

## What fires, and where it goes

Every policy goes to the `hangar-oncall` channels. A page means act now; a ticket means today. An open alert is sent again every 30 minutes.

| Alert | Fires when | Severity | Runbook |
| --- | --- | --- | --- |
| `job-starts-failing` | At least 3 starts failed in an hour, over half of them | Page | [Stuck job](runbooks/stuck-job.md) |
| `job-starts-slow-burn` | Over 15% of a day's starts failed, at least 5 | Ticket | [Job starts](slos.md#job-starts) |
| `queue-slow` | Over a quarter of 3 hours' starts waited past 5 minutes | Ticket | [Stuck job](runbooks/stuck-job.md) |
| `sandbox-creates-failing` | At least 3 creates failed in an hour, over a fifth of them | Page | [Disk or quota](runbooks/disk-and-quota.md) |
| `jobs-stuck` | A live job looked stuck in the last 10 minutes | Ticket | [Stuck job](runbooks/stuck-job.md) |
| `tower-fail-closed` | hangar couldn't reach Tower and failed closed | Page | [Tower down](runbooks/tower-down.md) |
| `restarts` | The control plane restarted 3 times in 15 minutes | Page | [Bad deploy](runbooks/bad-deploy.md) |
| `data-disk` | The `hangar-data` disk is over 85% full for 15 minutes | Ticket | [Disk or quota](runbooks/disk-and-quota.md) |
| `node-disk` | A node's ephemeral storage is over 90% for 15 minutes | Ticket | [Disk or quota](runbooks/disk-and-quota.md) |
| `api-down` | `/healthz` fails from two regions for 10 minutes; needs `domain` | Page | [Bad deploy](runbooks/bad-deploy.md) |

With `alert_escalation_email` set, `tower-fail-closed` and `sandbox-creates-failing` have an escalated copy. It tells the escalation contact when the alert is still open after an hour. Tower's own alerts, such as Tower down, come from `tower.sh up`.

Installs that scrape `/metrics` apply `deploy/k8s/observability/prometheus-rules.yaml` instead. It has the same alerts, and routes them with Alertmanager on `severity`.

## Fire each alert once

The acceptance test is that every alert has fired once. After the apply, at a quiet hour:

```bash
bash deploy/gke/fire-test-alerts.sh              # the six log-based alerts
bash deploy/gke/fire-test-alerts.sh fail-closed  # or just one
DRY_RUN=1 bash deploy/gke/fire-test-alerts.sh    # print the commands only
```

It writes lines shaped like hangar's own to the `hangar-slo-test` log, which the metrics also count. Each alert's email should arrive within about 5 minutes. Each closes by itself once its window passes. No job runs and nothing in the cluster changes.

Real starts in the same hour dilute the test failures below the alerts' ratios, so run it while the pools are down. Note the time of each email in KAN-144.

## Drills for the rest

Four alerts need a real failure or a lowered threshold. Test them in a maintenance window:

- **`api-down`.** Scale hangar to zero for 12 minutes: `kubectl -n hangar scale deploy/hangar --replicas=0`. Then scale it back to 1. Jobs wait meanwhile.
- **`restarts`, `data-disk` and `node-disk`.** Open the policy in Cloud Monitoring and edit its query's threshold. Use `>= 0` for restarts and `> 0.01` for the disks. Wait 20 minutes for the email, then re-run `terraform apply` to put it back.
