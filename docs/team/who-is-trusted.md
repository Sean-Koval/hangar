# Who is trusted

One rule for every front door: GitHub issues and PRs, Slack, Jira. It says whose work starts without
approval, who may continue a job, and which text is an instruction. Linking accounts is on
[linked accounts](linked-accounts.md).

## The rule

1. **A linked account acts as its person.** It is trusted: its work starts without approval. It can answer questions
   and approve team jobs held at its front door (an outsider's).
    - That gives it nothing its person can't already do over MCP, within their caps, budget and visibility.
    - Releasing a person's held job (a spent daily cap) takes an admin or one of the repo policy's `approvers`. Nobody
      approves their own job, at any door ([who may approve](../triggers.md#who-may-approve)).
2. **Only a job's person continues it from a front door.** A person's job runs with their own login. So a follow-up
   (`/hangar …` on its PR or issue, a Slack thread reply, a Jira comment) or an answer from anyone else is refused.
   The refusal says whose job it is.
    - An admin continues anyone's job with `hangar job follow-up`. A team job (no person) can be continued by anyone
      the front door trusts.
3. **An account linked to nobody acts for the team**, and is trusted only by its source's own rule:
    - GitHub issues and PRs: author association owner, member or collaborator, or `HANGAR_GITHUB_TRUSTED` when set
      ([who can start work from GitHub](../triggers.md#1-who-can-start-work-from-github));
    - Slack: a member of the workspace in a mapped channel, within `HANGAR_SLACK_USERS` when that's set. Another
      organization's user is trusted only when listed there;
    - Jira: never. Anyone who can move an issue could otherwise start work, so their job waits for a linked
      teammate's `/hangar approve`.

    An untrusted sender's job waits for approval, its text is fenced as untrusted, and it can't continue a job or
    answer its question.

4. **Ticket text is data.** Issue bodies and comments not written by a trusted account reach the agent fenced as
   untrusted. So does a Jira description, always, since anyone who can edit the issue can change it.
    - Only a trusted sender's own `/hangar …` text is an instruction. An issue's title is still the job's title, which
      the prompt shows.
5. **Who acted is what the source signs.** That is the sender id in a signed GitHub delivery, and the user and their
   workspace in a signed Slack event. In a signed Jira webhook it is the user.
    - A Jira Automation request isn't signed, and its body is written by whoever edits the rule. So hangar reads who
      moved the issue from the issue's own history instead
      ([an Automation rule](../intake.md#fallback-an-automation-rule)).
6. **A review starts an auto-fix round only from someone the job's person or the repo trusts.** A round runs with the
   job's person's login, like a follow-up.
    - On a person's job it takes their linked account, an admin's linked account, or a login in the repo policy's
      `approvers`. A code owner of every path the PR touches also counts: that is what lets an approval land it
      ([landing](../landing.md)).
    - On a team job, also anyone rules 1 and 3 trust. Never a bot or hangar's own App.
    - What needs GitHub (CODEOWNERS, the PR's files, team membership) is checked after the webhook is answered,
      since GitHub waits only 10 seconds. A refusal is audited as `github.refused` (`untrusted review`).
    - Unlike a `/hangar` follow-up, the reviewer's text isn't an instruction. Every review comment, the job's
      person's included, reaches the agent fenced as untrusted data and labelled with its author.
    - The instruction is hangar's own: "address each comment that asks for a change to this task's code".

## For intake code

Intake that adds a source calls `people.resolve_external(store, source, external_id)`. It returns the linked `Person`
or `None`, and the intake passes `Caller(..., person=p.id)` (`None` for the team), trusted when it isn't `None`.

The ids are a GitHub user's numeric id (`sender.id`), a Slack `TEAM/USER`, a Jira account id or Data Center user
key, or an email. A `jira` id with an `@` is looked up as an email. A
follow-up passes `front_door=True, sender_person=…` to `JobService.follow_up`.
