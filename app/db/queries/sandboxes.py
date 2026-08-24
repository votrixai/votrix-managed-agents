from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Sandbox
from app.db.models.sandboxes import SANDBOX_RUNNING
from app.db.queries import DEFAULT_PAGE_SIZE, Page, fetch_page
from app.utils.id_generator import new_id


def _expiry(ttl_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)


async def create_sandbox(
    db: AsyncSession,
    *,
    organization_id: str,
    environment_id: str,
    ttl_seconds: int,
    network_access: bool,
    provider: str = "e2b",
    external_sandbox_id: str | None = None,
    state: str = SANDBOX_RUNNING,
    error: dict[str, Any] | None = None,
) -> Sandbox:
    """A container held directly by its caller — no session owns it."""

    now = datetime.now(timezone.utc)
    sandbox = Sandbox(
        id=new_id("sbx"),
        organization_id=organization_id,
        session_id=None,
        environment_id=environment_id,
        provider=provider,
        external_sandbox_id=external_sandbox_id,
        state=state,
        ttl_seconds=ttl_seconds,
        expires_at=_expiry(ttl_seconds),
        last_active_at=now,
        network_access=network_access,
        error=error,
    )
    db.add(sandbox)
    await db.flush()
    return sandbox


async def get_sandbox(
    db: AsyncSession,
    *,
    sandbox_id: str,
    organization_id: str,
) -> Sandbox | None:
    result = await db.execute(
        select(Sandbox).where(
            Sandbox.id == sandbox_id,
            Sandbox.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def list_sandboxes(
    db: AsyncSession,
    *,
    organization_id: str,
    state: str | None = None,
    include_session_owned: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    """This tenant's containers.

    Session-owned ones are left out unless asked for: the caller cannot end
    them here, and a listing full of rows it may not act on is noise. They are
    listed properly under `/v1/sessions`.
    """
    stmt = select(Sandbox).where(Sandbox.organization_id == organization_id)
    if not include_session_owned:
        stmt = stmt.where(Sandbox.session_id.is_(None))
    if state is not None:
        stmt = stmt.where(Sandbox.state == state)
    return await fetch_page(
        db,
        stmt,
        sort=Sandbox.created_at,
        id_column=Sandbox.id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def touch(db: AsyncSession, sandbox: Sandbox) -> Sandbox:
    """Record that the container was just used, and push its pause out.

    Every call renews the container at the provider, so the row has to say the
    same thing or the two would disagree about when it goes to sleep — and the
    row is what the caller reads.
    """
    now = datetime.now(timezone.utc)
    sandbox.last_active_at = now
    sandbox.expires_at = now + timedelta(seconds=sandbox.ttl_seconds)
    await db.flush()
    return sandbox


async def set_state(
    db: AsyncSession,
    sandbox: Sandbox,
    *,
    state: str,
    error: dict[str, Any] | None = None,
) -> Sandbox:
    sandbox.state = state
    if error is not None:
        sandbox.error = error
    await db.flush()
    return sandbox
