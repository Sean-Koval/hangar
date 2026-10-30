# Team setup: people and credentials

One hangar serves a team. This guide sets it up: add the people and link their GitHub, Slack and Jira accounts. Then
add the team's credentials, and let each person add their own logins.

[People and OAuth connectors](security/people.md#people) has the access model behind it.
[The team release, step 7](releases/team-release-procedure.md#7-people-your-logins-stay-yours-add-team-credentials-then-the-team)
covers an existing install.

```bash
hangar person add alice --name "Alice"                    # 1. people (prints her login token once)
hangar person link alice github alice-gh                  # 2. her accounts elsewhere
hangar person link alice slack T0123ABCD/U012AB3CD
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt   # 3. team credentials
hangar cred add codex-team --team --harness codex --kind api_key --file openai-key.txt
#   alice, on her machine: hangar cred add claude-alice --harness claude-code --kind api_key --file key.txt
hangar doctor                                             # 5. check
```

| Question | Page |
| --- | --- |
| How do I link someone's GitHub, Slack and Jira accounts, and where does their inbox reach them? | [Linked accounts and inbox delivery](team/linked-accounts.md) |
| Whose work starts without approval, and who may continue it? | [Who is trusted](team/who-is-trusted.md) |
| Why can't jobs use a Claude Pro or ChatGPT login, and how are sign-in files shared? | [Subscription logins](team/subscription-logins.md) |
| What do the vendors' terms allow? | [What the vendors allow](team/vendor-terms.md) |
| Is it set up right? | [Check it: hangar doctor](team/doctor.md) |

## 1. Add people

```bash
hangar person add alice --name "Alice"            # a member: sees and acts on her own work
hangar person add bob --role admin                # an admin: everyone's work, people and the vault
hangar person ls
```

`person add` prints the person's login token once, and lists the team credentials their sandboxes will get. They run
`hangar login <url>` with it, then `hangar connect` for their MCP clients. `hangar person token alice` mints another one
(a new laptop); `hangar person rm alice` revokes them all.

## 2. Link their accounts elsewhere

Each sender on GitHub, Slack or Jira is an account there, not a hangar person, until an admin links
them. How, and what a linked account changes, is on [linked accounts](team/linked-accounts.md).

## 3. Team credentials

A **team credential** is one an admin adds for everyone. Jobs no person owns use it, and so does anyone without a
login of their own:

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt
hangar cred add codex-team  --team --harness codex --kind api_key --file openai-key.txt
hangar cred add gh-jobs     --team --harness git --kind git_token --file token.txt   # direct-mode git
hangar cred owner gh-jobs team                     # or make an existing API key or token the team's
hangar cred ls                                     # owner=team | owner=<person> | owner=control-plane
```

Without `--team`, a credential an admin adds is **their own**, so an admin's personal login never becomes the team's
by accident. Control-plane credentials belong to neither: they never reach a sandbox. Those are webhook signing keys,
bot tokens, GitHub App keys and hangar's own GitHub token. Members can't add team credentials.

**A team credential is an API key, or another credential the vendor's terms allow for automation. A subscription
login never is.** A Claude Pro or Max login (`--kind oauth_token`), a ChatGPT `auth.json`, a Google sign-in and the
other sign-in files are refused as team credentials.

They are refused whether added with `--team`, stored over with `team: true`, or moved with
`hangar cred owner NAME team` ([what the vendors allow](team/vendor-terms.md)). The refusal names the
`hangar cred add … --kind api_key` command to use instead. API keys also have no lease, so any number of sandboxes use
one at once.

Environments may list team credentials by name as before. A sandbox that runs for no one, or for someone without a
login of their own, gets the ones its environment lists for its harness.

When the environment lists no team one, it
gets every team credential for the harness. A git token is swapped the same way when the environment lists one.

## 4. Per-person logins

Each person adds their own, an API key first: it serves their jobs, schedules and sessions alike.

```bash
hangar cred add claude-alice --harness claude-code --kind api_key --file anthropic-key.txt
hangar cred add codex-alice --harness codex --kind api_key --file openai-key.txt
# optional, for her own interactive sessions only (see below):
claude setup-token | hangar cred add claude-alice-max --harness claude-code --kind oauth_token
```

A member's credential is always their own. It goes only into their sandboxes, replacing the team's for that harness,
and naming someone else's is refused. An admin stores one for someone with `--person alice`, or moves one with
`hangar cred owner NAME alice`.

## Whose login a job uses

| Work | Runs for | Login, by default |
| --- | --- | --- |
| MCP, REST, CLI jobs (`submit_job`) | the caller's person | their API key, else the team's |
| A schedule | its person (none for schedules from before people) | their API key, else the team's |
| GitHub issue or comment, Slack, Jira | the linked person, else the team | their API key, else the team's |
| Inbound webhooks | the team | the team's |
| A person's own `new_session`, `create_sandbox`, `run_agent` | the caller's person | theirs (a subscription login too, with a warning), else the team's |

Every job's sandbox is automated work, whatever its front door. That covers best-of-N attempts, follow-ups, autofix
rounds, environment-setup jobs, Cloud Doctor jobs, experiment trials and graders. It also covers replay and fork
sandboxes of any of them. None gets a [subscription login](team/subscription-logins.md).

A repo's policy can say otherwise:

```bash
hangar repo set acme/app --credentials person            # only the person's own; a job for no one fails, saying why
hangar repo set acme/app --credentials team              # always the team's, even for a person's job
hangar repo set acme/app --credentials person_then_team  # the default
```

A job on a `team` repo still runs for its person: their caps, budget and lists. Only the login changes.

## Credentials stored before people

Before people, every credential belonged to no one and went into every sandbox. On its first start this release makes
each of them **the owner's own**, once, and records it; control-plane credentials stay the control plane's:

```bash
hangar cred migration
# done 2026-10-14 10:02: made owner's own: claude-sean, codex-sean, gh-pat
# left to the control plane: gh-hook, gh-token
```

Your logins then never reach a member's sandbox, nor a job no person owns. Make the ones the team may share the
team's (`hangar cred owner gh-pat team`, for an API key or token), and add team API keys. Link your own GitHub login
(`hangar person link owner github <login>`) so your issues keep running with your login.
`GET /api/credentials/migration` returns the same record.

## A shared harness

The team's harness (its instructions, skills and Claude Code plugins) lives in a harness repo of its own, frozen from a
run that went well. hangar runs from it by name: `harness_lock: team-default@stable` on a job or session. Intake jobs
use it by default with `HANGAR_DEFAULT_HARNESS=team-default@stable`. Set `HANGAR_HARNESS_REPO` to the repo's URL; see
[the team harness repo](harness-lock.md#the-team-harness-repo).

## REST

| Route | |
| --- | --- |
| `POST /api/credentials` | `team: true` makes a team credential (admins); `person` an admin names makes it that person's; otherwise an admin's is their own and a member's is theirs. `team: true` with a subscription login is refused (403). The answer has `owner`. |
| `PUT /api/credentials/{name}/owner` | `{"person": "alice"}`, or `{"person": null}` for the team (refused for a subscription login). |
| `GET /api/credentials/migration` | The migration's record (admins). |
| `POST /api/people/{id}/links` | `{"source": "github", "id": "alice-gh"}` (admins). A GitHub login is looked up and stored as its user id (`looked_up` in the answer); `"id": "583231"` skips the lookup. Slack ids are `T…/U…`. |
| `DELETE /api/people/{id}/links?source=github&id=alice-gh` | Unlinks it (admins). |
| `GET /api/people` | Each person with their `links` and inbox delivery settings (`notify`, `tz`, `digest`, `digest_weekends`, `quiet`). |
| `PUT /api/people/{id}/delivery` | `{"dm": "off", "tz": "Europe/Berlin", "digest": ["09:30"], "digest_weekends": false, "quiet": "20:00-08:00"}`, any subset: the person themselves, or an admin. |

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="who-is-trusted"></span>Who is trusted | [Who is trusted](team/who-is-trusted.md) |
| <span id="codex-logins-one-sandbox-at-a-time"></span>Codex logins: one sandbox at a time | [Subscription logins](team/subscription-logins.md#codex-logins-one-sandbox-at-a-time) |
| <span id="subscription-logins"></span>Subscription logins | [Subscription logins](team/subscription-logins.md) |
| <span id="what-the-vendors-allow"></span>What the vendors allow | [What the vendors allow](team/vendor-terms.md) |
| <span id="inbox-delivery-dms-and-the-digest"></span>Inbox delivery: DMs and the digest | [Linked accounts and inbox delivery](team/linked-accounts.md#inbox-delivery-dms-and-the-digest) |
| <span id="check-it-hangar-doctor"></span>Check it: hangar doctor | [Check it: hangar doctor](team/doctor.md) |
