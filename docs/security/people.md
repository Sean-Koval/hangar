# People and OAuth connectors

Admins and members, what a token can do, and the limits on OAuth apps. Part of the [security model](README.md).

## People

One hangar serves a team: run one per team rather than splitting one between teams. Every token belongs to a person, and a person is an **admin** or a **member**.

| | Admin | Member |
| --- | --- | --- |
| Sandboxes, sessions, jobs, snapshots, schedules | everyone's | their own; anyone else's answers "not found", by id or by name |
| Environments | create and change any | create new ones and change their own; `secrets`, `image`, `base`, `runtime_class` and `credential_mode: direct` need an admin |
| Credentials | all; add team credentials (`hangar cred add --team`), assign one to a person (`hangar cred owner`) | the team credentials and their own (`hangar cred add` stores it as theirs) |
| Tokens | mint for anyone, within that person's role | their own: `user` (their login), `mcp` (what `hangar connect` mints), `jobs` |
| REST | everything | `/api/me`, `/api/connect`, their tokens and credentials; jobs through a `jobs` token |

- **A person's own credential** (their API key, or their Claude or Codex login for interactive use) goes only into their sandboxes. A sandbox for someone who has their own credential for its harness uses it in place of the team's; anyone else gets the team's. Naming another person's credential is refused. A repo's policy can require one or the other (`credentials: person | team | person_then_team`).
- **Team credentials** are added by an admin on purpose (`hangar cred add --team`); an admin's credential without `--team` is their own. Jobs no person owns use them: webhooks, schedules without a person, and GitHub and Slack senders linked to nobody. They are API keys, or other credentials the vendor's terms allow for automation: a subscription login (Claude Pro or Max, ChatGPT) is refused as a team credential, since the vendors' terms make it one person's and Claude Code's forbid routing requests through a Pro or Max login on behalf of others ([team.md](../team.md#what-the-vendors-allow)).
- **Ownership is recorded** as `person` on sandboxes, snapshots, sessions, jobs and schedules. A job's session and sandbox, a fork and a best-of-N attempt are their source's person's. Work from a GitHub, Slack or Jira account an admin linked to a person (`hangar person link`, by ids that don't change hands: a GitHub user id, a Slack workspace and user id, a Jira account id) is that person's: their caps, their budget, their lists. A linked account acts as its person, so it is trusted at every front door, and only it continues that person's jobs there ([team.md](../team.md#who-is-trusted)). Work from anyone linked to nobody, and from inbound webhooks, has no person, so only admins see it; the front door's own rule decides whether it waits for approval.
- **Existing installs keep working, and the owner's logins stay the owner's.** Tokens minted before people, the server's admin token and the bootstrap token belong to `owner`, an admin. Records made before have no person, so they are the admins'. Credentials stored before people become the owner's own on the first start, once (`hangar cred migration` shows which), so your Claude or Codex login never reaches a member's sandbox or a job no person owns; control-plane ones stay the control plane's. Add team credentials for everyone else ([team.md](../team.md)).
- **Removing someone** revokes their tokens (OAuth grants they approved stop with them). Their work stays, visible to admins; their own credentials go into nobody's sandbox until an admin reassigns them. Demoting an admin drops the `admin` scope from their tokens at once.
- **Visibility is enforced at the front doors** (MCP tools and resources, REST), by the record each tool call names, so a new MCP tool with a `sandbox`, `session`, `job`, `ref`, `source` or `schedule` argument is covered without a change.
- **Listings filter in the database.** `list_jobs`, `list_sessions`, the recent-work resources and `GET /api/jobs` ask the database for the caller's person's records (a member) or everyone's (an admin), then page through them with a cursor. A cursor is a position (a timestamp and a record id), not a credential: a member who passes a cursor made from someone else's record still gets only their own records after it. Archived jobs and sessions keep their `person` and are listed and found by the same rules.

## OAuth connectors

- **Scope:** OAuth access tokens reach `/mcp` only, never the REST API or the dashboard.
- **Lifetime:** every approval is tied to the hangar token that approved it and stops working when that token is revoked or the app is disconnected. Approvals last at most 90 days.
- **Registration:** limited to allowlisted redirect hosts (`HANGAR_OAUTH_REDIRECT_HOSTS`; an empty list allows none, `"*"` any) and capped in number. Redirect URIs must be `https`, `http` to `localhost`, `127.0.0.1` or `[::1]`, or a native app's own scheme (RFC 8252). A host entry (`claude.ai`) covers `https` and its subdomains; a native app's scheme needs an exact `scheme://host` entry, so listing `claude.ai` doesn't let an app register `evil://claude.ai`. RFC 8252 §7.1's reverse-domain form, which has no host (`com.example.app:/oauth2redirect`), needs an exact `scheme:` entry (`com.example.app:`). The consent page names the scheme of a non-web redirect (`cursor://anysphere.cursor-mcp`). Schemes a browser handles itself (`javascript:`, `data:`, `file:` and the like) are never allowed. A client that asks for several redirect URIs is registered with the allowed ones (RFC 7591 §3.2.1); it is refused only when none is allowed.
- **Cursor:** `cursor://anysphere.cursor-mcp` is in the default list. Any app on the user's machine can claim a custom scheme, just as any app there can listen on the loopback ports native clients use (also allowed by default), so the scheme adds no new exposure: the code goes to an app on the machine of the person who approved, PKCE (`S256`) binds it to the client that started the flow, and approving needs a hangar login token. Cursor also asks for `https://www.cursor.com/agents/mcp/oauth/callback`; that sends the code to Cursor's servers, so it is left out unless you list `www.cursor.com`.
- **Audience:** a client may ask for a token only for hangar's own MCP endpoint (`resource` = `HANGAR_PUBLIC_URL` or `…/mcp`, RFC 8707); any other resource is refused at `/authorize`.
- **PKCE:** `S256` is required, and the code is bound to its redirect URI (the MCP SDK's token endpoint checks both).
- **Consent page:** needs an admin token or a person's login (`user`) token, can't be framed, and isn't cached. The grant acts as that token: a member's connector sees their own work.

