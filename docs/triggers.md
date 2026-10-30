# Triggers: approvals, inbound webhooks and schedules

Besides the API, MCP, the CLI and GitHub issues ([jobs/README.md](jobs/README.md)), jobs can start from any system that sends webhooks, and on a schedule. GitHub triggers from people you don't trust wait for approval. The plan is in [spec 0011](specs/0011-factory-roadmap.md), workstream A.

## 1. Who can start work from GitHub

An account an admin linked to a hangar person (`hangar person link`) is always trusted: it acts as that person ([team.md](team/who-is-trusted.md)), and a person's job is continued from GitHub only by their own linked account. For everyone else, by default, a `/hangar` comment counts only when its author is an owner, member or collaborator of the repo. A label counts when the labeler has triage rights. The exception is an issue form that adds the label for whoever opens the issue: then the issue author's association decides.

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
| `POST /api/jobs/{id}/approve` | `POST /api/jobs/{id}/reject` |
| MCP `approve_job` | MCP `reject_job` |
| `hangar job approve ID` | `hangar job reject ID` |

Over REST the body may carry a `reason` (`{"reason": "..."}`, up to 2,000 characters). It is kept on the job as
`approval.decision_reason`, goes into the job's event and the audit entry, and is told to the requester where the job
was asked for (the issue comment). The web console requires a reason to reject and makes it optional to approve.

#### Who may approve

One rule decides on every path (`src/hangar/approvals.py`), so MCP and REST give the same answer and the same refusal text:

- **An admin, or one of the repo's approvers.** A person with role `admin`, or a linked person whose GitHub login is in the job repo's policy `approvers` ([repo-policy.md](repo-policy.md), the list landing uses). A repo approver sees that repo's held jobs over MCP and REST, even when they're someone else's.
- **With a login token.** Over MCP and REST, the token must hold `admin` or `user` (a person's own login; an OAuth grant counts as the login that approved it). An `mcp`-only token, which `hangar connect` writes into client configs, and a `jobs` service token can't approve or reject.
- **At the front doors, also the door's own trust, for team jobs.** A job nobody in particular asked for (an outsider's `/hangar`, an inbound webhook) can also be decided by anyone the door trusts: at GitHub a linked account or one the repo trusts (author association, `HANGAR_GITHUB_TRUSTED`), at Jira a linked account. A person's own held job, such as one a spent daily cap holds, is only an admin's or a repo approver's to release.
- **Never your own.** An approval is refused when the approver is the job's person, or when the approver (their MCP or REST identity, the commenting account, or any account linked to them) is the job's requester. That covers a spent daily cap, where the requester would otherwise approve their own job. Rejecting your own job is fine.
- **Solo installs.** `HANGAR_SELF_APPROVAL=allow_solo` lets the only person approve their own jobs while exactly one person exists (for example, a deployment with only legacy tokens, which all belong to the owner). The job and the audit entry record `self_approved: true`. With a second person, self-approval is refused again. The default is `deny`; `hangar doctor` warns when `deny` is set and only one person exists.

A refused `/hangar approve` gets a comment on the issue that says why.

Approving queues the job and records who approved it on the job's `approval`: `decided_by`, `decided_person`, `self_approved`, and the `rule` that allowed it (`admin`, `repo_approver`, `front_door`, `solo`, or `system` for hangar's own code). Rejecting cancels it with reason `rejected`. A job nobody decides on is cancelled after `HANGAR_JOB_APPROVAL_TTL_HOURS` (default 72) with reason `approval_expired`. Waiting jobs survive restarts. `/hangar approve` and `/hangar reject` are always decisions, never instructions.

Every decision is in the audit log: `job.approve` and `job.reject` (with `decided_person`, `self_approved` and `rule`), `job.approval_expired` (actor `system`), and `job.approve.refused` / `job.reject.refused` when someone who may not decide tries (with why).

Untrusted people's `/hangar` comments on a PR that a job opened are ignored: only trusted people can continue a job. The same goes for answering a job's question: when a job asks for input on an issue (`needs_input`), only a trusted person's `/hangar <answer>` reaches the agent. Anyone else's is ignored and audited (`github.refused`), even when they opened the issue.

### Replays and limits

GitHub signs only the body, so hangar treats the body as the delivery: a second copy of an `issues` or `issue_comment` delivery it has already handled (GitHub's *Redeliver*, or a captured request sent again with another `X-GitHub-Delivery`) is answered `200 {"ignored": "duplicate delivery"}` and does nothing. Deliveries are remembered in the store for `HANGAR_WEBHOOK_REPLAY_HOURS` (7 days), across restarts. A delivery that failed or was rate limited is forgotten, so a redelivery runs. A delivery counts as handled only once its handler returns: one that a dying pod (a deploy, the nightly power-down) left half handled runs again when GitHub redelivers it.

GitHub never retries a failed delivery on its own. So at startup hangar lists each repo's webhook deliveries since its last start (`GET /repos/{owner}/{repo}/hooks/{hook_id}/deliveries`, at most the 3 days GitHub keeps) and asks GitHub to redeliver every one with no successful attempt (`POST …/deliveries/{id}/attempts`). That covers everything sent while the cluster was down. The redeliveries come in through the webhook route like any other delivery. The control plane's token already has the Webhooks read and write permission this needs (`hangar setup github`). Turn it off with `HANGAR_GITHUB_REDELIVER_ON_START=false`.

Each GitHub user is a caller for [rate limits and the active-job cap](security/front-doors.md#front-door-limits). Past a rate limit the webhook answers 429; a trusted person gets a comment saying so, someone untrusted gets none (so a flood of `/hangar` comments can't become a flood of hangar comments). The same goes for the active-job cap. Submissions that wait for approval are counted in a source bucket of their own, so outsiders can't use up the one trusted people need. Jobs waiting for approval count toward their requester's cap.

### Untrusted text in the prompt

Text written by someone who isn't trusted is marked in the agent's prompt as untrusted input that can't override the instructions:

- the issue body, when the issue's author isn't trusted (the file is listed as `untrusted`);
- the extra text of an untrusted person's `/hangar` comment (quoted as a request, not as instructions);
- everything that came from an inbound webhook.

The prompt also tells the agent to ignore requests in that text to reveal credentials, contact other hosts or change anything outside the job's repo and branch. This lowers the risk of prompt injection; it doesn't remove it. Approve only what you have read.

## 2. Inbound webhooks

Systems that can only send webhooks (Sentry, PagerDuty, internal services) start jobs at `POST /api/hooks/inbound/<source>`. Jira has its own front door, which also writes back to the ticket and attribute the job to the linked person: see [intake.md](intake.md). Each source is configured in `HANGAR_INBOUND_WEBHOOKS`, a JSON mapping from source name to its settings:

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
- **Jira Cloud** (an admin webhook with a secret): `X-Hub-Signature` with `prefix: "sha256="`, hex HMAC-SHA256 of the body; `X-Atlassian-Webhook-Identifier` as the delivery id (the same across retries). Jira signs no timestamp.
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
| `429` | The source is over its [rate limit](security/front-doors.md#front-door-limits); the sender should retry later. |
| `503` | The source is misconfigured (bad settings, or its secret isn't in the vault). |

Signatures are compared in constant time. Jobs from a webhook have requester and owner `webhook:<source>`, are fenced like a `jobs` service token (only the configured, mapped repo and its environments), and their text is untrusted input to the agent.

**Replays.** A delivery is identified by what its signature covers. The delivery id header isn't signed, and neither is a timestamp header unless `signed_payload` contains `{timestamp}`, so whoever replays a captured request can change both; hangar ignores them for that decision and remembers signed content for `HANGAR_WEBHOOK_REPLAY_HOURS` (7 days, across restarts). `max_skew_seconds` protects only a timestamp that is signed: Sentry's `Sentry-Hook-Timestamp`, for example, isn't part of its signature. Where a provider signs a timestamp, configure it (`signed_payload: "{timestamp}.{body}"`): a replay is then rejected outright once the timestamp is stale.

**Limits.** Each source is one caller for [rate limits and the active-job cap](security/front-doors.md#front-door-limits): an alert storm starts at most 10 jobs at once and then 30 a minute, by default.

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
- **Defaults:** fields the job leaves out come from the repo's [policy](repo-policy.md), then its environment, as for every front door. Inbound webhooks get them the same way.
- The scheduler checks every `HANGAR_SCHEDULE_POLL_SECONDS` (10). A run that can't start (say, the environment was deleted) records why in `last_error`.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_GITHUB_TRUSTED` | `[]` | Trusted logins and `@org/team`s; unset: owners, members and collaborators |
| `HANGAR_GITHUB_TEAM_CACHE_SECONDS` | 600 | |
| `HANGAR_JOB_APPROVAL_TTL_HOURS` | 72 | Unapproved jobs are cancelled after this |
| `HANGAR_SELF_APPROVAL` | `deny` | `allow_solo`: the only person may approve their own jobs while exactly one person exists ([who may approve](#who-may-approve)) |
| `HANGAR_INBOUND_WEBHOOKS` | `{}` | See §2 |
| `HANGAR_INBOUND_MAX_BODY_BYTES` | 1000000 | |
| `HANGAR_SCHEDULE_POLL_SECONDS` | 10 | Scheduler and approval-expiry loop |
| `HANGAR_WEBHOOK_REPLAY_HOURS` | 168 | How long signed GitHub and inbound deliveries are remembered |

Schedules aren't rate limited: each slot runs at most once by design. Rate limits and caps are in [security/front-doors.md](security/front-doors.md#front-door-limits).
