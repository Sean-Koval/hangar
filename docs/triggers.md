# Triggers: approvals, inbound webhooks and schedules

Besides the API, MCP, the CLI and GitHub issues ([jobs.md](jobs.md)), jobs can start from any system that sends webhooks, and on a schedule. GitHub triggers from people you don't trust wait for approval. The plan is in [spec 0011](specs/0011-factory-roadmap.md), workstream A.

## 1. Who can start work from GitHub

By default, a `/hangar` comment counts only when its author is an owner, member or collaborator of the repo. A label counts when the labeler has triage rights. The exception is an issue form that adds the label for whoever opens the issue: then the issue author's association decides.

To choose exactly who is trusted, list GitHub logins and teams:

```bash
HANGAR_GITHUB_TRUSTED='["sean", "renovate[bot]", "@acme/maintainers"]'
```

- With the list set, author associations no longer count: only the listed logins and active members of the listed teams are trusted.
- Team membership is checked with the control plane's GitHub token (`HANGAR_GITHUB_TOKEN_CREDENTIAL`) and cached for 10 minutes (`HANGAR_GITHUB_TEAM_CACHE_SECONDS`). The token needs read access to the organization's members: the **Members** organization permission for a fine-grained token or GitHub App, or `read:org` for a classic token. GitHub answers 404 when the token can't see the team, so a missing permission looks like "not a member". A pending invitation doesn't count, and lookup errors count as untrusted.
- The same rule decides which issue comments become context for the agent.

### Approval

When someone untrusted triggers a job (a `/hangar` comment, or a label on their own issue), hangar:

1. creates the job in state `awaiting_approval` and doesn't queue it;
2. comments on the issue that it is waiting for a trusted person.

A trusted person then decides:

| Approve | Reject |
| --- | --- |
| `/hangar approve` on the issue | `/hangar reject` on the issue |
| `POST /api/jobs/{id}/approve` (admin token) | `POST /api/jobs/{id}/reject` |
| MCP `approve_job` | MCP `reject_job` |
| `hangar job approve ID` | `hangar job reject ID` |

Approving queues the job and records who approved it (`approval.decided_by` on the job). Rejecting cancels it with reason `rejected`. A job nobody decides on is cancelled after `HANGAR_JOB_APPROVAL_TTL_HOURS` (default 72) with reason `approval_expired`. Waiting jobs survive restarts. `/hangar approve` and `/hangar reject` are always decisions, never instructions.

Every decision is in the audit log: `job.approve`, `job.reject`, `job.approval_expired` (actor `system`), and `job.approve.refused` / `job.reject.refused` when someone untrusted tries.

Untrusted people's `/hangar` comments on a PR that a job opened are ignored: only trusted people can continue a job. The same goes for answering a job's question: when a job asks for input on an issue (`needs_input`), only a trusted person's `/hangar <answer>` reaches the agent. Anyone else's is ignored and audited (`github.refused`), even when they opened the issue.

### Replays and limits

GitHub signs only the body, so hangar treats the body as the delivery: a second copy of an `issues` or `issue_comment` delivery it has already handled (GitHub's *Redeliver*, or a captured request sent again with another `X-GitHub-Delivery`) is answered `200 {"ignored": "duplicate delivery"}` and does nothing. Deliveries are remembered in the store for `HANGAR_WEBHOOK_REPLAY_HOURS` (7 days), across restarts. A delivery that failed or was rate limited is forgotten, so a redelivery runs.

Each GitHub user is a caller for [rate limits and the active-job cap](security.md#front-door-limits). Past a rate limit the webhook answers 429; a trusted person gets a comment saying so, someone untrusted gets none (so a flood of `/hangar` comments can't become a flood of hangar comments). Jobs waiting for approval count toward their requester's cap.

### Untrusted text in the prompt

Text written by someone who isn't trusted is marked in the agent's prompt as untrusted input that can't override the instructions:
- the issue body, when the issue's author isn't trusted (the file is listed as `untrusted`);
- the extra text of an untrusted person's `/hangar` comment (quoted as a request, not as instructions);
- everything that came from an inbound webhook.

The prompt also tells the agent to ignore requests in that text to reveal credentials, contact other hosts or change anything outside the job's repo and branch. This lowers the risk of prompt injection; it doesn't remove it. Approve only what you have read.

## 2. Inbound webhooks

Systems that can only send webhooks (Sentry, Linear, PagerDuty, internal services) start jobs at `POST /api/hooks/inbound/<source>`. Each source is configured in `HANGAR_INBOUND_WEBHOOKS`, a JSON mapping from source name to its settings:

```json
{
  "sentry": {
    "secret": "sentry-hook",
    "signature": {"header": "Sentry-Hook-Signature", "timestamp_header": "Sentry-Hook-Timestamp"},
    "delivery_header": "Request-ID",
    "repo": "acme/app",
    "environment": "python",
    "filter": {"field": "action", "equals": "created"},
    "template": {
      "title": "Sentry: {data.issue.title}",
      "instructions": "Fix the error {data.issue.title} in {data.issue.culprit}. The event is in sentry-event.json.",
      "context": [{"name": "sentry-event.json", "kind": "log", "content": "{data}"}],
      "labels": {"sentry.issue": "{data.issue.id}"}
    }
  }
}
```

Store the signing secret in the vault: `hangar cred add sentry-hook --harness webhook --kind generic`.

| Field | Meaning |
| --- | --- |
| `secret` | Vault credential with the HMAC key. |
| `signature.header` | Header with the signature (default `X-Signature`). |
| `signature.prefix` | Text before the digest: `""` (default), `"sha256="` (GitHub style) or `"v1="` (PagerDuty). Several comma-separated signatures are accepted, for secret rotation. |
| `signature.encoding` | `hex` (default) or `base64`. |
| `signature.timestamp_header`, `max_skew_seconds` | Optional. Requests whose timestamp (seconds or milliseconds) is more than `max_skew_seconds` (default 300) away from now are rejected. |
| `signature.signed_payload` | What is signed: `{body}` (default), or e.g. `{timestamp}.{body}` for schemes that sign the timestamp too. |
| `delivery_header` | Header with a unique delivery id, used for idempotency. Without one, the hash of the body is used. Either way, a delivery whose signed content (the body, plus the timestamp when `signed_payload` includes it) was already seen is a duplicate, whatever its delivery id. |
| `repo`, `environment` | Where the job runs. The repo must be listed in `HANGAR_GITHUB_REPOS`, and the environment must be one mapped to it (default: the first). |
| `harness`, `model`, `verify`, `deliver` | Fixed job options for this source. |
| `template` | `title`, `instructions`, `context` (`name`, `kind`, `content`) and `labels`. `{a.b.c}` is replaced by that payload field (`{list.0}` indexes a list, `{$}` is the whole payload; objects become JSON; missing fields become empty). Nothing else is evaluated. |
| `filter` | Optional `{"field": "a.b", "equals": value}`, or a list of them (all must match). Other deliveries get `200 {"ignored": ...}`. |

Formats verified against the providers' docs (October 2026):
- **Sentry:** `Sentry-Hook-Signature`, hex HMAC-SHA256 of the body; `Sentry-Hook-Timestamp`; `Request-ID` as the delivery id.
- **Linear:** `Linear-Signature`, hex HMAC-SHA256 of the raw body; `Linear-Timestamp` in milliseconds (Linear recommends a 60 s window); `Linear-Delivery`.
- **PagerDuty v3:** `X-PagerDuty-Signature` with `prefix: "v1="`, one or more comma-separated hex HMAC-SHA256 signatures of the body. PagerDuty documents no delivery id header, so the body hash is used.

Responses:

| Status | When |
| --- | --- |
| `202 {"ok": true, "job": "job-…"}` | A job was started. A redelivery, or a replay with a new delivery id, returns the same job with `"duplicate": true` and starts nothing. |
| `200 {"ignored": …}` | The filter didn't match. |
| `400` | The body isn't a JSON object. |
| `401` | Missing or bad signature, or a stale timestamp. |
| `404` | No such source. |
| `413` | Body larger than `HANGAR_INBOUND_MAX_BODY_BYTES` (1 MB). |
| `422` | The payload doesn't make a valid job (e.g. empty instructions, repo not mapped, too many active jobs). |
| `429` | The source is over its [rate limit](security.md#front-door-limits); the sender should retry later. |
| `503` | The source is misconfigured (bad settings, or its secret isn't in the vault). |

Signatures are compared in constant time. Jobs from a webhook have requester and owner `webhook:<source>`, are fenced like a `jobs` service token (only the configured, mapped repo and its environments), and their text is untrusted input to the agent.

**Replays.** A delivery is identified by what its signature covers. The delivery id header isn't signed, and neither is a timestamp header unless `signed_payload` contains `{timestamp}`, so whoever replays a captured request can change both; hangar ignores them for that decision and remembers signed content for `HANGAR_WEBHOOK_REPLAY_HOURS` (7 days, across restarts). `max_skew_seconds` protects only a timestamp that is signed: Sentry's `Sentry-Hook-Timestamp`, for example, isn't part of its signature. Where a provider signs a timestamp, configure it (`signed_payload: "{timestamp}.{body}"`): a replay is then rejected outright once the timestamp is stale.

**Limits.** Each source is one caller for [rate limits and the active-job cap](security.md#front-door-limits): an alert storm starts at most 10 jobs at once and then 30 a minute, by default.

## 3. Scheduled jobs

A schedule submits the same job on a cron schedule:

```bash
hangar schedule create deps "Update dependencies and fix what breaks" --cron "0 6 * * 1" --tz Europe/Berlin \
  --repo https://github.com/acme/app --env python --verify "pytest -q"
hangar schedule list
hangar schedule run deps          # submit it now; regular runs are unaffected
hangar schedule disable deps      # and enable
hangar schedule rm deps
```

The same over REST (`/api/schedules`: `GET`, `POST`, `GET/PATCH/DELETE /{id or name}`, `POST /{id}/run`) and MCP (`create_schedule`, `list_schedules`, `delete_schedule`, `run_schedule_now`). Schedules need an admin token.

- **Cron:** 5 fields (minute, hour, day of month, month, day of week) with `*`, lists, ranges, steps and names (`mon-fri`, `jan`), plus `@hourly`, `@daily`, `@weekly`, `@monthly` and `@yearly`. When both day fields are restricted, a day matching either runs, as in standard cron. Times are in the schedule's IANA `timezone`. A time skipped when clocks go forward doesn't run that day; a time repeated when clocks go back runs once.
- **Job:** any `submit_job` fields except `branch` and `idempotency_key`. It is validated when the schedule is created.
- **Once per slot:** the slot is saved before the job is submitted, and each job's idempotency key is `schedule:<id>:<slot>`, so a slot never runs twice, even across restarts.
- **No backlog:** after downtime, only the most recent missed slot runs.
- **No overlap:** a slot is skipped (and `last_error` says why) while the schedule's previous job is still active, unless `allow_overlap` is set.
- The scheduler checks every `HANGAR_SCHEDULE_POLL_SECONDS` (10). A run that can't start (say, the environment was deleted) records why in `last_error`.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_GITHUB_TRUSTED` | `[]` | Trusted logins and `@org/team`s; unset: owners, members and collaborators |
| `HANGAR_GITHUB_TEAM_CACHE_SECONDS` | 600 | |
| `HANGAR_JOB_APPROVAL_TTL_HOURS` | 72 | Unapproved jobs are cancelled after this |
| `HANGAR_INBOUND_WEBHOOKS` | `{}` | See §2 |
| `HANGAR_INBOUND_MAX_BODY_BYTES` | 1000000 | |
| `HANGAR_SCHEDULE_POLL_SECONDS` | 10 | Scheduler and approval-expiry loop |
| `HANGAR_WEBHOOK_REPLAY_HOURS` | 168 | How long signed GitHub and inbound deliveries are remembered |

Schedules aren't rate limited: each slot runs at most once by design. Rate limits and caps are in [security.md](security.md#front-door-limits).
