"""Events out: `hangar.event/v1`, and the notifiers that carry them to a room, a bot or a pager.

hangar derives events from its own records (events_out.py): a job written to the store is compared with what was
already said about it, so each event goes once per notifier, after a restart too. An event's `id` is stable for
the fact it reports ("job_abc:pr.opened:12"), so a receiver can deduplicate on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from hangar.models import Person

SCHEMA = "hangar.event/v1"
# What hangar emits today. Other producers (change sets: "changeset.landing", "changeset.done", "changeset.broken")
# add types under their own prefix; a notifier filters with glob patterns ("pr.*", "changeset.*").
TYPES = ("job.queued", "job.awaiting_approval", "job.started", "job.needs_input", "turn.tool_approval",
         "job.delivered", "pr.opened", "pr.approved", "pr.checks", "pr.landed", "pr.closed", "job.failed",
         "job.finished", "digest")
# A job label a relay sets from `on_behalf_of.reply_to` ("symphony:<streamId>:<messageId>"): where its events
# belong, carried as Event.reply_to. Without it, the latest turn's `reply_to`, then the job's `source_ref`.
REPLY_TO_LABEL = "relay.reply_to"


class Action(BaseModel):
    """A call that answers the event, for renderers that offer buttons: {"tool": "answer_approval", "args": {...}}.
    The tool is an MCP tool; the caller still needs the scopes and the right to make it."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None


class Event(BaseModel):
    schema_: str = Field(SCHEMA, alias="schema")
    id: str  # stable per fact: the receiver's deduplication key
    type: str  # one of TYPES, or a producer's own ("changeset.done")
    at: float  # when the fact happened, as far as hangar knows
    job: str | None = None
    session: str | None = None
    person: str | None = None  # the hangar person the work is for
    repo: str | None = None
    reply_to: str | None = None
    links: dict[str, str] = Field(default_factory=dict)  # "job" (the dashboard), "pr", "ticket"
    summary: str = ""  # one line, redacted
    actions: list[Action] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)  # type-specific: {"pr_number": 12}, {"checks": "passed"}

    model_config = {"populate_by_name": True}

    def body(self) -> bytes:
        """The webhook body: the event as JSON, with `schema` spelled as such."""
        return self.model_dump_json(by_alias=True).encode()


@runtime_checkable
class Notifier(Protocol):
    name: str

    def wants(self, person: Person | None, event: Event) -> bool:
        """Whether to send this event (its type, the person's links and preferences)."""
        ...

    async def send(self, event: Event, person: Person | None) -> bool:
        """Deliver it. False (or raising): tried again on a later pass."""
        ...
