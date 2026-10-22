# The status key and the keyboard

What each status mark on the [Fleet](../console.md#fleet) means, the keys that move through it, and the one read the
page is built on.

## The status key

Every status is a mark, a word and, for the three that need someone, a color. Running work stays grey on purpose: a
screen with no color on it needs nobody.

| Status | Mark | When |
| --- | --- | --- |
| Running | grey dot | the sandbox is up and its work is going on |
| Queued | ring | the sandbox is pending or starting |
| Needs you (needs approval, needs input) | orange diamond | a job waits for approval to start, a running turn waits for a tool call to be allowed, or the agent asked a question |
| Failed | red triangle | the sandbox stopped on an error, or its job failed |
| Denied | red placard | policy denied a step in the running turn |
| Spun down | small dot | paused, stopping or stopped |

The brand yellow marks focus and the primary action, never a status.

## Keyboard

- The agents are one tab stop. **Arrow keys** move between rows and cards, across environments; **Home** and **End**
  go to the first and last.
- **Enter** (or **Space**) opens the focused agent in place; **Escape** closes it, and focus returns to its row.
- On the deck, **Previous** and **Next** move through the decisions; **Ctrl+Enter** (⌘+Enter) in a reason or an
  answer sends it.

Focus, scroll and a half-written answer or reason survive every update. A screen reader hears how many need you,
failed or were denied whenever that changes.

## The data

The page is one read, `GET /api/fleet`, kept current by its stream, `GET /api/fleet/stream`; both are described in
[the Fleet API](../api/fleet.md).
