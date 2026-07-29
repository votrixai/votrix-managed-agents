"""Session state schema, mirroring Claude Managed Agents (CMA).

Source: platform.claude.com/docs/en/managed-agents/sessions and
platform.claude.com/docs/en/managed-agents/events-and-streaming (verified
live during this session). This module is a clean-room definition based on
the official CMA contract -- it intentionally does not carry over any
votrix-specific extensions (e.g. extra stop_reason values) that exist
elsewhere in this codebase.

Session lifecycle (one-way except idle <-> running):

    rescheduling -> running <-> idle -> terminated
"""

# --- session.status -------------------------------------------------------

IDLE = "idle"
RUNNING = "running"
RESCHEDULING = "rescheduling"
TERMINATED = "terminated"

STATUSES = frozenset({IDLE, RUNNING, RESCHEDULING, TERMINATED})

# Sessions in these statuses are actively doing work; new events other than
# an interrupt are rejected while a session is in one of these.
ACTIVE_STATUSES = frozenset({RUNNING, RESCHEDULING})

# Sessions in these statuses can be handed a user.message / user.define_outcome
# to start a new turn.
RUNNABLE_STATUSES = frozenset({IDLE, RESCHEDULING})

TERMINAL_STATUSES = frozenset({TERMINATED})


# --- stop_reason.type on a session.status_idle event -----------------------
# CMA-confirmed values (client-patterns / events-and-streaming docs):

STOP_END_TURN = "end_turn"
"""Normal completion -- the agent finished the turn on its own."""

STOP_REQUIRES_ACTION = "requires_action"
"""Blocked on a client-side event: user.tool_confirmation, user.custom_tool_result,
or user.tool_result. The blocking event IDs are carried in stop_reason.event_ids."""

STOP_RETRIES_EXHAUSTED = "retries_exhausted"
"""Terminal failure after retryable errors were exhausted. Sourced from cached
CMA client-pattern docs; not independently re-verified against a live fetch in
this session -- double check against platform.claude.com before relying on it
for anything user-facing."""

STOP_REASONS = frozenset({STOP_END_TURN, STOP_REQUIRES_ACTION, STOP_RETRIES_EXHAUSTED})
