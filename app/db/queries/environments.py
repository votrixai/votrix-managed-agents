from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Environment
from app.ids import new_id
from app.workspace import workspace_id_or_default


async def create_environment(
    db: AsyncSession,
    *,
    name: str,
    config: dict[str, Any],
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
    workspace_id: str | None = None,
) -> Environment:
    environment = Environment(
        id=new_id("env"),
        workspace_id=workspace_id_or_default(workspace_id),
        name=name,
        description=description or "",
        config=config,
        metadata_=metadata or {},
    )
    db.add(environment)
    await db.flush()
    return environment


async def get_environment(
    db: AsyncSession,
    environment_id: str,
    *,
    workspace_id: str | None = None,
) -> Environment | None:
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.workspace_id == workspace_id_or_default(workspace_id),
        )
    )
    return result.scalar_one_or_none()


async def list_environments(
    db: AsyncSession,
    *,
    limit: int = 50,
    include_archived: bool = False,
    workspace_id: str | None = None,
) -> list[Environment]:
    stmt = (
        select(Environment)
        .where(
            Environment.deleted_at.is_(None),
            Environment.workspace_id == workspace_id_or_default(workspace_id),
        )
        .order_by(Environment.created_at.desc())
        .limit(limit)
    )
    if not include_archived:
        stmt = stmt.where(Environment.archived_at.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_environment(
    db: AsyncSession,
    environment: Environment,
    *,
    name: str | None = None,
    config: dict[str, Any] | None = None,
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Environment:
    if name is not None:
        environment.name = name
    if description is not None:
        environment.description = description
    if config is not None:
        environment.config = config
    if metadata is not None:
        environment.metadata_ = metadata
    await db.flush()
    return environment


async def archive_environment(db: AsyncSession, environment: Environment) -> Environment:
    environment.archived_at = datetime.now(timezone.utc)
    await db.flush()
    return environment


async def delete_environment(db: AsyncSession, environment: Environment) -> None:
    environment.deleted_at = datetime.now(timezone.utc)
    await db.flush()
