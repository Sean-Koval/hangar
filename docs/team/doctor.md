# Check it: `hangar doctor`

What `hangar doctor` checks about people, credentials and inbox delivery on a team hangar, and what happens to a turn
that would start with no login. The setup it checks is on [team setup](../team.md).

## The checks

- **personal credentials shared**: fails for any subscription login stored as a team credential, and says what the
  migration did.
- **team credentials for jobs**: lists what would run with no login. That is mapped repos (GitHub and Slack), inbound
  webhooks and schedules without a person whose environment's harness would get no login. It also lists repos whose
  `credentials: person` policy makes unlinked senders' jobs fail.
- **subscription logins**: warns for mapped repos, webhooks and schedules whose harness would find only subscription
  logins. It also warns for people whose only logins for a harness are subscriptions: their jobs fail while their
  sessions work. Its fix is the `hangar cred add` command for each harness's API key. It fails while
  `HANGAR_SUBSCRIPTION_LOGINS=any` is still stored ([removed switches](../upgrading.md#removed-switches)).
- **harness repo**: when `HANGAR_HARNESS_REPO` is set, whether it is reachable, when it was last refreshed, and
  whether every channel resolves.
- **inbox delivery**: with the Slack app configured, warns for people who get DMs (`--dm` isn't `off`) but have no
  linked Slack account: "inbox not delivered".
- **unlinked senders**: GitHub, Slack and Jira accounts linked to nobody that started 3 or more jobs
  in the last 14 days. Each comes with its `hangar person link` command.

## A turn with no login

A turn whose harness would start with no login isn't started. Such a turn has no credential for its harness among the
sandbox's and none of its login variables in the turn's environment. It has no login file either.

The turn ends at once with
`no login for <harness> reaches …` and the command to add a team key. It is recorded as `infra_error` / `no_login`,
not as the agent's failure.

`GET /api/credentials/login?environment=E[&person=P][&repo=R][&purpose=automated]` (admins) answers the same question
before anything starts. It gives the credentials a session (or a job) would get. It gives `login` true, false, or null
when the environment sets a login variable itself.
