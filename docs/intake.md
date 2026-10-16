# Intake: Jira and GitHub Projects

Work reaches hangar where the team already tracks it. Moving a Jira issue to a status (or giving it a label), or moving an issue to a column on a GitHub Projects board, starts a job on the mapped repo. Hangar keeps the ticket in step as the job runs:

| Stage | Jira | GitHub Projects |
| --- | --- | --- |
| Queued | a comment saying which job and environment | a comment on the issue |
| Agent started | a comment with a link to the job in hangar; the `started` transition | the `started` column |
| The agent asks a question | the question as a comment; the `needs_input` transition. A linked teammate's next comment answers it, and the agent picks up where it left off | the question on the issue; the `needs_input` column. `/hangar <answer>` on the issue answers it |
| Delivered | the outcome with the PR, which is also added as a remote link; the `delivered` transition | the outcome on the issue (the PR says `Closes #N`); the `delivered` column |
| Failed or cancelled | what went wrong; the `failed` transition | the `failed` column |

GitHub issues ([jobs.md §4](jobs.md#4-trigger-from-github)) and Slack ([slack.md](slack.md)) follow the same rule: their jobs go to the linked person too.

## Who a job belongs to, and who is trusted

Every front door uses one rule ([team.md](team.md#who-is-trusted) has it in full). An account in Jira or GitHub is a hangar person only when an admin has linked it to one (`hangar person link dana jira <account id>`, `hangar person link dana github <login>`, which is stored as the account's numeric GitHub id). Hangar asks `people.resolve_external` for every sender, by an id that doesn't change hands: the Atlassian account id (Data Center: the user key) and the GitHub user id (`sender.id`), never a GitHub login.

- **A linked sender acts as their person, so they are trusted.** The job is theirs: it counts against their caps and budget, runs with their own credentials where they have them, and they see it in their own job list.
- **Only the job's person continues it.** A person's job runs with their own login, so in Jira only that person's comment answers its question or continues it with `/hangar <message>`. Anyone else's, a linked teammate's included, is refused with a comment saying whose job it is (an admin uses `hangar job follow-up`). The same holds on GitHub issues, PRs and Slack threads.
- **Anyone else is untrusted.** Their job waits in `awaiting_approval` until a trusted teammate approves it: `/hangar approve` (or `/hangar reject`) in a comment on the issue, or `hangar job approve`. Its text reaches the agent fenced as untrusted, and it runs on the team's credentials, never on someone's own login. In Jira, a sender who isn't linked is never trusted (Jira payloads carry no equivalent of GitHub's collaborator association, and anyone who can move an issue would otherwise start work); only a linked teammate's comment approves a job, answers a team job's question or continues one. Other people's comments are ignored.
- On a GitHub Projects board, someone who isn't linked is trusted only if `HANGAR_GITHUB_TRUSTED` lists them. Project item payloads carry no repository association, so the owner, member and collaborator rule for issues can't apply.
- **Ticket text is data.** A Jira issue's summary and description always reach the agent fenced as untrusted, whoever reported it, since anyone with permission to edit the issue can rewrite them; the job's instructions only point at that file. A GitHub issue's body is fenced unless a trusted account wrote it, and its earlier comments always are. Only a trusted sender's own `/hangar <instructions>` are instructions. The issue's title is still the job's title, which the prompt shows.
- **Who acted is what Jira or GitHub says.** A signed webhook's `user` (Jira) or `sender` (GitHub) is used as sent. An Automation request isn't signed, so its body's `user` is ignored: hangar reads who moved the issue into the trigger status (or gave it the trigger label) from the issue's own history, or its creator when it was created that way.

The repo must be in `HANGAR_GITHUB_REPOS`, as for every front door, and the repo's [policy](repo-policy.md) sets the job's verify, environment, harness and model.

### Safety

The fence around ticket text is in the prompt; the boundaries are outside it. Under the default `HANGAR_INTAKE_POSTURE=enforce` ([security.md](security.md#intake-posture)) every Jira and GitHub Projects job needs the [git gateway](security.md#git-credential-gateway): without `HANGAR_GATEWAY_URL` (or for a repo whose host the gateway doesn't serve) the job is refused and the reason is posted on the issue. Its sandboxes hold only a fetch-only grant (hangar pushes), and they get at least `allowlist` egress, even in an `open` environment: the environment's presets and hosts, the repo policy's, and the harness's API. Anything else is an egress request a person answers. `warn` runs such jobs as before for one release, with a `posture.warn` event and a line in the review packet.

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
| `automation_max_transition_age_minutes`, `automation_max_starts_per_issue_per_day` | The [Automation fallback](#fallback-an-automation-rule) only: how recent the issue's trigger transition must be (10), and how many jobs one issue may start a day (3) |

A stage's status can't be the trigger status: hangar would move the issue back into the trigger and start the job again. `hangar doctor` checks the configuration, that the credentials are stored, that the token works (`GET /myself`) and that every mapped repo is allowlisted.

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

- **Only the issue key and timestamp are read.** Hangar reads the issue through the API. The job belongs to whoever the issue's history says moved it into the trigger status or gave it the trigger label (or its creator, when it was created that way); a `user` in the body is ignored.
- **The issue must have just been moved.** That change must be at most `automation_max_transition_age_minutes` (10) old when the request arrives, allowing `max_age_minutes` of clock skew the other way. A rule fires right after the transition, so an older one means someone is replaying the token for an issue a teammate moved long ago. Hangar answers `200 {"ignored": "no recent transition"}`, audits `hook.automation.stale_transition` (the issue, who moved it and how long ago) and alerts the admins.
- **At most `automation_max_starts_per_issue_per_day` (3) jobs per issue a day** start this way. Each issue has its own count, so moving a whole sprint at once isn't held up. Refusals are audited as `hook.automation.capped`.
- **The token only sends `hangar:automation` requests.** A token-authenticated body posing as a webhook event (`jira:issue_updated`, `comment_created`) is ignored, audited (`hook.automation.wrong_event`) and alerted.
- **Alerts.** A stale transition, a wrong event or a wrong token raises an `alert` in the admins' [inbox](landing.md#7-the-inbox-what-needs-you) and, with the [Slack app](slack.md) configured, a DM to each admin with a linked Slack account. There is at most one alert an hour per issue (and one an hour for wrong tokens); every refusal is still audited.

**Rotate the token** every 90 days, and whenever someone who could edit the rule leaves:

```bash
hangar cred add jira-automation --rotate    # a new random token, shown once, and the rule's steps
```

The old token stops working at once. In the rule's **Send web request** action, set the `X-Hangar-Token` header to the new value (still Hidden), publish, and move a test issue. `hangar doctor` warns when the token is more than 90 days old, and while `automation_token_credential` is set but issue events don't arrive through the signed webhook (no `webhook_secret_credential`, or no signed issue event in 30 days). `automation_max_transition_age_minutes` and `automation_max_starts_per_issue_per_day` go in `HANGAR_JIRA` beside `max_age_minutes`.

### Talking to the job from the issue

- **Answer a question:** any comment from the job's person while it waits on one (for a team job, from any linked teammate).
- **Approve or reject:** `/hangar approve`, `/hangar reject`.
- **Continue after it's done:** `/hangar <what to do next>`, from the job's person. It resumes the same session and branch.
- **Start one by hand:** `/hangar <instructions>` on an issue with no job.

Hangar's own comments start with `[hangar]` and are never read as replies.

## Jira Data Center

The same, with three differences:
- `"deployment": "server"` and no `email`. `token_credential` holds a [personal access token](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html), sent as `Authorization: Bearer`.
- Hangar calls REST v2 and writes comments as wiki markup.
- Use a version whose webhook form has a **Secret** field: it sends the same `X-Hub-Signature: sha256=…`. Accounts are linked by user key, not accountId (`hangar person link dana jira JIRAUSER10100`); hangar reads who moved an issue from `?expand=changelog`.

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

GitHub doesn't retry a failed delivery, so at each start hangar asks it to redeliver the `projects_v2_item` deliveries the organization webhook failed to deliver since the last start (up to the 3 days GitHub keeps them), as it does for the repository webhooks ([triggers.md](triggers.md#replays-and-limits)). That reads `GET /orgs/{org}/hooks` and the hook's deliveries and needs the organization's **Webhooks: Read and write** permission (classic tokens: `admin:org_hook`; listing an organization's webhooks also needs an organization owner's token). Without it hangar logs a warning and moves on; boards still work.

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

- **Authentic.** Jira's signature (or the Automation token) and GitHub's signature are checked in constant time before anything is parsed. Jira's payload `timestamp` sits inside the signed body and must be recent (`max_age_minutes`). The Automation token signs nothing, so an Automation request also needs a recent trigger transition in the issue's own history, and is capped per issue ([above](#fallback-an-automation-rule)).
- **Within 1 MB.** A Jira delivery larger than 1 MB (in practice, a very long description) isn't taken: hangar answers `413`. One that is authentic and fresh is audited (`jira.too_large`, with its size and delivery id), and if its issue is in the trigger status or has the trigger label, hangar comments on the issue once a day that the description is too large to start a job from. Jira doesn't retry a `413`, so shorten the description (attach long logs as files) and move the issue again.
- **Once.** Each delivery is claimed (by the digest of its signed body) before it's handled and marked done after. A retry of one that was handled is acknowledged and dropped, and a replay can't pass as new by changing an unsigned header. One that failed half way, or hit a rate limit, runs again on the sender's retry. Jira treats only `200` as delivered, so hangar answers `200` for everything handled or ignored, `429` when rate limited and `503` when misconfigured, which Jira retries.
- **One job per ticket.** A ticket with an unfinished job doesn't start a second.
- **Tainted from turn 1.** Ticket text is untrusted, so every intake job is [tainted](security.md#taint-gates): its PR opens as a draft labelled `hangar:tainted` and needs a reviewer other than the job's own person to land. Give intake repos `approvals: deny-risky`, so pushes and fetches inside the turn are refused too.
- **Credentials stay in the control plane.** The Jira token, webhook secret and Automation token are control-plane credentials: they can't be attached to a sandbox or mapped into one with `secrets:`.
- **Write-back never blocks the job.** If Jira or GitHub can't be reached, or the workflow has no transition to a status, that goes on the job's timeline and the job carries on.

## Adding a tracker

`src/hangar/intake.py` holds what every front door shares: `person_for` (who the sender is), `once` (commit after handle), `open_job` (one job per ticket), `submit` (approval for untrusted senders and refusals as answers) and `TRACKERS`, the write-back registry. A tracker sets the job label `intake.tracker` to its ticket reference and registers `TRACKERS[<scheme>] = update(svc, job, ticket, stage)`. Hangar calls it once per stage (`started`, `needs_input`, `delivered`, `failed`) whenever the job reports, whichever thread the conversation is in. `jira.py` and `ghprojects.py` are the two examples.
