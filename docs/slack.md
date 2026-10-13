# Slack: start and steer jobs from a channel

Type `/hangar fix the flaky upload test` in a channel, or mention `@hangar` with the instructions, and hangar runs a [job](jobs.md) on that channel's repo. It reports back in a thread:
- when the agent starts;
- when it needs a decision;
- when it finishes, with the outcome, the agent's summary, the PR link, the verification result, and what went wrong if it failed.

Reply in that thread to follow up. The agent picks up its own conversation, on the same branch and PR.

```
/hangar <instructions> ──► hangar posts "<@you> asked hangar to work on owner/repo" ──► thread:
                              Queued as hangar job job-3fa2c1d0 …
                              Agent started …
                              Done: PR, verification, summary
   you, in the thread: "also handle empty files" ──► follow-up turn ──► Done …
```

## Commands

| Command | |
| --- | --- |
| `/hangar <instructions>` or `@hangar <instructions>` | Start a job on the channel's repo |
| `/hangar repo=owner/repo env=NAME harness=NAME model=NAME <instructions>` | Choose the repo (any in `HANGAR_GITHUB_REPOS`), environment (one mapped to that repo), harness and model. Options go first. |
| `/hangar status JOB` | A job's state, PR and latest event (only you see the slash command's answer) |
| `/hangar help` | Usage |
| A reply in a job's thread | Follow-up once the current turn is done |

Slack doesn't allow slash commands inside threads, so a `/hangar` job gets a new top-level message and the thread hangs off it. An `@hangar` mention inside an existing thread (say, under an alert) runs the job in that thread. The parent message and earlier replies, up to 15 messages and 20 KB, go to the agent as a context file, `slack-thread.md`.

## Set it up

1. **Create the app.** At <https://api.slack.com/apps>, choose *Create New App → From a manifest*, pick the workspace and paste [`slack-app-manifest.yaml`](slack-app-manifest.yaml). Before pasting, replace `hangar.example.com` with your hangar's public URL. Install the app to the workspace.

   Slack checks the events URL right away. That check fails until step 3 is done; you'll retry it in step 4.
2. **Store its two secrets in hangar's vault.** Use *Basic Information → Signing Secret* and *OAuth & Permissions → Bot User OAuth Token* (`xoxb-…`). Paste each value when prompted, then press Ctrl-D. Nothing is echoed, and neither value is ever printed back:
   ```bash
   hangar cred add slack-signing --harness webhook --kind generic
   hangar cred add slack-bot --harness webhook --kind generic
   ```
   Both stay in the control plane. They are never put into sandboxes.
3. **Map channels to repos.** Get a channel's ID from *channel name → About*, at the bottom (it looks like `C0123ABCD`). Each repo must also be in `HANGAR_GITHUB_REPOS`, which picks its environment. On GKE, add the keys to the `hangar-site` config map, which `deploy/gke/up.sh` never overwrites:
   ```bash
   kubectl -n hangar create configmap hangar-site \
     --from-literal=HANGAR_SLACK_SIGNING_SECRET_CREDENTIAL=slack-signing \
     --from-literal=HANGAR_SLACK_BOT_TOKEN_CREDENTIAL=slack-bot \
     --from-literal=HANGAR_SLACK_CHANNELS='{"C0123ABCD": "acme/app"}' \
     --from-literal=HANGAR_SLACK_USERS='["U0456EFGH", "U0789IJKL"]' \
     --from-literal=HANGAR_GITHUB_REPOS='{"acme/app": "python"}' \
     --from-literal=HANGAR_GITHUB_TOKEN_CREDENTIAL=gh-control \
     --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n hangar rollout restart deploy/hangar
   ```
   `create configmap … | kubectl apply` replaces the whole map. Keep the keys you already had, such as the GitHub ones from [jobs §4](jobs.md#4-trigger-from-github). Elsewhere, set the same `HANGAR_*` variables in the environment or `.env`.
4. **Finish in Slack.** Under *Event Subscriptions*, retry the request URL; it should verify now. Then invite the app to each mapped channel with `/invite @hangar`. Thread replies only arrive from channels the app is in.
5. **Check the setup** with `hangar doctor`. It checks:
   - that both credentials are stored;
   - that the bot token works (`auth.test`);
   - that every mapped channel points at a repo in `HANGAR_GITHUB_REPOS`.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_SLACK_SIGNING_SECRET_CREDENTIAL` | | Vault credential holding the signing secret. The Slack routes return 404 until it is set. |
| `HANGAR_SLACK_BOT_TOKEN_CREDENTIAL` | | Vault credential holding the bot token |
| `HANGAR_SLACK_CHANNELS` | `{}` | `{"<channel id>": "owner/repo"}`: the channels that may start jobs, and each one's default repo. An exact git URL also works, as in `HANGAR_GITHUB_REPOS`. |
| `HANGAR_SLACK_USERS` | `[]` | Slack user IDs (`U…`, or `T…/U…` to pin the workspace) allowed to start and steer jobs. Empty means anyone in a mapped channel. |
| `HANGAR_SLACK_API_URL` | `https://slack.com/api` | |

## How it behaves

- **Requests are verified.** Each request must carry a valid `X-Slack-Signature`: `v0=` followed by the HMAC-SHA256 of `v0:<timestamp>:<raw body>`, keyed with the signing secret and compared in constant time. Requests whose `X-Slack-Request-Timestamp` is more than 5 minutes off are refused with 401.
- **Within 3 seconds.** Slack expects an answer within 3 seconds. Hangar acknowledges the command or event at once. It posts to Slack, reads the thread and submits the job in the background.
- **Exactly once.** Slack retries an event it thinks failed (`X-Slack-Retry-Num`), and a mention in a thread arrives both as `app_mention` and as `message`. Hangar acts once per `event_id` and once per message. The record of handled events survives a restart.
- **Only people.** Hangar ignores messages from bots (its own included), edits and deletions.
- **Who's asking.** The requester is `slack:<team>/<user>`, shown in the job's timeline, PR body and audit log. Slack jobs are fenced in like a `jobs` token: only repos in `HANGAR_GITHUB_REPOS`, only their environments, and no choice of provider.
- **Who can follow up.** Anyone allowed in a mapped channel can follow up in a job's thread, just as any collaborator can comment `/hangar` on a job's PR. A reply while the agent is still working gets "Can't continue yet"; reply again when the turn is done.
- **Needs input.** When an agent stops with a question (job state `needs_input`, spec 0011 §2), hangar posts the question in the thread. Your reply is the answer: it runs as an ordinary follow-up.
- **Safe output.** Text hangar didn't write itself, such as the agent's summary, failure output, or your instructions echoed back, is escaped. It can't ping `@channel` or turn into a link. Known secrets are redacted before anything is posted.
- **When posting fails.** If the app isn't in the channel or the token is wrong, a slash command answers privately, through Slack's `response_url`, with the reason, and no job starts. Failures to post updates are recorded on the job's timeline.

## Security notes

Treat a mapped channel like write access to its repo's `hangar/*` branches. Anyone who can post there can start an agent, unless `HANGAR_SLACK_USERS` is set. Messages in the thread are context for the agent, and the prompt marks them as data. Even so, they are untrusted input, like issue comments; see [jobs → Security notes](jobs.md#security-notes). Keep PR merges human-approved.
