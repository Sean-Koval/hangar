# The live intake check

Does a real Jira ticket, moved by a real person, become exactly one job and one draft PR on the deployed hangar?
Run this check once after a release reaches production, and again after a change to the Jira door.

The acceptance suite's `intake` check proves the door with a synthetic delivery. This check proves the whole path:
Jira's own webhook, the mover's linked account, a sandbox, the verify command and the draft PR. It runs one real job,
so it spends what that job spends.

## Before you start

| Check | How |
| --- | --- |
| The newest deploy is live | `hangar doctor` shows the `production lag` line at 0 commits behind, or names the commit you mean to test |
| Jira intake is set up | `hangar doctor` has no failing `jira` lines; `docs/intake/jira.md` covers the setup |
| You are linked | `hangar person ls` shows a `jira:` link on your line |
| A ticket to send | A small, real ticket in a mapped project, written to the [agent ticket template](https://github.com/Sean-Koval/hangar/blob/main/docs/templates/jira-agent-ticket.md), with a verify command. It must have no open job |
| The pools are up | `hangar power status` says `up`. Outside power hours the job only queues |
| A GitHub token (optional) | `HANGAR_ACCEPT_GITHUB_TOKEN` with read access to the ticket's repo, so the PR checks run. Without it they read "not checked" |
| A budget | `hangar status` shows a job budget or a daily cap, so one job can't overspend |

Then run the preflight. It spends nothing and moves nothing:

```sh
python scripts/acceptance/intake_live.py preflight --ticket KAN-123
```

It prints `READY`, or one `NOT READY` line per problem. Fix each one and run it again.

## The run

```sh
python scripts/acceptance/intake_live.py run --ticket KAN-123
```

The script never touches Jira. It asks you to act, waits for Enter, then checks:

1. **Move the ticket** to the trigger status, as yourself. The script waits up to 10 minutes for the job. It checks that
   exactly one job appeared, that it is yours, and that its title starts with the key.
2. **Move it out and back in** while the job is still running. After a minute the ticket must still have one job. If
   the job finished before this step, the check reads "not checked". Rerun on a slower ticket.
3. **Wait.** The job runs, verifies and opens its PR, for up to 90 minutes. If the job asks a question, answer it on
   the ticket. The script keeps waiting.

At the end it checks that the job succeeded and opened a PR. With a token it also checks three things. The PR must be
a draft. Its title must start with the key. Its body must carry a `Jira: KEY` line, which the GitHub for Jira app
reads.

## Recording the result

The script writes `intake-live-report.json` and `intake-live-summary.md`. Paste the summary as a comment on KAN-27 and
on [hangar#151](https://github.com/Sean-Koval/hangar/issues/151). It names the job id, the PR link, the timings and
each check.

File each defect as a hangar issue, then list it under "Defects" in the comment. A failed check is a defect, unless the
setup caused it. In that case fix the setup and run the check again.

## What a second move does later

A ticket with an open job never starts a second one. Once its job has finished, moving the ticket back to the trigger
status starts a new job. That is a deliberate re-request, not a duplicate, so this check doesn't test it.
