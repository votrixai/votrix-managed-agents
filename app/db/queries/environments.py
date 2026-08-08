from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Environment, Session
from app.db.models.environments import READY
from app.db.queries import DEFAULT_PAGE_SIZE, Page, fetch_page
from app.utils.id_generator import new_id


async def create_environment(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    description: str | None = None,
    config: dict[str, Any] | None = None,
    image_id: str | None = None,
    build_state: str = READY,
    build_id: str | None = None,
) -> Environment:
    environment = Environment(
        id=new_id("env"),
        organization_id=organization_id,
        name=name,
        description=description,
        config=config or {},
        image_id=image_id,
        build_state=build_state,
        build_id=build_id,
    )
    db.add(environment)
    await db.flush()
    return environment


async def count_sessions_using(db: AsyncSession, *, environment_id: str) -> int:
    """How many sessions still point at this environment."""
    result = await db.execute(
        select(func.count())
        .select_from(Session)
        .where(Session.environment_id == environment_id, Session.deleted_at.is_(None))
    )
    return int(result.scalar_one())


async def set_build(
    db: AsyncSession,
    environment: Environment,
    *,
    state: str,
    image_id: str | None = None,
    build_id: str | None = None,
    error: str | None = None,
) -> None:
    environment.build_state = state
    environment.build_error = error
    if image_id is not None:
        environment.image_id = image_id
    if build_id is not None:
        environment.build_id = build_id
    await db.flush()


async def get_environment(
    db: AsyncSession,
    *,
    environment_id: str,
    organization_id: str,
) -> Environment | None:
    result = await db.execute(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def list_environments(
    db: AsyncSession,
    *,
    organization_id: str,
    include_archived: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    stmt = select(Environment).where(Environment.organization_id == organization_id)
    if not include_archived:
        stmt = stmt.where(Environment.archived_at.is_(None))
    return await fetch_page(
        db, stmt, sort=Environment.created_at, id_column=Environment.id,
        limit=limit, before_id=before_id, after_id=after_id,
    )


async def update_environment(
    db: AsyncSession,
    environment: Environment,
    *,
    name: str | None = None,
    description: str | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    if name is not None:
        environment.name = name
    if description is not None:
        environment.description = description
    if config is not None:
        environment.config = config
    await db.flush()


async def archive_environment(db: AsyncSession, environment: Environment) -> None:
    environment.archived_at = datetime.now(timezone.utc)
    await db.flush()


async def delete_environment(db: AsyncSession, environment: Environment) -> None:
    await db.delete(environment)
    await db.flush()
