from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization, OrganizationOwner
from app.ids import new_id


async def is_owner(db: AsyncSession, organization_id: str, user_id: str) -> bool:
    result = await db.execute(
        select(OrganizationOwner.id).where(
            OrganizationOwner.organization_id == organization_id,
            OrganizationOwner.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def list_user_organizations(
    db: AsyncSession, user_id: str
) -> list[tuple[Organization, OrganizationOwner]]:
    result = await db.execute(
        select(Organization, OrganizationOwner)
        .join(OrganizationOwner, OrganizationOwner.organization_id == Organization.id)
        .where(
            OrganizationOwner.user_id == user_id,
            Organization.archived_at.is_(None),
        )
        .order_by(Organization.name.asc())
    )
    return list(result.all())


async def list_owners(db: AsyncSession, organization_id: str) -> list[OrganizationOwner]:
    result = await db.execute(
        select(OrganizationOwner)
        .where(OrganizationOwner.organization_id == organization_id)
        .order_by(OrganizationOwner.created_at.asc())
    )
    return list(result.scalars().all())


async def add_owner(
    db: AsyncSession,
    *,
    organization_id: str,
    user_id: str,
    email: str | None,
    granted_by: str,
) -> OrganizationOwner:
    existing = await db.scalar(
        select(OrganizationOwner).where(
            OrganizationOwner.organization_id == organization_id,
            OrganizationOwner.user_id == user_id,
        )
    )
    if existing is not None:
        if email:
            existing.email = email
        return existing
    owner = OrganizationOwner(
        id=new_id("owner"),
        organization_id=organization_id,
        user_id=user_id,
        email=email,
        granted_by=granted_by,
    )
    db.add(owner)
    await db.flush()
    return owner


async def remove_owner(db: AsyncSession, organization_id: str, user_id: str) -> bool:
    owner = await db.scalar(
        select(OrganizationOwner).where(
            OrganizationOwner.organization_id == organization_id,
            OrganizationOwner.user_id == user_id,
        )
    )
    if owner is None:
        return False
    await db.delete(owner)
    await db.flush()
    return True
