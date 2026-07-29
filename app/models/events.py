"""Session event shapes and the event type names.

Namespaces follow the `{domain}.{action}` convention:

    user.*     client -> agent
    system.*   client -> agent, appended context (does not start a run)
    agent.*    agent -> client
    session.*  session lifecycle
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import ApiModel

# --- user.* : client -> agent ----------------------------------------------
#
# Everything here needs a free session and is refused with 409 while one is
# running — except USER_INTERRUPT, whose whole job is to reach a busy session.

USER_MESSAGE = "user.message"
USER_INTERRUPT = "user.interrupt"
USER_TOOL_CONFIRMATION = "user.tool_confirmation"
USER_CUSTOM_TOOL_RESULT = "user.custom_tool_result"
USER_TOOL_RESULT = "user.tool_result"

# Answers to something a previous turn stopped to ask. Their presence is what
# tells the engine this turn resumes a paused graph rather than starting one.
ACTION_RESULT_EVENTS = [
    USER_TOOL_CONFIRMATION,
    USER_CUSTOM_TOOL_RESULT,
    USER_TOOL_RESULT,
]


def is_action_result(event_type: str) -> bool:
    return event_type in ACTION_RESULT_EVENTS

# --- system.* : client -> agent, appended context ---------------------------

SYSTEM_MESSAGE = "system.message"

# --- agent.* : agent -> client ----------------------------------------------

AGENT_MESSAGE = "agent.message"
AGENT_THINKING = "agent.thinking"
AGENT_TOOL_USE = "agent.tool_use"
AGENT_TOOL_RESULT = "agent.tool_result"
AGENT_CUSTOM_TOOL_USE = "agent.custom_tool_use"

# --- session.* : lifecycle ---------------------------------------------------

SESSION_STATUS_RUNNING = "session.status_running"
SESSION_STATUS_IDLE = "session.status_idle"
SESSION_STATUS_RESCHEDULED = "session.status_rescheduled"
SESSION_STATUS_TERMINATED = "session.status_terminated"
SESSION_ERROR = "session.error"

# --- shapes ------------------------------------------------------------------


class EventInput(ApiModel):
    """One event a client appends to a session."""

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SendEventsRequest(ApiModel):
    events: list[EventInput] = Field(min_length=1)


class EventResponse(ApiModel):
    id: str
    type: Literal["session_event"] = "session_event"
    session_id: str
    seq: int
    event_type: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SendEventsResponse(ApiModel):
    """The events as they were written. A refused batch never gets this far —
    it comes back as a 409 `SessionBusyError` with nothing appended."""

    data: list[EventResponse] = Field(default_factory=list)
