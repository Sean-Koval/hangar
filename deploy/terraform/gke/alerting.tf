# --------------------------------------------------------------------------- SLOs, alerts and the dashboard (KAN-144)
# hangar's SLOs (docs/slos.md) measured and alerted on with Google Cloud Monitoring, because this cluster runs no
# Prometheus server. hangar writes one `hangar.slo: event=...` line per signal to its container log (src/hangar/slolog.py);
# the log-based metrics below count them. Off unless enable_alerting = true; nothing here touches the cluster.
#
#   enable_alerting        = true
#   alert_email            = "sean@example.com"     # the on-call owner (docs/runbooks/on-call.md)
#   alert_escalation_email = "backup@example.com"   # optional: page alerts still open after an hour go here too
#   alert_slack_channel    = "#hangar-alerts"       # optional, with alert_slack_token (the Monitoring Slack app's)
#
# Every policy goes to the "hangar-oncall" channels: the email, and Slack when set. Output alert_channels lists them;
# Tower's deploy/gke/tower.sh up reads it and routes Tower's alerts to the same people. Fire each alert once after the
# apply with scripts/fire-test-alerts.sh (docs/alerting.md). Installs that scrape /metrics use
# deploy/k8s/observability/prometheus-rules.yaml instead: the same SLOs over hangar's own metrics.

variable "enable_alerting" {
  description = "Cloud Monitoring log-based metrics, alert policies, uptime check and dashboard for hangar's SLOs (docs/slos.md)."
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Where alerts go: the on-call owner's email (a Cloud Monitoring email channel named hangar-oncall)."
  type        = string
  default     = ""
}

variable "alert_escalation_email" {
  description = "Optional: who hears about a page alert still open after alert_escalate_after (docs/runbooks/on-call.md)."
  type        = string
  default     = ""
}

variable "alert_escalate_after" {
  description = "How long a page alert stays open before the escalation contact is told, as a duration (\"3600s\")."
  type        = string
  default     = "3600s"
}

variable "alert_slack_channel" {
  description = "Optional Slack channel for alerts (\"#hangar-alerts\"); needs alert_slack_token."
  type        = string
  default     = ""
}

variable "alert_slack_token" {
  description = "The Google Cloud Monitoring Slack app's OAuth token for alert_slack_channel. Keep it out of git: TF_VAR_alert_slack_token."
  type        = string
  default     = ""
  sensitive   = true
}

variable "hangar_namespace" {
  description = "The Kubernetes namespace hangar's control plane runs in (its container is named hangar)."
  type        = string
  default     = "hangar"
}

locals {
  alerting = var.enable_alerting
  docs     = "https://sean-koval.github.io/hangar"
  # Real lines from hangar's container, and the lines scripts/fire-test-alerts.sh writes to the hangar-slo-test log.
  slo_source = join(" ", [
    "((resource.type=\"k8s_container\" AND resource.labels.namespace_name=\"${var.hangar_namespace}\"",
    "AND resource.labels.container_name=\"hangar\")",
    "OR logName=\"projects/${var.project_id}/logs/hangar-slo-test\")",
  ])
  # SLO targets (docs/slos.md). Each error budget is over 28 days.
  slo = {
    job_start_success = 0.97 # of job starts reach running
    queue_seconds     = 300  # 95% of starts go from queued to running within this
    queue_share       = 0.95
    create_failure    = 0.02 # at most this share of sandbox creates fail
  }
  channels = concat(
    google_monitoring_notification_channel.email[*].id,
    google_monitoring_notification_channel.slack[*].id,
  )
  escalation = google_monitoring_notification_channel.escalation[*].id
  # Log-based metrics read in PromQL: logging.googleapis.com/user/NAME is logging_googleapis_com:user_NAME.
  m = {
    starts  = "logging_googleapis_com:user_hangar_job_starts"
    queue   = "logging_googleapis_com:user_hangar_job_queue_seconds"
    creates = "logging_googleapis_com:user_hangar_sandbox_creates"
    closed  = "logging_googleapis_com:user_hangar_fail_closed"
    stuck   = "logging_googleapis_com:user_hangar_jobs_stuck"
    lag     = "logging_googleapis_com:user_hangar_deploy_lag"
  }
}

check "alerting_has_a_channel" {
  assert {
    condition     = !var.enable_alerting || var.alert_email != "" || (var.alert_slack_channel != "" && var.alert_slack_token != "")
    error_message = "enable_alerting needs alert_email (or alert_slack_channel with alert_slack_token): alerts nobody receives page nobody."
  }
}

resource "google_project_service" "alerting" {
  for_each           = local.alerting ? toset(["monitoring.googleapis.com", "logging.googleapis.com"]) : toset([])
  service            = each.value
  disable_on_destroy = false
}

# --------------------------------------------------------------------------- who hears about it
resource "google_monitoring_notification_channel" "email" {
  count        = local.alerting && var.alert_email != "" ? 1 : 0
  display_name = "hangar-oncall (email)"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [google_project_service.alerting]
}

resource "google_monitoring_notification_channel" "slack" {
  count        = local.alerting && var.alert_slack_channel != "" && var.alert_slack_token != "" ? 1 : 0
  display_name = "hangar-oncall (Slack ${var.alert_slack_channel})"
  type         = "slack"
  labels       = { channel_name = var.alert_slack_channel }
  sensitive_labels {
    auth_token = var.alert_slack_token
  }
  depends_on = [google_project_service.alerting]
}

resource "google_monitoring_notification_channel" "escalation" {
  count        = local.alerting && var.alert_escalation_email != "" ? 1 : 0
  display_name = "hangar-oncall escalation (email)"
  type         = "email"
  labels       = { email_address = var.alert_escalation_email }
  depends_on   = [google_project_service.alerting]
}

# --------------------------------------------------------------------------- log-based metrics (src/hangar/slolog.py)
resource "google_logging_metric" "job_starts" {
  count       = local.alerting ? 1 : 0
  name        = "hangar_job_starts"
  description = "hangar job starts by result: ok (reached running), error, rejected (the requester's mistake)"
  filter      = "${local.slo_source} AND textPayload:\"hangar.slo: event=job_start \""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key        = "result"
      value_type = "STRING"
    }
  }
  label_extractors = { result = "REGEXP_EXTRACT(textPayload, \"result=([a-z]+)\")" }
  depends_on       = [google_project_service.alerting]
}

resource "google_logging_metric" "job_queue_seconds" {
  count           = local.alerting ? 1 : 0
  name            = "hangar_job_queue_seconds"
  description     = "Seconds from queued to running, for each job that started and wasn't held on purpose"
  filter          = "${local.slo_source} AND textPayload:\"hangar.slo: event=job_start result=ok \" AND textPayload:\" held=no \""
  value_extractor = "REGEXP_EXTRACT(textPayload, \"queued_s=([0-9.]+)\")"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
  }
  bucket_options {
    explicit_buckets {
      bounds = [5, 15, 30, 60, 120, 300, 600, 1800, 3600]
    }
  }
  depends_on = [google_project_service.alerting]
}

resource "google_logging_metric" "sandbox_creates" {
  count       = local.alerting ? 1 : 0
  name        = "hangar_sandbox_creates"
  description = "hangar sandbox creates by result (ok, error); a power-down isn't counted"
  filter      = "${local.slo_source} AND textPayload:\"hangar.slo: event=sandbox_create \""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key        = "result"
      value_type = "STRING"
    }
    labels {
      key        = "provider"
      value_type = "STRING"
    }
  }
  label_extractors = {
    result   = "REGEXP_EXTRACT(textPayload, \"result=([a-z]+)\")"
    provider = "REGEXP_EXTRACT(textPayload, \"provider=([A-Za-z0-9_.:/-]+)\")"
  }
  depends_on = [google_project_service.alerting]
}

resource "google_logging_metric" "fail_closed" {
  count       = local.alerting ? 1 : 0
  name        = "hangar_fail_closed"
  description = "Decisions hangar answered itself because Tower didn't (KAN-78): nothing risky was allowed"
  filter      = "${local.slo_source} AND textPayload:\"hangar.slo: event=fail_closed \""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key        = "failure"
      value_type = "STRING"
    }
  }
  label_extractors = { failure = "REGEXP_EXTRACT(textPayload, \"failure=([a-z_]+)\")" }
  depends_on       = [google_project_service.alerting]
}

resource "google_logging_metric" "jobs_stuck" {
  count       = local.alerting ? 1 : 0
  name        = "hangar_jobs_stuck"
  description = "One a minute while any live job looks stuck (src/hangar/slolog.py stuck_jobs)"
  filter      = "${local.slo_source} AND textPayload:\"hangar.slo: event=jobs_stuck \""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.alerting]
}

# KAN-216: production more than HANGAR_DEPLOY_LAG_ALERT_WORKING_DAYS behind main while the infra is up (hangar decides,
# so the night's power-down and a hold never write the line)
resource "google_logging_metric" "deploy_lag" {
  count       = local.alerting ? 1 : 0
  name        = "hangar_deploy_lag"
  description = "One a minute while production is too far behind main and the infra is up (src/hangar/deploylag.py)"
  filter      = "${local.slo_source} AND textPayload:\"hangar.slo: event=deploy_lag \""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.alerting]
}

# --------------------------------------------------------------------------- the control plane answers at all
resource "google_monitoring_uptime_check_config" "hangar" {
  count        = local.alerting && var.domain != "" ? 1 : 0
  display_name = "hangar-api"
  timeout      = "10s"
  period       = "300s"
  http_check {
    path         = "/healthz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }
  monitored_resource {
    type   = "uptime_url"
    labels = { project_id = var.project_id, host = var.domain }
  }
  depends_on = [google_project_service.alerting]
}

# --------------------------------------------------------------------------- alert policies
# `page`: act now (docs/runbooks/on-call.md); `ticket`: today. Each names its runbook.
locals {
  promql_policies = {
    job-starts-failing = {
      severity = "CRITICAL"
      title    = "hangar: job starts are failing (fast burn of the job-start SLO)"
      # Half of the last hour's starts failed, and at least 3: about 17x the 3% budget's burn rate.
      query    = "(sum(increase(${local.m.starts}{result=\"error\"}[1h])) >= 3) and (sum(increase(${local.m.starts}{result=\"error\"}[1h])) / sum(increase(${local.m.starts}{result=~\"ok|error\"}[1h])) > 0.5)"
      duration = "0s"
      runbook  = "runbooks/stuck-job/"
    }
    job-starts-slow-burn = {
      severity = "WARNING"
      title    = "hangar: job starts failing above budget (slow burn, 24 h)"
      query    = "(sum(increase(${local.m.starts}{result=\"error\"}[24h])) >= 5) and (sum(increase(${local.m.starts}{result=\"error\"}[24h])) / sum(increase(${local.m.starts}{result=~\"ok|error\"}[24h])) > 0.15)"
      duration = "0s"
      runbook  = "slos/#job-starts"
    }
    queue-slow = {
      severity = "WARNING"
      title    = "hangar: jobs wait too long to start (queued to running SLO)"
      # Over 3 hours, more than a quarter of starts waited past ${local.slo.queue_seconds} s (the SLO allows 5%).
      query    = "(sum(increase(${local.m.queue}_count[3h])) >= 4) and (1 - sum(increase(${local.m.queue}_bucket{le=\"300\"}[3h])) / sum(increase(${local.m.queue}_count[3h])) > 0.25)"
      duration = "0s"
      runbook  = "runbooks/stuck-job/"
    }
    sandbox-creates-failing = {
      severity = "CRITICAL"
      title    = "hangar: sandbox creates are failing"
      query    = "(sum(increase(${local.m.creates}{result=\"error\"}[1h])) >= 3) and (sum(increase(${local.m.creates}{result=\"error\"}[1h])) / sum(increase(${local.m.creates}[1h])) > 0.2)"
      duration = "0s"
      runbook  = "runbooks/disk-and-quota/"
    }
    jobs-stuck = {
      severity = "WARNING"
      title    = "hangar: a job looks stuck"
      query    = "sum(increase(${local.m.stuck}[10m])) > 0"
      duration = "0s"
      runbook  = "runbooks/stuck-job/"
    }
    deploy-lag = {
      severity = "WARNING"
      title    = "hangar: production is too far behind main (a deploy waits for approval)"
      query    = "sum(increase(${local.m.lag}[10m])) > 0"
      duration = "0s"
      runbook  = "runbooks/deploy/"
    }
    tower-fail-closed = {
      severity = "CRITICAL"
      title    = "hangar: Tower isn't answering, so hangar is failing closed"
      query    = "sum(increase(${local.m.closed}{failure=~\"unreachable|server_error|circuit_open|rate_limited\"}[10m])) > 0"
      duration = "0s"
      runbook  = "runbooks/tower-down/"
    }
    restarts = {
      severity = "CRITICAL"
      title    = "hangar: the control plane keeps restarting (a bad deploy?)"
      query    = "sum(increase(kubernetes_io:container_restart_count{monitored_resource=\"k8s_container\",namespace_name=\"${var.hangar_namespace}\",container_name=\"hangar\"}[15m])) >= 3"
      duration = "0s"
      runbook  = "runbooks/bad-deploy/"
    }
    data-disk = {
      severity = "WARNING"
      title    = "hangar: its data disk is over 85% full"
      query    = "max(kubernetes_io:pod_volume_utilization{monitored_resource=\"k8s_pod\",namespace_name=\"${var.hangar_namespace}\",volume_name=\"data\"}) > 0.85"
      duration = "900s"
      runbook  = "runbooks/disk-and-quota/"
    }
    node-disk = {
      severity = "WARNING"
      title    = "hangar: a node's ephemeral storage is over 90% used"
      query    = "max by (node_name) (kubernetes_io:node_ephemeral_storage_used_bytes{monitored_resource=\"k8s_node\"} / kubernetes_io:node_ephemeral_storage_total_bytes{monitored_resource=\"k8s_node\"}) > 0.9"
      duration = "900s"
      runbook  = "runbooks/disk-and-quota/"
    }
  }
  # Page alerts the escalation contact hears about when still open after alert_escalate_after.
  escalate = local.alerting && var.alert_escalation_email != "" ? toset(["tower-fail-closed", "sandbox-creates-failing"]) : toset([])
}

resource "google_monitoring_alert_policy" "slo" {
  for_each              = local.alerting ? local.promql_policies : {}
  display_name          = "hangar ${each.key}"
  combiner              = "OR"
  severity              = each.value.severity
  notification_channels = local.channels
  user_labels           = { product = "hangar", alert = each.key }
  conditions {
    display_name = each.value.title
    condition_prometheus_query_language {
      query               = each.value.query
      duration            = each.value.duration
      evaluation_interval = "60s"
    }
  }
  alert_strategy {
    auto_close = "86400s"
    dynamic "notification_channel_strategy" { # unanswered: told again every 30 minutes (docs/runbooks/on-call.md)
      for_each = length(local.channels) > 0 ? [1] : []
      content {
        notification_channel_names = local.channels
        renotify_interval          = "1800s"
      }
    }
  }
  documentation {
    mime_type = "text/markdown"
    subject   = each.value.title
    content   = "Runbook: ${local.docs}/${each.value.runbook}\n\nOn call and escalation: ${local.docs}/runbooks/on-call/. SLOs: ${local.docs}/slos/."
  }
  depends_on = [google_logging_metric.job_starts, google_logging_metric.job_queue_seconds,
  google_logging_metric.sandbox_creates, google_logging_metric.fail_closed, google_logging_metric.jobs_stuck,
  google_logging_metric.deploy_lag]
}

resource "google_monitoring_alert_policy" "escalated" {
  for_each              = local.escalate
  display_name          = "hangar ${each.key} (escalated)"
  combiner              = "OR"
  severity              = "CRITICAL"
  notification_channels = concat(local.channels, local.escalation)
  user_labels           = { product = "hangar", alert = "${each.key}-escalated" }
  conditions {
    display_name = "${local.promql_policies[each.key].title}, unresolved for ${var.alert_escalate_after}"
    condition_prometheus_query_language {
      query               = local.promql_policies[each.key].query
      duration            = var.alert_escalate_after
      evaluation_interval = "60s"
    }
  }
  documentation {
    mime_type = "text/markdown"
    subject   = "Escalated: ${local.promql_policies[each.key].title}"
    content   = "The on-call owner hasn't resolved this. Escalation: ${local.docs}/runbooks/on-call/#escalation. Runbook: ${local.docs}/${local.promql_policies[each.key].runbook}"
  }
  depends_on = [google_monitoring_alert_policy.slo]
}

resource "google_monitoring_alert_policy" "uptime" {
  count                 = local.alerting && var.domain != "" ? 1 : 0
  display_name          = "hangar api-down"
  combiner              = "OR"
  severity              = "CRITICAL"
  notification_channels = local.channels
  user_labels           = { product = "hangar", alert = "api-down" }
  conditions {
    display_name = "hangar's /healthz fails from two or more regions for 10 minutes"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.hangar[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
    }
  }
  alert_strategy {
    auto_close = "86400s"
  }
  documentation {
    mime_type = "text/markdown"
    subject   = "hangar's control plane isn't answering"
    content   = "Runbook: ${local.docs}/runbooks/bad-deploy/ (a deploy), then ${local.docs}/troubleshooting/. On call: ${local.docs}/runbooks/on-call/."
  }
}

# --------------------------------------------------------------------------- the dashboard: each SLO and its target
locals {
  tile = {
    job_start = "sum(increase(${local.m.starts}{result=\"ok\"}[28d])) / sum(increase(${local.m.starts}{result=~\"ok|error\"}[28d]))"
    queue     = "sum(increase(${local.m.queue}_bucket{le=\"300\"}[28d])) / sum(increase(${local.m.queue}_count[28d]))"
    creates   = "sum(increase(${local.m.creates}{result=\"error\"}[28d])) / sum(increase(${local.m.creates}[28d]))"
    queue_p95 = "histogram_quantile(0.95, sum by (le) (rate(${local.m.queue}_bucket[1h])))"
    closed    = "sum by (failure) (increase(${local.m.closed}[1h]))"
    stuck     = "sum(increase(${local.m.stuck}[10m]))"
  }
  widget = { for k, v in {
    job_start = ["Job starts that reach running, 28 days (SLO 97%)", local.tile.job_start, local.slo.job_start_success]
    queue     = ["Starts queued under 5 minutes, 28 days (SLO 95%)", local.tile.queue, local.slo.queue_share]
    creates   = ["Sandbox creates that fail, 28 days (SLO at most 2%)", local.tile.creates, local.slo.create_failure]
    queue_p95 = ["Queued to running, p95 over 1 h (seconds)", local.tile.queue_p95, local.slo.queue_seconds]
    closed    = ["Decisions hangar failed closed, by failure (1 h)", local.tile.closed, 0]
    stuck     = ["Stuck-job minutes (10 min)", local.tile.stuck, 0]
    } : k => {
    title = v[0]
    xyChart = {
      dataSets   = [{ plotType = "LINE", timeSeriesQuery = { prometheusQuery = v[1] } }]
      thresholds = [for t in [v[2]] : { value = t, label = "target" } if t > 0]
    }
  } }
}

resource "google_monitoring_dashboard" "slo" {
  count = local.alerting ? 1 : 0
  dashboard_json = jsonencode({
    displayName = "hangar SLOs"
    mosaicLayout = {
      columns = 12
      tiles = [for i, k in ["job_start", "queue", "creates", "queue_p95", "closed", "stuck"] : {
        xPos   = (i % 2) * 6
        yPos   = floor(i / 2) * 4
        width  = 6
        height = 4
        widget = local.widget[k]
      }]
    }
  })
  depends_on = [google_logging_metric.job_starts, google_logging_metric.job_queue_seconds,
  google_logging_metric.sandbox_creates, google_logging_metric.fail_closed, google_logging_metric.jobs_stuck]
}

output "alert_channels" {
  description = "The hangar-oncall notification channels, comma-separated: Tower's tower.sh up routes its alerts to them."
  value       = join(",", local.channels)
}

output "alert_escalation_channels" {
  description = "The escalation channel, if any: Tower's tower.sh up sends its escalated Tower-down alert there too."
  value       = join(",", local.escalation)
}

output "slo_dashboard" {
  value = local.alerting ? "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.slo[0].id), 3)}?project=${var.project_id}" : null
}
