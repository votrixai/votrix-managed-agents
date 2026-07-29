from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization, OrganizationOwner
from app.utils.id_generator import new_id


async def create_organization(db: AsyncSession, *, slug: str, name: str) -> Organization:
    organization = Organization(id=new_id("org"), slug=slug, name=name)
    db.add(organization)
    await db.flush()
    return organization


async def get_organization(db: AsyncSession, *, organization_id: str) -> Organization | None:
    return await db.get(Organization, organization_id)


async def get_organization_by_slug(db: AsyncSession, *, slug: str) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


async def list_organizations(
    db: AsyncSession,
    *,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Organization]:
    stmt = select(Organization)
    if not include_archived:
        stmt = stmt.where(Organization.archived_at.is_(None))
    stmt = stmt.order_by(Organization.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_organization(db: AsyncSession, organization: Organization, *, name: str) -> None:
    organization.name = name
    await db.flush()


async def archive_organization(db: AsyncSession, organization: Organization) -> None:
    organization.archived_at = datetime.now(timezone.utc)
    await db.flush()


async def add_owner(
    db: AsyncSession,
    *,
    organization_id: str,
    user_id: str,
    email: str | None = None,
) -> OrganizationOwner:
    owner = OrganizationOwner(
        id=new_id("owner"),
        organization_id=organization_id,
        user_id=user_id,
        email=email,
    )
    db.add(owner)
    await db.flush()
    return owner


async def get_owner(
    db: AsyncSession,
    *,
    organization_id: str,
    user_id: str,
) -> OrganizationOwner | None:
    result = await db.execute(
        select(OrganizationOwner).where(
            OrganizationOwner.organization_id == organization_id,
            OrganizationOwner.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def list_owners(db: AsyncSession, *, organization_id: str) -> list[OrganizationOwner]:
    result = await db.execute(
        select(OrganizationOwner)
        .where(OrganizationOwner.organization_id == organization_id)
        .order_by(OrganizationOwner.created_at)
    )
    return list(result.scalars().all())


async def delete_owner(db: AsyncSession, owner: OrganizationOwner) -> None:
    await db.delete(owner)
    await db.flush()
