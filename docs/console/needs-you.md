# Needs you

Everything waiting on a person, from the same list `hangar inbox`, Slack and MCP clients get (`GET /api/inbox`). It
holds your own work and, for an admin, also the work nobody in particular owns, such as jobs from GitHub issues and
Slack.

The page reads it with the Fleet: `GET /api/fleet` carries the inbox, and its stream keeps it current. The rest of the
console is on [the web console](../console.md).

<figure class="af-figure af-figure--lead">
<a href="../assets/front/ui-needs.png"><img src="../assets/front/ui-needs.png#only-dark" alt="Needs you: the held push on top of the deck with its command, changes and expiry, Deny, Allow for this turn and Approve push; the next decision peeking behind it"></a>
<a href="../assets/front/ui-needs-light.png"><img src="../assets/front/ui-needs-light.png#only-light" alt="Needs you: the held push on top of the deck with its command, changes and expiry, Deny, Allow for this turn and Approve push; the next decision peeking behind it"></a>
<figcaption>A held git push to main on top of the deck: its command, its changes and when it expires, with Deny, Allow for this turn and Approve push.</figcaption>
</figure>

## The deck

It is a **deck**: one decision at a time, the soonest to expire on top, and the next two peeking behind it. It shows
"1 of 7", previous and next, and an up-next list to jump to any of them. Decisions (a person is needed now) come
before what stopped.

Deciding happens on the card. A verdict line says what happened ("Approved by Sean; codex is running git push origin
main"), and the agent's row on the Fleet flips at once. After a few seconds the card slides away and the next one
rises. If someone else decided first, or the wait expired, the card says so and goes.

## The kinds

- **A held tool call**: a running turn under `approvals: ask` wants to run a command. The card shows the exact
  command, which agent asked and in which sandbox, for whom and how long ago. It shows the policy that held it, and
  whether the turn read untrusted content.
    - The card also says when it expires and what happens then: it is denied and the agent carries on.
    - The choices are **Approve**, **Allow for this turn** or **Deny**. **Allow for this turn** also allows calls like
      this one until the turn ends. Nothing is written to the agent's settings, so the next turn asks again.
- **A job waiting to start**: from someone the team doesn't trust, for example, or held by a daily spend cap; the card
  says why. **Approve** or **Deny**. Approving follows the same rule as every other approval path, so you can't
  approve your own work ([security](../security/README.md)).
- **A request** an agent made for access (an environment request): **Approve** or **Deny**.
- **A question** the agent asked: type an answer (or pick one of the options it offered) and **Answer**. The answer
  goes back as the agent's next message.
- **A failed job** (in the last week). **Retry** continues the same session, so the agent keeps its context and branch
  and re-runs the check that failed (`POST /api/jobs/{id}/retry`). **Start over** is a new job with the same task from
  the base branch (`?fresh=1`). **Acknowledge** puts it away.
- **A step policy denied** in a turn still running (`denied`): the agent was told and carries on without it. There is
  nothing to decide, so **Acknowledge**.

## Deciding

**Deny** needs a reason: the field opens in the card, and an empty one is refused. The reason goes back to the agent
or the requester. **Open job** or **Open session** shows the work first.

**Acknowledge** (`POST /api/inbox/{id}/ack`) puts a failed or denied item away for a week; a decision never goes that
way. Below the deck are the PRs waiting for your review on GitHub, with **Open job** and **Open PR**. With nothing
waiting, the deck says so and counts what is running. The **Needs you** count turns orange while a decision waits.

The same deck sits at the top of the [Fleet](../console.md#fleet). Each decision also opens with its agent there and
on its job's or session's page, as the same card.
