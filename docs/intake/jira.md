# Jira intake

This page is part of [Intake: Jira](../intake.md).

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

The wizard stores the credentials and writes `HANGAR_JIRA` with the defaults: an issue moved to **Ready for agent** starts a job, and hangar moves it to *In Progress*, *Blocked*, *In Review* or *To Do* as the job goes.

```bash
hangar setup jira --url https://acme.atlassian.net --email hangar@acme.com --project ABC=acme/api \
  --token-file jira-token --webhook-secret-file jira-webhook-secret
#   --trigger-status "Agent ready"     another status
#   --trigger-label hangar             a label instead (pass both for either)
#   --status started="Doing"           another status for a stage; --no-statuses leaves the status alone
```

Run again, it keeps what you don't pass: an existing configuration keeps its trigger and statuses. Create the *Ready for agent* status in the project's workflow first (hangar doesn't create statuses or workflows), with transitions into the stage statuses. The same as a setting:

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
  "trigger": {"status": "Ready for agent"},
  "statuses": {"started": "In Progress", "needs_input": "Blocked", "delivered": "In Review", "failed": "To Do"}
}'
```

Alternatively, a label starts jobs: `"trigger": {"label": "hangar"}`, or `{"status": "Ready for agent", "label": "hangar"}` for either. A status is the better door for planned work: it is where the ticket sits on the board, and moving it is a decision someone made.

| Field | Meaning |
| --- | --- |
| `url`, `email`, `token_credential` | The site and the account the API token belongs to (basic auth) |
| `webhook_secret_credential` | The webhook's secret |
| `automation_token_credential` | The token an Automation rule sends instead (below) |
| `projects` | Jira project key to repo (`owner/repo`, or a git URL as in `HANGAR_GITHUB_REPOS`); `components` sends an issue with that component to another repo (first match wins); `environment` picks one of the repo's allowed environments |
| `trigger.status`, `trigger.label` | An issue moved to this status, or given this label (or created with either), starts a job |
| `statuses` | The status to move the issue to at each stage, by name. Leave out any you don't want. The transition into it must exist from where the issue is; if it doesn't, hangar notes that on the job and leaves the status alone |
| `max_age_minutes` | How old a delivery's `timestamp` may be (120) |
| `context_comments`, `context_max_bytes` | What `jira-<key>.md` holds besides the description: the newest comments (30), marked trusted for linked people; the parent or epic, subtasks, links, text attachments (1 MB each) and Confluence links; at most 200,000 bytes, with a note on what was cut |
| `automation_max_transition_age_minutes`, `automation_max_starts_per_issue_per_day` | The [Automation fallback](#fallback-an-automation-rule) only: how recent the issue's trigger transition must be (10), and how many jobs one issue may start a day (3) |

A stage's status can't be the trigger status: hangar would move the issue back into the trigger and start the job again. `hangar doctor` checks the configuration, that the credentials are stored, that the token works (`GET /myself`) and that every mapped repo is allowlisted, and shows the trigger (`jira trigger`).

### Fallback: an Automation rule

**Use the signed webhook above for issue events.** It signs the whole body with its secret, so nothing in a delivery can be forged. An Automation rule's **Send web request** can't sign anything, so it is only a fallback, for a team that needs a rule's conditions (for example, only bugs with a priority) and can't express them in the webhook's JQL. Keep the webhook for **Comment created** either way: answers and approvals come only through it.

1. Store a random token: `hangar cred add jira-automation --harness webhook --kind generic --value "$(openssl rand -base64 32)"`, and set `"automation_token_credential": "jira-automation"` in the configuration.
2. Create a **global** rule (project admins can edit project-scoped rules) with your trigger and conditions, then the action **Send web request**:
   - **URL:** `https://<your hangar>/api/hooks/jira`, method `POST`
   - **Headers:** `X-Hangar-Token` with the token, marked **Hidden**; `Content-Type: application/json`
   - **Web request body:** Custom data:
     ```json
     {"webhookEvent": "hangar:automation", "timestamp": "{{now}}",
      "issue": {"key": "{{issue.key}}"}}
     ```

The hidden token stands in for a signature, but it only proves the request came from someone who holds it. It isn't tied to the body, the body's `{{now}}` is whatever the sender writes, and anyone who can edit the rule can change its body, or its URL and with it where the header goes. So hangar checks the request against Jira itself:

- **Only the issue key and timestamp are read.** hangar reads the issue through the API. The job belongs to whoever the issue's history says moved it into the trigger status or gave it the trigger label (or its creator, when it was created that way); a `user` in the body is ignored.
- **The issue must have just been moved.** That change must be at most `automation_max_transition_age_minutes` (10) old when the request arrives, allowing `max_age_minutes` of clock skew the other way. A rule fires right after the transition, so an older one means someone is replaying the token for an issue a teammate moved long ago. hangar answers `200 {"ignored": "no recent transition"}`, audits `hook.automation.stale_transition` (the issue, who moved it and how long ago) and alerts the admins.
- **At most `automation_max_starts_per_issue_per_day` (3) jobs per issue a day** start this way. Each issue has its own count, so moving a whole sprint at once isn't held up. Refusals are audited as `hook.automation.capped`.
- **The token only sends `hangar:automation` requests.** A token-authenticated body posing as a webhook event (`jira:issue_updated`, `comment_created`) is ignored, audited (`hook.automation.wrong_event`) and alerted.
- **Alerts.** A stale transition, a wrong event or a wrong token raises an `alert` in the admins' [inbox](../landing/review.md#7-the-inbox-what-needs-you) and, with the [Slack app](../slack.md) configured, a DM to each admin with a linked Slack account. There is at most one alert an hour per issue (and one an hour for wrong tokens); every refusal is still audited.

**Rotate the token** every 90 days, and whenever someone who could edit the rule leaves:

```bash
hangar cred add jira-automation --rotate    # a new random token, shown once, and the rule's steps
```

The old token stops working at once. In the rule's **Send web request** action, set the `X-Hangar-Token` header to the new value (still Hidden), publish, and move a test issue. `hangar doctor` warns when the token is more than 90 days old, and while `automation_token_credential` is set but issue events don't arrive through the signed webhook (no `webhook_secret_credential`, or no signed issue event in 30 days). `automation_max_transition_age_minutes` and `automation_max_starts_per_issue_per_day` go in `HANGAR_JIRA` beside `max_age_minutes`.

### Talking to the job from the issue

- **Answer a question:** any comment from the job's person while it waits on one (for a team job, from any linked teammate).
- **Approve or reject:** `/hangar approve`, `/hangar reject`.
- **Continue after it's done:** `/hangar <what to do next>`, from the job's person. It resumes the same session and branch.
- **Say what a follow-up was** ([steering.md](../steering.md#classifying-steers)): start it with `fix:` (a correction), `redirect:` (the task changed) or `info:` (something it didn't know), as in `/hangar fix: use the v2 client`; or afterwards comment `/hangar class correction`, `/hangar class redirect` or `/hangar class info` to classify your last unclassified one. hangar's "Continuing…" comment lists these. An answer to the agent's question counts as new information unless you say otherwise.
- **Start one by hand:** `/hangar <instructions>` on an issue with no job.

hangar's own comments start with `[hangar]` and are never read as replies.

### Branch and PR names carry the key

A job for a Jira issue is named with its key, so the [GitHub for Jira](https://github.com/marketplace/jira-software-github) app links the branch and PR to it. The branch is `hangar/<key>-<job>` (`hangar/KAN-35-job-1a2b3c4d`). The PR title starts with `<key>: ` and the PR body has a `Jira: <key>` line.

hangar takes the key from the Jira door's issue, a job's `jira:` [`ticket`](../changesets.md) or a `jira` label (`"labels": {"jira": "KAN-35"}`). An MCP `submit_job` with no `ticket` takes the one key its title and instructions name, if its project is in `HANGAR_JIRA`'s `projects`. Two or more give none; the job's events say why. A job given its own `branch` keeps it, and a repo policy's [`branch_template`](../repo-policy.md) names the branch instead.

## Jira Data Center

The same, with three differences:

- `"deployment": "server"` and no `email`. `token_credential` holds a [personal access token](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html), sent as `Authorization: Bearer`.
- hangar calls REST v2 and writes comments as wiki markup.
- Use a version whose webhook form has a **Secret** field: it sends the same `X-Hub-Signature: sha256=…`. Accounts are linked by user key, not accountId (`hangar person link dana jira JIRAUSER10100`); hangar reads who moved an issue from `?expand=changelog`.
