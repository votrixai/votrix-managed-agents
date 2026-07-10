from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agent, Environment, ManagedSession
from app.ids import new_id
from app.workspace import workspace_id_or_default


async def create_session(
    db: AsyncSession,
    *,
    agent: Agent,
    agent_version: int,
    environment: Environment,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
    resources: list[dict[str, Any]] | None = None,
    vault_ids: list[str] | None = None,
    workspace_id: str | None = None,
) -> ManagedSession:
    scoped_workspace_id = workspace_id_or_default(workspace_id or agent.workspace_id)
    session = ManagedSession(
        id=new_id("sess"),
        runtime_thread_id=new_id("thread"),
        workspace_id=scoped_workspace_id,
        agent_id=agent.id,
        agent_version=agent_version,
        environment_id=environment.id,
        title=title,
        status="idle",
        metadata_=metadata or {},
        status_details={
            "resources": resources or [],
            "vault_ids": vault_ids or [],
        },
        last_event_seq=0,
    )
    db.add(session)
    await db.flush()
    return session


async def get_session(
    db: AsyncSession,
    session_id: str,
    *,
    workspace_id: str | None = None,
    for_update: bool = False,
) -> ManagedSession | None:
    stmt = select(ManagedSession).where(ManagedSession.id == session_id)
    if workspace_id is not None:
        stmt = stmt.where(ManagedSession.workspace_id == workspace_id)
    else:
        stmt = stmt.where(ManagedSession.workspace_id == workspace_id_or_default())
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_sessions(
    db: AsyncSession,
    *,
    limit: int = 50,
    include_archived: bool = False,
    workspace_id: str | None = None,
) -> list[ManagedSession]:
    stmt = (
        select(ManagedSession)
        .where(
            ManagedSession.deleted_at.is_(None),
            ManagedSession.workspace_id == workspace_id_or_default(workspace_id),
        )
        .order_by(ManagedSession.created_at.desc())
        .limit(limit)
    )
    if not include_archived:
        stmt = stmt.where(ManagedSession.archived_at.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_session(
    db: AsyncSession,
    session: ManagedSession,
    *,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
    status: str | None = None,
    status_details: dict[str, Any] | None = None,
    stop_reason: dict[str, Any] | None = None,
    run_state: dict[str, Any] | None = None,
    sandbox_state: dict[str, Any] | None = None,
) -> ManagedSession:
    if title is not None:
        session.title = title
    if metadata is not None:
        session.metadata_ = metadata
    if status is not None:
        session.status = status
    if status_details is not None:
        session.status_details = status_details
    if stop_reason is not None:
        session.stop_reason = stop_reason
    if run_state is not None:
        session.run_state = run_state
    if sandbox_state is not None:
        session.sandbox_state = sandbox_state
    await db.flush()
    return session


async def archive_session(db: AsyncSession, session: ManagedSession) -> ManagedSession:
    session.archived_at = datetime.now(timezone.utc)
    await db.flush()
    return session


async def delete_session(db: AsyncSession, session: ManagedSession) -> None:
    session.deleted_at = datetime.now(timezone.utc)
    session.status = "terminated"
    await db.flush()
