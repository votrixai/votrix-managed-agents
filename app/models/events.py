"""Session event shapes and the event type names.

Every event is a flat object whose `type` says how to read the rest of it —
there is no `payload` envelope. A client hands the whole thing to a
discriminated union and hears about a missing field at the door, rather than
three seconds into a turn.

    what a client sends
      user.message              content[]
      user.interrupt            —
      user.tool_confirmation    tool_use_id  result  deny_message?
      user.custom_tool_result   custom_tool_use_id  content[]  is_error?

    what the agent produces
      agent.message             content[]
      agent.thinking            content[]
      agent.tool_use            tool_use_id  name  input  evaluated_permission
      agent.custom_tool_use     tool_use_id  name  input
      agent.tool_result         tool_use_id  content[]  is_error

    session lifecycle
      session.status_running    —
      session.status_idle       stop_reason
      session.status_terminated —
      session.error             error

Read back, every one of them also carries `id`, `seq` and `processed_at`.

Two deliberate departures from CMA, both because the engine underneath is
LangGraph rather than Anthropic's runtime:

* `agent.thinking` carries the model's reasoning text. CMA sends the event
  empty as a progress ping; we already have the text, and dropping it would
  make the stream strictly less useful than the transcript beside it.
* `requires_action` must be answered in full. CMA lets a client resolve some
  of the pending calls and re-reports the rest; LangGraph's human-in-the-loop
  middleware counts the decisions it gets back and raises unless the count
  matches exactly, so a partial answer would fail mid-turn instead of at the
  request.
* `tool_use_id` is the engine's own id for the call, not the id of the
  `agent.tool_use` event announcing it — in CMA those are the same string. The
  engine hands us that id on the call and again on its result, so passing it
  straight through is the whole of the correlation; inventing a second id would
  mean carrying a translation table across a pause, and across processes, to
  arrive back where we started.
"""

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import Field, model_validator

from app.models.common import ApiModel, ListResponse

# --- the vocabulary ----------------------------------------------------------
#
# Namespaces follow `{domain}.{action}`:
#
#     user.*     client -> agent
#     system.*   client -> agent, appended context (does not start a run)
#     agent.*    agent -> client
#     session.*  session lifecycle

USER_MESSAGE = "user.message"
USER_INTERRUPT = "user.interrupt"
USER_TOOL_CONFIRMATION = "user.tool_confirmation"
USER_CUSTOM_TOOL_RESULT = "user.custom_tool_result"
# In CMA this is how a self-hosted environment returns agent-toolset results.
# Every tool of ours runs in our own sandbox, so there is nothing for a client
# to return. The name is kept so the omission reads as a decision rather than
# an oversight; sending one is refused, because it has no meaning here.
USER_TOOL_RESULT = "user.tool_result"

SYSTEM_MESSAGE = "system.message"

AGENT_MESSAGE = "agent.message"
AGENT_THINKING = "agent.thinking"
AGENT_TOOL_USE = "agent.tool_use"
AGENT_TOOL_RESULT = "agent.tool_result"
AGENT_CUSTOM_TOOL_USE = "agent.custom_tool_use"

# A preview of a message still being written, so a client has something to show
# during the ten to thirty seconds one call takes. Deltas are not the log: the
# `agent.message` that follows carries the whole text, and a client replaces
# what it previewed with that rather than appending to it.
#
# These are the one thing here that is not an event. They are never written to
# `session_events`, have no `seq`, and cannot be read back or resumed — they
# travel on their own `NOTIFY` channel and are delivered only to streams open at
# the moment they are published. A stream that opens mid-call has missed the
# ones already sent and gets the rest; one that opens after the call has ended
# sees none at all, and neither client is missing anything, because the
# `agent.message` carries the whole text either way.
#
# That is deliberate rather than a limitation to fix later. Giving a preview a
# `seq` makes it a row: an insert, a commit, a bump of the session's counter,
# and a delete once the turn is over — the cost of a permanent record, paid four
# times a second for something discarded seconds later. What a client actually
# needs from a preview is that it arrives while the model is still talking, and
# nothing about that requires it to survive.
AGENT_MESSAGE_DELTA = "agent.message_delta"
AGENT_THINKING_DELTA = "agent.thinking_delta"

DELTA_EVENTS = (AGENT_MESSAGE_DELTA, AGENT_THINKING_DELTA)

SESSION_STATUS_RUNNING = "session.status_running"
SESSION_STATUS_IDLE = "session.status_idle"
SESSION_STATUS_TERMINATED = "session.status_terminated"
SESSION_ERROR = "session.error"

# Answers to something a previous turn stopped to ask. Their presence is what
# tells the engine this turn resumes a paused graph rather than starting one.
ACTION_RESULT_EVENTS = [
    USER_TOOL_CONFIRMATION,
    USER_CUSTOM_TOOL_RESULT,
    USER_TOOL_RESULT,
]


def is_action_result(event_type: str) -> bool:
    return event_type in ACTION_RESULT_EVENTS


# --- content -----------------------------------------------------------------


class TextBlock(ApiModel):
    """Plain text in a user or agent message."""

    type: Literal["text"] = "text"
    text: str


class ImageUrlBlock(ApiModel):
    """LangChain's standard image block for a publicly reachable image."""

    type: Literal["image"] = "image"
    url: str = Field(min_length=1)


class ImageBase64Block(ApiModel):
    """LangChain's standard image block for inline image bytes."""

    type: Literal["image"] = "image"
    base64: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)


UserContentBlock = Union[TextBlock, ImageUrlBlock, ImageBase64Block]


class RecordedEvent(ApiModel):
    """What an event gains once it is in the log.

    `seq` is ours rather than CMA's: it is what a dropped stream reconnects
    with, and it has to be an ordering to do that job — ids are opaque.
    """

    id: str
    seq: int
    processed_at: datetime


# --- user.* ------------------------------------------------------------------
#
# The `*Input` shapes are what a client may send. They carry no `id`, `seq` or
# `processed_at`: those are assigned on the way in, and a client supplying its
# own would be claiming a position in a log it does not own.


class UserMessageInput(ApiModel):
    type: Literal["user.message"]
    content: list[UserContentBlock] = Field(min_length=1)


class UserMessageEvent(RecordedEvent):
    type: Literal["user.message"] = USER_MESSAGE
    content: list[UserContentBlock]


class UserInterruptInput(ApiModel):
    type: Literal["user.interrupt"]


class UserInterruptEvent(RecordedEvent):
    type: Literal["user.interrupt"] = USER_INTERRUPT


class UserToolConfirmationInput(ApiModel):
    type: Literal["user.tool_confirmation"]
    # The call being answered — one of the ids the last `session.status_idle`
    # listed under `stop_reason.tool_use_ids`, sent back unchanged.
    tool_use_id: str = Field(min_length=1)
    result: Literal["allow", "deny"]
    # Goes back to the model as the reason, so it can try something else
    # rather than guess why the call vanished.
    deny_message: str | None = None


class UserToolConfirmationEvent(RecordedEvent):
    type: Literal["user.tool_confirmation"] = USER_TOOL_CONFIRMATION
    tool_use_id: str
    result: Literal["allow", "deny"]
    deny_message: str | None = None


class UserCustomToolResultInput(ApiModel):
    """A custom tool has no implementation on our side at all, so this event is
    the only way one ever completes."""

    type: Literal["user.custom_tool_result"]
    custom_tool_use_id: str = Field(min_length=1)
    content: list[TextBlock] = Field(min_length=1)
    is_error: bool = False


class UserCustomToolResultEvent(RecordedEvent):
    type: Literal["user.custom_tool_result"] = USER_CUSTOM_TOOL_RESULT
    custom_tool_use_id: str
    content: list[TextBlock]
    is_error: bool = False


# --- agent.* -----------------------------------------------------------------


class AgentMessageEvent(RecordedEvent):
    type: Literal["agent.message"] = AGENT_MESSAGE
    content: list[TextBlock]


class AgentThinkingEvent(RecordedEvent):
    """A model that returns no reasoning produces no event, rather than an
    empty one."""

    type: Literal["agent.thinking"] = AGENT_THINKING
    content: list[TextBlock]


class DeltaFrame(ApiModel):
    """Something that appeared on the stream without being recorded.

    Not a `RecordedEvent`, and the absent fields are the whole distinction —
    no `id`, no `seq`, no `processed_at`, because none of those mean anything
    for something that was never stored. A reader tells the two apart by type
    rather than by inspecting fields, which is what this base class is for.

    Incremental, not cumulative: a client appends these to build a preview, and
    replaces the lot with the `agent.message` when it lands. The batching window
    means one of these covers a few hundred milliseconds of output, not one
    token.
    """

    text: str


class AgentMessageDeltaFrame(DeltaFrame):
    type: Literal["agent.message_delta"] = AGENT_MESSAGE_DELTA


class AgentThinkingDeltaFrame(DeltaFrame):
    """The same, for reasoning. Most of what a thinking model emits is this —
    without it a client watching a turn sees nothing at all for most of it."""

    type: Literal["agent.thinking_delta"] = AGENT_THINKING_DELTA


_DELTA_FRAMES: dict[str, type[DeltaFrame]] = {
    AGENT_MESSAGE_DELTA: AgentMessageDeltaFrame,
    AGENT_THINKING_DELTA: AgentThinkingDeltaFrame,
}


def to_delta_frame(type: str, text: str) -> DeltaFrame | None:
    """Build a frame from what came off the channel, or nothing.

    Returns `None` for a type this build does not know rather than raising. The
    payload crossed a process boundary from a publisher that may be running
    different code — during a rollout it certainly is — and the correct response
    to a preview we cannot name is to skip it. There is no message to be lost:
    the `agent.message` it previewed is coming through the log regardless.
    """
    frame = _DELTA_FRAMES.get(type)
    return None if frame is None else frame(text=text)


class AgentToolUseEvent(RecordedEvent):
    """`evaluated_permission` is the agent's permission policy already applied
    to this call, so a client can tell at a glance whether the result is coming
    by itself or waiting on an answer. `ask` means this id will appear in the
    next `session.status_idle`."""

    type: Literal["agent.tool_use"] = AGENT_TOOL_USE
    tool_use_id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    evaluated_permission: Literal["allow", "ask"] = "allow"


class AgentCustomToolUseEvent(RecordedEvent):
    """No `evaluated_permission`: a custom tool is not ours to run, so it always
    waits for the client. There is no policy to evaluate."""

    type: Literal["agent.custom_tool_use"] = AGENT_CUSTOM_TOOL_USE
    tool_use_id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class AgentToolResultEvent(RecordedEvent):
    type: Literal["agent.tool_result"] = AGENT_TOOL_RESULT
    tool_use_id: str
    content: list[TextBlock] = Field(default_factory=list)
    is_error: bool = False


# --- session.* ---------------------------------------------------------------


class EndTurn(ApiModel):
    """Nothing is pending. The next message starts a fresh turn."""

    type: Literal["end_turn"] = "end_turn"


class RequiresAction(ApiModel):
    """Waiting on the client, and on all of these at once — see the module
    docstring for why a partial answer is refused.

    The order is the order the engine wants its answers in. A client may reply
    in any order, naming each call, and they are put back in this one.
    """

    type: Literal["requires_action"] = "requires_action"
    tool_use_ids: list[str]


class Interrupted(ApiModel):
    """The user stopped the turn.

    Carries nothing: the client knows what it sent, and a list here beside
    `requires_action`'s — meaning something else entirely — is a mistake
    waiting to be made.
    """

    type: Literal["interrupted"] = "interrupted"


class Failed(ApiModel):
    """The turn ended because something went wrong.

    Nothing about the failure is repeated here — the `session.error` event
    immediately before this one carries it. This exists so that a turn which
    failed still *ends*: a client waiting for `session.status_idle` is waiting
    for the one signal that means "your go", and a turn that stopped without
    ever sending it leaves that client waiting for good.
    """

    type: Literal["error"] = "error"


StopReason = Annotated[
    Union[EndTurn, RequiresAction, Interrupted, Failed],
    Field(discriminator="type"),
]


class SessionStatusRunningEvent(RecordedEvent):
    type: Literal["session.status_running"] = SESSION_STATUS_RUNNING


class SessionStatusIdleEvent(RecordedEvent):
    type: Literal["session.status_idle"] = SESSION_STATUS_IDLE
    stop_reason: StopReason


class SessionStatusTerminatedEvent(RecordedEvent):
    type: Literal["session.status_terminated"] = SESSION_STATUS_TERMINATED


class SessionErrorEvent(RecordedEvent):
    type: Literal["session.error"] = SESSION_ERROR
    error: dict[str, Any] = Field(default_factory=dict)


# --- the two unions, and what wraps them -------------------------------------

EventInput = Annotated[
    Union[
        UserMessageInput,
        UserInterruptInput,
        UserToolConfirmationInput,
        UserCustomToolResultInput,
    ],
    Field(discriminator="type"),
]

EventResponse = Annotated[
    Union[
        UserMessageEvent,
        UserInterruptEvent,
        UserToolConfirmationEvent,
        UserCustomToolResultEvent,
        AgentMessageEvent,
        AgentThinkingEvent,
        AgentToolUseEvent,
        AgentCustomToolUseEvent,
        AgentToolResultEvent,
        SessionStatusRunningEvent,
        SessionStatusIdleEvent,
            SessionStatusTerminatedEvent,
        SessionErrorEvent,
    ],
    Field(discriminator="type"),
]

_ANSWERS = (USER_TOOL_CONFIRMATION, USER_CUSTOM_TOOL_RESULT)


class SendEventsRequest(ApiModel):
    events: list[EventInput] = Field(min_length=1)

    @model_validator(mode="after")
    def _answers_are_not_mixed_with_anything(self) -> "SendEventsRequest":
        """One batch is one call into the graph, and the two graph inputs are
        not interchangeable: a message starts or continues a turn, answers
        resume a turn already paused waiting for exactly them. Sent together,
        half the batch would have to be dropped whichever way we went.
        """
        kinds = {event.type for event in self.events}
        if kinds & set(_ANSWERS) and not kinds <= set(_ANSWERS):
            raise ValueError(
                "tool answers cannot be sent in the same batch as anything else"
            )
        return self


class SendEventsResponse(ApiModel):
    """The events as they were written. A refused batch never gets this far —
    it comes back as a 409 `SessionBusyError` with nothing appended."""

    data: list[EventResponse] = Field(default_factory=list)


class ListEventsResponse(ListResponse[EventResponse]):
    """One page of the log, and where the log ended when it was read.

    A window has to be aimed before it is fetched, so a client paging
    backwards aims with its own idea of the end — which is stale exactly when
    a turn ran while it was not watching. `last_event_seq` makes that visible
    in the same response instead of costing a `GET /v1/sessions/{id}` first,
    and it is free to say: the ownership check this endpoint already performs
    has the session row in hand.
    """

    last_event_seq: int | None = None


# --- the log, read back ------------------------------------------------------
#
# A stored row is `type` plus a JSON blob. Turning one back into its own shape
# lives here rather than in the router, because this is the file that knows all
# fourteen of them and the router would only be guessing.

_EVENT_CLASSES = (
    UserMessageEvent,
    UserInterruptEvent,
    UserToolConfirmationEvent,
    UserCustomToolResultEvent,
    AgentMessageEvent,
    AgentThinkingEvent,
    AgentToolUseEvent,
    AgentCustomToolUseEvent,
    AgentToolResultEvent,
    SessionStatusRunningEvent,
    SessionStatusIdleEvent,
    SessionStatusTerminatedEvent,
    SessionErrorEvent,
)

_BY_TYPE = {cls.model_fields["type"].default: cls for cls in _EVENT_CLASSES}

def from_row(row: Any) -> EventResponse:
    """Rebuild a stored event. An unknown `type` is an error, not a blank event.

    A row we cannot name is a row written by code that no longer agrees with
    this file, and handing the client something empty would hide that until
    someone noticed a gap in a transcript.

    The payload is passed through whole: what the log holds is what the client
    sees, with nothing kept back.
    """
    model = _BY_TYPE.get(row.type)
    if model is None:
        raise ValueError(f"Unknown session event type {row.type!r}")
    return model(id=row.id, seq=row.seq, processed_at=row.created_at, **(row.payload or {}))
