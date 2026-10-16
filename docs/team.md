# Team setup: people, linked accounts and credentials

One hangar serves a team. This guide sets it up for one: add the people, link their GitHub, Slack and Jira accounts, add the team's credentials, and let each person add their own logins. [security.md](security.md#people) has the access model behind it; [upgrading.md](upgrading.md#7-people-your-logins-stay-yours-add-team-credentials-then-the-team) covers an existing install.

```bash
hangar person add alice --name "Alice"                    # 1. people (prints her login token once)
hangar person link alice github alice-gh                  # 2. her accounts elsewhere
hangar person link alice slack T0123ABCD/U012AB3CD
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt   # 3. team credentials
hangar cred add codex-team --team --harness codex --kind api_key --file openai-key.txt
#   alice, on her machine: hangar cred add claude-alice --harness claude-code --kind api_key --file key.txt
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
hangar person link alice github alice-gh           # a GitHub login, stored as its numeric user id
hangar person link alice slack T0123ABCD/U012AB3CD # workspace/user id (profile → ⋯ → Copy member ID)
hangar person link alice jira 5b10ac8d82e05b22cc7d4ef5   # an Atlassian account id (Data Center: the user key)
hangar person link alice email alice@acme.com      # a Jira user known only by email
hangar person link owner github sean               # you, too: the owner exists without being added
hangar person unlink alice github alice-gh
```

Links use ids that don't change hands. A GitHub login is looked up once, when you link it, with the control plane's GitHub token (`https://api.github.com/users/<login>`), and stored as the account's numeric id: a login can be renamed, and whoever registers the old one is not your teammate. Without a GitHub token, link by the id itself. A Slack user id is unique only within its workspace, so a link names both (`T…/U…`); in a Slack Connect channel the poster's own workspace counts.

Only admins link, and hangar doesn't check that the account is the person's: you vouch for it. Link an account you've seen the person use, for example by asking them to comment from it.

A job from a linked account **runs for that person**:

- it uses their own login when they have one, else the team's ([below](#whose-login-a-job-uses));
- it counts against their caps (running jobs, sessions, sandboxes) and their daily budget (`HANGAR_PERSON_MAX_USD_PER_DAY`), not a separate bucket per account;
- it is theirs to see and act on over MCP (`list_jobs`, `get_job`, `follow_up_job`, `cancel_job`), as their own work is.

A job from an account linked to nobody **runs for the team**: team credentials, no person's caps, visible to admins only. `hangar doctor` names the senders who start jobs often but are linked to nobody.

### Who is trusted

One rule for every front door (GitHub issues and PRs, Slack, Jira, GitHub Projects):

1. **A linked account acts as its person.** It is trusted: its work starts without approval, and it can approve others' work and answer questions. That gives it nothing its person can't already do over MCP, within their caps, budget and visibility.
2. **Only a job's person continues it from a front door.** A person's job runs with their own login, so a follow-up (`/hangar …` on its PR or issue, a Slack thread reply, a Jira comment) or an answer to its question from anyone else is refused, saying whose job it is. An admin continues anyone's job with `hangar job follow-up`. A team job (no person) can be continued by anyone the front door trusts.
3. **An account linked to nobody acts for the team**, and is trusted only by its source's own rule:
   - GitHub issues and PRs: author association owner, member or collaborator, or `HANGAR_GITHUB_TRUSTED` when set ([triggers.md](triggers.md#1-who-can-start-work-from-github));
   - GitHub Projects: `HANGAR_GITHUB_TRUSTED` only, since project item events carry no association;
   - Slack: a member of the workspace in a mapped channel, within `HANGAR_SLACK_USERS` when that's set; another organization's user only when listed there;
   - Jira: never. Anyone who can move an issue could otherwise start work, so their job waits for a linked teammate's `/hangar approve`.

   An untrusted sender's job waits for approval, its text is fenced as untrusted, and it can't continue a job or answer its question.
4. **Ticket text is data.** Issue bodies and comments not written by a trusted account reach the agent fenced as untrusted, and so does a Jira description always, since anyone who can edit the issue can change it. Only a trusted sender's own `/hangar …` text is an instruction. (An issue's title is still the job's title, which the prompt shows.)
5. **Who acted is what the source signs**: the sender id in a signed GitHub delivery, the user and their workspace in a signed Slack event, the user in a signed Jira webhook. A Jira Automation request isn't signed and its body is written by whoever edits the rule, so hangar reads who moved the issue from the issue's own history instead ([intake.md](intake.md#or-an-automation-rule)).
6. **A review starts an auto-fix round only from someone the job's person or the repo trusts.** A round runs with the job's person's login, like a follow-up, so on a person's job it takes their linked account, an admin's linked account, a login in the repo policy's `approvers`, or a code owner of every path the PR touches (what lets an approval land it, [landing.md](landing.md)). On a team job, also anyone rules 1 and 3 trust. Never a bot or hangar's own App. What needs GitHub (CODEOWNERS, the PR's files, team membership) is checked after the webhook is answered, since GitHub waits only 10 seconds; a refusal is audited as `github.refused` (`untrusted review`). Unlike a `/hangar` follow-up, the reviewer's text isn't an instruction: every review comment, the job's person's included, reaches the agent fenced as untrusted data and labelled with its author, and the instruction is hangar's own ("address each comment that asks for a change to this task's code").

Intake that adds a source calls `people.resolve_external(store, source, external_id)`, which returns the linked `Person` or `None`, and passes `Caller(..., person=p.id)` (`None` for the team), trusted when it isn't `None`. The ids are a GitHub user's numeric id (`sender.id`), a Slack `TEAM/USER`, a Jira account id or Data Center user key, or an email; `github_projects` is the same as `github`, and a `jira` id with an `@` is looked up as an email. A follow-up passes `front_door=True, sender_person=…` to `JobService.follow_up`.

## 3. Team credentials

A **team credential** is one an admin adds for everyone. Jobs no person owns use it, and so does anyone without a login of their own:

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt
hangar cred add codex-team  --team --harness codex --kind api_key --file openai-key.txt
hangar cred add gh-jobs     --team --harness git --kind git_token --file token.txt   # direct-mode git
hangar cred owner gh-jobs team                     # or make an existing API key or token the team's
hangar cred ls                                     # owner=team | owner=<person> | owner=control-plane
```

Without `--team`, a credential an admin adds is **their own**, so an admin's personal login never becomes the team's by accident. Control-plane credentials (webhook signing keys, bot tokens, GitHub App keys, hangar's own GitHub token) belong to neither: they never reach a sandbox. Members can't add team credentials.

**A team credential is an API key, or another credential the vendor's terms allow for automation. A subscription login never is.** A Claude Pro or Max login (`--kind oauth_token`), a ChatGPT `auth.json`, a Google sign-in and the other sign-in files are refused as team credentials, whether added with `--team`, stored over with `team: true`, or moved with `hangar cred owner NAME team` ([what the vendors allow](#what-the-vendors-allow)). The refusal names the `hangar cred add … --kind api_key` command to use instead. API keys also have no lease, so any number of sandboxes use one at once.

Environments may list team credentials by name as before; a sandbox that runs for no one, or for someone without a login of their own, gets the ones its environment lists for its harness, or every team credential for the harness when it lists no team one. A git token is swapped the same way when the environment lists one.

## 4. Per-person logins

Each person adds their own, an API key first: it serves their jobs, schedules and sessions alike.

```bash
hangar cred add claude-alice --harness claude-code --kind api_key --file anthropic-key.txt
hangar cred add codex-alice --harness codex --kind api_key --file openai-key.txt
# optional, for her own interactive sessions only (see below):
claude setup-token | hangar cred add claude-alice-max --harness claude-code --kind oauth_token
```

A member's credential is always their own. It goes only into their sandboxes, replacing the team's for that harness, and naming someone else's is refused. An admin stores one for someone with `--person alice`, or moves one with `hangar cred owner NAME alice`.

## Whose login a job uses

| Work | Runs for | Login, by default |
| --- | --- | --- |
| MCP, REST, CLI, A2A jobs (`submit_job`) | the caller's person | their API key, else the team's |
| A schedule | its person (none for schedules from before people) | their API key, else the team's |
| GitHub issue or comment, Slack, Jira, GitHub Projects | the linked person, else the team | their API key, else the team's |
| Inbound webhooks | the team | the team's |
| A person's own `new_session`, `create_sandbox`, `run_agent` | the caller's person | theirs (a subscription login too, with a warning), else the team's |

Every job's sandbox is automated work, whatever its front door: best-of-N attempts, follow-ups, autofix rounds, environment-setup jobs, Cloud Doctor jobs, experiment trials and graders, and replay and fork sandboxes of any of them. None gets a [subscription login](#subscription-logins).

A repo's policy can say otherwise:

```bash
hangar repo set acme/app --credentials person            # only the person's own; a job for no one fails, saying why
hangar repo set acme/app --credentials team              # always the team's, even for a person's job
hangar repo set acme/app --credentials person_then_team  # the default
```

A job on a `team` repo still runs for its person: their caps, budget and lists. Only the login changes.

## Codex logins: one sandbox at a time

A Codex ChatGPT login (`~/.codex/auth.json`) rotates its refresh token as it's used, so hangar leases it to one sandbox at a time and writes the new token back when the sandbox ends. The same holds for the other sign-in files (pi, Gemini). One such login serves one sandbox at once, across everyone who uses it.

A sign-in file is always someone's own, and only interactive sessions and sandboxes use one ([subscription logins](#subscription-logins); jobs only with `HANGAR_SUBSCRIPTION_LOGINS=any`), so this is about one person's sessions. When a person has several logins of a kind, they form a pool and a sandbox takes a free one. When all are in use, a job or session **waits** and says so in its events: `waiting for a free login (queued): alice's codex logins codex-1, codex-2 (all in use)`. It doesn't name the sandboxes holding them, which may be other people's. It starts when one is released, or fails after `HANGAR_LOGIN_WAIT_SECONDS` (600) with the same words. A sandbox made directly (`create_sandbox`) fails at once instead. A job's sandbox keeps its login while it is kept for follow-ups (`keep_minutes`, 120 by default), so a login stays busy for up to that long after each job; `keep_minutes: 0` releases it when the turn ends (follow-ups still resume from the checkpoint). To run many Codex jobs in parallel, use an OpenAI API key, which has no lease.

## Subscription logins

A subscription login is a credential stored with `--kind oauth_token` or `--kind auth_file`: `claude setup-token` (Claude Pro or Max), a ChatGPT `~/.codex/auth.json`, a Google sign-in for Antigravity, pi's and Gemini's sign-in files. The vendors' terms reserve these for one person's own use, and don't allow automated or headless use ([what the vendors allow](#what-the-vendors-allow)); Anthropic has enforced this against third-party harnesses since April 2026. So hangar uses them only interactively:

- **Jobs, schedules, webhooks, experiments and replays refuse them.** An automated resolution leaves subscription logins out of the pool. A person with only `claude setup-token` gets this, on the ticket or thread that started the job, before any sandbox starts:

  ```
  job needs an API-key credential for claude-code; subscription logins are interactive-only (docs/team.md#subscription-logins). add one: hangar cred add NAME --harness claude-code --kind api_key --file key.txt (your own; an admin adds --team for work no person owns)
  ```

  A job that names a subscription login in `credentials` is refused the same way. Their person's API key, or the team's, is used instead when there is one.
- **Interactive sessions and sandboxes warn.** A person's own `new_session`, `create_sandbox` or `run_agent` still uses their subscription login, and says so: a warning event, `warnings` on `get_session` and the sandbox, and an entry in each turn's manifest `warnings[]`: `subscription login in a hangar sandbox: personal, interactive use only`.
- **`HANGAR_SUBSCRIPTION_LOGINS`**: `interactive` (the default) as above; `any` lets jobs use them, with a warning on the job and sandbox, and `hangar doctor` fails the **subscription logins** check. Experiment trials and graders never get one, whatever it says.
- **Spend caps.** Put a workspace spend limit on each API key at the vendor (Anthropic Console workspace limits, OpenAI project limits); hangar can't set those. Hangar's job budgets and daily caps ([jobs.md](jobs.md)) are the cap inside hangar. Use both.
- **Codex Enterprise access tokens** ("for trusted scripts, schedulers, and private CI runners") are for automation: store one with `--kind api_key` and hangar treats it as an API key.
- **Antigravity**: a Google sign-in is an `auth_file`, so fleets and jobs use a `GEMINI_API_KEY` credential (`--kind api_key`).
- The one-at-a-time lease for sign-in files ([below](#codex-logins-one-sandbox-at-a-time)) still applies to interactive sessions.

Credential resolution happens when a job's sandbox is claimed; a submit also runs the same resolution as a dry run, so an obviously missing API key fails the submit at once, even while the pools are down.

## What the vendors allow

Checked on 2026-10-14 against the vendors' own pages. This is a summary, not legal advice; read the terms that apply to your plan.

- **Anthropic.** Free, Pro and Max logins are under the [Consumer Terms](https://www.anthropic.com/legal/consumer-terms): "You may not share your Account login information, Anthropic API key, or Account credentials with anyone else" and "You also may not make your Account available to anyone else." Claude Code's [legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance) adds that Pro and Max usage limits "assume ordinary, individual usage", and that configuring an API key "for use by the customer's own authorized users" is fine when the usage is billed to the key's owner. The same page says Anthropic doesn't permit developers "to route requests through Free, Pro, or Max plan credentials on behalf of their users", and that where Claude Code runs "in hosted sandboxes or other agent infrastructure", "each end user must authenticate with their own Anthropic API key, Claude subscription plan credentials, or 3P inference provider credential". A team subscription login used for everyone's jobs is exactly that kind of routing.
- **OpenAI.** The [Terms of Use](https://openai.com/policies/terms-of-use/) say: "You may not share your account credentials or make your account available to anyone else". The [Codex authentication docs](https://developers.openai.com/codex/auth) say to "Use API key authentication for programmatic Codex CLI workflows, such as CI/CD jobs" and to treat `~/.codex/auth.json` like a password. In a ChatGPT Enterprise workspace, an admin can also let members create Codex access tokens "for trusted scripts, schedulers, and private CI runners". A member creates one, so store it as theirs, like a login.

So in hangar: **a subscription login (a Claude OAuth token, a ChatGPT `auth.json`, a Google sign-in) is one person's own**, used only in their sandboxes. **Team credentials are API keys**, such as an Anthropic Console key and an OpenAI project key billed to your organization, or other credentials the vendor's terms allow for automation. Hangar refuses a subscription login as a team credential outright, even one for a dedicated account, since everyone's jobs would route through it. If one is stored as the team's anyway (by an older version, or around the API), `hangar doctor` fails until it is someone's own or removed.

## Credentials stored before people

Before people, every credential belonged to no one and went into every sandbox. On its first start this release makes each of them **the owner's own**, once, and records it; control-plane credentials stay the control plane's:

```bash
hangar cred migration
# done 2026-10-14 10:02: made owner's own: claude-sean, codex-sean, gh-pat
# left to the control plane: gh-hook, gh-token
```

Your logins then never reach a member's sandbox, nor a job no person owns. Make the ones the team may share the team's (`hangar cred owner gh-pat team`, for an API key or token), add team API keys, and link your own GitHub login (`hangar person link owner github <login>`) so your issues keep running with your login. `GET /api/credentials/migration` returns the same record.

## Check it: `hangar doctor`

- **personal credentials shared**: fails for any subscription login stored as a team credential, and says what the migration did.
- **team credentials for jobs**: mapped repos (GitHub and Slack), inbound webhooks and schedules without a person whose environment's harness would get no login, and repos whose `credentials: person` policy makes unlinked senders' jobs fail.
- **subscription logins**: warns for mapped repos, webhooks and schedules whose harness would find only subscription logins, and for people whose only logins for a harness are subscriptions (their jobs fail while their sessions work); fails under `HANGAR_SUBSCRIPTION_LOGINS=any`.
- **unlinked senders**: GitHub, Slack, Jira and GitHub Projects accounts linked to nobody that started 3 or more jobs in the last 14 days, with the `hangar person link` command for each.

## REST

| Route | |
| --- | --- |
| `POST /api/credentials` | `team: true` makes a team credential (admins); `person` an admin names makes it that person's; otherwise an admin's is their own and a member's is theirs. `team: true` with a subscription login is refused (403). The answer has `owner`. |
| `PUT /api/credentials/{name}/owner` | `{"person": "alice"}`, or `{"person": null}` for the team (refused for a subscription login). |
| `GET /api/credentials/migration` | The migration's record (admins). |
| `POST /api/people/{id}/links` | `{"source": "github", "id": "alice-gh"}` (admins). A GitHub login is looked up and stored as its user id (`looked_up` in the answer); `"id": "583231"` skips the lookup. Slack ids are `T…/U…`. |
| `DELETE /api/people/{id}/links?source=github&id=alice-gh` | Unlinks it (admins). |
| `GET /api/people` | Each person with their `links`. |
