"""Session event-type schema, mirroring Claude Managed Agents (CMA).

Source: platform.claude.com/docs/en/managed-agents/events-and-streaming
(verified live during this session, cross-checked against the bundled CMA
reference docs). Clean-room definition -- does not carry over any
votrix-specific event types or drift found elsewhere in this codebase.

Namespaces follow the `{domain}.{action}` convention CMA uses:

    user.*    client -> agent
    system.*  client -> agent, appended context (not a turn-starter)
    agent.*   agent -> client
    session.* session/thread lifecycle
    span.*    observability (model calls, outcome grading)

Two frame types (event_start / event_delta) are stream-only previews: they
are never persisted, carry no `id`/`seq` of their own, and only exist on a
connection that opted in with `event_deltas`. They are listed separately at
the bottom, not mixed into the durable event sets above.
"""

# --- user.* : client -> agent ----------------------------------------------

USER_MESSAGE = "user.message"
USER_INTERRUPT = "user.interrupt"
USER_DEFINE_OUTCOME = "user.define_outcome"
USER_TOOL_CONFIRMATION = "user.tool_confirmation"
USER_CUSTOM_TOOL_RESULT = "user.custom_tool_result"
USER_TOOL_RESULT = "user.tool_result"
"""Result for a standard (agent_toolset) tool the client executed itself --
self-hosted-environment scenarios only."""

USER_EVENT_TYPES = frozenset({
    USER_MESSAGE,
    USER_INTERRUPT,
    USER_DEFINE_OUTCOME,
    USER_TOOL_CONFIRMATION,
    USER_CUSTOM_TOOL_RESULT,
    USER_TOOL_RESULT,
})

# Starts a fresh turn from an idle/rescheduling session.
STARTS_WORK = frozenset({USER_MESSAGE, USER_DEFINE_OUTCOME})

# Resolves a pending session.status_idle(stop_reason=requires_action).
ACTION_RESULT_EVENTS = frozenset({USER_TOOL_CONFIRMATION, USER_CUSTOM_TOOL_RESULT, USER_TOOL_RESULT})


# --- system.* : client -> agent, appended context ---------------------------

SYSTEM_MESSAGE = "system.message"

SYSTEM_EVENT_TYPES = frozenset({SYSTEM_MESSAGE})


# --- agent.* : agent -> client ----------------------------------------------

AGENT_MESSAGE = "agent.message"
AGENT_THINKING = "agent.thinking"
"""Progress signal that the agent is thinking -- carries no thinking content."""
AGENT_TOOL_USE = "agent.tool_use"
AGENT_TOOL_RESULT = "agent.tool_result"
AGENT_MCP_TOOL_USE = "agent.mcp_tool_use"
AGENT_MCP_TOOL_RESULT = "agent.mcp_tool_result"
AGENT_CUSTOM_TOOL_USE = "agent.custom_tool_use"
AGENT_THREAD_CONTEXT_COMPACTED = "agent.thread_context_compacted"
AGENT_THREAD_MESSAGE_SENT = "agent.thread_message_sent"
"""Multiagent only: this thread sent a message to another thread."""
AGENT_THREAD_MESSAGE_RECEIVED = "agent.thread_message_received"
"""Multiagent only: this thread received a message from another thread."""

AGENT_EVENT_TYPES = frozenset({
    AGENT_MESSAGE,
    AGENT_THINKING,
    AGENT_TOOL_USE,
    AGENT_TOOL_RESULT,
    AGENT_MCP_TOOL_USE,
    AGENT_MCP_TOOL_RESULT,
    AGENT_CUSTOM_TOOL_USE,
    AGENT_THREAD_CONTEXT_COMPACTED,
    AGENT_THREAD_MESSAGE_SENT,
    AGENT_THREAD_MESSAGE_RECEIVED,
})

# Tool-use events whose evaluated_permission can be "ask" and therefore pair
# with a user.tool_confirmation (agent.custom_tool_use always pairs with
# user.custom_tool_result instead -- it has no allow/deny concept).
CONFIRMABLE_TOOL_USE_EVENTS = frozenset({AGENT_TOOL_USE, AGENT_MCP_TOOL_USE, AGENT_CUSTOM_TOOL_USE})


# --- session.* : session / thread lifecycle ---------------------------------

SESSION_STATUS_RUNNING = "session.status_running"
SESSION_STATUS_IDLE = "session.status_idle"
SESSION_STATUS_RESCHEDULED = "session.status_rescheduled"
SESSION_STATUS_TERMINATED = "session.status_terminated"
SESSION_ERROR = "session.error"

# Multiagent only.
SESSION_THREAD_CREATED = "session.thread_created"
SESSION_THREAD_STATUS_RUNNING = "session.thread_status_running"
SESSION_THREAD_STATUS_IDLE = "session.thread_status_idle"
SESSION_THREAD_STATUS_RESCHEDULED = "session.thread_status_rescheduled"
SESSION_THREAD_STATUS_TERMINATED = "session.thread_status_terminated"

SESSION_EVENT_TYPES = frozenset({
    SESSION_STATUS_RUNNING,
    SESSION_STATUS_IDLE,
    SESSION_STATUS_RESCHEDULED,
    SESSION_STATUS_TERMINATED,
    SESSION_ERROR,
    SESSION_THREAD_CREATED,
    SESSION_THREAD_STATUS_RUNNING,
    SESSION_THREAD_STATUS_IDLE,
    SESSION_THREAD_STATUS_RESCHEDULED,
    SESSION_THREAD_STATUS_TERMINATED,
})


# --- span.* : observability --------------------------------------------------

SPAN_MODEL_REQUEST_START = "span.model_request_start"
SPAN_MODEL_REQUEST_END = "span.model_request_end"
SPAN_OUTCOME_EVALUATION_START = "span.outcome_evaluation_start"
SPAN_OUTCOME_EVALUATION_ONGOING = "span.outcome_evaluation_ongoing"
SPAN_OUTCOME_EVALUATION_END = "span.outcome_evaluation_end"

SPAN_EVENT_TYPES = frozenset({
    SPAN_MODEL_REQUEST_START,
    SPAN_MODEL_REQUEST_END,
    SPAN_OUTCOME_EVALUATION_START,
    SPAN_OUTCOME_EVALUATION_ONGOING,
    SPAN_OUTCOME_EVALUATION_END,
})


# --- stream-only preview frames (never persisted, no id/seq of their own) ---

EVENT_START = "event_start"
EVENT_DELTA = "event_delta"

PREVIEW_FRAME_TYPES = frozenset({EVENT_START, EVENT_DELTA})

# Durable event types a client may opt in to preview deltas for.
PREVIEWABLE_EVENT_TYPES = frozenset({AGENT_MESSAGE, AGENT_THINKING})


# --- everything durable, for a single "is this a known event type" check ---

ALL_EVENT_TYPES = frozenset(
    USER_EVENT_TYPES | SYSTEM_EVENT_TYPES | AGENT_EVENT_TYPES | SESSION_EVENT_TYPES | SPAN_EVENT_TYPES
)
