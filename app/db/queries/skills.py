from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Skill
from app.db.queries import DEFAULT_PAGE_SIZE, Page, fetch_page
from app.utils.id_generator import new_id


async def create_skill(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    storage_key: str,
    description: str | None = None,
    size_bytes: int = 0,
    sha256: str | None = None,
) -> Skill:
    skill = Skill(
        id=new_id("skill"),
        organization_id=organization_id,
        name=name,
        description=description,
        storage_key=storage_key,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    db.add(skill)
    await db.flush()
    return skill


async def get_skill(db: AsyncSession, *, skill_id: str, organization_id: str) -> Skill | None:
    result = await db.execute(
        select(Skill).where(Skill.id == skill_id, Skill.organization_id == organization_id)
    )
    return result.scalar_one_or_none()


async def get_skill_by_name(db: AsyncSession, *, name: str, organization_id: str) -> Skill | None:
    result = await db.execute(
        select(Skill).where(Skill.name == name, Skill.organization_id == organization_id)
    )
    return result.scalar_one_or_none()


async def list_skills(
    db: AsyncSession,
    *,
    organization_id: str,
    include_archived: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    stmt = select(Skill).where(Skill.organization_id == organization_id)
    if not include_archived:
        stmt = stmt.where(Skill.archived_at.is_(None))
    return await fetch_page(
        db, stmt, sort=Skill.created_at, id_column=Skill.id,
        limit=limit, before_id=before_id, after_id=after_id,
    )


async def update_skill(
    db: AsyncSession,
    skill: Skill,
    *,
    name: str | None = None,
    description: str | None = None,
    storage_key: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
) -> None:
    if name is not None:
        skill.name = name
    if description is not None:
        skill.description = description
    if storage_key is not None:
        skill.storage_key = storage_key
    if size_bytes is not None:
        skill.size_bytes = size_bytes
    if sha256 is not None:
        skill.sha256 = sha256
    await db.flush()


async def archive_skill(db: AsyncSession, skill: Skill) -> None:
    skill.archived_at = datetime.now(timezone.utc)
    await db.flush()


async def delete_skill(db: AsyncSession, skill: Skill) -> None:
    await db.delete(skill)
    await db.flush()
