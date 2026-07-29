from __future__ import annotations

from typing import Any

SESSION_IDLE = "idle"
SESSION_RUNNING = "running"
SESSION_RESCHEDULING = "rescheduling"
SESSION_TERMINATED = "terminated"

TERMINAL_STATUSES = {SESSION_TERMINATED}
ACTIVE_STATUSES = {SESSION_RUNNING, SESSION_RESCHEDULING}
RUNNABLE_STATUSES = {SESSION_IDLE, SESSION_RESCHEDULING}

USER_EVENTS_THAT_START_WORK = {"user.message", "user.define_outcome"}
ACTION_RESULT_EVENTS = {"user.custom_tool_result", "user.tool_confirmation", "user.tool_result"}


def stop_reason_type(stop_reason: dict[str, Any] | None) -> str | None:
    if not isinstance(stop_reason, dict):
        return None
    value = stop_reason.get("type")
    return str(value) if value is not None else None


def is_waiting_for_action(stop_reason: dict[str, Any] | None) -> bool:
    return stop_reason_type(stop_reason) == "requires_action"


def pending_action_ids(stop_reason: dict[str, Any] | None) -> set[str]:
    if not is_waiting_for_action(stop_reason):
        return set()
    raw_ids = stop_reason.get("event_ids") if isinstance(stop_reason, dict) else None
    if not isinstance(raw_ids, list):
        return set()
    return {str(item) for item in raw_ids if item is not None}


def blocks_mutation(status: str) -> bool:
    return status in ACTIVE_STATUSES


def can_start_work(status: str, stop_reason: dict[str, Any] | None) -> bool:
    return status in RUNNABLE_STATUSES and not is_waiting_for_action(stop_reason)


def starts_work(event_type: str) -> bool:
    return event_type in USER_EVENTS_THAT_START_WORK


def is_action_result(event_type: str) -> bool:
    return event_type in ACTION_RESULT_EVENTS
