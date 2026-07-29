"""Canonical schema definitions, mirroring Claude Managed Agents (CMA).

Two submodules:

    app.schema.session  session.status + stop_reason.type constants
    app.schema.events   event type constants, grouped by {domain}.* namespace

This package is a clean-room definition sourced from the official CMA docs.
It is not wired into the running app yet -- app/session_state.py and
app/event_validation.py are still what the live code imports.
"""

from app.schema import events, session

__all__ = ["events", "session"]
