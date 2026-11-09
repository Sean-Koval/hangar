# Intake: Jira

Work reaches hangar where the team already tracks it. The doors:

- **Jira "Ready for agent"**, the default for planned work: moving an issue to that status in a mapped project starts a job (`hangar setup jira` sets it up). A label works too, as an alternative.
- **A GitHub label**, for repo-local chores: the `hangar` label on an issue.
- **`@hangar` in Slack**, for unplanned work: kept as it is, and not extended beyond `@hangar adr` ([slack.md](slack.md)).

Write tickets from the [agent ticket template](#agent-tickets). hangar keeps the ticket in step as the job runs:

| Stage | Jira |
| --- | --- |
| Queued | a comment saying which job and environment, when it starts if the sandbox pools are powered down (01:00-10:00), and a hint when the ticket has no acceptance criteria |
| Agent started | a comment with a link to the job in hangar; the `started` transition |
| The agent asks a question | the question as a comment; the `needs_input` transition. The job's person's next comment answers it (on a team job, any linked teammate's), and the agent picks up where it left off |
| Delivered | the outcome with the PR, which is also added as a remote link; the `delivered` transition |
| Failed or cancelled | what went wrong; the `failed` transition |

GitHub issues ([jobs/README.md §4](jobs/from-github-slack-jira.md#4-trigger-from-github)) and Slack ([slack.md](slack.md)) follow the same rule: their jobs go to the linked person too.

## Who a job belongs to, and who is trusted

Every front door uses one rule ([team.md](team/who-is-trusted.md) has it in full). An account in Jira or GitHub is a hangar person only when an admin has linked it to one (`hangar person link dana jira <account id>`, `hangar person link dana github <login>`, which is stored as the account's numeric GitHub id). hangar asks `people.resolve_external` for every sender, by an id that doesn't change hands: the Atlassian account id (Data Center: the user key) and the GitHub user id (`sender.id`), never a GitHub login.

- **A linked sender acts as their person, so they are trusted.** The job is theirs: it counts against their caps and budget, runs with their own credentials where they have them, and they see it in their own job list.
- **Only the job's person continues it.** A person's job runs with their own login, so in Jira only that person's comment answers its question or continues it with `/hangar <message>`. Anyone else's, a linked teammate's included, is refused with a comment saying whose job it is (an admin uses `hangar job follow-up`). The same holds on GitHub issues, PRs and Slack threads.
- **Anyone else is untrusted.** Their job waits in `awaiting_approval` until a trusted teammate approves it: `/hangar approve` (or `/hangar reject`) in a comment on the issue, or `hangar job approve`. Its text reaches the agent fenced as untrusted, and it runs on the team's credentials, never on someone's own login. In Jira, a sender who isn't linked is never trusted (Jira payloads carry no equivalent of GitHub's collaborator association, and anyone who can move an issue would otherwise start work); only a linked teammate's comment approves a job, answers a team job's question or continues one. Other people's comments are ignored.
- **Ticket text is data.** A Jira issue's summary and description always reach the agent fenced as untrusted, whoever reported it, since anyone with permission to edit the issue can rewrite them; the job's instructions only point at that file. A GitHub issue's body is fenced unless a trusted account wrote it, and its earlier comments always are. Only a trusted sender's own `/hangar <instructions>` are instructions. The issue's title is still the job's title, which the prompt shows.
- **Who acted is what Jira or GitHub says.** A signed webhook's `user` (Jira) or `sender` (GitHub) is used as sent. An Automation request isn't signed, so its body's `user` is ignored: hangar reads who moved the issue into the trigger status (or gave it the trigger label) from the issue's own history, or its creator when it was created that way.

The repo must be in `HANGAR_GITHUB_REPOS`, as for every front door, and the repo's [policy](repo-policy.md) sets the job's verify, environment, harness and model.

### Safety

The fence around ticket text is in the prompt; the boundaries are outside it. Every Jira job needs the [git gateway](security/git-gateway.md): without `HANGAR_GATEWAY_URL` (or for a repo whose host the gateway doesn't serve) the job is refused and the reason is posted on the issue. Its sandboxes hold only a fetch-only grant (hangar pushes), and they get at least `allowlist` egress, even in an `open` environment: the environment's presets and hosts, the repo policy's, and the harness's API. Anything else is an egress request a person answers. This is the [intake posture](security/front-doors.md#intake-posture), and no setting relaxes it.

## GitHub Projects

The GitHub Projects door was removed after v0.2.0 (KAN-217): a board's column no longer starts a job. Use a Jira status or a GitHub label instead. [Removed surfaces](upgrading.md#removed-surfaces) lists the setting to unset.

## Agent tickets

As the work moves to writing tickets an agent can finish, the ticket's shape is the tool. Two templates ship with hangar:

- [`docs/templates/jira-agent-ticket.md`](https://github.com/Sean-Koval/hangar/blob/main/docs/templates/jira-agent-ticket.md): a Jira description template (paste it, or make it the project's description template);
- [`docs/templates/github-issue-agent-task.yml`](https://github.com/Sean-Koval/hangar/blob/main/docs/templates/github-issue-agent-task.yml): a GitHub issue form (copy it to `.github/ISSUE_TEMPLATE/agent-task.yml`; it labels the issue `hangar`, so opening one starts a job).

Both have *Goal*, *Acceptance criteria* (a checklist), *Verify command*, *Out of scope* and *Touches risk paths? (y/n)*. hangar reads two of them by heading, for Jira and GitHub issues alike: Markdown headings, Jira wiki (`h2.`), Jira Cloud's editor (headings and action items), a GitHub issue form's output, or a bold line. Only the first 50 KB of a description is read.

- **Acceptance criteria** go to the agent as their own context file, `acceptance.md`, **fenced as untrusted**: anyone with Edit Issues can rewrite them, so they describe what done looks like and never override the task. Their count is the job's `intake.acceptance` label, for the weekly metrics.
- **The verify command is a suggestion.** The agent is told it may run it in its turn. It becomes the job's `verify` only when it matches a glob in the repo policy's [`verify_allowed`](repo-policy.md) (`make test*`, `pytest tests/*`), which repo admins own; otherwise hangar's check stays the policy's or the environment's (`verify_source`). Even matched, it runs as the agent's user inside the sandbox.
- **No criteria, still a job.** A ticket without them starts anyway, and the queued comment adds: "No acceptance criteria found; add them for better results (template: …)", once per job. It is counted as `intake.acceptance = 0`.

## How deliveries are handled

- **Authentic.** Jira's signature (or the Automation token) is checked in constant time before anything is parsed. Jira's payload `timestamp` sits inside the signed body and must be recent (`max_age_minutes`). The Automation token signs nothing, so an Automation request also needs a recent trigger transition in the issue's own history, and is capped per issue ([the Jira fallback](intake/jira.md#fallback-an-automation-rule)).
- **Within 1 MB.** A Jira delivery larger than 1 MB (in practice, a very long description) isn't taken: hangar answers `413`. One that is authentic and fresh is audited (`jira.too_large`, with its size and delivery id), and if its issue is in the trigger status or has the trigger label, hangar comments on the issue once a day that the description is too large to start a job from. Jira doesn't retry a `413`, so shorten the description (attach long logs as files) and move the issue again.
- **Once.** Each delivery is claimed (by the digest of its signed body) before it's handled and marked done after. A retry of one that was handled is acknowledged and dropped, and a replay can't pass as new by changing an unsigned header. One that failed half way, or hit a rate limit, runs again on the sender's retry. Jira treats only `200` as delivered, so hangar answers `200` for everything handled or ignored, `429` when rate limited and `503` when misconfigured, which Jira retries.
- **One job per ticket.** A ticket with an unfinished job doesn't start a second.
- **Tainted from turn 1.** Ticket text is untrusted, so every intake job is [tainted](security/taint.md#taint-gates): its PR opens as a draft labelled `hangar:tainted` and needs a reviewer other than the job's own person to land. Give intake repos `approvals: deny-risky`, so pushes and fetches inside the turn are refused too.
- **Credentials stay in the control plane.** The Jira token, webhook secret and Automation token are control-plane credentials: they can't be attached to a sandbox or mapped into one with `secrets:`.
- **Write-back never blocks the job.** If Jira or GitHub can't be reached, or the workflow has no transition to a status, that goes on the job's timeline and the job carries on.

## Adding a tracker

`src/hangar/intake.py` holds what every front door shares: `person_for` (who the sender is), `once` (commit after handle), `open_job` (one job per ticket), `submit` (approval for untrusted senders and refusals as answers) and `TRACKERS`, the write-back registry. A tracker is a [plugin](plugins.md): it sets the job label `intake.tracker` to its ticket reference, adds its door with `host.add_door` and its write-back with `host.add_tracker(<scheme>, update)`. hangar calls `update(svc, job, ticket, stage)` once per stage (`started`, `needs_input`, `delivered`, `failed`) whenever the job reports, whichever thread the conversation is in. `jira.py`'s `PLUGIN` is the example.

**Shadows never write back** (#183). A repo policy's `shadow` samples real intake jobs for a shadow run of a candidate
harness ([repo-policy.md](repo-policy/budgets.md#shadows)). A shadow is a `source: shadow` trial without `intake.tracker`,
`source_ref` or `reply_to`, so no tracker, Slack thread or GitHub issue hears about it: only the real job writes back.

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="jira-cloud"></span>Jira Cloud | [Jira intake](intake/jira.md#jira-cloud) |
| <span id="1-an-account-and-a-token-for-hangar"></span>1. An account and a token for hangar | [Jira intake](intake/jira.md#1-an-account-and-a-token-for-hangar) |
| <span id="2-the-webhook"></span>2. The webhook | [Jira intake](intake/jira.md#2-the-webhook) |
| <span id="3-the-configuration"></span>3. The configuration | [Jira intake](intake/jira.md#3-the-configuration) |
| <span id="fallback-an-automation-rule"></span>Fallback: an Automation rule | [Jira intake](intake/jira.md#fallback-an-automation-rule) |
| <span id="talking-to-the-job-from-the-issue"></span>Talking to the job from the issue | [Jira intake](intake/jira.md#talking-to-the-job-from-the-issue) |
| <span id="branch-and-pr-names-carry-the-key"></span>Branch and PR names carry the key | [Jira intake](intake/jira.md#branch-and-pr-names-carry-the-key) |
| <span id="jira-data-center"></span>Jira Data Center | [Jira intake](intake/jira.md#jira-data-center) |
