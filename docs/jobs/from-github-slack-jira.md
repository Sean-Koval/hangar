# Jobs from GitHub, Slack and Jira

Starting a job from an issue, a PR comment, a Slack thread or a ticket. Part of [jobs](README.md); the front doors' own pages are [triggers.md](../triggers.md), [slack.md](../slack.md) and [intake.md](../intake.md).

## 4. Trigger from GitHub

```bash
hangar setup github                    # asks for the repo, its environment and a token
hangar setup github --repo acme/app=python --token-file ~/.hangar/gh-token --non-interactive
```

The wizard:

1. **Checks the token** before storing anything. It must be able to read each repo and write to it, and to manage its webhooks. The token is read from a hidden prompt, `--token-file`, `--token-stdin` or `--token-from-gh` (the `gh` CLI's login), and it is never echoed or printed.
   - **Which token:** a [fine-grained token](https://github.com/settings/personal-access-tokens/new) for the repos, with repository permissions *Issues* and *Pull requests*: Read and write, *Webhooks*: Read and write, and *Actions*: Read (auto-fix reads failing CI logs with it). *Metadata*: Read is added automatically. A classic token needs the `repo` scope. GitHub doesn't open the check-runs API to fine-grained tokens, so with one, auto-fix describes a failing check from its webhook (title, summary, text) and its Actions log, without the check's annotations; a classic token gets those too.
   - **What it's for:** the control plane uses it to comment and open PRs; it never goes into a sandbox.
2. **Stores** the token as `gh-control` and a generated webhook secret as `gh-webhook` in the vault. Running the wizard again keeps the secret. `--rotate-secret` makes a new one and stores it only once every repo's webhook has it: if GitHub refuses one (a token with *Webhooks*: Read only), the hooks already changed are set back and the old secret stays, so deliveries keep verifying.
3. **Saves** `HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL` and `HANGAR_GITHUB_REPOS` in hangar's database. They apply at once and survive redeploys, with no config map to edit. If the server's environment (on GKE, `hangar-site`) also sets one of them, the environment wins, and the wizard tells you.
4. **Registers the webhook** on each repo, or updates the existing one: `https://<hangar>/api/hooks/github`, JSON, the stored secret, and the **issues**, **issue_comment**, **pull_request_review_comment**, **pull_request_review**, **check_run**, **check_suite**, **status** and **pull_request** events (`pull_request_review` through `status` drive [auto-fix](agent-loop.md); with `pull_request` they also track [landing](../landing.md)). A hook that lacks any of them, such as one set up before auto-fix that sent only the first three, gets the missing events added; `hangar doctor` names them and `hangar doctor --fix` adds them.
5. **Tests it twice.** First with a signed delivery through hangar's own route, then with a ping from GitHub, whose result it reads back from the hook's recent deliveries.

GitHub can't deliver to `localhost`. On a laptop the wizard still stores everything and skips the webhook; pass `--webhook-url https://<tunnel>` to register one through a tunnel. `--no-webhook` skips it if you'd rather add it by hand. A generated secret is never shown, so bring your own: `openssl rand -hex 32 > ~/.hangar/gh-webhook-secret`, then pass `--webhook-secret-file ~/.hangar/gh-webhook-secret`, and on GitHub use the URL, content type `application/json`, that secret, and the seven events above. Updating an existing hook keeps its other events and its SSL verification setting.

To set the same things by hand, use `hangar cred add gh-control --harness git --kind git_token`, `hangar cred add gh-webhook --harness webhook --kind generic`, and `hangar config set HANGAR_GITHUB_REPOS '{"acme/app": "python"}'`, together with the two `*_CREDENTIAL` settings.

Then:

- Label an issue `hangar` (`HANGAR_GITHUB_TRIGGER_LABEL`), or comment `/hangar` on it, to start a job. The issue body and recent comments by trusted people (by default owners, members and collaborators) become context; other people's comments are left out. `/hangar harness=codex model=gpt-5.5 also add a test` picks the harness and model and adds instructions.
- hangar comments when it starts and when it finishes, with the summary, verification and PR link. The PR says `Closes #N`.
- Comment `/hangar <message>` on the PR to continue in the same sandbox and branch.

A `/hangar` comment or label from someone who isn't trusted (by default: not an owner, member or collaborator; or set `HANGAR_GITHUB_TRUSTED`) creates the job in `awaiting_approval`, and it runs only after a trusted `/hangar approve`. A job held by a daily spend cap waits the same way, and only an admin or a repo policy approver other than its own person can release it. See [triggers.md](../triggers.md#who-may-approve). Only repos in `HANGAR_GITHUB_REPOS` are accepted, and GitHub's redelivery of the same event does not start a second job.

Jobs can also start from other systems' webhooks and on a schedule: see [triggers.md](../triggers.md).

Jobs from every front door get the repo's defaults (`verify`, environment, harness, model) from its [policy](../repo-policy.md), then from the environment: `hangar repo set acme/app --verify "pytest -q"`. A `/hangar env=…` option still picks one of the repo's allowed environments.

## 4a. Trigger from Slack

`/hangar <instructions>` or `@hangar <instructions>` in a mapped channel starts a job on the channel's repo. hangar replies in a thread when the agent starts, needs input and finishes, and a reply in the thread is a follow-up. Setup (app manifest, credentials, channel map) is in [slack.md](../slack.md).

## 4b. Trigger from Jira or GitHub Projects

Planned work comes in through Jira: moving an issue to **Ready for agent** in a mapped project is the default door (`hangar setup jira`), and a configured label works too. Repo-local chores come in through a GitHub label (`hangar`) or a GitHub Projects column of the same name. hangar comments on the issue when it queues the job (saying when it starts if the sandbox pools are powered down), when the agent starts, asks its questions there (a comment answers), and moves the ticket's status as the job goes. Setup is in [intake.md](../intake.md).

Write the ticket from the [agent ticket template](../intake.md#agent-tickets): *Goal*, *Acceptance criteria*, *Verify command*, *Out of scope*, *Touches risk paths?*. hangar hands the criteria to the agent as untrusted `acceptance.md` and counts them (`intake.acceptance`); the ticket's verify command is a suggestion unless the repo policy's `verify_allowed` matches it. A ticket without criteria still starts, and the queued comment says to add them.

