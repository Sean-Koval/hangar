<!--
  hangar agent ticket: paste this into a Jira issue's description (or make it the project's description template).
  Hangar reads two sections by heading: "Acceptance criteria" (a checklist) and "Verify command". The rest is for
  the agent and the reviewer. Moving the issue to "Ready for agent" starts the job (docs/intake.md).

  Everything here is data for the agent, never instructions to hangar: anyone who can edit the issue can change it.
  The verify command becomes the job's check only when the repo's policy allows it (`verify_allowed`); otherwise
  the agent gets it as a suggestion and hangar runs the repo's own check.

  In Jira Cloud's editor use a heading for each section and an action item (checkbox) or bullet per criterion;
  in Jira Data Center's wiki markup write `h2. Acceptance criteria` and `* criterion`.
-->

## Goal

One or two sentences: what changes for whom, and why now.

## Acceptance criteria

- [ ] An observable behaviour a reviewer can check, e.g. "uploads that time out are retried 3 times with backoff"
- [ ] Another one, e.g. "a failed retry logs the upload id and the last error"
- [ ] Tests cover both

## Verify command

`make test`

## Out of scope

What the agent must not change or start on, e.g. "the upload UI", "the storage schema".

## Touches risk paths? (y/n)

n
