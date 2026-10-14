# Team setup: people, linked accounts and credentials

One hangar serves a team. This guide sets it up for one: add the people, link their GitHub, Slack and Jira accounts, add the team's credentials, and let each person add their own logins. [security.md](security.md#people) has the access model behind it; [upgrading.md](upgrading.md#7-people-your-logins-stay-yours-add-team-credentials-then-the-team) covers an existing install.

```bash
hangar person add alice --name "Alice"                    # 1. people (prints her login token once)
hangar person link alice github alice-gh                  # 2. her accounts elsewhere
hangar person link alice slack U012AB3CD
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt   # 3. team credentials
hangar cred add codex-team --team --harness codex --kind api_key --file openai-key.txt
#   alice, on her machine: claude setup-token | hangar cred add claude-alice --harness claude-code --kind oauth_token
hangar doctor                                             # 5. check
```

## 1. Add people

```bash
hangar person add alice --name "Alice"            # a member: sees and acts on her own work
hangar person add bob --role admin                # an admin: everyone's work, people and the vault
hangar person ls
```

`person add` prints the person's login token once, and lists the team credentials their sandboxes will get. They run `hangar login <url>` with it, then `hangar connect` for their MCP clients. `hangar person token alice` mints another one (a new laptop); `hangar person rm alice` revokes them all.

## 2. Link their accounts elsewhere

Work reaches hangar from GitHub (issue labels, `/hangar` comments), Slack, and Jira and GitHub Projects intake. Each sender is an account there, not a hangar person, until an admin links them:

```bash
hangar person link alice github alice-gh           # a GitHub login (any case; also GitHub Projects items)
hangar person link alice slack U012AB3CD           # a Slack user id (profile → ⋯ → Copy member ID)
hangar person link alice jira 5b10ac8d82e05b22cc7d4ef5   # an Atlassian account id
hangar person link alice email alice@acme.com      # a Jira user known only by email
hangar person link owner github sean               # you, too: the owner exists without being added
hangar person unlink alice github alice-gh
```

A job from a linked account **runs for that person**:

- it uses their own login when they have one, else the team's ([below](#whose-login-a-job-uses));
- it counts against their caps (running jobs, sessions, sandboxes) and their daily budget (`HANGAR_PERSON_MAX_USD_PER_DAY`), not a separate bucket per account;
- it is theirs to see and act on over MCP (`list_jobs`, `get_job`, `follow_up_job`, `cancel_job`), as their own work is.

A job from an account linked to nobody **runs for the team**: team credentials, no person's caps, visible to admins only. `hangar doctor` names the senders who start jobs often but are linked to nobody.

Linking doesn't change trust. Who may start work without approval is still decided by the front door's rules ([triggers.md](triggers.md#1-who-can-start-work-from-github), and Slack's channel and user lists): a linked outsider's GitHub job still waits for a trusted person's `/hangar approve`, and an unlinked collaborator's still runs at once. One account links to one person. Only admins link, since a link decides whose work a job is.

Intake that adds a source calls `people.resolve_external(store, source, external_id)`, which returns the linked `Person` or `None`, and passes `Caller(..., person=p.id)` (`None` for the team). Sources are `github` (`github_projects` is the same), `slack`, `jira` and `email`; a `jira` id with an `@` is looked up as an email.

## 3. Team credentials

A **team credential** is one an admin adds for everyone. Jobs no person owns use it, and so does anyone without a login of their own:

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt
hangar cred add codex-team  --team --harness codex --kind api_key --file openai-key.txt
hangar cred add gh-jobs     --team --harness git --kind git_token --file token.txt   # direct-mode git
hangar cred owner gh-jobs team                     # or make an existing API key or token the team's
hangar cred ls                                     # owner=team | owner=<person> | owner=control-plane
```

Without `--team`, a credential an admin adds is **their own**, so an admin's personal login never becomes the team's by accident. Control-plane credentials (webhook signing keys, bot tokens, GitHub App keys, hangar's own GitHub token) belong to neither: they never reach a sandbox. Members can't add team credentials, and a person's own subscription login can't be made the team's (`hangar cred owner` refuses it): add a team credential of its own.

Environments may list team credentials by name as before; a sandbox that runs for no one, or for someone without a login of their own, gets the ones its environment lists for its harness, or every team credential for the harness when it lists no team one. A git token is swapped the same way when the environment lists one.

**Use API keys for team credentials** ([what the vendors allow](#what-the-vendors-allow)). API keys also have no lease, so any number of sandboxes use one at once.

## 4. Per-person logins

Each person adds their own:

```bash
claude setup-token | hangar cred add claude-alice --harness claude-code --kind oauth_token
hangar cred add codex-alice --harness codex --kind auth_file --file ~/.codex/auth.json
```

A member's credential is always their own. It goes only into their sandboxes, replacing the team's for that harness, and naming someone else's is refused. An admin stores one for someone with `--person alice`, or moves one with `hangar cred owner NAME alice`.

## Whose login a job uses

| Work | Runs for | Login, by default |
| --- | --- | --- |
| MCP, REST, CLI, A2A | the caller's person | theirs, else the team's |
| A schedule | its person (none for schedules from before people) | theirs, else the team's |
| GitHub issue or comment, Slack, Jira, GitHub Projects | the linked person, else the team | theirs, else the team's |
| Inbound webhooks | the team | the team's |

A repo's policy can say otherwise:

```bash
hangar repo set acme/app --credentials person            # only the person's own; a job for no one fails, saying why
hangar repo set acme/app --credentials team              # always the team's, even for a person's job
hangar repo set acme/app --credentials person_then_team  # the default
```

A job on a `team` repo still runs for its person: their caps, budget and lists. Only the login changes.

## Codex logins: one sandbox at a time

A Codex ChatGPT login (`~/.codex/auth.json`) rotates its refresh token as it's used, so hangar leases it to one sandbox at a time and writes the new token back when the sandbox ends. The same holds for the other sign-in files (pi, Gemini). One such login serves one sandbox at once, across everyone who uses it.

When several logins of a kind are available to a sandbox (a person's own, or the team's when an environment lists several or none), they form a pool and a sandbox takes a free one. When all are in use, a job or session **waits** and says so in its events: `waiting for a free login (queued): the team's codex logins codex-1 (in sandbox …), codex-2 (in sandbox …)`. It starts when one is released, or fails after `HANGAR_LOGIN_WAIT_SECONDS` (600) with the same words. A sandbox made directly (`create_sandbox`) fails at once instead. A job's sandbox keeps its login while it is kept for follow-ups (`keep_minutes`, 120 by default), so a login stays busy for up to that long after each job; `keep_minutes: 0` releases it when the turn ends (follow-ups still resume from the checkpoint). To run many Codex jobs in parallel, use an OpenAI API key, which has no lease.

## What the vendors allow

Checked on 2026-10-14 against the vendors' own pages. This is a summary, not legal advice; read the terms that apply to your plan.

- **Anthropic.** Free, Pro and Max logins are under the [Consumer Terms](https://www.anthropic.com/legal/consumer-terms): "You may not share your Account login information, Anthropic API key, or Account credentials with anyone else" and "You also may not make your Account available to anyone else." Claude Code's [legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance) adds that Pro and Max usage limits "assume ordinary, individual usage", and that configuring an API key "for use by the customer's own authorized users" is fine when the usage is billed to the key's owner.
- **OpenAI.** The [Terms of Use](https://openai.com/policies/terms-of-use/) say: "You may not share your account credentials or make your account available to anyone else". The [Codex authentication docs](https://developers.openai.com/codex/auth) say to "Use API key authentication for programmatic Codex CLI workflows, such as CI/CD jobs" and to treat `~/.codex/auth.json` like a password.

So in hangar: **a subscription login (a Claude OAuth token, a ChatGPT `auth.json`, a Google sign-in) is one person's own**, used only in their sandboxes. **Team credentials should be API keys**: an Anthropic Console key and an OpenAI project key, billed to your organization. Hangar lets an admin add a team subscription login on purpose (a dedicated account), but says the terms each time, and `hangar doctor` warns about every one.

## Credentials stored before people

Before people, every credential belonged to no one and went into every sandbox. On its first start this release makes each of them **the owner's own**, once, and records it; control-plane credentials stay the control plane's:

```bash
hangar cred migration
# done 2026-10-14 10:02: made owner's own: claude-sean, codex-sean, gh-pat
# left to the control plane: gh-hook, gh-token
```

Your logins then never reach a member's sandbox, nor a job no person owns. Make the ones the team may share the team's (`hangar cred owner gh-pat team`, for an API key or token), add team API keys, and link your own GitHub login (`hangar person link owner github <login>`) so your issues keep running with your login. `GET /api/credentials/migration` returns the same record.

## Check it: `hangar doctor`

- **personal credentials shared**: subscription logins that are team credentials, and what the migration did.
- **team credentials for jobs**: mapped repos (GitHub and Slack), inbound webhooks and schedules without a person whose environment's harness would get no login, and repos whose `credentials: person` policy makes unlinked senders' jobs fail.
- **unlinked senders**: GitHub and Slack accounts linked to nobody that started 3 or more jobs in the last 14 days, with the `hangar person link` command for each.

## REST

| Route | |
| --- | --- |
| `POST /api/credentials` | `team: true` makes a team credential (admins); `person` an admin names makes it that person's; otherwise an admin's is their own and a member's is theirs. The answer has `owner` and, for a shared subscription login, a `warning`. |
| `PUT /api/credentials/{name}/owner` | `{"person": "alice"}`, or `{"person": null}` for the team (refused for a person's own subscription login). |
| `GET /api/credentials/migration` | The migration's record (admins). |
| `POST /api/people/{id}/links` | `{"source": "github", "id": "alice-gh"}` (admins). |
| `DELETE /api/people/{id}/links?source=github&id=alice-gh` | Unlinks it (admins). |
| `GET /api/people` | Each person with their `links`. |
