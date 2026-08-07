from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MEMBER_ROLE_MEMBER,
    MEMBER_ROLES,
    Organization,
    OrganizationMember,
)
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


def normalize_member_role(role: str) -> str:
    normalized = str(role).strip().lower()
    if normalized not in MEMBER_ROLES:
        expected = ", ".join(sorted(MEMBER_ROLES))
        raise ValueError(f"role must be one of: {expected}")
    return normalized


async def add_member(
    db: AsyncSession,
    *,
    organization_id: str,
    user_id: str,
    email: str | None = None,
    role: str = MEMBER_ROLE_MEMBER,
) -> OrganizationMember:
    member = OrganizationMember(
        id=new_id("member"),
        organization_id=organization_id,
        user_id=user_id,
        email=email,
        role=normalize_member_role(role),
    )
    db.add(member)
    await db.flush()
    return member


async def get_member(
    db: AsyncSession,
    *,
    organization_id: str,
    user_id: str,
) -> OrganizationMember | None:
    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def list_members(
    db: AsyncSession,
    *,
    organization_id: str,
    role: str | None = None,
) -> list[OrganizationMember]:
    stmt = select(OrganizationMember).where(
        OrganizationMember.organization_id == organization_id
    )
    if role is not None:
        stmt = stmt.where(OrganizationMember.role == normalize_member_role(role))
    result = await db.execute(
        stmt.order_by(OrganizationMember.created_at, OrganizationMember.id)
    )
    return list(result.scalars().all())


async def update_member_role(
    db: AsyncSession,
    member: OrganizationMember,
    *,
    role: str,
) -> OrganizationMember:
    member.role = normalize_member_role(role)
    await db.flush()
    return member


async def delete_member(db: AsyncSession, member: OrganizationMember) -> None:
    await db.delete(member)
    await db.flush()
