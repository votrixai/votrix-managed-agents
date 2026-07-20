from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import Organization
from app.db.queries import api_keys as api_keys_q
from app.db.queries import organization_owners as owners_q
from app.human_auth import AuthenticatedUser, require_super_admin, require_user
from app.models.api_keys import ApiKeyResponse, api_key_to_response
from app.organization import resolve_organization_id


class OrganizationResponse(BaseModel):
    id: str
    slug: str
    name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class OwnerResponse(BaseModel):
    id: str
    organization_id: str
    user_id: str
    email: str | None
    granted_by: str
    created_at: datetime


class OrganizationCreateRequest(BaseModel):
    id: str
    slug: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OwnerCreateRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=320)


class AdminApiKeyCreateRequest(BaseModel):
    name: str = Field(default="Builder service", min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=lambda: [api_keys_q.API_SCOPE])
    expires_at: datetime | None = None


class AdminApiKeyRevokeRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class AdminApiKeyCreatedResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    prefix: str
    scopes: list[str]
    secret: str
    created_at: datetime


me_router = APIRouter(prefix="/v1/me", tags=["hosted organizations"], include_in_schema=False)
admin_router = APIRouter(
    prefix="/internal/organizations",
    tags=["private organization administration"],
    dependencies=[Depends(require_super_admin)],
    include_in_schema=False,
)


def _organization_response(organization: Organization) -> OrganizationResponse:
    return OrganizationResponse(
        id=organization.id,
        slug=organization.slug,
        name=organization.name,
        metadata=organization.metadata_,
        created_at=organization.created_at,
        updated_at=organization.updated_at,
    )


def _owner_response(owner) -> OwnerResponse:
    return OwnerResponse(
        id=owner.id,
        organization_id=owner.organization_id,
        user_id=owner.user_id,
        email=owner.email,
        granted_by=owner.granted_by,
        created_at=owner.created_at,
    )


@me_router.get("/organizations", response_model=list[OrganizationResponse])
async def my_organizations(
    user: AuthenticatedUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    if user.is_super_admin:
        return [
            _organization_response(organization)
            for organization in await owners_q.list_all_organizations(db)
        ]
    rows = await owners_q.list_user_organizations(db, user.id)
    return [_organization_response(organization) for organization, _owner in rows]


@admin_router.post("", response_model=OrganizationResponse, status_code=201)
async def create_organization(
    body: OrganizationCreateRequest,
    db: AsyncSession = Depends(get_session),
):
    organization = Organization(
        id=resolve_organization_id(body.id),
        slug=body.slug.strip(),
        name=body.name.strip(),
        metadata_=body.metadata,
    )
    db.add(organization)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Organization id or slug already exists") from exc
    await db.refresh(organization)
    return _organization_response(organization)


@admin_router.get("/{organization_id}/owners", response_model=list[OwnerResponse])
async def list_organization_owners(
    organization_id: str,
    db: AsyncSession = Depends(get_session),
):
    organization_id = resolve_organization_id(organization_id)
    if await db.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return [_owner_response(owner) for owner in await owners_q.list_owners(db, organization_id)]


@admin_router.post("/{organization_id}/owners", response_model=OwnerResponse, status_code=201)
async def add_organization_owner(
    organization_id: str,
    body: OwnerCreateRequest,
    super_admin: AuthenticatedUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_session),
):
    organization_id = resolve_organization_id(organization_id)
    if await db.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    owner = await owners_q.add_owner(
        db,
        organization_id=organization_id,
        user_id=body.user_id.strip(),
        email=body.email.strip().lower() if body.email else None,
        granted_by=super_admin.id,
    )
    await db.commit()
    await db.refresh(owner)
    return _owner_response(owner)


@admin_router.delete("/{organization_id}/owners/{user_id}", status_code=204)
async def remove_organization_owner(
    organization_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_session),
):
    removed = await owners_q.remove_owner(db, resolve_organization_id(organization_id), user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Owner not found")
    await db.commit()


@admin_router.post(
    "/{organization_id}/api-keys",
    response_model=AdminApiKeyCreatedResponse,
    status_code=201,
)
async def create_organization_api_key(
    organization_id: str,
    body: AdminApiKeyCreateRequest,
    super_admin: AuthenticatedUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_session),
):
    organization_id = resolve_organization_id(organization_id)
    if await db.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    try:
        scopes = api_keys_q.normalize_api_key_scopes(body.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    key, secret = await api_keys_q.create_api_key(
        db,
        organization_id=organization_id,
        name=body.name,
        scopes=scopes,
        expires_at=body.expires_at,
        created_by=super_admin.id,
        metadata={"provisioned_by": "superadmin_control_plane"},
    )
    await db.commit()
    return AdminApiKeyCreatedResponse(
        id=key.id,
        organization_id=key.organization_id,
        name=key.name,
        prefix=key.prefix,
        scopes=key.scopes,
        secret=secret,
        created_at=key.created_at,
    )


@admin_router.get("/{organization_id}/api-keys", response_model=list[ApiKeyResponse])
async def list_organization_api_keys(
    organization_id: str,
    db: AsyncSession = Depends(get_session),
):
    keys = await api_keys_q.list_api_keys(
        db, organization_id=resolve_organization_id(organization_id), include_revoked=True
    )
    return [api_key_to_response(key) for key in keys]


@admin_router.post(
    "/{organization_id}/api-keys/{key_id}/revoke",
    response_model=ApiKeyResponse,
)
async def revoke_organization_api_key(
    organization_id: str,
    key_id: str,
    body: AdminApiKeyRevokeRequest | None = None,
    super_admin: AuthenticatedUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_session),
):
    key = await api_keys_q.get_api_key(
        db,
        key_id,
        organization_id=resolve_organization_id(organization_id),
        for_update=True,
    )
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    await api_keys_q.revoke_api_key(
        db,
        key,
        revoked_by=super_admin.id,
        reason=body.reason if body else None,
    )
    await db.commit()
    return api_key_to_response(key)


@admin_router.post(
    "/{organization_id}/api-keys/{key_id}/rotate",
    response_model=AdminApiKeyCreatedResponse,
    status_code=201,
)
async def rotate_organization_api_key(
    organization_id: str,
    key_id: str,
    super_admin: AuthenticatedUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_session),
):
    key = await api_keys_q.get_api_key(
        db,
        key_id,
        organization_id=resolve_organization_id(organization_id),
        for_update=True,
    )
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if key.revoked_at is not None or key.archived_at is not None:
        raise HTTPException(status_code=409, detail="Revoked API keys cannot be rotated")
    replacement, secret = await api_keys_q.create_api_key(
        db,
        organization_id=key.organization_id,
        name=key.name,
        scopes=key.scopes,
        expires_at=key.expires_at,
        created_by=super_admin.id,
        replaces_key_id=key.id,
        metadata=dict(key.metadata_ or {}),
    )
    await api_keys_q.revoke_api_key(
        db,
        key,
        revoked_by=super_admin.id,
        reason="Rotated by superadmin",
        replaced_by_key_id=replacement.id,
    )
    await db.commit()
    return AdminApiKeyCreatedResponse(
        id=replacement.id,
        organization_id=replacement.organization_id,
        name=replacement.name,
        prefix=replacement.prefix,
        scopes=replacement.scopes,
        secret=secret,
        created_at=replacement.created_at,
    )
