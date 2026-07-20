import pytest

from app.db.engine import session_scope
from app.db.models import Organization
from app.db.queries.organization_owners import add_owner
from app.human_auth import AuthenticatedUser


async def _user(user_id: str = "user_owner", *, super_admin: bool = False):
    return AuthenticatedUser(
        id=user_id,
        email=f"{user_id}@example.com",
        app_metadata={"super_admin": super_admin},
    )


@pytest.mark.anyio
async def test_owner_can_list_and_access_multiple_organizations(client, monkeypatch):
    async with session_scope() as db:
        for suffix in ("one", "two"):
            organization = Organization(
                id=f"org_hosted_{suffix}",
                slug=f"hosted-{suffix}",
                name=f"Hosted {suffix}",
                metadata_={},
            )
            db.add(organization)
            await db.flush()
            await add_owner(
                db,
                organization_id=organization.id,
                user_id="user_owner",
                email="owner@example.com",
                granted_by="user_superadmin",
            )
        await db.commit()

    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    response = await client.get(
        "/v1/me/organizations", headers={"authorization": "Bearer a.b.c"}
    )
    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == {"org_hosted_one", "org_hosted_two"}

    response = await client.get(
        "/v1/agents",
        headers={
            "authorization": "Bearer a.b.c",
            "x-organization-id": "org_hosted_one",
            "votrix-managed-agents-beta": "votrix-managed-agents-2026-04-01",
        },
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_non_owner_is_rejected_and_owner_cannot_manage_api_keys(client, monkeypatch):
    async with session_scope() as db:
        db.add(Organization(id="org_hosted_guard", slug="hosted-guard", name="Guard", metadata_={}))
        await db.commit()

    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    headers = {
        "authorization": "Bearer a.b.c",
        "x-organization-id": "org_hosted_guard",
        "votrix-managed-agents-beta": "votrix-managed-agents-2026-04-01",
    }
    assert (await client.get("/v1/agents", headers=headers)).status_code == 403

    async with session_scope() as db:
        await add_owner(
            db,
            organization_id="org_hosted_guard",
            user_id="user_owner",
            email=None,
            granted_by="user_superadmin",
        )
        await db.commit()
    response = await client.get("/v1/api_keys", headers=headers)
    assert response.status_code == 403


@pytest.mark.anyio
async def test_superadmin_can_list_and_access_all_organizations_without_membership(
    client, monkeypatch
):
    async with session_scope() as db:
        for suffix in ("all_one", "all_two"):
            db.add(
                Organization(
                    id=f"org_superadmin_{suffix}",
                    slug=f"superadmin-{suffix}",
                    name=f"Superadmin {suffix}",
                    metadata_={},
                )
            )
        await db.commit()

    monkeypatch.setattr(
        "app.human_auth.authenticate_user",
        lambda _token: _user("user_superadmin", super_admin=True),
    )
    response = await client.get(
        "/v1/me/organizations", headers={"authorization": "Bearer a.b.c"}
    )
    assert response.status_code == 200
    assert {"org_superadmin_all_one", "org_superadmin_all_two"}.issubset(
        {item["id"] for item in response.json()}
    )

    response = await client.get(
        "/v1/agents",
        headers={
            "authorization": "Bearer a.b.c",
            "x-organization-id": "org_superadmin_all_one",
            "votrix-managed-agents-beta": "votrix-managed-agents-2026-04-01",
        },
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_superadmin_creates_organization_owner_and_api_key(client, monkeypatch):
    monkeypatch.setattr(
        "app.human_auth.authenticate_user",
        lambda _token: _user("user_superadmin", super_admin=True),
    )
    headers = {"authorization": "Bearer a.b.c"}
    response = await client.post(
        "/internal/organizations",
        headers=headers,
        json={"id": "org_admin_created", "slug": "admin-created", "name": "Admin created"},
    )
    assert response.status_code == 201

    response = await client.post(
        "/internal/organizations/org_admin_created/owners",
        headers=headers,
        json={"user_id": "user_owner", "email": "OWNER@EXAMPLE.COM"},
    )
    assert response.status_code == 201
    assert response.json()["email"] == "owner@example.com"

    response = await client.post(
        "/internal/organizations/org_admin_created/api-keys",
        headers=headers,
        json={"name": "Builder", "scopes": ["api"]},
    )
    assert response.status_code == 201
    assert response.json()["secret"].startswith("vma_")
    assert response.json()["scopes"] == ["api"]
    key_id = response.json()["id"]

    response = await client.post(
        f"/internal/organizations/org_admin_created/api-keys/{key_id}/rotate",
        headers=headers,
    )
    assert response.status_code == 201
    replacement_id = response.json()["id"]
    assert replacement_id != key_id

    response = await client.post(
        f"/internal/organizations/org_admin_created/api-keys/{replacement_id}/revoke",
        headers=headers,
        json={"reason": "Integration retired"},
    )
    assert response.status_code == 200
    assert response.json()["revoked_at"] is not None
