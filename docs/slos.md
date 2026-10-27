# What "up" means for hangar: the SLOs

Engineers depend on hangar to run their work. This page says what hangar promises, how each promise is measured, and how much failure it allows. [Alerting](alerting.md) says who hears when a promise is at risk. Tower's SLOs are on [Tower's own page](https://sean-koval.github.io/tower/slos/).

## The SLOs

Each error budget is over a rolling 28 days.

| SLO | Target | Error budget | Alert |
| --- | --- | --- | --- |
| [Job starts](#job-starts) | 97% of job starts reach `running` | 3% of starts may fail | `job-starts-failing` (page), `job-starts-slow-burn` (ticket) |
| [Queued to running](#queued-to-running) | 95% of starts leave the queue within 5 minutes | 5% may wait longer | `queue-slow` (ticket) |
| [Sandbox creates](#sandbox-creates) | At most 2% of sandbox creates fail | 2% of creates | `sandbox-creates-failing` (page) |

The latency targets for creates, turn starts and resumes stay where they were. `hangar status` shows them ([observability](observability.md#slos)).

## Where the numbers come from

hangar writes one log line per signal, from `src/hangar/slolog.py`:

```text
hangar.slo: event=job_start result=ok queued_s=4.2 held=no reason=-
hangar.slo: event=sandbox_create result=error provider=kubernetes environment=base seconds=12.5 error=FleetError
```

Sean's cluster has no Prometheus server. So Cloud Monitoring counts these lines as log-based metrics, from `deploy/terraform/gke/alerting.tf`. Installs that scrape `/metrics` get the same signals as Prometheus metrics, with rules in `deploy/k8s/observability/prometheus-rules.yaml`.

Cloud Monitoring reads both kinds of metric in PromQL. A log-based metric `hangar_job_starts` is `logging_googleapis_com:user_hangar_job_starts` there. The dashboard is **hangar SLOs** in Cloud Monitoring; terraform's `slo_dashboard` output links it.

## Job starts

A start is a job leaving `queued` or `provisioning` for `running` (ok) or `failed` (error). A failure the requester caused, such as a branch that already exists, is `rejected` and left out. Cancellations aren't starts.

```promql
# Cloud Monitoring (log-based metric)
sum(increase(logging_googleapis_com:user_hangar_job_starts{result="ok"}[28d]))
  / sum(increase(logging_googleapis_com:user_hangar_job_starts{result=~"ok|error"}[28d]))
# Prometheus (/metrics)
sum(increase(hangar_job_starts_total{result="ok"}[28d]))
  / sum(increase(hangar_job_starts_total{result=~"ok|error"}[28d]))
```

The page fires when at least 3 starts failed in an hour and they were over half of them. That burns the budget about 17 times too fast. The ticket fires at 15% over a day, with at least 5 failures.

## Queued to running

Each started job reports how long it waited, from the moment it was last queued. A follow-up or an approval queues it again. A job held on purpose is left out, marked `held=yes`. That covers the powered-down night, the review-capacity cap and the eval queue.

```promql
# Cloud Monitoring
sum(increase(logging_googleapis_com:user_hangar_job_queue_seconds_bucket{le="300"}[28d]))
  / sum(increase(logging_googleapis_com:user_hangar_job_queue_seconds_count[28d]))
# Prometheus
sum(increase(hangar_job_queue_seconds_bucket{le="300.0"}[28d]))
  / sum(increase(hangar_job_queue_seconds_count[28d]))
```

The ticket fires when over a quarter of 3 hours' starts waited past 5 minutes, with at least 4 starts.

## Sandbox creates

Every sandbox create counts, ok or not, KAN-136's failures among them. A create refused because the pools are powered down isn't a failure: its job waits for the morning.

```promql
# Cloud Monitoring
sum(increase(logging_googleapis_com:user_hangar_sandbox_creates{result="error"}[28d]))
  / sum(increase(logging_googleapis_com:user_hangar_sandbox_creates[28d]))
# Prometheus
sum(increase(hangar_sandbox_create_seconds_count{result="error"}[28d]))
  / sum(increase(hangar_sandbox_create_seconds_count[28d]))
```

The page fires when at least 3 creates failed in an hour and they were over a fifth of them.

## Signals without an SLO

These alert, but promise no rate:

- **Stuck jobs.** Every minute hangar counts live jobs that look stuck. That is a runnable job queued for an hour, or a sandbox not up in 20 minutes. It is also a job still running 30 minutes past its own timeout. The log line is `event=jobs_stuck`; the gauge is `hangar_jobs_stuck`.
- **Failing closed.** `event=fail_closed` marks a decision hangar answered itself because Tower didn't. It is never an allow. It starts counting once hangar asks Tower ([#172](https://github.com/Sean-Koval/hangar/issues/172)).
- **Restarts and disks.** GKE's own metrics: the control plane's restarts, its data disk and each node's ephemeral storage.
