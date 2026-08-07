import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    MEMBER_ROLE_ADMIN,
    MEMBER_ROLE_MEMBER,
    MEMBER_ROLE_OWNER,
)
from app.db.queries import organizations


async def test_organization_members_are_role_based_and_tenant_scoped(db, org):
    owner = await organizations.add_member(
        db,
        organization_id=org,
        user_id="user_owner",
        email="owner@example.com",
        role=MEMBER_ROLE_OWNER,
    )
    admin = await organizations.add_member(
        db,
        organization_id=org,
        user_id="user_admin",
        role=MEMBER_ROLE_ADMIN,
    )
    await db.commit()

    assert owner.id.startswith("member_")
    assert owner.role == MEMBER_ROLE_OWNER
    assert await organizations.get_member(
        db,
        organization_id=org,
        user_id=owner.user_id,
    ) is owner
    assert (
        await organizations.get_member(
            db,
            organization_id="org_someone_else",
            user_id=owner.user_id,
        )
        is None
    )
    assert await organizations.list_members(db, organization_id=org) == [owner, admin]
    assert await organizations.list_members(
        db,
        organization_id=org,
        role=MEMBER_ROLE_ADMIN,
    ) == [admin]

    await organizations.update_member_role(db, admin, role=MEMBER_ROLE_MEMBER)
    assert admin.role == MEMBER_ROLE_MEMBER
    await organizations.delete_member(db, admin)
    assert await organizations.list_members(db, organization_id=org) == [owner]


async def test_organization_member_role_is_validated(db, org):
    with pytest.raises(ValueError, match="role must be one of"):
        await organizations.add_member(
            db,
            organization_id=org,
            user_id="user_bad_role",
            role="superadmin",
        )


async def test_one_membership_per_organization_and_user(db, org):
    await organizations.add_member(
        db,
        organization_id=org,
        user_id="user_duplicate",
        role=MEMBER_ROLE_OWNER,
    )
    await db.commit()

    with pytest.raises(IntegrityError):
        await organizations.add_member(
            db,
            organization_id=org,
            user_id="user_duplicate",
            role=MEMBER_ROLE_MEMBER,
        )
    await db.rollback()
