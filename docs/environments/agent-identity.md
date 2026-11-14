# Giving agents their own identity

How to create the Atlassian and GitHub accounts an agent works under, so hangar can tell a person from an agent.
Read it before you set the agent environment's credentials.

## Why an agent needs its own accounts

Only a person on `agent_release.people` releases a ticket to agents ([who releases a ticket](../intake/agent-release.md)).
hangar checks that by the account in the issue's changelog. An agent holding a person's Atlassian token writes the
changelog as that person. Its label change then looks exactly like the person's release, and the check proves
nothing.

So an agent never holds a person's token. It gets an Atlassian account and a GitHub identity of its own.

## The Atlassian account

Pick one of two kinds. An organization admin creates either.

| Kind | Header value | How |
| --- | --- | --- |
| A bot user | `Basic <base64 of email:api-token>` | Invite a user on a shared mailbox, such as `agents@` your domain. Sign in as it and create an API token with the scopes the agent needs |
| A service account | `Bearer <api-key>` | In Atlassian Administration, create a service account and an API key for it, scoped to the sites and products the agent uses |

Then:

1. Give the account access to the Jira projects and Confluence spaces the agent works in, and nothing else.
2. Never add its account id to `agent_release.people`.
3. Store the header value as hangar's `atlassian-mcp` credential, or export it as `ATLASSIAN_MCP_AUTHORIZATION` for a
   local run.

The Atlassian MCP server accepts either value once an admin turns on API token authentication for the organization.

## The GitHub identity

The agent clones, pushes and opens pull requests with `GH_TOKEN`. Use one of these:

- a GitHub App installation token, from the App hangar opens pull requests as (`HANGAR_GITHUB_APP_CREDENTIAL`);
- a fine-grained token of a separate bot account, limited to the repositories the agent works in.

Never use a person's own token. A pull request it opens is that person's, and they can't review it as code owner.

## What hangar checks

`hangar env local up` and `check` take `--release-config`, a file holding `HANGAR_JIRA`. With it, hangar asks Jira
who `ATLASSIAN_MCP_AUTHORIZATION` belongs to, with the read-only `myself` call. It asks GitHub who `GH_TOKEN` belongs
to, with the read-only `GET /user`. It refuses to start when:

- the Atlassian account is on `agent_release.people`;
- the GitHub login or user id is on `agent_release.github_people`, the releasers' GitHub accounts;
- a variable is not set, or its call fails;
- the config lacks either list.

An App installation token has no user, so it passes.

`hangar config list --json > file` writes a file it reads. The refusal names the account and the problem.

## A local run you start yourself

hangar's own release check runs only where a Jira job starts: the webhook, an Automation request or a `/hangar`
comment. `hangar env local` has no Jira door. So a local run that you start yourself counts as your release: you
chose the ticket and started the agent. The identity check above still applies, so the agent can't act as you.
