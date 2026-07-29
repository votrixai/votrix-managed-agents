from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ManagedSession, SessionSandbox
from app.ids import new_id
from app.organization import resolve_organization_id


class SessionSandboxSessionNotFoundError(RuntimeError):
    """Raised when the parent session is outside the requested organization."""


class SessionSandboxLockConflictError(RuntimeError):
    """Raised when an optimistic sandbox state transition loses a race."""


class _Unset:
    pass


_UNSET = _Unset()


async def get_session_sandbox(
    db: AsyncSession,
    session_id: str,
    *,
    organization_id: str | None = None,
    for_update: bool = False,
) -> SessionSandbox | None:
    """Return the one sandbox belonging to a tenant-scoped session."""
    scoped_organization_id = resolve_organization_id(organization_id)
    parent = await _get_scoped_session(
        db,
        session_id=session_id,
        organization_id=scoped_organization_id,
        for_update=for_update,
    )
    if parent is None:
        return None

    stmt = (
        select(SessionSandbox)
        .where(
            SessionSandbox.organization_id == scoped_organization_id,
            SessionSandbox.session_id == session_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_session_sandbox_by_external_id(
    db: AsyncSession,
    *,
    provider: str,
    external_sandbox_id: str,
    organization_id: str | None = None,
    for_update: bool = False,
) -> SessionSandbox | None:
    """Resolve a provider ID without allowing it to cross organization scope."""
    scoped_organization_id = resolve_organization_id(organization_id)
    stmt = (
        select(SessionSandbox)
        .where(
            SessionSandbox.organization_id == scoped_organization_id,
            SessionSandbox.provider == provider,
            SessionSandbox.external_sandbox_id == external_sandbox_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        candidate = (await db.execute(stmt)).scalar_one_or_none()
        if candidate is None:
            return None
        parent = await _get_scoped_session(
            db,
            session_id=candidate.session_id,
            organization_id=scoped_organization_id,
            for_update=True,
        )
        if parent is None:
            return None
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def upsert_session_sandbox(
    db: AsyncSession,
    *,
    session_id: str,
    provider: str,
    state: str = "provisioning",
    external_sandbox_id: str | None = None,
    template_id: str | None = None,
    region: str | None = None,
    config: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    last_active_at: datetime | None = None,
    expires_at: datetime | None = None,
    organization_id: str | None = None,
) -> SessionSandbox:
    """Create or update the single provider record for a session.

    The parent session is tenant-scoped and locked before the child row is
    written. The database uniqueness constraint remains the final guard against
    two workers creating a second row for the same session.
    """
    scoped_organization_id = resolve_organization_id(organization_id)
    await _require_scoped_session(
        db,
        session_id=session_id,
        organization_id=scoped_organization_id,
        for_update=True,
    )

    now = datetime.now(timezone.utc)
    values = {
        "id": new_id("sbx"),
        "organization_id": scoped_organization_id,
        "session_id": session_id,
        "provider": provider,
        "external_sandbox_id": external_sandbox_id,
        "state": state,
        "template_id": template_id,
        "region": region,
        "config": dict(config or {}),
        "capabilities": dict(capabilities or {}),
        "error": error,
        "last_active_at": last_active_at,
        "expires_at": expires_at,
        "state_changed_at": now,
        "lock_version": 0,
        "created_at": now,
        "updated_at": now,
    }

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(
            f"Session sandbox upsert is not supported for database dialect {dialect!r}"
        )

    insert_stmt = insert(SessionSandbox).values(**values)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=[SessionSandbox.organization_id, SessionSandbox.session_id],
        set_={
            "provider": insert_stmt.excluded.provider,
            "external_sandbox_id": insert_stmt.excluded.external_sandbox_id,
            "state": insert_stmt.excluded.state,
            "template_id": insert_stmt.excluded.template_id,
            "region": insert_stmt.excluded.region,
            "config": insert_stmt.excluded.config,
            "capabilities": insert_stmt.excluded.capabilities,
            "error": insert_stmt.excluded.error,
            "last_active_at": insert_stmt.excluded.last_active_at,
            "expires_at": insert_stmt.excluded.expires_at,
            "state_changed_at": now,
            "lock_version": SessionSandbox.lock_version + 1,
            "updated_at": now,
        },
    )
    await db.execute(stmt)

    sandbox = await get_session_sandbox(
        db,
        session_id,
        organization_id=scoped_organization_id,
        for_update=True,
    )
    if sandbox is None:  # pragma: no cover - guarded by insert + unique key
        raise RuntimeError("Session sandbox upsert did not return a record")
    return sandbox


async def update_session_sandbox_state(
    db: AsyncSession,
    sandbox: SessionSandbox,
    *,
    state: str,
    expected_lock_version: int | None = None,
    external_sandbox_id: str | None | _Unset = _UNSET,
    template_id: str | None | _Unset = _UNSET,
    region: str | None | _Unset = _UNSET,
    config: dict[str, Any] | _Unset = _UNSET,
    capabilities: dict[str, Any] | _Unset = _UNSET,
    error: dict[str, Any] | None | _Unset = _UNSET,
    last_active_at: datetime | None | _Unset = _UNSET,
    expires_at: datetime | None | _Unset = _UNSET,
    organization_id: str | None = None,
) -> SessionSandbox:
    """Apply one tenant-scoped lifecycle transition using optimistic locking."""
    scoped_organization_id = resolve_organization_id(organization_id)
    expected_version = (
        sandbox.lock_version if expected_lock_version is None else expected_lock_version
    )
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "state": state,
        "state_changed_at": now,
        "updated_at": now,
        "lock_version": SessionSandbox.lock_version + 1,
    }
    optional_values = {
        "external_sandbox_id": external_sandbox_id,
        "template_id": template_id,
        "region": region,
        "config": config,
        "capabilities": capabilities,
        "error": error,
        "last_active_at": last_active_at,
        "expires_at": expires_at,
    }
    values.update(
        {key: value for key, value in optional_values.items() if value is not _UNSET}
    )

    result = await db.execute(
        update(SessionSandbox)
        .where(
            SessionSandbox.id == sandbox.id,
            SessionSandbox.organization_id == scoped_organization_id,
            SessionSandbox.session_id == sandbox.session_id,
            SessionSandbox.lock_version == expected_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise SessionSandboxLockConflictError(
            f"Session sandbox {sandbox.id} changed after lock version {expected_version}"
        )
    await db.refresh(sandbox)
    return sandbox


async def list_expired_session_sandboxes_for_cleanup(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    provider: str | None = None,
    limit: int = 50,
) -> list[SessionSandbox]:
    """List safe cleanup candidates for the trusted cross-organization janitor."""
    cutoff = now or datetime.now(timezone.utc)
    stmt = select(SessionSandbox).where(
        SessionSandbox.external_sandbox_id.is_not(None),
        SessionSandbox.expires_at.is_not(None),
        SessionSandbox.expires_at <= cutoff,
        SessionSandbox.state.in_({"idle", "paused", "error"}),
    )
    if provider is not None:
        stmt = stmt.where(SessionSandbox.provider == provider)
    stmt = stmt.order_by(
        SessionSandbox.expires_at.asc(), SessionSandbox.id.asc()
    ).limit(max(1, limit))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _get_scoped_session(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    for_update: bool = False,
) -> ManagedSession | None:
    stmt = select(ManagedSession).where(
        ManagedSession.id == session_id,
        ManagedSession.organization_id == organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt.execution_options(populate_existing=True))
    return result.scalar_one_or_none()


async def _require_scoped_session(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    for_update: bool = False,
) -> ManagedSession:
    session = await _get_scoped_session(
        db,
        session_id=session_id,
        organization_id=organization_id,
        for_update=for_update,
    )
    if session is None:
        raise SessionSandboxSessionNotFoundError(
            f"Session {session_id} was not found in organization {organization_id}"
        )
    return session


__all__ = [
    "SessionSandboxLockConflictError",
    "SessionSandboxSessionNotFoundError",
    "get_session_sandbox",
    "get_session_sandbox_by_external_id",
    "list_expired_session_sandboxes_for_cleanup",
    "update_session_sandbox_state",
    "upsert_session_sandbox",
]
