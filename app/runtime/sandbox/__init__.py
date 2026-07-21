"""Sandbox subsystem: create/connect/orchestrate E2B sandboxes for sessions."""

from app.runtime.sandbox.backend import deep_agents_backend
from app.runtime.sandbox.client import Sandbox
from app.runtime.sandbox.lifecycle import open_session_sandbox, provision_session_sandbox

__all__ = [
    "Sandbox",
    "deep_agents_backend",
    "open_session_sandbox",
    "provision_session_sandbox",
]
