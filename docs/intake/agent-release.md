# Who releases a ticket to agents

Only a person releases a Jira ticket to agents. Client-side guards in the harness hooks and the ai-dlc MCP server stop an agent from setting the release label. An agent with a Jira token can still call Jira directly. So hangar checks again, on its own side, before it starts the job.

## What hangar checks

A ticket carries a release label when it has `exec:agent` or `lane:agent`. Before hangar starts a job for such a ticket, it reads the issue's changelog from Jira. Two changes must be by an account on the allow-list:

- the newest change that added each release label the ticket carries;
- the newest move into the trigger status, such as "Ready for agent".

A label or status set when the issue was created counts as the creator's change. That holds only when hangar read the whole changelog (under 100 changes). With a longer history, hangar can't see the creation, so it refuses.

The check runs on every way a Jira job starts: the signed webhook, the Automation request and a `/hangar` comment. A ticket without a release label goes through [the usual rules](../intake.md) unchanged.

## It fails closed

hangar refuses the job when any of these holds:

- No allow-list is configured.
- A release change was made by an account not on the list, such as an agent, a bot or Automation for Jira.
- The changelog shows no such change, or a change has no author.
- The issue or its changelog can't be read.

A refusal starts no job. hangar comments on the issue to say why, and audits it as `jira.agent_release.refused`. To release the ticket again, a listed person removes the label, adds it back and moves the ticket to the trigger status.

## The allow-list

The list lives in `HANGAR_JIRA`, beside the trigger. hangar ships no default list.

```json
"agent_release": {
  "people": ["5b10ac8d82e05b22cc7d4ef5"],
  "labels": ["exec:agent", "lane:agent"]
}
```

| Field | Meaning |
| --- | --- |
| `people` | The accounts that may release a ticket: Cloud account ids, or Data Center user keys. Required, at least one |
| `labels` | The labels that mark a release. Leave it out for `exec:agent` and `lane:agent` |
| `github_people` | The same people's GitHub logins or user ids. `hangar env local --release-config` refuses an agent token of theirs ([agent identity](../environments/agent-identity.md)) |

List people only. Never list hangar's own Jira account, an agent's account or a bot's service account. An agent
works under accounts of its own ([agent identity](../environments/agent-identity.md)).
