# Who may continue a custom session branch?

A session can name its own branch (`branch` on `POST /api/sessions`). A new session continues that branch when it already exists. Outside `hangar/session/` the name is predictable. So another person's sandbox could create it first, plant commits, and have your agent build on them. This page says how hangar stops that. It is part of the [security model](README.md).

## What the gateway records

The [git gateway](git-gateway.md) reads every push before it goes upstream. When a push creates a branch, the gateway notes the person its sandbox belongs to. It does this only for a ref update from the zero id that upstream accepted.

A job's branch is recorded the same way: hangar pushes it with a delivery grant, through the gateway, so the job's person is the creator. A job delivered in direct mode records its person after the push, unless a record exists already. A sandbox with no person, such as a GitHub issue's job, is recorded as nobody's.

## What a new session decides

When a session names a branch outside `hangar/session/`, hangar looks up who created it on that repo:

| The branch was | The session |
| --- | --- |
| Created through the gateway by another person's sandbox | Is refused, with `session.branch_refused` in the audit log |
| Created by a sandbox with no person | Is refused the same way |
| Created by a sandbox of the same person | Starts, and continues the branch |
| Never seen created by hangar | Starts, and continues the branch, as before |

The last row keeps "continue my branch" working. It covers a branch a person pushed from their own machine, and one created before hangar kept records.

## The admin override

An admin who means to take such a branch passes `"take_branch": true`. The session starts, and the audit log gets `session.branch_override` with the creator's person and sandbox. A member who passes it is refused with 403, and the attempt is audited as `session.branch_refused`.

## From then on

The session holds its branch as soon as it is created. The gateway refuses any other sandbox that tries to create the branch, or one that would block it. A job's branch is held the same way from the moment the job is submitted.

## What this doesn't cover

- **Pushes outside the gateway.** A direct-mode sandbox holds the git token and pushes without a record. A person who pushes a branch from their laptop leaves none either. Both look like "never seen created", so a session continues them.
- **A record outlives a deleted branch.** If someone deletes the branch on GitHub, the old record still decides. An admin takes it with `take_branch`.
- **Two sessions on one branch.** A second session may still name a branch another session uses, as before. An admin's session can do that, since only admins name branches today. Both sessions' sandboxes may then push it.
