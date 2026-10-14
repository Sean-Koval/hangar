# Intake: Jira and GitHub Projects

Work reaches hangar where the team already tracks it. Moving a Jira issue to a status (or giving it a label), or moving an issue to a column on a GitHub Projects board, starts a job on the mapped repo. Hangar keeps the ticket in step as the job runs:

| Stage | Jira | GitHub Projects |
| --- | --- | --- |
| Queued | a comment saying which job and environment | a comment on the issue |
| Agent started | a comment with a link to the job in hangar; the `started` transition | the `started` column |
| The agent asks a question | the question as a comment; the `needs_input` transition. A linked teammate's next comment answers it, and the agent picks up where it left off | the question on the issue; the `needs_input` column. `/hangar <answer>` on the issue answers it |
| Delivered | the outcome with the PR, which is also added as a remote link; the `delivered` transition | the outcome on the issue (the PR says `Closes #N`); the `delivered` column |
| Failed or cancelled | what went wrong; the `failed` transition | the `failed` column |

GitHub issues ([jobs.md §4](jobs.md#4-trigger-from-github)) and Slack ([slack.md](slack.md)) work as before: their jobs go to the linked person too, and they keep their own trust rules.

## Who a job belongs to

An account in Jira or GitHub counts as a hangar person only when an admin has linked it to one (`hangar person link dana jira <account id>`, `hangar person link dana github <login>`). Hangar asks `people.resolve_external` for every sender.

- **A linked sender is trusted.** The job is theirs: it counts against their caps and budget, runs with their own credentials where they have them, and they see it in their own job list.
- **Anyone else is untrusted.** Their job waits in `awaiting_approval` until a trusted teammate approves it: `/hangar approve` (or `/hangar reject`) in a comment on the issue, or `hangar job approve`. Its text reaches the agent fenced as untrusted, and it runs on the team's shared credentials, never on someone's own login.
- In Jira, only a linked teammate's comment answers a question, approves a job or continues one. Other people's comments are ignored.
- On a GitHub Projects board, someone who isn't linked is trusted only if `HANGAR_GITHUB_TRUSTED` lists them. Project item payloads carry no repository association, so the owner, member and collaborator rule for issues can't apply.
- Issue text written by someone who isn't linked (the Jira reporter, the GitHub issue's author) reaches the agent fenced as untrusted even when a trusted person started the job. So do an issue's earlier GitHub comments.

The repo must be in `HANGAR_GITHUB_REPOS`, as for every front door, and the repo's [policy](repo-policy.md) sets the job's verify, environment, harness and model.

## Jira Cloud

### 1. An account and a token for hangar

Create an Atlassian account for hangar (a service user is clearer on the issue than a person's name) with permission to browse, comment on and transition issues in the mapped projects. Then [create an API token](https://id.atlassian.com/manage-profile/security/api-tokens) for it and store it:

```bash
hangar cred add jira-api --harness webhook --kind generic       # paste the API token, then Ctrl-D
```

### 2. The webhook

In Jira, go to **Settings → System → WebHooks → Create a WebHook**:
- **URL:** `https://<your hangar>/api/hooks/jira`
- **Secret:** generate a long random value, and store the same value in hangar: `hangar cred add jira-webhook --harness webhook --kind generic`
- **Events:** Issue **created** and **updated**, and Comment **created**, each with the JQL `project in (ABC, DEF)` for your mapped projects.

Jira signs each delivery with the secret (`X-Hub-Signature: sha256=<hex HMAC-SHA256 of the body>`) and retries a failed one up to five times over about an hour, with the same `X-Atlassian-Webhook-Identifier`.

### 3. The configuration

```bash
hangar config set jira '{
  "url": "https://acme.atlassian.net",
  "email": "hangar@acme.com",
  "token_credential": "jira-api",
  "webhook_secret_credential": "jira-webhook",
  "projects": {
    "ABC": {"repo": "acme/api"},
    "WEB": {"repo": "acme/web", "components": {"Docs": "acme/docs"}}
  },
  "trigger": {"status": "Ready for agent", "label": "hangar"},
  "statuses": {"started": "In Progress", "needs_input": "Blocked", "delivered": "In Review", "failed": "To Do"}
}'
```

| Field | Meaning |
| --- | --- |
| `url`, `email`, `token_credential` | The site and the account the API token belongs to (basic auth) |
| `webhook_secret_credential` | The webhook's secret |
| `automation_token_credential` | The token an Automation rule sends instead (below) |
| `projects` | Jira project key to repo (`owner/repo`, or a git URL as in `HANGAR_GITHUB_REPOS`); `components` sends an issue with that component to another repo (first match wins); `environment` picks one of the repo's allowed environments |
| `trigger.status`, `trigger.label` | An issue moved to this status, or given this label (or created with either), starts a job |
| `statuses` | The status to move the issue to at each stage, by name. Leave out any you don't want. The transition into it must exist from where the issue is; if it doesn't, hangar notes that on the job and leaves the status alone |
| `max_age_minutes` | How old a delivery's `timestamp` may be (120) |

A stage's status can't be the trigger status: hangar would move the issue back into the trigger and start the job again. `hangar doctor` checks the configuration, that the credentials are stored, that the token works (`GET /myself`) and that every mapped repo is allowlisted.

### Or: an Automation rule

If you'd rather decide in a rule (for example, only for bugs with a priority), use Automation instead of the webhook's issue events. Keep the webhook for **Comment created** so answers and approvals still reach hangar.

1. Store a random token: `hangar cred add jira-automation --harness webhook --kind generic`, and set `"automation_token_credential": "jira-automation"` in the configuration.
2. Create a rule with your trigger and conditions, then the action **Send web request**:
   - **URL:** `https://<your hangar>/api/hooks/jira`, method `POST`
   - **Headers:** `X-Hangar-Token` with the token, marked **Hidden**; `Content-Type: application/json`
   - **Web request body:** Custom data:
     ```json
     {"webhookEvent": "hangar:automation", "timestamp": "{{now}}",
      "issue": {"key": "{{issue.key}}"}, "user": {"accountId": "{{initiator.accountId}}"}}
     ```

Automation doesn't sign requests, so the hidden token stands in for the signature. Hangar reads the issue through the API, and the job belongs to whoever triggered the rule (`initiator`).

### Talking to the job from the issue

- **Answer a question:** any comment from a linked teammate while the job waits on one.
- **Approve or reject:** `/hangar approve`, `/hangar reject`.
- **Continue after it's done:** `/hangar <what to do next>`. It resumes the same session and branch.
- **Start one by hand:** `/hangar <instructions>` on an issue with no job.

Hangar's own comments start with `[hangar]` and are never read as replies.

## Jira Data Center

The same, with three differences:
- `"deployment": "server"` and no `email`. `token_credential` holds a [personal access token](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html), sent as `Authorization: Bearer`.
- Hangar calls REST v2 and writes comments as wiki markup.
- Use a version whose webhook form has a **Secret** field: it sends the same `X-Hub-Signature: sha256=…`. Accounts are linked by user key, not accountId.

## GitHub Projects

GitHub sends project item events (`projects_v2_item`) only for projects that belong to an organization, and only to an organization webhook or a GitHub App. A repository webhook never gets them. GitHub still marks these events as public preview.

### 1. The webhook

In the organization's **Settings → Webhooks → Add webhook**:
- **Payload URL:** `https://<your hangar>/api/hooks/github`, content type `application/json`
- **Secret:** the same value as the repository webhooks (`HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL`)
- **Events:** only **Projects v2 items**

If hangar runs as a GitHub App, give the App the organization's **Projects: Read and write** permission and subscribe it to **Projects v2 item** instead.

### 2. The token

Hangar reads the item and sets its status column with GitHub's GraphQL API, using the control plane's own token (`HANGAR_GITHUB_TOKEN_CREDENTIAL`). Give it access to the organization's projects: the `project` scope for a classic token, or **Projects: Read and write** on the organization for a fine-grained token or an App.

### 3. The boards

```bash
hangar config set github_projects '{
  "acme/7": {
    "trigger": "Ready for agent",
    "repos": ["acme/api", "acme/web"],
    "statuses": {"started": "In progress", "needs_input": "Blocked", "delivered": "In review", "failed": "Todo"}
  }
}'
```

The key is `org/project-number` (from the board's URL, `github.com/orgs/acme/projects/7`). `trigger` is the column that starts a job. `status_field` names the single-select field the columns are, if it isn't `Status`. `repos` limits which repos' issues count; empty means any repo in `HANGAR_GITHUB_REPOS`. `statuses` names the column for each stage. Draft items and pull requests on the board are ignored.

When an item moves to the trigger column, hangar reads the item and its issue in one query. It starts the job only if the item is still in that column, so a delivery that arrives late for an item that has moved on does nothing. From then on the job is the issue's job: the comments, `/hangar approve`, `/hangar <answer>` and `Closes #N` work exactly as for a [GitHub issue](jobs.md#4-trigger-from-github).

## How deliveries are handled

- **Authentic.** Jira's signature (or the Automation token) and GitHub's signature are checked in constant time before anything is parsed. Jira's payload `timestamp` sits inside the signed body and must be recent (`max_age_minutes`).
- **Once.** Each delivery is claimed (by the digest of its signed body) before it's handled and marked done after. A retry of one that was handled is acknowledged and dropped, and a replay can't pass as new by changing an unsigned header. One that failed half way, or hit a rate limit, runs again on the sender's retry. Jira treats only `200` as delivered, so hangar answers `200` for everything handled or ignored, `429` when rate limited and `503` when misconfigured, which Jira retries.
- **One job per ticket.** A ticket with an unfinished job doesn't start a second.
- **Credentials stay in the control plane.** The Jira token, webhook secret and Automation token are control-plane credentials: they can't be attached to a sandbox or mapped into one with `secrets:`.
- **Write-back never blocks the job.** If Jira or GitHub can't be reached, or the workflow has no transition to a status, that goes on the job's timeline and the job carries on.

## Adding a tracker

`src/hangar/intake.py` holds what every front door shares: `person_for` (who the sender is), `once` (commit after handle), `open_job` (one job per ticket), `submit` (approval for untrusted senders and refusals as answers) and `TRACKERS`, the write-back registry. A tracker sets the job label `intake.tracker` to its ticket reference and registers `TRACKERS[<scheme>] = update(svc, job, ticket, stage)`. Hangar calls it once per stage (`started`, `needs_input`, `delivered`, `failed`) whenever the job reports, whichever thread the conversation is in. `jira.py` and `ghprojects.py` are the two examples.
