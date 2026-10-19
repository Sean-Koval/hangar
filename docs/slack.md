# Slack: start and steer jobs from a channel

Type `/hangar fix the flaky upload test` in a channel, or mention `@hangar` with the instructions, and hangar runs a [job](jobs/README.md) on that channel's repo. It reports back in a thread:

- when the agent starts;
- when it needs a decision;
- when it finishes, with the outcome, the agent's summary, the PR link, the verification result, and what went wrong if it failed.

Reply in that thread to follow up. The agent picks up its own conversation, on the same branch and PR.

Slack is the door for **unplanned** work: an alert, a question in a thread. Planned work comes through Jira's *Ready for agent* status, and repo chores through a GitHub label or Projects column ([intake.md](intake.md)). The Slack door is maintained but not extended beyond `@hangar adr` (#170).

<figure class="af-figure">
--8<-- "slack-flow.svg"
<figcaption>Slack: /hangar starts a job, the thread carries its progress, a reply in the thread is a follow-up</figcaption>
</figure>

## Commands

| Command | |
| --- | --- |
| `/hangar <instructions>` or `@hangar <instructions>` | Start a job on the channel's repo |
| `/hangar repo=owner/repo env=NAME harness=NAME model=NAME <instructions>` | Choose the repo (any in `HANGAR_GITHUB_REPOS`), environment (one mapped to that repo), harness and model. Options go first. |
| `/hangar status JOB` | A job's state, PR and latest event (only you see the slash command's answer) |
| `@hangar adr [title]` in a thread | Draft an ADR from the thread as a PR ([below](#hangar-adr-a-thread-becomes-a-decision-record)) |
| `/hangar help` | Usage |
| A reply in a job's thread | Follow-up once the current turn is done. Start it with `fix:`, `redirect:` or `info:` to [classify it](#classifying-a-reply) |

Slack doesn't allow slash commands inside threads, so a `/hangar` job gets a new top-level message and the thread hangs off it. An `@hangar` mention inside an existing thread (say, under an alert) runs the job in that thread. The parent message and earlier replies, up to 15 messages and 20 KB, go to the agent as a context file, `slack-thread.md`. Other people and other apps wrote that thread, so the file is marked untrusted, and the prompt tells the agent to treat it as a description of the problem, never as instructions.

## `@hangar adr`: a thread becomes a decision record

Decisions made in Slack threads get lost. Mention `@hangar adr Use Parquet for runs` in a reply in the thread where the
decision was made, on a mapped channel, and hangar drafts an ADR from the thread and opens a PR that adds one file,
`docs/decisions/NNNN-use-parquet-for-runs.md`. A person other than the one who asked reviews and merges it.

- **Opt in per repo.** The repo needs a `docs/decisions/` directory: without one hangar refuses in the thread and
  points to [docs/templates/adr.md](https://github.com/Sean-Koval/hangar/blob/main/docs/templates/adr.md), the format it writes (frontmatter `id`, `kind: decision`,
  `status: proposed`, `label`, `sources` with the thread's permalink and content hash, `author: agent:<model>`,
  `supersedes`; sections Context, Decision, Options considered, Consequences, Checks).
- **The thread is untrusted.** It reaches the agent only as `slack-thread.md`, an untrusted context item (the parent
  and earlier replies, as for any mention), with stored secrets redacted. The instructions are fixed
  (`src/hangar/prompts/adr.md`): write that one file and change nothing else. The agent runs with egress `none`, and
  the job is tainted from turn 1, so hangar opens the PR after the turn as a draft labelled `hangar:tainted`
  ([security/taint.md](security/taint.md#taint-gates)); the agent never pushes or opens it.
- **The PR.** Title `ADR: <title>`, labels `agent-drafted` and `adr`, and a body linking the thread. It never lands on
  the requester's approval, whatever the repo's `self_approval` says ([landing.md](landing.md#agent-drafted-adrs)).
- **hangar checks it** after the turn, as the job's verify: exactly one new file, numbered one past the highest in
  `docs/decisions/`, named after the title, with the frontmatter and sections above. The optional `checks:` block (a
  fenced ```` ```yaml ```` block starting `checks:`, trace predicates flightdeck runs nightly) must be valid YAML; hangar
  never evaluates it. A failure fails the verify with the reason, and the agent gets a fix round like any failing
  check.
- **Path guard.** A change outside `docs/decisions/` still opens, as a draft, with a warning in the review packet.
- **Numbering.** Two ADR jobs that pick the same number are flagged in the packet; whichever merges second renumbers
  (a follow-up on its PR).

## Set it up

```bash
hangar setup slack --manifest slack-app.yaml
```

1. **Create the app.** `hangar setup slack` writes the [app manifest](slack-app-manifest.yaml) with your hangar's URL filled in. At <https://api.slack.com/apps>, choose *Create New App → From a manifest*, pick the workspace and paste it. Then install the app to the workspace.

   Slack checks the events URL right away. That check fails until the next step is done; you'll retry it in step 3.
2. **Give hangar the secrets.** Paste the *Signing Secret* (from *Basic Information*) and the *Bot User OAuth Token* (`xoxb-…`, from *OAuth & Permissions*) when asked. Neither is echoed. Then give the channel to take work from, and its repo. The wizard does four things:
   - checks the bot token with `auth.test`;
   - stores both secrets in the vault as `slack-signing` and `slack-bot` (they never go into sandboxes);
   - saves the Slack settings in hangar's database (no config map to edit);
   - sends a signed test request through hangar's Slack route.

   To script it: `hangar setup slack --signing-secret-file … --bot-token-file … --channel C0123ABCD=acme/app --user U0456EFGH --non-interactive`. Get a channel's ID from *channel name → About*, at the bottom. Each channel's repo must already be in `HANGAR_GITHUB_REPOS` (`hangar setup github`), which picks its environment.
3. **Finish in Slack.** Under *Event Subscriptions*, retry the request URL; it should verify now. Then invite the app to each mapped channel with `/invite @hangar`. Thread replies only arrive from channels the app is in.
4. **Check the setup** with `hangar doctor`. It checks:
   - that both credentials are stored;
   - that the bot token works (`auth.test`);
   - that every mapped channel points at a repo in `HANGAR_GITHUB_REPOS`;
   - that the bot token has `im:write`, for [inbox DMs](#inbox-dms), and who has no linked Slack account to get them.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_SLACK_SIGNING_SECRET_CREDENTIAL` | | Vault credential holding the signing secret. The Slack routes return 404 until it is set. |
| `HANGAR_SLACK_BOT_TOKEN_CREDENTIAL` | | Vault credential holding the bot token |
| `HANGAR_SLACK_CHANNELS` | `{}` | `{"<channel id>": "owner/repo"}`: the channels that may start jobs, and each one's default repo. An exact git URL also works, as in `HANGAR_GITHUB_REPOS`. |
| `HANGAR_SLACK_USERS` | `[]` | Slack user IDs (`U…`, or `T…/U…` to pin the workspace) allowed to start and steer jobs. Empty means anyone in a mapped channel who belongs to the workspace the app is installed in. Members of other organizations (Slack Connect) count only when listed here. |
| `HANGAR_SLACK_TEAMS` | `[]` | Other workspace IDs (`T…`) whose members count as your own, for Enterprise Grid orgs whose users carry another workspace's ID |
| `HANGAR_SLACK_API_URL` | `https://slack.com/api` | |

## How it behaves

- **Requests are verified.** Each request must carry a valid `X-Slack-Signature`: `v0=` followed by the HMAC-SHA256 of `v0:<timestamp>:<raw body>`, keyed with the signing secret and compared in constant time. Requests whose `X-Slack-Request-Timestamp` is more than 5 minutes off are refused with 401.
- **Within 3 seconds.** Slack expects an answer within 3 seconds. hangar acknowledges the command or event at once. It posts to Slack, reads the thread and submits the job in the background.
- **Exactly once.** Slack retries an event it thinks failed (`X-Slack-Retry-Num`), and a mention in a thread arrives both as `app_mention` and as `message`. hangar acts once per `event_id` and once per message. The record of handled events survives a restart. An event counts as handled only once its handling finishes, so one that a dying process acknowledged but never handled runs when Slack retries it.
- **No replays.** Slack doesn't retry slash commands, so a second copy of a signed command (same signature) inside the five-minute window is a replay: hangar answers "That request was already handled." and starts nothing.
- **Other organizations.** In a Slack Connect channel, messages from another organization's members carry their own workspace ID (`user_team`). hangar ignores their mentions and thread replies unless they are listed in `HANGAR_SLACK_USERS`, so sharing a channel doesn't give a partner company an agent on your repo. (Slack doesn't let another organization run your app's slash commands at all.)
- **Limits.** Each Slack user is a caller for [rate limits and the active-job cap](security/front-doors.md#front-door-limits): by default 10 jobs or follow-ups at once, refilling at 30 a minute, and at most 10 unfinished jobs. Past a limit, hangar says so in the thread.
- **Only people.** hangar ignores messages from bots (its own included), edits and deletions.
- **Who's asking.** The requester is `slack:<team>/<user>`, shown in the job's timeline, PR body and audit log. Slack jobs are fenced in like a `jobs` token: only repos in `HANGAR_GITHUB_REPOS`, only their environments, and no choice of provider.
- **Who can follow up.** On a person's job, only that person (an admin uses `hangar job follow-up`); on a team job, anyone the channel trusts. A job started by someone whose Slack account is linked (`hangar person link <id> slack T…/U…`) is that person's: it runs with their own login and budget, and anyone else's reply in its thread is refused, naming whose job it is. A job started by someone with no linked account is a team job. GitHub and Jira follow the same rule (`teamcreds.may_continue`). A reply while the agent is still working gets "Can't continue yet"; reply again when the turn is done.
- **Needs input.** When an agent stops with a question (job state `needs_input`, spec 0011 §2), hangar posts the question in the thread. Your reply is the answer: it runs as an ordinary follow-up.
- **Safe output.** Text hangar didn't write itself, such as the agent's summary, failure output, or your instructions echoed back, is escaped. It can't ping `@channel` or turn into a link. Known secrets are redacted before anything is posted.
- **When posting fails.** If the app isn't in the channel or the token is wrong, a slash command answers privately, through Slack's `response_url`, with the reason, and no job starts. Failures to post updates are recorded on the job's timeline.

## Classifying a reply

Under hangar's "Continuing hangar job …" answer to a thread reply, three buttons ask what the reply was: *Correction* (the agent got it wrong), *Redirect* (you changed the task) or *New info* (you added something it didn't know). One click records it; only corrections count against the agent in the harness metrics ([steering.md](steering.md#classifying-steers)). Or start the reply with `fix:`, `redirect:` or `info:`: it is classified on arrival, the prefix is stripped, and no buttons are shown. An answer to the agent's question counts as new info.

- Only the person who sent the reply can classify it. Anyone else's click gets a private ("only you can see this") refusal.
- Buttons come only on the acknowledgement of a person's reply, never on hangar's own status posts.
- Clicks arrive at `POST /api/hooks/slack/interactions` (the manifest's *Interactivity* request URL). They are verified like events and commands: the signing secret, the 5-minute timestamp window, the body size cap, and a replay guard (a second copy of a signed click is ignored). An app created before this needs *Interactivity & Shortcuts* turned on with that URL.

## Inbox DMs

hangar also DMs people what needs them ([the inbox](landing.md#delivery-it-comes-to-you)): each new question or approval of their agents at once, and a digest at 10:15 and 16:00. It opens the DM with `conversations.open` and posts with `chat.postMessage`, so the app needs the **`im:write`** bot scope (in the [manifest](slack-app-manifest.yaml); an app installed before it needs the scope added and a reinstall). `hangar doctor` warns when the bot token lacks it. A DM goes to a person's linked Slack account (`hangar person link <id> slack T…/U…`); settings per person are `hangar person set <id> --dm off|questions|all --tz … --digest … --quiet …` ([team.md](team.md#inbox-delivery-dms-and-the-digest)). DM text is escaped and redacted like thread posts, and links to the thread, ticket or PR where the answer is given; there are no buttons in it.

## Security notes

Treat a mapped channel like write access to its repo's `hangar/*` branches. Anyone in your workspace who can post there can start an agent, unless `HANGAR_SLACK_USERS` is set. Messages in the thread are context for the agent, and the prompt marks them as untrusted data. Even so, they are untrusted input, like issue comments; see [jobs → Security notes](jobs/reference.md#security-notes). A thread reply is a follow-up with its sender's authority: on a team job, any allowed user's; on a person's job, only that person's ([who can follow up](#how-it-behaves)). Keep PR merges human-approved.

`/hangar status JOB` answers for any job on a repo Slack may use, including jobs started over REST or GitHub, to anyone allowed in a mapped channel. It shows the title, state, requester and latest event.
