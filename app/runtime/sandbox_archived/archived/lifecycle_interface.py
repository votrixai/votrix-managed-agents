"""Session-scoped sandbox orchestration: DB-aware create/reconnect.

Interface only. These are the only two functions that know about VMA
sessions/organizations/the database; ``Sandbox`` itself (client.py) does not.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.runtime.sandbox.client import Sandbox


async def provision_session_sandbox(db: AsyncSession, session) -> str:
    """Create a sandbox for this session and remember its id. Called once per session."""
    ...


async def open_session_sandbox(db: AsyncSession, session) -> Sandbox:
    """Reconnect to this session's existing sandbox. Called once per turn."""
    ...


__all__ = ["open_session_sandbox", "provision_session_sandbox"]
